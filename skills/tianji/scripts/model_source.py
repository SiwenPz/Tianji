#!/usr/bin/env python3
"""Host-neutral model-source contract.

Whether Tianji needs to install and verify a local pool is a question about
*capability and user choice*, not about which host happens to be running: a
host whose native model menu is usable needs no pool, and a user who chose an
external provider needs one. Host names must never decide this.

Selection and health are deliberately separate axes:

* :func:`resolve` decides **which** source supplies models (host menu, or an
  external provider the user chose). It never inspects liveness.
* :func:`health` reports whether a chosen provider is **alive**, and takes the
  same tri-state observation as every other probe. It never decides which
  source was chosen.

Keeping them apart is what stops a dead pool from being reported as "no model
source", and a transient probe failure from being reported as a dead pool.
"""
from __future__ import annotations

from dataclasses import dataclass

import conclusions
import observation
from observation import Observation


HOST_MENU = "host_menu"
EXTERNAL_POOL = "external_pool"

MODEL_SOURCES = frozenset({HOST_MENU, EXTERNAL_POOL})


@dataclass(frozen=True)
class ModelSource:
    kind: str
    menu: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in MODEL_SOURCES:
            raise ValueError(f"model source must be one of {sorted(MODEL_SOURCES)}")
        # A host-menu source with no models is a contradiction: it claims to
        # supply roles from a menu it does not have.
        if self.kind == HOST_MENU and not self.menu:
            raise ValueError("a host-menu source must carry at least one model")
        # An external provider is the pool itself, so it carries no host menu.
        if self.kind == EXTERNAL_POOL and self.menu:
            raise ValueError("an external-pool source carries no host menu")

    @property
    def installs_pool(self) -> bool:
        """Only an external source is reason to install or check a pool."""
        return self.kind == EXTERNAL_POOL


def resolve(
    menu: Observation, *, external_chosen: bool = False,
) -> tuple[ModelSource | None, str]:
    """Decide which model source supplies roles.

    Returns ``(source, conclusion)``: exactly one of the two is meaningful.

    * a probe that could not run yields ``CHECK_UNAVAILABLE`` -- never an empty
      menu, and never a dead pool;
    * a usable host menu is the source, whatever external providers exist;
    * an empty menu with an explicitly chosen external provider is that
      provider (whether or not it is currently alive -- see :func:`health`);
    * an empty menu with no choice made asks the user, through the shared
      ``NEED_MODEL_SOURCE`` verdict.
    """
    if not isinstance(menu, Observation):
        raise ValueError("expected an Observation")
    verdict = menu.conclusion()
    if verdict is not None:
        return None, verdict
    if menu.state == observation.AVAILABLE:
        return ModelSource(HOST_MENU, tuple(menu.value)), ""
    if external_chosen:
        return ModelSource(EXTERNAL_POOL), ""
    return None, conclusions.NEED_MODEL_SOURCE


def health(alive: Observation) -> str:
    """Liveness of a provider that was already chosen, as a shared verdict.

    ``alive`` is the same tri-state as any other probe, which is what makes
    "we could not check" unrepresentable as "nothing is alive":

    * ``available([...])``    -- checked, these are up;
    * ``confirmed_empty()``   -- checked, none is up;
    * ``unavailable(reason)`` -- could not check, so the verdict is
      ``CHECK_UNAVAILABLE`` rather than ``DEGRADED``.
    """
    if not isinstance(alive, Observation):
        raise ValueError("expected an Observation")
    if alive.state == observation.UNAVAILABLE:
        return conclusions.CHECK_UNAVAILABLE
    if alive.state == observation.CONFIRMED_EMPTY:
        return conclusions.DEGRADED
    return ""
