"""天机自研技能包(票 16 验收 1-6): 清单/规范机械检查/安装动作/端到端试跑。"""

import os
import re
from pathlib import Path

import pytest

from tianji import ops, wizard

SKILLS_DIR = Path(__file__).parent.parent / "tianji" / "skills"

EXPECTED = ["triage", "wayfinder", "grilling", "domain-modeling", "to-spec",
            "to-tickets", "implement", "tdd", "code-review",
            "new-shell-onboarding"]

# 归口(19.3): 按六角色分工
ROLE_MAP = {
    "triage": "总控", "wayfinder": "总控", "grilling": "总控",
    "domain-modeling": "架构师", "to-spec": "架构师", "to-tickets": "架构师",
    "implement": "实施者", "tdd": "实施者",
    "code-review": "审核者", "new-shell-onboarding": "总控",
}

TIANJI_WORDS = ("账本", "任务书", "状态机", "双轴", "机械校验", "结算",
                "派单", "验收")


def _read(name):
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def test_ten_skills_complete_with_roles():
    """验收 2: 10 技能清单齐全,按六角色归口。"""
    dirs = sorted(d.name for d in SKILLS_DIR.iterdir()
                  if d.is_dir() and (d / "SKILL.md").exists())
    assert dirs == sorted(EXPECTED)
    for name, role in ROLE_MAP.items():
        assert f"归口: {role}" in _read(name)


def test_skill_mechanical_checklist():
    """验收 1: 逐条机械检查——frontmatter/完成判据/防提前完成/天机词汇。"""
    for name in EXPECTED:
        text = _read(name)
        assert re.search(r"^name: " + name + r"$", text, re.M), name
        assert re.search(r"^description: ", text, re.M), name
        assert "## 完成判据" in text, name          # 完成判据可检验
        assert "## 防提前完成" in text, name        # 防提前完成
        assert any(w in text for w in TIANJI_WORDS), name  # 天机词汇 leading words
        # 无"效果显著"类虚词(精简纪律抽查)
        assert "显著提升" not in text and "等等" not in text, name


def test_skills_point_to_ledger_not_rebuild():
    """验收 3(抽查): 技能只写交互礼仪,机制事实指账本/CLI,不重复造机制。"""
    for name in EXPECTED:
        text = _read(name)
        # 每个技能至少指一处账本/CLI 机制(tianji 命令或账本)
        assert ("tianji " in text) or ("账本" in text), name
        # 不自定义状态机/消息类型(机制本体在账本)
        assert "CREATE TABLE" not in text, name


def test_install_skills(conn, controller, tmp_path):
    """验收 4: 向导安装动作可验——目标目录出现 10 技能。"""
    r = wizard.install_skills(conn, controller, str(tmp_path / "skills"))
    assert sorted(r["installed"]) == sorted(EXPECTED)
    for name in EXPECTED:
        assert (tmp_path / "skills" / name / "SKILL.md").exists()
    assert conn.execute(
        "SELECT 1 FROM audit WHERE action='skills_install'").fetchone()


def test_triage_skill_e2e(conn, controller):
    """验收 5: triage 技能端到端试跑——过滤通过建账,账本查得到任务行。"""
    # 技能步骤 2-3: 建账→进讨论
    tid = ops.task_new(conn, controller, "试跑: triage 接单",
                       request_id="sk-triage")["task_id"]
    ops.task_transition(conn, controller, tid, "discussing",
                        request_id="sk-triage-d")
    tasks = ops.task_list(conn)
    assert any(t["id"] == tid and t["status"] == "discussing"
               for t in tasks)  # 完成判据: 账本里查到任务行


def test_new_shell_onboarding_matches_spec():
    """验收 6: 八问检查单全文+三关自测,与规格书 6.7 条目一致。"""
    text = _read("new-shell-onboarding")
    for kw in ("档 1 钩子", "档 2 状态文件", "档 3 无头", "provider 绑定类型",
               "后端协议清单", "权限拦截", "session_end", "思考级别"):
        assert kw in text, kw
    for kw in ("模拟事件全链路", "真会话 spawn", "最小真实任务闭环"):
        assert kw in text, kw
