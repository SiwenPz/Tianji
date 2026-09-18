#!/usr/bin/env python3
"""Remember the last observation that actually proved something.

A proof says "this configuration was proven"; when it later stops matching, the
useful question is *what changed*. Comparing two aggregate digests can only
answer "something", so the last successful observation is kept per subject
(role bindings, menu, adapter, host runtime) and compared subject by subject.

The journal is written only from a verified observation: recording a failed
probe would make the next comparison name the failure as the change. It is
advisory display data -- it never decides a verdict, and losing it costs a
better explanation, not correctness.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import route_proof


JOURNAL_NAME = "last-observation.json"
# 2: earlier journals could carry a menu value that was never observed (the probe
# failed, and the boolean interface handed back an empty list). Those entries are
# not comparable, so they are ignored rather than explained against.
JOURNAL_VERSION = 2

SUBJECTS = ("role_bindings", "menu", "adapter_digest", "host_runtime_version")

_LABELS = {
    "role_bindings": "角色绑定",
    "menu": "模型菜单",
    "adapter_digest": "适配层",
    "host_runtime_version": "宿主版本",
}


def journal_path(workspace: str | os.PathLike[str]) -> Path:
    return Path(workspace) / ".tianji" / JOURNAL_NAME


def remember(workspace, *, role_bindings: Any, menu: Any,
             adapter_digest: str, host_runtime_version: str) -> None:
    """Record the subjects as they were when a proof was last established."""
    payload = {
        "version": JOURNAL_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "role_bindings": role_bindings,
        "menu": menu,
        "adapter_digest": adapter_digest,
        "host_runtime_version": host_runtime_version,
    }
    path = journal_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        # Advisory only: failing to keep the explanation must never fail a check.
        pass


def recall(workspace) -> dict | None:
    """The last successful observation, or None when there is no usable one."""
    try:
        data = json.loads(journal_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != JOURNAL_VERSION:
        return None
    return data if all(name in data for name in SUBJECTS) else None


def _binding_changes(previous: Any, current: Any) -> list[str]:
    """Name the roles whose binding changed, was added or disappeared."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return []
    lines: list[str] = []
    for role in sorted(set(previous) | set(current)):
        before, after = previous.get(role), current.get(role)
        if before == after:
            continue
        if before is None:
            lines.append(f"{role} 新增绑定 {after}")
        elif after is None:
            lines.append(f"{role} 绑定消失（原 {before}）")
        else:
            lines.append(f"{role}: {before} → {after}")
    return lines


def explain_drift(previous: dict | None, *, role_bindings: Any, menu: Any,
                  adapter_digest: str, host_runtime_version: str) -> list[str]:
    """Name what changed since the last successful observation.

    Returns an empty list when everything matches, or when there is nothing to
    compare against -- "no explanation available" must not be dressed up as
    "nothing changed".
    """
    if not previous:
        return []

    current = {
        "role_bindings": role_bindings,
        "menu": menu,
        "adapter_digest": adapter_digest,
        "host_runtime_version": host_runtime_version,
    }
    lines: list[str] = []
    for role in _binding_changes(previous.get("role_bindings"), role_bindings):
        lines.append(f"{_LABELS['role_bindings']}: {role}")

    if previous.get("menu") != menu:
        before = previous.get("menu")
        lines.append(
            f"{_LABELS['menu']}: {_describe_menu(before)} → {_describe_menu(menu)}"
        )
    for name in ("adapter_digest", "host_runtime_version"):
        if previous.get(name) != current[name]:
            lines.append(
                f"{_LABELS[name]}: {previous.get(name) or '(空)'} → {current[name] or '(空)'}"
            )
    return lines


def _describe_menu(menu: Any) -> str:
    if menu is None:
        return "上次未读到"
    if isinstance(menu, (list, tuple)):
        return f"{len(menu)} 项"
    return str(menu)


def _menu_subject(detector) -> Any:
    """The menu as a *subject*, or None when it was never observed.

    A proof can be established while the menu probe fails -- the proof is about
    the dispatch, not the menu -- and then the recorded subject must say "not
    observed", never "empty". The tri-state interface is the only thing that can
    tell the two apart, so it is preferred whenever the detector offers it.
    """
    observer = getattr(detector, "menu_observation", None)
    if callable(observer):
        try:
            observation = observer()
        except Exception:
            return None
        state = getattr(observation, "state", "")
        if state == "unavailable":
            return None
        if state == "confirmed_empty":
            return []
        return getattr(observation, "value", None)
    try:
        return detector.menu_models()
    except Exception:
        return None


def snapshot_subjects(detector) -> dict[str, Any]:
    """Collect the comparable subjects from a detector, tolerating absence."""
    try:
        role_bindings = detector.role_bindings()
    except Exception:
        role_bindings = None
    menu = _menu_subject(detector)
    snapshot = detector.current_snapshot()
    return {
        "role_bindings": role_bindings,
        "menu": menu,
        "adapter_digest": snapshot.get("adapter_digest", ""),
        "host_runtime_version": snapshot.get("host_runtime_version", ""),
    }


def digest_of(subjects: dict[str, Any]) -> str:
    """The aggregate digest of these subjects, matching integrity_snapshot()."""
    return route_proof.snapshot_digest({
        "role_bindings_digest": route_proof.digest(subjects["role_bindings"]),
        "menu_snapshot_digest": route_proof.digest(subjects["menu"]),
        "adapter_digest": subjects["adapter_digest"],
        "host_runtime_version": subjects["host_runtime_version"],
    })
