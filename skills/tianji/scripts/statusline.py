#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# 天机 v2 状态行:把 kimi 原生底栏信息(模型/context/git)与天机状态(子代理跑/完/最近)
# 及号池可用模型清单渲染成一行。
#
# tui.toml 接线方式:
#   [status_line]
#   command = "python ~/.agents/skills/tianji/scripts/statusline.py"
#
# 约束:stdout 第一行替换底栏第一行;300ms 上限;任何异常静默 exit 0;
# 输出已按 UTF-8 配置(main 入口 reconfigure)，支持 Unicode 字符输出。

import sys
import os
import glob
from concurrent.futures import ThreadPoolExecutor
import json
import tomllib
import time
from datetime import datetime
import re

import ledger_reader  # noqa: E402

# === board 模块复用 (参照 tianji-dash.py L17-27) ===
_BOARD_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOARD_DIR not in sys.path:
    sys.path.insert(0, _BOARD_DIR)

from board import (  # noqa: E402
    _find_wire_files,
    _agent_identity_matches,
    _extract_wire_recent_time,
    _IDENTITY_LINES,
    format_tokens,
    short_model,
)

ANSI_ESCAPE = re.compile(r'\033\[[0-9;]*m')

TAIL_BYTES = 65536  # state.jsonl 只倒读尾部 ~64KB
WIRE_TAIL_BYTES = 8192  # wire.jsonl 只读尾部 8KB
RESUME_TAIL_CANDIDATES = 8  # resume fallback only inspects the freshest archives
MAX_LINE_WIDTH = 120
CONFIG = os.path.expanduser("~/.kimi-code/config.toml")
HAS_UTF8_BLOCKS = False
_WIRE_ROOT = os.environ.get("TJ_WIRE_ROOT",
                            os.path.expanduser("~/.kimi-code/sessions"))

POOL_PALETTE = [114, 148, 179, 110, 186, 81]


def _build_rainbow_palette():
    """Build 24-step smooth HSV hue palette mapped to xterm-256 (hue 0°→300°, S=1, V=1)."""
    palette = []
    for i in range(24):
        hue = i * 300.0 / 23.0  # 0° to 300° inclusive
        h = hue / 60.0
        sector = int(h) % 6
        frac = h - int(h)
        if sector == 0:
            r, g, b = 1.0, frac, 0.0
        elif sector == 1:
            r, g, b = 1.0 - frac, 1.0, 0.0
        elif sector == 2:
            r, g, b = 0.0, 1.0, frac
        elif sector == 3:
            r, g, b = 0.0, 1.0 - frac, 1.0
        elif sector == 4:
            r, g, b = frac, 0.0, 1.0
        else:
            r, g, b = 1.0, 0.0, 1.0 - frac
        r6 = round(r * 5)
        g6 = round(g * 5)
        b6 = round(b * 5)
        code = 16 + 36 * r6 + 6 * g6 + b6
        palette.append(code)
    return palette


_RAINBOW_PALETTE = _build_rainbow_palette()


def read_budgets(cwd, session_id):
    """What each dispatch of this session was allowed to spend.

    Read from the registry, not the ledger: the ledger says what was spent, the
    registry says what was allowed, and a ceiling that lives only in whoever
    wrote the task book is not a ceiling. A record that cannot be parsed is
    skipped rather than counted as zero -- an unreadable ceiling must not read
    as an overrun.
    """
    if not session_id:
        return {}
    pattern = os.path.join(
        cwd, ".tianji", "runtime", "runs", "*", "tasks", "*", "*",
        "invocations", "*.json",
    )
    budgets = {}
    for path in glob.iglob(pattern):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                record = json.load(stream)
        except (OSError, ValueError):
            continue
        if record.get("session_id") != session_id:
            continue
        invocation_id = record.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            continue
        try:
            budgets[invocation_id] = int(record.get("token_budget") or 0)
        except (TypeError, ValueError):
            # One record with a ceiling that is not a number costs that record,
            # not the meter: skipping the whole read would make the spend
            # disappear, and a display that vanishes reads as "nothing spent".
            continue
    return budgets


def read_time_limits(cwd, session_id):
    """Wall-clock ceilings each dispatch of this session was given.

    Read from the same registry records as `read_budgets`: a ceiling that
    lives only in whoever wrote the task book is not a ceiling, and the clock
    is no different. A record with no time budget is left out rather than
    counted as unbounded or as zero -- "no ceiling" is not a ceiling that has
    been exceeded.
    """
    if not session_id:
        return {}
    pattern = os.path.join(
        cwd, ".tianji", "runtime", "runs", "*", "tasks", "*", "*",
        "invocations", "*.json",
    )
    limits = {}
    for path in glob.iglob(pattern):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                record = json.load(stream)
        except (OSError, ValueError):
            continue
        if record.get("session_id") != session_id:
            continue
        invocation_id = record.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            continue
        try:
            minutes = int(record.get("time_budget_minutes") or 0)
        except (TypeError, ValueError):
            continue
        if minutes <= 0:
            continue
        limits[invocation_id] = (minutes, record.get("created_at") or "")
    return limits


def spend_by_invocation(events):
    """The newest count each dispatch reported.

    Progress rows carry a running total, so the largest one is where it stands,
    and a stop row is the last word. Taking the maximum gives both without
    trusting the tail of the ledger to be in order.
    """
    spend = {}
    for event in events:
        detail = event.get("detail")
        value = detail.get("tokensUsed") if isinstance(detail, dict) else None
        invocation_id = event.get("invocation_id") or ""
        if isinstance(value, int) and value > 0 and invocation_id:
            spend[invocation_id] = max(spend.get(invocation_id, 0), value)
    return spend


