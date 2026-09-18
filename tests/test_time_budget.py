# -*- coding: utf-8 -*-
"""Time budget: the clock must be written down, not just promised in the book."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_registry  # noqa: E402
import statusline  # noqa: E402
from ledger_schema import EventKey  # noqa: E402

REGISTRY = SCRIPTS / "run_registry.py"


def _open_workspace(tmp, minutes):
    proc = subprocess.run(
        [sys.executable, str(REGISTRY), "open",
         "--workspace", tmp, "--host", "h", "--session", "s",
         "--role", "r", "--time-budget", str(minutes)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _invocation_json(tmp):
    root = Path(tmp) / ".tianji" / "runtime" / "runs"
    paths = list(root.glob("*/tasks/*/*/invocations/*.json"))
    assert len(paths) == 1, paths
    return paths[0]


def _backdate(path, minutes):
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    record["created_at"] = (now - timedelta(minutes=minutes)).isoformat()
    record["updated_at"] = now.isoformat()
    Path(path).write_text(json.dumps(record), encoding="utf-8")


class OpenRecordsTimeBudget(unittest.TestCase):
    def test_open_persists_time_budget_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = _open_workspace(tmp, 8)
            record = json.loads(_invocation_json(tmp).read_text(encoding="utf-8"))
            self.assertEqual(record["invocation_id"], payload["invocation_id"])
            self.assertEqual(record["time_budget_minutes"], 8)


class CloseReportsTimeOverrun(unittest.TestCase):
    def _task(self, tmp, task_id):
        registry = run_registry.RunRegistry(tmp)
        run = registry.create_run(host="h", session_id="s")
        task = registry.create_task(run.run_id, task_id, attempt=1, role="r")
        return registry, task

    def test_over_time_is_reported_at_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, task = self._task(tmp, "t-time")
            inv, _ = registry.create_pending_invocation(
                task.event_key, host="h", session_id="s", role="r",
                time_budget_minutes=1,
            )
            path = registry._invocation_path(task.event_key, inv.invocation_id)
            _backdate(path, 10)
            status = registry.time_status(task.event_key)
            self.assertEqual(len(status["over_time"]), 1)
            self.assertEqual(status["over_time"][0]["budget"], 1)
            self.assertGreaterEqual(status["over_time"][0]["spent"], 9.0)
            buf = io.StringIO()
            with redirect_stdout(buf):
                run_registry._print_budget(registry, [task.event_key])
            out = buf.getvalue()
            self.assertIn("[OVER]", out)
            self.assertIn("\u5206\u949f", out)

    def test_no_time_budget_is_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, task = self._task(tmp, "t-none")
            inv, _ = registry.create_pending_invocation(
                task.event_key, host="h", session_id="s", role="r",
            )
            path = registry._invocation_path(task.event_key, inv.invocation_id)
            _backdate(path, 120)
            self.assertEqual(registry.time_status(task.event_key)["over_time"], [])


class StatuslineTimeMeter(unittest.TestCase):
    def _events(self, started, stopped=False):
        evs = [{"event": "subagent_progress", "invocation_id": "inv1",
                "agent": "w", "ts": started}]
        if stopped:
            evs.append({"event": "subagent_stop", "invocation_id": "inv1",
                        "agent": "w", "ts": started})
        return evs

    def test_running_over_time_warns(self):
        now = datetime.now(timezone.utc)
        opened = (now - timedelta(minutes=9)).isoformat()
        text, warn = statusline.time_meter_text(self._events(opened), {"inv1": (8, opened)}, now)
        self.assertTrue(warn)
        self.assertIn("\u65f6\u95f4", text)

    def test_finished_over_time_is_not_marked(self):
        now = datetime.now(timezone.utc)
        opened = (now - timedelta(minutes=9)).isoformat()
        text, warn = statusline.time_meter_text(
            self._events(opened, stopped=True), {"inv1": (8, opened)}, now)
        self.assertEqual((text, warn), ("", False))

    def test_inside_budget_is_quiet(self):
        now = datetime.now(timezone.utc)
        opened = (now - timedelta(minutes=1)).isoformat()
        self.assertEqual(
            statusline.time_meter_text(self._events(opened), {"inv1": (8, opened)}, now),
            ("", False))

    def test_no_ceiling_is_left_out(self):
        now = datetime.now(timezone.utc)
        opened = (now - timedelta(minutes=99)).isoformat()
        self.assertEqual(
            statusline.time_meter_text(self._events(opened), {}, now), ("", False))

    def test_budget_meter_leads_with_time_warning(self):
        now = datetime.now(timezone.utc)
        opened = (now - timedelta(minutes=9)).isoformat()
        text, warn = statusline.budget_meter(
            self._events(opened), {}, {"inv1": (8, opened)}, now)
        self.assertTrue(warn)
        self.assertIn("\033[1;38;5;196m", text)

    def test_read_time_limits_reads_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            _open_workspace(tmp, 8)
            limits = statusline.read_time_limits(tmp, "s")
            self.assertEqual(list(limits.values())[0][0], 8)
            self.assertEqual(statusline.read_time_limits(tmp, "other"), {})

    def _over_time_workspace(self, tmp, *, budget=3, elapsed=10, session="s"):
        """A dispatch that is still running and already past its ceiling."""
        from ledger_schema import build_event

        registry = run_registry.RunRegistry(tmp)
        run = registry.create_run(host="h", session_id=session)
        task = registry.create_task(run.run_id, "t-slow", attempt=1, role="r")
        invocation, _ = registry.create_pending_invocation(
            task.event_key, host="h", session_id=session, role="r",
            time_budget_minutes=budget,
        )
        _backdate(registry._invocation_path(task.event_key, invocation.invocation_id),
                  elapsed)
        now = datetime.now(timezone.utc).isoformat()
        row = build_event(
            event_id="cmdc:slow", event="subagent_progress", host="h",
            run_id=run.run_id, session_id=session, task_id="t-slow", attempt=1,
            invocation_id=invocation.invocation_id, correlation_id="call-slow",
            agent="tianji-worker", occurred_at=now, recorded_at=now,
            detail={"tokensUsed": 100, "toolName": "read_file"},
        )
        ledger = Path(tmp) / ".tianji" / "state.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        return invocation.invocation_id

    def _meter(self, tmp, session="s"):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "statusline.py"), "--meter"],
            input=json.dumps({"cwd": tmp, "sessionId": session}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def test_the_meter_cli_carries_the_time_warning(self):
        # Regression gate for the line the user actually reads. The time warning
        # used to be composed inside `budget_meter`, but the Command Code mod
        # draws its own line from `statusline.py --meter`, which called
        # `meter_text` and never saw a clock -- so on that host no time warning
        # appeared at all. A warning on one of two lines is a warning the reader
        # of the other never gets.
        with tempfile.TemporaryDirectory() as tmp:
            self._over_time_workspace(tmp)
            result = self._meter(tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\u65f6\u95f4", result.stdout)
        self.assertIn("\u5206!", result.stdout)
        self.assertNotIn("\033", result.stdout)

    def test_the_meter_cli_stays_quiet_inside_the_ceiling(self):
        # The control: the same path, a dispatch with time left, says nothing
        # about time -- so the assertion above is about the overrun.
        with tempfile.TemporaryDirectory() as tmp:
            self._over_time_workspace(tmp, budget=60, elapsed=1)
            result = self._meter(tmp)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\u65f6\u95f4", result.stdout)
        self.assertNotIn("\033", result.stdout)


if __name__ == "__main__":
    unittest.main()
