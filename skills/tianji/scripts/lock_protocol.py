#!/usr/bin/env python3
"""One lock protocol for the Tianji runtime, shared by every language.

Three locks protect three *different* resources, and they stay separate:
``registry`` guards run/task/invocation/claim state, ``ledger`` guards appends
to ``state.jsonl``, and ``installer`` guards install transactions. Merging them
into one global lock would serialize unrelated work and couple their failures.

What is shared is the *protocol*, so a writer in any language can contend on
the same lock honestly:

* the lock file holds a JSON payload (``pid``, ``host``, ``acquired_at``,
  ``lease_expires_at``);
* a lock is stale when the holder's process is gone **or** its lease has
  expired -- never on mtime alone, which cannot tell a slow holder from a dead
  one;
* liveness is only checkable on the same host, so a holder elsewhere falls back
  to its lease;
* locks are always acquired outermost first: installer -> registry -> ledger.
  A process that already holds a lock may only take one further down the order,
  which makes deadlock between the three impossible.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path


# Outermost first. Acquiring a lock that is not strictly below every lock the
# current thread already holds is a programming error, not a race to wait out.
LOCK_ORDER = ("installer", "registry", "ledger")

LOCK_TIMEOUT = 5.0
# Holders do short critical work, so one generous lease replaces per-operation
# heartbeats; a long-running holder calls refresh() instead of holding a lock
# past its lease.
LOCK_LEASE_SECONDS = 60.0
# A lock file exists a moment before its payload lands. An unreadable lock
# younger than this is being written, not abandoned -- stealing it here would
# let two holders in at once.
UNREADABLE_GRACE_SECONDS = 1.0
# Releasing means deleting the lock file, and on Windows that fails while any
# contender still holds the file open to read it. Contenders are short-lived,
# so a bounded retry clears the window instead of leaving a lock behind.
UNLINK_ATTEMPTS = 200
UNLINK_RETRY_SECONDS = 0.005


class LockOrderError(RuntimeError):
    """Raised when locks are acquired in an order that could deadlock."""


class LockError(RuntimeError):
    """Base class for lock acquisition failures."""


class LockTimeoutError(LockError, TimeoutError):
    """Raised when the lock could not be acquired before the deadline."""


class LockPermissionError(LockError):
    """Raised when the lock exists but cannot be created for real reasons."""


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def pid_alive(pid: int) -> bool:
    """True when the process still exists.

    Unknown answers report alive: stealing a lock from a live holder corrupts
    the resource, while waiting costs only time, so failures err toward waiting.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


def lock_payload(*, pid: int | None = None, lease_seconds: float = LOCK_LEASE_SECONDS,
                 now: float | None = None) -> dict:
    """The payload a holder writes into its lock file."""
    moment = time.time() if now is None else now
    return {
        "pid": os.getpid() if pid is None else pid,
        "host": hostname(),
        "acquired_at": moment,
        "lease_expires_at": moment + lease_seconds,
    }


def read_payload(lock_path: Path) -> dict | None:
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def staleness(info: dict | None, *, now: float | None = None,
              host: str | None = None) -> tuple[bool, str]:
    """Return ``(stale, reason)`` for a lock payload.

    An unreadable payload counts as stale: a lock nobody can describe is a lock
    nobody can honor.
    """
    moment = time.time() if now is None else now
    if not isinstance(info, dict):
        return True, "lock payload is unreadable"
    try:
        pid = int(info.get("pid"))
    except (TypeError, ValueError):
        return True, "lock payload has no pid"
    lock_host = str(info.get("host") or "")
    local_host = hostname() if host is None else host
    if lock_host and lock_host == local_host and not pid_alive(pid):
        return True, f"holder pid {pid} is gone"
    lease = info.get("lease_expires_at")
    if isinstance(lease, (int, float)) and not isinstance(lease, bool):
        if moment > float(lease):
            return True, "holder lease expired"
    return False, ""


