"""web 首次配置页(变体 B 一屏全览): /setup + /api/setup/*。

配置全程纯点选: 选总控(壳落账;claude 时重写 settings 一体文件)→
探测(mock 掉,不真联网)→落地(key 文件/条目+工人实例逐个注册,
角色+序号命名,个数顺延,重跑幂等)。写接口照旧要总控身份。
"""

import json

import pytest
from fastapi.testclient import TestClient

from tianji import webapp, wizard
from tianji.db import connect


@pytest.fixture
def home(tianji_home, monkeypatch):
    """模拟 tianji start 裸跑后的状态: 总控已注册,壳未定;扫描固定两壳。"""
    wizard.init_bootstrap(home=str(tianji_home), shell="未配置")
    monkeypatch.setattr(wizard, "scan_shells", lambda: [
        {"name": "claude", "path": "C:/bin/claude.exe", "supported": True},
        {"name": "codex", "path": "C:/bin/codex.exe", "supported": True},
        {"name": "dsh", "path": "C:/bin/dsh.cmd", "supported": False}])
    return tianji_home


@pytest.fixture
def client(home, monkeypatch):
    """生产路径: start 起 daemon 前已注入总控身份 env,写接口可用。"""
    secret = (home / "ctrl-secret.txt").read_text(encoding="utf-8").strip()
    monkeypatch.setenv("TIANJI_WORKER_ID", "总控")
    monkeypatch.setenv("TIANJI_SECRET", secret)
    return TestClient(webapp.app)


def test_initial_state(client):
    """初始: configured=False,总控壳未定,扫描列表带上,编制空。"""
    s = client.get("/api/setup/state").json()
    assert s["configured"] is False
    assert s["controller"] == {"shell": "未配置", "model": "未配置"}
    assert [x["name"] for x in s["scanned"]] == ["claude", "codex", "dsh"]
    assert s["scanned"][2]["supported"] is False  # 无模板如实标注
    assert s["instances"] == [] and s["keys"] == []
    # /api/state 也带 configured(首页据此放提示条)
    assert client.get("/api/state").json()["configured"] is False


