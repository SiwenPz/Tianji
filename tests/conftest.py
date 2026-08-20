"""测试夹具: 每测试独立 TIANJI_HOME(临时目录),预置总控+实施者实例。"""

import pytest

from tianji import ops
from tianji.db import connect


@pytest.fixture
def tianji_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TIANJI_HOME", str(home))
    return home


@pytest.fixture
def conn(tianji_home):
    c = connect()
    ops.ensure_defaults(c)
    yield c
    c.close()


@pytest.fixture
def controller(conn):
    """总控实例(用户主会话,兼架构师兼审核者,1.3 最小配置)。"""
    r = ops.instance_register(
        conn, "总控", "claude", "deepseek-v4-flash", controller=True)
    return {"worker_id": "总控", "secret": r["secret"]}


@pytest.fixture
def worker(conn):
    """实施者实例。"""
    r = ops.instance_register(
        conn, "铁蛋", "codex", "step-router-v1", launch_cmd="python mock_worker.py")
    return {"worker_id": "铁蛋", "secret": r["secret"]}
