"""Task-14 返工验证: 7 项 polished items."""

import inspect
import json
import os
from pathlib import Path

import pytest

from tianji import ops, hooks, plugins
from tianji.db import connect, task_dir
from tianji.render import spawn


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    os.environ["TIANJI_HOME"] = str(home)
    return str(home)


def _setup_conn(tmp_path):
    _make_home(tmp_path)
    conn = connect()
    from tianji.ops import ensure_defaults
    ensure_defaults(conn)
    cr = ops.instance_register(
        conn, "总控", "claude", "deepseek-v4-flash", controller=True)
    wk_r = ops.instance_register(
        conn, "验证工", "codex", "step-router-v1")
    rv_r = ops.instance_register(
        conn, "审核员", "claude", "deepseek-v4-flash-ng")
    ctrl = {"worker_id": cr["name"], "secret": cr["secret"]}
    wk = {"worker_id": wk_r["name"], "secret": wk_r["secret"]}
    rv = {"worker_id": rv_r["name"], "secret": rv_r["secret"]}
    return conn, ctrl, wk, rv


def _review_dispatch(conn, ctrl, wk, rv, axis, tag):
    """建任务→派工→结算→派审核(指定 axis),返回审核派单 id。"""
    tid = ops.task_new(conn, ctrl, f"t-{tag}", request_id=f"{tag}-new")["task_id"]
    ops.task_set_verify_cmd(conn, ctrl, tid, 'python -c "pass"',
                            request_id=f"{tag}-vc")
    ops.task_scope_set(conn, ctrl, tid, ["src"], request_id=f"{tag}-sc")
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, ctrl, tid, s, request_id=f"{tag}-{s}")
    did = ops.dispatch_issue(conn, ctrl, tid, wk["worker_id"],
                             request_id=f"{tag}-issue")["dispatch_id"]
    sw = spawn(conn, wk["worker_id"], did)
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("报告", encoding="utf-8")
    worker_env = {**sw["env"], "TIANJI_DISPATCH_ID": str(did)}
    ops.dispatch_settle(conn, worker_env, did, str(rp), "ok")
    return ops.dispatch_issue(conn, ctrl, tid, rv["worker_id"],
                              role="reviewer", axis=axis,
                              request_id=f"{tag}-rev")["dispatch_id"]


class _EnvGuard:
    """with 块内临时切换 TIANJI_WORKER_ID/TIANJI_SECRET,退出恢复。"""

    def __init__(self, ident):
        self.ident = ident
        self.old = {}

    def __enter__(self):
        for k in ("TIANJI_WORKER_ID", "TIANJI_SECRET"):
            self.old[k] = os.environ.get(k)
        os.environ["TIANJI_WORKER_ID"] = self.ident["worker_id"]
        os.environ["TIANJI_SECRET"] = self.ident["secret"]
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 1. 质量轴清单注入: 读 configs quality_axis_checklist,只注入质量轴
# ---------------------------------------------------------------------------

