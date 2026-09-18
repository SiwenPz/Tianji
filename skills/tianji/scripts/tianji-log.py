#!/usr/bin/env python3
"""
tianji-log.py - TJ-v2 Subagent Detail Viewer
Shows detailed info for a specific subagent instance by its board # index.

Usage:
    python tianji-log.py N [--cwd DIR]   # N = board #, 1-based
    python tianji-log.py [--cwd DIR]     # default: latest instance

Reuses board.py logic (read_state_events, build_instances, resolve_models, etc.)
"""

import argparse
import json
import os
import sys
from datetime import datetime

# ===================================================================
# Reuse board.py (same pattern as tianji-dash.py L17-27)
# ===================================================================
_BOARD_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOARD_DIR not in sys.path:
    sys.path.insert(0, _BOARD_DIR)

from board import (  # noqa: E402
    read_state_events,
    build_instances,
    resolve_models,
    parse_iso_duration,
    format_ts,
    _find_wire_files,
    _extract_model_alias,
    _extract_identity_text,
    _extract_wire_start_time,
    _agent_identity_matches,
    _WIRE_ROOT as _BOARD_WIRE_ROOT,
)

# ===================================================================
# Globals
# ===================================================================
_WIRE_ROOT = os.environ.get(
    "TJ_WIRE_ROOT",
    os.environ.get("TJ_KIMI_HOME", _BOARD_WIRE_ROOT),
)


# ===================================================================
# CLI
# ===================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="TJ-v2 Subagent Detail Viewer: show info for one instance.",
    )
    p.add_argument(
        "n", nargs="?", type=int, default=None,
        help="Instance # (1-based board index; omit for latest)",
    )
    p.add_argument(
        "--cwd", default=None,
        help="Project root (defaults to current directory)",
    )
    p.add_argument(
        "--kimi-home", default=None,
        help="Override kimi-code sessions root",
    )
    return p.parse_args()


def resolve_state_path(args):
    if args.cwd:
        return os.path.join(args.cwd, ".tianji", "state.jsonl")
    return os.path.join(os.getcwd(), ".tianji", "state.jsonl")


# ===================================================================
# Wire file lookup (mirrors board.py resolve_models pairing strategy)
# ===================================================================

def _parse_wire_label_agent_n(wire_path):
    """Extract the numeric suffix from agent-N directory name."""
    try:
        label = os.path.basename(os.path.dirname(wire_path))  # "agent-N"
        return int(label.split("-")[-1])
    except (ValueError, IndexError, OSError):
        return 9999


