
"""天机 daemon(18.2/18.3/7.1): start/stop/status 统一拉起监控器+驾驶舱Web,崩溃自动重拉,每日备份。

设计: daemon start 拉起 supervisor(detached),supervisor 再拉起 monitor + web
两常驻子进程并守护探活,任一死则自动重拉+审计行;每日备份由监控器巡检顺带
(18.5)。stop 全部停止。常驻进程无状态(7.1:运行知识全在账本,不写共享文件)。
"""

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import ops
from .db import connect, now, tianji_home, ledger_path

WEB_PORT_DEFAULT = 8787
BACKUP_KEEP_DEFAULT = 7


# ---------------------------------------------------------------- config helpers

def _cfg_get(conn, key, default=""):
    row = conn.execute("SELECT value FROM configs WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _cfg_set(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        (key, str(value), now()),
    )


def _pid_alive(pid):
    """探活已有 PID(复用 monitor._pid_alive 的跨平台逻辑)。"""
    from .monitor import _pid_alive
    return _pid_alive(pid)


def _win_flags():
    # CREATE_NEW_PROCESS_GROUP: 子进程脱离父进程的控制台进程组,避免 Ctrl+C/
    # 控制台事件在进程组内串扰(常驻子进程被强杀时不得波及宿主控制台,
    # 2026-08-25 实证: npx 绑终端启动 dsh 时,天机子进程共享其进程组,测试清理
    # 触发信号串扰 → npx 批处理弹 "Terminate batch job" 崩掉宿主)。
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def _child_env():
    """子进程环境: PYTHONPATH 预置本包父目录,保证 `python -m tianji` 找到的
    与当前进程是同一份代码(安装版=site-packages,源码版=仓库根);
    与 cwd 固定账本根配套(防 cwd 里的 tianji/ 源码目录顶包,2026-08-21 踩坑)。"""
    root = str(Path(__file__).resolve().parent.parent)
    env = os.environ.copy()
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root + os.pathsep + prev if prev else root
    return env


def _spawn_monitor(interval):
    """拉起监控器子进程(7.1: daemon 守护探活拉起,分钟级)。

    cwd 固定到账本根: `python -m` 会把调用方 cwd 放上 sys.path,cwd 里若恰有
    tianji/ 源码目录(如用户在天机仓库副本里跑 start)会顶包已安装版本
    (2026-08-21 实机踩坑: 新包被旧源码顶掉,web 起的是老代码)。"""
    log = open(tianji_home() / "monitor.log", "ab")
    return subprocess.Popen(
        [sys.executable, "-m", "tianji", "monitor", "--interval", str(interval)],
        stdout=log, stderr=subprocess.STDOUT, creationflags=_win_flags(),
        cwd=str(tianji_home()), env=_child_env())


def _spawn_web(port):
    """拉起驾驶舱 Web 子进程(18.2 常驻之二)。cwd 固定到账本根(同 _spawn_monitor)。"""
    log = open(tianji_home() / "web.log", "ab")
    return subprocess.Popen(
        [sys.executable, "-m", "tianji", "web", "--port", str(port)],
        stdout=log, stderr=subprocess.STDOUT, creationflags=_win_flags(),
        cwd=str(tianji_home()), env=_child_env())


