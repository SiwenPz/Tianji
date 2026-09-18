#!/usr/bin/env python3
"""Run a real Codex parent -> tianji-worker round trip and attest its config."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROLES = ("worker", "verifier", "referee")

# The role this prober dispatches. Any bound role proves the same thing, but a
# prober has to name one to spawn, and the worker is the role every host has and
# the one real work is dispatched to. The proof records what it observed rather
# than assuming: the role it names is written into the proof.
PROOF_ROLE = "worker"
PROOF_AGENT = f"tianji-{PROOF_ROLE}"
PROOF_RE = re.compile(r"\n?\[proof\]\n.*?(?=\n\[|\Z)", re.DOTALL)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def native_subject_digest(state: dict) -> str:
    subject = {
        "model_source": "host_native",
        "models": state.get("models", {}),
        "roles": state.get("roles", {}),
        "role_hashes": state.get("role_hashes", {}),
    }
    encoded = json.dumps(
        subject, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return digest(encoded)


def atomic_toml_write(path: Path, text: str) -> Path:
    tomllib.loads(text)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    original = path.read_bytes()
    handle, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        with path.open("rb") as stream:
            tomllib.load(stream)
    except Exception:
        path.write_bytes(original)
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
    return backup


def validate_adapter(root: Path) -> tuple[dict, str]:
    state_path = root / "tianji-adapter.toml"
    config_path = root / "config.toml"
    with state_path.open("rb") as stream:
        state = tomllib.load(stream)
    if state.get("status") != "configured":
        raise ValueError("adapter state is not configured")
    if state.get("model_source") == "host_native":
        if native_subject_digest(state) != state.get("subject_sha256"):
            raise ValueError("Codex host-native menu or role bindings changed after onboarding")
    else:
        config_text = config_path.read_text(encoding="utf-8-sig")
        config = tomllib.loads(config_text)
        if digest(config_text) != state.get("config_sha256"):
            raise ValueError("Codex config changed after Tianji configuration; re-run onboarding")
        provider = config.get("model_providers", {}).get("tianji", {})
        if (config.get("model_provider") != "tianji"
                or config.get("model") != state.get("main_model")
                or provider.get("wire_api") != "responses"
                or provider.get("base_url") != state.get("base_url")):
            raise ValueError("Codex is not using the recorded Tianji Responses provider")
    hashes = state.get("role_hashes", {})
    for role in ROLES:
        text = (root / "agents" / f"tianji-{role}.toml").read_text(encoding="utf-8")
        if digest(text) != hashes.get(role):
            raise ValueError(f"tianji-{role} changed after role assignment")
    proof_binding = state.get("roles", {}).get(PROOF_ROLE)
    expected_model = (state.get("main_model") if proof_binding == "主模型"
                      else state.get("models", {}).get(proof_binding))
    if not expected_model:
        raise ValueError(f"{PROOF_AGENT} has no resolvable model")
    return state, expected_model


def read_observations(state: dict, since: int = 0) -> dict:
    """Read the proxy's short-lived route evidence for this adapter."""
    base_url = str(state["base_url"]).rstrip("/")
    query = urllib.parse.urlencode({"adapter_id": state["adapter_id"], "since": since})
    request = urllib.request.Request(base_url + "/tianji/observations?" + query)
    token = os.environ.get("TIANJI_PROXY_TOKEN", "")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def matching_observation(data: dict, expected_model: str) -> dict | None:
    for item in data.get("data", []):
        if (item.get("requested_model") == expected_model
                and item.get("protocol") == "responses"
                and item.get("success") is True
                and 200 <= int(item.get("status", 0)) < 300):
            return item
    return None


def _native_probe_key(item: dict, expected_model: str) -> tuple[str, str] | None:
    detail = item.get("detail", {})
    session_id = item.get("session_id")
    if (item.get("agent") != PROOF_AGENT
            or not isinstance(detail, dict)
            or detail.get("model") != expected_model
            or not isinstance(session_id, str) or not session_id):
        return None
    turn_id = detail.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return None
    return session_id, turn_id


def native_dispatch_observation(path: Path, offset: int,
                                expected_model: str) -> tuple[dict, dict] | None:
    if not path.exists():
        return None
    with path.open("rb") as stream:
        stream.seek(offset)
        raw = stream.read().decode("utf-8", errors="replace")
    starts: dict[tuple[str, str], dict] = {}
    completed: list[tuple[dict, dict]] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = _native_probe_key(item, expected_model)
        if key is None:
            continue
        if item.get("event") == "subagent_start":
            starts[key] = item
        elif item.get("event") == "subagent_stop" and key in starts:
            completed.append((starts[key], item))
    return completed[-1] if completed else None


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp()
    except ValueError:
        return None