def find_wire_for_instance(inst, wire_root):
    """
    Find the best-matching wire.jsonl for *inst* using the same
    identity-match + time-nearest strategy as board.py resolve_models().
    Returns the wire path or None.
    """
    session_id = inst.get("session_id", "")
    if not session_id:
        return None

    wire_paths = _find_wire_files(session_id, wire_root)
    if not wire_paths:
        return None

    agent_type = inst.get("agent", "")

    if len(wire_paths) == 1:
        return wire_paths[0]

    # Collect metadata for each wire (same as resolve_models)
    wire_infos = []
    for wp in wire_paths:
        start_time = _extract_wire_start_time(wp)
        start_dt = None
        if start_time is not None:
            try:
                start_dt = datetime.fromtimestamp(start_time / 1000.0)
            except (OSError, ValueError, TypeError):
                pass
        wire_infos.append({
            "path": wp,
            "n": _parse_wire_label_agent_n(wp),
            "identity": _extract_identity_text(wp),
            "model": _extract_model_alias(wp),
            "start_dt": start_dt,
        })

    # Phase 1: identity-keyword match
    for wi in wire_infos:
        if not wi["model"]:
            continue
        if _agent_identity_matches(wi["identity"], agent_type):
            return wi["path"]

    # Phase 2: time-nearest fallback
    ts = inst.get("start_ts", "")
    inst_start = None
    if ts and ts != "--":
        try:
            inst_start = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            pass

    if inst_start:
        best_wp = None
        best_diff = None
        for wi in wire_infos:
            if not wi["model"] or wi["start_dt"] is None:
                continue
            diff = abs((inst_start - wi["start_dt"]).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_wp = wi["path"]
        if best_wp is not None and best_diff <= 10.0:
            return best_wp

    # 配对全失败:返回 None(显示"未找到"),不静默 fallback——
    # 拿第一个 wire 充数会张冠李戴,宁可报缺
    return None


# ===================================================================
# Wire content extraction
# ===================================================================

def extract_last_assistant_text(wire_path):
    """
    Read wire.jsonl and return the last assistant text message body.
    Searches two event shapes:
      1. type=context.append_loop_event → event.type=content.part
         where part.type == "text" (not "think")
      2. type=context.append_message where message.role == "assistant"
    Returns the text string or None.
    """
    last_text = None
    try:
        with open(wire_path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue

                etype = obj.get("type", "")

                # Shape 1: context.append_loop_event with content.part/text
                if etype == "context.append_loop_event":
                    inner = obj.get("event")
                    if isinstance(inner, dict) and inner.get("type") == "content.part":
                        part = inner.get("part")
                        if isinstance(part, dict) and part.get("type") == "text":
                            text = part.get("text", "")
                            if text and text.strip():
                                last_text = text

                # Shape 2: context.append_message with assistant role
                if etype == "context.append_message":
                    msg = obj.get("message")
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        content = msg.get("content")
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text and text.strip():
                                        last_text = text
    except (OSError, IOError):
        pass
    return last_text


def extract_token_usage(wire_path, agent_id=None):
    """
    Aggregate token usage from usage.record events in wire.jsonl.
    Returns dict with totals and model names.
    """
    totals = {
        "input_other": 0,
        "output": 0,
        "input_cache_read": 0,
        "input_cache_creation": 0,
    }
    models = set()
    try:
        with open(wire_path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != "usage.record":
                    continue
                # Optional agent filter
                if agent_id and obj.get("agentId") != agent_id:
                    # Also match "agent-1" pattern from "executor" etc.
                    a_id = obj.get("agentId", "")
                    if not a_id.endswith(str(agent_id)):
                        continue
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    totals["input_other"] += usage.get("inputOther", 0)
                    totals["output"] += usage.get("output", 0)
                    totals["input_cache_read"] += usage.get("inputCacheRead", 0)
                    totals["input_cache_creation"] += usage.get("inputCacheCreation", 0)
                model = obj.get("model")
                if model:
                    models.add(model)
    except (OSError, IOError):
        pass
    return totals, sorted(models)


def render_instance_timeline(inst):
    """Render just this instance's start/stop pair from build_instances() output."""
    lines = []
    status = inst.get("status", "?")
    start_ts = format_ts(inst.get("start_ts", "--"))
    end_ts = format_ts(inst.get("end_ts", "--"))

    if start_ts != "--":
        lines.append(f"    {start_ts}  start")
    if status in ("done", "orphan_stop") and end_ts != "--":
        lines.append(f"    {end_ts}  stop")
    if not lines:
        lines.append("    --  (no event data)")
    return lines


# ===================================================================
# Rendering
# ===================================================================

def render_detail(inst, idx, state_path, wire_root):
    """Render the full detail view for one instance."""
    lines = []
    sep = "=" * 72

    lines.append(sep)
    lines.append(f"  # {idx}  Detail  │  {state_path}")
    lines.append(sep)
    lines.append("")

    # ── Basic info ────────────────────────────────────
    agent = inst.get("agent", "?")
    model = inst.get("model", "?")
    status = inst.get("status", "?")
    start_ts = format_ts(inst.get("start_ts", "--"))
    end_ts = format_ts(inst.get("end_ts", "--"))
    duration = parse_iso_duration(inst["start_ts"], inst["end_ts"])
    session_id = inst.get("session_id", "")

    lines.append(f"  编号     : #{idx}")
    lines.append(f"  角色     : {agent}")
    lines.append(f"  模型     : {model}")
    lines.append(f"  状态     : {status}")
    lines.append(f"  开始时间 : {start_ts}")
    lines.append(f"  结束时间 : {end_ts}")
    lines.append(f"  耗时     : {duration}")
    if session_id:
        short_sid = session_id[:12] + "..." if len(session_id) > 12 else session_id
        lines.append(f"  Session  : {short_sid}")
    lines.append("")

    # ── Event timeline ────────────────────────────────
    lines.append("  ── 事件时间线 ──────────────────────────")
    timeline = render_instance_timeline(inst)
    for tline in timeline:
        lines.append(tline)
    lines.append("")

    # ── Wire file info ────────────────────────────────
    wire_path = find_wire_for_instance(inst, wire_root)
    lines.append(f"  ── Wire 文件 ───────────────────────────")
    if wire_path:
        lines.append(f"    {wire_path}")
    else:
        lines.append(f"    未找到 (session_id: {session_id or '(空)'})")
    lines.append("")

    # ── Delivery receipt (last assistant text) ────────
    lines.append(f"  ── 回执全文 ────────────────────────────")
    receipt = None
    if wire_path:
        receipt = extract_last_assistant_text(wire_path)
    if receipt:
        # Indent each line for readability
        for rline in receipt.splitlines():
            lines.append(f"    {rline}")
    else:
        lines.append(f"    未找到回执")
    lines.append("")

    # ── Token usage ───────────────────────────────────
    lines.append(f"  ── Token Usage ─────────────────────────")
    usage_data = None
    model_list = []
    if wire_path:
        # Try to match agentId - extract agent number from agent name
        usage_data, model_list = extract_token_usage(wire_path)
    if usage_data and sum(usage_data.values()) > 0:
        lines.append(f"    Input (other)        : {usage_data['input_other']:,}")
        lines.append(f"    Output               : {usage_data['output']:,}")
        lines.append(f"    Input (cache read)   : {usage_data['input_cache_read']:,}")
        lines.append(f"    Input (cache create) : {usage_data['input_cache_creation']:,}")
        total_in = (usage_data['input_other'] + usage_data['input_cache_read']
                    + usage_data['input_cache_creation'])
        total_all = total_in + usage_data['output']
        lines.append(f"    Total tokens         : {total_all:,}")
        if model_list:
            lines.append(f"    Models               : {', '.join(model_list)}")
    else:
        lines.append(f"    未找到 token usage 数据")
    lines.append("")

    lines.append(sep)
    return "\n".join(lines)


# ===================================================================
# Entry point
# ===================================================================

def main():
    # === stdio encoding unlock (same as statusline.py) ===
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, LookupError):
        pass

    args = parse_args()
    state_path = resolve_state_path(args)
    wire_root = args.kimi_home or os.environ.get(
        "TJ_WIRE_ROOT", os.environ.get("TJ_KIMI_HOME", _WIRE_ROOT)
    )

    # Read state
    if not os.path.isfile(state_path):
        print(f"state.jsonl not found: {state_path}")
        sys.exit(0)

    try:
        events, skip_count = read_state_events(state_path)
        instances = build_instances(events)
    except Exception as e:
        print(f"读取台账失败: {e}")
        sys.exit(0)

    if not instances:
        print("台账为空，没有子代理实例。")
        sys.exit(0)

    # Resolve models (mutates instances in-place)
    try:
        _ = resolve_models(instances, wire_root)
    except Exception:
        pass  # fail-open, leave models as-is

    # Select instance
    total = len(instances)
    if args.n is None:
        # Default: latest (highest # = last in sorted order)
        target_idx = total - 1
        target_num = total
    else:
        n = args.n
        if n < 1 or n > total:
            print(f"编号不存在: #{n} (台账共 {total} 个实例)")
            sys.exit(0)
        target_idx = n - 1
        target_num = n

    inst = instances[target_idx]
    output = render_detail(inst, target_num, state_path, wire_root)
    print(output)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            print(f"[tianji-log] error: {e}")
        except Exception:
            pass
        sys.exit(0)
