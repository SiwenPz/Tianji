"""Task-10: zstd 读取器懒加载(有库解析/无库降级) + dsh thinking patch 接线。"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import zstandard as zstd  # dev extra 已登记(pyproject.toml)

from tianji.adapters.transcript_parser import (
    _read_new_lines, _process_file, _cursor_key,
)
from tianji.shellrender import _render_codex, _render_config_binding, render
from tianji.db import connect


def _zstd_file(path: Path, text: str) -> Path:
    """现场压缩生成最小 zstd 样本(不依赖外部夹具)。"""
    path.write_bytes(zstd.ZstdCompressor().compress(text.encode("utf-8")))
    return path


class _LedgerCase:
    """TIANJI_HOME 隔离 + 账本连接的公共 setup。"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        home = Path(self.tmp) / "tianji"
        home.mkdir(parents=True, exist_ok=True)
        self._old_home = os.environ.get("TIANJI_HOME")
        os.environ["TIANJI_HOME"] = str(home)
        self.conn = connect()
        from tianji import ops
        ops.ensure_defaults(self.conn)

    def teardown_method(self):
        self.conn.close()
        if self._old_home:
            os.environ["TIANJI_HOME"] = self._old_home
        else:
            os.environ.pop("TIANJI_HOME", None)

    def _register(self, name="总控", thinking_level=""):
        from tianji import ops
        reg = ops.instance_register(
            self.conn, name, "dsh", "dsh-model",
            isolated_dir=str(Path(self.tmp) / f"iso-{name}"),
            controller=True, ident=None, thinking_level=thinking_level)
        return {"TIANJI_WORKER_ID": name, "TIANJI_SECRET": reg["secret"]}


# ---- zstd 懒加载: 有库时解压解析 ----

class TestZstdLazyLoading(_LedgerCase):
    """zstd 压缩转录文件的懒加载与增量处理(zstandard 已在 dev extra)。"""

    def test_read_new_lines_plain_file(self):
        """普通文件: _read_new_lines 从字节偏移读取新行。"""
        f = Path(self.tmp) / "plain.jsonl"
        content = "line1\nline2\nline3\nline4\n"
        f.write_bytes(content.encode("utf-8"))
        # write_bytes 不经过 CRLF 翻译(Windows 安全)
        # "line1\nline2\n" = 12 bytes, offset 12 → line3, line4
        lines = _read_new_lines(f, 12)
        assert lines == ["line3\n", "line4\n"]

    def test_read_new_lines_zstd_returns_decompressed(self):
        """.zstd 文件: _read_new_lines 解压后返回所有行。"""
        f = _zstd_file(Path(self.tmp) / "session.jsonl.zstd",
                       "line1\nline2\nline3\n")
        lines = _read_new_lines(f, 0)
        assert lines == ["line1\n", "line2\n", "line3\n"]

    def test_read_new_lines_zstd_line_count_offset(self):
        """.zstd 文件: offset=1 时跳过首行(行号计数)。"""
        f = _zstd_file(Path(self.tmp) / "session.jsonl.zstd",
                       "line1\nline2\nline3\n")
        lines = _read_new_lines(f, 1)
        assert lines == ["line2\n", "line3\n"]
        lines2 = _read_new_lines(f, 3)
        assert lines2 == []

    def test_read_new_lines_zstd_empty_file(self):
        """空 .zstd 文件: 返回空列表。"""
        f = _zstd_file(Path(self.tmp) / "empty.jsonl.zstd", "")
        lines = _read_new_lines(f, 0)
        assert lines == []

    def test_process_file_zstd_incremental(self):
        """_process_file 对 .zstd 文件增量处理(行号追踪)。"""
        worker_env = self._register()
        from tianji.adapters import template as tpl_mod
        tpl = tpl_mod.get_template("dsh")
        # 写两批内容模拟增量
        batch1 = json.dumps({"event": "UserPromptSubmit", "session_id": "s1",
                              "hook_event_name": "UserPromptSubmit",
                              "payload": {"prompt": "hello"}}) + "\n"
        batch2 = json.dumps({"event": "UserPromptSubmit", "session_id": "s1",
                              "hook_event_name": "UserPromptSubmit",
                              "payload": {"prompt": "world"}}) + "\n"
        # 第一批
        f = _zstd_file(Path(self.tmp) / "session.jsonl.zstd", batch1)
        _process_file(self.conn, worker_env, tpl, f, "s1",
                       {"files_processed": 0, "events_emitted": 0, "new_bytes": 0})
        row = self.conn.execute(
            "SELECT last_seq FROM cursors WHERE consumer_id=?",
            (_cursor_key("dsh", str(f)),)).fetchone()
        assert row is not None
        cur = json.loads(row["last_seq"])
        assert cur["offset"] == 1  # 处理了 1 行
        assert cur.get("compressed_size") > 0  # zstd 追踪压缩大小
        # 第二批: 在已有内容后追加(模拟文件增长)
        _zstd_file(f, batch1 + batch2)
        _process_file(self.conn, worker_env, tpl, f, "s1",
                       {"files_processed": 0, "events_emitted": 0, "new_bytes": 0})
        # 事件通过 messages 表存储(payload 列含 JSON)
        msgs = self.conn.execute(
            "SELECT payload FROM messages WHERE type='event'"
        ).fetchall()
        prompts = []
        for m in msgs:
            try:
                data = json.loads(m["payload"])
                inner = data.get("payload", {})
                if isinstance(inner, dict) and "prompt" in inner.get("payload", {}):
                    prompts.append(inner["payload"]["prompt"])
            except (json.JSONDecodeError, TypeError):
                pass
        assert "hello" in prompts, f"期望 'hello' 在 {prompts}"
        assert "world" in prompts, f"期望 'world' 在 {prompts}"


