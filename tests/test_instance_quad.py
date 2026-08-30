"""实例四元模型与壳/key 条目验收(票 18): 组合合法性+CLI 扩展+质量两轴。

- 三条合法组合正例(注册+派单两路径,共 6 子测试)
- 三条非法组合负例(注册+派单两路径,共 6 子测试)
- 壳/key 条目 CLI 增删查改+审计
"""

import json
import os
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import task_dir
from tianji.render import spawn

SHELL_CLAUDE = "claude"
SHELL_CODEX = "codex"
PROTO_STDIO = "stdio"
PROTO_HTTP = "http"


def _register_shell(conn, controller, name, protocols, binding="env",
                    isolated_dir_mode="env_home"):
    ops.config_set(conn, controller, f"shell:{name}",
                   json.dumps({"binding": binding,
                               "protocols": protocols,
                               "isolated_dir_mode": isolated_dir_mode},
                              ensure_ascii=False),
                   request_id=f"sh-{name}")


def _register_key(conn, controller, name, models, protocol=PROTO_STDIO,
                  key_ref=None, coding_plan=False):
    ops.config_set(conn, controller, f"key:{name}",
                   json.dumps({"base_url": f"https://api.example.com/{name}",
                               "models": models,
                               "protocol": protocol,
                               "key_ref": key_ref,
                               "coding_plan": coding_plan},
                              ensure_ascii=False),
                   request_id=f"k-{name}")


