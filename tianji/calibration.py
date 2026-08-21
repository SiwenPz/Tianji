"""动态活性阈值与 expect_min 校准(票 07,规格书 7.3/9.3)。

校准数据=自然任务积累的账本统计(派单→首次工具调用延迟/会话内事件间隔/
任务时长),不专门派测速活;每单结算或 30min 窗口重算一次滑动统计(非每 tick)。
防自漂移三件: EMA 平滑(单次波动不显著跳变)+下界(不低于冷启动默认×0.5)
+锚定(T2 活性线受 expect_min 约束;expect_min 漂移不脱离档位默认 [×0.5, ×2])。
网络波动标记(network_fluctuation 审计行,7.5 多次采样确认首次命中)的派单
不计入校准(波动不入校准,防污染)。
"""

import json
import math

from . import ops
from .db import now, tx

ALPHA = 0.3             # EMA 平滑系数: 单次样本只占 30% 权重
FLOOR_RATIO = 0.5       # 下界: 不低于冷启动默认×0.5
ANCHOR_RATIO = 2.0      # 锚定: expect_min 漂移区间=[档位默认×0.5, ×2]
MIN_SAMPLES = 5         # 实测足量才覆盖先验(9.1 宣称先验+实测后验)
DELAY_SAFETY = 3        # T1 目标 = p75(派单→首调延迟)×安全倍数
GAP_SAFETY = 3          # T2 目标 = p75(会话内事件间隔)×安全倍数
GAP_CAP_SECONDS = 3600  # 超 1h 的间隔=任务间空闲,不当活性样本
WINDOW_SECONDS = 1800   # 30min 窗口节流(监控器 tick 触发用)

T1_DEFAULT = 120
T2_DEFAULT = 600
TIER_DEFAULTS = {"simple": 15, "normal": 30, "hard": 60}


def _p75(values):
    """p75 分位(排序取 ceil(0.75n)-1,从简)。"""
    if not values:
        return None
    vs = sorted(values)
    return vs[max(0, math.ceil(len(vs) * 0.75) - 1)]


def _tier_of(priority: int) -> str:
    """与 ops._estimate_complexity 同口径(按 priority 分档)。"""
    if priority >= 3:
        return "hard"
    if priority >= 1:
        return "normal"
    return "simple"


def _set_config(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        (key, str(value), now()))


def collect_samples(conn):
    """收集校准样本: done 的实施者派单,排除网络波动标记(7.5 波动不入校准)。

    返回 {"delays": [派单→首调延迟秒], "gaps": [会话内事件间隔秒],
          "durations": {实例: [(tier, 时长分钟)]}}。
    """
    fluct = {
        r["did"] for r in conn.execute(
            "SELECT json_extract(detail,'$.dispatch_id') AS did FROM audit"
            " WHERE action='network_fluctuation'")
        if r["did"] is not None
    }
    rows = conn.execute(
        "SELECT d.id, d.worker_id, d.created_at, d.updated_at, t.priority"
        " FROM dispatches d JOIN tasks t ON d.task_id=t.id"
        " WHERE d.worker_role='worker' AND d.status='done'").fetchall()
    delays = []
    durations = {}
    for r in rows:
        if r["id"] in fluct:
            continue
        first_tool = conn.execute(
            "SELECT ts FROM messages WHERE type='event'"
            " AND json_extract(payload,'$.event_type')='pre_tool_use'"
            " AND sender=? AND ts>=? ORDER BY ts ASC LIMIT 1",
            (r["worker_id"], r["created_at"])).fetchone()
        if first_tool:
            delays.append(first_tool["ts"] - r["created_at"])
        dur_min = (r["updated_at"] - r["created_at"]) / 60
        durations.setdefault(r["worker_id"], []).append(
            (_tier_of(r["priority"]), dur_min))
    # 会话内事件间隔(跨任务空闲超 GAP_CAP 不当活性样本)
    gaps = []
    ev_rows = conn.execute(
        "SELECT sender, json_extract(payload,'$.session_id') AS sid, ts"
        " FROM messages WHERE type='event' ORDER BY sender, sid, ts").fetchall()
    prev = {}
    for e in ev_rows:
        key = (e["sender"], e["sid"])
        if key in prev and 0 < e["ts"] - prev[key] <= GAP_CAP_SECONDS:
            gaps.append(e["ts"] - prev[key])
        prev[key] = e["ts"]
    return {"delays": delays, "gaps": gaps, "durations": durations}


