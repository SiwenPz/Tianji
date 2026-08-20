"""双轴审核与架构师裁判(票 04) 验收测试。

覆盖六条验收标准 + _reschedule 修复回归 + 负例。
"""

import json
import os
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import connect, task_dir
from tianji.messages import send
from tianji.render import spawn
from tianji.events import ingest_event


# ====================================================================
# 夹具辅助
# ====================================================================

def _to_reviewing(conn, controller, worker_name="实施者铁蛋"):
    """真实链路快速走到 reviewing(实施者结算完毕)。"""
    tid = ops.task_new(conn, controller, "双轴测试任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    # 确保实施者实例存在
    existing = conn.execute(
        "SELECT name FROM instances WHERE name=?", (worker_name,)).fetchone()
    if not existing:
        ops.instance_register(conn, worker_name, "codex", "step-router-v1",
                              launch_cmd="python mock_worker.py")
    did = ops.dispatch_issue(conn, controller, tid, worker_name,
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker_name, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "s1", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "s1", "event_type": "pre_tool_use"})
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("实施者报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    return tid


def _issue_reviewer(conn, controller, task_id, worker_id, axis, request_id):
    """派一条指定 axis 的审核派单,并 spawn 生成 secret(使 settle 可验证身份)。"""
    dr = ops.dispatch_issue(conn, controller, task_id, worker_id,
                            role="reviewer", request_id=request_id,
                            axis=axis)
    s = spawn(conn, worker_id, dr["dispatch_id"])
    dr["secret"] = s["env"]["TIANJI_SECRET"]
    return dr


def _report(conn, did):
    p = Path(task_dir(did)) / "report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("审核报告", encoding="utf-8")
    return str(p)


def _register_reviewer(conn, name, shell, model):
    r = ops.instance_register(conn, name, shell, model,
                              launch_cmd="python mock_worker.py")
    return {"worker_id": name, "secret": r["secret"]}


# ====================================================================
# 标准 1: 质量轴 6 维清单在 configs 中
# ====================================================================

def test_quality_axis_checklist_default_exists(conn):
    cfg = ops.config_get(conn, "quality_axis_checklist")
    assert cfg is not None
    checklist = json.loads(cfg["value"])
    assert len(checklist) == 6
    dims = [item["dimension"] for item in checklist]
    expected = ["测试真伪", "改动最小性", "边界与错误处理",
                "死代码与重复", "机密与安全", "可维护性"]
    for e in expected:
        assert e in dims


# ====================================================================
# 标准 2: 双轴派单(不同实例不同模型) + reportPath 验证
# ====================================================================

def test_dual_axis_dispatch_different_instances(conn, controller):
    """两轴=2 个不同实例各自出 verdict。"""
    tid = ops.task_new(conn, controller, "双轴实例", request_id="r1")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    # 注册两个不同审核实例
    r1 = ops.instance_register(conn, "审核甲", "claude", "deepseek-v4-flash",
                               launch_cmd="python mock_worker.py")
    r2 = ops.instance_register(conn, "审核乙", "codex", "step-router-v1",
                               launch_cmd="python mock_worker.py")
    d1 = _issue_reviewer(conn, controller, tid, "审核甲", "spec", "r-spec")
    d2 = _issue_reviewer(conn, controller, tid, "审核乙", "quality", "r-quality")
    assert d1["dispatch_id"] != d2["dispatch_id"]
    d1_row = ops.dispatch_get(conn, d1["dispatch_id"])
    d2_row = ops.dispatch_get(conn, d2["dispatch_id"])
    assert d1_row["axis"] == "spec"
    assert d2_row["axis"] == "quality"
    assert d1_row["worker_id"] != d2_row["worker_id"]


def test_same_instance_two_axes_rejected(conn, controller):
    """两轴同实例被拒(硬约束 1.2)。"""
    tid = ops.task_new(conn, controller, "同实例被拒", request_id="r2")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    r1 = ops.instance_register(conn, "审核单", "claude", "deepseek-v4-flash",
                               launch_cmd="python mock_worker.py")
    _issue_reviewer(conn, controller, tid, "审核单", "spec", "r-spec1")
    with pytest.raises(ValueError, match="已有活跃审核派单|同一实例不可兼两轴"):
        _issue_reviewer(conn, controller, tid, "审核单", "quality", "r-quality1")


def test_same_model_two_axes_rejected(conn, controller):
    """两轴同模型被拒(硬约束 1.2)。"""
    tid = ops.task_new(conn, controller, "同模型被拒", request_id="r3")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    r1 = ops.instance_register(conn, "审核M1", "claude", "model-X",
                               launch_cmd="python mock_worker.py")
    r2 = ops.instance_register(conn, "审核M2", "codex", "model-X",
                               launch_cmd="python mock_worker.py")
    _issue_reviewer(conn, controller, tid, "审核M1", "spec", "r-spec2")
    with pytest.raises(ValueError, match="模型相同|不同模型"):
        _issue_reviewer(conn, controller, tid, "审核M2", "quality", "r-quality2")


# ====================================================================
# 标准 3/4: 双轴汇聚 + 架构师裁判(一致通过/分歧/缺确认机械拒绝)
# ====================================================================

def test_both_pass_architect_confirm_goes_to_awaiting(conn, controller):
    """两轴一致 pass + 架构师确认 → awaiting_final_confirm。"""
    tid = _to_reviewing(conn, controller)
    ops.instance_register(conn, "审核spec", "claude", "deepseek-v4-flash",
                          launch_cmd="python mock_worker.py")
    ops.instance_register(conn, "审核quality", "codex", "step-router-v1",
                          launch_cmd="python mock_worker.py")
    d_spec = _issue_reviewer(conn, controller, tid, "审核spec", "spec", "r-s")
    d_quality = _issue_reviewer(conn, controller, tid, "审核quality", "quality", "r-q")
    # 两轴都 pass(使用 spawn 生成的 secret)
    env_spec = {**os.environ, "TIANJI_WORKER_ID": "审核spec",
                "TIANJI_SECRET": d_spec["secret"], "TIANJI_DISPATCH_ID": str(d_spec["dispatch_id"])}
    env_quality = {**os.environ, "TIANJI_WORKER_ID": "审核quality",
                   "TIANJI_SECRET": d_quality["secret"], "TIANJI_DISPATCH_ID": str(d_quality["dispatch_id"])}
    rp_s = _report(conn, d_spec["dispatch_id"])
    rp_q = _report(conn, d_quality["dispatch_id"])
    ops.dispatch_settle(conn, env_spec, d_spec["dispatch_id"], rp_s, "pass")
    ops.dispatch_settle(conn, env_quality, d_quality["dispatch_id"], rp_q, "pass")
    # 架构师确认
    ops.architect_confirm(conn, controller, tid, "一致通过", request_id="r-ac")
    # 推进
    r = ops.task_transition(conn, controller, tid, "awaiting_final_confirm",
                            request_id="r-trans")
    assert ops.task_get(conn, tid)["status"] == "awaiting_final_confirm"


def test_both_pass_missing_architect_mechanically_rejected(conn, controller):
    """缺架构师确认时,机械拒绝进入 awaiting_final_confirm。"""
    tid = _to_reviewing(conn, controller)
    ops.instance_register(conn, "审核spec", "claude", "deepseek-v4-flash",
                          launch_cmd="python mock_worker.py")
    ops.instance_register(conn, "审核quality", "codex", "step-router-v1",
                          launch_cmd="python mock_worker.py")
    d_spec = _issue_reviewer(conn, controller, tid, "审核spec", "spec", "r-s2")
    d_quality = _issue_reviewer(conn, controller, tid, "审核quality", "quality", "r-q2")
    env_spec = {**os.environ, "TIANJI_WORKER_ID": "审核spec",
                "TIANJI_SECRET": d_spec["secret"], "TIANJI_DISPATCH_ID": str(d_spec["dispatch_id"])}
    env_quality = {**os.environ, "TIANJI_WORKER_ID": "审核quality",
                   "TIANJI_SECRET": d_quality["secret"], "TIANJI_DISPATCH_ID": str(d_quality["dispatch_id"])}
    rp_s = _report(conn, d_spec["dispatch_id"])
    rp_q = _report(conn, d_quality["dispatch_id"])
    ops.dispatch_settle(conn, env_spec, d_spec["dispatch_id"], rp_s, "pass")
    ops.dispatch_settle(conn, env_quality, d_quality["dispatch_id"], rp_q, "pass")
    # 不调用 architect_confirm,直接推进应被拒
    with pytest.raises(ValueError, match="架构师未确认|机械拒绝"):
        ops.task_transition(conn, controller, tid, "awaiting_final_confirm",
                            request_id="r-no-ac")


def test_disagree_goes_to_architect_deep_review(conn, controller):
    """两轴分歧→架构师深审裁决消息进账本。"""
    tid = _to_reviewing(conn, controller)
    ops.instance_register(conn, "审核spec", "claude", "deepseek-v4-flash",
                          launch_cmd="python mock_worker.py")
    ops.instance_register(conn, "审核quality", "codex", "step-router-v1",
                          launch_cmd="python mock_worker.py")
    d_spec = _issue_reviewer(conn, controller, tid, "审核spec", "spec", "r-s3")
    d_quality = _issue_reviewer(conn, controller, tid, "审核quality", "quality", "r-q3")
    env_spec = {**os.environ, "TIANJI_WORKER_ID": "审核spec",
                "TIANJI_SECRET": d_spec["secret"], "TIANJI_DISPATCH_ID": str(d_spec["dispatch_id"])}
    env_quality = {**os.environ, "TIANJI_WORKER_ID": "审核quality",
                   "TIANJI_SECRET": d_quality["secret"], "TIANJI_DISPATCH_ID": str(d_quality["dispatch_id"])}
    rp_s = _report(conn, d_spec["dispatch_id"])
    rp_q = _report(conn, d_quality["dispatch_id"])
    ops.dispatch_settle(conn, env_spec, d_spec["dispatch_id"], rp_s, "pass")
    ops.dispatch_settle(conn, env_quality, d_quality["dispatch_id"], rp_q, "reject",
                        reason="质量轴驳回")
    # 架构师裁决(此时应允许裁决,即便双轴不一致)
    r = ops.architect_review(conn, controller, tid, "架构师深审裁决",
                             request_id="r-ar")
    assert r["task_id"] == tid
    # 裁决消息进账本
    msg = conn.execute(
        "SELECT * FROM messages WHERE type='architect_verdict' AND payload LIKE ?",
        (f'%"task_id": {tid}%',)).fetchone()
    assert msg is not None


# ====================================================================
# 标准 5: 单实例串行降级
# ====================================================================

def test_single_instance_serial_degradation(conn, controller):
    """单实例串行跑两轴(质量降级)。"""
    tid = _to_reviewing(conn, controller)
    ops.instance_register(conn, "审核单例", "claude", "deepseek-v4-flash",
                          launch_cmd="python mock_worker.py")
    d_spec = _issue_reviewer(conn, controller, tid, "审核单例", "spec", "r-ss1")
    # 第一轴 settle done 后再派第二轴(串行)
    env = {**os.environ, "TIANJI_WORKER_ID": "审核单例",
           "TIANJI_SECRET": d_spec["secret"], "TIANJI_DISPATCH_ID": str(d_spec["dispatch_id"])}
    rp = _report(conn, d_spec["dispatch_id"])
    ops.dispatch_settle(conn, env, d_spec["dispatch_id"], rp, "pass")
    d_quality = _issue_reviewer(conn, controller, tid, "审核单例", "quality", "r-ss2")
    env2 = {**os.environ, "TIANJI_WORKER_ID": "审核单例",
            "TIANJI_SECRET": d_quality["secret"], "TIANJI_DISPATCH_ID": str(d_quality["dispatch_id"])}
    rp2 = _report(conn, d_quality["dispatch_id"])
    ops.dispatch_settle(conn, env2, d_quality["dispatch_id"], rp2, "pass")
    # 两轴都 done,状态仍在 reviewing(等架构师确认)
    assert ops.task_get(conn, tid)["status"] == "reviewing"


# ====================================================================
# 标准 6: 幂等重放
# ====================================================================

def test_review_verdict_replay_returns_original(conn, controller):
    """review_verdict 重放返回原回执。"""
    tid = _to_reviewing(conn, controller)
    ops.instance_register(conn, "审核幂等", "claude", "deepseek-v4-flash",
                          launch_cmd="python mock_worker.py")
    d = _issue_reviewer(conn, controller, tid, "审核幂等", "spec", "r-idempotent")
    env = {**os.environ, "TIANJI_WORKER_ID": "审核幂等",
           "TIANJI_SECRET": d["secret"], "TIANJI_DISPATCH_ID": str(d["dispatch_id"])}
    rp = _report(conn, d["dispatch_id"])
    first = ops.dispatch_settle(conn, env, d["dispatch_id"], rp, "pass")
    second = ops.dispatch_settle(conn, env, d["dispatch_id"], rp, "pass")
    assert second.get("replay") is True
    assert second["dispatch_id"] == first["dispatch_id"]


# ====================================================================
# _reschedule bug 修复: 驳回重派对象=最新 worker_role='worker' 的工人
# ====================================================================

def test_reschedule_after_review_reject_goes_to_worker(conn, controller):
    """审核驳回后重派给实施者,不派给审核者。"""
    # 构造: 任务 executing + 实施者派单 active + 审核者派单 issued
    tid = ops.task_new(conn, controller, "重派测试", request_id="r-resched")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    # 注册实施者
    ops.instance_register(conn, "实施者铁蛋", "codex", "step-router-v1",
                          launch_cmd="python mock_worker.py")
    did_worker = ops.dispatch_issue(conn, controller, tid, "实施者铁蛋",
                                    request_id="r-w")["dispatch_id"]
    # 开工证据
    s = spawn(conn, "实施者铁蛋", did_worker)
    env_w = {**os.environ, "TIANJI_WORKER_ID": "实施者铁蛋",
             "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
             "TIANJI_DISPATCH_ID": str(did_worker)}
    ingest_event(conn, env_w, {"session_id": "sw", "event_type": "session_start"})
    ingest_event(conn, env_w, {"session_id": "sw", "event_type": "pre_tool_use"})
    # 实施者 settle
    rp = Path(task_dir(did_worker)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("实施者报告", encoding="utf-8")
    ops.dispatch_settle(conn, env_w, did_worker, str(rp), "ok")
    # 现在任务 reviewing,派一条审核派单
    ops.instance_register(conn, "审核者庞统", "claude", "deepseek-v4-flash",
                          launch_cmd="python mock_worker.py")
    did_reviewer = ops.dispatch_issue(conn, controller, tid, "审核者庞统",
                                      role="reviewer", request_id="r-rev",
                                      axis="spec")["dispatch_id"]
    s_rev = spawn(conn, "审核者庞统", did_reviewer)
    # 审核者 settle reject
    rp_rev = Path(task_dir(did_reviewer)) / "report.md"
    rp_rev.parent.mkdir(parents=True, exist_ok=True)
    rp_rev.write_text("审核报告", encoding="utf-8")
    env_rev = {**os.environ, "TIANJI_WORKER_ID": "审核者庞统",
               "TIANJI_SECRET": s_rev["env"]["TIANJI_SECRET"],
               "TIANJI_DISPATCH_ID": str(did_reviewer)}
    ops.dispatch_settle(conn, env_rev, did_reviewer, str(rp_rev), "reject",
                        reason="驳回")
    # 新派单应发给实施者铁蛋,不是审核者庞统
    new_dispatch = conn.execute(
        "SELECT worker_id FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    assert new_dispatch is not None
    assert new_dispatch["worker_id"] == "实施者铁蛋"


# ====================================================================
# 额外: config_set JSON 合法性预校验 + old_value 审计
# ====================================================================

def test_config_set_quality_axis_requires_valid_json(conn, controller):
    """quality_axis_checklist 写入非 JSON 应被拒绝(fail-loud)。"""
    with pytest.raises(ValueError, match="JSON"):
        ops.config_set(conn, controller, "quality_axis_checklist",
                       "这不是合法的JSON", request_id="r-bad-json")


def test_config_set_audit_contains_old_value(conn, controller):
    """config_set 审计行应包含 old_value(预审 P2 #11)。"""
    ops.config_set(conn, controller, "expect_min_default", "60",
                   request_id="r-audit1")
    ops.config_set(conn, controller, "expect_min_default", "90",
                   request_id="r-audit2")
    rows = conn.execute(
        "SELECT detail FROM audit WHERE action='config_set' ORDER BY ts"
    ).fetchall()
    assert len(rows) >= 2
    detail = json.loads(rows[-1]["detail"])
    assert detail.get("old_value") == "60"


# ====================================================================
# 额外: config_set JSON 合法性预校验 + old_value 审计
# ====================================================================

def test_config_set_quality_axis_requires_valid_json(conn, controller):
    """quality_axis_checklist 写入非 JSON 应被拒绝(fail-loud)。"""
    with pytest.raises(ValueError, match="JSON"):
        ops.config_set(conn, controller, "quality_axis_checklist",
                       "这不是合法的JSON", request_id="r-bad-json")


def test_config_set_audit_contains_old_value(conn, controller):
    """config_set 审计行应包含 old_value(预审 P2 #11)。"""
    ops.config_set(conn, controller, "expect_min_default", "60",
                   request_id="r-audit1")
    ops.config_set(conn, controller, "expect_min_default", "90",
                   request_id="r-audit2")
    rows = conn.execute(
        "SELECT detail FROM audit WHERE action='config_set' ORDER BY ts"
    ).fetchall()
    assert len(rows) >= 2
    detail = json.loads(rows[-1]["detail"])
    assert detail.get("old_value") == "60"



def test_reject_cancels_inflight_other_axis(conn, controller):
    """驳回重派先作废在途另一轴审核派单(2026-08-18 司马懿实锤:
    质量轴 reject 时 spec 轴仍 issued,唯一活跃派单门卡死结算)。"""
    tid = _to_reviewing(conn, controller)
    _register_reviewer(conn, "审核甲", "claude", "deepseek-v4-pro")
    _register_reviewer(conn, "审核乙", "claude", "kimi-k2.7-code")
    d_spec = _issue_reviewer(conn, controller, tid, "审核甲", "spec", "r-spec")
    d_q = _issue_reviewer(conn, controller, tid, "审核乙", "quality", "r-quality")
    # 质量轴先出结论 reject,spec 轴仍在途
    env_q = {**os.environ, "TIANJI_WORKER_ID": "审核乙",
             "TIANJI_SECRET": d_q["secret"],
             "TIANJI_DISPATCH_ID": str(d_q["dispatch_id"])}
    r = ops.dispatch_settle(conn, env_q, d_q["dispatch_id"],
                            _report(conn, d_q["dispatch_id"]),
                            "reject", reason="缺口")
    assert r.get("verdict") == "reject"
    # 在途 spec 轴派单作废
    assert ops.dispatch_get(conn, d_spec["dispatch_id"])["status"] == "cancelled"
    # 任务回 dispatched,新实施派单已发出
    t = ops.task_get(conn, tid)
    assert t["status"] == "dispatched"
    d2 = conn.execute(
        "SELECT id, worker_role FROM dispatches WHERE task_id=?"
        " ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    assert d2["worker_role"] == "worker"
    assert ops.dispatch_get(conn, d2["id"])["status"] == "issued"
