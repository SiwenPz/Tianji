"""动态活性阈值与 expect_min 校准(票 07 验收 1-6)。"""

import json
import os
from pathlib import Path

import pytest

from tianji import messages, ops
from tianji.calibration import recalibrate
from tianji.db import task_dir
from tianji.render import spawn

BASE = 1_000_000  # 构造时间基点


def _done_dispatch(conn, controller, worker, seq, priority=1, dur_min=30,
                   delay_s=None, fluct=False):
    """构造一条 done 实施者派单(真实链路走通后改写时间戳,作校准样本)。"""
    start = BASE + seq * 10000
    tid = ops.task_new(conn, controller, f"任务{seq}", priority=priority,
                       request_id=f"r7-new-{seq}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s,
                            request_id=f"r7-{s}-{seq}")
    did = ops.dispatch_issue(conn, controller, tid, worker,
                             request_id=f"r7-issue-{seq}")["dispatch_id"]
    conn.execute(
        "UPDATE dispatches SET status='done', created_at=?, updated_at=?"
        " WHERE id=?", (start, start + int(dur_min * 60), did))
    if delay_s is not None:
        messages.send(conn, "event", worker,
                      {"event_type": "pre_tool_use", "session_id": f"s{seq}"},
                      ts=start + delay_s)
    if fluct:
        ops.audit(conn, "network_fluctuation",
                  {"dispatch_id": did, "silent_s": 999})
    return did


def _gap_events(conn, worker, session, start, interval_s, n):
    """构造会话内事件序列(post_tool_use 不干扰首调延迟样本)。"""
    for i in range(n):
        messages.send(conn, "event", worker,
                      {"event_type": "post_tool_use", "session_id": session},
                      ts=start + i * interval_s)


def _cfg(conn, key):
    return ops._config(conn, key)


def test_sliding_stats_computed(conn, controller, worker):
    """验收 1: 近 10 单构造数据,滑动统计(p75+EMA)计算正确。"""
    w = worker["worker_id"]
    for i in range(10):
        _done_dispatch(conn, controller, w, i, delay_s=10)
    _gap_events(conn, w, "gap-sess", BASE - 50000, 100, 10)
    recalibrate(conn, force=True)
    # T1: p75(延迟)=10 → 目标 30 → 120*0.7+30*0.3=93
    assert int(_cfg(conn, "t1_seconds")) == 93
    # T2: p75(间隔)=100 → 目标 300 → 600*0.7+300*0.3=510
    assert int(_cfg(conn, "t2_seconds")) == 510
    assert int(_cfg(conn, "calib_last_ts")) > 0
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='calibration'").fetchone()


def test_smoothing_no_jump(conn, controller, worker):
    """验收 2a: EMA 平滑——一次重算只走 30%,不一步跳到目标。"""
    w = worker["worker_id"]
    # 延迟全 100s(目标 300),当前 120 → 一次重算=174,不是 300
    for i in range(10):
        _done_dispatch(conn, controller, w, i, delay_s=100)
    recalibrate(conn, force=True)
    assert int(_cfg(conn, "t1_seconds")) == 174


def test_extreme_single_value_ignored(conn, controller, worker):
    """验收 2b: p75 对单次极端值免疫(9 单 10s + 1 单 50000s,结果与无极端一致)。"""
    w = worker["worker_id"]
    for i in range(9):
        _done_dispatch(conn, controller, w, i, delay_s=10)
    _done_dispatch(conn, controller, w, 9, delay_s=50000)
    recalibrate(conn, force=True)
    # p75(10 样本,9 个 10s)=10 → 目标 30 → 120*0.7+30*0.3=93
    assert int(_cfg(conn, "t1_seconds")) == 93


