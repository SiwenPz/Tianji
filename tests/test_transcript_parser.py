"""档 2 转录文件解析公共框架(6.3): 增量解析 + 独立游标 + 归一化 + 乱序不覆盖。

验收标准:
1. 转录文件增量解析补事件,每文件独立游标分别推进。
2. 乱序旧事件不覆盖新状态。
3. 解析产物进同一事件表(与档 1 同表不另造)。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from tianji import ops
from tianji.adapters.transcript_parser import (
    parse_incremental,
    parse_transcript,
    transcript_path,
)
from tianji.adapters.template import get_template
from tianji.events import ingest_event


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _env():
    return {**os.environ, "TIANJI_WORKER_ID": "总控", "TIANJI_SECRET": "x"}


def _codex_session_dir(home: Path) -> Path:
    """Create ~/.codex/sessions/<YYYY>/<MM>/<DD>/ directory under home."""
    d = home / ".codex" / "sessions" / "2026" / "08" / "17"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _claude_project_dir(home: Path) -> Path:
    """Create ~/.claude/projects/<proj>/ directory under home."""
    d = home / ".claude" / "projects" / "proj-abc"
    d.mkdir(parents=True, exist_ok=True)
    return d


_UNSET = object()  # 哨兵: 区分"未传"与"显式传 None(无佐证)"


def _write_cline_db(path: Path, session_id: str, status: str, updated_at: str,
                    exit_code: int | None | object = _UNSET,
                    ended_at: str | None | object = _UNSET,
                    pid: int = 999999) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exit_code is _UNSET:
        exit_code = 0 if status != "running" else None
    if ended_at is _UNSET:
        ended_at = updated_at if status != "running" else None
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY, "
            "source TEXT NOT NULL, pid INTEGER NOT NULL, started_at TEXT NOT NULL, "
            "ended_at TEXT, exit_code INTEGER, status TEXT NOT NULL, status_lock INTEGER NOT NULL DEFAULT 0, "
            "interactive INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, cwd TEXT NOT NULL, "
            "workspace_root TEXT NOT NULL, team_name TEXT, enable_tools INTEGER NOT NULL, "
            "enable_spawn INTEGER NOT NULL, enable_teams INTEGER NOT NULL, parent_session_id TEXT, "
            "parent_agent_id TEXT, agent_id TEXT, conversation_id TEXT, is_subagent INTEGER NOT NULL DEFAULT 0, "
            "prompt TEXT, metadata_json TEXT, transcript_path TEXT NOT NULL DEFAULT '', hook_path TEXT NOT NULL, "
            "messages_path TEXT, updated_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT OR REPLACE INTO sessions (session_id, source, pid, started_at, ended_at, exit_code, status, "
            "interactive, provider, model, cwd, workspace_root, enable_tools, enable_spawn, enable_teams, "
            "transcript_path, hook_path, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, "cli", pid, updated_at, ended_at, exit_code, status, 0, "deepseek", "v4", "/repo", "/repo",
                1, 1, 1, "", "hooks.json", updated_at,
            ),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# transcript_path
# ---------------------------------------------------------------------------

class TestTranscriptPath:
    def test_none_for_empty_session_id(self):
        assert transcript_path("codex", "") is None
        assert transcript_path("claude", "") is None
        assert transcript_path("dsh", "") is None

    def test_codex_path_found(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-abc123.jsonl"
        p.write_text("{}", encoding="utf-8")
        found = transcript_path("codex", "abc123", home_dir=home)
        assert found is not None
        assert found.name == "rollout-abc123.jsonl"

    def test_codex_path_not_found(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        assert transcript_path("codex", "nonexistent", home_dir=home) is None

    def test_claude_path(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _claude_project_dir(home)
        (d / "sess-xyz.jsonl").write_text("{}", encoding="utf-8")
        found = transcript_path("claude", "sess-xyz", home_dir=home)
        assert found is not None
        assert found.name == "sess-xyz.jsonl"

    def test_kimi_path_prefers_wire_jsonl(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        kimi_home = home / ".kimi"
        (kimi_home / "wire.jsonl").parent.mkdir(parents=True, exist_ok=True)
        (kimi_home / "wire.jsonl").write_text("{}\n", encoding="utf-8")
        monkeypatch.setenv("KIMI_HOME", str(kimi_home))
        found = transcript_path("kimi", "kimi-s1", home_dir=home)
        assert found == kimi_home / "wire.jsonl"

    def test_atomcode_path_uses_session_uuid(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        atom_home = home / ".atomcode"
        p = atom_home / "sessions" / "ab12cd" / "atom-s1.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n", encoding="utf-8")
        monkeypatch.setenv("ATOMCODE_HOME", str(atom_home))
        found = transcript_path("atomcode", "atom-s1", home_dir=home)
        assert found == p

    def test_cline_path_supports_data_db_layout(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        cline_home = home / ".cline"
        p = cline_home / "data" / "db" / "sessions.db"
        _write_cline_db(p, "cline-s1", "running", "2026-08-19T10:00:00Z")
        monkeypatch.setenv("CLINE_HOME", str(cline_home))
        found = transcript_path("cline", "cline-s1", home_dir=home)
        assert found == p


# ---------------------------------------------------------------------------
# 增量解析: codex
# ---------------------------------------------------------------------------

class TestParseIncrementalCodex:
    """codex 转录: rollout-*.jsonl,每文件独立游标。"""

    def test_no_file_returns_zero(self, conn, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        result = parse_incremental(conn, _env(), "codex", "no-such-session",
                                   home_dir=home)
        assert result["events_emitted"] == 0
        assert result["files_processed"] == 0

    def test_first_parse_reads_all(self, conn, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-s1.jsonl"
        p.write_text(json.dumps({"hook_event_name": "SessionStart",
                                 "session_id": "s1", "cwd": "/x"}) + "\n",
                     encoding="utf-8")

        result = parse_incremental(conn, _env(), "codex", "s1", home_dir=home)
        assert result["events_emitted"] == 1
        assert result["files_processed"] == 1

        # 验证事件进账本
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='s1'"
        ).fetchone()
        assert row is not None
        assert row["state"] == "idle"

    def test_incremental_only_new_lines(self, conn, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-inc-s.jsonl"

        # 第一批: 2 行
        p.write_text(
            json.dumps({"hook_event_name": "SessionStart",
                        "session_id": "inc-s", "cwd": "/x"}) + "\n"
            + json.dumps({"hook_event_name": "PreToolUse",
                          "session_id": "inc-s", "tool": "Read"}) + "\n",
            encoding="utf-8",
        )
        r1 = parse_incremental(conn, _env(), "codex", "inc-s", home_dir=home)
        assert r1["events_emitted"] == 2

        # 追加 1 行
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"hook_event_name": "Stop",
                                "session_id": "inc-s"}) + "\n")

        r2 = parse_incremental(conn, _env(), "codex", "inc-s", home_dir=home)
        assert r2["events_emitted"] == 1  # 只处理新增行
        assert r2["new_bytes"] > 0

        # 派生状态: stop → waiting (codex stop_is_completion 为 True,
        # 但 is_interrupt=False → waiting)
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='inc-s'"
        ).fetchone()
        assert row["state"] == "waiting"

    def test_skip_processed_lines(self, conn, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-skip.jsonl"
        p.write_text(
            json.dumps({"hook_event_name": "SessionStart",
                        "session_id": "skip-s"}) + "\n",
            encoding="utf-8",
        )
        parse_incremental(conn, _env(), "codex", "skip-s", home_dir=home)
        # 不追加新内容
        r = parse_incremental(conn, _env(), "codex", "skip-s", home_dir=home)
        assert r["events_emitted"] == 0
        assert r["new_bytes"] == 0

    def test_non_intersection_event_skipped(self, conn, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-ni-s.jsonl"
        p.write_text(
            json.dumps({"hook_event_name": "PreCompact",
                        "session_id": "ni-s"}) + "\n"
            + json.dumps({"hook_event_name": "SessionStart",
                          "session_id": "ni-s"}) + "\n",
            encoding="utf-8",
        )
        r = parse_incremental(conn, _env(), "codex", "ni-s", home_dir=home)
        assert r["events_emitted"] == 1  # 只 SessionStart

    def test_invalid_json_skipped(self, conn, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-bad.jsonl"
        p.write_text("not json\nvalid json\n", encoding="utf-8")
        r = parse_incremental(conn, _env(), "codex", "bad-s", home_dir=home)
        assert r["events_emitted"] == 0

    def test_out_of_order_does_not_override(self, conn, tmp_path):
        """乱序旧事件不覆盖(6.3): ingest_event 内部按 seq 单调保证。"""
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-ooo-s.jsonl"
        # 先写 session_start + session_end
        p.write_text(
            json.dumps({"hook_event_name": "SessionStart",
                        "session_id": "ooo-s"}) + "\n"
            + json.dumps({"hook_event_name": "SessionEnd",
                          "session_id": "ooo-s"}) + "\n",
            encoding="utf-8",
        )
        parse_incremental(conn, _env(), "codex", "ooo-s", home_dir=home)
        # 人为抬高 last_seq
        conn.execute(
            "UPDATE session_states SET last_seq=999999 WHERE session_id='ooo-s'")
        # 追加一个旧 seq 的事件
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"hook_event_name": "user_prompt",
                                "session_id": "ooo-s"}) + "\n")
        parse_incremental(conn, _env(), "codex", "ooo-s", home_dir=home)
        # 状态仍为 done(session_end),旧事件不覆盖
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='ooo-s'"
        ).fetchone()
        assert row["state"] == "done"

    def test_multiple_events_full_states(self, conn, tmp_path):
        """完整四态流转: idle → working → waiting → done。"""
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-st-s.jsonl"
        events = [
            {"hook_event_name": "SessionStart", "session_id": "st-s"},
            {"hook_event_name": "PreToolUse", "session_id": "st-s"},
            {"hook_event_name": "Stop", "session_id": "st-s"},
            {"hook_event_name": "SessionEnd", "session_id": "st-s"},
        ]
        p.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                     encoding="utf-8")
        parse_incremental(conn, _env(), "codex", "st-s", home_dir=home)
        st = lambda: conn.execute(
            "SELECT state FROM session_states WHERE session_id='st-s'"
        ).fetchone()["state"]
        assert st() == "done"  # 最终态

    def test_session_start_backfills_registration(self, conn, tmp_path):
        """session_start 回填登记行 active(与钩子 ingest-event 同语义)。"""
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-reg-s.jsonl"
        p.write_text(json.dumps({"hook_event_name": "SessionStart",
                                 "session_id": "reg-s"}) + "\n",
                     encoding="utf-8")
        parse_incremental(conn, _env(), "codex", "reg-s", home_dir=home)
        row = conn.execute(
            "SELECT status FROM instance_registrations"
            " WHERE instance_name='总控' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["status"] == "active"
class TestParseIncrementalOtherShells:
    def test_kimi_reads_wire_jsonl(self, conn, tmp_path, monkeypatch):
        home = tmp_path / "home"
        kimi_home = home / ".kimi"
        p = kimi_home / "wire.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"hook_event_name": "SessionEnd", "session_id": "kimi-s1"}) + "\n",
                     encoding="utf-8")
        monkeypatch.setenv("KIMI_HOME", str(kimi_home))
        result = parse_incremental(conn, _env(), "kimi", "kimi-s1", home_dir=home)
        assert result["events_emitted"] == 1
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='kimi-s1'"
        ).fetchone()
        assert row["state"] == "done"

    def test_kimi_wire_corrects_false_done(self, conn, tmp_path, monkeypatch):
        """权威校验: wire 显示运行中,账本误判 done → 以 wire 为准纠偏回 working。"""
        home = tmp_path / "home"
        kimi_home = home / ".kimi"
        p = kimi_home / "wire.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"hook_event_name": "UserPromptSubmit",
                                 "session_id": "kimi-c1"}) + "\n",
                     encoding="utf-8")
        monkeypatch.setenv("KIMI_HOME", str(kimi_home))
        r0 = parse_incremental(conn, _env(), "kimi", "kimi-c1", home_dir=home)
        assert r0["events_emitted"] == 1  # wire 行已消费,游标推进

        # 档 1 误报完成(hook 迟到事件把账本覆盖成 done)
        ingest_event(conn, _env(), {"session_id": "kimi-c1",
                                    "event_type": "session_end",
                                    "payload": {"wrong": True}})
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='kimi-c1'"
        ).fetchone()
        assert row["state"] == "done"

        # wire 权威校验: 最新行仍是运行中 → 纠偏
        r1 = parse_incremental(conn, _env(), "kimi", "kimi-c1", home_dir=home)
        assert r1["events_emitted"] == 1
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='kimi-c1'"
        ).fetchone()
        assert row["state"] == "working"

        # 幂等: 已一致,不再重复写
        r2 = parse_incremental(conn, _env(), "kimi", "kimi-c1", home_dir=home)
        assert r2["events_emitted"] == 0

    def test_kimi_wire_backfills_missing_completion(self, conn, tmp_path, monkeypatch):
        """权威校验: 档 1 缺口(账本被覆盖成 working)而 wire 权威完成 → 补正 done。"""
        home = tmp_path / "home"
        kimi_home = home / ".kimi"
        p = kimi_home / "wire.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"hook_event_name": "SessionEnd",
                                 "session_id": "kimi-b1"}) + "\n",
                     encoding="utf-8")
        monkeypatch.setenv("KIMI_HOME", str(kimi_home))
        r0 = parse_incremental(conn, _env(), "kimi", "kimi-b1", home_dir=home)
        assert r0["events_emitted"] == 1

        # 档 1 缺口: 迟到的运行中事件把账本覆盖成 working,wire 权威仍为完成
        ingest_event(conn, _env(), {"session_id": "kimi-b1",
                                    "event_type": "user_prompt",
                                    "payload": {"late": True}})
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='kimi-b1'"
        ).fetchone()
        assert row["state"] == "working"

        # wire 权威校验: 补正完成
        r1 = parse_incremental(conn, _env(), "kimi", "kimi-b1", home_dir=home)
        assert r1["events_emitted"] == 1
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='kimi-b1'"
        ).fetchone()
        assert row["state"] == "done"

        # 幂等
        r2 = parse_incremental(conn, _env(), "kimi", "kimi-b1", home_dir=home)
        assert r2["events_emitted"] == 0

    def test_atomcode_incremental_reads_only_new_lines(self, conn, tmp_path, monkeypatch):
        home = tmp_path / "home"
        atom_home = home / ".atomcode"
        p = atom_home / "sessions" / "f0f0" / "atom-s1.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"hook_event_name": "SessionStart", "sessionUuid": "atom-s1"}) + "\n",
                     encoding="utf-8")
        monkeypatch.setenv("ATOMCODE_HOME", str(atom_home))
        r1 = parse_incremental(conn, _env(), "atomcode", "atom-s1", home_dir=home)
        assert r1["events_emitted"] == 1
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"hook_event_name": "PreToolUse", "sessionUuid": "atom-s1"}) + "\n")
        r2 = parse_incremental(conn, _env(), "atomcode", "atom-s1", home_dir=home)
        assert r2["events_emitted"] == 1
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='atom-s1'"
        ).fetchone()
        assert row["state"] == "working"

    def test_cline_reads_sessions_db_status(self, conn, tmp_path, monkeypatch):
        home = tmp_path / "home"
        cline_home = home / ".cline"
        p = cline_home / "data" / "db" / "sessions.db"
        _write_cline_db(p, "cline-s1", "running", "2026-08-19T10:00:00Z")
        monkeypatch.setenv("CLINE_HOME", str(cline_home))
        r1 = parse_incremental(conn, _env(), "cline", "cline-s1", home_dir=home)
        assert r1["events_emitted"] == 1
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='cline-s1'"
        ).fetchone()
        assert row["state"] == "idle"

        _write_cline_db(p, "cline-s1", "completed", "2026-08-19T10:05:00Z")
        r2 = parse_incremental(conn, _env(), "cline", "cline-s1", home_dir=home)
        assert r2["events_emitted"] == 1
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='cline-s1'"
        ).fetchone()
        assert row["state"] == "done"
        # 完成判定主判据证据落库: status/exit_code/ended_at/pid 全参与并留痕
        msg = conn.execute(
            "SELECT payload FROM messages WHERE type='event'"
            " AND sender='总控' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(msg["payload"])
        assert payload["event_type"] == "session_end"
        assert payload["payload"]["db_status"] == "completed"
        assert payload["payload"]["db_exit_code"] == 0
        assert payload["payload"]["db_ended_at"] == "2026-08-19T10:05:00Z"
        assert payload["payload"]["db_pid"] == 999999

    def test_cline_intermediate_status_not_complete(self, conn, tmp_path, monkeypatch):
        """非 running 不能一律当完成: 中间态(queued)不判完成,进终态才判。"""
        home = tmp_path / "home"
        cline_home = home / ".cline"
        p = cline_home / "data" / "db" / "sessions.db"
        _write_cline_db(p, "cline-m1", "queued", "2026-08-19T10:00:00Z")
        monkeypatch.setenv("CLINE_HOME", str(cline_home))
        r1 = parse_incremental(conn, _env(), "cline", "cline-m1", home_dir=home)
        assert r1["events_emitted"] == 0
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='cline-m1'"
        ).fetchone()
        assert row is None  # 从未判定完成

        _write_cline_db(p, "cline-m1", "completed", "2026-08-19T10:05:00Z")
        r2 = parse_incremental(conn, _env(), "cline", "cline-m1", home_dir=home)
        assert r2["events_emitted"] == 1
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='cline-m1'"
        ).fetchone()
        assert row["state"] == "done"

    def test_cline_final_without_evidence_not_complete(self, conn, tmp_path, monkeypatch):
        """终态但缺 exit_code/ended_at 佐证 → 不判完成;补佐证后判完成。"""
        home = tmp_path / "home"
        cline_home = home / ".cline"
        p = cline_home / "data" / "db" / "sessions.db"
        _write_cline_db(p, "cline-e1", "completed", "2026-08-19T10:00:00Z",
                        exit_code=None, ended_at=None)
        monkeypatch.setenv("CLINE_HOME", str(cline_home))
        r1 = parse_incremental(conn, _env(), "cline", "cline-e1", home_dir=home)
        assert r1["events_emitted"] == 0

        _write_cline_db(p, "cline-e1", "completed", "2026-08-19T10:05:00Z",
                        exit_code=1, ended_at="2026-08-19T10:05:00Z")
        r2 = parse_incremental(conn, _env(), "cline", "cline-e1", home_dir=home)
        assert r2["events_emitted"] == 1
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='cline-e1'"
        ).fetchone()
        assert row["state"] == "done"

    def test_cline_deferred_until_process_exit(self, conn, tmp_path, monkeypatch):
        """进程未退时完成判定降级(deferred);进程退出后复查补判完成。"""
        home = tmp_path / "home"
        cline_home = home / ".cline"
        p = cline_home / "data" / "db" / "sessions.db"
        _write_cline_db(p, "cline-d1", "completed", "2026-08-19T10:00:00Z",
                        pid=os.getpid())  # 存活进程
        monkeypatch.setenv("CLINE_HOME", str(cline_home))
        r1 = parse_incremental(conn, _env(), "cline", "cline-d1", home_dir=home)
        assert r1["events_emitted"] == 0

        # 进程退出(pid 换成不存在的),updated_at 不变 → deferred 复查补判
        _write_cline_db(p, "cline-d1", "completed", "2026-08-19T10:00:00Z",
                        pid=999999)
        r2 = parse_incremental(conn, _env(), "cline", "cline-d1", home_dir=home)
        assert r2["events_emitted"] == 1
        row = conn.execute(
            "SELECT state FROM session_states WHERE session_id='cline-d1'"
        ).fetchone()
        assert row["state"] == "done"

    def test_cline_skip_same_updated_at(self, conn, tmp_path, monkeypatch):
        home = tmp_path / "home"
        cline_home = home / ".cline"
        p = cline_home / "data" / "db" / "sessions.db"
        _write_cline_db(p, "cline-s2", "running", "2026-08-19T10:00:00Z")
        monkeypatch.setenv("CLINE_HOME", str(cline_home))
        parse_incremental(conn, _env(), "cline", "cline-s2", home_dir=home)
        r2 = parse_incremental(conn, _env(), "cline", "cline-s2", home_dir=home)
        assert r2["events_emitted"] == 0


# ---------------------------------------------------------------------------
# 事件表合一: 档 2 与档 1 同一 messages 表
# ---------------------------------------------------------------------------

class TestUnifiedEventTable:
    def test_codex_events_in_messages_table(self, conn, tmp_path):
        """档 2 解析产物与档 1 事件同表,type='event'。"""
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-ut-s.jsonl"
        p.write_text(
            json.dumps({"hook_event_name": "SessionStart",
                        "session_id": "ut-s", "cwd": "/p"}) + "\n"
            + json.dumps({"hook_event_name": "UserPromptSubmit",
                          "session_id": "ut-s", "prompt": "hi"}) + "\n",
            encoding="utf-8",
        )
        parse_incremental(conn, _env(), "codex", "ut-s", home_dir=home)
        rows = conn.execute(
            "SELECT * FROM messages WHERE type='event' ORDER BY seq"
        ).fetchall()
        assert len(rows) == 2
        import json as _json
        p0 = _json.loads(rows[0]["payload"])
        assert p0["event_type"] == "session_start"
        p1 = _json.loads(rows[1]["payload"])
        assert p1["event_type"] == "user_prompt"
        assert p1["payload"]["prompt"] == "hi"


# ---------------------------------------------------------------------------
# parse_transcript 别名
# ---------------------------------------------------------------------------

class TestParseTranscriptAlias:
    def test_alias_calls_parse_incremental(self, conn, tmp_path):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        d = _codex_session_dir(home)
        p = d / "rollout-alias-s.jsonl"
        p.write_text(json.dumps({"hook_event_name": "SessionStart",
                                 "session_id": "alias-s"}) + "\n",
                     encoding="utf-8")
        result = parse_transcript(conn, _env(), "codex", "alias-s", home_dir=home)
        assert result["events_emitted"] == 1
