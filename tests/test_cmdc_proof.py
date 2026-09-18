import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(ROOT / "skills" / "tianji"))
sys.path.insert(0, str(SCRIPTS))

import ledger_reader  # noqa: E402
from host_adapters.cmdc import proof_collector, role_configure  # noqa: E402
from host_adapters.cmdc.detector import CmdcDetector  # noqa: E402
from host_adapters.cmdc.role_renderer import read_cmdc_binding, render_cmdc  # noqa: E402
from role_contract import (  # noqa: E402
    ECONOMY,
    ESCALATION,
    QUALITY,
    ROLE_TIERS,
    discover_role_packages,
    roles_for_tier,
)


RUN = "77802c0c-d50f-4fe8-bc09-8ea018cf2334"
MODELS = ["zai-org/glm-5.3", "moonshotai/kimi-k3", "gpt-5.6-terra"]


def envelope(event, agent, task_id="probe", correlation="call-probe",
             occurred="2026-09-10T12:00:00+00:00", event_id=None,
             invocation_id="inv-1"):
    return {
        "schema_version": 2,
        "event_id": event_id or f"e-{event}",
        "event": event,
        "host": "cmdc",
        "run_id": RUN,
        "session_id": "session-1",
        "task_id": task_id,
        "attempt": 1,
        "invocation_id": invocation_id,
        "correlation_id": correlation,
        "agent": agent,
        "occurred_at": occurred,
        "recorded_at": occurred,
        "detail": {},
    }


class RoleConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-rolecfg-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.agents = self.home / "agents"
        self.agents.mkdir(parents=True)
        self.packages = discover_role_packages(ROOT / "agents")
        for package in self.packages:
            (self.agents / f"{package.name}.md").write_text(
                render_cmdc(package), encoding="utf-8",
            )

    def detector(self):
        return CmdcDetector(root=self.home, run=lambda args: "1.53.0")

    def test_set_binding_writes_a_real_menu_model(self):
        role_configure.set_binding(
            self.agents, "tianji-worker", "zai-org/glm-5.3",
            models=MODELS, source_root=ROOT,
        )
        self.assertEqual(
            read_cmdc_binding(self.agents / "tianji-worker.md"), "zai-org/glm-5.3",
        )

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(role_configure.RoleConfigError):
            role_configure.set_binding(
                self.agents, "tianji-worker", "not-in-the-menu",
                models=MODELS, source_root=ROOT,
            )

    def test_reset_to_primary_sentinel_is_allowed(self):
        role_configure.set_binding(
            self.agents, "tianji-worker", "主模型", models=MODELS, source_root=ROOT,
        )
        self.assertEqual(read_cmdc_binding(self.agents / "tianji-worker.md"), "主模型")

    def test_uninstalled_role_cannot_be_bound(self):
        (self.agents / "tianji-worker.md").unlink()
        with self.assertRaises(role_configure.RoleConfigError):
            role_configure.set_binding(
                self.agents, "tianji-worker", "zai-org/glm-5.3",
                models=MODELS, source_root=ROOT,
            )

    def test_core_roles_are_reported_until_all_are_bound(self):
        self.assertEqual(
            sorted(role_configure.unconfigured(self.agents)),
            sorted(role_configure.REQUIRED_BINDINGS),
        )
        for role in role_configure.REQUIRED_BINDINGS:
            role_configure.set_binding(
                self.agents, role, "zai-org/glm-5.3", models=MODELS, source_root=ROOT,
            )
        self.assertEqual(role_configure.unconfigured(self.agents), [])

    def test_plan_names_tiers_not_vendors(self):
        text = role_configure.describe(self.agents, MODELS)
        # The tier names come from the shared contract, not from this adapter:
        # iterating ROLE_TIERS is what keeps the two from drifting apart again.
        for tier in ROLE_TIERS:
            self.assertIn(f"[{tier}]", text)
        for vendor in ("openai", "anthropic", "google"):
            self.assertNotIn(vendor, text.lower())


