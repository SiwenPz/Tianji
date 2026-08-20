"""票15 daemon(18.2/18.3/7.1): start/stop/status 全流程+崩溃自动重拉+审计行。

规则: 真实拉起子进程(隔离 TIANJI_HOME),测试结束必须 stop 清理,防残留进程。
"""

import socket
import time

import pytest

from tianji import daemon, ops
from tianji.db import connect


@pytest.fixture
def dconn(tianji_home):
    conn = connect()
    ops.ensure_defaults(conn)
    yield conn
    conn.close()


def _wait_for(predicate, timeout=12.0):
    """轮询等待谓词为真。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_daemon_start_status_stop(tianji_home, dconn):
    """验收 1: start 拉起监控器+驾驶舱,status 可见,stop 全部停止。"""
    r = daemon.daemon_start(interval=1, web_port=8801)
    assert r["ok"] is True
    try:
        assert _wait_for(lambda: daemon.daemon_status()["running"])
        assert _wait_for(lambda: daemon.daemon_status()["monitor_alive"])
        assert _wait_for(lambda: daemon.daemon_status()["web_alive"])
        st = daemon.daemon_status()
        assert st["web_port"] > 0
    finally:
        daemon.daemon_stop()
    st = daemon.daemon_status()
    assert st["running"] is False
    assert st["monitor_alive"] is False
    assert st["web_alive"] is False


def test_monitor_crash_auto_relaunch(tianji_home, dconn):
    """验收 2: 杀监控器进程→自动重拉(探活周期内),审计行记'监控器重启',无共享文件。"""
    r = daemon.daemon_start(interval=1, web_port=8802)
    assert r["ok"] is True
    try:
        assert _wait_for(lambda: daemon.daemon_status()["monitor_alive"])
        old_pid = daemon.daemon_status()["monitor_pid"]
        daemon._kill_pid(old_pid)

        def _relaunched():
            if not daemon.daemon_status()["monitor_alive"]:
                return False
            if daemon.daemon_status()["monitor_pid"] == old_pid:
                return False
            row = dconn.execute(
                "SELECT detail FROM audit WHERE action='monitor_restart'"
                " ORDER BY id DESC LIMIT 1").fetchone()
            return row is not None and str(old_pid) in row["detail"]
        assert _wait_for(_relaunched, timeout=15)
        new_pid = daemon.daemon_status()["monitor_pid"]
        assert new_pid != old_pid and new_pid > 0
    finally:
        daemon.daemon_stop()


def test_web_crash_auto_relaunch(tianji_home, dconn):
    """验收 3: 驾驶舱进程崩溃同样自动重拉。"""
    r = daemon.daemon_start(interval=1, web_port=8803)
    assert r["ok"] is True
    try:
        assert _wait_for(lambda: daemon.daemon_status()["web_alive"])
        old_pid = daemon.daemon_status()["web_pid"]
        daemon._kill_pid(old_pid)

        def _relaunched():
            if not daemon.daemon_status()["web_alive"]:
                return False
            if daemon.daemon_status()["web_pid"] == old_pid:
                return False
            row = dconn.execute(
                "SELECT detail FROM audit WHERE action='web_restart'"
                " ORDER BY id DESC LIMIT 1").fetchone()
            return row is not None and str(old_pid) in row["detail"]
        assert _wait_for(_relaunched, timeout=15)
        assert daemon.daemon_status()["web_pid"] != old_pid
    finally:
        daemon.daemon_stop()


def test_web_port_conflict_slides(tianji_home, dconn):
    """18.2: 端口冲突顺延+1(占 8804 → supervisor 用 8805)。"""
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 8804))
    blocker.listen(1)
    try:
        r = daemon.daemon_start(interval=1, web_port=8804)
        assert r["ok"] is True
        try:
            assert _wait_for(lambda: daemon.daemon_status()["web_alive"])
            assert daemon.daemon_status()["web_port"] == 8805
        finally:
            daemon.daemon_stop()
    finally:
        blocker.close()


def test_daemon_stop_clears_configs(tianji_home, dconn):
    """无共享文件: 状态全在账本 configs,stop 后 daemon.* 键清空。"""
    r = daemon.daemon_start(interval=1, web_port=8806)
    assert r["ok"] is True
    try:
        assert _wait_for(lambda: daemon.daemon_status()["running"])
    finally:
        daemon.daemon_stop()
    rows = dconn.execute(
        "SELECT key FROM configs WHERE key LIKE 'daemon.%'").fetchall()
    assert rows == []
