"""档 3 codex exec 进程兜底(6.1/7.4③): codex_exec_alive + iter_codex_exec_events。

验收标准:
1. codex_exec_alive 对无效 PID 返回 False。
2. iter_codex_exec_events 读取 output.jsonl 并翻译为统一事件。
3. 进程存活检查不抛异常(fail-open)。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tianji.adapters.codex_exec import codex_exec_alive, iter_codex_exec_events


# ---------------------------------------------------------------------------
# codex_exec_alive
# ---------------------------------------------------------------------------

class TestCodexExecAlive:
    def test_zero_pid_returns_false(self):
        assert codex_exec_alive(0) is False

    def test_nonexistent_pid_returns_false(self):
        # Windows: PID 4 是系统进程,但非 codex exec
        assert codex_exec_alive(99999) is False

    def test_no_exception_on_error(self):
        # 不应抛异常(fail-open)
        assert codex_exec_alive(-1) is False


# ---------------------------------------------------------------------------
# iter_codex_exec_events
# ---------------------------------------------------------------------------

class TestIterCodexExecEvents:
    def _write_output(self, tmp_path, session_id, lines):
        """Write output.jsonl under tmp_path as CODEX_HOME."""
        d = tmp_path / ".codex" / "sessions" / session_id
        d.mkdir(parents=True, exist_ok=True)
        p = d / "output.jsonl"
        p.write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n",
            encoding="utf-8",
        )
        return p

    def test_no_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        events = list(iter_codex_exec_events("no-such-session", home_dir=tmp_path))
        assert events == []

    def test_translates_codex_events(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        self._write_output(tmp_path, "s1", [
            {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/x"},
            {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Read"},
            {"hook_event_name": "Stop", "session_id": "s1"},
        ])
        events = list(iter_codex_exec_events("s1", home_dir=tmp_path))
        assert len(events) == 3
        assert events[0]["event_type"] == "session_start"
        assert events[1]["event_type"] == "pre_tool_use"
        assert events[2]["event_type"] == "stop"

    def test_skips_non_intersection_events(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        self._write_output(tmp_path, "s2", [
            {"hook_event_name": "PreCompact", "session_id": "s2"},
            {"hook_event_name": "SessionStart", "session_id": "s2"},
        ])
        events = list(iter_codex_exec_events("s2", home_dir=tmp_path))
        assert len(events) == 1
        assert events[0]["event_type"] == "session_start"

    def test_skips_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        d = tmp_path / ".codex" / "sessions" / "s3"
        d.mkdir(parents=True, exist_ok=True)
        # 直接写原始内容: 第一行非法 JSON, 第二行合法
        (d / "output.jsonl").write_text(
            "not json\n"
            + json.dumps({"hook_event_name": "SessionStart", "session_id": "s3"})
            + "\n",
            encoding="utf-8",
        )
        events = list(iter_codex_exec_events("s3", home_dir=tmp_path))
        assert len(events) == 1

    def test_respects_max_lines(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        self._write_output(tmp_path, "s4", [
            {"hook_event_name": "SessionStart", "session_id": "s4"},
        ] * 10)
        events = list(iter_codex_exec_events("s4", home_dir=tmp_path, max_lines=5))
        assert len(events) == 5

    def test_empty_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        d = tmp_path / ".codex" / "sessions" / "s5"
        d.mkdir(parents=True, exist_ok=True)
        (d / "output.jsonl").write_text("\n", encoding="utf-8")
        events = list(iter_codex_exec_events("s5", home_dir=tmp_path))
        assert len(events) == 0
