"""票 47: 硬约束补齐——实施者≠审核者(1.2e)、实施者≠架构师(1.2d)。

history-aware: 全量参与者历史(含 done/stale/requeue,排除 cancelled)。
单向 _task_participants + 双向约束(审核者≠历史实施者 ∧ 实施者≠历史审核者)。
计划产出闸门: discussing→awaiting_plan_confirm 也接入 1.2d。
"""

import os
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import connect, task_dir
from tianji.render import spawn


# ====================================================================
# 夹具辅助
# ====================================================================

def _register(conn, name, shell, model):
    return ops.instance_register(conn, name, shell, model,
                                 launch_cmd="python mock_worker.py",
                                 controller=False)


def _make_worker_dispatch(conn, task_id, worker_id, status="active"):
    """直接插入一条实施者派单记录。"""
    conn.execute(
        "INSERT INTO dispatches "
        "(task_id, worker_id, worker_role, axis, status, dcap_hash,"
        " expect_min, task_dir, payload, worktree_path,"
        " created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, worker_id, "worker", "", status, "", 15,
         "", "{}", "", ops.now(), ops.now()),
    )


def _make_reviewer_dispatch(conn, task_id, reviewer_id, status="active"):
    """直接插入一条审核者派单记录。"""
    conn.execute(
        "INSERT INTO dispatches "
        "(task_id, worker_id, worker_role, axis, status, dcap_hash,"
        " expect_min, task_dir, payload, worktree_path,"
        " created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, reviewer_id, "reviewer", "spec", status, "", 15,
         "", "{}", "", ops.now(), ops.now()),
    )


def _build_task_for_1_2e(conn, controller, worker_id, request_id):
    """建任务→派实施者单→推进到 reviewing,供 1.2e 审核时序测试。

    返回 (task_id, worker_dispatch_id)。
    """
    _register(conn, worker_id, "codex", "step-router-v1")
    tid, did = _build_task_to_dispatched(conn, controller, worker_id, request_id)
    _settle_done(conn, tid, did, worker_id)
    return tid, did


def _build_task_to_dispatched(conn, controller, worker_id, request_id):
    """建任务→推进到 dispatched,派实施者单。返回 (task_id, dispatch_id)。"""
    tid = ops.task_new(conn, controller, "票47测试",
                       request_id=request_id)["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s,
                            request_id=f"{request_id}-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker_id,
                             request_id=f"{request_id}-w")["dispatch_id"]
    return tid, did


def _build_task_planning_phase(conn, controller, worker_id="", request_id=""):
    """建任务→推进到 awaiting_plan_confirm。

    worker_id 非空时直接插入实施者派单(模拟实施者场景,不注册实例)。
    返回 task_id。
    """
    tid = ops.task_new(conn, controller, "票47测试",
                       request_id=request_id)["task_id"]
    for s in ("discussing", "awaiting_plan_confirm"):
        ops.task_transition(conn, controller, tid, s,
                            request_id=f"{request_id}-{s}")
    if worker_id:
        _make_worker_dispatch(conn, tid, worker_id)
    return tid


