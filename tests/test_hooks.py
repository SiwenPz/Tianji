"""钩子脚本分发与管理(票 13 验收 1-5)。"""

import json
from pathlib import Path

import pytest

from tianji import hooks, ops
from tianji.render import spawn


def _reg(conn, name="钩工", shell="claude", iso=None):
    ops.instance_register(conn, name, shell, "deepseek-v4-flash",
                          isolated_dir=str(iso) if iso else "")


def _manifest(conn, name="钩工"):
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"expected:{name}",)).fetchone()
    return json.loads(row["value"])["hooks"]


def test_artifacts_fingerprinted_and_in_checklist(conn, controller, tmp_path):
    """验收 1: 三脚本带版本指纹,全部进应然清单。"""
    _reg(conn, iso=tmp_path / "h")
    hooks.install_instance(conn, "钩工")
    manifest = _manifest(conn)
    names = {m["name"] for m in manifest}
    assert names == {"adapter", "statusline", "hooks-manifest"}  # 三样齐
    for m in manifest:
        assert m["fingerprint"] and m["version"] == hooks.HOOK_TEMPLATE_VERSION
        assert Path(m["path"]).exists()
    # 文件头带模板版本指纹
    adapter = (tmp_path / "h" / "tianji_claude_hook.py").read_text(
        encoding="utf-8")
    assert adapter.startswith("# tianji-hook-fingerprint:v1")
    doc = json.loads((tmp_path / "h" / "tianji-hooks.json")
                     .read_text(encoding="utf-8"))
    assert doc["_tianji"]["version"] == "v1"


def test_spawn_precheck_and_monitor_scan_refill(conn, controller, tmp_path):
    """验收 2: spawn 前必查拦截缺失(机械补);巡检自动补装(构造缺失场景)。"""
    _reg(conn, iso=tmp_path / "h2")
    hooks.install_instance(conn, "钩工")
    # 删掉一个生成物 → spawn 前对账机械补
    victim = tmp_path / "h2" / "tianji_statusline.py"
    victim.unlink()
    tid = ops.task_new(conn, controller, "活", request_id="hk-t")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"hk-{s}")
    did = ops.dispatch_issue(conn, controller, tid, "钩工",
                             request_id="hk-issue")["dispatch_id"]
    spawn(conn, "钩工", did)
    assert victim.exists()  # spawn 前对账已补
    # 巡检自动补(节流=0 强制扫)
    victim.unlink()
    r = hooks.scan_all(conn, throttle=0)
    assert r["scanned"]["钩工"] in ("regenerated", "ok")
    assert victim.exists()


def test_upgrade_regenerates(conn, controller, tmp_path, monkeypatch):
    """验收 3: 模板版本变化→对账机械重生成覆盖已装旧版。"""
    _reg(conn, iso=tmp_path / "h3")
    hooks.install_instance(conn, "钩工")
    target = tmp_path / "h3" / "tianji-hooks.json"
    old = target.read_text(encoding="utf-8")
    assert '"version": "v1"' in old
    monkeypatch.setattr(hooks, "HOOK_TEMPLATE_VERSION", "v2")
    r = hooks.reconcile_instance(conn, "钩工")
    assert r["status"] == "regenerated"
    assert '"version": "v2"' in target.read_text(encoding="utf-8")


def test_user_modified_protected_then_reinstall(conn, controller, tmp_path):
    """验收 4: 用户改过→不自动碰+差异报告+升级;裁决重置→reinstall 刷官方版。"""
    _reg(conn, iso=tmp_path / "h4")
    hooks.install_instance(conn, "钩工")
    target = tmp_path / "h4" / "tianji-hooks.json"
    target.write_text('{"我": "手工改的"}\n', encoding="utf-8")
    r = hooks.reconcile_instance(conn, "钩工")
    assert r["status"] == "user_modified"
    assert "手工改的" in target.read_text(encoding="utf-8")  # 不自动碰
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='hooks_reconcile_diff'").fetchone()
    assert conn.execute(
        "SELECT 1 FROM messages WHERE type='escalation' AND sender='hooks'"
    ).fetchone()
    # 用户裁决重置官方版
    hooks.install_instance(conn, "钩工")
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert "_tianji" in doc and "我" not in doc


def test_reinstall_selfcheck(conn, controller, tmp_path):
    """验收 5: 重装后装钩子自测——脚本可编译、JSON 可解析、指纹一致。"""
    _reg(conn, iso=tmp_path / "h5")
    hooks.install_instance(conn, "钩工")
    for m in _manifest(conn):
        p = Path(m["path"])
        content = p.read_text(encoding="utf-8")
        if p.suffix == ".py":
            compile(content, str(p), "exec")  # 语法可编译
        else:
            json.loads(content)               # JSON 可解析
        assert hooks._fp(content) == m["fingerprint"]  # 指纹一致
