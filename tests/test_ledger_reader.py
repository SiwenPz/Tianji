"""The shared ledger reader: classify every line, rewrite nothing.

The reader is the one place that decides what an authoritative record is, so
these tests pin the boundary between "this drives state" and "this is history
or garbage" -- and pin the promise that reading has no side effects.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import board  # noqa: E402
import ledger_reader  # noqa: E402
from ledger_schema import SCHEMA_VERSION  # noqa: E402


RUN = "77802c0c-d50f-4fe8-bc09-8ea018cf2334"


def envelope(**overrides):
    record = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "e-1",
        "event": "subagent_start",
        "host": "cmdc",
        "run_id": RUN,
        "session_id": "session-1",
        "task_id": "task-a",
        "attempt": 1,
        "invocation_id": "inv-1",
        "correlation_id": "call-a",
        "agent": "tianji-worker",
        "occurred_at": "2026-09-10T12:00:00+00:00",
        "recorded_at": "2026-09-10T12:00:00+00:00",
        "detail": {},
    }
    record.update(overrides)
    return record


class ClassificationTests(unittest.TestCase):
    def classify(self, record):
        return ledger_reader.classify(record)

    def test_a_valid_envelope_is_authoritative(self):
        kind, reason, parsed = self.classify(envelope())
        self.assertEqual(kind, ledger_reader.CANONICAL)
        self.assertEqual(reason, "")
        self.assertEqual(parsed["task_id"], "task-a")

    def test_only_the_exact_schema_version_is_accepted(self):
        # A newer protocol must be quarantined, not parsed with today's rules.
        for version in (SCHEMA_VERSION + 1, SCHEMA_VERSION + 7, 1, 0, -1, "2"):
            kind, reason, _ = self.classify(envelope(schema_version=version))
            self.assertEqual(kind, ledger_reader.QUARANTINED, version)
            self.assertEqual(reason, ledger_reader.UNKNOWN_VERSION, version)

    def test_a_bad_envelope_is_quarantined_as_invalid_v2(self):
        cases = {
            "unknown field": envelope(extra="nope"),
            "missing field": {k: v for k, v in envelope().items() if k != "detail"},
            "opaque run id": envelope(run_id="not-a-uuid"),
            "naive timestamp": envelope(occurred_at="2026-09-10T12:00:00"),
            "empty event id": envelope(event_id=""),
            "boolean attempt": envelope(attempt=True),
            "non-object detail": envelope(detail="text"),
        }
        for label, record in cases.items():
            kind, reason, _ = self.classify(record)
            self.assertEqual(kind, ledger_reader.QUARANTINED, label)
            self.assertEqual(reason, ledger_reader.INVALID_V2, label)

    def test_a_pre_envelope_record_is_historical(self):
        kind, reason, _ = self.classify(
            {"ts": "2026-09-01T01:00:00", "event": "subagent_start",
             "agent": "tianji-worker", "session_id": "old"}
        )
        self.assertEqual(kind, ledger_reader.LEGACY)
        self.assertEqual(reason, "")

    def test_an_explicit_legacy_record_is_historical_even_with_a_version(self):
        # A record that disclaims being an envelope is not read as one, whatever
        # version stamp it happens to carry.
        kind, _reason, _ = self.classify({
            "schema_version": 4, "legacy": True, "ts": "2026-09-10T12:00:00+00:00",
            "event": "subagent_start", "agent": "tianji-worker", "session_id": "s",
        })
        self.assertEqual(kind, ledger_reader.LEGACY)

    def test_a_versionless_record_that_is_not_legacy_shaped_is_quarantined(self):
        kind, reason, _ = self.classify({"event": "subagent_start"})
        self.assertEqual(kind, ledger_reader.QUARANTINED)
        self.assertEqual(reason, ledger_reader.UNRECOGNIZED)

    def test_a_non_object_is_quarantined(self):
        for value in ([], "text", 42, None, True):
            kind, reason, _ = self.classify(value)
            self.assertEqual(kind, ledger_reader.UNREADABLE, value)
            self.assertEqual(reason, ledger_reader.NOT_AN_OBJECT, value)


class ReadLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-reader-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "state.jsonl"

    def write(self, lines):
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.path.read_bytes()

    def test_every_category_is_classified_side_by_side(self):
        self.write([
            json.dumps(envelope()),
            json.dumps(envelope(event_id="e-2", extra="nope")),
            json.dumps(envelope(event_id="e-3", schema_version=SCHEMA_VERSION + 1)),
            json.dumps({"ts": "2026-09-01T01:00:00", "event": "subagent_start",
                        "agent": "tianji-worker"}),
            json.dumps([1, 2, 3]),
            "{not json",
        ])
        read = ledger_reader.read_ledger(self.path)
        self.assertEqual(len(read.canonical), 1)
        self.assertEqual(len(read.legacy), 1)
        self.assertEqual(read.diagnostic_count, 4)
        reasons = sorted(record.reason for record in read.quarantined)
        self.assertEqual(reasons, sorted([
            ledger_reader.INVALID_V2,
            ledger_reader.UNKNOWN_VERSION,
            ledger_reader.NOT_AN_OBJECT,
            ledger_reader.UNREADABLE_JSON,
        ]))
        self.assertEqual(read.lines_seen, 6)

    def test_quarantined_records_never_become_authoritative(self):
        self.write([
            json.dumps(envelope(event_id="e-1")),
            json.dumps(envelope(event_id="e-2", run_id="opaque")),
        ])
        read = ledger_reader.read_ledger(self.path)
        self.assertEqual([r["event_id"] for r in read.authoritative], ["e-1"])

    def test_a_partial_final_line_is_reported_as_such(self):
        # A reader that does not hold the lock can catch a writer mid-line.
        body = json.dumps(envelope()) + "\n" + '{"schema_version": 2, "event_id": "e-2"'
        self.path.write_text(body, encoding="utf-8")
        read = ledger_reader.read_ledger(self.path)
        self.assertEqual(len(read.canonical), 1)
        self.assertEqual(read.quarantined[-1].reason, ledger_reader.PARTIAL_TAIL)

    def test_a_complete_bad_line_is_not_blamed_on_a_tail(self):
        self.write([json.dumps(envelope()), "{not json"])
        read = ledger_reader.read_ledger(self.path)
        self.assertEqual(read.quarantined[-1].reason, ledger_reader.UNREADABLE_JSON)

    def test_the_summary_groups_the_reasons(self):
        self.write([
            json.dumps(envelope()),
            json.dumps(envelope(event_id="e-2", extra="x")),
            json.dumps(envelope(event_id="e-3", extra="x")),
        ])
        summary = ledger_reader.read_ledger(self.path).summary()
        self.assertEqual(summary["canonical"], 1)
        self.assertEqual(summary["quarantined"], 2)
        self.assertEqual(summary["quarantine_reasons"], {ledger_reader.INVALID_V2: 2})

    def test_reading_never_mutates_the_file(self):
        before = self.write([
            json.dumps(envelope()),
            json.dumps(envelope(event_id="e-2", schema_version=99)),
            "{not json",
        ])
        entries_before = sorted(p.name for p in self.root.iterdir())
        ledger_reader.read_ledger(self.path)
        board.read_state_events(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), entries_before)

    def test_a_missing_ledger_reads_as_empty_not_as_an_error(self):
        read = ledger_reader.read_ledger(self.root / "absent.jsonl")
        self.assertEqual(read.canonical, [])
        self.assertEqual(read.diagnostic_count, 0)
        self.assertEqual(read.lines_seen, 0)

    def test_blank_lines_are_not_records(self):
        self.path.write_text(
            json.dumps(envelope()) + "\n\n   \n" + json.dumps(envelope(event_id="e-2")) + "\n",
            encoding="utf-8",
        )
        read = ledger_reader.read_ledger(self.path)
        self.assertEqual(len(read.canonical), 2)
        self.assertEqual(read.diagnostic_count, 0)

    def test_quarantine_records_do_not_carry_the_payload(self):
        # A stray record could hold a capability; the reader must not copy it.
        self.write([json.dumps(envelope(event_id="e-1", extra="sk-secret-value"))])
        quarantined = ledger_reader.read_ledger(self.path).quarantined[0]
        self.assertEqual(
            set(vars(quarantined)),
            {"line_number", "reason", "event", "schema_version"},
        )
        self.assertNotIn("secret", quarantined.describe())


class BoardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-board-reader-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.jsonl"

    def write(self, records):
        self.path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )

    def test_an_invalid_v2_record_never_renders_as_running(self):
        self.write([
            envelope(event_id="e-1", extra="nope", event="subagent_start"),
            envelope(event_id="e-2", schema_version=SCHEMA_VERSION + 1),
        ])
        events, skipped = board.read_state_events(self.path)
        self.assertEqual(events, [])
        self.assertEqual(skipped, 2)
        self.assertEqual(board.build_instances(events), [])

    def test_a_valid_record_still_renders_and_pairs(self):
        self.write([
            envelope(event_id="e-1"),
            envelope(event_id="e-2", event="subagent_stop",
                     occurred_at="2026-09-10T12:01:00+00:00",
                     recorded_at="2026-09-10T12:01:00+00:00"),
        ])
        events, skipped = board.read_state_events(self.path)
        self.assertEqual(skipped, 0)
        instances = board.build_instances(events)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["status"], "done")
        self.assertEqual(instances[0]["event_key"], (RUN, "task-a", 1))

    def test_the_quarantine_report_explains_what_was_set_aside(self):
        self.write([envelope(event_id="e-1", schema_version=SCHEMA_VERSION + 1)])
        summary = board.quarantine_report(self.path)
        self.assertEqual(summary["quarantined"], 1)
        self.assertIn(ledger_reader.UNKNOWN_VERSION, summary["quarantine_reasons"])


class FooterReaderTests(unittest.TestCase):
    """The live footer counts running work, so it must use the same reader."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-footer-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / ".tianji").mkdir()
        self.path = self.root / ".tianji" / "state.jsonl"

    def write(self, records):
        self.path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )

    def test_a_quarantined_record_cannot_become_a_phantom_worker(self):
        import statusline

        self.write([
            envelope(event_id="e-1"),
            envelope(event_id="e-2", extra="nope"),
            envelope(event_id="e-3", schema_version=SCHEMA_VERSION + 1),
            {"ts": "2026-09-01T01:00:00", "event": "subagent_start",
             "agent": "tianji-worker"},
        ])
        events = statusline.read_state(str(self.root))
        self.assertEqual(len(events), 1)
        running, completed, _agent, last_time = statusline.analyze(events)
        self.assertEqual(running, {"tianji-worker": 1})
        self.assertEqual(completed, 0)
        # The canonical record's time is surfaced as `ts` for the footer.
        self.assertEqual(last_time, "2026-09-10T12:00:00+00:00")

    def test_a_missing_ledger_reads_as_none(self):
        import statusline

        self.assertIsNone(statusline.read_state(str(self.root / "nowhere")))


if __name__ == "__main__":
    unittest.main()
