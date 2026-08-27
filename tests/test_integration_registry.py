import json
import pytest
from tianji import integrations, ops, wizard
from tianji.integrations import (
    add_provider_model,
    discover_models,
    ensure_builtin_registry,
    migrate_legacy_entries,
    register_credential,
    register_custom_provider,
    registry_state,
)


def _config(conn, key):
    row = conn.execute("SELECT value FROM configs WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def test_builtin_provider_and_protocol_registry(conn, controller):
    ensure_builtin_registry(conn, controller, request_id="builtin")
    provider = _config(conn, "integration_provider:kimi")
    assert provider["base_url"] == "https://api.kimi.com/coding/"
    assert provider["protocol"] == "anthropic"
    assert provider["auth_style"] == "bearer"
    action = 'integration_ensure'
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action=?", (action,),
    ).fetchone()


def test_custom_provider_requires_minimum_fields(conn, controller):
    with pytest.raises(ValueError):
        register_custom_provider(
            conn, controller, "custom", base_url="", protocol="openai_chat",
            key_ref="k.txt", request_id="bad")
    result = register_custom_provider(
        conn, controller, "my-relay", base_url="https://relay.example/v1",
        protocol="openai_chat", key_ref="k.txt", request_id="custom")
    cfg = _config(conn, "integration_provider:my-relay")
    assert result["name"] == "my-relay"
    assert cfg["builtin"] is False


def test_legacy_entries_migrate_once(conn, controller):
    ops.config_set(conn, controller, "shell:kimi", json.dumps(
        wizard.SHELL_ENTRY_DEFAULTS["kimi"], ensure_ascii=False),
        request_id="legacy-shell")
    ops.config_set(conn, controller, "key:old-kimi", json.dumps({
        "base_url": "https://api.kimi.com/coding/", "models": [{"id": "m"}],
        "protocol": "anthropic", "key_ref": "old.txt", "coding_plan": False
    }, ensure_ascii=False), request_id="legacy-key")
    result = migrate_legacy_entries(conn, controller, request_id="migrate")
    assert result["providers"] >= 1 and result["shells"] >= 1
    shell = _config(conn, "integration_shell:kimi")
    provider = _config(conn, "integration_provider:kimi")
    assert shell["source"] == "legacy"
    assert provider["credential_key"] == "old-kimi"
    replay = migrate_legacy_entries(conn, controller, request_id="migrate")
    assert replay == {"providers": 1, "shells": 1, "replay": True}


def test_add_instance_requires_explicit_registry(conn, controller):
    with pytest.raises(ValueError, match='集成注册表'):
        wizard.add_instance(
            conn, controller, "现场工", "claude", "m", key_name="missing",
            base_url="https://api.example.com", confirm=True,
            request_id="runtime-assemble")


def test_registry_state_reports_migration(conn, controller):
    ops.config_set(conn, controller, "key:legacy", json.dumps({
        "base_url": "https://api.kimi.com/coding/",
        "models": [{"id": "m"}], "protocol": "anthropic"
    }, ensure_ascii=False), request_id="state-key")
    state = registry_state(conn)
    assert any(row["legacy"] == "key:legacy" for row in state["migrations"])


def test_register_credential_requires_provider_and_ref(conn, controller):
    """credential 只存 provider+key 文件引用;指向未登记供应商/缺引用=拒绝。"""
    ensure_builtin_registry(conn, controller, request_id="builtin")
    with pytest.raises(ValueError, match="未登记"):
        register_credential(conn, controller, "c1", "ghost",
                            key_ref="k.txt", request_id="bad-provider")
    with pytest.raises(ValueError, match="key_ref"):
        register_credential(conn, controller, "c1", "kimi",
                            key_ref="", request_id="bad-ref")
    r = register_credential(conn, controller, "主key", "kimi",
                            key_ref="keys/主key.key", request_id="cred-1")
    assert r["key"] == "credential:主key"
    assert _config(conn, "credential:主key") == {
        "provider": "kimi", "key_ref": "keys/主key.key"}


