#!/usr/bin/env python3
"""Shared Tianji ledger sink.

One ledger protocol, not one ledger program: any language may implement a
sink as long as it appends the same validated envelope. This module is the
reference implementation, and it is the only place that decides whether an
incoming event is a duplicate.

Deduplication is by ``event_id``: replaying the same event (hook retry,
resume, out-of-order redelivery) must not append twice.

Appending takes the shared ``ledger`` lock (see ``lock_protocol``), whose name,
payload and staleness rule are fixed protocol -- a writer in another language
contends on the same file by honoring them, so two languages can never
interleave a half-line.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from datetime import datetime, timezone

from ledger_schema import EventKey, build_event, validate_event
from lock_protocol import LOCK_TIMEOUT, FileLock


SCAN_LIMIT = 5000


def ledger_path(cwd) -> Path:
    return Path(cwd) / ".tianji" / "state.jsonl"


def ledger_lock_path(cwd) -> Path:
    """The one lock file for the ledger, shared across languages."""
    return Path(cwd) / ".tianji" / "state.lock"


def diagnostics_path(cwd) -> Path:
    """Where unidentifiable events go.

    An event nobody could identify is not a ledger event: writing it to
    ``state.jsonl`` would forge an authoritative record. It gets its own file
    instead, where a human can see it and the reducer cannot.
    """
    return Path(cwd) / ".tianji" / "diagnostics.jsonl"


def synthesized_stop(key: EventKey, *, host: str, session_id: str,
                     invocation_id: str, reason: str,
                     reconciled: bool = False) -> dict:
    """The stop the host never sent, for a task whose identity we can prove.

    Two callers need this and they must agree on the shape: a dispatcher
    compensating after a dispatch that failed, and the reconciler closing work
    whose process died without a stop event. The difference is recorded rather
    than implied -- ``reconciled`` says the ledger was repaired after the fact,
    which is a weaker claim than "we watched this dispatch fail", and a reader
    must be able to tell them apart.
    """
    now = datetime.now(timezone.utc).isoformat()
    kind = "reconciled" if reconciled else "compensated"
    detail = {"compensated": True, "reason": reason}
    if reconciled:
        detail["reconciled"] = True
    return build_event(
        event_id=f"{kind}:{key.run_id}:{key.task_id}:{key.attempt}",
        event="subagent_stop",
        host=host or "unknown",
        run_id=key.run_id,
        session_id=session_id,
        task_id=key.task_id,
        attempt=key.attempt,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        agent="tianji-compensation",
        occurred_at=now,
        recorded_at=now,
        detail=detail,
    )


def append_diagnostic(cwd, *, event: str, reason: str, host: str = "",
                      agent: str = "", tool_call_id: str = "",
                      detail: dict | None = None) -> None:
    """Append one unidentifiable event, under the same lock as the ledger."""
    path = diagnostics_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": _now_iso(),
        "host": host,
        "event": event,
        "reason": reason,
        "agent": agent or "unknown",
        "toolCallId": tool_call_id,
        "detail": detail or {},
    }
    with _file_lock(ledger_lock_path(cwd)):
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Cross-process append lock, using the shared lock protocol."""
    with FileLock(lock_path, kind="ledger", timeout=LOCK_TIMEOUT):
        yield


def event_ids(cwd) -> set[str]:
    """Collect existing event IDs so a replayed event is recognized."""
    path = ledger_path(cwd)
    seen: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as stream:
            lines = stream.readlines()[-SCAN_LIMIT:]
    except FileNotFoundError:
        return seen
    except OSError:
        return seen
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("event_id"), str):
            seen.add(record["event_id"])
    return seen


def append_event(cwd, event: dict) -> bool:
    """Append a validated v2 event, or skip it when its event_id already exists."""
    validated = validate_event(event)
    path = ledger_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(ledger_lock_path(cwd)):
        if validated["event_id"] in event_ids(cwd):
            return False
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(validated, ensure_ascii=False) + "\n")
    return True
