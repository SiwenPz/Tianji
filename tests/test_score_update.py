"""表现分 5 档更新(票 06 验收 2/4): 加权移动平均+5 档分值。"""

import json
import time

import pytest

from tianji import ops


def _register_shell_key(conn, controller, key_name):
    ops.config_set(conn, controller, "shell:codex", json.dumps({"binding": "env", "protocols": ["stdio"], "isolated_dir_mode": "env_home"}, ensure_ascii=False), request_id=f"r-sh-{key_name}")
    ops.config_set(conn, controller, f"key:{key_name}", json.dumps({"base_url": f"https://api.example.com/{key_name}", "models": [{"id": "step-router-v1", "display_name": "R", "context_window": 200000}], "protocol": "stdio"}, ensure_ascii=False), request_id=f"r-k-{key_name}")


def _register_and_set_score(conn, controller, name, score=60, key_name=None):
    if key_name is None:
        key_name = f"sk-{name}"
    _register_shell_key(conn, controller, key_name)
    ops.instance_register(conn, name, "codex", "step-router-v1", key_name=key_name)
    conn.execute(
        "UPDATE ability_profiles SET score=? WHERE instance_name=?",
        (score, name))
    return name


def _to_executing(conn, controller, worker_name):
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker_name,
                             request_id="r-issue")["dispatch_id"]
    from tianji.render import spawn
    from tianji.events import ingest_event
    s = spawn(conn, worker_name, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "pre_tool_use"})
    return tid, did, env


import os


class TestScoreUpdate:
    """表现分按 5 档事件更新,近 10 单加权移动平均。"""

    def test_on_time_settle_adds_score(self, conn, controller):
        name = _register_and_set_score(conn, controller, "on-time", score=60)
        tid, did, env = _to_executing(conn, controller, name)
        from pathlib import Path
        from tianji.db import task_dir
        rp = Path(task_dir(did)) / "report.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("ok", encoding="utf-8")
        # expect_min 默认 30, 实际在 expect_min 内 → on_time
        new_score = ops.update_score(conn, name, "on_time", expect_min=30,
                                     actual_minutes=10)
        assert new_score > 60

    def test_overtime_settle_adds_less(self, conn, controller):
        name = _register_and_set_score(conn, controller, "overtime", score=60)
        # expect_min=30, actual=45 → overtime
        new_score = ops.update_score(conn, name, "overtime", expect_min=30,
                                     actual_minutes=45)
        assert new_score > 60
        # on_time(10) > overtime(+5 权重)

    def test_progress_exceed_decreases_score(self, conn, controller):
        name = _register_and_set_score(conn, controller, "progress", score=60)
        new_score = ops.update_score(conn, name, "progress_exceed",
                                     expect_min=30, actual_minutes=70)
        assert new_score < 60

    def test_review_reject_decreases_score(self, conn, controller):
        name = _register_and_set_score(conn, controller, "rejected", score=60)
        new_score = ops.update_score(conn, name, "review_reject",
                                     expect_min=30)
        assert new_score < 60

    def test_process_dead_decreases_score(self, conn, controller):
        name = _register_and_set_score(conn, controller, "dead", score=60)
        new_score = ops.update_score(conn, name, "process_dead", expect_min=30)
        assert new_score < 60

    def test_weighted_moving_average_keeps_recent(self, conn, controller):
        name = _register_and_set_score(conn, controller, "wma", score=60)
        # 连续 5 次 on_time(+10), score 应明显上升
        for _ in range(5):
            ops.update_score(conn, name, "on_time", expect_min=30,
                             actual_minutes=10)
        row = conn.execute(
            "SELECT score FROM ability_profiles WHERE instance_name=?",
            (name,)).fetchone()
        assert row["score"] > 60
        history = json.loads(
            conn.execute(
                "SELECT score_history FROM ability_profiles WHERE instance_name=?",
                (name,)).fetchone()["score_history"])
        assert len(history) == 5

    def test_score_clamped_between_0_and_100(self, conn, controller):
        name = _register_and_set_score(conn, controller, "clamp", score=5)
        # 多次 progress_exceed(-8) 不应低于 0
        for _ in range(20):
            ops.update_score(conn, name, "progress_exceed", expect_min=30,
                             actual_minutes=100)
        row = conn.execute(
            "SELECT score FROM ability_profiles WHERE instance_name=?",
            (name,)).fetchone()
        assert 0 <= row["score"] <= 100

    def test_score_history_truncated_to_10(self, conn, controller):
        name = _register_and_set_score(conn, controller, "trunc", score=60)
        for _ in range(15):
            ops.update_score(conn, name, "on_time", expect_min=30,
                             actual_minutes=10)
        history = json.loads(
            conn.execute(
                "SELECT score_history FROM ability_profiles WHERE instance_name=?",
                (name,)).fetchone()["score_history"])
        assert len(history) == 10
