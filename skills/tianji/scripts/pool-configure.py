#!/usr/bin/env python3
"""pool-configure.py - TOML pool/direct configuration writer

用法:
  python pool-configure.py --base-url <池地址> --key <key> --model 别名=模型id --default <主力别名>
  python pool-configure.py --direct --provider "名称=type=base_url=key" --model "别名=provider/模型id" --default <主力别名>
  python pool-configure.py ... --role 工人=step-explore --role 审核=deepseek-v4
  python pool-configure.py ... --max-context-size 777777 --ctx "z-ai/glm-5.3-free=1048576"
  python pool-configure.py --set-ctx "别名=1048576"   # 独立模式: 原地更新 config [models] 里已存在别名的 max_context_size 一行

--role 会同步写入角色花名册 (~/.tianji/roles.toml 或 TJ_ROLES_FILE 指定路径),
并替换同名角色的既有指派。
--max-context-size 控制全局默认上下文窗口（默认 128000）。
--ctx 支持 per-model 覆盖,例: --ctx "z-ai/glm-5.3-free=1048576"。
"""

import argparse
import datetime
import os
import shutil
import sys
import tomllib
import json
import urllib.error
import urllib.request


def mask_key(key: str) -> str:
    """Mask API key: first 4, last 4, *** in middle."""
    if len(key) <= 8:
        return key[:2] + "***" + key[-2:]
    return key[:4] + "***" + key[-4:]


def find_block_range(lines: list[str], header: str) -> tuple[int | None, int | None]:
    """Find the start and end indices of a block with the given header.

    Returns (start, end) where end is exclusive, or (None, None) if not found.
    The block extends from the header line to the line before the next table header
    (a line starting with '[' and ending with ']') or to the end of file.
    """
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break

    if start is None:
        return None, None

    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            end = i
            break

    return start, end


def ensure_blank_line_before(lines: list[str]) -> None:
    """Ensure there's a blank line before the next content, if lines is not empty."""
    if lines and lines[-1].strip() != "":
        lines.append("\n")


def build_providers_block(base_url: str, api_key: str) -> list[str]:
    """Build the [providers.local-pool] block lines."""
    return [
        "[providers.local-pool]\n",
        'type = "openai"\n',
        f'base_url = "{base_url}"\n',
        f'api_key = "{api_key}"\n',
        "\n"
    ]


def build_model_block(alias: str, model_id: str, max_ctx: int = 128000) -> list[str]:
    """Build a [models."alias"] block lines."""
    return [
        f'[models."{alias}"]\n',
        f'provider = "local-pool"\n',
        f'model = "{model_id}"\n',
        f'max_context_size = {max_ctx}\n',
        'capabilities = [ "tool_use" ]\n',
        "\n"
    ]


def build_secondary_model_block(default_model: str) -> list[str]:
    """Build the [secondary_model] block lines."""
    return [
        "[secondary_model]\n",
        f'default_model = "{default_model}"\n',
        "\n"
    ]


def build_secondary_models_block(models: dict[str, str]) -> list[str]:
    """Build the [secondary_model.models] block lines."""
    lines = ["[secondary_model.models]\n"]
    for alias in models:
        lines.append(f'"{alias}" = ""\n')
    lines.append("\n")
    return lines


def parse_provider(spec: str) -> tuple[str, str, str, str]:
    """Parse provider spec: name=type=base_url=key"""
    parts = spec.split('=', 3)
    if len(parts) != 4:
        print(f"Error: invalid provider format '{spec}', expected name=type=base_url=key",
              file=sys.stderr)
        sys.exit(1)
    name, ptype, base_url, api_key = parts
    if not all([name, ptype, base_url, api_key]):
        print(f"Error: invalid provider format '{spec}', all fields must be non-empty",
              file=sys.stderr)
        sys.exit(1)
    return name, ptype, base_url, api_key


def build_direct_provider_block(name: str, ptype: str, base_url: str, api_key: str) -> list[str]:
    """Build a [providers."name"] block lines for direct mode."""
    return [
        f'[providers."{name}"]\n',
        f'type = "{ptype}"\n',
        f'base_url = "{base_url}"\n',
        f'api_key = "{api_key}"\n',
        "\n"
    ]


