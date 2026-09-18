import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(ROOT / "skills" / "tianji"))
sys.path.insert(0, str(SCRIPTS))

from host_adapters.cmdc.detector import (  # noqa: E402
    REQUIRED_BINDINGS,
    CmdcDetector,
    parse_models,
    parse_version,
)
from host_adapters.cmdc.role_renderer import render_cmdc  # noqa: E402
from role_contract import discover_role_packages  # noqa: E402
from route_proof import snapshot_digest  # noqa: E402


MENU = {
    "Open Source": ["moonshotai/kimi-k3", "zai-org/glm-5.3", "deepseek/deepseek-v4-flash"],
    "Anthropic": ["claude-sonnet-5", "claude-opus-5"],
    "OpenAI": ["gpt-6-astra"],
}

# Real 1.53.0 quirks: free-tier IDs carry a ":free" tag, and the listing ends
# with copy-pasteable usage examples that must not be read as models.
TAGGED_MENU = """\
Available models  ·  70 models

Open Source

meituan/longcat-2.0:free               FREE trillion-parameter agentic coding
inclusionai/ling-3.0-flash-sante:free  FREE health & medicine tuned model
poolside/laguna-s-2.1-free             FREE open-weight agentic coding

Sakana

sakana/fugu-ultra                      multi-agent orchestration

Pass the full id, or just the short name after the last "/":
cmdc --model moonshotai/kimi-k2.5
cmdc --model kimi-k2.5

Docs:  https://commandcode.ai/docs/reference/cli/models
"""


def sample_menu_output():
    lines = ["Available models  ·  70 models", "", "Open Source"]
    for model in MENU["Open Source"]:
        lines.append(f"{model:<40} fast reasoning")
    lines.append("")
    lines.append("Anthropic")
    for model in MENU["Anthropic"]:
        lines.append(f"{model:<40} high capability")
    lines.append("")
    lines.append("OpenAI")
    for model in MENU["OpenAI"]:
        lines.append(f"{model:<40} most capable")
    return "\n".join(lines) + "\n"


class CmdcDetectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-cmdc-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_stub = {
            "--version": "1.53.0",
            "-v": "1.53.0",
            "--list-models": sample_menu_output(),
        }
        self.run = lambda args: self.run_stub.get(" ".join(args), "")
        self.packages = discover_role_packages(ROOT / "agents")

    def install_roles(self, bindings=None):
        self.agents_dir().mkdir(parents=True, exist_ok=True)
        for package in self.packages:
            binding = (bindings or {}).get(package.name)
            (self.agents_dir() / f"{package.name}.md").write_text(
                render_cmdc(package, binding=binding), encoding="utf-8",
            )

    def install_mod(self):
        mod = self.root / "mods" / "tianji-state.ts"
        mod.parent.mkdir(parents=True, exist_ok=True)
        mod.write_text("// stub", encoding="utf-8")

    def agents_dir(self):
        return self.root / "agents"

    def write_state(self, roles=None, proof=None):
        self.root.mkdir(parents=True, exist_ok=True)
        state = {"host": "cmdc"}
        if roles:
            state["roles"] = roles
        if proof:
            state["proof"] = proof
        (self.root / "tianji-cmdc.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8",
        )

    def detector(self):
        return CmdcDetector(root=self.root, run=self.run)

    # ---- menu parsing ----------------------------------------------------
    def test_parse_models_returns_ids_and_skips_headers(self):
        models = parse_models(sample_menu_output())
        self.assertEqual(
            models,
            ["moonshotai/kimi-k3", "zai-org/glm-5.3", "deepseek/deepseek-v4-flash",
             "claude-sonnet-5", "claude-opus-5", "gpt-6-astra"],
        )

    def test_menu_supplies_host_native_models(self):
        detector = self.detector()
        self.assertEqual(detector.model_ids(), parse_models(sample_menu_output()))
        self.assertTrue(detector.requires_model_menu())

    def test_tagged_model_ids_parse_and_help_footer_is_ignored(self):
        models = parse_models(TAGGED_MENU)
        self.assertEqual(models, [
            "meituan/longcat-2.0:free",
            "inclusionai/ling-3.0-flash-sante:free",
            "poolside/laguna-s-2.1-free",
            "sakana/fugu-ultra",
        ])
        self.assertNotIn("cmdc", models)
        self.assertNotIn("Docs:", models)

    def test_version_parses_first_content_line(self):
        self.assertEqual(parse_version("Command Code  1.53.0\n"), "Command Code  1.53.0")
        self.assertEqual(parse_version("1.53.0\n"), "1.53.0")
        self.assertEqual(parse_version(None), "unknown")

    def test_no_pool_is_required_when_menu_is_sufficient(self):
        detector = self.detector()
        self.assertFalse(detector.supports_menu_reconciliation())
        # Command Code's status bar comes from the state mod, so the shared
        # machine never has to read a host configuration file for it.
        self.assertTrue(detector.status_line_ok()[0])

    # ---- install/roles facts ---------------------------------------------
    def test_installed_roles_discovered_from_rendered_files(self):
        self.install_roles()
        detector = self.detector()
        self.assertEqual(
            detector.installed_roles(),
            sorted(package.name for package in self.packages),
        )
        self.assertEqual(len(detector.installed_roles()), 3)

    def test_digest_changes_when_a_role_file_changes(self):
        self.install_roles({"tianji-worker": "zai-org/glm-5.3"})
        before = self.detector().role_files_digest()
        worker = self.root / "agents" / "tianji-worker.md"
        worker.write_text(
            worker.read_text(encoding="utf-8").replace("zai-org/glm-5.3", "zai-org/glm-5.2"),
            encoding="utf-8",
        )
        after = self.detector().role_files_digest()
        self.assertNotEqual(before["tianji-worker"], after["tianji-worker"])

    def test_host_runtime_change_marks_probe_false(self):
        self.install_roles({
            "tianji-worker": "zai-org/glm-5.3",
            "tianji-verifier": "zai-org/glm-5.3",
            "tianji-referee": "zai-org/glm-5.3",
        })
        snapshot = self.detector().current_snapshot()
        proof = {
            "integrity": {"snapshot": snapshot, "captured_at": "t0"},
            "dispatch": {
                "verified": True, "host": "cmdc", "role": "tianji-worker",
                "requested_model": "zai-org/glm-5.3", "declared_model": "zai-org/glm-5.3",
                "correlation_id": "call-1", "start_event_id": "e-start",
                "stop_event_id": "e-stop",
                "event_key": {"run_id": "77802c0c-d50f-4fe8-bc09-8ea018cf2334",
                              "task_id": "probe", "attempt": 1},
                "captured_at": "t0", "subject_digest": snapshot_digest(snapshot),
            },
            "wire": {"verified": False},
        }
        self.write_state(roles=[p.name for p in self.packages], proof=proof)

        fresh = self.detector()
        self.assertTrue(fresh.probe_ok()[0], fresh.probe_ok()[1])
        self.assertEqual(fresh.proof_level(), "host_dispatch")

        # Drift the host runtime version → proof collapses to none.
        self.run_stub["--version"] = "1.54.0"
        drifted = self.detector()
        self.assertFalse(drifted.probe_ok()[0], drifted.probe_ok()[1])
        self.assertEqual(drifted.proof_level(), "none")

    def test_roles_ok_requires_recorded_roles(self):
        detector = self.detector()
        self.assertFalse(detector.roles_ok()[0])

        self.install_roles()
        self.write_state(roles=[p.name for p in self.packages])
        self.assertTrue(self.detector().roles_ok()[0])

    def test_roles_ok_detects_missing_role_file(self):
        self.install_roles()
        self.write_state(roles=[p.name for p in self.packages])
        (self.root / "agents" / "tianji-referee.md").unlink()
        self.assertFalse(self.detector().roles_ok()[0])

    def test_role_bindings_require_every_contract_role(self):
        self.install_roles({"tianji-worker": "zai-org/glm-5.3"})
        self.assertFalse(self.detector().role_bindings_ok()[0])
        assert "tianji-worker" in REQUIRED_BINDINGS
        self.install_roles({
            "tianji-worker": "zai-org/glm-5.3",
            "tianji-verifier": "zai-org/glm-5.3",
            "tianji-referee": "zai-org/glm-5.3",
        })
        self.assertTrue(self.detector().role_bindings_ok()[0])

    def test_routing_supported_is_static_capability(self):
        detector = self.detector()
        # menu available, but no roles and no mod → not routable yet.
        self.assertFalse(detector.routing_supported()[0])
        self.install_roles()
        self.write_state(roles=[p.name for p in self.packages])
        detector = self.detector()
        self.assertFalse(detector.routing_supported()[0])  # miss the mod
        self.install_mod()
        self.assertTrue(self.detector().routing_supported()[0])

    def test_detector_has_no_conclusion_machine(self):
        detector = self.detector()
        for attr in ("determine_conclusion", "build_instances"):
            self.assertFalse(hasattr(detector, attr), attr)


if __name__ == "__main__":
    unittest.main()