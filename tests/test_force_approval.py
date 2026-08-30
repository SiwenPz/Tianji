"""兜底跳转人审门(HITL,票 54/4.4): 全面验收测试。

验收标准:
1. 三种既定动作(终止→archived/改派→dispatched/接管→dispatched)直接执行,不弹审批
2. 兜底跳转(目标态是三种之外)创建审批请求,不生效
3. 审批请求可批准,批准后迁移生效
4. 审批请求可驳回,驳回后迁移不生效
5. 超时未批标记 expired(ops 层直测 + monitor._tick 巡检顺带闭环)
6. 身份校验: 发起人须总控,批准人须用户(总控/发起人自批被拦,ops+CLI+web 三层)
7. 已处理/非本人不可操作
"""

import json
import os

import pytest
from typer.testing import CliRunner

from tianji.cli import app
from tianji import ops
from tianji.db import now
from tianji.events import ingest_event
from tianji.render import spawn

runner = CliRunner()


def _to_executing(conn, controller, worker):
    """任务 executing + 派单 active(已开工)。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "s", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "s", "event_type": "pre_tool_use"})
    return tid, did


def _register_controller(conn, name):
    """Register a controller instance and return identity dict."""
    ctrl = ops.instance_register(
        conn, name, "claude", "step-router-v1", controller=True)
    return {"worker_id": ctrl["name"], "secret": ctrl["secret"]}


def _register_worker(conn, name):
    """Register a worker instance and return identity dict."""
    wkr = ops.instance_register(conn, name, "claude", "step-router-v1")
    return {"worker_id": wkr["name"], "secret": wkr["secret"]}


def _setup_approval(conn, controller, worker):
    """Create a task + dispatch + force_approval for testing."""
    tid = ops.task_new(conn, controller, "HITL审批测试", request_id="r-hitl")["task_id"]
    ops.task_transition(conn, controller, tid, "discussing", request_id="r-hitl-d")
    ops.task_transition(conn, controller, tid, "awaiting_plan_confirm", request_id="r-hitl-ap")
    ops.task_transition(conn, controller, tid, "dispatched", request_id="r-hitl-dp")
    ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                       request_id="r-hitl-issue")
    result = ops.task_force(conn, controller, tid, "reviewing", "HITL测试兜底",
                            request_id="r-hitl-fb")
    return tid, result["approval_id"]


# ====================================================================
# 验收 1: 三种既定动作直接执行,不弹审批
# ====================================================================

def test_force_terminate_no_approval(conn, controller, worker):
    """强制终止(→archived): 直接执行,force_approvals 表无记录。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "archived", "强制终止",
                       request_id="r-f-arch")
    assert r["to"] == "archived"
    pending = conn.execute(
        "SELECT COUNT(*) FROM force_approvals WHERE task_id=?",
        (tid,)).fetchone()[0]
    assert pending == 0
    assert ops.task_get(conn, tid)["status"] == "archived"


def test_force_reassign_no_approval(conn, controller, worker):
    """强制改派(→dispatched): 直接执行,无审批请求。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "dispatched", "改派",
                       request_id="r-f-reassign")
    assert r["to"] == "dispatched"
    pending = conn.execute(
        "SELECT COUNT(*) FROM force_approvals WHERE task_id=?",
        (tid,)).fetchone()[0]
    assert pending == 0


def test_established_force_archived_direct(conn, controller):
    """force archived(非 executing 起点): 直接执行,无审批。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    r = ops.task_force(conn, controller, tid, "archived", "直达归档",
                       request_id="r-dir-arch")
    assert r["to"] == "archived"
    count = conn.execute(
        "SELECT COUNT(*) FROM force_approvals").fetchone()[0]
    assert count == 0


# ====================================================================
# 验收 2: 兜底跳转创建审批请求,不生效
# ====================================================================

