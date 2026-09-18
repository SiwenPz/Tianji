#!/usr/bin/env python3
"""Host-neutral tri-state observation contract.

A probe (model menu, host version, role bindings, ...) can end in three
genuinely different ways, and collapsing them into a boolean is what makes a
transient failure look like a configuration change:

* ``available(value)``    -- observed successfully; the value is real.
* ``confirmed_empty()``   -- observed successfully, and the answer is empty.
* ``unavailable(reason)`` -- the observation could not be taken at all.

Only the first two may take part in a digest comparison. ``unavailable`` must
never be read as an empty value, must never revoke an existing proof, and must
never be reported as drift: the honest verdict is
:data:`conclusions.CHECK_UNAVAILABLE`.

The three states are mutually exclusive and jointly exhaustive, and the
invariants are enforced here rather than left to callers:

* ``available`` carries a value and no reason;
* ``confirmed_empty`` carries neither a value nor a reason;
* ``unavailable`` carries a reason and no value.

In particular ``available(None)`` and ``available([])`` are rejected: an
observation with no content is exactly what ``confirmed_empty`` means, and
allowing it would let an empty probe masquerade as a successful reading.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import conclusions


AVAILABLE = "available"
CONFIRMED_EMPTY = "confirmed_empty"
UNAVAILABLE = "unavailable"

OBSERVATION_STATES = frozenset({AVAILABLE, CONFIRMED_EMPTY, UNAVAILABLE})

# Only these states carry a trustworthy value for fingerprint comparison.
OBSERVED_STATES = frozenset({AVAILABLE, CONFIRMED_EMPTY})


def _is_empty(value: Any) -> bool:
    """True for a container or string with no content.

    Scalar readings (0, False) are legitimate observations and stay non-empty;
    only "we looked and found nothing" is what confirmed_empty means.
    """
    if isinstance(value, (str, bytes)):
        return len(value) == 0
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value) == 0
    return False


@dataclass(frozen=True)
class Observation:
    state: str
    value: Any = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.state not in OBSERVATION_STATES:
            raise ValueError(
                f"observation state must be one of {sorted(OBSERVATION_STATES)}"
            )
        if self.state == AVAILABLE:
            if self.value is None or _is_empty(self.value):
                raise ValueError(
                    "an available observation must carry a non-empty value; "
                    "use confirmed_empty() when the answer is empty"
                )
            if self.reason:
                raise ValueError("an available observation carries no reason")
            return
        if self.state == CONFIRMED_EMPTY:
            if self.value is not None:
                raise ValueError("a confirmed_empty observation carries no value")
            if self.reason:
                raise ValueError("a confirmed_empty observation carries no reason")
            return
        # UNAVAILABLE
        if self.value is not None:
            raise ValueError("an unavailable observation carries no value")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("an unavailable observation must carry a reason")

    @classmethod
    def available(cls, value: Any) -> "Observation":
        return cls(AVAILABLE, value, "")

    @classmethod
    def confirmed_empty(cls) -> "Observation":
        return cls(CONFIRMED_EMPTY, None, "")

    @classmethod
    def unavailable(cls, reason: str) -> "Observation":
        return cls(UNAVAILABLE, None, reason)

    @property
    def was_observed(self) -> bool:
        """True when a comparison against this observation is meaningful."""
        return self.state in OBSERVED_STATES

    @property
    def is_empty(self) -> bool:
        """True only for a probe that ran and genuinely found nothing."""
        return self.state == CONFIRMED_EMPTY

    def error(self) -> str:
        return self.reason if self.state == UNAVAILABLE else ""

    def conclusion(self) -> str | None:
        """The shared verdict this observation forces, if any."""
        if self.state == UNAVAILABLE:
            return conclusions.CHECK_UNAVAILABLE
        return None


def conclusion_for(observation: Observation) -> str | None:
    """Map an observation onto the shared conclusion vocabulary.

    Returns ``CHECK_UNAVAILABLE`` when the probe failed, and ``None`` when the
    observation is usable and the caller should keep evaluating other checks —
    so an unavailable probe can never be mistaken for drift or for readiness.
    """
    if not isinstance(observation, Observation):
        raise ValueError("expected an Observation")
    return observation.conclusion()
