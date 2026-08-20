"""模板机制(6.2/6.4): 通用翻译+可插性+装钩子自测+完成判定。

验收标准:
1. 通用性: 模板机制与具体壳解耦,骨架壳能跑通 ingest-event 链路。
2. codex 模板: 事件映射正确,Stop 完成判定生效。
3. 装钩子自测: 模拟事件走完整链路(translate → ingest-event → 查账本)。
4. 钩子失败放行: 坏载荷不抛异常。
"""

from __future__ import annotations

import os

import pytest

from tianji import ops
from tianji.adapters.template import (
    TEMPLATE_ATOMCODE,
    TEMPLATE_CLINE,
    TEMPLATE_CODEX,
    TEMPLATE_DSH,
    TEMPLATE_KIMI,
    Template,
    get_template,
    is_completion_event,
    list_templates,
    translate,
)
from tianji.events import EventError, ingest_event


# ---------------------------------------------------------------------------
# 模板注册表
# ---------------------------------------------------------------------------

class TestTemplateRegistry:
    def test_builtin_templates_registered(self):
        assert "claude" in list_templates()
        assert "codex" in list_templates()
        assert "dsh" in list_templates()
        assert "kimi" in list_templates()
        assert "atomcode" in list_templates()
        assert "cline" in list_templates()

    def test_get_template(self):
        tpl = get_template("codex")
        assert tpl.name == "codex"
        assert tpl.version == "v1"

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError, match="未知壳模板"):
            get_template("nonexistent")

    def test_register_custom(self):
        custom = Template.from_dict({
            "name": "mycli",
            "hook_map": {"Start": "session_start", "End": "session_end"},
            "session_id_keys": ["sid"],
            "payload_exclude_keys": ["sid"],
            "interrupt": {
                "stop_event": "stop",
                "reason_field": None,
                "interrupt_reasons": set(),
                "interrupt_on_empty": False,
            },
            "transcript": {
                "path": "claude",
                "glob": None,
                "session_id_is_filename": False,
            },
        })
        from tianji.adapters import template as tpl_mod
        tpl_mod.register(custom)
        assert "mycli" in list_templates()
        assert get_template("mycli").name == "mycli"


# ---------------------------------------------------------------------------
# 通用翻译
# ---------------------------------------------------------------------------

