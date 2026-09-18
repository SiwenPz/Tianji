#!/usr/bin/env python3
"""Bind Tianji roles to real Command Code models (thin adapter wrapper).

Usage:
  python cmdc-role-configure.py --cmdc-home ~/.commandcode list
  python cmdc-role-configure.py --cmdc-home ~/.commandcode plan
  python cmdc-role-configure.py --cmdc-home ~/.commandcode set-tier economy <model-id>
  python cmdc-role-configure.py --cmdc-home ~/.commandcode set tianji-worker <model-id>
  python cmdc-role-configure.py --cmdc-home ~/.commandcode reset tianji-worker

The tiers come from the shared role contract (economy / quality / escalation).
Mapping a tier binds every installed role in it, which is the normal way to
configure this host: pick three models off the live menu, once. Individual roles
can still be bound or reset on their own.

Only the four core roles must be bound before Tianji is READY; the other four
may be mapped with their tier or bound on first use.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))

from host_adapters.cmdc.detector import CmdcDetector  # noqa: E402
from host_adapters.cmdc.role_configure import (  # noqa: E402
    REQUIRED_BINDINGS,
    RoleConfigError,
    bindings,
    bindings_by_tier,
    describe,
    set_binding,
    set_tier_binding,
    unconfigured,
    unmapped_tiers,
)

import python_runtime  # noqa: E402

import hashlib
import tempfile
import threading
import time

# The facts snapshot needs two host CLI observations ("--version" and
# "--list-models"). Each one is a full CLI start-up, and the menu pays an API
# round trip on top; the dispatcher asks for facts again before *every* order,
# so the same two answers were being bought once per order. Memoize them
# briefly on disk, keyed by the cmdc home: a later order costs nothing, while a
# real menu or version change is still observed once the memo has expired.
# The window is a minute, not minutes: these answers become the dispatch
# snapshot the route proof compares against, so a replay must stay short enough
# that "what the dispatcher saw" is still true when the ledger records it.
CLI_MEMO_TTL_SECONDS = 60.0
_CLI_MEMO_DIR = Path(tempfile.gettempdir()) / "tianji-cmdc-cli-memo"


def cached_cli_runner(cmdc_home: Path):
    """Return ``(run, warm)`` around the detector's own CLI runner, memoized.

    ``run`` replays a memoized answer while it is younger than the TTL and asks
    the CLI otherwise; an answer is never rewritten, only replayed. ``warm``
    fetches several independent observations at once, because fetched one after
    another they cost the sum of the CLI start-ups. Only answers the CLI
    actually gave are stored: a failed read is not a fact worth remembering.
    """
    def default_run(args):
        # The detector's own default runner, unmemoized.
        return CmdcDetector(root=cmdc_home)._run(args)

    memo_path = _CLI_MEMO_DIR / (
        hashlib.sha256(str(cmdc_home).encode("utf-8")).hexdigest()[:16] + ".json"
    )
    try:
        stored = json.loads(memo_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    now = time.time()
    memo: dict[str, list] = {}
    if isinstance(stored, dict):
        for key, entry in stored.items():
            if (isinstance(entry, list) and len(entry) == 2
                    and isinstance(entry[0], (int, float))
                    and now - entry[0] <= CLI_MEMO_TTL_SECONDS
                    and isinstance(entry[1], str)):
                memo[key] = [entry[0], entry[1]]

    lock = threading.Lock()

    def run(args):
        key = " ".join(args)
        with lock:
            hit = memo.get(key)
        if hit is not None:
            return hit[1]
        answer = default_run(args)
        if answer is None:
            return None
        with lock:
            memo[key] = [time.time(), answer]
            try:
                _CLI_MEMO_DIR.mkdir(parents=True, exist_ok=True)
                memo_path.write_text(json.dumps(memo), encoding="utf-8")
            except OSError:
                pass
        return answer

    def warm(calls):
        threads = [threading.Thread(target=run, args=(call,)) for call in calls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    return run, warm


def parse_args():
    parser = argparse.ArgumentParser(description="Command Code Tianji role binding")
    parser.add_argument("action",
                        choices=["list", "plan", "set", "set-tier", "reset", "check", "facts"])
    parser.add_argument("role", nargs="?", default="")
    parser.add_argument("model", nargs="?", default="")
    parser.add_argument("--cmdc-home", default=os.path.expanduser("~/.commandcode"))
    parser.add_argument("--source-root", default="",
                        help="repository root holding agents/ (shared role packages)")
    return parser.parse_args()


def resolve_source_root(cmdc_home: Path, explicit: str) -> Path | None:
    """The checkout holding ``agents/``: the flag, else what the install recorded.

    The installed tree has the skill but not the role packages, so after install
    this path exists nowhere except the runtime locator. Reading it back is what
    keeps a rebind from depending on the operator remembering a path.
    """
    if explicit:
        return Path(explicit)
    recorded = (python_runtime.read(cmdc_home) or {}).get("source_root", "")
    return Path(recorded) if isinstance(recorded, str) and recorded else None


def main() -> int:
    args = parse_args()
    cmdc_home = Path(args.cmdc_home).resolve()
    agents_dir = cmdc_home / "agents"
    detector = CmdcDetector(root=cmdc_home)
    source_root = resolve_source_root(cmdc_home, args.source_root)

    if args.action == "facts":
        # What the dispatcher saw, for `run_registry.py open` to record before
        # dispatching. The shared layer cannot compute these itself -- that
        # would make it depend on this host's layout -- so the host prints them
        # and the dispatcher passes them through.
        # This is the read the dispatcher repeats before every order, so it is
        # the one that carries the CLI cost: reuse the memo while it is fresh,
        # and fetch both observations at once when it is not.
        cli_run, warm_cli = cached_cli_runner(cmdc_home)
        detector = CmdcDetector(root=cmdc_home, run=cli_run)
        if not args.role:
            print("facts requires a role", file=sys.stderr)
            return 2
        warm_cli([["--version"], ["--list-models"]])
        from route_proof import snapshot_digest  # noqa: E402
        from host_adapters.cmdc.role_renderer import (  # noqa: E402
            declared_model, installed_max_turns,
        )

        model, source = declared_model(agents_dir / f"{args.role}.md")
        print(json.dumps({
            "role": args.role,
            "model": model,
            "model_source": source,
            "subject_digest": snapshot_digest(detector.current_snapshot()),
            # What the task book has to fit inside: the host ends the dispatch
            # at this budget, and a dispatch that ends early returns nothing.
            "subagent_limits": {"turns": installed_max_turns(agents_dir / f"{args.role}.md")},
        }, ensure_ascii=False))
        return 0

    if args.action == "check":
        pending = unconfigured(agents_dir)
        # The level every reader judges (live evidence first, else the persisted
        # proof re-judged against the current snapshot) -- this used to print the
        # persisted level directly, which made a third reader that could disagree
        # with `install.py status` about one and the same dispatch.
        print(f"proof_level: {detector.probe_level()}")
        for tier, roles in bindings_by_tier(agents_dir).items():
            shown = ", ".join(f"{role}={binding or '(未绑定)'}" for role, binding in roles.items())
            print(f"[{tier}] {shown}")
        unmapped = unmapped_tiers(agents_dir)
        if unmapped:
            print("未映射的 tier: " + ", ".join(unmapped))
        if pending:
            print("未绑定核心角色: " + ", ".join(pending))
            return 1
        print(f"{len(REQUIRED_BINDINGS)} 个核心角色均已绑定")
        return 0

    if args.action in ("list", "plan"):
        if args.action == "list":
            for model in detector.model_ids():
                print(model)
        else:
            print(describe(agents_dir, detector.model_ids()))
        return 0

    if not args.role:
        print("缺少角色名", file=sys.stderr)
        return 2

    # Every remaining action re-renders the role from its shared package, so a
    # missing root is reported here instead of as a package-loading error.
    if source_root is None:
        print(
            "FAIL: 找不到共享角色包所在的位置（安装时没记录过 source_root）。\n"
            "      跑一次 python install.py install --host cmdc，或显式传 "
            "--source-root <仓库根目录>。",
            file=sys.stderr,
        )
        return 1

    if args.action == "set-tier":
        if not args.model:
            print("缺少模型 id", file=sys.stderr)
            return 2
        try:
            written = set_tier_binding(
                agents_dir, args.role, args.model,
                models=detector.model_ids(), source_root=source_root,
            )
        except RoleConfigError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        print(f"[{args.role}] -> {args.model} ({len(written)} 个角色)")
        return 0

    binding = "主模型" if args.action == "reset" else args.model
    if args.action == "set" and not binding:
        print("缺少模型 id", file=sys.stderr)
        return 2
    try:
        target = set_binding(
            agents_dir, args.role, binding,
            models=detector.model_ids(), source_root=source_root,
        )
    except RoleConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"{args.role} -> {binding or '主模型'} ({target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