def running_invocations(events):
    """Dispatches with activity and no stop yet.

    Activity, not the start row, is what says a dispatch is alive: the footer
    reads only the tail of the ledger, so a long dispatch's start has usually
    scrolled out of it while its progress rows keep arriving.
    """
    active, stopped = set(), set()
    for event in events:
        invocation_id = event.get("invocation_id") or ""
        if not invocation_id:
            continue
        if event.get("event") == "subagent_stop":
            stopped.add(invocation_id)
        else:
            active.add(invocation_id)
    return active - stopped


def meter_text(events, budgets):
    """``(text, is_warning)`` for this session, with no colour in it.

    A footer is not the only place this number has to appear: the Command Code
    mod draws its own line and needs the same reading of the same ledger, so the
    decision lives here once and colour is applied by whoever can show it.

    A dispatch that finished over budget is not a warning: it is a fact about
    the past, and it is reported where it belongs, by `close-task` and on the
    board. A footer that stays red about an overrun from an hour ago is showing
    an old value as if it were the current one.

    The quiet ratio covers the dispatches that were given a ceiling, on both
    sides; an unbudgeted dispatch is left out rather than counted as free or
    charged to somebody else's limit. With no ceiling anywhere it falls back to
    plain spend, which claims nothing about what was allowed.
    """
    spend = spend_by_invocation(events)
    known = {inv: amount for inv, amount in spend.items() if inv in budgets}
    if not known:
        return "", False
    budgeted = {inv: amount for inv, amount in known.items() if budgets[inv]}

    running = running_invocations(events)
    live_over = [inv for inv in budgeted
                 if inv in running and budgeted[inv] > budgets[inv]]
    if live_over:
        worst = max(live_over, key=lambda inv: budgeted[inv] - budgets[inv])
        return "tok {}/{}!".format(
            format_tokens(budgeted[worst]), format_tokens(budgets[worst]),
        ), True

    if budgeted:
        return "tok {}/{}".format(
            format_tokens(sum(budgeted.values())),
            format_tokens(sum(budgets[inv] for inv in budgeted)),
        ), False
    return "tok {}".format(format_tokens(sum(known.values()))), False


def minutes_since(iso_ts, now=None):
    """Minutes elapsed since a registry timestamp, or None if unusable."""
    try:
        started = datetime.fromisoformat(str(iso_ts))
    except (ValueError, TypeError):
        return None
    now = now or datetime.now(started.tzinfo)
    if now.tzinfo is None and started.tzinfo is not None:
        now = now.replace(tzinfo=started.tzinfo)
    return (now - started).total_seconds() / 60.0


def time_meter_text(events, limits, now=None):
    """``(text, is_warning)`` for a dispatch that is *currently* past its time.

    Only a live dispatch can be over time right now: a finished one is a fact
    about the past, reported by `close-task` and on the board, so a footer that
    stays red about it would be showing an old value as if it were the current
    one. A dispatch with no time ceiling is left out rather than counted as
    free -- the same rule the token meter follows.
    """
    if not limits:
        return "", False
    running = running_invocations(events)
    live_over = []
    for invocation_id in running:
        entry = limits.get(invocation_id)
        if not entry:
            continue
        minutes, opened = entry
        elapsed = minutes_since(opened, now)
        if elapsed is None or elapsed <= minutes:
            continue
        live_over.append((minutes, elapsed))
    if not live_over:
        return "", False
    minutes, elapsed = max(live_over, key=lambda row: row[1] - row[0])
    return "\u65f6\u95f4 {}/{} \u5206!".format(int(elapsed), minutes), True


def call_spend_by_invocation(events):
    """The newest child tool-call count each dispatch reported.

    The same shape the token meter reads: the host reports a running total on
    its progress rows, so the largest one is where the dispatch stands. A row
    that carries no count contributes nothing -- an unreported call must not
    read as zero calls made.
    """
    spend = {}
    for event in events:
        detail = event.get("detail")
        value = detail.get("toolCalls") if isinstance(detail, dict) else None
        invocation_id = event.get("invocation_id") or ""
        if isinstance(value, int) and value > 0 and invocation_id:
            spend[invocation_id] = max(spend.get(invocation_id, 0), value)
    return spend


def read_call_limits(cwd, session_id):
    """Call ceilings each dispatch of this session was given.

    Read from the same registry records as `read_budgets`, for the same reason:
    a ceiling that lives only in whoever wrote the task book is not a ceiling.
    A record with no call budget is left out rather than counted as unbounded.
    """
    if not session_id:
        return {}
    pattern = os.path.join(
        cwd, ".tianji", "runtime", "runs", "*", "tasks", "*", "*",
        "invocations", "*.json",
    )
    limits = {}
    for path in glob.iglob(pattern):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                record = json.load(stream)
        except (OSError, ValueError):
            continue
        if record.get("session_id") != session_id:
            continue
        invocation_id = record.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            continue
        try:
            budget = int(record.get("call_budget") or 0)
        except (TypeError, ValueError):
            continue
        if budget <= 0:
            continue
        limits[invocation_id] = budget
    return limits


def call_meter_text(events, limits):
    """``(text, is_warning)`` for a dispatch that is *currently* past its calls.

    Only a live dispatch can be over its call budget right now: a finished one
    is a fact about the past, reported by `close-task` and on the board, so a
    footer that stayed red about it would be showing an old value as if it were
    the current one. A dispatch with no call ceiling is left out rather than
    counted as free -- the rule the other two meters follow.
    """
    if not limits:
        return "", False
    spend = call_spend_by_invocation(events)
    running = running_invocations(events)
    live_over = [(limits[inv], spend.get(inv, 0))
                 for inv in running
                 if inv in limits and spend.get(inv, 0) > limits[inv]]
    if not live_over:
        return "", False
    budget, spent = max(live_over, key=lambda row: row[1] - row[0])
    return "调用 {}/{}!".format(spent, budget), True


