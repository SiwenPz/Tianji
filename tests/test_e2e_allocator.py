"""端到端分配+结算+评分(票 06 验收 1/2): allocator_pick → dispatch → settle → score。"""

import json
import os

import pytest

from tianji import ops
from tianji.db import task_dir
from tianji.render import spawn
from tianji.events import ingest_event


def _register_shell_key(conn, controller, key_name):
    ops.config_set(conn, controller, "shell:codex", json.dumps({"binding": "env", "protocols": ["stdio"], "isolated_dir_mode": "env_home"}, ensure_ascii=False), request_id=f"r-sh-{key_name}")
    ops.config_set(conn, controller, f"key:{key_name}", json.dumps({"base_url": f"https://api.example.com/{key_name}", "models": [{"id": "step-router-v1", "display_name": "R", "context_window": 50000}], "protocol": "stdio"}, ensure_ascii=False), request_id=f"r-k-{key_name}")


def _to_executing(conn, controller, worker_name):
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker_name,
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker_name, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "pre_tool_use"})
    return tid, did, env


class TestE2EAllocatorScore:
    """分配→执行→结算→评分全链路。"""

    def test_pick_dispatch_and_settle_updates_score(self, conn, controller):
        _register_shell_key(conn, controller, "e2e-key")
        coder = ops.instance_register(
            conn, "coder-a", "codex", "step-router-v1",
            skills=json.dumps(["coding"], ensure_ascii=False),
            context_window=50000, permission_granularity="project",
            key_name="e2e-key")["name"]
        ops.config_set(conn, controller, "expect_min_simple", "15",
                       request_id="r-es")
        ops.config_set(conn, controller, "expect_min_normal", "30",
                       request_id="r-en")
        ops.config_set(conn, controller, "expect_min_hard", "60",
                       request_id="r-eh")
        tid = ops.task_new(conn, controller, "coding 任务", request_id="r-t")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s, request_id=f"r-t-{s}")
        pick = ops.allocator_pick(conn, tid)
        assert pick == "coder-a"
        # allocator_pick 只选人,派单由 _to_executing 统一执行
        tid2, did2, env = _to_executing(conn, controller, pick)
        from pathlib import Path
        rp = Path(task_dir(did2)) / "report.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("成果报告", encoding="utf-8")
        r = ops.dispatch_settle(conn, env, did2, str(rp), "ok")
        assert r["status"] == "done"
        row = conn.execute(
            "SELECT score FROM ability_profiles WHERE instance_name=?",
            (pick,)).fetchone()
        assert row["score"] != 60  # 评分已更新

    def test_all_filtered_no_dispatch(self, conn, controller):
        """全过滤后应返回 None,不应产生新派单。"""
        _register_shell_key(conn, controller, "bad-key")
        ops.instance_register(
            conn, "bad-a", "codex", "step-router-v1",
            context_window=500, permission_granularity="readonly",
            key_name="bad-key")
        tid = ops.task_new(conn, controller, "大任务", priority=1,
                           request_id="r-bad")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r-bad-{s}")
        pick = ops.allocator_pick(conn, tid)
        assert pick is None
        # 不应产生派单
        disps = conn.execute(
            "SELECT COUNT(*) AS n FROM dispatches WHERE task_id=?",
            (tid,)).fetchone()["n"]
        assert disps == 0
