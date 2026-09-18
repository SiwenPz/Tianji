import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import managed_files  # noqa: E402
from managed_files import ManagedInstaller, read_manifest  # noqa: E402


class ManagedInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-managed-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "manifest.json"
        self.installer = ManagedInstaller(self.root, self.manifest, version="0.1.0")

    def entries(self, marker="one"):
        return {"a.txt": f"alpha-{marker}".encode(), "sub/b.txt": b"beta"}

    def test_first_install_writes_and_records_manifest(self):
        plan = self.installer.plan(self.entries())
        self.assertEqual(sorted(plan.write), ["a.txt", "sub/b.txt"])
        result = self.installer.commit(self.entries(), plan)
        self.assertEqual(sorted(result["written"]), ["a.txt", "sub/b.txt"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"alpha-one")
        self.assertEqual(sorted(self.installer.managed_paths()), ["a.txt", "sub/b.txt"])

    def test_second_install_is_a_no_op(self):
        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)

        again = ManagedInstaller(self.root, self.manifest, version="0.1.0")
        second = again.plan(self.entries())
        self.assertEqual(second.write, [])
        self.assertEqual(sorted(second.skip), ["a.txt", "sub/b.txt"])
        result = again.commit(self.entries(), second)
        self.assertEqual(result["written"], [])

    def test_unmanaged_files_are_never_touched(self):
        user_file = self.root / "user-note.txt"
        user_file.write_text("mine", encoding="utf-8")
        extra = self.root / "extra" / "keep.txt"
        extra.parent.mkdir(parents=True)
        extra.write_text("keep", encoding="utf-8")

        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)
        self.assertEqual(user_file.read_text(encoding="utf-8"), "mine")
        self.assertEqual(extra.read_text(encoding="utf-8"), "keep")
        self.assertNotIn("user-note.txt", self.installer.managed_paths())

    def test_user_modified_managed_file_is_a_conflict(self):
        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)
        (self.root / "a.txt").write_text("user edited this", encoding="utf-8")

        fresh = ManagedInstaller(self.root, self.manifest, version="0.1.0")
        conflict = fresh.plan(self.entries("two"))
        self.assertIn("a.txt", conflict.conflict)
        result = fresh.commit(self.entries("two"), conflict)
        self.assertNotIn("a.txt", result["written"])
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "user edited this")

    def test_force_overwrites_a_conflict(self):
        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)
        (self.root / "a.txt").write_text("user edited this", encoding="utf-8")

        fresh = ManagedInstaller(self.root, self.manifest, version="0.1.0")
        conflict = fresh.plan(self.entries("two"))
        result = fresh.commit(self.entries("two"), conflict, force=True)
        self.assertIn("a.txt", result["written"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"alpha-two")

    def test_downgrade_is_blocked_unless_forced(self):
        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)

        older = ManagedInstaller(self.root, self.manifest, version="0.0.9")
        blocked = older.plan(self.entries("old"))
        self.assertEqual(sorted(blocked.blocked), ["a.txt"])
        self.assertEqual(sorted(blocked.skip), ["sub/b.txt"])
        result = older.commit(self.entries("old"), blocked)
        self.assertEqual(result["written"], [])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"alpha-one")

        forced = ManagedInstaller(self.root, self.manifest, version="0.0.9")
        forced_plan = forced.plan(self.entries("old"))
        forced_result = forced.commit(self.entries("old"), forced_plan, force=True)
        self.assertIn("a.txt", forced_result["written"])

    def test_legitimate_reconfigure_is_skipped_not_conflicted(self):
        # Real-machine case: a managed role file is reconfigured (model bound)
        # after install. Re-rendering with that configuration is identical to
        # what is on disk, so it must be a no-op — not a conflict.
        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)

        reconfigured = {"a.txt": b"alpha-user-configured", "sub/b.txt": b"beta"}
        (self.root / "a.txt").write_bytes(reconfigured["a.txt"])

        fresh = ManagedInstaller(self.root, self.manifest, version="0.1.0")
        replan = fresh.plan(reconfigured)
        self.assertEqual(replan.conflict, [])
        self.assertEqual(sorted(replan.skip), ["a.txt", "sub/b.txt"])
        result = fresh.commit(reconfigured, replan)
        self.assertEqual(result["written"], [])
        self.assertEqual(result["conflict"], [])

        # And the manifest is now current, so a third pass is still clean.
        third = ManagedInstaller(self.root, self.manifest, version="0.1.0")
        self.assertEqual(third.plan(reconfigured).conflict, [])

    def test_noop_install_does_not_touch_the_manifest(self):
        # The manifest timestamp is the "configuration last really changed"
        # marker, so an install that changes nothing must leave it alone.
        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)
        first_mtime = self.manifest.stat().st_mtime_ns

        fresh = ManagedInstaller(self.root, self.manifest, version="0.1.0")
        again = fresh.plan(self.entries())
        fresh.commit(self.entries(), again)
        self.assertEqual(again.write, [])
        self.assertEqual(self.manifest.stat().st_mtime_ns, first_mtime)

    def test_a_real_change_does_update_the_manifest(self):
        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)
        first = read_manifest(self.manifest)["entries"]["a.txt"]["sha256"]

        newer = ManagedInstaller(self.root, self.manifest, version="0.1.0")
        changed = newer.plan(self.entries("two"))
        newer.commit(self.entries("two"), changed)
        self.assertNotEqual(
            read_manifest(self.manifest)["entries"]["a.txt"]["sha256"], first,
        )

    def test_upgrade_writes_only_changed_files_and_updates_version(self):
        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)

        newer = ManagedInstaller(self.root, self.manifest, version="0.2.0")
        upgrade = newer.plan(self.entries("two"))
        self.assertEqual(sorted(upgrade.write), ["a.txt"])
        self.assertEqual(sorted(upgrade.skip), ["sub/b.txt"])
        newer.commit(self.entries("two"), upgrade)
        self.assertEqual(read_manifest(self.manifest)["version"], "0.2.0")

    def test_remove_only_deletes_recorded_unmodified_files(self):
        plan = self.installer.plan(self.entries())
        self.installer.commit(self.entries(), plan)
        (self.root / "sub" / "b.txt").write_text("user changed", encoding="utf-8")

        removed = self.installer.remove(["a.txt", "sub/b.txt", "never-managed.txt"])
        self.assertEqual(removed, ["a.txt"])
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual((self.root / "sub" / "b.txt").read_text(encoding="utf-8"), "user changed")
        self.assertEqual(self.installer.managed_paths(), ["sub/b.txt"])

    def test_corrupt_manifest_is_treated_as_empty(self):
        self.manifest.write_text("{ not json", encoding="utf-8")
        fresh = ManagedInstaller(self.root, self.manifest, version="0.1.0")
        plan = fresh.plan(self.entries())
        self.assertEqual(sorted(plan.write), ["a.txt", "sub/b.txt"])


