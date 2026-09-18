"""The shared lock protocol, exercised the way the runtime uses it."""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lock_protocol  # noqa: E402
from lock_protocol import FileLock, LockOrderError, LockPermissionError  # noqa: E402


def dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    return process.pid


class StalenessTests(unittest.TestCase):
    def test_a_live_holder_on_this_host_is_not_stale(self):
        info = lock_protocol.lock_payload(lease_seconds=3600.0)
        stale, _reason = lock_protocol.staleness(info)
        self.assertFalse(stale)

    def test_a_dead_holder_is_stale_even_within_its_lease(self):
        info = {"pid": dead_pid(), "host": lock_protocol.hostname(),
                "acquired_at": time.time(), "lease_expires_at": time.time() + 3600}
        stale, reason = lock_protocol.staleness(info)
        self.assertTrue(stale)
        self.assertIn("gone", reason)

    def test_an_expired_lease_is_stale_on_any_host(self):
        info = {"pid": 4242, "host": "somewhere-else",
                "acquired_at": time.time() - 7200,
                "lease_expires_at": time.time() - 3600}
        stale, reason = lock_protocol.staleness(info)
        self.assertTrue(stale)
        self.assertIn("lease", reason)

    def test_a_remote_holder_within_its_lease_is_respected(self):
        info = {"pid": 4242, "host": "somewhere-else",
                "acquired_at": time.time(), "lease_expires_at": time.time() + 3600}
        self.assertFalse(lock_protocol.staleness(info)[0])

    def test_mtime_alone_never_makes_a_lock_stale(self):
        # A slow holder whose pid is alive must not be stolen from.
        info = lock_protocol.lock_payload(lease_seconds=3600.0)
        stale, _reason = lock_protocol.staleness(info, now=time.time() + 3599)
        self.assertFalse(stale)

    def test_an_unreadable_payload_is_stale(self):
        for info in (None, {}, {"pid": "nonsense"}, {"host": "h"}):
            self.assertTrue(lock_protocol.staleness(info)[0], info)


class FileLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-lock-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "state.lock"

    def test_a_lock_round_trips_its_payload(self):
        with FileLock(self.path, kind="ledger"):
            info = lock_protocol.read_payload(self.path)
            self.assertIsInstance(info, dict)
            self.assertEqual(info["pid"], __import__("os").getpid())
        self.assertFalse(self.path.exists())

    def test_a_live_holder_is_waited_for_then_timed_out(self):
        info = lock_protocol.lock_payload(lease_seconds=3600.0)
        self.path.write_text(json.dumps(info), encoding="utf-8")
        with self.assertRaises(TimeoutError):
            with FileLock(self.path, kind="ledger", timeout=0.1):
                pass

    def test_a_fresh_unreadable_lock_is_not_stolen(self):
        # The holder may be between O_EXCL create and its payload write.
        self.path.write_text("", encoding="utf-8")
        with self.assertRaises(TimeoutError):
            with FileLock(self.path, kind="ledger", timeout=0.1):
                pass

    def test_an_old_unreadable_lock_is_reclaimed(self):
        self.path.write_text("abandoned", encoding="utf-8")
        old = time.time() - 3600
        __import__("os").utime(self.path, (old, old))
        with FileLock(self.path, kind="ledger", timeout=1.0):
            pass

    def test_a_dead_holders_lock_is_reclaimed(self):
        self.path.write_text(json.dumps({
            "pid": dead_pid(), "host": lock_protocol.hostname(),
            "acquired_at": time.time(), "lease_expires_at": time.time() + 3600,
        }), encoding="utf-8")
        with FileLock(self.path, kind="ledger", timeout=1.0):
            pass

    def test_a_permission_failure_is_loud(self):
        with mock.patch.object(lock_protocol.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(LockPermissionError):
                with FileLock(self.path, kind="ledger", timeout=0.1):
                    pass

    def test_holders_never_overlap(self):
        guard = threading.Lock()
        state = {"current": 0, "max": 0, "entered": 0}

        def worker(_index):
            with FileLock(self.path, kind="ledger", timeout=10.0):
                with guard:
                    state["current"] += 1
                    state["entered"] += 1
                    state["max"] = max(state["max"], state["current"])
                time.sleep(0.005)
                with guard:
                    state["current"] -= 1
            return True

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(worker, range(40)))
        self.assertEqual(state["entered"], 40)
        self.assertEqual(state["max"], 1, "two holders overlapped")

    def test_release_leaves_no_lock_behind_under_churn(self):
        def worker(_index):
            with FileLock(self.path, kind="ledger", timeout=10.0):
                return True

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(worker, range(40)))
        self.assertFalse(self.path.exists())


class LockOrderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-order-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _lock(self, kind):
        return FileLock(self.root / f"{kind}.lock", kind=kind, timeout=1.0)

    def test_outermost_first_is_allowed(self):
        with self._lock("installer"):
            with self._lock("registry"):
                with self._lock("ledger"):
                    pass

    def test_taking_an_outer_lock_while_holding_an_inner_one_is_refused(self):
        with self._lock("ledger"):
            with self.assertRaises(LockOrderError):
                with self._lock("registry"):
                    pass

    def test_taking_the_same_kind_twice_is_refused(self):
        with self._lock("registry"):
            with self.assertRaises(LockOrderError):
                with self._lock("registry"):
                    pass

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(LockOrderError):
            with FileLock(self.root / "x.lock", kind="whatever", timeout=0.1):
                pass

    def test_the_order_is_released_after_the_outer_lock_exits(self):
        with self._lock("registry"):
            pass
        # Nothing held: taking the ledger lock alone must be legal again.
        with self._lock("ledger"):
            pass


class LedgerCrossProcessTests(unittest.TestCase):
    """Two processes appending must never interleave or lose a line."""

    WRITER = """
import sys
sys.path.insert(0, r"{scripts}")
from ledger_sink import append_event

cwd, tag = sys.argv[1], sys.argv[2]
for index in range(int(sys.argv[3])):
    append_event(cwd, {{
        "schema_version": 2,
        "event_id": f"{{tag}}-{{index}}",
        "event": "subagent_start",
        "host": "cmdc",
        "run_id": "77802c0c-d50f-4fe8-bc09-8ea018cf2334",
        "session_id": "session-1",
        "task_id": "task-a",
        "attempt": 1,
        "invocation_id": "inv-1",
        "correlation_id": f"{{tag}}-{{index}}",
        "agent": "tianji-worker",
        "occurred_at": "2026-09-10T12:00:00+00:00",
        "recorded_at": "2026-09-10T12:00:00+00:00",
        "detail": {{}},
    }})
"""

    def test_concurrent_processes_neither_interleave_nor_lose_lines(self):
        temporary = tempfile.TemporaryDirectory(prefix="tianji-xproc-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        writers = 4
        per_writer = 12
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", self.WRITER.format(scripts=str(SCRIPTS)),
                 str(root), f"w{index}", str(per_writer)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for index in range(writers)
        ]
        for process in processes:
            _out, err = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, err)

        ledger = root / ".tianji" / "state.jsonl"
        lines = ledger.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), writers * per_writer)
        ids = []
        for line in lines:
            record = json.loads(line)  # a half-written line would raise here
            ids.append(record["event_id"])
        self.assertEqual(len(set(ids)), writers * per_writer)
        self.assertFalse((root / ".tianji" / "state.lock").exists())


class CrossLanguageLockTests(unittest.TestCase):
    """The TypeScript writer must be judged by this protocol, not by its own."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-crosslock-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # The mod locks the ledger's own file, under .tianji/.
        self.path = self.root / ".tianji" / "state.lock"

    def _write_runner(self, script: str) -> Path:
        """Write a snippet against the real mod, as a file so imports resolve."""
        mod = (ROOT / "skills" / "tianji" / "host_adapters" / "cmdc" / "mod"
               / "tianji-state.ts").as_uri()
        runner = self.root / "check.mts"
        runner.write_text(
            f"import {{ withLedgerLock }} from '{mod}';\n"
            f"import * as fs from 'node:fs';\n"
            f"const workspace = String.raw`{self.root}`;\n"
            f"const lock = String.raw`{self.path}`;\n"
            + script,
            encoding="utf-8",
        )
        return runner

    def _run_mod(self, script: str) -> str:
        node = shutil.which("node") or "node"
        result = subprocess.run(
            [node, str(self._write_runner(script))],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_a_lock_the_mod_wrote_is_read_as_a_lock_by_python(self):
        # The mod publishes the same payload; while its holder is alive, Python
        # must agree it is a live lock rather than reclaiming it.
        runner = self._write_runner(
            "let seen = '';\n"
            "withLedgerLock(workspace, () => {\n"
            "  seen = fs.readFileSync(lock, 'utf-8');\n"
            "  process.stdout.write(seen + '\\n');\n"
            "  const buffer = Buffer.alloc(1);\n"
            "  try { fs.readSync(0, buffer, 0, 1, null); } catch { /* released */ }\n"
            "  return true;\n"
            "});\n"
        )
        node = shutil.which("node") or "node"
        holder = subprocess.Popen(
            [node, str(runner)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            info = json.loads(holder.stdout.readline().strip())
            self.assertEqual(info["pid"], holder.pid)
            self.assertTrue(info["host"])
            self.assertIsInstance(info["lease_expires_at"], (int, float))
            stale, reason = lock_protocol.staleness(info)
            self.assertFalse(stale, reason)
        finally:
            try:
                holder.stdin.write("go\n")
                holder.stdin.flush()
            except (OSError, ValueError):
                pass
            holder.wait(timeout=60)

    def test_a_python_lock_is_respected_by_the_mod(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(lock_protocol.lock_payload(lease_seconds=3600.0)),
            encoding="utf-8",
        )
        outcome = self._run_mod(
            "const written = withLedgerLock(workspace, () => 'wrote');\n"
            "process.stdout.write(written === null ? 'blocked' : 'wrote');\n"
        )
        self.assertEqual(outcome, "blocked")


if __name__ == "__main__":
    unittest.main()
