#!/usr/bin/env python3
"""
board.py - TJ-v2 Completion Status Board
Reads subagent activity events from state.jsonl and renders a readable status board.
Uses only Python standard library.
"""

import argparse
import glob
import json
import os
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timezone

import ledger_reader

# ---------------------------------------------------------------------------
# Wire-archive lookup root (kimi-code sessions).
# Override:  env var TJ_WIRE_ROOT, or --kimi-home CLI flag
# ---------------------------------------------------------------------------
_WIRE_ROOT = os.environ.get("TJ_WIRE_ROOT",
                            os.path.expanduser("~/.kimi-code/sessions"))
# kimi-code config.toml,用于读号池菜单 ([secondary_model.models])
_KIMI_CONFIG = os.environ.get("TJ_KIMI_CONFIG",
                              os.path.expanduser("~/.kimi-code/config.toml"))
_IDENTITY_LINES = 500          # lines of wire.jsonl scanned for identity hints
_MODEL_SCAN_LINES = 200        # lines scanned for the first modelAlias field
_WIRE_TAIL_BYTES = 8192        # bytes scanned for the latest wire event time
_RESUME_TAIL_CANDIDATES = 8    # newest archives considered for resume fallback

# Chinese-identity keywords mapped to agent-type name strings (agent-1 等等)
_IDENTITY_HINTS = {
    "executor": ["\u6267\u884c\u5de5", "\u6267\u884c\u5458", "\u5b9e\u65bd\u5de5"],
    "reviewer": ["\u5ba1\u6838\u5458", "\u5ba1\u6838\u5de5", "\u5ba1\u6838"],
    "planner":  ["\u89c4\u5212\u5e08", "\u89c4\u5212\u5458"],
    "writer":   ["\u5199\u4f5c", "\u6587\u7a3f", "\u7f16\u5199"],
    "tianji-worker":   ["天机任务书|", "天机任务书｜"],
    "tianji-verifier": ["天机验收任务书|", "天机验收任务书｜", "天机验收员"],
    "tianji-referee":  ["天机裁判任务书|", "天机裁判任务书｜"],
}
# 注:天机 v2 角色用任务书前缀做身份锚点(互斥,最可靠);
# 勿加"审核员"这类会同时出现在裁判/验收 prompt 里的泛词,会抢错 wire。

# ===== CLI =================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="TJ-v2 Status Board: render subagent activity from state.jsonl."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path to state.jsonl (optional, default: <cwd>/.tianji/state.jsonl)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Project root, looks for .tianji/state.jsonl inside it",
    )
    parser.add_argument(
        "--kimi-home",
        default=None,
        help="Override path to kimi-code sessions root "
             "(default: ~/.kimi-code/sessions)",
    )
    return parser.parse_args()


def resolve_path(args):
    """Return the path to state.jsonl."""
    if args.cwd:
        return os.path.join(args.cwd, ".tianji", "state.jsonl")
    if args.path:
        return args.path
    return os.path.join(os.getcwd(), ".tianji", "state.jsonl")


# ===== Duration helpers ===================================================

def parse_iso_duration(start_iso, end_iso):
    """Compute duration between two ISO8601 timestamps. Returns 'Xh Xm Xs'."""
    if start_iso == "--" or end_iso == "--":
        return "N/A"
    fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        t_start = datetime.strptime(start_iso[:19], fmt)
        t_end   = datetime.strptime(end_iso[:19], fmt)
        delta = t_end - t_start
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "N/A"
        h, rem = divmod(total_seconds, 3600)
        m, s   = divmod(rem, 60)
        parts = []
        if h: parts.append(f"{h}h")
        if m: parts.append(f"{m}m")
        if s or not parts: parts.append(f"{s}s")
        return " ".join(parts)
    except (ValueError, TypeError):
        return "N/A"

# ===== State reader =======================================================

TRACKED_EVENTS = ("subagent_start", "subagent_stop")

# Classification lives in the shared reader so the board, the proof collector
# and any other consumer agree on what is authoritative. This module only
# decides how to *display* what the reader classified.


def decode_ledger_event(record):
    """Normalize one authoritative envelope; None when it is not displayable."""
    if record.get("event") not in TRACKED_EVENTS:
        return None
    return ledger_reader.canonical_event(record)


