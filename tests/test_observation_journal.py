"""The drift explanation must name what changed, and must not invent one."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import observation_journal as journal  # noqa: E402


BINDINGS = {"tianji-worker": "a/model-1", "tianji-verifier": "a/model-2"}
MENU = ["a/model-1", "a/model-2"]


def subjects(**overrides):
    base = {
        "role_bindings": dict(BINDINGS),
        "menu": list(MENU),
        "adapter_digest": "adapter-1",
        "host_runtime_version": "1.53.0",
    }
    base.update(overrides)
    return base


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-journal-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)

    def test_remember_then_recall_round_trips(self):
        journal.remember(self.workspace, **subjects())
        recalled = journal.recall(self.workspace)
        self.assertIsNotNone(recalled)
        self.assertEqual(recalled["role_bindings"], BINDINGS)
        self.assertEqual(recalled["menu"], MENU)

    def test_recall_without_a_journal_is_none_not_empty(self):
        self.assertIsNone(journal.recall(self.workspace))

    def test_a_corrupt_journal_is_not_treated_as_an_observation(self):
        path = journal.journal_path(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(journal.recall(self.workspace))

    def test_a_journal_from_another_version_is_not_used(self):
        path = journal.journal_path(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version": 99}', encoding="utf-8")
        self.assertIsNone(journal.recall(self.workspace))

    def test_a_changed_role_binding_is_named(self):
        journal.remember(self.workspace, **subjects())
        previous = journal.recall(self.workspace)
        lines = journal.explain_drift(
            previous, **subjects(
                role_bindings={"tianji-worker": "a/model-1", "tianji-verifier": "a/model-9"},
            ),
        )
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("tianji-verifier", lines[0])
        self.assertIn("a/model-2", lines[0])
        self.assertIn("a/model-9", lines[0])
        self.assertNotIn("tianji-worker", lines[0])

    def test_an_added_and_a_removed_role_are_both_named(self):
        journal.remember(self.workspace, **subjects())
        previous = journal.recall(self.workspace)
        lines = journal.explain_drift(
            previous,
            **subjects(role_bindings={"tianji-worker": "a/model-1", "tianji-new": "a/model-3"}),
        )
        joined = "\n".join(lines)
        self.assertIn("tianji-verifier 绑定消失", joined)
        self.assertIn("tianji-new 新增绑定", joined)

    def test_a_changed_menu_is_reported_by_size(self):
        journal.remember(self.workspace, **subjects())
        previous = journal.recall(self.workspace)
        lines = journal.explain_drift(previous, **subjects(menu=["a/model-1"]))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("模型菜单", lines[0])
        self.assertIn("2 项", lines[0])
        self.assertIn("1 项", lines[0])

    def test_an_adapter_and_a_runtime_change_are_separately_named(self):
        journal.remember(self.workspace, **subjects())
        previous = journal.recall(self.workspace)
        lines = journal.explain_drift(
            previous, **subjects(adapter_digest="adapter-2", host_runtime_version="1.54.0"),
        )
        joined = "\n".join(lines)
        self.assertIn("适配层", joined)
        self.assertIn("宿主版本", joined)

    def test_no_change_is_an_empty_explanation(self):
        journal.remember(self.workspace, **subjects())
        previous = journal.recall(self.workspace)
        self.assertEqual(journal.explain_drift(previous, **subjects()), [])

    def test_without_a_journal_there_is_no_explanation(self):
        # Nothing to compare against is not the same as nothing having changed.
        self.assertEqual(journal.explain_drift(None, **subjects()), [])

    def test_a_missing_binding_map_does_not_invent_role_drift(self):
        previous = {"role_bindings": None, "menu": MENU,
                    "adapter_digest": "adapter-1", "host_runtime_version": "1.53.0"}
        lines = journal.explain_drift(previous, **subjects())
        self.assertEqual([line for line in lines if "角色绑定" in line], [])

    def test_digest_of_matches_the_integrity_snapshot(self):
        from route_proof import integrity_snapshot, snapshot_digest

        expected = snapshot_digest(integrity_snapshot(
            role_bindings=BINDINGS, menu=MENU,
            adapter_digest="adapter-1", host_runtime_version="1.53.0",
        ))
        self.assertEqual(journal.digest_of(subjects()), expected)

    def test_remember_replaces_an_earlier_observation(self):
        journal.remember(self.workspace, **subjects(host_runtime_version="1.53.0"))
        journal.remember(self.workspace, **subjects(host_runtime_version="1.54.0"))
        self.assertEqual(journal.recall(self.workspace)["host_runtime_version"], "1.54.0")

    def test_an_unwritable_journal_does_not_raise(self):
        # Writing stays advisory: a failure here can never fail a check.
        path = journal.journal_path(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()  # a directory where the file should go
        journal.remember(self.workspace, **subjects())
        self.assertIsNone(journal.recall(self.workspace))


    def test_an_unreadable_menu_is_not_recorded_as_an_empty_one(self):
        # A proof can be established while the menu probe fails -- the proof is
        # about the dispatch, not the menu. Recording that as an empty menu makes
        # the next drift report say "模型菜单: 0 → 70 项", naming a configuration
        # that never existed. Measured 2026-09-17 on a host mid-restart.
        from observation import Observation

        class Detector:
            def role_bindings(self):
                return dict(BINDINGS)

            def menu_observation(self):
                return Observation.unavailable("host restarting")

            def current_snapshot(self):
                return {"adapter_digest": "adapter-1", "host_runtime_version": "1.53.0"}

        recorded = journal.snapshot_subjects(Detector())
        self.assertIsNone(recorded["menu"])

        journal.remember(self.workspace, **recorded)
        previous = journal.recall(self.workspace)
        self.assertIsNone(previous["menu"])

        lines = journal.explain_drift(previous, **subjects())
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("上次未读到", lines[0])
        self.assertNotIn("0 项", lines[0])

    def test_a_confirmed_empty_menu_still_reads_as_zero(self):
        # The other half of the pair: a probe that ran and found nothing is a
        # real reading, and the comparison must keep saying "0 项".
        from observation import Observation

        class Detector:
            def role_bindings(self):
                return dict(BINDINGS)

            def menu_observation(self):
                return Observation.confirmed_empty()

            def current_snapshot(self):
                return {"adapter_digest": "adapter-1", "host_runtime_version": "1.53.0"}

        self.assertEqual(journal.snapshot_subjects(Detector())["menu"], [])

        journal.remember(self.workspace, **subjects(menu=[]))
        lines = journal.explain_drift(journal.recall(self.workspace), **subjects())
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("0 项", lines[0])

    def test_a_journal_from_before_this_fix_is_ignored(self):
        # Entries written when the menu could be recorded unobserved are not
        # comparable, so they are ignored rather than explained against.
        path = journal.journal_path(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"version": 1, "role_bindings": {}, "menu": [], '
            '"adapter_digest": "adapter-1", "host_runtime_version": "1.53.0"}',
            encoding="utf-8",
        )
        self.assertIsNone(journal.recall(self.workspace))


if __name__ == "__main__":
    unittest.main()
