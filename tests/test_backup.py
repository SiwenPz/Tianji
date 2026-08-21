"""票15 备份(18.5): 每日一次账本快照+保留轮换+env 覆盖。"""

import json
import os
from pathlib import Path

from tianji import daemon, ops
from tianji.db import connect


def _write_ledger(tianji_home):
    """确保账本文件存在(有内容)。"""
    conn = connect()
    ops.ensure_defaults(conn)
    conn.close()
    return Path(daemon.ledger_path())


def test_backup_daily_once_per_day(tianji_home):
    """每日一次: 同日二次调用跳过(already=True)。"""
    _write_ledger(tianji_home)
    r1 = daemon.backup_ledger(today="20260818", backup_dir=tianji_home / "bk")
    assert r1["ok"] is True and r1["already"] is False
    assert Path(r1["file"]).exists()
    assert r1["file"].endswith("ledger-20260818.db")
    r2 = daemon.backup_ledger(today="20260818", backup_dir=tianji_home / "bk")
    assert r2["ok"] is True and r2["already"] is True


def test_backup_default_dir_and_keep(tianji_home):
    """默认目录=TIANJI_HOME/backups,默认保留 7 份。"""
    _write_ledger(tianji_home)
    r = daemon.backup_ledger(today="20260818")
    assert Path(r["file"]).parent == tianji_home / "backups"
    assert r["keep"] == 7


def test_backup_rotate_keep_env(tianji_home, monkeypatch):
    """构造日期推进: 生成多份,保留最近 keep 份,旧份被轮换删除。"""
    _write_ledger(tianji_home)
    monkeypatch.setenv("TIANJI_BACKUP_KEEP", "2")
    files = []
    for day in ("20260814", "20260815", "20260816"):
        r = daemon.backup_ledger(today=day, backup_dir=tianji_home / "bk2")
        assert r["keep"] == 2
        files.append(r["file"])
    # 第三份生成后,最旧的 20260814 被轮换删除
    remaining = sorted(p.name for p in (tianji_home / "bk2").glob("ledger-*.db"))
    assert remaining == ["ledger-20260815.db", "ledger-20260816.db"]
    assert not Path(files[0]).exists()
    assert Path(files[1]).exists() and Path(files[2]).exists()


def test_backup_dir_env_override(tianji_home, monkeypatch):
    """备份目录 env 覆盖生效。"""
    _write_ledger(tianji_home)
    custom = tianji_home / "my-backups"
    monkeypatch.setenv("TIANJI_BACKUP_DIR", str(custom))
    r = daemon.backup_ledger(today="20260818")
    assert Path(r["file"]).parent == custom
    assert custom.exists()


def test_backup_audit_and_data_equal(tianji_home):
    """备份落盘内容=账本(复制即备份),并留 audit 行。"""
    conn = connect()
    ops.ensure_defaults(conn)
    # 写一条数据确保账本非空
    conn.execute("INSERT INTO configs (key, value, updated_at) VALUES (?,?,?)",
                 ("x-dup-check", "v", daemon.now()))
    conn.close()
    r = daemon.backup_ledger(today="20260818",
                             backup_dir=tianji_home / "bk4",
                             conn=connect())
    # audit 行
    conn = connect()
    row = conn.execute(
        "SELECT detail FROM audit WHERE action='backup_done'").fetchone()
    assert row is not None
    assert json.loads(row["detail"])["keep"] == 7
    conn.close()
    # 复制内容一致(数据行存在)
    import sqlite3
    c = sqlite3.connect(r["file"])
    try:
        v = c.execute("SELECT value FROM configs WHERE key='x-dup-check'").fetchone()
        assert v is not None and v[0] == "v"
    finally:
        c.close()
