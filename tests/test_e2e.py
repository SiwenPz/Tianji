"""端到端闭环(验收 1): 最小 2 实例全流程走 CLI 层,全程机械校验。

总控会话(兼架构师兼审核者,1.3 最小配置)+ 1 实施者(模拟 worker,事件+settle 走真实 env)。
"""

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from typer.testing import CliRunner

from tianji.shellrender import render

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

def test_pool_wizard_integration(tianji_home):
    """ticket59 pool e2e: build pool -> assign key -> instance bound to pool -> dispatch -> spawn."""
    from tianji.db import connect
    conn = connect()
    try:
        from tianji import ops, integrations, wizard, pool as pool_mod
        from tianji.shellrender import render
        import secrets
        secret = secrets.token_hex(16)
        ops.ensure_defaults(conn)
        rc = ops.instance_register(
            conn, "ctrl", "claude", "deepseek-v4-flash", controller=True)
        secret = rc["secret"]
        ident = {"worker_id": "ctrl", "secret": secret}
        integrations.ensure_builtin_registry(conn, ident,
                                             request_id="e2e-reg")
        # 1. register provider + credential (显式关联,proxy 需要)
        integrations.register_custom_provider(
            conn, ident, "e2e-prov",
            base_url="http://127.0.0.1:19999",
            protocol="openai_chat",
            auth_style="bearer",
            request_id="e2e-prov")
        cred_ref = str(tianji_home / "pool-keys" / "c1.key")
        Path(cred_ref).parent.mkdir(parents=True, exist_ok=True)
        Path(cred_ref).write_text("upstream-key-42", encoding="utf-8")
        integrations.register_credential(
            conn, ident, "cred1", "e2e-prov", key_ref=cred_ref,
            request_id="e2e-cred")
        # 2. build pool with member
        r = pool_mod.pool_create(conn, ident, "test-pool",
                                  members=["cred1"],
                                  request_id="e2e-pool")
        assert r["name"] == "test-pool"
        assert len(r["members"]) == 1
        pool_token = r["token"]
        assert len(pool_token) == 64
        # 预置 proxy 端口(模拟 daemon 已启动)
        ops.config_set(conn, ident, "daemon.proxy_port", "9876",
                       request_id="e2e-proxy-port")
        # 3. create instance bound to pool
        iso = tianji_home / "instances" / "rev1-claude"
        inst = wizard.add_instance(conn, ident, "rev1", "claude",
                                    "deepseek-v4-flash", key_name="test-pool",
                                    isolated_dir=str(iso),
                                    skip_test=True, confirm=True,
                                    request_id="e2e-inst")
        assert inst["name"] == "rev1"
        assert inst["registered"] is True
        # 4. render launch_cmd with pool token
        cmd = render(conn, "claude", instance="rev1",
                      model="deepseek-v4-flash", key_name="test-pool",
                      isolated_dir=str(iso))
        launch_cmd, arts = cmd
        assert "settings" in launch_cmd
        settings_data = json.loads(Path(arts[0]).read_text(encoding="utf-8"))
        assert settings_data["env"]["ANTHROPIC_AUTH_TOKEN"] == pool_token
        assert "127.0.0.1" in settings_data["env"]["ANTHROPIC_BASE_URL"]
        # 5. dispatch + spawn
        task = _invoke(["task", "new", "pool-e2e-task",
                        "--description", "full pool chain",
                        "--request-id", "e2e-new-task"],
                       env=env_ctrl(secret, str(tianji_home)))
        tid = task["task_id"]
        for s in ("discussing", "awaiting_plan_confirm"):
            _invoke(["task", "transition", str(tid), s,
                     "--request-id", f"e2e-{s}"],
                    env=env_ctrl(secret, str(tianji_home)))
        _invoke(["task", "verify-cmd", str(tid), "echo ok",
                 "--request-id", "e2e-vcmd"],
                env=env_ctrl(secret, str(tianji_home)))
        _invoke(["task", "transition", str(tid), "dispatched",
                 "--request-id", "e2e-dispatch"],
                env=env_ctrl(secret, str(tianji_home)))
        dp = _invoke(["dispatch", "issue", str(tid), "rev1",
                       "--request-id", "e2e-issue"],
                      env=env_ctrl(secret, str(tianji_home)))
        sp = _invoke(["spawn", "rev1", str(dp["dispatch_id"])],
                      env=env_ctrl(secret, str(tianji_home)))
        assert Path(sp["taskbook"]).is_file()
        assert sp["env"]["TIANJI_WORKER_ID"] == "rev1"
        assert sp["env"]["TIANJI_DISPATCH_ID"] == str(dp["dispatch_id"])

        # 6. 真 proxy round-trip: 建池→绑池→经池出活落日志(修B)
        from tianji.proxy._pool import run_proxy
        class _BackendAlways200(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps({
                    "id": "chatcmpl-1", "object": "chat.completion",
                    "created": 1234567890,
                    "model": "test-model", "choices": [{"index": 0, "message": {
                        "role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a, **kw):
                pass

        backend = HTTPServer(("127.0.0.1", 0), _BackendAlways200)
        backend_port = backend.server_address[1]
        bt = threading.Thread(target=backend.serve_forever, daemon=True)
        bt.start()
        # 务必将 provider base_url 指向 mock 上游
        prov_entry = ops._config(conn, "integration_provider:e2e-prov")
        if isinstance(prov_entry, str):
            prov_entry = json.loads(prov_entry)
        prov_entry["base_url"] = f"http://127.0.0.1:{backend_port}"
        conn.execute(
            "UPDATE configs SET value=? WHERE key=?",
            (json.dumps(prov_entry, ensure_ascii=False),
             "integration_provider:e2e-prov"))

        proxy_port = 19008
        pt = threading.Thread(
            target=run_proxy, args=(proxy_port,), daemon=True)
        pt.start()
        try:
            import time as _time
            _time.sleep(0.5)
            body = json.dumps({"model": "test-model"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/proxy/test-pool"
                f"/v1/chat/completions?token={pool_token}",
                data=body, headers={"Content-Type": "application/json"},
                method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            assert resp.status == 200
            # pool_request_logs 落行(修B: 经池出活完整闭环)
            # 轮询兜底: 代理线程写日志可能在客户端收到 200 之后微秒级提交
            import time as _time
            logs = None
            for _ in range(20):
                logs = conn.execute(
                    "SELECT member_name, status_code, request_model, model"
                    " FROM pool_request_logs WHERE pool_name='test-pool'"
                    ).fetchall()
                if logs:
                    break
                _time.sleep(0.1)
            assert logs, "pool_request_logs 应有请求记录"
            assert logs[0]["member_name"] == "cred1"
            assert logs[0]["status_code"] == 200
        finally:
            pt.join(timeout=2)
            backend.shutdown()
    finally:
        conn.close()


def env_ctrl(secret, home):
    return {"TIANJI_WORKER_ID": "ctrl", "TIANJI_SECRET": secret, "TIANJI_HOME": home}


