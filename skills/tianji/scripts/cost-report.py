#!/usr/bin/env python3
"""cost-report.py - 天机 v2 编排成本实测表

用法:
  python cost-report.py --logs <new-api 日志目录>
  python cost-report.py --logs <new-api 日志目录> --wire <wire.jsonl 路径>

只使用 Python 标准库,日志和 wire 只读。
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

# Force UTF-8 on Windows stdout for Chinese characters
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---------- 日志解析 ----------

_LOG_RE = re.compile(
    r'record consume log: userId=\d+, params=(\{.*?\})\s*$'
)


def parse_logs(log_dir):
    """遍历 log_dir 下所有 .log 文件,提取 consume log 行,按 model_name 聚合并返回。
    同时返回 (bad_lines, total_consume_lines) 用于校错。
    """
    models = defaultdict(lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "calls": 0,
        "quota": 0,
    })
    bad_lines = 0
    total_consume_lines = 0

    if not os.path.isdir(log_dir):
        print(f"[ERROR] 日志目录不存在: {log_dir}", file=sys.stderr)
        sys.exit(1)

    log_files = sorted(f for f in os.listdir(log_dir) if f.endswith(".log"))

    for fname in log_files:
        fpath = os.path.join(log_dir, fname)
        with open(fpath, "r", encoding="utf-8") as fh:
            for line in fh:
                m = _LOG_RE.search(line)
                if not m:
                    continue
                total_consume_lines += 1
                try:
                    params = json.loads(m.group(1))
                except (json.JSONDecodeError, ValueError):
                    bad_lines += 1
                    continue

                model = params.get("model_name", "unknown")
                models[model]["prompt_tokens"] += params.get("prompt_tokens", 0)
                models[model]["completion_tokens"] += params.get("completion_tokens", 0)
                models[model]["calls"] += 1
                models[model]["quota"] += params.get("quota", 0)

    return dict(models), bad_lines, total_consume_lines


# ---------- wire 解析 ----------


def parse_wire(wire_path):
    """从 wire.jsonl 读取 token_counting.turn_recorded 事件,返回:
    (total_tokens, has_turn_recorded)
    如果 wire_path 为空或文件不存在返回 (None, False)
    """
    if not wire_path or not os.path.isfile(wire_path):
        return None, False

    total_tokens = 0
    has_turn_recorded = False

    with open(wire_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "token_counting.turn_recorded":
                total_tokens += event.get("tokens", 0)
                has_turn_recorded = True

    return total_tokens, has_turn_recorded


# ---------- 表格输出 ----------


def _cell(text, width):
    return str(text).rjust(width)


def print_table(models, session_tokens):
    keys = sorted(models.keys())
    col_w = [max(len(k) + 2, 10) for k in ["模型", "调用", "prompt", "completion", "quota"]]
    label_w = max(len(k) for k in ["模型", "调用", "prompt", "completion", "quota"]) + 2

    # header
    header = [
        _cell("模型", label_w),
        _cell("调用", col_w[1]),
        _cell("prompt", col_w[2]),
        _cell("completion", col_w[3]),
        _cell("quota", col_w[4]),
    ]
    sep = "-" * (label_w + 1) + "+-" + "-+-".join("-" * (w - 2) for w in col_w[1:]) + "-+"
    print("+-" + "-+-".join("-" * (w - 2) for w in [label_w] + col_w[1:]) + "-+")
    print("| " + " | ".join(header) + " |")
    print(sep)

    pool_prompt = 0
    pool_completion = 0
    pool_calls = 0
    pool_quota = 0

    for model in keys:
        d = models[model]
        row = [
            _cell(model, label_w),
            _cell(d["calls"], col_w[1]),
            _cell(d["prompt_tokens"], col_w[2]),
            _cell(d["completion_tokens"], col_w[3]),
            _cell(d["quota"], col_w[4]),
        ]
        print("| " + " | ".join(row) + " |")
        pool_prompt += d["prompt_tokens"]
        pool_completion += d["completion_tokens"]
        pool_calls += d["calls"]
        pool_quota += d["quota"]

    # 合计行
    row = [
        _cell("【池模型合计】", label_w),
        _cell(pool_calls, col_w[1]),
        _cell(pool_prompt, col_w[2]),
        _cell(pool_completion, col_w[3]),
        _cell(pool_quota, col_w[4]),
    ]
    print(sep)
    print("| " + " | ".join(row) + " |")

    # 主会话行
    if session_tokens is not None:
        row = [
            _cell("主会话(wire)", label_w),
            _cell("-", col_w[1]),
            _cell("-", col_w[2]),
            _cell("-", col_w[3]),
            _cell(session_tokens, col_w[4]),
        ]
        print(sep)
        print("| " + " | ".join(row) + " |")

    # 全局合计行
    total_prompt = pool_prompt
    total_completion = pool_completion
    total_cost = pool_quota + (session_tokens or 0)

    row = [
        _cell("【全局合计】", label_w),
        _cell(pool_calls, col_w[1]),
        _cell(total_prompt, col_w[2]),
        _cell(total_completion, col_w[3]),
        _cell(total_cost, col_w[4]),
    ]
    print(sep)
    print("| " + " | ".join(row) + " |")
    print("+" + "-+-".join("-" * (w - 2) for w in [label_w] + col_w[1:]) + "-+")

    # 关键指标
    pool_total_tokens = pool_prompt + pool_completion
    grand_total = pool_total_tokens + (session_tokens or 0)

    print()
    print("=== 关键指标 ===")
    print(f"  池模型 total_tokens  (prompt+completion): {pool_total_tokens:,}")
    if session_tokens is not None and grand_total > 0:
        ratio = pool_total_tokens / grand_total * 100
        print(f"  主会话 tokens (wire turn_recorded sum): {session_tokens:,}")
        print(f"  全局 total_tokens:                       {grand_total:,}")
        print(f"  池模型 tokens / 总 tokens 比例:           {ratio:.2f}%")
    else:
        print(f"  (未提供 --wire,主会话行留空)")


def main():
    parser = argparse.ArgumentParser(description="天机 v2 编排成本实测表")
    parser.add_argument(
        "--logs",
        required=True,
        help="new-api 日志目录",
    )
    parser.add_argument(
        "--wire",
        default=None,
        help="kimi main wire.jsonl 路径 (可选)",
    )
    args = parser.parse_args()

    # 1) 解析日志
    models, bad_lines, total_consume = parse_logs(args.logs)

    # 2) 解析 wire
    session_tokens, has_turn = parse_wire(args.wire)

    # 3) 输出表格
    print(f"日志目录 : {args.logs}")
    print(f"解析 consume log 行数 : {total_consume}  (坏行跳过: {bad_lines})")
    if args.wire:
        if has_turn:
            print(f"wire     : {args.wire}  (turn_recorded 事件已读取)")
        else:
            print(f"wire     : {args.wire}  (未找到 turn_recorded 事件)")
    print()
    print_table(models, session_tokens)


if __name__ == "__main__":
    main()
