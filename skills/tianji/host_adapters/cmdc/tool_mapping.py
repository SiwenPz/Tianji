"""Translate shared semantic capabilities into Command Code tool IDs."""
from ._shared import CANONICAL_CAPABILITIES


CAPABILITY_TO_CMDC_TOOLS: dict[str, tuple[str, ...]] = {
    "file.read": ("read_file", "read_directory"),
    "file.patch": ("edit_file",),
    "file.write": ("write_file",),
    "shell.execute": ("shell_command",),
    "file.glob": ("glob",),
    "text.search": ("grep",),
    "web.search": ("web_search",),
    "web.fetch": ("web_fetch",),
}


def cmdc_tools(capabilities) -> tuple[str, ...]:
    """Return host tool IDs for the supplied capabilities; unknown ones fail loudly."""
    unknown = sorted(set(capabilities) - set(CAPABILITY_TO_CMDC_TOOLS))
    if unknown:
        raise ValueError(f"no Command Code tool mapping for capability: {unknown}")
    tools: list[str] = []
    for capability in capabilities:
        for tool in CAPABILITY_TO_CMDC_TOOLS[capability]:
            if tool not in tools:
                tools.append(tool)
    return tuple(tools)


assert set(CAPABILITY_TO_CMDC_TOOLS) == set(CANONICAL_CAPABILITIES)
