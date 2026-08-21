"""插件机制框架(票 23 验收 1-4): 注册表/模板管线对账/视图 fail-open/核心边界。"""

import json
from pathlib import Path

import pytest

from tianji import plugins
from tianji.cockpit import render_snapshot, snapshot
from tianji.db import tianji_home

TPL_CONFIG = {
    "template": "# 绰号话术 v{ver}\n\n总控代号: {boss}\n",
    "params": {"ver": "一", "boss": "主公"},
    "target": "plugins/nickname.md",
}


def _register_tpl(conn, controller, version="v1"):
    return plugins.register(conn, controller, "绰号话术", "template", version,
                            dict(TPL_CONFIG), request_id=f"pl-reg-{version}")


def test_template_plugin_e2e(conn, controller):
    """验收 1: 模板类插件端到端——注册→渲染→改版本→对账发现→机械重生成。"""
    _register_tpl(conn, controller)
    r = plugins.render_template_plugin(conn, "绰号话术")
    target = Path(r["target"])
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "tianji-plugin:绰号话术 version:v1" in text
    assert "总控代号: 主公" in text
    # 对账一致 → ok
    assert plugins.reconcile(conn, "绰号话术")["status"] == "ok"
    # 改模板版本 → 对账发现旧版 → 机械重生成
    _register_tpl(conn, controller, version="v2")
    r2 = plugins.reconcile(conn, "绰号话术")
    assert r2["status"] == "regenerated_upgrade"
    assert "version:v2" in target.read_text(encoding="utf-8")
    # 生成物缺失 → 机械补
    target.unlink()
    assert plugins.reconcile(conn, "绰号话术")["status"] == "regenerated_missing"


def test_view_plugin_render_and_no_write_path(conn, controller, worker):
    """验收 2: 视图类插件读账本渲染展示块;无写账本旁路(白名单负例)。"""
    plugins.register(conn, controller, "任务概览", "view", "v1",
                     {"title": "任务概览", "source": "task_status_counts"},
                     request_id="pl-view1")
    snap = snapshot(conn)
    blocks = plugins.render_view_blocks(conn, snap)
    assert any("[任务概览]" in b for b in blocks)
    # 负例: 白名单外数据源拒绝注册(无代码执行面,21.2 核心边界)
    with pytest.raises(ValueError, match="内置数据源"):
        plugins.register(conn, controller, "坏插件", "view", "v1",
                         {"source": " arbitrary SQL "}, request_id="pl-view2")
    # 渲染器签名只有 (plugin, snapshot, extra): 无 conn,无写账本入口
    import inspect
    params = list(inspect.signature(plugins._render_view).parameters)
    assert "conn" not in params
    assert params[:2] == ["plugin", "snapshot"]
    # 展示块挂进驾驶舱渲染
    out = render_snapshot(snap, blocks)
    assert "插件展示块" in out


def test_user_modified_artifact_not_touched(conn, controller):
    """验收 3: 用户手工改过的生成物,对账出差异报告不自动碰。"""
    _register_tpl(conn, controller)
    r = plugins.render_template_plugin(conn, "绰号话术")
    target = Path(r["target"])
    target.write_text("<!-- tianji-plugin:绰号话术 version:v1 fingerprint：手工 -->\n用户自己改的内容\n", encoding="utf-8")
    r2 = plugins.reconcile(conn, "绰号话术")
    assert r2["status"] == "user_modified"
    # 不自动碰: 内容保持用户改动
    assert "用户自己改的内容" in target.read_text(encoding="utf-8")
    # 差异报告: 审计行+升级消息
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='plugin_reconcile_diff'").fetchone()
    assert conn.execute(
        "SELECT 1 FROM messages WHERE type='escalation' AND sender='plugins'"
    ).fetchone()


def test_view_plugin_error_fail_open(conn, controller):
    """验收 4: 视图插件运行期异常→退回默认块+审计,主流程不受影响。"""
    plugins.register(conn, controller, "会炸的视图", "view", "v1",
                     {"title": "会炸", "source": "task_status_counts"},
                     request_id="pl-view3")
    # 运行期数据源被改坏(模拟升级后配置不兼容)
    conn.execute(
        "UPDATE configs SET value=? WHERE key='plugin:会炸的视图'",
        (json.dumps({"name": "会炸的视图", "type": "view", "version": "v1",
                     "config": {"source": "不存在的数据源"}, "enabled": True,
                     "last_fingerprint": "", "last_version": ""},
                    ensure_ascii=False),))
    blocks = plugins.render_view_blocks(conn, snapshot(conn))
    assert any("插件渲染失败,已退回默认块" in b for b in blocks)
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='plugin_view_error'").fetchone()
    # 主流程不受影响: 快照照常渲染
    assert "天机驾驶舱只读快照" in render_snapshot(snapshot(conn), blocks)


def test_plugin_requires_controller(conn, controller):
    """边界: 非总控不能注册/开关/删除;类型只认 template|view。"""
    with pytest.raises(PermissionError):
        plugins.register(conn, {"worker_id": "路人", "secret": "x"},
                         "x", "view", "v1", {"source": "task_status_counts"},
                         request_id="pl-perm")
    with pytest.raises(ValueError, match="template|view"):
        plugins.register(conn, controller, "x", "code", "v1", {},
                         request_id="pl-code")
