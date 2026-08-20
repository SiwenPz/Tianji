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
