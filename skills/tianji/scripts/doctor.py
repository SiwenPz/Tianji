#!/usr/bin/env python3
"""The shared conclusion machine: facts in, one verdict out.

A host detector supplies observations; the decision rules live here and nowhere
else. The morning check probes a host with a detector and calls in, and the
installer's ``status`` command gathers the same facts and calls the very same
function -- so "ready" is decided in exactly one place, and a status report can
no longer disagree with the doctor.

``collect_facts`` deliberately stops at facts that are local to the machine.
The pool liveness probe is a network call, so it is the morning check's step,
and a caller that must stay offline states that explicitly instead of passing
an empty result set. An unprobed pool is therefore never read as a dead one --
nor as a live one.
"""
from __future__ import annotations

from dataclasses import dataclass

import conclusions as _conclusions
import observation as _observation


# Pool states that count as alive. Shared by the policy below and by the probes
# in the morning check, so "alive" cannot mean two different things.
POOL_ALIVE = frozenset({"活", "活-需认证"})

# What we know about the model pool's liveness. A bare list used to stand for
# both "this host has no pool to probe" and "we have not looked yet", and the
# second was silently read as the first -- so a host whose readiness still
# needed a liveness proof could be reported ready without one. The state names
# which of the two it is, and only a probe that actually ran can clear the gate.
POOL_NOT_REQUIRED = "not_required"
POOL_NOT_PROBED = "not_probed"
POOL_PROBED = "probed"
POOL_PROBE_FAILED = "probe_failed"

POOL_STATES = frozenset({
    POOL_NOT_REQUIRED, POOL_NOT_PROBED, POOL_PROBED, POOL_PROBE_FAILED,
})


@dataclass(frozen=True)
class PoolEvidence:
    """The pool-liveness evidence a caller hands to the machine.

    ``results`` holds one ``(alias, status)`` pair per menu model and exists
    only for :data:`POOL_PROBED`. Every other state carries a reason instead,
    so "we did not look" is unrepresentable as "we looked and found nothing".
    """

    state: str
    results: tuple = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.state not in POOL_STATES:
            raise ValueError(f"pool evidence state must be one of {sorted(POOL_STATES)}")
        if self.state == POOL_PROBED:
            if self.reason:
                raise ValueError("probed pool evidence carries no reason")
            return
        if self.results:
            raise ValueError("only probed pool evidence carries results")
        if self.state in (POOL_NOT_PROBED, POOL_PROBE_FAILED) and not self.reason.strip():
            raise ValueError(f"{self.state} pool evidence must carry a reason")

    @classmethod
    def not_required(cls) -> "PoolEvidence":
        """This host's model source needs no pool liveness proof."""
        return cls(POOL_NOT_REQUIRED)

    @classmethod
    def not_probed(cls, reason: str) -> "PoolEvidence":
        """The pool is required, but this caller deliberately stayed offline."""
        return cls(POOL_NOT_PROBED, (), reason)

    @classmethod
    def probed(cls, results) -> "PoolEvidence":
        """The probe ran; one entry per menu model, possibly none."""
        return cls(POOL_PROBED, tuple(results))

    @classmethod
    def failed(cls, reason: str) -> "PoolEvidence":
        """The probe could not run at all."""
        return cls(POOL_PROBE_FAILED, (), reason)


def host_checks(*, hooks, roles, role_bindings, status_line, menu, routing,
                probe, menu_observation=None):
    """Assemble the canonical facts the conclusion machine consumes.

    This is the one place the shape is spelled out, so a caller cannot
    half-fill a checks dict and silently change what the machine sees.
    """
    return {
        "hooks": hooks,
        "roles": roles,
        "role_bindings": role_bindings,
        "status_line": status_line,
        "menu": menu,
        "routing": routing,
        "probe": probe,
        "menu_observation": menu_observation,
    }


def observe_menu(detector):
    """Ask the detector for a tri-state menu reading.

    Returns ``None`` when the host has no tri-state interface, so untouched
    hosts keep the previous boolean behaviour. A detector that raises is
    reported as unavailable rather than as an empty menu: the probe failed, and
    that is not the same as the answer being empty.
    """
    observer = getattr(detector, "menu_observation", None)
    if not callable(observer):
        return None
    try:
        return observer()
    except Exception as exc:  # noqa: BLE001 - any probe failure is unavailability
        return _observation.Observation.unavailable(f"菜单观测异常: {exc}")


def _status_line_fact(detector):
    """The status-line fact, in the detector's own words.

    A host's configuration format is host knowledge: the detector owns it and
    answers with a normalized ``(ok, detail)``. The machine must not learn how
    to read any one host's configuration file -- that is how a host format ends
    up in the shared conclusion machine.
    """
    return detector.status_line_ok()


