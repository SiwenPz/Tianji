"""Host-neutral Tianji role contract and renderers."""
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Protocol


PRIMARY_BINDING = "主模型"

# Host tool names found in the shared role packages map to one semantic
# vocabulary. Renderers translate capabilities into their own host tool IDs;
# the shared contract never names a host tool.
SOURCE_TO_CAPABILITY = {
    "Read": "file.read",
    "Edit": "file.patch",
    "Write": "file.write",
    "Bash": "shell.execute",
    "Glob": "file.glob",
    "Grep": "text.search",
    "WebSearch": "web.search",
    "FetchURL": "web.fetch",
}

CANONICAL_CAPABILITIES = frozenset(SOURCE_TO_CAPABILITY.values())

WRITE_CAPABILITIES = frozenset({"file.patch", "file.write", "shell.execute"})


# ---------------------------------------------------------------------------
# Role tiers -- the one model-free statement of what a role is for.
#
# Every role any host can discover belongs to exactly one tier, and this is the
# only place that mapping is written down. No model id appears here: which model
# fills a tier is host configuration data, chosen against that host's own menu
# and never baked into the shared contract. The parent session is the
# controller; it is not a tier here, because it is never rendered as a subagent
# and never installed.
#
# One role per tier, deliberately. A tier is a duty boundary and a place on the
# model menu -- not a job title. Splitting one tier into five names (reading vs
# researching vs reproducing vs probing) bought no capability, because the same
# model filled all of them; it bought ceremony, dispatch round-trips and a
# larger surface to keep consistent. The duty that a role performs is declared
# by its task book, not by inventing a new role name for it. Two duties stay
# separate because they are genuinely different stations, not sizes: the
# quality tier reviews work it did not do, and the referee decides disputes the
# quality tier cannot.
# ---------------------------------------------------------------------------

CONTROLLER = "controller"
ECONOMY = "economy"
QUALITY = "quality"
ESCALATION = "escalation"

ROLE_TIERS = {
    ECONOMY: (
        "tianji-worker",
    ),
    QUALITY: (
        "tianji-verifier",
    ),
    ESCALATION: (
        "tianji-referee",
    ),
}


def tier_for_role(role_name: str) -> str | None:
    """The single tier a role belongs to, or ``None`` when it has none."""
    for tier, roles in ROLE_TIERS.items():
        if role_name in roles:
            return tier
    return None


def roles_for_tier(tier: str) -> tuple[str, ...]:
    """Every role in a tier; an unknown tier is a loud error, not an empty set."""
    try:
        return ROLE_TIERS[tier]
    except KeyError:
        raise ValueError(f"unknown role tier: {tier!r}") from None


TIER_ORDER = (ECONOMY, QUALITY, ESCALATION)


def all_roles() -> tuple[str, ...]:
    """Every role in the contract, in tier order.

    The single definition of "the roles a host has to bind". Adapters import
    this instead of keeping their own copy: a second list is how a converged
    topology leaves a stale name behind in one host only.
    """
    return tuple(role for tier in TIER_ORDER for role in roles_for_tier(tier))


def validate_role_tiers(role_packages) -> None:
    """Fail loud unless the tier contract and the role packages agree exactly.

    A role that appears in two tiers, a role that appears in none, or a tier
    that names a role no package provides is a silent policy change, so none of
    the three is allowed to pass quietly.
    """
    discovered = [getattr(package, "name", package) for package in role_packages]

    assigned: dict[str, str] = {}
    for tier, roles in ROLE_TIERS.items():
        for role in roles:
            if role in assigned:
                raise ValueError(
                    f"role {role!r} belongs to two tiers: "
                    f"{assigned[role]!r} and {tier!r}"
                )
            assigned[role] = tier

    unassigned = sorted(set(discovered) - set(assigned))
    if unassigned:
        raise ValueError(f"roles with no tier: {unassigned}")

    absent = sorted(set(assigned) - set(discovered))
    if absent:
        raise ValueError(f"tiers name roles with no package: {absent}")


def canonical_capabilities(source_tools) -> tuple[str, ...]:
    """Normalize role package tool names into semantic capabilities."""
    capabilities: list[str] = []
    unknown: list[str] = []
    for tool in source_tools:
        capability = SOURCE_TO_CAPABILITY.get(tool)
        if capability is None:
            unknown.append(tool)
        elif capability not in capabilities:
            capabilities.append(capability)
    if unknown:
        raise ValueError(f"unknown role capability source tool: {unknown}")
    return tuple(capabilities)


@dataclass(frozen=True)
class RolePackage:
    path: Path
    name: str
    description: str
    when_to_use: str
    source_tools: tuple[str, ...]
    capabilities: tuple[str, ...]
    instructions: str
    frontmatter: str

    @property
    def markdown(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def can_write(self) -> bool:
        return bool(WRITE_CAPABILITIES.intersection(self.capabilities))


class RoleRenderer(Protocol):
    """Render one shared role package into a host's native agent format."""

    def __call__(self, package: RolePackage, binding: str | None = None) -> str: ...


def _parse_frontmatter(text: str) -> dict[str, object]:
    values: dict[str, object] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current is not None:
                current.append(stripped[2:].strip().strip("\"'"))
            continue
        current = None
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if not value:
            current = []
            values[key] = current
        elif value.startswith("[") and value.endswith("]"):
            values[key] = [
                item.strip().strip("\"'")
                for item in value[1:-1].split(",") if item.strip()
            ]
        else:
            values[key] = value.strip("\"'")
    return values


def load_role_package(path: Path) -> RolePackage:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"invalid agent package: {path}")
    frontmatter, instructions = parts[1], parts[2].strip()
    values = _parse_frontmatter(frontmatter)
    name = str(values.get("name") or "")
    description = str(values.get("description") or "")
    if name != path.stem:
        raise ValueError(f"role name mismatch: {path} declares {name!r}")
    if not description:
        raise ValueError(f"role description missing: {path}")
    if not instructions:
        raise ValueError(f"role instructions missing: {path}")
    source_tools = tuple(str(tool) for tool in values.get("tools") or ())
    return RolePackage(
        path=path,
        name=name,
        description=description,
        when_to_use=str(values.get("whenToUse") or ""),
        source_tools=source_tools,
        capabilities=canonical_capabilities(source_tools),
        instructions=instructions,
        frontmatter=frontmatter,
    )


def discover_role_packages(root: Path) -> list[RolePackage]:
    return [load_role_package(path) for path in sorted(root.glob("tianji-*.md"))]


def render_kimi(package: RolePackage) -> str:
    return package.markdown


def render_codex(package: RolePackage, binding: str | None = None) -> str:
    lines = [
        f"name = {json.dumps(package.name, ensure_ascii=False)}",
        f"description = {json.dumps(package.description, ensure_ascii=False)}",
    ]
    if package.name != "tianji-worker":
        lines.append('sandbox_mode = "read-only"')
    lines.append(
        f"developer_instructions = {json.dumps(package.instructions, ensure_ascii=False)}"
    )
    if binding == PRIMARY_BINDING:
        lines.append('# tianji-role-binding = "primary"')
    elif binding:
        lines.append('# tianji-role-binding = "fixed"')
        lines.append(f"model = {json.dumps(binding, ensure_ascii=False)}")
    else:
        lines.append('# tianji-role-binding = "unconfigured"')
    return "\n".join(lines) + "\n"
