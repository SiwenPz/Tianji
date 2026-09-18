"""Regression coverage for Tianji configuration and resume model matching."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
POOL_CONFIGURE = SCRIPTS / "pool-configure.py"
INSTALL = ROOT / "install.py"
STATE_LOG = SCRIPTS / "state-log.py"
TIANJI_SKILL = ROOT / "skills" / "tianji" / "SKILL.md"
CODEX_ROLE_CONFIGURE = SCRIPTS / "codex-role-configure.py"
TIANJI_INIT = SCRIPTS / "tianji-init.py"
sys.path.insert(0, str(SCRIPTS))

import board  # noqa: E402
import statusline  # noqa: E402


class SetCtxRegressionTests(unittest.TestCase):
    def run_set_ctx(self, config: Path, *specs: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(POOL_CONFIGURE), "--config", str(config)]
        for spec in specs:
            command.extend(("--set-ctx", spec))
        return subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)

    def test_duplicate_alias_is_rejected_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            original = '[models."worker"]\nmodel = "worker-v1"\n'
            config.write_text(original, encoding="utf-8")

            result = self.run_set_ctx(config, "worker=100", "worker=200")

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertEqual(list(config.parent.glob("config.toml.bak-*")), [])

    def test_noncanonical_model_header_is_rejected_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            original = '[models.worker]\nmodel = "worker-v1"\n'
            config.write_text(original, encoding="utf-8")

            result = self.run_set_ctx(config, "worker=100")

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertEqual(list(config.parent.glob("config.toml.bak-*")), [])

    def test_canonical_model_header_updates_with_backup_and_valid_toml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            config.write_text('[models."worker"]\nmodel = "worker-v1"\n', encoding="utf-8")

            result = self.run_set_ctx(config, "worker=100")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                tomllib.loads(config.read_text(encoding="utf-8"))["models"]["worker"]["max_context_size"],
                100,
            )
            self.assertEqual(len(list(config.parent.glob("config.toml.bak-*"))), 1)


class ResumeModelMatchingRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.wire_root = Path(self.temp_dir.name) / "sessions"
        self.session_id = "session-1"
        self.resume_at = datetime(2026, 9, 7, 10, 0, 0)
        self.resume_ts = self.resume_at.strftime("%Y-%m-%dT%H:%M:%S")

        wire = self.wire_root / "project" / self.session_id / "agents" / "agent-1" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        first_run = datetime(2026, 9, 7, 9, 0, 0)
        events = [
            {"time": int(first_run.timestamp() * 1000), "modelAlias": "deepseek-v4"},
            {"time": int(self.resume_at.timestamp() * 1000), "type": "resume"},
        ]
        wire.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        historical_wire = self.wire_root / "project" / self.session_id / "agents" / "agent-2" / "wire.jsonl"
        historical_wire.parent.mkdir(parents=True)
        historical_events = [
            {"time": int(datetime(2026, 9, 7, 8, 0, 0).timestamp() * 1000), "modelAlias": "old-model"},
            {"time": int(first_run.timestamp() * 1000), "type": "completed"},
        ]
        historical_wire.write_text(
            "".join(json.dumps(event) + "\n" for event in historical_events), encoding="utf-8"
        )

    def test_board_uses_recent_wire_activity_for_resume(self) -> None:
        instances = [{
            "session_id": self.session_id,
            "agent": "unmatched-agent",
            "start_ts": self.resume_ts,
        }]

        board.resolve_models(instances, str(self.wire_root))

        self.assertEqual(instances[0]["model"], "deepseek-v4")

    def test_board_prefers_wire_start_time_for_normal_run(self) -> None:
        first_run = "2026-09-07T09:00:00"
        instances = [{
            "session_id": self.session_id,
            "agent": "unmatched-agent",
            "start_ts": first_run,
        }]

        board.resolve_models(instances, str(self.wire_root))

        self.assertEqual(instances[0]["model"], "deepseek-v4")

    def test_board_keeps_question_mark_without_time_evidence(self) -> None:
        instances = [{
            "session_id": self.session_id,
            "agent": "unmatched-agent",
            "start_ts": "2026-09-07T12:00:00",
        }]

        board.resolve_models(instances, str(self.wire_root))

        self.assertEqual(instances[0]["model"], "?")

    def test_statusline_uses_recent_wire_activity_for_resume(self) -> None:
        old_root = statusline._WIRE_ROOT
        self.addCleanup(setattr, statusline, "_WIRE_ROOT", old_root)
        statusline._WIRE_ROOT = str(self.wire_root)
        events = [{
            "event": "subagent_start",
            "agent": "unmatched-agent",
            "ts": self.resume_ts,
        }]

        result = statusline._resolve_agent_wire_map(
            self.session_id, {"unmatched-agent": 1}, events
        )

        self.assertEqual(result["unmatched-agent"]["model_alias"], "deepseek-v4")

    def test_statusline_keeps_no_match_without_time_evidence(self) -> None:
        old_root = statusline._WIRE_ROOT
        self.addCleanup(setattr, statusline, "_WIRE_ROOT", old_root)
        statusline._WIRE_ROOT = str(self.wire_root)
        events = [{
            "event": "subagent_start",
            "agent": "unmatched-agent",
            "ts": "2026-09-07T12:00:00",
        }]

        result = statusline._resolve_agent_wire_map(
            self.session_id, {"unmatched-agent": 1}, events
        )

        self.assertNotIn("unmatched-agent", result)


class StatuslinePerformanceRegressionTests(unittest.TestCase):
    def test_resolve_120_wires_within_statusline_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wire_root = Path(temp_dir) / "sessions"
            session_id = "performance-session"
            started_at = datetime(2026, 9, 7, 10, 0, 0)
            for number in range(120):
                wire = wire_root / "project" / session_id / "agents" / f"agent-{number}" / "wire.jsonl"
                wire.parent.mkdir(parents=True)
                at = started_at.replace(second=number % 60)
                events = [
                    {"time": int(at.timestamp() * 1000), "modelAlias": f"model-{number}"},
                    {"time": int(at.timestamp() * 1000), "type": "completed"},
                ]
                wire.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

            old_root = statusline._WIRE_ROOT
            self.addCleanup(setattr, statusline, "_WIRE_ROOT", old_root)
            statusline._WIRE_ROOT = str(wire_root)
            events = [{
                "event": "subagent_start",
                "agent": "unmatched-agent",
                "ts": started_at.strftime("%Y-%m-%dT%H:%M:%S"),
            }]

            started = time.perf_counter()
            result = statusline._resolve_agent_wire_map(
                session_id, {"unmatched-agent": 1}, events
            )
            elapsed = time.perf_counter() - started

            self.assertIn("unmatched-agent", result)
            self.assertLess(elapsed, 0.3, f"wire resolution took {elapsed:.3f}s")


class CodexHostRegressionTests(unittest.TestCase):
    RUN = "77802c0c-d50f-4fe8-bc09-8ea018cf2334"

    def _dispatch(self, temp_dir: str, *, task_id: str = "task-a") -> str:
        """Open a run/task/pending invocation and return its invocation id."""
        import run_registry

        registry = run_registry.RunRegistry(temp_dir)
        run = registry.create_run(host="cmdc", session_id="s1", run_id=self.RUN)
        task = registry.create_task(run.run_id, task_id, role="tianji-worker")
        invocation, _token = registry.create_pending_invocation(
            task.event_key, host="cmdc", session_id="s1", role="tianji-worker",
        )
        return invocation.invocation_id

    def test_compensation_without_an_event_key_is_refused(self) -> None:
        # An identity-less compensation is how dead workers looked alive.
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, str(STATE_LOG), "--compensate", "--cwd", temp_dir,
                 "--agent", "worker", "--session-id", "s1", "--reason", "timed_out"],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse((Path(temp_dir) / ".tianji" / "state.jsonl").exists())

    def test_failed_child_can_be_compensated_with_an_event_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            invocation_id = self._dispatch(temp_dir)
            result = subprocess.run(
                [sys.executable, str(STATE_LOG), "--compensate", "--cwd", temp_dir,
                 "--run-id", self.RUN, "--task-id", "task-a",
                 "--invocation-id", invocation_id,
                 "--host", "cmdc", "--reason", "timed_out"],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(
                (Path(temp_dir) / ".tianji" / "state.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(record["event"], "subagent_stop")
            self.assertEqual(record["run_id"], self.RUN)
            self.assertEqual(record["invocation_id"], invocation_id)
            self.assertTrue(record["detail"]["compensated"])
            self.assertEqual(record["detail"]["reason"], "timed_out")
            # A terminal event closes the task, so the next dispatch is free.
            import run_registry
            registry = run_registry.RunRegistry(temp_dir)
            key = run_registry.EventKey(self.RUN, "task-a", 1)
            self.assertEqual(registry.get_task(key).lifecycle, "closed")

    def test_acceptance_without_an_event_key_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, str(STATE_LOG), "--acceptance", "--cwd", temp_dir,
                 "--task-id", "task-1", "--verdict", "PASS"],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse((Path(temp_dir) / ".tianji" / "state.jsonl").exists())

    def test_acceptance_rejects_an_event_key_the_registry_never_issued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, str(STATE_LOG), "--acceptance", "--cwd", temp_dir,
                 "--run-id", self.RUN, "--task-id", "ghost", "--verdict", "PASS"],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(temp_dir) / ".tianji" / "state.jsonl").exists())

    def test_acceptance_with_event_key_is_terminal_once_per_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            invocation_id = self._dispatch(temp_dir)
            command = [sys.executable, str(STATE_LOG), "--acceptance", "--cwd", temp_dir,
                       "--task-id", "task-a", "--verdict", "PASS",
                       "--run-id", self.RUN, "--host", "cmdc",
                       "--invocation-id", invocation_id]
            first = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
            second = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 3, second.stderr)

            records = [json.loads(line) for line in
                       (Path(temp_dir) / ".tianji" / "state.jsonl")
                       .read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            first_record = records[0]
            self.assertEqual(first_record["schema_version"], 2)
            self.assertEqual(first_record["run_id"], self.RUN)
            self.assertEqual(first_record["task_id"], "task-a")
            self.assertEqual(first_record["attempt"], 1)
            self.assertEqual(first_record["host"], "cmdc")
            self.assertEqual(first_record["event"], "acceptance")
            self.assertEqual(first_record["invocation_id"], invocation_id)

    def test_acceptance_closes_the_task_it_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            invocation_id = self._dispatch(temp_dir)
            subprocess.run(
                [sys.executable, str(STATE_LOG), "--acceptance", "--cwd", temp_dir,
                 "--run-id", self.RUN, "--task-id", "task-a", "--verdict", "PASS",
                 "--host", "cmdc", "--invocation-id", invocation_id],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            import run_registry
            registry = run_registry.RunRegistry(temp_dir)
            key = run_registry.EventKey(self.RUN, "task-a", 1)
            self.assertEqual(registry.get_task(key).lifecycle, "closed")

    def test_acceptance_rejects_a_non_uuid_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, str(STATE_LOG), "--acceptance", "--cwd", temp_dir,
                 "--task-id", "task-a", "--verdict", "PASS", "--run-id", "not-a-uuid"],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)

    def test_an_identifiable_hook_event_is_written_authoritatively(self) -> None:
        import run_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            registry = run_registry.RunRegistry(temp_dir)
            run = registry.create_run(host="cmdc", session_id="s1")
            task = registry.create_task(run.run_id, "task-a", role="tianji-worker")
            _, token = registry.create_pending_invocation(
                task.event_key, host="cmdc", session_id="s1", role="tianji-worker",
            )
            payload = {
                "cwd": temp_dir,
                "hook_event_name": "SubagentStart",
                "session_id": "s1",
                "agent_name": "tianji-worker",
                "toolCallId": "call-a",
                "description": f"干点活 [TJ:{token}]",
            }
            result = subprocess.run(
                [sys.executable, str(STATE_LOG), "--host", "cmdc"],
                input=json.dumps(payload),
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(
                (Path(temp_dir) / ".tianji" / "state.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(record["task_id"], "task-a")
            self.assertEqual(record["event"], "subagent_start")

    def test_a_hook_event_without_a_marker_goes_to_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = {
                "cwd": temp_dir,
                "hook_event_name": "SubagentStart",
                "session_id": "s1",
                "agent_name": "tianji-worker",
                "toolCallId": "call-unknown",
                "description": "没有标记的任务",
            }
            result = subprocess.run(
                [sys.executable, str(STATE_LOG)], input=json.dumps(payload),
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(temp_dir) / ".tianji" / "state.jsonl").exists())
            diagnostic = json.loads(
                (Path(temp_dir) / ".tianji" / "diagnostics.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(diagnostic["event"], "subagent_start")
            self.assertIn("marker", diagnostic["reason"])

    def test_the_claim_marker_never_reaches_the_ledger(self) -> None:
        # The description is the marker's carrier, so it must be stripped before
        # it is recorded anywhere -- the ledger and the diagnostics alike.
        import run_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            registry = run_registry.RunRegistry(temp_dir)
            run = registry.create_run(host="cmdc", session_id="s1")
            task = registry.create_task(run.run_id, "task-a", role="tianji-worker")
            _, token = registry.create_pending_invocation(
                task.event_key, host="cmdc", session_id="s1", role="tianji-worker",
            )
            payload = {
                "cwd": temp_dir,
                "hook_event_name": "SubagentStart",
                "session_id": "s1",
                "agent_name": "tianji-worker",
                "toolCallId": "call-a",
                "description": f"干点活 [TJ:{token}]",
            }
            subprocess.run(
                [sys.executable, str(STATE_LOG), "--host", "cmdc"],
                input=json.dumps(payload), text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            ledger = (Path(temp_dir) / ".tianji" / "state.jsonl").read_text(encoding="utf-8")
            self.assertIn("干点活", ledger)
            self.assertNotIn(token, ledger)
            self.assertNotIn("TJ:", ledger)

    def test_a_marker_without_a_call_id_is_not_claimed_by_session(self) -> None:
        # Binding a claim to a session-wide id would make two dispatches of one
        # session share a binding, so it is refused instead.
        import run_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            registry = run_registry.RunRegistry(temp_dir)
            run = registry.create_run(host="cmdc", session_id="s1")
            task = registry.create_task(run.run_id, "task-a", role="tianji-worker")
            _, token = registry.create_pending_invocation(
                task.event_key, host="cmdc", session_id="s1", role="tianji-worker",
            )
            payload = {
                "cwd": temp_dir, "hook_event_name": "SubagentStart", "session_id": "s1",
                "agent_name": "tianji-worker", "description": f"活 [TJ:{token}]",
            }
            subprocess.run(
                [sys.executable, str(STATE_LOG), "--host", "cmdc"],
                input=json.dumps(payload), text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertFalse((Path(temp_dir) / ".tianji" / "state.jsonl").exists())
            diagnostic = json.loads(
                (Path(temp_dir) / ".tianji" / "diagnostics.jsonl").read_text(encoding="utf-8")
            )
            self.assertIn("call id", diagnostic["reason"])

    def test_an_invocation_id_that_is_not_this_tasks_is_refused(self) -> None:
        import run_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            registry = run_registry.RunRegistry(temp_dir)
            run = registry.create_run(host="cmdc", session_id="s1", run_id=self.RUN)
            task = registry.create_task(run.run_id, "task-a", role="tianji-worker")
            registry.create_pending_invocation(
                task.event_key, host="cmdc", session_id="s1", role="tianji-worker",
            )
            other = registry.create_task(run.run_id, "task-b", role="tianji-worker")
            other_invocation = registry.create_pending_invocation(
                other.event_key, host="cmdc", session_id="s1", role="tianji-worker",
            )[0]
            result = subprocess.run(
                [sys.executable, str(STATE_LOG), "--acceptance", "--cwd", temp_dir,
                 "--run-id", self.RUN, "--task-id", "task-a", "--verdict", "PASS",
                 "--host", "cmdc", "--invocation-id", other_invocation.invocation_id],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(temp_dir) / ".tianji" / "state.jsonl").exists())

    def test_model_supply_precedes_role_selection(self) -> None:
        module = runpy.run_path(str(TIANJI_INIT))
        conclude = module["determine_conclusion"]
        PoolEvidence = module["PoolEvidence"]
        not_required = PoolEvidence.not_required()
        checks = {key: (True, "fixture") for key in
                  ("hooks", "roles", "role_bindings", "status_line", "menu", "routing")}
        self.assertEqual(
            conclude(checks, PoolEvidence.probed([("model", "活")]))[0], "READY",
        )
        checks["role_bindings"] = (False, "unconfigured")
        checks["menu"] = (False, "missing")
        self.assertEqual(conclude(checks, not_required)[0], "NEED_POOL")
        checks["menu"] = (True, "configured")
        self.assertEqual(conclude(checks, not_required)[0], "NEED_ROLE_CONFIG")
        checks["routing"] = (False, "unsupported")
        self.assertEqual(conclude(checks, not_required)[0], "NEED_HOST_ADAPTER")
        checks["hooks"] = (False, "missing")
        self.assertEqual(conclude(checks, not_required)[0], "NEED_INSTALL")

    def run_codex_init(self, temp_dir: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(Path(temp_dir) / "codex")
        env["TJ_ROLES_FILE"] = str(Path(temp_dir) / "roles.toml")
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(TIANJI_INIT), "--host", "codex"], cwd=cwd,
            env=env, text=True, encoding="utf-8", capture_output=True, check=False,
        )

    def run_installer(self, temp_dir: str, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, str(INSTALL), command, "--host", "codex",
                "--agents-home", str(Path(temp_dir) / "agents"),
                "--codex-home", str(Path(temp_dir) / "codex"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_codex_install_preserves_user_hooks_and_writes_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex"
            codex_home.mkdir()
            # UTF-8 BOM is common when a user creates JSON with Windows tools.
            original = {"hooks": {"SubagentStart": [{"hooks": [{"type": "command", "command": "echo user"}]}]}}
            (codex_home / "hooks.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(original).encode())

            installed = self.run_installer(temp_dir, "install")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            self.assertEqual(hooks["hooks"]["SubagentStart"][0]["hooks"][0]["command"], "echo user")
            self.assertTrue(any(
                h.get("statusMessage") == "Tianji managed state ledger"
                for matcher in hooks["hooks"]["SubagentStop"] for h in matcher["hooks"]
            ))
            for role in ("worker", "verifier", "referee"):
                with open(codex_home / "agents" / f"tianji-{role}.toml", "rb") as f:
                    self.assertIn("developer_instructions", tomllib.load(f))

            project = Path(temp_dir) / "project"
            project.mkdir()
            pending = self.run_codex_init(temp_dir, project)
            self.assertIn("结论: NEED_HOST_ADAPTER", pending.stdout)

            before_roles = {p: p.read_bytes() for p in (codex_home / "agents").glob("*.toml")}

            configured = subprocess.run(
                [
                    sys.executable, str(CODEX_ROLE_CONFIGURE), "--codex-home", str(codex_home),
                    "--role", "worker=主模型", "--role", "verifier=gpt-5.6-terra",
                    "--role", "referee=主模型",
                ], text=True, encoding="utf-8", capture_output=True, check=False,
            )
            self.assertNotEqual(configured.returncode, 0)
            self.assertEqual(before_roles, {p: p.read_bytes() for p in before_roles})
            self.assertEqual(list((codex_home / "agents").glob("*.bak-*")), [])

            # Old installations may already contain confirmed model-only bindings.
            # They must not bypass the missing model-supply gate either.
            for path in before_roles:
                content = path.read_text(encoding="utf-8").replace(
                    '# tianji-role-binding = "unconfigured"', '# tianji-role-binding = "primary"')
                path.write_text(content, encoding="utf-8")
            ready = self.run_codex_init(temp_dir, project)
            self.assertIn("结论: NEED_HOST_ADAPTER", ready.stdout)
            worker_toml = (codex_home / "agents" / "tianji-worker.toml").read_text(encoding="utf-8")
            self.assertIn('# tianji-role-binding = "primary"', worker_toml)
            self.assertNotIn("\nmodel =", worker_toml)

            synced = self.run_installer(temp_dir, "install")
            self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
            worker_toml = (codex_home / "agents" / "tianji-worker.toml").read_text(encoding="utf-8")
            self.assertIn('# tianji-role-binding = "primary"', worker_toml)
            self.assertNotIn("\nmodel =", worker_toml)

            payload = {
                "session_id": "session-1", "cwd": str(project), "hook_event_name": "SubagentStart",
                "agent_id": "agent-1", "agent_type": "tianji-worker", "model": "gpt-test",
            }
            logged = subprocess.run([sys.executable, str(STATE_LOG)], input=json.dumps(payload), text=True, check=False)
            self.assertEqual(logged.returncode, 0)
            # A hook event with no markable identity is not a ledger event: it
            # is recorded as a diagnostic instead of forging a v2 record.
            self.assertFalse((project / ".tianji" / "state.jsonl").exists())
            diagnostic = json.loads(
                (project / ".tianji" / "diagnostics.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(diagnostic["agent"], "tianji-worker")
            self.assertEqual(diagnostic["event"], "subagent_start")

            removed = self.run_installer(temp_dir, "uninstall")
            self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
            after = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            self.assertEqual(after["hooks"]["SubagentStart"][0]["hooks"][0]["command"], "echo user")

    def test_codex_invalid_hooks_fail_before_any_runtime_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex"
            codex_home.mkdir()
            hooks = codex_home / "hooks.json"
            original = "{ not json"
            hooks.write_text(original, encoding="utf-8")

            result = self.run_installer(temp_dir, "install")

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(hooks.read_text(encoding="utf-8"), original)
            self.assertFalse((codex_home / "agents").exists())
            self.assertFalse((Path(temp_dir) / "agents").exists())


if __name__ == "__main__":
    unittest.main()