class TestTranslate:
    def test_codex_session_start(self):
        e = translate("codex", {
            "hook_event_name": "SessionStart",
            "session_id": "s1",
            "cwd": "/x",
        })
        assert e is not None
        assert e["event_type"] == "session_start"
        assert e["session_id"] == "s1"
        assert e["payload"] == {"cwd": "/x"}

    def test_codex_session_end(self):
        e = translate("codex", {"hook_event_name": "SessionEnd", "session_id": "s1"})
        assert e["event_type"] == "session_end"

    def test_codex_stop_not_interrupt(self):
        e = translate("codex", {"hook_event_name": "Stop", "session_id": "s1"})
        assert e["event_type"] == "stop"
        assert e["is_interrupt"] is False

    def test_codex_eight_types(self):
        for name, expected in [
            ("UserPromptSubmit", "user_prompt"),
            ("PreToolUse", "pre_tool_use"),
            ("PostToolUse", "post_tool_use"),
            ("PermissionRequest", "permission_request"),
            ("SubagentStart", "subagent_start"),
            ("SubagentStop", "subagent_stop"),
        ]:
            e = translate("codex", {"hook_event_name": name, "session_id": "s"})
            assert e["event_type"] == expected, f"{name} mapping failed"

    def test_codex_non_intersection_ignored(self):
        assert translate("codex", {"hook_event_name": "PreCompact"}) is None
        assert translate("codex", {"hook_event_name": "UnknownEvent"}) is None

    def test_codex_fallback_session_id_keys(self):
        e = translate("codex", {"event": "UserPromptSubmit", "conversation_id": "c9"})
        assert e["session_id"] == "c9"
        e = translate("codex", {"type": "PostToolUse", "sessionId": "s7"})
        assert e["session_id"] == "s7"

    def test_codex_fallback_event_name_keys(self):
        e = translate("codex", {"event": "PreToolUse", "session_id": "s"})
        assert e["event_type"] == "pre_tool_use"
        e = translate("codex", {"type": "PostToolUse", "session_id": "s"})
        assert e["event_type"] == "post_tool_use"

    def test_claude_stop_interrupt(self):
        e = translate("claude", {
            "hook_event_name": "Stop",
            "session_id": "s",
            "stop_reason": "interrupt",
        })
        assert e["event_type"] == "stop"
        assert e["is_interrupt"] is True

    def test_claude_stop_not_interrupt(self):
        e = translate("claude", {
            "hook_event_name": "Stop",
            "session_id": "s",
            "stop_reason": "user_stopped",
        })
        assert e["is_interrupt"] is False

    def test_claude_empty_reason_interrupt(self):
        e = translate("claude", {
            "hook_event_name": "Stop",
            "session_id": "s",
        })
        assert e["is_interrupt"] is True  # interrupt_on_empty=True

    def test_codex_empty_reason_not_interrupt(self):
        e = translate("codex", {"hook_event_name": "Stop", "session_id": "s"})
        assert e["is_interrupt"] is False  # interrupt_on_empty=False

    def test_no_event_name_returns_none(self):
        assert translate("codex", {"session_id": "s"}) is None

    def test_payload_excludes_name_fields(self):
        e = translate("codex", {
            "hook_event_name": "SessionStart",
            "session_id": "s1",
            "conversation_id": "c1",
            "sessionId": "s1-alt",
            "event": "SessionStart",
            "type": "SessionStart",
            "cwd": "/tmp",
        })
        assert "hook_event_name" not in e["payload"]
        assert "session_id" not in e["payload"]
        assert "conversation_id" not in e["payload"]
        assert "sessionId" not in e["payload"]
        assert "event" not in e["payload"]
        assert "type" not in e["payload"]
        assert e["payload"] == {"cwd": "/tmp"}

    def test_dsh_translate(self):
        e = translate("dsh", {
            "hook_event_name": "SessionStart",
            "session_id": "dsh-s1",
        })
        assert e["event_type"] == "session_start"
        assert e["session_id"] == "dsh-s1"

    def test_kimi_translate_and_payload(self):
        e = translate("kimi", {
            "hook_event_name": "SessionEnd",
            "sessionId": "kimi-s1",
            "mode": "chat",
        })
        assert e["event_type"] == "session_end"
        assert e["session_id"] == "kimi-s1"
        assert e["payload"] == {"mode": "chat"}

    def test_atomcode_session_uuid_fallback(self):
        e = translate("atomcode", {
            "event": "PreToolUse",
            "sessionUuid": "atom-s1",
            "tool": "Read",
        })
        assert e["event_type"] == "pre_tool_use"
        assert e["session_id"] == "atom-s1"
        assert e["payload"] == {"tool": "Read"}

    def test_cline_task_complete_maps_session_end(self):
        e = translate("cline", {
            "type": "TaskComplete",
            "task_id": "cline-s1",
            "status": "completed",
        })
        assert e["event_type"] == "session_end"
        assert e["session_id"] == "cline-s1"
        assert e["payload"] == {"status": "completed"}


# ---------------------------------------------------------------------------
# 完成判定
# ---------------------------------------------------------------------------

class TestCompletion:
    def test_codex_stop_is_completion(self):
        tpl = get_template("codex")
        assert is_completion_event(tpl, "stop") is True
        assert is_completion_event(tpl, "session_end") is True

    def test_codex_other_not_completion(self):
        tpl = get_template("codex")
        assert is_completion_event(tpl, "session_start") is False
        assert is_completion_event(tpl, "pre_tool_use") is False

    def test_dsh_stop_is_completion(self):
        tpl = get_template("dsh")
        assert is_completion_event(tpl, "stop") is True

    def test_claude_stop_not_completion(self):
        tpl = get_template("claude")
        assert is_completion_event(tpl, "stop") is False
        assert is_completion_event(tpl, "session_end") is True

    def test_kimi_stop_is_completion(self):
        tpl = get_template("kimi")
        assert is_completion_event(tpl, "stop") is True
        assert is_completion_event(tpl, "session_end") is True

    def test_atomcode_stop_not_completion(self):
        tpl = get_template("atomcode")
        assert is_completion_event(tpl, "stop") is False
        assert is_completion_event(tpl, "session_end") is True

    def test_cline_task_complete_is_completion(self):
        tpl = get_template("cline")
        assert is_completion_event(tpl, "session_end") is True
        assert is_completion_event(tpl, "stop") is False


