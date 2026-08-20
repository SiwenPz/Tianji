"""端到端闭环(验收 1): 最小 2 实例全流程走 CLI 层,全程机械校验。

总控会话(兼架构师兼审核者,1.3 最小配置)+ 1 实施者(模拟 worker,事件+settle 走真实 env)。
"""

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from tianji.cli import app

runner = CliRunner()


def _invoke(args, env=None, input=None):
    # CliRunner 传 env 时整体替换环境,须合并 TIANJI_HOME(conftest 注入)
    full = dict(env or {})
    full.setdefault("TIANJI_HOME", os.environ["TIANJI_HOME"])
    r = runner.invoke(app, args, env=full, input=input)
    assert r.exit_code == 0, f"CLI 失败 {args}: {r.output}\n{r.exception}"
    return json.loads(r.output) if r.output.strip() else {}


def _ctrl(secret):
    return {"TIANJI_WORKER_ID": "总控", "TIANJI_SECRET": secret}


def _worker_env(spawn_info):
    return {"TIANJI_WORKER_ID": spawn_info["env"]["TIANJI_WORKER_ID"],
            "TIANJI_SECRET": spawn_info["env"]["TIANJI_SECRET"],
            "TIANJI_DISPATCH_ID": str(spawn_info["dispatch_id"])}


