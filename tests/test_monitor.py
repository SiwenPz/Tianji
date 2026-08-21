"""监控器(验收 5): 双阶梯只警告不判死+进程退出确定性重派+漏声称升级+进度超限。"""

import os
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import task_dir
from tianji.monitor import _tick
from tianji.render import spawn


def _active_dispatch(conn, controller, worker):
    """任务 dispatched+派单 issued(未开工),返回 (tid, did, worker_env)。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    from tianji.events import ingest_event
    env = {**os.environ, "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "s", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "s", "event_type": "pre_tool_use"})
    return tid, did, env


def _escalations(conn, kind=None):
    rows = conn.execute(
        "SELECT payload FROM messages WHERE type='escalation' ORDER BY seq").fetchall()
    import json
    return [json.loads(r["payload"]) for r in rows]


def test_process_exit_no_settle_reschedules(conn, controller, worker):
    """验收 5: 杀进程无结算→确定性重派并计入重派计数(自动动作,7.4②)。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE instance_registrations SET pid=99999999"
                 " WHERE instance_name=? AND status='active'",
                 (worker["worker_id"],))
    state = {}
    _tick(conn, state)
    # 旧派单 requeue
    assert ops.dispatch_get(conn, did)["status"] == "requeue"
    # 任务回 dispatched,计数+1,新派单自动发出
    t = ops.task_get(conn, tid)
    assert t["status"] == "dispatched" and t["retry_count"] == 1
    d2 = conn.execute(
        "SELECT id FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    assert d2["id"] != did
    assert ops.dispatch_get(conn, d2["id"])["status"] == "issued"
    # 登记行补关+异常标记(11.3)
    reg = conn.execute(
        "SELECT status, abnormal FROM instance_registrations"
        " WHERE instance_name=? ORDER BY id DESC LIMIT 1",
        (worker["worker_id"],)).fetchone()
    assert reg["status"] == "closed" and reg["abnormal"] == 1
    # 升级消息
    assert any("确定性重派" in e["reason"] for e in _escalations(conn))


def test_t2_silence_escalates_not_kills(conn, controller, worker):
    """验收 5: 超 T2 只警告不判死(任务状态不动,派单 escalate)。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE configs SET value='1' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='2' WHERE key='t2_seconds'")
    # 无新事件无字节,静默从最近事件起算: 事件和派单都推到过去
    conn.execute("UPDATE dispatches SET created_at=? WHERE id=?",
                 (ops.now() - 100, did))
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=?",
                 (ops.now() - 100, worker["worker_id"]))
    state = {}
    _tick(conn, state)  # 第一次: 标记
    _tick(conn, state)  # 连续两次确认(网络波动防误报)
    assert ops.dispatch_get(conn, did)["status"] == "stale"
    # 只警告不判死: 任务停在原状态(已开工=executing),不因静默被改判
    assert ops.task_get(conn, tid)["status"] == "executing"
    assert any("静默超 T2" in e["reason"] for e in _escalations(conn))


def test_progress_overrun_escalates(conn, controller, worker):
    """7.5 慢: 进度超 expect_min×2 → 升级,总控定换人/加时。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE dispatches SET created_at=?, expect_min=? WHERE id=?",
                 (ops.now() - 5000, 30, did))  # 已 83 分钟 > 60 分钟上限
    _tick(conn, {})
    assert any("进度超限" in e["reason"] for e in _escalations(conn))


