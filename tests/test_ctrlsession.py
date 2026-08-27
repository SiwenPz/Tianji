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
from tianji.db import injected_dir

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
    from tianji.ctrlprotocols import ClaudeStreamBackend
    monkeypatch.setenv("TIANJI_HOME", str(tmp_path))
    injected_dir().mkdir(parents=True, exist_ok=True)
    (injected_dir() / "settings-controller.json").write_text(_json.dumps(
        {"env": {}, "appendSystemPrompt": "角色话术"}), encoding="utf-8")
    captured = {}

    class FakeProc:
        stdin = None
        stdout = iter([])
        poll = lambda self: 0

    monkeypatch.setattr(
        "tianji.ctrlprotocols.subprocess", _sp,
        raising=False,
    )
    # capture Popen args by monkeypatching at module level
    orig_popen = _sp.Popen
    def fake_popen(cmd, **kw):
        captured["cmd"] = list(cmd) if hasattr(cmd, "__iter__") else cmd
        return FakeProc()
    monkeypatch.setattr("tianji.ctrlprotocols.subprocess.Popen", fake_popen)
    monkeypatch.setattr("tianji.ctrlprotocols.shutil.which", lambda n: "claude")

    b = ClaudeStreamBackend(
        home=tmp_path, launch=["claude"],
        data_root_env=None, provider_env={},
    )
    b.start(tmp_path)
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


# ---------------------------------------------------------------- ⑤ 会话续命 / ⑦ 模型 pin

def _capture_popen(monkeypatch):
    """monkeypatch Popen 捕获 cmd(不真起 claude),返回 captured dict。"""
    import subprocess as _sp
    from tianji import ctrlprotocols
    captured = {}

    class FakeProc:
        class _Stdin:
            def write(self, s):
                pass
            def flush(self):
                pass
        stdin = _Stdin()
        stdout = iter([])
        poll = lambda self: None  # 活

    monkeypatch.setattr(
        "tianji.ctrlprotocols.subprocess", _sp,
        raising=False,
    )
    def fake_popen(cmd, **kw):
        captured["cmd"] = list(cmd) if hasattr(cmd, "__iter__") else cmd
        return FakeProc()
    monkeypatch.setattr("tianji.ctrlprotocols.subprocess.Popen", fake_popen)
    monkeypatch.setattr("tianji.ctrlprotocols.shutil.which", lambda n: "claude")
    return captured


def test_model_pin_explicit(tmp_path, monkeypatch):
    """⑦ 模型 pin: ctrl_session 显式 model → 翻译成 --model(规格书 13.3 授权,
    实现细节不回写);无 model 不传,不破坏本地默认。"""
    import json as _json
    from tianji.ctrlprotocols import ClaudeStreamBackend
    monkeypatch.setenv("TIANJI_HOME", str(tmp_path))
    injected_dir().mkdir(parents=True, exist_ok=True)
    (injected_dir() / "settings-controller.json").write_text(_json.dumps(
        {"env": {}, "appendSystemPrompt": ""}), encoding="utf-8")
    captured = _capture_popen(monkeypatch)

    b = ClaudeStreamBackend(
        home=tmp_path, launch=["claude"],
        data_root_env=None, provider_env={},
        model="deepseek-v4-flash")
    b.start(tmp_path)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "deepseek-v4-flash"

    captured["cmd"] = None
    b2 = ClaudeStreamBackend(
        home=tmp_path, launch=["claude"],
        data_root_env=None, provider_env={},)
    b2.start(tmp_path)
    assert "--model" not in captured["cmd"]


