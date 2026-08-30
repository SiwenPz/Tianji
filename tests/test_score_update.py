"""表现分 5 档更新(票 06 验收 2/4): 加权移动平均+5 档分值。"""

import json
import time

import pytest

from tianji import ops


def _register_shell_key(conn, controller, key_name):
    ops.config_set(conn, controller, "shell:codex", json.dumps({"binding": "env", "protocols": ["stdio"], "isolated_dir_mode": "env_home"}, ensure_ascii=False), request_id=f"r-sh-{key_name}")
    ops.config_set(conn, controller, f"key:{key_name}", json.dumps({"base_url": f"https://api.example.com/{key_name}", "models": [{"id": "step-router-v1", "display_name": "R", "context_window": 200000}], "protocol": "stdio"}, ensure_ascii=False), request_id=f"r-k-{key_name}")


def _register_and_set_score(conn, controller, name, score=60, key_name=None):
    if key_name is None:
        key_name = f"sk-{name}"
    _register_shell_key(conn, controller, key_name)
    ops.instance_register(conn, name, "codex", "step-router-v1", key_name=key_name)
    conn.execute(
        "UPDATE ability_profiles SET score=? WHERE instance_name=?",
        (score, name))
    return name


_flow_seq = [0]


def _to_executing(conn, controller, worker_name, verify_cmd=None):
    # request_id 全局唯一: 同一测试内多次走流程时幂等键不复用,
    # 否则第二次 task_new/dispatch_issue 命中幂等缓存返回首次的 id(踩坑)
    _flow_seq[0] += 1
    q = f"{_flow_seq[0]}-{worker_name}"
    tid = ops.task_new(conn, controller, "任务", request_id=f"r-new-{q}")["task_id"]
    if verify_cmd:
        # 验收命令须在计划确认前写入(8.3),new 状态即写
        ops.task_set_verify_cmd(conn, controller, tid, verify_cmd,
                                request_id=f"r-vc-{q}")
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}-{q}")
    did = ops.dispatch_issue(conn, controller, tid, worker_name,
                             request_id=f"r-issue-{q}")["dispatch_id"]
    from tianji.render import spawn
    from tianji.events import ingest_event
    s = spawn(conn, worker_name, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "pre_tool_use"})
    return tid, did, env


import os


def _to_reviewing(conn, controller, worker_name, verify_cmd=None):
    """任务走到 reviewing(结算完成,验收前)。返回 (task_id, dispatch_id, worker_env)。"""
    from pathlib import Path
    from tianji.db import task_dir
    tid, did, env = _to_executing(conn, controller, worker_name,
                                  verify_cmd=verify_cmd)
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    return tid, did, env