def meter_reading(events, budgets, time_limits=None, now=None, call_limits=None):
    """``(text, is_warning)`` for the counter and the clock together, no colour.

    Two hosts draw two different lines from the same ledger: the kimi footer
    calls `budget_meter` and the Command Code mod asks `statusline.py --meter`
    for plain text. Composing the warning inside the coloured path alone put the
    time warning on one line and not the other -- the mod's line showed no
    warning at all, because it read `meter_text` and nothing else. So the
    composing happens here once, `budget_meter` adds colour and the `--meter`
    path calls the same function.

    A live time overrun leads: it is the one warning the token meter does not
    already carry, and it is reported only while the dispatch is still running.
    """
    text, warning = meter_text(events, budgets)
    time_text, time_warning = time_meter_text(events, time_limits or {}, now)
    call_text, call_warning = call_meter_text(events, call_limits or {})
    lead = [part for part, live in ((time_text, time_warning),
                                    (call_text, call_warning)) if live]
    if lead:
        text = " ".join(lead + ([text] if text else []))
        warning = True
    return text, warning


def budget_meter(events, budgets, time_limits=None, now=None, call_limits=None):
    """`meter_reading` in the colour the terminal can show, for the hosts that have one."""
    text, warning = meter_reading(events, budgets, time_limits, now, call_limits)
    if not text:
        return "", False
    if warning:
        return "\033[1;38;5;196m{}\033[0m".format(text), True
    return "\033[38;5;245m{}\033[0m".format(text), False


def compose_line(head, tianji, meter, warning, sep):
    """Assemble the footer, keeping a warning where nothing can cut it.

    The line is truncated at the right edge, and the head is not a fixed width:
    a long model name, a deep directory and a branch name can eat the whole
    budget before the tianji segment starts. An overrun therefore leads the
    line; the ordinary meter stays in the tianji segment, where it does not
    displace the model the user is actually watching.
    """
    tail = tianji[len(sep):] if tianji.startswith(sep) else tianji
    if meter and warning:
        return meter + sep + head + (sep + tail if tail else "")
    if meter:
        return head + sep + meter + (" " + tail if tail else "")
    return head + tianji


def read_state(cwd):
    """Read tail of state.jsonl and parse events; None if file missing.

    Only authoritative records count. The footer shares one reducer with the
    rest of the core, so a quarantined or historical line must never inflate
    the running count -- that is precisely how a corrupt record becomes a
    phantom worker.
    """
    path = os.path.join(cwd, ".tianji", "state.jsonl")
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0 if size <= TAIL_BYTES else size - TAIL_BYTES)
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    read = ledger_reader.read_lines(raw, path=path)
    events = []
    for record in read.canonical:
        normalized = ledger_reader.canonical_event(record)
        if normalized:
            events.append(normalized)
    return events


def read_ledger(cwd):
    """Every authoritative event in this workspace's ledger, not just the tail.

    `read_state` reads the last 64KB because what is running *now* is at the end.
    A total is not: once the ledger outgrows the window, the rows carrying a
    session's spend scroll out of it and the number silently drops -- it shrank
    from `tok 1M/2.6M` to `tok 962.3K/2.5M` and still looked authoritative. A
    dispatcher's own check caught this, not a review.
    """
    path = os.path.join(cwd, ".tianji", "state.jsonl")
    try:
        with open(path, "rb") as stream:
            raw = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    read = ledger_reader.read_lines(raw, path=path)
    events = []
    for record in read.canonical:
        normalized = ledger_reader.canonical_event(record)
        if normalized:
            events.append(normalized)
    return events


def analyze(events):
    """Count running/completed and get last event; return worker count dict.

    Running is read with the same rule the meter uses -- a dispatch with activity
    and no stop -- not from start rows alone. This footer reads only the tail of
    the ledger, so a long dispatch's start is usually gone from it while its
    progress rows keep arriving; a display that forgets a live dispatch is worse
    than one that shows nothing, and a meter hidden because the start scrolled
    away is the same mistake wearing a different hat.
    """
    running = {}
    completed = 0
    last_agent = None
    last_time = None
    agent_of = {}
    for ev in events:
        etype = ev.get("event", "")
        agent = ev.get("agent", "")
        ts = ev.get("ts", "")
        invocation_id = ev.get("invocation_id") or ""
        if invocation_id and agent:
            agent_of[invocation_id] = agent
        if etype == "subagent_stop":
            completed += 1
        if ts and (last_time is None or ts > last_time):
            last_time = ts
            last_agent = agent
    for invocation_id in running_invocations(events):
        agent = agent_of.get(invocation_id)
        if agent:
            running[agent] = running.get(agent, 0) + 1
    return running, completed, last_agent, last_time


def format_time(iso_ts):
    try:
        return datetime.fromisoformat(iso_ts).strftime("%H:%M")
    except (ValueError, TypeError):
        return "??:??"


