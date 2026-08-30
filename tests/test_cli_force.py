"""CLI 层: `tianji task force` 接线验收(强制终止/改派/兜底跳转/非总控负例)。"""

import json
import os

import pytest
from typer.testing import CliRunner

from tianji.cli import app
from tianji.db import connect
from tianji import ops

runner = CliRunner()


def _invoke(args, env=None):
    """调用 CLI 并断言成功,返回解析后的 JSON 输出。"""
    full = dict(env or {})
    full.setdefault("TIANJI_HOME", os.environ["TIANJI_HOME"])
    r = runner.invoke(app, args, env=full)
    assert r.exit_code == 0, f"CLI 失败 {args}: {r.output}\n{r.exception}"
    return json.loads(r.output) if r.output.strip() else {}


def _setup_op_flow(conn, controller, worker):
    """建任务→推进到 dispatched→派单(不 spawn,简化)。"""
    tid = ops.task_new(conn, controller, "force 测试任务",
                       request_id="r-cli-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s,
                            request_id=f"r-cli-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-cli-issue")["dispatch_id"]
    return tid, did


# ====================================================================
# 验收 1: 强制终止 → archived
# ====================================================================

class TestForceTerminate:
    def test_force_terminate_from_dispatched(self, tianji_home, conn,
                                              controller, worker):
        """强制终止(→archived): CLI 调用后任务归档,无审批记录。"""
        tid, did = _setup_op_flow(conn, controller, worker)
        ctrl_env = {"TIANJI_WORKER_ID": controller["worker_id"],
                    "TIANJI_SECRET": controller["secret"]}

        result = _invoke(
            ["task", "force", str(tid), "archived", "CLI强制终止",
             "--request-id", "r-cli-term"],
            env=ctrl_env)

        assert result["to"] == "archived"
        assert result["task_id"] == tid
        assert ops.task_get(conn, tid)["status"] == "archived"
        # 无审批请求
        pending = conn.execute(
            "SELECT COUNT(*) FROM force_approvals WHERE task_id=?",
            (tid,)).fetchone()[0]
        assert pending == 0


# ====================================================================
# 验收 2: 强制改派 → dispatched
# ====================================================================

class TestForceReassign:
    def test_force_reassign_from_dispatched(self, tianji_home, conn,
                                            controller, worker):
        """强制改派(→dispatched): CLI 调用后任务回 dispatched,无审批记录。"""
        tid, did = _setup_op_flow(conn, controller, worker)
        ctrl_env = {"TIANJI_WORKER_ID": controller["worker_id"],
                    "TIANJI_SECRET": controller["secret"]}

        result = _invoke(
            ["task", "force", str(tid), "dispatched", "CLI改派",
             "--request-id", "r-cli-reassign"],
            env=ctrl_env)

        assert result["to"] == "dispatched"
        assert result["task_id"] == tid
        # 重派计数+1,任务仍 in dispatched
        t = ops.task_get(conn, tid)
        assert t["status"] == "dispatched"
        assert t["retry_count"] == 1


# ====================================================================
# 验收 3: 兜底跳转 → 待审批,输出含"已落待审批,需用户批准"
# ====================================================================

class TestForceFallback:
    def test_force_fallback_creates_approval(self, tianji_home, conn,
                                             controller, worker):
        """兜底跳转(→reviewing): 创建审批请求,输出含提示语。"""
        tid, did = _setup_op_flow(conn, controller, worker)
        ctrl_env = {"TIANJI_WORKER_ID": controller["worker_id"],
                    "TIANJI_SECRET": controller["secret"]}

        result = _invoke(
            ["task", "force", str(tid), "reviewing", "CLI兜底跳转",
             "--request-id", "r-cli-fb"],
            env=ctrl_env)

        assert result["status"] == "pending"
        assert result["to"] == "reviewing"
        # 子串断言: 提示语须含批准指引命令(ops.py message 附带 approve-force 命令)
        assert "已落待审批,需用户批准" in result["message"]
        assert f"tianji task approve-force {result['approval_id']}" in result["message"]
        assert "approval_id" in result
        # 任务状态不变
        assert ops.task_get(conn, tid)["status"] == "dispatched"


# ====================================================================
# 验收 4: 非总控身份被拒
# ====================================================================

class TestForceNonControllerRejected:
    def test_non_controller_rejected(self, tianji_home, conn, controller, worker):
        """非总控调用 task force → 明确报错,不静默。"""
        tid, did = _setup_op_flow(conn, controller, worker)
        worker_env = {"TIANJI_WORKER_ID": worker["worker_id"],
                      "TIANJI_SECRET": worker["secret"]}

        r = runner.invoke(
            app, ["task", "force", str(tid), "archived", "越权"],
            env={**worker_env, "TIANJI_HOME": os.environ["TIANJI_HOME"]})
        assert r.exit_code != 0
        # 输出须包含权限拒绝信息
        combined = (r.output or "") + (r.exception and str(r.exception) or "")
        assert "总控" in combined or "permission" in combined.lower() \
               or "拒绝" in combined or "Permission" in combined


# ====================================================================
# 验收: CLI 自批被拒(ops 层拦截)
# ====================================================================

class TestForceCLISelfApproveBlocked:
    def test_cli_cannot_self_approve(self, tianji_home, conn,
                                     controller, worker):
        """CLI 用户经真实身份批准自己发起的兜底跳转被 ops 层拒绝。"""
        tid, did = _setup_op_flow(conn, controller, worker)
        ctrl_env = {"TIANJI_WORKER_ID": controller["worker_id"],
                    "TIANJI_SECRET": controller["secret"]}

        # 总控发兜底跳转
        result = _invoke(
            ["task", "force", str(tid), "reviewing", "CLI自批测试",
             "--request-id", "r-cli-self"],
            env=ctrl_env)
        aid = result["approval_id"]

        # 总控用同一身份批准 → 被拒绝(HITL)
        r = runner.invoke(
            app, ["task", "approve-force", str(aid)],
            env={**ctrl_env, "TIANJI_HOME": os.environ["TIANJI_HOME"]})
        assert r.exit_code != 0
        combined = (r.output or "") + (r.exception and str(r.exception) or "")
        assert "禁止自批" in combined or "Permission" in combined
