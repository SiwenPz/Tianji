"""Task-06: Monitor dead branch fix (adapter dict → shell name check) + behavior tests."""

import os
import json
import time
import pytest

from tianji import ops
from tianji.monitor import _check_tier3_capability, _tick
from tianji.db import now


def _escalations(conn):
    """Local copy from test_monitor.py."""
    rows = conn.execute(
        "SELECT payload FROM messages WHERE type='escalation' ORDER BY seq").fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _register_codex_worker(conn):
    """Register a codex worker (tier3_process_alive=True in builtin template)."""
    r = ops.instance_register(conn, "codex-worker-06", "codex", "step-router-v1")
    return r


def _register_claude_worker(conn):
    """Register a claude worker (no tier3_process_alive)."""
    r = ops.instance_register(conn, "claude-worker-06", "claude", "deepseek-v4-flash")
    return r


def _spy_codex_exec_alive(monkeypatch, ret):
    """Patch codex_exec_alive with a call-recording spy; return the calls list."""
    calls = []
    monkeypatch.setattr(
        "tianji.adapters.codex_exec.codex_exec_alive",
        lambda pid: calls.append(pid) or ret)
    return calls


# ── Dead branch fix: _check_tier3_capability ──

class TestDeadBranchFix:
    """Line 166 dead branch: adapter==='codex' never matched (adapter is dict).

    以下断言用 spy 证明 codex_exec_alive 真实被路由调用——旧死分支代码下
    调用永远不发生,断言必红,不是恒绿废话。
    """

    def test_codex_shell_reaches_codex_exec_alive(self, conn, monkeypatch):
        """codex: _check_tier3_capability 必须真实调用 codex_exec_alive。"""
        _register_codex_worker(conn)
        calls = _spy_codex_exec_alive(monkeypatch, True)
        result = _check_tier3_capability(conn, "codex", pid=12345)
        assert calls == [12345], "codex 壳未路由到 codex_exec_alive(死分支仍在)"
        assert result is True  # 适配器验证通过 → 豁免成立

    def test_codex_zero_pid_short_circuits(self, conn, monkeypatch):
        """codex + pid=0: 提前返回 False,不进入 codex_exec_alive 分支。"""
        _register_codex_worker(conn)
        calls = _spy_codex_exec_alive(monkeypatch, True)
        result = _check_tier3_capability(conn, "codex", pid=0)
        assert result is False
        assert calls == [], "pid=0 应短路,不得调适配器"

    def test_codex_adapter_denies_returns_false(self, conn, monkeypatch):
        """codex + 适配器判定进程死: 豁免不成立,返回 False(但路由必须到达)。"""
        _register_codex_worker(conn)
        calls = _spy_codex_exec_alive(monkeypatch, False)
        result = _check_tier3_capability(conn, "codex", pid=os.getpid())
        assert result is False
        assert calls == [os.getpid()]

    def test_non_codex_shell_returns_false(self, conn, monkeypatch):
        """非 codex 壳: 模板无 tier3_process_alive,不路由,直接 False。"""
        _register_claude_worker(conn)
        calls = _spy_codex_exec_alive(monkeypatch, True)
        result = _check_tier3_capability(conn, "claude", pid=os.getpid())
        assert result is False
        assert calls == []

    def test_unknown_shell_returns_false(self, conn):
        """未知壳: KeyError 被捕获,返回 False。"""
        result = _check_tier3_capability(conn, "nonexistent-06", pid=123)
        assert result is False


# ── 行为端到端: 监控器 tick 中 tier3 兜底 ──

