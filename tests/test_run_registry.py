import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import claim  # noqa: E402
import lock_protocol  # noqa: E402
from ledger_schema import EventKey  # noqa: E402
from run_registry import RegistryError, RunRegistry  # noqa: E402


def dead_pid() -> int:
    """A PID that is certainly not running: a process we started and reaped."""
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    return process.pid


class RunRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = RunRegistry(self.root)

    def _pending(self, task, *, role="tianji-worker", session="session-1",
                 host="cmdc", **kwargs):
        return self.registry.create_pending_invocation(
            task.event_key, host=host, session_id=session, role=role, **kwargs,
        )

    def _only_invocation_id(self, key):
        directory = self.registry._attempt_dir(key) / "invocations"
        return next(directory.glob("*.json")).stem

    def test_same_session_can_own_distinct_runs_without_current_pointer(self):
        first = self.registry.create_run(host="codex", session_id="session-1")
        second = self.registry.create_run(host="codex", session_id="session-1")
        uuid.UUID(first.run_id)
        uuid.UUID(second.run_id)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertFalse(hasattr(self.registry, "current"))
        self.assertFalse(hasattr(self.registry, "current_run"))

    def test_parallel_tasks_claim_their_own_invocations(self):
        run = self.registry.create_run(host="cmdc", session_id="session-1")
        task_a = self.registry.create_task(run.run_id, "task-a", attempt=1, role="tianji-worker")
        task_b = self.registry.create_task(run.run_id, "task-b", attempt=1, role="tianji-verifier")
        _, token_a = self._pending(task_a, role="tianji-worker")
        _, token_b = self._pending(task_b, role="tianji-verifier")

        claimed_a = self.registry.claim_invocation(
            token=token_a, tool_call_id="call-a", host="cmdc", session_id="session-1",
        )
        claimed_b = self.registry.claim_invocation(
            token=token_b, tool_call_id="call-b", host="cmdc", session_id="session-1",
        )
        self.assertNotEqual(
            claimed_a.invocation.invocation_id, claimed_b.invocation.invocation_id,
        )
        self.assertEqual(
            self.registry.resolve_binding(
                host="cmdc", session_id="session-1", correlation_id="call-a",
            ).event_key,
            EventKey(run.run_id, "task-a", 1),
        )
        self.assertEqual(
            self.registry.resolve_binding(
                host="cmdc", session_id="session-1", correlation_id="call-b",
            ).event_key,
            EventKey(run.run_id, "task-b", 1),
        )

    def test_one_task_attempt_can_have_multiple_invocations(self):
        run = self.registry.create_run(host="cmdc", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a", attempt=1)
        one, _ = self._pending(task)
        two, _ = self._pending(task)
        self.assertNotEqual(one.invocation_id, two.invocation_id)
        self.assertEqual(one.event_key, two.event_key)

    def test_a_pending_invocation_has_no_call_id_until_claimed(self):
        run = self.registry.create_run(host="cmdc", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a", role="tianji-worker")
        invocation, token = self._pending(task)
        self.assertEqual(invocation.lifecycle, "pending")
        self.assertEqual(invocation.tool_call_id, "")
        # Only the digest is persisted; the raw capability is never stored.
        stored = "".join(
            path.read_text(encoding="utf-8")
            for path in (self.root / ".tianji" / "runtime").rglob("*.json")
        )
        self.assertNotIn(token, stored)
        self.assertIn(claim.token_digest(token), stored)

    def test_a_closed_task_releases_its_indexes_for_the_next_dispatch(self):
        run = self.registry.create_run(host="cmdc", session_id="session-1")
        first = self.registry.create_task(run.run_id, "task-a", role="tianji-worker")
        _, token = self._pending(first)
        self.registry.claim_invocation(
            token=token, tool_call_id="call-a", host="cmdc", session_id="session-1",
        )

        closed, released = self.registry.release_task(first.event_key)

        self.assertEqual(closed.lifecycle, "closed")
        self.assertEqual(released, 1)
        self.assertEqual(
            self.registry.get_invocation(
                first.event_key, self._only_invocation_id(first.event_key),
            ).lifecycle,
            "stopped",
        )
        with self.assertRaises(RegistryError):
            self.registry.resolve_binding(
                host="cmdc", session_id="session-1", correlation_id="call-a",
            )

        second = self.registry.create_task(run.run_id, "task-b", role="tianji-worker")
        _, token2 = self._pending(second)
        claimed = self.registry.claim_invocation(
            token=token2, tool_call_id="call-b", host="cmdc", session_id="session-1",
        )
        self.assertEqual(claimed.invocation.event_key, EventKey(run.run_id, "task-b", 1))

    def test_closing_a_task_drops_its_unconsumed_token(self):
        run = self.registry.create_run(host="cmdc", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a", role="tianji-worker")
        _, token = self._pending(task)
        self.registry.release_task(task.event_key)
        # A marker that never arrived must not claim a closed task.
        with self.assertRaises(RegistryError):
            self.registry.claim_invocation(
                token=token, tool_call_id="call-late",
                host="cmdc", session_id="session-1",
            )

    def test_closing_a_run_releases_its_call_ids(self):
        run = self.registry.create_run(host="cmdc", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a", role="tianji-worker")
        _, token = self._pending(task)
        self.registry.claim_invocation(
            token=token, tool_call_id="call-a", host="cmdc", session_id="session-1",
        )
        self.registry.transition_run(run.run_id, "closed")
        with self.assertRaises(RegistryError):
            self.registry.resolve_binding(
                host="cmdc", session_id="session-1", correlation_id="call-a",
            )

    def test_retry_changes_attempt_but_resume_preserves_run_id(self):
        run = self.registry.create_run(host="codex", session_id="session-1")
        first = self.registry.create_task(run.run_id, "task-a", attempt=1)
        retry = self.registry.create_task(run.run_id, "task-a", attempt=2)
        self.assertEqual(first.event_key.run_id, retry.event_key.run_id)
        self.assertNotEqual(first.event_key, retry.event_key)
        self.registry.transition_run(run.run_id, "aborted")
        resumed = self.registry.resume_run(run.run_id)
        self.assertEqual(resumed.run_id, run.run_id)
        self.assertEqual(resumed.lifecycle, "active")

    def test_binding_by_role_is_gone(self):
        # Role fallback is removed, not lowered: only a call id resolves.
        run = self.registry.create_run(host="cmdc", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a", role="tianji-worker")
        _, token = self._pending(task)
        self.registry.claim_invocation(
            token=token, tool_call_id="call-a", host="cmdc", session_id="session-1",
        )
        with self.assertRaises(RegistryError):
            self.registry.resolve_binding(
                host="cmdc", session_id="session-1", agent_id="tianji-worker",
            )
        with self.assertRaises(RegistryError):
            self.registry.resolve_binding(host="cmdc", session_id="session-1")

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(RegistryError):
            self.registry.create_run(host="../outside", session_id="session-1")
        run = self.registry.create_run(host="codex", session_id="session-1")
        with self.assertRaises(RegistryError):
            self.registry.create_task(run.run_id, "../outside")
        for hostile in ("..", ".", "a/b", "a\\b", "a\uFF0Fb", "a\uFF0Eb", "a\u2024b"):
            with self.assertRaises(RegistryError):
                self.registry.create_task(run.run_id, hostile)

    def test_a_task_may_be_named_in_chinese(self):
        run = self.registry.create_run(host="cmdc", session_id="session-1")
        task = self.registry.create_task(run.run_id, "整理接口文档")
        self.assertEqual(task.task_id, "整理接口文档")
        self.assertEqual(
            self.registry.get_task(task.event_key).task_id, "整理接口文档",
        )

    def test_lock_contention_times_out_instead_of_spinning(self):
        # A live holder is not stale, however long we wait.
        runtime = self.root / ".tianji" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / ".registry.lock").write_text(
            json.dumps(lock_protocol.lock_payload(lease_seconds=3600.0)),
            encoding="utf-8",
        )
        contended = RunRegistry(self.root, lock_timeout=0.05, lease_seconds=3600.0)
        with self.assertRaises(RegistryError):
            contended.create_run(host="codex", session_id="session-1")

    def test_a_dead_holder_is_reclaimed_even_within_its_lease(self):
        runtime = self.root / ".tianji" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / ".registry.lock").write_text(json.dumps({
            "pid": dead_pid(), "host": lock_protocol.hostname(),
            "acquired_at": time.time(), "lease_expires_at": time.time() + 3600,
        }), encoding="utf-8")
        reclaimed = RunRegistry(self.root, lock_timeout=1.0)
        uuid.UUID(reclaimed.create_run(host="codex", session_id="session-1").run_id)
        self.assertFalse((runtime / ".registry.lock").exists())

    def test_an_expired_lease_is_reclaimed(self):
        runtime = self.root / ".tianji" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / ".registry.lock").write_text(json.dumps({
            "pid": 4242, "host": "somewhere-else",
            "acquired_at": time.time() - 7200, "lease_expires_at": time.time() - 3600,
        }), encoding="utf-8")
        reclaimed = RunRegistry(self.root, lock_timeout=1.0)
        uuid.UUID(reclaimed.create_run(host="codex", session_id="session-1").run_id)

    def test_an_old_unreadable_lock_is_treated_as_stale(self):
        runtime = self.root / ".tianji" / "runtime"
        runtime.mkdir(parents=True)
        lock = runtime / ".registry.lock"
        lock.write_text("abandoned", encoding="utf-8")
        # A fresh unreadable lock is mid-write; only an old one is abandoned.
        old = time.time() - 3600
        os.utime(lock, (old, old))
        reclaimed = RunRegistry(self.root, lock_timeout=1.0)
        uuid.UUID(reclaimed.create_run(host="codex", session_id="session-1").run_id)

    def test_a_fresh_unreadable_lock_is_not_stolen(self):
        runtime = self.root / ".tianji" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / ".registry.lock").write_text("", encoding="utf-8")
        contended = RunRegistry(self.root, lock_timeout=0.1)
        with self.assertRaises(RegistryError):
            contended.create_run(host="codex", session_id="session-1")

    def test_lock_permission_failure_fails_loudly(self):
        with mock.patch.object(lock_protocol.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(RegistryError):
                self.registry.create_run(host="codex", session_id="session-1")

    def test_concurrent_writes_leave_only_valid_json(self):
        run = self.registry.create_run(host="codex", session_id="session-1")

        def create(index):
            return self.registry.create_task(run.run_id, f"task-{index}")

        with ThreadPoolExecutor(max_workers=8) as executor:
            tasks = list(executor.map(create, range(24)))
        self.assertEqual(len(tasks), 24)
        runtime = self.root / ".tianji" / "runtime"
        for path in runtime.rglob("*.json"):
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)


class ClaimInvocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-claim-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = RunRegistry(self.root)
        self.run = self.registry.create_run(host="cmdc", session_id="session-1")
        self.task = self.registry.create_task(
            self.run.run_id, "task-a", role="tianji-worker",
        )
        self.invocation, self.token = self.registry.create_pending_invocation(
            self.task.event_key, host="cmdc", session_id="session-1",
            role="tianji-worker",
        )

    def claim(self, **overrides):
        request = {
            "token": self.token, "tool_call_id": "call-a",
            "host": "cmdc", "session_id": "session-1",
        }
        request.update(overrides)
        return self.registry.claim_invocation(**request)

    def test_a_valid_token_claims_and_creates_the_call_index(self):
        result = self.claim()
        self.assertFalse(result.reused)
        self.assertEqual(result.invocation.lifecycle, "claimed")
        self.assertEqual(result.invocation.tool_call_id, "call-a")
        self.assertTrue(result.invocation.claimed_at)
        self.assertEqual(
            self.registry.resolve_binding(
                host="cmdc", session_id="session-1", correlation_id="call-a",
            ).invocation_id,
            self.invocation.invocation_id,
        )

    def test_retrying_the_same_token_and_call_id_is_idempotent(self):
        first = self.claim()
        second = self.claim()
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(
            first.invocation.invocation_id, second.invocation.invocation_id,
        )

    def test_one_token_cannot_claim_two_call_ids(self):
        self.claim()
        with self.assertRaises(RegistryError):
            self.claim(tool_call_id="call-b")

    def test_a_token_bound_to_another_session_is_refused(self):
        with self.assertRaises(RegistryError):
            self.claim(session_id="session-2")

    def test_a_token_bound_to_another_role_is_refused(self):
        with self.assertRaises(RegistryError):
            self.claim(role="tianji-verifier")

    def test_an_expired_token_is_refused_and_leaves_the_invocation_pending(self):
        invocation, token = self.registry.create_pending_invocation(
            self.task.event_key, host="cmdc", session_id="session-1",
            role="tianji-worker", ttl_seconds=-1.0,
        )
        with self.assertRaises(RegistryError):
            self.registry.claim_invocation(
                token=token, tool_call_id="call-expired",
                host="cmdc", session_id="session-1",
            )
        self.assertEqual(
            self.registry.get_invocation(
                self.task.event_key, invocation.invocation_id,
            ).lifecycle,
            "pending",
        )

    def age_token(self, token, *, issued_ago, expires_in):
        """Rewrite a stored token's stamps: the clock inside is not injectable."""
        import json as _json
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        now = _dt.now(_tz.utc)
        path = self.registry._claim_token_path(claim.token_digest(token))
        record = _json.loads(path.read_text(encoding="utf-8"))
        record["issued_at"] = (now - _td(seconds=issued_ago)).isoformat()
        record["expires_at"] = (now + _td(seconds=expires_in)).isoformat()
        path.write_text(_json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    def test_a_host_that_took_thirteen_minutes_can_still_claim(self):
        # The token is minted at open and the host's start event arrives when the
        # host sends it -- measured at 812s on a real host, where a 300s default
        # made the dispatch unclaimable and the work unrecoverable.
        self.age_token(self.token, issued_ago=700, expires_in=1100)

        claimed = self.claim(tool_call_id="call-slow")

        self.assertEqual(
            claimed.invocation.invocation_id, self.invocation.invocation_id,
        )

    def test_a_token_two_hours_old_is_still_refused(self):
        # Long is not unbounded: expiry keeps failing closed.
        self.age_token(self.token, issued_ago=7200, expires_in=-5400)

        with self.assertRaises(RegistryError):
            self.claim(tool_call_id="call-stale")

    def test_refresh_mints_a_new_token_for_the_same_invocation(self):
        # A late dispatch is repaired by moving the deadline, not by opening the
        # task again: a new attempt would lose the chain of what happened.
        self.age_token(self.token, issued_ago=7200, expires_in=-5400)

        refreshed, token = self.registry.refresh_claim_token(self.task.event_key)

        self.assertEqual(refreshed.invocation_id, self.invocation.invocation_id)
        claimed = self.claim(token=token, tool_call_id="call-refreshed")
        self.assertEqual(claimed.invocation.invocation_id, self.invocation.invocation_id)

    def test_refresh_refuses_an_invocation_that_was_already_claimed(self):
        self.claim()
        with self.assertRaises(RegistryError):
            self.registry.refresh_claim_token(self.task.event_key)

    def test_an_unknown_token_is_refused(self):
        with self.assertRaises(ValueError):
            self.claim(token=claim.new_token())

    def test_a_malformed_token_is_refused(self):
        for bad in ("", "short", "x" * 22 + "!", None):
            with self.assertRaises(ValueError):
                self.claim(token=bad)

    def test_claiming_requires_a_call_id(self):
        with self.assertRaises(RegistryError):
            self.claim(tool_call_id="")

    def test_a_claimed_invocation_cannot_be_claimed_again(self):
        self.claim()
        with self.assertRaises(RegistryError):
            self.registry.transition_invocation(
                self.task.event_key, self.invocation.invocation_id, "claimed",
            )

    def test_a_closed_task_cannot_be_claimed(self):
        self.registry.release_task(self.task.event_key)
        with self.assertRaises(RegistryError):
            self.claim(tool_call_id="call-late")

    def test_two_tokens_never_yield_the_same_invocation(self):
        _, other = self.registry.create_pending_invocation(
            self.task.event_key, host="cmdc", session_id="session-1",
            role="tianji-worker",
        )
        first = self.claim()
        second = self.registry.claim_invocation(
            token=other, tool_call_id="call-b",
            host="cmdc", session_id="session-1",
        )
        self.assertNotEqual(
            first.invocation.invocation_id, second.invocation.invocation_id,
        )


class ClaimCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-cli-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = RunRegistry(self.root)
        run = self.registry.create_run(host="cmdc", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a", role="tianji-worker")
        self.invocation, self.token = self.registry.create_pending_invocation(
            task.event_key, host="cmdc", session_id="session-1", role="tianji-worker",
        )

    def call(self, request):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "run_registry.py"), "claim"],
            input=json.dumps(request), text=True, capture_output=True,
            cwd=str(self.root),
        )

    def test_a_valid_request_returns_the_invocation_as_json(self):
        result = self.call({
            "workspace": str(self.root), "host": "cmdc", "session_id": "session-1",
            "token": self.token, "tool_call_id": "call-a", "role": "tianji-worker",
        })
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["invocation_id"], self.invocation.invocation_id)
        self.assertEqual(payload["tool_call_id"], "call-a")

    def test_the_claim_returns_the_binding_recorded_at_dispatch(self):
        # The adapter reports this instead of re-reading the role file when the
        # event lands, so a binding changed mid-dispatch cannot rewrite history.
        run = self.registry.create_run(host="cmdc", session_id="session-2")
        task = self.registry.create_task(run.run_id, "task-b", role="tianji-worker")
        _invocation, token = self.registry.create_pending_invocation(
            task.event_key, host="cmdc", session_id="session-2", role="tianji-worker",
            model="zai-org/glm-5.3", model_source="declared",
        )
        result = self.call({
            "workspace": str(self.root), "host": "cmdc", "session_id": "session-2",
            "token": token, "tool_call_id": "call-b", "role": "tianji-worker",
        })
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["model"], "zai-org/glm-5.3")
        self.assertEqual(payload["model_source"], "declared")

    def test_a_bad_token_exits_non_zero_with_json(self):
        result = self.call({
            "workspace": str(self.root), "host": "cmdc", "session_id": "session-1",
            "token": "not-a-real-token-value", "tool_call_id": "call-a",
        })
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])

    def test_a_missing_field_exits_non_zero(self):
        result = self.call({"workspace": str(self.root), "host": "cmdc"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_invalid_json_exits_non_zero(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_registry.py"), "claim"],
            input="{not json", text=True, capture_output=True, cwd=str(self.root),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
