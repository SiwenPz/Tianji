"""驾驶舱 Web 交互页(票 03 验收 1-7 + 票 02 残留两条交叉核对)。"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from tianji import auth, messages, ops
from tianji.web import _find_port
from tianji.webapp import app


@pytest.fixture
def client(conn, controller, monkeypatch):
    """页面写操作注入总控身份(15.3);未注入=只读。"""
    monkeypatch.setenv("TIANJI_WORKER_ID", controller["worker_id"])
    monkeypatch.setenv("TIANJI_SECRET", controller["secret"])
    return TestClient(app)


def _task_to(conn, controller, status, title="任务", seq="w"):
    tid = ops.task_new(conn, controller, title, request_id=f"rw-new-{seq}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"rw-{s}-{seq}")
        if status == s:
            return tid
    if status == "awaiting_final_confirm":
        ops.task_force(conn, controller, tid, "awaiting_final_confirm",
                       "测试构造", request_id=f"rw-f-{seq}")
    return tid


def test_layout_four_sections(client):
    """验收 1: 布局四段+抽屉齐全(顶部 bar/4 桶/流程卡片区/总控窗格/抽屉)。"""
    html = client.get("/").text
    for marker in ('id="topbar"', 'id="buckets"', 'id="flow"', 'id="pane"',
                   'id="drawer"', 'id="stream"', 'id="msg"'):
        assert marker in html, marker
    for bucket in ("attention", "working", "done", "idle"):
        assert bucket in html


def test_approve_plan_two_way_sync(client, conn, controller):
    """验收 3: 页面审批后账本变、卡片消失;对话侧(CLI)操作页面同步。"""
    tid = _task_to(conn, controller, "awaiting_plan_confirm", "计划活", "p1")
    state = client.get("/api/state").json()
    assert any(a["kind"] == "plan" and a["task_id"] == tid
               for a in state["approvals"])
    r = client.post("/api/approve", json={
        "kind": "plan", "task_id": tid, "decision": "approve",
        "request_id": "web-p1"})
    assert r.status_code == 200, r.text
    assert ops.task_get(conn, tid)["status"] == "dispatched"
    state2 = client.get("/api/state").json()
    assert not any(a["kind"] == "plan" and a["task_id"] == tid
                   for a in state2["approvals"])
    # 对话侧审批(自然语言入口 15.3)
    tid2 = _task_to(conn, controller, "awaiting_plan_confirm", "计划活2", "p2")
    r2 = client.post("/api/message", json={"text": f"批准 {tid2}"})
    assert r2.status_code == 200, r2.text
    assert ops.task_get(conn, tid2)["status"] == "dispatched"


def test_final_approve_archives(client, conn, controller):
    tid = _task_to(conn, controller, "awaiting_final_confirm", "收尾活", "f1")
    r = client.post("/api/approve", json={
        "kind": "final", "task_id": tid, "decision": "approve",
        "request_id": "web-f1"})
    assert r.status_code == 200, r.text
    assert ops.task_get(conn, tid)["status"] == "archived"


def test_permission_card_and_boundary(client, conn, controller):
    """验收 7 边界: 权限裁决卡渲染在 03;按钮调票 10 的机械执行。"""
    registered = ops.instance_register(conn, "工人", "claude",
                                       "deepseek-v4-flash")
    conn.execute(
        "INSERT INTO instance_registrations"
        " (instance_name, dispatch_id, status, dcap_hash, task_path, created_at)"
        " VALUES (?, NULL, 'spawned', ?, '', '2026-01-01T00:00:00')",
        ("工人", auth.secret_hash(registered["secret"])),
    )
    from tianji.events import ingest_event
    ingest_event(conn, {**os.environ, "TIANJI_WORKER_ID": "工人",
                        "TIANJI_SECRET": registered["secret"],
                        "TIANJI_DISPATCH_ID": "1"},
                 {"session_id": "s9", "event_type": "permission_request",
                  "payload": {"tool": "Bash(*)"}})
    state = client.get("/api/state").json()
    card = [a for a in state["approvals"] if a["kind"] == "permission"][0]
    r = client.post("/api/approve", json={
        "kind": "permission", "ruling_id": card["ruling_id"],
        "decision": "approve", "request_id": "web-pm1"})
    assert r.json()["decision"] == "allowed"
    assert not any(a["kind"] == "permission"
                   for a in client.get("/api/state").json()["approvals"])


def test_escalation_red_then_green(client, conn, controller):
    """验收 4: 升级=红色 note;任务进 reviewing 后恢复转绿。"""
    tid = _task_to(conn, controller, "dispatched", "卡死活", "e1")
    messages.send(conn, "escalation", "monitor",
                  {"task_id": tid, "reason": "静默超 T1"}, "controller")
    esc = client.get("/api/state").json()["escalations"][0]
    assert esc["recovered"] is False
    ops.task_force(conn, controller, tid, "reviewing", "恢复",
                   request_id="rw-e1-rec")
    esc2 = client.get("/api/state").json()["escalations"][0]
    assert esc2["recovered"] is True


def test_drawer_entries_and_org(client, conn, controller):
    """验收 5: 抽屉增删壳/Key 条目(新增标待测试);角色编排表渲染。"""
    r = client.post("/api/entry", json={
        "kind": "key", "name": "网页key",
        "data": {"base_url": "https://x", "models": [], "protocol": "openai"}})
    assert r.status_code == 200, r.text
    v = json.loads(conn.execute(
        "SELECT value FROM configs WHERE key='key:网页key'").fetchone()["value"])
    assert v["tested"] is False
    state = client.get("/api/state").json()
    assert state["org"]["controller"] == "总控"
    assert "网页key" in state["org"]["keys"]
    r2 = client.post("/api/entry/delete",
                     json={"kind": "key", "name": "网页key"})
    assert r2.status_code == 200, r2.text
    assert "网页key" not in client.get("/api/state").json()["org"]["keys"]


def test_drawer_registry_partitions_and_migration(client, conn, controller):
    """票 45: 抽屉显示注册表分区;旧 shell/key 条目可显式迁移。"""
    html = client.get("/").text
    for marker in ('id="registry"', 'data-registry-partition="${kind}"',
                   'id="registry-migrate"', "renderRegistry"):
        assert marker in html, marker
    client.post("/api/entry", json={
        "kind": "key", "name": "迁移key",
        "data": {"base_url": "https://registry.example/v1",
                 "models": [], "protocol": "openai_chat"}})
    client.post("/api/entry", json={
        "kind": "shell", "name": "网页壳",
        "data": {"protocols": ["openai_chat"]}})
    before = client.get("/api/integrations").json()
    assert any(row["legacy"] == "key:迁移key" and not row["migrated"]
               for row in before["migrations"])
    assert any(row["legacy"] == "shell:网页壳" and not row["migrated"]
               for row in before["migrations"])

    migrated = client.post("/api/integrations/migrate", json={})
    assert migrated.status_code == 200, migrated.text
    after = client.get("/api/integrations").json()
    assert any(row["key"].startswith("integration_protocol:")
               for row in after["entries"])
    provider = next(row for row in after["entries"]
                    if row.get("credential_key") == "迁移key")
    shell = next(row for row in after["entries"]
                 if row["key"] == "integration_shell:网页壳")
    assert provider["protocol"] == "openai_chat"
    assert shell["protocols"] == ["openai_chat"]
    assert all(row["migrated"] for row in after["migrations"]
               if row["legacy"] in ("key:迁移key", "shell:网页壳"))


def test_readonly_without_identity(conn, controller, monkeypatch):
    """未注入总控身份: 页面只读,写操作 403。"""
    monkeypatch.delenv("TIANJI_WORKER_ID", raising=False)
    monkeypatch.delenv("TIANJI_SECRET", raising=False)
    c = TestClient(app)
    assert c.get("/api/state").json()["readonly"] is True
    assert c.get("/api/integrations").status_code == 200
    assert 'btn.disabled=cockpitReadonly' in c.get("/").text
    r = c.post("/api/approve", json={"kind": "plan", "task_id": 1,
                                     "decision": "approve"})
    assert r.status_code == 403
    migrate = c.post("/api/integrations/migrate", json={})
    assert migrate.status_code == 403
    assert "只读" in migrate.json()["error"]


def test_port_conflict_slides(tmp_path):
    """验收 6: 端口占用顺延+1。"""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 8899))
    try:
        assert _find_port(8899) == 8900
    finally:
        s.close()


def test_requeue_dispatches_no_card(conn, controller, worker):
    """票 02 残留②: 被取代的 requeue 派单不出卡(假"进行中"消除)。"""
    tid = _task_to(conn, controller, "dispatched", "重派活", "rq")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="rw-rq-1")["dispatch_id"]
    # 模拟: 旧派单 requeue(被取代),会话状态滞留 working
    conn.execute("UPDATE dispatches SET status='requeue' WHERE id=?", (did,))
    conn.execute(
        "INSERT INTO session_states (session_id, instance_name, state,"
        " last_seq, updated_at) VALUES ('sx', ?, 'working', 1, 1)",
        (worker["worker_id"],))
    from tianji.cockpit import snapshot
    snap = snapshot(conn)
    cards = [c for cl in snap.values() if isinstance(cl, list)
             for c in cl if isinstance(c, dict)]
    wcards = [c for c in cards if c.get("dispatch_id") == did]
    assert not wcards  # requeue 不出卡
    inst_cards = [c for c in cards
                  if c["instance_name"] == worker["worker_id"]]
    assert inst_cards and inst_cards[0]["bucket"] == "idle"


def test_card_message_ts_same_source(conn, controller, worker):
    """票 02 残留①: 卡片消息内容与相对时间必须来自同一条记录(已知 ts 交叉核对)。"""
    tid = _task_to(conn, controller, "dispatched", "同源活", "ts")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="rw-ts-1")["dispatch_id"]
    m1 = messages.send(conn, "escalation", "monitor",
                       {"task_id": tid, "reason": "旧警告"}, "controller",
                       ts=1111111)
    m2 = messages.send(conn, "dispatch", "allocator",
                       {"task_id": tid, "dispatch_id": did}, "worker",
                       ts=2222222)
    from tianji.cockpit import snapshot
    snap = snapshot(conn)
    cards = [c for cl in snap.values() if isinstance(cl, list)
             for c in cl if isinstance(c, dict)]
    card = [c for c in cards if c.get("dispatch_id") == did][0]
    # 内容=最新一条(dispatch),ts 也必须=2222222(同一条 m2),不许旧警告顶新时间
    assert card["last_message"]["type"] == "dispatch"
    assert card["last_message"]["ts"] == 2222222
    assert card["last_message_ts"] == 2222222
