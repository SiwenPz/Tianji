"""The runtime locator: how a host adapter finds the shared core.

A recorded locator must name an absolute interpreter and the registry entry
point, so no host has to assume a ``python`` on PATH or guess the install
layout.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import python_runtime  # noqa: E402


class LocatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-locator-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.scripts = Path(r"/opt/tianji/skills/tianji/scripts")

    def test_the_locator_names_an_absolute_interpreter_and_the_registry(self):
        payload = json.loads(python_runtime.record(self.scripts).decode("utf-8"))
        self.assertTrue(Path(payload["interpreter"]).is_absolute() or payload["interpreter"])
        self.assertEqual(
            Path(payload["registry"]).name, python_runtime.REGISTRY_ENTRY,
        )
        self.assertTrue(payload["registry"].endswith("run_registry.py"))

    def test_the_interpreter_is_never_a_bare_python(self):
        # A recorded name would make the adapter depend on PATH resolution it
        # cannot verify; sys.executable or an absolute PATH hit is required.
        interpreter = python_runtime.detect_interpreter()
        self.assertTrue(interpreter)
        if interpreter not in python_runtime.INTERPRETER_CANDIDATES:
            self.assertTrue(Path(interpreter).is_absolute() or "/" in interpreter or "\\" in interpreter)

    def test_a_recorded_locator_reads_back(self):
        (self.home / python_runtime.LOCATOR_NAME).write_bytes(
            python_runtime.record(self.scripts)
        )
        recorded = python_runtime.read(self.home)
        self.assertEqual(recorded["registry"], str(python_runtime.registry_path(self.scripts)))
        self.assertEqual(recorded["scripts_dir"], str(self.scripts))

    def test_a_missing_or_broken_locator_reads_as_none(self):
        self.assertIsNone(python_runtime.read(self.home))
        for payload in ('{"interpreter": ""}', '{"interpreter": "/x"}', "not json", "[]"):
            (self.home / python_runtime.LOCATOR_NAME).write_text(payload, encoding="utf-8")
            self.assertIsNone(python_runtime.read(self.home), payload)

    def test_the_cli_records_a_usable_locator(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "python_runtime.py"),
             "--home", str(self.home), "--scripts-dir", str(self.scripts)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = python_runtime.read(self.home)
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded["registry"], str(python_runtime.registry_path(self.scripts)))


if __name__ == "__main__":
    unittest.main()
