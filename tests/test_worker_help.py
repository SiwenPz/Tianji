"""工人求助(worker_help / worker_help_reply): 消息类型+挂起/恢复/超时升级+档案+迁移。"""

import os
import sqlite3
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import connect, ledger_path, now, tx
from tianji.messages import MSG_RECIPIENTS, validate_message
from tianji.monitor import _tick
from tianji.render import spawn
from tianji.schema import MSG_TYPES, render_schema


def _active_dispatch(conn, controller, worker):
    """任务 dispatched+派单 issued(未开工),返回 (tid, did, worker_env)。"""
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
    return tid, did, env


def test_worker_help_types_in_msg_types():
    """验收 1: 两种新消息在 MSG_TYPES 白名单内。"""
    assert "worker_help" in MSG_TYPES
    assert "worker_help_reply" in MSG_TYPES


def test_worker_help_recipients():
    """验收 1: worker_help 定向总控,worker_help_reply 定向工人。"""
    validate_message("worker_help", "controller")
    validate_message("worker_help_reply", "worker")
    with pytest.raises(Exception):
        validate_message("worker_help", "worker")
    with pytest.raises(Exception):
        validate_message("worker_help_reply", "controller")


def test_help_suspend_ladder(conn, controller, worker, monkeypatch):
    """验收 2: 未答复 worker_help 期间监控器不报停滞(挂起活性阶梯)。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE configs SET value='1' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='2' WHERE key='t2_seconds'")
    conn.execute("UPDATE dispatches SET created_at=? WHERE id=?", (ops.now() - 100, did))
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=?", (ops.now() - 100, worker["worker_id"]))
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    # 发 worker_help
    conn.execute(
        "INSERT INTO messages (ts, type, sender, recipient_role, payload) VALUES (?,?,?,?,?)",
        (ops.now() - 50, "worker_help", worker["worker_id"], "controller", '{"claim":"思路"}'))
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "active"
    assert not [e for e in _escalations(conn) if "静默超" in (e.get("reason") or "")]
    # 审计 monitor_help_suspend
    aud = conn.execute("SELECT detail FROM audit WHERE action='monitor_help_suspend'").fetchone()
    assert aud is not None


def test_help_reply_resumes_ladder(conn, controller, worker, monkeypatch):
    """验收 2: worker_help_reply 送达后恢复活性计时。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE configs SET value='1' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='2' WHERE key='t2_seconds'")
    conn.execute("UPDATE dispatches SET created_at=? WHERE id=?", (ops.now() - 100, did))
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=?", (ops.now() - 100, worker["worker_id"]))
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    # 发 worker_help
    conn.execute(
        "INSERT INTO messages (ts, type, sender, recipient_role, payload) VALUES (?,?,?,?,?)",
        (ops.now() - 50, "worker_help", worker["worker_id"], "controller", '{"claim":"思路"}'))
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "active"
    # 总控答复 worker_help_reply(收件角色=worker)
    conn.execute(
        "INSERT INTO messages (ts, type, sender, recipient_role, payload) VALUES (?,?,?,?,?)",
        (ops.now() - 40, "worker_help_reply", "总控", "worker",
         '{"worker_id": "' + worker["worker_id"] + '"}'))
    state = {}
    print(f"[DEBUG] pending_help_after_reply={mon._pending_worker_help(conn, worker['worker_id'])}")
    _tick(conn, state)  # 恢复计时, hits=1
    print(f"[DEBUG] tick1: pending={mon._pending_worker_help(conn, worker['worker_id'])}, t2_hits={state.get('t2_hits', {}).get(did, 0)}, dispatch={ops.dispatch_get(conn, did)['status']}")
    _tick(conn, state)  # hits=2 -> stale
    print(f"[DEBUG] tick2: pending={mon._pending_worker_help(conn, worker['worker_id'])}, t2_hits={state.get('t2_hits', {}).get(did, 0)}, dispatch={ops.dispatch_get(conn, did)['status']}")
    _tick(conn, state)  # 确认 stale
    print(f"[DEBUG] tick3: pending={mon._pending_worker_help(conn, worker['worker_id'])}, t2_hits={state.get('t2_hits', {}).get(did, 0)}, dispatch={ops.dispatch_get(conn, did)['status']}")
    assert ops.dispatch_get(conn, did)["status"] == "stale"


