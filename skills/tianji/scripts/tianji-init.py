#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tianji-init.py - 天机晨检脚本
======================
主模型在会话开始前运行此脚本,获取全部环境检查结果,直接按结论分支,不做任何探测性思考。

检查项:
  1. hooks 受管块  — config.toml 含 # >>> tianji-managed >>> 块且内有 SubagentStart/SubagentStop
  2. status_line    — tui.toml 含 [status_line]
  3. 子代理模型菜单 — [secondary_model.models] 非空
  4. 池活性          — 菜单有模型时,GET 每个 provider 的 base_url/models,2s 超时
  5. 状态文件        — <cwd>/.tianji/state.jsonl 是否存在(纯信息)

结论: READY | NEED_POOL | NEED_MODEL_SOURCE | NEED_ROLE_CONFIG | NEED_HOST_ADAPTER | NEED_ROUTE_PROOF | NEED_INSTALL | CHECK_UNAVAILABLE | DEGRADED
"""

import argparse
import os
import sys
import urllib.request
import urllib.error
import tomllib
import json

# 导入失败时 conclusions 可能本身就是坏的那一个，拿不到它的常量，只能写死这个
# 字面码；测试钉住它必须等于 conclusions.EXIT_UNUSABLE。
_NO_VERDICT_EXIT = 2

# 结论机是共享的：本脚本负责探活并提供事实，install.py 的 status 用自己的事实
# 调同一个函数——两处不可能再出现"各自判定就绪"。POOL_ALIVE 的含意也只有一份。
#
# 共享模块坏了就没有任何结论可给：这同样要走统一错误边界，打印真实异常类型和简短
# 原因后退"没得出任何结论"的码，而不是让 traceback 把退出码变成普通的 1。
try:
    import conclusions as _conclusions
    from doctor import (
        POOL_ALIVE,
        PoolEvidence,
        collect_facts,
        determine_conclusion,
        observe_menu,
    )
    from host_detectors import CodexDetector, KimiDetector, detect_host
except Exception as _exc:  # noqa: BLE001 - 任何导入失败都归入同一边界
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, LookupError, ValueError):
            pass
    print(
        f"[FAIL] 天机共享模块导入失败，无法得出结论: "
        f"{type(_exc).__name__}: {_exc}",
        file=sys.stderr,
    )
    raise SystemExit(_NO_VERDICT_EXIT) from _exc

# 宿主注册表
HOST_DETECTORS = {
    "kimi": KimiDetector,
    # "claude-code": ClaudeCodeDetector,  # 占位,待实现
    "codex": CodexDetector,
}


# Adapters whose files are present but whose import failed, keyed by host.
# Without this, a broken adapter is indistinguishable from an unregistered
# host: the real ImportError never surfaces and the caller is told the host
# does not exist.
HOST_ADAPTER_ERRORS = {}


def _cmdc_detector_path():
    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(skill_root, "host_adapters", "cmdc", "detector.py")


def register_host_adapters():
    """Attach host adapters that live outside the shared script directory.

    Keeps the shared conclusion machine host-agnostic: adapters register
    themselves, the core never branches on a host name.

    An adapter that is not there is simply not registered. An adapter that is
    there but cannot be imported is recorded in ``HOST_ADAPTER_ERRORS`` so the
    failure is reported as itself instead of as a missing host.
    """
    if "cmdc" in HOST_DETECTORS:
        return
    if not os.path.isfile(_cmdc_detector_path()):
        return
    try:
        skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if skill_root not in sys.path:
            sys.path.insert(0, skill_root)
        from host_adapters.cmdc.detector import CmdcDetector
    except Exception as exc:  # noqa: BLE001 - the reason is what we must keep
        HOST_ADAPTER_ERRORS["cmdc"] = f"{type(exc).__name__}: {exc}"
        return
    HOST_DETECTORS["cmdc"] = CmdcDetector


# ============================================================================
# 池活性检测
# ============================================================================

def check_pool_liveness(models, timeout=2):
    """GET {base_url}/models,返回 [(alias, status_str), ...]。

    status_str: "活" | "活-需认证" | "死" | "跳过(无URL)"
    """
    results = []
    for alias, provider, base_url in models:
        if not base_url:
            results.append((alias, "跳过(无URL)"))
            continue

        # 构造请求 URL
        url = base_url.rstrip("/") + "/models"
        status = "死"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = "活"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                status = "活-需认证"
            else:
                status = "死"
        except (urllib.error.URLError, OSError, TimeoutError):
            status = "死"
        except Exception:
            status = "死"
        results.append((alias, status))
    return results


# ============================================================================
# 菜单对账 + roles.toml 读取
# ============================================================================

def _read_roles_toml(path):
    """解析 roles.toml,返回 {角色: [模型别名列表]}。优先 tomllib,失败则手动解析 [roles] 段。"""
    result = {}
    if not os.path.isfile(path):
        return result
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        cfg = tomllib.loads(raw.decode('utf-8'))
        roles = cfg.get('roles', {})
        if isinstance(roles, dict):
            for role, models in roles.items():
                if isinstance(models, list):
                    result[str(role)] = [str(m) for m in models]
                elif isinstance(models, str):
                    result[str(role)] = [models]
            return result
    except Exception:
        pass
    # 手动解析 fallback
    text = raw.decode('utf-8', errors='replace')
    in_roles = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith('[') and line.endswith(']'):
            in_roles = (line[1:-1].strip() == 'roles')
            continue
        if not in_roles or not line or line.startswith('#'):
            continue
        if '=' in line:
            key, _, val = line.partition('=')
            key = key.strip().strip('"\'')
            val = val.strip()
            if not key:
                continue
            if val.startswith('[') and val.endswith(']'):
                inner = val[1:-1]
                models = [m.strip().strip("\"'") for m in inner.split(',') if m.strip()]
                if models:
                    result[key] = models
            else:
                m = val.strip("\"'")
                if m:
                    result[key] = [m]
    return result


def fetch_provider_models(base_url, api_key=None, timeout=2):
    """GET {base_url}/models,返回模型 id 集合。失败返回空集合(fail-open)。"""
    url = base_url.rstrip("/") + "/models"
    model_ids = set()
    try:
        req = urllib.request.Request(url, method="GET")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("data", []):
                mid = item.get("id", "")
                if mid:
                    model_ids.add(mid)
    except Exception:
        pass
    return model_ids


def resolve_menu_model_ids(config_path):
    """从 config.toml 解析 [secondary_model.models],返回 {alias: model_id}。

    pool mode: value 为空,从 [models."alias"].model 取 model_id
    direct mode: value 为 "直连:provider/model_id",从中取 model_id
    """
    result = {}
    try:
        with open(config_path, 'rb') as f:
            cfg = tomllib.load(f)
    except Exception:
        return result

    secondary = cfg.get("secondary_model", {})
    models_dict = secondary.get("models", {})
    if not models_dict:
        return result

    model_entries = cfg.get("models", {})

    for alias, value in models_dict.items():
        value_str = value.strip() if isinstance(value, str) else ""
        if value_str.startswith("直连:"):
            parts = value_str[3:].split("/", 1)
            if len(parts) == 2:
                result[str(alias)] = parts[1]
            else:
                result[str(alias)] = value_str
        else:
            # pool mode: value 为空,从 [models."alias"].model 取 model_id
            model_entry = model_entries.get(alias, {})
            model_id = model_entry.get("model", "")
            if not model_id:
                for mk, mv in model_entries.items():
                    if mk.endswith("/" + str(alias)):
                        model_id = mv.get("model", "")
                        if model_id:
                            break
            if model_id:
                result[str(alias)] = model_id

    return result


def check_menu_reconciliation(config_path, pool_results, menu_list):
    """对账段:汇总池模型 id 与菜单对比,返回输出行列表。"""
    lines = []

    # 收集所有活着的 provider base_url
    alive_base_urls = set()
    for alias, status in pool_results:
        if status in POOL_ALIVE:
            for a, prov, bu in menu_list:
                if a == alias and bu:
                    alive_base_urls.add(bu)

    if not alive_base_urls:
        lines.append("无存活 provider 可对账")
        return lines

    # 读取 providers 获取 api_key
    try:
        with open(config_path, 'rb') as f:
            cfg = tomllib.load(f)
    except Exception:
        lines.append("无法读取 config.toml")
        return lines

    all_providers = cfg.get("providers", {})
    provider_map = {}  # base_url -> api_key
    for prov_name, prov_info in all_providers.items():
        bu = prov_info.get("base_url", "")
        if bu in alive_base_urls:
            key = prov_info.get("api_key", "")
            provider_map[bu] = key

    # 获取所有池模型 id
    pool_model_ids = set()
    for bu, key in provider_map.items():
        ids = fetch_provider_models(bu, key if key else None)
        pool_model_ids.update(ids)

    if not pool_model_ids:
        lines.append("池返回空模型列表")
        return lines

    # 解析菜单
    menu_alias_to_id = resolve_menu_model_ids(config_path)
    if not menu_alias_to_id:
        lines.append("菜单为空")
        return lines

    menu_id_to_alias = {}
    for alias, mid in menu_alias_to_id.items():
        menu_id_to_alias[mid] = alias

    # 对比
    new_ids = pool_model_ids - set(menu_alias_to_id.values())
    gone_ids = set(menu_alias_to_id.values()) - pool_model_ids

    if not new_ids and not gone_ids:
        lines.append("MENU_OK")
        return lines

    if new_ids:
        lines.append(f"MENU_CHANGED 新增: {', '.join(sorted(new_ids))} → 可跑 /tianji.update 同步进菜单")

    if gone_ids:
        gone_alias_list = []
        for mid in sorted(gone_ids):
            alias = menu_id_to_alias.get(mid, mid)
            gone_alias_list.append(f"{alias}({mid})")
        lines.append(f"MENU_CHANGED 消失: {', '.join(gone_alias_list)} → 跑 /tianji.update 同步菜单")

        # 检查悬空绑定
        roles_path = os.environ.get("TJ_ROLES_FILE", "").strip()
        if not roles_path:
            roles_path = os.path.expanduser("~/.tianji/roles.toml")
        else:
            roles_path = os.path.expanduser(roles_path)

        roles = _read_roles_toml(roles_path)
        dangling = []
        for role, aliases in roles.items():
            for alias in aliases:
                if alias in menu_alias_to_id:
                    model_id = menu_alias_to_id[alias]
                    if model_id in gone_ids:
                        dangling.append((role, alias))

        if dangling:
            for role, alias in dangling:
                lines.append(f"悬空绑定: {role} → {alias}")

    return lines


# ============================================================================
# 打印辅助
# ============================================================================

def print_check(tag, ok, detail):
    prefix = "[OK]      " if ok else "[MISSING] "
    print(f"{prefix}{tag}: {detail}")


def print_pool_results(results):
    for alias, status in results:
        prefix = "[OK]      " if status in POOL_ALIVE else "[FAIL]    "
        print(f"{prefix}池 {alias}: {status}")


# ============================================================================
# 主流程
# ============================================================================

def run_checks(detector):
    """执行全部检查,返回 (checks_dict, pool_evidence, recon_lines)。

    事实采集由共享结论机的 collect_facts 负责（全是本地读取）；本脚本只多做
    两件 status 不做的事：池探活（联网）与菜单对账。池证据用 PoolEvidence 明确
    表达"无需探测 / 已探测 / 探测失败"，空列表不再兼作"没探测"。
    """
    checks = collect_facts(detector)
    menu = detector.menu_models()

    # 池活性 (仅当菜单非空且宿主支持外部 provider 对账)
    pool = PoolEvidence.not_required()
    if menu and detector.supports_menu_reconciliation():
        pool = PoolEvidence.probed(check_pool_liveness(menu))

    # 菜单对账 (信息性,不影响 READY/DEGRADED 判定)
    recon_lines = []
    if menu and detector.supports_menu_reconciliation():
        try:
            recon_lines = check_menu_reconciliation(
                detector.config_path(), list(pool.results), menu,
            )
        except Exception:
            recon_lines = ["[INFO] 菜单对账执行异常"]

    # 状态文件
    has_state = detector.state_file_exists()
    state_loc = os.path.join(os.getcwd(), ".tianji", "state.jsonl")
    checks["state"] = (None, f"存在 ({state_loc})" if has_state else f"不存在 ({state_loc})")

    # 子代理轮次上限:派遣方要按它裁任务书,否则任务书比预算大 = 那次直接零产出
    limits = getattr(detector, "subagent_limits", None)
    if callable(limits):
        try:
            table = limits()
        except Exception:
            table = {}
        if table:
            checks["subagent_limits"] = (
                None, " | ".join(f"{role} {turns} 轮" for role, turns in sorted(table.items())),
            )

    return checks, pool, recon_lines


def print_report(checks, pool, conclusion, suggestion, host_name, recon_lines):
    """按固定格式输出晨检报告。"""
    print(f"=== 天机晨检 (host: {host_name}) ===")

    # 1. hooks
    ok, detail = checks["hooks"]
    tag = "hooks"
    prefix = "[OK]      " if ok else "[MISSING] "
    print(f"{prefix}{tag}: {detail}")

    ok, detail = checks["roles"]
    prefix = "[OK]      " if ok else "[MISSING] "
    print(f"{prefix}roles: {detail}")

    ok, detail = checks["role_bindings"]
    prefix = "[OK]      " if ok else "[MISSING] "
    print(f"{prefix}role_bindings: {detail}")

    limits = checks.get("subagent_limits")
    if limits:
        print(f"[INFO]     子代理轮次上限: {limits[1]}")

    ok, detail = checks["routing"]
    print_check("模型接入", ok, detail)

    ok, detail = checks["probe"]
    print_check("路由证明", ok, detail)

    # 2. status_line
    ok, detail = checks["status_line"]
    prefix = "[OK]      " if ok else "[MISSING] "
    print(f"{prefix}status_line: {detail}")

    # 3. 子代理模型菜单
    ok, detail = checks["menu"]
    prefix = "[OK]      " if ok else "[MISSING] "
    print(f"{prefix}模型菜单: {detail}")

    # 4. 池活性
    if pool.results:
        print_pool_results(pool.results)
    else:
        menu_ok = checks["menu"][0]
        if not menu_ok:
            print("[SKIP]     池活性: 菜单为空,跳过")
        else:
            print("[SKIP]     池活性: 无需检测")

    # 4.5 菜单对账
    if recon_lines:
        for line in recon_lines:
            print(f"[INFO]     菜单对账: {line}")
    elif pool.results:
        print("[INFO]     菜单对账: 未执行")

    # 5. 状态文件
    _, detail = checks["state"]
    print(f"[INFO]     状态文件: {detail}")

    # 结论
    print(f"\n结论: {conclusion}")
    print(f"建议: {suggestion}")


def report_unresolvable_host(host_name, reason=""):
    """Report a host we could not resolve, and return "no verdict" exit code."""
    available = ", ".join(sorted(HOST_DETECTORS.keys()))
    if host_name:
        print(f"[MISSING] 未知宿主 '{host_name}',可用宿主: {available}")
        error = HOST_ADAPTER_ERRORS.get(host_name)
        if error:
            print(f"[FAIL]    宿主适配层存在但无法加载: {error}")
    else:
        print(f"[MISSING] 无法确定宿主（{reason}）；请显式传 --host {{{available}}}")
    return _conclusions.EXIT_UNUSABLE


def main():
    """Run the morning check and return the process exit code.

    The code is the caller's only mechanical signal, so it is never success by
    accident: only an actionable verdict returns 0, a verdict that blocks work
    returns 1, and a check that could not run at all returns 2.
    """
    # 编码解锁(学 statusline.py)。stderr 一起解锁：崩溃兜底会往 stderr 打中文，
    # 在非 UTF-8 控制台上编码失败会再抛异常，把 EXIT_UNUSABLE 退化成 1。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, LookupError, ValueError):
            pass

    # CLI 参数
    parser = argparse.ArgumentParser(
        description="天机晨检脚本:检查天机环境是否就绪。"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="宿主类型;省略时按宿主运行时信号探测,探不出就报错而不猜。"
             "可选宿主通过 HOST_DETECTORS 注册表扩展",
    )
    args = parser.parse_args()

    # 宿主解析：显式 --host 优先；否则只看宿主自己设的运行时信号。
    # 没有可靠默认值：以前默认 kimi，会把 command code 机器报成"kimi 没装"。
    host_name = args.host
    identification = "显式 --host"
    if not host_name:
        host_name, identification = detect_host()
    if host_name is None:
        register_host_adapters()
        return report_unresolvable_host(None, identification)
    if host_name not in HOST_DETECTORS:
        register_host_adapters()
    if host_name not in HOST_DETECTORS:
        return report_unresolvable_host(host_name)

    detector_class = HOST_DETECTORS[host_name]
    detector = detector_class()

    try:
        checks, pool, recon_lines = run_checks(detector)
        conclusion, suggestion = determine_conclusion(checks, pool)
        print_report(checks, pool, conclusion, suggestion, host_name, recon_lines)
    except Exception as e:
        # A crash is not a verdict. DEGRADED means "the model pool or channel is
        # confirmed dead", and a script that just fell over knows no such thing:
        # report the real exception and return the "no verdict" code instead of
        # inventing a conclusion.
        print(f"[FAIL] 晨检失败，无法得出结论: {type(e).__name__}: {e}")
        print(f"建议: {_conclusions.CRASH_SUGGESTION}")
        return _conclusions.EXIT_UNUSABLE
    return _conclusions.exit_code(conclusion)


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        print(f"[FAIL] 晨检无法完成: {exc}", file=sys.stderr)
        code = _conclusions.EXIT_UNUSABLE
    # A main() that returns nothing must not read as success: that dropped
    # return value is exactly how a failed check used to exit zero.
    sys.exit(code if isinstance(code, int) else _conclusions.EXIT_UNUSABLE)