def recalibrate(conn, force: bool = False, now_ts: int = None) -> dict:
    """重算滑动统计(每单结算 force=True;监控器 tick 走 30min 窗口节流)。

    返回 {"changed": {...}} 或 {"skipped": "window"}。写 configs 用裸执行:
    调用方在事务内(结算)则并入该事务,事务外(监控器循环)则自动提交,
    不开自己的 tx(防 BEGIN 嵌套)。
    """
    now_ts = now_ts if now_ts is not None else now()
    if not force:
        last = int(ops._config(conn, "calib_last_ts") or 0)
        if now_ts - last < WINDOW_SECONDS:
            return {"skipped": "window", "last": last}
    samples = collect_samples(conn)
    changed = {}

    # T1: p75(首调延迟)×安全倍数 → EMA 平滑 → 下界 → 不超过 T2
    t1_old = int(ops._config(conn, "t1_seconds") or T1_DEFAULT)
    t2_old = int(ops._config(conn, "t2_seconds") or T2_DEFAULT)
    t1_new = t1_old
    d75 = _p75(samples["delays"])
    if d75 is not None:
        t1_new = round(t1_old * (1 - ALPHA) + d75 * DELAY_SAFETY * ALPHA)
        t1_new = max(t1_new, round(T1_DEFAULT * FLOOR_RATIO))
        t1_new = min(t1_new, t2_old)
    # T2: p75(事件间隔)×安全倍数 → EMA → 下界;锚定: 不超 expect_min_normal 档
    t2_new = t2_old
    g75 = _p75(samples["gaps"])
    if g75 is not None:
        anchor = int(ops._config(conn, "expect_min_normal")
                     or TIER_DEFAULTS["normal"]) * 60
        t2_new = round(t2_old * (1 - ALPHA)
                       + min(g75 * GAP_SAFETY, anchor) * ALPHA)
        t2_new = max(t2_new, round(T2_DEFAULT * FLOOR_RATIO))
    if t1_new != t1_old:
        _set_config(conn, "t1_seconds", t1_new)
        changed["t1_seconds"] = t1_new
    if t2_new != t2_old:
        _set_config(conn, "t2_seconds", t2_new)
        changed["t2_seconds"] = t2_new

    # expect_min 按实例漂移: (档位,实例) 历史时长 p75,足量才覆盖,
    # 与阈值同一滑动机制(EMA 平滑)+锚定 [档位×0.5, ×2]
    for inst, pairs in samples["durations"].items():
        prev_row = conn.execute(
            "SELECT value FROM configs WHERE key=?",
            (f"expect_min_calib:{inst}",)).fetchone()
        prev = json.loads(prev_row["value"]) if prev_row else {}
        tiers = dict(prev)
        for tier, default in TIER_DEFAULTS.items():
            vals = [m for t, m in pairs if t == tier]
            if len(vals) < MIN_SAMPLES:
                continue
            tier_default = int(ops._config(conn, f"expect_min_{tier}") or default)
            lo, hi = (round(tier_default * FLOOR_RATIO),
                      round(tier_default * ANCHOR_RATIO))
            p75 = _p75(vals)
            if tier in prev:
                p75 = round(prev[tier] * (1 - ALPHA) + p75 * ALPHA)
            tiers[tier] = int(min(max(p75, lo), hi))
        if tiers != prev:
            _set_config(conn, f"expect_min_calib:{inst}", json.dumps(tiers))
            changed[f"expect_min:{inst}"] = tiers

    _set_config(conn, "calib_last_ts", now_ts)
    ops.audit(conn, "calibration", {"changed": changed, "force": force})
    return {"changed": changed}
