#!/usr/bin/env python3
"""The single, shared vocabulary of Tianji conclusion verdicts.

Every host reports through these names, and only the shared conclusion machine
decides which one applies. An adapter never returns a verdict of its own, and
never maps "we could not observe right now" onto a configuration verdict.

``CHECK_UNAVAILABLE`` exists precisely so that an observation that could not be
taken stays distinct from both "the configuration drifted" and "nothing is
installed": it neither revokes an existing proof nor claims readiness. The
verdict is frozen here; wiring an observation into the conclusion machine is
the observability stage's job, so no second priority rule lives in the machine.

``NEED_MODEL_SOURCE`` is the capability-named replacement for ``NEED_POOL``:
the user must choose a model source (the host's own menu, or an external
provider). ``NEED_POOL`` is kept only while the current conclusion machine
still emits it, and is retired in the installer / model-source stage.
"""
from __future__ import annotations


READY = "READY"
NEED_INSTALL = "NEED_INSTALL"
NEED_HOST_ADAPTER = "NEED_HOST_ADAPTER"
NEED_MODEL_SOURCE = "NEED_MODEL_SOURCE"
# Legacy name, superseded by NEED_MODEL_SOURCE; still emitted by the current
# conclusion machine until the installer stage renames it.
NEED_POOL = "NEED_POOL"
NEED_ROLE_CONFIG = "NEED_ROLE_CONFIG"
NEED_ROUTE_PROOF = "NEED_ROUTE_PROOF"
CHECK_UNAVAILABLE = "CHECK_UNAVAILABLE"
DEGRADED = "DEGRADED"

ALL = frozenset({
    READY,
    NEED_INSTALL,
    NEED_HOST_ADAPTER,
    NEED_MODEL_SOURCE,
    NEED_POOL,
    NEED_ROLE_CONFIG,
    NEED_ROUTE_PROOF,
    CHECK_UNAVAILABLE,
    DEGRADED,
})

# Every spelling that means "choose a model source", so a reader can accept
# either while the rename is in flight.
MODEL_SOURCE_VERDICTS = frozenset({NEED_MODEL_SOURCE, NEED_POOL})

LEGACY = frozenset({NEED_POOL})

# Verdicts that permit work to start. Anything else keeps Tianji blocked.
ACTIONABLE = frozenset({READY})

# Findings that mean "we could not observe", and therefore neither permit work
# nor revoke an existing proof.
INCONCLUSIVE = frozenset({CHECK_UNAVAILABLE})

# The user-facing advice for each verdict, kept in one place so no host invents
# its own wording -- a DEGRADED report must read the same everywhere.
SUGGESTIONS = {
    READY: "直接开工",
    NEED_INSTALL: "运行 install.py install --host <当前宿主>",
    NEED_HOST_ADAPTER: "宿主模型接入未完成，停止初始化；不要收 key 或默认绑定角色",
    NEED_MODEL_SOURCE: "先确定模型来源（宿主自带菜单或外部渠道），再绑定角色",
    NEED_POOL: "先确定模型来源（宿主自带菜单或号池），再绑定角色",
    NEED_ROLE_CONFIG: "模型来源验证后，按 BOOTSTRAP.md 引导分配角色",
    NEED_ROUTE_PROOF: "按宿主引导派一次角色（只读自检），完成路由验证",
    CHECK_UNAVAILABLE: "观测不可用：无法确认当前状态，保持上次结论，不宣告就绪",
    DEGRADED: "报告用户模型池全死",
}


def suggestion(verdict: str) -> str:
    """The shared advice for a verdict; empty when the verdict is unknown."""
    return SUGGESTIONS.get(verdict, "")


# Advice for a check that crashed before a verdict could even be computed. It
# is deliberately not the DEGRADED wording: a crash is not a dead pool.
CRASH_SUGGESTION = "手动检查天机环境"

# Process exit codes for the doctor. A verdict has to be able to fail a
# caller's check, so only an actionable verdict exits zero. "No verdict was
# produced at all" (unknown host, broken adapter, crash) is a third code, so a
# caller can tell "not ready" apart from "the check never ran" -- the two used
# to be indistinguishable, because everything exited zero.
EXIT_ACTIONABLE = 0
EXIT_NOT_READY = 1
EXIT_UNUSABLE = 2


def exit_code(verdict: str) -> int:
    """The process exit code for a verdict.

    Only an actionable verdict exits zero. A known verdict that blocks work
    exits 1. Anything else -- an inconclusive observation, an unknown spelling,
    or a check that never reached a verdict -- exits 2, because no valid
    conclusion was reached and a caller must not read "we could not tell" as
    "we checked and it is not ready".
    """
    if verdict in ACTIONABLE:
        return EXIT_ACTIONABLE
    if verdict in INCONCLUSIVE:
        return EXIT_UNUSABLE
    if verdict in ALL:
        return EXIT_NOT_READY
    return EXIT_UNUSABLE