def _settle_done(conn, tid, did, worker_id):
    """spawn + settle done,把工人推进到 reviewing。"""
    s = spawn(conn, worker_id, did)
    env = {**os.environ, "TIANJI_WORKER_ID": worker_id,
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("工人报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")


def _settle_reviewer(conn, did, reviewer_id):
    """spawn + settle pass for reviewer."""
    s = spawn(conn, reviewer_id, did)
    env = {**os.environ, "TIANJI_WORKER_ID": reviewer_id,
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("审核报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "pass")


# ====================================================================
# 1.2e: 审核者≠实施者(history-aware, 双向)
# ====================================================================

class TestReviewerNotImplementer:
    """主路径: 实施者 done 后同实例派 reviewer → 拒绝。
    反向: 审核者 done 后同实例派 worker → 拒绝。
    最小配置串行双轴 → 放行(工人≠审核者)。
    """

    def test_main_path_same_instance_done_then_reviewer_rejected(self, conn, controller):
        """核心路径: 工人 settle done 后,同实例派 reviewer → 1.2e 拒绝。"""
        tid, _ = _build_task_for_1_2e(conn, controller, "工人甲", "r47a")
        with pytest.raises(ValueError, match=r"不能审自己的活|1\.2e"):
            ops.dispatch_issue(conn, controller, tid, "工人甲",
                               role="reviewer", axis="spec",
                               request_id="r47a-rev")

    def test_different_instance_reviewer_after_implementer_done_allowed(self, conn, controller):
        """工人 done 后,不同实例派 reviewer → 放行。"""
        tid, _ = _build_task_for_1_2e(conn, controller, "工人乙", "r47b")
        _register(conn, "审核乙", "claude", "deepseek-v4-flash")
        ops.dispatch_issue(conn, controller, tid, "审核乙",
                           role="reviewer", axis="spec",
                           request_id="r47b-rev")

    def test_reverse_same_instance_reviewer_done_then_worker_rejected(self, conn, controller):
        """反向自审: 某人先当 reviewer(settle done),再被派 worker → 反向 1.2e 拒绝。"""
        _register(conn, "工人丙", "codex", "step-router-v1")
        tid, did_w = _build_task_to_dispatched(conn, controller, "工人丙", "r47c-w")
        _settle_done(conn, tid, did_w, "工人丙")
        # task in reviewing,派审核单给审核丙
        _register(conn, "审核丙", "claude", "deepseek-v4-flash")
        did_r = ops.dispatch_issue(conn, controller, tid, "审核丙",
                                   role="reviewer", axis="spec",
                                   request_id="r47c-rev")["dispatch_id"]
        _settle_reviewer(conn, did_r, "审核丙")
        # 审核丙已成为历史审核者,尝试派 worker → 反向 1.2e 拒绝
        with pytest.raises(ValueError, match=r"反向.*1\.2e|历史审核者"):
            ops.dispatch_issue(conn, controller, tid, "审核丙",
                               role="worker",
                               request_id="r47c-rw")

    def test_min_config_serial_dual_axis_not_affected(self, conn, controller):
        """最小配置: 审核单例串行跑两轴(工人是另一实例) → 不受影响。"""
        _register(conn, "工人单例会", "claude", "deepseek-v4-flash")
        tid, did_w = _build_task_to_dispatched(conn, controller, "工人单例会", "r47d")
        _settle_done(conn, tid, did_w, "工人单例会")
        _register(conn, "审核单例", "claude", "deepseek-v4-flash")
        d1 = ops.dispatch_issue(conn, controller, tid, "审核单例",
                                role="reviewer", axis="spec",
                                request_id="r47d-spec")
        s = spawn(conn, "审核单例", d1["dispatch_id"])
        env = {**os.environ, "TIANJI_WORKER_ID": "审核单例",
               "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
               "TIANJI_DISPATCH_ID": str(d1["dispatch_id"])}
        rp = Path(task_dir(d1["dispatch_id"])) / "report.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("spec轴报告", encoding="utf-8")
        ops.dispatch_settle(conn, env, d1["dispatch_id"], str(rp), "pass")
        d2 = ops.dispatch_issue(conn, controller, tid, "审核单例",
                                role="reviewer", axis="quality",
                                request_id="r47d-quality")
        assert d2["dispatch_id"] != d1["dispatch_id"]

    def test_reschedule_old_implementer_blocked_as_reviewer(self, conn, controller):
        """驳回重派后,旧工人(已 done)不能再当 reviewer。"""
        _register(conn, "旧工人", "codex", "step-router-v1")
        tid, did_w = _build_task_to_dispatched(conn, controller, "旧工人", "r47e-w")
        _settle_done(conn, tid, did_w, "旧工人")
        # 审核者 reject → 驳回
        _register(conn, "审核驳回", "claude", "deepseek-v4-flash")
        did_r = ops.dispatch_issue(conn, controller, tid, "审核驳回",
                                   role="reviewer", axis="spec",
                                   request_id="r47e-rev")["dispatch_id"]
        s_r = spawn(conn, "审核驳回", did_r)
        env_r = {**os.environ, "TIANJI_WORKER_ID": "审核驳回",
                 "TIANJI_SECRET": s_r["env"]["TIANJI_SECRET"],
                 "TIANJI_DISPATCH_ID": str(did_r)}
        rp = Path(task_dir(did_r)) / "report.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("驳回报告", encoding="utf-8")
        ops.dispatch_settle(conn, env_r, did_r, str(rp), "reject",
                            reason="重做")
        # 旧工人想当新 reviewer → 拒绝
        with pytest.raises(ValueError, match=r"1\.2e"):
            ops.dispatch_issue(conn, controller, tid, "旧工人",
                               role="reviewer", axis="spec",
                               request_id="r47e-rev2")


# ====================================================================
# 1.2d: 架构师(总控)≠实施者(history-aware, 计划产出闸门)
# ====================================================================

class TestArchitectNotImplementer:
    """总控身份不得是该任务任何历史实施者。三个闸门: 验证命令、边界声明、计划推进。"""

    def test_controller_implementer_verify_cmd_rejected(self, conn, controller):
        """总控是历史实施者(派单 done) → 写验收命令拒绝。"""
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47f-new")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r47f-{s}")
        _make_worker_dispatch(conn, tid, controller["worker_id"], status="done")
        with pytest.raises(ValueError, match=r"不能写自己的验收命令|1\.2d"):
            ops.task_set_verify_cmd(conn, controller, tid,
                                    "python -c 'pass'",
                                    request_id="r47f-vc")

    def test_controller_implementer_scope_set_rejected(self, conn, controller):
        """总控是历史实施者(派单 done) → 写边界声明拒绝。"""
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47g-new")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r47g-{s}")
        _make_worker_dispatch(conn, tid, controller["worker_id"], status="done")
        with pytest.raises(ValueError, match=r"不能写自己的边界声明|1\.2d"):
            ops.task_scope_set(conn, controller, tid, ["src"],
                               request_id="r47g-sc")

    def test_controller_not_implementer_allowed(self, conn, controller):
        """总控不是实施者 → 验证命令/边界声明放行。"""
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47h-new")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r47h-{s}")
        ops.task_set_verify_cmd(conn, controller, tid,
                                "python -c 'pass'",
                                request_id="r47h-vc")
        ops.task_scope_set(conn, controller, tid, ["src"],
                           request_id="r47h-sc")

    def test_controller_implementer_plan_gate_rejected(self, conn, controller):
        """计划产出闸门: discussing→awaiting_plan_confirm,总控是历史实施者→拒绝。"""
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47i-new")["task_id"]
        ops.task_transition(conn, controller, tid, "discussing",
                            request_id="r47i-dis")
        _make_worker_dispatch(conn, tid, controller["worker_id"], status="done")
        with pytest.raises(ValueError, match=r"不能推进自己的计划|1\.2d"):
            ops.task_transition(conn, controller, tid, "awaiting_plan_confirm",
                                request_id="r47i-ap")

    def test_plan_gate_allowed_other_implementer(self, conn, controller):
        """计划产出闸门: discussing→awaiting_plan_confirm,实施者是其他人 → 放行。"""
        _register(conn, "工人网关", "codex", "step-router-v1")
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47j-new")["task_id"]
        ops.task_transition(conn, controller, tid, "discussing",
                            request_id="r47j-dis")
        _make_worker_dispatch(conn, tid, "工人网关")
        # 总控不是工人网关 → 放行
        ops.task_transition(conn, controller, tid, "awaiting_plan_confirm",
                            request_id="r47j-ap")

    def test_reschedule_old_implementer_verify_cmd_rejected(self, conn, controller):
        """总控做过 worker → 验收命令拒绝。"""
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47k-new")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r47k-{s}")
        # 总控有 worker 派单(模拟总控参与实施)
        _make_worker_dispatch(conn, tid, controller["worker_id"], status="done")
        with pytest.raises(ValueError, match=r"不能写自己的验收命令|1\.2d"):
            ops.task_set_verify_cmd(conn, controller, tid,
                                    "python -c 'pass'",
                                    request_id="r47k-vc")


