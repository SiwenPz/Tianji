"""P1: a budget is written at dispatch, reported at close, and never enforced."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "tianji" / "scripts"))

import ledger_reader  # noqa: E402
import ledger_schema  # noqa: E402
import run_registry  # noqa: E402


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-budget-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = run_registry.RunRegistry(self.root)
        self.run = self.registry.create_run(host="cmdc", session_id="session-1")
        self.task = self.registry.create_task(
            self.run.run_id, "task-a", role="tianji-worker",
        )
        self.invocation, _ = self.registry.create_pending_invocation(
            self.task.event_key, host="cmdc", session_id="session-1",
            role="tianji-worker", token_budget=1000,
        )
        self.rows = []

    def event(self, kind, *, tokens=None, invocation_id=""):
        now = datetime.now(timezone.utc).isoformat()
        detail = {} if tokens is None else {"tokensUsed": tokens}
        self.rows.append(ledger_schema.build_event(
            event_id=f"cmdc:{kind}-{len(self.rows)}", event=kind, host="cmdc",
            run_id=self.run.run_id, session_id="session-1", task_id="task-a",
            attempt=1, invocation_id=invocation_id or self.invocation.invocation_id,
            correlation_id="call-a", agent="tianji-worker",
            occurred_at=now, recorded_at=now, detail=detail,
        ))

    def flush(self):
        path = self.root / ".tianji" / "state.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows),
            encoding="utf-8",
        )

    def test_only_the_stop_carries_the_total(self):
        # Progress rows hold a running count as of each turn, so adding them up
        # counts one dispatch's tokens once per turn.
        self.event("subagent_start")
        self.event("subagent_progress", tokens=400)
        self.event("subagent_progress", tokens=900)
        self.event("subagent_stop", tokens=900)
        self.flush()

        self.assertEqual(
            self.registry.spend_by_invocation(self.task.event_key),
            {self.invocation.invocation_id: 900},
        )

    def test_progress_without_a_stop_is_not_an_amount_spent(self):
        # A dispatch still running has no total yet: reporting its running count
        # as the amount spent would call every dispatch over budget.
        self.event("subagent_start")
        self.event("subagent_progress", tokens=5_000_000)
        self.flush()

        self.assertEqual(self.registry.spend_by_invocation(self.task.event_key), {})

    def test_a_dispatch_over_its_budget_is_named_with_how_far_over(self):
        self.event("subagent_stop", tokens=1500)
        self.flush()

        status = self.registry.budget_status(self.task.event_key)

        self.assertEqual(len(status["over_budget"]), 1)
        self.assertEqual(status["over_budget"][0]["budget"], 1000)
        self.assertEqual(status["over_budget"][0]["spent"], 1500)
        self.assertEqual(status["over_budget"][0]["over_percent"], 50)

    def test_a_dispatch_inside_its_budget_is_not_reported(self):
        self.event("subagent_stop", tokens=999)
        self.flush()

        status = self.registry.budget_status(self.task.event_key)

        self.assertEqual(status["over_budget"], [])
        self.assertEqual(status["unbudgeted"], [])

    def test_a_dispatch_with_no_budget_is_not_reported_as_within_one(self):
        # "nobody set a budget" and "inside budget" are different facts, and
        # only one of them is reassuring.
        unbudgeted, _ = self.registry.create_pending_invocation(
            self.task.event_key, host="cmdc", session_id="session-1",
            role="tianji-worker",
        )
        self.event("subagent_stop", tokens=4_000,
                   invocation_id=unbudgeted.invocation_id)
        self.flush()

        status = self.registry.budget_status(self.task.event_key)

        self.assertEqual(status["over_budget"], [])
        self.assertEqual(
            [row["invocation_id"] for row in status["unbudgeted"]],
            [unbudgeted.invocation_id],
        )

    def test_another_tasks_spend_is_not_counted_here(self):
        self.event("subagent_stop", tokens=1500)
        self.flush()

        other = run_registry.EventKey(self.run.run_id, "task-b", 1)

        self.assertEqual(self.registry.spend_by_invocation(other), {})

    def test_closing_says_an_overrun_out_loud(self):
        self.event("subagent_stop", tokens=1500)
        self.flush()
        stream = io.StringIO()

        with contextlib.redirect_stdout(stream):
            run_registry._print_budget(self.registry, [self.task.event_key])

        printed = stream.getvalue()
        self.assertIn("[OVER]", printed)
        self.assertIn("+50%", printed)
        self.assertIn("1,500", printed)

    def test_the_budget_is_reported_and_never_enforced(self):
        # Nothing kills a worker mid-flight: the overrun is a fact to read, not
        # a trigger. The dispatch still closes normally.
        self.event("subagent_stop", tokens=999_999)
        self.flush()

        status = self.registry.budget_status(self.task.event_key)

        self.assertEqual(len(status["over_budget"]), 1)
        self.assertEqual(
            self.registry.get_invocation(
                self.task.event_key, self.invocation.invocation_id,
            ).token_budget,
            1000,
        )


class DispatchTokensTests(unittest.TestCase):
    def test_a_row_that_is_not_a_stop_has_no_total(self):
        for kind in ("subagent_start", "subagent_progress", "agent_receipt"):
            self.assertIsNone(
                ledger_reader.dispatch_tokens(
                    {"event": kind, "detail": {"tokensUsed": 10}},
                ),
            )

    def test_a_stop_without_a_reported_count_has_no_total(self):
        # None is "the host reported nothing", which is not zero.
        self.assertIsNone(ledger_reader.dispatch_tokens(
            {"event": "subagent_stop", "detail": {}},
        ))
        self.assertIsNone(ledger_reader.dispatch_tokens(
            {"event": "subagent_stop", "detail": {"tokensUsed": 0}},
        ))
        self.assertIsNone(ledger_reader.dispatch_tokens({"event": "subagent_stop"}))

    def test_a_stop_with_a_count_returns_it(self):
        self.assertEqual(
            ledger_reader.dispatch_tokens(
                {"event": "subagent_stop", "detail": {"tokensUsed": 4291}},
            ),
            4291,
        )
