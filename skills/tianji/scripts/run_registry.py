#!/usr/bin/env python3
"""Persistent, host-neutral Tianji run identity registry."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import claim
import ledger_reader
import ledger_sink
from ledger_schema import EventKey, require_canonical_uuid
from lock_protocol import LOCK_LEASE_SECONDS, FileLock, LockError


LIFECYCLES = frozenset({"active", "closed", "aborted"})
# An invocation has its own lifecycle: it is created pending at dispatch time,
# claimed once by the host's real call id, and reaches a terminal state when it
# ends. A v1 record whose lifecycle is in LEGACY_INVOCATION_LIFECYCLES predates
# claim tokens entirely and is isolated rather than reinterpreted -- the two
# vocabularies are never both live on one record.
INVOCATION_LIFECYCLES = frozenset({"pending", "claimed", "cancelled", "stopped"})
# Legal transitions. An invocation that never claimed (the dispatch never
# happened, or its marker never appeared) is cancelled rather than stopped:
# "stopped" means it ran and ended, and claiming otherwise would fake a run.
INVOCATION_TRANSITIONS = {
    "pending": frozenset({"claimed", "cancelled"}),
    "claimed": frozenset({"stopped", "cancelled"}),
    "cancelled": frozenset(),
    "stopped": frozenset(),
}
INVOCATION_TERMINAL = frozenset({"cancelled", "stopped"})

# Run/task lifecycle vocabulary, keyed by record kind, so migration can check a
# record's lifecycle against the family its kind actually uses.
RECORD_KINDS = frozenset({"run", "task", "invocation", "binding", "claim_token"})
LIFECYCLE_FAMILIES = {
    "run": LIFECYCLES,
    "task": LIFECYCLES,
    "binding": LIFECYCLES,
    "claim_token": LIFECYCLES,
    "invocation": INVOCATION_LIFECYCLES,
}

# Registry context files predate any version field: a record without one is
# version 1 (implicit), and every record written from now on is stamped
# version 2 (explicit). A record from the future is refused, never
# reinterpreted.
REGISTRY_LEGACY_VERSION = 1
REGISTRY_SCHEMA_VERSION = 2

# Migration outcomes. "isolated" means the record is preserved verbatim and
# kept out of the authoritative flow: it is neither stamped nor coerced.
MIGRATION_CURRENT = "current"
MIGRATION_MIGRATED = "migrated"
MIGRATION_ISOLATED = "isolated"

# Word characters cover non-ASCII names (a task may legitimately be named in
# Chinese), while path separators, dots and every other punctuation mark stay
# rejected so a label can never escape its directory.
SAFE_SEGMENT = re.compile(r"^[\w.-]{1,128}$")


class RegistryError(ValueError):
    pass


class RegistryLegacyError(RegistryError):
    """Raised when an authoritative read meets an isolated legacy record.

    Fail-closed on purpose: a pre-claim invocation cannot be given a claim
    token retroactively, so it must not be served as if it were current.
    """


@dataclass(frozen=True)
class RunContext:
    run_id: str
    host: str
    session_id: str
    lifecycle: str
    created_at: str
    updated_at: str
    resume_count: int = 0
    schema_version: int = REGISTRY_SCHEMA_VERSION


@dataclass(frozen=True)
class TaskContext:
    run_id: str
    task_id: str
    attempt: int
    role: str
    lifecycle: str
    created_at: str
    updated_at: str
    schema_version: int = REGISTRY_SCHEMA_VERSION

    @property
    def event_key(self) -> EventKey:
        return EventKey(self.run_id, self.task_id, self.attempt)


@dataclass(frozen=True)
class InvocationContext:
    run_id: str
    task_id: str
    attempt: int
    invocation_id: str
    host: str
    session_id: str
    role: str
    correlation_id: str
    agent_id: str
    lifecycle: str
    created_at: str
    updated_at: str
    # The host call id this invocation was claimed by; empty until claimed.
    tool_call_id: str = ""
    claimed_at: str = ""
    # The configuration snapshot the dispatcher saw when it dispatched. Proof
    # compares against this, never against a snapshot taken at collection time:
    # a digest recorded after the fact would approve whatever is there now.
    subject_digest: str = ""
    # The binding the dispatcher saw at dispatch time. Recorded here so the
    # adapter does not have to re-read (and re-parse) the role file when the
    # event arrives: the shared core holds the fact, the adapter reports it.
    model: str = ""
    model_source: str = ""
    # What this dispatch was allowed to spend, decided before it ran. Zero means
    # nobody set a budget, which is not the same as an unlimited one: a report
    # that cannot tell the two apart is guessing about the number that matters.
    token_budget: int = 0
    # Wall-clock ceiling, in minutes, decided before the dispatch ran. Zero
    # means nobody set one, the same distinction the token budget draws:
    # "no ceiling" and "inside the ceiling" are different facts.
    time_budget_minutes: int = 0
    # Tool calls this dispatch may make, decided before it ran. Zero means
    # nobody set one -- the same distinction the token and time budgets draw.
    call_budget: int = 0
    # Whether this order carries a configuration snapshot at all, and why not.
    # Its own fields rather than a convention: the route proof has to tell a
    # skipped snapshot from a taken one without parsing prose.
    snapshot_omitted: bool = False
    snapshot_source: str = ""
    schema_version: int = REGISTRY_SCHEMA_VERSION

    @property
    def event_key(self) -> EventKey:
        return EventKey(self.run_id, self.task_id, self.attempt)


@dataclass(frozen=True)
class Binding:
    host: str
    session_id: str
    binding_kind: str
    binding_value: str
    run_id: str
    task_id: str
    attempt: int
    invocation_id: str
    created_at: str
    schema_version: int = REGISTRY_SCHEMA_VERSION

    @property
    def event_key(self) -> EventKey:
        return EventKey(self.run_id, self.task_id, self.attempt)


@dataclass(frozen=True)
class ClaimToken:
    """A one-time capability that resolves to one pending invocation.

    The raw token is never stored -- only its digest -- so a leaked registry
    yields no usable claim. The record is bound to the host/session/role that
    issued it, so a token that travels into another session is refused rather
    than honored.
    """

    token_hash: str
    run_id: str
    task_id: str
    attempt: int
    invocation_id: str
    host: str
    session_id: str
    role: str
    issued_at: str
    expires_at: str
    consumed_at: str = ""
    tool_call_id: str = ""
    schema_version: int = REGISTRY_SCHEMA_VERSION

    @property
    def event_key(self) -> EventKey:
        return EventKey(self.run_id, self.task_id, self.attempt)

    def is_expired(self, now: datetime) -> bool:
        try:
            deadline = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return now > deadline


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of a claim: the invocation plus whether it was a retry."""

    invocation: InvocationContext
    reused: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _segment(value: str, label: str) -> str:
    if (not isinstance(value, str) or not SAFE_SEGMENT.fullmatch(value)
            or value in {".", ".."}):
        raise RegistryError(f"{label} is not a safe path segment")
    return value


def _run_id(value: str) -> str:
    try:
        return require_canonical_uuid("run_id", value)
    except ValueError as exc:
        raise RegistryError("run_id must be an opaque UUID") from exc


def _lifecycle(value: str) -> str:
    if value not in LIFECYCLES:
        raise RegistryError(f"lifecycle must be one of {sorted(LIFECYCLES)}")
    return value


