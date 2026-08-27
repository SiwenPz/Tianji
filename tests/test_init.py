"""一键起步(票 17 首次运行向导补强): tianji start 建账内核 + start 命令 smoke。

2026-08-21 用户裁决: tianji init 删除,替换为 tianji start——终端零问答,
配置(选总控/配实例/定角色)全在 web 配置页点选(归 test_setup_web.py)。
"""

import json
from pathlib import Path

from tianji import auth, wizard
from tianji.db import connect, injected_dir


def test_init_bootstrap_full(monkeypatch, tmp_path):
    home = tmp_path / "h1"
    monkeypatch.setenv("TIANJI_HOME", str(home))
    r = wizard.init_bootstrap(
        home=str(home), shell="claude", model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/anthropic", key_name="官key",
        key_value="sk-init-1", worker="赵云")
    # 产出: key 文件/账本/总控身份/settings 一体文件/工人
    assert (injected_dir() / "官key.key").read_text() == "sk-init-1"
    assert (home / "ledger.db").exists()
    settings = json.loads((injected_dir() / "settings-controller.json")
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
    secret1 = json.loads((injected_dir() / "settings-controller.json")
                         .read_text(encoding="utf-8"))["env"]["TIANJI_SECRET"]
    r2 = wizard.init_bootstrap(home=str(home), shell="claude", model="m",
                               base_url="https://a", key_value="sk-2")
    assert any("总控已注册,跳过" in s for s in r2["steps"])
    # secret 不被二跑轮换
    secret2 = json.loads((injected_dir() / "settings-controller.json")
                         .read_text(encoding="utf-8"))["env"]["TIANJI_SECRET"]
    assert secret1 == secret2


def test_init_bare_then_configure_later(monkeypatch, tmp_path):
    """裸起步: 不带 key 也能建账;provider 之后补,补完 settings 带上,secret 不换。"""
    home = tmp_path / "h3"
    monkeypatch.setenv("TIANJI_HOME", str(home))
    r1 = wizard.init_bootstrap(home=str(home))
    assert r1["provider_configured"] is False
    assert "配置页" in r1["next"]  # 配置在 web 页点选,不再靠会话聊
    env1 = json.loads((injected_dir() / "settings-controller.json")
                      .read_text(encoding="utf-8"))["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in env1  # 未配 provider 不写
    assert env1["TIANJI_WORKER_ID"] == "总控"
    # claude 壳: settings 带 appendSystemPrompt=总控角色自述(引导去配置页补配)
    doc1 = json.loads((injected_dir() / "settings-controller.json")
                      .read_text(encoding="utf-8"))
    assert "总控" in doc1["appendSystemPrompt"]
    assert "provider" in doc1["appendSystemPrompt"]
    assert "配置页" in doc1["appendSystemPrompt"]
    # 预授权 tianji 命令: 总控会话跑 tianji 不弹审核窗(2026-08-20 模拟实证)
    allow = doc1["permissions"]["allow"]
    assert "Bash(tianji:*)" in allow
    assert "Bash(python -m tianji:*)" in allow
    # 配好了 key → 重跑带上参数,就地更新
    r2 = wizard.init_bootstrap(home=str(home), model="deepseek-v4-flash",
                               base_url="https://api.deepseek.com/anthropic",
                               key_value="sk-late")
    assert r2["provider_configured"] is True
    env2 = json.loads((injected_dir() / "settings-controller.json")
                      .read_text(encoding="utf-8"))["env"]
    assert env2["ANTHROPIC_AUTH_TOKEN"] == "sk-late"
    assert env2["TIANJI_SECRET"] == env1["TIANJI_SECRET"]  # secret 不轮换
    # 配好后话术换分支: 不再说"还没配齐",防总控重复引导(2026-08-21 模拟)
    doc2 = json.loads((injected_dir() / "settings-controller.json")
                      .read_text(encoding="utf-8"))
    assert "还没配齐" not in doc2["appendSystemPrompt"]
    assert "已经配好" in doc2["appendSystemPrompt"]
    # 总控实例模型/key 就地更新(票 28 通道,不重建)
    c = connect()
    row = c.execute("SELECT model, key_name FROM instances WHERE name='总控'"
                    ).fetchone()
    assert row["model"] == "deepseek-v4-flash" and row["key_name"] == "主key"
    c.close()


def _git_init(tmp_path, name="proj"):
    import subprocess
    p = tmp_path / name
    subprocess.run(["git", "init", "-q", str(p)], check=True,
                   capture_output=True)
    return p


def test_start_records_default_project_dir(monkeypatch, tmp_path):
    """start 在 git 仓库根内 → 自动记当前目录为默认工作目录(18.1,票 39)。"""
    home = tmp_path / "h"
    proj = _git_init(tmp_path)
    expected = str(proj.resolve())
    monkeypatch.setenv("TIANJI_HOME", str(home))
    monkeypatch.chdir(proj)
    r = wizard.init_bootstrap(home=str(home))
    assert any(f"默认工作目录: {expected}" in s for s in r["steps"])
    c = connect()
    row = c.execute("SELECT value FROM configs WHERE key='default_project_dir'"
                    ).fetchone()
    assert row and row["value"] == expected
    c.close()


def test_start_ignores_cwd_equal_home(monkeypatch, tmp_path):
    """cwd=账本根 → 不自动记(避免把账本目录当项目)。"""
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("TIANJI_HOME", str(home))
    monkeypatch.chdir(home)
    wizard.init_bootstrap(home=str(home))
    c = connect()
    assert c.execute("SELECT 1 FROM configs WHERE key='default_project_dir'"
                     ).fetchone() is None
    c.close()


def test_start_ignores_non_git_cwd(monkeypatch, tmp_path):
    """非 git 目录 → 不自动记(走 web 配置页/命令手动设)。"""
    home = tmp_path / "h"
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("TIANJI_HOME", str(home))
    monkeypatch.chdir(plain)
    wizard.init_bootstrap(home=str(home))
    c = connect()
    assert c.execute("SELECT 1 FROM configs WHERE key='default_project_dir'"
                     ).fetchone() is None
    c.close()


def test_controller_discipline_in_role_prompt(monkeypatch, tmp_path):
    """票 41: 总控角色提示词带两条铁律——先摆分工再动工 + 不亲自实施。"""
    home = tmp_path / "h"
    monkeypatch.setenv("TIANJI_HOME", str(home))
    wizard.init_bootstrap(home=str(home))
    doc = json.loads((injected_dir() / "settings-controller.json")
                     .read_text(encoding="utf-8"))
    prompt = doc["appendSystemPrompt"]
    assert "分工" in prompt and "不亲自" in prompt
    # kimi 壳同源 role_text(ctrl_session 块)
    home_k = tmp_path / "hk"
    wizard.init_bootstrap(home=str(home_k), shell="kimi")
    doc_k = json.loads((injected_dir() / "settings-controller.json")
                       .read_text(encoding="utf-8"))
    assert "分工" in doc_k["ctrl_session"]["role_text"]
    assert "不亲自" in doc_k["ctrl_session"]["role_text"]


def test_start_explicit_work_dir_non_git(monkeypatch, tmp_path):
    """票 40: 引导显式给工作目录 → 直接记默认项目目录(非 git 目录也写)。"""
    home = tmp_path / "h"
    proj = tmp_path / "plain"
    proj.mkdir()
    monkeypatch.setenv("TIANJI_HOME", str(home))
    monkeypatch.chdir(tmp_path)  # 调用方 cwd 是不是 git 仓库无所谓
    r = wizard.init_bootstrap(home=str(home), work_dir=str(proj))
    assert any(f"工作目录: {str(proj.resolve())}" in s for s in r["steps"])
    c = connect()
    row = c.execute("SELECT value FROM configs WHERE key='default_project_dir'"
                    ).fetchone()
    assert row and row["value"] == str(proj.resolve())
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
    # dsh 也须扫到(2026-08-21 用户机器实测漏扫);无模板→如实标"暂不支持"
    monkeypatch.setattr(wz.shutil, "which",
                        lambda name: "C:/bin/dsh.cmd" if name == "dsh.cmd"
                        else None)
    found = wz.scan_shells()
    assert [f["name"] for f in found] == ["dsh"]
    assert found[0]["supported"] is False
    # 只装不支持壳 → 无 supported 项(配置页据此把不可选的置灰)
    monkeypatch.setattr(wz.shutil, "which",
                        lambda name: "C:/bin/aider.exe" if name == "aider"
                        else None)
    found = wz.scan_shells()
    assert [f["name"] for f in found] == ["aider"]
    assert all(not f["supported"] for f in found)


def test_start_cli_smoke(monkeypatch, tmp_path):
    """start=机械引导工作目录(回车=当前目录,票 40)+建账+起 daemon+打印配置页;重跑幂等。"""
    from typer.testing import CliRunner

    from tianji import daemon
    from tianji.cli import app
    monkeypatch.setenv("TIANJI_HOME", str(tmp_path))
    # 不真起 daemon(monitor+web 子进程),smoke 只验证编排
    monkeypatch.setattr(daemon, "daemon_start", lambda: {"web_port": 8899})
    # 回车(空输入)= 当前目录
    r = CliRunner().invoke(app, ["start", "--no-browser"], input="\n")
    assert r.exit_code == 0, r.output
    assert "http://127.0.0.1:8899/setup" in r.output
    # 引导提示与"工作目录"输出行在
    assert "工作目录" in r.output
    # 账本与身份真的建了;总控壳未定=未配置
    assert (tmp_path / "ledger.db").exists()
    assert (injected_dir() / "ctrl-secret.txt").exists()
    c = connect()
    row = c.execute("SELECT shell, model FROM instances WHERE name='总控'"
                    ).fetchone()
    assert row["shell"] == "未配置"
    c.close()
    # 重跑幂等: secret 不轮换(从 ctrl-secret.txt 读回)
    s1 = (injected_dir() / "ctrl-secret.txt").read_text(encoding="utf-8").strip()
    r2 = CliRunner().invoke(app, ["start", "--no-browser"], input="\n")
    assert r2.exit_code == 0, r2.output
    assert (injected_dir() / "ctrl-secret.txt").read_text(encoding="utf-8").strip() == s1


def test_start_cli_workdir_prompt_custom(monkeypatch, tmp_path):
    """票 40: start 引导输入目录 → 该目录写默认项目目录(显式值不要求 git)。"""
    from typer.testing import CliRunner

    from tianji import daemon
    from tianji.cli import app
    home = tmp_path / "h"
    proj = tmp_path / "myproj"  # 非 git 目录
    proj.mkdir()
    monkeypatch.setenv("TIANJI_HOME", str(home))
    monkeypatch.setattr(daemon, "daemon_start", lambda: {"web_port": 8899})
    r = CliRunner().invoke(app, ["start", "--no-browser"],
                           input=str(proj.resolve()) + "\n")
    assert r.exit_code == 0, r.output
    c = connect()
    row = c.execute("SELECT value FROM configs WHERE key='default_project_dir'"
                    ).fetchone()
    assert row and row["value"] == str(proj.resolve())
    c.close()