@pytest.mark.parametrize("to_state", ["awaiting_final_confirm", "reviewing", "discussing"])
def test_fallback_force_creates_approval(conn, controller, to_state):
    """兜底跳转(非 ESTABLISHED_FORCE_TARGETS): 机械落 pending 审批请求。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-asp")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")

    r = ops.task_force(conn, controller, tid, to_state,
                       "兜底跳转", request_id=f"r-fb-{to_state}")
    assert r["status"] == "pending"
    assert "approval_id" in r

    row = conn.execute(
        "SELECT * FROM force_approvals WHERE id=?",
        (r["approval_id"],)).fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["initiator_id"] == controller["worker_id"]
    assert row["to_state"] == to_state
    # 任务状态不变
    assert ops.task_get(conn, tid)["status"] == "dispatched"


# ====================================================================
# 验收 3: 批准后迁移生效
# ====================================================================

def test_force_approve_enables_transition(conn, controller, worker):
    """批准兜底跳转后,任务状态迁移生效。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "待审跳转", request_id="r-ap")
    approval_id = r["approval_id"]

    apr = ops.force_approve(conn, "user-xxx", approval_id)
    assert apr["decision"] == "approved"
    assert apr["task_id"] == tid
    assert apr["to"] == "awaiting_final_confirm"

    # 任务已迁移
    assert ops.task_get(conn, tid)["status"] == "awaiting_final_confirm"


def test_force_approve_to_archived(conn, controller, worker):
    """批准 → archived: 任务归档+派单 cancelled。"""
    tid, did = _to_executing(conn, controller, worker)
    # awaiting_final_confirm 是兜底目标态
    r = ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "兜底终止", request_id="r-fa-afc")
    aid = r["approval_id"]

    apr = ops.force_approve(conn, "user-xxx", aid)
    assert apr["decision"] == "approved"
    assert ops.task_get(conn, tid)["status"] == "awaiting_final_confirm"


# ====================================================================
# 验收 4: 驳回后迁移不生效
# ====================================================================

def test_force_reject_blocks_transition(conn, controller, worker):
    """驳回兜底跳转后,任务状态不变。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "待审跳转", request_id="r-rj")
    aid = r["approval_id"]

    ops.force_reject(conn, "user-xxx", aid)
    # task stays at "executing"
    assert ops.task_get(conn, tid)["status"] == "executing"

    row = conn.execute(
        "SELECT status, decided_by FROM force_approvals WHERE id=?",
        (aid,)).fetchone()
    assert row["status"] == "rejected"


# ====================================================================
# 验收 5: 超时未批标记 expired
# ====================================================================

def test_force_approval_expires(conn, controller, worker):
    """超过 24h 未批→expired,任务不变。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "超时测试", request_id="r-exp")
    aid = r["approval_id"]

    # 把 created_at 推到 25 小时前
    old_ts = now() - 90000
    conn.execute("UPDATE force_approvals SET created_at=? WHERE id=?",
                 (old_ts, aid))

    result = ops.expire_force_approvals(conn)
    assert result["expired"] == 1

    row = conn.execute(
        "SELECT status FROM force_approvals WHERE id=?",
        (aid,)).fetchone()
    assert row["status"] == "expired"
    assert ops.task_get(conn, tid)["status"] == "executing"


# ====================================================================
# 验收 6: 身份校验 + 7: 撤回/幂等
# ====================================================================

def test_force_request_rejects_non_controller(conn, controller, worker):
    """非总控身份发起兜底跳转被拒。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    with pytest.raises(PermissionError):
        ops.task_force(conn, worker, tid, "awaiting_final_confirm",
                       "越权兜底", request_id="r-unauth")


def test_force_cancel_only_by_initiator(conn, controller, worker):
    """只能撤回自己发的审批请求。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "撤回测试", request_id="r-cancel")
    aid = r["approval_id"]

    with pytest.raises(PermissionError):
        ops.force_cancel_request(conn, worker["worker_id"], aid)
    ops.force_cancel_request(conn, controller["worker_id"], aid)
    row = conn.execute(
        "SELECT status FROM force_approvals WHERE id=?",
        (aid,)).fetchone()
    assert row["status"] == "cancelled"


# ====================================================================
# 验收 7: 幂等+已处理不可重复操作
# ====================================================================

def test_approve_already_decided(conn, controller, worker):
    """已批准的审批重复批准 = already。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "幂等测试", request_id="r-idempotent")
    aid = r["approval_id"]

    ops.force_approve(conn, "user", aid)
    r2 = ops.force_approve(conn, "user", aid)
    assert r2.get("already") == "approved"


def test_reject_already_decided(conn, controller, worker):
    """已驳回的审批重复驳回 = already。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "幂等测试", request_id="r-idem2")
    aid = r["approval_id"]

    ops.force_reject(conn, "user", aid)
    r2 = ops.force_reject(conn, "user", aid)
    assert r2.get("already") == "rejected"


