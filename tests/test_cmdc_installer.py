import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(ROOT / "skills" / "tianji"))
sys.path.insert(0, str(SCRIPTS))

import python_runtime  # noqa: E402
from host_adapters.cmdc import native_installer  # noqa: E402
from host_adapters.cmdc.detector import STATE_FILE, CmdcDetector  # noqa: E402
from host_adapters.cmdc.role_renderer import read_cmdc_binding  # noqa: E402


ROLE_CONFIGURE = SCRIPTS / "cmdc-role-configure.py"


class NativeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-cmdc-install-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "commandcode"
        self.home.mkdir(parents=True)

    def install(self, **kwargs):
        return native_installer.install(self.home, ROOT, **kwargs)

    def role_files(self):
        return sorted(path.name for path in (self.home / "agents").glob("tianji-*.md"))

    def test_first_install_writes_three_roles_and_the_mod(self):
        report = self.install()
        self.assertEqual(len(self.role_files()), 3)
        self.assertTrue((self.home / "mods" / "tianji-state.ts").is_file())
        self.assertIn("agents/tianji-worker.md", report["written"])

    def test_second_install_changes_nothing(self):
        self.install()
        before = {
            path.name: path.read_bytes()
            for path in (self.home / "agents").glob("*.md")
        }
        second = self.install()
        self.assertEqual(second["written"], [])
        self.assertEqual(len(second["skip"]), 4)  # three roles + the mod
        after = {
            path.name: path.read_bytes()
            for path in (self.home / "agents").glob("*.md")
        }
        self.assertEqual(before, after)

    def test_state_file_records_managed_roles(self):
        self.install()
        state = json.loads((self.home / STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state["host"], "cmdc")
        self.assertEqual(len(state["roles"]), 3)
        self.assertEqual(state["adapter_version"], native_installer.adapter_version())

    def test_existing_binding_survives_reinstall(self):
        from host_adapters.cmdc.role_configure import set_binding

        self.install()
        set_binding(
            self.home / "agents", "tianji-worker", "zai-org/glm-5.3",
            models=["zai-org/glm-5.3"], source_root=ROOT,
        )
        report = self.install()
        self.assertIn("agents/tianji-worker.md", report["skip"])
        self.assertEqual(
            read_cmdc_binding(self.home / "agents" / "tianji-worker.md"),
            "zai-org/glm-5.3",
        )

    def test_a_hand_edited_role_is_a_conflict_and_is_kept(self):
        self.install()
        worker = self.home / "agents" / "tianji-worker.md"
        worker.write_bytes(
            worker.read_bytes() + b"\n<!-- hand edited by the user -->\n"
        )
        report = self.install()
        self.assertIn("agents/tianji-worker.md", report["conflict"])
        self.assertIn(b"hand edited", worker.read_bytes())

    def test_reconfiguring_a_role_keeps_installer_bytes_identical(self):
        # A bound role must be byte-identical to what the installer renders, or
        # every reinstall would report a phantom conflict (Windows CRLF trap).
        from host_adapters.cmdc.role_configure import set_binding

        self.install()
        set_binding(
            self.home / "agents", "tianji-worker", "zai-org/glm-5.3",
            models=["zai-org/glm-5.3"], source_root=ROOT,
        )
        report = self.install()
        self.assertEqual(report["conflict"], [])
        self.assertNotIn("agents/tianji-worker.md", report["written"])
        self.assertIn("agents/tianji-worker.md", report["skip"])

    def test_uninstall_removes_only_managed_files(self):
        self.install()
        user_file = self.home / "agents" / "my-own-agent.md"
        user_file.write_text("mine", encoding="utf-8")
        (self.home / "mods" / "tianji-state.ts").write_text("user tinkered", encoding="utf-8")

        report = native_installer.uninstall(self.home)
        self.assertEqual(self.role_files(), [])
        self.assertTrue(user_file.is_file())
        # A user-modified managed file is kept, not silently deleted.
        self.assertTrue((self.home / "mods" / "tianji-state.ts").is_file())
        self.assertNotIn("mods/tianji-state.ts", report["removed"])
        self.assertFalse((self.home / STATE_FILE).exists())

    def test_uninstall_leaves_shared_core_untouched(self):
        shared = Path(self.temporary.name) / "agents" / "skills" / "tianji"
        shared.mkdir(parents=True)
        (shared / "SKILL.md").write_text("shared", encoding="utf-8")
        self.install()
        native_installer.uninstall(self.home)
        self.assertEqual((shared / "SKILL.md").read_text(encoding="utf-8"), "shared")

    def test_dry_run_writes_nothing(self):
        report = self.install(dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertFalse((self.home / "agents").exists())
        self.assertFalse((self.home / STATE_FILE).exists())

    def test_installed_state_is_readable_by_the_detector(self):
        self.install()
        (self.home / "mods").mkdir(exist_ok=True)
        detector = CmdcDetector(root=self.home, run=lambda args: "1.53.0")
        self.assertEqual(len(detector.installed_roles()), 3)
        self.assertTrue(detector.roles_ok()[0], detector.roles_ok()[1])
        self.assertTrue(detector.hooks_ok()[0], detector.hooks_ok()[1])
        # Bindings are not confirmed yet, so the shared conclusion machine still
        # asks for role configuration.
        self.assertFalse(detector.role_bindings_ok()[0])

    def test_install_records_where_the_role_packages_live(self):
        # The installed tree carries the skill but not agents/, so this record is
        # the only thing that can find a role package again after install. The
        # locator is written when the installer is told where the shared scripts
        # went -- which is what production always passes.
        native_installer.install(
            self.home, ROOT, shared_scripts=ROOT / "skills" / "tianji" / "scripts",
        )
        locator = python_runtime.read(self.home)
        self.assertIsNotNone(locator)
        self.assertEqual(Path(locator["source_root"]).resolve(), ROOT.resolve())

    def test_a_package_update_still_reaches_a_rebound_role(self):
        # The installer reads "differs from the manifest" as a user edit. A
        # rebind is not a user edit: if it left no record, the next package
        # update would be held back and the installed role would go stale
        # while the source said otherwise.
        from host_adapters.cmdc.role_configure import set_binding

        self.install()
        set_binding(
            self.home / "agents", "tianji-worker", "zai-org/glm-5.3",
            models=["zai-org/glm-5.3"], source_root=ROOT,
        )
        with tempfile.TemporaryDirectory(prefix="tianji-newer-") as tmp:
            newer = Path(tmp) / "agents"
            newer.mkdir()
            for package in sorted((ROOT / "agents").glob("tianji-*.md")):
                newer.joinpath(package.name).write_text(
                    package.read_text(encoding="utf-8") + "\n<!-- newer contract -->\n",
                    encoding="utf-8",
                )
            report = native_installer.install(self.home, Path(tmp))

        self.assertEqual(report["conflict"], [])
        self.assertIn("agents/tianji-worker.md", report["written"])
        installed = self.home / "agents" / "tianji-worker.md"
        self.assertIn("newer contract", installed.read_text(encoding="utf-8"))
        # The update replaces the text, not the confirmed binding.
        self.assertEqual(read_cmdc_binding(installed), "zai-org/glm-5.3")

    def test_rebinding_without_a_recorded_source_says_what_to_do(self):
        # No install, so no record: the refusal must name the fix rather than
        # surface as a package-loading error from deep inside the renderer.
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, str(ROLE_CONFIGURE), "--cmdc-home", str(self.home),
             "set-tier", "economy", "zai-org/glm-5.3"],
            env=env, text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("source_root", result.stderr)
        self.assertIn("--source-root", result.stderr)


class PoolSkillIsNotInstalledTests(unittest.TestCase):
    """A host whose own menu is the model source gets no pool skill.

    The pool belongs to a host that routes through an external provider. Command
    Code's native menu supplies the models, so installing or checking
    tianji-proxy here would configure, and later nag about, a pool nobody asked
    for.
    """

    def run_installer(self, home: Path, *args):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(ROOT / "install.py"), *args, "--host", "cmdc",
             "--cmdc-home", str(home), "--agents-home", str(home / "agents")],
            cwd=str(ROOT), env=env, text=True, encoding="utf-8",
            capture_output=True, check=False,
        )

    def test_install_writes_the_core_skill_but_not_the_pool_skill(self):
        with tempfile.TemporaryDirectory(prefix="tianji-pool-skill-") as tmp:
            home = Path(tmp)
            result = self.run_installer(home, "install")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            skills = home / "agents" / "skills"
            self.assertTrue((skills / "tianji" / "SKILL.md").is_file())
            self.assertFalse((skills / "tianji-proxy").exists())

            manifest = json.loads(
                (home / "agents" / "shared-core.manifest.json").read_text(encoding="utf-8")
            )
            entries = manifest["entries"]
            self.assertTrue(any(key.startswith("skills/tianji/") for key in entries))
            self.assertFalse(any(key.startswith("skills/tianji-proxy/") for key in entries))

    def test_status_does_not_check_the_pool_skill(self):
        with tempfile.TemporaryDirectory(prefix="tianji-pool-status-") as tmp:
            home = Path(tmp)
            self.run_installer(home, "install")
            result = self.run_installer(home, "status")
        self.assertNotIn("skills/tianji-proxy", result.stdout)


if __name__ == "__main__":
    unittest.main()
