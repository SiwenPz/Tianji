"""能力画像字段建档与档案追加(票 06 验收 1/7): instance_register 建档+update_profile_notes。"""

import json
import time

import pytest

from tianji import ops


def _register_shell_key_multi(conn, controller, key_name="test-key"):
    ops.config_set(conn, controller, "shell:codex", json.dumps({"binding": "env", "protocols": ["stdio"], "isolated_dir_mode": "env_home"}, ensure_ascii=False), request_id=f"r-sh-{key_name}")
    ops.config_set(conn, controller, f"key:{key_name}", json.dumps({"base_url": f"https://api.example.com/{key_name}", "models": [{"id": "step-router-v1", "display_name": "R", "context_window": 200000}, {"id": "old-model", "display_name": "Old", "context_window": 64000}, {"id": "new-model", "display_name": "New", "context_window": 128000}], "protocol": "stdio"}, ensure_ascii=False), request_id=f"r-k-{key_name}")


class TestAbilityProfileFields:
    """ability_profiles 字段在注册/换绑时正确建档。"""

    def test_register_initializes_profile_fields(self, conn, controller):
        _register_shell_key_multi(conn, controller, "profile-key")
        r = ops.instance_register(
            conn, "profile-dev", "codex", "step-router-v1",
            skills='["coding", "debug"]', context_window=128000,
            permission_granularity="project", profile_notes="已知坑: 大文件卡顿",
            key_name="profile-key")
        assert r["name"] == "profile-dev"
        row = conn.execute(
            "SELECT * FROM ability_profiles WHERE instance_name=?",
            ("profile-dev",)).fetchone()
        assert row["skills"] == '["coding", "debug"]'
        assert row["permission_granularity"] == "project"
        assert row["context_window"] == 128000
        assert row["score"] == 60
        assert "大文件卡顿" in row["notes"]

    def test_rebind_resets_axes_and_updates_fields(self, conn, controller):
        _register_shell_key_multi(conn, controller, "rebind-key")
        ops.instance_register(
            conn, "rebind-p", "codex", "old-model",
            skills='["old"]', context_window=64000,
            permission_granularity="readonly", profile_notes="旧坑",
            key_name="rebind-key")
        ops.instance_unbind(conn, controller, "rebind-p", request_id="r-unbind")
        ops.instance_register(
            conn, "rebind-p", "codex", "new-model",
            skills='["new"]', context_window=128000,
            permission_granularity="project", profile_notes="新坑",
            key_name="rebind-key")
        row = conn.execute(
            "SELECT * FROM ability_profiles WHERE instance_name=?",
            ("rebind-p",)).fetchone()
        assert row["model"] == "new-model"
        assert row["skills"] == '["new"]'
        assert row["permission_granularity"] == "project"
        assert row["context_window"] == 128000
        assert "新坑" in row["notes"]
        assert row["model_source_score"] == 0
        assert row["key_body_score"] == 0

    def test_backward_compat_empty_permission_and_notes(self, conn, controller):
        """空 permission_granularity 和 notes 不报错(后向兼容)。"""
        r = ops.instance_register(
            conn, "compat-dev", "claude", "deepseek-v4-flash")
        assert r["name"] == "compat-dev"
        row = conn.execute(
            "SELECT * FROM ability_profiles WHERE instance_name=?",
            ("compat-dev",)).fetchone()
        assert row["permission_granularity"] == ""
        assert row["notes"] == ""


class TestUpdateProfileNotes:
    """实例档案可追加录入。"""

    def test_append_notes_creates_history(self, conn, controller):
        _register_shell_key_multi(conn, controller, "note-key")
        ops.instance_register(
            conn, "note-dev", "codex", "step-router-v1",
            profile_notes="初始坑", key_name="note-key")
        ops.update_profile_notes(conn, "note-dev", "429 限流")
        row = conn.execute(
            "SELECT notes FROM ability_profiles WHERE instance_name=?",
            ("note-dev",)).fetchone()
        assert "初始坑" in row["notes"]
        assert "429 限流" in row["notes"]

    def test_append_to_empty_notes(self, conn, controller):
        _register_shell_key_multi(conn, controller, "empty-note-key")
        ops.instance_register(conn, "empty-note", "codex", "step-router-v1", key_name="empty-note-key")
        ops.update_profile_notes(conn, "empty-note", "首次追加")
        row = conn.execute(
            "SELECT notes FROM ability_profiles WHERE instance_name=?",
            ("empty-note",)).fetchone()
        assert "首次追加" in row["notes"]


class TestInstanceDelete:
    """实例物理删除(13.6 增删条目,总控专属+审计)。"""

    def test_delete_removes_instance_and_profile(self, conn, controller, worker):
        _register_shell_key_multi(conn, controller, "del-key")
        ops.instance_register(conn, "del-dev", "codex", "step-router-v1", key_name="del-key")
        assert conn.execute("SELECT 1 FROM instances WHERE name='del-dev'").fetchone()
        assert conn.execute("SELECT 1 FROM ability_profiles WHERE instance_name='del-dev'").fetchone()
        # 非总控被拒
        with pytest.raises(PermissionError):
            ops.instance_delete(conn, worker, "del-dev", request_id="r-del-noauth")
        r = ops.instance_delete(conn, controller, "del-dev", request_id="r-del")
        assert r["deleted"] is True
        assert conn.execute("SELECT 1 FROM instances WHERE name='del-dev'").fetchone() is None
        assert conn.execute("SELECT 1 FROM ability_profiles WHERE instance_name='del-dev'").fetchone() is None

    def test_delete_rejects_busy_worker(self, conn, controller, worker):
        _register_shell_key_multi(conn, controller, "busy-key")
        ops.instance_register(conn, "busy-dev", "codex", "step-router-v1", key_name="busy-key")
        tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
        ops.dispatch_issue(conn, controller, tid, "busy-dev", request_id="r-issue")
        with pytest.raises(ValueError, match="在途派单"):
            ops.instance_delete(conn, controller, "busy-dev", request_id="r-del-busy")

    def test_delete_unknown_instance(self, conn, controller):
        with pytest.raises(KeyError, match="未注册"):
            ops.instance_delete(conn, controller, "ghost", request_id="r-del-ghost")


class TestInstanceUnbind:
    """实例换绑/下线(14.3 回收需总控确认)。"""

    def test_unbind_requires_controller(self, conn, controller, worker):
        """非总控 unbind 被拒绝(对照 instance_delete 同强度)。"""
        _register_shell_key_multi(conn, controller, "unbind-key")
        ops.instance_register(conn, "unbind-dev", "codex", "step-router-v1",
                              key_name="unbind-key")
        with pytest.raises(PermissionError,
                           match="instance_unbind 仅总控身份可执行"):
            ops.instance_unbind(conn, worker, "unbind-dev",
                                request_id="r-noctrl")
        r = ops.instance_unbind(conn, controller, "unbind-dev",
                                request_id="r-ctrl")
        assert r["is_active"] == 0