def _record_schema_version(record: dict) -> int:
    """Version of a stored runtime context.

    A file written before the field existed is version 1, never an error; a
    file from a newer registry is refused rather than reinterpreted.
    """
    if not isinstance(record, dict):
        raise RegistryError("runtime context is not an object")
    version = record.get("schema_version", REGISTRY_LEGACY_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RegistryError("runtime context schema_version must be a positive integer")
    if version > REGISTRY_SCHEMA_VERSION:
        raise RegistryError(
            f"runtime context schema_version {version} is newer than this "
            f"registry ({REGISTRY_SCHEMA_VERSION}); refusing to reinterpret it"
        )
    return version


def record_kind(record: dict) -> str:
    """Classify a stored runtime context by its distinguishing fields.

    The registry stores four shapes, and a migration has to know which one it
    is holding before it can decide whether a version stamp is safe.
    """
    if not isinstance(record, dict):
        raise RegistryError("runtime context is not an object")
    if "binding_kind" in record:
        return "binding"
    if "token_hash" in record:
        return "claim_token"
    if "invocation_id" in record:
        return "invocation"
    if "task_id" in record:
        return "task"
    if "resume_count" in record or ("run_id" in record and "host" in record):
        return "run"
    raise RegistryError("unrecognized runtime context record")


def _lifecycle_family(kind: str) -> frozenset:
    if kind not in LIFECYCLE_FAMILIES:
        raise RegistryError(f"unknown record kind {kind!r}")
    return LIFECYCLE_FAMILIES[kind]


@dataclass(frozen=True)
class MigrationOutcome:
    """Result of classifying one stored record.

    ``status`` is one of ``current`` / ``migrated`` / ``isolated``. Only the
    first two may be consumed as authoritative; an isolated record is returned
    exactly as found, and recovering it is an explicit operator action.
    """

    status: str
    kind: str
    record: dict
    reason: str = ""

    @property
    def is_authoritative(self) -> bool:
        return self.status in (MIGRATION_CURRENT, MIGRATION_MIGRATED)


def migrate_record(record: dict) -> MigrationOutcome:
    """Classify one stored record, migrating only what is safe to migrate.

    Migration is per record *kind*, because one rule cannot fit all four:

    * a current-version record is validated and passed through;
    * a v1 run/task/binding keeps the lifecycle vocabulary it was written with,
      so stamping the current version is lossless and it is migrated;
    * a v1 invocation cannot be migrated at all -- it predates claim tokens, so
      its ``active``/``closed`` lifecycle has no honest counterpart in the new
      vocabulary, and stamping it would mint a v2 record that lies. It is
      isolated verbatim instead.

    Isolation is deliberate and fail-closed: the record is never coerced, never
    admitted to proof/acceptance, and never silently dropped. Recovery
    (reconciling or retiring such a record) is an explicit operator step.
    """
    kind = record_kind(record)
    version = _record_schema_version(record)
    family = _lifecycle_family(kind)
    lifecycle = record.get("lifecycle")
    has_lifecycle = lifecycle is not None

    if version == REGISTRY_SCHEMA_VERSION:
        if has_lifecycle and lifecycle not in family:
            return MigrationOutcome(
                MIGRATION_ISOLATED, kind, dict(record),
                f"lifecycle {lifecycle!r} is not valid for a v2 {kind}",
            )
        return MigrationOutcome(MIGRATION_CURRENT, kind, dict(record))

    # v1: run/task/binding share the legacy vocabulary with their v2 form, so
    # the only change needed is the explicit version stamp.
    if kind != "invocation":
        if has_lifecycle and lifecycle not in family:
            return MigrationOutcome(
                MIGRATION_ISOLATED, kind, dict(record),
                f"legacy {kind} lifecycle {lifecycle!r} is not recognized",
            )
        migrated = dict(record)
        migrated["schema_version"] = REGISTRY_SCHEMA_VERSION
        return MigrationOutcome(MIGRATION_MIGRATED, kind, migrated)

    return MigrationOutcome(
        MIGRATION_ISOLATED, kind, dict(record),
        "legacy invocation predates claim tokens; its lifecycle has no "
        "honest v2 counterpart",
    )


def invocation_transition(current: str, next_state: str) -> str:
    """Return ``next_state`` when the invocation transition is legal."""
    if current not in INVOCATION_LIFECYCLES:
        raise RegistryError(
            f"invocation lifecycle must be one of {sorted(INVOCATION_LIFECYCLES)}"
        )
    if next_state not in INVOCATION_TRANSITIONS[current]:
        raise RegistryError(
            f"illegal invocation transition {current!r} -> {next_state!r}"
        )
    return next_state


def is_invocation_terminal(state: str) -> bool:
    """True once an invocation can never change state again."""
    return state in INVOCATION_TERMINAL


class RunRegistry:
    def __init__(
        self, workspace_root: str | Path, *, lock_timeout: float = 5.0,
        lease_seconds: float = LOCK_LEASE_SECONDS,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.runtime_root = self.workspace_root / ".tianji" / "runtime"
        self.lock_timeout = lock_timeout
        self.lease_seconds = lease_seconds
        self._thread_lock = threading.Lock()

    @property
    def lock_path(self) -> Path:
        return self.runtime_root / ".registry.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Take the shared registry lock, translating lock errors to ours."""
        with self._thread_lock:
            lock = FileLock(
                self.lock_path, kind="registry",
                timeout=self.lock_timeout, lease_seconds=self.lease_seconds,
            )
            try:
                lock.__enter__()
            except LockError as exc:
                raise RegistryError(str(exc)) from exc
            try:
                yield
            finally:
                lock.__exit__(None, None, None)

    @staticmethod
    def _stamped(value: dict) -> dict:
        """Stamp the current schema version on write.

        A legacy invocation is the one record that must not be stamped: it has
        no honest current-version form, so it is left verbatim and stays
        visibly isolated rather than being silently upgraded.
        """
        record = dict(value)
        if record_kind(record) == "invocation":
            lifecycle = record.get("lifecycle")
            if lifecycle is not None and lifecycle not in INVOCATION_LIFECYCLES:
                return record
        record["schema_version"] = REGISTRY_SCHEMA_VERSION
        return record

    def _atomic_write(self, path: Path, value: dict) -> None:
        value = self._stamped(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RegistryError(f"runtime context does not exist: {path.name}") from exc
        if not isinstance(value, dict):
            raise RegistryError(f"runtime context is not an object: {path.name}")
        return value

    def _run_path(self, run_id: str) -> Path:
        return self.runtime_root / "runs" / _run_id(run_id) / "run.json"

    def _attempt_dir(self, key: EventKey) -> Path:
        return (
            self.runtime_root / "runs" / _run_id(key.run_id) / "tasks"
            / _segment(key.task_id, "task_id") / str(key.attempt)
        )

    def _task_path(self, key: EventKey) -> Path:
        return self._attempt_dir(key) / "task.json"

    def _invocation_path(self, key: EventKey, invocation_id: str) -> Path:
        return (
            self._attempt_dir(key) / "invocations"
            / f"{_segment(invocation_id, 'invocation_id')}.json"
        )

    def _binding_path(
        self, host: str, session_id: str, kind: str, value: str,
    ) -> Path:
        host = _segment(host, "host")
        session_id = _segment(session_id, "session_id")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return self.runtime_root / "bindings" / host / session_id / f"{kind}-{digest}.json"

    def _claim_token_path(self, token_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", token_hash or ""):
            raise RegistryError("claim token digest is not a sha256 hex string")
        return self.runtime_root / "claim-tokens" / f"{token_hash}.json"

    def _binding_holder_is_active(self, path: Path) -> bool:
        """True while the binding's task (and its run) are still open.

        A closed task has handed the slot back, so its binding may be replaced
        by the next dispatch. An open one may not: two live tasks sharing a
        role cannot be told apart from a single role binding, and merging them
        would silently attribute one worker's events to another.
        """
        try:
            binding = self._read(path)
            key = EventKey(
                str(binding.get("run_id", "")),
                str(binding.get("task_id", "")),
                int(binding.get("attempt", 0)),
            )
            task = self.get_task(key)
        except (RegistryError, ValueError, TypeError):
            return False
        if task.lifecycle != "active":
            return False
        try:
            return self.get_run(task.run_id).lifecycle == "active"
        except (RegistryError, ValueError):
            return True

    def _release_bindings(self, key: EventKey) -> int:
        """Drop every host binding owned by this task attempt."""
        root = self.runtime_root / "bindings"
        if not root.is_dir():
            return 0
        released = 0
        for path in sorted(root.rglob("*.json")):
            try:
                binding = json.loads(path.read_text(encoding="utf-8"))
                owned = (
                    str(binding.get("run_id", "")) == key.run_id
                    and str(binding.get("task_id", "")) == key.task_id
                    and int(binding.get("attempt", 0)) == key.attempt
                )
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if not owned:
                continue
            try:
                path.unlink()
                released += 1
            except OSError:
                pass
        return released

    def _release_claim_tokens(self, key: EventKey) -> int:
        """Drop this task's *unconsumed* claim tokens.

        A consumed token is kept: it records which call id claimed which
        invocation, and that is the audit trail the ledger relies on. An
        unconsumed one is a dispatch that never arrived, and holding it would
        let a stale marker claim a closed task.
        """
        root = self.runtime_root / "claim-tokens"
        if not root.is_dir():
            return 0
        released = 0
        for path in sorted(root.glob("*.json")):
            try:
                token = json.loads(path.read_text(encoding="utf-8"))
                owned = (
                    isinstance(token, dict)
                    and str(token.get("run_id", "")) == key.run_id
                    and str(token.get("task_id", "")) == key.task_id
                    and int(token.get("attempt", 0)) == key.attempt
                    and not token.get("consumed_at")
                )
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if not owned:
                continue
            try:
                path.unlink()
                released += 1
            except OSError:
                pass
        return released

    def release_task(self, key: EventKey) -> tuple[TaskContext, int]:
        """Close the task, end its invocations legally, and free its indexes.

        Every invocation is moved along its own lifecycle: one that was claimed
        stops, one that never claimed is cancelled. A legacy (pre-claim)
        invocation is left untouched -- it is isolated, not rewritten.
        Unconsumed claim tokens and exact call-id bindings are dropped, so a
        closed task can never be re-claimed.
        """
        path = self._task_path(key)
        now = _now()
        with self._locked():
            current = self._read(path)
            current["lifecycle"] = "closed"
            current["updated_at"] = now
            self._atomic_write(path, current)
            invocations = self._attempt_dir(key) / "invocations"
            if invocations.is_dir():
                for invocation_path in sorted(invocations.glob("*.json")):
                    record = self._read(invocation_path)
                    if not migrate_record(record).is_authoritative:
                        continue
                    state = record.get("lifecycle")
                    if is_invocation_terminal(state):
                        continue
                    target = "stopped" if state == "claimed" else "cancelled"
                    record = dict(record)
                    record["lifecycle"] = invocation_transition(state, target)
                    record["updated_at"] = now
                    self._atomic_write(invocation_path, record)
            self._release_claim_tokens(key)
            released = self._release_bindings(key)
        return TaskContext(**current), released

    def create_run(
        self, *, host: str, session_id: str, run_id: str | None = None,
    ) -> RunContext:
        host = _segment(host, "host")
        session_id = _segment(session_id, "session_id")
        run_id = _run_id(run_id or str(uuid.uuid4()))
        now = _now()
        context = RunContext(run_id, host, session_id, "active", now, now)
        path = self._run_path(run_id)
        with self._locked():
            if path.exists():
                raise RegistryError("run_id already exists")
            self._atomic_write(path, asdict(context))
        return context

    def get_run(self, run_id: str) -> RunContext:
        return RunContext(**self._read(self._run_path(run_id)))

    def resume_run(self, run_id: str) -> RunContext:
        path = self._run_path(run_id)
        with self._locked():
            current = self._read(path)
            current["lifecycle"] = "active"
            current["updated_at"] = _now()
            current["resume_count"] = int(current.get("resume_count", 0)) + 1
            self._atomic_write(path, current)
        return RunContext(**current)

    def transition_run(self, run_id: str, lifecycle: str) -> RunContext:
        lifecycle = _lifecycle(lifecycle)
        path = self._run_path(run_id)
        with self._locked():
            current = self._read(path)
            current["lifecycle"] = lifecycle
            current["updated_at"] = _now()
            self._atomic_write(path, current)
        return RunContext(**current)

    def create_task(
        self, run_id: str, task_id: str, *, attempt: int = 1, role: str = "",
    ) -> TaskContext:
        key = EventKey(_run_id(run_id), _segment(task_id, "task_id"), attempt)
        now = _now()
        context = TaskContext(
            key.run_id, key.task_id, key.attempt, role, "active", now, now,
        )
        path = self._task_path(key)
        with self._locked():
            self._read(self._run_path(key.run_id))
            if path.exists():
                raise RegistryError("task attempt already exists")
            self._atomic_write(path, asdict(context))
        return context

    def get_task(self, key: EventKey) -> TaskContext:
        return TaskContext(**self._read(self._task_path(key)))

    def transition_task(self, key: EventKey, lifecycle: str) -> TaskContext:
        lifecycle = _lifecycle(lifecycle)
        path = self._task_path(key)
        with self._locked():
            current = self._read(path)
            current["lifecycle"] = lifecycle
            current["updated_at"] = _now()
            self._atomic_write(path, current)
        return TaskContext(**current)

    def create_invocation(
        self, key: EventKey, *, host: str, session_id: str, role: str,
        correlation_id: str = "", agent_id: str = "",
        invocation_id: str | None = None,
    ) -> InvocationContext:
        """Create a pending invocation.

        ``correlation_id`` is optional: at dispatch time the host call id does
        not exist yet, so identity is completed later by ``claim_invocation``.
        ``agent_id`` is accepted for compatibility but never indexed -- binding
        by role is exactly the ambiguity claim tokens remove.
        """
        host = _segment(host, "host")
        session_id = _segment(session_id, "session_id")
        invocation_id = _segment(
            invocation_id or f"inv-{uuid.uuid4()}", "invocation_id",
        )
        now = _now()
        context = InvocationContext(
            key.run_id, key.task_id, key.attempt, invocation_id,
            host, session_id, role, correlation_id, agent_id,
            "pending", now, now,
        )
        invocation_path = self._invocation_path(key, invocation_id)
        with self._locked():
            self._read(self._task_path(key))
            if invocation_path.exists():
                raise RegistryError("invocation already exists")
            self._atomic_write(invocation_path, asdict(context))
        return context

    def create_pending_invocation(
        self, key: EventKey, *, host: str, session_id: str, role: str,
        invocation_id: str | None = None,
        ttl_seconds: float = claim.DEFAULT_TTL_SECONDS,
        subject_digest: str = "",
        model: str = "",
        model_source: str = "",
        token_budget: int = 0,
        time_budget_minutes: int = 0,
        call_budget: int = 0,
        snapshot_omitted: bool = False,
        snapshot_source: str = "",
    ) -> tuple[InvocationContext, str]:
        """Create a pending invocation and mint its one-time claim token.

        Returns ``(invocation, token)``. The token is returned to the caller and
        never persisted in the clear; the registry keeps only its digest. The
        caller places the token in the host's dispatch description, and the host
        hands it back on the matching start event.

        ``subject_digest`` is the configuration the dispatcher saw *now*, and
        ``model``/``model_source`` the binding it saw *now*. They are recorded
        so proof can later ask whether anything changed since, instead of
        re-deriving the answer from whatever the configuration became. The host
        computes these facts; this layer only stores them, so it stays free of
        any knowledge of a particular host's layout.
        """
        host = _segment(host, "host")
        session_id = _segment(session_id, "session_id")
        invocation_id = _segment(
            invocation_id or f"inv-{uuid.uuid4()}", "invocation_id",
        )
        token = claim.new_token()
        token_hash = claim.token_digest(token)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        context = InvocationContext(
            key.run_id, key.task_id, key.attempt, invocation_id,
            host, session_id, role, "", "",
            "pending", now, now, subject_digest=str(subject_digest or ""),
            model=str(model or ""), model_source=str(model_source or ""),
            token_budget=max(0, int(token_budget or 0)),
            time_budget_minutes=max(0, int(time_budget_minutes or 0)),
            call_budget=max(0, int(call_budget or 0)),
            snapshot_omitted=bool(snapshot_omitted),
            snapshot_source=str(snapshot_source or ""),
        )
        record = ClaimToken(
            token_hash=token_hash,
            run_id=key.run_id, task_id=key.task_id, attempt=key.attempt,
            invocation_id=invocation_id, host=host, session_id=session_id,
            role=role, issued_at=now,
            expires_at=(now_dt + timedelta(seconds=ttl_seconds)).isoformat(),
        )
        invocation_path = self._invocation_path(key, invocation_id)
        token_path = self._claim_token_path(token_hash)
        with self._locked():
            self._read(self._task_path(key))
            if invocation_path.exists():
                raise RegistryError("invocation already exists")
            if token_path.exists():
                raise RegistryError("claim token collision; retry the dispatch")
            self._atomic_write(invocation_path, asdict(context))
            self._atomic_write(token_path, asdict(record))
        return context, token

    def pending_invocation_id(self, key: EventKey) -> str:
        """The one pending invocation of a task, when there is exactly one."""
        directory = self._attempt_dir(key) / "invocations"
        pending = []
        for path in sorted(directory.glob("*.json")):
            outcome = migrate_record(self._read(path))
            if outcome.is_authoritative and outcome.record.get("lifecycle") == "pending":
                pending.append(str(outcome.record.get("invocation_id", "")))
        if len(pending) != 1:
            raise RegistryError(
                f"task {key.as_dict()} has {len(pending)} pending invocations; "
                "name one explicitly"
            )
        return pending[0]

    def refresh_claim_token(
        self, key: EventKey, *, invocation_id: str = "",
        ttl_seconds: float = claim.DEFAULT_TTL_SECONDS,
    ) -> tuple[InvocationContext, str]:
        """Mint a fresh token for an invocation that is still waiting to claim.

        The alternative -- opening the task again -- is the wrong repair: a new
        attempt loses the chain that says what happened, and the dispatch that
        follows reads as a different piece of work. The identity stays put; only
        the deadline moves. An invocation that was already claimed is refused,
        because a second live token for one dispatch is not a refill.
        """
        self._require_live(key)
        invocation_id = invocation_id or self.pending_invocation_id(key)
        invocation = self.get_invocation(key, invocation_id)
        if invocation.lifecycle != "pending":
            raise RegistryError(
                f"invocation {invocation_id} is {invocation.lifecycle}, not pending: "
                "there is nothing to refresh"
            )
        token = claim.new_token()
        now_dt = datetime.now(timezone.utc)
        record = ClaimToken(
            token_hash=claim.token_digest(token),
            run_id=key.run_id, task_id=key.task_id, attempt=key.attempt,
            invocation_id=invocation_id, host=invocation.host,
            session_id=invocation.session_id, role=invocation.role,
            issued_at=now_dt.isoformat(),
            expires_at=(now_dt + timedelta(seconds=ttl_seconds)).isoformat(),
        )
        with self._locked():
            self._atomic_write(self._claim_token_path(record.token_hash), asdict(record))
        return invocation, token

    def spend_by_invocation(self, key: EventKey) -> dict[str, int]:
        """Tokens the ledger attributes to each invocation of one task.

        Read from the ledger rather than from a counter kept here: the ledger is
        the single source, and a budget checked against a second tally would be
        checked against the wrong number. Only stop rows carry a total -- adding
        progress rows counts the same tokens once per turn.
        """
        path = self.workspace_root / ".tianji" / "state.jsonl"
        spend: dict[str, int] = {}
        for event in ledger_reader.authoritative_events(path):
            # Readers see the canonical shape, where identity is one tuple --
            # not the flat run/task/attempt the raw line carries.
            if event.get("event_key") != (key.run_id, key.task_id, key.attempt):
                continue
            tokens = ledger_reader.dispatch_tokens(event)
            invocation_id = str(event.get("invocation_id") or "")
            if tokens and invocation_id:
                spend[invocation_id] = spend.get(invocation_id, 0) + tokens
        return spend

    def budget_status(self, key: EventKey) -> dict:
        """What ran past its budget -- and what never had one.

        Reported, never enforced: nothing kills a worker mid-flight, because a
        half-finished dispatch costs more than the overrun does. But a dispatch
        that closed over budget has to say so, or the number only surfaces when
        somebody adds the column up by hand, weeks later.
        """
        over: list[dict] = []
        unbudgeted: list[dict] = []
        for invocation_id, spent in sorted(self.spend_by_invocation(key).items()):
            try:
                invocation = self.get_invocation(key, invocation_id)
            except RegistryError:
                continue
            if not invocation.token_budget:
                unbudgeted.append({"invocation_id": invocation_id, "spent": spent})
                continue
            if spent > invocation.token_budget:
                over.append({
                    "invocation_id": invocation_id,
                    "role": invocation.role,
                    "model": invocation.model,
                    "budget": invocation.token_budget,
                    "spent": spent,
                    "over_percent": round(
                        (spent - invocation.token_budget) * 100 / invocation.token_budget,
                    ),
                })
        return {"over_budget": over, "unbudgeted": unbudgeted}

    def time_status(self, key: EventKey) -> dict:
        """What ran past its time budget -- reported, never enforced.

        The same discipline as the token budget: nothing kills a dispatch for
        running long, so the only thing between an overrun and nobody noticing
        is the line this feeds. Elapsed is wall clock from when the invocation
        was opened to its last state change. A dispatch with no time ceiling is
        left out, not counted as unbudgeted against time -- "no ceiling" is not
        an overrun.
        """
        over: list[dict] = []
        directory = self._attempt_dir(key) / "invocations"
        for path in sorted(directory.glob("*.json")):
            outcome = migrate_record(self._read(path))
            if not outcome.is_authoritative:
                continue
            record = outcome.record
            try:
                minutes = int(record.get("time_budget_minutes") or 0)
            except (TypeError, ValueError):
                continue
            if minutes <= 0:
                continue
            spent = _elapsed_minutes(record.get("created_at"), record.get("updated_at"))
            if spent is None or spent <= minutes:
                continue
            over.append({
                "invocation_id": str(record.get("invocation_id") or ""),
                "role": str(record.get("role") or ""),
                "budget": minutes,
                "spent": round(spent, 1),
                "over_percent": round((spent - minutes) * 100 / minutes),
            })
        return {"over_time": over}

    def calls_by_invocation(self, key: EventKey) -> dict[str, int]:
        """Child tool calls the ledger attributes to each invocation.

        Read from the ledger for the same reason tokens are: the host counts the
        calls as they happen and reports a running total on its progress rows,
        so the largest one is where the dispatch stands. A row that carries no
        count contributes nothing -- an unreported call must not read as zero
        calls made, and must not read as one either.
        """
        path = self.workspace_root / ".tianji" / "state.jsonl"
        calls: dict[str, int] = {}
        for event in ledger_reader.authoritative_events(path):
            if event.get("event_key") != (key.run_id, key.task_id, key.attempt):
                continue
            detail = event.get("detail")
            reported = detail.get("toolCalls") if isinstance(detail, dict) else None
            invocation_id = str(event.get("invocation_id") or "")
            if isinstance(reported, int) and reported > 0 and invocation_id:
                calls[invocation_id] = max(calls.get(invocation_id, 0), reported)
        return calls

    def call_status(self, key: EventKey) -> dict:
        """What ran past its call budget -- reported, never enforced.

        The third ceiling, under the same discipline as the other two: nothing
        kills a dispatch for making too many calls, so the only thing between an
        overrun and nobody noticing is the line this feeds. A dispatch with no
        call budget is left out rather than counted as unbudgeted -- "no ceiling"
        is not an overrun, and a report that confuses the two is guessing.
        """
        over: list[dict] = []
        for invocation_id, spent in sorted(self.calls_by_invocation(key).items()):
            try:
                invocation = self.get_invocation(key, invocation_id)
            except RegistryError:
                continue
            budget = invocation.call_budget
            if budget <= 0 or spent <= budget:
                continue
            over.append({
                "invocation_id": invocation_id,
                "role": invocation.role,
                "call_budget": budget,
                "spent": spent,
                "over": spent - budget,
            })
        return {"over_calls": over}

    def claim_invocation(
        self, *, token: str, tool_call_id: str, host: str, session_id: str,
        role: str = "",
    ) -> ClaimResult:
        """Atomically claim a pending invocation with the host's call id.

        Idempotent for ``(token, tool_call_id)``: a retried start returns the
        same invocation rather than claiming twice. The same token presented
        with a *different* call id is refused -- that is one capability being
        used to name two workers, which is exactly what a one-time token exists
        to prevent.
        """
        host = _segment(host, "host")
        session_id = _segment(session_id, "session_id")
        if not tool_call_id:
            raise RegistryError("tool_call_id is required to claim an invocation")
        token_hash = claim.token_digest(token)

        with self._locked():
            token_path = self._claim_token_path(token_hash)
            entry = ClaimToken(**self._read(token_path))

            if entry.host != host or entry.session_id != session_id:
                raise RegistryError(
                    "claim token was issued to another host session; refusing it"
                )
            if role and entry.role and role != entry.role:
                raise RegistryError(
                    f"claim token belongs to role {entry.role!r}, not {role!r}"
                )

            invocation_path = self._invocation_path(entry.event_key, entry.invocation_id)
            record = self._read(invocation_path)
            outcome = migrate_record(record)
            if not outcome.is_authoritative:
                raise RegistryLegacyError(
                    f"invocation {entry.invocation_id!r} is isolated legacy state"
                )
            current = outcome.record

            if entry.consumed_at:
                if entry.tool_call_id == tool_call_id:
                    return ClaimResult(InvocationContext(**current), reused=True)
                raise RegistryError(
                    "claim token has already been consumed by another call id"
                )
            if entry.is_expired(datetime.now(timezone.utc)):
                raise RegistryError("claim token has expired")

            self._require_live(entry.event_key)
            state = current.get("lifecycle")
            if state != "pending":
                raise RegistryError(
                    f"invocation is {state!r}, not pending; it cannot be claimed"
                )
            invocation_transition(state, "claimed")

            claimed = dict(current)
            claimed["lifecycle"] = "claimed"
            claimed["tool_call_id"] = tool_call_id
            claimed["claimed_at"] = _now()
            claimed["updated_at"] = claimed["claimed_at"]
            self._atomic_write(invocation_path, claimed)

            binding_path = self._binding_path(host, session_id, "correlation", tool_call_id)
            self._atomic_write(binding_path, asdict(Binding(
                host, session_id, "correlation", tool_call_id,
                entry.run_id, entry.task_id, entry.attempt,
                entry.invocation_id, claimed["claimed_at"],
            )))

            consumed = dict(asdict(entry))
            consumed["consumed_at"] = claimed["claimed_at"]
            consumed["tool_call_id"] = tool_call_id
            self._atomic_write(token_path, consumed)

        return ClaimResult(InvocationContext(**claimed), reused=False)

    def get_invocation(
        self, key: EventKey, invocation_id: str,
    ) -> InvocationContext:
        record = self._read(self._invocation_path(key, invocation_id))
        outcome = migrate_record(record)
        if not outcome.is_authoritative:
            raise RegistryLegacyError(
                f"invocation {invocation_id!r} is isolated legacy state: "
                f"{outcome.reason}"
            )
        return InvocationContext(**outcome.record)

    def resolve_binding(
        self, *, host: str, session_id: str, correlation_id: str = "",
        agent_id: str = "",
    ) -> Binding:
        """Resolve the exact binding for one host call id.

        Role fallback is gone: a dispatch whose call id is not yet known has no
        binding, and the honest answer is "unbound", not "the last task with
        that role". Two live dispatches of one role would otherwise merge into a
        single task, silently attributing one worker's events to another.
        """
        if agent_id and not correlation_id:
            raise RegistryError(
                "binding by role is not supported; resolve by correlation_id"
            )
        if not correlation_id:
            raise RegistryError("correlation_id is required")
        path = self._binding_path(host, session_id, "correlation", correlation_id)
        binding = Binding(**self._read(path))
        self._require_live(binding.event_key)
        return binding

    def _require_live(self, key: EventKey) -> None:
        """Re-validate the whole lifecycle chain; never trust the index alone.

        An index entry only proves a binding was written once. The run, task and
        invocation may all have closed since, so every resolve re-reads them.
        """
        self._read(self._run_path(key.run_id))
        self._read(self._task_path(key))
        if self.get_run(key.run_id).lifecycle != "active":
            raise RegistryError("run is not active; binding is no longer resolvable")
        if self.get_task(key).lifecycle != "active":
            raise RegistryError("task is not active; binding is no longer resolvable")

    def verify_event_key(self, key: EventKey) -> EventKey:
        """Confirm an EventKey this registry actually issued.

        A payload may carry a run/task/attempt, but that is a *claim*: it names
        an identity only if the registry knows the run and the task. Anything
        else is a forged or stale key and must not reach the ledger.
        """
        run = self.get_run(key.run_id)
        task = self.get_task(key)
        if task.attempt != key.attempt:
            raise RegistryError("task attempt does not match the claimed EventKey")
        if run.run_id != task.run_id:
            raise RegistryError("task does not belong to the claimed run")
        return key

    def task_invocation_id(self, key: EventKey) -> str:
        """The single invocation of a task.

        One invocation is unambiguous; several mean a retry happened, and only
        the caller knows which one it means -- so this refuses rather than
        picking one and attributing events to the wrong worker.
        """
        directory = self._attempt_dir(key) / "invocations"
        found = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                record = self._read(path)
                if not migrate_record(record).is_authoritative:
                    continue
                found.append(str(record.get("invocation_id") or path.stem))
        if len(found) != 1:
            raise RegistryError(
                "task does not have exactly one invocation; name it explicitly"
            )
        return found[0]

    def close_accepted_task(self, key: EventKey) -> TaskContext:
        """Close a task whose acceptance is terminal.

        Acceptance is the end of a task's life, so the registry is what closes
        it -- an event in the ledger must not be the only trace that a task is
        over, or the next dispatch would still see it as live.
        """
        task, _released = self.release_task(key)
        return task

    def resolve_claim_token(self, token: str) -> ClaimToken:
        """Read a claim token's record without consuming it (diagnostics)."""
        return ClaimToken(**self._read(self._claim_token_path(claim.token_digest(token))))

    def transition_invocation(
        self, key: EventKey, invocation_id: str, lifecycle: str,
    ) -> InvocationContext:
        if lifecycle not in INVOCATION_LIFECYCLES:
            raise RegistryError(
                f"invocation lifecycle must be one of {sorted(INVOCATION_LIFECYCLES)}"
            )
        path = self._invocation_path(key, invocation_id)
        with self._locked():
            current = self._read(path)
            if not migrate_record(current).is_authoritative:
                raise RegistryLegacyError(
                    f"invocation {invocation_id!r} is isolated legacy state"
                )
            invocation_transition(current.get("lifecycle"), lifecycle)
            current = dict(current)
            current["lifecycle"] = lifecycle
            current["updated_at"] = _now()
            self._atomic_write(path, current)
        return InvocationContext(**current)


# ============================================================================
# --- reconcile: close what is provably over, and name what is not ------------
#
# Two surfaces go stale in different ways, and neither repairs itself:
#
#   * a run whose tasks are all closed but which stays "active" -- closing a run
#     was always a deliberate step, so nothing ever performs it;
#   * a start in the ledger with no stop -- the board calls it running forever,
#     and after a worker is killed that never becomes true again.
#
# One rule covers both: act only where the identity chain proves the case. A run
# with a live task is left alone, and a start whose EventKey this registry never
# issued is reported rather than repaired -- synthesizing a stop without
# identity is precisely how dead workers came to look alive.

RECONCILE_STALE_MINUTES = 30


def _stamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _run_files(workspace) -> list[tuple[dict, list[dict]]]:
    """Every run on disk with its tasks, as recorded rather than assumed."""
    root = Path(workspace) / ".tianji" / "runtime" / "runs"
    entries: list[tuple[dict, list[dict]]] = []
    if not root.is_dir():
        return entries
    for run_dir in sorted(root.iterdir()):
        run_file = run_dir / "run.json"
        if not run_file.is_file():
            continue
        tasks = []
        tasks_dir = run_dir / "tasks"
        if tasks_dir.is_dir():
            for task_dir in sorted(tasks_dir.iterdir()):
                for attempt_dir in sorted(p for p in task_dir.iterdir() if p.is_dir()):
                    task_file = attempt_dir / "task.json"
                    if task_file.is_file():
                        tasks.append(RunRegistry._read(task_file))
        entries.append((RunRegistry._read(run_file), tasks))
    return entries


def _unpaired_starts(workspace) -> dict:
    """Ledger starts with no stop, keyed by EventKey: what a board calls running."""
    read = ledger_reader.read_ledger(Path(workspace) / ".tianji" / "state.jsonl")
    starts: dict = {}
    stopped = set()
    for record in read.canonical:
        event = ledger_reader.canonical_event(record)
        if event is None:
            continue
        if event["event"] == "subagent_start":
            starts.setdefault(event["event_key"], event)
        elif event["event"] == "subagent_stop":
            stopped.add(event["event_key"])
    return {key: event for key, event in starts.items() if key not in stopped}


def reconcile(workspace, *, stale_minutes: int = RECONCILE_STALE_MINUTES,
              apply: bool = False) -> dict:
    """Report, and optionally repair, the state that only looks alive.

    Dry by default: the caller sees the whole verdict before anything moves.
    Anything the identity chain cannot prove is returned under ``held_back`` or
    ``unprovable`` and left exactly as it was.
    """
    registry = RunRegistry(workspace)
    now = datetime.now(timezone.utc)
    cutoff = timedelta(minutes=stale_minutes)

    # The ledger first, because it is what makes the runs provable: a start with
    # no stop is a task that will never close itself, and a run cannot be closed
    # while a task in it is open. Doing it in this order is what lets one
    # command finish the whole cascade instead of needing two passes.
    repaired, unprovable, held_back = [], [], []
    ordered = sorted(_unpaired_starts(workspace).items(),
                     key=lambda item: [str(part) for part in item[0]])
    for key, event in ordered:
        started = _stamp(event.get("ts"))
        if started is None or now - started < cutoff:
            held_back.append({"event_key": list(key), "why": "activity is recent"})
            continue
        try:
            registry.verify_event_key(EventKey(*key))
        except RegistryError as exc:
            unprovable.append({"event_key": list(key),
                               "why": f"this registry never issued it ({exc})"})
            continue
        repaired.append({
            "event_key": list(key),
            "started": event.get("ts"),
            "host": event.get("host", ""),
            "session_id": event.get("session_id", ""),
            "invocation_id": event.get("invocation_id", ""),
        })

    # A task this repair will close counts as closed when judging its run, or a
    # dry run would under-count and an applied one would move further than it
    # said it would.
    settled = {tuple(entry["event_key"]) for entry in repaired}

    closable = []
    for run, tasks in _run_files(workspace):
        run_id = run.get("run_id")
        if run.get("lifecycle") == "closed":
            continue
        open_tasks = [task for task in tasks if task.get("lifecycle") != "closed"]
        if any((task.get("run_id"), task.get("task_id"), task.get("attempt"))
               not in settled for task in open_tasks):
            held_back.append({"run_id": run_id, "why": "a task in it is still open"})
            continue
        stamps = [_stamp(run.get("updated_at")), _stamp(run.get("created_at"))]
        for task in tasks:
            stamps += [_stamp(task.get("updated_at")), _stamp(task.get("created_at"))]
        stamps = [stamp for stamp in stamps if stamp is not None]
        if not stamps:
            held_back.append({"run_id": run_id, "why": "no usable timestamp"})
            continue
        last_activity = max(stamps)
        if now - last_activity < cutoff:
            held_back.append({"run_id": run_id, "why": "activity is recent"})
            continue
        closable.append({
            "run_id": run_id,
            "last_activity": last_activity.isoformat(),
            # Only reachable because the ledger repair above runs first.
            "after_repair": bool(open_tasks),
        })

    if apply:
        for entry in repaired:
            key = EventKey(*entry["event_key"])
            ledger_sink.append_event(workspace, ledger_sink.synthesized_stop(
                key,
                host=entry["host"],
                session_id=entry["session_id"],
                invocation_id=entry["invocation_id"],
                reason="reconciled: the stop event never arrived",
                reconciled=True,
            ))
            try:
                if registry.get_task(key).lifecycle != "closed":
                    registry.close_accepted_task(key)
            except RegistryError:
                # No task record left to close; the stop is still the repair.
                pass
        for entry in closable:
            registry.transition_run(entry["run_id"], "closed")

    return {
        "applied": apply,
        "stale_minutes": stale_minutes,
        "closable_runs": closable,
        "repaired_starts": repaired,
        "held_back": held_back,
        "unprovable": unprovable,
    }


# CLI — the dispatcher's tool: create the TaskContext at dispatch time so the
# EventKey travels with the task instead of being inferred at the end.
#
# ``claim`` is the one action a host adapter calls, and it is a process
# boundary rather than a file convention: the adapter hands over JSON on stdin
# and reads JSON on stdout, so it never needs to know this module's layout.
# ============================================================================

CLAIM_REQUIRED_FIELDS = ("workspace", "host", "session_id", "token", "tool_call_id")


def _elapsed_minutes(created_at, updated_at) -> float | None:
    """Wall-clock minutes between two registry timestamps, or None."""
    start = _stamp(created_at)
    end = _stamp(updated_at)
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 60.0


FACTS_TIMEOUT_SECONDS = 10.0
# Asking the adapter for its usage is a second start-up but no host read, so it
# is bounded the same way the facts read itself is.
USAGE_TIMEOUT_SECONDS = 10.0


def _adapter_action_is_known(script: str, action: str) -> bool:
    """Whether the adapter's own usage advertises ``action``.

    This is the fact an exit code cannot give. A host that never had the action
    and a host whose action failed both exit non-zero, and argparse's usage
    error is code 2 -- the same 2 that "no such action" produces -- so the
    failure cannot be read directly as an answer. The adapter is asked what it
    accepts instead, by the `--help` every argparse CLI already answers.

    Fail-safe by design: when the usage cannot be had at all (no `--help`, a
    crash, a non-zero exit) the answer is "cannot tell" (``True``), which keeps
    the loud ``auto-failed`` verdict. A broken adapter must never be able to
    pass itself off as a host with no contract -- that silent skip is exactly
    what this distinction exists to prevent.
    """
    try:
        proc = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=USAGE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if proc.returncode != 0:
        return True
    listed = f"{proc.stdout}\n{proc.stderr}"
    return re.search(rf"\b{re.escape(action)}\b", listed) is not None


def _host_facts(host: str, role: str) -> tuple[str, dict | None]:
    """The configuration snapshot, asked of the host adapter for *this* host.

    The shared core must not learn a host's name, let alone its layout, so it
    cannot derive the digest itself -- it asks the adapter that already does, by
    the one convention every host can meet: a `<host>-role-configure.py` beside
    this script that publishes a `facts` action. The dispatcher used to do this
    by hand and sometimes skipped it, which is how an order came to be recorded
    with no snapshot and no word said about it.

    Returns `(source, facts)`:

    * `"auto"` -- the adapter answered, and `facts` is what it said;
    * `"auto-failed"` -- the adapter was asked and could not answer;
    * `"none"` -- this host publishes no `facts` action, so there is nothing to
      take. Not the same fact as a read that failed, and it must not be
      reported as one.

    A non-zero exit is split by a question, never by its code: only an adapter
    whose own usage shows no `facts` action lands on `"none"`. A crash, a usage
    error on the arguments we passed, or a changed flag therefore surfaces as
    `"auto-failed"` instead of a silently skipped snapshot. Code 2 used to stand
    in for "no contract", but 2 is also argparse's usage error, so a real
    failure was indistinguishable from an absent action.

    The adapter memoizes the CLI observations behind `facts`, so a warm order
    costs milliseconds and only a cold one pays a start-up -- the fast path is
    not paid for on every order.
    """
    if not host or not role:
        return "none", None
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          f"{host}-role-configure.py")
    if not os.path.isfile(script):
        return "none", None
    try:
        proc = subprocess.run(
            [sys.executable, script, "facts", "--role", role],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=FACTS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return "auto-failed", None
    if proc.returncode != 0:
        if _adapter_action_is_known(script, "facts"):
            return "auto-failed", None
        return "none", None
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return "auto-failed", None
    return ("auto", payload) if isinstance(payload, dict) else ("auto-failed", None)


def _print_budget(registry, keys) -> None:
    """Say an overrun out loud, and do nothing else about it.

    Nothing in this system kills a worker for spending too much, so the only
    thing between an expensive dispatch and nobody noticing is this line. A
    dispatch that never had a budget is reported separately: "no budget" and
    "inside budget" are different facts, and only one of them is reassuring.
    """
    for key in keys:
        status = registry.budget_status(key)
        for row in status["over_budget"]:
            print(f"[OVER]     {key.task_id} {row['role']}: 预算 {row['budget']:,} "
                  f"实花 {row['spent']:,} token (+{row['over_percent']}%)")
        unbudgeted = status["unbudgeted"]
        if unbudgeted:
            total = sum(row["spent"] for row in unbudgeted)
            print(f"[INFO]     {key.task_id}: {len(unbudgeted)} 次派发未设预算，"
                  f"共 {total:,} token")
        for row in registry.time_status(key)["over_time"]:
            print(f"[OVER]     {key.task_id} {row['role']}: 预算 {row['budget']} 分钟 "
                  f"实花 {row['spent']} 分钟 (+{row['over_percent']}%)")
        for row in registry.call_status(key)["over_calls"]:
            print(f"[OVER]     {key.task_id} {row['role']}: "
                  f"call_budget={row['call_budget']}, 实际={row['spent']}, "
                  f"超支={row['over']}")


def _claim_from_stdin() -> int:
    """Claim an invocation from a JSON request on stdin.

    stdout is always a single JSON object. Failure is a non-zero exit code, so
    a caller that ignores stdout still cannot mistake a failure for a claim.
    """
    # The request is read as UTF-8, so the answer must leave as UTF-8 too -- and
    # on a Windows console it otherwise would not. This is a process boundary
    # with a fixed encoding on the other side, so the payload is emitted with
    # ASCII escapes: no codepage can corrupt an ASCII byte stream, whatever this
    # process's stdout happens to be. A Chinese task id that arrived as U+FFFD
    # would be stored that way in the ledger, and the proof could no longer
    # resolve the invocation the ledger names.
    def fail(message: str, code: int = 1) -> int:
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=True))
        return code

    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - stdin is either readable or not
        return fail(f"cannot read request: {exc}", 2)
    if not raw.strip():
        return fail("empty request", 2)
    try:
        request = json.loads(raw)
    except ValueError:
        return fail("request is not valid JSON", 2)
    if not isinstance(request, dict):
        return fail("request must be a JSON object", 2)
    missing = [name for name in CLAIM_REQUIRED_FIELDS if not request.get(name)]
    if missing:
        return fail(f"missing fields: {', '.join(missing)}", 2)

    registry = RunRegistry(request["workspace"])
    try:
        result = registry.claim_invocation(
            token=str(request["token"]),
            tool_call_id=str(request["tool_call_id"]),
            host=str(request["host"]),
            session_id=str(request["session_id"]),
            role=str(request.get("role") or ""),
        )
    except RegistryError as exc:
        return fail(str(exc), 1)
    except Exception as exc:  # pragma: no cover - defensive: never leak a traceback
        return fail(f"claim failed: {exc}", 1)

    invocation = result.invocation
    print(json.dumps({
        "ok": True,
        "reused": result.reused,
        "run_id": invocation.run_id,
        "task_id": invocation.task_id,
        "attempt": invocation.attempt,
        "invocation_id": invocation.invocation_id,
        "tool_call_id": invocation.tool_call_id,
        "role": invocation.role,
        # The dispatch-time binding, so the adapter reports the fact the shared
        # core recorded instead of re-reading the role file when the event lands.
        "model": invocation.model,
        "model_source": invocation.model_source,
        "subject_digest": invocation.subject_digest,
    }, ensure_ascii=True))
    return 0


def _cli() -> int:
    import argparse

    # UTF-8 stdout, the same unlock the other shared scripts use: a task may be
    # named in Chinese, and on Windows the default console encoding is not UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, LookupError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Tianji run/task identity registry")
    parser.add_argument(
        "action",
        choices=["open", "close-run", "close-task", "show", "claim", "refresh",
                 "reconcile"],
    )
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--host", default="")
    parser.add_argument("--session", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--subject-digest", default="",
                        help="configuration snapshot digest seen at dispatch time")
    parser.add_argument("--model", default="",
                        help="binding seen at dispatch time")
    parser.add_argument("--model-source", default="",
                        help="how that binding was chosen (declared/inherit/...)")
    parser.add_argument("--invocation-id", default="",
                        help="refresh: which pending invocation to re-token")
    parser.add_argument("--token-budget", type=int, default=0,
                        help="tokens this dispatch may spend (0 = none set)")
    parser.add_argument("--time-budget", type=int, default=0,
                        help="minutes this dispatch may run (0 = none set)")
    parser.add_argument("--call-budget", type=int, default=0,
                        help="tool calls this dispatch may make (0 = none set)")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="open: skip the configuration snapshot on purpose")
    parser.add_argument("--ttl", type=float, default=claim.DEFAULT_TTL_SECONDS,
                        help="seconds a claim token stays usable")
    parser.add_argument("--stale-minutes", type=int, default=RECONCILE_STALE_MINUTES,
                        help="idle time before reconcile considers something over")
    parser.add_argument("--apply", action="store_true",
                        help="reconcile: write the repair instead of only reporting it")
    args = parser.parse_args()

    if args.action == "claim":
        return _claim_from_stdin()

    registry = RunRegistry(args.workspace)

    if args.action == "open":
        if not args.host or not args.session or not args.role:
            print("open requires --host --session --role")
            return 2
        subject_digest = args.subject_digest
        model = args.model
        model_source = args.model_source
        # Why this order carries the snapshot it carries: a digest the
        # dispatcher passed, one the host adapter just read, a deliberate skip,
        # or a read that failed. Recorded as its own field so the route proof
        # can tell a skipped snapshot from a taken one without parsing prose.
        snapshot_source = "explicit" if subject_digest else "none"
        if not subject_digest:
            if args.no_snapshot:
                snapshot_source = "omitted"
            else:
                snapshot_source, facts = _host_facts(args.host, args.role)
                if facts:
                    subject_digest = str(facts.get("subject_digest") or "")
                    model = model or str(facts.get("model") or "")
                    model_source = model_source or str(facts.get("model_source") or "")
                if not subject_digest and snapshot_source == "auto":
                    snapshot_source = "auto-failed"
        snapshot_omitted = snapshot_source in ("omitted", "auto-failed")
        run = (registry.get_run(args.run_id) if args.run_id
               else registry.create_run(host=args.host, session_id=args.session))
        task_id = args.task_id or f"{args.role}-{uuid.uuid4().hex[:8]}"
        task = registry.create_task(run.run_id, task_id, attempt=args.attempt, role=args.role)
        # Mint the claim token now; the host call id does not exist yet, and the
        # marker carrying this token is what lets the start event claim it.
        invocation, token = registry.create_pending_invocation(
            task.event_key, host=args.host, session_id=args.session, role=args.role,
            subject_digest=subject_digest, ttl_seconds=args.ttl,
            model=model, model_source=model_source,
            token_budget=args.token_budget,
            time_budget_minutes=args.time_budget,
            call_budget=args.call_budget,
            snapshot_omitted=snapshot_omitted,
            snapshot_source=snapshot_source,
        )
        if snapshot_omitted:
            print("本单未带配置快照，路由证明不会采信它", file=sys.stderr)
        print(json.dumps({
            "run_id": run.run_id,
            "task_id": task.task_id,
            "attempt": task.attempt,
            "role": args.role,
            "invocation_id": invocation.invocation_id,
            "claim_token": token,
            "marker": claim.format_marker(token),
            "subject_digest": invocation.subject_digest,
            "model": invocation.model,
            "model_source": invocation.model_source,
            "call_budget": invocation.call_budget,
            "snapshot_omitted": invocation.snapshot_omitted,
            "snapshot_source": invocation.snapshot_source,
            "event_key": task.event_key.as_dict(),
        }, ensure_ascii=False))
        return 0

    if args.action == "close-run":
        keys = [EventKey(args.run_id, task["task_id"], task["attempt"])
                for run, tasks in _run_files(registry.workspace_root)
                if run.get("run_id") == args.run_id
                for task in tasks]
        print(json.dumps(asdict(registry.transition_run(args.run_id, "closed")), ensure_ascii=False))
        _print_budget(registry, keys)
        return 0

    if args.action == "close-task":
        if not args.run_id or not args.task_id:
            print("close-task requires --run-id --task-id")
            return 2
        key = EventKey(args.run_id, args.task_id, args.attempt)
        closed, released = registry.release_task(key)
        result = asdict(closed)
        result["released_bindings"] = released
        print(json.dumps(result, ensure_ascii=False))
        _print_budget(registry, [key])
        return 0

    if args.action == "refresh":
        if not args.run_id or not args.task_id:
            print("refresh requires --run-id --task-id", file=sys.stderr)
            return 2
        key = EventKey(args.run_id, args.task_id, args.attempt)
        try:
            invocation, token = registry.refresh_claim_token(
                key, invocation_id=args.invocation_id, ttl_seconds=args.ttl,
            )
        except RegistryError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({
            "run_id": key.run_id,
            "task_id": key.task_id,
            "attempt": key.attempt,
            "invocation_id": invocation.invocation_id,
            "claim_token": token,
            "marker": claim.format_marker(token),
            "ttl_seconds": args.ttl,
        }, ensure_ascii=False))
        return 0

    if args.action == "reconcile":
        report = reconcile(args.workspace, stale_minutes=args.stale_minutes,
                           apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.action == "show":
        print(json.dumps(asdict(registry.get_run(args.run_id)), ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