class TestQualityChecklist:

    def test_quality_axis_injects_default_checklist(self, tmp_path):
        """质量轴审核任务书带 configs 默认清单正文(维度+核查点)。"""
        from tianji.render import _review_section
        conn, ctrl, wk, rv = _setup_conn(tmp_path)
        with _EnvGuard(ctrl):
            rdid = _review_dispatch(conn, ctrl, wk, rv, "quality", "q1")
            rd = conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (rdid,)).fetchone()
            section = _review_section(conn, rd)
        assert "质量轴审核清单" in section
        checklist = json.loads(ops.DEFAULTS["quality_axis_checklist"])
        for item in checklist:
            assert item["dimension"] in section, \
                f"清单维度 '{item['dimension']}' 未注入"
            for chk in item["checks"]:
                assert chk in section, f"核查点 '{chk}' 未注入"
        conn.close()

    def test_checklist_follows_config_change(self, tmp_path):
        """总控改了 configs 清单,任务书跟着变(不硬编码)。"""
        from tianji.render import _review_section
        conn, ctrl, wk, rv = _setup_conn(tmp_path)
        custom = [{"dimension": "自定义维度X",
                   "checks": ["自定义核查点Y"]}]
        conn.execute(
            "INSERT OR REPLACE INTO configs (key, value, updated_at)"
            " VALUES ('quality_axis_checklist', ?, '2026-08-30')",
            (json.dumps(custom, ensure_ascii=False),))
        with _EnvGuard(ctrl):
            rdid = _review_dispatch(conn, ctrl, wk, rv, "quality", "q2")
            rd = conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (rdid,)).fetchone()
            section = _review_section(conn, rd)
        assert "自定义维度X" in section
        assert "自定义核查点Y" in section
        assert "测试真伪" not in section, "改配置后旧默认清单不应再出现"
        conn.close()

    def test_spec_axis_no_checklist(self, tmp_path):
        """spec 轴审核任务书不注入质量轴清单。"""
        from tianji.render import _review_section
        conn, ctrl, wk, rv = _setup_conn(tmp_path)
        with _EnvGuard(ctrl):
            rdid = _review_dispatch(conn, ctrl, wk, rv, "spec", "q3")
            rd = conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (rdid,)).fetchone()
            section = _review_section(conn, rd)
        assert "质量轴审核清单" not in section
        assert "测试真伪" not in section
        assert "审核轴: spec" in section
        conn.close()


# ---------------------------------------------------------------------------
# 2. Controller speech i18n (wizard intro text)
# ---------------------------------------------------------------------------

class TestControllerI18n:

    def test_wizard_intros_have_zh_and_en(self):
        """_write_controller_settings builds _INTROS with zh fallback + en."""
        from tianji.wizard import _write_controller_settings
        source = inspect.getsource(_write_controller_settings)
        assert '"zh"' in source
        assert '"en"' in source
        assert "fallback" in source or "回退" in source

    def test_lang_fallback_unsupported(self, tmp_path):
        """Unsupported language -- _write_controller_settings handles gracefully."""
        from tianji.wizard import _write_controller_settings
        conn, _, _, _ = _setup_conn(tmp_path)
        home = Path(".").resolve()
        settings_path = _write_controller_settings(
            home_p=home, home=str(home), shell="claude",
            secret="testsecret")
        assert Path(settings_path).exists()
        conn.close()

    def test_lang_fallback_audit_written(self, tmp_path):
        """未覆盖语言回退中文,且 lang_fallback 审计行真实落账
        (wizard.py 曾有 __import__('tianji.db').now() 死代码: 必抛
        AttributeError 被 except 吞掉,审计行永远写不进)。"""
        from tianji.wizard import _write_controller_settings
        conn, _, _, _ = _setup_conn(tmp_path)
        conn.execute(
            "INSERT OR REPLACE INTO configs (key, value, updated_at)"
            " VALUES ('user_language', 'fr', '2026-08-30')")
        home = Path(".").resolve()
        _write_controller_settings(
            home_p=home, home=str(home), shell="claude", secret="testsecret")
        row = conn.execute(
            "SELECT detail FROM audit WHERE action='lang_fallback'"
            " ORDER BY rowid DESC LIMIT 1").fetchone()
        assert row is not None, "回退未覆盖语言必须写 lang_fallback 审计行"
        detail = json.loads(row["detail"])
        assert detail["requested"] == "fr"
        assert detail["fallback"] == "zh"
        conn.close()

    def test_en_append_system_prompt_bilingual(self, tmp_path):
        """user_language=en 时三段条件话术渲染英文,身份值是真实实例名 总控。"""
        from tianji.wizard import _write_controller_settings
        conn, _, _, _ = _setup_conn(tmp_path)
        conn.execute(
            "INSERT OR REPLACE INTO configs (key, value, updated_at)"
            " VALUES ('user_language', 'en', '2026-08-30')")
        home = Path(".").resolve()
        cards = [{"shell": "codex", "model": "step-router-v1",
                  "key_name": "k1", "source": "key"}]
        settings_path = _write_controller_settings(
            home_p=home, home=str(home), shell="claude",
            secret="testsecret", provider={"key_value": "x"}, cards=cards)
        doc = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        sp = doc["appendSystemPrompt"]
        assert "Your model is ready" in sp, "en 分支工人卡牌话术未渲染英文"
        assert "你的模型已就绪" not in sp
        assert "TIANJI_WORKER_ID=总控" in sp, "en intro 身份值必须是真实实例名 总控"
        assert "TIANJI_WORKER_ID=controller" not in sp
        conn.close()

    def test_en_not_ready_branch_bilingual(self, tmp_path):
        """user_language=en 且 provider 未配齐: 第三段话术渲染英文。"""
        from tianji.wizard import _write_controller_settings
        conn, _, _, _ = _setup_conn(tmp_path)
        conn.execute(
            "INSERT OR REPLACE INTO configs (key, value, updated_at)"
            " VALUES ('user_language', 'en', '2026-08-30')")
        home = Path(".").resolve()
        settings_path = _write_controller_settings(
            home_p=home, home=str(home), shell="claude", secret="testsecret")
        doc = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        sp = doc["appendSystemPrompt"]
        assert "not fully configured yet" in sp
        assert "还没配齐" not in sp
        conn.close()


