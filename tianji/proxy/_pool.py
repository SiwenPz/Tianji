"""号池 proxy 常驻进程(票 56): HTTP 服务绑 127.0.0.1,由 daemon supervisor 守护。

职责: 透传转发 + 透明重试(同模型成员,流中段不重试) + 滑动窗口熔断器。
核心纯标准库(urllib/http.client),无新依赖。

路由协议: POST /proxy/<pool_name>/<api_path>?token=<池令牌>
  令牌亦支持 Authorization: Bearer / x-api-key 头(task-03 认证头兼容)。
  请求体含 model 字段 → 按模型+协议过滤候选 → 纯轮盘选成员 → 转发。
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from .. import integrations, ops
from ..db import connect, now
from .convert import convert_request, convert_response, is_conversion_needed
from .stream import stream_convert


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------
_PROXY_HOST = "127.0.0.1"
_PROXY_PORT_DEFAULT = 8799

# 超时三件套(秒)
_DEFAULT_TIMEOUT_FIRST_BYTE = 90
_DEFAULT_TIMEOUT_STREAM_IDLE = 180
_DEFAULT_TIMEOUT_TOTAL = 600

# 熔断器参数
_DEFAULT_CB_ERROR_RATE = 0.7
_DEFAULT_CB_MIN_SAMPLES = 15
_DEFAULT_CB_OPEN_SECONDS = 90
_DEFAULT_CB_HALF_OPEN_NEED = 3

# 重试次数
_DEFAULT_MAX_RETRIES = 5


# ---------------------------------------------------------------------------
# 账本读写
# ---------------------------------------------------------------------------
_LOG_PREFIX = "[tianji-proxy]"

def _cfg(conn, key, default=""):
    row = conn.execute("SELECT value FROM configs WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _pool_json(conn, name):
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       ("pool:" + name,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def _pool_token(conn, name):
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       ("pool:token:" + name,)).fetchone()
    return row["value"] if row else ""


def _verify_token(conn, pool_name, token):
    """恒定时间比较池令牌。无令牌=放行(历史兼容)。"""
    stored = _pool_token(conn, pool_name)
    if not stored:
        return True
    if not token:
        return False
    return hmac.compare_digest(stored, token)


# ---------------------------------------------------------------------------
# 滑动窗口熔断器
# ---------------------------------------------------------------------------
class CircuitBreaker:
    """逐成员滑动窗口熔断器: 错误率≥0.7 且样本≥15 → 开 90s → 半开 3 连成功恢复。"""

    __slots__ = (
        "error_threshold", "min_samples", "open_seconds", "half_open_need",
        "_state", "_opened_at", "_window", "_half_ok",
    )

    def __init__(self, error_threshold=None, min_samples=None,
                 open_seconds=None, half_open_need=None):
        self.error_threshold = (error_threshold
                                if error_threshold is not None else _DEFAULT_CB_ERROR_RATE)
        self.min_samples = (min_samples if min_samples is not None
                            else _DEFAULT_CB_MIN_SAMPLES)
        self.open_seconds = (open_seconds if open_seconds is not None
                             else _DEFAULT_CB_OPEN_SECONDS)
        self.half_open_need = (half_open_need if half_open_need is not None
                               else _DEFAULT_CB_HALF_OPEN_NEED)
        self._state = "closed"
        self._opened_at = 0.0
        self._window: list[bool] = []
        self._half_ok = 0

    @property
    def state(self):
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.open_seconds:
                self._state = "half_open"
                self._half_ok = 0
        return self._state

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self):
        s = self.state
        if s == "half_open":
            self._half_ok += 1
            if self._half_ok >= self.half_open_need:
                self._state = "closed"
                self._window = []
        else:
            self._window.append(True)
            if len(self._window) > self.min_samples:
                self._window = self._window[-self.min_samples:]

    def record_failure(self):
        s = self.state
        if s == "half_open":
            self._state = "open"
            self._opened_at = time.monotonic()
            self._window = []
        else:
            self._window.append(False)
            if len(self._window) > self.min_samples:
                self._window = self._window[-self.min_samples:]
            if (len(self._window) >= self.min_samples
                    and self._failure_rate() >= float(self.error_threshold)):
                self._state = "open"
                self._opened_at = time.monotonic()

    def _failure_rate(self):
        if not self._window:
            return 0.0
        return sum(1 for w in self._window if not w) / len(self._window)

    def to_dict(self):
        return {
            "state": self._state,
            "opened_at": self._opened_at,
            "window": self._window[-self.min_samples:],
            "half_ok": self._half_ok,
        }

    @classmethod
    def from_dict(cls, d, **kw):
        cb = cls(**kw)
        if d:
            cb._state = d.get("state", "closed")
            cb._opened_at = d.get("opened_at", 0.0)
            cb._window = list(d.get("window", []))
            cb._half_ok = d.get("half_ok", 0)
        return cb


# ---------------------------------------------------------------------------
# 池路由
# ---------------------------------------------------------------------------
class PoolRouter:
    """按模型+协议过滤 → 纯轮盘选成员(熔断跳过) → 返回 credential 明细。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._breakers: dict[str, CircuitBreaker] = {}
        self._rr: dict[str, int] = {}

    def _cfg(self, key, default=""):
        return _cfg(self._conn, key, default)

    def _load_breakers(self, pool_name, members):
        pool = _pool_json(self._conn, pool_name) or {}
        circuit_json = pool.get("circuit", {}) or {}
        kw = dict(
            error_threshold=float(
                self._cfg("pool_proxy.circuit_error_threshold",
                          _DEFAULT_CB_ERROR_RATE)),
            min_samples=int(self._cfg("pool_proxy.circuit_min_samples",
                                      _DEFAULT_CB_MIN_SAMPLES)),
            open_seconds=int(self._cfg("pool_proxy.circuit_open_seconds",
                                       _DEFAULT_CB_OPEN_SECONDS)),
            half_open_need=int(self._cfg("pool_proxy.circuit_half_open_need",
                                         _DEFAULT_CB_HALF_OPEN_NEED)),
        )
        for m in members:
            state = circuit_json.get(m)
            self._breakers[m] = CircuitBreaker.from_dict(state, **kw)

    def _persist_breakers(self, pool_name):
        pool = _pool_json(self._conn, pool_name)
        if not pool:
            return
        circuit = {}
        for m, cb in self._breakers.items():
            # 访问 state 触发半开计时器
            _ = cb.state
            circuit[m] = cb.to_dict()
        pool["circuit"] = circuit
        ts = now()
        self._conn.execute(
            "UPDATE configs SET value=?, updated_at=? WHERE key=?",
            (json.dumps(pool, ensure_ascii=False), ts, "pool:" + pool_name))
        ops.audit(self._conn, "proxy_circuit_update",
                  {"pool": pool_name,
                   "members": {m: cb.to_dict() for m, cb in self._breakers.items()}})

    def pick(self, pool_name, model, protocol):
        """选下一跳成员。返回 (member_name, cred_dict, provider_dict) 或 (None, None, None)。
        两段式: 先同协议透传优先,无可用成员时按可转换协议(anthropic↔openai_chat)兜底。
        """
        pool = _pool_json(self._conn, pool_name)
        if not pool:
            return None, None, None

        members = pool.get("members", [])
        if not members:
            return None, None, None

        # 惰性初始化熔断器
        if not self._breakers:
            self._load_breakers(pool_name, members)

        def _filter(candidates):
            """按模型+协议过滤候选,返回 [(m, cred, pentry)]。"""
            result = []
            for m in candidates:
                cb = self._breakers.get(m)
                if cb is not None and not cb.allow():
                    continue  # 熔断中跳过
                cred = integrations._config(self._conn, "credential:" + m)
                if not cred:
                    continue
                pname = cred.get("provider", "")
                pentry = integrations._config(
                    self._conn, "integration_provider:" + pname) if pname else None
                if not pentry:
                    continue
                if model:
                    models = [x.get("id") for x in pentry.get("models", [])
                              if isinstance(x, dict)]
                    if models and model not in models:
                        continue
                if protocol:
                    p_proto = integrations.normalize_legacy_protocol(
                        pentry.get("protocol", ""))
                    if p_proto != protocol:
                        continue
                result.append((m, cred, pentry))
            return result

        # 第一段: 严格同协议(透传优先)
        same_proto = _filter(members)
        matched = same_proto

        # 第二段: 无同协议成员时,按可转换协议兜底(anthropic↔openai_chat)
        if not matched and protocol:
            fallback = []
            for m in members:
                cb = self._breakers.get(m)
                if cb is not None and not cb.allow():
                    continue
                cred = integrations._config(self._conn, "credential:" + m)
                if not cred:
                    continue
                pname = cred.get("provider", "")
                pentry = integrations._config(
                    self._conn, "integration_provider:" + pname) if pname else None
                if not pentry:
                    continue
                if model:
                    pmodels = [x.get("id") for x in pentry.get("models", [])
                               if isinstance(x, dict)]
                    if pmodels and model not in pmodels:
                        continue
                p_proto = integrations.normalize_legacy_protocol(
                    pentry.get("protocol", ""))
                # 转换层只支持 anthropic↔openai_chat;openai_responses 等组合
                # 不在兜底范围(is_conversion_needed 会误放,fail-loud 拒绝)
                if integrations._proto_compatible(p_proto, protocol):
                    fallback.append((m, cred, pentry))
            matched = fallback

        if not matched:
            return None, None, None

        # 纯轮盘(从上次偏移接龙)
        idx = self._rr.get(pool_name, 0)
        choice = matched[idx % len(matched)]
        self._rr[pool_name] = (idx + 1) % len(matched)
        return choice

    def all_breakers_open(self) -> bool:
        """检查所有断路器是否都处于 open 状态。"""
        if not self._breakers:
            return False
        return all(not cb.allow() for cb in self._breakers.values())

    def record(self, pool_name, member_name, success):
        cb = self._breakers.get(member_name)
        if cb is None:
            return
        if success:
            cb.record_success()
        else:
            cb.record_failure()
        self._persist_breakers(pool_name)

        # pool_member_health 留痕(含熔断状态同步)
        _update_member_health(self._conn, pool_name, member_name, success,
                              circuit_state=cb.state if cb else "")


