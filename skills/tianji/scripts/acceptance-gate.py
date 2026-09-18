#!/usr/bin/env python3
"""Run a Tianji task's mechanical acceptance commands.

Spec format::

  {"commands": [{"name": "tests", "command": "python -m unittest", "timeout": 120}]}

The gate is host-neutral: a host only needs to produce this JSON contract.
It returns zero only when every command exits zero and prints a bounded JSON
result suitable for a verifier or a ledger entry.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_TIMEOUT = 300
MAX_CAPTURE = 8000


def load_spec(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    commands = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(commands, list) or not commands:
        raise ValueError("spec.commands must be a non-empty list")
    normalized = []
    for index, item in enumerate(commands):
        if not isinstance(item, dict) or not isinstance(item.get("command"), str) or not item["command"].strip():
            raise ValueError(f"commands[{index}].command must be a non-empty string")
        timeout = item.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"commands[{index}].timeout must be positive")
        normalized.append({
            "name": str(item.get("name") or f"command-{index + 1}"),
            "command": item["command"],
            "cwd": item.get("cwd"),
            "timeout": min(float(timeout), 3600.0),
        })
    return normalized


def run_command(item: dict, root: Path) -> dict:
    cwd = (root / item["cwd"]).resolve() if item.get("cwd") else root
    try:
        cwd.relative_to(root)
    except ValueError:
        return {"name": item["name"], "command": item["command"], "cwd": str(cwd),
                "exit_code": None, "passed": False, "error": "cwd escapes acceptance root"}
    if not cwd.is_dir():
        return {"name": item["name"], "command": item["command"], "cwd": str(cwd),
                "exit_code": None, "passed": False, "error": "cwd does not exist"}
    try:
        result = subprocess.run(
            item["command"], cwd=str(cwd), shell=True, text=True,
            encoding="utf-8", errors="replace", capture_output=True,
            timeout=item["timeout"], check=False,
        )
        output = (result.stdout + result.stderr)[-MAX_CAPTURE:]
        return {"name": item["name"], "command": item["command"], "cwd": str(cwd),
                "exit_code": result.returncode, "passed": result.returncode == 0,
                "output": output}
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or ""))[-MAX_CAPTURE:]
        return {"name": item["name"], "command": item["command"], "cwd": str(cwd),
                "exit_code": None, "passed": False, "timed_out": True, "output": output}
    except OSError as exc:
        return {"name": item["name"], "command": item["command"], "cwd": str(cwd),
                "exit_code": None, "passed": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Tianji mechanical acceptance commands")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--cwd", default=os.getcwd(), type=Path)
    args = parser.parse_args()
    try:
        commands = load_spec(args.spec)
        results = [run_command(item, args.cwd.resolve()) for item in commands]
        report = {"verdict": "PASS" if all(item["passed"] for item in results) else "FAIL",
                  "commands": results}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = {"verdict": "FAIL", "commands": [], "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