def test_floor_and_anchor(conn, controller, worker):
    """验收 3: 阈值不跌破下界(默认×0.5);T2 锚定不超 expect_min_normal 档。"""
    w = worker["worker_id"]
    # 下界: 当前 t1=61,样本目标 3(延迟 1s)→ EMA 后 44 → 下界兜回 60
    conn.execute("UPDATE configs SET value='61' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='301' WHERE key='t2_seconds'")
    for i in range(10):
        _done_dispatch(conn, controller, w, i, delay_s=1)
    _gap_events(conn, w, "gap-sess", BASE - 50000, 1, 10)
    recalibrate(conn, force=True)
    assert int(_cfg(conn, "t1_seconds")) == 60   # 下界 120×0.5
    assert int(_cfg(conn, "t2_seconds")) == 300  # 下界 600×0.5
    # 锚定: 当前 t2=1700,间隔 3500s(目标 10500)→ 锚定 cap 1800(normal 档 30min)
    conn.execute("UPDATE configs SET value='1700' WHERE key='t2_seconds'")
    _gap_events(conn, w, "gap-sess2", BASE + 500000, 3500, 10)
    recalibrate(conn, force=True)
    t2 = int(_cfg(conn, "t2_seconds"))
    assert t2 == round(1700 * 0.7 + 1800 * 0.3)  # 1730,而非 10500 方向
    assert t2 <= 1800


def test_expect_min_drift_and_anchor(conn, controller, worker):
    """验收 4: expect_min 按实例实测漂移,锚定不脱离档位默认 [×0.5, ×2]。"""
    w = worker["worker_id"]
    # 漂移: 5 单 normal 档(priority=1)时长 50min → 校准值=50
    for i in range(5):
        _done_dispatch(conn, controller, w, i, priority=1, dur_min=50)
    # 锚定上界: 另一实例 5 单 500min → clamp 到 60(30×2)
    ops.instance_register(conn, "慢工", "codex", "step-router-v1")
    for i in range(10, 15):
        _done_dispatch(conn, controller, "慢工", i, priority=1, dur_min=500)
    # 锚定下界: 另一实例 5 单 1min → clamp 到 15(30×0.5)
    ops.instance_register(conn, "快工", "codex", "step-router-v1")
    for i in range(20, 25):
        _done_dispatch(conn, controller, "快工", i, priority=1, dur_min=1)
    recalibrate(conn, force=True)
    tid = ops.task_new(conn, controller, "查找用", priority=1,
                       request_id="r7-lookup")["task_id"]
    assert ops._get_expect_min(conn, tid, w) == 50
    assert ops._get_expect_min(conn, tid, "慢工") == 60
    assert ops._get_expect_min(conn, tid, "快工") == 15
    # 无校准数据的实例仍用档位默认
    ops.instance_register(conn, "新工", "codex", "step-router-v1")
    assert ops._get_expect_min(conn, tid, "新工") == 30


def test_fluctuation_excluded(conn, controller, worker):
    """验收 5: 网络波动标记的派单不参与校准。"""
    w = worker["worker_id"]
    for i in range(5):
        _done_dispatch(conn, controller, w, i, priority=1, dur_min=30)
    # 3 单波动标记的 500min 异常单: 若混入,p75(8 样本)=500→clamp 60
    for i in range(10, 13):
        _done_dispatch(conn, controller, w, i, priority=1, dur_min=500,
                       fluct=True)
    recalibrate(conn, force=True)
    tid = ops.task_new(conn, controller, "查找用", priority=1,
                       request_id="r7-lookup2")["task_id"]
    # 只算 5 单 30min → 校准值 30,波动单不污染
    assert ops._get_expect_min(conn, tid, w) == 30


def test_trigger_settle_and_window(conn, controller, worker):
    """验收 6: 每单结算触发重算;30min 窗口节流(窗口内跳过,窗口外重算)。"""
    # 窗口节流
    r1 = recalibrate(conn)
    assert "skipped" not in r1
    r2 = recalibrate(conn)
    assert r2.get("skipped") == "window"
    import time
    old = int(time.time()) - 1900
    conn.execute("UPDATE configs SET value=? WHERE key='calib_last_ts'",
                 (str(old),))
    r3 = recalibrate(conn, now_ts=old + 1900)
    assert "skipped" not in r3
    # 结算触发: 无钩子壳 dispatched 直结(兜底)后 calib_last_ts 被刷新
    conn.execute("UPDATE configs SET value='1' WHERE key='calib_last_ts'")
    tid = ops.task_new(conn, controller, "结算触发", request_id="r7-t6")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r7-t6-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r7-t6-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("成果报告", encoding="utf-8")
    st = ops.dispatch_settle(conn, env, did, str(rp), "ok")
    assert st["status"] == "done"
    assert int(_cfg(conn, "calib_last_ts")) > 1  # 结算触发了重算