# ---------------------------------------------------------------------------
# pool_member_health 辅助
# ---------------------------------------------------------------------------
def _update_member_health(conn, pool_name, member_name, success, last_error="",
                          circuit_state=""):
    """更新成员健康快照(含熔断状态留痕)。"""
    ts = now()
    row = conn.execute(
        "SELECT * FROM pool_member_health WHERE pool_name=? AND member_name=?",
        (pool_name, member_name)).fetchone()
    if row:
        if success:
            conn.execute(
                "UPDATE pool_member_health SET consecutive_failures=0,"
                " last_success_at=?, last_error='',"
                " circuit_state=?, circuit_state_changed_at=?"
                " WHERE pool_name=? AND member_name=?",
                (ts,
                 circuit_state if circuit_state else row["circuit_state"],
                 ts if circuit_state else row["circuit_state_changed_at"],
                 pool_name, member_name))
        else:
            conn.execute(
                "UPDATE pool_member_health SET consecutive_failures=consecutive_failures+1,"
                " last_failure_at=?, last_error=?,"
                " circuit_state=?, circuit_state_changed_at=?"
                " WHERE pool_name=? AND member_name=?",
                (ts, last_error,
                 circuit_state if circuit_state else row["circuit_state"],
                 ts if circuit_state else row["circuit_state_changed_at"],
                 pool_name, member_name))
    else:
        if success:
            conn.execute(
                "INSERT INTO pool_member_health"
                " (pool_name, member_name, consecutive_failures,"
                " last_success_at, last_failure_at, last_error,"
                " circuit_state, circuit_state_changed_at)"
                " VALUES (?,?,0,?,0,'',?,?)",
                (pool_name, member_name, ts,
                 circuit_state or "", 0))
        else:
            conn.execute(
                "INSERT INTO pool_member_health"
                " (pool_name, member_name, consecutive_failures,"
                " last_success_at, last_failure_at, last_error,"
                " circuit_state, circuit_state_changed_at)"
                " VALUES (?,?,1,0,?,?,?,?)",
                (pool_name, member_name, ts, last_error or "upstream_error",
                 circuit_state or "", ts if circuit_state else 0))