def test_add_instance_bridges_legacy_style_call(conn, controller, tmp_path):
    """兼容桥(13.8): 旧四步参数建实例→集成条目显式增量登记,可复用不一次性。"""
    key_file = tmp_path / "k.txt"
    key_file.write_text("sk-bridge", encoding="utf-8")
    wizard.add_instance(
        conn, controller, "桥工", "claude", "m1",
        key_name="桥key", base_url="https://api.example.com/anthropic",
        protocol="anthropic", key_ref=str(key_file),
        isolated_dir=str(tmp_path / "i"), binary="python",
        confirm=True, request_id="bridge-1")
    shell = _config(conn, "integration_shell:claude")
    provider = _config(conn, "integration_provider:桥key")
    credential = _config(conn, "credential:桥key")
    assert shell and shell["source"] == "legacy"
    assert credential == {"provider": "桥key", "key_ref": str(key_file)}
    assert provider["credential_key"] == "桥key"
    assert provider["protocol"] == "anthropic"
    # 旧命名空间条目保留作迁移源与读取兼容(13.8)
    assert conn.execute(
        "SELECT 1 FROM configs WHERE key='key:桥key'").fetchone()
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='integration_bridge'").fetchone()


def test_discover_models_caches_with_source(conn, controller, monkeypatch):
    """票33: 多端点×双认证头发现成功→模型清单+来源+时间写进供应商条目。"""
    ensure_builtin_registry(conn, controller, request_id="builtin")
    seen = {}

    def fake_http(url, headers, timeout=10):
        seen["url"], seen["auth"] = url, headers
        return {"data": [{"id": "k3"}, {"id": "k4"}]}

    monkeypatch.setattr(integrations, "_http_json", fake_http)
    r = discover_models(conn, controller, provider="kimi",
                        key_value="sk-live-1")
    assert r["ok"] is True and r["cached"] is True
    assert r["models"] == ["k3", "k4"]
    # anthropic 协议: 端点候选第一个是 /v1/models,bearer 先试
    assert seen["url"] == "https://api.kimi.com/coding/v1/models"
    assert seen["auth"].get("Authorization") == "Bearer sk-live-1"
    entry = _config(conn, "integration_provider:kimi")
    assert [m["id"] for m in entry["models"]] == ["k3", "k4"]
    assert entry["discovered_at"] > 0
    assert entry["discovery_source"] == r["source"]
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='integration_discover'").fetchone()


def test_discover_models_fallback_path_and_auth_header(
        conn, controller, monkeypatch):
    """票33: /v1/models 撞 404 时落到下一候选;x-api-key 认证头也轮到。"""
    ops.config_set(conn, controller, "integration_provider:relay", json.dumps({
        "display": "relay", "base_url": "https://relay.example",
        "protocol": "openai_chat", "auth_style": "bearer",
        "category": "自定义", "builtin": False, "models": [],
        "discovered_at": 0,
        "model_discovery_paths": integrations.PROTOCOLS["openai_chat"]
        ["model_discovery_paths"],
        "key_ref": None, "credential_key": None}, ensure_ascii=False),
        request_id="relay")
    calls = []

    def fake_http(url, headers, timeout=10):
        calls.append((url, dict(headers)))
        if "/models" not in url or url.count("/") > 3:
            raise OSError("404")
        if "x-api-key" in headers:
            return {"data": [{"id": "mx"}]}
        raise OSError("401")

    monkeypatch.setattr(integrations, "_http_json", fake_http)
    r = discover_models(conn, controller, provider="relay",
                        key_value="sk-x")
    assert r["ok"] is True
    assert any("x-api-key" in h for _, h in calls)
    assert r["source"].startswith("GET ")


def test_discover_models_failure_no_write_and_reason(
        conn, controller, monkeypatch):
    """票33: 全部候选失败→返回原因、条目缓存不被污染。"""
    ensure_builtin_registry(conn, controller, request_id="builtin")

    def fake_http(url, headers, timeout=10):
        raise OSError("network down")

    monkeypatch.setattr(integrations, "_http_json", fake_http)
    before = _config(conn, "integration_provider:kimi")
    r = discover_models(conn, controller, provider="kimi",
                        key_value="sk-1")
    assert r["ok"] is False and "全部候选探测失败" in r["reason"]
    assert r["attempts"], "失败原因要带每组尝试"
    assert _config(conn, "integration_provider:kimi") == before


