"""钩子运行器(6.2): permission_request 入库→裁决→应答全链路 + 一般事件 fail-open。

权限测试走真实链路: 注册→派单→spawn 拿启动器级 secret → runner 喂钩子载荷
→ 真实 ingest-event 子进程进账本 → 总控裁决 → 再喂载荷按裁决应答。
载荷键用真实的 tool_name(与 events.py:130 / cockpit.py:172 口径一致)。
"""

import io
import json
import sys

import pytest

from tianji import ops, permission
from tianji.adapters.runner import main as runner_main
from tianji.render import spawn


def _make_stdin(hook_dict):
    return io.StringIO(json.dumps(hook_dict) + "\n")


def _spawned_worker(conn, controller, name, shell, tag):
    """真实身份链: 注册→派单→spawn,拿启动器注入级 secret(过 ingest 防冒名校验)。"""
    ops.instance_register(conn, name, shell, "step-router-v1")
    tid = ops.task_new(conn, controller, "任务",
                       request_id=f"{tag}-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"{tag}-{s}")
    did = ops.dispatch_issue(conn, controller, tid, name,
                             request_id=f"{tag}-issue")["dispatch_id"]
    return name, spawn(conn, name, did)["env"]["TIANJI_SECRET"]


def _hook_round(monkeypatch, capsys, shell, hook):
    """喂一轮钩子载荷给 runner,返回 stdout 解析出的应答 JSON。"""
    monkeypatch.setattr(sys, "stdin", _make_stdin(hook))
    assert runner_main(shell) == 0
    return json.loads(capsys.readouterr().out.strip())


# ── permission_request 全链路: ingest 入库 → 待裁决 → 总控裁决 → 应答 ──

def test_permission_request_full_chain_claude(
    conn, controller, monkeypatch, capsys
):
    """端到端(claude/cc 格式): 钩子载荷 → 进账本 pending → 无裁决 deny;
    总控裁决 allowed 后同一工具再请求 → allow。"""
    worker_id, secret = _spawned_worker(
        conn, controller, "钩工甲", "claude", "rn-c1")
    monkeypatch.setenv("TIANJI_WORKER_ID", worker_id)
    monkeypatch.setenv("TIANJI_SECRET", secret)
    hook = {"hook_event_name": "PermissionRequest", "session_id": "rn-s1",
            "tool_name": "Bash(rm -rf *)"}

    # 第一次: 无裁决 → deny(6.6 无头默认=天然拒绝);事件已入库产生 pending 裁决
    resp = _hook_round(monkeypatch, capsys, "claude", hook)
    assert resp["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    rows = permission.pending(conn)
    assert len(rows) == 1
    assert rows[0]["worker_id"] == worker_id
    assert rows[0]["tool"] == "Bash(rm -rf *)"
    assert rows[0]["status"] == "pending"

    # 决策入口唯一=总控: 裁决 allowed
    permission.decide(conn, controller, rows[0]["id"], True,
                      request_id="rn-c1-allow")

    # 第二次: 有 allowed 裁决 → allow
    resp = _hook_round(monkeypatch, capsys, "claude", hook)
    assert resp["hookSpecificOutput"]["decision"]["behavior"] == "allow"


def test_permission_request_full_chain_codex(
    conn, controller, monkeypatch, capsys
):
    """端到端(codex/bare 格式): allowed 裁决 → allow;denied 裁决 → deny。"""
    worker_id, secret = _spawned_worker(
        conn, controller, "钩工乙", "codex", "rn-x1")
    monkeypatch.setenv("TIANJI_WORKER_ID", worker_id)
    monkeypatch.setenv("TIANJI_SECRET", secret)
    hook_a = {"hook_event_name": "PermissionRequest", "session_id": "rn-s2",
              "tool_name": "Read(file)"}

    # 无裁决 → bare 格式 deny;进账本 pending
    resp = _hook_round(monkeypatch, capsys, "codex", hook_a)
    assert resp["decision"] == "deny"
    rows = permission.pending(conn)
    assert len(rows) == 1
    assert rows[0]["worker_id"] == worker_id
    assert rows[0]["tool"] == "Read(file)"

    # 总控裁决 allowed → 再请求放行
    permission.decide(conn, controller, rows[0]["id"], True,
                      request_id="rn-x1-allow")
    resp = _hook_round(monkeypatch, capsys, "codex", hook_a)
    assert resp["decision"] == "allow"

    # denied 裁决 → 仍拒(6.6)
    hook_b = {**hook_a, "tool_name": "Write(x)"}
    resp = _hook_round(monkeypatch, capsys, "codex", hook_b)
    assert resp["decision"] == "deny"
    rows_b = [r for r in permission.pending(conn) if r["tool"] == "Write(x)"]
    assert len(rows_b) == 1
    permission.decide(conn, controller, rows_b[0]["id"], False,
                      request_id="rn-x1-deny")
    resp = _hook_round(monkeypatch, capsys, "codex", hook_b)
    assert resp["decision"] == "deny"


# ── fail-closed 负例 ──

def test_permission_request_fail_closed_db_unreachable(
    conn, controller, monkeypatch, capsys
):
    """账本不可达(连接一次失败即 fail-closed,不重试) → deny 应答 + stderr 留痕。"""
    import tianji.db as db_mod

    worker_id, secret = _spawned_worker(
        conn, controller, "钩工丙", "claude", "rn-f1")

    def boom(*a, **kw):
        raise OSError("ledger unreachable")

    monkeypatch.setattr(db_mod, "connect", boom)

    monkeypatch.setenv("TIANJI_WORKER_ID", worker_id)
    monkeypatch.setenv("TIANJI_SECRET", secret)
    monkeypatch.setattr(sys, "stdin", _make_stdin({
        "hook_event_name": "PermissionRequest",
        "session_id": "rn-s3",
        "tool_name": "Bash(*)",
    }))
    ret = runner_main("claude")
    assert ret == 0
    captured = capsys.readouterr()
    resp = json.loads(captured.out.strip())
    assert resp["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert "fail-closed" in captured.err


def test_permission_request_fail_closed_timeout(
    conn, controller, monkeypatch, capsys
):
    """账本查询超时: fail-closed → deny (3s 超时保护)。"""
    import tianji.db as db_mod

    worker_id, secret = _spawned_worker(
        conn, controller, "钩工丁", "claude", "rn-f2")

    orig_connect = db_mod.connect

    def slow_connect(*a, **kw):
        conn2 = orig_connect(*a, **kw)

        class SlowConn:
            def __init__(self, real):
                self._real = real
            def execute(self, *a, **kw):
                import time
                time.sleep(10)  # 超时
                return self._real.execute(*a, **kw)
            def close(self):
                return self._real.close()
            def __getattr__(self, name):
                return getattr(self._real, name)

        return SlowConn(conn2)

    monkeypatch.setattr(db_mod, "connect", slow_connect)

    monkeypatch.setenv("TIANJI_WORKER_ID", worker_id)
    monkeypatch.setenv("TIANJI_SECRET", secret)
    monkeypatch.setattr(sys, "stdin", _make_stdin({
        "hook_event_name": "PermissionRequest",
        "session_id": "rn-s4",
        "tool_name": "Bash(*)",
    }))
    ret = runner_main("claude")
    assert ret == 0
    captured = capsys.readouterr()
    resp = json.loads(captured.out.strip())
    assert resp["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert "fail-closed" in captured.err


# ── 一般事件 fail-open ──

def test_normal_event_fail_open_on_ingest_failure(
    conn, monkeypatch, capsys
):
    """一般事件(非权限): ingest-event 失败仍 return 0 (fail-open 不变)。"""
    import tianji.adapters.runner as runner_mod

    monkeypatch.setattr(sys, "stdin", _make_stdin({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s1",
        "prompt": "hello",
    }))

    def fake_run(cmd, **kwargs):
        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "ingest-event boom"
        return FakeResult()

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    ret = runner_main("claude")
    assert ret == 0  # fail-open: 不阻断壳
    captured = capsys.readouterr()
    assert "ingest failed" in captured.err


def test_normal_event_unknown_event_ignored(conn, monkeypatch, capsys):
    """非交集事件: 忽略不阻塞 → return 0,stdout 无输出。"""
    monkeypatch.setattr(sys, "stdin", _make_stdin({
        "hook_event_name": "UnknownEvent",
        "session_id": "s1",
    }))

    ret = runner_main("claude")
    assert ret == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
