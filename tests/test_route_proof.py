import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from route_proof import (  # noqa: E402
    PROOF_LEVEL_DISPATCH,
    PROOF_LEVEL_INTEGRITY,
    PROOF_LEVEL_NONE,
    PROOF_LEVEL_WIRE,
    Dispatch,
    Integrity,
    RouteProof,
    Wire,
    integrity_snapshot,
    now_iso,
    snapshot_digest,
)


RUN_ID = "77802c0c-d50f-4fe8-bc09-8ea018cf2334"


class RouteProofTests(unittest.TestCase):
    def snapshot(self, **overrides):
        values = {
            "role_bindings": {"tianji-worker": "moonshotai/kimi-k3"},
            "menu": ["moonshotai/kimi-k3", "zai-org/glm-5.3"],
            "adapter_digest": "adapter-sha",
            "host_runtime_version": "1.53.0",
        }
        values.update(overrides)
        return integrity_snapshot(**values)

    def dispatch(self, subject_digest, verified=True):
        return Dispatch(
            verified=verified,
            host="cmdc",
            role="tianji-worker",
            requested_model="moonshotai/kimi-k3",
            declared_model="moonshotai/kimi-k3",
            correlation_id="call-1",
            start_event_id="evt-start",
            stop_event_id="evt-stop",
            event_key={"run_id": RUN_ID, "task_id": "probe", "attempt": 1},
            captured_at=now_iso(),
            subject_digest=subject_digest,
        )

    def test_integrity_is_recomputed_and_drift_invalidates_everything(self):
        snapshot = self.snapshot()
        proof = RouteProof(
            integrity=Integrity(snapshot=snapshot, captured_at=now_iso()),
            dispatch=self.dispatch(snapshot_digest(snapshot)),
            wire=Wire(verified=True, observed_endpoint="https://upstream", response_model="m"),
        )
        self.assertEqual(proof.effective_level(snapshot), PROOF_LEVEL_WIRE)

        drifted = self.snapshot(role_bindings={"tianji-worker": "other-model"})
        self.assertFalse(proof.integrity_valid(drifted))
        self.assertEqual(proof.effective_level(drifted), PROOF_LEVEL_NONE)
        self.assertFalse(proof.actual_model_verified(drifted))

    def test_level_climbs_one_evidence_domain_at_a_time(self):
        snapshot = self.snapshot()
        integrity = Integrity(snapshot=snapshot, captured_at=now_iso())

        bare = RouteProof(integrity=integrity)
        self.assertEqual(bare.effective_level(snapshot), PROOF_LEVEL_INTEGRITY)
        self.assertFalse(bare.actual_model_verified(snapshot))

        dispatched = RouteProof(
            integrity=integrity, dispatch=self.dispatch(snapshot_digest(snapshot)),
        )
        self.assertEqual(dispatched.effective_level(snapshot), PROOF_LEVEL_DISPATCH)
        self.assertFalse(dispatched.actual_model_verified(snapshot))

        wired = RouteProof(
            integrity=integrity,
            dispatch=self.dispatch(snapshot_digest(snapshot)),
            wire=Wire(verified=True, observed_endpoint="https://upstream", response_model="m"),
        )
        self.assertEqual(wired.effective_level(snapshot), PROOF_LEVEL_WIRE)
        self.assertTrue(wired.actual_model_verified(snapshot))

    def test_host_dispatch_is_the_command_code_ceiling(self):
        snapshot = self.snapshot()
        proof = RouteProof(
            integrity=Integrity(snapshot=snapshot, captured_at=now_iso()),
            dispatch=self.dispatch(snapshot_digest(snapshot)),
            wire=Wire(verified=False),
        )
        self.assertEqual(proof.effective_level(snapshot), PROOF_LEVEL_DISPATCH)
        self.assertFalse(proof.actual_model_verified(snapshot))
        self.assertFalse(proof.wire.verified)

    def test_dispatch_bound_to_another_configuration_does_not_count(self):
        snapshot = self.snapshot()
        proof = RouteProof(
            integrity=Integrity(snapshot=snapshot, captured_at=now_iso()),
            dispatch=self.dispatch("a-different-subject", verified=True),
        )
        self.assertEqual(proof.effective_level(snapshot), PROOF_LEVEL_INTEGRITY)

    def test_never_verified_dispatch_stays_at_integrity(self):
        snapshot = self.snapshot()
        proof = RouteProof(
            integrity=Integrity(snapshot=snapshot, captured_at=now_iso()),
            dispatch=self.dispatch(snapshot_digest(snapshot), verified=False),
        )
        self.assertEqual(proof.effective_level(snapshot), PROOF_LEVEL_INTEGRITY)

    def test_missing_integrity_means_no_proof_at_all(self):
        snapshot = self.snapshot()
        proof = RouteProof(dispatch=self.dispatch(snapshot_digest(snapshot)))
        self.assertEqual(proof.effective_level(snapshot), PROOF_LEVEL_NONE)

    def test_round_trip_through_json_shape(self):
        snapshot = self.snapshot()
        proof = RouteProof(
            integrity=Integrity(snapshot=snapshot, captured_at=now_iso()),
            dispatch=self.dispatch(snapshot_digest(snapshot)),
            wire=Wire(verified=False),
        )
        restored = RouteProof.from_dict(proof.as_dict())
        self.assertEqual(restored.effective_level(snapshot), proof.effective_level(snapshot))
        self.assertEqual(RouteProof.from_dict({}).effective_level(snapshot), PROOF_LEVEL_NONE)
        self.assertEqual(RouteProof.from_dict(None).effective_level(snapshot), PROOF_LEVEL_NONE)


if __name__ == "__main__":
    unittest.main()
