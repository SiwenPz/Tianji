"""nudge 顶层别名 + 断点摘要进新派单/任务书渲染(task-12)。"""

import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tianji import ops
from tianji.cli import app
from tianji.db import connect, now
from tianji.render import spawn, _render_taskbook, _TASKBOOK_TEMPLATES
from tianji.monitor import _tick

runner = CliRunner()


def _invoke(args, env=None):
    """调用 CLI 并断言成功,返回解析后的 JSON 输出。"""
    full = dict(env or {})
    full.setdefault("TIANJI_HOME", os.environ["TIANJI_HOME"])
    r = runner.invoke(app, args, env=full)
    assert r.exit_code == 0, f"CLI 失败 {args}: {r.output}\n{r.exception}"
    return json.loads(r.output) if r.output.strip() else {}


# ====================================================================
# 验收 a: 顶层 nudge 命令接线
# ====================================================================

def test_cli_nudge_alias_requires_controller(conn, controller, worker):
    """顶层 nudge 命令须总控身份(非总控=PermissionError)。"""
    tid = ops.task_new(conn, controller, "nudge 别名测试",
                       request_id="r-na")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    # 非总控调用应失败
    w_env = {"TIANJI_WORKER_ID": worker["worker_id"],
             "TIANJI_SECRET": worker["secret"]}
    r = runner.invoke(app, ["nudge", str(did), "--request-id", "r-w"], env=w_env)
    assert r.exit_code != 0


def test_cli_nudge_alias_success(conn, controller, worker):
    """顶层 nudge 与 dispatch nudge 行为一致(返回相同字段)。"""
    tid = ops.task_new(conn, controller, "nudge 别名成功测试",
                       request_id="r-na2")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    spawn(conn, worker["worker_id"], did)

    ctrl_env = {"TIANJI_WORKER_ID": controller["worker_id"],
                "TIANJI_SECRET": controller["secret"]}
    r1 = _invoke(["dispatch", "nudge", str(did), "--request-id", "r-nudge1"],
                 env=ctrl_env)
    r2 = _invoke(["nudge", str(did), "--request-id", "r-nudge2"],
                 env=ctrl_env)

    assert r1["supported"] == r2["supported"]
    assert r1["shell"] == r2["shell"]
    assert r1["dispatch_id"] == r2["dispatch_id"]


# ====================================================================
# 验收 b: 断点摘要穿过重派进入新派单+任务书渲染
# ====================================================================

def _setup_active_dispatch(conn, controller, worker):
    """建任务+派单+spawn,返回 (tid, did, worker_env_dict)。"""
    tid = ops.task_new(conn, controller, "断点摘要测试",
                       request_id="r-bp")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    env = {
        "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
        "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
        "TIANJI_DISPATCH_ID": str(did),
    }
    # 模拟登记行有 pid(让对账②认为进程存在后死亡)
    conn.execute(
        "UPDATE instance_registrations SET pid=?, session_id=?",
        (99990, "test-session-bp"))
    # 写转录文件(让 _transcript_bytes 有字节读)
    td = ops.dispatch_get(conn, did)["task_dir"]
    # 创建一个独立瞬时文件存放转录模拟数据(避免与 task.md 混在一起)
    tdir = Path(td)
    tdir.mkdir(parents=True, exist_ok=True)
    # 手动写一个假的转录文件——通过 transcript_parser 的路径逻辑
    # _transcript_path 需要知道 session_id+shell;这里用 dsh 壳
    # transcript_parser.transcript_path(shell, session_id)
    # For simplicity, patch _transcript_bytes to return our test data
    return tid, did, env


