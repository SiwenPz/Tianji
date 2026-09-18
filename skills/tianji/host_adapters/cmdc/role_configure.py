"""Bind shared roles to real Command Code models.

Bindings are install-time configuration data, never baked into the shared role
templates: this module rewrites the rendered host file and leaves the shared
package untouched.
"""
from __future__ import annotations

from pathlib import Path

from ._shared import (
    PRIMARY_BINDING,
    ROLE_TIERS,
    all_roles,
    discover_role_packages,
    roles_for_tier,
)
from .role_renderer import read_cmdc_binding, render_cmdc

from managed_files import record_written  # noqa: E402  (shared scripts dir)


# Derived from the shared contract, never retyped: the topology has one
# definition and both the detector and this configurator read it.
REQUIRED_BINDINGS = all_roles()


class RoleConfigError(ValueError):
    pass


def bindings(agents_dir: Path) -> dict[str, str]:
    """Confirmed bindings for every installed role."""
    result = {}
    for path in sorted(Path(agents_dir).glob("tianji-*.md")):
        binding = read_cmdc_binding(path)
        if binding:
            result[path.stem] = binding
    return result


def unconfigured(agents_dir: Path, roles=REQUIRED_BINDINGS) -> list[str]:
    resolved = bindings(agents_dir)
    return [role for role in roles if not resolved.get(role)]


def set_binding(agents_dir: Path, role: str, binding: str, *, models=None,
                source_root: Path | None = None) -> Path:
    """Render one role with a confirmed binding and write it back.

    ``models`` is the host menu; when supplied the binding must exist in it.
    ``binding`` may also be the shared "主模型" sentinel to inherit.
    """
    agents_dir = Path(agents_dir)
    target = agents_dir / f"{role}.md"
    if not target.is_file():
        raise RoleConfigError(f"role is not installed: {role}")
    if binding != PRIMARY_BINDING:
        if not binding or not binding.strip():
            raise RoleConfigError("binding must be a model id or the primary sentinel")
        if models is not None and binding not in set(models):
            raise RoleConfigError(f"model not in the current Command Code menu: {binding}")

    package = _load_package(role, source_root, target)
    # Write the exact bytes the installer would write: text-mode writes would
    # translate LF to CRLF on Windows and make a correct file look like drift.
    rendered = render_cmdc(package, binding=binding).encode("utf-8")
    target.write_bytes(rendered)
    # Leave the same trail the installer would: without this, the next install
    # reads this rebind as a user edit and keeps the now-stale file.
    record_written(_manifest_path(agents_dir), _agent_relative(role), rendered)
    return target


def _manifest_path(agents_dir: Path) -> Path:
    from .native_installer import ADAPTER_MANIFEST

    return Path(agents_dir).parent / ADAPTER_MANIFEST


def _agent_relative(role: str) -> str:
    from .native_installer import AGENT_DIR

    return f"{AGENT_DIR}/{role}.md"


def set_tier_binding(agents_dir: Path, tier: str, binding: str, *, models=None,
                     source_root: Path | None = None) -> list[Path]:
    """Bind every installed role in one tier to the same model.

    The user maps this host's menu onto economy / quality / escalation once; the
    roles in a tier come from the shared contract, not from a list kept here. An
    unknown tier, or a model the current menu does not offer, is refused rather
    than silently defaulted.
    """
    try:
        roles = roles_for_tier(tier)
    except ValueError as exc:
        raise RoleConfigError(str(exc)) from None
    written = []
    for role in roles:
        if not (Path(agents_dir) / f"{role}.md").is_file():
            continue
        written.append(set_binding(
            agents_dir, role, binding, models=models, source_root=source_root,
        ))
    if not written:
        raise RoleConfigError(f"no installed role belongs to tier {tier!r}")
    return written


def bindings_by_tier(agents_dir: Path) -> dict[str, dict[str, str]]:
    """Installed roles and their confirmed bindings, grouped by shared tier.

    An empty binding means the role is installed but has no chosen model, so a
    caller can tell "this tier is not mapped yet" from "this tier is mapped".
    """
    resolved = bindings(agents_dir)
    grouped: dict[str, dict[str, str]] = {}
    for tier in ROLE_TIERS:
        installed = {
            role: resolved.get(role, "")
            for role in roles_for_tier(tier)
            if (Path(agents_dir) / f"{role}.md").is_file()
        }
        if installed:
            grouped[tier] = installed
    return grouped


def unmapped_tiers(agents_dir: Path) -> list[str]:
    """Tiers with at least one installed but unbound role."""
    return [
        tier for tier, roles in bindings_by_tier(agents_dir).items()
        if any(not binding for binding in roles.values())
    ]


def _load_package(role: str, source_root: Path | None, target: Path):
    """Prefer the shared source package; fall back to the installed binding."""
    if source_root is not None:
        for package in discover_role_packages(Path(source_root) / "agents"):
            if package.name == role:
                return package
    raise RoleConfigError(
        f"cannot find the shared role package for {role}; pass --source-root"
    )


def describe(agents_dir: Path, models) -> str:
    """Human-readable plan: which tiers need which roles, and what is bound.

    The tiers come from the shared contract; this module only reports them
    against the Command Code menu and the bindings it has already recorded.
    """
    resolved = bindings(agents_dir)
    menu = list(models)
    lines = [f"可用模型 {len(menu)} 个（来自 Command Code 实时菜单）", ""]
    for tier, roles in ROLE_TIERS.items():
        installed = [role for role in roles if (Path(agents_dir) / f"{role}.md").is_file()]
        if not installed:
            continue
        lines.append(f"[{tier}]")
        for role in installed:
            current = resolved.get(role) or "(未绑定)"
            required = " *必需" if role in REQUIRED_BINDINGS else ""
            lines.append(f"  {role:<20} {current}{required}")
        lines.append("")
    pending = unconfigured(agents_dir)
    lines.append("待绑定核心角色: " + (", ".join(pending) if pending else "无"))
    return "\n".join(lines)
