"""实例配置就地修改(票 28 验收): instance update 不重建实例,组合校验复用 13.4。"""

import os
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import task_dir
from tianji.render import spawn


def test_update_model_ok_and_audit(conn, controller):
    """验收 1: 改 model 成功,审计含旧/新值,其余字段不变,画像同步。"""
    ops.instance_register(conn, "改改", "claude", "deepseek-v4-flash",
                          launch_cmd="claude", context_window=100)
    r = ops.instance_update(conn, controller, "改改", model="deepseek-v4-pro",
                            request_id="u1")
    assert r["updated"] == {"model": "deepseek-v4-pro"}
    assert r["old"] == {"model": "deepseek-v4-flash"}
    row = conn.execute("SELECT * FROM instances WHERE name='改改'").fetchone()
    assert row["model"] == "deepseek-v4-pro"
    assert row["launch_cmd"] == "claude"  # 其余字段不变
    p = conn.execute("SELECT model, context_window FROM ability_profiles"
                     " WHERE instance_name='改改'").fetchone()
    assert p["model"] == "deepseek-v4-pro"  # 画像同步
    assert p["context_window"] == 100
    a = conn.execute("SELECT detail FROM audit WHERE action='instance_update'"
                     ).fetchone()
    assert a is not None and "deepseek-v4-pro" in a["detail"]


def test_update_invalid_key_rejected(conn, controller):
    """验收 2: 改成不存在的 key 引用被 13.4 组合校验机械拒绝。"""
    ops.instance_register(conn, "改改2", "claude", "deepseek-v4-flash",
                          launch_cmd="claude")
    with pytest.raises(ValueError, match="组合不合法"):
        ops.instance_update(conn, controller, "改改2", key_name="不存在的key",
                            request_id="u2")


def test_update_with_active_dispatch_allowed(conn, controller, worker):
    """验收 3: 在途派单允许改(只影响下一次 spawn),在途派单结算不受影响。"""
    tid = ops.task_new(conn, controller, "在途任务", request_id="u3-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"u3-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="u3-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    r = ops.instance_update(conn, controller, worker["worker_id"],
                            launch_cmd="python new_worker.py", request_id="u3-upd")
    assert r["updated"] == {"launch_cmd": "python new_worker.py"}
    # 在途派单照常结算(env/dcap 已注入不受影响)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("成果报告", encoding="utf-8")
    st = ops.dispatch_settle(conn, env, did, str(rp), "ok")
    assert st["status"] == "done"


def test_update_non_controller_rejected(conn, controller):
    """验收 4: 非总控身份调用被拒。"""
    ops.instance_register(conn, "改改3", "claude", "deepseek-v4-flash",
                          launch_cmd="claude")
    with pytest.raises(PermissionError):
        ops.instance_update(conn, {"worker_id": "路人", "secret": "x"},
                            "改改3", model="deepseek-v4-pro", request_id="u4")


def test_update_no_change_rejected(conn, controller):
    """边界: 无变更字段(全空或与现值相同)明确报错。"""
    ops.instance_register(conn, "改改4", "claude", "deepseek-v4-flash",
                          launch_cmd="claude")
    with pytest.raises(ValueError, match="无变更字段"):
        ops.instance_update(conn, controller, "改改4", request_id="u5")
    with pytest.raises(ValueError, match="无变更字段"):
        ops.instance_update(conn, controller, "改改4",
                            model="deepseek-v4-flash", request_id="u6")


def test_update_unknown_instance(conn, controller):
    with pytest.raises(KeyError):
        ops.instance_update(conn, controller, "不存在", model="m", request_id="u7")
