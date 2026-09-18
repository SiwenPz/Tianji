import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
PROXY_PATH = ROOT / "skills" / "tianji-proxy" / "scripts" / "tianji-proxy.py"
SPEC = importlib.util.spec_from_file_location("tianji_proxy_responses", PROXY_PATH)
PROXY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PROXY)


class ProxyResponsesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-proxy-responses-")
        self.root = Path(self.temporary.name)
        self.servers = []

    def tearDown(self):
        for server in reversed(self.servers):
            server.shutdown()
            server.server_close()
        self.temporary.cleanup()

    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server

    def start_proxy(self, channels, token=""):
        lines = ["[server]", "port = 0", f"token = {json.dumps(token)}"]
        for index, (server, key) in enumerate(channels, 1):
            lines.extend([
                "", "[[channels]]", f'name = "channel-{index}"',
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"',
                'models = ["fixture-model"]', f"keys = [{json.dumps(key)}]",
            ])
        config = self.root / "proxy.toml"
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        pool = PROXY.ProxyPool(str(config), str(self.root / "state.json"))
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            lambda *args, **kwargs: PROXY.ProxyHandler(*args, pool=pool, **kwargs),
        )
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server

    @staticmethod
    def post(port, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST", path, body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.headers)
        connection.close()
        return response.status, headers, body

    def test_responses_and_chat_use_their_original_upstream_paths(self):
        hits = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                hits.append((self.path, payload["model"], self.headers.get("Authorization")))
                body = json.dumps({"id": "ok", "model": payload["model"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = self.start_server(Upstream)
        proxy = self.start_proxy([(upstream, "fixture-key")])
        for path in ("/v1/responses", "/v1/chat/completions"):
            status, _, body = self.post(proxy.server_port, path, {"model": "fixture-model"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["model"], "fixture-model")
        self.assertEqual(
            hits,
            [("v1/responses", "fixture-model", "Bearer fixture-key"),
             ("v1/chat/completions", "fixture-model", "Bearer fixture-key")],
        )

    def test_responses_retry_keeps_responses_path_and_changes_channel(self):
        hits = []

        def handler(status):
            class Upstream(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    self.rfile.read(int(self.headers["Content-Length"]))
                    hits.append((status, self.path, self.headers.get("Authorization")))
                    body = json.dumps({"ok": status == 200}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            return Upstream

        failing = self.start_server(handler(502))
        healthy = self.start_server(handler(200))
        proxy = self.start_proxy([(failing, "first-key"), (healthy, "second-key")])
        status, _, body = self.post(
            proxy.server_port, "/v1/responses", {"model": "fixture-model"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(
            hits,
            [(502, "v1/responses", "Bearer first-key"),
             (200, "v1/responses", "Bearer second-key")],
        )

    def test_responses_all_429_preserves_retry_after(self):
        class Limited(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "7")
                self.end_headers()
                self.wfile.write(b'{"error":{"type":"rate_limit_error"}}')

        first = self.start_server(Limited)
        second = self.start_server(Limited)
        proxy = self.start_proxy([(first, "first-key"), (second, "second-key")])
        status, headers, body = self.post(
            proxy.server_port, "/v1/responses", {"model": "fixture-model"}
        )
        self.assertEqual(status, 429)
        self.assertEqual(headers.get("Retry-After"), "7")
        self.assertEqual(json.loads(body)["error"]["type"], "rate_limit_error")

    def test_responses_sse_is_forwarded_before_upstream_finishes(self):
        class Streaming(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.created"}\n\n')
                self.wfile.flush()
                time.sleep(0.6)
                self.wfile.write(b'data: {"type":"response.completed"}\n\n')
                self.wfile.flush()

        upstream = self.start_server(Streaming)
        proxy = self.start_proxy([(upstream, "fixture-key")])
        connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
        connection.request(
            "POST", "/v1/responses",
            body=json.dumps({"model": "fixture-model", "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        started = time.monotonic()
        first = response.read1(4096)
        first_elapsed = time.monotonic() - started
        rest = response.read()
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn(b"response.created", first)
        self.assertLess(first_elapsed, 0.45)
        self.assertIn(b"response.completed", rest)


if __name__ == "__main__":
    unittest.main()


class ProxyProtocolCapabilityTests(unittest.TestCase):
    def test_channel_protocol_capability_filters_cross_provider_routing(self):
        with tempfile.TemporaryDirectory(prefix="tianji-protocols-") as temp_dir:
            config = Path(temp_dir) / "protocols.toml"
            config.write_text(
                "[server]\nport = 0\n\n"
                "[[channels]]\nname = \"responses-only\"\n"
                "base_url = \"http://127.0.0.1:1/v1\"\nmodels = [\"shared\"]\n"
                "keys = [\"r-key\"]\nprotocols = [\"responses\"]\n\n"
                "[[channels]]\nname = \"chat-only\"\n"
                "base_url = \"http://127.0.0.1:2/v1\"\nmodels = [\"shared\"]\n"
                "keys = [\"c-key\"]\nprotocols = [\"chat/completions\"]\n",
                encoding="utf-8",
            )
            pool = PROXY.ProxyPool(str(config), str(Path(temp_dir) / "state.json"))
            self.assertEqual(pool.pick("shared", "responses")[1]["channel"], "responses-only")
            self.assertEqual(pool.pick("shared", "chat/completions")[1]["channel"], "chat-only")
