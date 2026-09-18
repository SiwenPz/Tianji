#!/usr/bin/env python3
"""Configure Codex to route Tianji's parent and native agents through one provider.

This is an explicit onboarding action. It never guesses role bindings and it
will not replace Codex's main provider unless --allow-main-provider-change is
present. Every touched TOML file is preflighted, backed up, written atomically,
parsed again, and rolled back as a group on failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from role_contract import all_roles  # noqa: E402


# Codex names its role files tianji-<role>.toml and keys its adapter state by the
# bare role name. Derived from the shared contract: a retyped roster is how one
# host keeps a name the contract no longer has.
ROLES = tuple(name.removeprefix("tianji-") for name in all_roles())
PROVIDER_ID = "tianji"
STATE_NAME = "tianji-adapter.toml"
START = "# >>> tianji-codex-provider >>>"
END = "# <<< tianji-codex-provider <<<"
MANAGED_RE = re.compile(re.escape(START) + r".*?" + re.escape(END) + r"\n?", re.DOTALL)


def q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def native_subject_sha256(models: dict[str, str], roles: dict[str, str],
                          role_hashes: dict[str, str]) -> str:
    subject = {
        "model_source": "host_native",
        "models": models,
        "roles": roles,
        "role_hashes": role_hashes,
    }
    encoded = json.dumps(
        subject, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return sha256_text(encoded)


def parse_pairs(values: list[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, sep, target = value.partition("=")
        name, target = name.strip(), target.strip()
        if not sep or not name or not target:
            raise ValueError(f"invalid {label}: {value!r}; expected name=value")
        if name in result:
            raise ValueError(f"duplicate {label}: {name}")
        result[name] = target
    return result


def validate_base_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("--base-url must be an absolute http(s) URL")
    if parsed.path.rstrip("/") != "/v1":
        raise ValueError("--base-url must end in /v1")
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Codex Tianji adapter requires the local tianji-proxy Responses endpoint")
    return value


def split_preamble(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return lines[:index], lines[index:]
    return lines, []


def replace_top_level(text: str, updates: dict[str, str]) -> str:
    """Replace only root scalar assignments, leaving every table untouched."""
    clean = MANAGED_RE.sub("", text)
    preamble, tables = split_preamble(clean)
    remaining = dict(updates)
    output: list[str] = []
    assignment = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
    for line in preamble:
        match = assignment.match(line)
        key = match.group(1) if match else None
        if key in remaining:
            output.append(f"{key} = {q(remaining.pop(key))}")
        else:
            output.append(line)
    insert_at = len(output)
    while insert_at and not output[insert_at - 1].strip():
        insert_at -= 1
    for key, value in remaining.items():
        output.insert(insert_at, f"{key} = {q(value)}")
        insert_at += 1
    return "\n".join(output + tables).strip() + "\n"


def provider_block(base_url: str, env_key: str | None, adapter_id: str) -> str:
    lines = [
        START,
        f"[model_providers.{PROVIDER_ID}]",
        'name = "Tianji unified router"',
        'wire_api = "responses"',
        f"base_url = {q(base_url)}",
        f'http_headers = {{ "X-Tianji-Adapter" = {q(adapter_id)} }}',
    ]
    if env_key:
        lines.append(f"env_key = {q(env_key)}")
    lines.append(END)
    return "\n".join(lines) + "\n"


def configure_main(text: str, base_url: str, main_model: str,
                   env_key: str | None, adapter_id: str) -> str:
    updated = replace_top_level(text, {"model": main_model, "model_provider": PROVIDER_ID})
    return updated.rstrip() + "\n\n" + provider_block(base_url, env_key, adapter_id)


def restore_main(text: str, original: dict) -> str:
    clean = MANAGED_RE.sub("", text)
    preamble, tables = split_preamble(clean)
    keys = {"model", "model_provider"}
    assignment = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
    output = [line for line in preamble
              if not (assignment.match(line) and assignment.match(line).group(1) in keys)]
    insert_at = len(output)
    while insert_at and not output[insert_at - 1].strip():
        insert_at -= 1
    restored = []
    if original.get("model_present"):
        restored.append(f"model = {q(original['model'])}")
    if original.get("provider_present"):
        restored.append(f"model_provider = {q(original['model_provider'])}")
    output[insert_at:insert_at] = restored
    return "\n".join(output + tables).strip() + "\n"


def configure_role(text: str, binding: str, models: dict[str, str]) -> str:
    parsed = tomllib.loads(text)
    if not isinstance(parsed.get("developer_instructions"), str):
        raise ValueError("role file has no developer_instructions")
    lines = [line for line in text.splitlines()
             if not line.startswith("# tianji-role-binding = ")
             and not re.match(r"^model\s*=", line)]
    if binding == "主模型":
        lines.append('# tianji-role-binding = "primary"')
    else:
        if binding not in models:
            raise ValueError(f"unknown model alias for role: {binding}")
        lines.append('# tianji-role-binding = "fixed"')
        lines.append(f"model = {q(models[binding])}")
    return "\n".join(lines).strip() + "\n"


def state_text(*, base_url: str, main_model: str, env_key: str | None,
               adapter_id: str,
               models: dict[str, str], roles: dict[str, str],
               original: dict, config_sha256: str, role_hashes: dict[str, str]) -> str:
    lines = [
        "version = 1",
        'status = "configured"',
        f"configured_at = {q(datetime.now(timezone.utc).isoformat())}",
        f"provider_id = {q(PROVIDER_ID)}",
        f"base_url = {q(base_url)}",
        f"main_model = {q(main_model)}",
        f"adapter_id = {q(adapter_id)}",
        f"config_sha256 = {q(config_sha256)}",
    ]
    if env_key:
        lines.append(f"env_key = {q(env_key)}")
    lines.extend([
        "", "[original]",
        f"model_present = {str(original['model_present']).lower()}",
        f"provider_present = {str(original['provider_present']).lower()}",
    ])
    if original["model_present"]:
        lines.append(f"model = {q(original['model'])}")
    if original["provider_present"]:
        lines.append(f"model_provider = {q(original['model_provider'])}")
    lines.extend(["", "[models]"])
    for alias, model in models.items():
        lines.append(f"{q(alias)} = {q(model)}")
    lines.extend(["", "[roles]"])
    for role in ROLES:
        lines.append(f"{role} = {q(roles[role])}")
    lines.extend(["", "[role_hashes]"])
    for role in ROLES:
        lines.append(f"{role} = {q(role_hashes[role])}")
    return "\n".join(lines) + "\n"


def native_state_text(*, main_model: str | None, models: dict[str, str],
                      roles: dict[str, str], role_hashes: dict[str, str]) -> str:
    lines = [
        "version = 1",
        'status = "configured"',
        'model_source = "host_native"',
        f"configured_at = {q(datetime.now(timezone.utc).isoformat())}",
        f"subject_sha256 = {q(native_subject_sha256(models, roles, role_hashes))}",
    ]
    if main_model:
        lines.append(f"main_model = {q(main_model)}")
    lines.extend(["", "[models]"])
    for alias, model in models.items():
        lines.append(f"{q(alias)} = {q(model)}")
    lines.extend(["", "[roles]"])
    for role in ROLES:
        lines.append(f"{role} = {q(roles[role])}")
    lines.extend(["", "[role_hashes]"])
    for role in ROLES:
        lines.append(f"{role} = {q(role_hashes[role])}")
    return "\n".join(lines) + "\n"


def backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return path.with_name(f"{path.name}.bak-{stamp}")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def write_transaction(changes: dict[Path, str]) -> list[Path]:
    for text in changes.values():
        tomllib.loads(text)
    originals = {path: path.read_bytes() if path.exists() else None for path in changes}
    backups: list[Path] = []
    try:
        for path, original in originals.items():
            if original is not None:
                backup = backup_path(path)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)
                backups.append(backup)
        for path, text in changes.items():
            atomic_write(path, text)
            with path.open("rb") as stream:
                tomllib.load(stream)
    except Exception:
        for path, original in originals.items():
            if original is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_bytes(original)
        raise
    return backups


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure Tianji's Codex provider and role bindings")
    parser.add_argument("--codex-home", default=os.path.expanduser("~/.codex"))
    parser.add_argument("--restore", action="store_true", help="restore the pre-Tianji main provider")
    parser.add_argument("--host-native", action="store_true",
                        help="bind roles to models supplied by the Codex host without changing its provider")
    parser.add_argument("--base-url", help="unified Responses endpoint root ending in /v1")
    parser.add_argument("--main-model")
    parser.add_argument("--env-key", help="environment variable containing the router bearer token")
    parser.add_argument("--model", action="append", default=[], metavar="ALIAS=MODEL")
    parser.add_argument("--role", action="append", default=[], metavar="ROLE=ALIAS|主模型")
    parser.add_argument("--allow-main-provider-change", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.codex_home).resolve()
    if args.restore:
        try:
            state_path = root / STATE_NAME
            state_text_raw = state_path.read_text(encoding="utf-8-sig")
            state = tomllib.loads(state_text_raw)
            config_path = root / "config.toml"
            config_text = config_path.read_text(encoding="utf-8-sig")
            config = tomllib.loads(config_text)
            if state.get("status") != "configured":
                raise ValueError("no active Tianji adapter state")
            if (config.get("model_provider") != PROVIDER_ID
                    or config.get("model") != state.get("main_model")
                    or START not in config_text):
                raise ValueError("Codex main provider changed after Tianji setup; refusing to overwrite it")
            restored_config = restore_main(config_text, state.get("original", {}))
            restored_state = re.sub(r'^status = "configured"$', 'status = "restored"',
                                    state_text_raw, count=1, flags=re.MULTILINE)
            backups = write_transaction({config_path: restored_config, state_path: restored_state})
        except Exception as exc:
            print(f"Error: {exc}; adapter not restored.", file=sys.stderr)
            return 1
        print(f"Restored pre-Tianji Codex provider. Backups created: {len(backups)}")
        return 0
    if args.host_native:
        try:
            if args.base_url or args.env_key or args.allow_main_provider_change:
                raise ValueError("host-native mode does not accept provider-change options")
            models = parse_pairs(args.model, "model alias")
            roles = parse_pairs(args.role, "role")
            if not models:
                raise ValueError("at least one --model ALIAS=MODEL is required")
            if set(roles) != set(ROLES):
                missing = sorted(set(ROLES) - set(roles))
                extra = sorted(set(roles) - set(ROLES))
                raise ValueError(
                    f"roles must be exactly {', '.join(ROLES)}; missing={missing}, extra={extra}"
                )
            if any(binding == "主模型" for binding in roles.values()):
                raise ValueError(
                    "host-native roles must use fixed model aliases; 主模型 is not allowed"
                )
            resolved_roles = {
                role: models.get(binding)
                for role, binding in roles.items()
            }
            if not all(resolved_roles.values()):
                raise ValueError("every fixed role must reference a configured model alias")
            if resolved_roles["worker"] == resolved_roles["verifier"]:
                raise ValueError("worker and verifier must resolve to different actual model IDs")

            existing_state_path = root / STATE_NAME
            if existing_state_path.exists():
                existing_state = tomllib.loads(existing_state_path.read_text(encoding="utf-8-sig"))
                if (existing_state.get("status") == "configured"
                        and existing_state.get("model_source") != "host_native"):
                    raise ValueError("restore the active provider adapter before switching to host-native")

            role_texts: dict[str, str] = {}
            for role in ROLES:
                role_path = root / "agents" / f"tianji-{role}.toml"
                if not role_path.exists():
                    raise ValueError(f"missing installed role file: {role_path}")
                role_texts[role] = configure_role(
                    role_path.read_text(encoding="utf-8"), roles[role], models,
                )
            role_hashes = {role: sha256_text(text) for role, text in role_texts.items()}
            state = native_state_text(
                main_model=args.main_model, models=models, roles=roles, role_hashes=role_hashes,
            )
            changes = {root / STATE_NAME: state}
            changes.update({
                root / "agents" / f"tianji-{role}.toml": text
                for role, text in role_texts.items()
            })
            backups = write_transaction(changes)
        except Exception as exc:
            print(f"Error: {exc}; no partial configuration kept.", file=sys.stderr)
            return 1
        print(
            f"Configured Codex Tianji host-native roles "
            f"({len(models)} models, {len(roles)} roles)"
        )
        print(f"Backups created: {len(backups)}")
        print("Run codex-routing-probe.py before Tianji can become READY.")
        return 0
    if not args.allow_main_provider_change:
        print("Error: explicit --allow-main-provider-change is required; no files written.", file=sys.stderr)
        return 2
    try:
        if not args.base_url or not args.main_model:
            raise ValueError("--base-url and --main-model are required for configuration")
        base_url = validate_base_url(args.base_url)
        models = parse_pairs(args.model, "model alias")
        roles = parse_pairs(args.role, "role")
        if not models:
            raise ValueError("at least one --model ALIAS=MODEL is required")
        if set(roles) != set(ROLES):
            missing = sorted(set(ROLES) - set(roles))
            extra = sorted(set(roles) - set(ROLES))
            raise ValueError(f"roles must be exactly {', '.join(ROLES)}; missing={missing}, extra={extra}")
        if args.main_model not in models.values():
            raise ValueError("--main-model must be present in the configured model menu")
        resolved_roles = {
            role: (args.main_model if binding == "主模型" else models.get(binding))
            for role, binding in roles.items()
        }
        if not all(resolved_roles.values()):
            raise ValueError("every fixed role must reference a configured model alias")
        if resolved_roles["worker"] == resolved_roles["verifier"]:
            raise ValueError("worker and verifier must resolve to different actual model IDs")

        config_path = root / "config.toml"
        config_text = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else ""
        original_cfg = tomllib.loads(config_text)
        if PROVIDER_ID in original_cfg.get("model_providers", {}) and START not in config_text:
            raise ValueError("[model_providers.tianji] already exists but is not Tianji-managed")
        existing_state_path = root / STATE_NAME
        existing_state = {}
        if existing_state_path.exists():
            existing_state = tomllib.loads(existing_state_path.read_text(encoding="utf-8-sig"))
        if existing_state.get("status") == "configured":
            original = existing_state.get("original", {})
        else:
            original = {
                "model_present": isinstance(original_cfg.get("model"), str),
                "model": original_cfg.get("model", ""),
                "provider_present": isinstance(original_cfg.get("model_provider"), str),
                "model_provider": original_cfg.get("model_provider", ""),
            }
        adapter_id = secrets.token_urlsafe(24)
        new_config = configure_main(config_text, base_url, args.main_model, args.env_key, adapter_id)
        role_texts: dict[str, str] = {}
        for role in ROLES:
            role_path = root / "agents" / f"tianji-{role}.toml"
            if not role_path.exists():
                raise ValueError(f"missing installed role file: {role_path}")
            role_texts[role] = configure_role(role_path.read_text(encoding="utf-8"), roles[role], models)
        role_hashes = {role: sha256_text(text) for role, text in role_texts.items()}
        state = state_text(base_url=base_url, main_model=args.main_model, env_key=args.env_key,
                           adapter_id=adapter_id,
                           models=models, roles=roles, original=original,
                           config_sha256=sha256_text(new_config), role_hashes=role_hashes)
        changes = {config_path: new_config, root / STATE_NAME: state}
        changes.update({root / "agents" / f"tianji-{role}.toml": text
                        for role, text in role_texts.items()})
        backups = write_transaction(changes)
    except Exception as exc:
        print(f"Error: {exc}; no partial configuration kept.", file=sys.stderr)
        return 1

    print(f"Configured Codex Tianji adapter: {base_url} ({len(models)} models, {len(roles)} roles)")
    print(f"Backups created: {len(backups)}")
    print("Restart Codex, then run codex-routing-probe.py before Tianji can become READY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
