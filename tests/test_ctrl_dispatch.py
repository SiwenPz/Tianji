"""总控真会话链接测试: webapp._ctrl() 按 settings-protocol 分发后端。

验证:
- claude settings → _ctrl() 返回 ClaudeStreamBackend
- kimi settings  → _ctrl() 返回 ACPBackend
- settings 文件变更(mtime) → 旧进程 close + 重建
- 无 settings 文件 → 回退 BaseBackend(空壳,非 None)
- 切换 claude→kimi → 热生效(新协议启动)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from tianji import ctrlprotocols
from tianji.ctrlprotocols import ACPBackend, BaseBackend, ClaudeStreamBackend, get_backend_class
from tianji.db import injected_dir

# --- fixture: dummy tianji_home with settings ---

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TIANJI_HOME", str(home))
    injected_dir().mkdir(parents=True, exist_ok=True)
    (injected_dir() / "ctrl-secret.txt").write_text("test-secret", encoding="utf-8")
    return home


def _write_claude_settings(home: Path):
    doc = {
        "env": {"TIANJI_HOME": str(home), "TIANJI_WORKER_ID": "总控",
                "TIANJI_SECRET": "test-secret"},
        "appendSystemPrompt": "你是总控",
    }
    (injected_dir() / "settings-controller.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _write_kimi_settings(home: Path):
    doc = {
        "env": {"TIANJI_HOME": str(home), "TIANJI_WORKER_ID": "总控",
                "TIANJI_SECRET": "test-secret"},
        "ctrl_session": {
            "protocol": "acp",
            "launch": ["kimi", "acp"],
            "provider_env": {
                "target": "process_env",
                "map": {
                    "KIMI_MODEL_NAME": "{model}",
                    "KIMI_MODEL_API_KEY": "{key}",
                    "KIMI_MODEL_BASE_URL": "{base_url}",
                    "KIMI_MODEL_PROVIDER_TYPE": "{protocol}",
                },
            },
            "key_ref": "",
            "model": "kimi-test",
            "base_url": "https://api.test",
            "data_root_env": "KIMI_CODE_HOME",
            "role_text": "你是kimi总控",
        },
    }
    (injected_dir() / "settings-controller.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _write_acp_settings(home: Path, shell: str = "fake-acp"):
    """通用 ACP 壳 settings: ctrl_session 块在 Settings 写入。"""
    doc = {
        "env": {"TIANJI_HOME": str(home), "TIANJI_WORKER_ID": "总控",
                "TIANJI_SECRET": "test-secret"},
        "ctrl_session": {
            "protocol": "acp",
            "launch": [shell, "acp"],
            "provider_env": {
                "target": "process_env",
                "map": {"MAP_FOO": "{model}"},
            },
            "key_ref": "",
            "model": "fake-test",
            "base_url": "https://api.test",
            "data_root_env": "FAKE_HOME",
            "role_text": "你是假壳总控",
        },
    }
    (injected_dir() / "settings-controller.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _write_empty_settings(home: Path):
    (injected_dir() / "settings-controller.json").write_text(
        json.dumps({}, ensure_ascii=False), encoding="utf-8")


# =========================================================================
# Tests: BaseBackend.from_config factory
# =========================================================================


class TestBackendFromConfig:
    def test_claude_settings_produces_claude_backend(self, fake_home):
        _write_claude_settings(fake_home)
        settings = injected_dir() / "settings-controller.json"
        b = BaseBackend.from_config(fake_home, settings)
        assert isinstance(b, ClaudeStreamBackend)
        assert b.home == fake_home
        assert b.launch == ["claude"]
        assert b.provider_env == {}

    def test_kimi_settings_produces_acp_backend(self, fake_home):
        _write_kimi_settings(fake_home)
        settings = injected_dir() / "settings-controller.json"
        b = BaseBackend.from_config(fake_home, settings)
        assert isinstance(b, ACPBackend)
        assert b.home == fake_home
        assert b.launch == ["kimi", "acp"]
        assert b.provider_env == {"target": "process_env", "map": {
            "KIMI_MODEL_API_KEY": "{key}",
            "KIMI_MODEL_NAME": "{model}",
            "KIMI_MODEL_BASE_URL": "{base_url}",
            "KIMI_MODEL_PROVIDER_TYPE": "{protocol}",
        }}
        assert b._key_ref == ""
        assert b._model == "kimi-test"
        assert b._base_url == "https://api.test"
        assert b._role_text == "你是kimi总控"
        assert b.data_root_env == "KIMI_CODE_HOME"

    def test_empty_settings_falls_back_to_base(self, fake_home):
        _write_empty_settings(fake_home)
        settings = injected_dir() / "settings-controller.json"
        b = BaseBackend.from_config(fake_home, settings)
        assert type(b) is BaseBackend  # exact BaseBackend, not ClaudeStreamBackend

    def test_missing_settings_file_falls_back(self, fake_home):
        # No settings file at all
        b = BaseBackend.from_config(fake_home, fake_home / "no-such.json")
        assert type(b) is BaseBackend

    def test_unknown_protocol_falls_back(self, fake_home):
        doc = {"ctrl_session": {"protocol": "unknown-protocol"}}
        (injected_dir() / "settings-controller.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        b = BaseBackend.from_config(
            fake_home, injected_dir() / "settings-controller.json")
        assert type(b) is BaseBackend

    def test_claude_no_ctrl_session_block(self, fake_home):
        """claude settings 含 env 但无 ctrl_session 块 → 默认 stream-json。"""
        _write_claude_settings(fake_home)
        settings = injected_dir() / "settings-controller.json"
        b = BaseBackend.from_config(fake_home, settings)
        assert isinstance(b, ClaudeStreamBackend)

    def test_registry_lookup(self):
        assert get_backend_class("stream-json") is ClaudeStreamBackend
        assert get_backend_class("acp") is ACPBackend
        with pytest.raises(KeyError):
            get_backend_class("bogus")


# =========================================================================
# Tests: _ctrl() dispatch in webapp
# =========================================================================


class TestCtrlDispatch:
    def test_claude_dispatch(self, fake_home):
        """配置页选 claude → webapp._ctrl() 返回 ClaudeStreamBackend。"""
        _write_claude_settings(fake_home)
        # reload module so _ctrl picks up new tianji_home
        import tianji.webapp as wa
        import importlib
        importlib.reload(wa)
        # close any leak from other tests
        wa._ctrl_session = None
        wa._ctrl_cfg_mtime = 0.0
        b = wa._ctrl()
        try:
            assert isinstance(b, ClaudeStreamBackend)
        finally:
            if b and b.is_alive():
                b.close()

    def test_kimi_dispatch(self, fake_home):
        """配置页选 kimi → webapp._ctrl() 返回 ACPBackend。"""
        import tianji.webapp as wa
        import importlib
        importlib.reload(wa)
        wa._ctrl_session = None
        wa._ctrl_cfg_mtime = 0.0
        _write_kimi_settings(fake_home)
        b = wa._ctrl()
        try:
            assert isinstance(b, ACPBackend)
        finally:
            if b and b.is_alive():
                b.close()

    def test_config_change_rebuilds(self, fake_home):
        """settings 文件 mtime 变了 → _ctrl 返回新后端,旧进程已 close。"""
        import tianji.webapp as wa
        import importlib
        importlib.reload(wa)
        wa._ctrl_session = None
        wa._ctrl_cfg_mtime = 0.0

        _write_claude_settings(fake_home)
        b1 = wa._ctrl()
        assert isinstance(b1, ClaudeStreamBackend)
        pid1 = b1.proc.pid if b1.proc else None

        # 写出写 time.sleep(0.1)
        time.sleep(0.1)
        _write_kimi_settings(fake_home)
        b2 = wa._ctrl()
        assert isinstance(b2, ACPBackend)

        # ★ 旧进程已被 close
        assert b1.proc is None
        if pid1:
            assert wa._ctrl_session is b2
        if b2 and b2.is_alive():
            b2.close()

    def test_api_ctrl_send_accepts(self, fake_home, monkeypatch):
        """POST /api/ctrl/send 返回 accepted(dispatch 链路端到端验证)。"""
        import tianji.webapp as wa
        import importlib
        importlib.reload(wa)
        wa._ctrl_session = None
        wa._ctrl_cfg_mtime = 0.0

        # 注入总控身份
        monkeypatch.setenv("TIANJI_WORKER_ID", "admin")
        monkeypatch.setenv("TIANJI_SECRET", "admin-secret")

        # 注入身份检查: 伪造 auth.check_controller 返回 True
        import tianji.ops as ops_mod
        monkeypatch.setattr(
            ops_mod.auth, "check_controller",
            lambda conn, ident: ident["worker_id"] == "admin")

        _write_claude_settings(fake_home)
        client = TestClient(wa.app)
        r = client.post("/api/ctrl/send", json={"text": "hello"})
        assert r.status_code == 200
        assert r.json() == {"accepted": True}
        # 清理
        if wa._ctrl_session:
            wa._ctrl_session.close()


class TestWizardControllerSettings:
    def test_kimi_settings_has_ctrl_session(self, fake_home):
        """wizard._write_controller_settings 为 kimi 写出 ctrl_session 块含 launch。"""
        from tianji import wizard
        sub = fake_home / "sub_home"
        sub.mkdir(parents=True, exist_ok=True)
        keys_dir = sub / "keys"
        keys_dir.mkdir(parents=True, exist_ok=True)
        wizard._write_controller_settings(
            home_p=sub, home=str(sub), shell="kimi", secret="s",
            provider={"model": "m", "base_url": "https://x", "key_name": "k"},
            ready=True, cards=[])
        doc = json.loads((injected_dir() / "settings-controller.json").read_text(encoding="utf-8"))
        cs = doc["ctrl_session"]
        assert cs["protocol"] == "acp"
        assert cs["launch"] == ["kimi", "acp"]
        assert "provider_env" in cs


class TestPluginCtrlSession:
    """票 31: ctrl_session 块回进 SHELL_ENTRY_DEFAULTS(插件化收口)。"""

    def test_third_acp_shell_dispatches_zero_new_python(self, fake_home):
        """第三个 ACP 壳(条目标配 ctrl_session) → ACPBackend,零行新 Python。"""
        from tianji import wizard
        fake_entry = {
            "binding": "config", "protocols": ["anthropic"],
            "isolated_dir_mode": "workdir-grouping",
            "ctrl_session": {"protocol": "acp", "launch": ["fake-acp", "acp"],
                             "data_root_env": "FAKE_HOME",
                             "provider_env": {
                                 "target": "process_env",
                                 "map": {"MAP_FOO": "{model}"},
                             }},
        }
        wizard.SHELL_ENTRY_DEFAULTS["fake-acp"] = fake_entry
        try:
            _write_acp_settings(fake_home, "fake-acp")
            settings = injected_dir() / "settings-controller.json"
            b = BaseBackend.from_config(fake_home, settings)
            assert isinstance(b, ACPBackend)
            # launch 来自 settings 文件(条目数据优先,不硬编码)
            assert b.launch == ["fake-acp", "acp"]
            assert b.data_root_env == "FAKE_HOME"
            assert b.provider_env == {"target": "process_env", "map": {
                "MAP_FOO": "{model}",
            }}
        finally:
            wizard.SHELL_ENTRY_DEFAULTS.pop("fake-acp", None)

    def test_shell_without_ctrl_session_uses_append_system_prompt(self, fake_home):
        """无 ctrl_session 的壳(codex) → settings 用 appendSystemPrompt,不含 ctrl_session 块。"""
        from tianji import wizard
        wizard._write_controller_settings(
            home_p=fake_home, home=str(fake_home), shell="codex",
            secret="s")
        doc = json.loads((injected_dir() / "settings-controller.json").read_text(
            encoding="utf-8"))
        assert "ctrl_session" not in doc
        assert "appendSystemPrompt" in doc