# ---------------------------------------------------------------------------
# pool_request_logs 日志
# ---------------------------------------------------------------------------
def _log_request(conn, pool_name, member_name, req_model, model, status_code,
                 elapsed_ms, first_token_ms, input_tokens, output_tokens,
                 is_stream, is_converted, session_id, request_id):
    ts = now()
    rowcount = conn.execute(
        "INSERT OR IGNORE INTO pool_request_logs"
        " (request_id, pool_name, member_name, request_model, model,"
        " status_code, elapsed_ms, first_token_ms, input_tokens, output_tokens,"
        " is_stream, is_converted, session_id, ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (request_id, pool_name, member_name, req_model or "", model or "",
         status_code, elapsed_ms or 0, first_token_ms or 0,
         input_tokens or 0, output_tokens or 0,
         1 if is_stream else 0, 1 if is_converted else 0,
         session_id or "", ts)).rowcount
    # 真正落行(rowcount==1)才累加 rollup;重放 IGNORE(rowcount==0)不动
    if rowcount == 1:
        update_daily_rollup(
            conn, pool_name, member_name, model or "",
            status_code, input_tokens or 0, output_tokens or 0)
    return rowcount


# ---------------------------------------------------------------------------
# pool_daily_rollups 日聚合
# ---------------------------------------------------------------------------
def update_daily_rollup(conn, pool_name, member_name, model, status_code,
                        input_tokens, output_tokens):
    """追加/更新日聚合(UPSERT,重放安全)。"""
    from datetime import datetime, timezone
    rollup_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    model = model or ""
    row = conn.execute(
        "SELECT * FROM pool_daily_rollups"
        " WHERE rollup_date=? AND pool_name=? AND member_name=? AND model=?",
        (rollup_date, pool_name, member_name, model)).fetchone()
    if row:
        conn.execute(
            "UPDATE pool_daily_rollups SET"
            " request_count=request_count+1,"
            " success_count=success_count+?,"
            " errors=errors+?,"
            " input_tokens=input_tokens+?,"
            " output_tokens=output_tokens+?"
            " WHERE rollup_date=? AND pool_name=? AND member_name=? AND model=?",
            (1 if 200 <= status_code < 300 else 0,
             0 if 200 <= status_code < 300 else 1,
             input_tokens or 0, output_tokens or 0,
             rollup_date, pool_name, member_name, model))
    else:
        conn.execute(
            "INSERT INTO pool_daily_rollups"
            " (rollup_date, pool_name, member_name, model,"
            " request_count, success_count, errors, input_tokens, output_tokens)"
            " VALUES (?,?,?,?, 1,?,?,?,?)",
            (rollup_date, pool_name, member_name, model,
             1 if 200 <= status_code < 300 else 0,
             0 if 200 <= status_code < 300 else 1,
             input_tokens or 0, output_tokens or 0))