def test_claimed_done_no_settle_escalates(conn, controller, worker):
    """验收 5/对账①: 事件说 done 但无结算→升级(漏声称兜底,7.4①)。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    from tianji.events import ingest_event
    ingest_event(conn, env, {"session_id": "s", "event_type": "session_end"})
    _tick(conn, {})
    assert any("事件说完成但账本无结算" in e["reason"]
               for e in _escalations(conn))


def test_spawn_failed_no_pid_requeue_on_timeout(conn, controller, worker):
    """7.4②: spawn 未实际拉起(pid IS NULL),超时后对账→requeue+确定性重派+计重派数。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    # spawn 失败: pid 为空,created_at 推到 T2 之前(默认 T2=600s)
    conn.execute(
        "UPDATE instance_registrations SET pid=NULL, created_at=? WHERE dispatch_id=?",
        (ops.now() - 700, did))
    state = {}
    _tick(conn, state)
    # 旧派单 requeue
    assert ops.dispatch_get(conn, did)["status"] == "requeue"
    # 任务回 dispatched,计数+1,新派单自动发出
    t = ops.task_get(conn, tid)
    assert t["status"] == "dispatched" and t["retry_count"] == 1
    d2 = conn.execute(
        "SELECT id FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    assert d2["id"] != did
    assert ops.dispatch_get(conn, d2["id"])["status"] == "issued"
    # 登记行补关+异常标记
    reg = conn.execute(
        "SELECT status, abnormal FROM instance_registrations"
        " WHERE dispatch_id=? ORDER BY id DESC LIMIT 1", (did,)).fetchone()
    assert reg["status"] == "closed" and reg["abnormal"] == 1
    # 升级消息
    assert any("确定性重派" in e["reason"] for e in _escalations(conn))


def test_verify_auto_triggered_by_monitor(conn, controller, worker):
    """8.3: 机械验收异步执行——监控器 tick 自动跑 reviewing 任务的验收门。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    conn.execute("UPDATE tasks SET verify_cmd=? WHERE id=?",
                 ("python -c print(1)", tid))
    _tick(conn, {})
    aud = conn.execute(
        "SELECT detail FROM audit WHERE action='mechanical_verify'").fetchone()
    assert aud is not None and '"ok": true' in aud["detail"]


def test_monitor_close_syncs_session_state_to_done(conn, controller, worker):
    """返修项3: 对账②补关登记行时同步 session_states → done。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    # _active_dispatch 已发 session_start + pre_tool_use(session_id="s")
    session_id = "s"
    # 确认 session_state 是 working
    ss = conn.execute("SELECT state FROM session_states WHERE session_id=?",
                      (session_id,)).fetchone()
    assert ss["state"] == "working"
    # 结算派单(dispatch done)
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("done", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    # 杀进程(pid=NULL)触发对账②补关
    conn.execute("UPDATE instance_registrations SET pid=NULL WHERE dispatch_id=?", (did,))
    _tick(conn, {})
    # session_states 已同步为 done
    ss2 = conn.execute("SELECT state FROM session_states WHERE session_id=?",
                       (session_id,)).fetchone()
    assert ss2["state"] == "done"


def test_verify_retriggered_after_rework_settle(conn, controller, worker):
    """8.3 去重粒度=(任务,最新已结算派单): 返修新结算必须再触发验收(2026-08 踩坑)。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    conn.execute("UPDATE tasks SET verify_cmd=? WHERE id=?",
                 ("python -c print(1)", tid))
    r1 = ops.mechanical_verify(conn, tid)
    assert r1.get("ok") is True
    # 模拟返修: 同任务新增一条已结算派单(任务仍 reviewing)
    conn.execute(
        "INSERT INTO dispatches (task_id, worker_id, worker_role, status,"
        " dcap_hash, expect_min, task_dir, payload, created_at, updated_at)"
        " SELECT task_id, worker_id, worker_role, status, dcap_hash,"
        " expect_min, task_dir, payload, created_at, updated_at"
        " FROM dispatches WHERE id=?", (did,))
    r2 = ops.mechanical_verify(conn, tid)
    assert not r2.get("already"), "返修后的新派单必须重新验收"
    assert r2.get("ok") is True
    n = conn.execute("SELECT COUNT(*) AS n FROM audit"
                     " WHERE action='mechanical_verify'").fetchone()["n"]
    assert n == 2


def test_hook_degraded_first_sample_baseline(conn, controller, worker, monkeypatch):
    """7.4③: 监控器重启首采样只建基线不误报;转录不涨不重复报(2026-08 踩坑)。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    # 结算使派单 done,登记行保持 active(模拟空闲但活着的会话);
    # pid 置当前测试进程,防止对账②按"pid 死"补关登记行
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    conn.execute("UPDATE instance_registrations SET pid=? WHERE status='active'",
                 (os.getpid(),))
    sizes = iter([100, 100, 150, 150, 150, 160, 170])
    monkeypatch.setattr(mon, "_transcript_bytes", lambda sid, shell="claude": next(sizes))
    state = {}
    _tick(conn, state)   # 首采样: 建基线,不报
    _tick(conn, state)   # 不涨: 不报
    assert not [e for e in _escalations(conn)
                if "钩子失效" in (e.get("reason") or "")]
    _tick(conn, state)   # 涨但事件在 T2 内(思考间隙): 不报(2026-08-17 周期性误报实证)
    assert not [e for e in _escalations(conn)
                if "钩子失效" in (e.get("reason") or "")]
    # 事件时间推到 T2 之外: 转录连续两拍增长且无新事件 = 真钩子失效,报一次
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=?",
                 (ops.now() - 7200, worker["worker_id"]))
    _tick(conn, state)   # 超 T2 无新行+转录涨: 第一拍,只记嫌疑不报
    _tick(conn, state)   # 不涨: 仍不报
    _tick(conn, state)   # 再涨: 连续第二拍,报一次
    _tick(conn, state)   # 不再涨: 不重复报
    hits = [e for e in _escalations(conn) if "钩子失效" in (e.get("reason") or "")]
    assert len(hits) == 1


def test_offline_suspends_ladders_and_resumes(conn, controller, worker, monkeypatch):
    """断网时双阶梯挂起不升级,恢复后自动追平。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE configs SET value='1' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='2' WHERE key='t2_seconds'")
    conn.execute("UPDATE dispatches SET created_at=? WHERE id=?", (ops.now() - 100, did))
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=?", (ops.now() - 100, worker["worker_id"]))
    # 在线 tick: 不升级(hits=1)
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "active"
    assert not [e for e in _escalations(conn) if "静默超" in (e.get("reason") or "")]
    # 断网 tick: 挂起不升级
    monkeypatch.setattr(mon, "_check_network", lambda state: True)
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "active"
    # 恢复在线 tick: 追平,连续两拍后 stale
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    _tick(conn, state)  # hits=2 -> stale
    assert ops.dispatch_get(conn, did)["status"] == "stale"
    assert any("静默超 T2" in e["reason"] for e in _escalations(conn))


def test_offline_suspicion_exemption(conn, controller, worker, monkeypatch):
    """断网时点进程死→恢复后重派,表现分豁免并留痕。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE instance_registrations SET pid=123456"
                 " WHERE instance_name=? AND status='active'",
                 (worker["worker_id"],))
    # 断网 tick: 进程死→挂起,标记 offline_suspicion
    monkeypatch.setattr(mon, "_check_network", lambda state: True)
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "active"
    reg = conn.execute(
        "SELECT offline_suspicion FROM instance_registrations"
        " WHERE instance_name=? ORDER BY id DESC LIMIT 1",
        (worker["worker_id"],)).fetchone()
    assert reg["offline_suspicion"] == 1
    # 恢复在线 tick: 进程死→重派,表现分豁免
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "requeue"
    # 表现分豁免留痕在 audit,不在 escalation reason
    aud = conn.execute("SELECT detail FROM audit WHERE action='monitor_score_exempt'").fetchone()
    assert aud is not None