# ====================================================================
# 回补: 正向封堵 + 架构师裁决 + cancelled 历史语义
# ====================================================================


class TestArchitectForwardConstraint:
    """Blocker + High 补完: 1.2d forward 封堵 + architect_confirm/review 接入。"""

    def test_controller_writes_plan_then_dispatches_self_blocked(self, conn, controller):
        """时序漏洞封堵: 总控写计划后派自己为实施者 → 1.2d 拒绝。"""
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47fx-new")["task_id"]
        ops.task_transition(conn, controller, tid, "discussing",
                            request_id="r47fx-dis")
        # 先写计划(记录 architect_worker_id = 总控)
        ops.task_set_verify_cmd(conn, controller, tid, "python -c 'pass'",
                                request_id="r47fx-vc")
        ops.task_transition(conn, controller, tid, "awaiting_plan_confirm",
                            request_id="r47fx-ap")
        ops.task_transition(conn, controller, tid, "dispatched",
                            request_id="r47fx-dpd")
        with pytest.raises(ValueError, match=r"架构师.*实施者|1\.2d"):
            ops.dispatch_issue(conn, controller, tid, controller["worker_id"],
                               request_id="r47fx-w")

    def test_architect_confirm_self_implementer_blocked(self, conn, controller):
        """架构师确认: 总控是历史实施者(直接插派单) → architect_confirm 拒绝。"""
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47ay-new")["task_id"]
        ops.task_transition(conn, controller, tid, "discussing",
                            request_id="r47ay-dis")
        ops.task_set_verify_cmd(conn, controller, tid, "python -c 'pass'",
                                request_id="r47ay-vc")
        # 总控同时是实施者
        _make_worker_dispatch(conn, tid, controller["worker_id"], status="done")
        conn.execute("UPDATE tasks SET status='reviewing', architect_verdict='' WHERE id=?",
                     (tid,))
        with pytest.raises(ValueError, match=r"1\.2d"):
            ops.architect_confirm(conn, controller, tid,
                                  reason="test", request_id="r47ay-ac")

    def test_architect_review_self_implementer_blocked(self, conn, controller):
        """架构师裁决: 总控是历史实施者(直接插派单) → architect_review 拒绝。"""
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47by-new")["task_id"]
        ops.task_transition(conn, controller, tid, "discussing",
                            request_id="r47by-dis")
        ops.task_set_verify_cmd(conn, controller, tid, "python -c 'pass'",
                                request_id="r47by-vc")
        _make_worker_dispatch(conn, tid, controller["worker_id"], status="done")
        conn.execute("UPDATE tasks SET status='reviewing', architect_verdict='' WHERE id=?",
                     (tid,))
        with pytest.raises(ValueError, match=r"1\.2d"):
            ops.architect_review(conn, controller, tid, verdict="reject",
                                 reason="test", request_id="r47by-ar")

    def test_cancelled_worker_blocks_reviewer(self, conn, controller):
        """cancelled 派单仍视为历史参与者: 同实例开工后取消,不能当 reviewer。"""
        _register(conn, "坎", "codex", "step-router-v1")
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47cx-new")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r47cx-{s}")
        did = ops.dispatch_issue(conn, controller, tid, "坎",
                                 request_id="r47cx-w")["dispatch_id"]
        # 取消工人派单(模拟开工后取消)
        conn.execute("UPDATE dispatches SET status='cancelled' WHERE id=?",
                     (did,))
        # 同实例(坎)想当 reviewer → 因 cancelled 仍被视为历史实施者 → 拒绝
        with pytest.raises(ValueError, match=r"1\.2e"):
            ops.dispatch_issue(conn, controller, tid, "坎",
                               role="reviewer", axis="spec",
                               request_id="r47cx-rev")

    def test_plan_gate_persists_architect_blocks_self_dispatch(self, conn, controller):
        """正向绕路过补封: 不写 verify_cmd/scope,直接推进到 dispatched,再派自己 → 1.2d 拒绝。"""
        tid = ops.task_new(conn, controller, "票47测试",
                           request_id="r47bx-new")["task_id"]
        ops.task_transition(conn, controller, tid, "discussing",
                            request_id="r47bx-dis")
        # 不写 verify_cmd/scope,直接推进到 dispatched
        ops.task_transition(conn, controller, tid, "awaiting_plan_confirm",
                            request_id="r47bx-ap")
        ops.task_transition(conn, controller, tid, "dispatched",
                            request_id="r47bx-dpd")
        # 验证 architect_worker_id 已被定格
        t = ops.task_get(conn, tid)
        assert t.get("architect_worker_id") == controller["worker_id"], \
            "计划闸门应定格架构师身份"
        # 派自己为实施者 → 1.2d 拒绝
        with pytest.raises(ValueError, match=r"架构师.*实施者|1\.2d"):
            ops.dispatch_issue(conn, controller, tid, controller["worker_id"],
                               request_id="r47bx-w")
