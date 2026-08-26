"""事件流(验收 4): 事件行+payload 原文保留+派生状态+登记行回填+乱序不覆盖+派单激活。"""

import os

import pytest

from tianji import ops
from tianji.events import EventError, ingest_event
from tianji.render import spawn


def _env(worker, secret, did):
    return {**os.environ, "TIANJI_WORKER_ID": worker["worker_id"],
            "TIANJI_SECRET": secret, "TIANJI_DISPATCH_ID": str(did)}


def _ctrl_env(controller):
    """ops 身份 dict → env 格式(ingest_event 读 env,11.4)。"""
    return {**os.environ, "TIANJI_WORKER_ID": controller["worker_id"],
            "TIANJI_SECRET": controller["secret"]}


def _issue_and_spawn(conn, controller, worker):
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    return tid, did, s["env"]["TIANJI_SECRET"]


def test_ingest_requires_identity(conn):
    with pytest.raises(PermissionError):
        ingest_event(conn, {}, {"session_id": "s",
                                "event_type": "session_start"})


def test_ingest_illegal_event_type(conn, controller):
    with pytest.raises(EventError, match="非法事件类型"):
        ingest_event(conn, _ctrl_env(controller),
                     {"session_id": "s", "event_type": "nonsense"})


def test_ingest_controller_identity_is_fail_closed(conn, controller):
    env = {**os.environ,
           "TIANJI_WORKER_ID": controller["worker_id"],
           "TIANJI_SECRET": "forged-secret"}
    with pytest.raises(EventError, match="身份校验失败"):
        ingest_event(conn, env,
                     {"session_id": "s", "event_type": "session_start"})


