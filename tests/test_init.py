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
