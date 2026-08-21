"""主题插件(票 24 验收 1-6): 默认关/三国腔/铁界/中途关/名字耗尽/fail-open。"""

import os
from pathlib import Path

import pytest

from tianji import ops, plugins, theme
from tianji.db import task_dir
from tianji.render import spawn


def test_default_off_zero_trace(conn, controller, worker):
    """验收 1: 默认关——话术大白话;任务书/消息零主题痕迹(快照比对)。"""
    assert theme.is_enabled(conn) is False
    assert theme.guidance(conn) == theme._DEFAULT_GUIDANCE
    tid = ops.task_new(conn, controller, "任务", request_id="th-t1")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"th-t1-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="th-t1-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    book = Path(s["taskbook"]).read_text(encoding="utf-8")
    assert "主公" not in book and "号令" not in book


def test_enable_sanguo(conn, controller):
    """验收 2: 开三国主题——新实例按清单命名;总控话术按主题渲染。"""
    theme.enable(conn, controller, "三国", request_id="th-on")
    assert theme.next_name(conn) == "诸葛亮"
    g = theme.guidance(conn)
    assert "报告主公" in g and "三国" in g
    # 票 23 管线客户: 话术=模板类插件生成物(带指纹)
    p = plugins._load(conn, "主题话术")
    assert p and p["type"] == "template" and p["last_fingerprint"]


def test_iron_boundary_taskbook_clean(conn, controller, worker):
    """验收 3: 铁界机械检查——主题开着,任务书渲染产物不含主题话术。"""
    theme.enable(conn, controller, "三国", request_id="th-on3")
    tid = ops.task_new(conn, controller, "任务", request_id="th-t3")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"th-t3-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="th-t3-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    book = Path(s["taskbook"]).read_text(encoding="utf-8")
    assert "主公" not in book and "且听号令" not in book


def test_disable_midway(conn, controller):
    """验收 4: 中途关——实例名不变,话术退回大白话,审计有记录。"""
    theme.enable(conn, controller, "三国", request_id="th-on4")
    ops.instance_register(conn, "赵云", "codex", "m")
    theme.disable(conn, controller, request_id="th-off4")
    assert theme.guidance(conn) == theme._DEFAULT_GUIDANCE
    assert conn.execute("SELECT 1 FROM instances WHERE name='赵云'").fetchone()
    a = conn.execute("SELECT detail FROM audit WHERE action='theme_off'"
                     ).fetchone()
    assert "三国" in a["detail"]


def test_names_exhausted_not_blocking(conn, controller):
    """验收 5: 名字清单耗尽→提示消息进账本,流程不阻塞(返回 None)。"""
    theme.enable(conn, controller, "三国", request_id="th-on5")
    for n in theme.BUILTIN_THEMES["三国"]["names"] + \
            theme.BUILTIN_THEMES["三国"]["fallback_names"]:
        ops.instance_register(conn, n, "codex", "m")
    assert theme.next_name(conn) is None
    assert conn.execute(
        "SELECT 1 FROM messages WHERE type='escalation' AND sender='theme'"
    ).fetchone()


def test_fail_open_when_plugin_broken(conn, controller):
    """验收 6/失效回退: 插件关/坏→话术退回大白话+审计,已起名保留。"""
    theme.enable(conn, controller, "三国", request_id="th-on6")
    assert "报告主公" in theme.guidance(conn)
    plugins.set_enabled(conn, controller, "主题话术", False,
                        request_id="th-p-off")
    assert theme.guidance(conn) == theme._DEFAULT_GUIDANCE
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='theme_fallback'").fetchone()
