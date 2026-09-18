"""Collect real host-dispatch evidence for the shared proof model.

Only what Command Code actually emits is used: a paired subagent start/stop for
a role the current configuration binds. That proves the host dispatched the
role — it does not prove which upstream model answered, so wire evidence stays
false.

No role is privileged as "the probe". Dispatching a role is the evidence; which
role ran says what that role's binding is, not how strong the proof is. The
controller therefore earns a proof from whatever it actually dispatched, and no
dedicated probe role has to exist for the host path to be provable.

Evidence must also be *fresh*: a dispatch that predates the current adapter
install cannot vouch for the current adapter, so it is not accepted.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ledger_schema import EventKey
from route_proof import Dispatch, Integrity, RouteProof, now_iso
from run_registry import RunRegistry

import ledger_reader  # noqa: E402  (shared reader, flat scripts import)
import observation_journal  # noqa: E402
import role_contract  # noqa: E402

from .role_renderer import declared_model


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fresh_after(cmdc_home: Path) -> datetime | None:
    """The moment the adapter's code last really changed.

    Not the manifest file's own mtime: rebinding a role rewrites that file
    (``role_configure.set_binding`` -> ``managed_files.record_written``) while
    changing no adapter code at all, so after any rebind the baseline jumps past
    every dispatch already in the ledger and the gate refuses evidence for a
    configuration that is still current. Measured live 2026-09-17: the verifier
    was rebound at 08:34:13Z, the newest verifier dispatch was 08:03:04Z, and
    every probe reported "早于当前适配层安装".

    What an install places and nothing else rewrites -- the manifest's entries
    outside the role directory (the state mod and the runtime locator) -- is the
    adapter layer. Role cards are configuration, and a dispatch only earns
    host_dispatch when the snapshot recorded at dispatch time still matches the
    current one, so the bindings stay guarded without this baseline moving on a
    rebind. A manifest with no such entry keeps the old reading, so the gate is
    never wider than it was before.
    """
    from .native_installer import ADAPTER_MANIFEST, AGENT_DIR

    home = Path(cmdc_home)
    manifest_path = home / ADAPTER_MANIFEST
    try:
        entries = json.loads(
            manifest_path.read_text(encoding="utf-8"),
        ).get("entries") or {}
    except (OSError, ValueError, AttributeError):
        entries = {}

    newest: datetime | None = None
    for relative in entries:
        if str(relative).split("/", 1)[0] == AGENT_DIR:
            continue  # a role card is rebound by configuration, not by install
        try:
            moment = datetime.fromtimestamp(
                (home / relative).stat().st_mtime, tz=timezone.utc,
            )
        except OSError:
            continue
        if newest is None or moment > newest:
            newest = moment
    if newest is not None:
        return newest

    try:
        return datetime.fromtimestamp(manifest_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def dispatch_pairs(events, *, role: str, host: str = "", session_id: str = ""):
    """Every completed start/stop cycle for the role, oldest first.

    A pair only counts when both events agree about who ran: same role, same
    host, same session, same EventKey, same invocation and the same call id.
    A stop that merely happens to follow a start is not evidence that the start
    finished -- pairing them would let an unrelated stop vouch for a dispatch.

    A task may be dispatched more than once (a retry, or a re-probe after the
    adapter changed), so all cycles are considered -- not just the first.
    """
    grouped: dict[tuple, list] = {}
    for event in events:
        key = event.get("event_key")
        if not key:
            continue
        grouped.setdefault(key, []).append(event)

    pairs: list[tuple[dict, dict, tuple]] = []
    for key in sorted(grouped, key=lambda item: [str(part) for part in item]):
        group = sorted(grouped[key], key=lambda item: item["ts"])
        for index, start in enumerate(group):
            if start["event"] != "subagent_start" or start["agent"] != role:
                continue
            if host and start.get("host") != host:
                continue
            if session_id and start.get("session_id") != session_id:
                continue
            correlation = start.get("correlation_id")
            if not correlation:
                continue  # without a call id there is nothing to pair on
            invocation = start.get("invocation_id")
            if not invocation:
                continue
            stop = next(
                (item for item in group[index + 1:]
                 if item["event"] == "subagent_stop"
                 and item.get("correlation_id") == correlation
                 and item.get("invocation_id") == invocation
                 and item.get("agent") == role
                 and (not host or item.get("host") == host)),
                None,
            )
            if stop is not None:
                pairs.append((start, stop, key))
    pairs.sort(key=lambda pair: pair[0]["ts"])
    return pairs


def _recorded_subject_digest(registry, key, invocation_id: str) -> str:
    """The snapshot the dispatcher recorded for this invocation, or "".

    Read from the registry rather than recomputed: the whole point is that the
    value was fixed before the worker ran.
    """
    if registry is None or not invocation_id:
        return ""
    try:
        return registry.get_invocation(EventKey(*key), invocation_id).subject_digest
    except Exception:
        return ""


def find_dispatch(events, *, models: dict[str, str], host: str = "cmdc",
                  not_before: datetime | None = None,
                  registry=None, session_id: str = ""):
    """Pair the newest completed dispatch of any candidate role.

    ``models`` is the candidate set: role name to its declared binding. The
    newest completed cycle wins, so a proof reflects the most recent real
    dispatch rather than the first one that happens to be in the ledger.
    """
    newest: tuple | None = None
    dispatched_without_stop: list[str] = []

    for role in sorted(models):
        pairs = dispatch_pairs(events, role=role, host=host, session_id=session_id)
        if not pairs:
            started = [event for event in events
                       if event["event"] == "subagent_start" and event["agent"] == role]
            if started:
                dispatched_without_stop.append(
                    f"{role} 已派发但未见成对 stop "
                    f"(correlation={started[-1].get('correlation_id')})"
                )
            continue
        start, stop, key = pairs[-1]
        observed = _parse_ts(stop["ts"]) or datetime.min.replace(tzinfo=timezone.utc)
        if newest is None or observed > newest[0]:
            newest = (observed, start, stop, key, role)

    if newest is None:
        if dispatched_without_stop:
            return None, "；".join(dispatched_without_stop)
        return None, "未找到任何已绑定角色的派发记录"

    observed, start, stop, key, role = newest
    if not_before is not None and observed <= not_before:
        return None, f"{role} 的派发证据早于当前适配层安装，请重新派发一次再取证"
    recorded = _recorded_subject_digest(registry, key, start.get("invocation_id", ""))
    if not recorded:
        return None, f"{role} 的派发未记录派工时的配置快照，无法证明当前配置"
    return Dispatch(
        verified=True,
        host=host,
        role=role,
        requested_model=models[role],
        declared_model=models[role],
        correlation_id=start.get("correlation_id", ""),
        start_event_id=start.get("event_id") or "",
        stop_event_id=stop.get("event_id") or "",
        event_key={"run_id": key[0], "task_id": key[1], "attempt": key[2]},
        captured_at=now_iso(),
        subject_digest=recorded,
    ), f"{role} 派发已配对 (correlation={start.get('correlation_id')})"


def collect(detector, ledger_path: Path, *, roles=None,
            registry=None, session_id: str = "",
            not_before: datetime | None = None) -> tuple[RouteProof | None, str]:
    """Build a proof from evidence recorded at dispatch time.

    The dispatch's ``subject_digest`` comes from the registry -- the value the
    dispatcher fixed before the worker ran -- so a configuration that drifted
    afterwards simply fails to match, and the proof degrades to integrity
    instead of being retro-approved by a snapshot taken now.

    ``roles`` narrows which dispatches count; the default is every role in the
    contract, because any bound role proves the same thing: the host dispatched
    a Tianji role while the recorded bindings were the current ones.
    """
    path = Path(ledger_path)
    if not path.is_file():
        return None, f"账本不存在: {path}"

    if registry is None:
        # The registry lives beside the ledger it describes.
        registry = RunRegistry(path.parent.parent)

    models: dict[str, str] = {}
    unbound: list[str] = []
    for role in (tuple(roles) if roles else role_contract.all_roles()):
        model, source = declared_model(detector.agents_dir() / f"{role}.md")
        if source == "declared" and model:
            models[role] = model
        else:
            unbound.append(f"{role} 未固定绑定模型 (source={source})")
    if not models:
        return None, "先完成角色配置：" + "；".join(unbound)

    try:
        read = ledger_reader.read_ledger(path)
    except OSError as exc:
        return None, f"账本读取失败: {exc}"

    events = [
        event for event in (ledger_reader.canonical_event(r) for r in read.canonical)
        if event
    ]
    dispatch, reason = find_dispatch(
        events, models=models, not_before=not_before,
        registry=registry, session_id=session_id,
    )
    if dispatch is None:
        if read.diagnostic_count:
            reason = f"{reason}（另有 {read.diagnostic_count} 行非权威记录未采信）"
        return None, reason

    snapshot = detector.current_snapshot()
    return RouteProof(
        integrity=Integrity(snapshot=snapshot, captured_at=now_iso()),
        dispatch=dispatch,
        wire=None,
    ), reason


def record(detector, proof: RouteProof, *, workspace=None) -> None:
    """Persist the proof into the adapter state file the detector reads back.

    The observation this proof was established against is remembered too, so
    that when it later stops matching the report can name what changed rather
    than only noting that something did.

    That journal is per-workspace state, so with no workspace there is nowhere to
    record it. Falling back to the process's cwd is how a test run overwrote the
    live machine's observation, after which the check reported a fixture's values
    as this machine's history: "模型菜单: 0 项 → 70 项" and a role binding that
    had been replaced hours earlier (measured 2026-09-17).
    """
    state = dict(detector.state())
    state["proof"] = proof.as_dict()
    detector.state_path().write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if workspace:
        observation_journal.remember(
            workspace, **observation_journal.snapshot_subjects(detector),
        )


def explain_drift(detector, workspace=None) -> list[str]:
    """Name what changed since this proof was established, if anything did."""
    previous = observation_journal.recall(workspace or Path.cwd())
    if not previous:
        return []
    return observation_journal.explain_drift(
        previous, **observation_journal.snapshot_subjects(detector),
    )
