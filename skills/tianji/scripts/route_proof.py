"""Shared Tianji route-proof model.

Proof is stored as three independent evidence domains rather than one
monotonic level, because dispatch can be genuinely proven once and later go
stale when roles, the menu, the adapter or the host runtime change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


PROOF_LEVEL_NONE = "none"
PROOF_LEVEL_INTEGRITY = "integrity"
PROOF_LEVEL_DISPATCH = "host_dispatch"
PROOF_LEVEL_WIRE = "wire_verified"

PROOF_LEVELS = (
    PROOF_LEVEL_NONE,
    PROOF_LEVEL_INTEGRITY,
    PROOF_LEVEL_DISPATCH,
    PROOF_LEVEL_WIRE,
)


def digest(value: Any) -> str:
    """Stable content digest for any JSON-serializable value."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def integrity_snapshot(
    *, role_bindings: Any, menu: Any, adapter_digest: str, host_runtime_version: str,
) -> dict[str, str]:
    """Snapshot the subjects whose drift must invalidate an old proof."""
    return {
        "role_bindings_digest": digest(role_bindings),
        "menu_snapshot_digest": digest(menu),
        "adapter_digest": adapter_digest,
        "host_runtime_version": host_runtime_version,
    }


def snapshot_digest(snapshot: dict[str, str]) -> str:
    return digest(snapshot)


@dataclass(frozen=True)
class Integrity:
    snapshot: dict[str, str]
    captured_at: str

    def as_dict(self) -> dict[str, Any]:
        return {"snapshot": dict(self.snapshot), "captured_at": self.captured_at}


@dataclass(frozen=True)
class Dispatch:
    verified: bool
    host: str
    role: str
    requested_model: str
    declared_model: str
    correlation_id: str
    start_event_id: str
    stop_event_id: str
    event_key: dict[str, Any]
    captured_at: str
    subject_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "host": self.host,
            "role": self.role,
            "requested_model": self.requested_model,
            "declared_model": self.declared_model,
            "correlation_id": self.correlation_id,
            "start_event_id": self.start_event_id,
            "stop_event_id": self.stop_event_id,
            "event_key": dict(self.event_key),
            "captured_at": self.captured_at,
            "subject_digest": self.subject_digest,
        }


@dataclass(frozen=True)
class Wire:
    verified: bool = False
    observed_endpoint: str | None = None
    response_model: str | None = None
    captured_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "observed_endpoint": self.observed_endpoint,
            "response_model": self.response_model,
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True)
class RouteProof:
    integrity: Integrity | None = None
    dispatch: Dispatch | None = None
    wire: Wire | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "integrity": self.integrity.as_dict() if self.integrity else None,
            "dispatch": self.dispatch.as_dict() if self.dispatch else None,
            "wire": self.wire.as_dict() if self.wire else None,
        }

    @staticmethod
    def from_dict(data: Any) -> "RouteProof":
        if not isinstance(data, dict):
            return RouteProof()
        integrity = data.get("integrity")
        dispatch = data.get("dispatch")
        wire = data.get("wire")
        return RouteProof(
            integrity=Integrity(
                snapshot=dict(integrity["snapshot"]),
                captured_at=integrity["captured_at"],
            ) if isinstance(integrity, dict) and "snapshot" in integrity else None,
            dispatch=Dispatch(**dispatch) if isinstance(dispatch, dict) else None,
            wire=Wire(**wire) if isinstance(wire, dict) else None,
        )

    def integrity_valid(self, current_snapshot: dict[str, str]) -> bool:
        """Integrity is never persisted as a fact; it is recomputed on every check."""
        return self.integrity is not None and self.integrity.snapshot == current_snapshot

    def effective_level(self, current_snapshot: dict[str, str]) -> str:
        if not self.integrity_valid(current_snapshot):
            return PROOF_LEVEL_NONE
        if self.wire is not None and self.wire.verified:
            return PROOF_LEVEL_WIRE
        if (self.dispatch is not None and self.dispatch.verified
                and self.dispatch.subject_digest == snapshot_digest(current_snapshot)):
            return PROOF_LEVEL_DISPATCH
        return PROOF_LEVEL_INTEGRITY

    def actual_model_verified(self, current_snapshot: dict[str, str]) -> bool:
        return self.effective_level(current_snapshot) == PROOF_LEVEL_WIRE
