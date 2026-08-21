"""CLI 层: instance register 透传能力画像选项 + instance profile-notes 子命令(票 06 返修)。"""

import json
import os

from typer.testing import CliRunner

from tianji.cli import app
from tianji.db import connect

runner = CliRunner()


def _invoke(args, env=None):
    # CliRunner 传 env 时整体替换环境,须合并 TIANJI_HOME(conftest 注入)
    full = dict(env or {})
    full.setdefault("TIANJI_HOME", os.environ["TIANJI_HOME"])
    r = runner.invoke(app, args, env=full)
    assert r.exit_code == 0, f"CLI 失败 {args}: {r.output}\n{r.exception}"
    return json.loads(r.output) if r.output.strip() else {}


class TestRegisterProfileOptions:
    """instance register 的 --permission-granularity / --profile-notes 透传 ops 建档。"""

    def test_register_with_permission_and_notes(self, tianji_home):
        _invoke(["instance", "register", "画像甲", "codex", "step-router-v1",
                 "--permission-granularity", "project",
                 "--profile-notes", "已知坑: 大文件卡顿"])
        conn = connect()
        row = conn.execute(
            "SELECT permission_granularity, notes FROM ability_profiles"
            " WHERE instance_name=?", ("画像甲",)).fetchone()
        assert row is not None
        assert row["permission_granularity"] == "project"
        assert "大文件卡顿" in row["notes"]

    def test_register_without_options_backward_compat(self, tianji_home):
        """不带新选项注册不报错,字段为空(后向兼容)。"""
        _invoke(["instance", "register", "画像乙", "claude", "deepseek-v4-flash"])
        conn = connect()
        row = conn.execute(
            "SELECT permission_granularity, notes FROM ability_profiles"
            " WHERE instance_name=?", ("画像乙",)).fetchone()
        assert row is not None
        assert row["permission_granularity"] == ""
        assert row["notes"] == ""


class TestProfileNotesSubcommand:
    """instance profile-notes NAME TEXT 追加档案(走 ops.update_profile_notes)。"""

    def test_profile_notes_appends(self, tianji_home):
        _invoke(["instance", "register", "画像丙", "codex", "step-router-v1",
                 "--profile-notes", "初始坑"])
        out = _invoke(["instance", "profile-notes", "画像丙", "429 限流"])
        assert "429 限流" in out["notes"]
        conn = connect()
        row = conn.execute(
            "SELECT notes FROM ability_profiles WHERE instance_name=?",
            ("画像丙",)).fetchone()
        assert "初始坑" in row["notes"]
        assert "429 限流" in row["notes"]

    def test_profile_notes_first_append_on_empty(self, tianji_home):
        _invoke(["instance", "register", "画像丁", "codex", "step-router-v1"])
        out = _invoke(["instance", "profile-notes", "画像丁", "首次追加"])
        assert "首次追加" in out["notes"]
        conn = connect()
        row = conn.execute(
            "SELECT notes FROM ability_profiles WHERE instance_name=?",
            ("画像丁",)).fetchone()
        assert "首次追加" in row["notes"]

    def test_profile_notes_unknown_instance_fails(self, tianji_home):
        r = runner.invoke(app, ["instance", "profile-notes", "不存在", "x"],
                          env={"TIANJI_HOME": os.environ["TIANJI_HOME"]})
        assert r.exit_code != 0
        assert isinstance(r.exception, KeyError)
        assert "画像不存在" in str(r.exception)