class TestScoreUpdate:
    """表现分按 5 档事件更新,近 10 单加权移动平均。"""

    def test_on_time_settle_adds_score(self, conn, controller):
        name = _register_and_set_score(conn, controller, "on-time", score=60)
        tid, did, env = _to_executing(conn, controller, name)
        from pathlib import Path
        from tianji.db import task_dir
        rp = Path(task_dir(did)) / "report.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("ok", encoding="utf-8")
        # expect_min 默认 30, 实际在 expect_min 内 → on_time
        new_score = ops.update_score(conn, name, "on_time", expect_min=30,
                                     actual_minutes=10)
        assert new_score > 60

    def test_overtime_settle_adds_less(self, conn, controller):
        name = _register_and_set_score(conn, controller, "overtime", score=60)
        # expect_min=30, actual=45 → overtime
        new_score = ops.update_score(conn, name, "overtime", expect_min=30,
                                     actual_minutes=45)
        assert new_score > 60
        # on_time(10) > overtime(+5 权重)

    def test_progress_exceed_decreases_score(self, conn, controller):
        name = _register_and_set_score(conn, controller, "progress", score=60)
        new_score = ops.update_score(conn, name, "progress_exceed",
                                     expect_min=30, actual_minutes=70)
        assert new_score < 60

    def test_review_reject_decreases_score(self, conn, controller):
        name = _register_and_set_score(conn, controller, "rejected", score=60)
        new_score = ops.update_score(conn, name, "review_reject",
                                     expect_min=30)
        assert new_score < 60

    def test_process_dead_decreases_score(self, conn, controller):
        name = _register_and_set_score(conn, controller, "dead", score=60)
        new_score = ops.update_score(conn, name, "process_dead", expect_min=30)
        assert new_score < 60

    def test_weighted_moving_average_keeps_recent(self, conn, controller):
        name = _register_and_set_score(conn, controller, "wma", score=60)
        # 连续 5 次 on_time(+10), score 应明显上升
        for _ in range(5):
            ops.update_score(conn, name, "on_time", expect_min=30,
                             actual_minutes=10)
        row = conn.execute(
            "SELECT score FROM ability_profiles WHERE instance_name=?",
            (name,)).fetchone()
        assert row["score"] > 60
        history = json.loads(
            conn.execute(
                "SELECT score_history FROM ability_profiles WHERE instance_name=?",
                (name,)).fetchone()["score_history"])
        assert len(history) == 5

    def test_score_clamped_between_0_and_100(self, conn, controller):
        name = _register_and_set_score(conn, controller, "clamp", score=5)
        # 多次 progress_exceed(-8) 不应低于 0
        for _ in range(20):
            ops.update_score(conn, name, "progress_exceed", expect_min=30,
                             actual_minutes=100)
        row = conn.execute(
            "SELECT score FROM ability_profiles WHERE instance_name=?",
            (name,)).fetchone()
        assert 0 <= row["score"] <= 100

    def test_score_history_truncated_to_10(self, conn, controller):
        name = _register_and_set_score(conn, controller, "trunc", score=60)
        for _ in range(15):
            ops.update_score(conn, name, "on_time", expect_min=30,
                             actual_minutes=10)
        history = json.loads(
            conn.execute(
                "SELECT score_history FROM ability_profiles WHERE instance_name=?",
                (name,)).fetchone()["score_history"])
        assert len(history) == 10


def _score_and_history(conn, name):
    row = conn.execute(
        "SELECT score, score_history FROM ability_profiles WHERE instance_name=?",
        (name,)).fetchone()
    return row["score"], json.loads(row["score_history"] or "[]")


class TestMechanicalFail:
    """mechanical_fail 事件(8.3 机械验收驳回): -15 分档,与 review_reject 同档(9.4)。"""

    def test_mechanical_fail_deducts_15(self, conn, controller):
        """update_score 映射表有 mechanical_fail 分支: delta=-15,事件名不失真。"""
        name = _register_and_set_score(conn, controller, "mech", score=60)
        new_score = ops.update_score(conn, name, "mechanical_fail", expect_min=30)
        assert new_score < 60
        _, history = _score_and_history(conn, name)
        assert history[-1]["event"] == "mechanical_fail"
        assert history[-1]["delta"] == -15

    def test_mechanical_fail_same_tier_as_review_reject(self, conn, controller):
        """同 60 分起点: mechanical_fail 与 review_reject 扣分结果完全一致。"""
        a = _register_and_set_score(conn, controller, "mech-tier", score=60)
        b = _register_and_set_score(conn, controller, "rej-tier", score=60)
        sa = ops.update_score(conn, a, "mechanical_fail", expect_min=30)
        sb = ops.update_score(conn, b, "review_reject", expect_min=30)
        assert sa == sb
        _, ha = _score_and_history(conn, a)
        _, hb = _score_and_history(conn, b)
        assert ha[-1]["delta"] == hb[-1]["delta"] == -15

    def test_mechanical_verify_gate_fail_deducts_15(self, conn, controller):
        """真走机械验收门(ops.mechanical_verify): 验收命令失败 → mechanical_fail
        -15 + 驳回重派(覆盖 ops.py 机械验收失败分支的 update_score 调用)。"""
        name = _register_and_set_score(conn, controller, "mech-gate", score=60)
        tid, did, env = _to_reviewing(
            conn, controller, name,
            verify_cmd='python -c "import sys; sys.exit(1)"')
        r = ops.mechanical_verify(conn, tid)
        assert r["ok"] is False and r["rescheduled"] is True
        score, history = _score_and_history(conn, name)
        assert history[-1]["event"] == "mechanical_fail"
        assert history[-1]["delta"] == -15
        assert score < 60
        # 驳回=重派: 任务回 dispatched,计数+1
        t = ops.task_get(conn, tid)
        assert t["status"] == "dispatched" and t["retry_count"] == 1

    def test_mechanical_verify_missing_profile_no_crash(self, conn, controller):
        """画像行缺失: 机械验收失败路径 KeyError 保护,不炸验收事务
        (对齐 monitor.py 进程退出扣分口径)。"""
        name = _register_and_set_score(conn, controller, "mech-noprof", score=60)
        tid, did, env = _to_reviewing(
            conn, controller, name,
            verify_cmd='python -c "import sys; sys.exit(1)"')
        conn.execute("DELETE FROM ability_profiles WHERE instance_name=?", (name,))
        # 不抛 KeyError,驳回重派照常
        r = ops.mechanical_verify(conn, tid)
        assert r["ok"] is False and r["rescheduled"] is True
        assert ops.task_get(conn, tid)["status"] == "dispatched"


