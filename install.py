#!/usr/bin/env python3
"""
tianji-v2 install.py
部署/卸载/状态检查工具 - 仅标准库
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

# The shared role contract is imported before anything runs, so a broken tree
# must not surface as a bare traceback: no shared contract means no conclusion
# can be reached, and the caller must get the "no verdict" code for that.
try:
    from skills.tianji.scripts.role_contract import (
        discover_role_packages,
        load_role_package,
        render_codex,
        validate_role_tiers,
    )
except Exception as _exc:  # noqa: BLE001
    print(
        f"[FAIL] 天机共享角色契约导入失败，无法得出结论: "
        f"{type(_exc).__name__}: {_exc}",
        file=sys.stderr,
    )
    raise SystemExit(2) from _exc


# ================================================================
# 常量
# ================================================================

M_START = "# >>> tianji-managed >>>"
M_END   = "# <<< tianji-managed <<<"
CODEX_HOOK_STATUS = "Tianji managed state ledger"

def _managed_config_block(agents_home: str) -> str:
    ah = agents_home.replace("\\", "/")
    # 注意:state-log.py 从 stdin payload 的 hook_event_name 自判事件类型,
    # 不需要 start/stop 命令行参数;写法与 state-log.py docstring 示例保持一致。
    return (
        f"{M_START}\n"
        f"[[hooks]]\n"
        f'event = "SubagentStart"\n'
        f'command = "python {ah}/skills/tianji/scripts/state-log.py"\n'
        f"\n"
        f"[[hooks]]\n"
        f'event = "SubagentStop"\n'
        f'command = "python {ah}/skills/tianji/scripts/state-log.py"\n'
        f"{M_END}"
    )

def _managed_tui_block(agents_home: str) -> str:
    ah = agents_home.replace("\\", "/")
    return (
        f"{M_START}\n"
        f"[status_line]\n"
        f'command = "python {ah}/skills/tianji/scripts/statusline.py"\n'
        f"{M_END}"
    )


# ================================================================
# 工具函数
# ================================================================

def parse_args():
    p = argparse.ArgumentParser(description="tianji-v2 install tool")
    p.add_argument("command", choices=["install", "uninstall", "status"])
    p.add_argument("--host", choices=["kimi", "codex", "cmdc"], default=None,
                   help="target host; detected from host runtime signals when omitted")
    p.add_argument("--dry-run", action="store_true", help="只打印,不动手")
    p.add_argument("--force-overwrite", action="store_true",
                   help="强制覆盖用户改过的受管文件(默认拒绝)")
    # These defaults must match what the host detectors read, or `status` would
    # inspect a different install than the doctor does on the same machine.
    p.add_argument("--kimi-home",
                   default=os.environ.get("KIMI_CODE_HOME") or os.path.expanduser("~/.kimi-code"),
                   help="kimi-code 配置目录 (默认 $KIMI_CODE_HOME 或 ~/.kimi-code)")
    p.add_argument("--agents-home", default=os.path.expanduser("~/.agents"),
                   help="agents 目录 (默认 ~/.agents)")
    p.add_argument("--codex-home",
                   default=os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                   help="Codex configuration directory (default $CODEX_HOME or ~/.codex)")
    p.add_argument("--cmdc-home",
                   default=os.environ.get("CMDC_HOME") or os.path.expanduser("~/.commandcode"),
                   help="Command Code home (default $CMDC_HOME or ~/.commandcode)")
    return p.parse_args()


def src_root() -> Path:
    """源树根目录: install.py 所在目录"""
    return Path(__file__).resolve().parent


def _managed_role_sources() -> list[Path]:
    """Single source of truth for host-neutral Tianji role packages."""
    return sorted((src_root() / "agents").glob("tianji-*.md"))


def _codex_role_names(codex_home: Path) -> list[str]:
    """Return every managed role from source and the existing Codex target."""
    names = {p.stem for p in _managed_role_sources()}
    names.update(p.stem for p in (codex_home / "agents").glob("tianji-*.toml"))
    return sorted(names)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def bak_path(p: Path) -> Path:
    return p.parent / f"{p.name}.bak-{stamp()}"


# ---- TOML / managed-block 操作 ----

_BLOCK_RE = re.compile(
    re.escape(M_START) + r"(.*?)" + re.escape(M_END),
    re.DOTALL,
)


def extract_block(text: str):
    """返回 (inner_text, before, after) | (None, full_text, "") """
    m = _BLOCK_RE.search(text)
    if m:
        return m.group(1).strip(), text[:m.start()], text[m.end():]
    return None, text, ""


def strip_block(text: str) -> str:
    """从文本中删除 managed block（含标记行）。"""
    # 匹配从 start 标记整行到 end 标记整行（含 end 后的可选换行）
    full_re = re.compile(
        re.escape(M_START) + r".*?" + re.escape(M_END) + r"\n?",
        re.DOTALL,
    )
    return full_re.sub("", text)


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def validate_toml(path: Path):
    load_toml(path)


def _read_agent_markdown(path: Path) -> tuple[str, str]:
    package = load_role_package(path)
    return package.description, package.instructions


def _validate_managed_role_sources() -> list[Path]:
    """Validate the host-neutral role packages before either host is changed.

    The role tier contract is checked here, on every install for every host: a
    role that lost its tier, or a tier that names a role nobody ships, must stop
    the install instead of quietly changing what gets rendered.
    """
    packages = discover_role_packages(src_root() / "agents")
    validate_role_tiers(packages)
    return [package.path for package in packages]


def _codex_agent_toml(src: Path, binding: str | None = None) -> str:
    return render_codex(load_role_package(src), binding)


def _existing_codex_binding(path: Path) -> str | None:
    """Return a confirmed Tianji binding, rejecting malformed role TOML."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    if '# tianji-role-binding = "primary"' in text and "model" not in parsed:
        return "主模型"
    if '# tianji-role-binding = "fixed"' in text and isinstance(parsed.get("model"), str) and parsed["model"].strip():
        return parsed["model"]
    return None


