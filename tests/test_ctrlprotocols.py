"""总控会话协议 backend(ctrlprotocols): ACP 全流 + 三路帧 + 参数化。

测试隔离: monkeypatch home → tmp; KIMI_CODE_HOME 也指到 tmp,不碰用户真实 ~/.kimi-code/。
罐頭脚本模仿 kimi acp: initialize → session/new → session/prompt(通知流+收尾)。
脚本写临时 .py 文件再由 sys.executable 执行(避免 -c 长脚本在 Windows 的编码/长度问题)。
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from tianji import ctrlprotocols
from tianji.db import injected_dir


# ===================================================================
# 辅助: 写罐頭脚本到临时 .py 文件,返回可执行命令
# ===================================================================

def _fake_script(tmp_path: Path, content: str) -> Path:
    """写罐頭 ACP 脚本到 .py,return .py 绝对路径(供 sys.executable 执行)。"""
    s = tmp_path / "fake_acp.py"
    s.write_text(content, encoding="utf-8")
    return s


# ===================================================================
# 罐頭脚本: 虚假 kimi acp 进程
# ===================================================================

FAKE_ACP = r'''
import json, sys, os

# KIMI_CODE_HOME 指向 isolated dir (测试用)
isolated = os.environ.get("KIMI_CODE_HOME", "/tmp/fake-kimi")

pending = {}  # id → {method, params}

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

# 先吐一条 diagnostic 到 stderr (测试 stderr 通道)
sys.stderr.write("[fake-kimi] acp server starting\n")
sys.stderr.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except ValueError:
        continue

    mid = obj.get("method")
    rid = obj.get("id")
    params = obj.get("params", {})

    if rid is not None and mid is None:
        # response (不应出现, client 不 expect response 除了 _rpc_call)
        pass
    elif mid and rid is not None:
        # request: _rpc_call 的 initialize / session/new
        if mid == "initialize":
            send({"jsonrpc": "2.0", "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "sessionCapabilities": {"list": {}, "loadSession": True},
                "promptCapabilities": {"vision": False},
            }, "id": rid})
        elif mid == "session/new":
            send({"jsonrpc": "2.0", "result": {
                "sessionId": "fake-acp-sess",
                "cwd": params.get("cwd", ""),
            }, "id": rid})
        elif mid == "session/prompt":
            # 记下来
            pending[rid] = {"method": mid, "params": params}
            # 先吐 assistant 文本通知(实测形状: params.update.sessionUpdate)
            text = params.get("prompt", [{}])[0].get("text", "")
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "fake-acp-sess",
                             "update": {
                                 "sessionUpdate": "agent_message_chunk",
                                 "content": {"type": "text",
                                             "text": "echo:" + text[:80]}}}})
            # 再吐 thinking 通知
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "fake-acp-sess",
                             "update": {
                                 "sessionUpdate": "agent_thought_chunk",
                                 "content": {"type": "text",
                                             "text": "I think: " + text[:40]}}}})
            # 收尾 response
            send({"jsonrpc": "2.0", "result": {
                "stopReason": "end", "usage": {"input": 10, "output": 20}
            }, "id": rid})
        elif mid == "session/cancel":
            send({"jsonrpc": "2.0", "result": {"cancelled": True}, "id": rid})
        else:
            send({"jsonrpc": "2.0", "error": {"code": -32601,
                   "message": "unknown method: " + mid}, "id": rid})
    elif mid and rid is None:
        # notification from server (不会出现, server 是 Responder)
        pass

sys.stderr.write("[fake-kimi] acp server exiting\n")
sys.stderr.flush()
'''


FAKE_ACP_WITH_REQUEST = r'''import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except ValueError:
        continue
    mid = obj.get("method")
    rid = obj.get("id")
    if mid == "initialize":
        send({"jsonrpc": "2.0", "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True},
            "sessionCapabilities": {"list": {}, "loadSession": True},
            "promptCapabilities": {},
        }, "id": rid})
    elif mid == "session/new":
        send({"jsonrpc": "2.0", "result": {
            "sessionId": "fake-acp-sess",
        }, "id": rid})
    elif mid == "session/prompt":
        # reverse-RPC: 发 request_permission 给 client
        send({"jsonrpc": "2.0", "method": "session/request_permission",
              "params": {"sessionId": "fake-acp-sess",
                         "permission": {"type": "tool",
                                        "tool": "Bash"}},
              "id": 999})
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": "fake-acp-sess",
                         "update": {
                             "sessionUpdate": "agent_message_chunk",
                             "content": {"type": "text",
                                         "text": "permission?"}}}})
        send({"jsonrpc": "2.0", "result": {"stopReason": "end",
                                           "usage": {}}, "id": rid})
    else:
        send({"jsonrpc": "2.0", "result": {}, "id": rid})
'''


FAKE_ACP_INIT_FAIL = r'''
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except ValueError:
        continue
    if obj.get("method") == "initialize":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": "Authentication required"},
            "id": obj.get("id"),
        }) + "\n")
        sys.stdout.flush()
        sys.exit(1)
'''


# ===================================================================
# Fixtures
# ===================================================================
from pathlib import Path  # noqa: E402 (fmt after constants)


@pytest.fixture
def acp_home(tmp_path, monkeypatch):
    """独立 TIANJI_HOME / KIMI_CODE_HOME → tmp 下 isolated dir。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TIANJI_HOME", str(home))
    kimi_home = tmp_path / "fake-kimi-code"
    kimi_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    injected_dir().mkdir(parents=True, exist_ok=True)
    (injected_dir() / "ctrl-secret.txt").write_text("test-secret-abc", encoding="utf-8")
    return home


