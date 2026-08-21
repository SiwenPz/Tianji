"""组合红黑榜(票 25 验收 1-4): 视图类插件,只读无按钮。"""

import json
import os
from pathlib import Path

import pytest

from tianji import ops, plugins
from tianji.cockpit import render_snapshot, snapshot
from tianji.db import task_dir
from tianji.render import spawn


def _settle(conn, controller, worker, seq):
    """真实结算一单(产生实测数据)。"""
    tid = ops.task_new(conn, controller, f"活{seq}", request_id=f"rl-new-{seq}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"rl-{s}-{seq}")
    did = ops.dispatch_issue(conn, controller, tid, worker,
                             request_id=f"rl-issue-{seq}")["dispatch_id"]
    s = spawn(conn, worker, did)
    env = {**os.environ, "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")


def _key_entry(conn, controller, name, protocol="openai"):
    ops.config_set(conn, controller, f"key:{name}", json.dumps({
        "base_url": "https://x", "models": [{"id": "mA"}, {"id": "mB"}],
        "protocol": protocol}, ensure_ascii=False),
        request_id=f"rl-key-{name}")


def test_leaderboard_ranked_and_split_by_model(conn, controller, worker):
    """验收 1: 有实测组合按表现分降序;同 key 不同模型分两行。"""
    _key_entry(conn, controller, "k1")
    ops.config_set(conn, controller, "shell:codex", json.dumps(
        {"binding": "env", "protocols": ["openai"],
         "isolated_dir_mode": "env_home"}, ensure_ascii=False),
        request_id="rl-sh")
    ops.instance_register(conn, "高手", "codex", "mA", key_name="k1")
    ops.instance_register(conn, "低手", "codex", "mB", key_name="k1")
    _settle(conn, controller, "高手", 1)
    _settle(conn, controller, "低手", 2)
    conn.execute("UPDATE ability_profiles SET score=90 WHERE instance_name='高手'")
    conn.execute("UPDATE ability_profiles SET score=40 WHERE instance_name='低手'")
    rows = plugins.combo_leaderboard(conn)
    tested = [r for r in rows if r["status"] == "实测"]
    assert [r["score"] for r in tested] == sorted(
        [r["score"] for r in tested], reverse=True)
    combos = [r["combo"] for r in tested]
    assert "codex/k1/mA" in combos and "codex/k1/mB" in combos  # 同key不同模型两行
    assert combos.index("codex/k1/mA") < combos.index("codex/k1/mB")


def test_known_pits_in_row(conn, controller, worker):
    """验收 2: 每行带已知坑(实例档案)。"""
    _settle(conn, controller, worker["worker_id"], 3)
    ops.update_profile_notes(conn, worker["worker_id"], "网关偶断流")
    rows = plugins.combo_leaderboard(conn)
    row = [r for r in rows if r["instance"] == worker["worker_id"]][0]
    assert "网关偶断流" in row["notes"]


def test_cold_start_labels(conn, controller):
    """验收 3: 无数据标"待实测"不排名;先验声明标"宣称"。"""
    ops.instance_register(conn, "白板", "codex", "mA")
    ops.instance_register(conn, "声明", "codex", "mB",
                          profile_notes="用户说很强")
    rows = plugins.combo_leaderboard(conn)
    r1 = [r for r in rows if r["instance"] == "白板"][0]
    r2 = [r for r in rows if r["instance"] == "声明"][0]
    assert r1["status"] == "待实测"
    assert r2["status"] == "宣称"
    # 待实测不出现在排名区(渲染文本里只在"待实测"尾巴)
    block = plugins._render_view(
        {"name": "红黑榜", "config": {"title": "组合红黑榜",
                                      "source": "combo_leaderboard"}},
        {}, {"leaderboard": rows})
    ranked_part = block.split("待实测:")[0]
    assert "白板" not in ranked_part


def test_view_plugin_fail_open(conn, controller):
    """验收 4: 视图类插件形态(票 23 接口);失效退回提示不影响 cockpit 其余区块。"""
    # 出厂已注册(ensure_defaults 预置 plugin:红黑榜)
    p = plugins._load(conn, "红黑榜")
    assert p and p["type"] == "view" and p["enabled"] is True
    blocks = plugins.render_view_blocks(conn, snapshot(conn))
    assert any("[组合红黑榜]" in b for b in blocks)
    # 失效: 篡改数据源 → 退回默认块,cockpit 其余区块照渲染
    conn.execute("UPDATE configs SET value=? WHERE key='plugin:红黑榜'",
                 (json.dumps({"name": "红黑榜", "type": "view", "version": "v1",
                              "config": {"source": "坏源"}, "enabled": True,
                              "last_fingerprint": "", "last_version": ""},
                             ensure_ascii=False),))
    blocks2 = plugins.render_view_blocks(conn, snapshot(conn))
    assert any("插件渲染失败" in b for b in blocks2)
    out = render_snapshot(snapshot(conn), blocks2)
    assert "天机驾驶舱只读快照" in out and "插件展示块" in out