def test_background_subagent_exempts_ladder(conn, controller, worker, monkeypatch):
    """未收尾 subagent 使字节停不升级,配对 stop 后恢复阶梯。"""
    import tianji.monitor as mon
    import os
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE configs SET value='1' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='2' WHERE key='t2_seconds'")
    conn.execute("UPDATE dispatches SET created_at=? WHERE id=?", (ops.now() - 100, did))
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=?", (ops.now() - 100, worker["worker_id"]))
    # 在线+进程活+未收尾 subagent → 后台豁免,不升级
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    conn.execute("UPDATE instance_registrations SET pid=? WHERE instance_name=? AND status='active'",
                 (os.getpid(), worker["worker_id"]))
    from tianji.events import ingest_event
    ingest_event(conn, env, {"session_id": "s", "event_type": "subagent_start"})
    # 把 subagent_start 时间也推过去,制造持续静默
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=? AND json_extract(payload,'$.event_type')='subagent_start'",
                 (ops.now() - 100, worker["worker_id"]))
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "active"
    # 配对 subagent_stop 后恢复阶梯: 再两拍 stale
    ingest_event(conn, env, {"session_id": "s", "event_type": "subagent_stop"})
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=? AND json_extract(payload,'$.event_type')='subagent_stop'",
                 (ops.now() - 100, worker["worker_id"]))
    state = {}
    _tick(conn, state)  # hits=1
    _tick(conn, state)  # hits=2 -> stale
    assert ops.dispatch_get(conn, did)["status"] == "stale"


def test_taskbook_contains_background_clause(conn, controller, worker):
    """任务书渲染含后台任务清单条款。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    text = Path(s["taskbook"]).read_text(encoding="utf-8")
    assert "后台任务清单" in text


def test_offline_recovery_catches_up_reconcile2(conn, controller, worker, monkeypatch):
    """断网时进程死未重派,恢复后对账②追平。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE instance_registrations SET pid=99999999"
                 " WHERE instance_name=? AND status='active'",
                 (worker["worker_id"],))
    # 断网 tick: 挂起不重派
    monkeypatch.setattr(mon, "_check_network", lambda state: True)
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "active"
    # 恢复在线 tick: 重派
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "requeue"