_held = threading.local()


def _held_kinds() -> tuple[str, ...]:
    return tuple(getattr(_held, "kinds", ()))


def _assert_order(kind: str) -> None:
    if kind not in LOCK_ORDER:
        raise LockOrderError(f"unknown lock kind {kind!r}")
    rank = LOCK_ORDER.index(kind)
    # A new lock must sit strictly below every lock already held; taking an
    # outer lock while holding an inner one is the deadlock-prone direction.
    for held in _held_kinds():
        if rank <= LOCK_ORDER.index(held):
            raise LockOrderError(
                f"lock {kind!r} must be acquired after (below) {held!r}; "
                f"fixed order is {' -> '.join(LOCK_ORDER)}"
            )


class FileLock:
    """Cross-process lock honoring the shared protocol.

    Contention and Windows' transient delete-pending state both surface as
    errors on creation, so both wait -- but always against a deadline, so a
    persistent failure ends as a clear timeout instead of a spin.
    """

    def __init__(self, path: Path, *, kind: str, timeout: float = LOCK_TIMEOUT,
                 lease_seconds: float = LOCK_LEASE_SECONDS):
        self.path = Path(path)
        self.kind = kind
        self.timeout = timeout
        self.lease_seconds = lease_seconds

    def _try_acquire(self) -> bool:
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._reap_if_stale()
            return False
        except PermissionError:
            # Windows surfaces a lock being released as a permission error. Only
            # an unwritable directory is a real failure.
            if not self._directory_is_writable():
                raise LockPermissionError(f"{self.kind} lock is not writable: {self.path}")
            self._reap_if_stale()
            return False
        try:
            payload = json.dumps(lock_payload(lease_seconds=self.lease_seconds))
            os.write(descriptor, payload.encode("utf-8"))
        finally:
            os.close(descriptor)
        return True

    def _directory_is_writable(self) -> bool:
        """Probe the lock directory directly.

        The lock file itself cannot answer this: on Windows a release in flight
        reports the same permission error as an unwritable directory. A
        uniquely-named probe settles it without guessing at timing.
        """
        probe = self.path.with_name(f".{self.path.name}.probe-{uuid.uuid4().hex}")
        try:
            descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except PermissionError:
            return False
        except OSError:
            # Not a permission verdict; assume writable so the caller waits on
            # the lock instead of aborting on an unrelated error.
            return True
        try:
            os.close(descriptor)
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        return True

    def _unlink(self) -> None:
        """Delete the lock file, retrying past Windows sharing violations."""
        for _attempt in range(UNLINK_ATTEMPTS):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(UNLINK_RETRY_SECONDS)

    def _reap_if_stale(self) -> None:
        info = read_payload(self.path)
        if info is None:
            # The holder may be in the window between creating the lock file and
            # writing its payload. Only an unreadable lock old enough to have
            # been abandoned may be reclaimed.
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return
            if age < UNREADABLE_GRACE_SECONDS:
                return
        stale, _reason = staleness(info)
        if not stale:
            return
        self._unlink()

    def __enter__(self) -> "FileLock":
        _assert_order(self.kind)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_acquire():
                break
            if time.monotonic() >= deadline:
                raise LockTimeoutError(f"{self.kind} lock timed out: {self.path}")
            time.sleep(0.01)
        kinds = getattr(_held, "kinds", ())
        _held.kinds = kinds + (self.kind,)
        self._entered = True
        return self

    def refresh(self) -> None:
        """Extend the lease; for holders whose critical section runs long."""
        try:
            self.path.write_text(
                json.dumps(lock_payload(lease_seconds=self.lease_seconds)),
                encoding="utf-8",
            )
        except OSError:
            pass

    def __exit__(self, *_exc) -> None:
        kinds = tuple(k for k in _held_kinds() if k != self.kind)
        _held.kinds = kinds
        self._unlink()