# ---------------------------------------------------------------------------
# pool 耗尽信号(池=key 等价物)
# ---------------------------------------------------------------------------
def _set_pool_exhausted(conn, pool_name):
    """标记池耗尽：将 pool_name 写入 quota 表(pool=key 等价物)。"""
    import json
    qkey = "quota:" + pool_name
    data = json.dumps({"exhausted": True, "ts": now(),
                       "source": "proxy_pool", "reason": "all_members_failed"},
                      ensure_ascii=False)
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        (qkey, data, now()))


def _clear_pool_exhausted(conn, pool_name):
    """池恢复 → 清除耗尽标记。"""
    conn.execute("DELETE FROM configs WHERE key=?", ("quota:" + pool_name,))


# ---------------------------------------------------------------------------
# 请求转发
# ---------------------------------------------------------------------------
class _ForwardError(Exception):
    def __init__(self, code, detail="", stream_broken=False,
                 status_code=None, resp_headers=None):
        self.code = code
        self.detail = detail
        self.stream_broken = stream_broken
        self.status_code = status_code
        self.resp_headers = resp_headers or {}


def _resp_socket(conn_obj, resp):
    """取响应底层 socket 用于调超时。上游 HTTP/1.0(will_close)时
    getresponse() 已把 conn_obj.sock 置 None 并把连接移交给响应,
    须从 resp.fp.raw._sock 取(SocketIO dup 的独立 fd,同一底层 socket)。"""
    s = conn_obj.sock
    if s is not None:
        return s
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    return getattr(raw, "_sock", None)


def _forward_http(method, url, headers, body, timeout_total,
                  timeout_first_byte, timeout_stream_idle,
                  stream_callback=None):
    """通过标准库转发 HTTP 请求,返回 (status, resp_headers_dict, body_bytes, was_streamed)。
    失败抛 _ForwardError(stream_broken=True 表示流中断,不可重试)。
    单连接: SSE 在已建立的 http.client 响应上逐块流式读,绝不重发请求。"""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path_q = parsed.path + ("?" + parsed.query if parsed.query else "")
    use_ssl = parsed.scheme == "https"

    timeout = float(timeout_first_byte)
    t_start = time.monotonic()

    try:
        if use_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn_obj = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=ctx)
        else:
            conn_obj = http.client.HTTPConnection(
                host, port, timeout=timeout)

        safe_headers = {k: v for k, v in headers.items()
                        if isinstance(v, str) and v}
        conn_obj.request(method, path_q, body=body, headers=safe_headers)
        resp = conn_obj.getresponse()

        status = resp.status
        resp_headers = {k: v for k, v in resp.getheaders()}
        ctype = resp_headers.get("Content-Type", "")
        is_sse = "text/event-stream" in ctype

        # 错误码检查先于流式判定: 上游以 text/event-stream 返回 429/5xx
        # 也按可重试失败处理(进 all_429 口径),不当成功透传
        if status == 429 or status >= 500:
            conn_obj.close()
            raise _ForwardError(
                "retryable", detail=f"upstream_{status}",
                stream_broken=False, status_code=status,
                resp_headers=resp_headers)

        if is_sse and stream_callback is not None:
            # SSE: 单连接逐块流式读(不重建连接、不重发请求)。
            # 流空闲超时=相邻块间隔上限,总时长超时=整个流的截止。
            deadline = t_start + float(timeout_total)
            idle = float(timeout_stream_idle)
            sock = _resp_socket(conn_obj, resp)
            stream_callback.on_headers(status, resp_headers)
            try:
                while True:
                    now = time.monotonic()
                    remaining = deadline - now
                    if remaining <= 0:
                        raise _ForwardError(
                            "total_timeout", detail="total timeout",
                            stream_broken=True)
                    if sock is not None:
                        sock.settimeout(min(idle, remaining))
                    try:
                        chunk = resp.read1(65536)
                    except socket.timeout:
                        if time.monotonic() >= deadline:
                            raise _ForwardError(
                                "total_timeout", detail="total timeout",
                                stream_broken=True)
                        raise _ForwardError(
                            "stream_idle_timeout",
                            detail="stream idle timeout",
                            stream_broken=True)
                    if not chunk:
                        break  # 上游 EOF(连接关闭)
                    stream_callback.on_chunk(chunk)
            except _ForwardError:
                raise
            except (ConnectionError, OSError) as exc:
                raise _ForwardError(
                    "upstream_stream_error", detail=str(exc),
                    stream_broken=True)
            finally:
                conn_obj.close()
            return status, resp_headers, b"", True

        # 非流式: 读体受总时长截止约束(票 56 的 600s 即为此设)
        remaining = float(timeout_total) - (time.monotonic() - t_start)
        if remaining <= 0:
            conn_obj.close()
            raise _ForwardError(
                "total_timeout", detail="total timeout",
                stream_broken=False)
        nsock = _resp_socket(conn_obj, resp)
        if nsock is not None:
            nsock.settimeout(remaining)
        try:
            resp_body = resp.read()
        except socket.timeout:
            conn_obj.close()
            raise _ForwardError(
                "total_timeout", detail="total timeout",
                stream_broken=False)
        conn_obj.close()
        return status, resp_headers, resp_body, False
    except _ForwardError:
        raise
    except http.client.IncompleteRead as exc:
        raise _ForwardError("upstream_disconnect",
                            detail=str(exc), stream_broken=True)
    except (ConnectionError, OSError, socket.timeout) as exc:
        raise _ForwardError("upstream_unreachable",
                            detail=str(exc), stream_broken=False)
    except ssl.SSLError as exc:
        raise _ForwardError("ssl_error",
                            detail=str(exc), stream_broken=False)