def test_pick_controller_claude_rewrites_settings(client, home):
    """选 claude+外接 key: 总控牌就地更新(壳/模型/key),settings 一体文件重写。"""
    r = client.post("/api/setup/controller", json={
        "shell": "claude", "source": "key", "key": "sk-web-1",
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-flash"})
    assert r.status_code == 200, r.text
    c = connect()
    row = c.execute("SELECT shell, model, key_name FROM instances"
                    " WHERE name='总控'").fetchone()
    assert (row["shell"], row["model"], row["key_name"]) == (
        "claude", "deepseek-v4-flash", "主key")
    krow = c.execute("SELECT value FROM configs WHERE key='key:主key'").fetchone()
    assert json.loads(krow["value"])["base_url"] == (
        "https://api.deepseek.com/anthropic")
    c.close()
    # key 本体只落文件不进账本
    assert (home / "keys" / "主key.key").read_text() == "sk-web-1"
    # settings: 身份 env+provider env+角色话术+预授权
    doc = json.loads((home / "settings-controller.json").read_text(
        encoding="utf-8"))
    assert doc["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-web-1"
    assert doc["env"]["ANTHROPIC_BASE_URL"] == (
        "https://api.deepseek.com/anthropic")
    assert "总控" in doc["appendSystemPrompt"]
    assert "Bash(tianji:*)" in doc["permissions"]["allow"]
    # 还没工人 → 仍未 configured
    assert r.json()["state"]["configured"] is False


def test_pick_controller_builtin_login(client, home):
    """总控用自己的登录/订阅: 不写 provider env,壳落账,settings 仍重写。"""
    r = client.post("/api/setup/controller", json={
        "shell": "claude", "source": "builtin"})
    assert r.status_code == 200, r.text
    doc = json.loads((home / "settings-controller.json").read_text(
        encoding="utf-8"))
    assert "ANTHROPIC_AUTH_TOKEN" not in doc["env"]
    c = connect()
    assert c.execute("SELECT shell FROM instances WHERE name='总控'"
                     ).fetchone()["shell"] == "claude"
    c.close()


def test_probe_models_mocked(client, monkeypatch):
    """探测=唯一联网的一步,测试 mock;探不到回 null 由用户手填。"""
    monkeypatch.setattr(wizard, "probe_models",
                        lambda url, key: ["m-a", "m-b"])
    r = client.post("/api/setup/probe", json={
        "base_url": "https://x", "key": "sk-1"})
    assert r.json() == {"models": ["m-a", "m-b"]}
    monkeypatch.setattr(wizard, "probe_models", lambda url, key: None)
    assert client.post("/api/setup/probe", json={
        "base_url": "https://x", "key": "sk-1"}).json() == {"models": None}
    # 缺参数 400
    assert client.post("/api/setup/probe", json={"base_url": ""}).status_code == 400


def _land_worker_cards(client):
    return client.post("/api/setup/land", json={"cards": [
        {"shell": "codex", "source": "key", "key_value": "sk-step",
         "base_url": "https://api.stepfun.com", "model": "step-router-v1",
         "role": "审核", "count": 2},
        {"shell": "claude", "source": "builtin", "model": "内置m",
         "role": "实施", "count": 1},
    ]})


def test_land_full_flow(client, home):
    """落地全流程: key 文件/条目、工人注册(角色 notes)、多开命名顺延、幂等。"""
    client.post("/api/setup/controller", json={
        "shell": "claude", "source": "builtin"})
    r = _land_worker_cards(client)
    assert r.status_code == 200, r.text
    assert r.json()["registered"] == ["审核1", "审核2", "实施1"]
    # key 落地(自动起名 key1): 文件+条目
    assert (home / "keys" / "key1.key").read_text() == "sk-step"
    c = connect()
    kcfg = json.loads(c.execute("SELECT value FROM configs WHERE key='key:key1'"
                                ).fetchone()["value"])
    assert kcfg["base_url"] == "https://api.stepfun.com"
    assert [m["id"] for m in kcfg["models"]] == ["step-router-v1"]
    rows = c.execute(
        "SELECT i.name, i.shell, i.key_name, p.notes FROM instances i"
        " JOIN ability_profiles p ON p.instance_name=i.name"
        " WHERE i.name!='总控' ORDER BY i.name").fetchall()
    assert [r["name"] for r in rows] == ["实施1", "审核1", "审核2"]
    assert "拟定角色: 审核" in rows[1]["notes"]
    assert rows[1]["key_name"] == "key1"
    c.close()
    # 加开一个: 同名顺延 审核3
    r2 = client.post("/api/setup/land", json={"cards": [
        {"shell": "codex", "source": "key", "key_value": "sk-step",
         "base_url": "https://api.stepfun.com", "model": "step-router-v1",
         "role": "审核", "count": 3}]})
    assert r2.json()["registered"] == ["审核3"]
    # 重跑同 payload 幂等: 同牌面已注册的算已落地,不重复注册
    r3 = _land_worker_cards(client)
    assert r3.json()["registered"] == []
    assert r3.json()["state"]["configured"] is True
    c = connect()
    n = c.execute("SELECT COUNT(*) n FROM instances WHERE is_active=1"
                  ).fetchone()["n"]
    assert n == 1 + 4  # 总控 + 审核×3 + 实施×1
    c.close()


def test_setup_write_requires_identity(home, monkeypatch):
    """未注入总控身份: 写接口 403(只读口径与其他写接口一致)。"""
    monkeypatch.delenv("TIANJI_WORKER_ID", raising=False)
    monkeypatch.delenv("TIANJI_SECRET", raising=False)
    c = TestClient(webapp.app)
    assert c.post("/api/setup/controller",
                  json={"shell": "claude"}).status_code == 403
    assert c.post("/api/setup/land", json={"cards": [{}]}).status_code == 403
    # 只读 state 不拦
    assert c.get("/api/setup/state").status_code == 200


def test_setup_page_served(client):
    """配置页挂 /setup(变体 B 一屏全览);原型路由已清尾。"""
    html = client.get("/setup").text
    for marker in ("配一张牌", "编制总览", "/api/setup/land",
                   "/api/setup/controller", "/api/setup/probe"):
        assert marker in html, marker
    assert client.get("/prototype/setup").status_code == 404
    # 首页顶部有"配置"按钮互跳
    assert "/setup" in client.get("/").text