class TierMappingTests(unittest.TestCase):
    """A host maps its own live menu onto the shared tiers, once."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-tiers-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.agents = self.home / "agents"
        self.agents.mkdir(parents=True)
        for package in discover_role_packages(ROOT / "agents"):
            (self.agents / f"{package.name}.md").write_text(
                render_cmdc(package), encoding="utf-8",
            )

    def bind(self, tier, model):
        return role_configure.set_tier_binding(
            self.agents, tier, model, models=MODELS, source_root=ROOT,
        )

    def test_mapping_a_tier_binds_every_installed_role_in_it(self):
        written = self.bind(ECONOMY, "zai-org/glm-5.3")
        self.assertEqual(len(written), len(roles_for_tier(ECONOMY)))
        for role in roles_for_tier(ECONOMY):
            self.assertEqual(
                read_cmdc_binding(self.agents / f"{role}.md"), "zai-org/glm-5.3",
            )

    def test_a_tier_model_must_come_from_the_live_menu(self):
        with self.assertRaises(role_configure.RoleConfigError):
            self.bind(QUALITY, "not-in-the-menu")

    def test_an_unknown_tier_is_refused(self):
        with self.assertRaises(role_configure.RoleConfigError):
            self.bind("platinum", "zai-org/glm-5.3")

    def test_a_tier_with_no_installed_role_is_refused_not_silently_ok(self):
        for role in roles_for_tier(ESCALATION):
            (self.agents / f"{role}.md").unlink()
        with self.assertRaises(role_configure.RoleConfigError):
            self.bind(ESCALATION, "zai-org/glm-5.3")

    def test_unmapped_tiers_are_reported_until_each_is_mapped(self):
        self.assertEqual(set(role_configure.unmapped_tiers(self.agents)), set(ROLE_TIERS))
        for tier in ROLE_TIERS:
            self.bind(tier, "zai-org/glm-5.3")
        self.assertEqual(role_configure.unmapped_tiers(self.agents), [])

    def test_bindings_by_tier_groups_by_the_shared_tiers(self):
        self.bind(QUALITY, "gpt-5.6-terra")
        grouped = role_configure.bindings_by_tier(self.agents)
        self.assertEqual(set(grouped), set(ROLE_TIERS))
        self.assertEqual(set(grouped[QUALITY]), set(roles_for_tier(QUALITY)))
        self.assertTrue(
            all(binding == "gpt-5.6-terra" for binding in grouped[QUALITY].values()),
        )
        self.assertTrue(any(not binding for binding in grouped[ECONOMY].values()))


class ProofCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tianji-proof-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "commandcode"
        self.home.mkdir(parents=True)
        self.agents = self.home / "agents"
        self.agents.mkdir(parents=True)
        self.packages = discover_role_packages(ROOT / "agents")
        for package in self.packages:
            binding = "zai-org/glm-5.3" if package.name == "tianji-worker" else None
            (self.agents / f"{package.name}.md").write_text(
                render_cmdc(package, binding=binding), encoding="utf-8",
            )
        (self.home / "mods").mkdir(parents=True)
        (self.home / "mods" / "tianji-state.ts").write_text("// mod", encoding="utf-8")
        self.workspace = Path(self.temporary.name) / "ws"
        (self.workspace / ".tianji").mkdir(parents=True)
        self.ledger = self.workspace / ".tianji" / "state.jsonl"
        (self.home / "tianji-cmdc.json").write_text(
            json.dumps({"host": "cmdc", "roles": [p.name for p in self.packages]}),
            encoding="utf-8",
        )

    def detector(self, version="1.53.0"):
        return CmdcDetector(root=self.home, run=lambda args: version)

    def register_dispatch(self, *, digest=None, invocation_id="inv-1",
                          task_id="probe", attempt=1, detector=None,
                          role="tianji-worker"):
        """Record the dispatch, fixing its configuration snapshot.

        Proof compares against this value, so a test that expects host_dispatch
        must register the digest of the configuration the dispatch saw.
        """
        from run_registry import RunRegistry
        from route_proof import snapshot_digest

        detector = detector or self.detector()
        if digest is None:
            digest = snapshot_digest(detector.current_snapshot())
        registry = RunRegistry(self.workspace)
        try:
            run = registry.create_run(host="cmdc", session_id="session-1", run_id=RUN)
        except Exception:
            run = registry.get_run(RUN)
        try:
            task = registry.create_task(
                run.run_id, task_id, attempt=attempt, role=role,
            )
        except Exception:
            task = registry.get_task(EventKey(RUN, task_id, attempt))
        invocation, _token = registry.create_pending_invocation(
            task.event_key, host="cmdc", session_id="session-1",
            role=role, invocation_id=invocation_id,
            subject_digest=digest,
        )
        return invocation

    def write_ledger(self, records):
        self.ledger.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8",
        )

    def test_paired_dispatch_yields_host_dispatch_proof(self):
        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()
        proof, reason = proof_collector.collect(detector, self.ledger)
        self.assertIsNotNone(proof, reason)
        proof_collector.record(detector, proof)
        self.assertEqual(detector.proof_level(), "host_dispatch")
        self.assertTrue(detector.probe_ok()[0])
        self.assertFalse(detector.proof().actual_model_verified(detector.current_snapshot()))
        self.assertFalse(proof.wire.verified if proof.wire else False)

    def test_recording_without_a_workspace_writes_no_journal(self):
        # The observation journal is per-workspace state. Defaulting to the
        # process's cwd is how a test run overwrote the live machine's journal,
        # after which the check reported a fixture's values as this machine's
        # history (2026-09-17: "模型菜单: 0 项 → 70 项" plus a stale binding).
        import os

        import observation_journal

        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()
        proof, reason = proof_collector.collect(detector, self.ledger)
        self.assertIsNotNone(proof, reason)

        live = observation_journal.journal_path(os.getcwd())
        before = (live.exists(), live.stat().st_mtime_ns if live.exists() else None)
        proof_collector.record(detector, proof)
        after = (live.exists(), live.stat().st_mtime_ns if live.exists() else None)
        self.assertEqual(before, after, "a proof write must not touch the cwd's journal")

    def test_a_dispatch_without_a_recorded_snapshot_is_not_proof(self):
        # Nothing recorded what the configuration was, so nothing can attest it.
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        proof, reason = proof_collector.collect(self.detector(), self.ledger)
        self.assertIsNone(proof)
        self.assertIn("派工时的配置快照", reason)

    def test_a_snapshot_recorded_at_collection_time_cannot_vouch(self):
        # The digest was fixed before the configuration drifted, so the old
        # dispatch must not approve the new configuration.
        self.register_dispatch(digest="0" * 64)
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()
        proof, reason = proof_collector.collect(detector, self.ledger)
        self.assertIsNotNone(proof, reason)
        proof_collector.record(detector, proof)
        self.assertEqual(detector.proof_level(), "integrity")
        self.assertFalse(detector.probe_ok()[0])

    def test_start_without_stop_is_not_proof(self):
        self.write_ledger([envelope("subagent_start", "tianji-worker")])
        proof, reason = proof_collector.collect(self.detector(), self.ledger)
        self.assertIsNone(proof)
        self.assertIn("stop", reason)

    def test_mismatched_correlation_is_not_proof(self):
        self.write_ledger([
            envelope("subagent_start", "tianji-worker", correlation="call-a"),
            envelope("subagent_stop", "tianji-worker", correlation="call-b",
                     occurred="2026-09-10T12:00:30+00:00"),
        ])
        proof, _ = proof_collector.collect(self.detector(), self.ledger)
        self.assertIsNone(proof)

    def test_any_bound_role_proves_the_dispatch_path(self):
        # The evidence is "the host dispatched a Tianji role", not "the probe
        # role ran": a verifier dispatch proves the same path, so no role has to
        # be kept around purely to be dispatched once.
        role_configure.set_binding(
            self.agents, "tianji-verifier", "zai-org/glm-5.3",
            models=MODELS, source_root=ROOT,
        )
        self.register_dispatch(role="tianji-verifier")
        self.write_ledger([
            envelope("subagent_start", "tianji-verifier"),
            envelope("subagent_stop", "tianji-verifier",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()
        proof, reason = proof_collector.collect(detector, self.ledger)
        self.assertIsNotNone(proof, reason)
        self.assertEqual(proof.dispatch.role, "tianji-verifier")
        proof_collector.record(detector, proof)
        self.assertEqual(detector.proof_level(), "host_dispatch")

    def test_nothing_bound_cannot_prove_routing(self):
        (self.agents / "tianji-worker.md").write_text(
            render_cmdc(next(p for p in self.packages if p.name == "tianji-worker")),
            encoding="utf-8",
        )
        self.write_ledger([envelope("subagent_start", "tianji-worker")])
        proof, reason = proof_collector.collect(self.detector(), self.ledger)
        self.assertIsNone(proof)
        self.assertIn("未固定绑定", reason)

    def test_missing_ledger_is_reported(self):
        proof, reason = proof_collector.collect(self.detector(), self.ledger)
        self.assertIsNone(proof)
        self.assertIn("账本不存在", reason)

    def test_host_version_drift_invalidates_a_recorded_proof(self):
        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()
        proof, _ = proof_collector.collect(detector, self.ledger)
        proof_collector.record(detector, proof)
        self.assertTrue(self.detector().probe_ok()[0])

        drifted = self.detector(version="1.54.0")
        self.assertFalse(drifted.probe_ok()[0])
        self.assertEqual(drifted.proof_level(), "none")

    def test_role_file_drift_invalidates_a_recorded_proof(self):
        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()
        proof, _ = proof_collector.collect(detector, self.ledger)
        proof_collector.record(detector, proof)

        (self.agents / "tianji-worker.md").write_text("changed by user", encoding="utf-8")
        self.assertFalse(self.detector().probe_ok()[0])

    def test_dispatch_evidence_older_than_the_adapter_is_not_accepted(self):
        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        # Change the adapter after the dispatch: the old evidence cannot vouch
        # for the new adapter.
        cutoff = datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)
        proof, reason = proof_collector.collect(
            self.detector(), self.ledger, not_before=cutoff,
        )
        self.assertIsNone(proof)
        self.assertIn("早于当前适配层安装", reason)

        # A dispatch after the change is accepted.
        self.register_dispatch(invocation_id="inv-2", task_id="probe-2")
        self.write_ledger([
            envelope("subagent_start", "tianji-worker", task_id="probe-2",
                     invocation_id="inv-2", occurred="2026-09-10T14:00:00+00:00"),
            envelope("subagent_stop", "tianji-worker", task_id="probe-2",
                     invocation_id="inv-2",
                     occurred="2026-09-10T14:00:30+00:00", event_id="e-stop-2"),
        ])
        fresh, reason = proof_collector.collect(
            self.detector(), self.ledger, not_before=cutoff,
        )
        self.assertIsNotNone(fresh, reason)

    def test_reapplying_the_same_binding_does_not_outrun_the_dispatch(self):
        # Measured live 2026-09-17: rebinding a role rewrites the adapter
        # manifest (role_configure.set_binding -> managed_files.record_written)
        # without changing a byte of adapter code. The manifest's own mtime then
        # postdates every dispatch in the ledger, so the freshness gate refused
        # them all -- including dispatches whose recorded snapshot still matches
        # the current configuration.
        #
        # Re-applying the binding a role already has writes the same bytes: the
        # configuration did not change, so the dispatch is evidence for the
        # current configuration and must not be refused as "older than the
        # adapter". Only a real adapter change (the code the installer places)
        # may move that baseline.
        import os

        from host_adapters.cmdc import native_installer
        from managed_files import record_written

        manifest = self.home / native_installer.ADAPTER_MANIFEST
        manifest.write_text("{}", encoding="utf-8")
        record_written(manifest, "mods/tianji-state.ts", b"// mod")
        # The adapter's code was installed a day before the dispatch.
        installed_at = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc).timestamp()
        os.utime(self.home / "mods" / "tianji-state.ts", (installed_at, installed_at))

        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])

        # The rebind: same model, identical role card bytes, manifest rewritten.
        role_configure.set_binding(
            self.agents, "tianji-worker", "zai-org/glm-5.3",
            models=MODELS, source_root=ROOT,
        )

        detector = self.detector()
        proof, reason = proof_collector.collect(
            detector, self.ledger, not_before=proof_collector.fresh_after(self.home),
        )
        self.assertIsNotNone(proof, reason)
        self.assertEqual(proof.dispatch.role, "tianji-worker")
        proof_collector.record(detector, proof)
        self.assertEqual(detector.proof_level(), "host_dispatch")

    def test_a_changed_role_binding_is_named_in_the_probe_report(self):
        # A drift report that only says "something changed" makes the reader go
        # diff the whole configuration; it should name the role.
        import os

        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()
        proof, reason = proof_collector.collect(detector, self.ledger)
        self.assertIsNotNone(proof, reason)
        proof_collector.record(detector, proof, workspace=self.workspace)

        # tianji-worker is the bound role in this fixture; rebinding it is a real
        # configuration change, and the report must name it.
        role_configure.set_binding(
            self.agents, "tianji-worker", "moonshotai/kimi-k3",
            models=MODELS, source_root=ROOT,
        )
        previous = os.getcwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, previous)

        ok, detail = self.detector().probe_ok()
        self.assertFalse(ok)
        self.assertIn("配置已变", detail)
        self.assertIn("tianji-worker", detail)
        self.assertIn("zai-org/glm-5.3", detail)
        self.assertIn("moonshotai/kimi-k3", detail)

    def test_the_newest_dispatch_cycle_wins_over_an_earlier_one(self):
        # The same task was dispatched twice: the first cycle predates the
        # adapter change, the second does not. The fresh one must be used.
        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker", correlation="call-old",
                     occurred="2026-09-10T12:00:00+00:00", event_id="e1"),
            envelope("subagent_stop", "tianji-worker", correlation="call-old",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e2"),
            envelope("subagent_start", "tianji-worker", correlation="call-new",
                     occurred="2026-09-10T14:00:00+00:00", event_id="e3"),
            envelope("subagent_stop", "tianji-worker", correlation="call-new",
                     occurred="2026-09-10T14:00:30+00:00", event_id="e4"),
        ])
        proof, reason = proof_collector.collect(
            self.detector(), self.ledger,
            not_before=datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(proof, reason)
        self.assertEqual(proof.dispatch.correlation_id, "call-new")
        self.assertEqual(proof.dispatch.start_event_id, "e3")
        self.assertEqual(proof.dispatch.stop_event_id, "e4")

    def test_dispatch_pairs_returns_every_completed_cycle(self):
        self.write_ledger([
            envelope("subagent_start", "tianji-worker", correlation="c1"),
            envelope("subagent_stop", "tianji-worker", correlation="c1",
                     occurred="2026-09-10T12:00:30+00:00"),
            envelope("subagent_start", "tianji-worker", correlation="c2",
                     occurred="2026-09-10T13:00:00+00:00"),
            envelope("subagent_stop", "tianji-worker", correlation="c2",
                     occurred="2026-09-10T13:00:30+00:00"),
        ])
        events = ledger_reader.authoritative_events(self.ledger)
        pairs = proof_collector.dispatch_pairs(events, role="tianji-worker")
        self.assertEqual([pair[0]["correlation_id"] for pair in pairs], ["c1", "c2"])

    def test_a_start_without_a_stop_is_reported_as_unpaired(self):
        self.write_ledger([envelope("subagent_start", "tianji-worker", correlation="c1")])
        proof, reason = proof_collector.collect(self.detector(), self.ledger)
        self.assertIsNone(proof)
        self.assertIn("未见成对 stop", reason)

    def test_fresh_after_reads_the_adapter_manifest(self):
        self.assertIsNone(proof_collector.fresh_after(self.home))
        manifest = self.home / "tianji-cmdc.manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        self.assertIsNotNone(proof_collector.fresh_after(self.home))

    def test_status_reads_the_same_route_proof_the_probe_reports(self):
        # One fact, two instruments: the probe (cmdc-routing-probe.py) reports the
        # level of the proof it derives live from the ledger; install.py status
        # must derive its verdict from that same evidence. If status instead reads
        # only the last persisted artifact, the two disagree -- the probe says a
        # role was paired while status says the proof is MISSING -- even though
        # nothing about the dispatch or the configuration differs between them.
        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()

        # Reader A -- the probe path.
        probe_proof, _ = proof_collector.collect(
            detector, self.ledger,
            not_before=proof_collector.fresh_after(self.home),
        )
        self.assertIsNotNone(probe_proof)
        probe_level = probe_proof.effective_level(detector.current_snapshot())

        # Reader B -- the status wiring.
        status_level = detector.route_proof_from_ledger(self.ledger)

        self.assertEqual(status_level, probe_level)
        self.assertEqual(probe_level, "host_dispatch")

    def test_probe_ok_judges_the_live_evidence_not_the_persisted_proof(self):
        # The conclusion machine reads probe_ok() (doctor.collect_facts), so if it
        # judged only the last persisted artifact the machine could call a routing
        # configuration unproven while a dispatch for it sat in the ledger. Here the
        # ledger holds a valid dispatch and nothing is persisted: the fact must be
        # true.
        #
        # Called with no ledger argument -- the way the machine calls it -- so that
        # against the old implementation this goes red on the assertion (probe_ok()
        # returned the persisted level) rather than on the signature, which is what
        # makes it a control rather than a shape test.
        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()
        self.assertNotEqual(detector.proof_level(), "host_dispatch", "nothing was persisted")

        before = Path.cwd()
        self.addCleanup(os.chdir, before)
        os.chdir(self.workspace)

        ok, detail = detector.probe_ok()
        self.assertTrue(ok, detail)
        self.assertIn("host_dispatch", detail)

    def test_status_shows_the_level_the_machine_judges(self):
        # One fact, one level: the level status prints must be the one probe_ok()
        # verdicts, so the table and the conclusion machine cannot answer
        # differently about one and the same dispatch.
        self.register_dispatch()
        self.write_ledger([
            envelope("subagent_start", "tianji-worker"),
            envelope("subagent_stop", "tianji-worker",
                     occurred="2026-09-10T12:00:30+00:00", event_id="e-stop"),
        ])
        detector = self.detector()

        level = detector.probe_level(self.ledger)
        self.assertEqual(level, "host_dispatch")
        # The machine's fact is the verdict on that very level -- not a level of
        # its own, and not a second reading of the persisted artifact.
        ok, detail = detector.probe_ok(self.ledger)
        self.assertTrue(ok, detail)
        self.assertIn(level, detector.verdict_for_level(level)[1])


if __name__ == "__main__":
    unittest.main()