def test_instance_set_pid(conn, controller, worker):
    """手动回填 pid(外部拉起通道,7.4②)。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    # spawn 后 pid 应为空(run=False)
    reg = conn.execute(
        "SELECT pid FROM instance_registrations"
        " WHERE instance_name=? AND dispatch_id=?",
        (worker["worker_id"], did)).fetchone()
    assert reg["pid"] is None
    ops.instance_set_pid(conn, controller, worker["worker_id"], 99988877,
                         request_id="r-pid")
    reg = conn.execute(
        "SELECT pid FROM instance_registrations"
        " WHERE instance_name=? AND dispatch_id=?",
        (worker["worker_id"], did)).fetchone()
    assert reg["pid"] == 99988877
    aud = conn.execute(
        "SELECT detail FROM audit WHERE action='instance_set_pid'"
    ).fetchone()
    assert aud is not None


def test_reviewing_exempts_ladder(conn, controller, worker, monkeypatch):
    """审核态 reviewing 豁免活性阶梯,不升级不判死。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE configs SET value='1' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='2' WHERE key='t2_seconds'")
    conn.execute("UPDATE dispatches SET created_at=? WHERE id=?", (ops.now() - 100, did))
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=?", (ops.now() - 100, worker["worker_id"]))
    conn.execute("UPDATE tasks SET status='reviewing' WHERE id=?", (tid,))
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    state = {}
    _tick(conn, state)
    assert ops.dispatch_get(conn, did)["status"] == "active"
    assert not [e for e in _escalations(conn) if "静默超" in (e.get("reason") or "")]


