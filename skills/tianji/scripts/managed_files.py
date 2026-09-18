#!/usr/bin/env python3
"""Manifest-driven managed file installation.

Replaces "delete the directory and copy" with per-file management:

* only files recorded in the manifest are ever updated or removed;
* unmanaged files are never touched;
* unchanged content is skipped (no rewrite, no backup);
* a file the user edited after install is a conflict, not silent overwrite;
* writes are atomic;
* the whole transaction holds the installer lock, so two installs cannot
  interleave and leave the manifest describing a tree neither produced.

A manifest also carries a comparable version, because a content digest can
prove identity but never which side is newer.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from lock_protocol import FileLock


MANIFEST_VERSION = 1
INSTALLER_LOCK = ".tianji-install.lock"


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"manifest_version": MANIFEST_VERSION, "version": "", "entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"manifest_version": MANIFEST_VERSION, "version": "", "entries": {}}
    return data


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@dataclass
class Plan:
    write: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    conflict: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def record_written(manifest_path: Path, relative: str, data: bytes) -> None:
    """Update one manifest entry after an authorised writer changed a file.

    A managed file has one truth: the manifest entry describing the bytes that
    were last legitimately written. Any other component allowed to rewrite one
    -- the role configurator rebinding a role -- has to keep that truth current.
    Otherwise the next install compares the file against a stale entry, reads
    its own teammate as a user edit, and refuses to update the file.
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        # No manifest means nothing manages this tree; do not start claiming
        # files that an install never placed.
        return
    with FileLock(manifest_path.parent / INSTALLER_LOCK, kind="installer"):
        manifest = read_manifest(manifest_path)
        entries = dict(manifest.get("entries") or {})
        entries[relative] = {"sha256": digest_bytes(data)}
        manifest["entries"] = entries
        manifest.setdefault("manifest_version", MANIFEST_VERSION)
        _write_atomic(
            manifest_path,
            (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
             + "\n").encode("utf-8"),
        )


class ManagedInstaller:
    """Plan and commit a set of managed files against one manifest."""

    def __init__(self, root: Path, manifest_path: Path, *, version: str = ""):
        self.root = Path(root)
        self.manifest_path = Path(manifest_path)
        self.version = version
        self.manifest = read_manifest(self.manifest_path)

    def plan(self, entries: dict[str, bytes]) -> Plan:
        recorded = self.manifest.get("entries", {})
        installed_version = str(self.manifest.get("version") or "")
        plan = Plan()
        for relative, data in sorted(entries.items()):
            target = self.root / relative
            new_digest = digest_bytes(data)
            record = recorded.get(relative)
            on_disk = self._on_disk_digest(target)

            # Identity first: when what is on disk already equals what we would
            # write, there is nothing to do — even if the manifest hash is stale
            # because the file was legitimately reconfigured (e.g. a role bound
            # to a model after install).
            if on_disk == new_digest:
                plan.skip.append(relative)
                continue
            if record and on_disk is not None and on_disk != record.get("sha256"):
                # The user changed a managed file after install.
                plan.conflict.append(relative)
                continue
            if (installed_version and self.version
                    and _is_downgrade(self.version, installed_version)):
                plan.blocked.append(relative)
                continue
            plan.write.append(relative)
        return plan

    @contextmanager
    def _install_lock(self):
        """The installer lock, outermost in the fixed acquisition order."""
        with FileLock(self.root / INSTALLER_LOCK, kind="installer"):
            yield

    def commit(self, entries: dict[str, bytes], plan: Plan, *,
               force: bool = False) -> dict:
        """Write planned files atomically, then update the manifest in one step.

        The whole transaction holds the installer lock: two installs running at
        once would otherwise interleave writes and leave the manifest describing
        a tree neither of them produced.
        """
        with self._install_lock():
            return self._commit_locked(entries, plan, force=force)

    def _commit_locked(self, entries: dict[str, bytes], plan: Plan, *,
                       force: bool = False) -> dict:
        recorded = dict(self.manifest.get("entries", {}))
        written: list[str] = []
        for relative in plan.write:
            _write_atomic(self.root / relative, entries[relative])
            recorded[relative] = {"sha256": digest_bytes(entries[relative])}
            written.append(relative)
        for relative in plan.conflict:
            if not force:
                continue
            _write_atomic(self.root / relative, entries[relative])
            recorded[relative] = {"sha256": digest_bytes(entries[relative])}
            written.append(relative)
        for relative in plan.skip:
            # Keep the manifest hash current for files that are already correct,
            # so a legitimate reconfigure does not look like drift next time.
            recorded[relative] = {"sha256": digest_bytes(entries[relative])}

        blocked = list(plan.blocked) if not force else []
        for relative in plan.blocked:
            if not force:
                continue
            _write_atomic(self.root / relative, entries[relative])
            recorded[relative] = {"sha256": digest_bytes(entries[relative])}
            written.append(relative)

        next_manifest = {
            "manifest_version": MANIFEST_VERSION,
            "version": self.version,
            "entries": recorded,
        }
        # A no-op install must not touch the manifest: its timestamp is the
        # "configuration last really changed" marker that freshness checks use.
        if next_manifest != self.manifest:
            _write_atomic(
                self.manifest_path,
                (json.dumps(next_manifest, ensure_ascii=False, indent=2, sort_keys=True)
                 + "\n").encode("utf-8"),
            )
        self.manifest = next_manifest
        return {"written": written, "blocked": blocked,
                "conflict": list(plan.conflict), "skipped": list(plan.skip)}

    def remove(self, relative_paths) -> list[str]:
        """Remove only files this manifest manages and that are unmodified."""
        with self._install_lock():
            return self._remove_locked(relative_paths)

    def _remove_locked(self, relative_paths) -> list[str]:
        recorded = dict(self.manifest.get("entries", {}))
        removed: list[str] = []
        for relative in relative_paths:
            record = recorded.get(relative)
            if not record:
                continue  # never delete something we do not manage
            target = self.root / relative
            on_disk = self._on_disk_digest(target)
            if on_disk is None:
                recorded.pop(relative, None)  # already gone; stop tracking
                continue
            if on_disk != record.get("sha256"):
                continue  # user edited it: keep the file and keep tracking it
            try:
                target.unlink()
            except OSError:
                continue
            recorded.pop(relative, None)
            removed.append(relative)
        self.manifest = {
            "manifest_version": MANIFEST_VERSION,
            "version": self.manifest.get("version", ""),
            "entries": recorded,
        }
        _write_atomic(
            self.manifest_path,
            (json.dumps(self.manifest, ensure_ascii=False, indent=2, sort_keys=True)
             + "\n").encode("utf-8"),
        )
        return removed

    def managed_paths(self) -> list[str]:
        return sorted(self.manifest.get("entries", {}))

    @staticmethod
    def _on_disk_digest(path: Path) -> str | None:
        try:
            return digest_bytes(path.read_bytes())
        except OSError:
            return None


def _is_downgrade(incoming: str, installed: str) -> bool:
    """True when the incoming version is older than what is installed."""
    return _version_key(incoming) < _version_key(installed)


def _version_key(value: str) -> tuple:
    parts = []
    for chunk in str(value).replace("-", ".").split("."):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return tuple(parts)