def test_help_timeout_escalates(conn, controller, worker, monkeypatch):
    """验收 3: worker_help 超 T2 无答复→升级总控"有求助未响应"。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE configs SET value='1' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='2' WHERE key='t2_seconds'")
    conn.execute("UPDATE dispatches SET created_at=? WHERE id=?", (ops.now() - 100, did))
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=?", (ops.now() - 100, worker["worker_id"]))
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    # 发 worker_help 超 T2(设为 5 秒前)
    conn.execute(
        "INSERT INTO messages (ts, type, sender, recipient_role, payload) VALUES (?,?,?,?,?)",
        (ops.now() - 10, "worker_help", worker["worker_id"], "controller", '{"claim":"思路"}'))
    state = {}
    _tick(conn, state)
    assert any("有求助未响应" in e["reason"] for e in _escalations(conn))


def test_help_does_not_consume_retry(conn, controller, worker):
    """验收 4: 发送 worker_help 不消耗重派计数。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute(
        "INSERT INTO messages (ts, type, sender, recipient_role, payload) VALUES (?,?,?,?,?)",
        (ops.now(), "worker_help", worker["worker_id"], "controller", '{"claim":"思路"}'))
    assert ops.task_get(conn, tid)["retry_count"] == 0


def test_help_recorded_in_ability_profile(conn, controller, worker):
    """验收 4: worker_help 记录入实例档案(help_count/last_help_at/last_help_claim)。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    profile_before = conn.execute(
        "SELECT help_count, last_help_at, last_help_claim FROM ability_profiles"
        " WHERE instance_name=?", (worker["worker_id"],)).fetchone()
    assert profile_before["help_count"] == 0
    # 通过 message send 发送 worker_help(走 CLI 同一事务)
    env2 = {**os.environ, "TIANJI_WORKER_ID": worker["worker_id"],
            "TIANJI_SECRET": worker["secret"]}
    with tx(conn) as c:
        from tianji.messages import send
        send(c, "worker_help", worker["worker_id"], {"claim": "思路"}, "controller")
    profile_after = conn.execute(
        "SELECT help_count, last_help_at, last_help_claim FROM ability_profiles"
        " WHERE instance_name=?", (worker["worker_id"],)).fetchone()
    assert profile_after["help_count"] == 1
    assert profile_after["last_help_at"] > 0
    assert profile_after["last_help_claim"] == "思路"


def test_taskbook_contains_help_clause(conn, controller, worker):
    """验收 5: 任务书渲染含求助条款。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    text = Path(s["taskbook"]).read_text(encoding="utf-8")
    assert "求助纪律" in text
    assert "能自己查证的不许问" in text


def test_messages_check_migration_rebuilds_table(tmp_path, monkeypatch):
    """验收 6: messages 表 CHECK 白名单过期则重建(旧账本缺少新类型可写)。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TIANJI_HOME", str(home))
    # 手工建一个旧 schema 账本(messages 表只有旧类型)
    ledger = home / "ledger.db"
    old = sqlite3.connect(ledger)
    old.execute(
        "CREATE TABLE messages ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,"
        " type TEXT NOT NULL CHECK (type IN ('task_suggest','dispatch')),"
        " sender TEXT NOT NULL, recipient_role TEXT, payload TEXT NOT NULL)")
    old.execute("CREATE TABLE cursors (consumer_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL DEFAULT 0)")
    old.execute("CREATE TABLE receipts (request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, result TEXT NOT NULL)")
    old.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new')")
    old.execute("CREATE TABLE dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, worker_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'issued')")
    old.execute("CREATE TABLE instances (name TEXT PRIMARY KEY, shell TEXT NOT NULL, model TEXT NOT NULL)")
    old.execute("CREATE TABLE instance_registrations (id INTEGER PRIMARY KEY AUTOINCREMENT, instance_name TEXT NOT NULL, dispatch_id INTEGER, status TEXT NOT NULL DEFAULT 'spawned')")
    old.execute("CREATE TABLE ability_profiles (instance_name TEXT PRIMARY KEY, shell TEXT NOT NULL, model TEXT NOT NULL)")
    old.execute("CREATE TABLE configs (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    old.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL)")
    old.commit()
    old.close()
    conn = connect()
    try:
        # 新类型可写
        from tianji.messages import send
        with tx(conn) as c:
            send(c, "worker_help", "worker1", {"claim": "思路"}, "controller")
        row = conn.execute(
            "SELECT type FROM messages WHERE sender='worker1' AND type='worker_help'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def _escalations(conn, kind=None):
    rows = conn.execute(
        "SELECT payload FROM messages WHERE type='escalation' ORDER BY seq").fetchall()
    import json
    return [json.loads(r["payload"]) for r in rows]


def test_help_reply_for_other_worker_does_not_resume(conn, controller, worker):
    """返修回归: 给其他工人的 worker_help_reply 不算本工人答复(跨工人误判)。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute(
        "INSERT INTO messages (ts, type, sender, recipient_role, payload) VALUES (?,?,?,?,?)",
        (ops.now() - 50, "worker_help", worker["worker_id"], "controller", '{"claim":"思路"}'))
    # 总控回复的是"别的工人"(payload.worker_id 不同)
    conn.execute(
        "INSERT INTO messages (ts, type, sender, recipient_role, payload) VALUES (?,?,?,?,?)",
        (ops.now() - 40, "worker_help_reply", "总控", "worker", '{"worker_id": "别的工人"}'))
    pending = mon._pending_worker_help(conn, worker["worker_id"])
    assert pending is not None  # 本工人求助仍未答复


