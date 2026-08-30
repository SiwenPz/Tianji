"""Task-08: ${key} placeholder fix + fail-loud in provider_env rendering."""

import json
import os
import sys

import pytest

from tianji.adapters.template import TEMPLATE_CODEX, TEMPLATE_KIMI, TEMPLATE_CLAUDE
from tianji.shellrender import _render_config_binding
from tianji.ctrlprotocols import _build_provider_env
from tianji.db import connect


# ── ${key} → {key} 修复验证 ──

class TestProviderEnvPlaceholder:
    """provider_env.map 中的 key 占位符应是 {key} 而非 ${key}(6.5/6.6)。"""

    def test_codex_placeholder_no_dollar_prefix(self, tmp_path):
        """codex provider_env: {key} 被正确替换,无多余 $ 前缀。"""
        key_file = tmp_path / "k.key"
        key_file.write_text("sk-test-123", encoding="utf-8")
        ctx = {
            "shell": "codex",
            "entry": {"worker_data_root_env": "", "provider_env": TEMPLATE_CODEX["provider_env"]},
            "key_ref": str(key_file),
            "model": "step-router-v1",
            "base_url": "http://127.0.0.1:19999",
        }
        prefix_str = _render_config_binding(ctx)[0]
        # CODEX_WIZARD_KEY 应= sk-test-123(无 $ 前缀)
        assert "set CODEX_WIZARD_KEY=sk-test-123" in prefix_str

    def test_kimi_placeholder_no_dollar_prefix(self):
        """kimi provider_env: {key} 被正确替换。"""
        prov = TEMPLATE_KIMI["provider_env"]
        pmap = prov.get("map", {})
        ctx = {"key": "sk-kimi-456", "model": "kimi-v1", "base_url": "http://x", "protocol": "anthropic"}
        env = {}
        for var_name, tpl in pmap.items():
            val = tpl.format(**ctx)
            if val:
                env[var_name] = val
        assert env.get("KIMI_MODEL_API_KEY") == "sk-kimi-456", \
            f"KIMI_MODEL_API_KEY 不应带 $ 前缀: {env.get('KIMI_MODEL_API_KEY')!r}"

    def test_claude_placeholder_unchanged(self, tmp_path):
        """claude provider_env: {base_url}/{model}/{key} 全部正确替换。"""
        from tianji.shellrender import _read_key
        ctx = {
            "shell": "claude",
            "entry": {"worker_data_root_env": "", "provider_env": TEMPLATE_CLAUDE["provider_env"]},
            "key_ref": str(tmp_path / "k.key"),
            "model": "test-model",
            "base_url": "http://127.0.0.1:19999",
        }
        (tmp_path / "k.key").write_text("sk-claude", encoding="utf-8")
        prefix_str = _render_config_binding(ctx)[0]
        assert "set ANTHROPIC_AUTH_TOKEN=sk-claude" in prefix_str
        assert "set ANTHROPIC_BASE_URL=http://127.0.0.1:19999" in prefix_str
        assert "set ANTHROPIC_MODEL=test-model" in prefix_str


# ── fail-loud: key_ref 文件缺失 ──

class TestReadKeyFailLoud:
    """key_ref 给了但文件读不到 → FileNotFoundError 指路(附录 E.4);
    空 key_ref 保持返回 ""(免 key 放行)。"""

    def test_read_key_missing_file_raises(self, tmp_path):
        """_read_key: 引用文件不存在 → FileNotFoundError 带 key_ref 路径。"""
        from tianji.shellrender import _read_key
        missing = tmp_path / "no-such.key"
        with pytest.raises(FileNotFoundError, match="no-such.key"):
            _read_key(str(missing))

    def test_read_key_empty_ref_returns_empty(self):
        """_read_key: 空 key_ref 保持返回 ""(不 fail-loud)。"""
        from tianji.shellrender import _read_key
        assert _read_key("") == ""

    def test_config_binding_missing_key_ref_fails_loud(self, tmp_path):
        """端到端: config_binding 渲染走 _read_key,key 文件缺失即报错指路。"""
        ctx = {
            "shell": "kimi",
            "entry": {"provider_env": {"map": {"KIMI_MODEL_API_KEY": "{key}"}}},
            "key_ref": str(tmp_path / "absent.key"),
            "model": "kimi-v1",
            "base_url": "http://x",
            "isolated_dir": "",
        }
        with pytest.raises(FileNotFoundError, match="absent.key"):
            _render_config_binding(ctx)


# ── provider_env 模板占位符容错口径 ──

class TestFailLoudProviderEnv:
    """模板占位符与 ctx 不匹配时的行为口径: shellrender 跳过该变量
    (try/except 鲁棒性),ctrlprotocols 抛 KeyError(各自修复口径,见报告)。"""

    def test_shellrender_format_error_degrades(self):
        """shellrender: 模板含未知占位符 → 该变量跳过(try/except 鲁棒性),
        不崩也不拼出半截 set 语句。"""
        ctx = {
            "entry": {"provider_env": {"map": {"FOO": "{nonexistent}"}}},
            "key_ref": "",
            "model": "",
            "base_url": "",
            "shell": "test",
        }
        cmd = _render_config_binding(ctx)[0]
        assert cmd == "test"  # FOO 未注入,薄命令原样
        assert "FOO" not in cmd

    def test_ctrlprotocols_format_error_propagates(self):
        """ctrlprotocols: 模板含未知占位符 → KeyError 抛出让调用方看见。"""
        pmap = {"API_KEY": "{missing_key}"}
        with pytest.raises(KeyError, match="missing_key"):
            _build_provider_env(
                {"target": "process_env", "map": pmap},
                key_ref="", model="", base_url="", protocol="")

    def test_valid_template_still_works(self, tmp_path):
        """正常模板: .format() 无错误,正确渲染。"""
        pmap = {
            "API_KEY": "{key}",
            "MODEL_NAME": "{model}",
        }
        key_file = tmp_path / "k.key"
        key_file.write_text("my-secret", encoding="utf-8")
        result = _build_provider_env(
            {"target": "process_env", "map": pmap},
            key_ref=str(key_file), model="gpt-4", base_url="http://x", protocol="openai")
        assert result["API_KEY"] == "my-secret"
        assert result["MODEL_NAME"] == "gpt-4"
