"""Sandbox-only acceptance tests for the Codex Tianji adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.py"
CONFIGURE = ROOT / "skills" / "tianji" / "scripts" / "codex-role-configure.py"
PROBE = ROOT / "skills" / "tianji" / "scripts" / "codex-routing-probe.py"
INIT = ROOT / "skills" / "tianji" / "scripts" / "tianji-init.py"


class CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-codex-adapter-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / "codex"
        self.project = self.root / "project"
        self.project.mkdir()
        install_env = os.environ.copy()
        install_env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, str(INSTALL), "install", "--host", "codex",
             "--agents-home", str(self.root / "agents"), "--codex-home", str(self.codex_home)],
            env=install_env, text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.original_config = (
            'model = "original-main"\nmodel_provider = "openai"\n'
            'approval_policy = "on-request"\n\n[projects."fixture"]\ntrust_level = "trusted"\n'
        )
        (self.codex_home / "config.toml").write_text(self.original_config, encoding="utf-8")

    def configure(self, *, allow: bool = True) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable, str(CONFIGURE), "--codex-home", str(self.codex_home),
            "--base-url", "http://127.0.0.1:39001/v1", "--main-model", "main-model",
            "--model", "主力=main-model", "--model", "便宜=worker-model",
            "--role", "worker=便宜", "--role", "verifier=主模型",
            "--role", "referee=主模型",
        ]
        if allow:
            command.append("--allow-main-provider-change")
        return subprocess.run(command, text=True, encoding="utf-8", capture_output=True, check=False)

    def configure_native(self) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        command = [
            sys.executable, str(CONFIGURE), "--codex-home", str(self.codex_home),
            "--host-native",
            "--model", "主力=gpt-5.6-terra", "--model", "审核=gpt-5.6-luna",
            "--model", "裁判=gpt-6-astra",
            "--role", "worker=主力", "--role", "verifier=审核",
            "--role", "referee=裁判",
        ]
        return subprocess.run(
            command, env=env, text=True, encoding="utf-8", capture_output=True, check=False,
        )

    def init(self) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(CODEX_HOME=str(self.codex_home), PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(INIT), "--host", "codex"], cwd=self.project,
                              env=env, text=True, encoding="utf-8", capture_output=True, check=False)

    def test_explicit_authorization_is_required_before_main_provider_change(self) -> None:
        before = {path: path.read_bytes() for path in self.codex_home.rglob("*") if path.is_file()}
        result = self.configure(allow=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(list(self.codex_home.rglob("*.bak-*")), [])

    def test_configure_preserves_user_config_and_requires_live_proof(self) -> None:
        result = self.configure()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = tomllib.loads((self.codex_home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "main-model")
        self.assertEqual(config["model_provider"], "tianji")
        self.assertEqual(config["approval_policy"], "on-request")
        self.assertEqual(config["projects"]["fixture"]["trust_level"], "trusted")
        self.assertEqual(config["model_providers"]["tianji"]["wire_api"], "responses")
        worker = tomllib.loads((self.codex_home / "agents" / "tianji-worker.toml").read_text(encoding="utf-8"))
        verifier = tomllib.loads((self.codex_home / "agents" / "tianji-verifier.toml").read_text(encoding="utf-8"))
        self.assertEqual(worker["model"], "worker-model")
        self.assertNotIn("model", verifier)
        self.assertIn("结论: NEED_ROUTE_PROOF", self.init().stdout)

    def test_host_native_models_do_not_require_an_external_provider(self) -> None:
        result = self.configure_native()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            (self.codex_home / "config.toml").read_text(encoding="utf-8"),
            self.original_config,
        )
        state = tomllib.loads((self.codex_home / "tianji-adapter.toml").read_text(encoding="utf-8"))
        self.assertEqual(state["model_source"], "host_native")
        self.assertEqual(state["models"]["主力"], "gpt-5.6-terra")
        self.assertEqual(
            tomllib.loads((self.codex_home / "agents" / "tianji-worker.toml").read_text(encoding="utf-8"))["model"],
            "gpt-5.6-terra",
        )
        report = self.init().stdout
        self.assertNotIn("结论: NEED_HOST_ADAPTER", report)
        self.assertNotIn("结论: NEED_POOL", report)
        self.assertIn("结论: NEED_ROUTE_PROOF", report)

    def test_host_native_rejects_primary_binding_without_writes(self) -> None:
        before = {
            path.relative_to(self.codex_home): path.read_bytes()
            for path in self.codex_home.rglob("*") if path.is_file()
        }
        backups_before = set(self.codex_home.rglob("*.bak-*"))
        command = [
            sys.executable, str(CONFIGURE), "--codex-home", str(self.codex_home),
            "--host-native", "--main-model", "gpt-5.6-sol",
            "--model", "主力=gpt-5.6-terra", "--model", "审核=gpt-5.6-luna",
            "--model", "裁判=gpt-6-astra",
            "--role", "worker=主模型", "--role", "verifier=审核",
            "--role", "referee=裁判",
        ]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            command, env=env, text=True, encoding="utf-8",
            capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("主模型 is not allowed", result.stderr)
        after = {
            path.relative_to(self.codex_home): path.read_bytes()
            for path in self.codex_home.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertEqual(set(self.codex_home.rglob("*.bak-*")), backups_before)

    def test_host_native_probe_uses_hook_model_as_dispatch_evidence(self) -> None:
        self.assertEqual(self.configure_native().returncode, 0)
        fake = self.root / "fake-native-codex.py"
        fake.write_text(
            "import json, pathlib, sys\n"
            "cwd = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])\n"
            "state = cwd / '.tianji' / 'state.jsonl'\n"
            "state.parent.mkdir(parents=True, exist_ok=True)\n"
            "records = [\n"
            " {'ts':'2026-09-10T00:00:00','event':'subagent_start','agent':'tianji-worker','session_id':'native-session','detail':{'turn_id':'native-turn','model':'gpt-5.6-terra'}},\n"
            " {'ts':'2026-09-10T00:00:01','event':'subagent_stop','agent':'tianji-worker','session_id':'native-session','detail':{'turn_id':'native-turn','model':'gpt-5.6-terra'}}]\n"
            "with state.open('a', encoding='utf-8') as stream:\n"
            " for record in records: stream.write(json.dumps(record) + '\\n')\n"
            "token = next(x for x in sys.argv if 'TIANJI_ROUTE_OK_' in x).split('TIANJI_ROUTE_OK_',1)[1].split()[0].rstrip('.')\n"
            "token = 'TIANJI_ROUTE_OK_' + token\n"
            "print(json.dumps({'type':'item.completed','tool':'spawn_agent','agent_type':'tianji-worker'}))\n"
            "print(json.dumps({'type':'item.completed','text':token}))\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(PROBE), "--codex-home", str(self.codex_home),
             "--cwd", str(self.project), "--codex-bin", str(fake)],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = tomllib.loads((self.codex_home / "tianji-adapter.toml").read_text(encoding="utf-8"))
        self.assertEqual(state["proof"]["proof_level"], "host_dispatch")
        self.assertEqual(state["proof"]["proof_role"], "worker")
        self.assertEqual(state["proof"]["observed_model"], "gpt-5.6-terra")
        self.assertFalse(state["proof"]["wire_verified"])
        self.assertIn("结论: READY", self.init().stdout)
        status = subprocess.run(
            [sys.executable, str(INSTALL), "status", "--host", "codex",
             "--agents-home", str(self.root / "agents"), "--codex-home", str(self.codex_home)],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("codex-adapter:host-native", status.stdout)
        self.assertIn("Tianji is READY", status.stdout)

    def test_host_native_probe_rejects_start_only_evidence(self) -> None:
        self.assertEqual(self.configure_native().returncode, 0)
        fake = self.root / "fake-start-only-codex.py"
        fake.write_text(
            "import json, pathlib, sys\n"
            "cwd = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])\n"
            "state = cwd / '.tianji' / 'state.jsonl'\n"
            "state.parent.mkdir(parents=True, exist_ok=True)\n"
            "record = {'ts':'2026-09-10T00:00:00','event':'subagent_start','agent':'tianji-worker','session_id':'native-session','detail':{'turn_id':'native-turn','model':'gpt-5.6-terra'}}\n"
            "with state.open('a', encoding='utf-8') as stream: stream.write(json.dumps(record) + '\\n')\n"
            "token = next(x for x in sys.argv if 'TIANJI_ROUTE_OK_' in x).split('TIANJI_ROUTE_OK_',1)[1].split()[0].rstrip('.')\n"
            "token = 'TIANJI_ROUTE_OK_' + token\n"
            "print(json.dumps({'type':'item.completed','tool':'spawn_agent','agent_type':'tianji-worker'}))\n"
            "print(json.dumps({'type':'item.completed','text':token}))\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(PROBE), "--codex-home", str(self.codex_home),
             "--cwd", str(self.project), "--codex-bin", str(fake)],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no matching completed hook pair", result.stderr)
        state = tomllib.loads(
            (self.codex_home / "tianji-adapter.toml").read_text(encoding="utf-8")
        )
        self.assertNotIn("proof", state)
        self.assertIn("结论: NEED_ROUTE_PROOF", self.init().stdout)

    def test_host_native_completed_probe_can_be_attested_from_ledger(self) -> None:
        self.assertEqual(self.configure_native().returncode, 0)
        since = datetime.now(timezone.utc).isoformat()
        ledger = self.project / ".tianji" / "state.jsonl"
        ledger.parent.mkdir(parents=True)
        events = [
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "subagent_start",
                "agent": "tianji-worker",
                "session_id": "native-session",
                "detail": {"turn_id": "native-turn", "model": "gpt-5.6-terra"},
            },
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "subagent_stop",
                "agent": "tianji-worker",
                "session_id": "native-session",
                "detail": {"turn_id": "native-turn", "model": "gpt-5.6-terra"},
            },
        ]
        ledger.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(PROBE), "--codex-home", str(self.codex_home),
             "--cwd", str(self.project), "--from-ledger", "--since", since],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = tomllib.loads((self.codex_home / "tianji-adapter.toml").read_text(encoding="utf-8"))
        self.assertEqual(state["proof"]["proof_level"], "host_dispatch")
        self.assertEqual(state["proof"]["observed_turn_id"], "native-turn")
        self.assertFalse(state["proof"]["wire_verified"])
        self.assertIn("结论: READY", self.init().stdout)
        worker_path = self.codex_home / "agents" / "tianji-worker.toml"
        worker_path.write_text(
            worker_path.read_text(encoding="utf-8") + "\n# local drift\n",
            encoding="utf-8",
        )
        drifted = self.init().stdout
        self.assertNotIn("结论: READY", drifted)
        self.assertIn("角色或模型快照已漂移", drifted)

    def test_host_native_forged_proof_never_becomes_ready(self) -> None:
        self.assertEqual(self.configure_native().returncode, 0)
        state_path = self.codex_home / "tianji-adapter.toml"
        base = state_path.read_text(encoding="utf-8")
        subject = tomllib.loads(base)["subject_sha256"]
        # Two ways to be forged: claim wire evidence this host cannot produce,
        # or claim the bound role answered on a model it is not bound to.
        for wire_verified, observed_model in (
                (True, "gpt-5.6-terra"), (False, "gpt-5.6-luna")):
            with self.subTest(wire_verified=wire_verified, observed_model=observed_model):
                proof = (
                    "\n[proof]\n"
                    "status = \"passed\"\n"
                    "proof_level = \"host_dispatch\"\n"
                    f"wire_verified = {str(wire_verified).lower()}\n"
                    f"subject_sha256 = {json.dumps(subject)}\n"
                    "proof_role = \"worker\"\n"
                    "expected_role_model = \"gpt-5.6-terra\"\n"
                    f"observed_model = {json.dumps(observed_model)}\n"
                )
                state_path.write_text(base + proof, encoding="utf-8")
                self.assertIn("结论: NEED_ROUTE_PROOF", self.init().stdout)
                status = subprocess.run(
                    [sys.executable, str(INSTALL), "status", "--host", "codex",
                     "--agents-home", str(self.root / "agents"),
                     "--codex-home", str(self.codex_home)],
                    text=True, encoding="utf-8", capture_output=True, check=False,
                )
                self.assertNotIn("Tianji is READY", status.stdout)

    def test_fake_native_probe_records_hash_bound_proof_then_ready(self) -> None:
        self.assertEqual(self.configure().returncode, 0)
        fake = self.root / "fake-codex.py"
        fake.write_text(
            "import json, sys\n"
            "token = next(x for x in sys.argv if 'TIANJI_ROUTE_OK_' in x).split('TIANJI_ROUTE_OK_',1)[1].split()[0].rstrip('.')\n"
            "token = 'TIANJI_ROUTE_OK_' + token\n"
            "print(json.dumps({'type':'item.completed','tool':'spawn_agent','agent_type':'tianji-worker'}))\n"
            "print(json.dumps({'type':'item.completed','text':token}))\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(PROBE), "--codex-home", str(self.codex_home),
             "--cwd", str(self.project), "--codex-bin", str(fake),
             "--allow-transcript-only"],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = tomllib.loads((self.codex_home / "tianji-adapter.toml").read_text(encoding="utf-8"))
        self.assertEqual(state["proof"]["status"], "passed")
        self.assertEqual(state["proof"]["proof_role"], "worker")
        self.assertEqual(state["proof"]["expected_role_model"], "worker-model")
        self.assertIn("结论: READY", self.init().stdout)
        role = self.codex_home / "agents" / "tianji-worker.toml"
        role.write_text(role.read_text(encoding="utf-8") + "# user change\n", encoding="utf-8")
        self.assertIn("结论: NEED_ROLE_CONFIG", self.init().stdout)

    def test_restore_recovers_original_main_provider_without_losing_other_settings(self) -> None:
        self.assertEqual(self.configure().returncode, 0)
        result = subprocess.run([sys.executable, str(CONFIGURE), "--codex-home", str(self.codex_home),
                                 "--restore"], text=True, encoding="utf-8",
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored_text = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        restored = tomllib.loads(restored_text)
        self.assertEqual(restored["model"], "original-main")
        self.assertEqual(restored["model_provider"], "openai")
        self.assertNotIn("tianji", restored.get("model_providers", {}))
        self.assertEqual(restored["projects"]["fixture"]["trust_level"], "trusted")
        self.assertEqual(tomllib.loads((self.codex_home / "tianji-adapter.toml").read_text(encoding="utf-8"))["status"], "restored")


if __name__ == "__main__":
    unittest.main()
