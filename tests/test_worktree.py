"""任务级 git worktree 原语(票 05): 建树/同项目串行/合并/冲突升级/失败保留。"""

import json
import os
from pathlib import Path

import pytest

from tianji import ops
from tianji.db import connect, now, task_dir, tx
from tianji.render import spawn
from tianji.events import ingest_event


def _init(conn, controller, worker_name, project_dir):
    """快速到派单前置态。"""
    tid = ops.task_new(conn, controller, "worktree任务",
                       project_dir=project_dir, request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, worker_name,
                             request_id="r-issue")["dispatch_id"]
    return tid, did


def _to_active(conn, worker_name, did):
    """spawn + session_start + pre_tool_use 到 active。"""
    s = spawn(conn, worker_name, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ingest_event(conn, env, {"session_id": "s1", "event_type": "session_start"})
    ingest_event(conn, env, {"session_id": "s1", "event_type": "pre_tool_use"})
    return s


def _report(conn, did):
    p = Path(task_dir(did)) / "report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("worktree 报告", encoding="utf-8")
    return str(p)


def _audit_actions(conn):
    return [r["action"] for r in conn.execute("SELECT action FROM audit").fetchall()]


# ====================================================================
# 验收标准 1: git 项目派单→自动建独立分支工作树,主分支不受影响
# ====================================================================

def test_git_project_creates_worktree(conn, controller, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    # 初始化 git 仓库并提交一个文件
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)

    ops.instance_register(conn, "w1", "codex", "step-router-v1")
    tid, did = _init(conn, controller, "w1", str(project))
    d = ops.dispatch_get(conn, did)
    payload = json.loads(d["payload"])
    assert d["worktree_path"] != ""
    assert Path(d["worktree_path"]).is_dir()
    assert payload["worktree_path"] == d["worktree_path"]
    # 主分支 HEAD 文件仍在原处
    assert (project / "README.md").is_file()
    # worktree 内可改代码
    wt_file = Path(d["worktree_path"]) / "new.py"
    wt_file.write_text("print(1)", encoding="utf-8")
    assert wt_file.is_file()


# ====================================================================
# 验收标准 2: git 项目 worktree 并行放行;非 git 项目同项目串行
# (16.2: git 项目用任务级 worktree 隔离,多实施者同项目可并行;
#  非 git 项目无法 worktree,同项目退回串行)
# ====================================================================

def test_git_project_parallel_dispatch_allowed(conn, controller, tmp_path):
    """git 项目第二个活跃派单放行(worktree 隔离并行),两派单各建独立 worktree。"""
    project = tmp_path / "proj"
    project.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)

    # 两个不同 worker
    ops.instance_register(conn, "w1", "codex", "step-router-v1")
    ops.instance_register(conn, "w2", "codex", "step-router-v1")

    tid1 = ops.task_new(conn, controller, "任务1", project_dir=str(project), request_id="r-new1")["task_id"]
    tid2 = ops.task_new(conn, controller, "任务2", project_dir=str(project), request_id="r-new2")["task_id"]
    for tid in (tid1, tid2):
        for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
            ops.task_transition(conn, controller, tid, s, request_id=f"r-{tid}-{s}")

    did1 = ops.dispatch_issue(conn, controller, tid1, "w1", request_id="r-issue1")["dispatch_id"]
    did2 = ops.dispatch_issue(conn, controller, tid2, "w2", request_id="r-issue2")["dispatch_id"]
    # git 项目两派单并存(并行),各自有 worktree
    d1 = ops.dispatch_get(conn, did1)
    d2 = ops.dispatch_get(conn, did2)
    assert d1["worktree_path"] != ""
    assert d2["worktree_path"] != ""
    assert d1["worktree_path"] != d2["worktree_path"]
    # 两派单均为活跃态
    assert d1["status"] == "issued"
    assert d2["status"] == "issued"


def test_non_git_project_serial(conn, controller, tmp_path):
    """非 git 项目同项目串行,且不建 worktree。"""
    project = tmp_path / "proj"
    project.mkdir()
    ops.instance_register(conn, "w1", "codex", "step-router-v1")
    ops.instance_register(conn, "w2", "codex", "step-router-v1")
    tid1, did1 = _init(conn, controller, "w1", str(project))
    d1 = ops.dispatch_get(conn, did1)
    assert d1["worktree_path"] == ""
    # 非 git 项目同项目串行
    tid2 = ops.task_new(conn, controller, "任务2", project_dir=str(project), request_id="r-new2")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid2, s, request_id=f"r2-{s}")
    with pytest.raises(ValueError, match="同项目串行拒绝"):
        ops.dispatch_issue(conn, controller, tid2, "w2", request_id="r-issue2")


