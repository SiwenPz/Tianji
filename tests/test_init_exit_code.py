"""The doctor's exit code is a real signal, and a failure is never success.

The morning check used to exit zero on every path: main() returned no value, and
the ``__main__`` block both dropped whatever it returned and turned any crash
into ``sys.exit(0)``. A caller could therefore not tell a ready environment from
an unknown host from a crashed check. These tests pin the three codes apart,
pin that a broken adapter reports itself instead of masquerading as an absent
host, and pin that the host identity comes from a signal the host process itself
injects -- not from which configuration directories happen to exist.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
SKILL_ROOT = ROOT / "skills" / "tianji"
INIT = SCRIPTS / "tianji-init.py"
INSTALL = ROOT / "install.py"

sys.path.insert(0, str(SKILL_ROOT))
sys.path.insert(0, str(SCRIPTS))

import conclusions  # noqa: E402
import doctor  # noqa: E402
import host_detectors  # noqa: E402

HOST_SIGNAL_VARS = tuple(var for var, _ in host_detectors.HOST_SIGNALS)


def env_without_host_signals():
    env = os.environ.copy()
    for var in HOST_SIGNAL_VARS:
        env.pop(var, None)
    return env


def load_init():
    spec = importlib.util.spec_from_file_location("tianji_init_exit_under_test", INIT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


READY_CHECKS = {
    "hooks": (True, ""), "roles": (True, ""), "role_bindings": (True, ""),
    "status_line": (True, ""), "menu": (True, "2 个模型"),
    "routing": (True, ""), "probe": (True, ""),
}
READY_POOL = doctor.PoolEvidence.not_required()


class ExitCodeVocabularyTests(unittest.TestCase):
    def test_only_an_actionable_verdict_exits_zero(self):
        self.assertEqual(conclusions.exit_code(conclusions.READY), 0)
        blocking = conclusions.ALL - conclusions.ACTIONABLE - conclusions.INCONCLUSIVE
        for verdict in sorted(blocking):
            self.assertEqual(conclusions.exit_code(verdict), 1, verdict)

    def test_an_inconclusive_verdict_is_no_verdict_at_all(self):
        # "We could not confirm" is not "we checked and it is not ready".
        self.assertEqual(
            conclusions.exit_code(conclusions.CHECK_UNAVAILABLE),
            conclusions.EXIT_UNUSABLE,
        )

    def test_an_unknown_verdict_fails_closed(self):
        self.assertEqual(
            conclusions.exit_code("SOMETHING_NEW"), conclusions.EXIT_UNUSABLE,
        )

    def test_the_no_verdict_code_is_distinct_from_not_ready(self):
        self.assertEqual(conclusions.EXIT_ACTIONABLE, 0)
        self.assertEqual(conclusions.EXIT_NOT_READY, 1)
        self.assertEqual(conclusions.EXIT_UNUSABLE, 2)


class PoolEvidenceTests(unittest.TestCase):
    """Pool liveness is stated, never inferred from an empty list."""

    def test_an_unprobed_pool_blocks_ready(self):
        verdict, _ = doctor.determine_conclusion(
            READY_CHECKS, doctor.PoolEvidence.not_probed("status stayed offline"),
        )
        self.assertEqual(verdict, conclusions.CHECK_UNAVAILABLE)

    def test_a_failed_probe_is_unavailable_not_degraded(self):
        verdict, _ = doctor.determine_conclusion(
            READY_CHECKS, doctor.PoolEvidence.failed("probe crashed"),
        )
        self.assertEqual(verdict, conclusions.CHECK_UNAVAILABLE)

    def test_a_probed_all_dead_pool_is_degraded(self):
        verdict, _ = doctor.determine_conclusion(
            READY_CHECKS, doctor.PoolEvidence.probed([("m", "死")]),
        )
        self.assertEqual(verdict, conclusions.DEGRADED)

    def test_a_host_without_a_pool_is_not_asked_for_one(self):
        verdict, _ = doctor.determine_conclusion(
            READY_CHECKS, doctor.PoolEvidence.not_required(),
        )
        self.assertEqual(verdict, conclusions.READY)

    def test_an_unprobed_pool_is_not_an_empty_probe(self):
        # The two used to be the same value; they must not be interchangeable.
        self.assertNotEqual(
            doctor.PoolEvidence.not_probed("offline").state,
            doctor.PoolEvidence.probed([]).state,
        )

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(ValueError):
            doctor.PoolEvidence("something")

    def test_only_probed_evidence_carries_results(self):
        with self.assertRaises(ValueError):
            doctor.PoolEvidence(doctor.POOL_NOT_PROBED, [("m", "活")], "offline")


class ProcessExitCodeTests(unittest.TestCase):
    def run_init(self, *args, env=None, cwd=None):
        merged = os.environ.copy()
        merged["PYTHONIOENCODING"] = "utf-8"
        merged.update(env or {})
        return subprocess.run(
            [sys.executable, str(INIT), *args], cwd=cwd, env=merged,
            text=True, encoding="utf-8", capture_output=True, check=False,
        )

    def test_an_unknown_host_exits_with_the_no_verdict_code(self):
        result = self.run_init("--host", "no-such-host")
        self.assertEqual(result.returncode, conclusions.EXIT_UNUSABLE, result.stdout)
        self.assertIn("未知宿主", result.stdout)

    def test_a_blocking_verdict_reaches_the_process_exit_code(self):
        # The return value has to travel from main() to sys.exit: this is the
        # path that silently exited zero before.
        with tempfile.TemporaryDirectory(prefix="tianji-init-code-") as home:
            result = self.run_init("--host", "kimi", env={"KIMI_CODE_HOME": home})
        self.assertEqual(result.returncode, conclusions.EXIT_NOT_READY, result.stdout)
        self.assertIn("结论: NEED_INSTALL", result.stdout)


class HostIdentificationTests(unittest.TestCase):
    """Identity is an injected runtime signal, not an installed configuration."""

    def test_the_injected_runtime_signal_identifies_the_host(self):
        # A neutral drive on purpose: the value only has to be a path, and a
        # test in a published repository should not carry any machine's layout.
        host, reason = host_detectors.detect_host({"COMMANDCODE_SCRATCHPAD": r"X:\scratch"})
        self.assertEqual(host, "cmdc")
        self.assertTrue(reason)

    def test_a_config_directory_is_not_a_host_identity(self):
        # CODEX_HOME/KIMI_CODE_HOME only say what is installed on this machine;
        # treating them as identity is what misdiagnosed a Command Code session.
        for var in ("CODEX_HOME", "KIMI_CODE_HOME"):
            host, reason = host_detectors.detect_host({var: "/somewhere"})
            self.assertIsNone(host, var)
            self.assertTrue(reason)

    def test_no_signal_is_a_refusal_not_a_guess(self):
        host, reason = host_detectors.detect_host({})
        self.assertIsNone(host)
        self.assertTrue(reason)

    def test_an_unset_signal_does_not_count(self):
        host, _ = host_detectors.detect_host({"COMMANDCODE_SCRATCHPAD": ""})
        self.assertIsNone(host)


class DefaultHostRemovalTests(unittest.TestCase):
    """The doctor must not fall back to a host nobody asked for."""

    def run_init(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(INIT), *args], env=env, cwd=str(ROOT),
            text=True, encoding="utf-8", capture_output=True, check=False,
        )

    def test_no_host_and_no_signal_refuses(self):
        env = env_without_host_signals()
        env["PYTHONIOENCODING"] = "utf-8"
        result = self.run_init(env=env)
        self.assertEqual(result.returncode, conclusions.EXIT_UNUSABLE, result.stdout)
        self.assertIn("无法确定宿主", result.stdout)

    def test_a_runtime_signal_is_enough_to_proceed(self):
        with tempfile.TemporaryDirectory(prefix="tianji-host-signal-") as home:
            env = env_without_host_signals()
            env["PYTHONIOENCODING"] = "utf-8"
            env["COMMANDCODE_SCRATCHPAD"] = home
            env["CMDC_HOME"] = home
            result = self.run_init(env=env)
        # Detection succeeded, so the check ran and produced a verdict instead of
        # refusing: an empty Command Code home is a missing install, not an
        # unknown host.
        self.assertNotIn("无法确定宿主", result.stdout)
        self.assertEqual(result.returncode, conclusions.EXIT_NOT_READY, result.stdout)
        self.assertIn("结论: NEED_INSTALL", result.stdout)

    def test_an_explicit_host_still_wins(self):
        env = env_without_host_signals()
        env["PYTHONIOENCODING"] = "utf-8"
        result = self.run_init("--host", "no-such-host", env=env)
        self.assertEqual(result.returncode, conclusions.EXIT_UNUSABLE, result.stdout)
        self.assertIn("未知宿主", result.stdout)


class InstallerDefaultHostTests(unittest.TestCase):
    def test_install_refuses_without_a_host_or_signal(self):
        env = env_without_host_signals()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, str(INSTALL), "status"],
            env=env, cwd=str(ROOT), text=True, encoding="utf-8",
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("无法确定宿主", result.stdout)


class InstallerHomeDefaultsTests(unittest.TestCase):
    def test_status_honours_the_same_home_env_as_the_detector(self):
        # Otherwise `status` and the doctor inspect different installs on the
        # same machine, and "one verdict" is only true by accident.
        with tempfile.TemporaryDirectory(prefix="tianji-codex-home-") as home:
            env = env_without_host_signals()
            env["PYTHONIOENCODING"] = "utf-8"
            env["CODEX_HOME"] = home
            result = subprocess.run(
                [sys.executable, str(INSTALL), "status", "--host", "codex"],
                env=env, cwd=str(ROOT), text=True, encoding="utf-8",
                capture_output=True, check=False,
            )
        self.assertIn(home, result.stdout)
        self.assertEqual(
            result.returncode, conclusions.EXIT_NOT_READY, result.stdout + result.stderr,
        )


def _kimi_home_with_menu(root: Path) -> Path:
    """A Kimi home whose every local fact passes, but whose pool was never probed."""
    block = (
        "# >>> tianji-managed >>>\n"
        "[[hooks]]\n"
        'event = "SubagentStart"\n'
        'command = "python state-log.py"\n'
        "\n"
        "[[hooks]]\n"
        'event = "SubagentStop"\n'
        'command = "python state-log.py"\n'
        "# <<< tianji-managed <<<\n"
    )
    menu = (
        "\n[secondary_model.models]\n"
        '"alias-a" = ""\n'
        '\n[models."alias-a"]\n'
        'model = "some-model"\n'
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text(block + menu, encoding="utf-8")
    (root / "tui.toml").write_text(
        "# >>> tianji-managed >>>\n[status_line]\ncommand = \"python statusline.py\"\n"
        "# <<< tianji-managed <<<\n",
        encoding="utf-8",
    )
    return root


class InstallerStatusExitCodeTests(unittest.TestCase):
    """`status` is a check: a non-ready verdict must not exit zero."""

    def test_a_non_ready_cmdc_status_exits_nonzero(self):
        with tempfile.TemporaryDirectory(prefix="tianji-cmdc-status-") as home:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(
                [sys.executable, str(INSTALL), "status", "--host", "cmdc",
                 "--cmdc-home", home, "--agents-home", str(Path(home) / "agents")],
                env=env, cwd=str(ROOT), text=True, encoding="utf-8",
                capture_output=True, check=False,
            )
        self.assertEqual(
            result.returncode, conclusions.EXIT_NOT_READY, result.stdout + result.stderr,
        )
        self.assertIn("结论: NEED_INSTALL", result.stdout)

    def test_an_offline_status_cannot_call_a_pool_host_ready(self):
        # Every local Kimi fact passes here. The pool liveness proof is missing
        # because status never goes online, so the honest verdict is "we could
        # not confirm" -- not READY, which is what an empty result set used to
        # be read as.
        with tempfile.TemporaryDirectory(prefix="tianji-kimi-status-") as home:
            kimi_home = _kimi_home_with_menu(Path(home))
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(
                [sys.executable, str(INSTALL), "status", "--host", "kimi",
                 "--kimi-home", str(kimi_home),
                 "--agents-home", str(Path(home) / "agents")],
                env=env, cwd=str(ROOT), text=True, encoding="utf-8",
                capture_output=True, check=False,
            )
        self.assertEqual(
            result.returncode, conclusions.EXIT_UNUSABLE, result.stdout + result.stderr,
        )
        self.assertIn("结论: CHECK_UNAVAILABLE", result.stdout)
        self.assertNotIn("Tianji is READY", result.stdout)


class MainReturnValueTests(unittest.TestCase):
    def setUp(self):
        self.machine = load_init()

    def _run(self, host="kimi"):
        with mock.patch.object(sys, "argv", [str(INIT), "--host", host]):
            return self.machine.main()

    def _run_ready(self, checks=None, pool=READY_POOL):
        with mock.patch.object(
            self.machine, "run_checks", return_value=(checks or READY_CHECKS, pool, []),
        ), mock.patch.object(self.machine, "print_report"):
            return self._run()

    def test_a_ready_environment_returns_zero(self):
        self.assertEqual(self._run_ready(), conclusions.EXIT_ACTIONABLE)

    def test_a_blocking_verdict_returns_not_ready(self):
        checks = dict(READY_CHECKS, hooks=(False, "受管块标记缺失"))
        self.assertEqual(self._run_ready(checks), conclusions.EXIT_NOT_READY)

    def test_a_crash_during_the_check_returns_no_verdict(self):
        with mock.patch.object(self.machine, "run_checks", side_effect=RuntimeError("boom")):
            self.assertEqual(self._run(), conclusions.EXIT_UNUSABLE)

    def test_a_crash_is_not_reported_as_a_dead_pool(self):
        # DEGRADED means "the pool is confirmed dead"; a crashed check knows no
        # such thing, so printing that verdict would be an invented conclusion.
        stream = io.StringIO()
        with mock.patch.object(
            self.machine, "run_checks", side_effect=RuntimeError("boom"),
        ):
            with contextlib.redirect_stdout(stream):
                code = self._run()
        output = stream.getvalue()
        self.assertEqual(code, conclusions.EXIT_UNUSABLE)
        self.assertNotIn("DEGRADED", output)
        self.assertIn("无法得出结论", output)
        self.assertIn("boom", output)

    def test_main_always_returns_an_int(self):
        self.assertIsInstance(self._run_ready(), int)


class ImportBoundaryTests(unittest.TestCase):
    def test_a_broken_shared_import_exits_with_no_verdict(self):
        # Run a lone copy of the script with no sibling shared modules on the
        # path: the import fails before main(), and that must still be reported
        # as "no verdict" rather than as a traceback with the default code 1.
        with tempfile.TemporaryDirectory(prefix="tianji-init-import-") as tmp:
            lone = Path(tmp) / "tianji-init.py"
            lone.write_bytes(INIT.read_bytes())
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(
                [sys.executable, str(lone), "--host", "kimi"],
                cwd=tmp, env=env, text=True, encoding="utf-8",
                capture_output=True, check=False,
            )
        self.assertEqual(
            result.returncode, conclusions.EXIT_UNUSABLE,
            result.stdout + result.stderr,
        )
        self.assertIn("导入失败", result.stderr + result.stdout)

    def test_the_literal_no_verdict_code_matches_the_shared_vocabulary(self):
        self.assertEqual(load_init()._NO_VERDICT_EXIT, conclusions.EXIT_UNUSABLE)


class AdapterResolutionTests(unittest.TestCase):
    def setUp(self):
        self.machine = load_init()
        self.machine.HOST_DETECTORS.pop("cmdc", None)
        self.machine.HOST_ADAPTER_ERRORS.clear()
        self.addCleanup(self.machine.HOST_DETECTORS.pop, "cmdc", None)

    def test_a_present_but_unimportable_adapter_is_recorded_not_swallowed(self):
        # A poisoned module entry makes the import raise while the file is still
        # on disk: exactly the "the adapter is here but broken" case.
        with mock.patch.dict(sys.modules, {"host_adapters.cmdc.detector": None}):
            self.machine.register_host_adapters()
        self.assertNotIn("cmdc", self.machine.HOST_DETECTORS)
        self.assertIn("cmdc", self.machine.HOST_ADAPTER_ERRORS)
        self.assertTrue(self.machine.HOST_ADAPTER_ERRORS["cmdc"])

    def test_an_absent_adapter_is_not_recorded_as_broken(self):
        with mock.patch.object(
            self.machine, "_cmdc_detector_path", return_value=str(ROOT / "no" / "such.py"),
        ):
            self.machine.register_host_adapters()
        self.assertEqual(self.machine.HOST_ADAPTER_ERRORS, {})
        self.assertNotIn("cmdc", self.machine.HOST_DETECTORS)

    def test_a_healthy_adapter_still_registers(self):
        self.machine.register_host_adapters()
        self.assertEqual(self.machine.HOST_ADAPTER_ERRORS, {})
        self.assertIn("cmdc", self.machine.HOST_DETECTORS)

    def test_a_broken_adapter_reports_its_reason_and_no_verdict_code(self):
        self.machine.HOST_ADAPTER_ERRORS["cmdc"] = "ImportError: cannot import name 'X'"
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = self.machine.report_unresolvable_host("cmdc")
        self.assertEqual(code, conclusions.EXIT_UNUSABLE)
        self.assertIn("cannot import name 'X'", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
