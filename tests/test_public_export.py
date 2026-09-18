"""The public export carries the project, not the workbench.

The public repository is assembled by tools/publish_public.py from the
exclusions in tools/public-exclude.txt. Two things can go wrong there, and both
are silent: a pattern that no longer matches anything (the list rots and stops
protecting what it names), and machine-specific material that reaches a file the
export would publish. These tests pin both.
"""
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import publish_public  # noqa: E402


TEXT_SUFFIXES = (".md", ".py", ".ts", ".mts", ".json", ".sh", ".toml", ".txt")

# One machine's material, by shape: a home directory, a personal checkout.
# Paths are the signal worth matching -- a bare user name is a word, and this
# list lives in the repository it protects.
_PERSONAL = ("C:/Users/", "C:\\Users\\", "D:/test/", "D:\\test\\")

# Documents written for the people building Tianji, never for its users.
_WORKING_DOCUMENTS = (
    "docs/HANDOVER.md",
    "docs/PRD.md",
    "docs/STEP3-RESUME.md",
    "docs/HOST-PAYLOAD-AUDIT.md",
    "docs/tianji-proxy-eval.md",
)


class PublicExportTests(unittest.TestCase):
    def tracked(self):
        return [path for path in publish_public.git("ls-files").stdout.splitlines() if path]

    def split(self):
        return publish_public.split(self.tracked(), publish_public.exclusion_patterns())

    def test_every_exclusion_pattern_still_matches_a_file(self):
        # A pattern matching nothing protects nothing, and says so only here.
        _, _, unmatched = self.split()
        self.assertEqual(unmatched, [])

    def test_the_working_documents_are_held_back(self):
        _, held, _ = self.split()
        for path in _WORKING_DOCUMENTS:
            self.assertIn(path, held, f"{path} would be published")

    def test_no_published_file_carries_machine_specific_material(self):
        publish, _, _ = self.split()
        offenders = []
        for path in publish:
            if not path.endswith(TEXT_SUFFIXES):
                continue
            if Path(path).name == Path(__file__).name:
                continue  # this file holds the markers themselves
            text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
            if any(marker in text for marker in _PERSONAL):
                offenders.append(path)
        self.assertEqual(offenders, [], "these files would leak one machine's details")


if __name__ == "__main__":
    unittest.main()