# ---- zstd 懒加载: 无库时降级(不崩+字节数保活性+如实记录) ----

class TestZstdDegradeWithoutLibrary(_LedgerCase):
    """monkeypatch 模拟无 zstandard 库环境(sys.modules 置 None → ImportError)。"""

    def test_read_new_lines_zstd_no_library_returns_none(self, monkeypatch):
        """无库: _read_new_lines 不崩,返回 None 降级哨兵。"""
        f = _zstd_file(Path(self.tmp) / "session.jsonl.zstd", "line1\n")
        monkeypatch.setitem(sys.modules, "zstandard", None)
        assert _read_new_lines(f, 0) is None

    def test_process_file_zstd_no_library_degrades(self, monkeypatch):
        """无库降级: 不崩、字节数保活性、事件解析跳过、档案如实记且不重刷。"""
        self._register()
        # 档 1 先行: session_states 已有该会话(转录解析反查实例靠它)
        self.conn.execute(
            "INSERT INTO session_states (session_id, instance_name, state,"
            " last_seq, updated_at) VALUES ('s1','总控','working',0,1)")
        self.conn.commit()
        from tianji.adapters import template as tpl_mod
        tpl = tpl_mod.get_template("dsh")
        f = _zstd_file(Path(self.tmp) / "session.jsonl.zstd", "line1\nline2\n")
        size1 = f.stat().st_size

        monkeypatch.setitem(sys.modules, "zstandard", None)
        summary = {"files_processed": 0, "events_emitted": 0, "new_bytes": 0}
        _process_file(self.conn, {"TIANJI_WORKER_ID": "总控"}, tpl, f, "s1",
                      summary)  # 不崩即过一半
        # 字节数保活性: new_bytes 按压缩文件尺寸记账
        assert summary["new_bytes"] == size1
        assert summary["events_emitted"] == 0  # 事件级解析如实跳过
        assert summary["transcript_note"] == "转录压缩未解析(无 zstandard 库)"
        # 游标推进(压缩尺寸),下次文件不变大不重复处理
        cur = json.loads(self.conn.execute(
            "SELECT last_seq FROM cursors WHERE consumer_id=?",
            (_cursor_key("dsh", str(f)),)).fetchone()["last_seq"])
        assert cur["compressed_size"] == size1
        # 实例档案如实记
        notes = self.conn.execute(
            "SELECT notes FROM ability_profiles WHERE instance_name='总控'"
        ).fetchone()["notes"]
        assert "转录压缩未解析" in notes

        # 文件增长后再解析: 活性字节继续记账,档案不重复刷
        _zstd_file(f, "line1\nline2\nline3\n")
        size2 = f.stat().st_size
        summary2 = {"files_processed": 0, "events_emitted": 0, "new_bytes": 0}
        _process_file(self.conn, {"TIANJI_WORKER_ID": "总控"}, tpl, f, "s1",
                      summary2)
        assert summary2["new_bytes"] == size2 - size1
        notes2 = self.conn.execute(
            "SELECT notes FROM ability_profiles WHERE instance_name='总控'"
        ).fetchone()["notes"]
        assert notes2.count("转录压缩未解析") == 1

    def test_process_file_zstd_no_library_no_session_row(self, monkeypatch):
        """无库且账本无会话行: 仍不崩,跳过档案记录(不补建)。"""
        self._register()
        from tianji.adapters import template as tpl_mod
        tpl = tpl_mod.get_template("dsh")
        f = _zstd_file(Path(self.tmp) / "session.jsonl.zstd", "line1\n")
        monkeypatch.setitem(sys.modules, "zstandard", None)
        summary = {"files_processed": 0, "events_emitted": 0, "new_bytes": 0}
        _process_file(self.conn, {"TIANJI_WORKER_ID": "总控"}, tpl, f, "s9",
                      summary)
        assert summary["new_bytes"] == f.stat().st_size
        notes = self.conn.execute(
            "SELECT notes FROM ability_profiles WHERE instance_name='总控'"
        ).fetchone()["notes"]
        assert "转录压缩未解析" not in (notes or "")


