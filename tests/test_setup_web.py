"""web 首次配置页(变体 B 一屏全览): /setup + /api/setup/*。

配置全程纯点选: 选总控(壳落账;claude 时重写 settings 一体文件)→
探测(mock 掉,不真联网)→落地(key 文件/条目+工人实例逐个注册,
角色+序号命名,个数顺延,重跑幂等)。写接口照旧要总控身份。
"""

import json

import pytest
from fastapi.testclient import TestClient

from tianji import ops, webapp, wizard
from tianji.db import connect, injected_dir


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
    secret = (injected_dir() / "ctrl-secret.txt").read_text(encoding="utf-8").strip()
    monkeypatch.setenv("TIANJI_WORKER_ID", "总控")
    monkeypatch.setenv("TIANJI_SECRET", secret)
    return TestClient(webapp.app)


def test_initial_state(client):
    """初始: configured=False,总控壳未定,扫描列表带上,编制空。"""
    s = client.get("/api/setup/state").json()
    assert s["configured"] is False
    assert s["controller"] == {"shell": "未配置", "model": "未配置",
                               "source": "builtin"}
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
    assert (injected_dir() / "主key.key").read_text() == "sk-web-1"
    # settings: 身份 env+provider env+角色话术+预授权
    doc = json.loads((injected_dir() / "settings-controller.json").read_text(
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
    doc = json.loads((injected_dir() / "settings-controller.json").read_text(
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
    assert (injected_dir() / "key1.key").read_text() == "sk-step"
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
    assert c.post("/api/setup/project-dir",
                  json={"path": "D:/x"}).status_code == 403
    # 只读 state 不拦
    assert c.get("/api/setup/state").status_code == 200


def test_project_dir_roundtrip(client, home):
    """默认工作目录(18.1,票 39): state 带现值;POST 保存/清空。"""
    c = connect()
    c.execute("DELETE FROM configs WHERE key='default_project_dir'")
    c.close()
    s = client.get("/api/setup/state").json()
    assert s["default_project_dir"] == ""
    r = client.post("/api/setup/project-dir", json={"path": "D:/my-proj"})
    assert r.status_code == 200, r.text
    assert r.json()["default_project_dir"] == "D:/my-proj"
    c = connect()
    assert c.execute("SELECT value FROM configs WHERE key='default_project_dir'"
                     ).fetchone()["value"] == "D:/my-proj"
    c.close()
    # 留空保存=清除
    r2 = client.post("/api/setup/project-dir", json={"path": ""})
    assert r2.status_code == 200
    assert r2.json()["default_project_dir"] == ""
    c = connect()
    assert c.execute("SELECT 1 FROM configs WHERE key='default_project_dir'"
                     ).fetchone() is None
    c.close()


def test_setup_page_served(client):
    """配置页挂 /setup(变体 B 一屏全览);票33 起探测走集成注册表发现端点。"""
    html = client.get("/setup").text
    for marker in ("配一张牌", "编制总览", "/api/setup/land",
                   "/api/setup/controller",
                   "/api/integrations/discover-models"):
        assert marker in html, marker
    assert client.get("/prototype/setup").status_code == 404
    # 首页顶部有"配置"按钮互跳
    assert "/setup" in client.get("/").text


def test_land_registers_provider_and_credential(client, home):
    """票45: 落地走注册表;旧 key 条目只是兼容迁移源。"""
    client.post("/api/setup/controller", json={
        "shell": "claude", "source": "builtin"})
    r = client.post("/api/setup/land", json={"cards": [{
        "shell": "codex", "source": "key", "provider": "deepseek",
        "protocol": "openai_chat", "base_url":
            "https://api.deepseek.com/v1",
        "key_value": "sk-registry", "model": "deepseek-chat",
        "role": "审查", "count": 1}]})
    assert r.status_code == 200, r.text
    c = connect()
    provider = json.loads(c.execute(
        "SELECT value FROM configs WHERE"
        " key='integration_provider:deepseek'").fetchone()["value"])
    credential = json.loads(c.execute(
        "SELECT value FROM configs WHERE key='credential:key1'"
    ).fetchone()["value"])
    legacy = json.loads(c.execute(
        "SELECT value FROM configs WHERE key='key:key1'"
    ).fetchone()["value"])
    c.close()
    assert provider["protocol"] == "openai_chat"
    assert credential == {
        "provider": "deepseek",
        "key_ref": str(injected_dir() / "key1.key")}
    assert legacy["protocol"] == "openai_chat"


def test_land_rejects_protocol_and_missing_model_before_write(client, home):
    """提交前过滤协议不兼容与模型不在缓存清单的组合。"""
    client.post("/api/setup/controller", json={
        "shell": "claude", "source": "builtin"})
    bad_shell = client.post("/api/setup/land", json={"cards": [{
        "shell": "claude", "source": "key", "provider": "deepseek",
        "protocol": "openai_chat", "base_url":
            "https://api.deepseek.com/v1",
        "key_value": "sk-x", "model": "m", "role": "审查"}]})
    assert bad_shell.status_code == 400
    assert "协议不兼容" in bad_shell.json()["error"]
    assert not (injected_dir() / "key1.key").exists()

    # 先模拟发现缓存;人工补录和探测结果都进同一清单。
    ident = {"worker_id": "总控", "secret": (injected_dir() / "ctrl-secret.txt")
             .read_text(encoding="utf-8").strip()}
    entry = json.loads(connect().execute(
        "SELECT value FROM configs WHERE"
        " key='integration_provider:kimi'").fetchone()["value"])
    entry["models"] = [{"id": "cached-m"}]
    ops.config_set(connect(), ident, "integration_provider:kimi",
                   json.dumps(entry, ensure_ascii=False),
                   request_id="seed-models")
    bad_model = client.post("/api/setup/land", json={"cards": [{
        "shell": "kimi", "source": "key", "provider": "kimi",
        "protocol": "anthropic", "base_url":
            "https://api.kimi.com/coding/",
        "key_value": "sk-y", "model": "not-cached", "role": "审查"}]})
    assert bad_model.status_code == 400
    assert "不在供应商 kimi 清单" in bad_model.json()["error"]


def test_land_rejects_coding_plan_cross_shell(client, home):
    client.post("/api/setup/controller", json={
        "shell": "claude", "source": "builtin"})
    ident = {"worker_id": "总控", "secret": (injected_dir() / "ctrl-secret.txt")
             .read_text(encoding="utf-8").strip()}
    # 直接构造旧凭据语义;Web 不提供创建 CodingPlan 绑定的入口。
    ops.config_set(connect(), ident, "key:cp-key", json.dumps({
        "base_url": "https://cp.example", "models": [{"id": "m"}],
        "protocol": "anthropic", "key_ref": "shell:claude",
        "coding_plan": True}, ensure_ascii=False), request_id="cp-key")
    r = client.post("/api/setup/land", json={"cards": [{
        "shell": "codex", "source": "key", "key_name": "cp-key",
        "protocol": "openai_chat", "base_url": "https://cp.example",
        "key_value": "sk-cp", "model": "m", "role": "审查"}]})
    assert r.status_code == 400
    assert "CodingPlan 跨壳" in r.json()["error"]


def test_identical_card_post_is_idempotent(client):
    """同牌重复 POST 只按数量补差,不追加同配置实例。"""
    client.post("/api/setup/controller", json={
        "shell": "claude", "source": "builtin"})
    card = {"shell": "codex", "source": "key", "provider": "deepseek",
            "protocol": "openai_chat",
            "base_url": "https://api.deepseek.com/v1",
            "key_value": "sk-idem", "model": "deepseek-chat",
            "role": "实时"}
    first = client.post("/api/setup/land", json={"cards": [
        {**card, "count": 2}]})
    second = client.post("/api/setup/land", json={"cards": [
        {**card, "count": 2}]})
    assert first.json()["registered"] == ["实时1", "实时2"]
    assert second.json()["registered"] == []
    c = connect()
    n = c.execute(
        "SELECT COUNT(*) n FROM instances WHERE is_active=1 AND shell='codex'"
    ).fetchone()["n"]
    c.close()
    assert n == 2


def test_scanned_shells_have_ctrl_session_flag(client):
    """scanned 壳列表带 ctrl_session 标记: claude/kimi=True, codex/dsh=False。"""
    s = client.get("/api/setup/state").json()
    by_name = {x["name"]: x for x in s["scanned"]}
    assert by_name["claude"]["ctrl_session"] is True
    assert by_name["codex"]["ctrl_session"] is False
    assert by_name["dsh"]["ctrl_session"] is False
    # kimi 在 mock 里没出现;直接查 SHELL_ENTRY_DEFAULTS
    assert wizard.SHELL_ENTRY_DEFAULTS["kimi"].get("ctrl_session") is not None


def test_shell_without_ctrl_session_rejected_as_controller(client):
    """无 ctrl_session 块的壳(如 codex)选作总控 → 400 拒绝,不静默兜底。"""
    r = client.post("/api/setup/controller", json={
        "shell": "codex", "source": "builtin", "model": "test-model"})
    assert r.status_code == 400
    assert "暂不支持作总控" in r.json()["error"]


def test_claude_settings_has_explicit_ctrl_session(home):
    """claude settings 显式声明 ctrl_session: stream-json(不依赖旧账本兜底)。"""
    from tianji import wizard as wz
    import os
    wz._write_controller_settings(
        home_p=home, home=str(home), shell="claude",
        secret="s")
    doc = json.loads((injected_dir() / "settings-controller.json").read_text(
        encoding="utf-8"))
    assert "ctrl_session" in doc
    assert doc["ctrl_session"]["protocol"] == "stream-json"
    assert doc["ctrl_session"]["launch"] == ["claude"]


def test_generic_settings_carries_ctrl_session_when_present(home):
    """kimi settings: ctrl_session 块(acp/启动器/data_root_env/角色话术)。"""
    from tianji import wizard as wz
    wz._write_controller_settings(
        home_p=home, home=str(home), shell="kimi",
        secret="s")
    doc = json.loads((injected_dir() / "settings-controller.json").read_text(
        encoding="utf-8"))
    assert doc["ctrl_session"]["protocol"] == "acp"
    assert doc["ctrl_session"]["launch"] == ["kimi", "acp"]


def test_land_with_pool_no_crash_when_all_exist(client, home):
    """票59 归池 NameError: count<=have(无需新建实例)时 pool_add_member 不抛 NameError。"""
    from tianji import pool as pool_mod
    client.post("/api/setup/controller", json={
        "shell": "claude", "source": "builtin"})
    ident = {"worker_id": "总控",
             "secret": (injected_dir() / "ctrl-secret.txt").read_text(
                 encoding="utf-8").strip()}
    conn = connect()
    pool_mod.pool_create(conn, ident, "wp01", members=[], request_id="p1")
    conn.close()
    r = client.post("/api/setup/land", json={"cards": [
        {"shell": "codex", "source": "key", "provider": "deepseek",
         "protocol": "openai_chat",
         "base_url": "https://api.deepseek.com/v1",
         "key_value": "sk-wp", "model": "deepseek-chat",
         "role": "审核", "count": 2, "pool": "wp01"}]})
    assert r.status_code == 200, r.text
    assert r.json()["registered"] == ["审核1", "审核2"]
    r2 = client.post("/api/setup/land", json={"cards": [
        {"shell": "codex", "source": "key", "provider": "deepseek",
         "protocol": "openai_chat",
         "base_url": "https://api.deepseek.com/v1",
         "key_value": "sk-wp", "model": "deepseek-chat",
         "role": "审核", "count": 2, "pool": "wp01"}]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["registered"] == []
