import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ledger_schema import (  # noqa: E402
    EventKey,
    REQUIRED_FIELDS,
    build_event,
    event_key_from,
    normalize_legacy_event,
    validate_event,
)


class LedgerSchemaTests(unittest.TestCase):
    def event(self, **overrides):
        values = {
            "event_id": "codex:session-1:turn-1:start",
            "event": "subagent_start",
            "host": "codex",
            "run_id": "77802c0c-d50f-4fe8-bc09-8ea018cf2334",
            "session_id": "session-1",
            "task_id": "task-a",
            "attempt": 1,
            "invocation_id": "invocation-1",
            "correlation_id": "turn-1",
            "agent": "tianji-worker",
            "occurred_at": "2026-09-10T12:00:00+00:00",
            "recorded_at": "2026-09-10T12:00:01+00:00",
            "detail": {"model": "gpt-5.6-terra"},
        }
        values.update(overrides)
        return build_event(**values)

    def test_v2_event_has_explicit_identity_and_event_key(self):
        event = self.event()
        self.assertEqual(set(event), REQUIRED_FIELDS)
        self.assertEqual(event["schema_version"], 2)
        self.assertEqual(
            event_key_from(event),
            EventKey("77802c0c-d50f-4fe8-bc09-8ea018cf2334", "task-a", 1),
        )

    def test_validation_rejects_missing_or_unknown_fields(self):
        event = self.event()
        del event["event_id"]
        with self.assertRaises(ValueError):
            validate_event(event)
        event = self.event()
        event["invented"] = True
        with self.assertRaises(ValueError):
            validate_event(event)

    def test_event_id_is_required_from_the_producer(self):
        with self.assertRaises((TypeError, ValueError)):
            self.event(event_id="")

    def test_v2_run_id_and_timestamps_are_strict(self):
        with self.assertRaises(ValueError):
            self.event(run_id="not-a-uuid")
        with self.assertRaises(ValueError):
            self.event(occurred_at="2026-09-10T12:00:00")

    def test_legacy_unknown_is_display_only_and_has_no_event_key(self):
        legacy = normalize_legacy_event({
            "ts": "2026-09-01T01:02:03",
            "event": "subagent_start",
            "agent": "tianji-worker",
            "session_id": "old-session",
            "detail": {},
        })
        self.assertTrue(legacy["legacy"])
        self.assertEqual(legacy["host"], "legacy_unknown")
        self.assertEqual(legacy["occurred_at"], "2026-09-01T01:02:03")
        self.assertIsNone(event_key_from(legacy))

    def test_same_task_name_in_different_runs_never_merges(self):
        one = self.event(run_id="77802c0c-d50f-4fe8-bc09-8ea018cf2334")
        two = self.event(
            event_id="other:start",
            host="another-host",
            run_id="34ff8429-0bc2-4b4e-b48c-9b7d7508fbb4",
        )
        self.assertNotEqual(event_key_from(one), event_key_from(two))

    def test_json_schema_required_fields_match_python_contract(self):
        schema = json.loads(
            (ROOT / "skills" / "tianji" / "schemas" / "tianji-ledger.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(set(schema["required"]), REQUIRED_FIELDS)
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
