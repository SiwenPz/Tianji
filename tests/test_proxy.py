"""票 56: 号池 proxy 模块测试。

覆盖: 熔断器生命周期 / PoolRouter 路由 / HTTP handler 路径解析 /
      透明重试(429→换牌) / 流中断不重试 / 令牌校验 / 端到端转发。
"""

import json
import socket
import sqlite3
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import connect, tianji_home
from tianji.daemon import daemon_start, daemon_stop, daemon_status
from tianji.integrations import register_custom_provider, register_credential
from tianji.proxy import CircuitBreaker, PoolRouter, _ForwardError, _verify_token, _pool_json, _cfg
from tianji.pool import pool_create, pool_add_member, pool_rotate_token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tproxy_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TIANJI_HOME", str(home))
    return home


@pytest.fixture
def pconn(tproxy_home):
    c = connect()
    ops.ensure_defaults(c)
    yield c
    c.close()


@pytest.fixture
def controller(pconn):
    r = ops.instance_register(
        pconn, "总控", "claude", "deepseek-v4-flash", controller=True)
    return {"worker_id": "总控", "secret": r["secret"]}


@pytest.fixture
def pctx(pconn, controller, tproxy_home):
    """预置: 供应商 + credential + 池,返回 dict。"""
    from tianji import integrations
    prov = "testprov"
    register_custom_provider(
        pconn, controller, prov,
        base_url="http://127.0.0.1:19999",
        protocol="openai_chat",
        auth_style="bearer",
        request_id="register-prov-1")
    # 模型
    from tianji.integrations import model_entry
    entry = integrations._config(
        pconn, "integration_provider:" + prov)
    entry["models"] = [model_entry({"id": "test-model"})]
    pconn.execute(
        "UPDATE configs SET value=? WHERE key=?",
        (json.dumps(entry, ensure_ascii=False),
         "integration_provider:" + prov))
    # 凭据 + key 文件
    cred_name = "testcred"
    key_dir = tproxy_home / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file = key_dir / "test.key"
    key_file.write_text("test-api-key-12345", encoding="utf-8")
    register_credential(
        pconn, controller, cred_name,
        provider=prov,
        key_ref=str(key_file),
        request_id="register-cred-1")
    # 池
    pool_name = "test-pool"
    pool_create(pconn, controller, pool_name,
                members=[cred_name],
                request_id="create-pool-1")
    return {"conn": pconn, "controller": controller, "pool_name": pool_name,
            "key_file": key_file, "prov": prov, "cred_name": cred_name}


# ---------------------------------------------------------------------------
# 熔断器单测
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_closed_initially(self):
        cb = CircuitBreaker()
        assert cb.state == "closed"
        assert cb.allow()

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(min_samples=3)
        for _ in range(3):
            cb.record_failure()
        assert cb._failure_rate() >= 0.7
        # 访问 state 触发检测(时间未过,保持 open)
        assert cb.state == "open"
        assert not cb.allow()

    def test_no_trip_below_min_samples(self):
        cb = CircuitBreaker(min_samples=15, error_threshold=0.7)
        for _ in range(14):
            cb.record_failure()
        assert cb.state == "closed"
        assert cb.allow()

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker(min_samples=2, open_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.15)
        assert cb.state == "half_open"
        assert cb.allow()

    def test_half_open_recovery(self):
        cb = CircuitBreaker(min_samples=2, open_seconds=0.1,
                            half_open_need=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.15)
        for _ in range(3):
            cb.record_success()
        assert cb.state == "closed"
        assert cb.allow()

    def test_half_open_fail_returns_to_open(self):
        cb = CircuitBreaker(min_samples=2, open_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.15)
        assert cb.state == "half_open"
        cb.record_failure()
        assert cb.state == "open"
        assert not cb.allow()

    def test_success_does_not_overflow(self):
        cb = CircuitBreaker(min_samples=5)
        for _ in range(100):
            cb.record_success()
        assert len(cb._window) <= 5

    def test_from_dict_roundtrip(self):
        cb = CircuitBreaker(min_samples=3)
        cb.record_failure()
        cb.record_failure()
        d = cb.to_dict()
        cb2 = CircuitBreaker.from_dict(d, min_samples=3)
        assert cb2._state == cb._state
        assert cb2._window == cb._window


# ---------------------------------------------------------------------------
# PoolRouter 单测
# ---------------------------------------------------------------------------

