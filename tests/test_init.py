"""一键起步(票 17 首次运行向导补强): tianji init/console。"""

import json
from pathlib import Path

from tianji import auth, wizard
from tianji.db import connect


def test_init_bootstrap_full(monkeypatch, tmp_path):
    home = tmp_path / "h1"
    monkeypatch.setenv("TIANJI_HOME", str(home))
    r = wizard.init_bootstrap(
        home=str(home), shell="claude", model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/anthropic", key_name="官key",
        key_value="sk-init-1", worker="赵云")
    # 产出: key 文件/账本/总控身份/settings 一体文件/工人
    assert (home / "keys" / "官key.key").read_text() == "sk-init-1"
    assert (home / "ledger.db").exists()
    settings = json.loads((home / "settings-controller.json")
                          .read_text(encoding="utf-8"))
    env = settings["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-init-1"
    assert env["TIANJI_WORKER_ID"] == "总控" and env["TIANJI_SECRET"]
    assert env["TIANJI_HOME"] == str(home.resolve())
    assert r["next"].startswith("开总控会话")
    assert r["worker"]["registered"] is True
    # key 本体不进账本
    c = connect()
    for row in c.execute("SELECT value FROM configs"):
        assert "sk-init-1" not in (row["value"] or "")
    # controller 身份可用
    assert auth.check_controller(c, {"worker_id": "总控",
                                     "secret": env["TIANJI_SECRET"]})
    c.close()


def test_init_idempotent(monkeypatch, tmp_path):
    home = tmp_path / "h2"
    monkeypatch.setenv("TIANJI_HOME", str(home))
    wizard.init_bootstrap(home=str(home), shell="claude", model="m",
                          base_url="https://a", key_value="sk-1")
    secret1 = json.loads((home / "settings-controller.json")
                         .read_text(encoding="utf-8"))["env"]["TIANJI_SECRET"]
    r2 = wizard.init_bootstrap(home=str(home), shell="claude", model="m",
                               base_url="https://a", key_value="sk-2")
    assert any("总控已注册,跳过" in s for s in r2["steps"])
    # secret 不被二跑轮换
    secret2 = json.loads((home / "settings-controller.json")
                         .read_text(encoding="utf-8"))["env"]["TIANJI_SECRET"]
    assert secret1 == secret2


def test_init_bare_then_configure_later(monkeypatch, tmp_path):
    """裸 init: 不带 key 也能起步;provider 进会话后补,补完 settings 带上,secret 不换。"""
    home = tmp_path / "h3"
    monkeypatch.setenv("TIANJI_HOME", str(home))
    r1 = wizard.init_bootstrap(home=str(home))
    assert r1["provider_configured"] is False
    assert "当前会话" in r1["next"]
    env1 = json.loads((home / "settings-controller.json")
                      .read_text(encoding="utf-8"))["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in env1  # 未配 provider 不写
    assert env1["TIANJI_WORKER_ID"] == "总控"
    # claude 壳: settings 带 appendSystemPrompt=总控角色自述(会话一开就引导配 provider)
    doc1 = json.loads((home / "settings-controller.json")
                      .read_text(encoding="utf-8"))
    assert "总控" in doc1["appendSystemPrompt"]
    assert "provider" in doc1["appendSystemPrompt"]
    # 预授权 tianji 命令: 总控会话跑 tianji 不弹审核窗(2026-08-20 模拟实证)
    allow = doc1["permissions"]["allow"]
    assert "Bash(tianji:*)" in allow
    assert "Bash(python -m tianji:*)" in allow
    # 会话里配好了 key → 重跑 init 带上参数,就地更新
    r2 = wizard.init_bootstrap(home=str(home), model="deepseek-v4-flash",
                               base_url="https://api.deepseek.com/anthropic",
                               key_value="sk-late")
    assert r2["provider_configured"] is True
    env2 = json.loads((home / "settings-controller.json")
                      .read_text(encoding="utf-8"))["env"]
    assert env2["ANTHROPIC_AUTH_TOKEN"] == "sk-late"
    assert env2["TIANJI_SECRET"] == env1["TIANJI_SECRET"]  # secret 不轮换
    # 总控实例模型/key 就地更新(票 28 通道,不重建)
    c = connect()
    row = c.execute("SELECT model, key_name FROM instances WHERE name='总控'"
                    ).fetchone()
    assert row["model"] == "deepseek-v4-flash" and row["key_name"] == "主key"
    c.close()


def test_scan_shells_mechanical(monkeypatch):
    """壳清单=机械扫描本机实际安装(假 which),不是模板清单;supported 按有无模板判。"""
    import tianji.wizard as wz
    # 装了 claude(有模板) + gemini(无模板) → 列表=扫到的,gemini 标暂不支持
    monkeypatch.setattr(wz.shutil, "which",
                        lambda name: {"claude": "C:/bin/claude.exe",
                                      "gemini": "C:/bin/gemini.exe"}.get(name))
    found = wz.scan_shells()
    assert [f["name"] for f in found] == ["claude", "gemini"]
    assert found[0]["supported"] is True
    assert found[1]["supported"] is False
    # Windows 坑: cline 是 npm 无扩展名 shim,补 .cmd 探测才能扫到
    monkeypatch.setattr(wz.shutil, "which",
                        lambda name: "C:/bin/cline.cmd" if name == "cline.cmd"
                        else None)
    found = wz.scan_shells()
    assert [f["name"] for f in found] == ["cline"]
    assert found[0]["path"] == "C:/bin/cline.cmd"
    # 只装不支持壳 → 无 supported 项(init 层据此提示先装支持的壳)
    monkeypatch.setattr(wz.shutil, "which",
                        lambda name: "C:/bin/aider.exe" if name == "aider"
                        else None)
    found = wz.scan_shells()
    assert [f["name"] for f in found] == ["aider"]
    assert all(not f["supported"] for f in found)


def test_init_cli_interactive(monkeypatch, tmp_path):
    """CLI 交互: 扫描→选壳→二次确认→打印总控打开命令(不真启动)。"""
    from typer.testing import CliRunner
    from tianji.cli import app
    import tianji.wizard as wz
    monkeypatch.setenv("TIANJI_HOME", str(tmp_path))
    monkeypatch.setattr(wz, "scan_shells", lambda: [
        {"name": "claude", "path": "C:/bin/claude.exe", "supported": True}])
    r = CliRunner().invoke(app, ["init", "--print"], input="1\ny\n")
    assert r.exit_code == 0
    assert "总控 = claude" in r.output
    assert "选一个助手当总控" in r.output
    assert "--settings" in r.output
    assert "--append-system-prompt" in r.output  # 总控角色自述走 CLI 参数注入
    # 账本与身份真的建了
    assert (tmp_path / "ledger.db").exists()
    assert (tmp_path / "ctrl-secret.txt").exists()


def test_init_cli_no_shells(monkeypatch, tmp_path):
    """没扫到任何壳 → 提示先装,不建账退出。"""
    from typer.testing import CliRunner
    from tianji.cli import app
    import tianji.wizard as wz
    monkeypatch.setenv("TIANJI_HOME", str(tmp_path))
    monkeypatch.setattr(wz, "scan_shells", lambda: [])
    r = CliRunner().invoke(app, ["init"])
    assert r.exit_code == 1
    assert "先装一个" in r.output
    assert not (tmp_path / "ledger.db").exists()
