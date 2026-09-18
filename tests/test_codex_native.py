"""Opt-in native Codex unified-provider check; local fake servers, no credentials.

Run: python tests/test_codex_native.py
This verifies that parent and named child use one Responses endpoint while the
child role's model override survives. It does not test LLM decision quality.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@unittest.skipUnless(os.environ.get("TJ_TEST_NATIVE_CODEX") == "1" or __name__ == "__main__",
                     "opt-in: run python tests/test_codex_native.py")
class NativeRoutingTests(unittest.TestCase):
    def test_parent_and_child_share_provider_but_use_distinct_models(self):
        executable = shutil.which("codex.cmd" if os.name == "nt" else "codex")
        if not executable:
            self.skipTest("Codex CLI unavailable")
        calls = []
        observed_headers = []
        child_requested = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model = request.get("model")
                calls.append((self.server.server_port, self.path, model))
                observed_headers.append({key.lower(): value for key, value in self.headers.items()})
                if model == "child-fixture":
                    child_requested.set()
                if model == "parent-fixture" and sum(c[2] == model for c in calls) == 1:
                    item = {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
                            "name": "spawn_agent", "namespace": "multi_agent_v1", "arguments": json.dumps({
                                "agent_type": "tianji-worker", "message": "Reply ROUTE_OK", "fork_context": False})}
                else:
                    if model == "parent-fixture":
                        child_requested.wait(10)
                    item = {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "ROUTE_OK", "annotations": []}]}
                response = {"id": "resp_fixture", "object": "response", "model": model,
                            "status": "completed", "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in [
                    {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0, "item": item},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler)]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="tianji-native-") as tmp:
                root = Path(tmp)
                config = root / "codex"
                (config / "agents").mkdir(parents=True)
                project = root / "project"
                project.mkdir()
                (config / "config.toml").write_text(
                    'model = "parent-fixture"\nmodel_provider = "tianji"\n'
                    '[model_providers.tianji]\nname = "tianji"\nwire_api = "responses"\n'
                    f'base_url = "http://127.0.0.1:{servers[0].server_port}/v1"\n', encoding="utf-8")
                (config / "agents" / "tianji-worker.toml").write_text(
                    'name = "tianji-worker"\ndescription = "Local routing fixture"\n'
                    'developer_instructions = "Reply ROUTE_OK"\nmodel = "child-fixture"\n', encoding="utf-8")
                env = os.environ.copy()
                env.update(CODEX_HOME=str(config), USERPROFILE=str(root), HOME=str(root))
                for key in tuple(env):
                    if key.endswith("API_KEY") or key in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY"):
                        env.pop(key)
                result = subprocess.run(
                    [executable, "exec", "--strict-config", "--skip-git-repo-check", "--json",
                     "--sandbox", "read-only", "-C", str(project),
                     "Spawn tianji-worker to reply ROUTE_OK; wait for it and report."],
                    env=env, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=90)
                diagnostic = result.stdout + result.stderr + "\nCALLS=" + repr(calls)
                self.assertEqual(result.returncode, 0, diagnostic)
                child_calls = [call for call in calls if call[2] == "child-fixture"]
                self.assertTrue(child_calls, diagnostic)
                print(json.dumps({"unified_provider": True, "child_model_applied": True,
                                  "header_names": sorted({key for item in observed_headers for key in item})}))
                self.assertTrue(all(port == servers[0].server_port and path == "/v1/responses"
                                    for port, path, _ in child_calls), diagnostic)
                self.assertTrue(all(port == servers[0].server_port for port, _, model in calls if model == "parent-fixture"))
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