# ---- dsh thinking patch 接线(票 26 验收 3) ----

class TestDshThinkingPatch(_LedgerCase):
    """实例设了 thinking_level 且壳模板声明 --patch 机制时,launch_cmd 接 patch。"""

    def test_dsh_launch_cmd_includes_patch_and_file_exists(self):
        """端到端: render._apply_thinking_level 写 patch 文件,shellrender 接进
        launch_cmd,两处路径逐字对齐且文件真实存在。"""
        from tianji import ops
        from tianji.adapters.template import TEMPLATE_DSH
        from tianji.render import _apply_thinking_level
        iso = Path(self.tmp) / "iso-dsh"
        iso.mkdir()
        ops.instance_register(self.conn, "dsh工", "dsh", "dsh-model",
                              isolated_dir=str(iso), thinking_level="高",
                              controller=True, ident=None)
        inst = self.conn.execute(
            "SELECT * FROM instances WHERE name='dsh工'").fetchone()
        r = _apply_thinking_level(self.conn, inst, {})
        assert r["applied"] is True
        cmd, _ = render(self.conn, "dsh", instance="dsh工", model="dsh-model",
                        isolated_dir=str(iso), entry=TEMPLATE_DSH,
                        thinking_level="高")
        assert "--patch" in cmd
        patch = iso / "thinking.patch.yml"
        assert str(patch) in cmd           # 与 render.py 写入路径逐字对齐
        assert patch.is_file()             # 引用的 patch 文件真实存在
        assert 'reasoningEfforts: ["high"]' in patch.read_text(encoding="utf-8")

    def test_config_binding_patch_unit(self):
        """单元: _render_config_binding 按 ctx 拼 --patch <iso>/thinking.patch.yml。"""
        from tianji.adapters.template import TEMPLATE_DSH
        with tempfile.TemporaryDirectory() as iso:
            cmd, _ = _render_config_binding({
                "shell": "dsh", "entry": TEMPLATE_DSH, "key_ref": "",
                "model": "m", "base_url": "", "isolated_dir": iso,
                "thinking_level": "低",
            })
            assert "--patch" in cmd
            assert str(Path(iso) / "thinking.patch.yml") in cmd

    def test_no_thinking_level_no_patch(self):
        """未设 thinking_level: launch_cmd 不接 --patch。"""
        from tianji.adapters.template import TEMPLATE_DSH
        with tempfile.TemporaryDirectory() as iso:
            cmd, _ = _render_config_binding({
                "shell": "dsh", "entry": TEMPLATE_DSH, "key_ref": "",
                "model": "m", "base_url": "", "isolated_dir": iso,
                "thinking_level": "",
            })
            assert "--patch" not in cmd
            assert cmd.startswith("dsh")

    def test_shell_without_thinking_mechanism_no_patch(self):
        """壳模板没声明 thinking 机制(kimi): 设了级别也不接 --patch。"""
        from tianji.adapters.template import TEMPLATE_KIMI
        cmd, _ = _render_config_binding({
            "shell": "kimi", "entry": TEMPLATE_KIMI, "key_ref": "",
            "model": "m", "base_url": "", "isolated_dir": "",
            "thinking_level": "高",
        })
        assert "--patch" not in cmd


# ---- codex thinking 参数化回退的回归守卫 ----

class TestCodexThinkingRevert:
    """codex 思考级别由 render.py config_key 分支处理(13.3),渲染层保持
    硬编码 medium 基线;_render_codex 的参数化半残品已回退。"""

    def test_codex_template_hardcodes_medium(self):
        from tianji.shellrender import _CODEX_CONFIG
        assert 'model_reasoning_effort = "medium"' in _CODEX_CONFIG
        assert "{thinking_effort}" not in _CODEX_CONFIG

    def test_render_codex_ignores_thinking_level(self):
        """首次渲染永远 medium(思考级别注入由 render.py 改写 config.toml)。"""
        with tempfile.TemporaryDirectory() as iso:
            _render_codex({
                "model": "gpt-4", "key_name": "test",
                "base_url": "http://127.0.0.1:8080",
                "key_ref": "unused.key", "isolated_dir": iso,
                "thinking_level": "高",
            })
            text = (Path(iso) / "config.toml").read_text(encoding="utf-8")
            assert 'model_reasoning_effort = "medium"' in text


class TestNonCodexRenderersUnaffected:
    """thinking_level 接线不影响非 dsh 壳(如 claude)。"""

    def test_claude_render_no_thinking(self):
        """claude renderer 不受 thinking_level 参数影响。"""
        from tianji.shellrender import _render_claude
        with tempfile.TemporaryDirectory() as iso:
            cmd, arts = _render_claude({
                "model": "claude-3",
                "key_ref": "",
                "base_url": "http://x",
                "isolated_dir": iso,
                "thinking_level": "高",
                "entry": {},
            })
            # claude 产物是 settings.json, 不含 model_reasoning_effort
            assert "settings.json" in arts[0]
