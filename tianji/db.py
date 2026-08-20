"""账本连接与路径: TIANJI_HOME 为根派生账本/工作目录,零配置文件(18.1)。"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .schema import render_schema, DISPATCH_STATES, MSG_TYPES


def tianji_home() -> Path:
    return Path(os.environ.get("TIANJI_HOME") or Path.home() / ".tianji")


def ledger_path() -> Path:
    return tianji_home() / "ledger.db"


def task_dir(dispatch_id: int) -> Path:
    """每任务一目录: <TIANJI_HOME>/tasks/<dispatch_id>/(16.1)。"""
    return tianji_home() / "tasks" / str(dispatch_id)


def connect() -> sqlite3.Connection:
    home = tianji_home()
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ledger_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 多进程写(总控 CLI/worker ingest/监控器常驻) 并发,WAL 下写锁冲突
    # busy_timeout 让 BEGIN IMMEDIATE 等锁而不是立刻抛 database is locked
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(render_schema())
    # 表重建迁移期临时关外键(老表可能引用不存在的行;迁移完恢复)
    conn.execute("PRAGMA foreign_keys=OFF")
    _migrate(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection):
    """轻量 schema 迁移: 已有表缺列则补(票 18 四元化+两轴;票 22 offline_suspicion;票 04 双轴;票 06 能力画像)。

    既有账本(如 demo 账本)不含新增列,CREATE TABLE IF NOT EXISTS 不补列,
    监控器 _tick 直接引用会 OperationalError(2026-08-17 审核实证)。
    """
    _add_column_if_missing(conn, "instances", "key_name",
                           "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "ability_profiles", "key_name",
                           "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "ability_profiles", "skills",
                           "TEXT NOT NULL DEFAULT '[]'")
    _add_column_if_missing(conn, "ability_profiles", "permission_granularity",
                           "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "ability_profiles", "context_window",
                           "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "ability_profiles", "score",
                           "REAL NOT NULL DEFAULT 60")
    _add_column_if_missing(conn, "ability_profiles", "model_source_score",
                           "REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "ability_profiles", "key_body_score",
                           "REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "ability_profiles", "notes",
                           "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "ability_profiles", "score_history",
                           "TEXT NOT NULL DEFAULT '[]'")
    _add_column_if_missing(conn, "instance_registrations", "offline_suspicion",
                           "INTEGER NOT NULL DEFAULT 0")
    # 票 04: dispatches.axis + tasks.architect_verdict
    _add_column_if_missing(conn, "dispatches", "axis",
                           "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "tasks", "architect_verdict",
                           "TEXT NOT NULL DEFAULT ''")
    # 票 20: ability_profiles 实例档案求助记录
    _add_column_if_missing(conn, "ability_profiles", "help_count",
                           "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "ability_profiles", "last_help_at",
                           "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "ability_profiles", "last_help_claim",
                           "TEXT NOT NULL DEFAULT ''")
    # 票 27: ability_profiles 实例档案续推(nudge)记录(7.5 续推通道,供参考不设上限)
    _add_column_if_missing(conn, "ability_profiles", "nudge_count",
                           "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "ability_profiles", "last_nudge_at",
                           "INTEGER NOT NULL DEFAULT 0")
    # 票 05: worktree_path(须在 dispatches CHECK 重建之前补列,
    # 否则重建按新 schema 建表会丢该列数据)
    _add_column_if_missing(conn, "dispatches", "worktree_path",
                           "TEXT NOT NULL DEFAULT ''")
    # 票 20: messages 表 CHECK 白名单过期则表重建
    _migrate_table_check(conn, "messages", MSG_TYPES, "seq")
    # 票 19 补: dispatches 表 CHECK 七态过期则表重建(cancelled 老账本缺失,
    # 2026-08-18 实证: demo 账本强制干预改派被 CHECK 拒)
    _migrate_table_check(conn, "dispatches", DISPATCH_STATES, "id")
    # 票 21 补: tasks 表加改动边界声明列(干偏护栏 8.3/11.2)
    _add_column_if_missing(conn, "tasks", "scope_guard",
                           "TEXT NOT NULL DEFAULT ''")
    # 票 26 补: instances 表加显示模式/默认思考级别(15.8/13.3)
    _add_column_if_missing(conn, "instances", "display_mode",
                           "TEXT NOT NULL DEFAULT '前台'")
    _add_column_if_missing(conn, "instances", "thinking_level",
                           "TEXT NOT NULL DEFAULT ''")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str,
                           coltype: str):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {r["name"] for r in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _migrate_table_check(conn: sqlite3.Connection, table: str,
                         whitelist: tuple, pk_col: str):
    """表 CHECK 白名单过期则表重建(票 20 messages;票 19 dispatches cancelled)。

    从 sqlite_master 读表 DDL,白名单值任一缺失则按当前 render_schema()
    重建表(建 new→拷数据→drop→rename,保主键序列)。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    if row is None or not row["sql"]:
        return
    ddl = row["sql"]
    missing = [t for t in whitelist if f"'{t}'" not in ddl]
    if not missing:
        return
    cols = [r["name"] for r in conn.execute(
        f"PRAGMA table_info({table})").fetchall()]
    conn.execute("BEGIN EXCLUSIVE")
    try:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        for stmt in render_schema().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        # 新 schema 里老表缺的列补字面值默认(TEXT→'',INTEGER→0),
        # 否则 NOT NULL 无默认列(dcap_hash 等)会让拷贝失败(2026-08-18 实证)
        # 新 schema 里老表缺的列: 优先用列默认值(过 CHECK,如 worker_role
        # DEFAULT 'worker'),无默认值按类型补 ''或 0(dcap_hash 等)
        new_cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        select_parts = []
        for c in new_cols:
            if c["name"] in cols:
                select_parts.append(c["name"])
            elif c["dflt_value"] is not None:
                select_parts.append(c["dflt_value"])
            else:
                select_parts.append(
                    "''" if c["type"].startswith("TEXT") else "0")
        new_collist = ", ".join(c["name"] for c in new_cols)
        conn.execute(
            f"INSERT INTO {table} ({new_collist})"
            f" SELECT {', '.join(select_parts)} FROM {table}_old")
        conn.execute(f"DROP TABLE {table}_old")
        conn.execute(
            f"UPDATE sqlite_sequence SET seq = (SELECT MAX({pk_col}) FROM {table})"
            f" WHERE name='{table}'")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def now() -> int:
    """epoch 秒。独立函数便于测试注入。"""
    import time
    return int(time.time())


@contextmanager
def tx(conn: sqlite3.Connection):
    """单事务(一次操作一个事务: 状态迁移+审计+回执一次提交)。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row) if row is not None else None