# ---------------------------------------------------------------------------
# 3. Smart auto-scroll: JS code present in webapp
# ---------------------------------------------------------------------------

class TestAutoScroll:

    def test_auto_scroll_js_present(self):
        """autoScroll function should exist with 120px threshold."""
        src = Path("tianji/webapp.py").read_text(encoding="utf-8")
        assert "autoScroll" in src, "autoScroll function not found"
        assert "120" in src, "120px threshold not found"

    def test_dead_user_scrolled_up_removed(self):
        """_userScrolledUp 死状态(设了从不读)已删除。"""
        src = Path("tianji/webapp.py").read_text(encoding="utf-8")
        assert "_userScrolledUp" not in src, \
            "_userScrolledUp dead state should be removed"

    def test_cockpit_greeting_follows_language(self, tmp_path):
        """过场问候跟随 user_language: zh=你好,天机 / en=Hello, Tianji。"""
        from tianji.webapp import app
        from starlette.testclient import TestClient
        conn, _, _, _ = _setup_conn(tmp_path)
        client = TestClient(app)
        r = client.get("/setup")
        assert r.status_code == 200
        html = r.content.decode("utf-8")
        assert 'text:"你好,天机"' in html
        assert 'text:"Hello, Tianji"' not in html
        conn.execute(
            "INSERT OR REPLACE INTO configs (key, value, updated_at)"
            " VALUES ('user_language', 'en', '2026-08-30')")
        r = client.get("/setup")
        assert r.status_code == 200
        html = r.content.decode("utf-8")
        assert 'text:"Hello, Tianji"' in html
        assert 'text:"你好,天机"' not in html
        conn.close()


# ---------------------------------------------------------------------------
# 4. Plugin reconcile at spawn + monitor tick
# ---------------------------------------------------------------------------