def build_direct_model_block(alias: str, provider_name: str, model_id: str, max_ctx: int = 128000) -> list[str]:
    """Build a [models."alias"] block lines for direct mode."""
    return [
        f'[models."{alias}"]\n',
        f'provider = "{provider_name}"\n',
        f'model = "{model_id}"\n',
        f'max_context_size = {max_ctx}\n',
        'capabilities = [ "tool_use" ]\n',
        "\n"
    ]


def parse_model_spec(spec: str) -> tuple[str, str, str]:
    """Parse direct-mode model spec: alias=provider_name/model_id"""
    parts = spec.split('=', 1)
    if len(parts) != 2:
        print(f"Error: invalid model format '{spec}', expected alias=provider/model_id",
              file=sys.stderr)
        sys.exit(1)
    alias = parts[0].strip()
    provider_model = parts[1].strip()
    if '/' not in provider_model:
        print(f"Error: invalid model format '{spec}', expected alias=provider/model_id (missing '/')",
              file=sys.stderr)
        sys.exit(1)
    slash_idx = provider_model.index('/')
    provider_name = provider_model[:slash_idx]
    model_id = provider_model[slash_idx + 1:]
    if not alias or not provider_name or not model_id:
        print(f"Error: invalid model format '{spec}', alias, provider and model_id must be non-empty",
              file=sys.stderr)
        sys.exit(1)
    return alias, provider_name, model_id


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


def resolve_max_ctx_global(ctx_overrides, default_max_ctx, alias, model_id):
    """Per-model 覆盖优先于全局默认。别名优先匹配,其次模型 id。"""
    if alias in ctx_overrides:
        return ctx_overrides[alias]
    if model_id in ctx_overrides:
        return ctx_overrides[model_id]
    return default_max_ctx


def read_providers_from_config(config_path):
    """从 config.toml 读取 providers,返回 [(name, type, base_url, api_key), ...]。"""
    providers = []
    try:
        with open(config_path, 'rb') as f:
            cfg = tomllib.load(f)
        for name, info in cfg.get("providers", {}).items():
            ptype = info.get("type", "openai")
            base_url = info.get("base_url", "")
            api_key = info.get("api_key", "")
            if base_url:
                providers.append((str(name), str(ptype), str(base_url), str(api_key)))
    except Exception:
        pass
    return providers


def get_menu_entries(config_path):
    """从 config.toml 读取 [secondary_model.models],返回 [(alias, is_direct, model_id), ...]。"""
    entries = []
    try:
        with open(config_path, 'rb') as f:
            cfg = tomllib.load(f)
        models_dict = cfg.get("secondary_model", {}).get("models", {})
        model_entries = cfg.get("models", {})
        for alias, value in models_dict.items():
            v = value.strip() if isinstance(value, str) else ""
            if v.startswith("直连:"):
                parts = v[3:].split("/", 1)
                model_id = parts[1] if len(parts) == 2 else v
                entries.append((str(alias), True, model_id))
            else:
                model_entry = model_entries.get(alias, {})
                model_id = model_entry.get("model", "")
                if not model_id:
                    for mk, mv in model_entries.items():
                        if mk.endswith("/" + str(alias)):
                            model_id = mv.get("model", "")
                            if model_id:
                                break
                entries.append((str(alias), False, model_id))
    except Exception:
        pass
    return entries


