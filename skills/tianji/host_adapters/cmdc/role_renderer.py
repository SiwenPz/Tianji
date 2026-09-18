"""Render shared role packages into Command Code agent markdown.

Only Command Code specifics live here: frontmatter fields, tool IDs, the
permission field and the model binding comment format.
"""
import json
import re
from pathlib import Path

from ._shared import PRIMARY_BINDING, RolePackage
from .tool_mapping import cmdc_tools


PERMISSION_AUTO_ACCEPT = "auto-accept"

# Per-role turn budget, rendered into the agent file. This is the limit the
# host actually enforces on a dispatch, so a task book that cannot finish
# inside it dies with no verdict -- it must be sized to the role, not to hope.
CMDC_MAX_TURNS = {
    "tianji-referee": 15,
    "tianji-verifier": 20,
    "tianji-worker": 40,
}
CMDC_DEFAULT_MAX_TURNS = 30

_MAX_TURNS_LINE = re.compile(r"(?m)^maxTurns:\s*(\d+)\s*$")


def installed_max_turns(path) -> int | None:
    """The turn budget the host will enforce on this role, as installed.

    Read back from the rendered file rather than from CMDC_MAX_TURNS: the file
    is what the host obeys, so a hand-edited budget is the one that will really
    cut a dispatch short.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    found = _MAX_TURNS_LINE.search(text)
    return int(found.group(1)) if found else None


BINDING_PRIMARY = '# tianji-role-binding = "primary"'
BINDING_FIXED = '# tianji-role-binding = "fixed"'
BINDING_UNCONFIGURED = '# tianji-role-binding = "unconfigured"'

_MODEL_LINE = re.compile(r"(?m)^model:\s*(.+)$")


def render_cmdc(package: RolePackage, binding: str | None = None) -> str:
    """Render deterministically: identical inputs must always produce identical bytes."""
    description = " ".join(
        part for part in (package.description, package.when_to_use) if part
    ).strip() or package.name
    lines = [
        "---",
        f"name: {package.name}",
        f"description: {json.dumps(description, ensure_ascii=False)}",
        f"tools: {', '.join(cmdc_tools(package.capabilities))}",
    ]
    if binding == PRIMARY_BINDING:
        lines.append(BINDING_PRIMARY)
    elif binding:
        lines.append(f"model: {binding}")
        lines.append(BINDING_FIXED)
    else:
        lines.append(BINDING_UNCONFIGURED)
    if package.can_write:
        lines.append(f"permissionMode: {PERMISSION_AUTO_ACCEPT}")
    lines.append(f"maxTurns: {CMDC_MAX_TURNS.get(package.name, CMDC_DEFAULT_MAX_TURNS)}")
    lines.append("showOutput: true")
    lines.extend(["---", "", package.instructions])
    return "\n".join(lines) + "\n"


def read_cmdc_binding(path: Path) -> str | None:
    """Return the binding confirmed by a previous install, or None if unconfigured."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if BINDING_PRIMARY in text:
        return PRIMARY_BINDING
    if BINDING_FIXED in text:
        match = _MODEL_LINE.search(text)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def declared_model(path: Path) -> tuple[str, str]:
    """Return (model, source) for a rendered role file; source is declared/inherit/none."""
    if not path.exists():
        return "", "none"
    text = path.read_text(encoding="utf-8")
    match = _MODEL_LINE.search(text)
    if match and match.group(1).strip():
        return match.group(1).strip(), "declared"
    if BINDING_PRIMARY in text:
        return "主模型(inherit)", "inherit"
    return "", "none"