def test_monitor_reschedule_passes_breakpoint_summary(conn, controller, worker, monkeypatch):
    """监控器对账②重派: 新派单 payload 含旧派单断点摘要。"""
    tid, did, env = _setup_active_dispatch(conn, controller, worker)

    import tianji.monitor as mon
    import tianji.adapters.transcript_parser as tp_mod

    # 创建虚假转录文件(让 _transcript_bytes 有字节读)
    tp_res = tp_mod.transcript_path("claude", "test-session-bp")
    if tp_res:
        tp_res.parent.mkdir(parents=True, exist_ok=True)
        tp_res.write_text("line1\nline2\nlast-stop-event\n", encoding="utf-8")

    # 模拟 pid 已死
    conn.execute(
        "UPDATE instance_registrations SET pid=99999"
        " WHERE instance_name=?", (worker["worker_id"],))
    monkeypatch.setattr(mon, "_pid_alive", lambda pid: False if pid == 99999 else True)

    # 写一个产物文件(让 _append_breakpoint_summary 能找到 artifacts)
    d = ops.dispatch_get(conn, did)
    rp = Path(d["task_dir"]) / "report.md"
    rp.write_text("partial report", encoding="utf-8")

    # Run tick (will do reschedule)
    _tick(conn, {})

    # Find the NEW dispatch for the same task (status=issued)
    new_disp = conn.execute(
        "SELECT * FROM dispatches WHERE task_id=? AND status='issued'"
        " ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    assert new_disp is not None, "应生成新派单"
    new_payload = json.loads(new_disp["payload"] or "{}")

    # 断点摘要须传递到新派单
    assert "breakpoint_summary" in new_payload
    bs = new_payload["breakpoint_summary"]
    assert "taskbook" in bs
    assert "artifacts" in bs
    assert "transcript_tail" in bs
    # 产物清单应包含我们写入的 report.md
    assert "report.md" in bs["artifacts"]


def test_no_breakpoint_section_when_no_summary(conn):
    """无断点摘要时任务书不出现该节。"""
    from tianji.ops import ensure_defaults
    ensure_defaults(conn)
    conn.execute(
        "INSERT OR IGNORE INTO configs (key, value, updated_at) "
        "VALUES ('user_language','zh',1)")
    conn.execute(
        "INSERT OR IGNORE INTO tasks (id, title, description, verify_cmd, "
        "scope_guard, project_dir, created_at, updated_at) "
        "VALUES (1,'测试任务','描述','echo ok','[]','',?,?)",
        (now(), now()))
    conn.execute(
        "INSERT OR IGNORE INTO dispatches (id, task_id, worker_id, worker_role, "
        "status, payload, task_dir, expect_min, dcap_hash, created_at, updated_at) "
        "VALUES (1,1,'测试员','worker','issued','{}','/tmp/task1',30,'',?,?)",
        (now(), now()))
    task = conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()
    dispatch = conn.execute("SELECT * FROM dispatches WHERE id=1").fetchone()
    result = _render_taskbook(conn, dict(dispatch), dict(task), "/tmp/report.md")
    assert "断点摘要" not in result
    assert "Breakpoint Summary" not in result


def test_breakpoint_section_rendered_zh(conn):
    """断点摘要存在时中文任务书渲染断点摘要节。"""
    from tianji.ops import ensure_defaults
    ensure_defaults(conn)
    conn.execute(
        "INSERT OR IGNORE INTO configs (key, value, updated_at) "
        "VALUES ('user_language','zh',1)")
    conn.execute(
        "INSERT OR IGNORE INTO tasks (id, title, description, verify_cmd, "
        "scope_guard, project_dir, created_at, updated_at) "
        "VALUES (1,'测试任务','描述','echo ok','[]','',?,?)",
        (now(), now()))
    summary_payload = json.dumps({
        "task_id": 1, "expect_min": 30, "reason": "", "axis": "",
        "breakpoint_summary": {
            "taskbook": "旧任务书内容",
            "artifacts": ["report.md", "fix.patch"],
            "transcript_tail": "last event: stop\nno interrupt",
        },
    }, ensure_ascii=False)
    conn.execute(
        "INSERT OR IGNORE INTO dispatches (id, task_id, worker_id, worker_role, "
        "status, payload, task_dir, expect_min, dcap_hash, created_at, updated_at) "
        "VALUES (1,1,'测试员','worker','issued',?,'/tmp/task1',30,'',?,?)",
        (summary_payload, now(), now()))
    task = conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()
    dispatch = conn.execute("SELECT * FROM dispatches WHERE id=1").fetchone()
    result = _render_taskbook(conn, dict(dispatch), dict(task), "/tmp/report.md")
    assert "断点摘要" in result
    assert "上一轮转录尾部" in result
    assert "上一轮产物清单" in result
    assert "- report.md" in result
    assert "- fix.patch" in result
    assert "旧任务书内容" not in result  # taskbook 字段不直接渲染进摘要,只存 payload


def test_breakpoint_section_rendered_en(conn):
    """断点摘要存在时英文任务书渲染断点摘要节。"""
    from tianji.ops import ensure_defaults
    ensure_defaults(conn)
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) "
        "VALUES ('user_language','en',1)")
    conn.execute(
        "INSERT OR IGNORE INTO tasks (id, title, description, verify_cmd, "
        "scope_guard, project_dir, created_at, updated_at) "
        "VALUES (1,'Test Task','desc','echo ok','[]','',?,?)",
        (now(), now()))
    summary_payload = json.dumps({
        "task_id": 1, "expect_min": 30, "reason": "", "axis": "",
        "breakpoint_summary": {
            "taskbook": "old taskbook",
            "artifacts": ["report.md"],
            "transcript_tail": "stop event here",
        },
    }, ensure_ascii=False)
    conn.execute(
        "INSERT OR IGNORE INTO dispatches (id, task_id, worker_id, worker_role, "
        "status, payload, task_dir, expect_min, dcap_hash, created_at, updated_at) "
        "VALUES (1,1,'worker','worker','issued',?,'/tmp/task1',30,'',?,?)",
        (summary_payload, now(), now()))
    task = conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()
    dispatch = conn.execute("SELECT * FROM dispatches WHERE id=1").fetchone()
    result = _render_taskbook(conn, dict(dispatch), dict(task), "/tmp/report.md")
    assert "Breakpoint Summary" in result
    assert "Last Session Transcript Tail" in result
    assert "Artifacts from Previous Session" in result
    assert "- report.md" in result