def decode_legacy_event(record):
    """Normalize a historical record for display; None when it is not tracked."""
    if record.get("event") not in TRACKED_EVENTS:
        return None
    if "ts" not in record or "agent" not in record:
        return None
    return ledger_reader.legacy_event(record)


def read_state_events(filepath):
    """Read state.jsonl. Returns (events, skip_count).

    Authoritative records and historical ones are both returned so the board can
    show them, but only the authoritative ones carry an EventKey. Quarantined
    lines -- invalid v2, unknown versions, unreadable rows -- are counted and
    dropped, so they can never render as running work.

    The reader never branches on host, and never rewrites the file.
    """
    read = ledger_reader.read_ledger(filepath)
    events = []
    for record in read.canonical:
        decoded = decode_ledger_event(record)
        if decoded is not None:
            events.append(decoded)
    for record in read.legacy:
        decoded = decode_legacy_event(record)
        if decoded is not None:
            events.append(decoded)
    return events, read.diagnostic_count


def quarantine_report(filepath):
    """The shared reader's summary, for callers that want to show why lines
    were set aside."""
    return ledger_reader.read_ledger(filepath).summary()

# ===== Instance builder ===================================================

def _short(scaled):
    text = f"{scaled:,.1f}"
    return text[:-2] if text.endswith(".0") else text


def format_tokens(value):
    """Short human form, so a cost column stays readable: 798, 8K, 108.3K, 4.9M."""
    if value < 1000:
        return f"{value:,}"
    if value < 1_000_000:
        return f"{_short(value / 1000)}K"
    return f"{_short(value / 1_000_000)}M"


def short_model(model):
    """A model name without its vendor prefix.

    The prefix repeats on every row and rarely adds anything the reader needs;
    when one name does belong to two vendors, the summary keeps both whole
    rather than printing the same label twice with different numbers.
    """
    return str(model).rsplit("/", 1)[-1]


def record_tokens(event):
    """Tokens the host attributed to one subagent; None when it reported none."""
    value = (event.get("detail") or {}).get("tokensUsed")
    return value if isinstance(value, int) and value > 0 else None


def record_model(event):
    """The model the dispatch record names, or "" when it names none.

    This is what the host recorded when it routed the call, so it is available
    on every host — unlike the wire archives, which only one of them keeps.
    """
    value = (event.get("detail") or {}).get("model")
    return value if isinstance(value, str) else ""


def _pair(starts, stops):
    """Pair starts with stops in chronological order; leftovers keep their role."""
    instances = []
    for index, start in enumerate(starts):
        instance = {
            "agent": start["agent"],
            "session_id": start.get("session_id", ""),
            "session_idx": start.get("_sidx", -1),
            "start_ts": start["ts"],
            "end_ts": "--",
            "status": "running",
            "tokens": None,
            "recorded_model": record_model(start),
        }
        for key in ("event_key", "correlation_id"):
            if key in start:
                instance[key] = start[key]
        if index < len(stops):
            instance["end_ts"] = stops[index]["ts"]
            instance["status"] = "done"
            instance["tokens"] = record_tokens(stops[index])
            instance["recorded_model"] = record_model(stops[index]) or instance["recorded_model"]
        instances.append(instance)
    return instances