class TestProcessDeadPathConsistency:
    """process_dead 两条扣分路径(结算侧 _reschedule 联动 / 监控侧 _tick 对账②)
    产生一致 score+history: 同事件名、同 -10 档位、只记一条(不双扣不漏扣)。"""

    def test_settle_side_and_monitor_side_consistent(self, conn, controller,
                                                     worker, monkeypatch):
        from tianji import monitor as monitor_mod
        from tianji.db import tx
        from tianji.monitor import _tick
        # 活性验收不依赖本机外网连通性(与 test_monitor 同口径)
        monkeypatch.setattr(monitor_mod, "_check_network", lambda state: False)

        # ---- 监控侧: _tick 对账② 发现进程退出无结算 →
        # update_score(process_dead) + _reschedule(skip_score=True) ----
        w_mon = worker["worker_id"]
        conn.execute(
            "UPDATE ability_profiles SET score=60, score_history='[]'"
            " WHERE instance_name=?", (w_mon,))
        tid1, did1, env1 = _to_executing(conn, controller, w_mon)
        conn.execute(
            "UPDATE instance_registrations SET pid=99999999"
            " WHERE instance_name=? AND status='active'", (w_mon,))
        _tick(conn, {})
        # 防恒真: 监控侧确实发生了确定性重派
        assert ops.dispatch_get(conn, did1)["status"] == "requeue"
        s_mon, h_mon = _score_and_history(conn, w_mon)

        # ---- 结算侧: _reschedule 内部表现分联动(skip_score=False,
        # reason 命中"进程退出/无结算"关键词,ops.py 联动分支) ----
        w_set = _register_and_set_score(conn, controller, "settle-dead", score=60)
        conn.execute(
            "UPDATE ability_profiles SET score_history='[]'"
            " WHERE instance_name=?", (w_set,))
        tid2, did2, env2 = _to_executing(conn, controller, w_set)
        with tx(conn) as c:
            ops._reschedule(c, tid2, w_set, "进程退出无结算(结算侧联动扣分)")
        s_set, h_set = _score_and_history(conn, w_set)

        # 两条路径: 同样恰好一条 process_dead/-10,最终分一致
        assert [(h["event"], h["delta"]) for h in h_mon] == [("process_dead", -10)]
        assert [(h["event"], h["delta"]) for h in h_set] == [("process_dead", -10)]
        assert s_mon == s_set
