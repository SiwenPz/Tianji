"""总控真会话(web 总控对话面): 假子进程喂罐头 stream-json 行 + webapp 接口。

假进程协议(对齐 claude 实测口径): 一启动先吐 init 行;stdin 收到一条 user
消息就回 assistant + result 两行,assistant 文本里回显带来的 session_id,
用来验证多轮透传。
"""

import sys
import time

import pytest
from fastapi.testclient import TestClient

from tianji import webapp
from tianji.ctrlsession import ControllerSession

FAKE = r"""
import json, sys
sid = "fake-sess"
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}),
      flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    text = msg["message"]["content"][0]["text"]
    got = msg.get("session_id") or "-"
    print(json.dumps({"type": "assistant", "session_id": sid,
                      "message": {"role": "assistant", "content": [
                          {"type": "text",
                           "text": "回声[%s]:%s" % (got, text)}]}}),
          flush=True)
    print(json.dumps({"type": "result", "session_id": sid,
                      "duration_ms": 3, "total_cost_usd": 0.0}), flush=True)
"""

# 首行吐一坨非 JSON,验证坏行丢弃不炸
FAKE_BAD = 'print("垃圾行{不是JSON", flush=True)\n' + FAKE


def _fake(script=FAKE):
    # -X utf8: Windows 上子进程管道默认 gbk,中文回显会变乱码
    return ControllerSession(
        cmd_override=[sys.executable, "-X", "utf8", "-c", script])


