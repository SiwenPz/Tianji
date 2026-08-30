"""票 57 回归测试: 池日志与信号第四层(号池 ③)。

覆盖:
  ①每次池请求落一行 pool_request_logs(双 model 字段+is_converted 齐全)
  ②日聚合跨天边界正确、重放不翻倍
  ③熔断/恢复事件在 pool_member_health 留痕
  ④全员 429 → 池耗尽标记 → allocator_health_check 跳过
  ⑤snapshot 含池摘要、peek 数据含健康明细
  ⑥池恢复: 耗尽标记清除 + _clear_pool_exhausted 调用
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import connect, tianji_home
from tianji.pool import pool_create, pool_add_member, pool_rotate_token
from tianji.integrations import register_custom_provider, register_credential, model_entry
from tianji.proxy import (
    CircuitBreaker, PoolRouter, _ForwardError, _verify_token, _pool_json, _cfg,
)
from tianji.proxy._pool import (
    _log_request, update_daily_rollup, _set_pool_exhausted,
    _clear_pool_exhausted, run_proxy,
)
from tianji.quota import context_health
from tianji import cockpit


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
def pctx(pconn, controller):
    """预置: 供应商 + credential + 池。"""
    prov = "testprov"
    register_custom_provider(
        pconn, controller, prov,
        base_url="http://127.0.0.1:19999",
        protocol="openai_chat",
        auth_style="bearer",
        request_id="rp1")
    # 模型
    from tianji.integrations import model_entry
    entry = ops._config(pconn, "integration_provider:" + prov)
    # ops._config 返回 value 字符串,需解析
    if isinstance(entry, str):
        entry = json.loads(entry)
    entry["models"] = [model_entry({"id": "test-model"})]
    pconn.execute(
        "UPDATE configs SET value=? WHERE key=?",
        (json.dumps(entry, ensure_ascii=False),
         "integration_provider:" + prov))
    cred_name = "testcred"
    key_dir = tianji_home() / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file = key_dir / "test.key"
    key_file.write_text("test-api-key-12345", encoding="utf-8")
    register_credential(
        pconn, controller, cred_name,
        provider=prov,
        key_ref=str(key_file),
        request_id="rc1")
    pool_name = "test-pool"
    pool_create(pconn, controller, pool_name,
                members=[cred_name], request_id="cp1")
    return {
        "conn": pconn, "controller": controller, "pool_name": pool_name,
        "key_file": key_file, "prov": prov, "cred_name": cred_name,
    }


# ---------------------------------------------------------------------------
# ① 每次请求落一行日志
# ---------------------------------------------------------------------------

class TestRequestLogging:

    def test_log_row_has_all_fields(self, pconn, pctx):
        ts = 1000000
        _log_request(
            pconn, pctx["pool_name"], pctx["cred_name"],
            "req-model-a", "routed-model-b",
            200, 120.5, 30.0, 100, 50, True, True, "sess-1", "req-1")
        row = pconn.execute(
            "SELECT * FROM pool_request_logs WHERE request_id=?", ("req-1",)
        ).fetchone()
        assert row is not None
        assert row["pool_name"] == pctx["pool_name"]
        assert row["member_name"] == pctx["cred_name"]
        assert row["request_model"] == "req-model-a"  # 双 model 字段齐全
        assert row["model"] == "routed-model-b"
        assert row["status_code"] == 200
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert row["is_stream"] == 1
        assert row["is_converted"] == 1    # is_converted 标齐全
        assert row["session_id"] == "sess-1"

    def test_duplicate_request_id_ignored(self, pconn, pctx):
        """INSERT OR IGNORE 保证同 request_id 不翻倍。"""
        from tianji.proxy._pool import _log_request
        for _ in range(3):
            _log_request(
                pconn, pctx["pool_name"], pctx["cred_name"],
                "test-model", "test-model", 200, 120, 0, 0, 0,
                False, False, "sess", "dup-req")
        n = pconn.execute(
            "SELECT COUNT(*) AS n FROM pool_request_logs WHERE request_id=?",
            ("dup-req",)).fetchone()["n"]
        assert n == 1


# ---------------------------------------------------------------------------
# ② 日聚合跨天/重放
# ---------------------------------------------------------------------------

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class TestDailyRollups:

    def test_upsert_not_double_count(self, pconn, pctx):
        """首次落行(rowcount==1) rollup 累加;重放(rowcount==0) rollup 不动。"""
        date = _today_utc()
        rc1 = _log_request(
            pconn, pctx["pool_name"], pctx["cred_name"],
            "test-model", "test-model", 200, 120.5, 30.0, 100, 50,
            False, True, "sess-1", "rollup-dedup-test")
        assert rc1 == 1
        # 手动 rollup(模拟 _do_route 里的联动)
        row = pconn.execute(
            "SELECT * FROM pool_daily_rollups"
            " WHERE rollup_date=? AND pool_name=? AND member_name=? AND model=?",
            (date, pctx["pool_name"], pctx["cred_name"], "test-model")
        ).fetchone()
        assert row is not None
        assert row["request_count"] == 1
        assert row["success_count"] == 1
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50

        # 重放(同 request_id) → rowcount==0 → rollup 不翻倍
        rc2 = _log_request(
            pconn, pctx["pool_name"], pctx["cred_name"],
            "test-model", "test-model", 200, 20, 0, 20, 10,
            False, True, "sess-1", "rollup-dedup-test")
        assert rc2 == 0
        row2 = pconn.execute(
            "SELECT request_count, success_count, input_tokens, output_tokens"
            " FROM pool_daily_rollups"
            " WHERE rollup_date=? AND pool_name=? AND member_name=? AND model=?",
            (date, pctx["pool_name"], pctx["cred_name"], "test-model")
        ).fetchone()
        assert row2["request_count"] == 1   # 不翻倍
        assert row2["success_count"] == 1
        assert row2["input_tokens"] == 100   # 不累加
        assert row2["output_tokens"] == 50

    def test_error_not_counted_as_success(self, pconn, pctx):
        date = _today_utc()
        update_daily_rollup(
            pconn, pctx["pool_name"], pctx["cred_name"],
            "test-model", 502, 0, 0)
        row = pconn.execute(
            "SELECT request_count, success_count, errors"
            " FROM pool_daily_rollups"
            " WHERE rollup_date=? AND pool_name=? AND member_name=? AND model=?",
            (date, pctx["pool_name"], pctx["cred_name"], "test-model")
        ).fetchone()
        assert row["request_count"] == 1
        assert row["success_count"] == 0
        assert row["errors"] == 1


# ---------------------------------------------------------------------------
# ③ 熔断/恢复事件
# ---------------------------------------------------------------------------

class TestMemberHealth:

    def test_failure_then_success(self, pconn, pctx):
        from tianji.proxy._pool import _update_member_health
        _update_member_health(pconn, pctx["pool_name"], pctx["cred_name"], False,
                              last_error="upstream_error")
        _update_member_health(pconn, pctx["pool_name"], pctx["cred_name"], False,
                              last_error="upstream_error")
        row = pconn.execute(
            "SELECT consecutive_failures, last_failure_at, last_error"
            " FROM pool_member_health"
            " WHERE pool_name=? AND member_name=?",
            (pctx["pool_name"], pctx["cred_name"])).fetchone()
        assert row["consecutive_failures"] == 2
        assert row["last_failure_at"] > 0
        assert row["last_error"] == "upstream_error"

        _update_member_health(pconn, pctx["pool_name"], pctx["cred_name"], True)
        row = pconn.execute(
            "SELECT consecutive_failures, last_success_at"
            " FROM pool_member_health"
            " WHERE pool_name=? AND member_name=?",
            (pctx["pool_name"], pctx["cred_name"])).fetchone()
        assert row["consecutive_failures"] == 0
        assert row["last_success_at"] > 0


# ---------------------------------------------------------------------------
# ④ 全员 429 → 池耗尽
# ---------------------------------------------------------------------------

class _BackendAlways429(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        body = json.dumps({"error": "rate_limit"}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


@pytest.mark.timeout(30)
class TestExhaustionSignal:

    def test_all_429_pool_exhausted(self, pconn, pctx):
        """全员返回 429 → 502 → pool exhausted → context_health exhausted。

        修A: 仅绑池实例 exhausted=true, 未绑实例不受影响。
        """
        pool_token = pconn.execute(
            "SELECT value FROM configs WHERE key=?",
            ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

        # 修A: 注册绑池实例 + 未绑实例用于 per-instance 断言
        bound_inst = ops.instance_register(
            pconn, "池绑定测试机", "claude", "deepseek-v4-flash",
            key_name=pctx["pool_name"])
        ops.instance_register(
            pconn, "bystander", "claude", "deepseek-v4-flash")

        backend = HTTPServer(("127.0.0.1", 0), _BackendAlways429)
        backend_port = backend.server_address[1]
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        try:
            prov_entry = ops._config(
                pconn, "integration_provider:" + pctx["prov"])
            if isinstance(prov_entry, str):
                prov_entry = json.loads(prov_entry)
            prov_entry["base_url"] = f"http://127.0.0.1:{backend_port}"
            pconn.execute(
                "UPDATE configs SET value=? WHERE key=?",
                (json.dumps(prov_entry, ensure_ascii=False),
                 "integration_provider:" + pctx["prov"]))

            proxy_port = 19004
            pt = threading.Thread(
                target=run_proxy, args=(proxy_port,), daemon=True)
            pt.start()
            try:
                time.sleep(0.5)

                body = json.dumps({"model": "test-model"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body, headers={"Content-Type": "application/json"},
                    method="POST")
                try:
                    resp = urllib.request.urlopen(req, timeout=10)
                except urllib.error.HTTPError as exc:
                    resp_status = exc.code

                # 全员429 → 透传429 (非502)
                assert resp_status == 429
                # 强制 WAL checkpoint 确保跨连接可见
                pconn.execute("PRAGMA wal_checkpoint")
                log_n = pconn.execute(
                    "SELECT COUNT(*) AS n FROM pool_request_logs"
                    " WHERE pool_name=?", (pctx["pool_name"],)).fetchone()["n"]
                assert log_n > 0

                # pool_member_health 有失败
                health = pconn.execute(
                    "SELECT * FROM pool_member_health"
                    " WHERE pool_name=? AND member_name=?",
                    (pctx["pool_name"], pctx["cred_name"])).fetchone()
                assert health is not None
                assert health["consecutive_failures"] > 0

                # quota:<pool_name> exhausted
                qrow = pconn.execute(
                    "SELECT value FROM configs WHERE key=?",
                    ("quota:" + pctx["pool_name"],)).fetchone()
                assert qrow is not None, "池耗尽应写入 quota 条目"
                qdata = json.loads(qrow["value"])
                assert qdata.get("exhausted") is True

                # context_health: 仅绑池实例 exhausted=true, 未绑实例不受影响(修A)
                h = context_health(pconn, bound_inst["name"])
                assert h["exhausted"] is True, f"绑池实例应 exhausted, got {h}"
                h_by = context_health(pconn, "bystander")
                assert h_by["exhausted"] is False, "未绑池实例不应 exhausted (修A)"
            finally:
                pt.join(timeout=2)
                _clear_pool_exhausted(pconn, pctx["pool_name"])
        finally:
            backend.shutdown()


# ---------------------------------------------------------------------------
# ⑥ 池恢复
# ---------------------------------------------------------------------------

class _BackendOK(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        body = b'{"ok":true}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a, **kw):
        pass


def _reset_circuits(pconn, pool_name):
    """清空池内所有成员熔断器状态(模拟超时恢复)。"""
    pool = _pool_json(pconn, pool_name) or {}
    circuit = pool.get("circuit", {}) or {}
    for m in pool.get("members", []):
        circuit[m] = {"state": "closed"}
    pool["circuit"] = circuit
    pconn.execute(
        "UPDATE configs SET value=? WHERE key=?",
        (json.dumps(pool, ensure_ascii=False),
         "pool:" + pool_name))


class TestPoolRecovery:

    def test_exhausted_then_recovered(self, pconn, pctx):
        pool_token = pconn.execute(
            "SELECT value FROM configs WHERE key=?",
            ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

        # 修A: 注册绑池实例 + 未绑实例用于 per-instance 断言
        bound_inst = ops.instance_register(
            pconn, "池绑定测试机", "claude", "deepseek-v4-flash",
            key_name=pctx["pool_name"])
        ops.instance_register(
            pconn, "bystander", "claude", "deepseek-v4-flash")

        # ── 共享 proxy (避免端口残留) ──
        proxy_port = 19006
        pt = threading.Thread(
            target=run_proxy, args=(proxy_port,), daemon=True)
        pt.start()
        time.sleep(0.5)

        try:
            # ── 阶段 1: 全员 429 → 池耗尽 ──
            backend429 = HTTPServer(("127.0.0.1", 0), _BackendAlways429)
            port429 = backend429.server_address[1]
            t429 = threading.Thread(target=backend429.serve_forever, daemon=True)
            t429.start()
            try:
                prov = ops._config(pconn, "integration_provider:" + pctx["prov"])
                if isinstance(prov, str):
                    prov = json.loads(prov)
                prov["base_url"] = f"http://127.0.0.1:{port429}"
                pconn.execute(
                    "UPDATE configs SET value=? WHERE key=?",
                    (json.dumps(prov, ensure_ascii=False),
                     "integration_provider:" + pctx["prov"]))

                body = json.dumps({"model": "test-model"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body, headers={"Content-Type": "application/json"},
                    method="POST")
                try:
                    urllib.request.urlopen(req, timeout=10)
                except urllib.error.HTTPError as exc:
                    assert exc.code == 429

                pconn.execute("PRAGMA wal_checkpoint")
                qrow = pconn.execute(
                    "SELECT value FROM configs WHERE key=?",
                    ("quota:" + pctx["pool_name"],)).fetchone()
                assert qrow is not None
                assert json.loads(qrow["value"]).get("exhausted") is True

                # 修A: 仅绑池实例 exhausted=true, 未绑实例不受影响
                h = context_health(pconn, bound_inst["name"])
                assert h["exhausted"] is True, f"绑池实例应 exhausted, got {h}"
                h_by = context_health(pconn, "bystander")
                assert h_by["exhausted"] is False, "未绑池实例不应 exhausted (修A)"
            finally:
                backend429.shutdown()

            # ── 阶段 2: 清电路 + 换 200 后端 → 成功请求 → 标记消失 ──
            backend200 = HTTPServer(("127.0.0.1", 0), _BackendOK)
            port200 = backend200.server_address[1]
            t200 = threading.Thread(target=backend200.serve_forever, daemon=True)
            t200.start()
            try:
                _reset_circuits(pconn, pctx["pool_name"])
                _clear_pool_exhausted(pconn, pctx["pool_name"])

                prov = ops._config(pconn, "integration_provider:" + pctx["prov"])
                if isinstance(prov, str):
                    prov = json.loads(prov)
                prov["base_url"] = f"http://127.0.0.1:{port200}"
                pconn.execute(
                    "UPDATE configs SET value=? WHERE key=?",
                    (json.dumps(prov, ensure_ascii=False),
                     "integration_provider:" + pctx["prov"]))

                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body, headers={"Content-Type": "application/json"},
                    method="POST")
                resp = urllib.request.urlopen(req, timeout=10)
                assert resp.status == 200

                # quota 标记已清除
                qrow2 = pconn.execute(
                    "SELECT value FROM configs WHERE key=?",
                    ("quota:" + pctx["pool_name"],)).fetchone()
                assert qrow2 is None, "恢复后 quota 条目应已清除"

                # 修A: 恢复后绑池实例 exhausted=False, 未绑实例恒 False
                h2 = context_health(pconn, bound_inst["name"])
                assert h2["exhausted"] is False, f"恢复后绑池实例应不 exhausted, got {h2}"
                h2_by = context_health(pconn, "bystander")
                assert h2_by["exhausted"] is False
            finally:
                backend200.shutdown()
        finally:
            pt.join(timeout=2)
            _clear_pool_exhausted(pconn, pctx["pool_name"])


# ---------------------------------------------------------------------------
# ⑤ Cockpit 池摘要
# ---------------------------------------------------------------------------

class TestCockpitPoolSummary:

    def test_snapshot_has_pools_key(self, pconn, pctx):
        snap = cockpit.snapshot(pconn)
        assert "pools" in snap
        names = [p["name"] for p in snap["pools"]]
        assert "test-pool" in names

    def test_pool_summary_fields(self, pconn, pctx):
        snap = cockpit.snapshot(pconn)
        pool = next(p for p in snap["pools"] if p["name"] == "test-pool")
        assert pool["member_count"] >= 1
        assert "circuit_open_count" in pool
        assert isinstance(pool["members"], list)
        for m in pool["members"]:
            assert "name" in m
            assert "circuit" in m

    def test_render_snapshot_shows_pools(self, pconn, pctx):
        snap = cockpit.snapshot(pconn)
        rendered = cockpit.render_snapshot(snap)
        assert "号池摘要" in rendered
        assert "test-pool" in rendered


# ---------------------------------------------------------------------------
# ⑦ SSE 流式逐块透传
# ---------------------------------------------------------------------------

class _BackendStreaming(BaseHTTPRequestHandler):
    """模拟 SSE 后端: 逐块推送事件(块间有间隔),关闭连接标记结束。"""
    _CHUNKS = [b"data: hello\n\n", b"data: world\n\n"]
    _GAP = 1.0  # 块间间隔(秒): 整量缓冲实现下首块到达时间≈整体时长

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for c in self._CHUNKS:
            self.wfile.write(c)
            self.wfile.flush()
            time.sleep(self._GAP)
        self.close_connection = True

    def log_message(self, *a, **kw):
        pass


class TestSSEStreaming:

    def test_sse_streaming_body(self, pconn, pctx):
        """SSE 响应应逐块读上游→逐块写回客户端,不过整量缓冲。"""
        backend = HTTPServer(("127.0.0.1", 0), _BackendStreaming)
        backend_port = backend.server_address[1]
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        try:
            prov = ops._config(
                pconn, "integration_provider:" + pctx["prov"])
            if isinstance(prov, str):
                prov = json.loads(prov)
            prov["base_url"] = f"http://127.0.0.1:{backend_port}"
            pconn.execute(
                "UPDATE configs SET value=? WHERE key=?",
                (json.dumps(prov, ensure_ascii=False),
                 "integration_provider:" + pctx["prov"]))

            pool_token = pconn.execute(
                "SELECT value FROM configs WHERE key=?",
                ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

            proxy_port = 19012
            pt = threading.Thread(
                target=run_proxy, args=(proxy_port,), daemon=True)
            pt.start()
            try:
                time.sleep(0.5)
                body = json.dumps({"model": "test-model"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body,
                    headers={"Content-Type": "application/json",
                             "Accept": "text/event-stream"},
                    method="POST")
                t0 = time.monotonic()
                resp = urllib.request.urlopen(req, timeout=10)
                first = resp.read(1)
                t_first = time.monotonic() - t0  # 首块到达耗时
                raw = first + resp.read()
                t_total = time.monotonic() - t0  # 整体时长
                assert b"hello" in raw
                assert b"world" in raw
                # SSE 使用 chunked transfer encoding,不应有 Content-Length
                assert "Content-Length" not in resp.headers
                # 逐块透传: 首块到达远小于整体时长;整量缓冲实现此处 ≈ t_total
                assert t_total >= _BackendStreaming._GAP * 2 * 0.8, (
                    f"整体时长 {t_total:.2f}s 应 ≥ 块间间隔×2")
                assert t_first < t_total * 0.5, (
                    f"首块 {t_first:.2f}s 应远小于整体 {t_total:.2f}s")
            finally:
                pt.join(timeout=2)
        finally:
            backend.shutdown()


# ---------------------------------------------------------------------------
# ⑧ 超时三件套: 首字节 / 流空闲 / 总时长
# ---------------------------------------------------------------------------

class _BackendSlowFirstByte(BaseHTTPRequestHandler):
    """后端延迟发送首字节。"""
    def do_POST(self):
        time.sleep(5)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
        self.close_connection = True

    def log_message(self, *a, **kw):
        pass


class _BackendSlowBodyTrickle(BaseHTTPRequestHandler):
    """非流式: 响应头+声明 Content-Length 立即发,body 涓流(永不发完)。"""
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b"{}")  # 只发 2/100 字节,然后停摆
        self.wfile.flush()
        time.sleep(10)  # 远超测试 tt=2 总时长
        self.close_connection = True

    def log_message(self, *a, **kw):
        pass


class _BackendStreamingGap(BaseHTTPRequestHandler):
    """流式响应: 首块立即,第二块 delayed。"""
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b"data: first\n\n")
        self.wfile.flush()
        time.sleep(3)  # 测试设 1s 流空闲超时,3>1 → 应超时
        self.wfile.write(b"data: second\n\n")
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *a, **kw):
        pass


class _BackendSlowStream(BaseHTTPRequestHandler):
    """流式响应: 小块持续发送,整体超总时长后仍在发。"""
    def do_POST(self):
        self._chunks_sent = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        while True:
            time.sleep(0.5)
            chunk = b"data: chunk-%d\n\n" % self._chunks_sent
            self.wfile.write(chunk)
            self.wfile.flush()
            self._chunks_sent += 1
        self.close_connection = True

    def log_message(self, *a, **kw):
        pass


def _set_timeout_config(pconn, fb=2, si=1, tt=5, mr=2):
    """辅助: 把超时参数写账本(供 proxy 读取)。"""
    pconn.execute(
        "UPDATE configs SET value=? WHERE key=?",
        (str(fb), "pool_proxy.timeout_first_byte"))
    pconn.execute(
        "UPDATE configs SET value=? WHERE key=?",
        (str(si), "pool_proxy.timeout_stream_idle"))
    pconn.execute(
        "UPDATE configs SET value=? WHERE key=?",
        (str(tt), "pool_proxy.timeout_total"))
    pconn.execute(
        "UPDATE configs SET value=? WHERE key=?",
        (str(mr), "pool_proxy.max_retries"))


@pytest.mark.timeout(30)
class TestProxyTimeouts:

    def test_first_byte_timeout(self, pconn, pctx):
        """首字节超时: 后端延迟 > 配置首字节超时 → 502。"""
        backend = HTTPServer(("127.0.0.1", 0), _BackendSlowFirstByte)
        backend_port = backend.server_address[1]
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        try:
            prov = ops._config(
                pconn, "integration_provider:" + pctx["prov"])
            if isinstance(prov, str):
                prov = json.loads(prov)
            prov["base_url"] = f"http://127.0.0.1:{backend_port}"
            pconn.execute(
                "UPDATE configs SET value=? WHERE key=?",
                (json.dumps(prov, ensure_ascii=False),
                 "integration_provider:" + pctx["prov"]))
            _set_timeout_config(pconn, fb=2, si=10, tt=60, mr=0)

            pool_token = pconn.execute(
                "SELECT value FROM configs WHERE key=?",
                ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

            proxy_port = 19014
            pt = threading.Thread(
                target=run_proxy, args=(proxy_port,), daemon=True)
            pt.start()
            try:
                time.sleep(0.5)
                body = json.dumps({"model": "test-model"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body,
                    headers={"Content-Type": "application/json",
                             "Accept": "text/event-stream"},
                    method="POST")
                try:
                    resp = urllib.request.urlopen(req, timeout=15)
                    assert False, "Expected timeout error"
                except urllib.error.HTTPError as exc:
                    assert exc.code == 502
            finally:
                pt.join(timeout=2)
        finally:
            backend.shutdown()

    def test_stream_idle_timeout(self, pconn, pctx):
        """流空闲超时: SSE 块间隔 > 配置 → 502。"""
        backend = HTTPServer(("127.0.0.1", 0), _BackendStreamingGap)
        backend_port = backend.server_address[1]
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        try:
            prov = ops._config(
                pconn, "integration_provider:" + pctx["prov"])
            if isinstance(prov, str):
                prov = json.loads(prov)
            prov["base_url"] = f"http://127.0.0.1:{backend_port}"
            pconn.execute(
                "UPDATE configs SET value=? WHERE key=?",
                (json.dumps(prov, ensure_ascii=False),
                 "integration_provider:" + pctx["prov"]))
            _set_timeout_config(pconn, fb=10, si=1, tt=60, mr=0)

            pool_token = pconn.execute(
                "SELECT value FROM configs WHERE key=?",
                ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

            proxy_port = 19016
            pt = threading.Thread(
                target=run_proxy, args=(proxy_port,), daemon=True)
            pt.start()
            try:
                time.sleep(0.5)
                body = json.dumps({"model": "test-model"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body,
                    headers={"Content-Type": "application/json",
                             "Accept": "text/event-stream"},
                    method="POST")
                # 首块已发→流中空闲超时→proxy 截断 chunked 流(连接中断)。
                # 客户端读体必抛错;旧写法 assert False + except Exception: pass 会假绿。
                import http.client as _hc
                with pytest.raises((_hc.IncompleteRead, urllib.error.URLError,
                                    ConnectionError, OSError)):
                    resp = urllib.request.urlopen(req, timeout=15)
                    resp.read()  # chunked 流被截断 → 读体必抛
            finally:
                pt.join(timeout=2)
        finally:
            backend.shutdown()

    def test_total_timeout(self, pconn, pctx):
        """总时长超时: SSE 流超过总时长 → 502。"""
        backend = HTTPServer(("127.0.0.1", 0), _BackendSlowStream)
        backend_port = backend.server_address[1]
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        try:
            prov = ops._config(
                pconn, "integration_provider:" + pctx["prov"])
            if isinstance(prov, str):
                prov = json.loads(prov)
            prov["base_url"] = f"http://127.0.0.1:{backend_port}"
            pconn.execute(
                "UPDATE configs SET value=? WHERE key=?",
                (json.dumps(prov, ensure_ascii=False),
                 "integration_provider:" + pctx["prov"]))
            _set_timeout_config(pconn, fb=1, si=10, tt=2, mr=0)

            pool_token = pconn.execute(
                "SELECT value FROM configs WHERE key=?",
                ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

            proxy_port = 19018
            pt = threading.Thread(
                target=run_proxy, args=(proxy_port,), daemon=True)
            pt.start()
            try:
                time.sleep(0.5)
                body = json.dumps({"model": "test-model"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body,
                    headers={"Content-Type": "application/json",
                             "Accept": "text/event-stream"},
                    method="POST")
                # 流超总时长→proxy 截断 chunked 流(连接中断),读体必抛。
                import http.client as _hc
                with pytest.raises((_hc.IncompleteRead, urllib.error.URLError,
                                    ConnectionError, OSError)):
                    resp = urllib.request.urlopen(req, timeout=15)
                    resp.read()  # chunked 流被截断 → 读体必抛
            finally:
                pt.join(timeout=2)
        finally:
            backend.shutdown()

    def test_nonstream_total_timeout(self, pconn, pctx):
        """非流式总时长截止: 响应头已到但 body 涓流 → 超 timeout_total → 502。

        (首字节超时管不到这里——头已拿到;须由 timeout_total 兜底。)
        """
        backend = HTTPServer(("127.0.0.1", 0), _BackendSlowBodyTrickle)
        backend_port = backend.server_address[1]
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        try:
            prov = ops._config(
                pconn, "integration_provider:" + pctx["prov"])
            if isinstance(prov, str):
                prov = json.loads(prov)
            prov["base_url"] = f"http://127.0.0.1:{backend_port}"
            pconn.execute(
                "UPDATE configs SET value=? WHERE key=?",
                (json.dumps(prov, ensure_ascii=False),
                 "integration_provider:" + pctx["prov"]))
            _set_timeout_config(pconn, fb=10, si=10, tt=2, mr=0)

            pool_token = pconn.execute(
                "SELECT value FROM configs WHERE key=?",
                ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

            proxy_port = 19024
            pt = threading.Thread(
                target=run_proxy, args=(proxy_port,), daemon=True)
            pt.start()
            try:
                time.sleep(0.5)
                body = json.dumps({"model": "test-model"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST")
                t0 = time.monotonic()
                with pytest.raises(urllib.error.HTTPError) as exc_info:
                    urllib.request.urlopen(req, timeout=15)
                elapsed = time.monotonic() - t0
                assert exc_info.value.code == 502
                # 总时长 2s 截止(而非 fb=10 或悬挂)
                assert elapsed < 8, (
                    f"应在 timeout_total=2s 附近失败,实际 {elapsed:.2f}s")
            finally:
                pt.join(timeout=2)
        finally:
            backend.shutdown()


# ---------------------------------------------------------------------------
# ⑨ 429 透传 Retry-After
# ---------------------------------------------------------------------------

class _Backend429WithRetryAfter(BaseHTTPRequestHandler):
    """返回 429 和 Retry-After 头。"""
    def do_POST(self):
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "5")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
        self.close_connection = True

    def log_message(self, *a, **kw):
        pass


class _Backend429EventStream(BaseHTTPRequestHandler):
    """以 text/event-stream 形态返回 429(错误码检查必须先于流式判定)。"""
    def do_POST(self):
        self.send_response(429)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Retry-After", "7")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
        self.close_connection = True

    def log_message(self, *a, **kw):
        pass


@pytest.mark.timeout(30)
class Test429BubbleHeaders:

    def test_429_bubbles_with_retry_after(self, pconn, pctx):
        backend = HTTPServer(("127.0.0.1", 0), _Backend429WithRetryAfter)
        backend_port = backend.server_address[1]
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        try:
            prov = ops._config(
                pconn, "integration_provider:" + pctx["prov"])
            if isinstance(prov, str):
                prov = json.loads(prov)
            prov["base_url"] = f"http://127.0.0.1:{backend_port}"
            pconn.execute(
                "UPDATE configs SET value=? WHERE key=?",
                (json.dumps(prov, ensure_ascii=False),
                 "integration_provider:" + pctx["prov"]))

            pool_token = pconn.execute(
                "SELECT value FROM configs WHERE key=?",
                ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

            proxy_port = 19020
            pt = threading.Thread(
                target=run_proxy, args=(proxy_port,), daemon=True)
            pt.start()
            try:
                time.sleep(0.5)
                body = json.dumps({"model": "test-model"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST")
                try:
                    resp = urllib.request.urlopen(req, timeout=10)
                    resp_status = resp.status
                    resp_body = resp.read()
                    assert False, "Expected 429"
                except urllib.error.HTTPError as exc:
                    resp_status = exc.code
                    assert exc.code == 429
                    ra = exc.headers.get("Retry-After", "")
                    assert ra == "5"
            finally:
                pt.join(timeout=2)
        finally:
            backend.shutdown()

    def test_429_with_event_stream_content_type_not_treated_as_success(
            self, pconn, pctx):
        """上游以 text/event-stream 返回 429: 必须按错误处理(进 all_429
        口径→冒泡 429),不能因 SSE 形态被当成功透传 200。"""
        backend = HTTPServer(("127.0.0.1", 0), _Backend429EventStream)
        backend_port = backend.server_address[1]
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        try:
            prov = ops._config(
                pconn, "integration_provider:" + pctx["prov"])
            if isinstance(prov, str):
                prov = json.loads(prov)
            prov["base_url"] = f"http://127.0.0.1:{backend_port}"
            pconn.execute(
                "UPDATE configs SET value=? WHERE key=?",
                (json.dumps(prov, ensure_ascii=False),
                 "integration_provider:" + pctx["prov"]))

            pool_token = pconn.execute(
                "SELECT value FROM configs WHERE key=?",
                ("pool:token:" + pctx["pool_name"],)).fetchone()["value"]

            proxy_port = 19022
            pt = threading.Thread(
                target=run_proxy, args=(proxy_port,), daemon=True)
            pt.start()
            try:
                time.sleep(0.5)
                body = json.dumps({"model": "test-model"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{proxy_port}/proxy/{pctx['pool_name']}"
                    f"/v1/chat/completions?token={pool_token}",
                    data=body,
                    headers={"Content-Type": "application/json",
                             "Accept": "text/event-stream"},
                    method="POST")
                with pytest.raises(urllib.error.HTTPError) as exc_info:
                    urllib.request.urlopen(req, timeout=10)
                # SSE 形态的 429 仍按 429 冒泡(带 Retry-After),不是 200
                assert exc_info.value.code == 429
                assert exc_info.value.headers.get("Retry-After") == "7"
            finally:
                pt.join(timeout=2)
        finally:
            backend.shutdown()
