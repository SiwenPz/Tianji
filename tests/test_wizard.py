"""初始化向导与动态新增(票 14 验收 1-7)。"""

import json
from pathlib import Path

import pytest

from tianji import ops, wizard
from tianji.render import spawn


def test_four_steps_e2e(conn, controller, tmp_path):
    """验收 1: 四步走完整跑通,产出全进账本,零配置文件(账本=真源)。"""
    key_file = tmp_path / "k.txt"
    key_file.write_text("sk-test-123", encoding="utf-8")
    iso = tmp_path / "inst"
    r = wizard.add_instance(
        conn, controller, "新工", "claude", "deepseek-v4-flash",
        key_name="向导key", base_url="https://api.example.com/anthropic",
        protocol="anthropic", key_ref=str(key_file), isolated_dir=str(iso),
        binary="python", confirm=True, request_id="wz-1")
    assert r["registered"] is True and r["status"] == "已注册"
    # 产出进账本: 实例注册+key 条目+应然清单+审计
    inst = conn.execute("SELECT * FROM instances WHERE name='新工'").fetchone()
    assert inst["is_active"] == 1 and inst["key_name"] == "向导key"
    assert conn.execute(
        "SELECT 1 FROM configs WHERE key='key:向导key'").fetchone()
    exp = conn.execute(
        "SELECT value FROM configs WHERE key='expected:新工'").fetchone()
    assert exp and "launch_cmd" in exp["value"]
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='wizard_add'").fetchone()
    # claude 分类法: settings 生成物含 provider env(13.2 env 注入型)
    settings = (iso / "settings.json").read_text(encoding="utf-8")
    assert "https://api.example.com/anthropic" in settings
    assert "--settings" in inst["launch_cmd"]


def test_probe_fail_marked_pending_not_profiled(conn, controller):
    """验收 2: 测不通标"待测试",不注册不入能力画像。"""
    r = wizard.add_instance(conn, controller, "坏工", "claude", "m",
                            binary="不存在的二进制xyz123", request_id="wz-2")
    assert r["registered"] is False and r["status"] == "待测试"
    assert conn.execute(
        "SELECT 1 FROM instances WHERE name='坏工'").fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM ability_profiles WHERE instance_name='坏工'"
    ).fetchone() is None
    a = conn.execute("SELECT detail FROM audit WHERE action='wizard_add'"
                     ).fetchone()
    assert "待测试" in a["detail"]


def test_binding_classification_and_spawn(conn, controller, tmp_path):
    """验收 3: 分类法生效——codex 生成 config.toml,spawn 成功+登记行写入。"""
    key_file = tmp_path / "k.txt"
    key_file.write_text("sk-x", encoding="utf-8")
    iso = tmp_path / "cx"
    r = wizard.add_instance(
        conn, controller, "码工", "codex", "deepseek-v4-flash",
        key_name="向导key2", base_url="https://api.example.com/v1",
        key_ref=str(key_file), isolated_dir=str(iso),
        binary="python", confirm=True, request_id="wz-3")
    cfg = (iso / "config.toml").read_text(encoding="utf-8")
    assert "model_provider" in cfg and "wizard" in cfg
    assert "CODEX_HOME" in r["launch_cmd"]  # env 注入型启动器形态
    assert "sk-x" not in r["launch_cmd"]    # key 不落 launch_cmd
    # spawn 成功+登记行
    tid = ops.task_new(conn, controller, "任务", request_id="wz-3-t")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"wz-3-{s}")
    did = ops.dispatch_issue(conn, controller, tid, "码工",
                             request_id="wz-3-issue")["dispatch_id"]
    s = spawn(conn, "码工", did)
    reg = conn.execute(
        "SELECT status FROM instance_registrations WHERE instance_name='码工'"
        " AND dispatch_id=?", (did,)).fetchone()
    assert reg["status"] == "spawned" and s["taskbook"]


def test_present_tiers_and_single_key_note(conn, controller):
    """验收 4/7: 质量档位如实标注;单 key 如实说明质量降级。"""
    ops.config_set(conn, controller, "key:kA", json.dumps({
        "base_url": "https://a", "models": [{"id": "m1"}],
        "protocol": "anthropic"}, ensure_ascii=False), request_id="wz-ka")
    ops.config_set(conn, controller, "shell:claude", json.dumps(
        wizard.SHELL_ENTRY_DEFAULTS["claude"], ensure_ascii=False),
        request_id="wz-shc")
    ops.instance_register(conn, "老工", "claude", "m1", key_name="kA")
    tiers = wizard.quality_tiers(conn, "codex", "kA")
    assert tiers["老工"] == "三档(同key不同壳)"
    tiers2 = wizard.quality_tiers(conn, "claude", "kB")
    assert tiers2["老工"] == "二档(同壳不同key)"
    p = wizard.present(conn)
    assert "质量降级" in p["single_key_note"]  # 无 key 条目=单 key 口径
    assert p["allocation"]


def test_dynamic_add_immediately_allocatable(conn, controller, tmp_path):
    """验收 5: 动态新增后立即可分配(分配器能选中)。"""
    iso = tmp_path / "inst5"
    wizard.add_instance(conn, controller, "快手", "claude", "m",
                        isolated_dir=str(iso), binary="python",
                        confirm=True, request_id="wz-5")
    ops.instance_update(conn, controller, "快手", context_window=100000,
                        request_id="wz-5-cw")
    tid = ops.task_new(conn, controller, "活", request_id="wz-5-t")["task_id"]
    assert ops.allocator_pick(conn, tid) == "快手"


def test_key_only_ref_in_ledger(conn, controller, tmp_path):
    """验收 6: key 只存引用,账本无 key 明文。"""
    key_file = tmp_path / "k.txt"
    key_file.write_text("sk-secret-456", encoding="utf-8")
    wizard.add_instance(conn, controller, "密工", "claude", "m",
                        key_name="密key", base_url="https://api.example.com/anthropic", protocol="anthropic",
                        key_ref=str(key_file), isolated_dir=str(tmp_path / "i"),
                        binary="python", confirm=True, request_id="wz-6")
    for table, col in (("configs", "value"),):
        rows = conn.execute(f"SELECT {col} FROM {table}").fetchall()
        for r in rows:
            assert "sk-secret-456" not in (r[col] or "")
    kcfg = json.loads(conn.execute(
        "SELECT value FROM configs WHERE key='key:密key'").fetchone()["value"])
    assert kcfg["key_ref"] == str(key_file)
