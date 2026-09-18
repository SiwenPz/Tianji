"""The claim bridge is a process boundary, and its encoding is part of the contract.

The Command Code mod spawns this registry and reads stdout as UTF-8. When the
registry answered in the console's codepage instead -- which is what happens on
Windows -- a Chinese task id reached the adapter as U+FFFD, the ledger stored
the replacement characters, and the routing proof could no longer resolve the
invocation the ledger named. The symptom was "the dispatch recorded no
configuration snapshot", three layers away from the cause.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
REGISTRY = SCRIPTS / "run_registry.py"

TASK = "复验再取"


class ClaimBridgeEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-bridge-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)

    def open_task(self):
        return subprocess.run(
            [sys.executable, str(REGISTRY), "open", "--workspace", str(self.workspace),
             "--host", "cmdc", "--session", "s-1", "--role", "tianji-worker",
             "--task-id", TASK],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )

    def claim(self, token, tool_call_id="call-a"):
        """Exactly what the adapter does: UTF-8 bytes in, read stdout as bytes."""
        request = json.dumps({
            "workspace": str(self.workspace), "host": "cmdc", "session_id": "s-1",
            "token": token, "tool_call_id": tool_call_id, "role": "tianji-worker",
        }).encode("utf-8")
        return subprocess.run(
            [sys.executable, str(REGISTRY), "claim"], input=request,
            capture_output=True, check=False,
        )

    def test_the_answer_is_ascii_so_no_codepage_can_mangle_it(self):
        opened = self.open_task()
        self.assertEqual(opened.returncode, 0, opened.stderr)
        token = json.loads(opened.stdout)["claim_token"]

        answered = self.claim(token)
        self.assertEqual(answered.returncode, 0, answered.stderr)
        self.assertEqual(
            [byte for byte in answered.stdout if byte > 0x7F], [],
            "the bridge emitted non-ASCII bytes",
        )
        payload = json.loads(answered.stdout.decode("ascii"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_id"], TASK)

    def test_a_failure_message_is_ascii_too(self):
        # A refused claim is read through the same boundary, so it has the same
        # rule: the reason has to survive the trip back.
        answered = self.claim("not-a-real-token")
        self.assertNotEqual(answered.returncode, 0)
        self.assertEqual([byte for byte in answered.stdout if byte > 0x7F], [])
        self.assertFalse(json.loads(answered.stdout.decode("ascii"))["ok"])

    def test_the_ledger_values_resolve_back_to_the_invocation(self):
        # What the adapter writes into the ledger is what must be resolvable
        # afterwards: run_id + task_id + attempt must name the invocation.
        opened = self.open_task()
        token = json.loads(opened.stdout)["claim_token"]
        answered = json.loads(self.claim(token).stdout.decode("ascii"))

        sys.path.insert(0, str(SCRIPTS))
        from ledger_schema import EventKey
        from run_registry import RunRegistry

        registry = RunRegistry(self.workspace)
        key = EventKey(answered["run_id"], answered["task_id"], answered["attempt"])
        invocation = registry.get_invocation(key, answered["invocation_id"])
        self.assertEqual(invocation.task_id, TASK)
        self.assertEqual(invocation.invocation_id, answered["invocation_id"])
        self.assertEqual(invocation.subject_digest, answered["subject_digest"])


if __name__ == "__main__":
    unittest.main()
