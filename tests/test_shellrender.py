"""声明式壳渲染器(13.8/票34): 注册表驱动渲染、零壳名分支、重启一致性。"""

import json
from pathlib import Path

import pytest

from tianji import ops, shellrender, wizard
from tianji.shellrender import RENDERERS, rerender_instance


def test_renderers_cover_five_shells():
    """验收①: 五个出厂壳各有 renderer(或明确迁移映射到同形态实现)。"""
    for name in ("claude", "codex", "kimi", "atomcode", "cline"):
        assert name in RENDERERS, name


def test_generic_launcher_has_no_shell_name_branches():
    """验收③: 通用启动器零具体壳名分支——特殊行为全落在壳 renderer。"""
    import inspect
    src = inspect.getsource(wizard._generate_launcher)
    assert "shell ==" not in src and 'shell in' not in src
    assert "shellrender.render" in src


def _add_claude_instance(conn, controller, tmp_path, name="新工"):
    key_file = tmp_path / f"{name}.key"
    key_file.write_text("sk-render-1", encoding="utf-8")
    iso = tmp_path / f"{name}-iso"
    r = wizard.add_instance(
        conn, controller, name, "claude", "deepseek-v4-flash",
        key_name=f"{name}key", base_url="https://api.example.com/anthropic",
        protocol="anthropic", key_ref=str(key_file), isolated_dir=str(iso),
        binary="python", confirm=True, request_id=f"sr-{name}")
    return r, key_file, iso


def test_double_render_is_deterministic(conn, controller, tmp_path):
    """验收⑤: 同一实例连续两次重渲染关键产物内容一致。"""
    r, key_file, iso = _add_claude_instance(conn, controller, tmp_path)
    settings = Path(r["artifacts"][0])
    first = settings.read_text(encoding="utf-8")
    cmd1 = r["launch_cmd"]
    for _ in range(2):
        cmd, arts = rerender_instance(conn, "新工")
        assert cmd == cmd1
        assert Path(arts[0]).read_text(encoding="utf-8") == first


def test_credential_plaintext_only_in_protected_artifact(
        conn, controller, tmp_path):
    """验收⑤: 凭据本体只在受保护引用位置(隔离目录 settings),不进
    launch_cmd/账本 configs。"""
    r, _, _ = _add_claude_instance(conn, controller, tmp_path)
    settings_text = Path(r["artifacts"][0]).read_text(encoding="utf-8")
    assert "sk-render-1" in settings_text          # 受保护产物内注入
    assert "sk-render-1" not in r["launch_cmd"]
    for row in conn.execute("SELECT value FROM configs").fetchall():
        assert "sk-render-1" not in (row["value"] or "")


def test_codex_render_reads_key_from_ref_not_launch_cmd(
        conn, controller, tmp_path):
    """codex 渲染: config.toml 落隔离目录,launch_cmd 只带 key 文件引用。"""
    key_file = tmp_path / "cx.key"
    key_file.write_text("sk-cx-9", encoding="utf-8")
    iso = tmp_path / "cx-iso"
    r = wizard.add_instance(
        conn, controller, "码工", "codex", "deepseek-v4-flash",
        key_name="cxkey", base_url="https://api.example.com/v1",
        key_ref=str(key_file),
        isolated_dir=str(iso), binary="python", confirm=True,
        request_id="sr-cx")
    assert "sk-cx-9" not in r["launch_cmd"]
    assert str(key_file) in r["launch_cmd"]       # 引用位置,非本体
    cfg = (iso / "config.toml").read_text(encoding="utf-8")
    assert "model_provider" in cfg and "sk-cx-9" not in cfg
    cmd2, arts2 = rerender_instance(conn, "码工")
    assert cmd2 == r["launch_cmd"]


def test_config_binding_shells_thin_command_byte_compat(
        conn, controller):
    """kimi/atomcode/cline 同形态迁移映射: 薄命令与旧启动器字节一致;
    条目显式声明 worker_data_root_env 才注入数据根隔离。"""
    for shell in ("kimi", "atomcode", "cline"):
        ops.config_set(conn, controller, f"shell:{shell}",
                       json.dumps(wizard.SHELL_ENTRY_DEFAULTS[shell],
                                  ensure_ascii=False),
                       request_id=f"sr-{shell}")
        cmd, arts = shellrender.render(conn, shell, instance="x",
                                       model="m", key_name="")
        assert cmd == shell and arts == []
    entry = dict(wizard.SHELL_ENTRY_DEFAULTS["kimi"])
    entry["worker_data_root_env"] = "KIMI_CODE_HOME"
    cmd, _ = shellrender.render(conn, "kimi", instance="x", model="m",
                                key_name="", isolated_dir="D:/iso/k",
                                entry=entry)
    assert cmd == 'cmd /c "set KIMI_CODE_HOME=D:/iso/k&& kimi"'


def test_rerender_creates_nothing_when_registry_missing(
        conn, controller, tmp_path):
    """验收④: 重启重渲染只查表——注册表缺条目时不触发任何补建。"""
    # 直接注册实例,不经向导: 账本里没有任何 shell:/key:/集成条目
    ops.instance_register(conn, "裸工", "claude", "m1",
                          isolated_dir=str(tmp_path / "bare"))
    before = conn.execute("SELECT COUNT(*) n FROM configs").fetchone()["n"]
    rerender_instance(conn, "裸工")   # 兜底渲染成功与否都不得写账本
    after = conn.execute("SELECT COUNT(*) n FROM configs").fetchone()["n"]
    assert before == after


def test_rerender_unknown_instance_raises(conn):
    with pytest.raises(ValueError, match="未注册"):
        rerender_instance(conn, "查无此人")


def test_credential_resolution_prefers_registry_over_legacy(
        conn, controller):
    """注册表优先: credential→provider 链路的 base_url 压过旧 key: 条目。"""
    integrations = shellrender.integrations
    integrations.ensure_builtin_registry(conn, controller,
                                         request_id="builtin")
    integrations.register_custom_provider(
        conn, controller, "newrelay", "https://new.example/v1",
        "openai_chat", request_id="prov")
    integrations.register_credential(conn, controller, "ck", "newrelay",
                                     key_ref="k.txt", request_id="cred")
    ops.config_set(conn, controller, "key:ck", json.dumps({
        "base_url": "https://old.example/v1"}, ensure_ascii=False),
        request_id="legacy")
    _, prov, key_ref, base_url = shellrender.resolve_credential(conn, "ck")
    assert base_url == "https://new.example/v1"
    assert prov["protocol"] == "openai_chat"
    assert key_ref == "k.txt"


def test_legacy_only_ledger_falls_back_to_key_entry(conn, controller):
    """迁移映射: 无 credential 的旧账本回落旧 key: 条目(只读,不补建)。"""
    ops.config_set(conn, controller, "key:老key", json.dumps({
        "base_url": "https://old.example/v1", "key_ref": "old.txt"},
        ensure_ascii=False), request_id="legacy")
    cred, prov, key_ref, base_url = shellrender.resolve_credential(
        conn, "老key")
    assert cred is None and prov is None
    assert base_url == "https://old.example/v1" and key_ref == "old.txt"
