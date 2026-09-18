"""Command Code host facts for the shared Tianji conclusion machine.

This module supplies observations only. Conclusion policy, task state,
acceptance and compensation rules stay in the shared core.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .role_renderer import installed_max_turns, read_cmdc_binding

from observation import Observation  # noqa: E402  (scripts dir is on sys.path)
from role_contract import all_roles  # noqa: E402
from route_proof import (  # noqa: E402  (scripts dir is on sys.path via ._shared)
    PROOF_LEVEL_DISPATCH,
    PROOF_LEVEL_INTEGRITY,
    PROOF_LEVEL_NONE,
    PROOF_LEVEL_WIRE,
    RouteProof,
    digest,
    integrity_snapshot,
)


# Every role in the shared contract must carry a binding. Derived, not copied:
# a local list is how one host keeps a name the contract no longer has.
REQUIRED_BINDINGS = all_roles()

STATE_FILE = "tianji-cmdc.json"
MOD_RELATIVE = Path("mods") / "tianji-state.ts"

_SECTION_HEADERS = {
    "Available", "Open", "Anthropic", "OpenAI", "Google", "Sakana", "Meta",
    "xAI", "Pass", "Docs",
}
# Model IDs may carry a tag suffix such as ":free".
_MODEL_ID = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._/:-]*)\s")
_MENU_FOOTER = "Pass the full id"


def _cmdc_exec() -> str | None:
    for name in ("cmdc.cmd", "cmdc"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def _drift_explanation(detector) -> list[str]:
    """Name what changed since the last proven observation, best effort.

    Advisory display data only: it explains a verdict, it never produces one,
    and a missing or unreadable journal simply means no explanation.
    """
    try:
        import observation_journal

        previous = observation_journal.recall(os.getcwd())
        if not previous:
            return []
        return observation_journal.explain_drift(
            previous, **observation_journal.snapshot_subjects(detector),
        )
    except Exception:
        return []


def _run_cmdc(args: list[str], timeout: float = 20.0) -> str | None:
    """Run the Command Code CLI; return stdout or None when unavailable."""
    exe = _cmdc_exec()
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout or ""


def parse_version(output: str | None) -> str:
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "unknown"


def parse_models(output: str | None) -> list[str]:
    """Parse ``cmdc --list-models`` into model IDs, ignoring headers and help text."""
    text = output or ""
    cut = text.find(_MENU_FOOTER)
    if cut != -1:
        text = text[:cut]
    models: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Available"):
            continue
        if stripped.split()[0] in _SECTION_HEADERS:
            continue
        match = _MODEL_ID.match(stripped)
        if match and match.group(1) not in models:
            models.append(match.group(1))
    return models


class CmdcDetector:
    """Observe Command Code's install layout, menu and evidence state."""

    def __init__(self, root=None, run=None):
        home = os.environ.get("CMDC_HOME", os.path.expanduser("~/.commandcode"))
        self._root = Path(root) if root is not None else Path(home)
        self._run = run if run is not None else _run_cmdc
        self._cache: dict[str, object] = {}

    # ---- paths -----------------------------------------------------------
    def agents_dir(self) -> Path:
        return self._root / "agents"

    def mod_path(self) -> Path:
        return self._root / MOD_RELATIVE

    def state_path(self) -> Path:
        return self._root / STATE_FILE

    # ---- shared HostDetector interface -----------------------------------
    def host_label(self) -> str:
        return "Command Code"

    def config_path(self) -> str:
        return str(self.state_path())

    def status_line_ok(self):
        return True, "Command Code 状态栏由状态 mod 提供，无需宿主配置文件"

    def requires_model_menu(self) -> bool:
        return True

    def supports_menu_reconciliation(self) -> bool:
        return False

    def state_file_exists(self) -> bool:
        return os.path.isfile(os.path.join(os.getcwd(), ".tianji", "state.jsonl"))

    def hooks_ok(self):
        path = self.mod_path()
        if path.is_file():
            return True, f"状态 mod 已安装 ({path})"
        return False, f"缺少状态 mod ({path})"

    # ---- Command Code facts ---------------------------------------------
    def native_version(self) -> str:
        if "version" not in self._cache:
            self._cache["version"] = parse_version(self._run(["--version"]))
        return str(self._cache["version"])

    def model_ids(self) -> list[str]:
        if "models" not in self._cache:
            output = self._run(["--list-models"])
            # A menu we could not read is not an empty menu; keep the two apart
            # so a transient CLI failure is never reported as drift.
            self._cache["menu_unavailable"] = (
                "cmdc --list-models 无法执行" if output is None else ""
            )
            self._cache["models"] = parse_models(output)
        return list(self._cache["models"])

    def menu_observation(self):
        """Tri-state reading of the host menu: available / empty / unavailable."""
        models = self.model_ids()
        reason = self._cache.get("menu_unavailable", "")
        if reason:
            return Observation.unavailable(reason)
        if not models:
            return Observation.confirmed_empty()
        return Observation.available(list(models))

    def menu_models(self):
        return [(model_id, "cmdc", "") for model_id in self.model_ids()]

    def state(self) -> dict:
        try:
            data = json.loads(self.state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def recorded_roles(self) -> list[str]:
        """Managed role names recorded by the installer (list or legacy mapping)."""
        roles = self.state().get("roles")
        if isinstance(roles, dict):
            return sorted(str(name) for name in roles)
        if isinstance(roles, list):
            return sorted(str(name) for name in roles)
        return []

    def installed_roles(self) -> list[str]:
        return sorted(path.stem for path in self.agents_dir().glob("tianji-*.md"))

    def role_bindings(self) -> dict[str, str]:
        bindings: dict[str, str] = {}
        for role in self.installed_roles():
            binding = read_cmdc_binding(self.agents_dir() / f"{role}.md")
            if binding:
                bindings[role] = binding
        return bindings

    def subagent_limits(self) -> dict[str, int]:
        """Turns the host allows each installed role -- the budget a task book
        has to fit inside.

        A dispatch that cannot finish inside its role's budget returns nothing
        at all rather than a partial result, so a dispatcher that does not know
        the number writes work that cannot come back.
        """
        limits: dict[str, int] = {}
        for role in self.installed_roles():
            turns = installed_max_turns(self.agents_dir() / f"{role}.md")
            if turns is not None:
                limits[role] = turns
        return limits

    def role_files_digest(self) -> dict[str, str]:
        digests: dict[str, str] = {}
        for role in self.installed_roles():
            path = self.agents_dir() / f"{role}.md"
            digests[role] = digest(path.read_text(encoding="utf-8"))
        return digests

    def adapter_digest(self) -> str:
        subject = {
            "mod": digest(self.mod_path().read_text(encoding="utf-8"))
            if self.mod_path().is_file() else None,
            "roles": self.role_files_digest(),
        }
        return digest(subject)

    def current_snapshot(self) -> dict[str, str]:
        return integrity_snapshot(
            role_bindings=self.role_bindings(),
            menu=self.model_ids(),
            adapter_digest=self.adapter_digest(),
            host_runtime_version=self.native_version(),
        )

    def proof(self) -> RouteProof:
        return RouteProof.from_dict(self.state().get("proof"))

    def proof_level(self) -> str:
        return self.proof().effective_level(self.current_snapshot())

    # ---- conclusion-machine inputs --------------------------------------
    def roles_ok(self):
        recorded = self.recorded_roles()
        if not recorded:
            return False, "尚无受管角色记录，先运行安装"
        missing = [
            role for role in recorded
            if not (self.agents_dir() / f"{role}.md").is_file()
        ]
        if missing:
            return False, "缺少受管角色文件: " + ", ".join(sorted(missing))
        return True, f"受管角色已就位 ({len(recorded)} 个)"

    def role_bindings_ok(self):
        bindings = self.role_bindings()
        unconfigured = [role for role in REQUIRED_BINDINGS if not bindings.get(role)]
        if unconfigured:
            return False, "未确认核心角色模型: " + ", ".join(unconfigured)
        return True, f"{len(REQUIRED_BINDINGS)} 个核心角色模型绑定已确认"

    def routing_supported(self):
        """Static capability: can this adapter route at all?

        Deliberately independent of the model menu. A menu we could not read is
        an observation failure (CHECK_UNAVAILABLE) and an empty menu is the
        model-source verdict; folding either one into "this adapter cannot
        route" would mask both behind NEED_HOST_ADAPTER.
        """
        if not self.installed_roles():
            return False, "尚未渲染任何天机角色"
        if not self.mod_path().is_file():
            return False, "状态 mod 未安装"
        return True, f"Command Code {self.native_version()} 可路由"

    def verdict_for_level(self, level: str):
        """The one verdict for a route-proof level, shared by every reader.

        The probe, ``install.py status`` and the conclusion machine must all
        judge "有效路由证明" the same way: only a host_dispatch (or wire) level
        proves that a role was really dispatched under the current configuration.
        A bare integrity level means the configuration is intact but no dispatch
        vouches for it yet -- not proof.
        """
        if level in (PROOF_LEVEL_DISPATCH, PROOF_LEVEL_WIRE):
            return True, f"路由证明有效 (proof_level={level})"
        if level == PROOF_LEVEL_INTEGRITY:
            return False, "配置完整但尚无派发证据，需派一次角色(只读自检)后取证"
        # Say what changed when we can; "something drifted" is far less useful
        # than "this role's binding changed".
        changed = _drift_explanation(self)
        if changed:
            return False, "无有效路由证明，配置已变: " + "；".join(changed)
        return False, "无有效路由证明 (配置漂移或尚无派发记录)"

    def default_ledger_path(self):
        """The workspace ledger to read when no path is given.

        ``install.py status`` already resolves the workspace as the current
        directory (its ``.tianji/state.jsonl``), and the probe takes a
        ``--workspace``; this is the same convention, kept here so a caller that
        has no opinion cannot silently read a different ledger.
        """
        return Path.cwd() / ".tianji" / "state.jsonl"

    def route_proof_from_ledger(self, ledger_path=None, *, session_id: str = ""):
        """Derive the route proof the way the probe does: live, from the ledger.

        The probe (``cmdc-routing-probe.py``) reports the level of the proof it
        collects from the workspace ledger -- gated by the adapter-install
        freshness baseline -- rather than the level of whatever proof happens to
        be persisted. If a reader takes the persisted artifact instead, it can
        answer "MISSING" about a dispatch the probe just reported, or the reverse,
        although the dispatch and the configuration are one and the same fact.
        Reading the same live evidence through this same gate keeps the
        instruments in agreement.
        """
        from .proof_collector import collect, fresh_after  # local: avoids a cycle

        proof, _reason = collect(
            self, Path(ledger_path or self.default_ledger_path()), session_id=session_id,
            not_before=fresh_after(self._root),
        )
        if proof is None:
            return PROOF_LEVEL_NONE
        return proof.effective_level(self.current_snapshot())

    def probe_level(self, ledger_path=None):
        """The route-proof level every reader judges -- the one fact, one level.

        Live evidence first, from the ledger through the same gate the probe uses;
        the persisted proof otherwise, because it is the same evidence recorded
        earlier and is re-judged against the same current snapshot (so a drifted
        configuration invalidates it too). Only a live reading that *proves* wins:
        an integrity reading carries less than a recorded host_dispatch, and the
        whole point of one reader is that it cannot answer two ways.

        `install.py status` shows this level, `probe_ok()` verdicts it, and the
        conclusion machine (`doctor.collect_facts`) reads that verdict -- so a
        reader that derived its own level could disagree about one dispatch.
        """
        live = self.route_proof_from_ledger(ledger_path)
        if live in (PROOF_LEVEL_DISPATCH, PROOF_LEVEL_WIRE):
            return live
        return self.proof_level()

    def probe_ok(self, ledger_path=None):
        """Dynamic evidence: does a proof hold under the current configuration?

        Only a host_dispatch (or wire) level proves that the configuration routes.
        """
        return self.verdict_for_level(self.probe_level(ledger_path))
