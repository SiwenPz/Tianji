#!/usr/bin/env python3
"""The dispatch claim token and its host marker, shared by every host.

A dispatch needs to travel from the shared dispatcher into the host, and come
back identified. The host gives us exactly one channel -- the subagent
description -- so the token rides there, in a marker that is visible but
inert to a human reading the task:

    [TJ:<22-char base64url>]

The token is **not** the invocation id: it is a one-time capability that
resolves to a pending invocation, is bound to the host/session/role that
issued it, expires, and is dead once consumed. Keeping it separate is what
stops a leaked description from naming a worker's identity directly.

The marker is anchored to the end of the description and appears at most once,
so stripping it is unambiguous and a description that merely mentions
``[TJ:...]`` mid-sentence is not mistaken for a claim.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets


TOKEN_BYTES = 16  # 128 bits
_TOKEN_CHARS = 22  # base64url without padding

MARKER_PREFIX = "[TJ:"
MARKER_SUFFIX = "]"

# Anchored at the end, exactly one marker, in the one encoding this protocol
# mints. A trailing space or newline is tolerated because hosts add both.
MARKER_RE = re.compile(r"\[TJ:([A-Za-z0-9_-]{22})\]\s*$")

# How long a claim token lives. It is anchored at ``open``, but the event it
# has to match -- the host's own start event -- arrives whenever the host
# decides to send it, and that is not ours to schedule. Measured on a real host:
# dispatches 14s apart claimed fine, one 812s apart could not claim at all. The
# three guards that matter (session binding, one-time use, live task) are
# untouched by the length, and an expired token still fails closed -- so the
# default is set where a dispatch is not lost to a slow host rather than where
# it feels tidy.
DEFAULT_TTL_SECONDS = 1800.0


def new_token() -> str:
    """Mint a fresh 128-bit claim token."""
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")


def is_well_formed(token: object) -> bool:
    """True when the value is exactly a token this protocol could mint."""
    return (isinstance(token, str) and len(token) == _TOKEN_CHARS
            and MARKER_RE.match(MARKER_PREFIX + token + MARKER_SUFFIX) is not None)


def token_digest(token: str) -> str:
    """The stable key a token is indexed under.

    Only the digest is stored, so a registry leak never yields a usable token.
    """
    if not is_well_formed(token):
        raise ValueError("claim token is not well formed")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def format_marker(token: str) -> str:
    if not is_well_formed(token):
        raise ValueError("claim token is not well formed")
    return f"{MARKER_PREFIX}{token}{MARKER_SUFFIX}"


def extract_marker(text: object) -> str | None:
    """Return the claim token carried by a description, if any."""
    if not isinstance(text, str):
        return None
    match = MARKER_RE.search(text)
    return match.group(1) if match else None


def strip_marker(text: object) -> str:
    """Return the description with its claim marker removed.

    The marker must never reach the ledger: it is a capability, and a ledger
    is exactly the kind of place that gets read, copied and shared.
    """
    if not isinstance(text, str):
        return "" if text is None else str(text)
    return MARKER_RE.sub("", text).rstrip()


def attach_marker(description: object, token: str) -> str:
    """Append the marker to a description, replacing any marker already there."""
    return f"{strip_marker(description)} {format_marker(token)}".strip()


# Payload fields that may carry an EventKey claim. Two spellings because hosts
# differ, and a claim that cannot be parsed is simply absent.
RUN_FIELDS = ("run_id", "runId")
TASK_FIELDS = ("task_id", "taskId")


def payload_event_key(payload: object):
    """The EventKey a payload *claims*, or None.

    A claim is never identity by itself: the registry has to confirm that it
    issued this run and task before the claim may be used.
    """
    if not isinstance(payload, dict):
        return None
    from ledger_schema import EventKey, require_canonical_uuid

    run_id = _first_text(payload, RUN_FIELDS)
    task_id = _first_text(payload, TASK_FIELDS)
    attempt = payload.get("attempt")
    if not run_id or not task_id:
        return None
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return None
    try:
        run_id = require_canonical_uuid("run_id", run_id)
    except ValueError:
        return None
    return EventKey(run_id, task_id, attempt)


def _first_text(payload: dict, fields) -> str:
    for name in fields:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
