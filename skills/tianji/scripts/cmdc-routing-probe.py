#!/usr/bin/env python3
"""Verify Command Code host dispatch and record the route proof.

Runs after a Tianji role has been dispatched once in the current workspace --
any bound role will do, because dispatching is the evidence. It pairs that
dispatch's start/stop events by EventKey and stores the resulting evidence in
the adapter state. The proof never exceeds host_dispatch, because Command Code
subagent events carry no upstream model identity.

Usage:
  python cmdc-routing-probe.py --cmdc-home ~/.commandcode --workspace .
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))

from host_adapters.cmdc.detector import CmdcDetector  # noqa: E402
from host_adapters.cmdc.proof_collector import collect, fresh_after, record  # noqa: E402
from route_proof import PROOF_LEVEL_DISPATCH  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description="Command Code Tianji routing probe")
    parser.add_argument("--cmdc-home", default=os.path.expanduser("~/.commandcode"))
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--role", default=None, action="append",
                        help="narrow the evidence to this role (repeatable); "
                             "default: any role the configuration binds")
    parser.add_argument("--allow-stale", action="store_true",
                        help="accept dispatch evidence from before the last install")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cmdc_home = Path(args.cmdc_home).resolve()
    detector = CmdcDetector(root=cmdc_home)
    ledger = Path(args.workspace).resolve() / ".tianji" / "state.jsonl"

    cutoff = None if args.allow_stale else fresh_after(cmdc_home)
    proof, reason = collect(detector, ledger, roles=args.role, not_before=cutoff)
    if proof is None:
        print(f"FAIL: {reason}")
        return 1

    record(detector, proof)
    # Report the level every reader judges, not the one this script just paired:
    # the pairing is evidence for the current configuration, and one reader means
    # the probe cannot see a different answer than `install.py status` does.
    level = detector.probe_level(ledger)
    effective = level == PROOF_LEVEL_DISPATCH
    # Pairing a dispatch is not the same fact as proving one holds for the
    # current configuration, and a first line that only says "OK" gets read as
    # the second. Say which one this is.
    print(f"{'OK' if effective else 'PAIRED'}: {reason}")
    print(f"proof_level: {level}")
    print(f"actual_model_verified: {str(proof.actual_model_verified(detector.current_snapshot())).lower()}")
    print(f"event_key: {proof.dispatch.event_key}")
    if effective:
        print("说明: Command Code 不暴露上游模型，最高证明到宿主派发 (host_dispatch)。")
    else:
        print(f"说明: 派发已配对，但当前配置下证明级别只有 {level} -- 配置在派发后变过，"
              "或该派发早于最后一次安装；配置冻结后重新派发一次即可取证。")
    return 0 if effective else 1


if __name__ == "__main__":
    raise SystemExit(main())
