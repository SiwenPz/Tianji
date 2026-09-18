"""Install the Command Code adapter's native artifacts.

Owns only host files: rendered role markdown, the state mod, and the adapter
manifest. The shared skill tree is a separate transaction owned by the shared
installer, so adapter uninstall can never remove the shared core.
"""
from __future__ import annotations

import json
from pathlib import Path

from ._shared import discover_role_packages
from .role_renderer import read_cmdc_binding, render_cmdc
from .detector import STATE_FILE

from core_version import ADAPTER_VERSIONS  # noqa: E402
from managed_files import ManagedInstaller  # noqa: E402
import python_runtime  # noqa: E402


ADAPTER_MANIFEST = "tianji-cmdc.manifest.json"
MOD_DEST = Path("mods") / "tianji-state.ts"
AGENT_DIR = "agents"


def adapter_version() -> str:
    return ADAPTER_VERSIONS["cmdc"]


def agent_relative(package_name: str) -> str:
    return f"{AGENT_DIR}/{package_name}.md"


def build_role_entries(packages, agents_dir: Path) -> dict[str, bytes]:
    """Render every shared role, preserving an already-confirmed binding."""
    entries: dict[str, bytes] = {}
    for package in packages:
        existing = read_cmdc_binding(agents_dir / f"{package.name}.md")
        entries[agent_relative(package.name)] = render_cmdc(
            package, binding=existing,
        ).encode("utf-8")
    return entries


def install(cmdc_home: Path, source_root: Path, *, dry_run: bool = False,
            force: bool = False, shared_scripts: Path | None = None) -> dict:
    """Install roles and the state mod; return a report of what changed.

    ``shared_scripts`` is where the shared core's ``scripts/`` directory was
    installed. It is recorded for the mod so the claim bridge can reach the
    shared registry without guessing an interpreter or a path.
    """
    cmdc_home = Path(cmdc_home)
    packages = discover_role_packages(Path(source_root) / "agents")
    mod_source = Path(source_root) / "skills" / "tianji" / "host_adapters" / "cmdc" / "mod" / "tianji-state.ts"

    entries = build_role_entries(packages, cmdc_home / AGENT_DIR)
    if mod_source.is_file():
        entries[MOD_DEST.as_posix()] = mod_source.read_bytes()
    if shared_scripts is not None:
        # The role packages stay in this checkout, so the locator records where
        # that is: without it, a later rebind cannot re-render a role.
        entries[python_runtime.LOCATOR_NAME] = python_runtime.record(
            Path(shared_scripts), source_root=Path(source_root).resolve(),
        )

    installer = ManagedInstaller(
        cmdc_home, cmdc_home / ADAPTER_MANIFEST, version=adapter_version(),
    )
    plan = installer.plan(entries)
    if dry_run:
        return {
            "dry_run": True,
            "write": sorted(plan.write),
            "skip": sorted(plan.skip),
            "conflict": sorted(plan.conflict),
            "blocked": sorted(plan.blocked),
        }

    result = installer.commit(entries, plan, force=force)
    write_state(cmdc_home, packages)
    result.update({
        "dry_run": False,
        "skip": sorted(plan.skip),
        "roles": sorted(package.name for package in packages),
    })
    return result


def write_state(cmdc_home: Path, packages) -> None:
    """Record the managed artifact set that the detector reads back."""
    state_path = Path(cmdc_home) / STATE_FILE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state["host"] = "cmdc"
    state["adapter_version"] = adapter_version()
    state["roles"] = sorted(package.name for package in packages)
    state.setdefault("proof", None)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def uninstall(cmdc_home: Path) -> dict:
    """Remove only adapter-managed files; the shared core is untouched."""
    cmdc_home = Path(cmdc_home)
    installer = ManagedInstaller(
        cmdc_home, cmdc_home / ADAPTER_MANIFEST, version=adapter_version(),
    )
    removed = installer.remove(installer.managed_paths())
    state_path = cmdc_home / STATE_FILE
    if state_path.exists():
        state_path.unlink()
    return {"removed": removed, "state": str(state_path)}