def build_instances(events):
    """
    Group events into task instances.

    Identity order: the standard EventKey when the envelope carries one, else
    the legacy per-agent pairing. *session_idx* preserves the 0-based order of
    start events within a session for downstream wire-archive binding.

    Returns list of dict: agent, session_id, session_idx, start_ts, end_ts,
    status (status = "done", "running", "orphan_stop")
    """
    keyed = [event for event in events if event.get("event_key")]
    legacy = [event for event in events if not event.get("event_key")]

    # ── Phase A: assign session-local index to every tracked start ────────
    starts_by_sid = defaultdict(list)
    for ev in events:
        if ev["event"] == "subagent_start":
            starts_by_sid[ev.get("session_id", "")].append(ev)

    for sid, starts in starts_by_sid.items():
        for rank, ev in enumerate(sorted(starts, key=lambda e: e["ts"])):
            ev["_sidx"] = rank

    instances = []

    # ── Phase B1: standard envelopes are grouped by EventKey ─────────────
    by_key = defaultdict(list)
    for ev in keyed:
        by_key[ev["event_key"]].append(ev)
    for key in sorted(by_key, key=lambda item: [str(part) for part in item]):
        evts = sorted(by_key[key], key=lambda x: x["ts"])
        starts = [e for e in evts if e["event"] == "subagent_start"]
        stops = [e for e in evts if e["event"] == "subagent_stop"]
        instances.extend(_pair(starts, stops))
        for orphan in stops[len(starts):]:
            instances.append({
                "agent": orphan["agent"],
                "session_id": orphan.get("session_id", ""),
                "session_idx": -1,
                "start_ts": "--",
                "end_ts": orphan["ts"],
                "status": "orphan_stop",
                "event_key": orphan["event_key"],
            })

    # ── Phase B2: legacy records fall back to per-agent pairing ──────────
    by_agent = defaultdict(list)
    for ev in legacy:
        by_agent[ev["agent"]].append(ev)

    for agent in sorted(by_agent.keys()):
        evts   = sorted(by_agent[agent], key=lambda x: x["ts"])
        starts = [e for e in evts if e["event"] == "subagent_start"]
        stops  = [e for e in evts if e["event"] == "subagent_stop"]
        instances.extend(_pair(starts, stops))
        for orphan in stops[len(starts):]:
            instances.append({
                "agent":       orphan["agent"],
                "session_id":  orphan.get("session_id", ""),
                "session_idx": -1,
                "start_ts":    "--",
                "end_ts":      orphan["ts"],
                "status":      "orphan_stop",
            })

    return instances

# ===== Wire-archive helpers ===============================================

def _find_wire_files(session_id, wire_root):
    """Return sorted list of wire.jsonl paths that match *session_id*."""
    pattern = os.path.join(wire_root, "*", session_id, "agents", "*", "wire.jsonl")
    return sorted(glob.glob(pattern))


def _extract_model_alias(wire_path, max_lines=200):
    """Return the first modelAlias found in *wire_path*, or None."""
    try:
        with open(wire_path, "r", encoding="utf-8") as fh:
            for _ in range(max_lines):
                raw = fh.readline()
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and "modelAlias" in obj:
                        return obj["modelAlias"]
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
    except (OSError, IOError):
        pass
    return None


def _collect_strings(obj, out):
    """Recursively harvest string values from common text fields."""
    if isinstance(obj, str):
        out.append(obj)
        return
    if not isinstance(obj, dict):
        return
    for key in ("content", "text", "message", "prompt", "system_prompt"):
        val = obj.get(key)
        _collect_strings(val, out)
    content = obj.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                _collect_strings(block, out)
    parts = obj.get("parts")
    if isinstance(parts, list):
        for p in parts:
            _collect_strings(p, out)


def _extract_identity_text(wire_path, max_lines=500):
    """Concatenate text from the head of wire.jsonl for identity-keyword matching."""
    chunks = []
    try:
        with open(wire_path, "r", encoding="utf-8") as fh:
            for _ in range(max_lines):
                raw = fh.readline()
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict):
                        chunks.append(str(obj))
                        continue
                    _collect_strings(obj, chunks)
                except (json.JSONDecodeError, ValueError, TypeError):
                    chunks.append(raw)
    except (OSError, IOError):
        pass
    return "".join(chunks)


def _extract_wire_start_time(wire_path):
    """Return epoch-ms of the first event in wire.jsonl, or None."""
    try:
        with open(wire_path, "r", encoding="utf-8") as fh:
            for _ in range(50):
                raw = fh.readline()
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and "time" in obj:
                        return int(obj["time"])
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
    except (OSError, IOError):
        pass
    return None


