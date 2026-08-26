"""身份与权限(验收 2/3): 未注入 env 被拒;非总控 task new 被拒;硬约束。"""

import json

import pytest

from tianji import ops
from tianji.auth import require_identity, secret_hash


def test_no_env_identity_rejected():
    with pytest.raises(PermissionError, match="身份缺失"):
        require_identity({})


def test_task_new_requires_identity(conn):
    with pytest.raises(PermissionError):
        ops.task_new(conn, None, "无身份建任务")


def test_task_new_non_controller_rejected(conn, worker):
    with pytest.raises(PermissionError, match="仅总控身份"):
        ops.task_new(conn, worker, "实施者想建任务")


def test_task_new_controller_ok(conn, controller):
    r = ops.task_new(conn, controller, "总控建任务", request_id="r-new")
    assert r["status"] == "new"
    assert ops.task_get(conn, r["task_id"])["status"] == "new"


def test_task_new_falls_back_to_default_project_dir(conn, controller):
    """未显式给项目工作目录 → 回退默认项目目录(18.1),审计带标记。"""
    ops.config_set(conn, controller, "default_project_dir", "D:/my-proj",
                   request_id="r-dpd")
    r = ops.task_new(conn, controller, "回退任务", request_id="r-t2")
    assert ops.task_get(conn, r["task_id"])["project_dir"] == "D:/my-proj"
    a = conn.execute("SELECT detail FROM audit WHERE action='task_new'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(a["detail"])["project_dir_fallback"] is True


def test_task_new_no_default_keeps_empty(conn, controller):
    """无默认项目目录 → project_dir 保持空,不回退。"""
    r = ops.task_new(conn, controller, "无默认任务", request_id="r-t3")
    assert ops.task_get(conn, r["task_id"])["project_dir"] == ""


def test_task_new_explicit_project_dir_not_overridden(conn, controller):
    """显式给了 project_dir → 不覆盖,审计无回退标记。"""
    ops.config_set(conn, controller, "default_project_dir", "D:/my-proj",
                   request_id="r-dpd2")
    r = ops.task_new(conn, controller, "显式任务", request_id="r-t4",
                     project_dir="D:/other")
    assert ops.task_get(conn, r["task_id"])["project_dir"] == "D:/other"
    a = conn.execute("SELECT detail FROM audit WHERE action='task_new'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(a["detail"])["project_dir_fallback"] is False


def test_transition_requires_controller(conn, controller, worker):
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    with pytest.raises(PermissionError, match="仅总控身份"):
        ops.task_transition(conn, worker, tid, "discussing")


def test_config_set_requires_controller(conn, worker):
    with pytest.raises(PermissionError):
        ops.config_set(conn, worker, "max_retries", "5")


def test_secret_hash_constant_time():
    s = "abc123"
    assert secret_hash(s) == secret_hash(s)
    assert secret_hash(s) != secret_hash("abc124")


def test_controller_recover_rotates_secret(conn, controller):
    """恢复通道: 新 secret 生效,旧 secret 作废(审计留痕)。"""
    r = ops.controller_recover(conn, "总控")
    assert r["controller"] == "总控" and r["secret"]
    new_ident = {"worker_id": "总控", "secret": r["secret"]}
    assert ops.task_new(conn, new_ident, "恢复后建任务", request_id="r-rec").get("task_id")
    with pytest.raises(PermissionError):
        ops.task_new(conn, controller, "旧 secret 已作废", request_id="r-old")
    rows = conn.execute(
        "SELECT action FROM audit WHERE action='controller_recovery'").fetchall()
    assert len(rows) == 1


def test_controller_recover_unknown_instance(conn):
    with pytest.raises(KeyError):
        ops.controller_recover(conn, "不存在")