class InstallerLockTests(unittest.TestCase):
    """The installer lock is the third of the three locks the spec requires."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-installer-lock-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.installer = ManagedInstaller(
            self.root, self.root / "manifest.json", version="1.0.0",
        )

    def test_a_commit_holds_the_installer_lock_while_it_writes(self):
        held = []
        real_write = managed_files._write_atomic

        def spy(path, data):
            held.append((self.root / managed_files.INSTALLER_LOCK).exists())
            return real_write(path, data)

        entries = {"a.txt": b"hello"}
        plan = self.installer.plan(entries)
        with mock.patch.object(managed_files, "_write_atomic", side_effect=spy):
            self.installer.commit(entries, plan)
        self.assertTrue(held, "nothing was written")
        self.assertEqual(set(held), {True}, "a write ran outside the installer lock")
        # And the lock is released afterwards.
        self.assertFalse((self.root / managed_files.INSTALLER_LOCK).exists())

    def test_a_contended_install_lock_refuses_to_write(self):
        import lock_protocol

        (self.root / managed_files.INSTALLER_LOCK).write_text(
            json.dumps(lock_protocol.lock_payload(lease_seconds=3600.0)),
            encoding="utf-8",
        )
        entries = {"a.txt": b"hello"}
        plan = self.installer.plan(entries)
        with self.assertRaises(Exception):
            self.installer.commit(entries, plan)
        self.assertFalse((self.root / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
