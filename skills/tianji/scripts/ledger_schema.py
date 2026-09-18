#!/usr/bin/env python3
"""Shared Tianji ledger envelope and identity contract."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 2

# A writer that does not know the invocation writes this sentinel. It means "I
# could not identify this", which is exactly what an authoritative record must
# never claim, so it is refused here and has to be recorded as a diagnostic
# instead.
UNKNOWN_IDENTIFIER = "unknown"

REQUIRED_FIELDS = frozenset({
    "schema_version",
    "event_id",
    "event",
    "host",
    "run_id",
    "session_id",
    "task_id",
    "attempt",
    "invocation_id",
    "correlation_id",
    "agent",
    "occurred_at",
    "recorded_at",
    "detail",
})


@dataclass(frozen=True)
class EventKey:
    run_id: str
    task_id: str
    attempt: int

    def __post_init__(self) -> None:
        require_canonical_uuid("run_id", self.run_id)
        _require_text("task_id", self.task_id)
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
        }


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def require_canonical_uuid(name: str, value: Any) -> str:
    """Return the canonical UUID form, or fail loudly for opaque identity."""
    text = _require_text(name, value)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc
    canonical = str(parsed)
    if text.lower() != canonical:
        raise ValueError(f"{name} must use canonical UUID form")
    return canonical


def _require_timestamp(name: str, value: Any, *, require_timezone: bool = False) -> str:
    text = _require_text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if require_timezone and (parsed.tzinfo is None or parsed.utcoffset() is None):
        raise ValueError(f"{name} must include a timezone offset")
    return text


def require_timestamp(name: str, value: Any, *, require_timezone: bool = False) -> str:
    """Public alias for producers that normalize host timestamps."""
    return _require_timestamp(name, value, require_timezone=require_timezone)


def is_exact_schema_version(record: Any) -> bool:
    """Reader contract: only the current protocol version is authoritative.

    A newer version is not assumed compatible — it is quarantined until this
    reader learns it, rather than being parsed with today's rules.
    """
    return isinstance(record, dict) and record.get("schema_version") == SCHEMA_VERSION


def validate_event(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("ledger event must be an object")
    fields = set(event)
    missing = REQUIRED_FIELDS - fields
    unknown = fields - REQUIRED_FIELDS
    if missing or unknown:
        raise ValueError(
            f"ledger fields mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    if event["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    for name in (
        "event_id", "event", "host", "task_id",
        "invocation_id", "correlation_id", "agent",
    ):
        _require_text(name, event[name])
    if event["invocation_id"] == UNKNOWN_IDENTIFIER:
        raise ValueError(
            "invocation_id must identify a real invocation; "
            f"{UNKNOWN_IDENTIFIER!r} belongs in a diagnostic, not the ledger"
        )
    # session_id is host session context: a terminal acceptance recorded outside
    # any host session legitimately has none, and inventing one would be a lie.
    if not isinstance(event["session_id"], str):
        raise ValueError("session_id must be a string")
    if isinstance(event["attempt"], bool) or not isinstance(event["attempt"], int):
        raise ValueError("attempt must be a positive integer")
    EventKey(event["run_id"], event["task_id"], event["attempt"])
    _require_timestamp("occurred_at", event["occurred_at"], require_timezone=True)
    _require_timestamp("recorded_at", event["recorded_at"], require_timezone=True)
    if not isinstance(event["detail"], dict):
        raise ValueError("detail must be an object")
    return dict(event)


def build_event(
    *,
    event_id: str,
    event: str,
    host: str,
    run_id: str,
    session_id: str,
    task_id: str,
    attempt: int,
    invocation_id: str,
    correlation_id: str,
    agent: str,
    occurred_at: str,
    recorded_at: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    return validate_event({
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event": event,
        "host": host,
        "run_id": run_id,
        "session_id": session_id,
        "task_id": task_id,
        "attempt": attempt,
        "invocation_id": invocation_id,
        "correlation_id": correlation_id,
        "agent": agent,
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "detail": detail,
    })


def event_key_from(event: dict[str, Any]) -> EventKey | None:
    if event.get("legacy") is True or event.get("schema_version") != SCHEMA_VERSION:
        return None
    validated = validate_event(event)
    return EventKey(
        validated["run_id"], validated["task_id"], validated["attempt"],
    )


def normalize_legacy_event(
    raw: dict[str, Any], *, source_kind: str = "unknown",
) -> dict[str, Any]:
    """Return a display-only legacy record; never manufacture trusted identity."""
    if not isinstance(raw, dict):
        raise ValueError("legacy event must be an object")
    event = _require_text("event", raw.get("event"))
    agent = _require_text("agent", raw.get("agent"))
    occurred_at = _require_timestamp("ts", raw.get("ts"))
    if source_kind not in {"kimi_hook", "codex_hook", "unknown"}:
        raise ValueError("unsupported legacy source kind")
    explicit_host = raw.get("host")
    host = explicit_host if isinstance(explicit_host, str) and explicit_host else "legacy_unknown"
    detail = raw.get("detail", {})
    if not isinstance(detail, dict):
        detail = {"legacy_detail": detail}
    return {
        "schema_version": 1,
        "legacy": True,
        "legacy_source_kind": source_kind,
        "event_id": None,
        "event": event,
        "host": host,
        "run_id": None,
        "session_id": str(raw.get("session_id") or ""),
        "task_id": None,
        "attempt": None,
        "invocation_id": None,
        "correlation_id": str(raw.get("correlation_id") or raw.get("toolCallId") or ""),
        "agent": agent,
        "occurred_at": occurred_at,
        "recorded_at": occurred_at,
        "detail": detail,
    }