def _terminate(proc):
    """优雅终止子进程(先发再等,超时强杀)。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        _kill_pid(proc.pid)


def _kill_pid(pid):
    """强杀进程(Windows ctypes TerminateProcess; POSIX SIGKILL)。"""
    if not pid:
        return
    if os.name == "nt":
        import ctypes
        try:
            h = ctypes.windll.kernel32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE
            if h:
                ctypes.windll.kernel32.TerminateProcess(h, 1)
                ctypes.windll.kernel32.CloseHandle(h)
        except OSError:
            pass
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _find_free_port(start: int = WEB_PORT_DEFAULT) -> int:
    """回环端口冲突顺延 +1(18.2)。"""
    for p in range(start, start + 100):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return start


def run_daemon(interval: int = 30, web_port: int = WEB_PORT_DEFAULT):
    """supervisor 主循环(被 daemon start 后台拉起): 拉两常驻+守护探活。

    无状态(7.1): 运行知识全在账本 configs,重启不丢;不写共享文件;
    监控器/驾驶舱任一死 → 自动重拉 + 审计行(7.1 监控器重启)。
    """
    conn = connect()
    ops.ensure_defaults(conn)
    port = _find_free_port(web_port)
    _cfg_set(conn, "daemon.pid", str(os.getpid()))
    _cfg_set(conn, "daemon.web_port", str(port))
    _cfg_set(conn, "daemon.started_at", str(now()))
    mon = _spawn_monitor(interval)
    web = _spawn_web(port)
    _cfg_set(conn, "daemon.monitor_pid", str(mon.pid))
    _cfg_set(conn, "daemon.web_pid", str(web.pid))
    ops.audit(conn, "daemon_start",
              {"monitor_pid": mon.pid, "web_pid": web.pid, "web_port": port})
    print(f"daemon 启动: monitor={mon.pid} web={web.pid} 端口={port}", flush=True)
    try:
        while True:
            # 探活间隔复用 interval(默认 30s,分钟级守护探活 7.1)
            time.sleep(max(1, interval))
            if not _pid_alive(mon.pid):
                ops.audit(conn, "monitor_restart",
                          {"old_pid": mon.pid, "ts": now()})
                mon = _spawn_monitor(interval)
                _cfg_set(conn, "daemon.monitor_pid", str(mon.pid))
                print(f"监控器重启: {mon.pid}", flush=True)
            if not _pid_alive(web.pid):
                ops.audit(conn, "web_restart",
                          {"old_pid": web.pid, "ts": now()})
                web = _spawn_web(port)
                _cfg_set(conn, "daemon.web_pid", str(web.pid))
                print(f"Web 重启: {web.pid}", flush=True)
    finally:
        _terminate(mon)
        _terminate(web)
        for key in ("daemon.pid", "daemon.monitor_pid", "daemon.web_pid",
                    "daemon.web_port", "daemon.started_at"):
            try:
                conn.execute("DELETE FROM configs WHERE key=?", (key,))
            except sqlite3.Error:
                pass
        conn.close()


def daemon_start(interval: int = 30, web_port: int = WEB_PORT_DEFAULT):
    """`daemon start`: 后台拉起 supervisor,supervisor 再拉起 monitor+web 两常驻。

    返回后 status 即可见(等待 supervisor 写入 web_port)。不做电脑登录自启(18.3)。
    """
    tianji_home().mkdir(parents=True, exist_ok=True)
    log = open(tianji_home() / "daemon.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "tianji", "daemon", "run",
         "--interval", str(interval), "--web-port", str(web_port)],
        stdout=log, stderr=subprocess.STDOUT, creationflags=_win_flags(),
        cwd=str(tianji_home()), env=_child_env())
    conn = connect()
    ops.ensure_defaults(conn)
    deadline = time.time() + 5
    while time.time() < deadline:
        p = _cfg_get(conn, "daemon.web_port")
        if p:
            break
        time.sleep(0.2)
    return {"ok": True, "daemon_pid": proc.pid,
            "web_port": _cfg_get(conn, "daemon.web_port") or web_port}


def daemon_stop():
    """`daemon stop`: 停 supervisor+monitor+web 全部常驻,清 configs 状态。"""
    conn = connect()
    ops.ensure_defaults(conn)
    mon_pid = int(_cfg_get(conn, "daemon.monitor_pid") or 0)
    web_pid = int(_cfg_get(conn, "daemon.web_pid") or 0)
    daemon_pid = int(_cfg_get(conn, "daemon.pid") or 0)
    _kill_pid(web_pid)
    _kill_pid(mon_pid)
    _kill_pid(daemon_pid)
    for key in ("daemon.pid", "daemon.monitor_pid", "daemon.web_pid",
                "daemon.web_port", "daemon.started_at"):
        conn.execute("DELETE FROM configs WHERE key=?", (key,))
    ops.audit(conn, "daemon_stop",
              {"monitor_pid": mon_pid, "web_pid": web_pid})
    return {"ok": True, "monitor_pid": mon_pid, "web_pid": web_pid,
            "daemon_pid": daemon_pid}


def daemon_status():
    """`daemon status`: 三进程活性快照。"""
    conn = connect()
    ops.ensure_defaults(conn)
    daemon_pid = int(_cfg_get(conn, "daemon.pid") or 0)
    mon_pid = int(_cfg_get(conn, "daemon.monitor_pid") or 0)
    web_pid = int(_cfg_get(conn, "daemon.web_pid") or 0)
    return {
        "running": bool(daemon_pid and _pid_alive(daemon_pid)),
        "daemon_pid": daemon_pid,
        "monitor_pid": mon_pid,
        "monitor_alive": bool(mon_pid and _pid_alive(mon_pid)),
        "web_pid": web_pid,
        "web_alive": bool(web_pid and _pid_alive(web_pid)),
        "web_port": int(_cfg_get(conn, "daemon.web_port") or 0),
        "started_at": _cfg_get(conn, "daemon.started_at") or "",
    }


# ---------------------------------------------------------------- 备份(18.5)

def backup_ledger(conn=None, today=None, backup_dir=None, keep=None):
    """每日一次账本快照+保留轮换(18.5)。

    - 每日一次: 文件名=日期(ledger-YYYYMMDD.db),同日已存在则跳过
    - 默认保留最近 7 份;备份目录/份数 env 可改
      (TIANJI_BACKUP_DIR / TIANJI_BACKUP_KEEP)
    - 复制即备份(标准库 shutil.copy2,复制 ledger.db;不引备份工具)
    - 无自动恢复(恢复=手动拷回)
    - today: 注入日期字符串(YYYYMMDD)供测试构造日期推进验证
    返回 dict(ok/file/already/keep/rotated)。
    """
    bdir = Path(backup_dir) if backup_dir else (
        Path(os.environ.get("TIANJI_BACKUP_DIR") or tianji_home() / "backups"))
    bdir.mkdir(parents=True, exist_ok=True)
    keep = int(keep if keep is not None else int(
        os.environ.get("TIANJI_BACKUP_KEEP") or BACKUP_KEEP_DEFAULT))
    if today is None:
        today = time.strftime("%Y%m%d")
    target = bdir / f"ledger-{today}.db"
    if target.exists():
        return {"ok": True, "already": True, "file": str(target), "keep": keep}
    shutil.copy2(ledger_path(), target)
    backups = sorted(bdir.glob("ledger-*.db"))
    rotated = []
    while len(backups) > keep:
        old = backups.pop(0)
        old.unlink(missing_ok=True)
        rotated.append(old.name)
    if conn is not None:
        ops.audit(conn, "backup_done",
                  {"file": str(target), "keep": keep, "rotated": rotated})
    return {"ok": True, "already": False, "file": str(target),
            "keep": keep, "rotated": rotated}


# ---------------------------------------------------------------- 入口

if __name__ == "__main__":
    import argparse
    _p = argparse.ArgumentParser(prog="tianji.daemon")
    _p.add_argument("cmd", choices=("run",))
    _p.add_argument("--interval", type=int, default=30)
    _p.add_argument("--web-port", type=int, default=WEB_PORT_DEFAULT)
    _a = _p.parse_args()
    run_daemon(_a.interval, _a.web_port)
