"""权限控制框架(票 10 验收 1-6): 待裁决/总控审批按壳执行/无头默认拒绝/缺口兜底/任务边界/粒度过滤。"""

import json
import os

import pytest

from tianji import auth, ops, permission
from tianji.events import ingest_event


_EVENT_SECRETS = {}


@pytest.fixture(autouse=True)
def _capture_event_secrets(monkeypatch):
    """instance_register 只回显一次明文 secret;事件夹具捕获它供 _env 使用。"""
    _EVENT_SECRETS.clear()
    original = ops.instance_register

    def register(*args, **kwargs):
        result = original(*args, **kwargs)
        db = args[0]
        if args:
            name = args[1]
        else:
            name = kwargs["name"]
        _EVENT_SECRETS[name] = result["secret"]
        db.execute(
            "INSERT INTO instance_registrations"
            " (instance_name, dispatch_id, status, dcap_hash, task_path,"
            " created_at) VALUES (?, NULL, 'spawned', ?, '',"
            " '2026-01-01T00:00:00')",
            (name, auth.secret_hash(result["secret"])),
        )
        return result

    monkeypatch.setattr(ops, "instance_register", register)


def _env(worker_name):
    return {**os.environ, "TIANJI_WORKER_ID": worker_name,
            "TIANJI_SECRET": _EVENT_SECRETS[worker_name],
            "TIANJI_DISPATCH_ID": "1"}


def _perm_event(conn, worker_name, tool="Bash(rm -rf *)"):
    """模拟 permission_request 事件进账本(6.5 契约)。"""
    return ingest_event(conn, _env(worker_name),
                        {"session_id": "sp1", "event_type": "permission_request",
                         "payload": {"tool": tool}})


def test_request_pending_and_worker_no_entry(conn, controller, worker):
    """验收 1: 权限请求进账本待裁决;工人侧零决策入口。"""
    ops.instance_register(conn, "工人甲", "claude", "deepseek-v4-flash")
    _perm_event(conn, "工人甲")
    rows = permission.pending(conn)
    assert len(rows) == 1 and rows[0]["tool"] == "Bash(rm -rf *)"
    assert rows[0]["status"] == "pending"
    # 重复事件去重
    _perm_event(conn, "工人甲")
    assert len(permission.pending(conn)) == 1
    # 工人身份裁决=拒绝(决策入口唯一=总控)
    with pytest.raises(PermissionError):
        permission.decide(conn, {"worker_id": "工人甲", "secret": "x"},
                          rows[0]["id"], True, request_id="pm-x")


def test_decide_hook_allow_shell(conn, controller):
    """验收 2(钩子 allow 类): 总控批准后钩子应答放行;未批=拒。"""
    ops.instance_register(conn, "工人乙", "claude", "deepseek-v4-flash")
    _perm_event(conn, "工人乙", "Edit(src/**)")
    rid = permission.pending(conn)[0]["id"]
    # 批准前: 钩子应答=拒
    r = permission.hook_response(conn, "claude", "工人乙", "Edit(src/**)")
    assert r["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    # 总控批准 → 钩子应答=放行
    d = permission.decide(conn, controller, rid, True, request_id="pm-a")
    assert d["decision"] == "allowed"
    r2 = permission.hook_response(conn, "claude", "工人乙", "Edit(src/**)")
    assert r2["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    # codex 形态映射
    r3 = permission.hook_response(conn, "codex", "工人乙", "Edit(src/**)")
    assert r3["decision"] == "allow"


def test_headless_default_deny(conn, controller):
    """验收 3: 无头默认=天然拒绝(无裁决记录即拒)。"""
    r = permission.hook_response(conn, "claude", "无此人", "Bash(*)")
    assert r["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    r2 = permission.hook_response(conn, "codex", "无此人", "Bash(*)")
    assert r2["decision"] == "deny"


def test_kimi_rules_and_cline_autoapprove(conn, controller, tmp_path):
    """验收 4/2: kimi 规则表(含静态 deny 兜底)、cline 大类开关落文件。"""
    kimi_dir = tmp_path / "kimi"
    kimi_dir.mkdir()
    ops.instance_register(conn, "工人丙", "kimi", "k3",
                          isolated_dir=str(kimi_dir))
    _perm_event(conn, "工人丙", "Read(**)")
    rid = permission.pending(conn)[0]["id"]
    d = permission.decide(conn, controller, rid, True, request_id="pm-k")
    rules = json.loads((kimi_dir / "permission-rules.json")
                       .read_text(encoding="utf-8"))
    assert "Read(**)" in rules["allow"]
    # 静态 deny 兜底(无头全批缺口)
    _perm_event(conn, "工人丙", "Bash(curl *)")
    rid2 = [r for r in permission.pending(conn)
            if r["tool"] == "Bash(curl *)"][0]["id"]
    permission.decide(conn, controller, rid2, False, reason="危险",
                      request_id="pm-k2")
    rules = json.loads((kimi_dir / "permission-rules.json")
                       .read_text(encoding="utf-8"))
    assert "Bash(curl *)" in rules["deny"]
    # cline 大类开关
    cline_dir = tmp_path / "cline"
    cline_dir.mkdir()
    ops.instance_register(conn, "工人丁", "cline", "deepseek-v4-flash",
                          isolated_dir=str(cline_dir))
    _perm_event(conn, "工人丁", "file_edit")
    rid3 = [r for r in permission.pending(conn)
            if r["worker_id"] == "工人丁"][0]["id"]
    permission.decide(conn, controller, rid3, True, request_id="pm-c")
    aa = json.loads((cline_dir / "autoapprove.json").read_text(encoding="utf-8"))
    assert "file_edit" in aa["autoApprove"]
    assert "超时强杀" in aa["note"]


def test_rules_take_effect_at_task_boundary(conn, controller, tmp_path):
    """验收 5: 规则文件由裁决表整体重生成(任务边界重启生效,热更不作依赖)。"""
    d = tmp_path / "k2"
    d.mkdir()
    ops.instance_register(conn, "工人戊", "kimi", "k3", isolated_dir=str(d))
    _perm_event(conn, "工人戊", "ToolA")
    rid = permission.pending(conn)[0]["id"]
    permission.decide(conn, controller, rid, True, request_id="pm-b1")
    first = (d / "permission-rules.json").read_text(encoding="utf-8")
    _perm_event(conn, "工人戊", "ToolB")
    rid2 = [r for r in permission.pending(conn)
            if r["tool"] == "ToolB"][0]["id"]
    permission.decide(conn, controller, rid2, False, request_id="pm-b2")
    second = (d / "permission-rules.json").read_text(encoding="utf-8")
    assert first != second
    rules = json.loads(second)
    assert rules["allow"] == ["ToolA"] and rules["deny"] == ["ToolB"]
    assert "任务边界重启生效" in rules["note"]


def test_permission_granularity_in_allocator(conn, controller):
    """验收 6: 权限粒度进画像,分配器硬过滤引用(readonly 不接有优先级活)。"""
    ops.instance_register(conn, "只读工", "codex", "step-router-v1",
                          context_window=100000,
                          permission_granularity="readonly")
    ops.instance_register(conn, "全能工", "codex", "step-router-v1",
                          context_window=100000,
                          permission_granularity="project")
    p = conn.execute("SELECT permission_granularity FROM ability_profiles"
                     " WHERE instance_name='只读工'").fetchone()
    assert p["permission_granularity"] == "readonly"
    tid = ops.task_new(conn, controller, "有优先级活", priority=1,
                       request_id="pm-task")["task_id"]
    picked = ops.allocator_pick(conn, tid)
    assert picked == "全能工"
