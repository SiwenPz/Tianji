#!/usr/bin/env python3
"""The one strict reader for the Tianji ledger.

``state.jsonl`` is a mixed file by design: it accumulates records written by
different versions, different hosts, and occasionally a hand-edited line. The
reader's job is to *classify* every line, never to rewrite the file.

Rules, in order:

* a record that declares ``legacy`` is historical: display only, never
  identity;
* a record with **exactly** the current ``schema_version`` must pass the full
  envelope validation, or it is quarantined as ``invalid_v2``;
* a record with a *different* ``schema_version`` is quarantined as
  ``unknown_version`` -- a future version is never assumed compatible;
* a record with no ``schema_version`` at all is a pre-envelope record, and is
  historical when it has the shape one really had;
* anything else is quarantined with the reason that fits.

Quarantined records are excluded from running state, proof, acceptance,
compensation and deduplication. The reader has **no side effects**: it never
rotates, migrates, truncates or rewrites the ledger. Physical archival is a
separate, explicit maintenance operation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ledger_schema import SCHEMA_VERSION, validate_event


CANONICAL = "canonical"
LEGACY = "legacy"
QUARANTINED = "quarantined"
UNREADABLE = "unreadable"

# Why a line was quarantined. Stated as data so a caller can group and report it.
INVALID_V2 = "invalid_v2"
UNKNOWN_VERSION = "unknown_version"
UNRECOGNIZED = "unrecognized"
NOT_AN_OBJECT = "not_an_object"
PARTIAL_TAIL = "partial_tail"
UNREADABLE_JSON = "unreadable_json"

# Legacy records were written before the envelope existed and are only ever
# displayed. The shape check is deliberately loose: these are historical rows.
LEGACY_FIELDS = ("ts", "event", "agent")


@dataclass(frozen=True)
class QuarantinedRecord:
    line_number: int
    reason: str
    event: str = ""
    schema_version: object = None

    def describe(self) -> str:
        parts = [f"line {self.line_number}", self.reason]
        if self.event:
            parts.append(f"event={self.event}")
        if self.schema_version is not None:
            parts.append(f"schema_version={self.schema_version!r}")
        return ", ".join(parts)


@dataclass
class LedgerRead:
    """Every line of the ledger, classified. Nothing is dropped silently."""

    path: str = ""
    canonical: list[dict] = field(default_factory=list)
    legacy: list[dict] = field(default_factory=list)
    quarantined: list[QuarantinedRecord] = field(default_factory=list)

    @property
    def authoritative(self) -> list[dict]:
        """The records that may drive state, proof, acceptance or dedup."""
        return list(self.canonical)

    @property
    def diagnostic_count(self) -> int:
        return len(self.quarantined)

    @property
    def lines_seen(self) -> int:
        return len(self.canonical) + len(self.legacy) + len(self.quarantined)

    def summary(self) -> dict:
        by_reason: dict[str, int] = {}
        for record in self.quarantined:
            by_reason[record.reason] = by_reason.get(record.reason, 0) + 1
        return {
            "path": self.path,
            "canonical": len(self.canonical),
            "legacy": len(self.legacy),
            "quarantined": len(self.quarantined),
            "quarantine_reasons": by_reason,
        }


def _looks_legacy(record: dict) -> bool:
    return all(key in record for key in LEGACY_FIELDS)


def classify(record: object) -> tuple[str, str, dict | None]:
    """Classify one parsed record.

    Returns ``(kind, reason, record)``. ``reason`` is empty for canonical and
    legacy rows.
    """
    if not isinstance(record, dict):
        return UNREADABLE, NOT_AN_OBJECT, None

    # A record that says it is legacy is legacy, whatever else it carries: it
    # explicitly disclaims being an envelope, so it must not be read as one.
    if record.get("legacy") is True:
        if _looks_legacy(record):
            return LEGACY, "", record
        return QUARANTINED, UNRECOGNIZED, record

    if "schema_version" not in record:
        if _looks_legacy(record):
            return LEGACY, "", record
        return QUARANTINED, UNRECOGNIZED, record

    version = record.get("schema_version")
    if version != SCHEMA_VERSION or isinstance(version, bool):
        # Exact match only: a newer protocol is quarantined until this reader
        # learns it, never parsed with today's rules.
        return QUARANTINED, UNKNOWN_VERSION, record
    try:
        validate_event(record)
    except ValueError:
        return QUARANTINED, INVALID_V2, record
    return CANONICAL, "", record


def canonical_event(record: dict) -> dict | None:
    """Normalize an authoritative record for readers (board, proof)."""
    run_id = record.get("run_id")
    task_id = record.get("task_id")
    attempt = record.get("attempt")
    if not isinstance(run_id, str) or not run_id:
        return None
    if not isinstance(task_id, str) or not task_id:
        return None
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        return None
    return {
        "ts": record.get("occurred_at") or "",
        "event": record.get("event"),
        "agent": record.get("agent"),
        "session_id": record.get("session_id", ""),
        "host": record.get("host", ""),
        "event_key": (run_id, task_id, attempt),
        "event_id": record.get("event_id"),
        "invocation_id": record.get("invocation_id", ""),
        "correlation_id": record.get("correlation_id", ""),
        "detail": record.get("detail") or {},
        "legacy": False,
    }


def legacy_event(record: dict) -> dict:
    """Normalize a historical record for display only -- never identity."""
    return {
        "ts": record.get("ts", ""),
        "event": record.get("event"),
        "agent": record.get("agent"),
        "session_id": record.get("session_id", ""),
        "legacy": True,
    }


def read_lines(text: str, *, path: str = "") -> LedgerRead:
    """Classify an already-read ledger body (used by tests and callers)."""
    result = LedgerRead(path=path)
    lines = text.split("\n")
    # A trailing empty element is the final newline, not a record.
    if lines and lines[-1] == "":
        lines.pop()
    last_index = len(lines) - 1
    complete = text.endswith("\n") or not text

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            reason = UNREADABLE_JSON
            if index == last_index and not complete:
                # A writer appends a whole line under the lock, but a reader
                # that does not hold it can still catch a line mid-write.
                reason = PARTIAL_TAIL
            result.quarantined.append(QuarantinedRecord(index + 1, reason))
            continue
        kind, reason, parsed = classify(record)
        if kind == CANONICAL:
            result.canonical.append(parsed)
        elif kind == LEGACY:
            result.legacy.append(parsed)
        else:
            result.quarantined.append(QuarantinedRecord(
                index + 1, reason or UNRECOGNIZED,
                event=str(parsed.get("event", "")) if parsed else "",
                schema_version=parsed.get("schema_version") if parsed else None,
            ))
    return result


def read_ledger(path: str | Path) -> LedgerRead:
    """Read and classify a ledger. Read-only; never mutates the file."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LedgerRead(path=str(target))
    except OSError as exc:
        raise OSError(f"cannot read ledger {target}: {exc}") from exc
    return read_lines(text, path=str(target))


def dispatch_tokens(record: dict) -> int | None:
    """Tokens the host attributed to the dispatch a stop closes.

    Only a stop carries a total. Progress rows carry a running count as of each
    turn, so adding those up counts the same tokens once per turn -- the figure
    has to come from one row per dispatch, and that row is the stop. None means
    the host reported nothing, which is not the same as zero.
    """
    if record.get("event") != "subagent_stop":
        return None
    detail = record.get("detail")
    value = detail.get("tokensUsed") if isinstance(detail, dict) else None
    return value if isinstance(value, int) and value > 0 else None


def authoritative_events(path: str | Path) -> list[dict]:
    """Only the records that may drive state -- canonical, in file order."""
    read = read_ledger(path)
    return [event for event in (canonical_event(r) for r in read.canonical) if event]