def _read_roles_toml(cwd):
    """读取花名册 roles.toml,返回 {角色: [模型名列表]} 或 None。读取优先级: TJ_ROLES_FILE 环境变量 > ~/.tianji/roles.toml

    使用 tomllib 解析;由于角色名可能是 UTF-8 非 ASCII 的 unquoted key(Python tomllib 不支持),
    先尝试 tomllib.loads 直接解析,若失败则按简化格式手动解析 [roles] 段。
    """
    paths = []
    env_path = os.environ.get("TJ_ROLES_FILE", "").strip()
    if env_path:
        paths.append(os.path.expanduser(env_path))
    paths.append(os.path.expanduser("~/.tianji/roles.toml"))
    for p in paths:
        try:
            if not os.path.isfile(p):
                continue
            with open(p, "rb") as f:
                raw_bytes = f.read()
            # 先尝试 tomllib 直接解析
            try:
                cfg = tomllib.loads(raw_bytes.decode("utf-8"))
                roles = cfg.get("roles", {})
                if isinstance(roles, dict):
                    result = {}
                    for role, models in roles.items():
                        if isinstance(models, list):
                            result[str(role)] = [str(m) for m in models]
                        elif isinstance(models, str):
                            result[str(role)] = [models]
                    if result:
                        return result
            except Exception:
                pass
            # tomllib 失败,手动解析 [roles] section
            text = raw_bytes.decode("utf-8", errors="replace")
            result = {}
            in_roles = False
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if line.startswith("[") and line.endswith("]"):
                    in_roles = (line[1:-1].strip() == "roles")
                    continue
                if not in_roles or not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if not key:
                        continue
                    if val.startswith("[") and val.endswith("]"):
                        inner = val[1:-1]
                        models = []
                        current = ""
                        in_str = False
                        str_char = None
                        for ch in inner:
                            if in_str:
                                current += ch
                                if ch == str_char:
                                    in_str = False
                            elif ch in '"\'':
                                in_str = True
                                str_char = ch
                                current += ch
                            elif ch == ",":
                                m = current.strip().strip("\"'")
                                if m:
                                    models.append(m)
                                current = ""
                            else:
                                current += ch
                        m = current.strip().strip("\"'")
                        if m:
                            models.append(m)
                        if models:
                            result[key] = models
                    else:
                        m = val.strip("\"'")
                        if m:
                            result[key] = [m]
            if result:
                return result
        except Exception:
            continue
    return None


def _extract_completion_title(events, agent_name):
    """从 events 倒序找该角色最近一条 subagent_start,提取任务标题。"""
    for ev in reversed(events):
        if ev.get("event") == "subagent_start" and ev.get("agent") == agent_name:
            detail = ev.get("detail", {})
            prompt = detail.get("prompt") or ""
            if isinstance(prompt, str):
                for line in prompt.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    line = re.sub(r'^天机[^|｜]{0,8}任务书[|｜]', '', line)
                    for ch in ['#', '*', '`', '|']:
                        line = line.replace(ch, '')
                    line = re.sub(r'\s+', '', line)
                    if len(line) > 20:
                        line = line[:20] + '…'
                    return line
            break
    return None


def pool_models():
    """Read [secondary_model.models] keys from config.toml; [] on any failure."""
    try:
        with open(CONFIG, "rb") as f:
            cfg = tomllib.load(f)
        models = cfg.get("secondary_model", {}).get("models", {})
        return [short_model(k) for k in models]
    except Exception:
        return []


def make_progress_bar(ctx, width=8, breathing=False, second=None):
    """Return colored solid-block progress bar string with ANSI codes, or None if ctx missing.

    width=8, uses █ (filled) and ░ (empty) for utf-8, or # (filled) and - (empty) for fallback.
    If breathing=True and second is given, filled color pulses between dim and bright each second.
    """
    if not isinstance(ctx, (int, float)):
        return None
    pct = max(0.0, min(1.0, ctx))
    filled = round(pct * width)

    # Determine base color and bright variant
    if pct >= 0.80:
        dim_code, bright_code = 31, 91    # red
    elif pct >= 0.50:
        dim_code, bright_code = 33, 93    # yellow
    else:
        dim_code, bright_code = 32, 92    # green

    if breathing and second is not None:
        # Even seconds → dim grade, Odd seconds → bright
        block_color = dim_code if second % 2 == 0 else bright_code
    else:
        block_color = dim_code

    reset = "\033[0m"

    # Choose block characters based on whether stdout can handle utf-8
    if HAS_UTF8_BLOCKS:
        block_filled = "█"
        block_empty = "░"
    else:
        block_filled = "#"
        block_empty = "-"

    bar_content = "{}{}".format(block_filled * filled, block_empty * (width - filled))
    pct_content = " {}%".format(round(pct * 100))
    full_content = "{}{}".format(bar_content, pct_content)
    return "\033[{}m{}\033[0m".format(block_color, full_content)


def visible_len(s):
    """String length excluding ANSI escape sequences."""
    return len(ANSI_ESCAPE.sub("", s))


def truncate_visible(s, max_width):
    """Truncate by visible character width."""
    if visible_len(s) <= max_width:
        return s
    result = ""
    vis = 0
    i = 0
    while i < len(s):
        m = ANSI_ESCAPE.match(s, i)
        if m:
            result += m.group()
            i = m.end()
            continue
        if vis >= max_width - 3:
            result += "...\033[0m"  # 补复位,防截断处颜色渗出
            break
        result += s[i]
        vis += 1
        i += 1
    return result


def rainbow_text(text):
    """Apply per-letter ANSI 256-color smooth rainbow with time-based phase."""
    if not text:
        return text
    phase = (int(time.time()) * 2) % 24
    n = len(text)
    result = ""
    for i, ch in enumerate(text):
        idx = round(i / (n - 1) * 23) if n > 1 else 0
        color = _RAINBOW_PALETTE[(idx + phase) % 24]
        result += "\033[38;5;{}m{}\033[0m".format(color, ch)
    return result


def _colorize_pool_model(name, idx):
    """Return model name with xterm-256 color based on POOL_PALETTE."""
    color = POOL_PALETTE[idx % len(POOL_PALETTE)]
    return "\033[38;5;{}m{}\033[0m".format(color, name)


