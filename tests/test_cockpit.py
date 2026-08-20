"""驾驶舱只读快照验收(票02): 4桶归类+升级可见+只读性。"""

import os
from pathlib import Path
from typing import Any, Dict

import pytest

from tianji import ops
from tianji.cockpit import render_snapshot, snapshot
from tianji.events import ingest_event
from tianji.messages import send
from tianji.render import spawn

def _worker_env(spawned: Dict[str, Any], dispatch_id: int) -> Dict[str, str]:
    return {
        **os.environ,
        "TIANJI_WORKER_ID": spawned["env"]["TIANJI_WORKER_ID"],
        "TIANJI_SECRET": spawned["env"]["TIANJI_SECRET"],
        "TIANJI_DISPATCH_ID": str(dispatch_id),
    }

def _active_worker(conn, controller, worker, request_prefix="r", role="worker", axis=""):
    prefix = request_prefix
    tid = ops.task_new(conn, controller, "任务", request_id=f"{prefix}-new")["task_id"]
    for state in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, state, request_id=f"{prefix}-{state}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             role=role, axis=axis,
                             request_id=f"{prefix}-issue")["dispatch_id"]
    spawned = spawn(conn, worker["worker_id"], did)
    return tid, did, spawned

class ReadOnlyConnection:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        text = sql.strip().lower() if isinstance(sql, str) else ""
        if any(text.startswith(prefix) for prefix in (
                "insert", "update", "delete", "replace",
                "create", "drop", "alter", "begin", "commit", "rollback")):
            raise AssertionError(f"cockpit snapshot triggered a write: {sql}")
        return self._conn.execute(sql, *args, **kwargs)

    def executemany(self, sql, seq_of_params):
        text = sql.strip().lower() if isinstance(sql, str) else ""
        if any(text.startswith(prefix) for prefix in (
                "insert", "update", "delete", "replace",
                "create", "drop", "alter", "begin", "commit", "rollback")):
            raise AssertionError(f"cockpit snapshot triggered a write: {sql}")
        return self._conn.executemany(sql, seq_of_params)

    def commit(self):
        raise AssertionError("cockpit snapshot triggered a write: commit")

def _clean_read_only_tables(conn):
    for table in ("receipts", "audit", "cursors"):
        conn.execute(f"DELETE FROM {table}")

def _register_extra_worker(conn, worker, name):
    r = ops.instance_register(
        conn, name, "codex", "step-router-v1", launch_cmd="python mock_worker.py")
    return {"worker_id": name, "secret": r["secret"]}


def test_four_bucket_classification(conn, controller, worker):
    worker_attention = _register_extra_worker(conn, worker, "铁蛋-attention")
    worker_working = _register_extra_worker(conn, worker, "铁蛋-working")
    worker_done = _register_extra_worker(conn, worker, "铁蛋-done")
    worker_reviewer = _register_extra_worker(conn, worker, "铁蛋-reviewer")
    worker_idle = _register_extra_worker(conn, worker, "铁蛋-idle")

    tid_a, did_a, spawned_a = _active_worker(conn, controller, worker_attention, request_prefix="r-attention")
    ingest_event(conn, _worker_env(spawned_a, did_a),
                 {"session_id": f"sa-{did_a}", "event_type": "session_start"})

    tid_w, did_w, spawned_w = _active_worker(conn, controller, worker_working, request_prefix="r-working")
    ingest_event(conn, _worker_env(spawned_w, did_w),
                 {"session_id": f"sw-{did_w}", "event_type": "session_start"})
    ingest_event(conn, _worker_env(spawned_a, did_a),
                 {"session_id": f"sa-{did_a}", "event_type": "session_start"})
    ingest_event(conn, _worker_env(spawned_w, did_w),
                 {"session_id": f"sw-{did_w}", "event_type": "pre_tool_use",
                  "payload": {"tool_name": "Write"}})

    tid_d, did_d, spawned_d = _active_worker(conn, controller, worker_done, request_prefix="r-done")
    rp = Path(ops.task_dir(did_d) / "report.md")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("done", encoding="utf-8")
    ops.dispatch_settle(conn, _worker_env(spawned_d, did_d), did_d, str(rp), "ok")

    tid_review, did_review, spawned_review = _active_worker(conn, controller, worker_reviewer, request_prefix="r-review", role="reviewer", axis="spec")
    # dispatched→reviewing 不可直跳;需 pre_tool_use 触发 executing→worker_done 结算
    ingest_event(conn, _worker_env(spawned_review, did_review),
                 {"session_id": f"sr-{did_review}", "event_type": "pre_tool_use",
                  "payload": {"tool_name": "Read"}})
    rp_review = Path(ops.task_dir(did_review) / "report.md")
    rp_review.parent.mkdir(parents=True, exist_ok=True)
    rp_review.write_text("review", encoding="utf-8")
    ops.dispatch_settle(conn, _worker_env(spawned_review, did_review),
                        did_review, str(rp_review), "pass")

    ops.task_new(conn, controller, "待处理任务", request_id="r-attention")
    archived_task = ops.task_new(conn, controller, "归档任务", request_id="r-archived")
    # new→archived 不可直跳;经 discussing 即可满足 attention 归类验证
    ops.task_transition(conn, controller, archived_task["task_id"],
                        "discussing", request_id="r-archived-task")

    tid_no_dispatch = ops.task_new(conn, controller, "未派单任务", request_id="r-no-dispatch")["task_id"]

    snap = snapshot(conn)
    # 返修项1: 结算派单进 done(dispatch_status="done" 优先于 session_state)
    # reviewer settle("pass") → dispatch done → done bucket.
    # worker settle("ok") → dispatch done + task reviewing → done bucket.
    # attention worker dispatched but not executing → attention.
    # working worker executing → working.
    assert len(snap.get("done", [])) == 2
    assert len(snap.get("working", [])) == 1
    assert len(snap.get("attention", [])) == 1
    assert len(snap.get("idle", [])) == 3  # 总控+铁蛋(conftest)+铁蛋-idle
