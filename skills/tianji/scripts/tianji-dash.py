#!/usr/bin/env python3
"""
tianji-dash.py - TJ-v2 Real-time Dashboard
Watch-style terminal dashboard, refreshes every interval seconds.
Pure stdlib. ANSI colors. Windows msvcrt for non-blocking key check.
"""

import argparse
import os
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Reuse board.py logic
# ---------------------------------------------------------------------------
_BOARD_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOARD_DIR not in sys.path:
    sys.path.insert(0, _BOARD_DIR)

from board import (  # noqa: E402
    read_state_events,
    build_instances,
    resolve_models,
    parse_iso_duration,
    format_ts,
)

# ---------------------------------------------------------------------------
# ANSI helpers (pure ASCII, safe for GBK terminals)
# ---------------------------------------------------------------------------
_GREEN = "\033[32m"
_GRAY = "\033[90m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"
_CLEAR = "\033[2J\033[H"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="TJ-v2 Real-time Dashboard")
    p.add_argument("--cwd", default=None, help="Project root (default: cwd)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Refresh interval in seconds (default: 1)")
    p.add_argument("--kimi-home", default=None,
                   help="Override kimi-code sessions root")
    return p.parse_args()


def resolve_state_path(args):
    if args.cwd:
        return os.path.join(args.cwd, ".tianji", "state.jsonl")
    return os.path.join(os.getcwd(), ".tianji", "state.jsonl")


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------

def _is_stale(inst, now):
    """RUNNING older than 10 minutes without a stop => stale."""
    if inst["status"] != "running":
        return False
    ts = inst.get("start_ts", "")
    if not ts or ts == "--":
        return False
    try:
        ts_clean = ts.replace("Z", "+00:00")
        t = datetime.fromisoformat(ts_clean)
        # Normalize both to naive local (strip tz for arithmetic)
        if t.tzinfo is not None:
            t = t.astimezone().replace(tzinfo=None)
        now_naive = now.replace(tzinfo=None)
        delta = (now_naive - t).total_seconds()
        return delta > 600  # 10 minutes
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Duration for running instances (live clock)
# ---------------------------------------------------------------------------

def _running_duration(inst, now):
    ts = inst.get("start_ts", "")
    if not ts or ts == "--":
        return "?"
    try:
        ts_clean = ts.replace("Z", "+00:00")
        t = datetime.fromisoformat(ts_clean)
        if t.tzinfo is not None:
            t = t.astimezone().replace(tzinfo=None)
        now_naive = now.replace(tzinfo=None)
        delta = now_naive - t
        total = int(delta.total_seconds())
        if total < 0:
            return "0s"
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        parts = []
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
        parts.append(f"{s}s")
        return " ".join(parts)
    except (ValueError, TypeError):
        return "?"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_STATUS_COLORS = {
    "done": _GRAY,
    "running": _GREEN,
    "orphan_stop": _YELLOW,
    "stale": _RED,
}

_STATUS_LABELS = {
    "done": "DONE",
    "running": "RUNNING",
    "orphan_stop": "ORPHAN",
    "stale": "STALE",
}


def render(instances, skip_count, state_path, now, err_msg=None):
    """Render one frame. Returns the full string."""
    lines = []

    # Title
    lines.append(f"TJ-v2 Dashboard  |  {now:%Y-%m-%d %H:%M:%S}  |  {state_path}")

    # Header
    hdr = f"  {'#':>3}  {'Role':<18}  {'Model':<22}  {'Status':<9}  {'Elapsed':>10}  {'Last Event':<22}"
    lines.append(hdr)
    lines.append("  " + "-" * 115)

    running_n = done_n = stale_n = 0
    for i, inst in enumerate(instances, 1):
        stale = _is_stale(inst, now)
        status = "stale" if stale else inst["status"]

        if status == "running" or (stale and inst["status"] == "running"):
            if stale:
                stale_n += 1
            else:
                running_n += 1
        elif status == "done":
            done_n += 1
        elif status == "stale":
            stale_n += 1
        elif status == "orphan_stop":
            running_n += 1  # treat orphan as running-ish

        # Duration
        if status in ("done", "orphan_stop"):
            dur = parse_iso_duration(inst["start_ts"], inst["end_ts"])
        else:
            dur = _running_duration(inst, now)

        color = _STATUS_COLORS.get(status, _RESET)
        label = _STATUS_LABELS.get(status, status.upper())

        model = inst.get("model", "?")
        # Truncate model to fit
        if len(model) > 20:
            model = model[:18] + ".."

        role = inst["agent"]
        if len(role) > 16:
            role = role[:14] + ".."

        last_evt = format_ts(inst.get("end_ts" if status == "done" else "start_ts", "--"))
        if len(last_evt) > 20:
            last_evt = last_evt[:20]

        row = (f"  {i:>3}  {role:<18}  {model:<22}  "
               f"{color}{label:<9}{_RESET}  {dur:>10}  {last_evt:<22}")
        lines.append(row)

    # Summary
    lines.append("")
    lines.append(f"  run {running_n}  |  done {done_n}  |  stale {stale_n}")

    # Error footer
    if err_msg:
        lines.append("")
        lines.append(f"  {_YELLOW}[ERR] {err_msg}{_RESET}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    interval = max(0.5, args.interval)
    state_path = resolve_state_path(args)

    wire_root = args.kimi_home or os.environ.get("TJ_WIRE_ROOT",
                                                  os.path.expanduser("~/.kimi-code/sessions"))

    print(_CLEAR, end="")
    print("TJ-v2 Dashboard starting... (press 'q' or Ctrl-C to quit)")
    time.sleep(1)

    try:
        while True:
            now = datetime.now()
            frame_err = None

            try:
                if not os.path.isfile(state_path):
                    print(_CLEAR, end="")
                    print(f"TJ-v2 Dashboard  |  {now:%Y-%m-%d %H:%M:%S}  |  {state_path}")
                    print()
                    print("  no state yet, waiting...")
                    print()
                    print(f"  run 0  |  done 0  |  stale 0")
                else:
                    events, skip_count = read_state_events(state_path)
                    instances = build_instances(events)
                    extra = resolve_models(instances, wire_root)
                    # Add model info to instances for the dashboard
                    # resolve_models already mutates instances in-place with "model"
                    frame = render(instances, skip_count, state_path, now, None)
                    print(_CLEAR, end="")
                    print(frame)
            except Exception as e:
                frame_err = str(e)
                # On error, still try to show something
                try:
                    print(_CLEAR, end="")
                    now2 = datetime.now()
                    print(f"TJ-v2 Dashboard  |  {now2:%Y-%m-%d %H:%M:%S}  |  {state_path}")
                    print()
                    print(f"  {_YELLOW}[ERR] {frame_err}{_RESET}")
                    print()
                    print(f"  run ?  |  done ?  |  stale ?")
                except Exception:
                    pass

            # Non-blocking key check (msvcrt, Windows)
            try:
                import msvcrt
                start_t = time.time()
                while time.time() - start_t < interval:
                    if msvcrt.kbhit():
                        ch = msvcrt.getch()
                        if ch in (b'q', b'Q', b'\x1b'):
                            raise KeyboardInterrupt
                    time.sleep(0.05)
            except ImportError:
                # Non-Windows: just sleep
                time.sleep(interval)
            except KeyboardInterrupt:
                raise

    except KeyboardInterrupt:
        pass
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        # Restore cursor and colors
        print("\033[?25h\033[0m", flush=True)
        print("Dashboard stopped.", flush=True)


if __name__ == "__main__":
    main()