def test_dsh_transcript_bytes(conn, controller, worker, tmp_path, monkeypatch):
    """dsh 壳使用 DSH_HOME 转录源,claude 壳使用默认路径。"""
    import tianji.monitor as mon
    # 创建 dsh 转录文件
    dsh_home = tmp_path / "dsh"
    sess = dsh_home / "sessions" / "--cwd--" / "s"
    sess.mkdir(parents=True)
    f = sess / "session.jsonl.zstd"
    f.write_text("hello", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    # dsh 壳
    assert mon._transcript_bytes("s", shell="dsh") == 5
    # claude 壳(未创建文件)
    assert mon._transcript_bytes("s", shell="claude") == 0


def test_dsh_worker_not_misjudged_stale(conn, controller, tmp_path, monkeypatch):
    """dsh 壳工人在 _tick 下不被误标 stale(硬验收): 转录字节增长生效。"""
    import tianji.monitor as mon
    from tianji.events import ingest_event
    from tianji.render import spawn
    # 注册 dsh 实例
    r = ops.instance_register(conn, "dsh-worker", "dsh", "dsh-model")
    dsh_worker = {"worker_id": "dsh-worker", "secret": r["secret"]}
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, dsh_worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, dsh_worker["worker_id"], did)
    env = {**os.environ, "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "dsh-session", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "dsh-session", "event_type": "pre_tool_use"})
    # 构造 DSH_HOME 转录文件,字节增长
    dsh_home = tmp_path / "dsh"
    sess = dsh_home / "sessions" / "--cwd--" / "dsh-session"
    sess.mkdir(parents=True)
    f = sess / "session.jsonl.zstd"
    f.write_text("hello", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    state = {}
    _tick(conn, state)
    # dsh 转录字节增长,不应被标 stale
    assert ops.dispatch_get(conn, did)["status"] == "active"


def test_existing_ledger_migrates_offline_suspicion(tianji_home):
    """既有账本(旧 schema 无 offline_suspicion 列)connect 后自动补列(审核返修点)。"""
    import sqlite3
    from tianji.db import connect, ledger_path
    # 手工建一个旧 schema 账本(无 offline_suspicion 列)
    ledger_path().parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(ledger_path())
    old.execute(
        "CREATE TABLE instance_registrations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, instance_name TEXT,"
        " dispatch_id INTEGER, status TEXT, session_id TEXT, dcap_hash TEXT,"
        " task_path TEXT, pid INTEGER, abnormal INTEGER DEFAULT 0,"
        " created_at INTEGER, closed_at INTEGER)")
    old.commit()
    old.close()
    conn = connect()
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(instance_registrations)")}
    assert "offline_suspicion" in cols
    # 既有行不受影响,新列默认 0
    conn.close()


def test_reviewer_dispatch_exit_no_task_reschedule(conn, controller, worker):
    """对账②角色盲区修复(2026-08-18 实锤): 审核派单进程退出→requeue+升级,
    不触发任务重派/计数/扣分(防误杀任务)。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    # 任务进 reviewing(结算),发审核派单
    rp = Path(task_dir(did)) / "report.md"
    rp.write_text("报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    dr = ops.dispatch_issue(conn, controller, tid, "总控", role="reviewer",
                            axis="spec", request_id="r-rev")["dispatch_id"]
    from tianji.render import spawn as _spawn
    s = _spawn(conn, "总控", dr)
    # 审核进程死(回填死 pid),对账②
    conn.execute("UPDATE instance_registrations SET pid=99999999"
                 " WHERE dispatch_id=?", (dr,))
    old_retry = ops.task_get(conn, tid)["retry_count"]
    _tick(conn, {})
    assert ops.dispatch_get(conn, dr)["status"] == "requeue"
    t = ops.task_get(conn, tid)
    assert t["status"] == "reviewing" and t["retry_count"] == old_retry
    assert any("审核派单进程退出" in e["reason"] for e in _escalations(conn))


def test_dispatch_revive_stale_to_active(conn, controller, worker):
    """stale 复活(误杀比漏报贵): 总控确认工人活着,stale→active,结算恢复可走。"""
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE dispatches SET status='stale' WHERE id=?", (did,))
    with pytest.raises(PermissionError):
        ops.dispatch_revive(conn, worker, did)
    r = ops.dispatch_revive(conn, controller, did, reason="工人已恢复",
                            request_id="r-revive")
    assert r["to"] == "active"
    assert ops.dispatch_get(conn, did)["status"] == "active"
    # 非 stale 不可复活
    with pytest.raises(ValueError, match="不可复活"):
        ops.dispatch_revive(conn, controller, did, request_id="r-revive2")


def test_no_hook_worker_alive_not_stale(conn, controller, worker, monkeypatch):
    """无钩子壳(零事件+零字节证据)但进程活: 不标 stale,只升级提示(7.5 三层证据②)。

    2026-08-19 实证: dsh 工人未装钩子时事件/字节活性都拿不到,老派单第一拍
    必超 T2 被标 stale,worker_done 结算通道被 5.4 stale 拒绝码卡死。
    """
    import tianji.monitor as mon
    import os
    tid, did, env = _active_dispatch(conn, controller, worker)
    conn.execute("UPDATE configs SET value='1' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='2' WHERE key='t2_seconds'")
    # 派单创建时间推到 T2 之前很久(模拟老派单),且无事件/转录证据
    conn.execute("UPDATE dispatches SET created_at=? WHERE id=?", (ops.now() - 1000, did))
    conn.execute("DELETE FROM messages WHERE type='event' AND sender=?",
                 (worker["worker_id"],))
    # 进程活(用自己 pid)
    conn.execute("UPDATE instance_registrations SET pid=?"
                 " WHERE dispatch_id=?", (os.getpid(), did))
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    state = {}
    _tick(conn, state)  # hits=1
    _tick(conn, state)  # hits=2,若误判则 stale
    assert ops.dispatch_get(conn, did)["status"] == "active"
    # 进程死+零事件 → 对账②确定性重派(requeue,7.4② 优先于活性阶梯 stale)
    conn.execute("UPDATE instance_registrations SET pid=99999999"
                 " WHERE dispatch_id=?", (did,))
    _tick(conn, {})  # 新 state, hits 重新累计
    _tick(conn, {})
    assert ops.dispatch_get(conn, did)["status"] in ("requeue", "stale")


def test_stop_t1_escalation_carries_nudge_hint(conn, controller, worker, monkeypatch):
    """7.5 续推通道: stop(答完一轮未打断)后无新活动超 T1 → 升级带"建议 nudge"线索。"""
    import tianji.monitor as mon
    tid, did, env = _active_dispatch(conn, controller, worker)
    from tianji.events import ingest_event
    ingest_event(conn, env, {"session_id": "s", "event_type": "stop",
                             "is_interrupt": False})
    conn.execute("UPDATE configs SET value='1' WHERE key='t1_seconds'")
    conn.execute("UPDATE configs SET value='500' WHERE key='t2_seconds'")
    conn.execute("UPDATE dispatches SET created_at=? WHERE id=?",
                 (ops.now() - 100, did))
    conn.execute("UPDATE messages SET ts=? WHERE type='event' AND sender=?",
                 (ops.now() - 100, worker["worker_id"]))
    monkeypatch.setattr(mon, "_check_network", lambda state: False)
    _tick(conn, {})
    hits = [e for e in _escalations(conn) if "nudge" in e.get("reason", "")]
    assert hits, "T1 升级应带 nudge 提示线索"
    assert "tianji nudge" in hits[0]["reason"]
    assert str(did) in hits[0]["reason"]