def _codex_hook_command(agents_home: Path) -> str:
    script = (agents_home / "skills" / "tianji" / "scripts" / "state-log.py").as_posix()
    return f'python "{script}"'


def _managed_codex_hooks(command: str) -> dict:
    handler = {"type": "command", "command": command,
               "statusMessage": CODEX_HOOK_STATUS, "timeout": 5}
    return {"SubagentStart": [{"hooks": [handler]}],
            "SubagentStop": [{"hooks": [handler]}]}


def _without_managed_codex_hooks(data: dict) -> dict:
    """Remove only Tianji's handlers, preserving all user hook definitions."""
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json field 'hooks' must be an object")
    result, cleaned = dict(data), {}
    for event, matchers in hooks.items():
        if not isinstance(matchers, list):
            raise ValueError(f"hooks.json event '{event}' must be a list")
        kept_matchers = []
        for matcher in matchers:
            if not isinstance(matcher, dict):
                raise ValueError(f"hooks.json event '{event}' has invalid matcher")
            handlers = matcher.get("hooks", [])
            if not isinstance(handlers, list):
                raise ValueError(f"hooks.json event '{event}' has invalid handlers")
            kept = [h for h in handlers if not (isinstance(h, dict) and h.get("statusMessage") == CODEX_HOOK_STATUS)]
            if kept:
                copied = dict(matcher)
                copied["hooks"] = kept
                kept_matchers.append(copied)
        if kept_matchers:
            cleaned[event] = kept_matchers
    result["hooks"] = cleaned
    return result


def _write_json_with_backup(path: Path, data: dict, rpt: dict, label: str):
    """Backup -> write -> parse -> rollback, matching the TOML safety rule."""
    rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8-sig") == rendered:
        rpt.setdefault("skipped", []).append(str(path))
        print(f"  Skipped unchanged: {path}")
        return
    existed, bak = path.exists(), bak_path(path)
    if existed:
        shutil.copy2(path, bak)
        rpt["backups"].append(str(bak))
        print(f"  Backup: {bak}")
    try:
        write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        rpt["modified"].append(str(path))
        print(f"  Updated: {path}")
    except Exception as e:
        if existed:
            shutil.copy2(bak, path)
        else:
            path.unlink(missing_ok=True)
        print(f"  FAIL: {label} validation error -> rolled back: {e}")
        sys.exit(1)