# ---------------------------------------------------------------------------
# 7.5 续推通道: 总控 nudge 命令(成功翻译/不支持 fail-loud/不计重派不扣分)
# ---------------------------------------------------------------------------

def _issue_dispatch(conn, controller, worker_id):
    """建任务并派单(spawn 未开工),返回 (tid, did)。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker_id,
                             request_id="r-issue")["dispatch_id"]
    spawn(conn, worker_id, did)
    return tid, did


def test_nudge_supported_shell_success(conn, controller):
    """验收: 支持续跑的壳(atomcode)nudge 成功,翻译续跑命令+审计+档案计数。"""
    r = ops.instance_register(conn, "云哥", "atomcode", "m")
    tid, did = _issue_dispatch(conn, controller, "云哥")
    out = ops.dispatch_nudge(conn, controller, did, request_id="r-nudge")
    assert out["supported"] is True
    assert out["shell"] == "atomcode"
    assert "atomcode -c -p" in out["cmd"]  # 无头续会话原语(实证)
    assert str(did) in out["prompt"] or "任务书" in out["prompt"]
    # 审计闭环
    aud = conn.execute(
        "SELECT detail FROM audit WHERE action='dispatch_nudge'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    assert aud is not None and "atomcode" in aud["detail"]
    # 实例档案: nudge_count+1
    p = conn.execute(
        "SELECT nudge_count FROM ability_profiles WHERE instance_name='云哥'"
    ).fetchone()
    assert p["nudge_count"] == 1


def test_nudge_unsupported_shell_fail_loud(conn, controller, worker):
    """验收: 不支持续跑的壳(codex)fail-loud+审计+实例档案,退回人工。"""
    tid, did = _issue_dispatch(conn, controller, worker["worker_id"])
    out = ops.dispatch_nudge(conn, controller, did, request_id="r-nudge")
    assert out["supported"] is False
    assert "退回人工" in out["reason"]
    # 审计留痕
    aud = conn.execute(
        "SELECT detail FROM audit WHERE action='dispatch_nudge_unsupported'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    assert aud is not None
    # 实例档案 notes 记录失败原因
    p = conn.execute(
        "SELECT notes FROM ability_profiles WHERE instance_name=?",
        (worker["worker_id"],)).fetchone()
    assert p["notes"] and "退回人工" in p["notes"]


def test_nudge_does_not_consume_retry_or_score(conn, controller, worker):
    """验收: 连续 nudge 不计重派数、不扣表现分(不是失败,7.5)。"""
    tid, did = _issue_dispatch(conn, controller, worker["worker_id"])
    score_before = conn.execute(
        "SELECT score FROM ability_profiles WHERE instance_name=?",
        (worker["worker_id"],)).fetchone()["score"]
    ops.dispatch_nudge(conn, controller, did, request_id="r-n1")
    ops.dispatch_nudge(conn, controller, did, request_id="r-n2")
    ops.dispatch_nudge(conn, controller, did, request_id="r-n3")
    assert ops.task_get(conn, tid)["retry_count"] == 0
    score_after = conn.execute(
        "SELECT score FROM ability_profiles WHERE instance_name=?",
        (worker["worker_id"],)).fetchone()["score"]
    assert score_after == score_before
    # 派单状态机不动(不改任务/派单状态)
    assert ops.dispatch_get(conn, did)["status"] in ("issued", "active")


def test_nudge_requires_controller(conn, controller, worker):
    """验收: nudge 仅总控身份可执行(花钱动作,14.5)。"""
    tid, did = _issue_dispatch(conn, controller, worker["worker_id"])
    with pytest.raises(PermissionError, match="总控"):
        ops.dispatch_nudge(conn, worker, did, request_id="r-n")
