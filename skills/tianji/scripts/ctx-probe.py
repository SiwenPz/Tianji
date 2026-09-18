#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ctx-probe.py - 实测模型真实上下文窗口探针

用法:
  python ctx-probe.py --model <别名> [--config <路径>] [--expected N] [--timeout 秒]
  python ctx-probe.py --model <别名> --ladder [--expected N] [--timeout 秒]

说明:
  - 默认档(近零成本):2 发请求。第 1 发小 ping 验证模型活着并校准 prompt_tokens;
    第 2 发故意超限(约 expected*1.1 tokens),预期被后端拒绝,从错误文本解析真实窗口。
    超限请求被拒不计费。
  - --ladder 档(会计费):从 128k 倍增发直到 FAIL,再二分收敛。仅当默认档解析失败或
    expected 标小时启用。
  ⚠  仅用户显式要求"实测上下文"时才运行此脚本。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import tomllib

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def die(msg):
    """打印错误到 stderr 并退出。CTX_RESULT 由调用方负责输出。"""
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def find_config() -> str:
    """按优先级找 config 路径: KIMI_CODE_HOME > ~/.kimi-code/config.toml(--config 由 argparse 处理)。"""
    env_home = os.environ.get("KIMI_CODE_HOME", "").strip()
    if env_home:
        return os.path.join(os.path.expanduser(env_home), "config.toml")
    return os.path.expanduser("~/.kimi-code/config.toml")


def load_config(path: str) -> dict:
    """读取 config.toml,不存在则报错。"""
    if not os.path.isfile(path):
        die(f"config 文件不存在: {path}")
    with open(path, "rb") as f:
        return tomllib.load(f)


def find_model_entry(cfg: dict, alias: str) -> tuple[str, str, str, int]:
    """从 config 找模型,返回 (model_id, provider_name, base_url, api_key)。"""
    models_cfg = cfg.get("models", {})
    entry = models_cfg.get(alias)
    if not entry:
        die(f"模型别名 '{alias}' 不存在于 config 的 [models] 段")

    model_id = entry.get("model", "")
    provider_name = entry.get("provider", "")
    if not model_id or not provider_name:
        die(f"模型 '{alias}' 缺少 model 或 provider 字段")

    # 预期窗口:优先读模型块的 max_context_size,缺失用 128000
    expected_ctx = entry.get("max_context_size")
    if expected_ctx is None:
        expected_ctx = 128000
    else:
        expected_ctx = int(expected_ctx)

    # 找 provider 的 base_url + api_key
    providers_cfg = cfg.get("providers", {})
    prov = providers_cfg.get(provider_name)
    if not prov:
        die(f"provider '{provider_name}' 不存在于 config 的 [providers] 段")

    base_url = prov.get("base_url", "")
    api_key = prov.get("api_key", "")
    if not base_url:
        die(f"provider '{provider_name}' 缺少 base_url")

    return model_id, provider_name, base_url, api_key, expected_ctx


def build_request(base_url: str, model_id: str, api_key: str, n_tok_words: int, timeout: int):
    """构造超限测试请求体,返回 (req, body_json)。"""
    # 估算: "tok " * n ≈ n+15 tokens (glm 实测)
    big_text = "tok " * n_tok_words
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": big_text + "\n回复ok"}],
        "max_tokens": 8,
        "stream": False,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    return req, body


def send_shot(req, timeout: int, label: str) -> tuple[bool, int, str]:
    """发送请求,返回 (是否 PASS, prompt_tokens 或 0, 错误文本或 '')。
    PASS=True 表示 HTTP 200 且返回了 usage; 否则 FAIL。
    429 时 sleep 65s 最多重试 2 次。
    """
    retries = 0
    while True:
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                pt = data.get("usage", {}).get("prompt_tokens", 0)
                elapsed = int((time.time() - t0) * 1000)
                print(f"[{label}] PASS HTTP {resp.status} {elapsed}ms | prompt_tokens={pt}", flush=True)
                return True, pt, ""
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            elapsed = int((time.time() - t0) * 1000)
            # 429 限流: 重试最多 2 次
            if e.code == 429 and retries < 2:
                retries += 1
                print(f"[{label}] 429 RATE LIMITED {elapsed}ms | sleep 65s (retry {retries}/2)", flush=True)
                time.sleep(65)
                continue
            print(f"[{label}] FAIL HTTP {e.code} {elapsed}ms | {body_text[:600]}", flush=True)
            return False, 0, body_text
        except Exception as e:
            elapsed = int((time.time() - t0) * 1000)
            print(f"[{label}] ERROR {elapsed}ms | {repr(e)[:300]}", flush=True)
            return False, 0, repr(e)