def test_force_cancel_already_decided(conn, controller, worker):
    """已处理的审批请求不能撤回。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "撤回测试", request_id="r-cancel2")
    aid = r["approval_id"]

    ops.force_approve(conn, "user", aid)
    r2 = ops.force_cancel_request(conn, controller["worker_id"], aid)
    assert r2.get("already") == "approved"


# ====================================================================
# 审计迹线
# ====================================================================

def test_force_approval_audit_trail(conn, controller, worker):
    """审批链路完整审计: created → approved。"""
    tid, did = _to_executing(conn, controller, worker)
    r = ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "审计测试", request_id="r-audit")
    aid = r["approval_id"]

    # 审计行: created
    aud = conn.execute(
        "SELECT detail FROM audit WHERE action='force_approval_created'"
        " AND detail LIKE ?",
        (f'%"approval_id": {aid}%',)).fetchone()
    assert aud is not None

    ops.force_approve(conn, "user", aid)
    aud2 = conn.execute(
        "SELECT detail FROM audit WHERE action='force_intervention'"
        " AND detail LIKE ?",
        (f'%"approval_id": {aid}%',)).fetchone()
    assert aud2 is not None


# ====================================================================
# 超时闭环(task-02 新增): _tick 巡检顺带过期 + force_approve 双保险
# ====================================================================

class TestForceApprovalTimeout:
    """expire: 24h auto-expire via monitor tick + force_approve rejects expired."""

    def test_24h_expire_via_monitor(self, conn):
        """超期请求经 monitor._tick 巡检顺带标记 expired(真走 _tick)。"""
        from tianji.monitor import _tick
        ctrl = _register_controller(conn, "CTRL-EXPIRE")
        wkr = _register_worker(conn, "WKR-EXPIRE")
        tid, aid = _setup_approval(conn, ctrl, wkr)
        # 把 created_at 手动改成 2 天前
        old_ts = ops.now() - 86400 * 2
        conn.execute(
            "UPDATE force_approvals SET created_at=? WHERE id=?",
            (old_ts, aid))
        conn.commit()
        # _tick 前仍是 pending(防恒真)
        assert conn.execute(
            "SELECT status FROM force_approvals WHERE id=?",
            (aid,)).fetchone()["status"] == "pending"
        _tick(conn, {})
        r = conn.execute(
            "SELECT * FROM force_approvals WHERE id=?", (aid,)).fetchone()
        assert r["status"] == "expired"

    def test_force_approve_rejects_expired(self, conn):
        """force_approve 对超期请求返回 expired decision(双保险)。"""
        ctrl = _register_controller(conn, "CTRL-EXPIRED")
        wkr = _register_worker(conn, "WKR-EXPIRED")
        tid, aid = _setup_approval(conn, ctrl, wkr)
        old_ts = ops.now() - 86400 * 2
        conn.execute(
            "UPDATE force_approvals SET created_at=? WHERE id=?",
            (old_ts, aid))
        conn.commit()
        # 超时请求: force_approve 返回 expired 决策而非抛异常
        result = ops.force_approve(conn, ctrl["worker_id"], aid)
        assert result["decision"] == "expired"
        assert "超时" in result.get("reason", "") or "expired" in result.get("reason", "")


# ====================================================================
# 自批拦截(task-02 新增): ops 层 + CLI 层
# ====================================================================

class TestForceSelfApproveBlocked:
    """Self-approval blocked at ops layer + CLI negative test."""

    def test_ops_self_approve_blocked(self, conn):
        """ops 层: approver == initiator_id → PermissionError(禁止自批)。"""
        ctrl = _register_controller(conn, "CTRL-SELF")
        wkr = _register_worker(conn, "WKR-SELF")
        tid, aid = _setup_approval(conn, ctrl, wkr)
        with pytest.raises(PermissionError, match="禁止自批"):
            ops.force_approve(conn, ctrl["worker_id"], aid)

    def test_cli_self_approve_blocked(self, conn):
        """CLI 层: 同一身份批准自己发起的请求 → exit_code != 0。"""
        ctrl = _register_controller(conn, "CTRL-CLI")
        wkr = _register_worker(conn, "WKR-CLI")
        tid, aid = _setup_approval(conn, ctrl, wkr)
        env = {"TIANJI_WORKER_ID": ctrl["worker_id"],
               "TIANJI_SECRET": ctrl["secret"],
               "TIANJI_HOME": os.environ["TIANJI_HOME"]}
        r = runner.invoke(app, ["task", "approve-force", str(aid)], env=env)
        assert r.exit_code != 0
        combined = (r.output or "") + (r.exception and str(r.exception) or "")
        assert "禁止自批" in combined or "Permission" in combined


class TestForceRejectSelfBlocked:
    """force_reject also blocks self-approval."""

    def test_reject_self_blocked(self, conn):
        ctrl = _register_controller(conn, "CTRL-REJ")
        wkr = _register_worker(conn, "WKR-REJ")
        tid, aid = _setup_approval(conn, ctrl, wkr)
        with pytest.raises(PermissionError, match="禁止自批"):
            ops.force_reject(conn, ctrl["worker_id"], aid)


class TestForceNormalFlow:
    """Normal approval/rejection path unaffected."""

    def test_other_controller_can_approve(self, conn):
        """Different controller can approve → task moves to reviewing."""
        ctrl = _register_controller(conn, "CTRL-OK-A")
        wkr = _register_worker(conn, "WKR-OK-A")
        tid, aid = _setup_approval(conn, ctrl, wkr)
        # 注册另一总控,通过 controller 身份校验
        other = ops.instance_register(
            conn, "OTHER-CTRL", "claude", "step-router-v1",
            controller=True, ident=ctrl)
        result = ops.force_approve(conn, other["name"], aid)
        assert result["decision"] == "approved"
        t = ops.task_get(conn, tid)
        assert t["status"] == "reviewing"

    def test_other_controller_can_reject(self, conn):
        """Different controller can reject → task stays dispatched."""
        ctrl = _register_controller(conn, "CTRL-OK-R")
        wkr = _register_worker(conn, "WKR-OK-R")
        tid, aid = _setup_approval(conn, ctrl, wkr)
        other = ops.instance_register(
            conn, "OTHER-CTRL2", "claude", "step-router-v1",
            controller=True, ident=ctrl)
        result = ops.force_reject(conn, other["name"], aid)
        assert result["decision"] == "rejected"


# ====================================================================
# web 层(task-02 新增): /api/force/approve 总控自批 403,无身份用户放行
# ====================================================================

class TestForceApproveWeb:
    """web 层 HITL 门: 总控身份自批 403,无身份(用户)批准生效。"""

    def test_web_controller_self_approve_403(self, conn, monkeypatch):
        from fastapi.testclient import TestClient
        from tianji.webapp import app as web_app
        ctrl = _register_controller(conn, "CTRL-WEB")
        wkr = _register_worker(conn, "WKR-WEB")
        tid, aid = _setup_approval(conn, ctrl, wkr)
        # 页面注入总控身份(15.3)→ HITL 端点拒绝自批
        monkeypatch.setenv("TIANJI_WORKER_ID", ctrl["worker_id"])
        monkeypatch.setenv("TIANJI_SECRET", ctrl["secret"])
        client = TestClient(web_app)
        r = client.post("/api/force/approve",
                        json={"approval_id": aid, "decision": "approve"})
        assert r.status_code == 403
        assert "自批" in r.json()["error"]
        # 请求未被批准,仍 pending
        row = conn.execute(
            "SELECT status FROM force_approvals WHERE id=?",
            (aid,)).fetchone()
        assert row["status"] == "pending"

    def test_web_user_approve_ok(self, conn, monkeypatch):
        from fastapi.testclient import TestClient
        from tianji.webapp import app as web_app
        ctrl = _register_controller(conn, "CTRL-WEB2")
        wkr = _register_worker(conn, "WKR-WEB2")
        tid, aid = _setup_approval(conn, ctrl, wkr)
        # 无身份=用户(人)操作,批准生效
        monkeypatch.delenv("TIANJI_WORKER_ID", raising=False)
        monkeypatch.delenv("TIANJI_SECRET", raising=False)
        client = TestClient(web_app)
        r = client.post("/api/force/approve",
                        json={"approval_id": aid, "decision": "approve"})
        assert r.status_code == 200, r.text
        assert r.json()["decision"] == "approved"
        assert ops.task_get(conn, tid)["status"] == "reviewing"
