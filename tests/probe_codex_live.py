"""Opt-in, paid live pool probe. Credentials only via TJ_LIVE_POOL_KEY.

python tests/probe_codex_live.py --base-url http://localhost:3000/v1 --model MODEL
Only synthetic task input is sent. Never copy the real Codex home or auth files.
"""
import argparse
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def probe(base_url, key, model, protocol):
    if protocol == "responses":
        payload = {"model": model, "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply exactly POOL_OK"}]}], "max_output_tokens": 256,
                   "stream": True, "store": False}
        path = "/responses"
    else:
        payload = {"model": model, "messages": [{"role": "user", "content": "Reply exactly POOL_OK"}],
                   "max_tokens": 256, "stream": True}
        path = "/chat/completions"
    started = time.monotonic()
    result = {"protocol": protocol, "requested_model": model}
    request = urllib.request.Request(base_url.rstrip("/") + path,
                                     data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result["http_status"] = response.status
            result["content_type"] = response.headers.get("Content-Type")
            events, returned_models, text = set(), set(), []
            for line in response:
                if time.monotonic() - started > 60:
                    raise TimeoutError()
                if not line.startswith(b"data:"):
                    continue
                raw = line[5:].strip()
                if raw == b"[DONE]":
                    events.add("DONE")
                    continue
                item = json.loads(raw)
                events.add(item.get("type", "chat.chunk"))
                actual = item.get("model") or item.get("response", {}).get("model")
                if actual:
                    returned_models.add(actual)
                if item.get("type") == "response.output_text.delta":
                    text.append(item.get("delta", ""))
                for choice in item.get("choices", []):
                    text.append(choice.get("delta", {}).get("content") or "")
            result.update(events=sorted(events), returned_models=sorted(returned_models),
                          expected_text_seen="POOL_OK" in "".join(text))
    except urllib.error.HTTPError as error:
        result["http_status"] = error.code
        # Provider errors can contain keys/URLs; never print the raw body.
        try:
            body = json.loads(error.read())
            detail = body.get("error", {})
            result["error_type"] = str(detail.get("type", ""))[:100] if isinstance(detail, dict) else "provider_error"
            result["error_code"] = str(detail.get("code", ""))[:100] if isinstance(detail, dict) else ""
            message = str(detail.get("message", "")) if isinstance(detail, dict) else ""
            message = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", message.replace(key, "[REDACTED]"))
            result["error_message"] = message[:350]
        except Exception:
            result["error_type"] = "non_json_error"
    except Exception as error:
        result["error_type"] = type(error).__name__
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    emit(result)
    return result


def native_probe(base_url, key, model, child_model):
    """Transparent relay observes actual traffic; never synthesizes model output."""
    executable = shutil.which("codex.cmd" if os.name == "nt" else "codex")
    if not executable:
        raise RuntimeError("Codex CLI unavailable")
    records = []
    relay_key = secrets.token_urlsafe(24)
    lock = threading.Lock()

    class Relay(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/v1/responses" or self.headers.get("Authorization") != "Bearer " + relay_key:
                self.send_error(403)
                return
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            data = json.loads(raw)
            record = {"requested_model": data.get("model"),
                      "child_instructions": "TJ_CHILD_PROBE" in str(data.get("instructions", "")),
                      "events": set(), "returned_models": set(), "tool_calls": []}
            with lock:
                if len(records) >= 8:
                    self.send_error(429, "Probe request limit")
                    return
                records.append(record)
            request = urllib.request.Request(base_url.rstrip("/") + "/responses", data=raw,
                                             headers={"Authorization": "Bearer " + key,
                                                      "Content-Type": "application/json"})
            try:
                upstream = urllib.request.urlopen(request, timeout=60)
            except urllib.error.HTTPError as error:
                record["http_status"] = error.code
                self.send_response(error.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                # Do not relay untrusted upstream errors that might contain a credential.
                self.wfile.write(json.dumps({"error": {"message": "Pool rejected request", "code": error.code}}).encode())
                emit({"request": len(records), "model": record["requested_model"], "http_status": error.code})
                return
            except Exception as error:
                record["error"] = type(error).__name__
                self.send_error(502, "Pool connection failed")
                return
            with upstream:
                record["http_status"] = upstream.status
                self.send_response(upstream.status)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "text/event-stream"))
                self.end_headers()
                try:
                    for line in upstream:
                        self.wfile.write(line)
                        self.wfile.flush()
                        if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                            continue
                        event = json.loads(line[5:])
                        record["events"].add(event.get("type"))
                        actual = event.get("response", {}).get("model")
                        if actual:
                            record["returned_models"].add(actual)
                        item = event.get("item", {})
                        if event.get("type") == "response.output_item.done" and item.get("type") == "function_call":
                            record["tool_calls"].append({"name": item.get("name"), "namespace": item.get("namespace")})
                except Exception as error:
                    record["error"] = type(error).__name__
            emit({"model": record["requested_model"], "child": record["child_instructions"],
                  "http_status": record["http_status"], "tool_calls": record["tool_calls"]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="tianji-live-") as temporary:
            root = Path(temporary)
            config, project = root / "codex", root / "project"
            (config / "agents").mkdir(parents=True)
            project.mkdir()
            config_text = (f'model = {json.dumps(model)}\nmodel_provider = "pool"\n'
                           'web_search = "disabled"\nmodel_reasoning_effort = "low"\n'
                           '[model_providers.pool]\nname = "Authorized pool probe"\nwire_api = "responses"\n'
                           f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                           'env_key = "TJ_RELAY_KEY"\nrequest_max_retries = 0\nstream_max_retries = 0\n'
                           'stream_idle_timeout_ms = 60000\n')
            role_text = ('name = "tianji-worker"\ndescription = "Read-only live routing probe"\n'
                         f'model = {json.dumps(child_model)}\nsandbox_mode = "read-only"\n'
                         'developer_instructions = "TJ_CHILD_PROBE: Run only the requested arithmetic shell command, '
                         'then report its exact stdout. Do not inspect configuration, environment, credentials or other files. '
                         'Do not spawn agents."\n')
            for path, content in ((config / "config.toml", config_text),
                                  (config / "agents" / "tianji-worker.toml", role_text)):
                path.write_text(content, encoding="utf-8")
                try:
                    tomllib.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    path.unlink()
                    raise
            env = os.environ.copy()
            for name in tuple(env):
                if name.endswith("API_KEY") or name in ("TJ_LIVE_POOL_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY"):
                    env.pop(name)
            env.update(CODEX_HOME=str(config), USERPROFILE=str(root), HOME=str(root), TJ_RELAY_KEY=relay_key)
            prompt = ("This is an authorized, minimal native subagent routing test. Do not read any files or environment. "
                      "Spawn exactly one tianji-worker using agent_type, fresh context, with this task: "
                      "Run python -c \"print(19*23)\" using exec_command and report the stdout. "
                      "Wait for that agent's completed result, then reply PROBE_PASS only if its command output is 437. "
                      "Do not calculate or run commands yourself. No retries or other tasks.")
            try:
                result = subprocess.run([executable, "exec", "--strict-config", "--skip-git-repo-check", "--json",
                                         "--sandbox", "read-only", "-C", str(project), prompt],
                                        stdin=subprocess.DEVNULL, env=env, text=True, encoding="utf-8",
                                        errors="replace", capture_output=True, timeout=180)
                # No raw stderr, tool args or transcript is printed/persisted.
                events = []
                for line in result.stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    item = event.get("item", {})
                    if item.get("type") == "collab_tool_call":
                        events.append({"tool": item.get("tool"), "status": item.get("status"),
                                       "receivers": len(item.get("receiver_thread_ids", []))})
                emit({"codex_exit": result.returncode, "native_agent_events": events,
                      "parent_claimed_pass": "PROBE_PASS" in result.stdout,
                      "requests": [{**record, "events": sorted(record["events"]),
                                    "returned_models": sorted(record["returned_models"])} for record in records]})
            except subprocess.TimeoutExpired:
                emit({"codex_timeout": 180, "requests": len(records)})
    finally:
        server.shutdown()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--protocol", choices=("responses", "chat"), default="responses")
    parser.add_argument("--native", action="store_true", help="Run a real Codex parent and native child through the pool")
    parser.add_argument("--child-model", help="Exact pool model name for the native child")
    args = parser.parse_args()
    key = os.environ.get("TJ_LIVE_POOL_KEY")
    if not key:
        parser.error("Set TJ_LIVE_POOL_KEY in this process environment; never put it in a file or argument")
    if args.native:
        if len(args.model) != 1 or not args.child_model:
            parser.error("--native requires exactly one --model and --child-model")
        native_probe(args.base_url, key, args.model[0], args.child_model)
    else:
        for model in args.model:
            probe(args.base_url, key, model, args.protocol)


if __name__ == "__main__":
    main()
