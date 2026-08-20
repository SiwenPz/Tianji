"""expect_min 三档默认(票 06 验收 5): configs 存 simple/normal/hard + dispatch_issue 集成。"""

import pytest

from tianji import ops


def _to_dispatched(conn, controller, title, priority=0):
    tid = ops.task_new(conn, controller, title, priority=priority,
                       request_id=f"r-{title}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s,
                            request_id=f"r-{title}-{s}")
    return tid


class TestExpectMinDefaults:
    """expect_min 三档默认值来自 configs,按任务复杂度映射。"""

    def test_simple_task_gets_simple_expect(self, conn, controller):
        _to_dispatched(conn, controller, "简单任务", priority=0)
        ops.config_set(conn, controller, "expect_min_simple", "15",
                       request_id="r-es")
        ops.config_set(conn, controller, "expect_min_normal", "30",
                       request_id="r-en")
        ops.config_set(conn, controller, "expect_min_hard", "60",
                       request_id="r-eh")
        ops.instance_register(conn, "exp-worker", "codex", "step-router-v1")
        tid = _to_dispatched(conn, controller, "简单", priority=0)
        d = ops.dispatch_issue(conn, controller, tid, "exp-worker",
                               request_id="r-issue")
        assert d["expect_min"] == 15

    def test_normal_task_gets_normal_expect(self, conn, controller):
        _to_dispatched(conn, controller, "中等任务", priority=1)
        ops.config_set(conn, controller, "expect_min_simple", "15",
                       request_id="r-es2")
        ops.config_set(conn, controller, "expect_min_normal", "30",
                       request_id="r-en2")
        ops.config_set(conn, controller, "expect_min_hard", "60",
                       request_id="r-eh2")
        ops.instance_register(conn, "exp-worker2", "codex", "step-router-v1")
        tid = _to_dispatched(conn, controller, "中等", priority=1)
        d = ops.dispatch_issue(conn, controller, tid, "exp-worker2",
                               request_id="r-issue2")
        assert d["expect_min"] == 30

    def test_hard_task_gets_hard_expect(self, conn, controller):
        _to_dispatched(conn, controller, "困难任务", priority=3)
        ops.config_set(conn, controller, "expect_min_simple", "15",
                       request_id="r-es3")
        ops.config_set(conn, controller, "expect_min_normal", "30",
                       request_id="r-en3")
        ops.config_set(conn, controller, "expect_min_hard", "60",
                       request_id="r-eh3")
        ops.instance_register(conn, "exp-worker3", "codex", "step-router-v1")
        tid = _to_dispatched(conn, controller, "困难", priority=3)
        d = ops.dispatch_issue(conn, controller, tid, "exp-worker3",
                               request_id="r-issue3")
        assert d["expect_min"] == 60

    def test_explicit_expect_min_overrides_default(self, conn, controller):
        """显式传入 expect_min 时,不按三档覆盖。"""
        _to_dispatched(conn, controller, "自定义", priority=0)
        ops.config_set(conn, controller, "expect_min_simple", "15",
                       request_id="r-es4")
        ops.instance_register(conn, "exp-worker4", "codex", "step-router-v1")
        tid = _to_dispatched(conn, controller, "自定义", priority=0)
        d = ops.dispatch_issue(conn, controller, tid, "exp-worker4",
                               expect_min=99, request_id="r-issue4")
        assert d["expect_min"] == 99
