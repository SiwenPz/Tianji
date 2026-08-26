"""工人显示模式与默认思考级别(票 26 验收 1-4)。"""

import json
import subprocess
from pathlib import Path

import pytest

from tianji import ops
from tianji.cockpit import render_snapshot, snapshot
from tianji.render import _spawn_flags, spawn


def _to_dispatched(conn, controller, worker_name, seq="d"):
    tid = ops.task_new(conn, controller, "任务", request_id=f"rd-new-{seq}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"rd-{s}-{seq}")
    return tid


def test_register_update_fields_with_audit(conn, controller):
    """验收 1: 两字段注册/查询/修改带审计;非法值机械拒绝。"""
    ops.instance_register(conn, "小赵", "codex", "step-router-v1",
                          display_mode="后台", thinking_level="高")
    row = conn.execute("SELECT display_mode, thinking_level FROM instances"
                       " WHERE name='小赵'").fetchone()
    assert row["display_mode"] == "后台" and row["thinking_level"] == "高"
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='instance_register'"
        " AND detail LIKE '%小赵%'").fetchone()
    # 默认前台(可见性优先)
    ops.instance_register(conn, "小钱", "codex", "step-router-v1")
    row2 = conn.execute("SELECT display_mode, thinking_level FROM instances"
                        " WHERE name='小钱'").fetchone()
    assert row2["display_mode"] == "前台" and row2["thinking_level"] == ""
    # 修改(票 28 instance update 通道)带审计
    r = ops.instance_update(conn, controller, "小钱", display_mode="后台",
                            thinking_level="中", request_id="d-upd1")
    assert r["updated"] == {"display_mode": "后台", "thinking_level": "中"}
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='instance_update'"
        " AND detail LIKE '%后台%'").fetchone()
    # 非法值拒绝
    with pytest.raises(ValueError, match="前台|后台"):
        ops.instance_register(conn, "小孙", "codex", "m", display_mode="隐身")
    with pytest.raises(ValueError, match="低|中|高"):
        ops.instance_update(conn, controller, "小钱", thinking_level="超高",
                            request_id="d-upd2")


def test_dispatch_override_not_change_default(conn, controller):
    """验收 1 续: 派单单点覆盖生效且不改实例默认。"""
    ops.instance_register(conn, "小周", "codex", "step-router-v1",
                          thinking_level="高")
    tid = _to_dispatched(conn, controller, "小周", "ov")
    did = ops.dispatch_issue(conn, controller, tid, "小周",
                             display_mode="后台", thinking_level="低",
                             request_id="rd-ov-issue")["dispatch_id"]
    payload = json.loads(ops.dispatch_get(conn, did)["payload"])
    assert payload["display_mode"] == "后台"
    assert payload["thinking_level"] == "低"
    row = conn.execute("SELECT display_mode, thinking_level FROM instances"
                       " WHERE name='小周'").fetchone()
    assert row["display_mode"] == "前台" and row["thinking_level"] == "高"


def test_spawn_flags_display_mode():
    """验收 2(逻辑级): 后台=无窗口;前台交互壳=可见窗口;无头壳始终无窗口。
    无窗口分支附 CREATE_NEW_PROCESS_GROUP(2026-08-25 防控制台信号串扰)。"""
    _nw = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    assert _spawn_flags("claude", "后台") == _nw
    assert _spawn_flags("claude", "前台") == subprocess.CREATE_NEW_CONSOLE
    assert _spawn_flags('kimi -p "任务"', "前台") == _nw
    assert _spawn_flags('kimi -p "任务"', "后台") == _nw