class TestPoolRouter:
    def test_pick_round_robin(self, pctx):
        # 加第二个成员,同一供应商
        register_credential(
            pctx["conn"], pctx["controller"], "testcred2",
            provider=pctx["prov"],
            key_ref=str(pctx["key_file"]),
            request_id="reg-cred-2")
        pool_add_member(pctx["conn"], pctx["controller"], pctx["pool_name"],
                        "testcred2", request_id="add-mem-2")
        conn = pctx["conn"]
        router = PoolRouter(conn)
        m1, _, _ = router.pick(pctx["pool_name"], "test-model", "openai_chat")
        m2, _, _ = router.pick(pctx["pool_name"], "test-model", "openai_chat")
        assert m1 != m2
        # 两个成员
        assert {m1, m2} == {"testcred", "testcred2"}

    def test_filter_by_model(self, pctx):
        # 不同模型供应商
        from tianji import integrations
        prov2 = "deepseek-prov"
        register_custom_provider(
            pctx["conn"], pctx["controller"], prov2,
            base_url="http://127.0.0.1:19998",
            protocol="openai_chat",
            auth_style="bearer",
            request_id="reg-prov-2")
        from tianji.integrations import model_entry
        entry2 = integrations._config(
            pctx["conn"], "integration_provider:" + prov2)
        entry2["models"] = [model_entry({"id": "deepseek-model"})]
        pctx["conn"].execute(
            "UPDATE configs SET value=? WHERE key=?",
            (json.dumps(entry2, ensure_ascii=False),
             "integration_provider:" + prov2))
        cred2 = "deepseek-cred"
        register_credential(
            pctx["conn"], pctx["controller"], cred2,
            provider=prov2,
            key_ref=str(pctx["key_file"]),
            request_id="reg-cred-ds")
        pool_add_member(pctx["conn"], pctx["controller"], pctx["pool_name"],
                        cred2, request_id="add-mem-ds")
        router = PoolRouter(pctx["conn"])
        m, _, _ = router.pick(pctx["pool_name"], "test-model", "openai_chat")
        assert m == "testcred"
        m2, _, _ = router.pick(pctx["pool_name"], "deepseek-model",
                                "openai_chat")
        assert m2 == "deepseek-cred"

    def test_skip_circuit_broken(self, pctx):
        router = PoolRouter(pctx["conn"])
        cb = CircuitBreaker(min_samples=2)
        cb.record_failure()
        cb.record_failure()
        router._breakers["testcred"] = cb
        m, _, _ = router.pick(pctx["pool_name"], "test-model", "openai_chat")
        assert m is None

    def test_persist_circuit(self, pctx):
        router = PoolRouter(pctx["conn"])
        cb = CircuitBreaker(min_samples=2)
        cb.record_failure()
        cb.record_failure()
        router._breakers["testcred"] = cb
        router._persist_breakers(pctx["pool_name"])
        from tianji.proxy import _pool_json
        pool = _pool_json(pctx["conn"], pctx["pool_name"])
        assert pool["circuit"]["testcred"]["state"] == "open"


# ---------------------------------------------------------------------------
# HTTP handler 单元测
# ---------------------------------------------------------------------------

class _FakeRouter:
    """模拟 PoolRouter。"""
    def __init__(self, responses):
        self._r = list(responses)
        self.calls = []

    def pick(self, pool_name, model, proto):
        self.calls.append((pool_name, model, proto))
        if self._r:
            return self._r.pop(0)
        return None, None, None

    def record(self, pool_name, member, success):
        pass

    @staticmethod
    def always(member_name, cred, prov):
        """构造永远返回同一成员的 router(用于重试测)。"""
        router = _FakeRouter([])
        router.pick = lambda *a, **kw: (member_name, cred, prov)
        return router


_ProxyHandlerCls = None
try:
    from tianji.proxy import _ProxyHandler as _ProxyHandlerCls
except ImportError:
    pass


class _FakeHandler(BaseHTTPRequestHandler):
    """Passthrough handler using a fake router。"""
    router = None
    client_address = ("127.0.0.1", 12345)
    server = type("S", (), {"server_version": "test"})()

    def log_message(self, *a, **kw):
        pass

    # Inherit _do_route, _route, _send_resp, _send_json from real handler
    if _ProxyHandlerCls is not None:
        _do_route = _ProxyHandlerCls._do_route
        _route = _ProxyHandlerCls._route
        _send_resp = _ProxyHandlerCls._send_resp
        _send_json = _ProxyHandlerCls._send_json


class _FakeHandlerFactory:
    """根据给定 router 实例构造一个 handler 类。"""
    @staticmethod
    def make(router, max_retries=5):
        cls_name = "FakeHandler_{}".format(id(router))
        attrs = {
            "router": router,
            "max_retries": max_retries,
            "timeout_first_byte": 90,
            "timeout_stream_idle": 180,
            "timeout_total": 600,
            "server_version": "test",
            "client_address": ("127.0.0.1", 0),
        }
        cls = type(cls_name, (_FakeHandler,), attrs)
        return cls


