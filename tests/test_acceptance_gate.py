import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "skills" / "tianji" / "scripts" / "acceptance-gate.py"


class AcceptanceGateTests(unittest.TestCase):
    def run_gate(self, spec, cwd):
        spec_path = Path(cwd) / "acceptance.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        return subprocess.run([sys.executable, str(GATE), "--spec", str(spec_path), "--cwd", str(cwd)],
                              text=True, encoding="utf-8", capture_output=True, check=False)

    def test_all_commands_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_gate({"commands": [{"name": "ok", "command": "echo PASS"}]}, temp)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["verdict"], "PASS")
            self.assertEqual(report["commands"][0]["exit_code"], 0)

    def test_any_nonzero_command_fails_and_keeps_order(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_gate({"commands": [
                {"name": "first", "command": "echo first"},
                {"name": "bad", "command": "cmd /c exit 7"},
            ]}, temp)
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["verdict"], "FAIL")
            self.assertEqual([item["name"] for item in report["commands"]], ["first", "bad"])
            self.assertEqual(report["commands"][1]["exit_code"], 7)

    def test_malformed_spec_fails_without_running_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_gate({"commands": []}, temp)
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["verdict"], "FAIL")
            self.assertIn("non-empty list", report["error"])


if __name__ == "__main__":
    unittest.main()