def _wait(cond, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _results(s):
    return [e for e in s.get_events(0)[0] if e["type"] == "result"]


def test_real_command_composition(tmp_path, monkeypatch):
    """真实命令组装(不真起 claude): --settings 一体文件 + --append-system-prompt
    显式注入 + --include-partial-messages(打字机/思维链原料)。"""
    import json as _json
    import subprocess as _sp
    from tianji import ctrlsession
    (tmp_path / "settings-controller.json").write_text(_json.dumps(
        {"env": {}, "appendSystemPrompt": "角色话术"}), encoding="utf-8")
    captured = {}

    class FakeProc:
        stdin = None
        stdout = iter([])
        poll = lambda self: 0

    monkeypatch.setattr(ctrlsession.subprocess, "Popen",
                        lambda cmd, **kw: captured.update(cmd=cmd) or FakeProc())
    monkeypatch.setattr(ctrlsession.shutil, "which", lambda n: "claude")
    s = ControllerSession()
    s.start(tmp_path)
    cmd = captured["cmd"]
    assert "--include-partial-messages" in cmd
    assert cmd[cmd.index("--settings") + 1].endswith("settings-controller.json")
    assert cmd[cmd.index("--append-system-prompt") + 1] == "角色话术"
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"


def _assistant_texts(s):
    return [b["text"] for e in s.get_events(0)[0] if e["type"] == "assistant"
            for b in e["message"]["content"] if b["type"] == "text"]


def test_send_and_events_cursor(tmp_path):
    """send→假进程回三行;游标拉增量;is_alive/close。"""
    s = _fake()
    try:
        s.start(tmp_path)
        s.send("你好")
        assert _wait(lambda: len(_results(s)) >= 1)
        evs, nxt = s.get_events(0)
        assert [e["type"] for e in evs] == ["system", "assistant", "result"]
        assert nxt == 3
        evs2, nxt2 = s.get_events(nxt)  # 游标到头=空增量
        assert evs2 == [] and nxt2 == nxt
        assert s.session_id == "fake-sess"
        assert s.is_alive()
    finally:
        s.close()
    assert not s.is_alive()


def test_session_id_resend(tmp_path):
    """多轮: 首轮不带 session_id,拿到后第二轮带上(假进程回显验证)。"""
    s = _fake()
    try:
        s.start(tmp_path)
        s.send("第一条")
        assert _wait(lambda: len(_results(s)) >= 1)
        s.send("第二条")
        assert _wait(lambda: len(_results(s)) >= 2)
        texts = _assistant_texts(s)
        assert texts == ["回声[-]:第一条", "回声[fake-sess]:第二条"]
    finally:
        s.close()


def test_bad_line_skipped(tmp_path):
    """stdout 混进非 JSON 行: 丢弃,不炸,后续事件照常收。"""
    s = _fake(FAKE_BAD)
    try:
        s.start(tmp_path)
        s.send("你好")
        assert _wait(lambda: len(_results(s)) >= 1)
        evs, _ = s.get_events(0)
        assert all(isinstance(e, dict) and "type" in e for e in evs)
        assert [e["type"] for e in evs] == ["system", "assistant", "result"]
    finally:
        s.close()


def test_restart_after_crash(tmp_path):
    """进程死了: 下一次 send 如实记一条重启事件,重拉新进程接着聊。"""
    s = _fake()
    try:
        s.start(tmp_path)
        s.send("你好")
        assert _wait(lambda: len(_results(s)) >= 1)
        s.proc.kill()  # 模拟暴毙
        assert _wait(lambda: not s.is_alive())
        s.send("又活了")
        assert _wait(lambda: len(_results(s)) >= 2)
        notes = [e for e in s.get_events(0)[0]
                 if e["type"] == "system" and e.get("subtype") == "restart"]
        assert len(notes) == 1 and "重启" in notes[0]["note"]
        assert s.session_id == "fake-sess"  # 新会话 id 重新拿到
        assert _assistant_texts(s)[-1] == "回声[-]:又活了"  # 新会话首轮不带 id
    finally:
        s.close()


# ---------------------------------------------------------------- webapp 接口

@pytest.fixture
def ctrl_client(conn, controller, monkeypatch, tmp_path):
    """注入总控身份 + 假进程会话(替换模块级懒持有的那个)。"""
    monkeypatch.setenv("TIANJI_WORKER_ID", controller["worker_id"])
    monkeypatch.setenv("TIANJI_SECRET", controller["secret"])
    fake = _fake()
    fake.home = tmp_path  # send 首次拉起时的 cwd
    monkeypatch.setattr(webapp, "_ctrl_session", fake)
    yield TestClient(webapp.app)
    fake.close()


def test_ctrl_send_requires_identity(conn, controller, monkeypatch):
    """未注入总控身份: /api/ctrl/send 403(页面只读口径一致)。"""
    monkeypatch.delenv("TIANJI_WORKER_ID", raising=False)
    monkeypatch.delenv("TIANJI_SECRET", raising=False)
    c = TestClient(webapp.app)
    r = c.post("/api/ctrl/send", json={"text": "你好"})
    assert r.status_code == 403


def test_ctrl_idle_before_first_send(conn, controller, monkeypatch):
    """懒持有: 没聊过时 events 空、status 不活(也不许自动拉起)。"""
    monkeypatch.setattr(webapp, "_ctrl_session", None)
    c = TestClient(webapp.app)
    assert c.get("/api/ctrl/events?after=0").json() == {"events": [], "next": 0}
    assert c.get("/api/ctrl/status").json() == {"alive": False,
                                                "session_id": None}


def test_ctrl_send_events_status(ctrl_client):
    """send→accepted;events 游标拉增量;status 活着且拿到 session_id。"""
    r = ctrl_client.post("/api/ctrl/send", json={"text": "你好"})
    assert r.json() == {"accepted": True}
    assert _wait(lambda: any(
        e["type"] == "result"
        for e in ctrl_client.get("/api/ctrl/events?after=0").json()["events"]))
    data = ctrl_client.get("/api/ctrl/events?after=0").json()
    assert [e["type"] for e in data["events"]] == [
        "system", "assistant", "result"]
    assert data["next"] == 3
    d2 = ctrl_client.get(f"/api/ctrl/events?after={data['next']}").json()
    assert d2["events"] == [] and d2["next"] == data["next"]
    st = ctrl_client.get("/api/ctrl/status").json()
    assert st["alive"] is True and st["session_id"] == "fake-sess"


def test_page_wires_ctrl():
    """页面下半区接真会话: 路由分流与轮询端点都在。"""
    html = TestClient(webapp.app).get("/").text
    for marker in ("/api/ctrl/send", "/api/ctrl/events?after=",
                   "renderCtrl", "pollCtrl"):
        assert marker in html, marker