# ===== 跑马灯新增:wire 配对 + 摘要提取 =======================================

def _tail_wire(wire_path, max_bytes=WIRE_TAIL_BYTES):
    """只读 wire.jsonl 尾部 max_bytes,返回解码文本或 None。"""
    try:
        size = os.path.getsize(wire_path)
    except OSError:
        return None
    try:
        with open(wire_path, "rb") as f:
            if size <= max_bytes:
                f.seek(0)
            else:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def _strip_ws(text):
    """去除全部空白和换行,保留纯文本。"""
    return re.sub(r'\s+', '', text)


def _collect_strings(obj, out):
    """从 JSON 对象递归收集字符串字段值,供身份关键词匹配用。"""
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


def _collect_strings_iter(fh, out, max_lines=_IDENTITY_LINES):
    """从文件逐行迭代,单次 head 扫描同时完成三件事:
    1. 收集全部字符串值(供 identity 关键词匹配);
    2. 记下第一个 time 字段的值(供起始时间 fallback 用);
    3. 记下第一个 modelAlias 值(供 wire 配对模型名用)。
    循环结束后返回 (time, model_alias) 二元组,不提前退出。
    max_lines 与 board.py `_IDENTITY_LINES` 同源,别各自硬编码。
    """
    try:
        wire_start_time = None
        model_alias = None
        for i, raw_line in enumerate(fh):
            if i >= max_lines:
                break
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                if isinstance(obj, str):
                    out.append(obj)
                continue
            # 记下第一个 time 值,不退出循环
            if wire_start_time is None and "time" in obj:
                try:
                    wire_start_time = int(obj["time"])
                except (ValueError, TypeError):
                    pass
            # 记下第一个 modelAlias,不退出循环
            if model_alias is None:
                def _find_alias(o):
                    if not isinstance(o, dict):
                        return None
                    if "modelAlias" in o:
                        return str(o["modelAlias"])
                    for v in o.values():
                        if isinstance(v, dict):
                            r = _find_alias(v)
                            if r:
                                return r
                        elif isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict):
                                    r = _find_alias(item)
                                    if r:
                                        return r
                    return None
                model_alias = _find_alias(obj)
            # 递归收集字符串值(identity 用)
            _collect_strings(obj, out)
        return wire_start_time, model_alias
    except Exception:
        return None, None


def _read_wire_info(wp):
    """Read one wire's pairing metadata; safe to run in a bounded worker."""
    label = os.path.basename(os.path.dirname(wp))
    try:
        agent_num = int(label.split("-")[-1])
    except (ValueError, IndexError):
        agent_num = 9999
    identity = ""
    wire_start_time = None
    model_alias = None
    wire_mtime = 0
    try:
        with open(wp, "r", encoding="utf-8") as fh:
            identity_chunks = []
            wire_start_time, model_alias = _collect_strings_iter(fh, identity_chunks, max_lines=500)
            identity = "".join(identity_chunks) if identity_chunks else ""
            wire_mtime = os.fstat(fh.fileno()).st_mtime
    except Exception:
        pass
    wire_start_dt = None
    if wire_start_time is not None:
        try:
            wire_start_dt = datetime.fromtimestamp(wire_start_time / 1000.0)
        except Exception:
            pass
    return {
        "path": wp, "label": label, "n": agent_num, "identity": identity,
        "start_dt": wire_start_dt, "recent_dt": None, "recent_checked": False,
        "mtime": wire_mtime, "model_alias": model_alias,
    }
def _truncate_snippet(text, max_width=24):
    """截断到可见 max_width 字符,超出加 …。"""
    clean = ANSI_ESCAPE.sub("", str(text))
    if len(clean) <= max_width:
        return clean
    return clean[:max_width - 1] + "…"


def _make_tool_snippet(tool_name, first_arg):
    """从工具名+首个参数生成片段,如 Read board.py / Bash python …"""
    tool_name = tool_name.strip()
    if not first_arg:
        return "{} …".format(tool_name)
    first_arg = str(first_arg).strip()
    if tool_name in ("Read", "Edit", "Write", "Glob", "Grep"):
        # 取路径的 basename
        raw = first_arg.split("?")[0].strip('"').strip("'")
        basename = os.path.basename(raw)
        if not basename:
            basename = first_arg[:16]
        return "{} {}".format(tool_name, basename)
    elif tool_name == "Bash":
        parts = first_arg.split()
        if len(parts) >= 2:
            second = parts[1][:12] + ("…" if len(parts[1]) > 12 else "")
            return "{} {} {}".format(tool_name, parts[0][:8], second)
        elif len(parts) == 1:
            return "{} {}".format(tool_name, parts[0][:16])
        return tool_name
    return "{} …".format(tool_name)


def _extract_snippet(wire_path):
    """从 wire.jsonl 尾部提取最后一条有意义事件的摘要。

    返回字符串片段或 None。
    类型:
      tool.call    → 工具名+首个参数 basename
      content.part(text) → 文本前20字
      llm.response → 文本前20字
      其它          → None (无法提取有意义片段)
    """
    raw = _tail_wire(wire_path)
    if raw is None:
        return None

    lines = raw.splitlines()
    # 从尾部向前扫描,找最后一条有意义事件
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue

        event_type = obj.get("type", "")

        # 工具调用事件
        if event_type == "context.append_loop_event":
            inner = obj.get("event")
            if not isinstance(inner, dict):
                continue
            inner_type = inner.get("type", "")

            if inner_type == "tool.call":
                name = inner.get("name", "?")
                args = inner.get("args")
                if isinstance(args, dict):
                    first_val = None
                    for key in ["command", "path", "input", "content"]:
                        if key in args:
                            first_val = args[key]
                            break
                    if first_val is None and args:
                        first_val = next(iter(args.values()))
                    snippet = _make_tool_snippet(name, first_val)
                    return _truncate_snippet("正在:{}".format(snippet))
                return _truncate_snippet("正在:{}".format(name))

            elif inner_type == "content.part":
                part = inner.get("part")
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text and _strip_ws(text):
                        return _truncate_snippet("正在:{}".format(_strip_ws(text)[:20]))
                # think 类型跳过 (内部思考,不算正在干啥)

            # step.begin / step.end / tool.result 跳过

        # assistant 文本 (llm.response)
        elif event_type == "llm.response":
            text = obj.get("text", "")
            if text and _strip_ws(text):
                return _truncate_snippet("正在:{}".format(_strip_ws(text)[:20]))

        # 其余事件类型跳过

    return None


