"""Reconcile closes what is provably over, and refuses to guess the rest.

Two different lies sit in one workspace: a run nobody ever closed, and a start
whose worker died. Both are reachable, and both were left alone for as long as
they were -- which is why the board showed dead workers as running. The refusal
cases matter as much as the repairs: synthesizing a stop without identity is
exactly how they came to look alive.
"""
import json
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ledger_sink  # noqa: E402
import run_registry  # noqa: E402
from ledger_reader import read_ledger  # noqa: E402
from ledger_schema import build_event  # noqa: E402


OLD = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
NOW = datetime.now(timezone.utc).isoformat()


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="tianji-reconcile-")
        self.addCleanup(self.temporary.cleanup)
        self.ws = Path(self.temporary.name)

    # -- fixtures ----------------------------------------------------------

    def write_run(self, run_id, *, run_lifecycle="active", task_lifecycle="closed",
                  stamp=OLD):
        run_dir = self.ws / ".tianji" / "runtime" / "runs" / run_id
        attempt_dir = run_dir / "tasks" / "task-a" / "1"
        attempt_dir.mkdir(parents=True)
        for path, body in (
            (run_dir / "run.json", {"run_id": run_id, "host": "cmdc",
                                    "session_id": "session-1",
                                    "lifecycle": run_lifecycle}),
            (attempt_dir / "task.json", {"run_id": run_id, "task_id": "task-a",
                                         "attempt": 1, "role": "tianji-worker",
                                         "lifecycle": task_lifecycle}),
        ):
            body.update({"schema_version": 2, "created_at": stamp, "updated_at": stamp})
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")

    def write_start(self, run_id, task_id="task-a", *, occurred=OLD,
                    invocation_id="inv-1"):
        ledger_sink.append_event(self.ws, build_event(
            event_id=f"e-{run_id}-{task_id}", event="subagent_start", host="cmdc",
            run_id=run_id, session_id="session-1", task_id=task_id, attempt=1,
            invocation_id=invocation_id, correlation_id=invocation_id,
            agent="tianji-worker", occurred_at=occurred, recorded_at=occurred,
            detail={},
        ))

    def issued_run(self):
        """A run and task this registry really issued, so its key verifies."""
        registry = run_registry.RunRegistry(self.ws)
        run = registry.create_run(host="cmdc", session_id="session-1")
        task = registry.create_task(run.run_id, "task-a", role="tianji-worker")
        return registry, run.run_id, task.event_key

    # -- the run surface ---------------------------------------------------

    def test_only_a_finished_run_goes_quietly_and_only_when_it_is_old(self):
        finished = str(uuid.uuid4())
        unfinished = str(uuid.uuid4())
        recent = str(uuid.uuid4())
        self.write_run(finished)
        self.write_run(unfinished, task_lifecycle="active")
        self.write_run(recent, stamp=NOW)

        report = run_registry.reconcile(self.ws)

        self.assertEqual([entry["run_id"] for entry in report["closable_runs"]],
                         [finished])
        self.assertEqual(report["applied"], False)
        reasons = {entry.get("run_id"): entry["why"] for entry in report["held_back"]}
        self.assertEqual(reasons[unfinished], "a task in it is still open")
        self.assertEqual(reasons[recent], "activity is recent")

        # Nothing moved yet: a dry run is a verdict, not a change.
        run_file = self.ws / ".tianji" / "runtime" / "runs" / finished / "run.json"
        self.assertEqual(json.loads(run_file.read_text(encoding="utf-8"))["lifecycle"],
                         "active")

        applied = run_registry.reconcile(self.ws, apply=True)
        self.assertTrue(applied["applied"])
        self.assertEqual(json.loads(run_file.read_text(encoding="utf-8"))["lifecycle"],
                         "closed")
        self.assertEqual(run_registry.reconcile(self.ws)["closable_runs"], [])

    # -- the ledger surface ------------------------------------------------

    def age_run_files(self, run_id):
        """Rewrite every recorded stamp under a run so it looks two days old."""
        run_dir = self.ws / ".tianji" / "runtime" / "runs" / run_id
        for path in [run_dir / "run.json", *sorted(run_dir.rglob("task.json"))]:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["created_at"] = data["updated_at"] = OLD
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")

    def test_repairing_the_ledger_is_what_lets_the_run_close(self):
        # The whole cascade in one pass: an unpaired start closes its task, and
        # that is what makes the run closable. A design that needed two passes
        # would leave the run behind exactly when nobody is watching anymore.
        _, run_id, key = self.issued_run()
        self.write_start(run_id, key.task_id)
        self.age_run_files(run_id)

        report = run_registry.reconcile(self.ws)

        self.assertEqual([entry["event_key"][0] for entry in report["repaired_starts"]],
                         [run_id])
        self.assertEqual([entry["run_id"] for entry in report["closable_runs"]],
                         [run_id])
        self.assertTrue(report["closable_runs"][0]["after_repair"])

        run_registry.reconcile(self.ws, apply=True)

        registry = run_registry.RunRegistry(self.ws)
        self.assertEqual(registry.get_task(key).lifecycle, "closed")
        self.assertEqual(registry.get_run(run_id).lifecycle, "closed")

    def test_a_start_is_repaired_only_when_the_registry_issued_its_key(self):
        _, issued_run, issued_task = self.issued_run()
        self.write_start(issued_run, issued_task.task_id)
        ghost_run = str(uuid.uuid4())
        self.write_start(ghost_run, occurred=OLD)

        report = run_registry.reconcile(self.ws)

        repaired = [entry["event_key"][0] for entry in report["repaired_starts"]]
        unprovable = [entry["event_key"][0] for entry in report["unprovable"]]
        self.assertEqual(repaired, [issued_run])
        self.assertEqual(unprovable, [ghost_run])
        self.assertIn("never issued", report["unprovable"][0]["why"])

    def test_the_repair_is_a_reconciled_stop_and_it_happens_once(self):
        _, issued_run, issued_task = self.issued_run()
        self.write_start(issued_run, issued_task.task_id)

        run_registry.reconcile(self.ws, apply=True)

        stops = [record for record in read_ledger(
            self.ws / ".tianji" / "state.jsonl").canonical
            if record["event"] == "subagent_stop"]
        self.assertEqual(len(stops), 1)
        self.assertTrue(stops[0]["detail"]["reconciled"])
        self.assertTrue(stops[0]["detail"]["compensated"])
        # And a repaired start is no longer unpaired, so a second pass is a no-op.
        again = run_registry.reconcile(self.ws, apply=True)
        self.assertEqual(again["repaired_starts"], [])
        self.assertEqual(len([record for record in read_ledger(
            self.ws / ".tianji" / "state.jsonl").canonical
            if record["event"] == "subagent_stop"]), 1)


if __name__ == "__main__":
    unittest.main()