class TestTier3LivenessInTick:
    """_tick 钩子失效检测链(对账③): 转录增长+事件超 T2 时才走到 tier3 兜底。

    两个用例都把事件 ts 回拨超过 T2(600s)并让转录持续增长,保证检测链
    真实到达 tier3 分支——不打补丁时这些断言在恒绿(转录不增长根本进不去)。
    tier3 判定用 codex_exec_alive 补丁,绝不对 _check_tier3_capability
    自身打补丁(否则测的是补丁不是代码)。
    """

    def _setup_codex_dispatch(self, conn, controller, req_prefix):
        _register_codex_worker(conn)
        tid = ops.task_new(conn, controller, "Tier3任务",
                           request_id=f"{req_prefix}-new")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s,
                                request_id=f"{req_prefix}-{s}")
        did = ops.dispatch_issue(
            conn, controller, tid, "codex-worker-06",
            request_id=f"{req_prefix}-issue")["dispatch_id"]
        from tianji.render import spawn
        sp = spawn(conn, "codex-worker-06", did)
        env_o = {
            "TIANJI_WORKER_ID": sp["env"]["TIANJI_WORKER_ID"],
            "TIANJI_SECRET": sp["env"]["TIANJI_SECRET"],
            "TIANJI_DISPATCH_ID": str(did),
        }
        return env_o

    def _ingest_and_age_events(self, conn, env_o, session_id):
        """喂两条事件并把 ts 回拨超 T2(600s),使钩子失效检测链真实可达。"""
        from tianji.events import ingest_event
        ingest_event(conn, env_o, {
            "session_id": session_id, "event_type": "session_start"})
        ingest_event(conn, env_o, {
            "session_id": session_id, "event_type": "pre_tool_use"})
        old_ts = int(time.time()) - 700
        conn.execute(
            "UPDATE messages SET ts=? WHERE type='event' AND sender=?",
            (old_ts, env_o["TIANJI_WORKER_ID"]))

    def test_codex_process_alive_prevents_hook_degraded(
        self, conn, controller, monkeypatch
    ):
        """codex 壳: 事件超 T2 + 转录增长,但 tier3 进程验证活 → 不报钩子失效。"""
        import tianji.monitor as mon

        env_o = self._setup_codex_dispatch(conn, controller, "r-t3")
        self._ingest_and_age_events(conn, env_o, "t3-s")
        conn.execute(
            "UPDATE instance_registrations SET pid=?"
            " WHERE instance_name='codex-worker-06' AND status='active'",
            (os.getpid(),))
        # 转录持续增长(真实转录由 codex 进程写出)
        counter = {"n": 0}

        def _growing(sid, shell="codex", **kw):
            counter["n"] += 100
            return counter["n"]

        monkeypatch.setattr(mon, "_transcript_bytes", _growing)
        monkeypatch.setattr(mon, "_check_network", lambda state: False)
        alive_calls = _spy_codex_exec_alive(monkeypatch, True)
        state = {}
        _tick(conn, state)
        _tick(conn, state)
        hook_hits = [e for e in _escalations(conn)
                     if "钩子失效" in (e.get("reason") or "")]
        assert hook_hits == []
        assert alive_calls, (
            "tier3 兜底未被调用——检测链没到达分支(转录未增长或事件未超 T2),"
            "测试形同虚设")

    def test_tick_isolation_between_cases(self, conn, controller, monkeypatch):
        """旧嫌疑不污染新活性: 先真实触发一次钩子失效,新事件到达后不得误报。"""
        import tianji.monitor as mon

        env_o = self._setup_codex_dispatch(conn, controller, "r-iso")
        self._ingest_and_age_events(conn, env_o, "iso-s")
        counter = {"n": 0}

        def _growing(sid, shell="codex", **kw):
            counter["n"] += 100
            return counter["n"]

        monkeypatch.setattr(mon, "_transcript_bytes", _growing)
        monkeypatch.setattr(mon, "_check_network", lambda state: False)
        # 进程判定为死 → tier3 不豁免,检测链必须真实报出钩子失效
        monkeypatch.setattr(
            "tianji.adapters.codex_exec.codex_exec_alive", lambda pid: False)
        state = {}
        _tick(conn, state)   # 首拍建事件/字节基线
        _tick(conn, state)   # 嫌疑第 1 拍
        _tick(conn, state)   # 嫌疑第 2 拍 → 确认报警
        hook_hits = [e for e in _escalations(conn)
                     if "钩子失效" in (e.get("reason") or "")]
        assert len(hook_hits) == 1, (
            "转录增长+事件超 T2+进程死: 两拍后必须报钩子失效(证明检测链真实)")
        # 新事件到达(工人恢复活性)→ 嫌疑清除,转录停增后不得再报
        from tianji.events import ingest_event
        ingest_event(conn, env_o, {
            "session_id": "iso-s", "event_type": "pre_tool_use"})
        monkeypatch.setattr(
            mon, "_transcript_bytes",
            lambda sid, shell="codex", **kw: counter["n"])
        _tick(conn, state)
        _tick(conn, state)
        hook_hits = [e for e in _escalations(conn)
                     if "钩子失效" in (e.get("reason") or "")]
        assert len(hook_hits) == 1, "新事件到达后不得沿用旧嫌疑重复误报"
