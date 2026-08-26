"""档 2 转录文件解析公共框架(6.3): 增量解析 + 每文件独立游标 + 归一化事件进账本。

- 读哪个文件: 由壳模板 transcript.glob 模式决定。
- 怎么解析: 逐行 JSONL,通过壳模板 translate 归一化。
- 每文件独立游标: 每文件一个字节偏移,只处理新增内容。
- 解析产物进同一事件表(与档 1 同表不另造)。
- 派生状态按 seq 单调更新,乱序旧事件不覆盖(由 ingest_event 保证)。
- 解析补事件语义: 与监控器只计数各取所需(7.2)。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from tianji import events
from tianji.db import now, tx
from tianji.adapters import template as tpl_mod


# ---------------------------------------------------------------------------
# 转录文件定位
# ---------------------------------------------------------------------------

def transcript_path(shell: str, session_id: str, home_dir: "Path | None" = None) -> Path | None:
    """按壳类型返回转录文件路径(不解析内容)。"""
    if not session_id:
        return None
    home = home_dir or Path.home()
    tpl = tpl_mod.get_template(shell)
    cfg = tpl.transcript
    path_kind = cfg.get("path", shell)

    if path_kind == "codex":
        return _codex_path(session_id, home)
    if path_kind == "dsh":
        return _dsh_path(session_id, home)
    if path_kind == "kimi":
        return _kimi_path(session_id, home)
    if path_kind == "atomcode":
        return _atomcode_path(session_id, home)
    if path_kind == "cline":
        return _cline_path(session_id, home)
    # claude / default
    return _claude_path(session_id, home)


def _claude_path(session_id: str, home: Path) -> Path | None:
    hits = list(home.glob(f".claude/projects/*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _codex_path(session_id: str, home: Path) -> Path | None:
    base = home / ".codex" / "sessions"
    if not base.is_dir():
        return None
    hits = list(base.glob(f"**/rollout-{session_id}.jsonl"))
    return hits[0] if hits else None


def _dsh_path(session_id: str, home: Path) -> Path | None:
    dsh_home = Path(os.environ.get("DSH_HOME", str(home / ".dsh")))
    base = dsh_home / "sessions"
    if not base.is_dir():
        return None
    for suffix in ("session.jsonl.zstd", "session.jsonl"):
        hits = list(base.glob(f"*/{session_id}/{suffix}"))
        if hits:
            return hits[0]
    return None


def _kimi_path(session_id: str, home: Path) -> Path | None:
    kimi_home = Path(os.environ.get("KIMI_HOME", str(home / ".kimi")))
    candidates = (
        kimi_home / "wire.jsonl",
        kimi_home / "wire" / "wire.jsonl",
        kimi_home / "wire" / f"wire-{session_id}.jsonl",
        kimi_home / f"wire-{session_id}.jsonl",
    )
    return next((p for p in candidates if p.is_file()), None)


def _atomcode_path(session_id: str, home: Path) -> Path | None:
    """atomcode 档 2 转录: sessions/<hex>/<uuid>.jsonl 按轮追加解析。

    多实例隔离 = ATOMCODE_HOME。
    <hex> = 实例隔离目录哈希; <uuid> = 会话 UUID。
    按轮追加: 同一文件增量写入,解析侧用独立游标推进。
    """
    atomcode_home = Path(os.environ.get("ATOMCODE_HOME", str(home / ".atomcode")))
    base = atomcode_home / "sessions"
    if not base.is_dir():
        return None
    # <hex> 子目录下 <uuid>.jsonl, uuid 即 session_id
    hits = list(base.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _cline_path(session_id: str, home: Path) -> Path | None:
    cline_home = Path(os.environ.get("CLINE_HOME", str(home / ".cline")))
    candidates = (
        cline_home / "data" / "db" / "sessions.db",
        cline_home / "sessions" / "sessions.db",
        cline_home / "sessions.db",
    )
    return next((p for p in candidates if p.is_file()), None)


# ---------------------------------------------------------------------------
# 增量解析
# ---------------------------------------------------------------------------

def parse_incremental(
    conn: sqlite3.Connection,
    env: dict,
    shell: str,
    session_id: str,
    home_dir: "Path | None" = None,
) -> dict:
    """增量解析壳转录文件,归一化事件进账本(6.3)。

    参数:
        conn: 账本连接。
        env: 身份环境变量。
        shell: 壳名(模板注册名)。
        session_id: 会话 ID。
        home_dir: 可选 home 目录覆盖(测试用)。

    返回:
        解析摘要 {session_id, files_processed, events_emitted, new_bytes}。
    """
    tpl = tpl_mod.get_template(shell)
    path = transcript_path(shell, session_id, home_dir=home_dir)
    if path is None or not path.is_file():
        return {"session_id": session_id, "files_processed": 0,
                "events_emitted": 0, "new_bytes": 0}

    summary = {"session_id": session_id, "files_processed": 0,
               "events_emitted": 0, "new_bytes": 0}
    if tpl.transcript.get("source_type") == "sqlite":
        _process_sqlite(conn, env, tpl, path, session_id, summary)
    else:
        _process_file(conn, env, tpl, path, session_id, summary)
    # 档 2 权威校验源(票 09): wire.jsonl 对档 1 缺口兜底纠偏,不只是一句声明
    if tpl.transcript.get("authoritative_source") == "wire":
        _wire_reconcile(conn, env, tpl, path, session_id, summary)
    return summary


def parse_transcript(
    conn: sqlite3.Connection,
    env: dict,
    shell: str,
    session_id: str,
    home_dir: "Path | None" = None,
) -> dict:
    """解析单壳单会话转录(供 CLI 调用)。"""
    return parse_incremental(conn, env, shell, session_id, home_dir=home_dir)


def _cursor_key(shell: str, path: str) -> str:
    return f"transcript:{shell}:{path}"


def _read_cursor(conn: sqlite3.Connection, key: str) -> dict:
    row = conn.execute(
        "SELECT last_seq FROM cursors WHERE consumer_id=?", (key,)
    ).fetchone()
    if row is None:
        return {"path": "", "offset": 0}
    try:
        return json.loads(row["last_seq"])
    except (json.JSONDecodeError, TypeError):
        return {"path": "", "offset": 0}


def _write_cursor(
    conn: sqlite3.Connection, key: str, path: str, offset: int,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cursors (consumer_id, last_seq) VALUES (?,?)",
        (key, json.dumps({"path": path, "offset": offset})),
    )


def _process_file(
    conn: sqlite3.Connection,
    env: dict,
    tpl: tpl_mod.Template,
    path: Path,
    session_id: str,
    summary: dict,
) -> None:
    """处理单个 JSONL 转录文件(增量,从上次游标偏移继续)。"""
    str_path = str(path)
    cursor_key = _cursor_key(tpl.name, str_path)
    cur = _read_cursor(conn, cursor_key)
    if cur.get("path") != str_path:
        cur = {"path": str_path, "offset": 0}

    file_size = path.stat().st_size
    if file_size <= cur["offset"]:
        return

    new_bytes = file_size - cur["offset"]
    lines = _read_new_lines(path, cur["offset"])
    cur["offset"] = file_size

    events_count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = tpl_mod.translate(tpl.name, raw)
        if event is None:
            continue
        event["session_id"] = session_id
        try:
            events.ingest_event(conn, env, event)
            events_count += 1
        except events.EventError:
            pass

    with tx(conn) as c:
        _write_cursor(c, cursor_key, str_path, cur["offset"])

    summary["files_processed"] += 1
    summary["events_emitted"] += events_count
    summary["new_bytes"] += new_bytes


# cline 完成态集合(除 running 外 sessions.db 的终态;中间态不能一律当完成)
_CLINE_FINAL_STATUSES = {
    "completed", "finished", "succeeded", "done",
    "aborted", "error", "rejected",
}


def _pid_alive(pid: int) -> bool | None:
    """档 3 进程活性判定: True=存活, False=已退出, None=无法判定(pid 无效)。"""
    if not pid or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但无权访问 → 保守按存活
        return True
    except OSError:
        return None


def _cline_db_event(row: sqlite3.Row) -> dict | None:
    """cline 档 2 完成判定: status/exit_code/ended_at + 进程退出为主,TaskComplete hook 为辅。

    主判据: status 非 running 且属终态 + exit_code/ended_at 佐证 + 进程已退出;
    任一不满足(中间态/佐证缺失/进程仍存活)都返回 None——非 running 不能一律当完成。
    """
    status = row["status"]
    if status == "running":
        return {"hook_event_name": "TaskStart", "taskId": row["session_id"],
                "status": status, "updated_at": row["updated_at"]}
    if status not in _CLINE_FINAL_STATUSES:
        return None  # 中间态(pending/queued/paused 等),不判完成
    has_evidence = row["exit_code"] is not None or bool(row["ended_at"])
    if not has_evidence:
        return None  # 无 exit_code/ended_at 佐证,不能当完成
    if _pid_alive(row["pid"]) is True:
        return None  # 进程仍存活未退出,不能断言完成
    return {"hook_event_name": "TaskComplete", "taskId": row["session_id"],
            "status": status, "updated_at": row["updated_at"]}


def _process_sqlite(
    conn: sqlite3.Connection,
    env: dict,
    tpl: tpl_mod.Template,
    path: Path,
    session_id: str,
    summary: dict,
) -> None:
    """处理 SQLite 状态源,按行更新时间做增量同步。

    完成判定以 sessions.db 的 status/exit_code/ended_at + 进程退出为主(6.4):
    游标带 deferred 标记——终态但进程仍存活时保留游标,进程退出后复查补判完成。
    """
    str_path = str(path)
    cursor_key = _cursor_key(tpl.name, str_path)
    cur = _read_cursor(conn, cursor_key)
    last_updated = str(cur.get("updated_at") or "")
    deferred = bool(cur.get("deferred"))

    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    try:
        row = src.execute(
            "SELECT session_id, status, updated_at, exit_code, ended_at, pid"
            " FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
    finally:
        src.close()

    if row is None:
        return
    same_row = bool(last_updated and row["updated_at"] <= last_updated)
    if same_row and not deferred:
        return

    raw = _cline_db_event(row)
    if raw is None:
        # 不可判完成: 终态但进程仍存活 → 保留 deferred 游标,退出后复查;
        # 中间态/佐证不足 → 该状态已消费,推进游标(后续终态 updated_at 更新会再触发)
        if row["status"] in _CLINE_FINAL_STATUSES and _pid_alive(row["pid"]) is True:
            with tx(conn) as c:
                c.execute(
                    "INSERT OR REPLACE INTO cursors (consumer_id, last_seq) VALUES (?,?)",
                    (cursor_key, json.dumps({
                        "path": str_path, "updated_at": row["updated_at"], "deferred": True,
                    })),
                )
            return
        if same_row:
            return
        with tx(conn) as c:
            c.execute(
                "INSERT OR REPLACE INTO cursors (consumer_id, last_seq) VALUES (?,?)",
                (cursor_key, json.dumps({
                    "path": str_path, "updated_at": row["updated_at"],
                })),
            )
        return

    event = tpl_mod.translate(tpl.name, raw)
    if event is None:
        return
    event["session_id"] = session_id
    event["payload"] = {
        **event.get("payload", {}),
        "db_status": row["status"],
        "db_updated_at": row["updated_at"],
        "db_exit_code": row["exit_code"],
        "db_ended_at": row["ended_at"],
        "db_pid": row["pid"],
    }
    try:
        events.ingest_event(conn, env, event)
        events_count = 1
    except events.EventError:
        events_count = 0

    with tx(conn) as c:
        c.execute(
            "INSERT OR REPLACE INTO cursors (consumer_id, last_seq) VALUES (?,?)",
            (cursor_key, json.dumps({
                "path": str_path, "updated_at": row["updated_at"],
            })),
        )

    summary["files_processed"] += 1
    summary["events_emitted"] += events_count
    summary["new_bytes"] += path.stat().st_size


def _read_new_lines(path: Path, offset: int) -> list[str]:
    """从文件偏移量处读取新行(不加载整个文件到内存)。"""
    lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        for line in f:
            lines.append(line)
    return lines


def _read_last_json(path: Path) -> dict | None:
    """读 JSONL 文件最后一行并解析(权威校验用,轻量,不读全文)。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            pos = size - 1
            while pos > 0:
                f.seek(pos - 1)
                if f.read(1) == b"\n":
                    break
                pos -= 1
            f.seek(pos)
            last = f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    if not last:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


