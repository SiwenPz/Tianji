#!/usr/bin/env python3
"""Where the shared core and its interpreter live, recorded at install time.

A host adapter must call the shared registry across a process boundary, which
needs things it cannot honestly guess:

* the interpreter that can run the shared scripts -- guessing decides the
  adapter depends on a ``python`` on PATH, which is exactly the assumption that
  breaks on machines where only ``py`` or a bundled interpreter exists;
* the path to the registry entry point in the installed tree;
* the source checkout the shared role packages live in. Installing copies the
  skill tree but not ``agents/``, so after install the role packages are only
  reachable through a path nothing else remembers -- and re-binding a role has
  to re-render it from its package. Recording that root is what makes a rebind
  one argument instead of a hunt.

All of them are discovered once, at install time, and recorded in one small
file that the adapter reads.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


LOCATOR_NAME = "tianji-runtime.json"
REGISTRY_ENTRY = "run_registry.py"

# Last resort only, and never a bare "python": each candidate is resolved
# through PATH so we record an absolute executable, not a name to search for.
INTERPRETER_CANDIDATES = ("python3", "python", "py")


def detect_interpreter() -> str:
    """The interpreter a host adapter should use to call the shared core.

    The interpreter running this installer is the strongest evidence that it
    works, so it wins; PATH probing is only a fallback.
    """
    if sys.executable:
        return sys.executable
    for candidate in INTERPRETER_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    return ""


def registry_path(scripts_dir: Path) -> Path:
    return Path(scripts_dir) / REGISTRY_ENTRY


def locator_payload(scripts_dir: Path, interpreter: str = "", source_root=None) -> dict:
    scripts_dir = Path(scripts_dir)
    payload = {
        "interpreter": interpreter or detect_interpreter(),
        "registry": str(registry_path(scripts_dir)),
        "scripts_dir": str(scripts_dir),
    }
    # Omitted rather than written empty: a missing key says "this install did
    # not record one", which is honest, where "" would look like a real path.
    if source_root is not None:
        payload["source_root"] = str(source_root)
    return payload


def record(scripts_dir: Path, interpreter: str = "", source_root=None) -> bytes:
    """The locator file contents for an install (bytes, for the manifest)."""
    return (
        json.dumps(
            locator_payload(scripts_dir, interpreter, source_root),
            ensure_ascii=False, indent=2, sort_keys=True,
        ) + "\n"
    ).encode("utf-8")


def read(home: Path) -> dict | None:
    """Read a recorded locator, or None when it is missing or unusable."""
    try:
        value = json.loads((Path(home) / LOCATOR_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    interpreter = value.get("interpreter")
    registry = value.get("registry")
    if not isinstance(interpreter, str) or not interpreter:
        return None
    if not isinstance(registry, str) or not registry:
        return None
    return value


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Record the shared Tianji runtime location")
    # No default: a host-specific default path in shared code is exactly the
    # boundary leak this module exists to avoid.
    parser.add_argument("--home", required=True)
    parser.add_argument("--scripts-dir", required=True)
    args = parser.parse_args()
    payload = locator_payload(Path(args.scripts_dir))
    if not payload["interpreter"]:
        print("FAIL: no Python interpreter could be detected", file=sys.stderr)
        return 1
    target = Path(args.home) / LOCATOR_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(record(Path(args.scripts_dir)))
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