def _make_handler(router, max_retries=5, method="POST",
                  path="/proxy/test-pool/v1/chat/completions?token=abc",
                  headers=None, body=b'{"model":"test-model"}'):
    cls = _FakeHandlerFactory.make(router, max_retries)
    h = cls.__new__(cls)
    h.path = path
    h.command = method
    h.request_version = "HTTP/1.1"
    h.headers = _FakeHeaders(headers or {})
    h.wfile = _FakeWfile()
    h.rfile = _FakeRfile(body)
    h.responses = {}
    h.send_response = lambda *a: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h.client_address = ("127.0.0.1", 0)
    h.server = type("S", (), {"server_version": "test"})()
    return h


class _FakeHeaders:
    def __init__(self, d):
        self._d = d

    def get(self, k, d=""):
        return self._d.get(k, d)

    def items(self):
        return self._d.items()


class _FakeRfile:
    def __init__(self, data):
        self._d = data
        self._p = 0

    def read(self, n):
        r = self._d[self._p:self._p + n]
        self._p += len(r)
        return r


class _FakeWfile:
    def __init__(self):
        self._out = bytearray()
        self._hdrs = []

    def write(self, b):
        self._out.extend(b)

    def flush(self):
        pass

    @property
    def content(self):
        return bytes(self._out)