def _wire_reconcile(
    conn: sqlite3.Connection,
    env: dict,
    tpl: tpl_mod.Template,
    path: Path,
    session_id: str,
    summary: dict,
) -> None:
    """档 2 wire.jsonl 权威校验(票 09): 以 wire 最新行为准,对档 1 缺口兜底纠偏。

    权威校验规则:
    - wire 最新行是完成事件(SessionEnd/Stop)而账本未 done → 补正(档 1 缺口兜底);
    - wire 最新行是非完成事件而账本已 done → 纠偏回运行(档 1 误报完成,以 wire 为准);
    - 两者一致 → 已同步,不重复写。
    幂等: 纠偏/补正后账本状态与 wire 权威一致,后续 parse 不再触发。
    """
    last = _read_last_json(path)
    if last is None:
        return
    event = tpl_mod.translate(tpl.name, last)
    if event is None:
        return
    event["session_id"] = session_id
    wire_done = tpl_mod.is_completion_event(tpl, event["event_type"])

    row = conn.execute(
        "SELECT state FROM session_states WHERE session_id=?", (session_id,)
    ).fetchone()
    cur_state = row["state"] if row else None

    if wire_done:
        if cur_state == "done":
            return  # 账本已同步 wire 的完成结论
        # 档 1 缺口: wire 权威判完成 → 补正
    else:
        if cur_state != "done":
            return  # 账本未完成且 wire 也显示运行中,一致
        # 档 1 误报完成: wire 显示仍在运行 → 以 wire 为准纠偏回运行
    try:
        events.ingest_event(conn, env, event)
        summary["events_emitted"] += 1
    except events.EventError:
        pass
