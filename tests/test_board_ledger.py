import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import board  # noqa: E402


RUN_A = "77802c0c-d50f-4fe8-bc09-8ea018cf2334"
RUN_B = "34ff8429-0bc2-4b4e-b48c-9b7d7508fbb4"


def envelope(event, agent, run_id=RUN_A, task_id="task-a", attempt=1,
             occurred="2026-09-10T12:00:00+00:00", event_id=None, correlation="call-a"):
    return {
        "schema_version": 2,
        "event_id": event_id or f"e-{event}-{task_id}-{correlation}",
        "event": event,
        "host": "cmdc",
        "run_id": run_id,
        "session_id": "session-1",
        "task_id": task_id,
        "attempt": attempt,
        "invocation_id": "inv-1",
        "correlation_id": correlation,
        "agent": agent,
        "occurred_at": occurred,
        "recorded_at": occurred,
        "detail": {},
    }


class TokenDisplayTests(unittest.TestCase):
    def test_token_counts_stay_short_enough_to_read(self):
        for value, expected in (
            (0, "0"),
            (798, "798"),
            (1000, "1K"),
            (7986, "8K"),
            (64_024, "64K"),
            (108_339, "108.3K"),
            (1_000_000, "1M"),
            (1_405_256, "1.4M"),
            (4_925_816, "4.9M"),
        ):
            with self.subTest(value=value):
                self.assertEqual(board.format_tokens(value), expected)


class BoardLedgerReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-board-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.jsonl"

    def write(self, records):
        self.path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8",
        )

    def test_standard_envelopes_are_read_with_their_event_key(self):
        self.write([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:01:00+00:00"),
        ])
        events, skipped = board.read_state_events(self.path)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_key"], (RUN_A, "task-a", 1))
        self.assertFalse(events[0]["legacy"])

    def test_two_tasks_in_one_run_pair_independently(self):
        self.write([
            envelope("subagent_start", "tianji-worker", task_id="task-a",
                     correlation="call-a"),
            envelope("subagent_start", "tianji-verifier", task_id="task-b",
                     correlation="call-b", occurred="2026-09-10T12:00:30+00:00"),
            envelope("subagent_stop", "tianji-worker", task_id="task-a",
                     correlation="call-a", occurred="2026-09-10T12:01:00+00:00"),
            envelope("subagent_stop", "tianji-verifier", task_id="task-b",
                     correlation="call-b", occurred="2026-09-10T12:02:00+00:00"),
        ])
        events, _ = board.read_state_events(self.path)
        instances = board.build_instances(events)
        self.assertEqual(len(instances), 2)
        by_task = {instance["event_key"][1]: instance for instance in instances}
        self.assertEqual(by_task["task-a"]["status"], "done")
        self.assertEqual(by_task["task-b"]["status"], "done")
        self.assertEqual(by_task["task-a"]["agent"], "tianji-worker")
        self.assertEqual(by_task["task-b"]["agent"], "tianji-verifier")

    def test_same_task_id_in_two_runs_never_merges(self):
        self.write([
            envelope("subagent_start", "tianji-worker", run_id=RUN_A),
            envelope("subagent_start", "tianji-worker", run_id=RUN_B),
        ])
        events, _ = board.read_state_events(self.path)
        instances = board.build_instances(events)
        self.assertEqual(len(instances), 2)
        self.assertEqual({instance["status"] for instance in instances}, {"running"})
        self.assertEqual(len({instance["event_key"] for instance in instances}), 2)

    def test_orphan_stop_is_reported_inside_its_task(self):
        self.write([envelope("subagent_stop", "tianji-worker", task_id="task-z")])
        events, _ = board.read_state_events(self.path)
        instances = board.build_instances(events)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["status"], "orphan_stop")

    def test_legacy_records_still_pair_by_agent(self):
        self.write([
            {"ts": "2026-09-01T01:00:00", "event": "subagent_start",
             "agent": "tianji-worker", "session_id": "old"},
            {"ts": "2026-09-01T01:05:00", "event": "subagent_stop",
             "agent": "tianji-worker", "session_id": "old"},
        ])
        events, skipped = board.read_state_events(self.path)
        self.assertEqual(skipped, 0)
        self.assertTrue(events[0]["legacy"])
        instances = board.build_instances(events)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["status"], "done")
        self.assertNotIn("event_key", instances[0])

    def test_legacy_and_standard_records_coexist(self):
        self.write([
            {"ts": "2026-09-01T01:00:00", "event": "subagent_start",
             "agent": "tianji-worker", "session_id": "old"},
            envelope("subagent_start", "tianji-verifier", task_id="task-b"),
            envelope("subagent_stop", "tianji-verifier", task_id="task-b",
                     occurred="2026-09-10T12:00:30+00:00"),
        ])
        events, _ = board.read_state_events(self.path)
        instances = board.build_instances(events)
        self.assertEqual(len(instances), 2)
        statuses = sorted(instance["status"] for instance in instances)
        self.assertEqual(statuses, ["done", "running"])

    def test_unbound_legacy_rows_from_a_host_are_not_keyed(self):
        # An unbound host row carries schema_version and ts but no identity.
        self.write([{
            "schema_version": 4, "ts": "2026-09-10T12:00:00+00:00",
            "event": "subagent_start", "agent": "tianji-worker",
            "session_id": "s", "legacy": True, "run_id": None, "task_id": None,
        }])
        events, _ = board.read_state_events(self.path)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["legacy"])
        instances = board.build_instances(events)
        self.assertNotIn("event_key", instances[0])

    def render(self, instances):
        # No wire archive: the dispatch record itself has to carry the model.
        extra = board.resolve_models(instances, str(Path(self.temporary.name) / "wire"))
        return board.render_board(instances, 0, extra)

    def test_the_board_reports_tokens_and_which_model_spent_them(self):
        worker_stop = envelope(
            "subagent_stop", "tianji-worker", task_id="task-a", correlation="call-a",
            occurred="2026-09-10T12:01:00+00:00",
        )
        worker_stop["detail"] = {"tokensUsed": 7986, "model": "deepseek/deepseek-v4.1-flash"}
        verifier_stop = envelope(
            "subagent_stop", "tianji-verifier", task_id="task-b", correlation="call-b",
            occurred="2026-09-10T12:02:00+00:00",
        )
        verifier_stop["detail"] = {"tokensUsed": 1000, "model": "z-ai/glm-5.3-flash"}
        self.write([
            envelope("subagent_start", "tianji-worker", task_id="task-a", correlation="call-a"),
            envelope("subagent_start", "tianji-verifier", task_id="task-b", correlation="call-b",
                     occurred="2026-09-10T12:00:30+00:00"),
            worker_stop,
            verifier_stop,
        ])
        events, _ = board.read_state_events(self.path)
        text = self.render(board.build_instances(events))

        # Per-model totals, biggest spender first -- the answer to "who costs what".
        # The vendor prefix is dropped: it repeats on every row and says little.
        self.assertIn(
            "Tokens: 9K total | deepseek-v4.1-flash 8K  glm-5.3-flash 1K",
            text,
        )
        self.assertNotIn("deepseek/deepseek-v4.1-flash", text)
        # The dispatch record names the model even where no wire archive exists.
        self.assertNotIn("?", text)

    def test_two_vendors_sharing_a_name_stay_distinguishable(self):
        # Shortening a label must never merge two models' spend into one number.
        first = envelope("subagent_stop", "tianji-worker", task_id="task-a",
                         correlation="call-a", occurred="2026-09-10T12:01:00+00:00")
        first["detail"] = {"tokensUsed": 500, "model": "zai-org/glm-5.3"}
        second = envelope("subagent_stop", "tianji-verifier", task_id="task-b",
                          correlation="call-b", occurred="2026-09-10T12:02:00+00:00")
        second["detail"] = {"tokensUsed": 300, "model": "z-ai/glm-5.3"}
        self.write([
            envelope("subagent_start", "tianji-worker", task_id="task-a", correlation="call-a"),
            envelope("subagent_start", "tianji-verifier", task_id="task-b", correlation="call-b",
                     occurred="2026-09-10T12:00:30+00:00"),
            first,
            second,
        ])
        events, _ = board.read_state_events(self.path)
        text = self.render(board.build_instances(events))

        self.assertIn("Tokens: 800 total | zai-org/glm-5.3 500  z-ai/glm-5.3 300", text)

    def test_a_silent_host_reads_as_unknown_not_as_zero(self):
        self.write([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:01:00+00:00"),
        ])
        events, _ = board.read_state_events(self.path)
        text = self.render(board.build_instances(events))

        self.assertIn("--", text)
        self.assertNotIn("Tokens:", text)  # nothing was reported, so nothing is claimed

    def test_reader_has_no_host_specific_branch(self):
        source = (SCRIPTS / "board.py").read_text(encoding="utf-8")
        self.assertNotIn("cmdc", source)
        self.assertNotIn("CMDC", source)


if __name__ == "__main__":
    unittest.main()
