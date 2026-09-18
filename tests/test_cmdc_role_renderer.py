import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(ROOT / "skills" / "tianji"))
sys.path.insert(0, str(SCRIPTS))

from host_adapters.cmdc.role_renderer import (  # noqa: E402
    BINDING_FIXED,
    BINDING_PRIMARY,
    BINDING_UNCONFIGURED,
    declared_model,
    installed_max_turns,
    read_cmdc_binding,
    render_cmdc,
)
from host_adapters.cmdc.tool_mapping import (  # noqa: E402
    CAPABILITY_TO_CMDC_TOOLS,
    cmdc_tools,
)
from role_contract import (  # noqa: E402
    CANONICAL_CAPABILITIES,
    canonical_capabilities,
    discover_role_packages,
)


CMDC_TOOL_IDS = {tool for tools in CAPABILITY_TO_CMDC_TOOLS.values() for tool in tools}


class TurnBudgetTests(unittest.TestCase):
    def test_the_budget_reads_back_from_the_rendered_role(self):
        # The rendered file is what the host obeys, so the budget is read from
        # it: a task book sized to anything else dies mid-dispatch, and a
        # dispatch that runs out of turns returns nothing rather than a part.
        packages = {package.name: package
                    for package in discover_role_packages(ROOT / "agents")}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tianji-verifier.md"
            path.write_text(render_cmdc(packages["tianji-verifier"], binding=""),
                            encoding="utf-8")

            self.assertEqual(field_value(path.read_text(encoding="utf-8"),
                                         "maxTurns"), "20")
            self.assertEqual(installed_max_turns(path), 20)
            self.assertIsNone(installed_max_turns(Path(temporary) / "absent.md"))


def frontmatter_lines(text):
    parts = text.split("---", 2)
    return [line for line in parts[1].splitlines() if line.strip()]


def field_value(text, key):
    for line in frontmatter_lines(text):
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return None


class CmdcRoleRendererTests(unittest.TestCase):
    def setUp(self):
        self.packages = discover_role_packages(ROOT / "agents")

    def test_every_shared_role_renders_for_command_code(self):
        self.assertEqual(len(self.packages), 3)
        for package in self.packages:
            rendered = render_cmdc(package)
            self.assertEqual(field_value(rendered, "name"), package.name)
            self.assertTrue(field_value(rendered, "description"))
            self.assertIn(package.instructions, rendered)
            self.assertEqual(field_value(rendered, "showOutput"), "true")
            self.assertTrue(field_value(rendered, "maxTurns").isdigit())

    def test_render_is_deterministic_and_carries_no_timestamp(self):
        for package in self.packages:
            first = render_cmdc(package, binding="gpt-5.6-terra")
            second = render_cmdc(package, binding="gpt-5.6-terra")
            self.assertEqual(first, second)
            self.assertNotIn("generated", first)
            self.assertNotIn("generated_at", first)

    def test_rendered_tools_are_command_code_ids_only(self):
        for package in self.packages:
            tools = field_value(render_cmdc(package), "tools")
            self.assertTrue(tools)
            for tool in (item.strip() for item in tools.split(",")):
                self.assertIn(tool, CMDC_TOOL_IDS)

    def test_binding_variants_are_explicit(self):
        worker = next(p for p in self.packages if p.name == "tianji-worker")
        primary = render_cmdc(worker, binding="主模型")
        self.assertIn(BINDING_PRIMARY, primary)
        self.assertIsNone(field_value(primary, "model"))

        fixed = render_cmdc(worker, binding="moonshotai/kimi-k3")
        self.assertEqual(field_value(fixed, "model"), "moonshotai/kimi-k3")
        self.assertIn(BINDING_FIXED, fixed)

        unconfigured = render_cmdc(worker)
        self.assertIn(BINDING_UNCONFIGURED, unconfigured)
        self.assertIsNone(field_value(unconfigured, "model"))

    def test_permission_follows_write_and_execute_capability(self):
        # Roles that can write or run commands must not block on prompts in an
        # interactive session; pure readers keep the host default.
        for package in self.packages:
            mode = field_value(render_cmdc(package), "permissionMode")
            if package.can_write:
                self.assertEqual(mode, "auto-accept", package.name)
            else:
                self.assertIsNone(mode, package.name)

    def test_binding_round_trips_through_the_rendered_file(self):
        worker = next(p for p in self.packages if p.name == "tianji-worker")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tianji-worker.md"
            path.write_text(render_cmdc(worker, binding="zai-org/glm-5.3"), encoding="utf-8")
            self.assertEqual(read_cmdc_binding(path), "zai-org/glm-5.3")
            self.assertEqual(declared_model(path), ("zai-org/glm-5.3", "declared"))

            path.write_text(render_cmdc(worker, binding="主模型"), encoding="utf-8")
            self.assertEqual(read_cmdc_binding(path), "主模型")
            self.assertEqual(declared_model(path), ("主模型(inherit)", "inherit"))

            path.write_text(render_cmdc(worker), encoding="utf-8")
            self.assertIsNone(read_cmdc_binding(path))
            self.assertEqual(declared_model(path), ("", "none"))

    def test_unknown_capability_and_tool_names_fail_loud(self):
        with self.assertRaises(ValueError):
            cmdc_tools(["file.teleport"])
        with self.assertRaises(ValueError):
            canonical_capabilities(["Teleport"])


class SharedCapabilityContractTests(unittest.TestCase):
    def setUp(self):
        self.packages = discover_role_packages(ROOT / "agents")

    def test_capabilities_are_semantic_and_source_tools_are_preserved(self):
        for package in self.packages:
            self.assertTrue(package.source_tools, package.name)
            self.assertTrue(package.capabilities, package.name)
            self.assertTrue(set(package.capabilities) <= CANONICAL_CAPABILITIES)
            self.assertEqual(
                package.capabilities, canonical_capabilities(package.source_tools),
            )

    def test_review_roles_get_no_file_editing_tools(self):
        # A reviewer runs acceptance commands, so it needs a shell. It must not
        # be handed an editor: the one thing a reviewer may never do is quietly
        # repair the work it is judging.
        for name in ("tianji-verifier", "tianji-referee"):
            package = next(p for p in self.packages if p.name == name)
            self.assertIn("shell.execute", package.capabilities, name)
            self.assertNotIn("file.patch", package.capabilities, name)
            self.assertNotIn("file.write", package.capabilities, name)

    def test_worker_can_write_and_execute(self):
        worker = next(p for p in self.packages if p.name == "tianji-worker")
        self.assertTrue(worker.can_write)
        self.assertIn("file.patch", worker.capabilities)
        self.assertIn("shell.execute", worker.capabilities)


if __name__ == "__main__":
    unittest.main()