def collect_facts(detector):
    """Gather the machine's local facts through the detector interface.

    No network and nothing but the detector's own readings. The tri-state menu
    observation is kept alongside the boolean so that "we could not look" stays
    distinct from "there is nothing there".
    """
    menu_observation = observe_menu(detector)
    menu = detector.menu_models()
    if detector.requires_model_menu():
        menu_fact = (bool(menu), _menu_detail(menu, menu_observation))
    else:
        menu_fact = (True, "宿主自带模型菜单，不需要外部 provider 接入")
    return host_checks(
        hooks=detector.hooks_ok(),
        roles=detector.roles_ok(),
        role_bindings=detector.role_bindings_ok(),
        status_line=_status_line_fact(detector),
        menu=menu_fact,
        routing=detector.routing_supported(),
        probe=detector.probe_ok(),
        menu_observation=menu_observation,
    )


def _menu_detail(menu, menu_observation):
    """Say *which* empty it is.

    An empty reading has two very different causes: the menu is genuinely not
    configured, or the probe could not read it (a host mid-restart, a CLI that
    vanished for a moment). The conclusion machine already keeps the two apart;
    the human-readable line must too, or a transient outage sends the reader off
    to provision a menu that was never gone. Measured 2026-09-16: the line said
    "模型来源/菜单未配置" while the host was restarting, and the next run of the
    same check said "已配置 70 个模型".
    """
    if menu:
        return f"已配置 {len(menu)} 个模型"
    reason = menu_observation.error() if menu_observation is not None else ""
    if reason:
        return f"模型菜单读不到（{reason}）"
    return "模型来源/菜单未配置"


def determine_conclusion(checks, pool):
    """根据检查结果返回 (conclusion, suggestion)。

    结论名取自共享词表 conclusions；宿主适配层只提供事实，不自行判定就绪。
    ``pool`` 是 PoolEvidence：未探测或探测失败都不是"没有死池"，不得据此放行。
    """
    hooks_ok = checks["hooks"][0]
    roles_ok = checks["roles"][0]
    bindings_ok = checks["role_bindings"][0]
    sl_ok = checks["status_line"][0]
    menu_ok = checks["menu"][0]

    # hooks 或 status_line MISSING → NEED_INSTALL
    if not hooks_ok or not roles_ok or not sl_ok:
        return _conclusions.NEED_INSTALL, _conclusions.suggestion("NEED_INSTALL")

    if not checks.get("routing", (True, ""))[0]:
        return _conclusions.NEED_HOST_ADAPTER, _conclusions.suggestion("NEED_HOST_ADAPTER")

    # A menu probe that could not run says nothing about the configuration, so
    # it must not be read as "drift" and must not be reported as ready. It sits
    # after the install and adapter gates so that a real missing install is
    # still reported instead of being masked by an unavailable probe, and before
    # the empty-menu branch, which cannot be evaluated without a reading.
    conclusion = _observation.conclusion_for(checks["menu_observation"]) if checks.get("menu_observation") else None
    if conclusion:
        return conclusion, _conclusions.suggestion(conclusion)

    # 菜单 MISSING → NEED_POOL
    if not menu_ok:
        return _conclusions.NEED_POOL, _conclusions.suggestion("NEED_POOL")

    if not bindings_ok:
        return _conclusions.NEED_ROLE_CONFIG, _conclusions.suggestion("NEED_ROLE_CONFIG")

    if not checks.get("probe", (True, ""))[0]:
        return _conclusions.NEED_ROUTE_PROOF, _conclusions.suggestion("NEED_ROUTE_PROOF")

    # The pool gate sits last: a missing install, an unbound role or an absent
    # proof is a more actionable answer than "we did not look", and only this
    # point is reached with every other check satisfied. An unprobed or failed
    # probe cannot clear it -- saying "no dead pool" requires having looked.
    if pool.state in (POOL_NOT_PROBED, POOL_PROBE_FAILED):
        return (
            _conclusions.CHECK_UNAVAILABLE,
            _conclusions.suggestion(_conclusions.CHECK_UNAVAILABLE),
        )

    # 菜单有但池全死 → DEGRADED
    if pool.state == POOL_PROBED:
        alive = [status for _, status in pool.results if status in POOL_ALIVE]
        if not alive:
            return _conclusions.DEGRADED, _conclusions.suggestion(_conclusions.DEGRADED)

    # 其余 → READY
    return _conclusions.READY, _conclusions.suggestion(_conclusions.READY)
