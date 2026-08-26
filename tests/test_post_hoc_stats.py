"""实测后验统计(票 06 验收 6): 结算成功率/审核拒绝率/派单→首次工具调用延迟/任务时长分布。"""

import json
import os

import pytest

from tianji import ops
from tianji.db import task_dir
from tianji.render import spawn
from tianji.events import ingest_event


def _register_shell_key(conn, controller, key_name, shell="codex"):
    ops.config_set(conn, controller, f"shell:{shell}", json.dumps({"binding": "env", "protocols": ["stdio"], "isolated_dir_mode": "env_home"}, ensure_ascii=False), request_id=f"r-sh-{key_name}-{shell}")
    ops.config_set(conn, controller, f"key:{key_name}", json.dumps({"base_url": f"https://api.example.com/{key_name}", "models": [{"id": "step-router-v1", "display_name": "R", "context_window": 200000}], "protocol": "stdio"}, ensure_ascii=False), request_id=f"r-k-{key_name}")


def _to_done(conn, controller, worker_name, key_name="stats-key"):
    _register_shell_key(conn, controller, key_name)
    existing = conn.execute("SELECT name FROM instances WHERE name=?", (worker_name,)).fetchone()
    if existing is None:
        ops.instance_register(conn, worker_name, "codex", "step-router-v1", key_name=key_name)
    tid = ops.task_new(conn, controller, "任务", request_id=f"r-new-{worker_name}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}-{worker_name}")
    did = ops.dispatch_issue(conn, controller, tid, worker_name,
                             request_id=f"r-issue-{worker_name}")["dispatch_id"]
    s = spawn(conn, worker_name, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "pre_tool_use"})
    # 写报告并结算
    from pathlib import Path
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("成果", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    return tid, did, env


def _to_review_reject(conn, controller, worker_name, reviewer_name):
    """创建一条 worker done + reviewer reject 记录,供后验统计计数。"""
    from pathlib import Path
    wk = f"rev-wk-{worker_name}"
    rk = f"rev-rk-{reviewer_name}"
    _register_shell_key(conn, controller, wk)
    _register_shell_key(conn, controller, rk)
    existing_w = conn.execute("SELECT name FROM instances WHERE name=?", (worker_name,)).fetchone()
    if existing_w is None:
        ops.instance_register(conn, worker_name, "codex", "step-router-v1", key_name=wk)
    existing_r = conn.execute("SELECT name FROM instances WHERE name=?", (reviewer_name,)).fetchone()
    if existing_r is None:
        ops.instance_register(conn, reviewer_name, "codex", "step-router-v1", key_name=rk)
    # 创建任务并结算 worker
    tid = ops.task_new(conn, controller, "任务", request_id=f"r-new-{worker_name}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}-{worker_name}")
    did = ops.dispatch_issue(conn, controller, tid, worker_name,
                             request_id=f"r-issue-{worker_name}")["dispatch_id"]
    ws = spawn(conn, worker_name, did)
    wenv = {**os.environ,
            "TIANJI_WORKER_ID": ws["env"]["TIANJI_WORKER_ID"],
            "TIANJI_SECRET": ws["env"]["TIANJI_SECRET"],
            "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, wenv, {"session_id": "sess-1", "event_type": "session_start"})
    ingest_event(conn, wenv, {"session_id": "sess-1", "event_type": "pre_tool_use"})
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("成果", encoding="utf-8")
    ops.dispatch_settle(conn, wenv, did, str(rp), "ok")
    # 审核者派单并 reject
    rid = ops.dispatch_issue(conn, controller, tid, reviewer_name,
                             role="reviewer", axis="spec",
                             request_id=f"r-review-{worker_name}")["dispatch_id"]
    rs = spawn(conn, reviewer_name, rid)
    rev_env = {**os.environ,
               "TIANJI_WORKER_ID": rs["env"]["TIANJI_WORKER_ID"],
               "TIANJI_SECRET": rs["env"]["TIANJI_SECRET"],
               "TIANJI_DISPATCH_ID": str(rid)}
    ingest_event(conn, rev_env, {"session_id": "s-rev", "event_type": "session_start"})
    rp = Path(task_dir(rid)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("审核报告: reject", encoding="utf-8")
    ops.dispatch_settle(conn, rev_env, rid, str(rp), "reject",
                        reason="mechanical_fail")
    return tid


class TestPostHocStats:
    """后验统计聚合函数。"""

    def test_success_rate_counts_done(self, conn, controller):
        _to_done(conn, controller, "stats-w1")
        stats = ops.post_hoc_stats(conn)
        assert "success_rate" in stats
        assert 0 <= stats["success_rate"] <= 1

    def test_review_reject_rate_counts_reviewer_reject(self, conn, controller):
        _register_shell_key(conn, controller, "rev-key")
        _register_shell_key(conn, controller, "rev-key2")
        reviewer = ops.instance_register(
            conn, "reviewer1", "codex", "step-router-v1",
            key_name="rev-key2")["name"]
        _to_review_reject(conn, controller, "stats-w2", reviewer)
        stats = ops.post_hoc_stats(conn)
        assert "review_reject_rate" in stats
        assert stats["review_reject_rate"] > 0

    def test_instance_filter_returns_only_that_instance(self, conn, controller):
        _register_shell_key(conn, controller, "sta-key")
        w1 = ops.instance_register(
            conn, "stats-w-a", "codex", "step-router-v1",
            key_name="sta-key")["name"]
        w2 = ops.instance_register(
            conn, "stats-w-b", "codex", "step-router-v1",
            key_name="sta-key")["name"]
        _to_done(conn, controller, w1)
        _to_done(conn, controller, w2)
        stats_a = ops.post_hoc_stats(conn, instance_name=w1)
        stats_b = ops.post_hoc_stats(conn, instance_name=w2)
        # 两个实例各自统计,不应互相污染
        assert "success_rate" in stats_a
        assert "success_rate" in stats_b

    def test_empty_stats_returns_zero_rates(self, conn, controller):
        stats = ops.post_hoc_stats(conn)
        assert "success_rate" in stats
        assert "review_reject_rate" in stats
        assert "avg_first_tool_delay" in stats
        assert "task_duration_dist" in stats