class TestProxyHandler:
    def test_bad_path(self):
        router = _FakeRouter([])
        h = _make_handler(router, path="/wrong/path")
        h._do_route("POST")
        content = h.wfile.content.decode()
        assert "bad_path" in content

    def test_no_proxy_prefix(self):
        router = _FakeRouter([])
        h = _make_handler(router, path="/api/v1/chat")
        h._do_route("POST")
        content = h.wfile.content.decode()
        assert "bad_path" in content

    def test_retry_on_429_then_success(self, pctx):
        call_count = [0]
        # 读真实池令牌( pool_create 随机生成,必须匹配才能过验票)
        pool_token = pctx["conn"].execute(
            "SELECT value FROM configs WHERE key=?",
            ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

        def _mock_fwd(method, url, headers, body, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise _ForwardError("upstream_429", detail="rate_limited",
                                    stream_broken=False)
            return 200, {"Content-Type": "application/json"}, b'{"ok":true}'


        from tianji.proxy import _pool as pool_mod
        orig_fwd = pool_mod._forward_http
        pool_mod._forward_http = _mock_fwd
        try:
            router = _FakeRouter.always(
                "testcred",
                {"key_ref": str(pctx["key_file"]), "provider": "testprov"},
                {"base_url": "http://127.0.0.1:9999", "protocol": "openai_chat",
                 "auth_style": "bearer", "models": [{"id": "test-model"}]})
            h = _make_handler(router,
                              path=f"/proxy/{pctx['pool_name']}/v1/chat/completions?token={pool_token}")
            h._do_route("POST")
            assert call_count[0] == 2  # 第一次 429 重试,第二次成功
        finally:
            pool_mod._forward_http = orig_fwd

    def test_stream_broken_no_retry(self, pctx):
        call_count = [0]
        pool_token = pctx["conn"].execute(
            "SELECT value FROM configs WHERE key=?",
            ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

        def _mock_fwd(method, url, headers, body, *a, **kw):
            call_count[0] += 1
            raise _ForwardError("stream_interrupted", detail="connection_reset",
                                stream_broken=True)

        from tianji.proxy import _pool as pool_mod
        orig_fwd = pool_mod._forward_http
        pool_mod._forward_http = _mock_fwd
        try:
            router = _FakeRouter.always(
                "testcred",
                {"key_ref": str(pctx["key_file"]), "provider": "testprov"},
                {"base_url": "http://127.0.0.1:9999", "protocol": "openai_chat",
                 "models": [{"id": "test-model"}]})
            h = _make_handler(router,
                              path=f"/proxy/{pctx['pool_name']}/v1/chat?token={pool_token}")
            h._do_route("POST")
            assert call_count[0] == 1  # 流中断不重试
        finally:
            pool_mod._forward_http = orig_fwd


# ---------------------------------------------------------------------------
# Token 校验
# ---------------------------------------------------------------------------

class TestTokenVerify:
    def test_no_token_entry_allows_any(self, pconn):
        # 无令牌条目的池 → 放行任意 token(历史兼容)
        assert _verify_token(pconn, "nonexistent-pool", "xxx") is True
        assert _verify_token(pconn, "nonexistent-pool", "") is True

    def test_with_token_entry_requires_token(self, pconn, pctx):
        # rotate 后更新令牌
        pool_rotate_token(pconn, pctx["controller"],
                              pctx["pool_name"],
                              request_id="rot-1")
        stored = pconn.execute(
            "SELECT value FROM configs WHERE key=?",
            ("pool:token:" + pctx["pool_name"],)
        ).fetchone()["value"]
        assert _verify_token(pconn, pctx["pool_name"], stored) is True
        assert _verify_token(pconn, pctx["pool_name"], "wrong-token") is False
        assert _verify_token(pconn, pctx["pool_name"], "") is False


# ---------------------------------------------------------------------------
# Daemon proxy 生命周期集成
# ---------------------------------------------------------------------------

class TestDaemonProxyLifecycle:
    def test_daemon_includes_proxy(self, tproxy_home):
        r = daemon_start(interval=1, web_port=8811)
        assert r["ok"] is True
        assert r["proxy_port"] > 0
        try:
            import time as _time
            deadline = _time.time() + 12
            while _time.time() < deadline:
                st = daemon_status()
                if st.get("proxy_alive"):
                    break
                _time.sleep(0.3)
            assert daemon_status()["proxy_alive"] is True
            assert daemon_status()["proxy_pid"] > 0
        finally:
            daemon_stop()
        st = daemon_status()
        assert st.get("proxy_alive") is False
        assert st.get("proxy_pid") == 0

    def test_daemon_stop_clears_proxy_config(self, tproxy_home):
        r = daemon_start(interval=1, web_port=8812)
        assert r["ok"] is True
        try:
            import time as _time
            deadline = _time.time() + 12
            while _time.time() < deadline:
                if daemon_status().get("proxy_alive"):
                    break
                _time.sleep(0.3)
            daemon_stop()
        finally:
            daemon_stop()
        conn = connect()
        row = conn.execute(
            "SELECT key FROM configs WHERE key LIKE 'daemon.%'").fetchall()
        conn.close()
        assert row == []

    def test_proxy_crash_auto_relaunch(self, tproxy_home):
        r = daemon_start(interval=1, web_port=8813)
        assert r["ok"] is True
        try:
            import time as _time
            deadline = _time.time() + 12
            while _time.time() < deadline:
                if daemon_status().get("proxy_alive"):
                    break
                _time.sleep(0.3)
            old_pid = daemon_status()["proxy_pid"]
            from tianji.daemon import _kill_pid
            _kill_pid(old_pid)

            deadline2 = _time.time() + 15
            while _time.time() < deadline2:
                st = daemon_status()
                if st.get("proxy_alive") and st["proxy_pid"] != old_pid:
                    break
                _time.sleep(0.3)
            st = daemon_status()
            assert st["proxy_pid"] != old_pid
            assert st["proxy_pid"] > 0
        finally:
            daemon_stop()


# ---------------------------------------------------------------------------
# 端到端: proxy 监听 + HTTP 转发(假后端)
# ---------------------------------------------------------------------------

class _BackendHandler(BaseHTTPRequestHandler):
    """模拟 upstream provider。"""
    backend_counter = 0  # class-level counter

    def do_POST(self):
        _BackendHandler.backend_counter += 1
        if _BackendHandler.backend_counter <= 2:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"error": "rate_limit"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"result": "ok"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


class TestProxyE2E:
    def test_retry_429_then_200(self, pctx, controller, tmp_path):
        # 起假后端
        backend = HTTPServer(("127.0.0.1", 0), _BackendHandler)
        backend_port = backend.server_address[1]
        _BackendHandler.backend_counter = 0
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        try:
            # 更新供应商 base_url 指向假后端
            from tianji import integrations as _intg
            prov_entry = _intg._config(
                pctx["conn"], "integration_provider:" + pctx["prov"])
            prov_entry["base_url"] = "http://127.0.0.1:{}".format(backend_port)
            pctx["conn"].execute(
                "UPDATE configs SET value=? WHERE key=?",
                (json.dumps(prov_entry, ensure_ascii=False),
                 "integration_provider:" + pctx["prov"]))
            # 起 proxy
            from tianji.proxy import run_proxy
            proxy_port = 19001
            pt = threading.Thread(
                target=run_proxy, args=(proxy_port,), daemon=True)
            pt.start()
            time.sleep(0.5)

            # 发请求(给 2 次 429 后转 200)
            import urllib.request
            pool_token = pctx["conn"].execute(
                "SELECT value FROM configs WHERE key=?",
                ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]
            body = json.dumps({"model": "test-model"}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:{}/proxy/{}/v1/chat/completions".format(
                    proxy_port, pctx["pool_name"]) + "?token=" + pool_token,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            assert resp.status == 200
            # 应该经历了 3 次 upstream 调用(2 次 429 + 1 次 200)
            assert _BackendHandler.backend_counter >= 3
        finally:
            backend.shutdown()
            pt.join(timeout=2)
