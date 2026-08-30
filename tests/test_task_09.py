"""Task-09: 池日志字段真值 E2E + circuit_state 真实链路 + 保留期清理。"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import connect, now
from tianji.proxy._pool import PoolRouter, _update_member_health, run_proxy
from tianji.integrations import (
    register_custom_provider, register_credential, model_entry)
from tianji.pool import pool_create, pool_add_member
from tianji.monitor import _purge_pool_logs


# ---------------------------------------------------------------------------
# 辅助: 预置供应商+凭据+池
# ---------------------------------------------------------------------------

def _mk_provider(conn, ident, name, base_url, protocol="openai_chat",
                 models=("test-model",), request_id="t9-prov"):
    register_custom_provider(
        conn, ident, name, base_url=base_url, protocol=protocol,
        auth_style="bearer", request_id=request_id)
    entry = ops._config(conn, "integration_provider:" + name)
    if isinstance(entry, str):
        entry = json.loads(entry)
    entry["models"] = [model_entry({"id": m}) for m in models]
    conn.execute(
        "UPDATE configs SET value=? WHERE key=?",
        (json.dumps(entry, ensure_ascii=False),
         "integration_provider:" + name))


def _mk_credential(conn, ident, name, provider, key_ref, request_id="t9-cred"):
    register_credential(conn, ident, name, provider,
                        key_ref=key_ref, request_id=request_id)


def _pool_token(conn, pool_name):
    row = conn.execute(
        "SELECT value FROM configs WHERE key=?",
        ("pool:token:" + pool_name,)).fetchone()
    assert row, "池令牌应已生成"
    return row["value"]


def _wait_log_row(conn, pool_name, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = conn.execute(
            "SELECT * FROM pool_request_logs WHERE pool_name=?",
            (pool_name,)).fetchall()
        if rows:
            return rows
        time.sleep(0.1)
    return []


# ---- E2E: proxy 端口 → pool_request_logs(不直接调 _log_request) ----

class _BackendOK(BaseHTTPRequestHandler):
    """非流式 openai_chat 正常后端。"""
    def do_POST(self):
        body = json.dumps({
            "id": "cmpl-e2e", "object": "chat.completion",
            "created": 1234567890, "model": "real-backend-model",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "e2e log test"},
                "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                      "total_tokens": 8},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


class _BackendSSE(BaseHTTPRequestHandler):
    """SSE 流式后端: 两块 + [DONE]。"""
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for c in (b"data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n",
                  b"data: [DONE]\n\n"):
            self.wfile.write(c)
            self.wfile.flush()
            time.sleep(0.05)
        self.close_connection = True

    def log_message(self, *a, **kw):
        pass


class _Backend500(BaseHTTPRequestHandler):
    """恒 500 后端。"""
    def do_POST(self):
        body = b'{"error":"boom"}'
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


def _start_backend(handler_cls):
    backend = HTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=backend.serve_forever, daemon=True)
    t.start()
    return backend, backend.server_address[1]


def _start_proxy(port):
    pt = threading.Thread(target=run_proxy, args=(port,), daemon=True)
    pt.start()
    time.sleep(0.3)
    return pt


def _post(pool_name, port, token, body_dict, accept="application/json"):
    url = (f"http://127.0.0.1:{port}/proxy/{pool_name}"
           f"/v1/chat/completions?token={token}")
    req = urllib.request.Request(
        url, data=json.dumps(body_dict).encode(),
        headers={"Content-Type": "application/json", "Accept": accept},
        method="POST")
    return urllib.request.urlopen(req, timeout=10)


class TestProxyE2EFieldLog:
    """通过代理端口发请求,审计端(泳道日志)对账。"""

    def test_proxy_request_logs_to_pool_request_logs(
            self, conn, controller, tmp_path):
        """非流式代理请求 → pool_request_logs 落行,model=实际出活成员名。"""
        backend, backend_port = _start_backend(_BackendOK)
        pt = None
        try:
            key_ref = str(tmp_path / "k.key")
            Path(key_ref).write_text("upstream-key-42", encoding="utf-8")
            _mk_provider(conn, controller, "t9-prov",
                         base_url=f"http://127.0.0.1:{backend_port}")
            _mk_credential(conn, controller, "t9-cred", "t9-prov", key_ref)
            pool_create(conn, controller, "t9-test-pool",
                        members=["t9-cred"], request_id="t9-pool")
            token = _pool_token(conn, "t9-test-pool")

            pt = _start_proxy(19050)
            resp = _post("t9-test-pool", 19050, token,
                         {"model": "test-model",
                          "messages": [{"role": "user", "content": "hi"}]})
            assert resp.status == 200

            logs = _wait_log_row(conn, "t9-test-pool")
            assert logs, "pool_request_logs 应落行(代理端口端到端)"
            row = logs[0]
            assert row["status_code"] == 200
            assert row["request_model"] == "test-model"
            # model 必须等于实际出活成员名(重试换牌时可区分)
            assert row["model"] == "t9-cred"
            assert row["member_name"] == "t9-cred"
            assert row["is_stream"] == 0
            assert row["first_token_ms"] > 0
        finally:
            if pt:
                pt.join(timeout=2)
            backend.shutdown()

    def test_streaming_request_logs_is_stream_and_first_token(
            self, conn, controller, tmp_path):
        """流式请求过 proxy → 日志行 is_stream=1 且 first_token_ms>0。"""
        backend, backend_port = _start_backend(_BackendSSE)
        pt = None
        try:
            key_ref = str(tmp_path / "k.key")
            Path(key_ref).write_text("upstream-key-42", encoding="utf-8")
            _mk_provider(conn, controller, "t9s-prov",
                         base_url=f"http://127.0.0.1:{backend_port}")
            _mk_credential(conn, controller, "t9s-cred", "t9s-prov", key_ref)
            pool_create(conn, controller, "t9-stream-pool",
                        members=["t9s-cred"], request_id="t9s-pool")
            token = _pool_token(conn, "t9-stream-pool")

            pt = _start_proxy(19052)
            resp = _post("t9-stream-pool", 19052, token,
                         {"model": "test-model", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]},
                         accept="text/event-stream")
            assert resp.status == 200
            raw = resp.read()
            assert b"data:" in raw

            logs = _wait_log_row(conn, "t9-stream-pool")
            assert logs, "流式请求应落行"
            row = logs[0]
            assert row["status_code"] == 200
            assert row["is_stream"] == 1, (
                f"流式请求 is_stream 应为 1,得到 {row['is_stream']}")
            assert row["first_token_ms"] > 0, (
                f"流式请求 first_token_ms 应>0,得到 {row['first_token_ms']}")
            assert row["model"] == "t9s-cred"
        finally:
            if pt:
                pt.join(timeout=2)
            backend.shutdown()

    def test_retry_switch_member_logs_actual_member(
            self, conn, controller, tmp_path):
        """重试换牌: 首成员 500 失败 → 第二成员出活 → model 反映实际成员。"""
        b500, port500 = _start_backend(_Backend500)
        b200, port200 = _start_backend(_BackendOK)
        pt = None
        try:
            key_ref = str(tmp_path / "k.key")
            Path(key_ref).write_text("upstream-key-42", encoding="utf-8")
            _mk_provider(conn, controller, "t9r-prov-a",
                         base_url=f"http://127.0.0.1:{port500}",
                         request_id="t9r-pa")
            _mk_provider(conn, controller, "t9r-prov-b",
                         base_url=f"http://127.0.0.1:{port200}",
                         request_id="t9r-pb")
            _mk_credential(conn, controller, "t9r-cred-a", "t9r-prov-a",
                           key_ref, request_id="t9r-ca")
            _mk_credential(conn, controller, "t9r-cred-b", "t9r-prov-b",
                           key_ref, request_id="t9r-cb")
            pool_create(conn, controller, "t9-retry-pool",
                        members=["t9r-cred-a"], request_id="t9r-pool")
            pool_add_member(conn, controller, "t9-retry-pool",
                            "t9r-cred-b", request_id="t9r-add-b")
            token = _pool_token(conn, "t9-retry-pool")

            pt = _start_proxy(19054)
            # 轮盘第一棒 t9r-cred-a(500) → 重试换 t9r-cred-b(200)
            resp = _post("t9-retry-pool", 19054, token,
                         {"model": "test-model",
                          "messages": [{"role": "user", "content": "hi"}]})
            assert resp.status == 200

            logs = _wait_log_row(conn, "t9-retry-pool")
            assert logs, "重试换牌请求应落行"
            row = logs[0]
            assert row["status_code"] == 200
            assert row["model"] == "t9r-cred-b", (
                f"重试换牌后 model 应反映实际出活成员 t9r-cred-b,"
                f"得到 {row['model']}")
            assert row["member_name"] == "t9r-cred-b"
        finally:
            if pt:
                pt.join(timeout=2)
            b500.shutdown()
            b200.shutdown()


# ---- circuit_state 熔断留痕(Router.record() 真实链路) ----

class TestCircuitStateWiring:
    """PoolRouter.record() → CircuitBreaker 变迁 → pool_member_health 留痕。"""

    def _setup_pool(self, conn, controller, tmp_path, pool_name, member):
        key_ref = str(tmp_path / "k.key")
        Path(key_ref).write_text("upstream-key-42", encoding="utf-8")
        _mk_provider(conn, controller, "t9cs-prov",
                     base_url="http://127.0.0.1:19997",
                     request_id="t9cs-prov")
        _mk_credential(conn, controller, member, "t9cs-prov", key_ref,
                       request_id="t9cs-cred")
        pool_create(conn, controller, pool_name,
                    members=[member], request_id="t9cs-pool")
        # 放低熔断门槛便于测试触发
        for key, val in (("pool_proxy.circuit_error_threshold", "0.5"),
                         ("pool_proxy.circuit_min_samples", "2"),
                         ("pool_proxy.circuit_open_seconds", "1"),
                         ("pool_proxy.circuit_half_open_need", "2")):
            conn.execute(
                "UPDATE configs SET value=? WHERE key=?", (val, key))

    def test_circuit_state_propagates_on_failure(
            self, conn, controller, tmp_path):
        """record(False)×2 → 熔断开 → pool_member_health.circuit_state=open。"""
        pool_name, member = "t9-cs-pool", "t9-cs-m"
        self._setup_pool(conn, controller, tmp_path, pool_name, member)
        router = PoolRouter(conn)
        # pick 触发熔断器惰性初始化
        m, _, _ = router.pick(pool_name, "test-model", "openai_chat")
        assert m == member

        router.record(pool_name, member, False)
        row = conn.execute(
            "SELECT * FROM pool_member_health"
            " WHERE pool_name=? AND member_name=?",
            (pool_name, member)).fetchone()
        assert row is not None
        assert row["consecutive_failures"] == 1
        # 样本不足(1<2),熔断仍 closed,留痕应同步
        assert row["circuit_state"] == "closed"

        router.record(pool_name, member, False)
        cb = router._breakers[member]
        assert cb.state == "open", f"2 连败应开闸,得到 {cb.state}"
        row = conn.execute(
            "SELECT * FROM pool_member_health"
            " WHERE pool_name=? AND member_name=?",
            (pool_name, member)).fetchone()
        assert row["circuit_state"] == "open"
        assert row["circuit_state_changed_at"] > 0
        assert row["consecutive_failures"] == 2

    def test_circuit_state_recovers_to_closed(
            self, conn, controller, tmp_path):
        """开闸→冷却→半开→连续成功→closed,留痕跟随变迁。"""
        pool_name, member = "t9-cs-pool2", "t9-cs-m2"
        self._setup_pool(conn, controller, tmp_path, pool_name, member)
        router = PoolRouter(conn)
        m, _, _ = router.pick(pool_name, "test-model", "openai_chat")
        assert m == member

        router.record(pool_name, member, False)
        router.record(pool_name, member, False)
        cb = router._breakers[member]
        assert cb.state == "open"
        row = conn.execute(
            "SELECT circuit_state FROM pool_member_health"
            " WHERE pool_name=? AND member_name=?",
            (pool_name, member)).fetchone()
        assert row["circuit_state"] == "open"

        # 冷却到期(open_seconds=1)→ 半开 → 2 连成功恢复
        time.sleep(1.1)
        assert cb.state == "half_open"
        router.record(pool_name, member, True)
        router.record(pool_name, member, True)
        assert cb.state == "closed"
        row = conn.execute(
            "SELECT circuit_state FROM pool_member_health"
            " WHERE pool_name=? AND member_name=?",
            (pool_name, member)).fetchone()
        assert row["circuit_state"] == "closed"


# ---- 补遗: 失败 INSERT last_success_at=0 + 保留期清理 ----

class TestMemberHealthFailureInsert:
    """failure INSERT 的 last_success_at 必为 0(历史 bug: 写入 -1)。"""

    def test_failure_insert_last_success_at_is_zero(self, conn):
        _update_member_health(conn, "t9-hpool", "t9-hm", False,
                              last_error="upstream_error")
        row = conn.execute(
            "SELECT * FROM pool_member_health"
            " WHERE pool_name='t9-hpool' AND member_name='t9-hm'"
        ).fetchone()
        assert row is not None
        assert row["consecutive_failures"] == 1
        assert row["last_success_at"] == 0, (
            f"失败 INSERT 的 last_success_at 应为 0,得到 {row['last_success_at']}")
        assert row["last_error"] == "upstream_error"


def _insert_log(conn, request_id, pool_name, ts):
    conn.execute(
        "INSERT INTO pool_request_logs"
        " (request_id, pool_name, member_name, request_model, model,"
        " status_code, elapsed_ms, first_token_ms, input_tokens,"
        " output_tokens, is_stream, is_converted, session_id, ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (request_id, pool_name, "m", "mdl", "m",
         200, 50.0, 50.0, 5, 5, 0, 0, "", ts))


class TestPoolLogPurge:
    """_purge_pool_logs 保留期清理。"""

    def test_purges_expired_entries(self, conn):
        old_ts = now() - 40 * 86400
        recent_ts = now() - 86400
        _insert_log(conn, "t9-old-1", "t9-purge-p", old_ts)
        _insert_log(conn, "t9-new-1", "t9-purge-p", recent_ts)
        _purge_pool_logs(conn)
        assert conn.execute(
            "SELECT 1 FROM pool_request_logs WHERE request_id='t9-old-1'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM pool_request_logs WHERE request_id='t9-new-1'"
        ).fetchone() is not None

    def test_custom_retention_days(self, conn):
        """保留期配置可改: 改为 7 天 → 10 天前被清、5 天前保留。"""
        conn.execute(
            "UPDATE configs SET value='7'"
            " WHERE key='pool_log_retention_days'")
        _insert_log(conn, "t9-cust-old", "t9-purge-c", now() - 10 * 86400)
        _insert_log(conn, "t9-cust-new", "t9-purge-c", now() - 5 * 86400)
        _purge_pool_logs(conn)
        assert conn.execute(
            "SELECT 1 FROM pool_request_logs WHERE request_id='t9-cust-old'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM pool_request_logs WHERE request_id='t9-cust-new'"
        ).fetchone() is not None


class TestOldLedgerMigration:
    """老账本(缺 circuit_state 列)迁移: connect() 补列后健康表可写。"""

    def test_pool_member_health_columns_migrated(
            self, tmp_path, monkeypatch):
        import sqlite3
        home = tmp_path / "home"
        monkeypatch.setenv("TIANJI_HOME", str(home))
        home.mkdir(parents=True)
        # 造一份老账本: pool_member_health 无 circuit_state 两列
        old = sqlite3.connect(str(home / "ledger.db"))
        old.execute(
            "CREATE TABLE pool_member_health ("
            " pool_name TEXT NOT NULL, member_name TEXT NOT NULL,"
            " consecutive_failures INTEGER NOT NULL DEFAULT 0,"
            " last_success_at INTEGER NOT NULL DEFAULT 0,"
            " last_failure_at INTEGER NOT NULL DEFAULT 0,"
            " last_error TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (pool_name, member_name))")
        old.commit()
        old.close()

        c = connect()  # _migrate 应补列,不得 OperationalError
        try:
            cols = {r["name"] for r in c.execute(
                "PRAGMA table_info(pool_member_health)").fetchall()}
            assert "circuit_state" in cols
            assert "circuit_state_changed_at" in cols
            # 补列后健康表立即可写(老账本的炸点)
            _update_member_health(c, "t9-mig-pool", "t9-mig-m", False,
                                  last_error="upstream_error",
                                  circuit_state="open")
            row = c.execute(
                "SELECT * FROM pool_member_health"
                " WHERE pool_name='t9-mig-pool' AND member_name='t9-mig-m'"
            ).fetchone()
            assert row is not None
            assert row["circuit_state"] == "open"
            assert row["consecutive_failures"] == 1
        finally:
            c.close()