def test_full_loop_closed(tianji_home):
    # ---- 注册: 总控(controller)+实施者 ----
    rc = _invoke(["instance", "register", "总控", "claude",
                  "deepseek-v4-flash", "--controller"])
    ctrl = _ctrl(rc["secret"])
    _invoke(["instance", "register", "铁蛋", "codex", "step-router-v1",
             "--launch-cmd", "python mock_worker.py"])
    lst = _invoke(["instance", "list"])
    assert {i["name"] for i in lst["instances"]} == {"总控", "铁蛋"}

    # ---- 立项→讨论→计划确认(验收命令在计划确认前写入,8.3) ----
    t = _invoke(["task", "new", "端到端任务", "--description", "写一个 hello.py",
                 "--request-id", "e2e-new"], ctrl)
    tid = t["task_id"]
    for s in ("discussing", "awaiting_plan_confirm"):
        _invoke(["task", "transition", str(tid), s,
                 "--request-id", f"e2e-{s}"], ctrl)
    _invoke(["task", "verify-cmd", str(tid), "python -c print(1)",
             "--request-id", "e2e-vcmd"], ctrl)
    _invoke(["task", "transition", str(tid), "dispatched",
             "--request-id", "e2e-plan-confirm"], ctrl)

    # ---- 派单→spawn→实施者干活(模拟会话) ----
    d = _invoke(["dispatch", "issue", str(tid), "铁蛋",
                 "--request-id", "e2e-issue"], ctrl)
    did = d["dispatch_id"]
    s = _invoke(["spawn", "铁蛋", str(did)])
    assert Path(s["taskbook"]).is_file()  # 任务书落盘(11.2)
    assert "验收命令" in Path(s["taskbook"]).read_text(encoding="utf-8")
    wenv = _worker_env(s)
    _invoke(["ingest-event"], env=wenv, input=json.dumps(
        {"session_id": "e2e-sess", "event_type": "session_start"}))
    # 开工证据
    _invoke(["ingest-event"], env=wenv, input=json.dumps(
        {"session_id": "e2e-sess", "event_type": "pre_tool_use",
         "payload": {"tool_name": "Write"}}))
    show = _invoke(["dispatch", "show", str(did)])
    assert show["status"] == "active"  # 5.1 派单 active
    assert _invoke(["task", "show", str(tid)])["status"] == "executing"

    # ---- worker_done 结算(唯一权威完成信号,5.4) ----
    rp = str(Path(s["env"]["TIANJI_TASK_PATH"]).parent / "report.md")
    Path(rp).write_text("hello.py 已写好", encoding="utf-8")
    st = _invoke(["dispatch", "settle", str(did), rp, "ok"], wenv)
    assert st["task_status"] == "reviewing"
    assert _invoke(["task", "show", str(tid)])["status"] == "reviewing"

    # ---- 机械验收门(声称触发,8.3) ----
    v = _invoke(["verify", str(tid)], ctrl)
    assert v["ok"] is True

    # ---- 双轴审核(总控 spec 轴 + 再注册一个质量轴): 审核派单→pass→架构师确认 ----
    dr_spec = _invoke(["dispatch", "issue", str(tid), "总控", "--role", "reviewer",
                        "--axis", "spec", "--request-id", "e2e-rev-spec"], ctrl)
    sr_spec = _invoke(["spawn", "总控", str(dr_spec["dispatch_id"])])
    renv_spec = _worker_env(sr_spec)
    _invoke(["ingest-event"], env=renv_spec, input=json.dumps(
        {"session_id": "e2e-rev-spec", "event_type": "session_start"}))
    rp_rev_spec = str(Path(sr_spec["env"]["TIANJI_TASK_PATH"]).parent / "review_spec.md")
    Path(rp_rev_spec).write_text("Spec 轴通过", encoding="utf-8")
    vv_spec = _invoke(["dispatch", "settle", str(dr_spec["dispatch_id"]), rp_rev_spec,
                        "pass", "--reason", "Spec 轴通过"], renv_spec)
    assert vv_spec["verdict"] == "pass"

    # 质量轴: 再注册一个审核实例(不同模型)
    _invoke(["instance", "register", "审核乙", "codex", "step-router-v1",
             "--launch-cmd", "python mock_worker.py"])
    dr_quality = _invoke(["dispatch", "issue", str(tid), "审核乙", "--role", "reviewer",
                           "--axis", "quality", "--request-id", "e2e-rev-quality"], ctrl)
    sr_quality = _invoke(["spawn", "审核乙", str(dr_quality["dispatch_id"])])
    renv_quality = _worker_env(sr_quality)
    _invoke(["ingest-event"], env=renv_quality, input=json.dumps(
        {"session_id": "e2e-rev-quality", "event_type": "session_start"}))
    rp_rev_quality = str(Path(sr_quality["env"]["TIANJI_TASK_PATH"]).parent / "review_quality.md")
    Path(rp_rev_quality).write_text("质量轴通过", encoding="utf-8")
    vv_quality = _invoke(["dispatch", "settle", str(dr_quality["dispatch_id"]), rp_rev_quality,
                           "pass", "--reason", "质量轴通过"], renv_quality)
    assert vv_quality["verdict"] == "pass"

    # 架构师确认
    _invoke(["architect", "confirm", str(tid), "--reason", "双轴一致通过",
             "--request-id", "e2e-ac"], ctrl)

    # ---- 最终确认闸门(20.4 默认开)→ 归档 ----
    _invoke(["task", "transition", str(tid), "awaiting_final_confirm",
             "--request-id", "e2e-afc"], ctrl)
    _invoke(["task", "transition", str(tid), "archived",
             "--request-id", "e2e-arch"], ctrl)
    assert _invoke(["task", "show", str(tid)])["status"] == "archived"

    # ---- 全程机械校验留痕: 审计+消息 ----
    actions = [r["action"] for r in
               conn_rows(tianji_home, "SELECT action FROM audit")]
    for want in ("task_new", "task_transition", "dispatch_issue", "worker_done",
                 "mechanical_verify", "review_settle", "task_verify_cmd"):
        assert want in actions, f"审计缺 {want}: {actions}"
    types = [r["type"] for r in
             conn_rows(tianji_home, "SELECT type FROM messages")]
    for want in ("dispatch", "worker_done", "review_verdict", "event",
                 "final_confirm"):
        assert want in types, f"消息缺 {want}: {types}"

    # ---- 游标+幂等: 总控消费未读并 ack;重放任务不可逆操作返原回执 ----
    unread = _invoke(["message", "check", "controller-main", "controller"])
    assert len(unread["unread"]) > 0
    max_seq = max(m["seq"] for m in unread["unread"])
    _invoke(["message", "ack", "controller-main", str(max_seq)])
    again = _invoke(["message", "check", "controller-main", "controller"])
    assert again["unread"] == []
    # 幂等重放: 同 request-id 再归档 → 原回执不重复执行
    replay = _invoke(["task", "transition", str(tid), "archived",
                      "--request-id", "e2e-arch"], ctrl)
    assert replay.get("replay") is True

    # ---- 导出(3.4 跨机预留) ----
    exp = _invoke(["ledger", "export", "--after", "0"])
    assert len(exp["messages"]) >= 10


def conn_rows(home, sql):
    import sqlite3
    conn = sqlite3.connect(str(Path(home) / "ledger.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]
