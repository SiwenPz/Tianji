"""Convert Command Code native subagent events into the shared ledger envelope.

This is the write boundary the architecture requires: host events are decoded
here and nowhere else. ``board.py`` and ``tianji-dash.py`` only ever read the
standard envelope.

Identity resolution order (never a "current task" or "that role" guess):
  1. explicit run/task/attempt carried by the taskbook (metadata or env);
  2. the host call id bound by the shared claim recorded for this dispatch;
  3. otherwise the event stays unbound and is display-only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ._shared import (
    EventKey, build_event, normalize_legacy_event, require_canonical_uuid, require_timestamp,
)


HOST = "cmdc"


class IdentityConflict(ValueError):
    """The payload asserts an identity the registry does not confirm.

    Raised instead of picking a winner: an event whose identity is disputed is
    not written as authoritative, it is reported.
    """


# The host events this decoder accepts. ``status_alert`` is deliberately absent:
# it was a business judgement (a worker looked stuck) written as if the adapter
# could see the shared ledger, and the adapter has no business deciding that.
NATIVE_EVENT_NAMES = frozenset({
    "run_start", "subagent_start", "subagent_progress", "subagent_stop",
    "tool_completed", "agent_receipt", "mod_error",
})

CORRELATION_FIELDS = ("toolCallId", "tool_call_id", "correlationId", "correlation_id")
AGENT_FIELDS = ("subagentType", "subagent_type", "agent", "agentType")
SESSION_FIELDS = ("sessionId", "session_id")
EVENT_ID_FIELDS = ("eventId", "event_id")
RUN_FIELDS = ("runId", "run_id")
TASK_FIELDS = ("taskId", "task_id")


def _first(raw: dict, fields, default=""):
    for name in fields:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return default


def _occurred_at(raw: dict, fallback: str | None) -> str:
    value = raw.get("occurred_at") or raw.get("ts") or raw.get("timestamp")
    if isinstance(value, str) and value.strip():
        return require_timestamp("occurred_at", value.strip(), require_timezone=True)
    return fallback or datetime.now(timezone.utc).isoformat()


def derive_event_id(raw: dict, *, event: str, session_id: str, correlation_id: str,
                    occurred_at: str, detail: dict) -> str:
    """Use a native ID when present; otherwise a stable content-derived digest.

    The canonical subject keys are shared protocol: the Command Code mod must
    derive the same id from the same event (see schemas/ledger-identity.fixture.json).
    Absent optional keys are omitted rather than serialized as null.
    """
    native = _first(raw, EVENT_ID_FIELDS)
    if native:
        return str(native)
    subject = {
        "host": HOST,
        "event": event,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "agent": str(_first(raw, AGENT_FIELDS, "unknown")),
        "occurred_at": occurred_at,
        "detail": detail,
    }
    sequence = raw.get("sequence") or raw.get("seq")
    if sequence is not None:
        subject["sequence"] = sequence
    import hashlib
    return "cmdc:" + hashlib.sha256(
        json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()[:32]


def event_key_claim(raw: dict) -> tuple[str, str, int] | None:
    """The EventKey a payload *claims*, if it names one at all.

    This is never identity on its own. A claim has to be confirmed by the
    registry -- either because the shared claim bound this call id to that
    task, or because the registry knows the run and task -- so a payload can
    never mint an identity by asserting one.
    """
    run_id = _first(raw, RUN_FIELDS)
    task_id = _first(raw, TASK_FIELDS)
    attempt = raw.get("attempt")
    if run_id and task_id and isinstance(attempt, int) and not isinstance(attempt, bool):
        run_id = require_canonical_uuid("run_id", run_id)
        return run_id, str(task_id), attempt
    return None


def _resolve_binding(registry, session: str, correlation_id: str, agent: str):
    """Resolve by the host call id alone.

    There is deliberately no role fallback: a dispatch's call id is what the
    claim established, and falling back to "the task last bound to this role"
    would attribute one worker's events to another whenever two dispatches of
    the same role overlap.
    """
    if not correlation_id:
        return None
    try:
        return registry.resolve_binding(
            host=HOST, session_id=session, correlation_id=correlation_id,
        )
    except Exception:
        return None


def to_ledger_event(raw: dict, registry=None, *, session_id: str | None = None,
                    recorded_at: str | None = None) -> dict:
    """Return a validated v2 envelope, or a display-only legacy record.

    ``registry`` is a RunRegistry used to confirm identity. Identity comes from
    the shared claim recorded for this call id; an EventKey carried in the
    payload is only a *claim*, and is confirmed against the registry before it
    may be used. When the two disagree the payload is wrong about something
    that matters, so this raises rather than quietly picking a winner: a
    conflict means the event must not be written as authoritative at all.

    When no identity can be established the event is unbound: it never
    participates in proof or authoritative acceptance.
    """
    if not isinstance(raw, dict):
        raise ValueError("native event must be an object")
    event = raw.get("event") or raw.get("hook_event_name")
    if not isinstance(event, str) or event not in NATIVE_EVENT_NAMES:
        raise ValueError(f"unsupported native event: {event!r}")

    session = session_id or str(_first(raw, SESSION_FIELDS, ""))
    correlation_id = str(_first(raw, CORRELATION_FIELDS, ""))
    agent = str(_first(raw, AGENT_FIELDS, "unknown"))
    occurred_at = _occurred_at(raw, None)
    detail = raw.get("detail")
    if not isinstance(detail, dict):
        detail = {}

    claimed = event_key_claim(raw)
    binding = (
        _resolve_binding(registry, session, correlation_id, agent)
        if registry is not None else None
    )
    bound = (binding.run_id, binding.task_id, binding.attempt) if binding else None

    if bound is not None and claimed is not None and claimed != bound:
        raise IdentityConflict(
            "payload EventKey disagrees with the identity the shared claim "
            f"recorded: claim={claimed}, registry={bound}"
        )

    identity = bound or claimed
    invocation_id = str(binding.invocation_id) if binding else ""
    if identity is not None and bound is None:
        # No claim bound this call id, so the payload's assertion has to be
        # confirmed by the registry before it can be trusted -- and it must
        # resolve to a real invocation, or it is not identity at all.
        try:
            registry.verify_event_key(EventKey(*identity))
            invocation_id = registry.task_invocation_id(EventKey(*identity))
        except Exception as exc:
            raise IdentityConflict(
                f"payload EventKey {claimed} is not known to the registry"
            ) from exc

    if identity is None or not invocation_id:
        return normalize_legacy_event(
            {"ts": occurred_at, "event": event, "agent": agent,
             "session_id": session, "correlation_id": correlation_id, "detail": detail},
            source_kind="unknown",
        )

    run_id, task_id, attempt = identity
    event_id = derive_event_id(
        raw, event=event, session_id=session, correlation_id=correlation_id,
        occurred_at=occurred_at, detail=detail,
    )
    return build_event(
        event_id=event_id,
        event=event,
        host=HOST,
        run_id=run_id,
        session_id=session,
        task_id=task_id,
        attempt=attempt,
        invocation_id=invocation_id,
        correlation_id=correlation_id or invocation_id,
        agent=agent,
        occurred_at=occurred_at,
        recorded_at=recorded_at or datetime.now(timezone.utc).isoformat(),
        detail=detail,
    )