class TestPluginReconcile:

    def test_hooks_scan_all_real_behavior(self, tmp_path):
        """scan_all 返回 dict;节流窗口内第二次调用返回 skipped
        (旧测试 except Exception: pass 吞掉 isinstance(..., list) 必失败=假绿)。"""
        conn, _, _, _ = _setup_conn(tmp_path)
        first = hooks.scan_all(conn, throttle=0)
        assert isinstance(first, dict), "scan_all 应返回 dict"
        second = hooks.scan_all(conn)  # 默认节流 1800s,窗口内必 skipped
        assert second == {"skipped": "window"}, \
            f"节流窗口内应 skipped,实际 {second}"
        conn.close()

    def test_monitor_scan_calls(self):
        """监控器巡检(run_monitor 循环)接 hooks.scan_all 与插件对账。"""
        from tianji.monitor import run_monitor
        source = inspect.getsource(run_monitor)
        assert "scan_all" in source, "run_monitor 应调 hooks.scan_all"
        assert "_reconcile_plugins" in source, "run_monitor 应接插件对账"

    def test_spawn_reconciles_template_plugin(self, tmp_path):
        """spawn 前对账启用的模板类插件: 生成物缺失→机械重生成(三态语义沿用)。"""
        conn, ctrl, wk, _ = _setup_conn(tmp_path)
        with _EnvGuard(ctrl):
            plugins.register(
                conn, ctrl, "测试模板插件", "template", "v1",
                {"template": "正文{slot}", "params": {"slot": "A"},
                 "target": "plugtest/out.md"},
                request_id="pr-reg")
            tid = ops.task_new(conn, ctrl, "t-plug", request_id="plug-new")["task_id"]
            ops.task_set_verify_cmd(conn, ctrl, tid, 'python -c "pass"',
                                    request_id="plug-vc")
            ops.task_scope_set(conn, ctrl, tid, ["src"], request_id="plug-sc")
            for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
                ops.task_transition(conn, ctrl, tid, s, request_id=f"plug-{s}")
            did = ops.dispatch_issue(conn, ctrl, tid, wk["worker_id"],
                                     request_id="plug-issue")["dispatch_id"]
            target = Path(os.environ["TIANJI_HOME"]) / "plugtest" / "out.md"
            assert not target.exists(), "前置: 生成物缺失"
            spawn(conn, wk["worker_id"], did)
            assert target.exists(), "spawn 应对账并重生成缺失的插件生成物"
            text = target.read_text(encoding="utf-8")
            assert "正文A" in text
            assert "tianji-plugin:" in text, "重生成的生成物应带版本指纹头"
        conn.close()

    def test_spawn_reconcile_respects_user_modified(self, tmp_path):
        """三态语义: 用户手工改过生成物→对账不碰+升级总控(消息入账)。"""
        conn, ctrl, wk, _ = _setup_conn(tmp_path)
        with _EnvGuard(ctrl):
            plugins.register(
                conn, ctrl, "测试模板插件", "template", "v1",
                {"template": "正文{slot}", "params": {"slot": "A"},
                 "target": "plugtest/out.md"},
                request_id="um-reg")
            plugins.render_template_plugin(conn, "测试模板插件")
            target = Path(os.environ["TIANJI_HOME"]) / "plugtest" / "out.md"
            target.write_text("用户手工改过的内容", encoding="utf-8")
            tid = ops.task_new(conn, ctrl, "t-um", request_id="um-new")["task_id"]
            ops.task_set_verify_cmd(conn, ctrl, tid, 'python -c "pass"',
                                    request_id="um-vc")
            ops.task_scope_set(conn, ctrl, tid, ["src"], request_id="um-sc")
            for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
                ops.task_transition(conn, ctrl, tid, s, request_id=f"um-{s}")
            did = ops.dispatch_issue(conn, ctrl, tid, wk["worker_id"],
                                     request_id="um-issue")["dispatch_id"]
            spawn(conn, wk["worker_id"], did)
            assert target.read_text(encoding="utf-8") == "用户手工改过的内容", \
                "用户改过的生成物对账不应碰"
            row = conn.execute(
                "SELECT 1 FROM audit WHERE action='plugin_reconcile_diff'"
            ).fetchone()
            assert row is not None, "用户改过须有 plugin_reconcile_diff 审计"
        conn.close()


# ---------------------------------------------------------------------------
# 5. Theme toggle in setup page
# ---------------------------------------------------------------------------

