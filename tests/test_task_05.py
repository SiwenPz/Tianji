"""Task-05: 权限钩子应答接生产链路——hooks.py 生成脚本真实可执行 + fail-loud 校验。"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tianji import hooks, ops, permission
from tianji.adapters.runner import main as runner_main
from tianji.permission import hook_response
from tianji.render import spawn


@pytest.fixture
def claude_worker(conn):
    ops.instance_register(conn, "钩工甲", "claude", "step-router-v1")
    return "钩工甲"


def _make_stdin(hook_dict):
    return io.StringIO(json.dumps(hook_dict) + "\n")


# ── __main__ 入口: hooks.py 生成工序产出的实例钩子脚本真实可执行 ──

def test_generated_hook_script_responds_to_permission_request(
    conn, controller, tmp_path
):
    """hooks.install_instance 产出的钩子脚本: 子进程直接执行(走 __main__ 块),
    喂 PermissionRequest 载荷 → 事件真实入库(账本产生 pending 裁决)
    且 stdin→stdout 有应答(无 allowed 裁决=deny, cc 格式)。"""
    iso = tmp_path / "iso"
    ops.instance_register(conn, "钩工丙", "claude", "step-router-v1",
                          isolated_dir=str(iso))
    hooks.install_instance(conn, "钩工丙")
    script = iso / "tianji_claude_hook.py"
    assert script.is_file()
    # 真实身份链: 派单→spawn 拿启动器注入级 secret(过 ingest 防冒名校验)
    tid = ops.task_new(conn, controller, "任务",
                       request_id="t05-g-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"t05-g-{s}")
    did = ops.dispatch_issue(conn, controller, tid, "钩工丙",
                             request_id="t05-g-issue")["dispatch_id"]
    secret = spawn(conn, "钩工丙", did)["env"]["TIANJI_SECRET"]

    # 生产上 tianji 是已安装包,隔离目录里直接跑脚本也能 import;
    # 测试环境未安装,用 PYTHONPATH 指到仓库根模拟(脚本目录不在包路径上)。
    import tianji
    repo_root = str(Path(tianji.__file__).resolve().parent.parent)
    env = {**os.environ, "TIANJI_WORKER_ID": "钩工丙", "TIANJI_SECRET": secret,
           "PYTHONPATH": repo_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    payload = {"hook_event_name": "PermissionRequest", "session_id": "t05-s1",
               "tool_name": "Bash(ls)"}
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload, ensure_ascii=False) + "\n",
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0
    assert "Traceback" not in proc.stderr
    resp = json.loads(proc.stdout.strip())
    assert resp["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert resp["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    # 权限请求已进账本产生 pending 裁决(走脚本内的真实 ingest-event 链路)
    rows = permission.pending(conn)
    assert len(rows) == 1
    assert rows[0]["worker_id"] == "钩工丙"
    assert rows[0]["tool"] == "Bash(ls)"
    assert rows[0]["status"] == "pending"


def test_main_block_returns_zero_on_empty_stdin(monkeypatch, capsys):
    """__main__ 入口: 空 stdin → return 0 (fail-open 不变)。"""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ret = runner_main("claude")
    assert ret == 0


# ── fail-loud: hook_response 参数校验(5.6 不静默默认) ──

def test_hook_response_raises_on_empty_shell(conn):
    """fail-loud: shell='' → ValueError。"""
    with pytest.raises(ValueError, match="shell"):
        hook_response(conn, "", "w1", "Bash")


def test_hook_response_raises_on_empty_worker_id(conn):
    """fail-loud: worker_id='' → ValueError。"""
    with pytest.raises(ValueError, match="worker_id"):
        hook_response(conn, "claude", "", "Bash")


def test_hook_response_raises_on_empty_tool(conn):
    """fail-loud: tool='' → ValueError(runner 侧捕获后 deny+stderr 留痕,不静默)。"""
    with pytest.raises(ValueError, match="tool"):
        hook_response(conn, "claude", "w1", "")


# ── hook_response 单测: 裁决查询与格式 ──

def test_hook_response_no_ruling_denies(conn, claude_worker):
    """无裁决时返回 deny(行为不变,只是 bad input 才 raise)。"""
    resp = hook_response(conn, "claude", claude_worker, "Bash(*)")
    assert resp["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_hook_response_allowed_ruling_permits(conn, controller, claude_worker):
    """有 allowed 裁决时返回 allow。"""
    rid = permission.record_request(
        conn, claude_worker, "s1", "Read(file)", {})["ruling_id"]
    permission.decide(conn, controller, rid, True, request_id="pm-05")
    resp = hook_response(conn, "claude", claude_worker, "Read(file)")
    assert resp["hookSpecificOutput"]["decision"]["behavior"] == "allow"


# ── 一般事件: 经 runner → 真实 ingest-event 子进程入账 ──

def test_normal_event_ingested_end_to_end(conn, controller, monkeypatch):
    """session_start 钩子载荷 → runner → ingest-event 子进程真实入账
    (session_states 派生 idle;登记行 spawned→active)。"""
    ops.instance_register(conn, "钩工丁", "claude", "step-router-v1")
    tid = ops.task_new(conn, controller, "任务",
                       request_id="t05-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"t05-{s}")
    did = ops.dispatch_issue(conn, controller, tid, "钩工丁",
                             request_id="t05-issue")["dispatch_id"]
    secret = spawn(conn, "钩工丁", did)["env"]["TIANJI_SECRET"]

    monkeypatch.setenv("TIANJI_WORKER_ID", "钩工丁")
    monkeypatch.setenv("TIANJI_SECRET", secret)
    monkeypatch.setattr(sys, "stdin", _make_stdin({
        "hook_event_name": "SessionStart",
        "session_id": "t05-s2",
    }))
    assert runner_main("claude") == 0

    row = conn.execute(
        "SELECT state, instance_name FROM session_states WHERE session_id=?",
        ("t05-s2",)).fetchone()
    assert row is not None
    assert row["state"] == "idle"
    assert row["instance_name"] == "钩工丁"
    reg = conn.execute(
        "SELECT status FROM instance_registrations WHERE instance_name=?"
        " ORDER BY id DESC LIMIT 1", ("钩工丁",)).fetchone()
    assert reg["status"] == "active"
