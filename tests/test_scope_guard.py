"""干偏护栏: 改动边界声明与验收比对(票 21 验收 1-5)。"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import task_dir
from tianji.render import spawn


def _git(cmd, cwd):
    subprocess.run(["git"] + cmd, cwd=cwd, check=True, capture_output=True)


def _git_repo(tmp_path):
    """构造 git 项目: main 分支,初始提交含 src/a.txt。"""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    (repo / "src").mkdir()
    (repo / "src" / "a.txt").write_text("a", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
         repo)
    return repo


def _to_reviewing(conn, controller, worker, project_dir="", scope=None,
                  seq="g"):
    """任务走到 reviewing(无钩子壳直结兜底),返回 (task_id, dispatch_id, env)。"""
    tid = ops.task_new(conn, controller, "任务", project_dir=project_dir,
                       request_id=f"rg-new-{seq}")["task_id"]
    ops.task_set_verify_cmd(conn, controller, tid, 'python -c "pass"',
                            request_id=f"rg-vc-{seq}")
    if scope is not None:
        ops.task_scope_set(conn, controller, tid, scope,
                           request_id=f"rg-sc-{seq}")
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s,
                            request_id=f"rg-{s}-{seq}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id=f"rg-issue-{seq}")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    rp = Path(task_dir(did)) / "report.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("报告", encoding="utf-8")
    ops.dispatch_settle(conn, env, did, str(rp), "ok")
    return tid, did, env


def _worktree(conn, did):
    return json.loads(ops.dispatch_get(conn, did)["payload"])["worktree_path"]


def _commit_file(wt, relpath, content="x"):
    p = Path(wt) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", "-A"], wt)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "wip"],
         wt)


def test_taskbook_contains_scope(conn, controller, worker):
    """验收 1: 任务书渲染含改动边界声明(快照比对)。"""
    tid = ops.task_new(conn, controller, "任务", request_id="rg-t1-new")["task_id"]
    ops.task_scope_set(conn, controller, tid, ["tianji", "tests"],
                       request_id="rg-t1-sc")
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"rg-t1-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                             request_id="rg-t1-issue")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], did)
    book = Path(s["taskbook"]).read_text(encoding="utf-8")
    assert "改动边界声明" in book
    assert "- `tianji`" in book and "- `tests`" in book


def test_git_in_scope_passes(conn, controller, worker, tmp_path):
    """验收 2 正例: git 项目界内改动正常过验收门。"""
    repo = _git_repo(tmp_path)
    tid, did, _ = _to_reviewing(conn, controller, worker,
                                project_dir=str(repo), scope=["src"], seq="in")
    _commit_file(_worktree(conn, did), "src/b.txt")
    r = ops.mechanical_verify(conn, tid)
    assert r["ok"] is True


def test_git_out_of_scope_rejected(conn, controller, worker, tmp_path):
    """验收 2 反例: 界外改动→mechanical_fail 驳回重派。"""
    repo = _git_repo(tmp_path)
    tid, did, _ = _to_reviewing(conn, controller, worker,
                                project_dir=str(repo), scope=["src"], seq="out")
    _commit_file(_worktree(conn, did), "src/b.txt")
    _commit_file(_worktree(conn, did), "other/c.txt")  # 越界
    r = ops.mechanical_verify(conn, tid)
    assert r["ok"] is False and r["rescheduled"] is True
    assert ops.task_get(conn, tid)["status"] == "dispatched"  # 驳回重派
    a = conn.execute(
        "SELECT detail FROM audit WHERE action='mechanical_verify'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    assert "other/c.txt" in a["detail"]


def test_non_git_degraded(conn, controller, worker, tmp_path):
    """验收 3: 非 git 项目比对跳过并如实标注降级;审核指令含人工核对项。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    tid, did, _ = _to_reviewing(conn, controller, worker,
                                project_dir=str(plain), scope=["src"],
                                seq="ng")
    r = ops.mechanical_verify(conn, tid)
    assert r["ok"] is True
    assert "非 git" in r.get("scope_skipped", "")
    # 审核任务书含人工核对降级项
    rdid = ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                              role="reviewer", axis="spec",
                              request_id="rg-ng-rev")["dispatch_id"]
    s = spawn(conn, worker["worker_id"], rdid)
    book = Path(s["taskbook"]).read_text(encoding="utf-8")
    assert "非 git 项目降级项" in book and "人工核对" in book


def test_scope_expansion_channel(conn, controller, worker, tmp_path):
    """验收 4: 扩界流程——总控批准改声明带审计,改后按新边界比对。"""
    repo = _git_repo(tmp_path)
    tid, did, _ = _to_reviewing(conn, controller, worker,
                                project_dir=str(repo), scope=["src"], seq="ex")
    wt = _worktree(conn, did)
    _commit_file(wt, "src/b.txt")
    _commit_file(wt, "other/c.txt")  # 当前边界外
    # 扩界前: 越界
    t = ops.task_get(conn, tid)
    wpayload = json.loads(ops.dispatch_get(conn, did)["payload"])
    assert ops._scope_check(conn, t, wpayload)["out"] == ["other/c.txt"]
    # 工人 worker_help 申请→总控批准→改声明(带审计)
    ops.task_scope_set(conn, controller, tid, ["src", "other"],
                       reason="工人 worker_help 申请扩界,总控批准",
                       request_id="rg-ex-expand")
    a = conn.execute(
        "SELECT detail FROM audit WHERE action='task_scope_set'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    assert "other" in a["detail"] and "批准" in a["detail"]
    # 改后按新边界比对: 过验收门
    r = ops.mechanical_verify(conn, tid)
    assert r["ok"] is True


def test_scope_undeclared_skipped(conn, controller, worker):
    """边界: 未声明边界→放行并如实标注(旧任务兼容)。"""
    tid, did, _ = _to_reviewing(conn, controller, worker, seq="un")
    r = ops.mechanical_verify(conn, tid)
    assert r["ok"] is True
    assert "未声明" in r.get("scope_skipped", "")
