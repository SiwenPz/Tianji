"""分配算法(票 06 验收 1/3): allocator_pick 硬过滤+软排序+全不合格升级。"""

import json
import os

import pytest

from tianji import ops


def _register_shell_key_multi(conn, controller, key_name):
    ops.config_set(conn, controller, "shell:codex", json.dumps({"binding": "env", "protocols": ["stdio"], "isolated_dir_mode": "env_home"}, ensure_ascii=False), request_id=f"r-sh-{key_name}")
    ops.config_set(conn, controller, f"key:{key_name}", json.dumps({"base_url": f"https://api.example.com/{key_name}", "models": [{"id": "step-router-v1", "display_name": "R", "context_window": 200000}], "protocol": "stdio"}, ensure_ascii=False), request_id=f"r-k-{key_name}")


def _make_worker(conn, controller, name, skills, context_window, permission,
                 score=60, key_name=None):
    if key_name is None:
        key_name = f"key-{name}"
    _register_shell_key_multi(conn, controller, key_name)
    ops.instance_register(
        conn, name, "codex", "step-router-v1",
        skills=json.dumps(skills, ensure_ascii=False),
        context_window=context_window,
        permission_granularity=permission,
        key_name=key_name)
    # 手动设置 score
    conn.execute(
        "UPDATE ability_profiles SET score=? WHERE instance_name=?",
        (score, name))
    return name


def _to_dispatched(conn, controller, title, priority=0):
    tid = ops.task_new(conn, controller, title, priority=priority,
                       request_id=f"r-{title}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s,
                            request_id=f"r-{title}-{s}")
    return tid


class TestAllocatorHardFilter:
    """硬过滤: 窗口不足/权限不足/忙者不可选。"""

    def test_context_window_too_small_filtered(self, conn, controller):
        """上下文窗口装不下任务预期规模 → 硬过滤跳过。"""
        _make_worker(conn, controller, "small-win", ["coding"], 500,
                     "project", score=90, key_name="ak-small")
        _make_worker(conn, controller, "big-win", ["coding"], 50000,
                     "project", score=50, key_name="ak-big")
        tid = _to_dispatched(conn, controller, "大任务", priority=1)
        ops.config_set(conn, controller, "expect_min_simple", "15",
                       request_id="r-es")
        ops.config_set(conn, controller, "expect_min_normal", "30",
                       request_id="r-en")
        ops.config_set(conn, controller, "expect_min_hard", "60",
                       request_id="r-eh")
        # 任务 priority=1 → normal → expect_min=30 → 粗估 4000
        pick = ops.allocator_pick(conn, tid)
        assert pick == "big-win"

    def test_permission_granularity_insufficient_filtered(self, conn, controller):
        """权限粒度不足且任务 priority>0 → 硬过滤跳过。"""
        _make_worker(conn, controller, "readonly-w", ["coding"], 50000,
                     "readonly", score=90, key_name="ak-readonly")
        _make_worker(conn, controller, "project-w", ["coding"], 50000,
                     "project", score=50, key_name="ak-project")
        tid = _to_dispatched(conn, controller, "需权限任务", priority=1)
        pick = ops.allocator_pick(conn, tid)
        assert pick == "project-w"

    def test_busy_worker_excluded(self, conn, controller, worker):
        """已有活跃派单的实施者不可选。"""
        _make_worker(conn, controller, "idle-w", ["coding"], 50000,
                     "project", score=80, key_name="ak-idle")
        _make_worker(conn, controller, "busy-w", ["coding"], 50000,
                     "project", score=90, key_name="ak-busy")
        tid1 = _to_dispatched(conn, controller, "任务1", priority=1)
        ops.dispatch_issue(conn, controller, tid1, "busy-w",
                           request_id="r-issue1")
        tid2 = _to_dispatched(conn, controller, "任务2", priority=1)
        pick = ops.allocator_pick(conn, tid2)
        assert pick == "idle-w"


class TestAllocatorSoftSort:
    """软排序: 表现分降序+擅长面标签命中加分。"""

    def test_score_desc_then_skills_bonus(self, conn, controller):
        """擅长面命中加分可使较低表现分反超。"""
        _make_worker(conn, controller, "no-bonus", ["other"], 50000,
                     "project", score=80, key_name="ak-nb")
        _make_worker(conn, controller, "with-bonus", ["coding"], 50000,
                     "project", score=79, key_name="ak-wb")
        _make_worker(conn, controller, "low-score", ["other"], 50000,
                     "project", score=70, key_name="ak-ls")
        tid = _to_dispatched(conn, controller, "coding 任务", priority=0)
        # with-bonus: 79+5=84 > no-bonus: 80, 擅长面命中加分反超
        pick = ops.allocator_pick(conn, tid)
        assert pick == "with-bonus"

    def test_no_skills_match_falls_back_to_score(self, conn, controller):
        """无擅长面命中时纯按表现分排序。"""
        _make_worker(conn, controller, "a", ["a"], 50000, "project", score=70, key_name="ak-a")
        _make_worker(conn, controller, "b", ["b"], 50000, "project", score=90, key_name="ak-b")
        tid = _to_dispatched(conn, controller, "zzz-no-match", priority=0)
        pick = ops.allocator_pick(conn, tid)
        assert pick == "b"


class TestAllocatorEscalation:
    """全不合格 → 升级总控,不自动派单。"""

    def test_all_filtered_escalates_and_returns_none(self, conn, controller):
        _make_worker(conn, controller, "small", [], 500, "readonly", score=10, key_name="ak-small2")
        tid = _to_dispatched(conn, controller, "大任务需权限", priority=1)
        pick = ops.allocator_pick(conn, tid)
        assert pick is None
        msgs = conn.execute(
            "SELECT * FROM messages WHERE type='escalation'"
        ).fetchall()
        assert len(msgs) >= 1
        payload = json.loads(msgs[-1]["payload"])
        assert payload["task_id"] == tid