def _resolve_agent_wire_map(session_id, running, events):
    """为每个 running agent 找到对应的 wire.jsonl,返回 dict: agent -> {mtime, snippet, model_alias}。

    配对逻辑: identity 关键词匹配 → 时间就近 10s fallback。
    只扫描当前 session 目录,保证 300ms 预算。
    全程 fail-open,任何异常返回空 dict。
    """
    if not session_id or not running:
        return {}

    try:
        wire_paths = _find_wire_files(session_id, _WIRE_ROOT)
    except Exception:
        return {}

    if not wire_paths:
        return {}

    # 构建 wire 信息列表——优化:每个 wire 只做一次 head 扫描,
    # 同时提取 identity 文本、wire 起始时间 和 modelAlias,避免三次打开文件。
    workers = min(16, max(1, len(wire_paths)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tianji-wire") as executor:
        wire_infos = list(executor.map(_read_wire_info, wire_paths))

    # 新鲜度优先:候选 wire 按 mtime 倒序,同分时新 wire 先被挑走,
    # 防止同角色的历史 wire(昨天/上周的)抢走正在跑的配对
    wire_infos.sort(key=lambda wi: wi["mtime"], reverse=True)

    agent_names = sorted(running.keys())
    matched = {}  # agent_name -> wire_info
    used = set()
    unmatched = []

    # Phase 1: identity keyword match
    for agent_name in agent_names:
        for j, wi in enumerate(wire_infos):
            if j in used:
                continue
            if _agent_identity_matches(wi["identity"], agent_name):
                matched[agent_name] = wi
                used.add(j)
                break
        else:
            unmatched.append(agent_name)

    # Phase 2: time-nearest fallback (within 10s)
    # 从 events 提取每个 agent 的最新 start_ts(覆盖式=保留最后一次起跑;
    # 同名角色一天跑多趟,钉最早一次会把配对锁死在最老的 wire 上)
    agent_latest_start = {}
    for ev in events:
        if ev.get("event") == "subagent_start" and ev.get("agent"):
            ts = ev.get("ts", "")
            if ts:
                agent_latest_start[ev["agent"]] = ts

    unmatched.sort(key=lambda a: agent_latest_start.get(a, ""))
    agent_start_local = {}
    for agent_name in unmatched:
        ts = agent_latest_start.get(agent_name, "")
        if ts and ts != "--":
            try:
                agent_start_local[agent_name] = datetime.strptime(
                    ts[:19], "%Y-%m-%dT%H:%M:%S"
                )
            except (ValueError, TypeError):
                agent_start_local[agent_name] = None
        else:
            agent_start_local[agent_name] = None

    for agent_name in unmatched:
        if agent_start_local.get(agent_name) is None:
            continue
        best_j = None
        best_diff = None
        for j, wi in enumerate(wire_infos):
            if j in used or wi["start_dt"] is None:
                continue
            diff = abs((agent_start_local[agent_name] - wi["start_dt"]).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_j = j
        if best_j is not None and best_diff <= 10.0:
            matched[agent_name] = wire_infos[best_j]
            used.add(best_j)

    # Phase 3: a resumed agent appends to its original wire archive. Its
    # first timestamp is stale, but the latest event is evidence of the
    # resumed run and can be matched with the same conservative tolerance.
    for agent_name in unmatched:
        if agent_name in matched or agent_start_local.get(agent_name) is None:
            continue
        best_j = None
        best_diff = None
        for j, wi in enumerate(wire_infos[:RESUME_TAIL_CANDIDATES]):
            if not wi["recent_checked"]:
                wi["recent_checked"] = True
                recent_time = _extract_wire_recent_time(wi["path"])
                if recent_time is not None:
                    try:
                        wi["recent_dt"] = datetime.fromtimestamp(recent_time / 1000.0)
                    except Exception:
                        pass
            if j in used or wi["recent_dt"] is None:
                continue
            diff = abs((agent_start_local[agent_name] - wi["recent_dt"]).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_j = j
        if best_j is not None and best_diff <= 10.0:
            matched[agent_name] = wire_infos[best_j]
            used.add(best_j)

    # 对每个 agent 读取 snippet 和 mtime
    # 同时记录 stale 状态 (mtime 距今 > 60s)
    now = time.time()
    result = {}
    for agent_name, wi in matched.items():
        snippet = None
        try:
            snippet = _extract_snippet(wi["path"])
        except Exception:
            pass
        age = now - wi["mtime"] if wi["mtime"] > 0 else float("inf")
        result[agent_name] = {
            "path": wi["path"],
            "mtime": wi["mtime"],
            "age": age,
            "snippet": snippet,
            "model_alias": wi.get("model_alias") or "?",
        }
    return result


def _colorize_name(name, is_stale):
    """按 stale 状态着色: stale 红色常亮,否则彩虹。"""
    if is_stale:
        return "\033[38;5;196m{}\033[0m".format(name)  # 红色
    return rainbow_text(name)


def _colorize_name_stale_aware(name, stale_set, counts):
    """返回着色后的名字+后缀。stale 变红常亮,否则彩虹。"""
    suffix = ""
    cnt = counts.get(name, 1)
    if cnt > 1:
        suffix = " x{}".format(cnt)
    full = name + suffix
    is_stale = name in stale_set
    return _colorize_name(full, is_stale)


def _format_completion_flash(flash_agent, finished_events, wire_map):
    """生成完工闪现字符串: ✓ 角色:模型 完成<任务标题>

    模型只认 wire 配对实证(modelAlias),配不到显示 ?——不拿花名册或主模型充数。
    """
    info = wire_map.get(flash_agent, {})
    model_alias = short_model(info.get("model_alias", "?"))

    title = _extract_completion_title(finished_events, flash_agent)

    if title:
        return " \033[38;5;114m✓ {}:{} 完成{}\033[0m".format(flash_agent, model_alias, title)
    else:
        return " \033[38;5;114m✓ {}:{} 完成\033[0m".format(flash_agent, model_alias)


def main():
    # === 编码解锁 ===
    global HAS_UTF8_BLOCKS
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        HAS_UTF8_BLOCKS = True
    except (AttributeError, LookupError):
        HAS_UTF8_BLOCKS = False

    # 原始输入
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        snap = json.loads(raw) if raw.strip() else {}
    except Exception:
        snap = {}

    # 只吐刻度:Command Code 的底栏行是宿主 mod 自己拼的,但它显示的数字必须和
    # kimi 那边同一份口径——所以这里把刻度单独开一个出口,谁拼行谁调用,不复制规则。
    # 口径里包含时间:合成在 meter_reading 里做一次,这里不另起一套判断,
    # 否则 mod 那行永远等不到时间告警(时间告警曾经只长在带颜色的 budget_meter 上)。
    if "--meter" in sys.argv:
        cwd = snap.get("cwd") or os.getcwd()
        session_id = snap.get("sessionId") or snap.get("session_id", "")
        try:
            text, _ = meter_reading(
                read_ledger(cwd) or [], read_budgets(cwd, session_id),
                read_time_limits(cwd, session_id),
                call_limits=read_call_limits(cwd, session_id))
        except Exception:
            text = ""
        print(text)
        return

    now = time.time()
    current_second = int(now)

    # 原始信息
    model = short_model(snap.get("model") or "?")
    ctx = snap.get("contextUsage")
    git = snap.get("gitBranch") or ""
    cwd = snap.get("cwd") or os.getcwd()

    # 天机运行状态
    events = read_state(cwd)
    running = {}
    completed = 0
    last_agent = None
    last_time = None
    start_ts = None
    agent_start_times = {}
    if events:
        running, completed, last_agent, last_time = analyze(events)
        for ev in reversed(events):
            if ev.get("event") == "subagent_start" and ev.get("agent"):
                a = ev["agent"]
                if a not in agent_start_times:
                    agent_start_times[a] = ev.get("ts", "")
        for ev in reversed(events):
            if ev.get("event") == "subagent_start":
                start_ts = ev.get("ts", "")
                break

    has_running = bool(running)

    # === 花名册 ===
    roles_map = _read_roles_toml(cwd)

    # === 跑马灯: 配线 + 摘要 + 抽风检测 ===
    # 注意:TUI 快照的键是驼峰 sessionId(实测 0.40.1);state.jsonl 台账里是蛇形 session_id,两处不一样
    session_id = snap.get("sessionId") or snap.get("session_id", "")
    wire_map = {}  # agent -> {mtime, age, snippet, model_alias}
    stale_set = set()

    # === 花费刻度: 本次会话花了多少 / 顶是多少 ===
    # 读整份账本,不是尾部窗口:合计会随文件增长"缩水",而窗口读法看不见这件事。
    # 只在有派发在跑时画:空闲时没有"此刻正在超"可言,刻度是运行时的仪表,
    # 摆在那里只是个要读过去的数字(Command Code 那边的 mod 同此规则)。
    if has_running:
        try:
            meter, meter_warning = budget_meter(
                read_ledger(cwd) or [], read_budgets(cwd, session_id),
                read_time_limits(cwd, session_id),
                call_limits=read_call_limits(cwd, session_id))
        except Exception:
            meter, meter_warning = "", False
    else:
        meter, meter_warning = "", False

    if has_running:
        try:
            wire_map = _resolve_agent_wire_map(session_id, running, events)
        except Exception:
            wire_map = {}

        for agent_name in running:
            info = wire_map.get(agent_name)
            if info and info["age"] > 60:
                stale_set.add(agent_name)

        # 死人清场:stop 事件缺失(被掐/崩溃/补账漏)的 agent 不能永远挂在状态栏。
        # 配到 wire 但 600s 没写,或配不到 wire 且最近起跑已超 600s → 视为已死
        for agent_name in list(running.keys()):
            info = wire_map.get(agent_name)
            if info is not None:
                if info["age"] > 600:
                    del running[agent_name]
                    stale_set.discard(agent_name)
            else:
                ts0 = agent_start_times.get(agent_name, "")
                try:
                    if ts0 and now - datetime.fromisoformat(ts0).timestamp() > 600:
                        del running[agent_name]
                except (ValueError, TypeError):
                    pass
        has_running = bool(running)

    # === 完工闪现 ===
    completion_flash = None
    last_stop_agent = None
    last_stop_time = None
    if events:
        for ev in reversed(events):
            if ev.get("event") == "subagent_stop" and ev.get("agent"):
                last_stop_agent = ev["agent"]
                last_stop_time = ev.get("ts", "")
                break
        if last_stop_agent and last_stop_time:
            try:
                stop_dt = datetime.fromisoformat(last_stop_time)
                flash_age = now - stop_dt.timestamp()
                if 0 <= flash_age <= 5.0:
                    completion_flash = last_stop_agent
            except (ValueError, TypeError):
                pass

    # 完工角色的 wire 配对(拿真实 modelAlias);已退出 running 的也能配
    if completion_flash and completion_flash not in wire_map:
        try:
            wire_map.update(
                _resolve_agent_wire_map(session_id, {completion_flash: 1}, events)
            )
        except Exception:
            pass

    # === 进度条(without breathing) ===
    sep_color = "\033[38;5;240m"
    reset = "\033[0m"
    sep_text = "{} | {}".format(sep_color, reset)

    bar = make_progress_bar(ctx, width=8)
    if bar is not None:
        ctx_part = " " + bar
    elif isinstance(ctx, (int, float)):
        ctx_part = " {}%".format(round(ctx * 100))
    else:
        ctx_part = ""

    # 头部: 亮青主模型 + 进度条 | 目录 | git分支
    dir_basename = os.path.basename(os.path.normpath(cwd))
    head = "\033[1;96m{}\033[0m{}".format(model, ctx_part)
    head += "{}{}{}".format(sep_text, dir_basename, reset)
    if git:
        head += "{}\033[38;5;75m{}\033[0m".format(sep_text, git)

    # 轮换索引
    running_list = sorted(running.keys()) if running else []
    cycle_idx = int(now) % len(running_list) if running_list else 0

    if has_running:
        total_run = sum(running.values())

        # 耗时 mm:ss / h:mm
        elapsed_str = ""
        if start_ts:
            try:
                start_dt = datetime.fromisoformat(start_ts)
                delta = now - start_dt.timestamp()
                total_sec = max(0, int(delta))
                h = total_sec // 3600
                m = (total_sec % 3600) // 60
                s = total_sec % 60
                if h > 0:
                    elapsed_str = " {}{}:{:02d}{}".format(sep_color, h, m, reset)
                else:
                    elapsed_str = " {:02d}:{:02d}".format(m, s)
            except Exception:
                elapsed_str = " 00:00"

        # agent:N [模型名,模型名] mm:ss
        run_label_color = [226, 214, 208][current_second % 3]
        run_tag_str = "\033[1;38;5;{}magent:{}\033[0m".format(run_label_color, total_run)

        # 模型名(彩虹, stale红, xN 后缀)
        names_parts = []
        for name in running_list:
            info2 = wire_map.get(name, {})
            model_alias = short_model(info2.get("model_alias", "?"))
            named_colored = _colorize_name_stale_aware(model_alias, stale_set, running)
            names_parts.append(named_colored)
        names_str = ",".join(names_parts)
        names_part = "[{}]".format(names_str)

        # 正在段: 完工闪现 vs 轮播
        doing_str = ""
        if completion_flash:
            doing_str = _format_completion_flash(
                completion_flash, finished_events=events, wire_map=wire_map
            )
        elif wire_map:
            current_agent = running_list[cycle_idx]
            info2 = wire_map.get(current_agent)
            if info2 and info2.get("snippet"):
                # ▶ 段显示当前任务标题(取该角色最新 start 的 prompt 首行),
                # 取不到才退回模型名——模型名已经在 agent:N[...] 段有了,不重复占宽
                title = _extract_completion_title(events, current_agent) \
                        or short_model(info2.get("model_alias", "?"))
                doing_str = " \033[38;5;248m▶ [{}]:{}\033[0m".format(
                    title, info2["snippet"]
                )

        tj = sep_text + run_tag_str + names_part + elapsed_str + doing_str
    else:
        # 待命态: 花名册或池菜单 fallback
        tj = ""

        # 完工闪现优先显示
        flash_str = ""
        if completion_flash:
            flash_str = _format_completion_flash(
                completion_flash, finished_events=events, wire_map=wire_map
            )

        if flash_str:
            # 闪现顶替花名册/池菜单,避免超宽被截断
            tj = sep_text + flash_str
        elif roles_map:
            # 花名册: tianji(工人:模型,模型|审核:模型|裁判:模型)
            role_parts = []
            for role_name, model_list in roles_map.items():
                # 多模型只显示主力(第一个)+ "+N" 角标,防超宽截断
                first = short_model(model_list[0]) if model_list else "?"
                # "主模型" 是活绑定:跟随当前会话主模型,显示解析后的具体模型名
                if model_list and model_list[0] == "主模型":
                    first = model
                extra = "+{}".format(len(model_list) - 1) if len(model_list) > 1 else ""
                role_parts.append("{}:{}{}".format(role_name, first, extra))
            roles_joined = "\033[38;5;240m|\033[0m".join(role_parts)
            roles_str = "\033[38;5;114mtianji\033[0m({})".format(roles_joined)
            tj = sep_text + roles_str
        else:
            pool = pool_models()
            if pool:
                pool_parts = []
                for idx, name in enumerate(pool):
                    color = POOL_PALETTE[idx % len(POOL_PALETTE)]
                    pool_parts.append("\033[38;5;{}m{}\033[0m".format(color, name))
                comma_sep = "\033[38;5;240m,\033[0m".join(pool_parts)
                pool_str2 = "tianji:{}".format(comma_sep)
                tj = sep_text + pool_str2

    output = compose_line(head, tj, meter, meter_warning, sep_text)
    print(truncate_visible(output, MAX_LINE_WIDTH))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            print("")
            sys.stdout.flush()
        except Exception:
            pass
        sys.exit(0)
