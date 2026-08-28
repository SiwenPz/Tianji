"""额度检测与上下文健康度(票 11,规格书 14.1/14.2)。

- 信号源三层: ①claude 壳 statusline 机械上报上下文占用百分比(进程内官方状态)
  ②转录 usage 累加兜底(巡检顺带) ③cc-switch 账目表(只覆盖走 cc-switch 实例)
  +实例档案错误码归类(429=限流≠故障,不误杀)
- 总额黑盒: 目标=已尽必知、将尽有提示;不做官方 usage 接口轮询体系
- 上下文健康度两处检查点: 派活前分配器机械检查(装不下跳过/健康度低提示续接)
  +监控器巡检复查(派单后涨满可检出);阈值存 configs(实现期参数)
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import messages, ops
from .db import now

# 错误码归类(14.1③): 429=限流≠故障
ERROR_CLASSES = {429: "限流", 403: "权限/封禁", 401: "认证失效"}


def _key(instance: str) -> str:
    return f"quota:{instance}"


def _load(conn, instance: str) -> dict:
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (_key(instance),)).fetchone()
    return json.loads(row["value"]) if row else {}


def _save(conn, instance: str, data: dict):
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        (_key(instance), json.dumps(data, ensure_ascii=False), now()))


def report_context_pct(conn, instance: str, pct: float,
                       source: str = "statusline") -> dict:
    """statusline 机械上报入口(14.1①): 上下文窗口占用百分比进账本。

    高频上报只写 configs,不写审计(机械上报零噪音)。
    """
    data = _load(conn, instance)
    data.update({"context_pct": pct, "ts": now(), "source": source})
    _save(conn, instance, data)
    return {"instance": instance, "context_pct": pct}


def scan_transcript_usage(conn, instance: str, transcript_path: str) -> dict:
    """转录 usage 累加(14.1②): 解析 jsonl 里的 usage 字段累计 token。

    claude 转录格式: message.usage.{input_tokens,output_tokens,...};
    其它壳有 usage 字段即累加,没有跳过(读不了的壳=静态预估,14.2)。
    账本里的 usage=该转录文件的当前累计总量;尺寸守卫: 巡检每拍都路过,
    文件没长过就回报存量、不整读重扫;文件变了→全量重算(幂等,不翻倍)。
    """
    data = _load(conn, instance)
    usage = data.get("usage") or {"input_tokens": 0, "output_tokens": 0,
                                  "lines": 0}
    p = Path(transcript_path)
    if not p.is_file():
        return {"instance": instance, "skipped": "转录不存在"}
    size = p.stat().st_size
    if data.get("usage_last_size") == size:
        return {"instance": instance, "usage": usage, "unchanged": True}
    usage = {"input_tokens": 0, "output_tokens": 0, "lines": 0}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"usage"' not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        u = (row.get("message") or {}).get("usage") or row.get("usage") or {}
        if not u:
            continue
        usage["input_tokens"] += int(u.get("input_tokens") or 0)
        usage["output_tokens"] += int(u.get("output_tokens") or 0)
        usage["lines"] += 1
    data["usage"] = usage
    data["usage_last_size"] = size
    _save(conn, instance, data)
    return {"instance": instance, "usage": usage}


def _ccswitch_proxy_summary(db) -> dict:
    """proxy_request_logs: 错误码归类表(429=限流≠故障)。"""
    rows = db.execute(
        "SELECT status_code, COUNT(*) AS n FROM proxy_request_logs"
        " GROUP BY status_code").fetchall()
    summary = {}
    for r in rows:
        code = r["status_code"]
        cls = ERROR_CLASSES.get(code, "故障" if code >= 500 else "其他")
        summary[code] = {"class": cls, "count": r["n"]}
    return summary


def _ccswitch_extra_table(db, table: str) -> dict:
    """cc-switch 附加账目表 best-effort 摘要(usage_daily_rollups/provider_health)。

    版本差异容忍: 表不存在/列对不上都降级成只报在场情况,不抛错。
    """
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    if row is None:
        return {"table": table, "present": False}
    out = {"table": table, "present": True}
    try:
        out["rows"] = db.execute(
            f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    except sqlite3.Error:
        out["rows"] = -1
        return out
    try:
        cols = [c[1] for c in db.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return out
    token_cols = ("input_tokens", "output_tokens", "total_tokens",
                  "prompt_tokens", "completion_tokens")
    status_cols = ("status", "state", "result", "healthy")
    for c in cols:
        cl = c.lower()
        if cl in token_cols:
            try:
                out[cl] = db.execute(
                    f"SELECT COALESCE(SUM({c}), 0) AS s FROM {table}"
                ).fetchone()["s"]
            except sqlite3.Error:
                pass
        elif cl in status_cols:
            try:
                out[f"{c}_by"] = {
                    r["k"]: r["n"] for r in db.execute(
                        f"SELECT {c} AS k, COUNT(*) AS n FROM {table}"
                        f" GROUP BY {c}").fetchall()}
            except sqlite3.Error:
                pass
    return out


def read_ccswitch(conn, db_path: str, instance: str) -> dict:
    """cc-switch 账目表读取+错误码归类(14.1③;未装 cc-switch 的环境跳过)。

    429=限流(额度已尽,置 exhausted,12 暂停派新活消费);403=权限/封禁
    (归类写档案,不算额度用尽);另补读 usage_daily_rollups/provider_health
    两张账目表(版本差异容忍,缺表就如实报 absent)。
    """
    if not os.path.isfile(db_path):
        return {"skipped": "无 cc-switch 库(该层不适用)"}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        summary = _ccswitch_proxy_summary(db)
    except sqlite3.Error:
        db.close()
        return {"skipped": "cc-switch 库无 proxy_request_logs 表"}
    rollups = _ccswitch_extra_table(db, "usage_daily_rollups")
    health = _ccswitch_extra_table(db, "provider_health")
    db.close()
    data = _load(conn, instance)
    if summary:
        data["last_error"] = ""
        data["exhausted"] = False
    if 429 in summary:
        # 429=限流≠故障: 实例档案归类+额度已尽标记(12 的暂停派新活消费此信号)
        data["last_error"] = "rate_limit"
        data["exhausted"] = True
        ops.update_profile_notes(conn, instance, "429=限流(额度已尽),非故障")
    if 403 in summary:
        # 403=权限/封禁: 归类写实例档案,但不是额度用尽,不置 exhausted
        data["last_error"] = "forbidden"
        ops.update_profile_notes(conn, instance, "403=权限/封禁,按账号问题归类(非限流)")
    data["ccswitch"] = {
        "proxy_request_logs": summary,
        "usage_daily_rollups": rollups,
        "provider_health": health,
    }
    _save(conn, instance, data)
    return {"instance": instance, "summary": summary,
            "usage_daily_rollups": rollups, "provider_health": health}


def context_health(conn, instance: str) -> dict:
    """上下文健康度(14.2): pct/剩余窗口/提示。读不了转录的壳=静态预估调用方兜。

    同时检查池级耗尽信号(池=key 等价物): 若存在 quota: 键(非实例名)且
    exhausted=true → 该实例视为耗尽,allocator_health_check 会跳过。
    """
    data = _load(conn, instance)
    pct = float(data.get("context_pct") or 0)
    prof = conn.execute(
        "SELECT context_window FROM ability_profiles WHERE instance_name=?",
        (instance,)).fetchone()
    window = (prof["context_window"] if prof else 0) or 0
    remaining = window * (100 - pct) / 100 if window else 0
    threshold = float(ops._config(conn, "health_pct_threshold") or 85)
    hint = ""
    if pct >= threshold:
        hint = f"上下文占用 {pct:.0f}%,建议先续接(14.2)"

    # 池级耗尽排查(票 57): quota: 键中 exhausted=true 且非实例名即视为池耗尽
    instance_exhausted = bool(data.get("exhausted"))
    pool_exhausted = _pool_exhausted(conn, instance)
    return {"instance": instance, "pct": pct, "remaining": int(remaining),
            "window": window, "hint": hint,
            "exhausted": instance_exhausted or pool_exhausted}


def _pool_exhausted(conn, instance: str) -> bool:
    """检查是否存在非实例名的 exhausted quota 条目（池级耗尽信号）。"""
    known_instances = {r["name"] for r in conn.execute(
        "SELECT name FROM instances").fetchall()}
    for row in conn.execute(
            "SELECT key, value FROM configs WHERE key LIKE 'quota:%'").fetchall():
        key = row["key"][len("quota:"):]
        if key == instance:
            continue  # 实例级已由调用方处理
        if key in known_instances:
            continue  # 其他实例级,不干扰
        try:
            d = json.loads(row["value"])
            if d.get("exhausted"):
                return True
        except (json.JSONDecodeError, TypeError):
            pass
    return False


def allocator_health_check(conn, task_id: int, expected_size: int,
                           candidates: list) -> dict:
    """派活前机械检查(14.2,供分配器调用): 返回 {skipped, hints, qualified_names}。

    - 额度已尽(exhausted)→跳过(12: 暂停派新活的消费侧在 allocator_pick)
    - 剩余窗口装不下→跳过(硬过滤)
    - 健康度低但装得下→不硬跳,产出续接提示
    """
    skipped, hints = {}, {}
    qualified = []
    for inst in candidates:
        h = context_health(conn, inst)
        if h["exhausted"]:
            skipped[inst] = "额度已尽(429 限流)"
            continue
        if h["window"] and h["remaining"] < expected_size:
            skipped[inst] = (f"剩余窗口 {h['remaining']} 装不下 "
                             f"{expected_size}(占用 {h['pct']:.0f}%)")
            continue
        if h["hint"]:
            hints[inst] = h["hint"]
        qualified.append(inst)
    return {"qualified": qualified, "skipped": skipped, "hints": hints}


def _shell_quota_sources(conn, shell: str) -> list:
    """读壳条目能力声明里的额度信号层(integration_shell: → shell: → 内置模板兜底)。"""
    for key in (f"integration_shell:{shell}", f"shell:{shell}"):
        row = conn.execute(
            "SELECT value FROM configs WHERE key=?", (key,)).fetchone()
        if row:
            try:
                caps = (json.loads(row["value"]) or {}).get("capabilities") or {}
            except json.JSONDecodeError:
                caps = {}
            srcs = caps.get("quota_sources")
            if srcs:
                return list(srcs)
    try:
        from .adapters import template as tpl_mod
        tpl = tpl_mod.get_template(shell)
        return list((tpl.capabilities or {}).get("quota_sources") or [])
    except KeyError:
        return []


def _scan_instance_transcript(conn, instance: str, shell: str,
                              isolated_dir: str = "") -> dict | None:
    """按该实例最近登记会话定位转录文件,顺带累加 usage(14.1② 巡检接线)。"""
    reg = conn.execute(
        "SELECT session_id FROM instance_registrations"
        " WHERE instance_name=? ORDER BY id DESC LIMIT 1",
        (instance,)).fetchone()
    if reg is None or not reg["session_id"]:
        return None
    try:
        from .adapters import transcript_parser
        p = transcript_parser.transcript_path(
            shell, reg["session_id"], isolated_dir=isolated_dir)
    except Exception:
        return None
    if p is None:
        return None
    return scan_transcript_usage(conn, instance, str(p))


def monitor_scan(conn):
    """监控器巡检复查(14.1 零新增常驻/14.2 第二检查点): 将尽提示+已尽必知。

    接线(票 48): 按壳声明的额度信号层顺带跑——
    ②转录 usage 累加(声明 transcript 的壳);③cc-switch 账目(配了库路径
    且壳声明 ccswitch 的实例)。单个实例失败只跳过该实例,不拖垮整轮巡检。
    """
    full_pct = float(ops._config(conn, "quota_full_pct") or 98)
    ccswitch_db = (ops._config(conn, "ccswitch_db_path") or "").strip()
    for inst in conn.execute(
            "SELECT name, shell, isolated_dir FROM instances"
            " WHERE is_active=1").fetchall():
        name = inst["name"]
        try:
            sources = _shell_quota_sources(conn, inst["shell"])
            if "transcript" in sources:
                _scan_instance_transcript(conn, name, inst["shell"],
                                          inst["isolated_dir"])
            if ccswitch_db and "ccswitch" in sources:
                read_ccswitch(conn, ccswitch_db, name)
        except Exception:
            continue  # 单个实例的额度巡检失败不拖垮整轮
        data = _load(conn, name)
        pct = float(data.get("context_pct") or 0)
        if data.get("exhausted"):
            continue  # 已尽已归类,不重复升级
        if pct >= full_pct:
            messages.send(
                conn, "escalation", "monitor",
                {"worker_id": name,
                 "reason": f"上下文占用 {pct:.0f}% 将尽(14.1 已尽必知,"
                           f"将尽有提示);建议续接或换实例"},
                "controller")
