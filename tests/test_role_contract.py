import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skills.tianji.scripts.role_contract import (
    discover_role_packages,
    render_codex,
    render_kimi,
)


class RoleContractTests(unittest.TestCase):
    def setUp(self):
        self.packages = discover_role_packages(ROOT / "agents")

    def test_all_roles_have_shared_contract(self):
        self.assertGreaterEqual(len(self.packages), 1)
        for package in self.packages:
            self.assertEqual(package.name, package.path.stem)
            self.assertTrue(package.description)
            self.assertTrue(package.instructions)

    def test_kimi_renderer_preserves_source_package(self):
        for package in self.packages:
            self.assertEqual(render_kimi(package), package.path.read_text(encoding="utf-8"))

    def test_codex_renderer_is_valid_and_has_same_role_identity(self):
        for package in self.packages:
            rendered = tomllib.loads(render_codex(package))
            self.assertEqual(rendered["name"], package.name)
            self.assertEqual(rendered["description"], package.description)
            self.assertTrue(rendered["developer_instructions"])


if __name__ == "__main__":
    unittest.main()
