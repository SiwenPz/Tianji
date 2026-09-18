#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tianji-proxy.py - 天机轻量号池代理
====================================
单文件 · 纯标准库 · 零依赖

功能:
  - OpenAI 兼容总入口 (GET /v1/models, POST /v1/chat/completions, POST /v1/responses)
  - 多渠道多 key 纯轮盘 + 熔断跳过 + 429/5xx 透明重试
  - SSE 流式字节透传 (stream=true)
  - 状态持久化 (~/.tianji/proxy-state.json, 原子写)
  - 完整 CLI: serve / add-channel / add-key / remove-key / remove-channel / list / status

配置:
  默认 ~/.tianji/proxy.toml (环境变量 TJ_PROXY_CONFIG 覆盖)

铁律:
  - fail-open: 内部异常透传 502，不主动 crash
  - key 全程掩码: 前 4 位 + *** + 后 4 位
  - 状态原子写: 先写临时文件再 rename
  - 超时: 连接 10s、读 600s
"""
from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# 标准库导入（尽量延迟，提升 CLI 子命令冷启速度）
# ──────────────────────────────────────────────────────────────────────────────
import argparse
import copy
import datetime
import hashlib
import hmac
import http.server
import json
import logging
import os
import re
import shutil
import signal
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.client

try:
    import tomllib                 # Python ≥ 3.11
except ImportError:
    import tomli as tomllib        # 回退：需外部安装（v2 最低支持 3.11，此分支备用）

# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_PORT       = 8317
DEFAULT_CONFIG     = os.path.join(os.path.expanduser("~"), ".tianji", "proxy.toml")
DEFAULT_STATE      = os.path.join(os.path.expanduser("~"), ".tianji", "proxy-state.json")
CONN_TIMEOUT       = 10      # 连接超时（秒）
READ_TIMEOUT       = 600     # 读超时（秒）
MAX_RETRIES        = 5       # 单请求最大重试次数
CHUNK_SIZE         = 4096    # SSE 透传块大小
OBSERVATION_LIMIT  = 512     # 仅内存中的脱敏路由证据

# 熔断器默认参数
CIRCUIT_MIN_SAMPLES    = 15
CIRCUIT_ERROR_RATE     = 0.7
CIRCUIT_OPEN_SECONDS   = 90
CIRCUIT_HALF_OPEN_NEED = 3   # 半开连续成功次数才关闸

# ──────────────────────────────────────────────────────────────────────────────
# 日志
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tianji-proxy")


# ──────────────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────────────
def mask_key(key: str) -> str:
    """掩码 key：前 4 位 + *** + 后 4 位；短 key 全掩。"""
    if not key:
        return "***"
    key = str(key)
    return key[:4] + "***" + key[-4:] if len(key) > 8 else "***"


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def utc_iso(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(datetime.timezone.utc).isoformat()


def from_iso(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 熔断器（单个 key，滑动窗口）
# ──────────────────────────────────────────────────────────────────────────────
class CircuitBreaker:
    """滑动窗口熔断器。

    状态机: CLOSED →（失败率超标）→ OPEN →（冷却过期）→ HALF_OPEN
           HALF_OPEN + 成功 → CLOSED
           HALF_OPEN + 失败 → OPEN
    """

    def __init__(
        self,
        key: str,
        min_samples: int = CIRCUIT_MIN_SAMPLES,
        error_rate: float = CIRCUIT_ERROR_RATE,
        open_seconds: float = CIRCUIT_OPEN_SECONDS,
        half_open_need: int = CIRCUIT_HALF_OPEN_NEED,
    ):
        self.key = key
        self.min_samples   = min_samples
        self.error_rate    = error_rate
        self.open_seconds  = open_seconds
        self.half_open_need = half_open_need

        # 滑动窗口：[(utc_iso_str, is_failure), ...]
        self._window: list[tuple[str, bool]] = []

        self.state: str = "closed"          # closed | open | half_open
        self.opened_at: datetime.datetime | None = None
        self.half_ok: int = 0               # 半开连续成功计数

    # ── 持久化序列化 ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "key":            self.key,
            "state":          self.state,
            "opened_at":      utc_iso(self.opened_at),
            "half_ok":        self.half_ok,
            "window":         self._window[-self.min_samples * 3:],  # 保留足够
            "min_samples":    self.min_samples,
            "error_rate":     self.error_rate,
            "open_seconds":   self.open_seconds,
            "half_open_need": self.half_open_need,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CircuitBreaker":
        cb = cls(
            key            = d.get("key", ""),
            min_samples    = d.get("min_samples",    CIRCUIT_MIN_SAMPLES),
            error_rate     = d.get("error_rate",     CIRCUIT_ERROR_RATE),
            open_seconds   = d.get("open_seconds",   CIRCUIT_OPEN_SECONDS),
            half_open_need = d.get("half_open_need", CIRCUIT_HALF_OPEN_NEED),
        )
        cb.state       = d.get("state", "closed")
        cb.opened_at   = from_iso(d.get("opened_at"))
        cb.half_ok     = d.get("half_ok", 0)
        cb._window     = [tuple(x) for x in d.get("window", [])]
        return cb

    # ── 核心逻辑 ─────────────────────────────────────────────────────────────

    def _purge(self, now: datetime.datetime) -> None:
        """移除窗口内过期条目。"""
        cutoff = now - datetime.timedelta(seconds=self.open_seconds * 2)
        self._window = [
            (ts, fail) for ts, fail in self._window
            if from_iso(ts) is None or from_iso(ts) >= cutoff
        ]

    def _failure_rate(self) -> float:
        fails = sum(1 for _, f in self._window if f)
        return fails / len(self._window) if self._window else 0.0

    def _maybe_trip(self, now: datetime.datetime) -> None:
        """closed 状态下检测是否该开闸。"""
        if self.state != "closed":
            return
        self._purge(now)
        if (
            len(self._window) >= self.min_samples
            and self._failure_rate() >= self.error_rate
        ):
            self.state     = "open"
            self.opened_at = now
            log.warning(
                "熔断开闸: key=%s 失败率=%.0f%% (样本=%d)",
                mask_key(self.key),
                self._failure_rate() * 100,
                len(self._window),
            )

    def record_failure(self) -> None:
        now = utc_now()
        self._window.append((utc_iso(now), True))
        if self.state == "half_open":
            # 半开探测失败 → 立即重开
            self.state     = "open"
            self.opened_at = now
            self.half_ok   = 0
            return
        self._maybe_trip(now)

    def record_success(self) -> None:
        now = utc_now()
        if self.state == "half_open":
            self.half_ok += 1
            if self.half_ok >= self.half_open_need:
                self.state     = "closed"
                self.opened_at = None
                self.half_ok   = 0
                self._window.clear()
                log.warning("熔断关闸: key=%s 半开探测通过", mask_key(self.key))
            return
        self._window.append((utc_iso(now), False))
        # closed 状态下成功也检查是否触发 trip（新增失败条目后可能刚过阈值）
        self._maybe_trip(now)

    def allow(self) -> bool:
        """是否允许请求通过。"""
        now = utc_now()
        if self.state == "closed":
            return True
        if self.state == "open":
            if self.opened_at and (now - self.opened_at).total_seconds() >= self.open_seconds:
                self.state = "half_open"
                self.half_ok = 0
                log.warning("熔断半开: key=%s 冷却到期, 开始探测", mask_key(self.key))
                return True   # 允许一次探测
            return False
        # half_open：允许探测流量
        return True

    def summary(self) -> dict:
        return {
            "state":             self.state,
            "failure_rate":      round(self._failure_rate(), 3),
            "window_size":       len(self._window),
            "opened_at":         utc_iso(self.opened_at),
            "half_open_ok":      self.half_ok,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 代理池（配置 + 状态 + 选路）
# ──────────────────────────────────────────────────────────────────────────────
class ProxyPool:
    """多渠道多 key 号池。"""

    def __init__(self, config_path: str, state_path: str):
        self.config_path = config_path
        self.state_path  = state_path

        # 结构: {channel_name: {"base_url": str, "models": list[str], "keys": list[str]}}
        self.channels: dict[str, dict] = {}

        # 每个 key 的熔断器（跨渠道唯一，以 key 值为键）
        self.breakers: dict[str, CircuitBreaker] = {}

        # 每个渠道的轮盘游标 {channel_name: {"cursor": int, "model_cursors": {model: idx}}}
        self.rr_cursors: dict[str, dict] = {}

        # 每 key 累计统计（用于 status 子命令，非熔断用）
        self.stats: dict[str, dict] = {}

        # Codex 实证读取的短期脱敏观测；不持久化、不含 key/请求体/响应体。
        self._observation_lock = threading.Lock()
        self._observation_seq = 0
        self._observations: list[dict] = []

        self._load_config()
        self._load_state()

    def observe_route(self, adapter_id: str, model: str, channel: str,
                      base_url: str, protocol: str, status: int) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", adapter_id or ""):
            return
        with self._observation_lock:
            self._observation_seq += 1
            self._observations.append({
                "seq": self._observation_seq,
                "observed_at": utc_iso(utc_now()),
                "adapter_id": adapter_id,
                "requested_model": model,
                "channel": channel,
                "upstream_id": hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:16],
                "protocol": protocol,
                "status": status,
                "success": 200 <= status < 300,
            })
            del self._observations[:-OBSERVATION_LIMIT]

    def route_observations(self, adapter_id: str, since: int) -> dict:
        with self._observation_lock:
            records = [dict(item) for item in self._observations
                       if item["adapter_id"] == adapter_id and item["seq"] > since]
            cursor = self._observation_seq
        model_channels: dict[str, list[dict]] = {}
        for channel, meta in self.channels.items():
            upstream_id = hashlib.sha256(meta["base_url"].encode("utf-8")).hexdigest()[:16]
            for model in meta["models"]:
                model_channels.setdefault(model, []).append({
                    "channel": channel, "upstream_id": upstream_id,
                })
        return {"cursor": cursor, "model_channels": model_channels, "data": records}

    # ── 配置 I/O ──────────────────────────────────────────────────────────────

    def _load_config(self) -> None:
        with open(self.config_path, "rb") as fh:
            cfg = tomllib.load(fh)

        sv = cfg.get("server", {})
        self.port  = int(svc.get("port",  DEFAULT_PORT)) if (svc := cfg.get("server", {})) else DEFAULT_PORT
        self.token = (svc or {}).get("token", "")

        # 熔断器参数（可配置，回退到常量默认值）
        circ = cfg.get("circuit", {})
        cb_min_samples    = int(circ.get("min_samples",    CIRCUIT_MIN_SAMPLES))
        cb_error_rate     = float(circ.get("error_rate",     CIRCUIT_ERROR_RATE))
        cb_open_seconds   = float(circ.get("open_seconds",   CIRCUIT_OPEN_SECONDS))
        cb_half_open_need = int(circ.get("half_open_need", CIRCUIT_HALF_OPEN_NEED))

        self.channels = {}
        for ch in cfg.get("channels", []):
            name = ch["name"]
            self.channels[name] = {
                "base_url": ch["base_url"].rstrip("/"),
                "models":   [str(m) for m in ch.get("models", [])],
                "keys":     [str(k) for k in ch.get("keys", [])],
                "protocols": self._normalize_protocols(ch.get("protocols")),
            }
            self.rr_cursors.setdefault(name, {"cursor": 0, "model_cursors": {}})
            for k in self.channels[name]["keys"]:
                if k not in self.breakers:
                    self.breakers[k] = CircuitBreaker(
                        k,
                        min_samples    = cb_min_samples,
                        error_rate     = cb_error_rate,
                        open_seconds   = cb_open_seconds,
                        half_open_need = cb_half_open_need,
                    )
                self.stats.setdefault(k, {
                    "requests": 0, "failures": 0, "tripped": 0, "retries": 0
                })

    def _save_config(self) -> None:
        """持久化当前 channels / rr_cursors / breakers / stats 到 config.toml（含备份）。"""
        path = self.config_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        # 读现存内容，保留 userscape（channels 之外的段）
        existing_text = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                existing_text = fh.read()

        # 备份
        bak = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        if existing_text:
            shutil.copy2(path, bak)

        # 构造 channels 段
        lines: list[str] = []
        for name, ch in self.channels.items():
            lines.append(f'\n[[channels]]')
            lines.append(f'name = {json.dumps(name)}')
            lines.append(f'base_url = {json.dumps(ch["base_url"])}')
            models_str = ", ".join(json.dumps(m) for m in ch["models"])
            lines.append(f'models = [{models_str}]')
            keys_str = ", ".join(json.dumps(k) for k in ch["keys"])
            lines.append(f'keys = [{keys_str}]')
            protocols_str = ", ".join(json.dumps(p) for p in ch.get("protocols", ["responses", "chat/completions"]))
            lines.append(f'protocols = [{protocols_str}]')

        # 正则替换 channels 段（含 [[channels]] 到下一个同级段或文件末尾）
        import re
        channels_re = re.compile(
            r"^\[\[channels\]\].*?(?=^\[|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        channels_text = "\n".join(lines) + "\n"
        new_text = channels_re.sub(channels_text, existing_text) if channels_re.search(existing_text) \
                   else existing_text + channels_text

        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new_text)
            # TOML 校验
            with open(path, "rb") as fh:
                tomllib.load(fh)
        except Exception as exc:
            # 回滚
            if os.path.exists(bak):
                shutil.copy2(bak, path)
            else:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(existing_text)
            raise RuntimeError(f"配置写入失败，已回滚: {exc}") from exc

    # ── 状态 I/O ──────────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for d in raw.get("breakers", []):
                cb = CircuitBreaker.from_dict(d)
                self.breakers[cb.key] = cb
            saved_cursors = raw.get("rr_cursors", {})
            for name, cur in saved_cursors.items():
                if name in self.rr_cursors:
                    self.rr_cursors[name].update(cur)
        except Exception as exc:
            log.warning("状态文件加载失败（忽略）: %s", exc)

    def save_state(self) -> None:
        """原子写：先写临时文件，再 rename 覆盖。"""
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        payload = {
            "version":      1,
            "updated_at":   utc_iso(utc_now()),
            "breakers":     [cb.to_dict() for cb in self.breakers.values()],
            "rr_cursors":   self.rr_cursors,
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    # ── 渠道 / key 管理 ────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_protocols(protocols) -> list[str]:
        if protocols is None:
            return ["responses", "chat/completions"]
        if isinstance(protocols, str):
            protocols = [protocols]
        allowed = {"responses", "chat/completions"}
        result = [str(item).strip().lstrip("/") for item in protocols if str(item).strip().lstrip("/") in allowed]
        return result or ["responses", "chat/completions"]

    def add_channel(self, name: str, base_url: str, models: list[str], keys: list[str], protocols=None) -> None:
        self.channels[name] = {
            "base_url": base_url.rstrip("/"),
            "models":   models,
            "keys":     keys,
            "protocols": self._normalize_protocols(protocols),
        }
        self.rr_cursors.setdefault(name, {"cursor": 0, "model_cursors": {}})
        for k in keys:
            self.breakers.setdefault(k, CircuitBreaker(k))
            self.stats.setdefault(k, {"requests": 0, "failures": 0, "tripped": 0, "retries": 0})
        self._save_config()
        self.save_state()

    def remove_channel(self, name: str) -> None:
        if name not in self.channels:
            raise KeyError(f"渠道 '{name}' 不存在")
        # 清理状态
        self.rr_cursors.pop(name, None)
        for k in self.channels[name]["keys"]:
            self.breakers.pop(k, None)
            self.stats.pop(k, None)
        del self.channels[name]
        self._save_config()
        self.save_state()

    def add_key(self, channel: str, key: str) -> None:
        if channel not in self.channels:
            raise KeyError(f"渠道 '{channel}' 不存在")
        if key not in self.channels[channel]["keys"]:
            self.channels[channel]["keys"].append(key)
            self.breakers.setdefault(key, CircuitBreaker(key))
            self.stats.setdefault(key, {"requests": 0, "failures": 0, "tripped": 0, "retries": 0})
            self._save_config()
            self.save_state()

    def remove_key(self, channel: str, key: str) -> None:
        if channel not in self.channels:
            raise KeyError(f"渠道 '{channel}' 不存在")
        if key not in self.channels[channel]["keys"]:
            raise KeyError(f"key '{mask_key(key)}' 不在渠道 '{channel}'")
        self.channels[channel]["keys"].remove(key)
        self.breakers.pop(key, None)
        self.stats.pop(key, None)
        self._save_config()
        self.save_state()

    # ── 选路 ───────────────────────────────────────────────────────────────────

    def pick(self, model: str, protocol: str | None = None) -> tuple[str, dict] | None:
        """按 model 选 key（含轮盘游标 + 熔断跳过 + 跨渠道 fallback）。

        遍历所有挂载该 model 的渠道（轮盘顺序），跳过全熔断渠道，
        仅在全部渠道均无可用 key 时返回 None。
        """
        requested_protocol = protocol.lstrip("/") if protocol else None
        eligible_channels: list[str] = [
            n for n, ch in self.channels.items()
            if model in ch["models"]
            and (requested_protocol is None or requested_protocol in ch.get("protocols", ["responses", "chat/completions"]))
        ]
        if not eligible_channels:
            return None

        ch_count = len(eligible_channels)

        # 渠道级轮盘起点（model_cursors 存在第一个 eligible channel 的 cursor slot 中）
        first_ch_name = eligible_channels[0]
        first_slot = self.rr_cursors.setdefault(first_ch_name, {"cursor": 0, "model_cursors": {}})
        mc = first_slot.setdefault("model_cursors", {})
        ch_start = mc.get(model, 0) % ch_count

        # 轮询所有 eligible channels，跳过全熔断的渠道
        for ch_offset in range(ch_count):
            ch_idx = (ch_start + ch_offset) % ch_count
            channel_name = eligible_channels[ch_idx]
            ch = self.channels[channel_name]
            keys = ch["keys"]
            if not keys:
                continue

            cursor_slot = self.rr_cursors.setdefault(channel_name, {"cursor": 0, "model_cursors": {}})
            start = cursor_slot["cursor"] % len(keys)

            for i in range(len(keys)):
                idx = (start + i) % len(keys)
                k   = keys[idx]
                cb  = self.breakers.get(k)
                if cb is None or cb.allow():
                    cursor_slot["cursor"] = (idx + 1) % len(keys)
                    # 更新 model_cursors：指向下一渠道（供下次 pick 使用）
                    mc[model] = (ch_idx + 1) % ch_count
                    return k, {"channel": channel_name, "base_url": ch["base_url"]}

        return None  # 全部渠道全部熔断

    # ── 状态查询 ───────────────────────────────────────────────────────────────

    def channel_summary(self) -> list[dict]:
        result = []
        for name, ch in self.channels.items():
            entry = {
                "name":    name,
                "base_url": ch["base_url"],
                "models":  ch["models"],
                "protocols": ch.get("protocols", ["responses", "chat/completions"]),
                "keys":    [mask_key(k) for k in ch["keys"]],
                "breakers": {},
            }
            for k in ch["keys"]:
                cb = self.breakers.get(k)
                entry["breakers"][mask_key(k)] = cb.summary() if cb else {"state": "unknown"}
            result.append(entry)
        return result

    def key_status(self) -> list[dict]:
        rows = []
        for k, cb in sorted(self.breakers.items()):
            st = self.stats.get(k, {})
            rows.append({
                "key":        mask_key(k),
                "state":      cb.state,
                "requests":   st.get("requests",  0),
                "failures":   st.get("failures",  0),
                "retries":    st.get("retries",   0),
                "failure_rate": cb._failure_rate(),
                "opened_at":  utc_iso(cb.opened_at),
            })
        return rows


# ──────────────────────────────────────────────────────────────────────────────
# HTTP 请求处理器
# ──────────────────────────────────────────────────────────────────────────────
class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """OpenAI 兼容代理请求处理器。"""

    server_version = "TianjiProxy/1.0"

    def __init__(self, *args, pool: ProxyPool | None = None, **kwargs):
        self.pool = pool
        super().__init__(*args, **kwargs)

    # 日志静默
    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def _send_json(self, code: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── 认证 ───────────────────────────────────────────────────────────────────

    def _authenticate(self) -> bool:
        token = self.pool.token if self.pool else ""
        if not token:
            return True   # 未配置 token → 放行（fail-open 兼容）
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        provided = auth[7:]
        if not hmac.compare_digest(provided, token):
            return False
        return True

    # ── GET /v1/models ─────────────────────────────────────────────────────────

    def _handle_models(self) -> None:
        try:
            models_by_channel: dict[str, list[dict]] = {}
            for name, ch in self.pool.channels.items():
                for k in ch["keys"]:
                    cb = self.pool.breakers.get(k)
                    if cb is not None and not cb.allow():
                        continue
                    # 用第一个可用 key 拉 models
                    upstream = ch["base_url"] + "/models"
                    try:
                        result = self._http_get(upstream, k)
                        if result:
                            status, _, raw = result
                            if status == 200:
                                data = json.loads(raw)
                                models_by_channel[name] = data.get("data", [])
                                break
                    except Exception as exc:
                        log.warning("渠道 %s 拉 models 失败: %s", name, exc)

            # 聚合
            aggregated_by_id: dict[str, dict] = {}
            aggregated: list[dict] = []
            for name, model_list in models_by_channel.items():
                for m in model_list:
                    mid = m.get("id", "")
                    if not mid:
                        continue
                    entry = aggregated_by_id.get(mid)
                    if entry is None:
                        entry = {"id": mid, "object": "model", "owned_by": name,
                                 "tianji_channels": [name]}
                        aggregated_by_id[mid] = entry
                        aggregated.append(entry)
                    elif name not in entry["tianji_channels"]:
                        entry["tianji_channels"].append(name)
                        entry["owned_by"] = ",".join(entry["tianji_channels"])

            body = {"object": "list", "data": aggregated}
            self._send_json(200, body)
        except Exception as exc:
            log.exception("GET /v1/models 异常")
            self.send_error(502, f"models 聚合失败: {exc}")

    def _handle_observations(self, query: dict[str, list[str]]) -> None:
        adapter_id = (query.get("adapter_id") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", adapter_id):
            self.send_error(400, "invalid adapter_id")
            return
        try:
            since = max(0, int((query.get("since") or ["0"])[0]))
        except ValueError:
            self.send_error(400, "invalid since cursor")
            return
        self._send_json(200, self.pool.route_observations(adapter_id, since))

    # ── POST /v1/chat/completions | /v1/responses ───────────────────────────────

    def _handle_chat_completions(self) -> None:
        self._handle_model_request("/chat/completions")

    def _handle_responses(self) -> None:
        self._handle_model_request("/responses")

    def _handle_model_request(self, upstream_path: str) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            body: dict = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON body")
            return

        model = body.get("model", "")
        if not model:
            self.send_error(400, "缺少 model 字段")
            return

        key_info = self.pool.pick(model, upstream_path.lstrip("/"))
        if key_info is None:
            self.send_error(400, f"模型 '{model}' 无可用渠道")
            return

        key, meta = key_info
        upstream = meta["base_url"] + upstream_path
        adapter_id = self.headers.get("X-Tianji-Adapter", "")

        # 是否流式
        is_stream = bool(body.get("stream", False))

        # 构造上游请求头
        hdrs = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {key}",
        }
        for h_name in ("X-API-Key", "X-Api-Key"):
            if self.headers.get(h_name):
                hdrs[h_name] = self.headers.get(h_name)

        try:
            if is_stream:
                streamed, s_all_429, s_retry_after = self._relay_stream(
                    upstream, raw_body, hdrs, key, model, upstream_path,
                    adapter_id, meta["channel"], meta["base_url"],
                )
                if streamed:
                    return
                if s_all_429:
                    extra = {"Retry-After": s_retry_after} if s_retry_after else None
                    self._send_json(
                        429,
                        {"error": {"message": "所有上游均限流 (429)", "type": "rate_limit_error"}},
                        headers=extra,
                    )
                    return
                self._send_json(
                    502,
                    {"error": {"message": "所有上游成员失败 (all_members_failed)", "type": "proxy_error"}},
                )
                return

            status, _resp_headers, resp_body, all_429, retry_after = self._relay_with_retry(
                upstream, raw_body, hdrs, key, model, upstream_path,
                adapter_id, meta["channel"], meta["base_url"],
            )
        except Exception as exc:
            log.exception("转发异常")
            self._send_json(502, {"error": {"message": str(exc), "type": "proxy_error"}})
            return

        if status is not None and status < 500:
            # 2xx / 非 429 4xx：直接透传
            self._send_json(status, json.loads(resp_body) if resp_body else {})
            return

        if all_429:
            extra = {"Retry-After": retry_after} if retry_after else None
            self._send_json(
                429,
                {"error": {"message": "所有上游均限流 (429)", "type": "rate_limit_error"}},
                headers=extra,
            )
            return

        self._send_json(
            502,
            {"error": {"message": "所有上游成员失败 (all_members_failed)", "type": "proxy_error"}},
        )

    # ── 核心转发（含重试）───────────────────────────────────────────────────────

    def _relay_with_retry(
        self,
        upstream: str,
        body: bytes,
        headers: dict,
        start_key: str,
        model: str,
        upstream_path: str,
        adapter_id: str,
        start_channel: str,
        start_base_url: str,
    ) -> tuple[int | None, dict, bytes, bool, str | None]:
        """非流式转发。返回 (status, headers, body, all_429, retry_after)。

        status 为 None 表示重试耗尽:all_429=True 则全员限流,否则混合失败。
        """
        attempts: list[tuple[str, int]] = []
        used_keys = {start_key}
        retry_after: str | None = None
        current_channel = start_channel
        current_base_url = start_base_url

        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                # 换下一个可用 key（同时更新上游 URL）
                picked = self.pool.pick(model, upstream_path.lstrip("/"))
                if picked is None:
                    break
                key, meta = picked
                if key in used_keys:
                    break
                used_keys.add(key)
                upstream = meta["base_url"] + upstream_path
                current_channel = meta["channel"]
                current_base_url = meta["base_url"]
                headers["Authorization"] = f"Bearer {key}"
                log.warning(
                    "透明重试 #%d: model=%s 换 key=%s channel=%s",
                    attempt + 1, model, mask_key(key), meta["channel"],
                )
                st = self.pool.stats.setdefault(key, {})
                st["retries"] = st.get("retries", 0) + 1

            cur_key = headers.get("Authorization", "").replace("Bearer ", "")
            try:
                status, resp_headers, body_bytes = self._http_post(upstream, body, headers)
                self.pool.observe_route(adapter_id, model, current_channel, current_base_url,
                                        upstream_path.lstrip("/"), status)

                if status == 429:
                    retry_after = resp_headers.get("Retry-After", retry_after)
                    cb = self.pool.breakers.get(cur_key)
                    if cb:
                        cb.record_failure()
                    attempts.append((cur_key[:8], status))
                    continue

                if status < 500:
                    # 2xx / 非 429 4xx：直接透传，不重试；只给胜方记成功
                    cb = self.pool.breakers.get(cur_key)
                    if cb:
                        cb.record_success()
                    st = self.pool.stats.setdefault(cur_key, {})
                    st["requests"] = st.get("requests", 0) + 1
                    return status, resp_headers, body_bytes, False, retry_after

                # ≥500：可重试
                cb = self.pool.breakers.get(cur_key)
                if cb:
                    cb.record_failure()
                attempts.append((cur_key[:8], status))

            except Exception as exc:
                self.pool.observe_route(adapter_id, model, current_channel, current_base_url,
                                        upstream_path.lstrip("/"), -1)
                cb = self.pool.breakers.get(cur_key)
                if cb:
                    cb.record_failure()
                attempts.append((cur_key[:8], -1))
                log.warning("上游请求异常: %s", exc)

        # 重试耗尽
        has_429   = any(s == 429 for _, s in attempts)
        has_other = any(s != 429 for _, s in attempts)
        all_429   = has_429 and not has_other

        self.pool.save_state()
        return None, {}, b"", all_429, retry_after

    # ── HTTP 底层 ───────────────────────────────────────────────────────────────

    @staticmethod
    def _http_get(url: str, key: str) -> tuple[int, dict, bytes] | None:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {key}")
        try:
            resp = urllib.request.urlopen(req, timeout=CONN_TIMEOUT)
            try:
                return resp.status, dict(resp.headers), resp.read()
            finally:
                resp.close()
        except Exception as exc:
            log.warning("_http_get 失败 %s: %s", mask_key(key), exc)
            return None

    @staticmethod
    def _http_post(url: str, body: bytes, headers: dict) -> tuple[int, dict, bytes]:
        upstream_host = url.split("/")[2]
        path = url.split("/", 3)[-1] or "/"
        conn = http.client.HTTPConnection(upstream_host, timeout=CONN_TIMEOUT)
        conn.timeout = READ_TIMEOUT
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            body_bytes = resp.read()
            return resp.status, dict(resp.headers), body_bytes
        finally:
            conn.close()

    @staticmethod
    def _http_post_stream(
        url: str, body: bytes, headers: dict
    ) -> tuple[int, dict, http.client.HTTPConnection, http.client.HTTPResponse]:
        """流式上游请求:只读到响应头即返回,连接交由调用方逐块消费并关闭。"""
        upstream_host = url.split("/")[2]
        path = url.split("/", 3)[-1] or "/"
        conn = http.client.HTTPConnection(upstream_host, timeout=CONN_TIMEOUT)
        conn.timeout = READ_TIMEOUT
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.headers), conn, resp
        except Exception:
            conn.close()
            raise

    # ── SSE 流式透传 ───────────────────────────────────────────────────────────

    def _relay_stream(
        self,
        upstream: str,
        body: bytes,
        headers: dict,
        start_key: str,
        model: str,
        upstream_path: str,
        adapter_id: str,
        start_channel: str,
        start_base_url: str,
    ) -> tuple[bool, bool, str | None]:
        """流式转发。返回 (已开始流, all_429, retry_after)。

        拿到可流状态前允许换 key 重试;流一旦开始(响应头已发给客户端)不再换 key。
        """
        used_keys = {start_key}
        attempts: list[tuple[str, int]] = []
        retry_after: str | None = None
        current_channel = start_channel
        current_base_url = start_base_url

        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                picked = self.pool.pick(model, upstream_path.lstrip("/"))
                if picked is None:
                    break
                key, meta = picked
                if key in used_keys:
                    break
                used_keys.add(key)
                upstream = meta["base_url"] + upstream_path
                current_channel = meta["channel"]
                current_base_url = meta["base_url"]
                headers["Authorization"] = f"Bearer {key}"
                log.warning(
                    "流式透明重试 #%d: model=%s 换 key=%s channel=%s",
                    attempt + 1, model, mask_key(key), meta["channel"],
                )

            cur_key = headers.get("Authorization", "").replace("Bearer ", "")

            # 阶段 1:拿响应头。失败(连不上/超时)可以换 key 重来
            try:
                status, resp_headers, conn, resp = self._http_post_stream(upstream, body, headers)
            except Exception as exc:
                self.pool.observe_route(adapter_id, model, current_channel, current_base_url,
                                        upstream_path.lstrip("/"), -1)
                cb = self.pool.breakers.get(cur_key)
                if cb:
                    cb.record_failure()
                attempts.append((cur_key[:8], -1))
                log.warning("流式上游连接异常: %s", exc)
                continue

            if status == 429 or status >= 500:
                self.pool.observe_route(adapter_id, model, current_channel, current_base_url,
                                        upstream_path.lstrip("/"), status)
                # 还没给客户端发任何东西,可以换 key 重来
                if status == 429:
                    retry_after = resp_headers.get("Retry-After", retry_after)
                resp.read()
                conn.close()
                cb = self.pool.breakers.get(cur_key)
                if cb:
                    cb.record_failure()
                attempts.append((cur_key[:8], status))
                continue

            # 阶段 2:可流状态。响应头 + 逐块字节透传,此后不再换 key
            try:
                self.send_response(status)
                hop_by_hop = {
                    "connection", "keep-alive", "proxy-authenticate",
                    "proxy-authorization", "te", "trailers",
                    "transfer-encoding", "upgrade", "content-length",
                }
                for k, v in resp_headers.items():
                    if k.lower() not in hop_by_hop:
                        self.send_header(k, v)
                self.end_headers()

                while True:
                    chunk = resp.read1(CHUNK_SIZE)  # read1: 有数据即返回,不攒块
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                conn.close()
            except Exception as exc:
                # 流中途断裂:客户端连接已半开,只能记账兜底
                log.warning("流式转发中途断裂: %s", exc)
                self.pool.observe_route(adapter_id, model, current_channel, current_base_url,
                                        upstream_path.lstrip("/"), -1)
                try:
                    conn.close()
                except Exception:
                    pass
                cb = self.pool.breakers.get(cur_key)
                if cb:
                    cb.record_failure()
                return True, False, retry_after

            # 流完整结束才记成功
            self.pool.observe_route(adapter_id, model, current_channel, current_base_url,
                                    upstream_path.lstrip("/"), status)
            cb = self.pool.breakers.get(cur_key)
            if cb:
                cb.record_success()
            st = self.pool.stats.setdefault(cur_key, {})
            st["requests"] = st.get("requests", 0) + 1
            return True, False, retry_after

        has_429   = any(s == 429 for _, s in attempts)
        has_other = any(s != 429 for _, s in attempts)
        self.pool.save_state()
        return False, has_429 and not has_other, retry_after

    # ── dispatch ───────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in ("/v1/models", "/v1/tianji/observations"):
            if not self._authenticate():
                self.send_error(401, "Unauthorized")
                return
            if parsed.path == "/v1/models":
                self._handle_models()
            else:
                self._handle_observations(urllib.parse.parse_qs(parsed.query))
            return
        self.send_error(404, "Not Found")

    def do_POST(self):
        handlers = {
            "/v1/chat/completions": self._handle_chat_completions,
            "/v1/responses": self._handle_responses,
        }
        handler = handlers.get(self.path)
        if handler is not None:
            if not self._authenticate():
                self.send_error(401, "Unauthorized")
                return
            handler()
            return
        self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()


# ──────────────────────────────────────────────────────────────────────────────
# CLI 子命令
# ──────────────────────────────────────────────────────────────────────────────
def get_config_path() -> str:
    return os.environ.get("TJ_PROXY_CONFIG", DEFAULT_CONFIG)


def load_pool(config_path: str | None = None) -> ProxyPool:
    path = config_path or get_config_path()
    if not os.path.exists(path):
        print(f"错误: 配置文件不存在: {path}", file=sys.stderr)
        print("请先使用 add-channel 创建渠道，或手动编写配置文件。", file=sys.stderr)
        sys.exit(1)
    state_path = os.path.join(os.path.dirname(path), "proxy-state.json")
    return ProxyPool(path, state_path)


def cmd_serve(args: argparse.Namespace) -> None:
    """前台启动代理服务。"""
    pool = load_pool(args.config)
    port = args.port or pool.port
    addr = ("127.0.0.1", port)
    shutdown = threading.Event()

    def _signal_handler(signum, _frame):
        print(f"\n收到信号 {signum}，正在关闭...")
        shutdown.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    httpd = socketserver.ThreadingTCPServer(addr, lambda *a, **kw: ProxyHandler(*a, pool=pool, **kw))
    print(f"天机代理已启动: http://127.0.0.1:{port}")
    print(f"  配置: {pool.config_path}")
    print(f"  状态: {pool.state_path}")
    print(f"  Ctrl+C 退出")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        pool.save_state()
        print("代理已停止。")


def cmd_add_channel(args: argparse.Namespace) -> None:
    """新增渠道。"""
    path = args.config or get_config_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    # 确保配置文件存在
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[server]\nport = 8317\ntoken = \"\"\n\n")

    try:
        pool = ProxyPool(path, os.path.join(os.path.dirname(path), "proxy-state.json"))
    except Exception as exc:
        print(f"读取配置失败: {exc}", file=sys.stderr)
        sys.exit(1)

    pool.add_channel(
        name    = args.name,
        base_url= args.base_url,
        models  = [m.strip() for m in args.models.split(",") if m.strip()],
        keys    = [k.strip() for k in args.keys.split(",")  if k.strip()],
    )
    print(f"已添加渠道: {args.name} (models={args.models}, keys={len(args.keys.split(','))} 个)")
    bak = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    if os.path.exists(bak):
        print(f"  备份: {bak}")


def cmd_add_key(args: argparse.Namespace) -> None:
    """向渠道添加 key。"""
    pool = load_pool(args.config)
    pool.add_key(args.channel, args.key)
    print(f"已添加 key 到渠道 '{args.channel}': {mask_key(args.key)}")


def cmd_remove_key(args: argparse.Namespace) -> None:
    """从渠道移除 key。"""
    pool = load_pool(args.config)
    pool.remove_key(args.channel, args.key)
    print(f"已从渠道 '{args.channel}' 移除 key: {mask_key(args.key)}")


def cmd_remove_channel(args: argparse.Namespace) -> None:
    """删除渠道。"""
    pool = load_pool(args.config)
    pool.remove_channel(args.name)
    print(f"已删除渠道: {args.name}")


def cmd_list(_args: argparse.Namespace) -> None:
    """列出所有渠道及状态概览。"""
    pool = load_pool()
    summary = pool.channel_summary()
    if not summary:
        print("无渠道。请先使用 add-channel 添加。")
        return
    print(f"共 {len(summary)} 个渠道:\n")
    for entry in summary:
        print(f"渠道: {entry['name']}")
        print(f"  base_url : {entry['base_url']}")
        print(f"  models   : {', '.join(entry['models'])}")
        print(f"  keys ({len(entry['keys'])}): {', '.join(entry['keys'])}")
        for mk, bsum in entry["breakers"].items():
            print(f"    [{mk}] 状态={bsum['state']} 失败率={bsum['failure_rate']:.0%}")
        print()


def cmd_status(_args: argparse.Namespace) -> None:
    """每 key 详细状态。"""
    pool = load_pool()
    rows = pool.key_status()
    if not rows:
        print("无 key。请先使用 add-channel 添加。")
        return
    print(f"{'key':<18} {'state':<12} {'req':>6} {'fail':>5} {'retry':>6} {'fail_rate':>10}  opened_at")
    print("-" * 80)
    for r in rows:
        opened = r["opened_at"] or ""
        print(
            f"{r['key']:<18} {r['state']:<12} "
            f"{r['requests']:>6} {r['failures']:>5} {r['retries']:>6} "
            f"{r['failure_rate']:>9.0%}  {opened}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────────────────
def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)

    sub = sys.argv[1]

    if sub == "serve":
        ap = argparse.ArgumentParser(prog="tianji-proxy serve")
        ap.add_argument("--port",   type=int, default=None)
        ap.add_argument("--config", type=str, default=None)
        cmd_serve(ap.parse_args(sys.argv[2:]))
        return

    if sub == "add-channel":
        ap = argparse.ArgumentParser(prog="tianji-proxy add-channel")
        ap.add_argument("--name",    required=True)
        ap.add_argument("--base-url", required=True)
        ap.add_argument("--models",  required=True)
        ap.add_argument("--keys",    required=True)
        ap.add_argument("--config",  default=None)
        cmd_add_channel(ap.parse_args(sys.argv[2:]))
        return

    if sub == "add-key":
        ap = argparse.ArgumentParser(prog="tianji-proxy add-key")
        ap.add_argument("--channel", required=True)
        ap.add_argument("--key",     required=True)
        ap.add_argument("--config",  default=None)
        cmd_add_key(ap.parse_args(sys.argv[2:]))
        return

    if sub == "remove-key":
        ap = argparse.ArgumentParser(prog="tianji-proxy remove-key")
        ap.add_argument("--channel", required=True)
        ap.add_argument("--key",     required=True)
        ap.add_argument("--config",  default=None)
        cmd_remove_key(ap.parse_args(sys.argv[2:]))
        return

    if sub == "remove-channel":
        ap = argparse.ArgumentParser(prog="tianji-proxy remove-channel")
        ap.add_argument("--name",   required=True)
        ap.add_argument("--config", default=None)
        cmd_remove_channel(ap.parse_args(sys.argv[2:]))
        return

    if sub == "list":
        cmd_list(argparse.Namespace(config=None))
        return

    if sub == "status":
        cmd_status(argparse.Namespace(config=None))
        return

    print(f"未知子命令: {sub}", file=sys.stderr)
    print("可用子命令: serve | add-channel | add-key | remove-key | remove-channel | list | status",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