# ---------------------------------------------------------------------------
# 模板字段完整性
# ---------------------------------------------------------------------------

class TestTemplateFields:
    def test_codex_has_thinking_map(self):
        tpl = get_template("codex")
        assert tpl.thinking_level_map is not None
        assert "low" in tpl.thinking_level_map
        assert tpl.thinking_level_map["low"]["config_key"] == "model_reasoning_effort"

    def test_dsh_has_thinking_map(self):
        tpl = get_template("dsh")
        assert tpl.thinking_level_map is not None
        assert tpl.thinking_level_map["medium"]["param"] == "--patch"

    def test_claude_no_thinking_map(self):
        tpl = get_template("claude")
        assert tpl.thinking_level_map is None

    def test_codex_transcript_config(self):
        tpl = get_template("codex")
        assert tpl.transcript["path"] == "codex"
        assert "rollout-{session_id}" in tpl.transcript["glob"]
        assert tpl.transcript["session_id_is_filename"] is True

    def test_dsh_sandbox_allowlist(self):
        tpl = get_template("dsh")
        assert "%TIANJI_HOME%" in tpl.sandbox_allowlist

    def test_dsh_transcript_config(self):
        tpl = get_template("dsh")
        assert tpl.transcript["path"] == "dsh"
        assert "session.jsonl" in tpl.transcript["glob"]

    def test_kimi_template_fields(self):
        tpl = get_template("kimi")
        assert tpl.transcript["path"] == "kimi"
        assert tpl.transcript["authoritative_source"] == "wire"
        assert tpl.permission_slot["type"] == "rule_table"

    def test_atomcode_template_fields(self):
        tpl = get_template("atomcode")
        assert tpl.transcript["path"] == "atomcode"
        assert "{uuid}.jsonl" in tpl.transcript["glob"]

    def test_cline_template_fields(self):
        tpl = get_template("cline")
        assert tpl.transcript["path"] == "cline"
        assert tpl.transcript["source_type"] == "sqlite"
        assert tpl.permission_slot["type"] == "auto_approve"


class TestResumeCommand:
    """7.5 续推通道: 壳模板续跑翻译(成功翻译/不支持 fail-loud)。"""

    def test_resume_field_on_supported_shells(self):
        """三壳模板定义了 resume 续跑原语,其余壳无。"""
        assert get_template("claude").resume is not None
        assert get_template("dsh").resume is not None
        assert get_template("atomcode").resume is not None
        assert get_template("codex").resume is None
        assert get_template("kimi").resume is None
        assert get_template("cline").resume is None

    def test_claude_resume_command(self):
        """claude 续跑原语: -c 续最近会话 + -p 无头打印(实证)。"""
        from tianji.adapters.template import resume_command
        r = resume_command("claude", task_path="D:/t/task.md")
        assert r["supported"] is True
        assert "claude -c -p" in r["cmd"]
        assert "D:/t/task.md" in r["prompt"]

    def test_dsh_resume_command_uses_session_id(self):
        """dsh 续跑原语: --resume <session_id> 续同会话(实证)。"""
        from tianji.adapters.template import resume_command
        r = resume_command("dsh", task_path="D:/t/task.md", session_id="sess-9")
        assert r["supported"] is True
        assert "--resume sess-9" in r["cmd"]

    def test_atomcode_resume_command(self):
        """atomcode 续跑原语: -c 续上一会话 + -p 无头 prompt(实证)。"""
        from tianji.adapters.template import resume_command
        r = resume_command("atomcode", task_path="D:/t/task.md")
        assert r["supported"] is True
        assert "atomcode -c -p" in r["cmd"]

    def test_unsupported_shell_fail_loud(self):
        """不支持续跑的壳 fail-loud: supported=False 带原因(退回人工)。"""
        from tianji.adapters.template import resume_command
        for shell in ("codex", "kimi", "cline"):
            r = resume_command(shell, task_path="D:/t/task.md")
            assert r["supported"] is False
            assert "退回人工" in r["reason"]