def handle_sync(config_path, args):
    """Sync [secondary_model.models] from pool providers in config.toml。"""
    report_lines = []

    if not os.path.isfile(config_path):
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    # 备份
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{config_path}.bak-{timestamp}"
    shutil.copy2(config_path, backup_path)
    print(f"Backup created: {backup_path}")

    # 读取 providers
    providers = read_providers_from_config(config_path)
    if not providers:
        report_lines.append("[INFO] --sync: 未找到可用的 provider 配置,保留现有菜单")
        # 仍然校验 TOML
        try:
            with open(config_path, 'rb') as f:
                tomllib.load(f)
        except Exception as e:
            print(f"Error: config.toml invalid: {e}", file=sys.stderr)
            shutil.copy2(backup_path, config_path)
            sys.exit(1)
        for line in report_lines:
            print(line)
        print(f"Config validated: {config_path}")
        return

    # 获取池模型 id
    all_pool_ids = set()
    provider_model_counts = {}
    provider_models = {}  # name -> ids,给新模型挂 provider 用
    for name, ptype, base_url, api_key in providers:
        ids = fetch_provider_models(base_url, api_key if api_key else None)
        provider_model_counts[name] = len(ids)
        provider_models[name] = ids
        all_pool_ids.update(ids)

    # 空池守卫:providers 在但一个模型都没拉到(池全死/全空)→ fail-open 只报告,
    # 绝不清菜单(空池 ≠ 所有模型消失,多半是没拉到)
    if not all_pool_ids:
        for name, count in provider_model_counts.items():
            report_lines.append(f"[INFO] --sync: provider '{name}' 拉到 {count} 个模型")
        report_lines.append("[WARN] --sync: 池无存活模型,保留现有菜单(未做任何改写)")
        for line in report_lines:
            print(line)
        return

    # 读取现有菜单
    menu_entries = get_menu_entries(config_path)

    # 分离 pool 和 direct 条目
    pool_entries = {}   # alias -> model_id
    direct_entries = {} # alias -> raw value (e.g. "provider/model_id")
    for alias, is_direct, model_id in menu_entries:
        if is_direct:
            # 重新读取原始值以保留完整格式
            try:
                with open(config_path, 'rb') as f:
                    cfg = tomllib.load(f)
                raw_val = cfg.get("secondary_model", {}).get("models", {}).get(alias, model_id)
                direct_entries[alias] = raw_val
            except Exception:
                direct_entries[alias] = model_id
        elif model_id:
            pool_entries[alias] = model_id

    # 保留存活 pool 条目
    new_pool_entries = {}
    removed_aliases = []
    for alias, mid in pool_entries.items():
        if mid in all_pool_ids:
            new_pool_entries[alias] = mid
        else:
            removed_aliases.append((alias, mid))

    # 追加新池模型
    existing_ids = set(new_pool_entries.values())
    new_count = 0
    for pid in sorted(all_pool_ids):
        if pid not in existing_ids:
            new_alias = pid
            if new_alias in new_pool_entries or new_alias in direct_entries:
                new_alias = f"{pid}-pool"
            new_pool_entries[new_alias] = pid
            new_count += 1

    # 确定默认模型
    final_default = args.default
    if final_default not in new_pool_entries and final_default not in direct_entries:
        all_aliases = sorted(set(list(new_pool_entries.keys()) + list(direct_entries.keys())))
        if all_aliases:
            final_default = all_aliases[0]
            report_lines.append(f"[INFO] --sync: 默认模型 '{args.default}' 已消失,自动选择 '{final_default}'")

    report_lines.append(
        f"[INFO] --sync: 池 {len(all_pool_ids)} 模型, "
        f"保留池条目 {len(new_pool_entries)} (移除 {len(removed_aliases)}, 新增 {new_count})"
    )

    # 检查悬空绑定
    roles_path = os.environ.get("TJ_ROLES_FILE", "").strip()
    if not roles_path:
        roles_path = os.path.expanduser("~/.tianji/roles.toml")
    else:
        roles_path = os.path.expanduser(roles_path)

    roles = _parse_roles_toml(roles_path)
    dangling = []
    for role, aliases in roles.items():
        for alias in aliases:
            if alias in pool_entries:
                mid = pool_entries[alias]
                if mid not in all_pool_ids:
                    dangling.append((role, alias))

    if dangling:
        for role, alias in dangling:
            report_lines.append(f"悬空绑定: {role} → {alias}")

    for line in report_lines:
        print(line)

    # 读取现有文件行
    with open(config_path, 'r', encoding='utf-8') as f:
        existing_lines = f.readlines()

    lines = list(existing_lines)

    # 更新 [secondary_model] default
    sec_block = build_secondary_model_block(final_default)
    start, end = find_block_range(lines, "[secondary_model]")
    if start is not None:
        lines[start:end] = sec_block
    else:
        ensure_blank_line_before(lines)
        lines.extend(sec_block)

    # 移除旧 pool 条目的 [models."alias"] 块
    for alias, mid in removed_aliases:
        header = f'[models."{alias}"]'
        start, end = find_block_range(lines, header)
        if start is not None:
            lines[start:end] = []

    # 添加新 pool 条目的 [models."alias"] 块
    # 新模型挂到"列表里真有它"的 provider——不能盲用第一个有 base_url 的:
    # config 里可能还有 managed:kimi-code 这类内置 provider(2026-09-06 实测踩坑,
    # 新模型挂错 provider 会绕过池直连内置渠道)
    first_provider_name = None
    for name, ptype, base_url, api_key in providers:
        if base_url:
            first_provider_name = name
            break

    def provider_of(mid):
        for name, ptype, base_url, api_key in providers:
            if mid in provider_models.get(name, set()):
                return name
        return first_provider_name or "local-pool"

    for alias, mid in new_pool_entries.items():
        if alias not in pool_entries:  # 只对新条目创建 [models."alias"] 块
            pname = provider_of(mid)
            max_ctx = resolve_max_ctx_global(args.ctx_overrides, args.max_context_size, alias, mid)
            block = build_model_block(alias, mid, max_ctx=max_ctx)
            # 修改 block 中的 provider
            for i, line in enumerate(block):
                if line.startswith('provider = '):
                    block[i] = f'provider = "{pname}"\n'
            header = f'[models."{alias}"]'
            start, end = find_block_range(lines, header)
            if start is not None:
                lines[start:end] = block
            else:
                ensure_blank_line_before(lines)
                lines.extend(block)

    # 更新 [secondary_model.models]
    sm_lines = ["[secondary_model.models]\n"]
    for alias in sorted(direct_entries.keys()):
        val = direct_entries[alias]
        sm_lines.append(f'"{alias}" = "{val}"\n')
    for alias in sorted(new_pool_entries.keys()):
        sm_lines.append(f'"{alias}" = ""\n')
    sm_lines.append("\n")

    start, end = find_block_range(lines, "[secondary_model.models]")
    if start is not None:
        lines[start:end] = sm_lines
    else:
        ensure_blank_line_before(lines)
        lines.extend(sm_lines)

    # 写入
    content = ''.join(lines)
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"Error writing config: {e}", file=sys.stderr)
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, config_path)
            print(f"Rolled back from backup: {backup_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, 'rb') as f:
            tomllib.load(f)
    except Exception as e:
        print(f"Error: generated config is invalid TOML: {e}", file=sys.stderr)
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, config_path)
            print(f"Rolled back from backup: {backup_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Config updated successfully: {config_path}")
    print(f"Default model: {final_default}")
    print(f"Pool entries: {', '.join(sorted(new_pool_entries.keys()))}")