class TestThemeSetup:

    def test_theme_routes_exist(self, tmp_path):
        """Theme endpoints should respond without 404."""
        from tianji.webapp import app
        from starlette.testclient import TestClient
        _, ctrl, _, _ = _setup_conn(tmp_path)
        with _EnvGuard(ctrl):
            client = TestClient(app)
            for route, method, kwargs in [
                ("/api/theme/state", "get", {}),
                ("/api/theme/enable", "post", {"json": {"name": "三国"}}),
                ("/api/theme/disable", "post", {"json": {}}),
            ]:
                fn = client.get if method == "get" else client.post
                r = fn(route, **kwargs)
                assert r.status_code != 404, f"{method.upper()} {route} returned 404"
                assert r.status_code == 200, f"{method.upper()} {route} returned {r.status_code}"

    def test_setup_page_has_theme_section(self, tmp_path):
        """Setup page HTML should contain theme toggle elements."""
        from tianji.webapp import app
        from starlette.testclient import TestClient
        _setup_conn(tmp_path)
        client = TestClient(app)
        r = client.get("/setup")
        assert r.status_code == 200
        html = r.text
        assert "theme" in html.lower() or "主题" in html

    def test_unknown_theme_returns_400(self, tmp_path):
        """未知主题名 → 400 带明确信息,不再裸 500。"""
        from tianji.webapp import app
        from starlette.testclient import TestClient
        _, ctrl, _, _ = _setup_conn(tmp_path)
        with _EnvGuard(ctrl):
            client = TestClient(app)
            r = client.post("/api/theme/enable", json={"name": "不存在的主题"})
            assert r.status_code == 400, \
                f"未知主题应 400,实际 {r.status_code}"
            body = r.json()
            assert "error" in body and "不存在的主题" in body["error"], \
                f"400 应带明确错误信息,实际 {body}"
            # 已知主题仍正常开启
            r2 = client.post("/api/theme/enable", json={"name": "三国"})
            assert r2.status_code == 200


# ---------------------------------------------------------------------------
# 6. Leaderboard plugin block (红黑榜)
# ---------------------------------------------------------------------------

class TestLeaderboardBlock:

    def test_combo_leaderboard_function_exists(self):
        """combo_leaderboard source should be registered in VIEW_SOURCES."""
        from tianji.plugins import VIEW_SOURCES
        assert "combo_leaderboard" in VIEW_SOURCES, \
            "combo_leaderboard not in VIEW_SOURCES"

    def test_combo_leaderboard_no_crash(self, tmp_path):
        """combo_leaderboard should return a list without crashing."""
        from tianji.plugins import combo_leaderboard
        conn, _, _, _ = _setup_conn(tmp_path)
        result = combo_leaderboard(conn)
        assert isinstance(result, list)
        conn.close()

    def test_plugin_blocks_escaped_in_js(self):
        """插件输出拼 innerHTML 前必须 esc() 转义(防插件内容注入)。"""
        src = Path("tianji/webapp.py").read_text(encoding="utf-8")
        assert "renderPluginBlocks" in src
        assert "${esc(b)}" in src, "插件块拼接 innerHTML 前必须 esc() 转义"
        assert "${b}</div>" not in src, "未转义的 ${b} 拼接不应存在"


# ---------------------------------------------------------------------------
# 7. Skills new-shell-onboarding exists and non-empty
# ---------------------------------------------------------------------------

class TestOnboardingSkill:

    def test_skill_file_exists(self):
        """new-shell-onboarding SKILL.md should exist and be meaningful."""
        skill_path = Path("tianji/skills/new-shell-onboarding/SKILL.md")
        assert skill_path.exists(), f"Skill file not found at {skill_path}"
        text = skill_path.read_text(encoding="utf-8")
        assert len(text) > 50, "SKILL.md appears empty or too short"
        assert any(
            kw in text.lower() for kw in ("session", "bootstrap", "shell")
        ), "SKILL.md missing bootstrap/session/shell content"
