"""worker_done 单事务结算(验收 2/3): 四种拒绝码+幂等重放+身份冒名。"""

import os
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import task_dir
from tianji.render import spawn


def _to_executing(conn, controller, worker):
    """真实链路快速走到任务 executing+派单 active(结算前置)。

    spawn 生成 secret 注入 env(11.4),事件流触发开工证据(5.1)。
    返回 (task_id, dispatch_id, worker_env)。
    """
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    from tianji.events import ingest_event
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "pre_tool_use"})
    return tid, did, env


def _report(conn, did):
    p = Path(task_dir(did)) / "report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("成果报告", encoding="utf-8")
    return str(p)


def test_settle_ok_chain(conn, controller, worker):
    tid, did, env = _to_executing(conn, controller, worker)
    rp = _report(conn, did)
    r = ops.dispatch_settle(conn, env, did, rp, "ok")
    assert r["status"] == "done" and r["task_status"] == "reviewing"
    assert ops.task_get(conn, tid)["status"] == "reviewing"
    assert ops.dispatch_get(conn, did)["status"] == "done"


def test_settle_auth_fail_forged_secret(conn, controller, worker):
    tid, did, env = _to_executing(conn, controller, worker)
    rp = _report(conn, did)
    fake = {**env, "TIANJI_SECRET": "假secret"}
    r = ops.dispatch_settle(conn, fake, did, rp, "ok")
    assert r["rejected"] == "auth_fail"


def test_settle_unknown_dispatch(conn, controller, worker):
    env = {**os.environ, "TIANJI_WORKER_ID": worker["worker_id"],
           "TIANJI_SECRET": worker["secret"]}
    r = ops.dispatch_settle(conn, env, 99999, "x.md", "ok")
    assert r["rejected"] == "unknown"


def test_settle_stale_after_requeue(conn, controller, worker):
    tid, did, env = _to_executing(conn, controller, worker)
    conn.execute("UPDATE dispatches SET status='requeue' WHERE id=?", (did,))
    r = ops.dispatch_settle(conn, env, did, _report(conn, did), "ok")
    assert r["rejected"] == "stale"


def test_settle_replay_returns_original(conn, controller, worker):
    """幂等: 重放同一 dispatch_id 返回原回执,不重复执行(验收 2)。"""
    tid, did, env = _to_executing(conn, controller, worker)
    rp = _report(conn, did)
    first = ops.dispatch_settle(conn, env, did, rp, "ok")
    second = ops.dispatch_settle(conn, env, did, rp, "ok")
    assert second["replay"] is True
    assert second["dispatch_id"] == first["dispatch_id"]
    # 审计只有一次 worker_done
    n = conn.execute("SELECT COUNT(*) AS n FROM audit WHERE action='worker_done'"
                     ).fetchone()["n"]
    assert n == 1


def test_settle_report_missing_rejected(conn, controller, worker):
    """载荷完整性: report_path 必须已落盘(实施者不自证,8.3)。"""
    tid, did, env = _to_executing(conn, controller, worker)
    r = ops.dispatch_settle(conn, env, did, "C:/不存在/report.md", "ok")
    assert r["rejected"] == "unknown"


def test_settle_requires_identity_env(conn, controller, worker):
    tid, did, env = _to_executing(conn, controller, worker)
    with pytest.raises(PermissionError):
        ops.dispatch_settle(conn, {}, did, "x.md", "ok")


def test_settle_hookless_dispatched_fallback(conn, controller, worker):
    """无钩子壳兜底(2026-08-20,票27/票15 两踩): dsh/cline 等壳事件不进账本,
    任务停在 dispatched 无开工证据;worker_done 结算自动落账 dispatched→reviewing
    +审计行,不再要总控 task force 手动补位。"""
    tid = ops.task_new(conn, controller, "无钩子壳任务", request_id="r-h-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-h-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-h-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    # 无事件注入: 任务停在 dispatched(无钩子壳无开工证据)
    assert ops.task_get(conn, tid)["status"] == "dispatched"
    rp = _report(conn, did)
    r = ops.dispatch_settle(conn, env, did, rp, "ok")
    assert r["status"] == "done" and r["task_status"] == "reviewing"
    assert ops.task_get(conn, tid)["status"] == "reviewing"
    assert ops.dispatch_get(conn, did)["status"] == "done"
    row = conn.execute(
        "SELECT detail FROM audit WHERE action='settle_hookless_fallback'"
    ).fetchone()
    assert row is not None and "无钩子壳" in row["detail"]
