"""额度检测与上下文健康度(票 11,规格书 14.1/14.2)。

- 信号源三层: ①claude 壳 statusline 机械上报上下文占用百分比(进程内官方状态)
  ②转录 usage 累加兜底(巡检顺带) ③cc-switch 账目表(只覆盖走 cc-switch 实例)
  +实例档案错误码归类(429=限流≠故障,不误杀)
- 总额黑盒: 目标=已尽必知、将尽有提示;不做官方 usage 接口轮询体系
- 上下文健康度两处检查点: 派活前分配器机械检查(装不下跳过/健康度低提示续接)
  +监控器巡检复查(派单后涨满可检出);阈值存 configs(实现期参数)
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import messages, ops
from .db import now

# 错误码归类(14.1③): 429=限流≠故障
ERROR_CLASSES = {429: "限流", 403: "权限/封禁", 401: "认证失效"}


def _key(instance: str) -> str:
    return f"quota:{instance}"


def _load(conn, instance: str) -> dict:
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (_key(instance),)).fetchone()
    return json.loads(row["value"]) if row else {}


def _save(conn, instance: str, data: dict):
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        (_key(instance), json.dumps(data, ensure_ascii=False), now()))


def report_context_pct(conn, instance: str, pct: float,
                       source: str = "statusline") -> dict:
    """statusline 机械上报入口(14.1①): 上下文窗口占用百分比进账本。

    高频上报只写 configs,不写审计(机械上报零噪音)。
    """
    data = _load(conn, instance)
    data.update({"context_pct": pct, "ts": now(), "source": source})
    _save(conn, instance, data)
    return {"instance": instance, "context_pct": pct}


def scan_transcript_usage(conn, instance: str, transcript_path: str) -> dict:
    """转录 usage 累加(14.1②): 解析 jsonl 里的 usage 字段累计 token。

    claude 转录格式: message.usage.{input_tokens,output_tokens,...};
    其它壳有 usage 字段即累加,没有跳过(读不了的壳=静态预估,14.2)。
    """
    data = _load(conn, instance)
    usage = data.get("usage") or {"input_tokens": 0, "output_tokens": 0,
                                  "lines": 0}
    p = Path(transcript_path)
    if not p.is_file():
        return {"instance": instance, "skipped": "转录不存在"}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"usage"' not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        u = (row.get("message") or {}).get("usage") or row.get("usage") or {}
        if not u:
            continue
        usage["input_tokens"] += int(u.get("input_tokens") or 0)
        usage["output_tokens"] += int(u.get("output_tokens") or 0)
        usage["lines"] += 1
    data["usage"] = usage
    _save(conn, instance, data)
    return {"instance": instance, "usage": usage}


def read_ccswitch(conn, db_path: str, instance: str) -> dict:
    """cc-switch 账目表读取+错误码归类(14.1③;未装 cc-switch 的环境跳过)。"""
    if not os.path.isfile(db_path):
        return {"skipped": "无 cc-switch 库(该层不适用)"}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT status_code, COUNT(*) AS n FROM proxy_request_logs"
            " GROUP BY status_code").fetchall()
    except sqlite3.Error:
        db.close()
        return {"skipped": "cc-switch 库无 proxy_request_logs 表"}
    db.close()
    summary = {}
    for r in rows:
        code = r["status_code"]
        cls = ERROR_CLASSES.get(code, "故障" if code >= 500 else "其他")
        summary[code] = {"class": cls, "count": r["n"]}
    data = _load(conn, instance)
    if 429 in summary:
        # 429=限流≠故障: 实例档案归类+额度已尽标记(12 的暂停派新活消费此信号)
        data["last_error"] = "rate_limit"
        data["exhausted"] = True
        ops.update_profile_notes(conn, instance, "429=限流(额度已尽),非故障")
    elif summary:
        data["last_error"] = ""
        data["exhausted"] = False
    data["ccswitch"] = summary
    _save(conn, instance, data)
    return {"instance": instance, "summary": summary}


def context_health(conn, instance: str) -> dict:
    """上下文健康度(14.2): pct/剩余窗口/提示。读不了转录的壳=静态预估调用方兜。"""
    data = _load(conn, instance)
    pct = float(data.get("context_pct") or 0)
    prof = conn.execute(
        "SELECT context_window FROM ability_profiles WHERE instance_name=?",
        (instance,)).fetchone()
    window = (prof["context_window"] if prof else 0) or 0
    remaining = window * (100 - pct) / 100 if window else 0
    threshold = float(ops._config(conn, "health_pct_threshold") or 85)
    hint = ""
    if pct >= threshold:
        hint = f"上下文占用 {pct:.0f}%,建议先续接(14.2)"
    return {"instance": instance, "pct": pct, "remaining": int(remaining),
            "window": window, "hint": hint, "exhausted": bool(data.get("exhausted"))}


def allocator_health_check(conn, task_id: int, expected_size: int,
                           candidates: list) -> dict:
    """派活前机械检查(14.2,供分配器调用): 返回 {skipped, hints, qualified_names}。

    - 额度已尽(exhausted)→跳过(12: 暂停派新活的消费侧在 allocator_pick)
    - 剩余窗口装不下→跳过(硬过滤)
    - 健康度低但装得下→不硬跳,产出续接提示
    """
    skipped, hints = {}, {}
    qualified = []
    for inst in candidates:
        h = context_health(conn, inst)
        if h["exhausted"]:
            skipped[inst] = "额度已尽(429 限流)"
            continue
        if h["window"] and h["remaining"] < expected_size:
            skipped[inst] = (f"剩余窗口 {h['remaining']} 装不下 "
                             f"{expected_size}(占用 {h['pct']:.0f}%)")
            continue
        if h["hint"]:
            hints[inst] = h["hint"]
        qualified.append(inst)
    return {"qualified": qualified, "skipped": skipped, "hints": hints}


def monitor_scan(conn):
    """监控器巡检复查(14.1 零新增常驻/14.2 第二检查点): 将尽提示+已尽必知。"""
    full_pct = float(ops._config(conn, "quota_full_pct") or 98)
    for inst in conn.execute(
            "SELECT name FROM instances WHERE is_active=1").fetchall():
        data = _load(conn, inst["name"])
        pct = float(data.get("context_pct") or 0)
        if data.get("exhausted"):
            continue  # 已尽已归类,不重复升级
        if pct >= full_pct:
            messages.send(
                conn, "escalation", "monitor",
                {"worker_id": inst["name"],
                 "reason": f"上下文占用 {pct:.0f}% 将尽(14.1 已尽必知,"
                           f"将尽有提示);建议续接或换实例"},
                "controller")
