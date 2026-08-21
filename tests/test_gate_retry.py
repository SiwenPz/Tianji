"""成果确认闸门(验收 7)+重派护栏(验收 6 超限终止)+机械验收门(验收 6)。"""

import os
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import task_dir
from tianji.render import spawn


def _to_reviewing(conn, controller, worker):
    """任务走到 reviewing(结算完成,验收前)。返回 (task_id, worker_env)。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    from tianji.events import ingest_event
    env = {**os.environ, "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "s", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "s", "event_type": "pre_tool_use"})
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    return tid, env


def _review_pass(conn, controller, worker, tid):
    """双轴审核 pass(架构师确认前): 同一实例串行跑 spec+quality 两轴。"""
    # 注册第二个审核实例(质量轴),保证不同模型
    ops.instance_register(conn, "审核质量", "claude", "deepseek-v4-flash-quality",
                          launch_cmd="python mock_worker.py")
    did_spec = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                                  role="reviewer", request_id="r-rev-spec",
                                  axis="spec")["dispatch_id"]
    did_quality = ops.dispatch_issue(conn, controller, tid, "审核质量",
                                     role="reviewer", request_id="r-rev-quality",
                                     axis="quality")["dispatch_id"]
    s_spec = spawn(conn, worker["worker_id"], did_spec)
    s_quality = spawn(conn, "审核质量", did_quality)
    from tianji.events import ingest_event
    env_spec = {**os.environ, "TIANJI_WORKER_ID": s_spec["env"]["TIANJI_WORKER_ID"],
                "TIANJI_SECRET": s_spec["env"]["TIANJI_SECRET"],
                "TIANJI_DISPATCH_ID": str(did_spec)}
    env_quality = {**os.environ, "TIANJI_WORKER_ID": s_quality["env"]["TIANJI_WORKER_ID"],
                   "TIANJI_SECRET": s_quality["env"]["TIANJI_SECRET"],
                   "TIANJI_DISPATCH_ID": str(did_quality)}
    ingest_event(conn, env_spec, {"session_id": "rev-spec", "event_type": "session_start"})
    ingest_event(conn, env_quality, {"session_id": "rev-quality", "event_type": "session_start"})
    rp_spec = Path(task_dir(did_spec)) / "review.md"
    rp_spec.parent.mkdir(parents=True, exist_ok=True)
    rp_spec.write_text("审核报告: 通过", encoding="utf-8")
    rp_quality = Path(task_dir(did_quality)) / "review.md"
    rp_quality.parent.mkdir(parents=True, exist_ok=True)
    rp_quality.write_text("审核报告: 通过", encoding="utf-8")
    ops.dispatch_settle(conn, env_spec, did_spec, str(rp_spec), "pass",
                        reason="Spec 轴通过")
    ops.dispatch_settle(conn, env_quality, did_quality, str(rp_quality), "pass",
                        reason="质量轴通过")
    return did_spec


def test_gate_on_blocks_direct_archive(conn, controller, worker):
    """闸门默认开: reviewing→archived 被 CLI 拒绝(20.4)。"""
    tid, env = _to_reviewing(conn, controller, worker)
    _review_pass(conn, controller, worker, tid)
    with pytest.raises(ValueError, match="成果确认闸门"):
        ops.task_transition(conn, controller, tid, "archived",
                            request_id="r-a")


def test_gate_on_full_chain_ok(conn, controller, worker):
    tid, env = _to_reviewing(conn, controller, worker)
    _review_pass(conn, controller, worker, tid)
    ops.architect_confirm(conn, controller, tid, "双轴通过", request_id="r-ac")
    ops.task_transition(conn, controller, tid, "awaiting_final_confirm",
                        request_id="r-afc")
    ops.task_transition(conn, controller, tid, "archived", request_id="r-arch")
    assert ops.task_get(conn, tid)["status"] == "archived"


def test_gate_off_allows_direct_archive(conn, controller, worker):
    """闸门关闭模式: 审核通过后 reviewing→archived 直通(4.2/20.4)。"""
    tid, env = _to_reviewing(conn, controller, worker)
    _review_pass(conn, controller, worker, tid)
    ops.config_set(conn, controller, "final_confirm_gate", "off",
                   request_id="r-cfg")
    ops.task_transition(conn, controller, tid, "archived", request_id="r-a")
    assert ops.task_get(conn, tid)["status"] == "archived"
    aud = conn.execute(
        "SELECT action FROM audit WHERE action='config_set'").fetchone()
    assert aud is not None  # 配置变更有审计


def test_user_reject_back_to_discussing(conn, controller, worker):
    """4.5 评审补: 用户最终驳回→discussing 重新对齐,不消耗重派计数、不触发终止。"""
    tid, env = _to_reviewing(conn, controller, worker)
    _review_pass(conn, controller, worker, tid)
    ops.architect_confirm(conn, controller, tid, "双轴通过", request_id="r-ac")
    ops.task_transition(conn, controller, tid, "awaiting_final_confirm",
                        request_id="r-afc")
    ops.task_transition(conn, controller, tid, "discussing",
                        request_id="r-urej", reason="方向变了,重对齐")
    t = ops.task_get(conn, tid)
    assert t["status"] == "discussing"
    assert t["retry_count"] == 0  # 用户驳回不计机械重派计数(12.1 口径)
    aud = conn.execute(
        "SELECT action FROM audit WHERE action='user_reject'").fetchone()
    assert aud is not None  # 单独审计留痕(4.5)
    # 旧派单未被占用,重新对齐后可再走计划链
    ops.task_transition(conn, controller, tid, "awaiting_plan_confirm",
                        request_id="r-p2")
    assert ops.task_get(conn, tid)["status"] == "awaiting_plan_confirm"


def test_awaiting_final_confirm_requires_pass_verdict(conn, controller, worker):
    """8.2: 无架构师确认(无双轴一致通过)到不了 awaiting_final_confirm。"""
    tid, env = _to_reviewing(conn, controller, worker)
    with pytest.raises(ValueError, match="双轴审核未一致通过|架构师未确认"):
        ops.task_transition(conn, controller, tid, "awaiting_final_confirm",
                            request_id="r-afc")


def test_mechanical_fail_reschedules(conn, controller, worker):
    """验收 6: 验收命令失败→mechanical_fail 驳回重派(计数+1,新派单)。"""
    tid, env = _to_reviewing(conn, controller, worker)
    # 验收命令=必然失败;验收命令须在计划确认前写入,此处已 reviewing,直接改库模拟架构师已写
    conn.execute("UPDATE tasks SET verify_cmd=? WHERE id=?",
                 ("python -c \"import sys; sys.exit(1)\"", tid))
    r = ops.mechanical_verify(conn, tid)
    assert r["ok"] is False and r["rescheduled"] is True
    t = ops.task_get(conn, tid)
    assert t["status"] == "dispatched"
    assert t["retry_count"] == 1
    # 新派单已自动发出
    d = conn.execute(
        "SELECT id FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    assert ops.dispatch_get(conn, d["id"])["status"] == "issued"


def test_rework_taskbook_carries_reject_reason(conn, controller, worker):
    """4.3: 重派任务书须带上一轮驳回原因(模板渲染,不靠总控手写补位)。"""
    tid, env = _to_reviewing(conn, controller, worker)
    conn.execute("UPDATE tasks SET verify_cmd=? WHERE id=?",
                 ("python -c \"import sys; sys.exit(1)\"", tid))
    ops.mechanical_verify(conn, tid)
    d = conn.execute(
        "SELECT id FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    s = spawn(conn, worker["worker_id"], d["id"])
    taskbook = Path(s["taskbook"]).read_text(encoding="utf-8")
    assert "上一轮驳回/重派原因" in taskbook
    assert "mechanical_fail" in taskbook
    # 首次派单(无驳回原因)不渲染该节: 同一任务的首张已结算派单任务书即反例
    d2 = conn.execute(
        "SELECT id FROM dispatches WHERE task_id=? AND status='done'",
        (tid,)).fetchone()
    first = Path(task_dir(d2["id"])) / "task.md"
    assert "上一轮驳回/重派原因" not in first.read_text(encoding="utf-8")


def test_verify_ok_does_not_reschedule(conn, controller, worker):
    tid, env = _to_reviewing(conn, controller, worker)
    conn.execute("UPDATE tasks SET verify_cmd=? WHERE id=?",
                 ("python -c \"print('ok')\"", tid))
    r = ops.mechanical_verify(conn, tid)
    assert r["ok"] is True
    assert ops.task_get(conn, tid)["status"] == "reviewing"
    # 重复调用返回 already(幂等)
    r2 = ops.mechanical_verify(conn, tid)
    assert r2["already"] is True


def test_retry_limit_terminates(conn, controller, worker):
    """验收 6: 重派超上限→escalation+归档+审计记终止(12.1/12.4)。"""
    tid, env = _to_reviewing(conn, controller, worker)
    for i in range(4):  # 上限 3,第 4 次触发终止(共 4 次尝试)
        ops.task_transition(conn, controller, tid, "dispatched",
                            request_id=f"r-r{i}", reason="驳回重派")
        if i < 3:
            # 模拟重派后再次结算(派单 done+任务 reviewing)回到可驳回态
            conn.execute("UPDATE dispatches SET status='done' WHERE task_id=?"
                         " AND status IN ('issued','active')", (tid,))
            conn.execute("UPDATE tasks SET status='reviewing' WHERE id=?", (tid,))
    t = ops.task_get(conn, tid)
    assert t["status"] == "archived"
    assert t["retry_count"] == 4
    aud = conn.execute(
        "SELECT action FROM audit WHERE action='terminate_max_retries'").fetchone()
    assert aud is not None
    esc = conn.execute(
        "SELECT type FROM messages WHERE type='escalation' ORDER BY seq DESC "
        "LIMIT 1").fetchone()
    assert esc is not None and "重做超限终止" in conn.execute(
        "SELECT payload FROM messages WHERE type='escalation' ORDER BY seq DESC "
        "LIMIT 1").fetchone()["payload"]


def test_reopen_resets_retry_count(conn, controller, worker):
    """10.6: reopened 重派计数清零,reopened→reviewing 唯一后继。"""
    tid, env = _to_reviewing(conn, controller, worker)
    conn.execute("UPDATE tasks SET status='archived', retry_count=3 WHERE id=?",
                 (tid,))
    ops.task_transition(conn, controller, tid, "reopened", request_id="r-ro")
    t = ops.task_get(conn, tid)
    assert t["status"] == "reopened" and t["retry_count"] == 0
    with pytest.raises(ValueError, match="非法转换"):
        ops.task_transition(conn, controller, tid, "dispatched",
                            request_id="r-bad")
    ops.task_transition(conn, controller, tid, "reviewing", request_id="r-rv")
    assert ops.task_get(conn, tid)["status"] == "reviewing"


def test_verify_utf8_output_no_gbk_crash(conn, controller, worker):
    """验收命令输出含 UTF-8 专属字符时不得炸 GBK 解码(2026-08-19 实证:
    subprocess text=True 默认 locale 编码,监控器被 readerthread 崩死)。"""
    tid, env = _to_reviewing(conn, controller, worker)
    conn.execute("UPDATE tasks SET verify_cmd=? WHERE id=?",
                 ("python -c \"print('中文§✓')\"", tid))
    r = ops.mechanical_verify(conn, tid)
    assert r["ok"] is True
