"""The menu probe's three outcomes must stay three outcomes.

A failing probe is not an empty menu and not drift: it must not report the
configuration as broken, and it must not report it as ready.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"

import sys  # noqa: E402

sys.path.insert(0, str(ROOT / "skills" / "tianji"))
sys.path.insert(0, str(SCRIPTS))

import conclusions  # noqa: E402
from observation import Observation  # noqa: E402
from host_adapters.cmdc.detector import CmdcDetector  # noqa: E402
from host_adapters.cmdc.role_renderer import render_cmdc  # noqa: E402
from role_contract import discover_role_packages  # noqa: E402


def load_init():
    spec = importlib.util.spec_from_file_location(
        "tianji_init_under_test", SCRIPTS / "tianji-init.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TianjiInit = load_init()

MENU = (
    "Available models\n"
    "zai-org/glm-5.3   GLM 5.3\n"
    "moonshotai/kimi-k3   Kimi K3\n"
)


class MenuObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-menu-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "commandcode"
        self.home.mkdir(parents=True)
        self.agents = self.home / "agents"
        self.agents.mkdir(parents=True)
        for package in discover_role_packages(ROOT / "agents"):
            (self.agents / f"{package.name}.md").write_text(
                render_cmdc(package), encoding="utf-8",
            )
        (self.home / "mods").mkdir(parents=True)
        (self.home / "mods" / "tianji-state.ts").write_text("// mod", encoding="utf-8")
        (self.home / "tianji-cmdc.json").write_text(
            json.dumps({
                "host": "cmdc",
                "roles": ["tianji-worker", "tianji-verifier", "tianji-referee"],
            }), encoding="utf-8",
        )

    def detector(self, menu_output):
        def run(args):
            return menu_output if "--list-models" in args else "1.53.0"
        return CmdcDetector(root=self.home, run=run)

    def conclusion_for(self, menu_output):
        detector = self.detector(menu_output)
        checks, pool_results, _recon = TianjiInit.run_checks(detector)
        return TianjiInit.determine_conclusion(checks, pool_results)[0]

    def test_a_failed_probe_is_check_unavailable(self):
        self.assertEqual(self.conclusion_for(None), conclusions.CHECK_UNAVAILABLE)

    def test_a_failed_probe_is_not_drift_and_not_ready(self):
        verdict = self.conclusion_for(None)
        self.assertNotEqual(verdict, conclusions.NEED_POOL)
        self.assertNotEqual(verdict, conclusions.NEED_ROLE_CONFIG)
        self.assertNotEqual(verdict, conclusions.READY)
        self.assertIn(verdict, conclusions.INCONCLUSIVE)
        self.assertNotIn(verdict, conclusions.ACTIONABLE)

    def test_an_empty_menu_is_a_configuration_verdict(self):
        # The probe ran and found nothing, which is genuinely configurable.
        self.assertEqual(self.conclusion_for(""), conclusions.NEED_POOL)

    def test_a_failed_probe_does_not_mask_a_missing_install(self):
        (self.home / "mods" / "tianji-state.ts").unlink()
        self.assertEqual(self.conclusion_for(None), conclusions.NEED_INSTALL)

    def test_the_detector_reports_the_three_states_distinctly(self):
        missing = self.detector(None).menu_observation()
        self.assertEqual(missing.state, "unavailable")
        self.assertFalse(missing.is_empty)
        self.assertFalse(missing.was_observed)

        empty = self.detector("").menu_observation()
        self.assertTrue(empty.is_empty)
        self.assertTrue(empty.was_observed)

        observed = self.detector(MENU).menu_observation()
        self.assertTrue(observed.was_observed)
        self.assertFalse(observed.is_empty)
        self.assertEqual(observed.value, ["zai-org/glm-5.3", "moonshotai/kimi-k3"])

    def test_an_unavailable_probe_does_not_revoke_an_existing_proof(self):
        # The proof lives independently of the probe: losing sight of the menu
        # must not erase what was already proven.
        detector = self.detector(MENU)
        self.assertEqual(detector.proof_level(), "none")
        checks, pool_results, _recon = TianjiInit.run_checks(self.detector(None))
        self.assertEqual(
            TianjiInit.determine_conclusion(checks, pool_results)[0],
            conclusions.CHECK_UNAVAILABLE,
        )
        self.assertEqual(detector.proof_level(), "none")
        self.assertFalse(self.detector(None).probe_ok()[0])

    def test_the_line_says_which_empty_it_is(self):
        # The reader acts on the line, not on the conclusion enum: "unconfigured"
        # sends them to provision a menu, "unreadable" sends them to look at the
        # host. Worded wrong, a restart looks like a configuration loss.
        import doctor

        unavailable = doctor.collect_facts(self.detector(None))["menu"][1]
        self.assertIn("读不到", unavailable)
        self.assertNotIn("未配置", unavailable)

        empty = doctor.collect_facts(self.detector(""))["menu"][1]
        self.assertIn("未配置", empty)
        self.assertNotIn("读不到", empty)

        configured = doctor.collect_facts(self.detector(MENU))["menu"][1]
        self.assertIn("已配置 2 个模型", configured)

    def test_a_host_without_the_interface_keeps_the_old_behaviour(self):
        class Legacy:
            def menu_models(self):
                return [("m", "cmdc", "")]

        self.assertIsNone(TianjiInit.observe_menu(Legacy()))

    def test_a_raising_probe_is_unavailability_not_emptiness(self):
        class Exploding:
            def menu_observation(self):
                raise RuntimeError("cli vanished")

        observation = TianjiInit.observe_menu(Exploding())
        self.assertEqual(observation.state, "unavailable")
        self.assertIn("cli vanished", observation.reason)
        self.assertFalse(observation.is_empty)


if __name__ == "__main__":
    unittest.main()