# ---------------------------------------------------------------------------
# 装钩子自测: 模拟事件走完整链路(translate → ingest-event → 查账本)
# ---------------------------------------------------------------------------

def _env(worker, secret):
    return {**os.environ, "TIANJI_WORKER_ID": worker,
            "TIANJI_SECRET": secret}


class TestHookE2e:
    """模拟钩子载荷走完整链路: translate → ingest-event → 查账本验证。"""

    def test_codex_session_start_writes_to_ledger(self, conn):
        ctrl = conn.execute(
            "SELECT name FROM instances WHERE name='总控'").fetchone()
        secret = "test-secret-session-start"
        if ctrl is None:
            ops.instance_register(conn, "总控", "claude", "deepseek-v4-flash",
                                  controller=True)
        env = _env("总控", secret)
        # 模拟 codex 钩子载荷
        hook = {
            "hook_event_name": "SessionStart",
            "session_id": "codex-sess-001",
            "cwd": "/project",
        }
        event = translate("codex", hook)
        assert event is not None
        result = ingest_event(conn, env, event)
        # 验证事件写入账本
        row = conn.execute(
            "SELECT * FROM messages WHERE seq=?", (result["seq"],)).fetchone()
        assert row is not None
        import json as _json
        payload = _json.loads(row["payload"])
        assert payload["event_type"] == "session_start"
        assert payload["session_id"] == "codex-sess-001"
        # 验证派生状态
        st = conn.execute(
            "SELECT state FROM session_states WHERE session_id='codex-sess-001'"
        ).fetchone()
        assert st is not None
        assert st["state"] == "idle"

    def test_codex_pre_tool_use_derives_working(self, conn):
        env = _env("总控", "test-secret-working")
        ingest_event(conn, env, {
            "session_id": "codex-sess-002",
            "event_type": "session_start",
        })
        ingest_event(conn, env, {
            "session_id": "codex-sess-002",
            "event_type": "pre_tool_use",
            "payload": {"tool_name": "Read"},
        })
        st = conn.execute(
            "SELECT state FROM session_states WHERE session_id='codex-sess-002'"
        ).fetchone()
        assert st["state"] == "working"

    def test_codex_stop_derives_waiting(self, conn):
        env = _env("总控", "test-secret-waiting")
        ingest_event(conn, env, {
            "session_id": "codex-sess-003",
            "event_type": "session_start",
        })
        ingest_event(conn, env, {
            "session_id": "codex-sess-003",
            "event_type": "stop",
            "is_interrupt": False,
        })
        st = conn.execute(
            "SELECT state FROM session_states WHERE session_id='codex-sess-003'"
        ).fetchone()
        assert st["state"] == "waiting"

    def test_bad_payload_does_not_crash(self, conn):
        """钩子失败放行: 非 JSON 载荷不抛异常。"""
        from tianji.adapters.runner import main as runner_main
        import io
        import sys

        old_stdin = sys.stdin
        sys.stdin = io.StringIO("not json\n")
        try:
            # runner main 调用 translate → None → return 0
            rc = runner_main("codex")
            assert rc == 0
        finally:
            sys.stdin = old_stdin

    def test_claude_hook_e2e(self, conn):
        """claude 模板: 完整链路 translate → ingest-event。"""
        env = _env("总控", "test-secret-claude")
        event = translate("claude", {
            "hook_event_name": "PreToolUse",
            "session_id": "cl-s-1",
            "tool_name": "Edit",
        })
        assert event is not None
        assert event["event_type"] == "pre_tool_use"
        result = ingest_event(conn, env, event)
        row = conn.execute(
            "SELECT * FROM messages WHERE seq=?", (result["seq"],)).fetchone()
        assert row is not None
        import json as _json
        payload = _json.loads(row["payload"])
        assert payload["event_type"] == "pre_tool_use"
        assert payload["payload"]["tool_name"] == "Edit"
