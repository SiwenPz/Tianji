"""The role tier contract is shared, model-free, and survives adapter removal.

Which roles are economy, which are quality, and which is the escalation referee
is a Tianji-wide statement, so it is written down once in the shared contract.
A host decides which of its own models fills a tier; it never redefines the
tiers themselves.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
ROLE_DIR = ROOT / "agents"

sys.path.insert(0, str(SCRIPTS))

import role_contract  # noqa: E402
from role_contract import (  # noqa: E402
    ROLE_TIERS,
    discover_role_packages,
    roles_for_tier,
    tier_for_role,
    validate_role_tiers,
)


# Model ids that belong to one Codex session, never to the shared contract.
CODEX_MODEL_IDS = ("gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra")

EXPECTED = {
    "economy": {"tianji-worker"},
    "quality": {"tianji-verifier"},
    "escalation": {"tianji-referee"},
}


class RoleTierCoverageTests(unittest.TestCase):
    def test_the_tiers_name_exactly_the_roles_that_ship(self):
        self.assertEqual(
            {role for roles in ROLE_TIERS.values() for role in roles},
            {package.name for package in discover_role_packages(ROLE_DIR)},
        )

    def test_every_discovered_role_has_exactly_one_tier(self):
        packages = discover_role_packages(ROLE_DIR)
        validate_role_tiers(packages)  # must not raise
        self.assertEqual(len(packages), 3)
        for package in packages:
            self.assertIsNotNone(tier_for_role(package.name), package.name)

    def test_every_tier_is_one_role(self):
        # Convergence is the policy, not a coincidence: a second name in a tier
        # is a second duty in the same station, which the task book should have
        # declared instead of a new role. Pinned so a re-split is deliberate.
        for tier, roles in ROLE_TIERS.items():
            self.assertEqual(len(roles), 1, tier)

    def test_the_expected_membership(self):
        for tier, expected in EXPECTED.items():
            self.assertEqual(set(roles_for_tier(tier)), expected, tier)

    def test_no_role_is_listed_twice(self):
        listed = [role for roles in ROLE_TIERS.values() for role in roles]
        self.assertEqual(len(listed), len(set(listed)))

    def test_controller_is_not_a_renderable_role(self):
        # The parent session is the controller; it is not a tier and has no
        # package, so a host cannot dispatch it as a subagent.
        self.assertNotIn(role_contract.CONTROLLER, ROLE_TIERS)
        with self.assertRaises(ValueError):
            roles_for_tier(role_contract.CONTROLLER)
        self.assertNotIn(
            "tianji-controller",
            {package.name for package in discover_role_packages(ROLE_DIR)},
        )


class RoleTierValidationTests(unittest.TestCase):
    def test_an_unknown_tier_is_a_loud_error(self):
        with self.assertRaises(ValueError):
            roles_for_tier("platinum")

    def test_an_unknown_role_has_no_tier(self):
        self.assertIsNone(tier_for_role("tianji-ghost"))

    def test_a_role_in_two_tiers_is_refused(self):
        with mock.patch.dict(ROLE_TIERS, {"platinum": ("tianji-worker",)}):
            with self.assertRaises(ValueError):
                validate_role_tiers(discover_role_packages(ROLE_DIR))

    def test_a_role_with_no_tier_is_refused(self):
        class Unassigned:
            name = "tianji-ghost"

        with self.assertRaises(ValueError):
            validate_role_tiers([*discover_role_packages(ROLE_DIR), Unassigned()])

    def test_a_tier_naming_a_missing_package_is_refused(self):
        packages = [
            package for package in discover_role_packages(ROLE_DIR)
            if package.name != "tianji-referee"
        ]
        with self.assertRaises(ValueError):
            validate_role_tiers(packages)


class NoModelInTheSharedContractTests(unittest.TestCase):
    def test_the_contract_names_no_codex_model(self):
        text = (SCRIPTS / "role_contract.py").read_text(encoding="utf-8")
        for model in CODEX_MODEL_IDS:
            self.assertNotIn(model, text, model)

    def test_the_shared_role_packages_name_no_codex_model(self):
        for package in discover_role_packages(ROLE_DIR):
            for model in CODEX_MODEL_IDS:
                self.assertNotIn(model, package.markdown, package.name)

    def test_the_tier_values_are_role_names(self):
        for roles in ROLE_TIERS.values():
            for role in roles:
                self.assertTrue(role.startswith("tianji-"), role)

    def test_the_tier_definition_is_not_host_owned(self):
        # The contract must not reach for an adapter: it is what an adapter
        # consumes.
        self.assertNotIn(
            "host_adapters", (SCRIPTS / "role_contract.py").read_text(encoding="utf-8"),
        )


class AdapterRemovalTests(unittest.TestCase):
    def test_the_shared_role_contract_works_without_the_adapter_tree(self):
        # Copy only the shared scripts and the role packages: the adapter is
        # gone, and discovery plus validation must still work.
        with tempfile.TemporaryDirectory(prefix="tianji-no-adapter-") as tmp:
            root = Path(tmp)
            shutil.copytree(
                SCRIPTS, root / "scripts",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            shutil.copytree(ROLE_DIR, root / "agents")
            program = (
                "from pathlib import Path\n"
                f"import sys; sys.path.insert(0, {str(root / 'scripts')!r})\n"
                "import role_contract as rc\n"
                f"packages = rc.discover_role_packages(Path({str(root / 'agents')!r}))\n"
                "rc.validate_role_tiers(packages)\n"
                "print(len(packages), rc.tier_for_role('tianji-referee'),"
                " len(rc.roles_for_tier(rc.ECONOMY)))\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", program], capture_output=True,
                text=True, encoding="utf-8", check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "3 escalation 1")


if __name__ == "__main__":
    unittest.main()