def _extract_wire_recent_time(wire_path, max_bytes=_WIRE_TAIL_BYTES):
    """Return epoch-ms of the latest timestamped event in a wire archive."""
    try:
        with open(wire_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
        for raw in reversed(lines):
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and "time" in obj:
                    return int(obj["time"])
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    except (OSError, IOError):
        pass
    return None


def _agent_identity_matches(identity_text, agent_type):
    """True when *identity_text* appears to describe *agent_type*."""
    if not identity_text or not agent_type:
        return False
    if agent_type in identity_text:
        return True
    for hint in _IDENTITY_HINTS.get(agent_type, []):
        if hint in identity_text:
            return True
    return False

# ========================================================================
# Main resolution entry point
# ========================================================================

def _pool_model_shortnames():
    """Read pool menu ([secondary_model.models] keys) from kimi config.

    Returns a set of short names (last path segment). Empty set on any
    failure — callers should treat that as "no filtering" (宁吵勿丢).
    """
    try:
        with open(_KIMI_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
        models = cfg.get("secondary_model", {}).get("models", {})
        return {str(k).split("/")[-1] for k in models}
    except Exception:
        return set()


def resolve_models(instances, wire_root):
    """
    Populate ``instances[i]["model"]`` by reading kimi-code wire archives.
    Strategy per instance (in order):
      1. identity-keyword match across all wires in the session
      2. time-nearest fallback: match each unmatched instance to the
         remaining wire whose start time (from wire first event) is
         closest to the instance's start_ts, within a 10-second window
         (greedy by start time order; each wire used at most once).
      Anything unmatched / out of tolerance / any IO failure → "?"
    Returns a sorted list of model strings found in wires but NOT matched
    to any instance (displayed in the summary area). Only models still
    present in the pool menu are reported; historical models from removed
    channels are filtered out as noise. If the pool menu is unreadable,
    no filtering is applied.
    """
    sessions = defaultdict(list)
    for i, inst in enumerate(instances):
        sessions[inst.get("session_id", "")].append(i)

    extra_models = set()

    for sid, idxs in sessions.items():
        # ── no session → all "?" ──────────────────────────────────────────
        if not sid:
            for i in idxs:
                instances[i]["model"] = "?"
            continue

        wire_paths = _find_wire_files(sid, wire_root)

        # ── no wire files → all "?" ───────────────────────────────────────
        if not wire_paths:
            for i in idxs:
                instances[i]["model"] = "?"
            continue

        # ── read metadata from every wire.jsonl ───────────────────────────
        wire_infos = []
        for wp in wire_paths:
            label = os.path.basename(os.path.dirname(wp))   # "agent-N"
            try:
                n = int(label.split("-")[-1])
            except (ValueError, IndexError):
                n = 9999
            # read first event's time (epoch ms) for time-based fallback
            wire_start_time = _extract_wire_start_time(wp)
            # convert epoch ms to local naive datetime for diff computation
            wire_start_local = None
            if wire_start_time is not None:
                try:
                    wire_start_local = datetime.fromtimestamp(
                        wire_start_time / 1000.0
                    )
                except (OSError, ValueError, TypeError):
                    pass
            try:
                wire_mtime = os.path.getmtime(wp)
            except OSError:
                wire_mtime = 0
            wire_infos.append({
                "path":         wp,
                "label":        label,
                "n":            n,
                "identity":     _extract_identity_text(wp, _IDENTITY_LINES),
                "model":        _extract_model_alias(wp, _MODEL_SCAN_LINES),
                "start_dt":     wire_start_local,   # local naive datetime | None
                "recent_dt":    None,
                "recent_checked": False,
                "mtime":        wire_mtime,
            })

        used       = set()   # indices into wire_infos already consumed
        unmatched  = []      # instance indices from Sidx that need fallback

        # ── Phase 1: identity-keyword match ───────────────────────────────
        for ii in idxs:
            atype = instances[ii].get("agent", "")
            for j, wi in enumerate(wire_infos):
                if j in used or not wi["model"]:
                    continue
                if _agent_identity_matches(wi["identity"], atype):
                    instances[ii]["model"] = wi["model"]
                    used.add(j)
                    break
            else:
                instances[ii]["model"] = None
                unmatched.append(ii)

        # ── Phase 2: time-nearest fallback (within 10 s) ────────────────
        unmatched.sort(key=lambda i: instances[i].get("start_ts", ""))
        inst_start_local = {}
        for ii in unmatched:
            ts = instances[ii].get("start_ts", "")
            if ts and ts != "--":
                try:
                    inst_start_local[ii] = datetime.strptime(ts[:19],
                                                              "%Y-%m-%dT%H:%M:%S")
                except (ValueError, TypeError):
                    inst_start_local[ii] = None
            else:
                inst_start_local[ii] = None

        for ii in unmatched:
            if inst_start_local[ii] is None:
                instances[ii]["model"] = None
                continue
            best_j = None
            best_diff = None
            for j, wi in enumerate(wire_infos):
                if j in used or not wi["model"] or wi["start_dt"] is None:
                    continue
                diff = abs((inst_start_local[ii] - wi["start_dt"]).total_seconds())
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_j = j
            if best_j is not None and best_diff <= 10.0:
                instances[ii]["model"] = wire_infos[best_j]["model"]
                used.add(best_j)
            else:
                instances[ii]["model"] = None

        # ── Phase 3: resume fallback by latest wire event (within 10 s) ──
        # resume appends to an existing archive, so its first event is stale.
        resume_candidates = sorted(enumerate(wire_infos),
                                   key=lambda item: item[1]["mtime"], reverse=True)
        for ii in unmatched:
            if instances[ii].get("model") is not None or inst_start_local[ii] is None:
                continue
            best_j = None
            best_diff = None
            for j, wi in resume_candidates[:_RESUME_TAIL_CANDIDATES]:
                if j in used or not wi["model"]:
                    continue
                if not wi["recent_checked"]:
                    wi["recent_checked"] = True
                    recent_time = _extract_wire_recent_time(wi["path"])
                    if recent_time is not None:
                        try:
                            wi["recent_dt"] = datetime.fromtimestamp(recent_time / 1000.0)
                        except (OSError, ValueError, TypeError):
                            pass
                if wi["recent_dt"] is None:
                    continue
                diff = abs((inst_start_local[ii] - wi["recent_dt"]).total_seconds())
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_j = j
            if best_j is not None and best_diff <= 10.0:
                instances[ii]["model"] = wire_infos[best_j]["model"]
                used.add(best_j)

        # ── anything still None / unresolvable → "?" ──────────────────────
        for ii in idxs:
            if instances[ii].get("model") is None:
                instances[ii]["model"] = "?"

        # remaining wires not consumed → extra_models
        for j, wi in enumerate(wire_infos):
            if j not in used and wi["model"]:
                extra_models.add(wi["model"])

    # 收敛噪音:只报告仍在号池菜单里的未匹配模型(已删渠道的历史模型不再列出)。
    # 读不到池菜单时不过滤——宁吵勿丢。
    pool_names = _pool_model_shortnames()
    if pool_names:
        extra_models = {m for m in extra_models
                        if str(m).split("/")[-1] in pool_names}

    # The wire archive belongs to one host; the dispatch record belongs to all
    # of them. When the archive cannot name the model, show the recorded one
    # instead of "?" — an unknown label is worse than the host's own record.
    for inst in instances:
        if inst.get("model") in (None, "?") and inst.get("recorded_model"):
            inst["model"] = inst["recorded_model"]

    return sorted(extra_models)

# ===== Rendering ===========================================================

def format_ts(ts):
    if ts == "--" or ts is None:
        return "--"
    return ts.replace("+00:00", "").replace("Z", "")


def render_board(instances, skip_count, extra_models=None):
    """Render the plain-text board, including the new Model column."""
    if extra_models is None:
        extra_models = []

    headers  = ["#", "Agent", "Model", "Start", "End", "Duration", "Tokens", "Status"]
    col_keys = ["index", "agent", "model", "start_ts", "end_ts",
                "duration", "tokens", "status"]

    rows = []
    for i, inst in enumerate(instances, 1):
        dur = parse_iso_duration(inst["start_ts"], inst["end_ts"])
        tokens = inst.get("tokens")
        rows.append({
            "index":    str(i),
            "agent":    inst["agent"],
            "model":    short_model(inst.get("model", "?")),
            "start_ts": format_ts(inst["start_ts"]),
            "end_ts":   format_ts(inst["end_ts"]),
            "duration": dur,
            # "--" is "the host reported none", which is not the same as zero.
            "tokens":   format_tokens(tokens) if tokens else "--",
            "status":   inst["status"],
        })

    # ── summary counts ───────────────────────────────────────────────────
    total        = len(instances)
    completed    = sum(1 for r in rows if r["status"] == "done")
    running      = sum(1 for r in rows if r["status"] == "running")
    orphan_stops = sum(1 for r in rows if r["status"] == "orphan_stop")

    total_seconds = 0
    for inst in instances:
        if inst["start_ts"] != "--" and inst["end_ts"] != "--" \
                and inst["status"] == "done":
            try:
                fmt_str = "%Y-%m-%dT%H:%M:%S"
                t_s = datetime.strptime(inst["start_ts"][:19], fmt_str)
                t_e = datetime.strptime(inst["end_ts"][:19],   fmt_str)
                total_seconds += int((t_e - t_s).total_seconds())
            except (ValueError, TypeError):
                pass
    h, rem = divmod(total_seconds, 3600)
    m, s   = divmod(rem, 60)
    td = []
    if h: td.append(f"{h}h")
    if m: td.append(f"{m}m")
    if s or not td: td.append(f"{s}s")
    total_dur = " ".join(td)

    # ── column widths ────────────────────────────────────────────────────
    widths = {}
    for key in col_keys:
        max_w = len(headers[col_keys.index(key)])
        for r in rows:
            w = len(r[key])
            if w > max_w:
                max_w = w
        widths[key] = max_w

    right_aligned = ("index", "tokens")

    def fmt_row(vals):
        parts = []
        for i, v in enumerate(vals):
            key = col_keys[i]
            w = widths[key]
            parts.append(v.rjust(w) if key in right_aligned else v.ljust(w))
        return "  ".join(parts)

    sep_w = sum(widths[k] for k in col_keys) + 2 * (len(col_keys) - 1) + 2
    sep   = "=" * sep_w

    lines = [
        sep,
        " TJ-v2 Status Board",
        sep,
        "",
        fmt_row(headers),
        "-" * sep_w,
    ]
    for r in rows:
        lines.append(fmt_row([r[k] for k in col_keys]))

    # ── summary line (unchanged format) ──────────────────────────────────
    sum_parts = (
        f" Running: {running}"
        f"  Done: {completed}"
        f"  Orphan-stops: {orphan_stops}"
        f"  Total: {total}"
        f"  Time: {total_dur}"
    )
    lines.append("")
    lines.append("-" * sep_w)
    lines.append(f" Summary{sum_parts}")

    # Cost per model, because "which tier should get more money" is a question
    # about spend, and a board that cannot answer it is only half a board.
    spend = defaultdict(int)
    for inst in instances:
        if inst.get("tokens"):
            spend[inst.get("model") or "?"] += inst["tokens"]
    if spend:
        # Spend is summed per full model id, so two vendors sharing a name are
        # never added together; only the label gets shortened.
        names = defaultdict(list)
        for model in spend:
            names[short_model(model)].append(model)

        def label(model):
            return model if len(names[short_model(model)]) > 1 else short_model(model)

        ranked = sorted(spend.items(), key=lambda item: (-item[1], item[0]))
        lines.append(
            f" Tokens: {format_tokens(sum(spend.values()))} total | "
            + "  ".join(f"{label(model)} {format_tokens(count)}" for model, count in ranked)
        )

    if extra_models:
        lines.append(f" Unmatched models: {', '.join(extra_models)}")

    if skip_count > 0:
        lines.append(f" [WARN] {skip_count} corrupt/invalid line(s) skipped.")
    lines.append("")
    return "\n".join(lines)

# ===== Entry point =========================================================

def main():
    args = parse_args()
    wire_root = args.kimi_home or os.environ.get("TJ_WIRE_ROOT", _WIRE_ROOT)
    filepath  = resolve_path(args)

    if not os.path.isfile(filepath):
        print(f"state.jsonl not found: {filepath}")
        sys.exit(0)

    events, skip_count = read_state_events(filepath)
    instances           = build_instances(events)
    extra               = resolve_models(instances, wire_root)
    board               = render_board(instances, skip_count, extra)
    print(board)


if __name__ == "__main__":
    main()
