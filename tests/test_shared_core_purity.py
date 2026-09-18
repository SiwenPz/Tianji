"""Guards the shared core / host adapter boundary.

Removing the Command Code adapter must leave the shared core complete: no host
name may appear in a shared semantic module, and the shared readers must not
import a host adapter.
"""
import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Shared semantic modules: role/orchestration state, ledger, proof, installers
# and readers. A host name here means the boundary has been breached.
SHARED_SEMANTIC_MODULES = (
    "acceptance-gate.py",
    "board.py",
    "claim.py",
    "conclusions.py",
    "doctor.py",
    "ledger_schema.py",
    "ledger_reader.py",
    "ledger_sink.py",
    "lock_protocol.py",
    "managed_files.py",
    "model_source.py",
    "observation.py",
    "python_runtime.py",
    "role_contract.py",
    "route_proof.py",
    "run_registry.py",
    "state-log.py",
    "tianji-dash.py",
)

HOST_MARKERS = (re.compile(r"\bcmdc\b", re.IGNORECASE), re.compile(r"commandcode", re.IGNORECASE))


class SharedCorePurityTests(unittest.TestCase):
    def test_shared_semantic_modules_never_name_a_host(self):
        offenders = {}
        for name in SHARED_SEMANTIC_MODULES:
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            if any(marker.search(text) for marker in HOST_MARKERS):
                offenders[name] = [
                    line.strip() for line in text.splitlines()
                    if any(marker.search(line) for marker in HOST_MARKERS)
                ]
        self.assertEqual(offenders, {}, f"shared core names a host: {offenders}")

    def test_shared_readers_do_not_import_a_host_adapter(self):
        for name in ("board.py", "tianji-dash.py", "state-log.py", "ledger_reader.py"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertNotIn("host_adapters", text, name)

    def test_the_proof_collector_reads_through_the_shared_reader(self):
        # A host collector must not reach the ledger some other way: the shared
        # reader is what decides which records are authoritative.
        adapter = ROOT / "skills" / "tianji" / "host_adapters" / "cmdc"
        collector = (adapter / "proof_collector.py").read_text(encoding="utf-8")
        self.assertIn("import ledger_reader", collector)
        self.assertNotIn("import board", collector)
        self.assertNotIn("from board import", collector)

    def test_shared_core_is_importable_without_the_adapter(self):
        # ledger, proof and registry must stand on their own.
        from ledger_schema import EventKey, build_event  # noqa: F401
        from run_registry import RunRegistry  # noqa: F401
        from route_proof import RouteProof  # noqa: F401
        import board  # noqa: F401
        import doctor  # noqa: F401

        run_id = "77802c0c-d50f-4fe8-bc09-8ea018cf2334"
        event = build_event(
            event_id="e-1", event="subagent_start", host="any-host", run_id=run_id,
            session_id="s", task_id="t", attempt=1, invocation_id="i",
            correlation_id="c", agent="a", occurred_at="2026-09-10T12:00:00+00:00",
            recorded_at="2026-09-10T12:00:00+00:00", detail={},
        )
        self.assertEqual(EventKey(event["run_id"], "t", 1).task_id, "t")
        self.assertIsNotNone(RouteProof.from_dict(None))

    def test_the_conclusion_machine_never_names_a_host(self):
        # The machine answers for every host; naming one -- including hosts the
        # cmdc-only marker above would miss -- is the boundary breaking.
        text = (SCRIPTS / "doctor.py").read_text(encoding="utf-8").lower()
        for marker in ("kimi", "codex", "cmdc", "commandcode", "claude", "host_adapters"):
            self.assertNotIn(marker, text, marker)

    def test_the_conclusion_machine_never_parses_a_host_config_format(self):
        # Reading a host's own configuration format is the host detector's job;
        # a shared machine that parses a tui file has learned one host's config.
        text = (SCRIPTS / "doctor.py").read_text(encoding="utf-8")
        self.assertNotIn("tomllib", text)
        self.assertNotIn("tui", text.lower())
        self.assertIn("status_line_ok", text)

    def test_host_registration_is_the_only_shared_extension_point(self):
        text = (SCRIPTS / "tianji-init.py").read_text(encoding="utf-8")
        # The shared conclusion machine must not branch on a host name; host
        # names may only appear in the detector registry.
        self.assertIn("HOST_DETECTORS", text)
        self.assertNotIn('host_name == "', text)
        self.assertNotIn("host_name in (", text)
        self.assertNotIn('host_name ==', text)

    def test_the_native_mod_reaches_identity_only_through_the_claim_bridge(self):
        # The adapter must not read the registry's internal files: identity
        # comes from the shared claim process, and nothing else.
        mod = (
            ROOT / "skills" / "tianji" / "host_adapters" / "cmdc" / "mod" / "tianji-state.ts"
        ).read_text(encoding="utf-8")
        for forbidden in ("bindings", "readBinding", "agentBindingFile", "bindingFile",
                          "resolveEventKey", "claim-tokens"):
            self.assertNotIn(forbidden, mod, f"the mod still reaches into {forbidden}")
        self.assertIn("claimInvocation", mod)

    def test_the_native_mod_owns_no_reducer_or_health_verdict(self):
        # Deciding that a worker is stuck, and alerting on it, is a judgement
        # over the shared ledger. The footer reports raw facts; it does not
        # replay the ledger, grade health, or write an alert event.
        mod = (
            ROOT / "skills" / "tianji" / "host_adapters" / "cmdc" / "mod" / "tianji-state.ts"
        ).read_text(encoding="utf-8")
        for forbidden in ("healthOf", "replayWorkers", "status_alert",
                          "YELLOW_MS", "RED_MS", "'stalled'", "'slow'"):
            self.assertNotIn(forbidden, mod, f"the mod still decides {forbidden}")
        self.assertIn("setStatus", mod)

    def test_the_native_mod_never_shells_out(self):
        # spawnSync with an argument array; a shell would re-parse the token,
        # the workspace and the call id as command text.
        mod = (
            ROOT / "skills" / "tianji" / "host_adapters" / "cmdc" / "mod" / "tianji-state.ts"
        ).read_text(encoding="utf-8")
        for forbidden in ("shell: true", "execSync", "execFileSync", "child_process.exec"):
            self.assertNotIn(forbidden, mod, f"the mod uses a shell primitive: {forbidden}")
        imports = re.findall(r"from '(node:child_process)'", mod)
        self.assertEqual(imports, ["node:child_process"])
        self.assertIn("import { spawnSync } from 'node:child_process'", mod)

    def test_the_ts_mod_uses_the_shared_ledger_lock(self):
        mod = (
            ROOT / "skills" / "tianji" / "host_adapters" / "cmdc" / "mod" / "tianji-state.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("'.tianji', 'state.lock'", mod)
        # Both appends -- ledger and diagnostics -- are inside the lock closure,
        # so neither can interleave with another writer.
        self.assertIn("const written = withLedgerLock(workspace, () => {", mod)
        self.assertIn("withLedgerLock(workspace, () => {", mod)
        self.assertEqual(mod.count("fs.appendFileSync("), 2, "an append escaped the lock")

    def test_adapter_package_is_self_contained(self):
        adapter = ROOT / "skills" / "tianji" / "host_adapters" / "cmdc"
        self.assertTrue((adapter / "detector.py").is_file())
        # The adapter may consume the shared schema; it must not redefine it.
        for name in ("ledger_schema.py", "run_registry.py", "route_proof.py",
                     "managed_files.py", "board.py"):
            self.assertFalse((adapter / name).exists(), f"adapter redefines {name}")

    def test_ledger_schema_document_matches_the_python_contract(self):
        from ledger_schema import REQUIRED_FIELDS

        schema = json.loads(
            (ROOT / "skills" / "tianji" / "schemas" / "tianji-ledger.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(set(schema["required"]), REQUIRED_FIELDS)

    def test_cross_language_identity_fixture_exists_and_is_shared(self):
        fixture = ROOT / "skills" / "tianji" / "schemas" / "ledger-identity.fixture.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"description", "input", "expected"})
        self.assertTrue(data["expected"]["event_id"].startswith("cmdc:"))


# Directories that are not this repository's own source: tool state, build
# output, and the user's local (gitignored) checkouts.
_NOT_OURS = (".git", ".tianji", ".tmp", ".third_party", ".commandcode",
             "__pycache__", "node_modules")
_TEXT_SUFFIXES = (".md", ".py", ".ts", ".toml", ".json", ".yaml", ".yml", ".txt")


class NoThirdPartyMaterialTests(unittest.TestCase):
    """The repository is pure MIT: nothing in it is copied from another project.

    Pinned mechanically because the failure mode is silent: a copied file
    arrives looking like ordinary source, and the licence obligation it drags
    in is only noticed by whoever audits the repo much later.
    """

    def sources(self):
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
                continue
            if any(part in _NOT_OURS for part in path.relative_to(ROOT).parts):
                continue
            # This file holds the markers themselves; it is the guard, not content.
            if path.resolve() == Path(__file__).resolve():
                continue
            yield path

    def test_no_license_appendages_remain(self):
        for name in ("NOTICE", "THIRD_PARTY_NOTICES.md", "third_party"):
            self.assertFalse((ROOT / name).exists(), f"{name} is gone with the code it covered")

    def test_no_file_declares_an_upstream_origin(self):
        markers = ("codex-astra-luna-orchestrator", "Apache-2.0", "Apache License")
        offenders = [
            str(path.relative_to(ROOT))
            for path in self.sources()
            if any(marker in path.read_text(encoding="utf-8", errors="replace")
                   for marker in markers)
        ]
        self.assertEqual(offenders, [], "copied-in material carries a licence obligation")