def test_thinking_codex_config_toml(conn, controller, tmp_path):
    """验收 3(codex): 思考级别落隔离目录 config.toml(顶层键,不窜 section)。"""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        'model = "deepseek-v4-flash"\n\n[model_providers.x]\nwire_api = "responses"\n',
        encoding="utf-8")
    ops.instance_register(conn, "小吴", "codex", "deepseek-v4-flash",
                          isolated_dir=str(home), thinking_level="中")
    tid = _to_dispatched(conn, controller, "小吴", "cx")
    did = ops.dispatch_issue(conn, controller, tid, "小吴",
                             request_id="rd-cx-issue")["dispatch_id"]
    r = spawn(conn, "小吴", did)
    assert r["thinking"]["applied"] is True
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert 'model_reasoning_effort = "medium"' in text
    assert text.startswith('model_reasoning_effort = "medium"')  # 顶层
    # 单点覆盖优先: 另一实例派单"低"覆盖实例默认"中"
    ops.instance_register(conn, "小吴2", "codex", "deepseek-v4-flash",
                          isolated_dir=str(home), thinking_level="中")
    tid2 = _to_dispatched(conn, controller, "小吴2", "cx2")
    did2 = ops.dispatch_issue(conn, controller, tid2, "小吴2",
                              thinking_level="低",
                              request_id="rd-cx2-issue")["dispatch_id"]
    spawn(conn, "小吴2", did2)
    assert 'model_reasoning_effort = "low"' in \
        (home / "config.toml").read_text(encoding="utf-8")


def test_thinking_dsh_patch_file(conn, controller, tmp_path):
    """验收 3(dsh): patch 覆盖文件生成(实机生效依赖 launch_cmd 引用 patch)。"""
    home = tmp_path / "dsh-home"
    home.mkdir()
    ops.instance_register(conn, "小郑", "dsh", "deepseek-v4-flash",
                          isolated_dir=str(home), thinking_level="高")
    tid = _to_dispatched(conn, controller, "小郑", "dsh")
    did = ops.dispatch_issue(conn, controller, tid, "小郑",
                             request_id="rd-dsh-issue")["dispatch_id"]
    r = spawn(conn, "小郑", did)
    assert r["thinking"]["applied"] is True
    patch = (home / "thinking.patch.yml").read_text(encoding="utf-8")
    assert 'reasoningEfforts: ["high"]' in patch


def test_thinking_unsupported_noted(conn, controller):
    """验收 4: 不支持的组合=实例档案如实记录+审计,不静默假装生效。"""
    ops.instance_register(conn, "小王", "claude", "deepseek-v4-flash",
                          thinking_level="高")
    tid = _to_dispatched(conn, controller, "小王", "un")
    did = ops.dispatch_issue(conn, controller, tid, "小王",
                             request_id="rd-un-issue")["dispatch_id"]
    r = spawn(conn, "小王", did)
    assert r["thinking"]["applied"] is False
    notes = conn.execute("SELECT notes FROM ability_profiles"
                         " WHERE instance_name='小王'").fetchone()["notes"]
    assert "思考级别注入未生效" in notes
    a = conn.execute("SELECT detail FROM audit WHERE action='thinking_apply'"
                     ).fetchone()
    assert json.loads(a["detail"])["applied"] is False


def test_cockpit_backend_badge(conn, controller):
    """验收 2(徽记): CLI 快照卡片标"后台"。"""
    ops.instance_register(conn, "小冯", "codex", "step-router-v1",
                          display_mode="后台")
    tid = _to_dispatched(conn, controller, "小冯", "bd")
    ops.dispatch_issue(conn, controller, tid, "小冯", request_id="rd-bd-issue")
    snap = snapshot(conn)
    cards = [c for cl in snap.values() if isinstance(cl, list)
             for c in cl if isinstance(c, dict)]
    card = [c for c in cards if c["instance_name"] == "小冯"][0]
    assert card["display_mode"] == "后台"
    assert "小冯·后台" in render_snapshot(snap)


def test_hard_task_thinking_hint(conn, controller):
    """13.3/14.5: 难活派单载荷带调级建议(分配不自动调级)。"""
    ops.instance_register(conn, "小褚", "codex", "step-router-v1")
    tid = ops.task_new(conn, controller, "难活", priority=5,
                       request_id="rd-h-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"rd-h-{s}")
    did = ops.dispatch_issue(conn, controller, tid, "小褚",
                             request_id="rd-h-issue")["dispatch_id"]
    payload = json.loads(ops.dispatch_get(conn, did)["payload"])
    assert "建议高档" in payload.get("thinking_hint", "")