def completed_native_observation(path: Path, since: str,
                                 expected_model: str) -> tuple[dict, dict] | None:
    since_timestamp = _timestamp(since)
    if since_timestamp is None:
        raise ValueError("--since must be an ISO-8601 timestamp")
    if not path.exists():
        return None
    starts: dict[tuple[str, str], dict] = {}
    completed: list[tuple[float, dict, dict]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        occurred = _timestamp(item.get("ts"))
        if occurred is None or occurred < since_timestamp:
            continue
        key = _native_probe_key(item, expected_model)
        if key is None:
            continue
        if item.get("event") == "subagent_start":
            starts[key] = item
        elif item.get("event") == "subagent_stop" and key in starts:
            completed.append((occurred, starts[key], item))
    if not completed:
        return None
    _, start, stop = max(completed, key=lambda entry: entry[0])
    return start, stop


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prove native Codex Tianji routing")
    parser.add_argument("--codex-home", default=os.path.expanduser("~/.codex"))
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--codex-bin", help=argparse.SUPPRESS)
    parser.add_argument("--from-ledger", action="store_true",
                        help="attest a probe already completed by the current Codex host")
    parser.add_argument("--since", help="only consider hook events at or after this ISO timestamp")
    parser.add_argument("--allow-transcript-only", action="store_true",
                        help="test-only fallback when no Tianji proxy observation endpoint exists")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.codex_home).resolve()
    try:
        state, expected_model = validate_adapter(root)
    except Exception as exc:
        print(f"Error: {exc}; probe not run.", file=sys.stderr)
        return 1

    native = state.get("model_source") == "host_native"
    ledger_path = Path(args.cwd).resolve() / ".tianji" / "state.jsonl"
    if args.from_ledger:
        if not native:
            print("Error: --from-ledger is only valid for host-native routing.", file=sys.stderr)
            return 1
        if not args.since:
            print("Error: --from-ledger requires --since.", file=sys.stderr)
            return 1
        try:
            observation = completed_native_observation(
                ledger_path, args.since, expected_model,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if observation is None:
            print(
                f"Error: no completed matching {PROOF_AGENT} hook pair was recorded after --since.",
                file=sys.stderr,
            )
            return 1
        start, stop = observation
        detail = stop["detail"]
        state_path = root / "tianji-adapter.toml"
        original = state_path.read_text(encoding="utf-8-sig")
        proof = [
            "[proof]",
            'status = "passed"',
            'proof_level = "host_dispatch"',
            "wire_verified = false",
            f"verified_at = {json.dumps(datetime.now(timezone.utc).isoformat())}",
            f"subject_sha256 = {json.dumps(state['subject_sha256'])}",
            f"proof_role = {json.dumps(PROOF_ROLE, ensure_ascii=False)}",
            f"expected_role_model = {json.dumps(expected_model, ensure_ascii=False)}",
            f"observed_model = {json.dumps(detail['model'], ensure_ascii=False)}",
            f"observed_session_id = {json.dumps(stop.get('session_id', ''), ensure_ascii=False)}",
            f"observed_turn_id = {json.dumps(detail['turn_id'], ensure_ascii=False)}",
            f"start_event_sha256 = {json.dumps(digest(json.dumps(start, ensure_ascii=False, sort_keys=True)))}",
            f"stop_event_sha256 = {json.dumps(digest(json.dumps(stop, ensure_ascii=False, sort_keys=True)))}",
        ]
        updated = PROOF_RE.sub("", original).rstrip() + "\n\n" + "\n".join(proof) + "\n"
        try:
            backup = atomic_toml_write(state_path, updated)
        except Exception as exc:
            print(f"Error: could not record probe proof: {exc}", file=sys.stderr)
            return 1
        print(f"PASS: current Codex host -> {PROOF_AGENT} dispatch ({expected_model})")
        print(f"Proof recorded; backup: {backup}")
        return 0

    if native:
        before_offset = ledger_path.stat().st_size if ledger_path.exists() else 0
        before = {"cursor": 0}
    else:
        try:
            before = read_observations(state)
        except Exception as exc:
            if not args.allow_transcript_only:
                print(f"Error: cannot read Tianji route evidence before probe: {exc}", file=sys.stderr)
                return 1
            before = {"cursor": 0}
    executable = args.codex_bin or shutil.which("codex.cmd" if os.name == "nt" else "codex")
    if not executable:
        print("Error: Codex CLI not found; probe not run.", file=sys.stderr)
        return 1

    token = "TIANJI_ROUTE_OK_" + secrets.token_hex(12)
    prompt = (
        f"Use the native spawn_agent tool to start exactly one agent_type={PROOF_AGENT}. "
        f"Tell it to reply with exactly {token}. Wait for it. Your final answer must be exactly {token}."
    )
    env = os.environ.copy()
    env["CODEX_HOME"] = str(root)
    command = ([sys.executable, executable] if str(executable).endswith(".py") else [executable])
    command += ["exec", "--strict-config", "--skip-git-repo-check", "--json",
               "--sandbox", "read-only", "-C", str(Path(args.cwd).resolve()), prompt]
    try:
        result = subprocess.run(command, env=env, text=True, encoding="utf-8", errors="replace",
                                capture_output=True, timeout=args.timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Error: Codex native probe failed: {exc}", file=sys.stderr)
        return 1
    transcript = result.stdout
    has_spawn_evidence = "spawn_agent" in transcript and PROOF_AGENT in transcript
    if result.returncode != 0 or token not in transcript or not has_spawn_evidence:
        print(f"Error: Codex native probe did not complete (exit={result.returncode}).", file=sys.stderr)
        return 1

    if native:
        observed = native_dispatch_observation(ledger_path, before_offset, expected_model)
        if observed is None:
            print(
                "Error: probe transcript passed but no matching completed hook pair was recorded.",
                file=sys.stderr,
            )
            return 1
        start, stop = observed
    else:
        try:
            evidence = read_observations(state, int(before.get("cursor", 0)))
            observed = matching_observation(evidence, expected_model)
        except Exception as exc:
            if not args.allow_transcript_only:
                print(f"Error: probe transcript passed but route evidence failed: {exc}", file=sys.stderr)
                return 1
            observed = None
        if observed is None and not args.allow_transcript_only:
            print(
                "Error: probe token passed but no successful Responses observation matched the expected model.",
                file=sys.stderr,
            )
            return 1

    state_path = root / "tianji-adapter.toml"
    original = state_path.read_text(encoding="utf-8-sig")
    proof = [
        "[proof]",
        'status = "passed"',
        f"verified_at = {json.dumps(datetime.now(timezone.utc).isoformat())}",
        f"proof_role = {json.dumps(PROOF_ROLE, ensure_ascii=False)}",
        f"expected_role_model = {json.dumps(expected_model, ensure_ascii=False)}",
        f"transcript_sha256 = {json.dumps(digest(result.stdout))}",
    ]
    if native:
        detail = stop["detail"]
        proof.extend([
            'proof_level = "host_dispatch"',
            "wire_verified = false",
            f"subject_sha256 = {json.dumps(state['subject_sha256'])}",
            f"observed_model = {json.dumps(detail['model'], ensure_ascii=False)}",
            f"observed_session_id = {json.dumps(stop.get('session_id', ''), ensure_ascii=False)}",
            f"observed_turn_id = {json.dumps(detail['turn_id'], ensure_ascii=False)}",
            f"start_event_sha256 = {json.dumps(digest(json.dumps(start, ensure_ascii=False, sort_keys=True)))}",
            f"stop_event_sha256 = {json.dumps(digest(json.dumps(stop, ensure_ascii=False, sort_keys=True)))}",
        ])
    else:
        proof.append(f"config_sha256 = {json.dumps(state['config_sha256'])}")
    if observed and not native:
        proof.extend([
            f"observed_channel = {json.dumps(observed.get('channel', ''), ensure_ascii=False)}",
            f"observed_upstream_id = {json.dumps(observed.get('upstream_id', ''), ensure_ascii=False)}",
            f"observed_protocol = {json.dumps(observed.get('protocol', ''), ensure_ascii=False)}",
        ])
    updated = PROOF_RE.sub("", original).rstrip() + "\n\n" + "\n".join(proof) + "\n"
    try:
        backup = atomic_toml_write(state_path, updated)
    except Exception as exc:
        print(f"Error: could not record probe proof: {exc}", file=sys.stderr)
        return 1
    print(f"PASS: native Codex parent -> {PROOF_AGENT} route ({expected_model})")
    print(f"Proof recorded; backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