def test_resume_flag_composed(tmp_path, monkeypatch):
    """⑤ 有落盘 session_id → start 命令带 --resume;无盘 → 不带。"""
    import json as _json
    from tianji.ctrlprotocols import ClaudeStreamBackend, _persist_session
    monkeypatch.setenv("TIANJI_HOME", str(tmp_path))
    injected_dir().mkdir(parents=True, exist_ok=True)
    (injected_dir() / "settings-controller.json").write_text(_json.dumps(
        {"env": {}, "appendSystemPrompt": ""}), encoding="utf-8")
    captured = _capture_popen(monkeypatch)

    _persist_session(tmp_path, "sess-123")
    b = ClaudeStreamBackend(
        home=tmp_path, launch=["claude"],
        data_root_env=None, provider_env={},)
    b.start(tmp_path)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--resume") + 1] == "sess-123"

    from tianji.ctrlprotocols import _clear_persisted_session
    _clear_persisted_session(tmp_path)  # 无盘场景
    captured["cmd"] = None
    b2 = ClaudeStreamBackend(
        home=tmp_path, launch=["claude"],
        data_root_env=None, provider_env={},)
    b2.start(tmp_path)
    assert "--resume" not in captured["cmd"]


def test_session_id_persisted(tmp_path):
    """⑤ session_id 拿到即落盘 .ctrl-session.json(崩溃后 --resume 续命)。"""
    from tianji.ctrlprotocols import _load_persisted_session
    s = _fake()
    try:
        s.start(tmp_path)
        s.send("你好")
        assert _wait(lambda: len(_results(s)) >= 1)
    finally:
        s.close()
    assert _load_persisted_session(tmp_path) == "fake-sess"


def test_resume_fail_clears_and_retries(tmp_path, monkeypatch):
    """⑤ 带 --resume 起后 5s 内死=resume 失败: send 清盘重开,重启事件注明,
    新命令不带死 id(不留脏存档反复踩)。"""
    import json as _json
    import time as _t
    from tianji.ctrlprotocols import ClaudeStreamBackend, _persist_session
    monkeypatch.setenv("TIANJI_HOME", str(tmp_path))
    injected_dir().mkdir(parents=True, exist_ok=True)
    (injected_dir() / "settings-controller.json").write_text(_json.dumps(
        {"env": {}, "appendSystemPrompt": ""}), encoding="utf-8")
    _persist_session(tmp_path, "dead-sess")
    captured = _capture_popen(monkeypatch)

    class DeadProc:
        stdin = None
        stdout = iter([])
        def poll(self):
            return 1  # 死

    class AliveProc:
        class _Stdin:
            def write(self, s):
                pass
            def flush(self):
                pass
        stdin = _Stdin()
        stdout = iter([])
        def poll(self):
            return None

    b = ClaudeStreamBackend(
        home=tmp_path, launch=["claude"],
        data_root_env=None, provider_env={},)
    b.start(tmp_path)
    assert "--resume" in captured["cmd"]  # 首起带 resume(盘上有 id)
    b._started_at = _t.monotonic() - 1.0  # 模拟年轻死亡(起后 5s 内)
    b.proc = DeadProc()
    b.send("hi")
    assert "--resume" not in captured["cmd"]  # 重开不带死 id
    assert not (tmp_path / ".ctrl-session.json").exists()  # 清盘
    restarts = [e for e in b.get_events(0)[0]
                if e.get("subtype") == "restart"]
    assert restarts and "清盘" in restarts[0]["note"]


# 首行后紧跟上下文压缩事件(实机形状: system/subtype=compacting)
FAKE_COMPACT = r"""
import json, sys
sid = "fake-sess"
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}),
      flush=True)
print(json.dumps({"type": "system", "subtype": "compacting",
                  "session_id": sid, "text": "context compaction underway"}),
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
    print(json.dumps({"type": "assistant", "session_id": sid,
                      "message": {"role": "assistant", "content": [
                          {"type": "text", "text": "回声:" + text}]}}),
          flush=True)
    print(json.dumps({"type": "result", "session_id": sid,
                      "duration_ms": 3, "total_cost_usd": 0.0}), flush=True)
"""


def test_overflow_clears_resume(tmp_path):
    """⑤ 上下文压缩/溢出事件 → 清 resume 存档 + session_reset 事件
    (防下次 --resume 回旧上下文)。"""
    s = _fake(FAKE_COMPACT)
    try:
        s.start(tmp_path)
        s.send("你好")
        assert _wait(lambda: len(_results(s)) >= 1)
        assert not (tmp_path / ".ctrl-session.json").exists()
        evs, _ = s.get_events(0)
        assert any(e.get("subtype") == "session_reset" for e in evs)
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
