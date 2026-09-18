#!/usr/bin/env python3
"""
pool-detect.py - 号池后端探测器
探测本机可用的模型池端点,列出模型清单。
用法: python pool-detect.py [--key KEY] [--timeout SECONDS]
"""

import json
import sys
import urllib.request
import urllib.error
import argparse


# ---------------------------------------------------------------------------
# 候选端点列表
# ---------------------------------------------------------------------------
ENDPOINTS = [
    ("http://localhost:3000/v1/models", "new-api / one-api"),
    ("http://localhost:8317/v1/models", "cc-switch"),
    ("http://localhost:7890/v1/models", "local-proxy"),
]


def probe_endpoint(url: str, timeout: int, api_key: str | None = None) -> dict:
    """
    探测单个端点,返回结构化结果。
    不抛异常,所有错误都落在 result 里。
    """
    result = {"url": url, "status": "dead", "models": [], "model_count": 0, "needs_key": False}

    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "tianji-pool-detect/1.0")

        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)

        # OpenAI 兼容格式: data 是 list of {id, ...}
        if isinstance(data, dict) and "data" in data:
            models_raw = data["data"]
        elif isinstance(data, list):
            models_raw = data
        else:
            models_raw = []

        model_ids = sorted(m.get("id", "") for m in models_raw if isinstance(m, dict) and m.get("id"))

        result["status"] = "alive"
        result["needs_key"] = False
        result["models"] = model_ids
        result["model_count"] = len(model_ids)

    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            result["status"] = "alive"
            result["needs_key"] = True
            result["note"] = f"HTTP {exc.code} {exc.reason}"
        elif exc.code == 403:
            result["status"] = "alive"
            result["needs_key"] = True
            result["note"] = f"HTTP {exc.code} {exc.reason}"
        else:
            result["note"] = f"HTTP {exc.code} {exc.reason}"

    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "Connection refused" in reason or "No connection" in reason or "actively refused" in reason:
            result["note"] = "connection refused"
        else:
            result["note"] = reason

    except json.JSONDecodeError as exc:
        result["note"] = f"invalid JSON response: {exc}"

    except Exception as exc:  # pragma: no cover
        result["note"] = f"{type(exc).__name__}: {exc}"

    return result


def format_report(results: list[dict], max_list: int = 20) -> str:
    """将探测结果格式化为终端可读的报告。"""
    lines = []
    lines.append("=" * 60)
    lines.append("  Tianji Pool 后端探测报告")
    lines.append("=" * 60)

    alive = 0
    for r in results:
        tag = {
            "alive": "[活]",
            "dead": "[死]",
        }.get(r["status"], "[?]")

        # 状态修饰
        if r.get("needs_key"):
            tag = "[活-需认证]"

        line = f"  {tag}  {r['url']}"
        lines.append(line)

        note = r.get("note", "")
        if note:
            lines.append(f"        注: {note}")

        count = r["model_count"]
        if r["status"] == "alive" and count > 0:
            lines.append(f"        模型数: {count}")
            model_list = r["models"]
            if count <= max_list:
                for mid in model_list:
                    lines.append(f"          - {mid}")
            else:
                for mid in model_list[:max_list]:
                    lines.append(f"          - {mid}")
                lines.append(f"          ... 还有 {count - max_list} 个")
            alive += 1
        else:
            lines.append(f"        模型数: 0")

        lines.append("")

    lines.append("-" * 60)
    lines.append(f"  总结: 共 {len(results)} 个端点, {alive} 个可用")
    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="号池后端探测器")
    parser.add_argument("--key", default=None, help="API Key (可选)")
    parser.add_argument("--timeout", type=int, default=3, help="单端点超时秒数 (默认 3)")
    args = parser.parse_args()

    results = []
    for url, label in ENDPOINTS:
        # 第一次:不带 key
        r = probe_endpoint(url, args.timeout, api_key=None)

        # 如果端点返回 401 且用户提供了 key,再带 key 重试一次
        if r["needs_key"] and args.key:
            r = probe_endpoint(url, args.timeout, api_key=args.key)
            if r["status"] == "alive":
                r["needs_key"] = False  # 带 key 后已通过认证

        results.append(r)

    report = format_report(results)
    print(report)
    sys.exit(0)


if __name__ == "__main__":
    main()
