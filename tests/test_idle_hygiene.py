"""空闲发现与上下文卫生(票 12 验收 1-7)。"""

import json

import pytest

from tianji import hygiene, ops
from tianji.db import now, task_dir
from tianji.render import spawn
import os
from pathlib import Path


def _settle_task(conn, controller, worker, title, seq):
    """真实链路结算一个任务,返回 task_id。"""
    tid = ops.task_new(conn, controller, title, request_id=f"rh-new-{seq}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"rh-{s}-{seq}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id=f"rh-issue-{seq}")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    env = {**os.environ, "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    return tid, did


def test_idle_bonus_in_soft_sort(conn, controller):
    """验收 1: 空闲超阈值在软排序中加分(同分下闲者胜出)。"""
    ops.instance_register(conn, "闲工", "codex", "step-router-v1",
                          context_window=100000)
    ops.instance_register(conn, "忙惯工", "codex", "step-router-v1",
                          context_window=100000)
    # 同表现分(60 默认);闲工上次派单 2 小时前,忙惯工 5 分钟前
    old = now() - 7200
    recent = now() - 300
    tid0 = ops.task_new(conn, controller, "旧活", request_id="rh-old")["task_id"]
    for name, ts in (("闲工", old), ("忙惯工", recent)):
        conn.execute(
            "INSERT INTO dispatches (task_id, worker_id, worker_role, status,"
            " dcap_hash, expect_min, task_dir, payload, created_at, updated_at)"
            " VALUES (?,?,'worker','done','',30,'','{}',?,?)",
            (tid0, name, ts, ts))
    tid = ops.task_new(conn, controller, "新活", request_id="rh-new0")["task_id"]
    assert ops.allocator_pick(conn, tid) == "闲工"


def test_hygiene_cleanup_before_unrelated_dispatch(conn, controller, worker):
    """验收 2/4: 换不相关新活前,上次任务未归档→先打扫(摘要落账+登记行标已打扫)→才派活。"""
    tid_a, did_a = _settle_task(conn, controller, worker, "任务A", "a")
    # 任务 A 停在 reviewing(未归档)
    assert ops.task_get(conn, tid_a)["status"] == "reviewing"
    # 派不相关新活 B 给同一工人
    tid_b = ops.task_new(conn, controller, "任务B", request_id="rh-b-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid_b, s, request_id=f"rh-b-{s}")
    ops.dispatch_issue(conn, controller, tid_b, worker["worker_id"],
                       request_id="rh-b-issue")
    # 打扫发生: 摘要落账+登记行标已打扫
    a = conn.execute("SELECT detail FROM audit WHERE action='hygiene_clean'"
                     ).fetchone()
    assert a is not None
    detail = json.loads(a["detail"])
    assert detail["summary"]["last_task_id"] == tid_a
    reg = conn.execute(
        "SELECT cleaned_at FROM instance_registrations WHERE dispatch_id=?",
        (did_a,)).fetchone()
    assert reg["cleaned_at"] is not None


def test_same_task_no_cleanup(conn, controller, worker):
    """验收 3: "不相关"机械判定——同任务续接(重派)不触发打扫。"""
    tid_a, did_a = _settle_task(conn, controller, worker, "任务A", "c")
    # 同任务再派(驳回重派场景,任务 reviewing 态可直接补派工人单)
    ops.dispatch_issue(conn, controller, tid_a, worker["worker_id"],
                       request_id="rh-c-issue2")
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='hygiene_clean'").fetchone() is None


def test_resume_uses_same_mechanism(conn, controller, worker):
    """验收 4 续: 续接滚动与打扫同套机制(摘要+落盘),触发场景不同。"""
    tid_a, did_a = _settle_task(conn, controller, worker, "任务A", "r")
    # 同任务不触发换活打扫;续接快满滚动=显式同套调用
    r = hygiene.cleanup(conn, worker["worker_id"], 999, reason="续接滚动")
    # 同任务(999 不存在≠同任务,上次未归档)→按同套机制产出摘要
    assert r["cleaned"] is True
    a = conn.execute("SELECT detail FROM audit WHERE action='hygiene_clean'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    assert "续接滚动" in a["detail"]


def test_retire_keeps_resources(conn, controller):
    """验收 6: 回收(unbind)需总控动作;回收后画像/表现分保留,可复活重启。"""
    ops.instance_register(conn, "老工", "codex", "step-router-v1",
                          context_window=100000)
    conn.execute("UPDATE ability_profiles SET score=77 WHERE instance_name='老工'")
    ops.instance_unbind(conn, "老工", request_id="rh-unbind")
    p = conn.execute("SELECT score FROM ability_profiles"
                     " WHERE instance_name='老工'").fetchone()
    assert p["score"] == 77  # 回收≠删实例: 表现分保留
    # 复活重启(换绑通道)
    ops.instance_register(conn, "老工", "codex", "step-router-v1")
    p2 = conn.execute("SELECT score FROM ability_profiles"
                      " WHERE instance_name='老工'").fetchone()
    assert p2["score"] == 77
    assert conn.execute("SELECT is_active FROM instances WHERE name='老工'"
                        ).fetchone()["is_active"] == 1


def test_monitor_never_acts_directly():
    """验收 7: 监控器只产状态+建议——源码无删实例/强制干预类调用。"""
    src = Path(__file__).parent.parent / "tianji" / "monitor.py"
    text = src.read_text(encoding="utf-8")
    for forbidden in ("instance_unbind", "instance_delete", "task_force",
                      "dispatch_cancel"):
        assert forbidden not in text, forbidden