def parse_window_from_error(body_text: str) -> int | None:
    """从错误文本解析真实上下文窗口。返回窗口值或 None。"""
    # 正则: "The input (N tokens) is longer than the model's context length (M tokens)"
    m = re.search(
        r"The input \((\d+) tokens\) is longer than the model's context length \((\d+) tokens\)",
        body_text,
    )
    if m:
        return int(m.group(2))
    return None


def ladder_probe(base_url: str, model_id: str, api_key: str, timeout: int,
                 model: str = "?", start_ctx: int = 128000, max_ctx: int = 2_000_000):
    """兜底阶梯二分探针。从 start_ctx 倍增发请求直到 FAIL,再二分收敛。
    返回 (窗口值, 总发数)。
    ⚠  本档会计费——每发 PASS 的请求都计费。
    """
    # 预估最大消耗
    max_probes = 0
    tmp = start_ctx
    while tmp < max_ctx:
        max_probes += 1
        tmp *= 2
    max_probes += 8  # 二分最多 8 轮
    max_tok = max_ctx * 11 // 10  # expected*1.1 估算
    cost_est = max_probes * (max_tok / 1_000_000)
    print(f"[LADDER] 预估最多 {max_probes} 发请求, 总输入约 {cost_est:.1f}M tokens, ⚠  本档会计费", flush=True)

    # 找上限:倍增直到 FAIL
    ctx = start_ctx
    last_pass_ctx = None
    last_pass_tokens = None
    total_probes = 0
    consecutive_fails = 0  # 连续失败计数器,防止无上限死循环

    while ctx <= max_ctx:
        n_words = ctx * 110 // 100  # 按目标 ctx 足量构造,不钳制
        req, _ = build_request(base_url, model_id, api_key, n_words, timeout)
        total_probes += 1
        ok, pt, err = send_shot(req, timeout, f"ladder-up-{ctx}")
        if ok:
            last_pass_ctx = ctx
            last_pass_tokens = pt
            ctx *= 2
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            # 第一次就 FAIL:尝试降半重试一次
            if last_pass_ctx is None and consecutive_fails == 1:
                ctx = max(start_ctx // 2, 64_000)
                continue
            # 降半后仍 FAIL 或已经有过一次降档:窗口太小,无法探测
            if last_pass_ctx is None:
                print(f"CTX_RESULT model={model} window=0 source=ladder_failed", flush=True)
                die("ladder: 窗口小于最小探测值 ({}),无法继续".format(ctx))
            break

    if last_pass_ctx is None or last_pass_tokens is None:
        print(f"CTX_RESULT model={model} window=0 source=ladder_failed", flush=True)
        die("ladder: 未找到任何 PASS 点,无法收敛")

    # 二分收敛:在 last_pass_ctx ~ ctx 之间
    lo = last_pass_ctx
    hi = ctx
    print(f"[LADDER] 二分范围: {lo} ~ {hi}", flush=True)
    for _ in range(12):  # 最多 12 轮
        if hi - lo <= int(lo * 0.05):  # ±5% 容差
            break
        mid = (lo + hi) // 2
        n_words = mid * 110 // 100  # 按目标 ctx 足量构造
        req, _ = build_request(base_url, model_id, api_key, n_words, timeout)
        total_probes += 1
        ok, pt, _ = send_shot(req, timeout, f"ladder-bisect-{mid}")
        if ok:
            lo = mid
            last_pass_tokens = pt
        else:
            hi = mid

    final_ctx = lo
    print(f"[LADDER] 收敛: 最后 PASS={last_pass_tokens} prompt_tokens at ctx≈{final_ctx}", flush=True)
    return final_ctx, total_probes


def main():
    import argparse

    parser = argparse.ArgumentParser(description="实测模型真实上下文窗口")
    parser.add_argument("--model", required=True, help="模型别名(config [models] 段的 key)")
    parser.add_argument("--config", default=None,
                        help="config 路径 (默认: --config 选项 / KIMI_CODE_HOME / ~/.kimi-code/config.toml)")
    parser.add_argument("--expected", type=int, default=None,
                        help="标称窗口大小(默认读 config 块的 max_context_size,缺失则 128000)")
    parser.add_argument("--ladder", action="store_true",
                        help="启用兜底阶梯二分(默认关闭,会计费)")
    parser.add_argument("--timeout", type=int, default=420,
                        help="请求超时秒数(默认 420)")
    args = parser.parse_args()

    config_path = args.config or find_config()
    cfg = load_config(config_path)
    model_id, provider_name, base_url, api_key, config_expected = \
        find_model_entry(cfg, args.model)

    expected = args.expected if args.expected is not None else config_expected
    print(f"[INFO] model={args.model} model_id={model_id} provider={provider_name}", flush=True)
    print(f"[INFO] base_url={base_url} expected_ctx={expected}", flush=True)

    # === 第 1 发: 小 ping ===
    req_ping, _ = build_request(base_url, model_id, api_key, 1, args.timeout)
    ok_ping, pt_ping, err_ping = send_shot(req_ping, args.timeout, "ping")
    if not ok_ping:
        # 模型甚至不响应
        print("CTX_RESULT model={} window=0 source=ping_failed".format(args.model), flush=True)
        die("模型 ping 失败,无法继续实测")

    print(f"[INFO] ping prompt_tokens={pt_ping} (calibration)", flush=True)

    # === 第 2 发: 故意超限 (expected * 1.1) ===
    # "tok " * n ≈ n+15 tokens (glm 校准),所以选 n = expected * 1.1
    n_over = int(expected * 1.1)
    print(f"[INFO] 第 2 发: 构造 ~{n_over} 词 ({n_over}+15 ≈ tokens) 超限请求", flush=True)
    req_over, _ = build_request(base_url, model_id, api_key, n_over, args.timeout)
    ok_over, _, err_over = send_shot(req_over, args.timeout, "over-limit")

    if not ok_over:
        # 解析错误中的真实窗口
        real_window = parse_window_from_error(err_over)
        if real_window:
            print(f"\n== 结论 ==", flush=True)
            print(f"model={args.model}", flush=True)
            print(f"真实上下文窗口: {real_window} tokens", flush=True)
            print(f"标称值: {expected}, 实测值: {real_window}, 差异: {real_window - expected:+d}", flush=True)
            print(f"CTX_RESULT model={args.model} window={real_window} source=error_text", flush=True)
            return
        else:
            print("[WARN] 错误文本不含可解析的窗口数字", flush=True)
            if not args.ladder:
                print("[INFO] 提示: 使用 --ladder 启用阶梯二分兜底(会计费)", flush=True)
                print(f"CTX_RESULT model={args.model} window=0 source=error_no_number", flush=True)
                die("错误响应格式无法解析,未启用 --ladder")
            # fall through to ladder
    else:
        # 意外 PASS: expected 标小了
        print(f"[WARN] 超限请求意外 PASS! 说明标称值 {expected} 远小于真实窗口", flush=True)
        if not args.ladder:
            print(f"[INFO] 使用 --expected <更大的值> 重跑, 或 --ladder 自动递增探底", flush=True)
            print(f"CTX_RESULT model={args.model} window=0 source=expected_too_small", flush=True)
            die("标称值偏小,未启用 --ladder")
        # fall through to ladder

    # === 兜底阶梯二分 ===
    if args.ladder:
        print(f"[LADDER] 启动兜底阶梯二分(会计费)", flush=True)
        real_window, n_probes = ladder_probe(base_url, model_id, api_key, args.timeout, model=args.model)
        print(f"\n== 结论 ==", flush=True)
        print(f"model={args.model}", flush=True)
        print(f"真实上下文窗口: ~{real_window} tokens (阶梯二分,±5%)", flush=True)
        print(f"CTX_RESULT model={args.model} window={real_window} source=ladder", flush=True)


if __name__ == "__main__":
    main()
