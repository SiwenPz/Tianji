"""监控器(7.x): tick 循环,活性双阶梯+对账三件+停滞分级。

只判信号不判原因(0.3-3): 字节计数不解析内容(躲 GBK);自动动作仅两件
(进程退出无结算→重派、进度超限→升级),其余警告+人看。
监控器无状态(运行知识全在账本);内存采样缓存(字节基线/去重)丢失无损。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import socket
from pathlib import Path

from . import events, messages, ops
from .db import connect, now, tx
from .state import check_dispatch_transition


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        # Windows: os.kill(pid, 0) 是 TerminateProcess 无条件强杀,绝不能用作活性探测
        # (7.4② 平台差异,2026-08 实测踩坑: 监控器每 tick 杀光登记进程)。
        # 探测三件套:
        #   OpenProcess 开句柄 → 失败时判 GetLastError:
        #     ERROR_INVALID_PARAMETER=进程不存在(死);
        #     ERROR_ACCESS_DENIED=权限不足(按活,0.3-4 误杀比漏报贵)。
        #   WaitForSingleObject 消"假活"(进程已退出但句柄未全关时 OpenProcess 仍成功):
        #     WAIT_TIMEOUT=活,WAIT_OBJECT_0=已退出。
        import ctypes
        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x00000000
        WAIT_TIMEOUT = 0x00000102
        ERROR_INVALID_PARAMETER = 87
        ERROR_ACCESS_DENIED = 5
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return k32.GetLastError() == ERROR_ACCESS_DENIED
        try:
            return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        # POSIX: 僵尸进程(Z)=已死未收尸,判死(票56实测:supervisor持有Popen不wait,
        # 子进程死后变僵尸,os.kill(pid,0)仍成功→探活误判→守护不重拉。Windows分支
        # 的WaitForSingleObject天然正确,不受影响)
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                state = f.read().rsplit(")", 1)[-1].split()[0]
            return state != "Z"
        except (OSError, IndexError):
            return True
    except OSError:
        return False


def _transcript_path(session_id: str, shell: str = "claude",
                     home_dir: "Path | None" = None,
                     isolated_dir: str = "") -> "Path | None":
    """按壳条目 transcript 数据定位转录文件(不解析内容);委托 transcript_parser 统一处理。"""
    if not session_id:
        return None
    from .adapters import transcript_parser
    return transcript_parser.transcript_path(shell, session_id,
                                             home_dir=home_dir,
                                             isolated_dir=isolated_dir)


def _transcript_bytes(session_id: str, shell: str = "claude",
                      home_dir: "Path | None" = None,
                      isolated_dir: str = "") -> int:
    """档 2 转录文件字节计数(不解析内容,躲 GBK 控制台坑)。"""
    p = _transcript_path(session_id, shell, home_dir=home_dir,
                         isolated_dir=isolated_dir)
    if not p:
        return 0
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _escalate(conn, state, task_id, worker_id, reason, kind):
    """升级消息带诊断线索(7.5 归因给人看);短间隔去重防刷屏。"""
    key = (task_id, worker_id, kind)
    last = state.get("escalated", {}).get(key)
    if last and now() - last < 300:
        return
    state.setdefault("escalated", {})[key] = now()
    with tx(conn) as c:
        messages.send(c, "escalation", "monitor",
                      {"task_id": task_id, "worker_id": worker_id,
                       "reason": reason}, "controller")
        ops.audit(c, "monitor_escalate",
                  {"task_id": task_id, "worker_id": worker_id, "reason": reason})


def _mark_dispatch_stale(conn, dispatch_id, task_id, to_state):
    with tx(conn) as c:
        d = c.execute("SELECT status FROM dispatches WHERE id=?",
                      (dispatch_id,)).fetchone()
        if d and check_dispatch_transition(d["status"], to_state):
            c.execute("UPDATE dispatches SET status=?, updated_at=? WHERE id=?",
                      (to_state, now(), dispatch_id))
            ops.audit(c, "monitor_dispatch_stale",
                      {"dispatch_id": dispatch_id, "to": to_state})



def _check_network(state: dict) -> bool:
    """探测本机外网连通性(0.3-3): 多地址去单点误判,结果缓存 60s。"""
    if not state:
        state["offline"] = False
    last = state.get("offline_last_check") or 0
    now_ts = now()
    if now_ts - last < 60:
        return bool(state.get("offline"))
    targets = [("8.8.8.8", 53), ("1.1.1.1", 53), ("www.baidu.com", 80)]
    offline = True
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=3):
                offline = False
                break
        except OSError:
            continue
    state["offline_last_check"] = now_ts
    state["offline"] = offline
    return offline


def _check_tier3_capability(conn, shell: str, pid: int = 0) -> bool:
    """档 3 进程活性豁免(附录 E.7): 壳条目声明 tier3_process_alive 则调用对应适配器验证。

    不是"声名即豁免"——要和旧代码等价: 模板说支持 tier3 → 调 adapter 实际验证进程;
    验证通过才豁免钩子失效判定,否则按无 tier3 处理。
    """
    # 先读账本壳条目(决定用哪个 adapter);缺则读模板兜底
    entry = conn.execute(
        "SELECT value FROM configs WHERE key=?",
        (f"integration_shell:{shell}",)).fetchone()
    adapter = None
    if entry:
        data = json.loads(entry["value"])
        if data.get("capabilities", {}).get("tier3_process_alive"):
            adapter = data.get("adapter")
    if adapter is None:
        try:
            from .adapters.template import get_template
            tpl = get_template(shell)
            if not tpl.capabilities.get("tier3_process_alive"):
                return False
            adapter = tpl.adapter
        except KeyError:
            return False
    # 按 adapter 调用实际进程验证(codex_exec_alive);缺 adapter 或不支持 → False
    if not pid or not adapter:
        return False
    try:
        output_file = adapter.get("output_file", "") if isinstance(adapter, dict) else ""
        if "codex" in output_file:
            from .adapters.codex_exec import codex_exec_alive
            return codex_exec_alive(pid)
    except Exception:
        pass
    return False


def _unclosed_subagents(conn, worker_id, since_ts):
    if not since_ts:
        return 0
    start = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE type='event' AND sender=?"
        " AND json_extract(payload,'$.event_type')='subagent_start' AND ts>=?",
        (worker_id, since_ts)).fetchone()["c"]
    stop = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE type='event' AND sender=?"
        " AND json_extract(payload,'$.event_type')='subagent_stop' AND ts>=?",
        (worker_id, since_ts)).fetchone()["c"]
    return max(start - stop, 0)


def _pending_worker_help(conn, worker_id: str) -> dict | None:
    """返回该工人最近一条未答复的 worker_help,无则 None。"""
    row = conn.execute(
        "SELECT seq, ts, payload FROM messages WHERE type='worker_help' AND sender=? "
        "ORDER BY seq DESC LIMIT 1", (worker_id,)).fetchone()
    if not row:
        return None
    # worker_help_reply 收件角色固定为 worker;须按 payload.worker_id 精确匹配,
    # 否则多工人交错求助时会把别人的 reply 误判为己方答复(票 20 返修,审核实证)
    for r in conn.execute(
        "SELECT payload FROM messages WHERE type='worker_help_reply' AND recipient_role='worker' "
        "AND ts > ?", (row["ts"],)):
        try:
            rp = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            rp = {}
        if rp.get("worker_id") == worker_id:
            return None
    return dict(row)


def _append_breakpoint_summary(conn, dispatch_id, worker_id):
    """断点摘要(7.5): 旧会话转录尾部+任务书+产物清单,随重派写入 payload。"""
    import json
    from pathlib import Path

    d = conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if not d:
        return
    task_path = Path(d["task_dir"]) / "task.md"
    taskbook = task_path.read_text(encoding="utf-8") if task_path.is_file() else ""
    tdir = Path(d["task_dir"])
    artifacts = sorted(p.name for p in tdir.iterdir() if p.is_file()) if tdir.is_dir() else []
    reg = conn.execute(
        "SELECT session_id, instance_name FROM instance_registrations"
        " WHERE instance_name=? ORDER BY id DESC LIMIT 1",
        (worker_id,)).fetchone()
    transcript_tail = ""
    if reg and reg["session_id"]:
        shell = "claude"
        inst = conn.execute(
            "SELECT shell FROM instances WHERE name=?",
            (reg["instance_name"],)).fetchone()
        if inst:
            shell = inst["shell"]
        p = _transcript_path(reg["session_id"], shell)
        if p:
            try:
                lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                transcript_tail = "\n".join(lines[-20:])
            except Exception:
                transcript_tail = ""
    payload = json.loads(d["payload"] or "{}")
    payload["breakpoint_summary"] = {
        "taskbook": taskbook,
        "artifacts": artifacts,
        "transcript_tail": transcript_tail,
    }
    conn.execute(
        "UPDATE dispatches SET payload=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), dispatch_id))


def _deduct_score(conn, worker_id, delta, offline_suspicion=False):
    """表现分扣减(9.4): 断网嫌疑可豁免,分数下限 0。"""
    if delta <= 0:
        return
    if offline_suspicion:
        ops.audit(conn, "monitor_score_exempt",
                  {"worker_id": worker_id, "delta": delta,
                   "reason": "offline_suspicion"})
        return
    conn.execute(
        "UPDATE ability_profiles SET score=MAX(0, score-?) WHERE instance_name=?",
        (delta, worker_id))
    ops.audit(conn, "monitor_score_deduct",
              {"worker_id": worker_id, "delta": delta})


def _tick(conn, state: dict):
    """一次巡检。state 为内存采样缓存,丢失无损(7.1)。"""
    t1 = int(ops._config(conn, "t1_seconds") or 120)
    t2 = int(ops._config(conn, "t2_seconds") or 600)
    ts = now()

    # 断网探测(7.5): 全量挂起依据,恢复后自然追平
    offline = _check_network(state)
    if offline and not state.get("offline"):
        state["offline_since"] = ts
        ops.audit(conn, "monitor_offline_start", {"ts": ts})
    elif not offline and state.get("offline"):
        ops.audit(conn, "monitor_offline_end",
                  {"ts": ts, "duration": ts - state.get("offline_since", ts)})
        state.pop("offline_since", None)
    state["offline"] = offline

    # ---- 活跃派单: 双阶梯+进度阶梯(7.3/7.5) ----
    rows = conn.execute(
        "SELECT d.id AS did, d.task_id AS tid, d.worker_id, d.expect_min,"
        " d.status AS dstatus, d.created_at AS dcreated, t.status AS tstatus"
        " FROM dispatches d JOIN tasks t ON t.id=d.task_id"
        " WHERE d.status IN ('issued','active')").fetchall()
    for r in rows:
        task_id, worker = r["tid"], r["worker_id"]
        # 审核态: 豁免活性阶梯(7.5/11.2),但进度阶梯超上限仍升级(断网时挂起)
        if r["tstatus"] == "reviewing":
            age = ts - r["dcreated"]
            if age > r["expect_min"] * 120:
                if not offline:
                    _escalate(conn, state, task_id, worker,
                              f"审核态进度超限: 已 {age//60} 分钟 > expect_min×2"
                              f"({r['expect_min']*2} 分钟),总控定换人/加时(7.5 慢)",
                              "progress")
                else:
                    ops.audit(conn, "monitor_ladder_suspend",
                              {"dispatch_id": r["did"], "reason": "offline"})
            continue  # 跳过活性阶梯(豁免)
        task_id, worker = r["tid"], r["worker_id"]
        # 求助等待挂起(7.5): 未答复 worker_help 的派单活性阶梯挂起
        pending_help = _pending_worker_help(conn, worker)
        if pending_help is not None:
            ops.audit(conn, "monitor_help_suspend",
                      {"dispatch_id": r["did"], "worker_id": worker,
                       "help_seq": pending_help["seq"],
                       "reason": "未答复 worker_help,活性阶梯挂起"})
            help_age = ts - pending_help["ts"]
            if help_age > t2:
                _escalate(conn, state, task_id, worker,
                          f"有求助未响应: worker_help(seq={pending_help['seq']})"
                          f"已等 {help_age}s > T2({t2}s),请总控答复(7.5)",
                          "help_timeout")
            continue
        # 事件活性(全局 seq 单调,事件即活动)
        ev = conn.execute(
            "SELECT MAX(ts) AS last_ts, MAX(seq) AS last_seq FROM messages"
            " WHERE type='event' AND sender=?", (worker,)).fetchone()
        event_ts = ev["last_ts"] or r["dcreated"]
        # 字节活性: 读实例的 shell + isolated_dir 委托 transcript_parser
        reg = conn.execute(
            "SELECT session_id FROM instance_registrations"
            " WHERE instance_name=? AND status='active'"
            " ORDER BY id DESC LIMIT 1", (worker,)).fetchone()
        session_id = reg["session_id"] if reg else ""
        inst = conn.execute(
            "SELECT shell, isolated_dir FROM instances WHERE name=?",
            (worker,)).fetchone()
        shell = inst["shell"] if inst else "claude"
        iso_dir = (inst["isolated_dir"] or "") if inst else ""
        bnow = _transcript_bytes(session_id, shell, isolated_dir=iso_dir)
        base = state.setdefault("bytes", {}).get(worker, (0, r["dcreated"]))
        last_byte_ts = base[1] if bnow > base[0] else base[1]
        if bnow > base[0]:
            state["bytes"][worker] = (bnow, ts)
        last_active = max(event_ts, last_byte_ts)
        silent = ts - last_active

        if not offline:
            unclosed = _unclosed_subagents(conn, worker, r["dcreated"])
        else:
            unclosed = 0

        pid_row = conn.execute(
            "SELECT pid FROM instance_registrations"
            " WHERE instance_name=? AND status IN ('spawned','active')"
            " ORDER BY id DESC LIMIT 1", (worker,)).fetchone()
        pid = pid_row["pid"] if pid_row else 0
        # 无事件活性证据的壳(未装钩子/dsh 转录无 session_id)降级判活:
        # 7.5 三层证据②——字节停+进程活=活着在等,不标 stale(保结算通道),
        # 但仍按阶梯升级提示(只警告不判死,0.3-4 误杀比漏报贵)。
        # 2026-08-19 实证: dsh 工人无钩子时事件/字节活性都拿不到,老派单
        # 第一拍必超 T2 被标 stale,worker_done 结算通道被卡死(5.4 stale 拒绝码)。
        # 判定从严: 派单时间窗内零事件产出(连一条都没有),且无字节证据。
        ev_exist = conn.execute(
            "SELECT 1 FROM messages WHERE type='event' AND sender=?"
            " AND ts >= ? LIMIT 1", (worker, r["dcreated"])).fetchone()
        no_evidence = (ev_exist is None and bnow == 0)
        stale_guard = no_evidence and _pid_alive(pid)

        # 后台判活豁免(7.5): 字节停+进程活+未收尾=暂不升级
        if not offline and silent > t1 and _pid_alive(pid) and unclosed > 0:
            ops.audit(conn, "monitor_ladder_exempt",
                      {"dispatch_id": r["did"], "reason": "未收尾后台任务", "unclosed": unclosed})
            continue

        if silent > t2:
            # T2 超时: 派单→escalate(长活只警告不判死),连续两次确认防网络波动误报
            hits = state.setdefault("t2_hits", {}).get(r["did"], 0) + 1
            state["t2_hits"][r["did"]] = hits
            if hits == 1:
                # 首次命中=疑似波动(7.5 多次采样确认);波动数据不入校准(7.3,票 07)
                ops.audit(conn, "network_fluctuation",
                          {"dispatch_id": r["did"], "silent_s": silent})
            if hits >= 2:
                if stale_guard:
                    # 进程活但无活性证据: 只升级不标 stale(误杀比漏报贵)
                    _escalate(conn, state, task_id, worker,
                              f"静默超 T2({t2}s)但进程存活: 无事件/字节活性证据"
                              f"(壳未装钩子),按进程活性视为在干;若确已停住"
                              f"可用 tianji nudge <dispatch_id> 续推(7.5)",
                              "t2_silent_no_evidence")
                elif not offline:
                    _mark_dispatch_stale(conn, r["did"], task_id, "stale")
                    _escalate(conn, state, task_id, worker,
                              f"静默超 T2({t2}s): 事件最后 {now()-event_ts}s 前,"
                              f"字节最后 {now()-last_byte_ts}s 前(只警告不判死,7.5)",
                              "t2_silent")
                else:
                    ops.audit(conn, "monitor_ladder_suspend",
                              {"dispatch_id": r["did"], "reason": "offline"})
        elif silent > t1:
            if not offline:
                # 7.5 续推通道: 最后事件=stop(答完一轮未打断)且无新活动超 T1
                # → 升级消息带"建议 nudge"线索(监控器不自动续推,花钱动作归总控 14.5)
                last_ev = conn.execute(
                    "SELECT json_extract(payload,'$.event_type') AS et,"
                    " json_extract(payload,'$.is_interrupt') AS intr"
                    " FROM messages WHERE type='event' AND sender=?"
                    " AND ts>=? ORDER BY seq DESC LIMIT 1",
                    (worker, r["dcreated"])).fetchone()
                hint = ""
                if last_ev and last_ev["et"] == "stop" and not last_ev["intr"]:
                    hint = (f";工人答完一轮停下(stop 未打断),可续推:"
                            f" tianji nudge {r['did']}(7.5)")
                _escalate(conn, state, task_id, worker,
                          f"静默超 T1({t1}s): 提示检查(7.3 顶部只警告){hint}",
                          "t1_silent")
            else:
                ops.audit(conn, "monitor_ladder_suspend",
                          {"dispatch_id": r["did"], "reason": "offline"})
        # 进度阶梯 expect_min×2(慢=字节在流但超预期时长)
        age = ts - r["dcreated"]
        if age > r["expect_min"] * 120:
            if not offline:
                _escalate(conn, state, task_id, worker,
                          f"进度超限: 已 {age//60} 分钟 > expect_min×2"
                          f"({r['expect_min']*2} 分钟),总控定换人/加时(7.5 慢)",
                          "progress")
            else:
                ops.audit(conn, "monitor_ladder_suspend",
                          {"dispatch_id": r["did"], "reason": "offline"})

    # ---- 对账①: 事件说 done 无结算(漏声称兜底,7.4①) ----
    for r in rows:
        if r["tstatus"] in ("reviewing", "awaiting_final_confirm", "archived"):
            continue  # 已结算链路,跳过
        end = conn.execute(
            "SELECT MAX(ts) AS t FROM messages WHERE type='event' AND sender=?"
            " AND json_extract(payload,'$.event_type') = 'session_end'"
            " AND ts >= ?", (r["worker_id"], r["dcreated"])).fetchone()
        if end and end["t"]:
            _escalate(conn, state, r["tid"], r["worker_id"],
                      "事件说完成但账本无结算(漏声称兜底 7.4①),请对账",
                      "claimed_done_no_settle")

    # ---- 对账②: 进程退出无结算→确定性重派(自动动作,7.4②) ----
    regs = conn.execute(
        "SELECT r.id AS rid, r.instance_name, r.pid, r.dispatch_id, r.session_id,"
        " r.status AS rstatus, r.created_at AS rcreated, r.offline_suspicion"
        " FROM instance_registrations r"
        " WHERE r.status IN ('spawned','active')").fetchall()
    for r in regs:
        d = conn.execute(
            "SELECT d.id, d.status AS dstatus, d.task_id, d.worker_id, d.worker_role"
            " FROM dispatches d WHERE d.id=?",
            (r["dispatch_id"],)).fetchone() if r["dispatch_id"] else None
        should_pause = offline and d is not None and d["dstatus"] not in ("done", "requeue", "escalate", "cancelled")
        if offline:
            # 断网时对账②完全挂起;若活跃派单进程死则标记断网嫌疑
            if should_pause and not _pid_alive(r["pid"]):
                with tx(conn) as c:
                    c.execute(
                        "UPDATE instance_registrations SET offline_suspicion=1"
                        " WHERE id=?", (r["rid"],))
            ops.audit(conn, "monitor_reconcile2_suspend",
                      {"registration_id": r["rid"], "reason": "offline"})
            continue
        if d is None or d["dstatus"] in ("done", "requeue", "escalate", "cancelled"):
            # 11.3 异常退出补关: 派单已完结,但进程死了没发 session_end,
            # 登记行补关+标异常(不重派,任务/派单状态不动)。
            # cancelled 派单跳过对账②(强制干预已结案,防"喊停又被重派" 5.1/7.4)。
            # 2026-08 演示踩坑: 强杀已结算会话后登记行永远挂 active。
            if not _pid_alive(r["pid"]):
                with tx(conn) as c:
                    c.execute(
                        "UPDATE instance_registrations SET status='closed',"
                        " closed_at=?, abnormal=1 WHERE id=?",
                        (now(), r["rid"]))
                    # 返修项3: 对账②补关时同步 session_states → done
                    if r["session_id"]:
                        c.execute(
                            "UPDATE session_states SET state='done', updated_at=? "
                            "WHERE session_id=? AND state='working'",
                            (now(), r["session_id"]))
                    ops.audit(c, "monitor_close_registration",
                              {"registration_id": r["rid"],
                               "dispatch_id": r["dispatch_id"],
                               "worker_id": r["instance_name"],
                               "reason": "进程退出无 session_end(派单已完结)"})
            continue
        if not r["pid"]:
            # 7.4②: spawn 未实际拉起(pid IS NULL),按登记行 created_at 超时纳入对账
            if ts - r["rcreated"] < t2:
                continue
        elif _pid_alive(r["pid"]):
            continue
        with tx(conn) as c:
            # 断点摘要(7.5): 读取旧会话转录尾部+任务书+产物清单
            _append_breakpoint_summary(conn, d["id"], r["instance_name"])
            # 读回摘要,传递给新派单
            d_updated = conn.execute("SELECT payload FROM dispatches WHERE id=?",
                                     (d["id"],)).fetchone()
            bp_summary = None
            if d_updated:
                try:
                    bp_summary = json.loads(d_updated["payload"] or "{}").get(
                        "breakpoint_summary")
                except Exception:
                    pass
            c.execute(
                "UPDATE dispatches SET status='requeue', updated_at=? WHERE id=?",
                (now(), d["id"]))
            c.execute(
                "UPDATE instance_registrations SET status='closed', closed_at=?,"
                " abnormal=1 WHERE id=?", (now(), r["rid"]))
            ops.audit(c, "monitor_requeue",
                      {"dispatch_id": d["id"], "task_id": d["task_id"],
                       "worker_id": r["instance_name"],
                       "reason": "进程退出无结算"})
        # 审核派单进程退出: 不走任务级确定性重派(不占重派计数、不扣表现分,
        # 防误杀——2026-08-18 实锤: 审核派单按实施者逻辑重派会把任务推向超限
        # 终止);只升级总控补发审核派单
        if d["worker_role"] != "worker":
            _escalate(conn, state, d["task_id"], r["instance_name"],
                      "审核派单进程退出无结算,请总控补发审核派单(不占任务重派计数)",
                      "requeue")
            continue
        # 任务回 dispatched + 自动重派(计数;监控器确定性重派=规格书例外动作,
        # 不走用户命令面的转换表检查,审计留痕)
        t = conn.execute("SELECT status FROM tasks WHERE id=?",
                         (d["task_id"],)).fetchone()
        if t and t["status"] in ("dispatched", "executing"):
            with tx(conn) as c:
                off_susp = bool(offline) or r["offline_suspicion"] == 1
                if not off_susp:
                    dm = c.execute(
                        "SELECT expect_min FROM dispatches "
                        "WHERE task_id=? ORDER BY id DESC LIMIT 1",
                        (d["task_id"],)).fetchone()
                    em = dm["expect_min"] if dm else 30
                    try:
                        ops.update_score(c, r["instance_name"],
                                         "process_dead", em)
                    except KeyError:
                        pass
                else:
                    ops.audit(c, "monitor_score_exempt",
                              {"worker_id": r["instance_name"],
                               "reason": "offline_suspicion"})
                ops._reschedule(c, d["task_id"], r["instance_name"],
                                "进程退出无结算(确定性重派 7.4②)",
                                skip_score=True,
                                breakpoint_summary=bp_summary)
            _escalate(conn, state, d["task_id"], r["instance_name"],
                      "进程退出无结算已确定性重派,新派单请 spawn(7.4②)",
                      "requeue")

    # ---- 对账③: 转录增长但事件无新行→钩子失效警告(7.4③) ----
    t2_3 = int(ops._config(conn, "t2_seconds") or 600)
    for r in regs:
        if not r["session_id"]:
            continue
        ev_row = conn.execute(
            "SELECT MAX(seq) AS s, MAX(ts) AS t FROM messages WHERE type='event' AND sender=?",
            (r["instance_name"],)).fetchone()
        ev = ev_row["s"] or 0
        ev_ts = ev_row["t"] or 0
        prev_ev = state.setdefault("last_event_seq", {}).get(r["instance_name"], 0)
        # 独立基线字典: 不与活性节共享 state["bytes"](后者二元组第二元素
        # 是"最后增长时间",覆写会重置静密计时,T2 永不触发——2026-08 踩坑)
        hb = state.setdefault("hook_bytes", {})
        size = hb.get(r["instance_name"])
        shell = "claude"
        inst = conn.execute(
            "SELECT shell, isolated_dir FROM instances WHERE name=?",
            (r["instance_name"],)).fetchone()
        if inst:
            shell = inst["shell"]
        iso_dir = (inst["isolated_dir"] or "") if inst else ""
        cur = _transcript_bytes(r["session_id"], shell,
                                isolated_dir=iso_dir)
        if ev > prev_ev:
            state["last_event_seq"][r["instance_name"]] = ev
            hb[r["instance_name"]] = cur
            state.get("hook_suspect", {}).pop(r["instance_name"], None)
        elif size is None:
            hb[r["instance_name"]] = cur
        elif cur > size:
            # 档 3 兜底: 壳条目声明 tier3_process_alive 则豁免(tier3 验证按
            # adapter 的 output_file 派发,目前仅 codex_exec 有实现,其余壳按无 tier3 处理)
            tier3_alive = _check_tier3_capability(conn, shell, pid=r["pid"])
            if tier3_alive:
                hb[r["instance_name"]] = cur
                continue
            if (int(time.time()) - ev_ts) <= t2_3:
                # T2 以内的思考间隙(转录在写、事件没来)是常态,不报(2026-08-17 周期性误报实证)
                continue
            # 转录在增长但事件超 T2 无新行 = 钩子链路断了;
            # 连续两拍才确认(长阅读工人单拍跨线是常态——同日 follow-up)
            hits = state.setdefault("hook_suspect", {})
            n = hits.get(r["instance_name"], 0) + 1
            hits[r["instance_name"]] = n
            if n < 2:
                continue
            _escalate(conn, state, 0, r["instance_name"],
                      "钩子失效: 转录增长但事件超 T2 无新行(连续两拍),活性退到档 2(信号降级 7.4③)",
                      "hook_degraded")
            state["last_event_seq"][r["instance_name"]] = ev
            # 基线跟着更新,不重复报同一存量(2026-08 踩坑)
            hb[r["instance_name"]] = cur
            hits[r["instance_name"]] = 0

    # ---- 机械验收(8.3): 异步执行,声称触发 ----
    reviewing = conn.execute(
        "SELECT id FROM tasks WHERE status='reviewing'").fetchall()
    for t in reviewing:
        # 去重粒度=(任务,最新已结算派单): 返修后的新结算要再验(与 ops.mechanical_verify 同键)
        d = conn.execute(
            "SELECT id FROM dispatches WHERE task_id=? AND status='done' "
            "ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
        did = d["id"] if d else -1
        done = conn.execute(
            "SELECT id FROM audit WHERE action='mechanical_verify' AND detail LIKE ?",
            (f'%"task_id": {t["id"]}, "dispatch_id": {did}%',)).fetchone()
        if done:
            continue
        try:
            ops.mechanical_verify(conn, t["id"], timeout=900)
        except ValueError as e:
            _escalate(conn, state, t["id"], "",
                      f"机械验收门受阻: {e}(8.3)", "verify_blocked")
        except Exception as e:
            _escalate(conn, state, t["id"], "",
                      f"机械验收异常: {e}", "verify_error")

    # HITL 超时闭环(票 54 task-02): 顺带过期超 24h 的待审批请求;
    # 接在 _tick 内,once 模式(run_monitor(once=True))也执行
    try:
        ops.expire_force_approvals(conn)
    except Exception:
        pass


def _purge_pool_logs(conn):
    """清除超期 pool_request_logs(票 57 task-09, 监控器巡检顺带)。"""
    retention_days = int(ops._config(conn, "pool_log_retention_days") or 30)
    cutoff = now() - retention_days * 86400
    result = conn.execute(
        "DELETE FROM pool_request_logs WHERE ts < ?", (cutoff,))
    if result.rowcount:
        ops.audit(conn, "pool_log_purge",
                  {"deleted": result.rowcount,
                   "retention_days": retention_days,
                   "cutoff_ts": cutoff})


def _monitor_backup_once(conn):
    """每日备份(18.5): 监控器巡检顺带,失败写 backup_failed audit 不崩溃。"""
    try:
        from .daemon import backup_ledger
        backup_ledger(conn)
    except Exception as e:
        try:
            ops.audit(conn, "backup_failed", {"error": str(e)})
            # 显式落盘: 不靠下一个事务顺带提交,崩溃窗口不丢审计行
            conn.commit()
        except Exception:
            pass


def _reconcile_plugins(conn):
    """插件对账巡检(21.4,票 23): 启用中的模板类插件逐一对账,
    缺失/旧版机械重生成,用户改过不碰+审计升级;单个失败不拖垮其余。"""
    from . import plugins
    for p in plugins.list_plugins(conn):
        if p.get("type") != "template" or not p.get("enabled", True):
            continue
        try:
            plugins.reconcile(conn, p["name"])
        except Exception:
            pass


def run_monitor(interval: int = 30, once: bool = False):
    conn = connect()
    ops.ensure_defaults(conn)
    # 票 06: 监控器是常驻进程,才启用内存 pacer(CLI 短进程路径不挂)
    ops.PACER.enabled = True
    state: dict = {}
    if once:
        _tick(conn, state)
        return
    print(f"监控器启动: tick={interval}s(停止=Ctrl+C)", flush=True)
    while True:
        try:
            _tick(conn, state)
        except Exception as e:
            # 单次巡检失败不拖垮循环;审计留痕
            try:
                with tx(conn) as c:
                    ops.audit(c, "monitor_tick_error", {"error": str(e)})
            except Exception:
                pass
        # 每日备份(18.5): 监控器巡检顺带
        _monitor_backup_once(conn)
        # 日志保留期清理(票 57 task-09): 顺带清除超期 pool_request_logs
        try:
            _purge_pool_logs(conn)
        except Exception:
            pass
        # 动态校准(7.3/9.3,票 07): 30min 窗口节流重算滑动统计
        try:
            from .calibration import recalibrate
            recalibrate(conn)
        except Exception:
            pass
        # 额度/上下文健康度巡检复查(14.1/14.2,票 11): 将尽提示,零新增常驻
        try:
            from .quota import monitor_scan
            monitor_scan(conn)
        except Exception:
            pass
        # 钩子对账巡检(17.2②,票 13): ~30min 节流,缺失/旧版机械补
        try:
            from .hooks import scan_all
            scan_out = scan_all(conn)
        except Exception:
            scan_out = None
        # 插件对账巡检(21.4,票 23): 与钩子对账同窗节流,
        # 缺失/旧版机械重生成,用户改过不碰+审计升级
        if scan_out and "scanned" in scan_out:
            try:
                _reconcile_plugins(conn)
            except Exception:
                pass
        time.sleep(interval)
