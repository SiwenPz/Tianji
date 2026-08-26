"""派单 cancelled 态与强制干预配套(票 19)。"""

import json
import os
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import task_dir
from tianji.monitor import _tick
from tianji.render import spawn
from tianji.events import ingest_event


def _to_executing(conn, controller, worker):
    """任务 executing + 派单 active(已开工)。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    env = {**os.environ, "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "s", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "s", "event_type": "pre_tool_use"})
    return tid, did, env


def _escalations(conn):
    rows = conn.execute(
        "SELECT payload FROM messages WHERE type='escalation' ORDER BY seq"
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# ====================================================================
# 验收标准 1: force 终止——派单 cancelled + 登记行 closed + 任务 archived
# + 审计行，四项一笔事务；中途失败全回滚。
# ====================================================================

def test_force_terminate_cancels_dispatch_archives_task_and_closes_registration(
        conn, controller, worker):
    tid, did, env = _to_executing(conn, controller, worker)

    reg = conn.execute(
        "SELECT id, status FROM instance_registrations WHERE dispatch_id=?",
        (did,)).fetchone()
    assert reg["status"] == "active"
    assert ops.dispatch_get(conn, did)["status"] == "active"
    assert ops.task_get(conn, tid)["status"] == "executing"

    r = ops.task_force(conn, controller, tid, "archived", "强制终止",
                       request_id="r-f1")
    assert r["to"] == "archived"

    # 派单 cancelled
    assert ops.dispatch_get(conn, did)["status"] == "cancelled"
    # 登记行 closed + abnormal
    reg2 = conn.execute(
        "SELECT status, abnormal FROM instance_registrations WHERE id=?",
        (reg["id"],)).fetchone()
    assert reg2["status"] == "closed" and reg2["abnormal"] == 1
    # 任务 archived
    assert ops.task_get(conn, tid)["status"] == "archived"
    # 审计行
    aud = conn.execute(
        "SELECT action, detail FROM audit WHERE action='force_intervention'"
    ).fetchone()
    assert aud is not None
    detail = json.loads(aud["detail"])
    assert detail["to"] == "archived"
    assert detail["reason"] == "强制终止"


def test_force_terminate_rolls_back_on_failure(conn, controller, worker,
                                               monkeypatch):
    """中途失败全回滚：消息发送步骤抛异常时，任务/派单/登记行均不变。"""
    tid, did, env = _to_executing(conn, controller, worker)
    old_task = ops.task_get(conn, tid)["status"]
    old_dispatch = ops.dispatch_get(conn, did)["status"]
    old_reg = conn.execute(
        "SELECT status FROM instance_registrations WHERE dispatch_id=?",
        (did,)).fetchone()["status"]

    def failing_send(c, type_, sender, payload, recipient_role=None, ts=None):
        raise RuntimeError("模拟消息发送失败")

    monkeypatch.setattr(ops.messages, "send", failing_send)

    with pytest.raises(RuntimeError, match="模拟消息发送失败"):
        ops.task_force(conn, controller, tid, "archived", "回滚测试",
                       request_id="r-f2")

    assert ops.task_get(conn, tid)["status"] == old_task
    assert ops.dispatch_get(conn, did)["status"] == old_dispatch
    reg = conn.execute(
        "SELECT status FROM instance_registrations WHERE dispatch_id=?",
        (did,)).fetchone()
    assert reg["status"] == old_reg


# ====================================================================
# 验收标准 2: force 改派——原派单 cancelled + 任务回 dispatched
# + 可正常发新派单；重派计数照旧累计。
# ====================================================================

def test_force_reassign_cancels_dispatch_requeues_task_and_issues_new_dispatch(
        conn, controller, worker):
    tid, did, env = _to_executing(conn, controller, worker)
    old_retry = ops.task_get(conn, tid)["retry_count"]

    r = ops.task_force(conn, controller, tid, "dispatched", "改派",
                       request_id="r-f3")
    assert r["to"] == "dispatched"

    # 原派单 cancelled
    assert ops.dispatch_get(conn, did)["status"] == "cancelled"
    # 登记行 closed
    reg = conn.execute(
        "SELECT status FROM instance_registrations WHERE dispatch_id=?",
        (did,)).fetchone()
    assert reg["status"] == "closed"
    # 任务回 dispatched，retry_count+1
    t = ops.task_get(conn, tid)
    assert t["status"] == "dispatched"
    assert t["retry_count"] == old_retry + 1
    # 新派单 issued
    d2 = conn.execute(
        "SELECT id, status FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    assert d2["id"] != did
    assert ops.dispatch_get(conn, d2["id"])["status"] == "issued"
    # 审计行
    aud = conn.execute(
        "SELECT detail FROM audit WHERE action='force_cancel_dispatch'"
    ).fetchone()
    assert aud is not None


def test_force_reassign_to_new_worker(conn, controller, worker):
    """4.4 补全: 改派可指定目标工人(缺省派回原工人);目标未注册被拒。"""
    ops.instance_register(conn, "赵云", "claude", "step-router-v1")
    tid, did, env = _to_executing(conn, controller, worker)

    with pytest.raises(ValueError, match="未注册或不活跃"):
        ops.task_force(conn, controller, tid, "dispatched", "改派",
                       request_id="r-f4a", new_worker="不存在")

    r = ops.task_force(conn, controller, tid, "dispatched", "改派换人",
                       request_id="r-f4b", new_worker="赵云")
    assert r["to"] == "dispatched"
    assert ops.dispatch_get(conn, did)["status"] == "cancelled"
    d2 = conn.execute(
        "SELECT id, worker_id FROM dispatches WHERE task_id=?"
        " ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    assert d2["worker_id"] == "赵云"
    assert ops.dispatch_get(conn, d2["id"])["status"] == "issued"
    # 同状态改派(dispatched→dispatched)合法: 取消在途派单即语义本身
    r2 = ops.task_force(conn, controller, tid, "dispatched", "同状态再改派",
                        request_id="r-f4c")
    d3 = conn.execute(
        "SELECT id FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    assert ops.dispatch_get(conn, d3["id"])["status"] == "issued"
    assert ops.dispatch_get(conn, d2["id"])["status"] == "cancelled"


# ====================================================================
# 验收标准 3: cancelled 派单的登记进程退出后，监控器对账②
# 不重派、不升级。
# ====================================================================

def test_cancelled_dispatch_skips_requeue_and_escalation(conn, controller, worker):
    tid, did, env = _to_executing(conn, controller, worker)
    ops.task_force(conn, controller, tid, "archived", "强制终止",
                   request_id="r-f4")

    # 重新激活登记行 + 杀 pid（模拟 race / 延迟关闭场景）
    conn.execute(
        "UPDATE instance_registrations SET status='active', pid=1 WHERE dispatch_id=?",
        (did,))
    assert ops.dispatch_get(conn, did)["status"] == "cancelled"

    state = {}
    _tick(conn, state)

    # 派单仍为 cancelled
    assert ops.dispatch_get(conn, did)["status"] == "cancelled"
    # 无确定性重派、无升级
    assert not any("确定性重派" in e["reason"] for e in _escalations(conn))
    assert not any("进程退出无结算" in e["reason"] for e in _escalations(conn))


# ====================================================================
# 验收标准 4: cancelled 派单时长不进校准统计、表现分无变化。
# ====================================================================

def test_cancelled_dispatch_does_not_change_ability_score(conn, controller, worker):
    tid, did, env = _to_executing(conn, controller, worker)
    old_score = conn.execute(
        "SELECT score FROM ability_profiles WHERE instance_name=?",
        (worker["worker_id"],)).fetchone()["score"]

    ops.task_force(conn, controller, tid, "archived", "强制终止",
                   request_id="r-f5")

    new_score = conn.execute(
        "SELECT score FROM ability_profiles WHERE instance_name=?",
        (worker["worker_id"],)).fetchone()["score"]
    assert old_score == new_score


# ====================================================================
# 验收标准 5: 干预杀会话后监控器不误重派（"喊停又被派出去"回归）。
# ====================================================================

def test_intervention_killed_session_does_not_get_requeued(conn, controller, worker):
    tid, did, env = _to_executing(conn, controller, worker)
    ops.task_force(conn, controller, tid, "archived", "强制终止",
                   request_id="r-f6")

    # 重新激活登记行，pid 指向不存在的进程
    conn.execute(
        "UPDATE instance_registrations SET status='active', pid=12345678"
        " WHERE dispatch_id=?",
        (did,))

    state = {}
    _tick(conn, state)

    assert ops.dispatch_get(conn, did)["status"] == "cancelled"
    assert not any("确定性重派" in e["reason"] for e in _escalations(conn))


def test_escalate_to_cancelled_is_valid():
    """escalate→cancelled 为合法转换(票 19 返修)。"""
    from tianji.state import check_dispatch_transition
    assert check_dispatch_transition("escalate", "cancelled") is True


def test_dispatches_check_migration_adds_cancelled(tmp_path, monkeypatch):
    """老账本 dispatches CHECK 缺 cancelled 则表重建(2026-08-18 demo 账本实证)。"""
    import sqlite3 as _s
    from tianji.db import connect
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TIANJI_HOME", str(home))
    old = _s.connect(home / "ledger.db")
    old.execute(
        "CREATE TABLE dispatches ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,"
        " worker_id TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'issued' CHECK (status IN"
        " ('issued','active','done','stale','requeue','escalate')))")
    old.execute(
        "INSERT INTO dispatches (task_id, worker_id, status) VALUES (1,'铁蛋','done')")
    old.commit()
    old.close()
    conn = connect()
    try:
        conn.execute("UPDATE dispatches SET status='cancelled' WHERE id=1")
        row = conn.execute("SELECT status FROM dispatches WHERE id=1").fetchone()
        assert row["status"] == "cancelled"
        # 序列保持: sqlite_sequence 已对齐
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='dispatches'").fetchone()
        assert seq["seq"] == 1
    finally:
        conn.close()


def test_dispatch_cancel_single(conn, controller, worker):
    """总控取消单个在途派单(4.4 配套): cancelled+关登记行+secret 作废,
    不动任务状态、不计重派。"""
    tid, did, env = _to_executing(conn, controller, worker)
    with pytest.raises(PermissionError):
        ops.dispatch_cancel(conn, worker, did, "越权")
    old_retry = ops.task_get(conn, tid)["retry_count"]
    r = ops.dispatch_cancel(conn, controller, did, "误派纠正",
                            request_id="r-cancel")
    assert r["to"] == "cancelled"
    assert ops.task_get(conn, tid)["retry_count"] == old_retry
    assert ops.task_get(conn, tid)["status"] == "executing"
    # 终态不可再取消
    with pytest.raises(ValueError, match="不可取消"):
        ops.dispatch_cancel(conn, controller, did, "再来", request_id="r-cancel2")