def handle_set_ctx(config_path: str, specs: list[tuple[str, int]]) -> None:
    """--set-ctx 独立模式: 原地替换 [models.\"别名\"] 块的 max_context_size 一行。

    替换语义(非追加)。所有 spec 先全量预校验: 别名必须已存在于 config 的 [models],
    任一不存在 → 一条都不写、不留 .bak,exit 1。备份 → 逐块定位替换/插入 →
    写回 → tomllib 校验 → 失败回滚。输出每条 [OK] set-ctx 别名: 旧值 → 新值。
    """
    if not os.path.isfile(config_path):
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    # 全量预校验: 所有别名必须已存在于 [models],失败不写、不留 .bak
    with open(config_path, 'rb') as f:
        cfg = tomllib.load(f)
    models_cfg = cfg.get("models", {})
    if not isinstance(models_cfg, dict):
        models_cfg = {}
    missing = [alias for alias, _ in specs if alias not in models_cfg]
    if missing:
        print(f"Error: --set-ctx 别名在 config 的 [models] 中不存在: {', '.join(missing)}",
              file=sys.stderr)
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    malformed = [alias for alias, _ in specs
                 if find_block_range(lines, f'[models."{alias}"]')[0] is None]
    if malformed:
        print(f"Error: --set-ctx 找不到规范 [models.\"别名\"] 块: {', '.join(malformed)}",
              file=sys.stderr)
        sys.exit(1)

    # 备份
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{config_path}.bak-{timestamp}"
    shutil.copy2(config_path, backup_path)
    print(f"Backup created: {backup_path}")

    results = []
    for alias, new_val in specs:
        header = f'[models."{alias}"]'
        start, end = find_block_range(lines, header)
        if start is None:
            # 预校验通过后理论不可达;防御性回滚
            print(f"Error: 找不到 [models.\"{alias}\"] 块", file=sys.stderr)
            shutil.copy2(backup_path, config_path)
            sys.exit(1)
        block = lines[start:end]

        # 块内已有 max_context_size 行 → 原位替换;否则插在 model = 行后
        mc_idx = None
        for i, line in enumerate(block):
            stripped = line.strip()
            if '=' in stripped and stripped.split('=', 1)[0].strip() == "max_context_size":
                mc_idx = i
                break

        if mc_idx is not None:
            line = block[mc_idx]
            old_val = line.split('=', 1)[1].strip()
            indent = line[:len(line) - len(line.lstrip())]
            block[mc_idx] = f"{indent}max_context_size = {new_val}\n"
        else:
            old_val = "(none)"
            insert_at = None
            for i, line in enumerate(block):
                stripped = line.strip()
                if '=' in stripped and stripped.split('=', 1)[0].strip() == "model":
                    insert_at = i + 1
                    break
            if insert_at is None:
                insert_at = 1  # 无 model 行,插在块头后
            block.insert(insert_at, f"max_context_size = {new_val}\n")

        # 修改的是切片副本,写回 lines
        lines[start:end] = block

        results.append((alias, old_val, new_val))

    # 写回
    content = ''.join(lines)
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"Error writing config: {e}", file=sys.stderr)
        shutil.copy2(backup_path, config_path)
        print(f"Rolled back from backup: {backup_path}", file=sys.stderr)
        sys.exit(1)

    # tomllib 校验,失败回滚
    try:
        with open(config_path, 'rb') as f:
            tomllib.load(f)
    except Exception as e:
        print(f"Error: generated config is invalid TOML: {e}", file=sys.stderr)
        shutil.copy2(backup_path, config_path)
        print(f"Rolled back from backup: {backup_path}", file=sys.stderr)
        sys.exit(1)

    for alias, old_val, new_val in results:
        print(f"[OK] set-ctx {alias}: {old_val} → {new_val}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure pool/direct settings in TOML config")
    parser.add_argument("--base-url", default=None, help="Pool base URL")
    parser.add_argument("--key", default=None, help="Pool API key")
    parser.add_argument("--direct", action="store_true", help="Use direct connection mode")
    parser.add_argument("--provider", action="append", default=None,
                        help="Provider name=type=base_url=key (repeatable, --direct only)")
    parser.add_argument("--model", action="append", default=None,
                        help="Model alias=model_id (pool) or alias=provider/model_id (--direct); repeatable")
    parser.add_argument("--default", required=False, help="Default sub-agent model alias (full-write 模式必填; --sync/--set-ctx 模式可省)")
    parser.add_argument("--config", default=None,
                        help="Config file path (default: $KIMI_CODE_HOME/config.toml or ~/.kimi-code/config.toml)")
    parser.add_argument("--role", action="append", default=None,
                        help="角色名=模型别名 (repeatable, writes to roles.toml)")
    parser.add_argument("--sync", action="store_true",
                        help="Sync [secondary_model.models] from pool (read providers from config.toml)")
    parser.add_argument("--max-context-size", type=int, default=128000, dest="max_context_size",
                        help="全局默认 max_context_size（默认 128000）。例: --max-context-size 777777")
    parser.add_argument("--ctx", action="append", default=None, dest="ctx_overrides",
                        help="per-model 覆盖: --ctx \"别名=1048576\" 或 --ctx \"provider/model_id=1048576\"，可重复。优先级高于 --max-context-size")
    parser.add_argument("--set-ctx", action="append", default=None,
                        help="独立模式: 原地替换 config [models] 里已存在别名的 max_context_size 一行。格式 --set-ctx 别名=N,可重复;不能与 --sync/--direct/--model/--base-url/--key/--default/--role 混用")

    args = parser.parse_args()

    # 解析 --ctx per-model 覆盖: key=别名或模型id, value=正整数
    ctx_overrides: dict[str, int] = {}
    if args.ctx_overrides:
        for spec in args.ctx_overrides:
            if '=' not in spec:
                print(f"Error: --ctx 格式错误 '{spec}', 期望 KEY=N (缺少 '=')",
                      file=sys.stderr)
                sys.exit(1)
            key_str, val_str = spec.split('=', 1)
            key_str = key_str.strip()
            if not key_str:
                print(f"Error: --ctx 格式错误 '{spec}', KEY 不能为空",
                      file=sys.stderr)
                sys.exit(1)
            if not val_str.isdigit() or int(val_str) <= 0:
                print(f"Error: --ctx 格式错误 '{spec}', N 必须是正整数 (got '{val_str}')",
                      file=sys.stderr)
                sys.exit(1)
            ctx_overrides[key_str] = int(val_str)

    # 把解析好的 dict 写回 args,handle_sync 需要它
    args.ctx_overrides = ctx_overrides

    # 解析 --set-ctx 别名=N (独立模式): 格式错误/别名空 → stderr 报错 exit 1
    set_ctx_specs: list[tuple[str, int]] = []
    set_ctx_aliases: set[str] = set()
    if args.set_ctx:
        for spec in args.set_ctx:
            if '=' not in spec:
                print(f"Error: --set-ctx 格式错误 '{spec}', 期望 别名=N (缺少 '=')",
                      file=sys.stderr)
                sys.exit(1)
            alias_str, val_str = spec.split('=', 1)
            alias_str = alias_str.strip()
            if not alias_str:
                print(f"Error: --set-ctx 格式错误 '{spec}', 别名不能为空",
                      file=sys.stderr)
                sys.exit(1)
            if not val_str.isdigit() or int(val_str) <= 0:
                print(f"Error: --set-ctx 格式错误 '{spec}', N 必须是正整数 (got '{val_str}')",
                      file=sys.stderr)
                sys.exit(1)
            if alias_str in set_ctx_aliases:
                print(f"Error: --set-ctx 重复指定别名 '{alias_str}'", file=sys.stderr)
                sys.exit(1)
            set_ctx_aliases.add(alias_str)
            set_ctx_specs.append((alias_str, int(val_str)))

    # 路径优先级: 命令行 --config > KIMI_CODE_HOME 环境变量 > 默认 ~/.kimi-code/config.toml
    if args.config:
        config_path = args.config
    else:
        env_home = os.environ.get("KIMI_CODE_HOME", "").strip()
        if env_home:
            config_path = os.path.join(os.path.expanduser(env_home), "config.toml")
        else:
            config_path = os.path.expanduser("~/.kimi-code/config.toml")

    if args.set_ctx:
        # 独立模式: 与其它模式参数互斥,同时给 → 报错 exit 1
        conflicts = []
        if args.sync:
            conflicts.append("--sync")
        if args.direct:
            conflicts.append("--direct")
        if args.model:
            conflicts.append("--model")
        if args.base_url:
            conflicts.append("--base-url")
        if args.key:
            conflicts.append("--key")
        if args.default:
            conflicts.append("--default")
        if args.role:
            conflicts.append("--role")
        if conflicts:
            print(f"Error: --set-ctx 为独立模式,不能与 {'/'.join(conflicts)} 同时使用",
                  file=sys.stderr)
            sys.exit(1)
        handle_set_ctx(config_path, set_ctx_specs)
        return

    if args.sync:
        # Sync mode: read providers from config.toml, ignore --base-url/--key/--model/--direct/--provider
        if args.direct:
            print("Error: --sync cannot be combined with --direct", file=sys.stderr)
            sys.exit(1)
    elif args.direct:
        if not args.provider:
            print("Error: --direct requires at least one --provider", file=sys.stderr)
            sys.exit(1)
        if not args.default:
            print("Error: --default is required", file=sys.stderr)
            sys.exit(1)
    else:
        if not args.base_url:
            parser.error("--base-url is required (or use --direct for direct-connection mode)")
        if not args.key:
            parser.error("--key is required (or use --direct for direct-connection mode)")
        if not args.model:
            parser.error("--model is required")
        if not args.default:
            print("Error: --default is required", file=sys.stderr)
            sys.exit(1)

    # --role 预校验提前:格式/重复角色问题在任何配置写入前拒绝,保证原子性
    if args.role:
        validate_role_specs(args.role)

    if args.sync:
        handle_sync(config_path, args)
        # handle_sync 内部已处理写入和校验,直接返回
        if args.role:
            write_roles_toml(args.role)
        return

    models = {}
    if args.direct:
        providers = {}
        for p in args.provider:
            name, ptype, base_url, api_key = parse_provider(p)
            providers[name] = (ptype, base_url, api_key)
        for m in args.model:
            alias, provider_name, model_id = parse_model_spec(m)
            models[alias] = (provider_name, model_id)
    else:
        providers = {}
        for m in args.model:
            if '=' not in m:
                print(f"Error: invalid model format '{m}', expected alias=model_id", file=sys.stderr)
                sys.exit(1)
            alias, model_id = m.split('=', 1)
            if not alias or not model_id:
                print(f"Error: invalid model format '{m}', alias and model_id must be non-empty",
                      file=sys.stderr)
                sys.exit(1)
            models[alias] = model_id

    if args.default not in models:
        print(f"Error: default model alias '{args.default}' not found in --model parameters",
              file=sys.stderr)
        sys.exit(1)

    existing_lines = []
    file_existed = os.path.exists(config_path)
    if file_existed:
        with open(config_path, 'r', encoding='utf-8') as f:
            existing_lines = f.readlines()

    backup_path = None
    if file_existed:
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{config_path}.bak-{timestamp}"
        shutil.copy2(config_path, backup_path)
        print(f"Backup created: {backup_path}")

    lines = list(existing_lines)

    if args.direct:
        for name in providers:
            ptype, base_url, api_key = providers[name]
            block = build_direct_provider_block(name, ptype, base_url, api_key)
            header = f'[providers."{name}"]'
            start, end = find_block_range(lines, header)
            if start is not None:
                lines[start:end] = block
            else:
                ensure_blank_line_before(lines)
                lines.extend(block)

        for alias in models:
            provider_name, model_id = models[alias]
            max_ctx = resolve_max_ctx_global(ctx_overrides, args.max_context_size, alias, model_id)
            block = build_direct_model_block(alias, provider_name, model_id, max_ctx=max_ctx)
            header = f'[models."{alias}"]'
            start, end = find_block_range(lines, header)
            if start is not None:
                lines[start:end] = block
            else:
                ensure_blank_line_before(lines)
                lines.extend(block)
    else:
        block = build_providers_block(args.base_url, args.key)
        start, end = find_block_range(lines, "[providers.local-pool]")
        if start is not None:
            lines[start:end] = block
        else:
            ensure_blank_line_before(lines)
            lines.extend(block)

        for alias in models:
            max_ctx = resolve_max_ctx_global(ctx_overrides, args.max_context_size, alias, models[alias])
            block = build_model_block(alias, models[alias], max_ctx=max_ctx)
            header = f'[models."{alias}"]'
            start, end = find_block_range(lines, header)
            if start is not None:
                lines[start:end] = block
            else:
                ensure_blank_line_before(lines)
                lines.extend(block)

    block = build_secondary_model_block(args.default)
    start, end = find_block_range(lines, "[secondary_model]")
    if start is not None:
        lines[start:end] = block
    else:
        ensure_blank_line_before(lines)
        lines.extend(block)

    if args.direct:
        sm_lines = ["[secondary_model.models]\n"]
        for alias in models:
            provider_name, model_id = models[alias]
            sm_lines.append(f'"{alias}" = "直连:{provider_name}/{model_id}"\n')
        sm_lines.append("\n")
        block = sm_lines
    else:
        block = build_secondary_models_block(models)
    start, end = find_block_range(lines, "[secondary_model.models]")
    if start is not None:
        lines[start:end] = block
    else:
        ensure_blank_line_before(lines)
        lines.extend(block)

    content = ''.join(lines)
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"Error writing config: {e}", file=sys.stderr)
        if backup_path and os.path.exists(backup_path):
            shutil.copy2(backup_path, config_path)
            print(f"Rolled back from backup: {backup_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, 'rb') as f:
            tomllib.load(f)
    except Exception as e:
        print(f"Error: generated config is invalid TOML: {e}", file=sys.stderr)
        if backup_path and os.path.exists(backup_path):
            shutil.copy2(backup_path, config_path)
            print(f"Rolled back from backup: {backup_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Config updated successfully: {config_path}")
    if args.direct:
        for name in providers:
            ptype, base_url, api_key = providers[name]
            print(f"Provider {name}: type={ptype}, base_url={base_url}, api_key={mask_key(api_key)}")
        for alias in models:
            provider_name, model_id = models[alias]
            print(f"Model '{alias}': provider={provider_name}, model={model_id}")
    else:
        print(f"Base URL: {args.base_url}")
        print(f"API Key: {mask_key(args.key)}")
        print(f"Models: {', '.join(models.keys())}")
    print(f"Default model: {args.default}")

    if args.role:
        write_roles_toml(args.role)


def parse_role_spec(spec: str) -> tuple[str, str]:
    """Parse role spec: 角色名=模型别名"""
    parts = spec.split('=', 1)
    if len(parts) != 2:
        print(f"Error: invalid role format '{spec}', expected 角色名=模型别名",
              file=sys.stderr)
        sys.exit(1)
    role_name = parts[0].strip()
    model_alias = parts[1].strip()
    if not role_name or not model_alias:
        print(f"Error: invalid role format '{spec}', role name and model alias must be non-empty",
              file=sys.stderr)
        sys.exit(1)
    return role_name, model_alias


def validate_role_specs(role_specs: list[str]) -> None:
    """--role 预校验(格式+命令行内重复角色)。在任何配置写入前调用,保证原子性。"""
    seen: dict[str, str] = {}
    for spec in role_specs:
        role_name, model_alias = parse_role_spec(spec)
        if role_name in seen:
            print(f"Error: --role 重复指定角色 '{role_name}' (已有 {seen[role_name]})",
                  file=sys.stderr)
            sys.exit(1)
        seen[role_name] = model_alias


def _parse_roles_toml(path: str) -> dict[str, list[str]]:
    """解析 roles.toml,返回 {角色: [模型别名列表]}。优先 tomllib,失败则手动解析 [roles] 段。"""
    result: dict[str, list[str]] = {}
    if not os.path.isfile(path):
        return result
    with open(path, 'rb') as f:
        raw = f.read()
    try:
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


def _build_roles_toml(roles: dict[str, list[str]]) -> str:
    """生成 roles.toml 内容。key 加引号以兼容 tomllib(非 ASCII 裸 key 不支持)。"""
    lines = [
        "# 天机角色花名册: 角色名 = [模型别名列表]\n",
        "# 自动写入,换模型拍板后同步改它\n",
        "\n",
        "[roles]\n",
    ]
    for role in sorted(roles.keys()):
        aliases = roles[role]
        role_quoted = f'"{role}"'
        if len(aliases) == 1:
            lines.append(f'{role_quoted} = ["{aliases[0]}"]\n')
        else:
            alias_str = ', '.join(f'"{a}"' for a in aliases)
            lines.append(f'{role_quoted} = [{alias_str}]\n')
    return ''.join(lines)


def write_roles_toml(role_specs: list[str]) -> None:
    """写入角色花名册。路径优先级: TJ_ROLES_FILE > ~/.tianji/roles.toml"""
    env_path = os.environ.get("TJ_ROLES_FILE", "").strip()
    if env_path:
        roles_path = os.path.expanduser(env_path)
    else:
        roles_path = os.path.expanduser("~/.tianji/roles.toml")

    existing_roles = _parse_roles_toml(roles_path)

    # 检查命令行内部重复角色
    seen_roles: dict[str, str] = {}
    for spec in role_specs:
        role_name, model_alias = parse_role_spec(spec)
        if role_name in seen_roles:
            print(f"Error: --role 重复指定角色 '{role_name}' (已有 {seen_roles[role_name]})",
                  file=sys.stderr)
            sys.exit(1)
        seen_roles[role_name] = model_alias

    # 每个角色在一次调用中只能指定一次; 指定同名角色即明确改绑,替换旧指派。
    for role_name, model_alias in seen_roles.items():
        existing_roles[role_name] = [model_alias]

    # 备份
    roles_backup = None
    if os.path.exists(roles_path):
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        roles_backup = f"{roles_path}.bak-{timestamp}"
        shutil.copy2(roles_path, roles_backup)
        print(f"Roles backup created: {roles_backup}")

    roles_content = _build_roles_toml(existing_roles)
    try:
        with open(roles_path, 'w', encoding='utf-8') as f:
            f.write(roles_content)
    except Exception as e:
        print(f"Error writing roles: {e}", file=sys.stderr)
        if roles_backup and os.path.exists(roles_backup):
            shutil.copy2(roles_backup, roles_path)
            print(f"Rolled back roles from backup: {roles_backup}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(roles_path, 'rb') as f:
            tomllib.load(f)
    except Exception as e:
        print(f"Error: generated roles.toml is invalid TOML: {e}", file=sys.stderr)
        if roles_backup and os.path.exists(roles_backup):
            shutil.copy2(roles_backup, roles_path)
            print(f"Rolled back roles from backup: {roles_backup}", file=sys.stderr)
        sys.exit(1)

    print(f"Roles updated: {roles_path}")


if __name__ == "__main__":
    main()