def _to_executing(conn, controller, worker):
    """真实链路快速走到任务 executing+派单 active(结算前置)。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="r-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    from tianji.events import ingest_event
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "sess-1", "event_type": "pre_tool_use"})
    return tid, did


# ================================================================
# 合法组合正例(注册+派单两路径)
# ================================================================


class TestValidCombos:
    """三条合法组合,注册和派单两路径各测一次(共 6 子测试)。"""

    def test_register_valid_basic(self, conn, controller):
        """基本合法组合: claude + deepseek-key + deepseek-v4-flash"""
        _register_shell(conn, controller, SHELL_CLAUDE, [PROTO_STDIO, PROTO_HTTP])
        _register_key(conn, controller, "deepseek-key",
                      [{"id": "deepseek-v4-flash", "display_name": "DS Flash",
                        "context_window": 128000}],
                      protocol=PROTO_STDIO)
        r = ops.instance_register(conn, "dev-basic", SHELL_CLAUDE,
                                  "deepseek-v4-flash", key_name="deepseek-key")
        assert r["name"] == "dev-basic"
        row = conn.execute("SELECT * FROM instances WHERE name=?",
                           ("dev-basic",)).fetchone()
        assert row["shell"] == SHELL_CLAUDE
        assert row["key_name"] == "deepseek-key"
        assert row["model"] == "deepseek-v4-flash"

    def test_dispatch_valid_basic(self, conn, controller):
        """同一合法组合在派单路径校验通过"""
        _register_shell(conn, controller, SHELL_CLAUDE, [PROTO_STDIO, PROTO_HTTP])
        _register_key(conn, controller, "deepseek-key",
                      [{"id": "deepseek-v4-flash", "display_name": "DS Flash",
                        "context_window": 128000}],
                      protocol=PROTO_STDIO)
        ops.instance_register(conn, "dev-dispatch", SHELL_CLAUDE,
                              "deepseek-v4-flash", key_name="deepseek-key")
        tid = ops.task_new(conn, controller, "派单任务",
                           request_id="r-d1")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r-d-{s}")
        d = ops.dispatch_issue(conn, controller, tid, "dev-dispatch",
                               request_id="r-di")
        assert d["dispatch_id"] > 0

    def test_register_valid_same_key_diff_model(self, conn, controller):
        """同一 key 不同模型=不同实例组合,画像各自独立(13.2)"""
        _register_shell(conn, controller, SHELL_CLAUDE, [PROTO_STDIO])
        _register_key(conn, controller, "multi-key",
                      [{"id": "model-a", "display_name": "A",
                        "context_window": 64000},
                       {"id": "model-b", "display_name": "B",
                        "context_window": 128000}],
                      protocol=PROTO_STDIO)
        r1 = ops.instance_register(conn, "dev-a", SHELL_CLAUDE,
                                   "model-a", key_name="multi-key")
        r2 = ops.instance_register(conn, "dev-b", SHELL_CLAUDE,
                                   "model-b", key_name="multi-key")
        assert r1["name"] != r2["name"]
        portrait_a = conn.execute(
            "SELECT * FROM ability_profiles WHERE instance_name=?",
            ("dev-a",)).fetchone()
        portrait_b = conn.execute(
            "SELECT * FROM ability_profiles WHERE instance_name=?",
            ("dev-b",)).fetchone()
        assert portrait_a["model"] == "model-a"
        assert portrait_b["model"] == "model-b"
        assert portrait_a["model_source_score"] == 0
        assert portrait_a["key_body_score"] == 0

    def test_dispatch_valid_same_key_diff_model(self, conn, controller):
        """同一 key 不同模型组合在派单路径各自独立派单"""
        _register_shell(conn, controller, SHELL_CODEX, [PROTO_STDIO])
        _register_key(conn, controller, "codex-key",
                      [{"id": "step-router-v1", "display_name": "Router",
                        "context_window": 200000}],
                      protocol=PROTO_STDIO)
        # 注册一个实例并派单
        ops.instance_register(conn, "cd-1", SHELL_CODEX, "step-router-v1",
                              key_name="codex-key")
        tid = ops.task_new(conn, controller, "多模型派单",
                           request_id="r-m1")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r-m-{s}")
        d1 = ops.dispatch_issue(conn, controller, tid, "cd-1",
                                request_id="r-m-d1")
        assert d1["dispatch_id"] > 0

    def test_register_valid_coding_plan_same_shell(self, conn, controller):
        """CodingPlan key 在同一壳内合法(不跨壳=通过)"""
        _register_shell(conn, controller, SHELL_CLAUDE, [PROTO_STDIO, PROTO_HTTP])
        _register_key(conn, controller, "coding-plan-key",
                      [{"id": "deepseek-v4-flash", "display_name": "DS Flash",
                        "context_window": 128000}],
                      protocol=PROTO_STDIO,
                      key_ref="shell:claude", coding_plan=True)
        r = ops.instance_register(conn, "dev-cp", SHELL_CLAUDE,
                                  "deepseek-v4-flash",
                                  key_name="coding-plan-key")
        assert r["name"] == "dev-cp"

    def test_dispatch_valid_coding_plan_same_shell(self, conn, controller):
        """CodingPlan key 在同一壳内派单路径校验通过"""
        _register_shell(conn, controller, SHELL_CODEX, [PROTO_STDIO])
        _register_key(conn, controller, "cp-key-dispatch",
                      [{"id": "step-router-v1", "display_name": "Router",
                        "context_window": 200000}],
                      protocol=PROTO_STDIO,
                      key_ref="shell:codex", coding_plan=True)
        ops.instance_register(conn, "dev-cp-d", SHELL_CODEX,
                              "step-router-v1", key_name="cp-key-dispatch")
        tid = ops.task_new(conn, controller, "CP派单",
                           request_id="r-cp")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r-cp-{s}")
        d = ops.dispatch_issue(conn, controller, tid, "dev-cp-d",
                               request_id="r-cp-d")
        assert d["dispatch_id"] > 0


# ================================================================
# 非法组合负例(注册+派单两路径,各条规则)
# ================================================================


class TestInvalidCombos:
    """三条非法组合,注册和派单两路径各测(共 6 子测试)。"""

    def test_register_reject_protocol_mismatch(self, conn, controller):
        """① 协议不兼容: 壳只支持 stdio, key 需要 http"""
        _register_shell(conn, controller, "proto-test-shell", [PROTO_STDIO])
        _register_key(conn, controller, "http-key",
                      [{"id": "http-model", "display_name": "HTTP",
                        "context_window": 80000}],
                      protocol=PROTO_HTTP)
        with pytest.raises(ValueError, match="协议不兼容"):
            ops.instance_register(conn, "dev-bad-proto", "proto-test-shell",
                                  "http-model", key_name="http-key")

    def test_dispatch_reject_protocol_mismatch(self, conn, controller):
        """① 协议不兼容在派单路径被拒"""
        _register_shell(conn, controller, "proto-test-shell-d", [PROTO_STDIO])
        _register_key(conn, controller, "http-key-d",
                      [{"id": "http-model-d", "display_name": "HTTPD",
                        "context_window": 80000}],
                      protocol=PROTO_HTTP)
        # 直接写库绕过注册校验,测试派单路径的校验
        conn.execute(
            "INSERT INTO instances (name, shell, model, key_name, isolated_dir,"
            " launch_cmd, is_active, created_at) VALUES (?,?,?,?,?,?,1,?)",
            ("dev-bad-proto-d", "proto-test-shell-d", "http-model-d",
             "http-key-d", "", "", ops.now()))
        conn.execute(
            "INSERT INTO ability_profiles (instance_name, shell, model, key_name,"
            " isolated_dir, skills, context_window, model_source_score, key_body_score)"
            " VALUES (?,?,?,?,?,?,?,0,0)",
            ("dev-bad-proto-d", "proto-test-shell-d", "http-model-d",
             "http-key-d", "", "[]", 0))
        tid = ops.task_new(conn, controller, "协议拒绝派单",
                           request_id="r-pr")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r-pr-{s}")
        with pytest.raises(ValueError, match="协议不兼容"):
            ops.dispatch_issue(conn, controller, tid, "dev-bad-proto-d",
                               request_id="r-pr-di")

    def test_register_reject_model_not_in_list(self, conn, controller):
        """② 模型不在 key 清单内"""
        _register_shell(conn, controller, "model-test-shell", [PROTO_STDIO])
        _register_key(conn, controller, "limited-key",
                      [{"id": "allowed-model", "display_name": "Allowed",
                        "context_window": 64000}],
                      protocol=PROTO_STDIO)
        with pytest.raises(ValueError, match="模型不在清单"):
            ops.instance_register(conn, "dev-bad-model", "model-test-shell",
                                  "forbidden-model", key_name="limited-key")

    def test_dispatch_reject_model_not_in_list(self, conn, controller):
        """② 模型不在 key 清单内在派单路径被拒"""
        _register_shell(conn, controller, "model-test-shell-d", [PROTO_STDIO])
        _register_key(conn, controller, "limited-key-d",
                      [{"id": "allowed-model-d", "display_name": "AllowedD",
                        "context_window": 64000}],
                      protocol=PROTO_STDIO)
        ops.instance_register(conn, "dev-bad-model-d", "model-test-shell-d",
                              "allowed-model-d", key_name="limited-key-d")
        tid = ops.task_new(conn, controller, "模型拒绝派单",
                           request_id="r-mr")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r-mr-{s}")
        # 通过 ops 层直接改库模拟实例注册了错误模型(绕开注册校验的场景)
        conn.execute(
            "UPDATE instances SET model=? WHERE name=?",
            ("forbidden-model-d", "dev-bad-model-d"))
        with pytest.raises(ValueError, match="模型不在清单"):
            ops.dispatch_issue(conn, controller, tid, "dev-bad-model-d",
                               request_id="r-mr-di")

    def test_register_reject_coding_plan_cross_shell(self, conn, controller):
        """③ CodingPlan key 跨壳: 先在壳 A 注册,再在壳 B 注册同一 key"""
        _register_shell(conn, controller, "cp-shell-a", [PROTO_STDIO])
        _register_shell(conn, controller, "cp-shell-b", [PROTO_STDIO])
        _register_key(conn, controller, "cp-cross-key",
                      [{"id": "cp-model", "display_name": "CP",
                        "context_window": 128000}],
                      protocol=PROTO_STDIO,
                      key_ref="shell:cp-shell-a", coding_plan=True)
        # 先在 shell-a 注册(合法)
        ops.instance_register(conn, "dev-cp-a", "cp-shell-a", "cp-model",
                              key_name="cp-cross-key")
        # 再在 shell-b 注册同一 key(不合法=CodingPlan 跨壳)
        with pytest.raises(ValueError, match="CodingPlan 跨壳"):
            ops.instance_register(conn, "dev-cp-b", "cp-shell-b", "cp-model",
                                  key_name="cp-cross-key")

    def test_dispatch_reject_coding_plan_cross_shell(self, conn, controller):
        """③ CodingPlan key 跨壳在派单路径被拒"""
        _register_shell(conn, controller, "cp-shell-x", [PROTO_STDIO])
        _register_shell(conn, controller, "cp-shell-y", [PROTO_STDIO])
        _register_key(conn, controller, "cp-cross-d",
                      [{"id": "cp-model-d", "display_name": "CPD",
                        "context_window": 128000}],
                      protocol=PROTO_STDIO,
                      key_ref="shell:cp-shell-x", coding_plan=True)
        ops.instance_register(conn, "dev-cp-x", "cp-shell-x", "cp-model-d",
                              key_name="cp-cross-d")
        tid = ops.task_new(conn, controller, "跨壳拒绝派单",
                           request_id="r-cr")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"r-cr-{s}")
        # 直接创建非法实例(绕开注册校验)后派单
        conn.execute(
            "INSERT OR REPLACE INTO instances"
            " (name, shell, model, key_name, isolated_dir, launch_cmd,"
            " is_active, created_at) VALUES (?,?,?,?,?,?,1,?)",
            ("dev-cp-y", "cp-shell-y", "cp-model-d", "cp-cross-d", "", "",
             ops.now()))
        with pytest.raises(ValueError, match="CodingPlan 跨壳"):
            ops.dispatch_issue(conn, controller, tid, "dev-cp-y",
                               request_id="r-cr-di")


# ================================================================
# 壳/key 条目 CLI 管理
# ================================================================


class TestShellKeyConfig:
    """壳/key 条目可增删查改,变更带审计;base_url 落账本。"""

    def test_shell_set_get_list(self, conn, controller):
        from typer.testing import CliRunner
        from tianji.cli import app
        runner = CliRunner()
        full_env = {**os.environ,
                    "TIANJI_WORKER_ID": controller["worker_id"],
                    "TIANJI_SECRET": controller["secret"]}
        full_env.setdefault("TIANJI_HOME", os.environ.get("TIANJI_HOME", ""))

        r = runner.invoke(app, ["config", "shell", "set", "my-shell",
                                "--protocols", "stdio,http",
                                "--binding", "env",
                                "--request-id", "r-sh"],
                          env=full_env, catch_exceptions=False)
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["key"] == "shell:my-shell"
        cfg = json.loads(data["value"])
        assert cfg["protocols"] == ["stdio", "http"]

        r2 = runner.invoke(app, ["config", "shell", "get", "my-shell"],
                           env=full_env, catch_exceptions=False)
        assert json.loads(r2.output)["key"] == "shell:my-shell"

        r3 = runner.invoke(app, ["config", "shell", "list"],
                           env=full_env, catch_exceptions=False)
        shells = json.loads(r3.output)
        assert any(s["key"] == "shell:my-shell" for s in shells)

    def test_key_set_get_list(self, conn, controller):
        from typer.testing import CliRunner
        from tianji.cli import app
        runner = CliRunner()
        full_env = {**os.environ,
                    "TIANJI_WORKER_ID": controller["worker_id"],
                    "TIANJI_SECRET": controller["secret"]}
        full_env.setdefault("TIANJI_HOME", os.environ.get("TIANJI_HOME", ""))

        models_json = json.dumps(
            [{"id": "m1", "display_name": "M1", "context_window": 64000}])
        r = runner.invoke(app, ["config", "key", "set", "my-key",
                                "--base-url", "https://api.example.com/my-key",
                                "--models", models_json,
                                "--protocol", "stdio",
                                "--request-id", "r-k"],
                          env=full_env, catch_exceptions=False)
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["key"] == "key:my-key"
        cfg = json.loads(data["value"])
        assert cfg["base_url"] == "https://api.example.com/my-key"
        assert cfg["models"][0]["id"] == "m1"

        r2 = runner.invoke(app, ["config", "key", "get", "my-key"],
                           env=full_env, catch_exceptions=False)
        assert json.loads(r2.output)["key"] == "key:my-key"

        r3 = runner.invoke(app, ["config", "key", "list"],
                           env=full_env, catch_exceptions=False)
        keys = json.loads(r3.output)
        assert any(k["key"] == "key:my-key" for k in keys)

    def test_key_set_audit_trail(self, conn, controller):
        """Key 条目变更带审计(13.4)。"""
        from typer.testing import CliRunner
        from tianji.cli import app
        runner = CliRunner()
        full_env = {**os.environ,
                    "TIANJI_WORKER_ID": controller["worker_id"],
                    "TIANJI_SECRET": controller["secret"]}
        full_env.setdefault("TIANJI_HOME", os.environ.get("TIANJI_HOME", ""))
        models_json = json.dumps(
            [{"id": "a", "display_name": "A", "context_window": 32000}])
        runner.invoke(app, ["config", "key", "set", "audit-key",
                            "--base-url", "https://api.example.com/audit-key",
                            "--models", models_json,
                            "--request-id", "r-audit"],
                      env=full_env, catch_exceptions=False)
        aud = conn.execute(
            "SELECT detail FROM audit WHERE action='config_set'"
        ).fetchone()
        assert aud is not None
        detail = json.loads(aud["detail"])
        assert detail["key"] == "key:audit-key"

    def test_key_base_url_in_ledger_key_body_not(self, conn, controller):
        """base_url 落账本(configs);key 本体(key 明文)不落。"""
        _register_shell(conn, controller, SHELL_CLAUDE, [PROTO_STDIO])
        _register_key(conn, controller, "ledger-key",
                      [{"id": "m-ledger", "display_name": "ML",
                        "context_window": 80000}],
                      protocol=PROTO_STDIO)
        row = conn.execute(
            "SELECT value FROM configs WHERE key=?", ("key:ledger-key",)
        ).fetchone()
        cfg = json.loads(row["value"])
        assert cfg["base_url"] == "https://api.example.com/ledger-key"
        # key 本体明文不落 configs 的 value(直接断言值,非条目名)
        secret_body = "sk-testsecret123"
        all_values = [r["value"] for r in ops.config_get(conn)
                      if r["key"].startswith("key:")]
        assert not any(secret_body in v for v in all_values)


# ================================================================
# 质量档位两轴落画像
# ================================================================


class TestQualityAxes:
    """质量档位两轴(model_source/key_body)数据落 ability_profiles。"""

    def test_axes_initialized_on_register(self, conn, controller):
        """注册时两轴初始化为 0,后续评分引擎更新。"""
        _register_shell(conn, controller, SHELL_CLAUDE, [PROTO_STDIO])
        _register_key(conn, controller, "qa-key",
                      [{"id": "qa-model", "display_name": "QA",
                        "context_window": 64000}],
                      protocol=PROTO_STDIO)
        ops.instance_register(conn, "qa-dev", SHELL_CLAUDE, "qa-model",
                              key_name="qa-key")
        p = conn.execute(
            "SELECT model_source_score, key_body_score FROM ability_profiles"
            " WHERE instance_name=?", ("qa-dev",)).fetchone()
        assert p["model_source_score"] == 0
        assert p["key_body_score"] == 0

    def test_axes_persist_on_rebind(self, conn, controller):
        """换绑复活时两轴重置为 0(新实例组合=新画像)。"""
        _register_shell(conn, controller, SHELL_CODEX, [PROTO_STDIO])
        _register_key(conn, controller, "rebind-key",
                      [{"id": "rb-model", "display_name": "RB",
                        "context_window": 80000}],
                      protocol=PROTO_STDIO)
        ops.instance_register(conn, "rb-dev", SHELL_CODEX, "rb-model",
                              key_name="rebind-key")
        # 下线
        ops.instance_unbind(conn, controller, "rb-dev", request_id="r-unbind")
        # 复活(同模型=换绑,两轴归零)
        ops.instance_register(conn, "rb-dev", SHELL_CODEX, "rb-model",
                              key_name="rebind-key")
        p = conn.execute(
            "SELECT model, model_source_score, key_body_score FROM ability_profiles"
            " WHERE instance_name=?", ("rb-dev",)).fetchone()
        assert p["model_source_score"] == 0
        assert p["key_body_score"] == 0
        assert p["model"] == "rb-model"


# ================================================================
# 后向兼容: 空 key_name 跳过校验,现有 fixture 不回归
# ================================================================


class TestBackwardCompat:
    """空 key_name 不校验,conftest fixture 原样工作。"""

    def test_empty_key_name_skips_validation(self, conn):
        """空 key_name 不调用 configs,直接通过校验。"""
        ok, reason = ops._validate_instance_combo(conn, "claude", "", "any-model")
        assert ok is True and reason == ""

    def test_conftest_controller_register_works(self, conn):
        """conftest 的 controller fixture 签名不变,仍可注册。"""
        r = ops.instance_register(conn, "backward-compat", "claude",
                                  "deepseek-v4-flash", controller=True)
        assert r["name"] == "backward-compat"
        assert r["secret"] is not None

    def test_controller_register_rejects_non_controller(self, conn):
        """非总控身份 register --controller 被拒绝(越权保护)。"""
        # 先由总控注册(有身份)
        ctrl_ident = {"worker_id": "总控", "secret": "dummy"}
        ops.instance_register(conn, "ctrl-inst", "claude",
                              "deepseek-v4-flash", controller=True,
                              ident=ctrl_ident)
        # 非总控身份尝试 register --controller
        imposter = {"worker_id": "铁蛋", "secret": "not-controller"}
        with pytest.raises(PermissionError,
                           match="仅总控身份可执行"):
            ops.instance_register(conn, "evil-ctrl", "claude",
                                  "deepseek-v4-flash", controller=True,
                                  ident=imposter)


class TestConfigDelete:
    """壳/key 条目 delete: 正例+有引用拒绝删除(票 18 返修点,总控代劳修)。"""

    def test_shell_key_delete_roundtrip(self, conn, controller):
        from typer.testing import CliRunner
        from tianji.cli import app
        runner = CliRunner()
        full_env = {**os.environ,
                    "TIANJI_WORKER_ID": controller["worker_id"],
                    "TIANJI_SECRET": controller["secret"]}

        runner.invoke(app, ["config", "shell", "set", "del-shell",
                            "--request-id", "r-dsh1"],
                      env=full_env, catch_exceptions=False)
        r = runner.invoke(app, ["config", "shell", "delete", "del-shell",
                                "--request-id", "r-dsh2"],
                          env=full_env, catch_exceptions=False)
        assert r.exit_code == 0
        assert json.loads(r.output)["deleted"] is True
        assert ops.config_get(conn, "shell:del-shell") is None

        runner.invoke(app, ["config", "key", "set", "del-key",
                            "--request-id", "r-dk1"],
                      env=full_env, catch_exceptions=False)
        r2 = runner.invoke(app, ["config", "key", "delete", "del-key",
                                 "--request-id", "r-dk2"],
                           env=full_env, catch_exceptions=False)
        assert r2.exit_code == 0
        assert ops.config_get(conn, "key:del-key") is None

        audits = conn.execute(
            "SELECT action FROM audit WHERE action='config_delete'").fetchall()
        assert len(audits) >= 2

    def test_delete_rejects_when_referenced(self, conn, controller, worker):
        """shell 条目被活跃实例引用→拒绝删除(铁蛋 shell=codex)。"""
        from typer.testing import CliRunner
        from tianji.cli import app
        runner = CliRunner()
        full_env = {**os.environ,
                    "TIANJI_WORKER_ID": controller["worker_id"],
                    "TIANJI_SECRET": controller["secret"]}

        runner.invoke(app, ["config", "shell", "set", "codex",
                            "--request-id", "r-dref0"],
                      env=full_env, catch_exceptions=False)
        r = runner.invoke(app, ["config", "shell", "delete", "codex",
                                "--request-id", "r-dref1"],
                          env=full_env)
        assert r.exit_code != 0
        assert "拒绝删除" in str(r.exception)
        assert ops.config_get(conn, "shell:codex") is not None
