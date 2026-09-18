import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
# The skill ships as a flat scripts/ directory; importing shared modules the
# same way the scripts do keeps a single module identity (no duplicate classes).
sys.path.insert(0, str(ROOT / "skills" / "tianji"))
sys.path.insert(0, str(SCRIPTS))

from host_adapters.cmdc.event_adapter import (  # noqa: E402
    HOST,
    IdentityConflict,
    derive_event_id,
    to_ledger_event,
)
from ledger_schema import EventKey, REQUIRED_FIELDS, event_key_from  # noqa: E402
from ledger_sink import append_event, event_ids, ledger_path  # noqa: E402
from run_registry import RunRegistry  # noqa: E402


class EventAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-event-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = RunRegistry(self.root)
        self.session = "session-1"

    def start_payload(self, tool_call_id, subagent_type="tianji-worker"):
        return {
            "event": "subagent_start",
            "toolCallId": tool_call_id,
            "subagentType": subagent_type,
            "description": "do the thing",
            "background": False,
            "showOutput": True,
            "ts": "2026-09-10T12:00:00+00:00",
        }

    def bound_task(self, task_id, role, tool_call_id, run=None):
        """Dispatch a task and claim it with a real host call id."""
        run = run or self.registry.create_run(host=HOST, session_id=self.session)
        task = self.registry.create_task(run.run_id, task_id, role=role)
        _, token = self.registry.create_pending_invocation(
            task.event_key, host=HOST, session_id=self.session, role=role,
        )
        self.registry.claim_invocation(
            token=token, tool_call_id=tool_call_id, host=HOST, session_id=self.session,
        )
        return run, task

    # ---- unbound ---------------------------------------------------------
    def test_unbound_event_is_display_only(self):
        event = to_ledger_event(
            self.start_payload("call-unknown"), self.registry, session_id=self.session,
        )
        self.assertTrue(event["legacy"])
        self.assertEqual(event["host"], "legacy_unknown")
        self.assertIsNone(event_key_from(event))

    def test_unknown_native_event_is_rejected(self):
        with self.assertRaises(ValueError):
            to_ledger_event({"event": "not_a_tianji_event"}, self.registry,
                            session_id=self.session)

    # ---- binding resolution ---------------------------------------------
    def test_binding_resolves_exact_event_key(self):
        run, task = self.bound_task("task-a", "tianji-worker", "call-a")
        event = to_ledger_event(
            self.start_payload("call-a"), self.registry, session_id=self.session,
        )
        self.assertEqual(set(event), REQUIRED_FIELDS)
        self.assertEqual(event["schema_version"], 2)
        self.assertEqual(event["host"], HOST)
        self.assertEqual(event["agent"], "tianji-worker")
        self.assertEqual(event["correlation_id"], "call-a")
        self.assertEqual(event_key_from(event), task.event_key)

    def test_parallel_tasks_do_not_cross_contaminate(self):
        run = self.registry.create_run(host=HOST, session_id=self.session)
        _, task_a = self.bound_task("task-a", "tianji-worker", "call-a", run=run)
        _, task_b = self.bound_task("task-b", "tianji-verifier", "call-b", run=run)

        event_a = to_ledger_event(self.start_payload("call-a"), self.registry, session_id=self.session)
        event_b = to_ledger_event(
            {**self.start_payload("call-b", "tianji-verifier"), "event": "subagent_stop"},
            self.registry, session_id=self.session,
        )
        self.assertEqual(event_key_from(event_a), task_a.event_key)
        self.assertEqual(event_key_from(event_b), task_b.event_key)

    def test_two_correlations_under_one_task_share_the_event_key(self):
        run = self.registry.create_run(host=HOST, session_id=self.session)
        task = self.registry.create_task(run.run_id, "task-a", role="tianji-worker")
        for call in ("call-1", "call-2"):
            _, token = self.registry.create_pending_invocation(
                task.event_key, host=HOST, session_id=self.session, role="tianji-worker",
            )
            self.registry.claim_invocation(
                token=token, tool_call_id=call, host=HOST, session_id=self.session,
            )
        first = to_ledger_event(self.start_payload("call-1"), self.registry, session_id=self.session)
        second = to_ledger_event(self.start_payload("call-2"), self.registry, session_id=self.session)
        self.assertEqual(event_key_from(first), task.event_key)
        self.assertEqual(event_key_from(second), task.event_key)
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_a_payload_event_key_is_a_claim_the_registry_must_confirm(self):
        # The payload cannot mint identity by asserting one: without a claim
        # bound to this call id, the EventKey has to be one the registry knows,
        # and it has to resolve to a real invocation.
        run = self.registry.create_run(host=HOST, session_id=self.session)
        task = self.registry.create_task(run.run_id, "explicit-task", attempt=1,
                                         role="tianji-worker")
        self.registry.create_pending_invocation(
            task.event_key, host=HOST, session_id=self.session, role="tianji-worker",
        )
        payload = {
            **self.start_payload("call-unbound"),
            "event": "subagent_progress",
            "run_id": run.run_id,
            "task_id": "explicit-task",
            "attempt": 1,
        }
        event = to_ledger_event(payload, self.registry, session_id=self.session)
        self.assertEqual(event_key_from(event), task.event_key)
        self.assertFalse(event["invocation_id"] == "unknown")

    def test_a_payload_event_key_the_registry_never_issued_is_rejected(self):
        self.registry.create_run(host=HOST, session_id=self.session)
        payload = {
            **self.start_payload("call-forged"),
            "event": "subagent_progress",
            "run_id": "34ff8429-0bc2-4b4e-b48c-9b7d7508fbb4",
            "task_id": "ghost-task",
            "attempt": 1,
        }
        with self.assertRaises(IdentityConflict):
            to_ledger_event(payload, self.registry, session_id=self.session)

    def test_a_payload_event_key_that_contradicts_the_claim_is_rejected(self):
        run, task = self.bound_task("task-a", "tianji-worker", "call-a")
        payload = {
            **self.start_payload("call-a"),
            "event": "subagent_progress",
            "run_id": "34ff8429-0bc2-4b4e-b48c-9b7d7508fbb4",
            "task_id": "other-task",
            "attempt": 1,
        }
        with self.assertRaises(IdentityConflict):
            to_ledger_event(payload, self.registry, session_id=self.session)

    def test_bad_explicit_run_id_fails_loud(self):
        payload = {**self.start_payload("call-a"), "run_id": "not-a-uuid",
                   "task_id": "t", "attempt": 1}
        with self.assertRaises(ValueError):
            to_ledger_event(payload, self.registry, session_id=self.session)

    # ---- event identity --------------------------------------------------
    def test_event_id_is_stable_for_replayed_payload(self):
        payload = self.start_payload("call-a")
        first = derive_event_id(payload, event="subagent_start", session_id="s",
                                correlation_id="call-a",
                                occurred_at="2026-09-10T12:00:00+00:00", detail={})
        second = derive_event_id(payload, event="subagent_start", session_id="s",
                                 correlation_id="call-a",
                                 occurred_at="2026-09-10T12:00:00+00:00", detail={})
        self.assertEqual(first, second)

    def test_native_event_id_is_preferred(self):
        payload = {**self.start_payload("call-a"), "eventId": "native-42"}
        self.assertEqual(
            derive_event_id(payload, event="subagent_start", session_id="s",
                            correlation_id="call-a",
                            occurred_at="2026-09-10T12:00:00+00:00", detail={}),
            "native-42",
        )

    def test_an_unclaimed_dispatch_stays_unbound(self):
        # A dispatch whose call id was never claimed has no identity. The old
        # role fallback would have silently attributed it to the last task of
        # that role -- that fallback is gone, so it must stay display-only.
        run = self.registry.create_run(host=HOST, session_id=self.session)
        self.registry.create_task(run.run_id, "probe", role="tianji-worker")
        event = to_ledger_event(
            {**self.start_payload("call-unknown-id", "tianji-worker"),
             "event": "subagent_start"},
            self.registry, session_id=self.session,
        )
        self.assertTrue(event["legacy"])
        self.assertIsNone(event_key_from(event))

    def test_a_call_id_claimed_by_another_role_does_not_leak(self):
        # Two dispatches share a role; only the claimed call id resolves.
        run = self.registry.create_run(host=HOST, session_id=self.session)
        _, claimed = self.bound_task("probe-claimed", "tianji-worker", "call-claimed", run=run)
        self.registry.create_task(run.run_id, "probe-loose", role="tianji-worker")

        resolved = to_ledger_event(
            self.start_payload("call-claimed", "tianji-worker"),
            self.registry, session_id=self.session,
        )
        loose = to_ledger_event(
            self.start_payload("call-loose", "tianji-worker"),
            self.registry, session_id=self.session,
        )
        self.assertEqual(event_key_from(resolved), claimed.event_key)
        self.assertTrue(loose["legacy"])
        self.assertIsNone(event_key_from(loose))

    def test_no_binding_at_all_stays_unbound(self):
        event = to_ledger_event(
            self.start_payload("call-lonely", "tianji-worker"),
            self.registry, session_id=self.session,
        )
        self.assertTrue(event["legacy"])
        self.assertIsNone(event_key_from(event))

    def test_naive_timestamp_is_rejected(self):
        payload = {**self.start_payload("call-a"), "ts": "2026-09-10T12:00:00"}
        with self.assertRaises(ValueError):
            to_ledger_event(payload, self.registry, session_id=self.session)

    def test_cross_language_identity_fixture_matches_the_mod(self):
        fixture = json.loads(
            (ROOT / "skills" / "tianji" / "schemas" / "ledger-identity.fixture.json")
            .read_text(encoding="utf-8")
        )
        source = fixture["input"]
        # The fixture's detail is non-empty and nested, so this fails if either
        # side only sorts its top-level keys.
        self.assertTrue(source["detail"], "the fixture must exercise a real detail")
        derived = derive_event_id(
            {"event": source["event"], "toolCallId": source["toolCallId"],
             "subagentType": source["agent"], "ts": source["ts"]},
            event=source["event"], session_id=source["session_id"],
            correlation_id=source["toolCallId"], occurred_at=source["ts"],
            detail=source["detail"],
        )
        self.assertEqual(derived, fixture["expected"]["event_id"])


class LedgerSinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-sink-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = RunRegistry(self.root)
        self.run = self.registry.create_run(host=HOST, session_id="session-1")
        self.task = self.registry.create_task(self.run.run_id, "task-a", role="tianji-worker")

    def event(self, event_id="e-1", correlation="call-0", task=None):
        target = task or self.task
        _, token = self.registry.create_pending_invocation(
            target.event_key, host=HOST, session_id="session-1", role="tianji-worker",
        )
        self.registry.claim_invocation(
            token=token, tool_call_id=correlation, host=HOST, session_id="session-1",
        )
        return to_ledger_event({
            "event": "subagent_start", "toolCallId": correlation,
            "subagentType": "tianji-worker", "eventId": event_id,
            "ts": "2026-09-10T12:00:00+00:00",
        }, self.registry, session_id="session-1")

    def test_replayed_event_id_is_written_once(self):
        event = self.event()
        self.assertTrue(append_event(self.root, event))
        self.assertFalse(append_event(self.root, event))
        records = ledger_path(self.root).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(records), 1)

    def test_distinct_events_both_land(self):
        self.assertTrue(append_event(self.root, self.event("e-1", "call-1")))
        self.assertTrue(append_event(self.root, self.event("e-2", "call-2")))
        self.assertEqual(event_ids(self.root), {"e-1", "e-2"})

    def test_sink_rejects_an_invalid_envelope(self):
        with self.assertRaises(ValueError):
            append_event(self.root, {"event_id": "x"})

    def test_jsonl_survives_concurrent_appenders(self):
        from concurrent.futures import ThreadPoolExecutor

        events = [self.event(f"e-{index}", f"call-{index}") for index in range(16)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda item: append_event(self.root, item), events))
        lines = ledger_path(self.root).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 16)
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)


if __name__ == "__main__":
    unittest.main()