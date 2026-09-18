"""P1 in the footer: what the session spent, against what it was allowed."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ledger_reader  # noqa: E402
import ledger_schema  # noqa: E402
import run_registry  # noqa: E402
import statusline  # noqa: E402


RED = "\033[1;38;5;196m"


class MeterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-meter-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = run_registry.RunRegistry(self.root)
        self.run = self.registry.create_run(host="cmdc", session_id="session-1")

    def dispatch(self, task_id="task-a", *, budget=1000, session="session-1"):
        task = self.registry.create_task(self.run.run_id, task_id, role="tianji-worker")
        invocation, _ = self.registry.create_pending_invocation(
            task.event_key, host="cmdc", session_id=session, role="tianji-worker",
            token_budget=budget,
        )
        return invocation

    def events(self, rows):
        now = datetime.now(timezone.utc).isoformat()
        built = [
            ledger_schema.build_event(
                event_id=f"cmdc:{index}", event=kind, host="cmdc",
                run_id=self.run.run_id, session_id="session-1", task_id="task-a",
                attempt=1, invocation_id=invocation_id, correlation_id="call-a",
                agent="tianji-worker", occurred_at=now, recorded_at=now,
                detail=({} if tokens is None else {"tokensUsed": tokens}),
            )
            for index, (kind, invocation_id, tokens) in enumerate(rows)
        ]
        return [event for event in
                (ledger_reader.canonical_event(row) for row in built) if event]


class SpendTests(MeterTests):
    def test_the_newest_running_count_is_the_one_that_counts(self):
        # Progress rows are cumulative snapshots, so the largest is where the
        # dispatch stands -- summing them would multiply the same tokens.
        events = self.events([
            ("subagent_start", "inv-1", None),
            ("subagent_progress", "inv-1", 400),
            ("subagent_progress", "inv-1", 900),
        ])

        self.assertEqual(statusline.spend_by_invocation(events), {"inv-1": 900})

    def test_a_row_without_a_count_is_not_zero(self):
        events = self.events([
            ("subagent_start", "inv-1", None),
            ("subagent_stop", "inv-1", None),
        ])

        self.assertEqual(statusline.spend_by_invocation(events), {})

    def test_a_row_without_an_invocation_is_not_attributed(self):
        # canonical_event defaults a missing invocation id to "", so a reader
        # has to tolerate one -- and must not pile spend onto an empty name.
        events = [{"event": "subagent_progress", "invocation_id": "",
                   "detail": {"tokensUsed": 500}}]

        self.assertEqual(statusline.spend_by_invocation(events), {})


class BudgetReadTests(MeterTests):
    def test_only_this_sessions_dispatches_are_read(self):
        mine = self.dispatch("task-a", budget=1000)
        other = self.dispatch("task-b", budget=9999, session="session-2")

        budgets = statusline.read_budgets(str(self.root), "session-1")

        self.assertEqual(budgets, {mine.invocation_id: 1000})
        self.assertNotIn(other.invocation_id, budgets)

    def test_a_record_that_cannot_be_parsed_is_skipped_not_zeroed(self):
        # An unreadable ceiling must not read as an overrun, and must not read
        # as unlimited either: it is left out, and the meter says less.
        invocation = self.dispatch("task-a", budget=1000)
        key = run_registry.EventKey(self.run.run_id, "task-a", 1)
        record_path = self.registry._invocation_path(key, invocation.invocation_id)
        self.assertTrue(record_path.is_file())
        record_path.write_text("{ this is not json", encoding="utf-8")

        self.assertEqual(statusline.read_budgets(str(self.root), "session-1"), {})

    def test_a_ceiling_that_is_not_a_number_costs_only_its_record(self):
        # Found by review: int() sat outside the guard, so one malformed record
        # raised out of read_budgets and the whole meter disappeared -- and a
        # meter that vanishes reads as "nothing spent".
        good = self.dispatch("task-a", budget=1000)
        bad = self.dispatch("task-b", budget=1000)
        path = self.registry._invocation_path(
            run_registry.EventKey(self.run.run_id, "task-b", 1), bad.invocation_id,
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        record["token_budget"] = "not a number"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

        budgets = statusline.read_budgets(str(self.root), "session-1")

        self.assertEqual(budgets, {good.invocation_id: 1000})

    def test_no_session_means_no_budgets(self):
        self.dispatch("task-a")

        self.assertEqual(statusline.read_budgets(str(self.root), ""), {})


class MeterLineTests(MeterTests):
    def meter_for(self, rows, budget=1000):
        invocation = self.dispatch("task-a", budget=budget)
        events = self.events([(kind, invocation.invocation_id, tokens)
                              for kind, tokens in rows])
        budgets = statusline.read_budgets(str(self.root), "session-1")
        return statusline.budget_meter(events, budgets)

    def test_a_dispatch_inside_its_budget_is_not_red(self):
        meter, warning = self.meter_for(
            [("subagent_progress", 900), ("subagent_stop", 900)],
        )

        self.assertIn("tok 900/1K", meter)
        self.assertNotIn(RED, meter)
        self.assertFalse(warning)

    def test_a_running_dispatch_over_its_budget_is_named_with_its_own_ratio(self):
        # The ratio shown is the dispatch the mark is about, not the session:
        # "35% of everything" next to a warning makes the reader work out which
        # of the two numbers it refers to.
        meter, warning = self.meter_for([("subagent_progress", 1500)])

        self.assertIn("tok 1.5K/1K", meter)
        self.assertIn(RED, meter)
        self.assertTrue(meter.endswith("!\033[0m"))
        self.assertTrue(warning)

    def test_a_finished_overrun_is_not_a_warning(self):
        # It is a fact about the past: close-task and the board report it, and a
        # footer that stays red about it would be showing an old value as if it
        # were the current one.
        meter, warning = self.meter_for([("subagent_stop", 1500)])

        self.assertFalse(warning)
        self.assertNotIn(RED, meter)
        self.assertNotIn("!", meter)

    def test_a_finished_overrun_still_counts_toward_the_session_ratio(self):
        meter, _ = self.meter_for([("subagent_stop", 1500)])

        self.assertIn("tok 1.5K/1K", meter)

    def test_a_running_dispatch_over_budget_is_flagged_before_it_stops(self):
        # The point of the meter: see it go over while it is still running,
        # from the progress rows, not from the stop that may never arrive.
        meter, warning = self.meter_for([("subagent_progress", 4000)])

        self.assertIn(RED, meter)
        self.assertTrue(warning)

    def test_an_unbudgeted_dispatch_is_not_charged_to_anothers_ceiling(self):
        # The first version of this meter summed every dispatch's spend against
        # whichever ceilings existed, and showed "tok 1M/50K!" for a session
        # whose only ceiling was 50K on one small dispatch. A number that
        # compares the wrong two things is worse than no number.
        budgeted = self.dispatch("task-a", budget=50_000)
        unbudgeted = self.dispatch("task-b", budget=0)
        events = self.events([
            ("subagent_progress", budgeted.invocation_id, 72_000),
            ("subagent_stop", unbudgeted.invocation_id, 1_400_000),
        ])
        budgets = statusline.read_budgets(str(self.root), "session-1")

        meter, warning = statusline.budget_meter(events, budgets)

        self.assertIn("tok 72K/50K", meter)
        self.assertNotIn("1.4M", meter)
        self.assertIn(RED, meter)
        self.assertTrue(warning)

    def test_a_dispatch_with_no_ceiling_does_not_join_the_ceiling(self):
        invocation = self.dispatch("task-a", budget=0)
        events = self.events([("subagent_stop", invocation.invocation_id, 50_000)])
        budgets = statusline.read_budgets(str(self.root), "session-1")

        meter, warning = statusline.budget_meter(events, budgets)

        self.assertIn("tok 50K", meter)
        self.assertNotIn("/", meter)
        self.assertNotIn(RED, meter)
        self.assertFalse(warning)

    def test_the_worst_live_overrun_is_the_one_shown(self):
        first = self.dispatch("task-a", budget=1000)
        second = self.dispatch("task-b", budget=1000)
        events = self.events([
            ("subagent_progress", first.invocation_id, 1500),
            ("subagent_progress", second.invocation_id, 2000),
        ])
        budgets = statusline.read_budgets(str(self.root), "session-1")

        meter, warning = statusline.budget_meter(events, budgets)

        self.assertTrue(warning)
        # task-b is over by 1000, task-a by 500: the worse one is what a reader
        # has to act on, and the session total would have said neither.
        self.assertIn("tok 2K/1K", meter)
        self.assertNotIn("3.5K", meter)

    def test_a_live_dispatch_inside_its_ceiling_does_not_warn_for_another(self):
        # One dispatch over its ceiling does not put a mark on a dispatch that
        # is fine: the mark follows the offending dispatch.
        fine = self.dispatch("task-a", budget=10_000)
        events = self.events([("subagent_progress", fine.invocation_id, 900)])
        budgets = statusline.read_budgets(str(self.root), "session-1")

        meter, warning = statusline.budget_meter(events, budgets)

        self.assertFalse(warning)
        self.assertIn("tok 900/10K", meter)

    def test_nothing_dispatched_means_nothing_drawn(self):
        self.assertEqual(statusline.budget_meter([], {}), ("", False))


class MeterTextTests(MeterTests):
    """The same reading with no colour in it.

    Command Code's footer is assembled by the host mod, which asks for the number
    here instead of re-deriving it: the meter was written into this script alone
    and the footer that readers actually look at never showed it, because the two
    hosts draw their own lines.
    """

    def meter_for(self, rows, budget=1000):
        invocation = self.dispatch("task-a", budget=budget)
        events = self.events([(kind, invocation.invocation_id, tokens)
                              for kind, tokens in rows])
        budgets = statusline.read_budgets(str(self.root), "session-1")
        return statusline.meter_text(events, budgets)

    def test_a_live_overrun_reads_the_same_without_colour(self):
        text, warning = self.meter_for([("subagent_progress", 1500)])

        self.assertEqual(text, "tok 1.5K/1K!")
        self.assertTrue(warning)
        self.assertNotIn("\033", text)

    def test_a_quiet_session_is_plain_text_and_not_a_warning(self):
        text, warning = self.meter_for([("subagent_stop", 900)])

        self.assertEqual(text, "tok 900/1K")
        self.assertFalse(warning)

    def test_nothing_to_say_is_empty_not_zero(self):
        self.assertEqual(statusline.meter_text([], {}), ("", False))

    def test_the_total_does_not_shrink_as_the_ledger_grows(self):
        # Found by a dispatcher's own check, not by review: the meter was built
        # from the footer's 64KB tail window, so once the ledger outgrew it the
        # budgeted dispatch's rows fell out of the window and the number quietly
        # dropped (`tok 1M/2.6M` became `tok 962.3K/2.5M`) while still looking
        # authoritative. The first assertion is the control: it shows the old
        # reading really does lose it, so the second is about the defect.
        invocation = self.dispatch("task-a", budget=1000)
        now = datetime.now(timezone.utc).isoformat()

        def row(index, event, invocation_id, session, tokens):
            return ledger_schema.build_event(
                event_id=f"cmdc:{index}", event=event, host="cmdc",
                run_id=self.run.run_id, session_id=session, task_id="task-a",
                attempt=1, invocation_id=invocation_id, correlation_id="call-a",
                agent="tianji-worker", occurred_at=now, recorded_at=now,
                detail={"tokensUsed": tokens, "toolName": "read_file",
                        "padding": "x" * 400},
            )

        rows = [row(0, "subagent_progress", invocation.invocation_id,
                    "session-1", 1500)]
        rows += [row(f"f{index}", "subagent_progress", f"inv-filler-{index}",
                     "another-session", 1000) for index in range(400)]
        ledger = self.root / ".tianji" / "state.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n",
            encoding="utf-8",
        )

        budgets = statusline.read_budgets(str(self.root), "session-1")

        self.assertEqual(
            statusline.meter_text(statusline.read_state(str(self.root)), budgets),
            ("", False),
        )
        self.assertEqual(
            statusline.meter_text(statusline.read_ledger(str(self.root)), budgets)[0],
            "tok 1.5K/1K!",
        )

    def test_the_cli_prints_one_plain_line_for_the_host_that_draws_its_own(self):
        invocation = self.dispatch("task-a", budget=1000)
        now = datetime.now(timezone.utc).isoformat()
        row = ledger_schema.build_event(
            event_id="cmdc:1", event="subagent_progress", host="cmdc",
            run_id=self.run.run_id, session_id="session-1", task_id="task-a",
            attempt=1, invocation_id=invocation.invocation_id,
            correlation_id="call-a", agent="tianji-worker",
            occurred_at=now, recorded_at=now,
            detail={"tokensUsed": 1500, "toolName": "read_file"},
        )
        ledger = self.root / ".tianji" / "state.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "statusline.py"), "--meter"],
            input=json.dumps({"cwd": str(self.root), "sessionId": "session-1"}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "tok 1.5K/1K!")
        self.assertNotIn("\033", result.stdout)


    def test_the_rendered_line_keeps_the_meter_out_of_an_idle_footer(self):
        # The meter is a live instrument: with nothing running there is no
        # "currently over" to watch, and a number nobody acts on is noise.
        invocation = self.dispatch("task-a", budget=1000)
        now = datetime.now(timezone.utc).isoformat()

        def write(target, kind, tokens):
            row = ledger_schema.build_event(
                event_id=f"cmdc:{kind}:{target.invocation_id}", event=kind, host="cmdc",
                run_id=self.run.run_id, session_id="session-1", task_id="task-a",
                attempt=1, invocation_id=target.invocation_id,
                correlation_id="call-a", agent="tianji-worker",
                occurred_at=now, recorded_at=now,
                detail={"tokensUsed": tokens, "toolName": "read_file"},
            )
            ledger = self.root / ".tianji" / "state.jsonl"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(ledger, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

        def render():
            return subprocess.run(
                [sys.executable, str(SCRIPTS / "statusline.py")],
                input=json.dumps({"cwd": str(self.root), "sessionId": "session-1"}),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )

        write(invocation, "subagent_stop", 900)
        idle = render()
        self.assertEqual(idle.returncode, 0, idle.stderr)
        self.assertNotIn("tok ", idle.stdout)

        # A second dispatch, still open: now the reading appears. (A progress row
        # after a stop would not do it -- a stop is the last word on a dispatch,
        # which is the whole point of the running reducer.)
        live = self.dispatch("task-b", budget=5000)
        write(live, "subagent_progress", 1200)
        running = render()
        self.assertEqual(running.returncode, 0, running.stderr)
        self.assertIn("tok ", running.stdout)


class PlacementTests(unittest.TestCase):
    """Where the meter goes, which is the whole reason it is not simply appended.

    Found by review: drawn first in the tianji segment it was still cut off when
    the head ran long, because truncation happens at the right edge and the head
    is not a fixed width.
    """

    SEP = " | "

    def test_a_warning_leads_the_line_even_when_the_head_is_long(self):
        meter = "\033[1;38;5;196mtok 9.9M/6M!\033[0m"
        head = "long-model-name " + "directory " * 10

        line = statusline.compose_line(head, self.SEP + "tianji(x)", meter, True,
                                       self.SEP)
        cut = statusline.truncate_visible(line, 60)

        self.assertTrue(line.startswith(meter))
        self.assertIn("tok 9.9M/6M!", statusline.ANSI_ESCAPE.sub("", cut))

    def test_an_ordinary_meter_does_not_displace_the_head(self):
        # Inside budget the meter is information, not a warning: the model the
        # user is watching keeps the front of the line.
        meter = "\033[38;5;245mtok 900/1K\033[0m"

        line = statusline.compose_line("model", self.SEP + "tianji(x)", meter,
                                       False, self.SEP)

        self.assertTrue(line.startswith("model"))
        self.assertIn("tok 900/1K", line)

    def test_no_meter_leaves_the_line_exactly_as_it_was(self):
        self.assertEqual(
            statusline.compose_line("model", self.SEP + "tianji(x)", "", False,
                                    self.SEP),
            "model" + self.SEP + "tianji(x)",
        )

    def test_a_lone_meter_needs_no_dangling_separator(self):
        meter = "\033[38;5;245mtok 900/1K\033[0m"

        self.assertEqual(
            statusline.compose_line("model", "", meter, False, self.SEP),
            "model" + self.SEP + meter,
        )
        self.assertEqual(
            statusline.compose_line("model", "", meter, True, self.SEP),
            meter + self.SEP + "model",
        )


if __name__ == "__main__":
    unittest.main()