# ====================================================================
# 验收标准 3: 审核全程在树内(spawn cwd=工作树根)
# ====================================================================

def test_spawn_cwd_is_worktree(conn, controller, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)

    ops.instance_register(conn, "w1", "codex", "step-router-v1")
    tid, did = _init(conn, controller, "w1", str(project))
    s = _to_active(conn, "w1", did)
    wt = s["worktree_path"]
    assert wt != ""
    assert Path(wt).is_dir()
    # 任务书仍在任务目录,不混入 worktree
    taskbook = Path(s["taskbook"])
    assert taskbook.is_file()
    assert "worktree" not in str(taskbook.parent.name)
    # 审核派单 spawn_cwd 也落在实施者 worktree 内(票 05/16.3)
    rp = _report(conn, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ops.dispatch_settle(conn, env, did, rp, "ok")
    did_review = ops.dispatch_issue(conn, controller, tid, "总控", role="reviewer",
                                    axis="spec", request_id="r-review")["dispatch_id"]
    sr = spawn(conn, "总控", did_review)
    review_wt = sr["worktree_path"]
    assert review_wt == wt, f"审核派单 worktree_path 应为实施者 worktree: {review_wt} vs {wt}"
    assert Path(review_wt).is_dir()
    # 审核 spawn 的 cwd 必须落在实施者 worktree 根,而非审核者任务目录(票 05/16.3)
    assert f"cd /d {wt}" in sr["cmd"], (
        f"审核 spawn_cwd 应为实施者 worktree 根: {sr['cmd']}")
    assert f"cd /d {Path(sr['env']['TIANJI_TASK_PATH']).parent}" not in sr["cmd"], (
        "审核 spawn_cwd 不得落在审核者任务目录")


def test_mechanical_verify_cwd_is_worktree(conn, controller, tmp_path):
    """机械验收命令在实施者 worktree 根执行(票 05/16.3),不在任务目录。"""
    project = tmp_path / "proj"
    project.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)

    ops.instance_register(conn, "w1", "codex", "step-router-v1")
    tid, did = _init(conn, controller, "w1", str(project))
    s = _to_active(conn, "w1", did)
    wt = s["worktree_path"]
    # 树内放 marker: 验收命令只在 cwd=worktree 根时可见
    marker = Path(wt) / "marker.txt"
    marker.write_text("marker", encoding="utf-8")
    assert Path(task_dir(did)) / "marker.txt" != marker, "marker 不应落在任务目录"
    # 实施者结算 → reviewing
    rp = _report(conn, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ops.dispatch_settle(conn, env, did, rp, "ok")
    # 验收命令依赖 cwd 下的 marker.txt(worktree 有,任务目录无)
    conn.execute("UPDATE tasks SET verify_cmd=? WHERE id=?",
                 ("python -c \"import os,sys; sys.exit(0 if os.path.isfile('marker.txt') else 1)\"",
                  tid))
    r = ops.mechanical_verify(conn, tid)
    assert r.get("ok") is True, f"验收命令应在 worktree 内执行: {r}"
    aud = conn.execute(
        "SELECT detail FROM audit WHERE action='mechanical_verify'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    assert aud is not None and '"ok": true' in aud["detail"]


# ====================================================================
# 验收标准 4: 最终确认机械合并回基础分支→合并成功删树
# ====================================================================

def test_final_confirm_merges_and_removes_worktree(conn, controller, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)

    ops.instance_register(conn, "w1", "codex", "step-router-v1")
    tid = ops.task_new(conn, controller, "任务", project_dir=str(project), request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, "w1", request_id="r-issue")["dispatch_id"]
    s = _to_active(conn, "w1", did)
    wt = s["worktree_path"]
    # 在 worktree 内实施改动并提交
    wt_file = Path(wt) / "impl.txt"
    wt_file.write_text("wt change", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "impl change"], cwd=wt, check=True, capture_output=True)
    # 实施者干完活 settle
    rp = _report(conn, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ops.dispatch_settle(conn, env, did, rp, "ok")
    # 双轴审核通过
    did_spec = ops.dispatch_issue(conn, controller, tid, "总控", role="reviewer",
                                  axis="spec", request_id="r-spec")["dispatch_id"]
    sr_spec = spawn(conn, "总控", did_spec)
    renv_spec = {**os.environ,
                 "TIANJI_WORKER_ID": sr_spec["env"]["TIANJI_WORKER_ID"],
                 "TIANJI_SECRET": sr_spec["env"]["TIANJI_SECRET"],
                 "TIANJI_DISPATCH_ID": str(did_spec)}
    ingest_event(conn, renv_spec, {"session_id": "s-spec", "event_type": "session_start"})
    rp_spec = str(Path(sr_spec["env"]["TIANJI_TASK_PATH"]).parent / "review_spec.md")
    Path(rp_spec).write_text("spec 通过", encoding="utf-8")
    ops.dispatch_settle(conn, renv_spec, did_spec, rp_spec, "pass", reason="spec 通过")

    did_quality = ops.dispatch_issue(conn, controller, tid, "总控", role="reviewer",
                                     axis="quality", request_id="r-quality")["dispatch_id"]
    sr_quality = spawn(conn, "总控", did_quality)
    renv_quality = {**os.environ,
                    "TIANJI_WORKER_ID": sr_quality["env"]["TIANJI_WORKER_ID"],
                    "TIANJI_SECRET": sr_quality["env"]["TIANJI_SECRET"],
                    "TIANJI_DISPATCH_ID": str(did_quality)}
    ingest_event(conn, renv_quality, {"session_id": "s-quality", "event_type": "session_start"})
    rp_quality = str(Path(sr_quality["env"]["TIANJI_TASK_PATH"]).parent / "review_quality.md")
    Path(rp_quality).write_text("quality 通过", encoding="utf-8")
    ops.dispatch_settle(conn, renv_quality, did_quality, rp_quality, "pass", reason="quality 通过")

    ops.architect_confirm(conn, controller, tid, reason="ok", request_id="r-ac")
    ops.task_transition(conn, controller, tid, "awaiting_final_confirm",
                        request_id="r-afc")
    # final_confirm→archived 触发 worktree 合并
    ops.task_transition(conn, controller, tid, "archived",
                        request_id="r-arch")
    assert not Path(wt).exists(), "合并成功 worktree 应被删除"
    # 断言基础分支收到改动
    d_done = ops.dispatch_get(conn, did)
    payload_done = json.loads(d_done["payload"])
    base_branch = payload_done["worktree_base"]
    result = subprocess.run(
        ["git", "show", f"{base_branch}:impl.txt"],
        cwd=project, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "wt change"
    # 断言审计行
    actions = _audit_actions(conn)
    assert "worktree_merge_ok" in actions


# ====================================================================
# 验收标准 5: 合并冲突→升级总控,机器不自动覆盖
# ====================================================================

def test_merge_conflict_escalates(conn, controller, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)

    ops.instance_register(conn, "w1", "codex", "step-router-v1")
    tid = ops.task_new(conn, controller, "任务", project_dir=str(project), request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    did = ops.dispatch_issue(conn, controller, tid, "w1", request_id="r-issue")["dispatch_id"]
    s = _to_active(conn, "w1", did)
    wt = s["worktree_path"]
    # 制造冲突: 修改主分支同一文件
    (project / "README.md").write_text("conflict", encoding="utf-8")
    subprocess.run(["git", "commit", "-a", "-m", "main change"], cwd=project, check=True, capture_output=True)
    # worktree 也修改同一文件并提交
    (Path(wt) / "README.md").write_text("wt change", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "wt change"], cwd=wt, check=True, capture_output=True)

    rp = _report(conn, did)
    env = {**os.environ,
           "TIANJI_WORKER_ID": s["env"]["TIANJI_WORKER_ID"],
           "TIANJI_SECRET": s["env"]["TIANJI_SECRET"],
           "TIANJI_DISPATCH_ID": str(did)}
    ops.dispatch_settle(conn, env, did, rp, "ok")
    # 双轴审核通过
    did_spec = ops.dispatch_issue(conn, controller, tid, "总控", role="reviewer",
                                  axis="spec", request_id="r-spec")["dispatch_id"]
    sr_spec = spawn(conn, "总控", did_spec)
    renv_spec = {**os.environ,
                 "TIANJI_WORKER_ID": sr_spec["env"]["TIANJI_WORKER_ID"],
                 "TIANJI_SECRET": sr_spec["env"]["TIANJI_SECRET"],
                 "TIANJI_DISPATCH_ID": str(did_spec)}
    ingest_event(conn, renv_spec, {"session_id": "s-spec", "event_type": "session_start"})
    rp_spec = str(Path(sr_spec["env"]["TIANJI_TASK_PATH"]).parent / "review_spec.md")
    Path(rp_spec).write_text("spec 通过", encoding="utf-8")
    ops.dispatch_settle(conn, renv_spec, did_spec, rp_spec, "pass", reason="spec 通过")

    did_quality = ops.dispatch_issue(conn, controller, tid, "总控", role="reviewer",
                                     axis="quality", request_id="r-quality")["dispatch_id"]
    sr_quality = spawn(conn, "总控", did_quality)
    renv_quality = {**os.environ,
                    "TIANJI_WORKER_ID": sr_quality["env"]["TIANJI_WORKER_ID"],
                    "TIANJI_SECRET": sr_quality["env"]["TIANJI_SECRET"],
                    "TIANJI_DISPATCH_ID": str(did_quality)}
    ingest_event(conn, renv_quality, {"session_id": "s-quality", "event_type": "session_start"})
    rp_quality = str(Path(sr_quality["env"]["TIANJI_TASK_PATH"]).parent / "review_quality.md")
    Path(rp_quality).write_text("quality 通过", encoding="utf-8")
    ops.dispatch_settle(conn, renv_quality, did_quality, rp_quality, "pass", reason="quality 通过")

    ops.architect_confirm(conn, controller, tid, reason="ok", request_id="r-ac")
    ops.task_transition(conn, controller, tid, "awaiting_final_confirm",
                        request_id="r-afc")
    ops.task_transition(conn, controller, tid, "archived",
                        request_id="r-arch")
    # 冲突升级,worktree 保留
    assert Path(wt).exists(), "冲突时 worktree 应保留供人查看"
    actions = _audit_actions(conn)
    assert "worktree_merge_conflict" in actions
    # 冲突必须向总控发 escalation 消息(16.3),而非仅写 audit
    esc = conn.execute(
        "SELECT type, recipient_role, payload FROM messages"
        " WHERE type='escalation' AND recipient_role='controller'"
    ).fetchall()
    assert esc, "合并冲突须向 controller 发 escalation 消息"
    esc_payload = json.loads(esc[0]["payload"])
    assert "task_id" in esc_payload and esc_payload["task_id"] == tid
    assert "worktree 合并冲突" in esc_payload["reason"]


# ====================================================================
# 验收标准 6: 任务失败/重派耗尽→工作树保留,审计可见
# ====================================================================

def test_failed_task_keeps_worktree(conn, controller, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)

    ops.instance_register(conn, "w1", "codex", "step-router-v1")
    tid, did = _init(conn, controller, "w1", str(project))
    s = _to_active(conn, "w1", did)
    wt = s["worktree_path"]
    # 模拟重派耗尽→任务 archived(先回 dispatched 再走 _reschedule 耗尽)
    ops.task_transition(conn, controller, tid, "reviewing",
                        request_id="r-review")
    # 多次驳回耗尽重派(每次重派前关闭旧派单,模拟真实 settle done)
    for i in range(4):
        conn.execute(
            "UPDATE dispatches SET status='done' WHERE task_id=?"
            " AND status IN ('issued','active')", (tid,))
        r = ops.task_transition(conn, controller, tid, "dispatched",
                                reason=f"reject-{i}", request_id=f"r-reject-{i}")
        if r.get("terminated"):
            break
        conn.execute("UPDATE tasks SET status='reviewing' WHERE id=?", (tid,))
    # 最后一次重派超限,任务 archived
    t = ops.task_get(conn, tid)
    assert t["status"] == "archived"
    assert Path(wt).exists(), "失败/终止任务 worktree 应保留"
    actions = _audit_actions(conn)
    assert "terminate_max_retries" in actions


# ====================================================================
# 验收标准 7: spawn cwd=工作树根,登记行/任务书先写后跑不变
# ====================================================================

def test_spawn_cwd_worktree_and_taskbook_separate(conn, controller, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)

    ops.instance_register(conn, "w1", "codex", "step-router-v1")
    tid, did = _init(conn, controller, "w1", str(project))
    s = _to_active(conn, "w1", did)
    wt = s["worktree_path"]
    # 任务书目录与 worktree 分离
    task_dir_path = Path(s["taskbook"]).parent
    assert task_dir_path != Path(wt)
    # 任务书目录存在
    assert task_dir_path.is_dir()
    # worktree 存在
    assert Path(wt).is_dir()
    # 登记行 task_path 指向任务书,非 worktree
    reg = conn.execute(
        "SELECT task_path FROM instance_registrations WHERE dispatch_id=?",
        (did,)).fetchone()
    assert reg["task_path"] == str(Path(s["taskbook"]))