def test_ingest_accepts_dispatch_secret_for_controller_as_reviewer(
        conn, controller):
    """总控全局凭据不匹配时,其名下有效派单的派单凭据仍可上报事件。"""
    tid = ops.task_new(conn, controller, "总控审核事件",
                       request_id="r-event-ctrl")["task_id"]
    for state in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, state,
                            request_id=f"r-event-ctrl-{state}")
    dr = ops.dispatch_issue(conn, controller, tid,
                            controller["worker_id"], role="reviewer",
                            axis="spec", request_id="r-event-ctrl-review")
    spawned = spawn(conn, controller["worker_id"], dr["dispatch_id"])
    env = {**os.environ,
           "TIANJI_WORKER_ID": controller["worker_id"],
           "TIANJI_SECRET": spawned["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(dr["dispatch_id"])}
    result = ingest_event(env=env, conn=conn,
                          event={"session_id": "review-session",
                                 "event_type": "session_start"})
    assert result["event_type"] == "session_start"


def test_event_row_and_payload_kept(conn, controller):
    """事件行 payload 原文保留(6.3)+事件不寻址(recipient 为空)。"""
    r = ingest_event(conn, _ctrl_env(controller),
                     {"session_id": "s-1", "event_type": "user_prompt",
                      "payload": {"prompt": "你好"}})
    row = conn.execute(
        "SELECT * FROM messages WHERE seq=?", (r["seq"],)).fetchone()
    assert row["type"] == "event"
    assert row["recipient_role"] is None
    import json
    payload = json.loads(row["payload"])
    assert payload["event_type"] == "user_prompt"
    assert payload["payload"]["prompt"] == "你好"
    assert payload["worker_id"] == "总控"


def test_session_start_backfills_registration(conn, controller, worker):
    """11.1: session_start 事件按 worker 回填登记行 active=验证。"""
    tid, did, secret = _issue_and_spawn(conn, controller, worker)
    reg_before = conn.execute(
        "SELECT status FROM instance_registrations WHERE instance_name=? "
        "ORDER BY id DESC LIMIT 1", (worker["worker_id"],)).fetchone()
    assert reg_before["status"] == "spawned"
    ingest_event(conn, _env(worker, secret, did),
                 {"session_id": "sess-9", "event_type": "session_start"})
    reg = conn.execute(
        "SELECT status, session_id FROM instance_registrations WHERE"
        " instance_name=? ORDER BY id DESC LIMIT 1",
        (worker["worker_id"],)).fetchone()
    assert reg["status"] == "active"
    assert reg["session_id"] == "sess-9"


def test_session_end_closes_registration(conn, controller, worker):
    tid, did, secret = _issue_and_spawn(conn, controller, worker)
    env = _env(worker, secret, did)
    ingest_event(conn, env, {"session_id": "s1", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "s1", "event_type": "session_end"})
    reg = conn.execute(
        "SELECT status FROM instance_registrations WHERE instance_name=? "
        "ORDER BY id DESC LIMIT 1", (worker["worker_id"],)).fetchone()
    assert reg["status"] == "closed"


def test_derived_state_four_states(conn, controller):
    """6.3 派生四态: idle/working/waiting/done。"""
    st = lambda sid: conn.execute(
        "SELECT state FROM session_states WHERE session_id=?", (sid,)).fetchone()["state"]
    env = _ctrl_env(controller)
    ingest_event(conn, env, {"session_id": "a", "event_type": "session_start"})
    assert st("a") == "idle"
    ingest_event(conn, env, {"session_id": "a", "event_type": "pre_tool_use"})
    assert st("a") == "working"
    ingest_event(conn, env, {"session_id": "a", "event_type": "stop",
                             "is_interrupt": False})
    assert st("a") == "waiting"
    ingest_event(conn, env, {"session_id": "a", "event_type": "stop",
                             "is_interrupt": True})
    assert st("a") == "working"  # 打断=还在干
    ingest_event(conn, env, {"session_id": "a", "event_type": "session_end"})
    assert st("a") == "done"


def test_out_of_order_old_event_does_not_override(conn, controller):
    """6.3: 派生状态按 seq 单调,乱序旧事件不覆盖新状态。"""
    env = _ctrl_env(controller)
    ingest_event(conn, env, {"session_id": "x", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "x", "event_type": "session_end"})
    # 人为抬高 last_seq,再投一个"旧"事件(seq 必然更小)→ 不覆盖
    conn.execute("UPDATE session_states SET last_seq=999999 WHERE session_id='x'")
    ingest_event(conn, env, {"session_id": "x", "event_type": "user_prompt"})
    st = conn.execute("SELECT state FROM session_states WHERE session_id='x'"
                      ).fetchone()["state"]
    assert st == "done"  # 旧事件不覆盖


def test_pre_tool_use_activates_dispatch(conn, controller, worker):
    """5.1 开工证据: pre_tool_use → 派单 active+任务 executing(联动)。"""
    tid, did, secret = _issue_and_spawn(conn, controller, worker)
    assert ops.dispatch_get(conn, did)["status"] == "issued"
    ingest_event(conn, _env(worker, secret, did),
                 {"session_id": "s", "event_type": "session_start"})
    ingest_event(conn, _env(worker, secret, did),
                 {"session_id": "s", "event_type": "pre_tool_use",
                  "payload": {"tool_name": "Read"}})
    assert ops.dispatch_get(conn, did)["status"] == "active"
    assert ops.task_get(conn, tid)["status"] == "executing"


def test_codex_hook_translate_mapping():
    """codex 适配器: 0.146.1 实证有 SessionEnd;Stop=每轮结束;8 类交集;非交集忽略。"""
    from tianji.adapters.template import translate
    e = translate("codex", {"hook_event_name": "SessionStart", "session_id": "s1",
                            "cwd": "/x"})
    assert e["event_type"] == "session_start" and e["session_id"] == "s1"
    assert e["payload"] == {"cwd": "/x"}  # 原文保留,名字段剔除
    assert translate("codex", {"hook_event_name": "SessionEnd", "session_id": "s1"}
                     )["event_type"] == "session_end"
    e = translate("codex", {"hook_event_name": "Stop", "session_id": "s1"})
    assert e["event_type"] == "stop" and e["is_interrupt"] is False
    assert translate("codex", {"hook_event_name": "PreToolUse", "session_id": "s"}
                     )["event_type"] == "pre_tool_use"
    assert translate("codex", {"hook_event_name": "PreCompact"}
                     ) is None  # 非交集忽略


def test_codex_hook_translate_fallback_keys():
    """codex 载荷字段名未完全文档化: 事件名/会话 id 常见键兜底。"""
    from tianji.adapters.template import translate
    e = translate("codex", {"event": "UserPromptSubmit", "conversation_id": "c9"})
    assert e["event_type"] == "user_prompt" and e["session_id"] == "c9"
    e = translate("codex", {"type": "PostToolUse", "sessionId": "s7"})
    assert e["event_type"] == "post_tool_use" and e["session_id"] == "s7"
