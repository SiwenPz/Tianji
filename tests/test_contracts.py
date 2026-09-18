"""Frozen contracts for the shared core (step 1a).

These pin the vocabulary and the state machines that the rest of the rework
implements, so later stages cannot quietly redefine them:

* a probe result is tri-state, never a boolean, and its invariants hold;
* a model source is well-formed (no empty host menu) and is chosen separately
  from provider health, which is itself tri-state;
* an invocation lifecycle is disjoint from the run/task lifecycle and only
  permits its legal transitions;
* the registry migrates per record kind, isolates what cannot be migrated,
  refuses the future, and round-trips through its storage codec;
* the ledger reader accepts only the exact current protocol version.

The observation types are frozen here. Only the menu observation and the pool
evidence are wired into the conclusion machine, and each is stated explicitly:
there is no global "something was unavailable" axis, so a stray key cannot
become a second priority rule.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import conclusions  # noqa: E402
import doctor  # noqa: E402
import model_source  # noqa: E402
import observation  # noqa: E402
import run_registry  # noqa: E402
from ledger_schema import SCHEMA_VERSION, is_exact_schema_version  # noqa: E402
from observation import Observation  # noqa: E402
from run_registry import RunRegistry  # noqa: E402


def load_conclusion_machine():
    path = SCRIPTS / "tianji-init.py"
    spec = importlib.util.spec_from_file_location("tianji_init_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def all_checks(**overrides):
    checks = {
        "hooks": (True, ""), "roles": (True, ""), "role_bindings": (True, ""),
        "status_line": (True, ""), "menu": (True, "2 个模型"),
        "routing": (True, ""), "probe": (True, ""),
    }
    checks.update(overrides)
    return checks


class ObservationTests(unittest.TestCase):
    def test_available_carries_a_non_empty_value(self):
        obs = Observation.available(["m-1", "m-2"])
        self.assertEqual(obs.state, observation.AVAILABLE)
        self.assertTrue(obs.was_observed)
        self.assertFalse(obs.is_empty)
        self.assertIsNone(obs.conclusion())
        self.assertEqual(obs.error(), "")

    def test_confirmed_empty_is_observed_but_empty(self):
        obs = Observation.confirmed_empty()
        self.assertTrue(obs.was_observed)
        self.assertTrue(obs.is_empty)
        self.assertIsNone(obs.value)
        self.assertIsNone(obs.conclusion())

    def test_unavailable_requires_a_reason(self):
        with self.assertRaises(ValueError):
            Observation.unavailable("")

    def test_unavailable_yields_check_unavailable(self):
        obs = Observation.unavailable("--list-models timed out")
        self.assertFalse(obs.was_observed)
        self.assertFalse(obs.is_empty)
        self.assertEqual(obs.conclusion(), conclusions.CHECK_UNAVAILABLE)
        self.assertEqual(obs.error(), "--list-models timed out")

    def test_an_unknown_state_is_rejected(self):
        with self.assertRaises(ValueError):
            Observation("maybe")

    def test_available_without_content_is_rejected(self):
        # available(None) and available([]) both mean "we looked and found
        # nothing", which is what confirmed_empty is for.
        for empty in (None, [], (), {}, "", set()):
            with self.assertRaises(ValueError, msg=repr(empty)):
                Observation.available(empty)

    def test_a_scalar_reading_is_still_a_real_observation(self):
        # 0 and False are genuine readings, not absences.
        self.assertEqual(Observation.available(0).value, 0)
        self.assertEqual(Observation.available(False).value, False)

    def test_state_invariants_are_enforced_on_construction(self):
        with self.assertRaises(ValueError):
            Observation(observation.CONFIRMED_EMPTY, ["m-1"], "")
        with self.assertRaises(ValueError):
            Observation(observation.AVAILABLE, ["m-1"], "because")
        with self.assertRaises(ValueError):
            Observation(observation.CONFIRMED_EMPTY, None, "because")
        with self.assertRaises(ValueError):
            Observation(observation.UNAVAILABLE, ["m-1"], "timeout")

    def test_value_and_reason_are_mutually_exclusive_across_states(self):
        for obs in (
            Observation.available(["m-1"]),
            Observation.confirmed_empty(),
            Observation.unavailable("timeout"),
        ):
            has_value = obs.value is not None
            has_reason = bool(obs.reason)
            self.assertFalse(has_value and has_reason, obs)
            if obs.state == observation.AVAILABLE:
                self.assertTrue(has_value)
            else:
                self.assertFalse(has_value)
            if obs.state == observation.UNAVAILABLE:
                self.assertTrue(has_reason)
            else:
                self.assertFalse(has_reason)

    def test_the_three_states_are_mutually_exclusive_and_exhaustive(self):
        states = {
            Observation.available(["m-1"]).state,
            Observation.confirmed_empty().state,
            Observation.unavailable("timeout").state,
        }
        self.assertEqual(states, observation.OBSERVATION_STATES)

    def test_a_failed_probe_is_never_read_as_empty(self):
        failed = Observation.unavailable("timeout")
        empty = Observation.confirmed_empty()
        self.assertNotEqual(failed.state, empty.state)
        self.assertIsNotNone(failed.conclusion())
        self.assertIsNone(empty.conclusion())

    def test_conclusion_for_rejects_non_observations(self):
        with self.assertRaises(ValueError):
            observation.conclusion_for("unavailable")


class ConclusionVocabularyTests(unittest.TestCase):
    def test_check_unavailable_is_a_shared_conclusion(self):
        self.assertIn(conclusions.CHECK_UNAVAILABLE, conclusions.ALL)

    def test_check_unavailable_is_not_actionable(self):
        self.assertNotIn(conclusions.CHECK_UNAVAILABLE, conclusions.ACTIONABLE)
        self.assertIn(conclusions.READY, conclusions.ACTIONABLE)

    def test_need_model_source_exists_and_is_host_neutral(self):
        self.assertIn(conclusions.NEED_MODEL_SOURCE, conclusions.ALL)
        self.assertIn(conclusions.NEED_POOL, conclusions.MODEL_SOURCE_VERDICTS)
        self.assertIn(conclusions.NEED_MODEL_SOURCE, conclusions.MODEL_SOURCE_VERDICTS)
        self.assertIn(conclusions.NEED_POOL, conclusions.LEGACY)

    def test_every_verdict_has_shared_advice(self):
        for verdict in conclusions.ALL:
            self.assertTrue(conclusions.suggestion(verdict), verdict)

    def test_the_degraded_advice_comes_from_the_shared_table(self):
        self.assertEqual(
            conclusions.suggestion(conclusions.DEGRADED),
            conclusions.SUGGESTIONS[conclusions.DEGRADED],
        )

    def test_the_machine_has_no_global_unavailable_axis(self):
        # A stray "unavailable" key must not become a second priority rule: only
        # the menu observation and the pool evidence are wired, and each says
        # explicitly what it observed.
        machine = load_conclusion_machine()
        polluted = all_checks(unavailable=Observation.unavailable("boom"))
        self.assertEqual(
            machine.determine_conclusion(polluted, doctor.PoolEvidence.not_required())[0],
            conclusions.READY,
        )

    def test_the_shared_machine_emits_the_readiness_verdict_when_all_checks_pass(self):
        machine = load_conclusion_machine()
        self.assertEqual(
            machine.determine_conclusion(all_checks(), doctor.PoolEvidence.not_required())[0],
            conclusions.READY,
        )

    def test_every_verdict_the_machine_returns_is_in_the_vocabulary(self):
        machine = load_conclusion_machine()
        not_required = doctor.PoolEvidence.not_required()
        scenarios = [
            (all_checks(hooks=(False, "")), not_required),
            (all_checks(routing=(False, "")), not_required),
            (all_checks(menu=(False, "")), not_required),
            (all_checks(role_bindings=(False, "")), not_required),
            (all_checks(probe=(False, "")), not_required),
            (all_checks(), doctor.PoolEvidence.probed([("p", "死")])),
            (all_checks(), doctor.PoolEvidence.not_probed("offline")),
            (all_checks(), doctor.PoolEvidence.failed("probe crashed")),
        ]
        for checks, pool in scenarios:
            verdict = machine.determine_conclusion(checks, pool)[0]
            self.assertIn(verdict, conclusions.ALL, verdict)


class InvocationLifecycleTests(unittest.TestCase):
    def test_invocation_lifecycles_include_a_cancel_terminal(self):
        self.assertEqual(
            run_registry.INVOCATION_LIFECYCLES,
            frozenset({"pending", "claimed", "cancelled", "stopped"}),
        )

    def test_invocation_lifecycles_are_disjoint_from_run_lifecycles(self):
        self.assertFalse(run_registry.INVOCATION_LIFECYCLES & run_registry.LIFECYCLES)

    def test_legal_transitions_are_accepted(self):
        for current, nxt in (
            ("pending", "claimed"), ("pending", "cancelled"),
            ("claimed", "stopped"), ("claimed", "cancelled"),
        ):
            self.assertEqual(run_registry.invocation_transition(current, nxt), nxt)

    def test_illegal_transitions_are_refused(self):
        for current, nxt in (
            ("pending", "stopped"), ("stopped", "claimed"),
            ("cancelled", "claimed"), ("claimed", "pending"),
        ):
            with self.assertRaises(run_registry.RegistryError):
                run_registry.invocation_transition(current, nxt)

    def test_an_unknown_current_state_is_refused(self):
        with self.assertRaises(run_registry.RegistryError):
            run_registry.invocation_transition("active", "claimed")

    def test_terminal_states_are_terminal(self):
        for state in ("cancelled", "stopped"):
            self.assertTrue(run_registry.is_invocation_terminal(state))
        for state in ("pending", "claimed"):
            self.assertFalse(run_registry.is_invocation_terminal(state))

    def test_every_non_terminal_state_can_reach_a_terminal_state(self):
        for state in run_registry.INVOCATION_LIFECYCLES:
            if run_registry.is_invocation_terminal(state):
                self.assertEqual(run_registry.INVOCATION_TRANSITIONS[state], frozenset())
                continue
            reachable = run_registry.INVOCATION_TRANSITIONS[state]
            self.assertTrue(
                reachable & run_registry.INVOCATION_TERMINAL,
                f"{state} cannot reach a terminal state",
            )


class RecordKindTests(unittest.TestCase):
    def test_each_record_shape_is_recognized(self):
        cases = [
            ({"binding_kind": "correlation", "task_id": "t", "run_id": "r"}, "binding"),
            ({"invocation_id": "inv-1", "task_id": "t"}, "invocation"),
            ({"task_id": "t", "attempt": 1}, "task"),
            ({"run_id": "r", "host": "h", "resume_count": 0}, "run"),
        ]
        for record, expected in cases:
            self.assertEqual(run_registry.record_kind(record), expected)

    def test_an_unrecognized_record_is_refused(self):
        with self.assertRaises(run_registry.RegistryError):
            run_registry.record_kind({"unrelated": True})

    def test_no_record_kind_has_an_empty_lifecycle_family(self):
        for kind in run_registry.RECORD_KINDS:
            self.assertTrue(run_registry.LIFECYCLE_FAMILIES[kind])


class RegistryMigrationTests(unittest.TestCase):
    def test_a_current_record_passes_through(self):
        record = {"task_id": "t", "lifecycle": "active",
                  "schema_version": run_registry.REGISTRY_SCHEMA_VERSION}
        outcome = run_registry.migrate_record(record)
        self.assertEqual(outcome.status, run_registry.MIGRATION_CURRENT)
        self.assertTrue(outcome.is_authoritative)

    def test_a_legacy_run_is_stamped_and_keeps_its_fields(self):
        legacy = {"run_id": "r", "host": "h", "resume_count": 0, "lifecycle": "active"}
        outcome = run_registry.migrate_record(legacy)
        self.assertEqual(outcome.status, run_registry.MIGRATION_MIGRATED)
        self.assertEqual(
            outcome.record["schema_version"], run_registry.REGISTRY_SCHEMA_VERSION,
        )
        for key, value in legacy.items():
            self.assertEqual(outcome.record[key], value, key)
        self.assertNotIn("schema_version", legacy)

    def test_a_legacy_task_is_stamped(self):
        legacy = {"task_id": "t", "attempt": 1, "role": "", "lifecycle": "closed"}
        outcome = run_registry.migrate_record(legacy)
        self.assertEqual(outcome.status, run_registry.MIGRATION_MIGRATED)

    def test_a_legacy_invocation_is_isolated_and_not_stamped(self):
        # active/closed predate claim tokens: stamping v2 would mint a record
        # whose lifecycle the new vocabulary cannot express.
        for lifecycle in ("active", "closed", "aborted"):
            legacy = {
                "invocation_id": "inv-1", "task_id": "t", "lifecycle": lifecycle,
            }
            outcome = run_registry.migrate_record(legacy)
            self.assertEqual(outcome.status, run_registry.MIGRATION_ISOLATED, lifecycle)
            self.assertFalse(outcome.is_authoritative)
            self.assertNotIn("schema_version", outcome.record)
            self.assertTrue(outcome.reason)

    def test_a_current_invocation_with_a_legacy_lifecycle_is_isolated(self):
        record = {
            "invocation_id": "inv-1", "task_id": "t", "lifecycle": "closed",
            "schema_version": run_registry.REGISTRY_SCHEMA_VERSION,
        }
        outcome = run_registry.migrate_record(record)
        self.assertEqual(outcome.status, run_registry.MIGRATION_ISOLATED)

    def test_a_current_invocation_with_a_current_lifecycle_passes(self):
        record = {
            "invocation_id": "inv-1", "task_id": "t", "lifecycle": "pending",
            "schema_version": run_registry.REGISTRY_SCHEMA_VERSION,
        }
        outcome = run_registry.migrate_record(record)
        self.assertEqual(outcome.status, run_registry.MIGRATION_CURRENT)

    def test_a_future_record_is_refused(self):
        with self.assertRaises(run_registry.RegistryError):
            run_registry.migrate_record(
                {"task_id": "t", "schema_version": run_registry.REGISTRY_SCHEMA_VERSION + 1}
            )

    def test_isolation_preserves_the_record_verbatim(self):
        legacy = {"invocation_id": "inv-1", "task_id": "t", "lifecycle": "active",
                  "extra": {"kept": True}}
        outcome = run_registry.migrate_record(legacy)
        self.assertEqual(outcome.record, legacy)


class StorageCodecTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-codec-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = RunRegistry(self.root)

    def test_a_current_record_round_trips_through_disk_unchanged(self):
        path = self.root / "record.json"
        record = {
            "task_id": "整理接口文档", "attempt": 1, "role": "tianji-worker",
            "lifecycle": "active", "schema_version": run_registry.REGISTRY_SCHEMA_VERSION,
        }
        self.registry._atomic_write(path, record)
        self.assertEqual(self.registry._read(path), record)

    def test_writing_stamps_the_current_version(self):
        path = self.root / "run.json"
        self.registry._atomic_write(path, {"run_id": "r", "host": "h", "resume_count": 0})
        self.assertEqual(
            self.registry._read(path)["schema_version"],
            run_registry.REGISTRY_SCHEMA_VERSION,
        )

    def test_a_legacy_invocation_is_not_stamped_on_write(self):
        path = self.root / "inv-legacy.json"
        self.registry._atomic_write(
            path, {"invocation_id": "inv-1", "task_id": "t", "lifecycle": "active"},
        )
        self.assertNotIn("schema_version", self.registry._read(path))

    def test_stored_runs_and_tasks_construct_their_dataclasses(self):
        run = self.registry.create_run(host="codex", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a")
        self.assertEqual(self.registry.get_run(run.run_id).run_id, run.run_id)
        self.assertEqual(self.registry.get_task(task.event_key).task_id, "task-a")

    def test_a_legacy_record_on_disk_still_constructs_its_dataclass(self):
        run = self.registry.create_run(host="codex", session_id="session-1")
        path = self.registry._run_path(run.run_id)
        legacy = self.registry._read(path)
        legacy.pop("schema_version")
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.registry.get_run(run.run_id).run_id, run.run_id)


class IsolatedInvocationReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-iso-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = RunRegistry(self.root)

    def test_reading_an_isolated_legacy_invocation_fails_closed(self):
        run = self.registry.create_run(host="codex", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a")
        path = self.registry._invocation_path(task.event_key, "inv-legacy")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "run_id": run.run_id, "task_id": "task-a", "attempt": 1,
            "invocation_id": "inv-legacy", "host": "codex",
            "session_id": "session-1", "role": "tianji-worker",
            "correlation_id": "", "agent_id": "tianji-worker",
            "lifecycle": "active", "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }), encoding="utf-8")
        with self.assertRaises(run_registry.RegistryLegacyError):
            self.registry.get_invocation(task.event_key, "inv-legacy")

    def test_a_new_invocation_is_pending_and_readable(self):
        run = self.registry.create_run(host="codex", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a")
        invocation = self.registry.create_invocation(
            task.event_key, host="codex", session_id="session-1",
            role="tianji-worker", correlation_id="turn-a",
        )
        self.assertEqual(invocation.lifecycle, "pending")
        self.assertEqual(
            self.registry.get_invocation(
                task.event_key, invocation.invocation_id,
            ).lifecycle,
            "pending",
        )

    def test_a_claimed_invocation_can_stop_but_not_pend_again(self):
        run = self.registry.create_run(host="codex", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a")
        invocation = self.registry.create_invocation(
            task.event_key, host="codex", session_id="session-1",
            role="tianji-worker", correlation_id="turn-a",
        )
        self.registry.transition_invocation(
            task.event_key, invocation.invocation_id, "claimed",
        )
        stopped = self.registry.transition_invocation(
            task.event_key, invocation.invocation_id, "stopped",
        )
        self.assertEqual(stopped.lifecycle, "stopped")
        with self.assertRaises(run_registry.RegistryError):
            self.registry.transition_invocation(
                task.event_key, invocation.invocation_id, "claimed",
            )

    def test_a_pending_invocation_cannot_be_stopped_directly(self):
        run = self.registry.create_run(host="codex", session_id="session-1")
        task = self.registry.create_task(run.run_id, "task-a")
        invocation = self.registry.create_invocation(
            task.event_key, host="codex", session_id="session-1",
            role="tianji-worker", correlation_id="turn-a",
        )
        with self.assertRaises(run_registry.RegistryError):
            self.registry.transition_invocation(
                task.event_key, invocation.invocation_id, "stopped",
            )


class ModelSourceTests(unittest.TestCase):
    def test_a_usable_host_menu_is_the_source_without_a_pool(self):
        source, conclusion = model_source.resolve(Observation.available(["m-1"]))
        self.assertEqual(conclusion, "")
        self.assertEqual(source.kind, model_source.HOST_MENU)
        self.assertFalse(source.installs_pool)

    def test_an_unobservable_menu_is_not_an_empty_menu(self):
        source, conclusion = model_source.resolve(Observation.unavailable("timeout"))
        self.assertIsNone(source)
        self.assertEqual(conclusion, conclusions.CHECK_UNAVAILABLE)

    def test_an_empty_menu_without_a_choice_asks_for_a_model_source(self):
        source, conclusion = model_source.resolve(Observation.confirmed_empty())
        self.assertIsNone(source)
        self.assertEqual(conclusion, conclusions.NEED_MODEL_SOURCE)

    def test_a_chosen_external_provider_is_the_source(self):
        source, conclusion = model_source.resolve(
            Observation.confirmed_empty(), external_chosen=True,
        )
        self.assertEqual(conclusion, "")
        self.assertEqual(source.kind, model_source.EXTERNAL_POOL)
        self.assertTrue(source.installs_pool)

    def test_resolve_rejects_non_observations(self):
        with self.assertRaises(ValueError):
            model_source.resolve("timeout")

    def test_a_host_menu_source_cannot_be_empty(self):
        with self.assertRaises(ValueError):
            model_source.ModelSource(model_source.HOST_MENU, ())

    def test_an_external_source_cannot_carry_a_host_menu(self):
        with self.assertRaises(ValueError):
            model_source.ModelSource(model_source.EXTERNAL_POOL, ("m-1",))

    def test_an_unknown_source_kind_is_refused(self):
        with self.assertRaises(ValueError):
            model_source.ModelSource("somewhere_else")

    def test_the_contract_never_mentions_a_host_name(self):
        text = (SCRIPTS / "model_source.py").read_text(encoding="utf-8")
        for marker in ("cmdc", "commandcode", "kimi", "codex"):
            self.assertNotIn(marker, text.lower(), marker)


class ProviderHealthTests(unittest.TestCase):
    def test_a_checked_provider_list_reports_liveness(self):
        self.assertEqual(model_source.health(Observation.available(["proxy-a"])), "")

    def test_a_checked_but_empty_provider_list_is_degraded(self):
        self.assertEqual(
            model_source.health(Observation.confirmed_empty()), conclusions.DEGRADED,
        )

    def test_an_unchecked_provider_is_unavailable_not_degraded(self):
        verdict = model_source.health(Observation.unavailable("pool probe timed out"))
        self.assertEqual(verdict, conclusions.CHECK_UNAVAILABLE)
        self.assertNotEqual(verdict, conclusions.DEGRADED)

    def test_health_cannot_be_called_without_stating_what_was_observed(self):
        # health([], []) is unrepresentable by construction: "we did not look"
        # must be said explicitly.
        with self.assertRaises(ValueError):
            model_source.health([])

    def test_health_is_independent_of_source_selection(self):
        chosen, _ = model_source.resolve(
            Observation.confirmed_empty(), external_chosen=True,
        )
        self.assertEqual(chosen.kind, model_source.EXTERNAL_POOL)
        self.assertEqual(
            model_source.health(Observation.confirmed_empty()), conclusions.DEGRADED,
        )


class IdentityAuthorityTests(unittest.TestCase):
    """The registry, not the payload, decides identity."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-identity-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = RunRegistry(self.root)
        self.run = self.registry.create_run(host="cmdc", session_id="s1")
        self.task = self.registry.create_task(self.run.run_id, "task-a", role="tianji-worker")

    def test_a_real_event_key_verifies(self):
        self.assertEqual(
            self.registry.verify_event_key(self.task.event_key), self.task.event_key,
        )

    def test_an_unknown_run_or_task_is_refused(self):
        from ledger_schema import EventKey

        with self.assertRaises(run_registry.RegistryError):
            self.registry.verify_event_key(
                EventKey("34ff8429-0bc2-4b4e-b48c-9b7d7508fbb4", "task-a", 1)
            )
        with self.assertRaises(run_registry.RegistryError):
            self.registry.verify_event_key(EventKey(self.run.run_id, "ghost", 1))
        with self.assertRaises(run_registry.RegistryError):
            self.registry.verify_event_key(EventKey(self.run.run_id, "task-a", 2))

    def test_the_invocation_of_a_task_is_resolved_only_when_unambiguous(self):
        first = self.registry.create_pending_invocation(
            self.task.event_key, host="cmdc", session_id="s1", role="tianji-worker",
        )[0]
        self.assertEqual(
            self.registry.task_invocation_id(self.task.event_key), first.invocation_id,
        )
        self.registry.create_pending_invocation(
            self.task.event_key, host="cmdc", session_id="s1", role="tianji-worker",
        )
        with self.assertRaises(run_registry.RegistryError):
            self.registry.task_invocation_id(self.task.event_key)

    def test_the_unknown_sentinel_is_not_a_ledger_identity(self):
        from ledger_schema import UNKNOWN_IDENTIFIER, build_event

        with self.assertRaises(ValueError):
            build_event(
                event_id="e-1", event="subagent_start", host="cmdc",
                run_id=self.run.run_id, session_id="s1", task_id="task-a", attempt=1,
                invocation_id=UNKNOWN_IDENTIFIER, correlation_id="call-a",
                agent="tianji-worker", occurred_at="2026-09-10T12:00:00+00:00",
                recorded_at="2026-09-10T12:00:00+00:00", detail={},
            )

    def test_acceptance_closes_the_task_it_accepted(self):
        self.assertEqual(self.registry.get_task(self.task.event_key).lifecycle, "active")
        closed = self.registry.close_accepted_task(self.task.event_key)
        self.assertEqual(closed.lifecycle, "closed")
        self.assertEqual(
            self.registry.get_task(self.task.event_key).lifecycle, "closed",
        )


class ExactSchemaVersionTests(unittest.TestCase):
    def test_only_the_current_version_is_authoritative(self):
        self.assertTrue(is_exact_schema_version({"schema_version": SCHEMA_VERSION}))
        self.assertFalse(is_exact_schema_version({"schema_version": SCHEMA_VERSION + 1}))
        self.assertFalse(is_exact_schema_version({"schema_version": 1}))
        self.assertFalse(is_exact_schema_version({}))
        self.assertFalse(is_exact_schema_version(None))


if __name__ == "__main__":
    unittest.main()