# ---------------------------------------------------------------------------
# HTTP 请求处理器
# ---------------------------------------------------------------------------
class _ProxyHandler(BaseHTTPRequestHandler):
    """单请求处理器。共享状态通过类变量注入。"""

    router: PoolRouter = None  # type: ignore[assignment]
    max_retries: int = _DEFAULT_MAX_RETRIES
    timeout_first_byte: int = _DEFAULT_TIMEOUT_FIRST_BYTE
    timeout_stream_idle: int = _DEFAULT_TIMEOUT_STREAM_IDLE
    timeout_total: int = _DEFAULT_TIMEOUT_TOTAL
    server_version = "TianjiProxy/1.0"

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def do_PATCH(self):
        self._route("PATCH")

    # ---- 内部 ----

    def _route(self, method):
        try:
            self._do_route(method)
        except _ForwardError as exc:
            code = exc.code
            detail = exc.detail
            # 流已中断(已发 headers): 只能关闭连接,不能发错误响应
            if exc.stream_broken:
                self.close_connection = True
                return
            self._send_json(502, code, detail)
        except Exception as exc:
            import sys, traceback as tb
            tb.print_exc(file=sys.stderr)
            self._send_json(500, "proxy_internal",
                            "{}: {}".format(type(exc).__name__, exc))

    def _do_route(self, method):
        try:
            self._do_route_impl(method)
        except Exception as exc:
            import sys
            sys.stderr.write(f"[PROXY CRASH] {type(exc).__name__}: {exc}\n")
            import traceback as tb
            tb.print_exc(file=sys.stderr)
            try:
                self._send_json(500, "proxy_internal",
                                "{}: {}".format(type(exc).__name__, exc))
            except Exception:
                pass

    def _do_route_impl(self, method):
        path = urllib.parse.urlparse(self.path)
        raw = path.path

        # 解析 /proxy/<pool>/<api>* 或 /proxy/<pool>
        parts = raw.split("/", 3)
        if len(parts) < 3 or parts[1] != "proxy":
            self._send_json(400, "bad_path",
                            "expected /proxy/<pool_name>/<api_path>")
            return

        pool_name = parts[2]
        api_path = "/" + parts[3] if len(parts) > 3 and parts[3] else "/"

        # 连接账本(每次请求新建,SQLite 本地无开销)
        conn = connect()
        try:
            # 令牌校验(query / Authorization Bearer / x-api-key,优先级降序)
            qs = urllib.parse.parse_qs(path.query)
            token_list = qs.get("token", [""])
            client_token = token_list[0] if token_list else ""
            if not client_token:
                auth_hdr = self.headers.get("Authorization", "")
                if auth_hdr.lower().startswith("bearer "):
                    client_token = auth_hdr[7:].strip()
            if not client_token:
                client_token = self.headers.get("x-api-key", "")
            if not _verify_token(conn, pool_name, client_token):
                self._send_json(401, "unauthorized",
                                "invalid or missing pool token")
                return

            # 读请求体 + 提取 model
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""
            req_model = ""
            if body:
                try:
                    body_json = json.loads(body)
                    req_model = body_json.get("model") or ""
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

            # 协议: Accept 首推断 / 默认 openai_chat
            accept = self.headers.get("Accept", "")
            req_proto = "openai_chat"
            if "anthropic" in accept and "json" in accept:
                req_proto = "anthropic"

            # 超时配置
            tf = int(_cfg(conn, "pool_proxy.timeout_first_byte",
                          _DEFAULT_TIMEOUT_FIRST_BYTE))
            ts = int(_cfg(conn, "pool_proxy.timeout_stream_idle",
                          _DEFAULT_TIMEOUT_STREAM_IDLE))
            tt = int(_cfg(conn, "pool_proxy.timeout_total",
                          _DEFAULT_TIMEOUT_TOTAL))
            max_retries = int(_cfg(conn, "pool_proxy.max_retries",
                                   _DEFAULT_MAX_RETRIES))

            t0 = time.monotonic()
            first_token_ms = 0
            self._first_token_ts = 0  # 首字节到达时刻(流式),on_headers 赋值
            sw = self._StreamWriter(self, t0)

            # 重试循环(上限默认 5 次,进账本可配;流中断不重试由 _ForwardError 控制)
            final_member = ""
            final_protocol = ""
            final_status = 0
            final_input_tokens = 0
            final_output_tokens = 0
            final_is_stream = False
            final_is_converted = False
            final_elapsed_ms = 0.0
            last_error_detail = ""
            any_member_used = False
            _resp_sent = False
            _stream_broken = False
            request_id = self.headers.get("X-Request-ID", "") or uuid.uuid4().hex
            session_id = self.headers.get("X-Session-ID", "")

            all_429 = True
            last_429_hdrs = {}

            for attempt in range(max_retries + 1):
                member_name, cred, prov = self.router.pick(
                    pool_name, req_model, req_proto)

                if member_name is None:
                    # all_429 stays as-is: no members = exhausted due to 429-errors
                    if attempt == 0:
                        final_status = 503
                        self._send_json(
                            503, "no_available_member",
                            "pool={} model={} proto={}".format(
                                pool_name, req_model, req_proto))
                        _resp_sent = True
                    break

                any_member_used = True
                t_try_start = time.monotonic()

                # 记转换信息
                resp_proto = prov.get("protocol", "openai_chat")
                normalized_req = integrations.normalize_legacy_protocol(req_proto)
                normalized_resp = integrations.normalize_legacy_protocol(
                    resp_proto)
                is_converted = is_conversion_needed(
                    normalized_req, normalized_resp)

                # 读明文 key(key_ref 文件)
                key_ref = cred.get("key_ref", "")
                key_value = ""
                if key_ref:
                    p = Path(key_ref)
                    key_value = p.read_text(encoding="utf-8").strip() if p.is_file() else ""

                base_url = prov.get("base_url", "")
                proto = prov.get("protocol", "openai_chat")
                auth_style = cred.get("auth_style", "bearer")

                target_url = base_url.rstrip("/") + api_path

                fwd_headers = _build_fwd_headers(
                    self.headers, proto, auth_style, key_value)

                # 跨协议转换:请求体从客户端协议→上游协议(失败 fail-loud → 502)
                fwd_body = body
                if is_converted and body:
                    try:
                        converted_req, _req_tags = convert_request(
                            json.loads(body), normalized_req, normalized_resp)
                        fwd_body = json.dumps(
                            converted_req, ensure_ascii=False).encode("utf-8")
                    except Exception as exc:
                        self._send_json(
                            502, "conversion_failed",
                            "request convert {} -> {} failed: {}".format(
                                normalized_req, normalized_resp, exc))
                        _resp_sent = True
                        break

                # SSE 流式转换包装:跨协议时 wrapper 转码每块再写入下游
                # (流方向: 上游→客户端, 故 from=上游协议, to=客户端协议)
                fwd_sw = sw
                if is_converted:
                    fwd_sw = self._ConvertingStreamWriter(
                        sw, normalized_resp, normalized_req, req_model)

                try:
                    status, resp_hdrs, resp_body, was_streamed = _forward_http(
                        method, target_url, fwd_headers, fwd_body,
                        tt, tf, ts,
                        stream_callback=fwd_sw)

                    if first_token_ms == 0:
                        first_token_ms = (time.monotonic() - t0) * 1000

                    # 跨协议转换:非流式响应体从上游协议→客户端协议
                    # (失败 fail-loud → 502,不静默透传未转换体)
                    if is_converted and not was_streamed:
                        try:
                            converted_resp, _resp_tags = convert_response(
                                json.loads(resp_body),
                                normalized_resp, normalized_req)
                            resp_body = json.dumps(
                                converted_resp, ensure_ascii=False).encode("utf-8")
                        except Exception as exc:
                            self._send_json(
                                502, "conversion_failed",
                                "response convert {} -> {} failed: {}".format(
                                    normalized_resp, normalized_req, exc))
                            _resp_sent = True
                            break

                    # Token 统计(尝试从响应提取)
                    try:
                        rj = json.loads(resp_body)
                        usage = rj.get("usage") or {}
                        final_input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                        final_output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
                        final_is_stream = False
                    except Exception:
                        pass

                    final_member = member_name
                    final_protocol = prov.get("protocol", "")
                    final_elapsed_ms = (time.monotonic() - t0) * 1000
                    final_status = status
                    final_is_converted = is_converted
                    # is_stream: 流式请求=True,非流式=False
                    final_is_stream = was_streamed
                    # first_token_ms: 流式=_first_token_ts(首块到达),非流式=总耗时
                    if was_streamed:
                        first_token_ms = self._first_token_ts or (time.monotonic() - t0) * 1000
                    else:
                        first_token_ms = final_elapsed_ms
                    # model: 实际路由到的成员名(非抄 request_model)
                    final_model = member_name
                    self.router.record(pool_name, member_name, True)
                    all_429 = False
                    # 落行在前(修时序): 客户端收到 200 时日志已提交
                    _log_request(
                        conn, pool_name, final_member, req_model, final_model,
                        final_status, final_elapsed_ms, first_token_ms,
                        final_input_tokens, final_output_tokens,
                        final_is_stream, final_is_converted, session_id, request_id)
                    if was_streamed:
                        sw.on_complete()
                    else:
                        self._send_resp(status, resp_hdrs, resp_body)
                    _resp_sent = True
                    # 成功: 清池耗尽标记
                    _clear_pool_exhausted(conn, pool_name)
                    break
                except _ForwardError as exc:
                    # Only update all_429/final_status for actual HTTP status codes;
                    # connection errors (status_code=None) preserve existing state
                    if exc.status_code is None:
                        pass  # connection error: keep all_429 and final_status as-is
                    else:
                        all_429 = all_429 and (exc.status_code == 429)
                        final_status = exc.status_code
                        if exc.status_code == 429 and exc.resp_headers:
                            last_429_hdrs = exc.resp_headers
                    final_member = member_name
                    final_protocol = prov.get("protocol", "")
                    final_elapsed_ms = (time.monotonic() - t_try_start) * 1000
                    final_is_converted = is_converted
                    last_error_detail = exc.detail or ""
                    self.router.record(pool_name, member_name, False)
                    if exc.stream_broken:
                        _stream_broken = True
                        break

            # 一行日志: 取"产生最终结果的那次尝试"(成功路径已提前落行,此处仅兜底失败路径)
            if final_member and not _resp_sent:
                # 失败路径:first_token_ms 已在流式首块时写入 self._first_token_ts
                if self._first_token_ts:
                    first_token_ms = self._first_token_ts
                _log_request(
                    conn, pool_name, final_member, req_model, final_member,
                    final_status, final_elapsed_ms, first_token_ms,
                    final_input_tokens, final_output_tokens,
                    final_is_stream, final_is_converted, session_id, request_id)

            # 成员健康: 失败次数 + 失败详情传递
            if final_member and final_status >= 400:
                cb = (self.router._breakers or {}).get(final_member)
                _update_member_health(
                    conn, pool_name, final_member, False, last_error_detail,
                    circuit_state=cb.state if cb else "")

            # 重试耗尽 → 池耗尽标记(无论 members 耗尽还是全员失败)
            if not any_member_used and final_status in (502, 503):
                _set_pool_exhausted(conn, pool_name)
            elif any_member_used and final_status >= 400 and not _resp_sent:
                _set_pool_exhausted(conn, pool_name)

            # 429 透传: 全员 429 → 透传 429,只带 Retry-After(全量透传会与自身
            # JSON 载荷的 Content-Length/Content-Type 冲突)
            if not _resp_sent and not _stream_broken and all_429 and last_429_hdrs:
                retry_hdrs = {k: v for k, v in last_429_hdrs.items()
                              if k.lower() == "retry-after"}
                self._send_json(final_status, "all_members_retry_later",
                                "all members rate-limited",
                                extra_headers=retry_hdrs)
            # 重试耗尽但未发响应 → 502
            elif not _resp_sent and not _stream_broken:
                self._send_json(502, "all_members_failed",
                                "pool={} status={}".format(pool_name, final_status))

        finally:
            conn.close()

    def _send_resp(self, status, headers, body):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in ("transfer-encoding", "content-length",
                             "connection", "keep-alive"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream_headers(self, status, headers):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in ("transfer-encoding", "content-length",
                             "connection", "keep-alive",
                             "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    class _ConvertingStreamWriter:
        """SSE 跨协议转换包装器: 每块经 stream_convert 转码后写入下游。"""

        def __init__(self, wrapped, from_proto, to_proto, model=""):
            self._w = wrapped
            self._fp = from_proto
            self._tp = to_proto
            self._model = model
            self._buf = b""

        def on_headers(self, status, headers):
            self._w.on_headers(status, headers)

        def on_chunk(self, data):
            self._buf += data
            parts = self._buf.split(b"\n\n")
            self._buf = parts[-1]
            for part in parts[:-1]:
                txt = part.decode("utf-8", "replace")
                converted = "".join(
                    stream_convert(
                        iter([txt + "\n\n"]),
                        self._fp, self._tp, self._model))
                if converted:
                    self._w.on_chunk(converted.encode("utf-8"))

        def on_complete(self):
            if self._buf:
                txt = self._buf.decode("utf-8", "replace")
                converted = "".join(
                    stream_convert(
                        iter([txt]),
                        self._fp, self._tp, self._model))
                if converted:
                    self._w.on_chunk(converted.encode("utf-8"))
            self._w.on_complete()

    class _StreamWriter:
        def __init__(self, handler, t0):
            self._h = handler
            self._done = False
            self._t0 = t0

        def on_headers(self, status, headers):
            # 首字节/首块到达:记录时刻供 first_token_ms 使用
            import time as _time
            self._h._first_token_ts = (_time.monotonic() - self._t0) * 1000
            self._h._send_stream_headers(status, headers)
            self._done = True

        def on_chunk(self, data):
            hex_len = "{:x}\r\n".format(len(data)).encode("ascii")
            self._h.wfile.write(hex_len)
            self._h.wfile.write(data)
            self._h.wfile.write(b"\r\n")
            self._h.wfile.flush()

        def on_complete(self):
            self._done = True
            try:
                self._h.wfile.write(b"0\r\n\r\n")
                self._h.wfile.flush()
            except OSError:
                pass

    def _send_json(self, status, code, detail, extra_headers=None):
        payload = json.dumps({"error": code, "detail": detail}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass  # 静默日志


def _build_fwd_headers(src_headers, proto, auth_style, key_value):
    """构建转发请求头: 携带认证 + 透传无关安全头。"""
    hdrs: dict[str, str] = {}
    # 认证
    if auth_style == "bearer" and key_value:
        hdrs["Authorization"] = "Bearer " + key_value
    elif auth_style == "x-api-key" and key_value:
        hdrs["x-api-key"] = key_value
    # 透传客户端头
    for passthrough in ("Accept", "Content-Type", "X-Model-Provider",
                        "X-Pool-Protocol"):
        val = src_headers.get(passthrough, "")
        if val:
            hdrs[passthrough] = val
    # 默认 Accept(兼容性)
    hdrs.setdefault("Accept", "application/json, text/event-stream")
    hdrs["Content-Type"] = src_headers.get("Content-Type", "application/json")
    return hdrs


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------
def run_proxy(port=_PROXY_PORT_DEFAULT, host=_PROXY_HOST,
              ping_log=None):
    """启动 proxy 常驻进程(被 daemon 子进程调用或直接 python -m 运行)。

    ping_log: 可选文件路径,每 60s 写一行心跳(监控/探活用)。
    """
    router = PoolRouter(connect())
    _ProxyHandler.router = router
    _ProxyHandler.max_retries = int(
        _cfg(router._conn, "pool_proxy.max_retries", _DEFAULT_MAX_RETRIES))
    _ProxyHandler.timeout_first_byte = int(
        _cfg(router._conn, "pool_proxy.timeout_first_byte",
             _DEFAULT_TIMEOUT_FIRST_BYTE))
    _ProxyHandler.timeout_stream_idle = int(
        _cfg(router._conn, "pool_proxy.timeout_stream_idle",
             _DEFAULT_TIMEOUT_STREAM_IDLE))
    _ProxyHandler.timeout_total = int(
        _cfg(router._conn, "pool_proxy.timeout_total",
             _DEFAULT_TIMEOUT_TOTAL))

    httpd = HTTPServer((host, port), _ProxyHandler)
    # 复用 server 的 socket 做探活
    httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    addr_str = "{}:{}".format(host, port)
    print("{} proxy 启动: {}".format(_LOG_PREFIX, addr_str), flush=True)

    if ping_log:
        import threading

        def _ping():
            while True:
                time.sleep(60)
                try:
                    Path(ping_log).write_text(
                        "{}\\n".format(time.time()), encoding="utf-8")
                except OSError:
                    break

        t = threading.Thread(target=_ping, daemon=True)
        t.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("{} proxy 停止".format(_LOG_PREFIX), flush=True)


# ---------------------------------------------------------------
# 入口
# ---------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    _p = argparse.ArgumentParser(prog="tianji.proxy")
    _p.add_argument("--port", type=int, default=_PROXY_PORT_DEFAULT)
    _p.add_argument("--host", default=_PROXY_HOST)
    _p.add_argument("--ping-log", default="")
    _a = _p.parse_args()
    run_proxy(port=_a.port, host=_a.host, ping_log=_a.ping_log or None)