def _load_json_object(path: Path) -> dict:
    """Read JSON as UTF-8, accepting the BOM written by Windows tools."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("hooks.json root must be an object")
    return data


def _tree_signature(root: Path) -> tuple:
    if not root.exists():
        return ()
    rows = []
    for item in sorted(root.rglob("*")):
        if not item.is_file() or "__pycache__" in item.parts:
            continue
        stat = item.stat()
        rows.append((item.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(rows)


def _sync_tree(source: Path, target: Path, rpt: dict) -> bool:
    if target.exists() and _tree_signature(source) == _tree_signature(target):
        rpt.setdefault("skipped", []).append(str(target))
        print(f"  Skipped unchanged dir: {target}")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    rpt.setdefault("modified", []).append(str(target))
    print(f"  Copied dir: {target}")
    return True


def _write_toml_with_backup(path: Path, text: str, rpt: dict, label: str):
    if path.exists() and path.read_text(encoding="utf-8") == text:
        rpt.setdefault("skipped", []).append(str(path))
        print(f"  Skipped unchanged: {path}")
        return
    existed, bak = path.exists(), bak_path(path)
    if existed:
        shutil.copy2(path, bak)
        rpt["backups"].append(str(bak))
        print(f"  Backup: {bak}")
    try:
        write_text(path, text)
        validate_toml(path)
    except Exception as e:
        if existed:
            shutil.copy2(bak, path)
        else:
            path.unlink(missing_ok=True)
        print(f"  FAIL: {label} validation error -> rolled back: {e}")
        sys.exit(1)


# ---- 号池探测 ----

def pool_configured(kimi_home: str) -> bool:
    cfg = Path(kimi_home) / "config.toml"
    if not cfg.exists():
        return False
    try:
        data = load_toml(cfg)
        # 检查是否有 secondary_model 相关段
        for key in data:
            if "secondary_model" in key.lower():
                return True
            val = data[key]
            if isinstance(val, dict):
                for sub in val:
                    if "secondary_model" in sub.lower():
                        return True
        return False
    except Exception:
        return False


# ================================================================
# INSTALL
# ================================================================

def do_install_codex(args):
    """Install Codex-native agents and ledger hooks; shared skills stay in ~/.agents."""
    dry = args.dry_run
    agents_home = Path(args.agents_home).resolve()
    codex_home = Path(args.codex_home).resolve()
    src = src_root()
    agent_src = src / "agents"
    agent_dst = codex_home / "agents"
    hooks_path = codex_home / "hooks.json"
    rpt: dict = {"installed": [], "modified": [], "backups": [], "warnings": [], "skipped": []}

    # Validate every source/config input before changing any runtime file.
    try:
        role_sources = [
            (source, _codex_agent_toml(source, _existing_codex_binding(agent_dst / f"{source.stem}.toml")))
            for source in _validate_managed_role_sources()
        ]
        for _, text in role_sources:
            tomllib.loads(text)
        existing_hooks = _load_json_object(hooks_path) if hooks_path.exists() else {}
        base_hooks = _without_managed_codex_hooks(existing_hooks)
    except Exception as e:
        print(f"  FAIL: Codex install preflight failed; nothing written: {e}")
        sys.exit(1)

    print(f"{'[DRY RUN] ' if dry else ''}=== tianji-v2 install (codex) ===")
    print(f"  Codex home:  {codex_home}")
    print(f"  Agents home: {agents_home} (shared skills)")

    for skill_name in ("tianji", "tianji-proxy"):
        skill_src = src / "skills" / skill_name
        skill_dst = agents_home / "skills" / skill_name
        if dry:
            print(f"  [dry] cp -r {skill_src} -> {skill_dst}")
        else:
            _sync_tree(skill_src, skill_dst, rpt)
        rpt["installed"].append(str(skill_dst))

    for source, role_text in role_sources:
        target = agent_dst / f"{source.stem}.toml"
        if dry:
            print(f"  [dry] generate {target}")
        else:
            _write_toml_with_backup(target, role_text, rpt, target.name)
        rpt["installed"].append(str(target))

    command = _codex_hook_command(agents_home)
    if dry:
        print(f"  [dry] would merge Tianji hooks into {hooks_path}")
        rpt["modified"].append(str(hooks_path))
    else:
        merged = base_hooks
        hooks = merged.setdefault("hooks", {})
        for event, entries in _managed_codex_hooks(command).items():
            hooks.setdefault(event, []).extend(entries)
        _write_json_with_backup(hooks_path, merged, rpt, "hooks.json")

    rpt["pool"] = "not_applicable"
    _print_report(rpt, dry)
    print("  Next: restart Codex, review Tianji hooks with /hooks, then invoke '天机'.")
    print("  Tianji will guide model source, menu, role binding, restart, and native route proof.")


def do_install(args):
    if args.host == "codex":
        do_install_codex(args)
        return
    dry = args.dry_run
    kimi_home = Path(args.kimi_home).resolve()
    agents_home = Path(args.agents_home).resolve()
    src = src_root()

    print(f"{'[DRY RUN] ' if dry else ''}=== tianji-v2 install ===")
    print(f"  Source:      {src}")
    print(f"  Kimi home:   {kimi_home}")
    print(f"  Agents home: {agents_home}")

    rpt: dict = {"installed": [], "modified": [], "backups": [], "warnings": [], "skipped": []}

    try:
        _validate_managed_role_sources()
    except Exception as e:
        print(f"  FAIL: role package preflight failed; nothing written: {e}")
        sys.exit(1)

    # ---- 1. 拷贝文件 ----
    copy_tasks = [
        (src / "skills" / "tianji",            agents_home / "skills" / "tianji"),
        (src / "skills" / "tianji-proxy",      agents_home / "skills" / "tianji-proxy"),
    ]
    agent_src = src / "agents"
    agent_dst = agents_home / "agents"

    if not dry:
        for _, d in copy_tasks:
            d.parent.mkdir(parents=True, exist_ok=True)
        agent_dst.mkdir(parents=True, exist_ok=True)

    for s, d in copy_tasks:
        if dry:
            print(f"  [dry] cp -r {s} -> {d}")
            rpt["installed"].append(str(d))
        else:
            _sync_tree(s, d, rpt)
            rpt["installed"].append(str(d))

    for f in _managed_role_sources():
        dst = agent_dst / f.name
        if dry:
            print(f"  [dry] cp {f} -> {dst}")
        else:
            if dst.exists() and dst.read_bytes() == f.read_bytes():
                rpt.setdefault("skipped", []).append(str(dst))
                print(f"  Skipped unchanged: {dst}")
            else:
                shutil.copy2(f, dst)
                rpt.setdefault("modified", []).append(str(dst))
                print(f"  Copied file: {dst}")
        rpt["installed"].append(str(dst))

    # ---- 2. config.toml 受管块 ----
    cfg_path = kimi_home / "config.toml"
    cfg_block = _managed_config_block(str(agents_home))

    if dry:
        print(f"  [dry] Would upsert managed hooks block in {cfg_path}")
        rpt["modified"].append(str(cfg_path))
    else:
        existed = cfg_path.exists()
        existing = cfg_path.read_text(encoding="utf-8") if existed else ""

        inner, before, after = extract_block(existing)
        if inner is not None and inner == extract_block(cfg_block)[0]:
            new_text = existing
        elif inner is not None:
            new_text = before + cfg_block + "\n" + after
        else:
            suffix = "\n" if existing and not existing.endswith("\n") else ""
            new_text = existing + suffix + cfg_block + "\n"

        if new_text == existing:
            rpt.setdefault("skipped", []).append(str(cfg_path))
            print(f"  Skipped unchanged: {cfg_path}")
        else:
            bak = bak_path(cfg_path)
            if existed:
                shutil.copy2(cfg_path, bak)
                rpt["backups"].append(str(bak))
                print(f"  Backup: {bak}")
            write_text(cfg_path, new_text)
            try:
                validate_toml(cfg_path)
                print(f"  Updated: {cfg_path}")
                rpt["modified"].append(str(cfg_path))
            except Exception as e:
                if existed:
                    shutil.copy2(bak, cfg_path)
                else:
                    cfg_path.unlink(missing_ok=True)
                print(f"  FAIL: config.toml validation error → rolled back: {e}")
                sys.exit(1)

    # ---- 3. tui.toml 受管块 ----
    tui_path = kimi_home / "tui.toml"
    tui_block = _managed_tui_block(str(agents_home))

    # 读取现有文本(干跑也读,用于检测用户段)
    tui_text = tui_path.read_text(encoding="utf-8") if tui_path.exists() else ""

    # 检查: 用户自己有 [status_line] 且在受管块之外?
    no_managed = strip_block(tui_text)
    has_user_sl = bool(re.search(r'(?m)^\[status_line\]\s*$', no_managed))

    if has_user_sl:
        warn = (f"WARNING: {tui_path} has user [status_line] outside managed block. "
                f"Skipping tui.toml - please merge manually.")
        print(f"  {warn}")
        rpt["warnings"].append(warn)
        if not dry:
            rpt["modified"].append(str(tui_path))  # 尝试但跳过也算报告
    elif dry:
        print(f"  [dry] Would upsert managed status_line block in {tui_path}")
        rpt["modified"].append(str(tui_path))
    else:
        existed = tui_path.exists()

        inner, before, after = extract_block(tui_text)
        if inner is not None and inner == extract_block(tui_block)[0]:
            new_text = tui_text
        elif inner is not None:
            new_text = before + tui_block + "\n" + after
        else:
            suffix = "\n" if tui_text and not tui_text.endswith("\n") else ""
            new_text = tui_text + suffix + tui_block + "\n"

        if new_text == tui_text:
            rpt.setdefault("skipped", []).append(str(tui_path))
            print(f"  Skipped unchanged: {tui_path}")
        else:
            bak = bak_path(tui_path)
            if existed:
                shutil.copy2(tui_path, bak)
                rpt["backups"].append(str(bak))
                print(f"  Backup: {bak}")
            write_text(tui_path, new_text)
            try:
                validate_toml(tui_path)
                print(f"  Updated: {tui_path}")
                rpt["modified"].append(str(tui_path))
            except Exception as e:
                if existed:
                    shutil.copy2(bak, tui_path)
                else:
                    tui_path.unlink(missing_ok=True)
                print(f"  FAIL: tui.toml validation error → rolled back: {e}")
                sys.exit(1)

    # ---- 4. 号池检查 + 报告 ----
    if pool_configured(str(kimi_home)):
        rpt["pool"] = "configured"
    else:
        rpt["pool"] = "not_configured"

    _print_report(rpt, dry)


def _print_report(rpt, dry):
    print()
    print("--- Install Report ---")
    if dry:
        print("  (DRY RUN — no changes written)")
    print(f"  Installed ({len(rpt['installed'])}):")
    for x in rpt["installed"]:
        print(f"    + {x}")
    print(f"  Modified ({len(rpt['modified'])}):")
    for x in rpt["modified"]:
        print(f"    ~ {x}")
    print(f"  Backups ({len(rpt['backups'])}):")
    for x in rpt["backups"]:
        print(f"    @ {x}")
    for w in rpt.get("warnings", []):
        print(f"    ! {w}")
    if rpt.get("pool") == "not_configured":
        print()
        print("  下一步: 对 kimi 说'天机'进入引导配置号池")
    print("--- End Report ---\n")


# ================================================================
# UNINSTALL
# ================================================================

def do_uninstall_codex(args):
    dry = args.dry_run
    codex_home = Path(args.codex_home).resolve()
    hooks_path = codex_home / "hooks.json"
    rpt = {"removed_files": [], "modified": [], "backups": [], "removed_blocks": []}
    print(f"{'[DRY RUN] ' if dry else ''}=== tianji-v2 uninstall (codex) ===")

    cleaned_hooks = None
    if hooks_path.exists():
        try:
            cleaned_hooks = _without_managed_codex_hooks(_load_json_object(hooks_path))
        except Exception as e:
            print(f"  FAIL: hooks.json is invalid; nothing written: {e}")
            sys.exit(1)

    adapter_state = codex_home / "tianji-adapter.toml"
    if adapter_state.exists():
        try:
            active = load_toml(adapter_state).get("status") == "configured"
        except Exception as e:
            print(f"  FAIL: adapter state is invalid; nothing written: {e}")
            sys.exit(1)
        if active and dry:
            print(f"  [dry] would restore pre-Tianji provider from {adapter_state}")
        elif active:
            restore_script = src_root() / "skills" / "tianji" / "scripts" / "codex-role-configure.py"
            restored = subprocess.run(
                [sys.executable, str(restore_script), "--codex-home", str(codex_home), "--restore"],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            if restored.returncode != 0:
                print("  FAIL: could not safely restore the pre-Tianji Codex provider; nothing else removed.")
                print("  " + restored.stderr.strip())
                sys.exit(1)
            print("  Restored pre-Tianji Codex main provider.")

    if hooks_path.exists():
        if dry:
            print(f"  [dry] would remove Tianji hooks from {hooks_path}")
        else:
            _write_json_with_backup(hooks_path, cleaned_hooks, rpt, "hooks.json")
            # _write_json_with_backup already records the modification.
        if dry:
            rpt["modified"].append(str(hooks_path))

    for name in _codex_role_names(codex_home):
        target = codex_home / "agents" / f"{name}.toml"
        if dry:
            print(f"  [dry] rm {target}")
            rpt["removed_files"].append(str(target))
        elif target.exists():
            target.unlink()
            print(f"  Removed: {target}")
            rpt["removed_files"].append(str(target))

    if adapter_state.exists():
        if dry:
            print(f"  [dry] rm {adapter_state}")
            rpt["removed_files"].append(str(adapter_state))
        else:
            adapter_state.unlink()
            print(f"  Removed: {adapter_state}")
            rpt["removed_files"].append(str(adapter_state))

    # ~/.agents/skills/tianji is shared by Codex and Kimi, so Codex uninstall
    # deliberately leaves it in place rather than risking another host.
    print("  Preserved shared skill: ~/.agents/skills/tianji")
    print(f"  Removed files ({len(rpt['removed_files'])}):")
    for x in rpt["removed_files"]:
        print(f"    - {x}")
    print(f"  Modified ({len(rpt['modified'])}):")
    for x in rpt["modified"]:
        print(f"    ~ {x}")
    print(f"  Backups ({len(rpt['backups'])}):")
    for x in rpt["backups"]:
        print(f"    @ {x}")

def do_uninstall(args):
    if args.host == "codex":
        do_uninstall_codex(args)
        return
    dry = args.dry_run
    kimi_home = Path(args.kimi_home).resolve()
    agents_home = Path(args.agents_home).resolve()

    print(f"{'[DRY RUN] ' if dry else ''}=== tianji-v2 uninstall ===")

    rpt = {"removed_files": [], "modified": [], "backups": [], "removed_blocks": []}

    # ---- 1. 删除受管块 ----
    for name in ["config.toml", "tui.toml"]:
        p = kimi_home / name
        if not p.exists():
            print(f"  {p} not found, skip.")
            continue

        if dry:
            print(f"  [dry] Would remove managed block from {p}")
            rpt["modified"].append(str(p))
            rpt["removed_blocks"].append(name)
            continue

        text = p.read_text(encoding="utf-8")
        inner, before, after = extract_block(text)
        if inner is None:
            print(f"  No managed block in {p}, skip.")
            continue

        bak = bak_path(p)
        shutil.copy2(p, bak)
        rpt["backups"].append(str(bak))
        print(f"  Backup: {bak}")

        new_text = before + after
        new_text = re.sub(r'\n{3,}', '\n\n', new_text)  # 清理多余空行

        write_text(p, new_text)
        try:
            validate_toml(p)
            print(f"  Removed managed block from: {p}")
            rpt["modified"].append(str(p))
            rpt["removed_blocks"].append(name)
        except Exception as e:
            shutil.copy2(bak, p)
            print(f"  FAIL: {name} validation error → rolled back: {e}")
            sys.exit(1)

    # ---- 2. 删除拷贝的文件 ----
    rm_dirs = [
        agents_home / "skills" / "tianji",
        agents_home / "skills" / "tianji-proxy",
    ]
    rm_files = sorted((agents_home / "agents").glob("tianji-*.md")) if (agents_home / "agents").exists() else []

    for d in rm_dirs:
        if dry:
            print(f"  [dry] rm -rf {d}")
            rpt["removed_files"].append(str(d))
        elif d.exists():
            shutil.rmtree(d)
            print(f"  Removed: {d}")
            rpt["removed_files"].append(str(d))

    for f in rm_files:
        if dry:
            print(f"  [dry] rm {f}")
            rpt["removed_files"].append(str(f))
        elif f.exists():
            f.unlink()
            print(f"  Removed: {f}")
            rpt["removed_files"].append(str(f))

    # ---- 报告 ----
    print()
    print("--- Uninstall Report ---")
    if dry:
        print("  (DRY RUN — no changes written)")
    print(f"  Removed files ({len(rpt['removed_files'])}):")
    for x in rpt["removed_files"]:
        print(f"    - {x}")
    print(f"  Modified ({len(rpt['modified'])}):")
    for x in rpt["modified"]:
        print(f"    ~ {x}")
    print(f"  Backups ({len(rpt['backups'])}):")
    for x in rpt["backups"]:
        print(f"    @ {x}")
    print("--- End Report ---\n")


# ================================================================
# 共享结论机（唯一 READY 判定）
# ================================================================

def _shared_modules():
    """Import the shared conclusion machine and detectors instead of copying them.

    The machine and the host detectors live with the doctor CLI in the shared
    skill; install.py only feeds facts. Importing them (rather than re-deriving
    readiness here) is what keeps ``status`` from growing a second verdict.
    """
    scripts = src_root() / "skills" / "tianji" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import conclusions
    import doctor
    import host_detectors
    return conclusions, doctor, host_detectors


def _print_shared_verdict(detector):
    """Print the machine's verdict for a detector's local facts; return it.

    Only local facts are fed. The pool liveness probe is a network call that
    belongs to the morning check, so ``status`` never performs it -- and it says
    that explicitly, because an unprobed pool must not be read as a live one,
    nor as a dead one. When a host's readiness needs that proof, staying offline
    means the verdict is inconclusive rather than ready.
    """
    _, doctor, _ = _shared_modules()
    checks = doctor.collect_facts(detector)
    if checks["menu"][0] and detector.supports_menu_reconciliation():
        pool = doctor.PoolEvidence.not_probed("status 不联网，池活性未探测")
    else:
        pool = doctor.PoolEvidence.not_required()
    verdict, suggestion = doctor.determine_conclusion(checks, pool)
    print(f"\n  结论: {verdict}")
    print(f"  建议: {suggestion}")
    return verdict


# ================================================================
# STATUS
# ================================================================

def do_status_codex(args):
    agents_home = Path(args.agents_home).resolve()
    codex_home = Path(args.codex_home).resolve()
    hooks_path = codex_home / "hooks.json"
    # Unpacked up front: the roster below is read from the shared contract, so
    # this status table and the detector cannot disagree about which roles exist.
    conclusions, _, host_detectors = _shared_modules()
    checks = {
        f"skills/{name}/": ((agents_home / "skills" / name).exists(), str(agents_home / "skills" / name))
        for name in ("tianji", "tianji-proxy")
    }
    for name in _codex_role_names(codex_home):
        path = codex_home / "agents" / f"{name}.toml"
        try:
            ok = path.exists() and bool(load_toml(path).get("developer_instructions"))
        except Exception:
            ok = False
        checks[f"codex-agents/{name}.toml"] = (ok, str(path))
    try:
        raw = _load_json_object(hooks_path)
        hooks = raw.get("hooks", {})
        managed = all(any(
            handler.get("statusMessage") == CODEX_HOOK_STATUS
            for matcher in hooks.get(event, []) if isinstance(matcher, dict)
            for handler in matcher.get("hooks", []) if isinstance(handler, dict)
        ) for event in ("SubagentStart", "SubagentStop"))
    except Exception:
        managed = False
    checks["managed-hooks:hooks.json"] = (managed, str(hooks_path))
    adapter_path = codex_home / "tianji-adapter.toml"
    try:
        adapter = load_toml(adapter_path)
        model_source = adapter.get("model_source", "responses")
        if model_source == "host_native":
            subject = {
                "model_source": "host_native",
                "models": adapter.get("models", {}),
                "roles": adapter.get("roles", {}),
                "role_hashes": adapter.get("role_hashes", {}),
            }
            subject_sha256 = hashlib.sha256(json.dumps(
                subject, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            current_role_hashes = {
                role: hashlib.sha256(
                    (codex_home / "agents" / f"tianji-{role}.toml").read_text(
                        encoding="utf-8",
                    ).encode("utf-8")
                ).hexdigest()
                for role in host_detectors.CODEX_ROLES
            }
            adapter_ok = (
                adapter.get("status") == "configured"
                and bool(adapter.get("models"))
                and subject_sha256 == adapter.get("subject_sha256")
                and current_role_hashes == adapter.get("role_hashes")
            )
            proof = adapter.get("proof", {})
            models = adapter.get("models", {})
            roles = adapter.get("roles", {})
            proof_role = proof.get("proof_role")
            expected_model = (
                models.get(roles.get(proof_role))
                if isinstance(models, dict) and isinstance(roles, dict)
                and isinstance(proof_role, str) and proof_role else None
            )
            proof_ok = (
                adapter_ok
                and proof.get("status") == "passed"
                and proof.get("proof_level") == "host_dispatch"
                and proof.get("wire_verified") is False
                and isinstance(expected_model, str) and bool(expected_model)
                and proof.get("expected_role_model") == expected_model
                and proof.get("observed_model") == expected_model
                and proof.get("subject_sha256") == adapter.get("subject_sha256")
            )
            adapter_label = "codex-adapter:host-native"
        else:
            config_text = (codex_home / "config.toml").read_text(encoding="utf-8-sig")
            config = tomllib.loads(config_text)
            provider = config.get("model_providers", {}).get("tianji", {})
            adapter_ok = (
                adapter.get("status") == "configured"
                and config.get("model_provider") == "tianji"
                and config.get("model") == adapter.get("main_model")
                and provider.get("wire_api") == "responses"
                and provider.get("base_url") == adapter.get("base_url")
                and hashlib.sha256(config_text.encode("utf-8")).hexdigest() == adapter.get("config_sha256")
            )
            proof_ok = (
                adapter_ok and adapter.get("proof", {}).get("status") == "passed"
                and adapter.get("proof", {}).get("config_sha256") == adapter.get("config_sha256")
            )
            adapter_label = "codex-adapter:responses"
    except Exception:
        adapter_ok = proof_ok = False
        adapter_label = "codex-adapter"
    checks[adapter_label] = (adapter_ok, str(adapter_path))
    checks["codex-route-proof"] = (proof_ok, str(adapter_path))

    print("=== tianji-v2 status (codex) ===\n")
    print(f"  {'Check':<38} {'Status':<10} Path")
    print(f"  {'-' * 70}")
    for name, (ok, detail) in checks.items():
        print(f"  {name:<38} {'OK' if ok else 'MISSING':<10} {detail}")

    # READY comes from the shared conclusion machine, not from a second
    # all(checks) rule: the doctor and this table must never disagree.
    verdict = _print_shared_verdict(host_detectors.CodexDetector(root=str(codex_home)))
    if verdict in conclusions.ACTIONABLE:
        print("\n  Installation and Codex routing checks PASSED. Tianji is READY.\n")
    else:
        print("\n  NOT ready. Missing install files require install; adapter/proof items are completed by Tianji onboarding.\n")
    return conclusions.exit_code(verdict)

def do_status(args):
    if args.host == "codex":
        do_status_codex(args)
        return
    kimi_home = Path(args.kimi_home).resolve()
    agents_home = Path(args.agents_home).resolve()

    print("=== tianji-v2 status ===\n")

    checks = {}

    # 文件检查
    tianji_dir = agents_home / "skills" / "tianji"
    checks["skills/tianji/"]           = (tianji_dir.exists(), str(tianji_dir))

    tianji_proxy_dir = agents_home / "skills" / "tianji-proxy"
    checks["skills/tianji-proxy/"]     = (tianji_proxy_dir.exists(), str(tianji_proxy_dir))

    for source in _managed_role_sources():
        fn = source.name
        checks[f"agents/{fn}"] = (
            (agents_home / "agents" / fn).exists(),
            str(agents_home / "agents" / fn),
        )

    # 受管块检查
    for toml_name in ["config.toml", "tui.toml"]:
        tp = kimi_home / toml_name
        key = f"managed-block:{toml_name}"
        if tp.exists():
            txt = tp.read_text(encoding="utf-8")
            inner, _, _ = extract_block(txt)
            checks[key] = (inner is not None, str(tp))
        else:
            checks[key] = (False, str(tp))

    # 号池
    checks["pool:secondary_model"] = (pool_configured(str(kimi_home)), "config.toml → secondary_model")

    # 打印表
    print(f"  {'Check':<38} {'Status':<10} Path")
    print(f"  {'-'*70}")
    for name, (ok, detail) in checks.items():
        st = "OK" if ok else "MISSING"
        print(f"  {name:<38} {st:<10} {detail}")

    missing = [k for k, (v, _) in checks.items() if not v]
    print()
    # This line is about the file-level checks in the table only, never about
    # readiness: the verdict below is the single readiness statement.
    if not missing:
        print("  文件检查全部通过（就绪判定见下方结论）。")
    else:
        print(f"  文件检查有缺项: {', '.join(missing)}")
        if not checks.get("pool:secondary_model", (False,))[0]:
            print("  下一步: 对 kimi 说'天机'进入引导配置号池")

    # 就绪与否由共享结论机判定；status 只喂本地事实，不探池——未探测本身就是一条
    # 要如实上报的证据，而不是"没有死池"。
    conclusions, _, host_detectors = _shared_modules()
    detector = host_detectors.KimiDetector(root=str(kimi_home))
    verdict = _print_shared_verdict(detector)
    print()
    return conclusions.exit_code(verdict)


# ================================================================
# Command Code (cmdc) — 薄适配层
# ================================================================

def _cmdc_adapter_modules():
    """Import the Command Code adapter lazily, using the skill's flat layout."""
    skill_dir = src_root() / "skills" / "tianji"
    for path in (str(skill_dir / "scripts"), str(skill_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from managed_files import ManagedInstaller
    from core_version import SHARED_CORE_VERSION
    from host_adapters.cmdc import native_installer
    return ManagedInstaller, SHARED_CORE_VERSION, native_installer


_SHARED_CORE_SKIP_DIRS = {"__pycache__", ".tmp", ".tianji", ".git"}

# The pool skill belongs to an install only when the model source is an external
# provider. A host whose own menu supplies the models -- Command Code's native
# menu has 70 of them -- must not have it installed, checked or mentioned.
CORE_SKILLS = ("tianji",)
POOL_SKILL = "tianji-proxy"


def _shared_core_entries(src: Path, *, include_pool: bool) -> dict:
    """Byte payloads for the shared skill trees, keyed by install-relative path."""
    names = CORE_SKILLS + ((POOL_SKILL,) if include_pool else ())
    entries = {}
    for skill_name in names:
        root = src / "skills" / skill_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            if _SHARED_CORE_SKIP_DIRS.intersection(path.parts):
                continue
            relative = path.relative_to(src).as_posix()
            entries[relative] = path.read_bytes()
    return entries


def _sync_shared_core(args, *, include_pool: bool) -> dict:
    """Shared phase: only manifest-managed files are touched, never rmtree."""
    ManagedInstaller, SHARED_CORE_VERSION, _ = _cmdc_adapter_modules()
    agents_home = Path(args.agents_home).resolve()
    installer = ManagedInstaller(
        agents_home, agents_home / "shared-core.manifest.json",
        version=SHARED_CORE_VERSION,
    )
    entries = _shared_core_entries(src_root(), include_pool=include_pool)
    plan = installer.plan(entries)
    if args.dry_run:
        return {"write": sorted(plan.write), "skip": sorted(plan.skip),
                "conflict": sorted(plan.conflict), "blocked": sorted(plan.blocked)}
    return installer.commit(entries, plan, force=args.force_overwrite)


def do_install_cmdc(args):
    """Install shared core, then the Command Code adapter artifacts."""
    _, SHARED_CORE_VERSION, native_installer = _cmdc_adapter_modules()
    cmdc_home = Path(args.cmdc_home).resolve()
    agents_home = Path(args.agents_home).resolve()
    dry = args.dry_run

    try:
        _validate_managed_role_sources()
    except Exception as e:
        print(f"  FAIL: role package preflight failed; nothing written: {e}")
        sys.exit(1)

    print(f"{'[DRY RUN] ' if dry else ''}=== tianji-v2 install (cmdc) ===")
    print(f"  Command Code home: {cmdc_home}")
    print(f"  Shared skills:     {agents_home / 'skills'} (core v{SHARED_CORE_VERSION})")

    # Command Code's model source is its own menu, so the pool skill is not part
    # of this install: shipping it would install and later check a pool nobody
    # asked for.
    shared = _sync_shared_core(args, include_pool=False)
    if dry:
        print(f"  [dry] shared core to write: {len(shared.get('write', []))}")
    else:
        print(f"  shared core: {len(shared.get('written', []))} written, "
              f"{len(shared.get('skipped', []))} unchanged")
        if shared.get("conflict"):
            print(f"  shared core conflicts (kept): {', '.join(sorted(shared['conflict']))}")
        if shared.get("blocked"):
            print(f"  shared core blocked (older version): {', '.join(sorted(shared['blocked']))}")

    report = native_installer.install(
        cmdc_home, src_root(), dry_run=dry, force=args.force_overwrite,
        shared_scripts=agents_home / "skills" / "tianji" / "scripts",
    )
    if dry:
        print(f"  [dry] roles/mod to write: {len(report['write'])}")
        print("  Next: re-run without --dry-run, restart Command Code.")
    else:
        print(f"  adapter: {len(report['written'])} written, {len(report['skip'])} unchanged")
        if report.get("conflict"):
            print(f"  adapter conflicts (kept): {', '.join(sorted(report['conflict']))}")
        print("  Next: restart Command Code, then invoke 天机 for role binding.")


def do_uninstall_cmdc(args):
    _, _, native_installer = _cmdc_adapter_modules()
    cmdc_home = Path(args.cmdc_home).resolve()
    print(f"{'[DRY RUN] ' if args.dry_run else ''}=== tianji-v2 uninstall (cmdc) ===")
    if args.dry_run:
        print(f"  [dry] would remove adapter-managed files under {cmdc_home}")
        return
    report = native_installer.uninstall(cmdc_home)
    print(f"  Removed ({len(report['removed'])}):")
    for item in report["removed"]:
        print(f"    - {item}")
    print("  Shared core untouched (uninstall it with --host kimi/codex if needed).")


def do_status_cmdc(args):
    _, _, native_installer = _cmdc_adapter_modules()
    skill_dir = src_root() / "skills" / "tianji"
    for path in (str(skill_dir / "scripts"), str(skill_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from host_adapters.cmdc.detector import CmdcDetector

    cmdc_home = Path(args.cmdc_home).resolve()
    agents_home = Path(args.agents_home).resolve()
    detector = CmdcDetector(root=cmdc_home)

    checks = {}
    # No pool check: this host's models come from its own menu.
    core_path = agents_home / "skills" / CORE_SKILLS[0]
    checks[f"skills/{CORE_SKILLS[0]}/"] = (core_path.exists(), str(core_path))
    checks["managed-roles"] = detector.roles_ok()
    checks["managed-hooks"] = detector.hooks_ok()
    checks["model-menu"] = (
        bool(detector.model_ids()),
        f"{len(detector.model_ids())} 个模型 (Command Code {detector.native_version()})",
    )
    checks["role-bindings"] = detector.role_bindings_ok()
    checks["routing"] = detector.routing_supported()
    # The route proof is one fact with one reader. `probe_ok()` verdicts the level
    # `probe_level()` derives (live evidence, else the persisted proof re-judged
    # against the current snapshot), and the conclusion machine below reads that
    # same verdict -- deriving a level here would let this table and the machine
    # answer differently about one and the same dispatch.
    checks["route-proof"] = detector.probe_ok()

    print(f"{'[DRY RUN] ' if args.dry_run else ''}=== tianji-v2 status (cmdc) ===\n")
    print(f"  {'Check':<38} {'Status':<10} Detail")
    print(f"  {'-' * 74}")
    for name, (ok, detail) in checks.items():
        print(f"  {name:<38} {'OK' if ok else 'MISSING':<10} {detail}")

    level = detector.probe_level()
    print(f"\n  proof_level: {level}")
    print(f"  actual_model_verified: {str(detector.proof().actual_model_verified(detector.current_snapshot())).lower()}")
    if level == "host_dispatch":
        print("  Command Code 无上游模型标识：最高只能证明到宿主派发 (host_dispatch)。")
    # The same machine the doctor runs decides READY; this table does not.
    conclusions, _, _ = _shared_modules()
    verdict = _print_shared_verdict(detector)
    if verdict in conclusions.ACTIONABLE:
        print("\n  Installation and routing checks PASSED. Tianji is READY.\n")
    else:
        print("\n  NOT ready. Run install --host cmdc, then bind the four core roles in Command Code.\n")
    return conclusions.exit_code(verdict)


# ================================================================
# HOST ADAPTERS / MAIN
# ================================================================

class HostAdapter:
    """宿主适配边界；编排主体不通过这里分叉。"""

    def __init__(self, name: str, install, uninstall, status):
        self.name = name
        self.install = install
        self.uninstall = uninstall
        self.status = status

    def run(self, command: str, args):
        return getattr(self, command)(args)


HOST_ADAPTERS = {
    "kimi": HostAdapter("kimi", do_install, do_uninstall, do_status),
    "codex": HostAdapter("codex", do_install_codex, do_uninstall_codex, do_status_codex),
    "cmdc": HostAdapter("cmdc", do_install_cmdc, do_uninstall_cmdc, do_status_cmdc),
}


def main() -> int:
    # UTF-8 stdout, the same unlock the other shared scripts use: on Windows the
    # default console encoding is not UTF-8 and the verdict text is Chinese.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, LookupError, ValueError):
        pass
    try:
        args = parse_args()
        if not args.host:
            # No silent kimi default: refuse rather than install the wrong host.
            _, _, host_detectors = _shared_modules()
            args.host, reason = host_detectors.detect_host()
            if args.host is None:
                print(f"无法确定宿主（{reason}）；请显式传 --host {{{', '.join(sorted(HOST_ADAPTERS))}}}")
                return 2
        result = HOST_ADAPTERS[args.host].run(args.command, args)
    except SystemExit:
        # The command already decided its own code (a failed write rolls back
        # and exits 1); do not rewrite it.
        raise
    except Exception as exc:
        # A broken adapter or a crashed command reached no verdict: report the
        # real exception and return the "no verdict" code, never a success.
        print(f"[FAIL] 天机命令失败，无法得出结论: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    # status returns the shared exit code for its verdict; install and uninstall
    # return nothing when they succeed.
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