def test_discover_models_adhoc_custom_url(monkeypatch, conn, controller):
    """票33: 未登记的自定义 OpenAI 兼容服务给 base_url+协议也能保底发现。"""

    def fake_http(url, headers, timeout=10):
        return {"data": [{"id": "deepseek-v4-flash"}]}

    monkeypatch.setattr(integrations, "_http_json", fake_http)
    r = discover_models(conn, controller, base_url="https://mid.example/v1",
                        protocol="openai_chat", key_value="sk-m")
    assert r["ok"] is True and r["cached"] is False


def test_discover_models_key_from_credential_ref(
        conn, controller, tmp_path, monkeypatch):
    """票33: key 从 credential 引用的文件现读;明文不落账本。"""
    kfile = tmp_path / "k.key"
    kfile.write_text("sk-from-file", encoding="utf-8")
    ensure_builtin_registry(conn, controller, request_id="builtin")
    register_credential(conn, controller, "主key", "kimi",
                        key_ref=str(kfile), request_id="cred")
    seen = {}

    def fake_http(url, headers, timeout=10):
        seen["headers"] = headers
        return {"data": [{"id": "m"}]}

    monkeypatch.setattr(integrations, "_http_json", fake_http)
    r = discover_models(conn, controller, provider="kimi",
                        credential="主key")
    assert r["ok"] is True
    assert seen["headers"]["Authorization"] == "Bearer sk-from-file"


def test_discover_models_captures_context_window(conn, controller,
                                                 monkeypatch):
    """票48(13.1): 探测可得→上下文窗口字段进缓存;探测不到→标'待实测'。

    假象消除证明: 之前模型条目只有 {"id":...},14.2/9.2 拿不到窗口,
    现在 discover 响应里的 context_window/context_length 会被带出。
    """
    ensure_builtin_registry(conn, controller, request_id="builtin")

    def fake_http(url, headers, timeout=10):
        return {"data": [{"id": "big", "context_window": 128000},
                         {"id": "ctxlen", "context_length": "64000"},
                         {"id": "plain"}]}

    monkeypatch.setattr(integrations, "_http_json", fake_http)
    r = discover_models(conn, controller, provider="kimi",
                        key_value="sk-live-1")
    assert r["ok"] is True
    assert r["models"] == ["big", "ctxlen", "plain"]  # 对外仍返回纯 id 清单
    entry = _config(conn, "integration_provider:kimi")
    by = {m["id"]: m for m in entry["models"]}
    assert by["big"]["context_window"] == 128000
    assert "context_window_status" not in by["big"]  # 探测到=实测值,无待实测标
    assert by["ctxlen"]["context_window"] == 64000  # 字符串数字也认
    assert by["plain"]["context_window"] is None
    assert by["plain"]["context_window_status"] == "待实测"


def test_manual_model_add_entry_has_window_field(conn, controller):
    """票48(13.1): 人工补录模型同样带 context_window 字段(无探测值→待实测)。"""
    ensure_builtin_registry(conn, controller, request_id="builtin")
    add_provider_model(conn, controller, "kimi", "kimi-manual-1",
                       request_id="ma-1")
    entry = _config(conn, "integration_provider:kimi")
    manual = [m for m in entry["models"] if m["id"] == "kimi-manual-1"][0]
    assert manual["pending_test"] is True
    assert manual["context_window"] is None
    assert manual["context_window_status"] == "待实测"


def test_manual_model_add_marks_pending_test(conn, controller):
    """票13.8: 人工补录模型标'待实测';重复补录拒绝。"""
    ensure_builtin_registry(conn, controller, request_id="builtin")
    add_provider_model(conn, controller, "kimi", "kimi-manual-1",
                       request_id="ma-1")
    entry = _config(conn, "integration_provider:kimi")
    manual = [m for m in entry["models"] if m["id"] == "kimi-manual-1"]
    assert manual and manual[0]["pending_test"] is True
    with pytest.raises(ValueError, match="已在"):
        add_provider_model(conn, controller, "kimi", "kimi-manual-1",
                           request_id="ma-2")
