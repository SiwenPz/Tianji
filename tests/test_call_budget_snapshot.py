# -*- coding: utf-8 -*-
"""The two holes that were visible and left unfixed.

§1 the number of tool calls a dispatch may make was a spoken promise: no ledger
row, no close-time report, nothing on the footer. §2 an order opened without
`--subject-digest` silently carried no configuration snapshot, so the route
proof could not take it -- and nothing said so at open time.
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ledger_schema  # noqa: E402
import run_registry  # noqa: E402
import statusline  # noqa: E402

REGISTRY = SCRIPTS / "run_registry.py"
WARNING = "路由证明不会采信它"


def _open(tmp, *extra, host="cmdc"):
    return subprocess.run(
        [sys.executable, str(REGISTRY), "open", "--workspace", tmp,
         "--host", host, "--session", "s", "--role", "r", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


def _invocation_record(tmp):
    paths = list((Path(tmp) / ".tianji" / "runtime" / "runs").glob(
        "*/tasks/*/*/invocations/*.json"))
    assert len(paths) == 1, paths
    return json.loads(paths[0].read_text(encoding="utf-8"))


def _cli(argv, facts=None):
    """Run `run_registry._cli` in-process, optionally stubbing the host facts."""
    out, err = io.StringIO(), io.StringIO()
    saved_argv = sys.argv
    saved_facts = run_registry._host_facts
    sys.argv = ["run_registry.py"] + argv
    if facts is not None:
        run_registry._host_facts = lambda host, role: facts
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = run_registry._cli()
    finally:
        sys.argv = saved_argv
        run_registry._host_facts = saved_facts
    return code, out.getvalue(), err.getvalue()


class CallBudgetRecorded(unittest.TestCase):
    """§1① the ceiling has to be written into the same ledger as the other two."""

    def test_open_persists_call_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = _open(tmp, "--call-budget", "16", "--no-snapshot")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(_invocation_record(tmp)["call_budget"], 16)

    def test_a_dispatch_with_no_ceiling_records_zero_not_a_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = _open(tmp, "--no-snapshot")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(_invocation_record(tmp)["call_budget"], 0)


class CallOverrunReported(unittest.TestCase):
    """§1② a closed dispatch has to say what it spent, in the same shape."""

    def _task(self, tmp, *, budget):
        registry = run_registry.RunRegistry(tmp)
        run = registry.create_run(host="cmdc", session_id="s")
        task = registry.create_task(run.run_id, "t-calls", attempt=1, role="r")
        invocation, _ = registry.create_pending_invocation(
            task.event_key, host="cmdc", session_id="s", role="r",
            call_budget=budget,
        )
        now = datetime.now(timezone.utc).isoformat()
        row = ledger_schema.build_event(
            event_id="cmdc:calls", event="subagent_progress", host="cmdc",
            run_id=run.run_id, session_id="s", task_id="t-calls", attempt=1,
            invocation_id=invocation.invocation_id, correlation_id="call-a",
            agent="tianji-worker", occurred_at=now, recorded_at=now,
            detail={"tokensUsed": 100, "toolCalls": 29, "toolName": "read_file"},
        )
        ledger = Path(tmp) / ".tianji" / "state.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(row, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        return registry, task

    def test_a_dispatch_over_its_call_budget_is_reported_at_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, task = self._task(tmp, budget=16)

            status = registry.call_status(task.event_key)
            self.assertEqual(len(status["over_calls"]), 1)
            row = status["over_calls"][0]
            self.assertEqual(row["call_budget"], 16)
            self.assertEqual(row["spent"], 29)
            self.assertEqual(row["over"], 13)

            buf = io.StringIO()
            with redirect_stdout(buf):
                run_registry._print_budget(registry, [task.event_key])
            out = buf.getvalue()
            self.assertIn("[OVER]", out)
            self.assertIn("call_budget=16, 实际=29, 超支=13", out)

    def test_no_call_budget_is_not_an_overrun(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, task = self._task(tmp, budget=0)

            self.assertEqual(registry.call_status(task.event_key)["over_calls"],
                             [])


class SnapshotIdentifiability(unittest.TestCase):
    """§2 which path an order took has to be readable, not inferred."""

    def test_a_deliberate_skip_is_recorded_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = _open(tmp, "--no-snapshot")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            record = _invocation_record(tmp)
            self.assertTrue(record["snapshot_omitted"])
            self.assertEqual(record["snapshot_source"], "omitted")
            self.assertIn(WARNING, proc.stderr)

    def test_an_explicit_digest_is_not_marked_as_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = _open(tmp, "--subject-digest", "explicit-digest")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            record = _invocation_record(tmp)
            self.assertFalse(record["snapshot_omitted"])
            self.assertEqual(record["snapshot_source"], "explicit")
            self.assertEqual(record["subject_digest"], "explicit-digest")
            self.assertNotIn(WARNING, proc.stderr)

    def test_an_order_without_a_digest_takes_the_snapshot_itself(self):
        # The hole: `open --model x --model-source declared` with no digest was
        # a silent fast path that produced an order the route proof could not
        # take. It now reads the snapshot from the host adapter instead.
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = _cli(
                ["open", "--workspace", tmp, "--host", "cmdc",
                 "--session", "s", "--role", "r"],
                facts=("auto", {"subject_digest": "auto-digest", "model": "m",
                                "model_source": "declared"}),
            )

            self.assertEqual(code, 0, err)
            record = _invocation_record(tmp)
            self.assertEqual(record["subject_digest"], "auto-digest")
            self.assertEqual(record["snapshot_source"], "auto")
            self.assertFalse(record["snapshot_omitted"])
            self.assertEqual(record["model"], "m")
            self.assertEqual(record["model_source"], "declared")
            self.assertNotIn(WARNING, err)
            self.assertEqual(json.loads(out)["snapshot_source"], "auto")

    def test_a_failed_read_is_marked_apart_from_a_deliberate_skip(self):
        # A snapshot nobody could read must not look like one that was skipped
        # on purpose: only one of those is a decision.
        with tempfile.TemporaryDirectory() as tmp:
            code, _out, err = _cli(
                ["open", "--workspace", tmp, "--host", "cmdc",
                 "--session", "s", "--role", "r"],
                facts=("auto-failed", None),
            )

            self.assertEqual(code, 0, err)
            record = _invocation_record(tmp)
            self.assertEqual(record["snapshot_source"], "auto-failed")
            self.assertTrue(record["snapshot_omitted"])
            self.assertIn(WARNING, err)

    def test_a_host_with_no_facts_contract_is_left_alone(self):
        # A host whose adapter publishes no `facts` action has no snapshot to
        # take. That is not a failure and must not be reported as one, or the
        # warning becomes noise nobody reads.
        with tempfile.TemporaryDirectory() as tmp:
            code, _out, err = _cli(
                ["open", "--workspace", tmp, "--host", "other",
                 "--session", "s", "--role", "r"],
                facts=("none", None),
            )

            self.assertEqual(code, 0, err)
            record = _invocation_record(tmp)
            self.assertEqual(record["snapshot_source"], "none")
            self.assertFalse(record["snapshot_omitted"])
            self.assertNotIn(WARNING, err)


class CallMeterOnTheFooter(unittest.TestCase):
    """§1③ one more item in the warning slot the footer already has."""

    def test_a_running_dispatch_over_its_call_budget_warns(self):
        events = [{"event": "subagent_progress", "invocation_id": "inv1",
                   "agent": "w", "detail": {"toolCalls": 29}}]

        text, warning = statusline.call_meter_text(events, {"inv1": 16})

        self.assertEqual(text, "调用 29/16!")
        self.assertTrue(warning)

    def test_a_finished_overrun_is_not_a_warning(self):
        events = [
            {"event": "subagent_progress", "invocation_id": "inv1",
             "agent": "w", "detail": {"toolCalls": 29}},
            {"event": "subagent_stop", "invocation_id": "inv1", "agent": "w"},
        ]

        self.assertEqual(statusline.call_meter_text(events, {"inv1": 16}),
                         ("", False))

    def test_no_ceiling_is_left_out(self):
        events = [{"event": "subagent_progress", "invocation_id": "inv1",
                   "agent": "w", "detail": {"toolCalls": 29}}]

        self.assertEqual(statusline.call_meter_text(events, {}), ("", False))

    def test_the_meter_cli_carries_the_call_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = run_registry.RunRegistry(tmp)
            run = registry.create_run(host="cmdc", session_id="session-1")
            task = registry.create_task(run.run_id, "t-loud", attempt=1,
                                        role="r")
            invocation, _ = registry.create_pending_invocation(
                task.event_key, host="cmdc", session_id="session-1", role="r",
                call_budget=16,
            )
            now = datetime.now(timezone.utc).isoformat()
            row = ledger_schema.build_event(
                event_id="cmdc:1", event="subagent_progress", host="cmdc",
                run_id=run.run_id, session_id="session-1", task_id="t-loud",
                attempt=1, invocation_id=invocation.invocation_id,
                correlation_id="call-a", agent="tianji-worker",
                occurred_at=now, recorded_at=now,
                detail={"tokensUsed": 100, "toolCalls": 29,
                        "toolName": "read_file"},
            )
            ledger = Path(tmp) / ".tianji" / "state.jsonl"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(json.dumps(row, ensure_ascii=False) + "\n",
                              encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "statusline.py"), "--meter"],
                input=json.dumps({"cwd": tmp, "sessionId": "session-1"}),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("调用", result.stdout)
        self.assertNotIn("\033", result.stdout)


class HostFactsContractDetection(unittest.TestCase):
    """§2 the snapshot probe must read the adapter, not trust an exit code.

    An exit code of 2 used to mean "this host has no `facts` action". But 2 is
    also what argparse returns for a usage error, so a real read failure, a
    changed flag, or a crash all looked like "no contract" and the snapshot was
    skipped without a word. The cases below are the split that fixes it.
    """

    def setUp(self):
        self._saved_file = run_registry.__file__

    def tearDown(self):
        run_registry.__file__ = self._saved_file

    def _adapter(self, tmp, host, body):
        (Path(tmp) / f"{host}-role-configure.py").write_text(body, encoding="utf-8")

    def _probe(self, tmp, host):
        # `_host_facts` looks for the adapter beside run_registry.py; point that
        # beside the temp dir instead, so a fake adapter stands in for a host.
        run_registry.__file__ = str(Path(tmp) / "run_registry.py")
        return run_registry._host_facts(host, "r")

    def test_a_host_with_no_facts_action_is_none(self):
        # ---------------- §2①: whose usage never lists `facts` ---------------
        with tempfile.TemporaryDirectory() as tmp:
            self._adapter(tmp, "nofacts", (
                "import argparse\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('action', choices=['list', 'set'])\n"
                "p.add_argument('--role', default='')\n"
                "p.parse_args()\n"
            ))
            self.assertEqual(self._probe(tmp, "nofacts"), ("none", None))

    def test_an_ordinary_nonzero_failure_is_auto_failed(self):
        # ------ §2②: has `facts`, but the read died with a plain message -----
        # Old code saw returncode 2 and called it "none": a red light.
        with tempfile.TemporaryDirectory() as tmp:
            self._adapter(tmp, "plainfail", (
                "import argparse, sys\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('action', choices=['facts'])\n"
                "p.add_argument('--role', default='')\n"
                "p.parse_args()\n"
                "print('cannot reach the host', file=sys.stderr)\n"
                "sys.exit(2)\n"
            ))
            self.assertEqual(self._probe(tmp, "plainfail"), ("auto-failed", None))

    def test_an_argparse_usage_error_is_auto_failed(self):
        # ---- §2③: the action exists, but the arguments it takes changed -----
        # `--role` is unknown to this adapter; argparse exits 2. Old code read
        # that 2 as "no contract": another red light.
        with tempfile.TemporaryDirectory() as tmp:
            self._adapter(tmp, "argchange", (
                "import argparse\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('action', choices=['facts'])\n"
                "p.parse_args()\n"
            ))
            self.assertEqual(self._probe(tmp, "argchange"), ("auto-failed", None))

    def test_the_codex_adapter_still_reports_no_contract(self):
        # The real codex adapter publishes no `facts` action today; a host that
        # never had the contract must keep landing on `none` with no warning.
        with tempfile.TemporaryDirectory() as tmp:
            code, _out, err = _cli(
                ["open", "--workspace", tmp, "--host", "codex",
                 "--session", "s", "--role", "r"],
            )
            self.assertEqual(code, 0, err)
            record = _invocation_record(tmp)
            self.assertEqual(record["snapshot_source"], "none")
            self.assertFalse(record["snapshot_omitted"])
            self.assertNotIn(WARNING, err)


if __name__ == "__main__":
    unittest.main()