@pytest.fixture
def fake_acp_script(tmp_path):
    """写 FAKE_ACP → .py,return (_fake_script 结果, launch 命令)。"""
    p = _fake_script(tmp_path, FAKE_ACP)
    return str(p), [sys.executable, str(p)]


@pytest.fixture
def backend(fake_acp_script, acp_home):
    """造一个 ACPBackend (不 start,测试显式 start)。"""
    _, launch = fake_acp_script
    b = ctrlprotocols.ACPBackend(
        home=acp_home, launch=launch,
        data_root_env="KIMI_CODE_HOME",
        provider_env={"target": "process_env", "map": {
            "KIMI_MODEL_NAME": "{model}",
            "KIMI_MODEL_API_KEY": "{key}",
            "KIMI_MODEL_BASE_URL": "{base_url}",
            "KIMI_MODEL_PROVIDER_TYPE": "{protocol}",
        }},
        key_ref="", model="", base_url="", protocol="anthropic",
        role_text="",
    )
    return b


def _wait_events(b, count, timeout=8.0):
    """轮询直到 _events ≥ count 或超时。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        evs, _ = b.get_events(0)
        if len(evs) >= count:
            return evs
        time.sleep(0.05)
    return []


# ===================================================================
# Tests: 生命周期
# ===================================================================


class TestACPLifecycle:
    def test_start_initialize_and_session(self, backend):
        backend.start()
        assert backend.is_alive()
        t0 = time.time()
        while time.time() - t0 < 5:
            if backend.session_id is not None:
                break
            time.sleep(0.05)
        assert backend.session_id == "fake-acp-sess"

    def test_start_init_failure(self, acp_home, fake_acp_script):
        fail_script = _fake_script(acp_home, FAKE_ACP_INIT_FAIL)
        b = ctrlprotocols.ACPBackend(
            home=acp_home,
            launch=[sys.executable, str(fail_script)],
            data_root_env="KIMI_CODE_HOME",
        provider_env={"target": "process_env", "map": {
            "KIMI_MODEL_NAME": "{model}",
            "KIMI_MODEL_API_KEY": "{key}",
            "KIMI_MODEL_BASE_URL": "{base_url}",
            "KIMI_MODEL_PROVIDER_TYPE": "{protocol}",
        }},
        )
        b.start()
        # 假进程收到 error response 后退出,给一点 kernel 时间回收;
        # 失败场景下 _rpc_call 在 error response 后 return,start() 不关 proc
        evs, _ = b.get_events(0)
        assert any(
            e.get("type") == "system" and e.get("subtype") == "error"
            for e in evs
        )
        # 确认没拿到 session_id (初始化失败)
        assert b.session_id is None
        # 清理: 关掉仍在等 stdin 的假进程
        b.close()

    def test_send_and_receive(self, backend):
        backend.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if backend.session_id:
                break
            time.sleep(0.05)

        backend.send("hello")
        evs = _wait_events(backend, 3)
        types = [e["type"] for e in evs]
        assert "assistant" in types
        assert "result" in types

        assistant = [e for e in evs if e["type"] == "assistant"]
        assert "echo:hello" in assistant[0]["text"]

    def test_multi_turn_context(self, backend):
        backend.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if backend.session_id:
                break
            time.sleep(0.05)

        backend.send("第一条")
        # 假进程: 1 条 assistant + 1 条 thinking + 1 条 result → result 走 _prompt_ids 被吞
        _wait_events(backend, 2)
        evs1, n1 = backend.get_events(0)

        backend.send("第二条")
        # Turn 2 再发 1 条 assistant text → 等 2 条新事件
        _wait_events(backend, 2 + n1)
        evs2, _ = backend.get_events(n1)

        texts = [e["text"] for e in evs2 if e["type"] == "assistant"]
        assert len(texts) == 1
        assert "第二条" in texts[0]

    def test_crash_restart(self, backend):
        backend.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if backend.session_id:
                break
            time.sleep(0.05)

        backend.send("先聊")
        _wait_events(backend, 1)

        backend.proc.kill()
        t0 = time.time()
        while time.time() - t0 < 3:
            if not backend.is_alive():
                break
            time.sleep(0.05)

        backend.send("又活了")
        evs = _wait_events(backend, 3)
        restarts = [
            e for e in evs
            if e.get("type") == "system" and e.get("subtype") == "restart"
        ]
        assert len(restarts) == 1
        assert "重启" in restarts[0]["note"]

        t0 = time.time()
        while time.time() - t0 < 5:
            if backend.session_id == "fake-acp-sess":
                break
            time.sleep(0.05)
        assert backend.session_id == "fake-acp-sess"

    def test_close(self, backend):
        backend.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if backend.is_alive():
                break
            time.sleep(0.05)
        assert backend.is_alive()
        backend.close()
        assert not backend.is_alive()


# ===================================================================
# Tests: 三路帧
# ===================================================================


class TestFrameRouting:
    def test_notification_translated(self, backend):
        backend.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if backend.session_id:
                break
            time.sleep(0.05)

        backend.send("ping")
        evs = _wait_events(backend, 2, timeout=8)
        ai = [e for e in evs if e["type"] == "assistant"]
        assert len(ai) >= 1
        assert "echo:ping" in ai[0]["text"]

    def test_result_event_from_prompt_response(self, backend):
        backend.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if backend.session_id:
                break
            time.sleep(0.05)

        backend.send("hi")
        evs = _wait_events(backend, 1, timeout=8)
        # first event is the assistant notification;result may follow → 多等一下
        time.sleep(0.5)
        evs_all, _ = backend.get_events(0)
        res = [e for e in evs_all if e["type"] == "result"]
        assert len(res) >= 1
        assert res[0]["stop_reason"] == "end"
        assert res[0]["usage"] == {"input": 10, "output": 20}

    def test_error_response_prompt_level(self, backend):
        """假进程走成功路径;用 time.sleep 等 result 事件出全。"""
        backend.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if backend.session_id:
                break
            time.sleep(0.05)

        backend.send("error test")
        time.sleep(0.5)  # result 出得比 assistant 慢
        evs, _ = backend.get_events(0)
        assert any(e["type"] == "result" for e in evs)


# ===================================================================
# Tests: reverse-RPC (request from server)
# ===================================================================


class TestReverseRPC:
    def test_request_permission_scaffold(self, acp_home, tmp_path):
        req_script = _fake_script(tmp_path, FAKE_ACP_WITH_REQUEST)
        b = ctrlprotocols.ACPBackend(
            home=acp_home,
            launch=[sys.executable, str(req_script)],
            data_root_env="KIMI_CODE_HOME",
        provider_env={"target": "process_env", "map": {
            "KIMI_MODEL_NAME": "{model}",
            "KIMI_MODEL_API_KEY": "{key}",
            "KIMI_MODEL_BASE_URL": "{base_url}",
            "KIMI_MODEL_PROVIDER_TYPE": "{protocol}",
        }},
        )
        b.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if b.session_id:
                break
            time.sleep(0.05)

        b.send("do tool")
        evs = _wait_events(b, 1, timeout=8)
        assert len(evs) >= 1


# ===================================================================
# Tests: stderr 通道
# ===================================================================


class TestStderrChannel:
    def test_stderr_becomes_system_events(self, backend):
        backend.start()
        # 假进程立即写一条 stderr
        evs = _wait_events(backend, 1, timeout=8)
        assert any(
            e.get("type") == "system" and e.get("subtype") == "kimi_log"
            for e in evs
        )


# ===================================================================
# Tests: 角色话术注入
# ===================================================================


class TestRoleInjection:
    def test_role_text_first_message_only(self, acp_home, fake_acp_script):
        b = ctrlprotocols.ACPBackend(
            home=acp_home,
            launch=[sys.executable, str(_fake_script(acp_home, FAKE_ACP))],
            data_root_env="KIMI_CODE_HOME",
        provider_env={"target": "process_env", "map": {
            "KIMI_MODEL_NAME": "{model}",
            "KIMI_MODEL_API_KEY": "{key}",
            "KIMI_MODEL_BASE_URL": "{base_url}",
            "KIMI_MODEL_PROVIDER_TYPE": "{protocol}",
        }},
            role_text="你是一个助手",
        )
        b.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if b.session_id:
                break
            time.sleep(0.05)

        b.send("第一条")
        _wait_events(b, 1)
        assert b._role_injected is True

    def test_no_role_text_no_injection(self, backend):
        backend.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if backend.session_id:
                break
            time.sleep(0.05)
        assert backend._role_injected is False
        backend.send("plain message")
        _wait_events(backend, 1)
        assert backend._role_injected is False


# ===================================================================
# Tests: data_root_env 隔离
# ===================================================================


class TestDataRootIsolation:
    def test_kimi_code_home_set_to_isolated(self, acp_home, fake_acp_script):
        _, launch = fake_acp_script
        b = ctrlprotocols.ACPBackend(
            home=acp_home, launch=launch,
            data_root_env="KIMI_CODE_HOME",
            provider_env={"target": "process_env", "map": {
                "KIMI_MODEL_NAME": "{model}",
                "KIMI_MODEL_API_KEY": "{key}",
                "KIMI_MODEL_BASE_URL": "{base_url}",
                "KIMI_MODEL_PROVIDER_TYPE": "{protocol}",
            }},
        )
        b.start()
        t0 = time.time()
        while time.time() - t0 < 3:
            if b.session_id:
                break
            time.sleep(0.05)
        isolated = acp_home / ".isolated"
        assert isolated.exists()


# ===================================================================
# Tests: key env 构造 (调用模块级函数)
# ===================================================================


class TestProviderEnv:
    """provider_env 构造: 按壳条目 map 模板生成进程级 env(E.2)。"""

    def test_kimi_provider_env(self, acp_home, tmp_path):
        """kimi provider_env + key_ref → KIMI_MODEL_* env 注入。"""
        key_file = tmp_path / "my.key"
        key_file.write_text("sk-test-key-123", encoding="utf-8")
        prov = {"target": "process_env", "map": {
            "KIMI_MODEL_NAME": "{model}",
            "KIMI_MODEL_API_KEY": "{key}",
            "KIMI_MODEL_BASE_URL": "{base_url}",
            "KIMI_MODEL_PROVIDER_TYPE": "{protocol}",
        }}
        env = ctrlprotocols._build_provider_env(
            prov, str(key_file),
            model="", base_url="", protocol="anthropic")
        # 空值不进 env(map 里只有 key 有实际值)
        assert env["KIMI_MODEL_API_KEY"] == "sk-test-key-123"
        assert "KIMI_MODEL_NAME" not in env

    def test_no_key_ref_empty_env(self, acp_home):
        assert ctrlprotocols._build_provider_env(
            {"target": "process_env", "map": {"X": "{key}"}}, "") == {}

    def test_missing_key_file_fails_loud(self, acp_home, tmp_path):
        """key_ref 指了路径但文件不存在 → fail-loud 报错指路,不许静默空串。"""
        prov = {"target": "process_env", "map": {"K": "{key}"}}
        missing = str(tmp_path / "no-such.key")
        with pytest.raises(FileNotFoundError, match="no-such.key"):
            ctrlprotocols._build_provider_env(prov, missing)

    def test_settings_env_target_empty(self, acp_home):
        """target=settings_env 不注入进程 env(settings 文件已写好)。"""
        prov = {"target": "settings_env", "map": {"X": "{key}"}}
        assert ctrlprotocols._build_provider_env(prov, "any") == {}

    def test_empty_provider_env(self, acp_home):
        assert ctrlprotocols._build_provider_env({}, "any") == {}


# ===================================================================
# Tests: 事件翻译映射
# ===================================================================


class TestEventTranslation:
    """实测形状(kimi 0.38.0): params.update.sessionUpdate 是判别字段。"""

    def test_translate_text_content(self):
        raw = {"jsonrpc": "2.0", "method": "session/update",
               "params": {"sessionId": "s1",
                          "update": {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text",
                                                 "text": "hi there"}}}}
        ev = ctrlprotocols._translate_kimi_event(raw)
        assert ev is not None
        assert ev["type"] == "assistant"
        assert ev["text"] == "hi there"

    def test_translate_empty_chunk_ignored(self):
        raw = {"jsonrpc": "2.0", "method": "session/update",
               "params": {"update": {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": ""}}}}
        assert ctrlprotocols._translate_kimi_event(raw) is None

    def test_translate_thinking(self):
        raw = {"jsonrpc": "2.0", "method": "session/update",
               "params": {"update": {"sessionUpdate": "agent_thought_chunk",
                                     "content": {"type": "text",
                                                 "text": "<thinking>?"}}}}
        ev = ctrlprotocols._translate_kimi_event(raw)
        assert ev is not None
        assert ev["type"] == "system"
        assert ev["subtype"] == "thinking_tokens"

    def test_translate_tool_call(self):
        raw = {"jsonrpc": "2.0", "method": "session/update",
               "params": {"update": {"sessionUpdate": "tool_call",
                                     "title": "Bash",
                                     "rawInput": {"cmd": "echo hi"}}}}
        ev = ctrlprotocols._translate_kimi_event(raw)
        assert ev is not None
        assert ev["type"] == "assistant"
        assert ev["tool"]["name"] == "Bash"

    def test_translate_tool_result(self):
        raw = {"jsonrpc": "2.0", "method": "session/update",
               "params": {"update": {"sessionUpdate": "tool_call_update",
                                     "status": "completed",
                                     "rawOutput": "hi\n"}}}
        ev = ctrlprotocols._translate_kimi_event(raw)
        assert ev is not None
        assert ev["type"] == "system"
        assert ev["subtype"] == "tool_result"

    def test_translate_ignorable_updates(self):
        for utype in ("available_commands_update", "session_info_update",
                      "usage_update", "current_mode_update", "plan"):
            raw = {"jsonrpc": "2.0", "method": "session/update",
                   "params": {"update": {"sessionUpdate": utype}}}
            assert ctrlprotocols._translate_kimi_event(raw) is None, utype

    def test_translate_unknown_type_fallback(self):
        raw = {"jsonrpc": "2.0", "method": "session/update",
               "params": {"update": {"sessionUpdate": "future_new_type",
                                     "data": 123}}}
        ev = ctrlprotocols._translate_kimi_event(raw)
        assert ev is not None
        assert ev["type"] == "system"
        assert ev["subtype"] == "session/update:future_new_type"
