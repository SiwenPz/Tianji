"""核心操作层: 校验三件套(转换合法性+身份令牌+载荷完整性)+ 一次操作一个单事务。

账本 CLI 是唯一写入口,本层是所有写操作的实现。状态迁移写审计行,不重复造消息。
"""

import json
import os
import subprocess
import sqlite3
from pathlib import Path

from . import auth, messages, state
from .db import now, task_dir, tx
from .state import check_dispatch_transition, check_task_transition


# ====================================================================
# worktree 原语(票 05): 调 git CLI,不引第三方库
# ====================================================================

def _git(args, cwd=None, check=True):
    """调 git CLI,返回 CompletedProcess。"""
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",  # Windows 默认 GBK 会炸 UTF-8 输出(2026-08-19 实证)
        timeout=120, check=check)


def _is_git_repo(path: str) -> bool:
    """检测路径本身是否为 git 仓库根目录。"""
    if not os.path.isdir(path):
        return False
    try:
        p = _git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)
        if p.returncode != 0 or (p.stdout or "").strip() != "true":
            return False
        p2 = _git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
        if p2.returncode != 0:
            return False
        toplevel = (p2.stdout or "").strip()
        return os.path.normpath(toplevel) == os.path.normpath(path)
    except Exception:
        return False


def _get_default_branch(path: str) -> str:
    """推断默认分支(优先 origin/HEAD,否则 HEAD 所指)。"""
    try:
        p = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=path, check=False)
        if p.returncode == 0:
            ref = (p.stdout or "").strip()
            if ref:
                return ref.split("/")[-1]
    except Exception:
        pass
    try:
        p = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path, check=False)
        if p.returncode == 0:
            return (p.stdout or "").strip() or "main"
    except Exception:
        pass
    return "main"


def _create_worktree(project_dir: str, dispatch_id: int, base_branch: str) -> str:
    """创建任务级 git worktree(独立分支,基=base_branch)。返回 worktree 绝对路径。"""
    branch = f"tianji-dispatch-{dispatch_id}"
    wt_root = os.path.join(project_dir, ".tianji", "worktrees")
    os.makedirs(wt_root, exist_ok=True)
    wt_path = os.path.join(wt_root, str(dispatch_id))
    # 分支不存在则基于 base_branch 创建
    p = _git(["rev-parse", "--verify", branch], cwd=project_dir, check=False)
    if p.returncode != 0:
        _git(["branch", branch, base_branch], cwd=project_dir)
    # 分支已存在时,不加 -b,直接关联已有分支
    _git(["worktree", "add", wt_path, branch], cwd=project_dir)
    return wt_path


def _remove_worktree(worktree_path: str):
    """移除 worktree(尽量不 force,保留人工查看机会)。"""
    if not os.path.isdir(worktree_path):
        return
    # 推断主仓库目录: worktree/.git 通常包含 "gitdir: ../../.git/modules/..."
    git_dir = os.path.join(worktree_path, ".git")
    repo_root = worktree_path
    if os.path.isfile(git_dir):
        try:
            with open(git_dir, "r", encoding="utf-8") as fh:
                first = fh.readline().strip()
            if first.startswith("gitdir: "):
                rel = first[len("gitdir: "):]
                repo_root = os.path.abspath(os.path.join(worktree_path, rel))
                # 向上找到 .git 实际目录后,再向上到仓库根
                while repo_root and repo_root != os.path.dirname(repo_root):
                    if os.path.isdir(os.path.join(repo_root, ".git")):
                        break
                    repo_root = os.path.dirname(repo_root)
        except Exception:
            repo_root = worktree_path
    try:
        _git(["worktree", "remove", worktree_path], cwd=repo_root, check=False)
    except Exception:
        pass


def _get_repo_root(worktree_path: str) -> str:
    """从 worktree 路径推断主仓库根目录。"""
    git_dir = os.path.join(worktree_path, ".git")
    repo_root = worktree_path
    if os.path.isfile(git_dir):
        try:
            with open(git_dir, "r", encoding="utf-8") as fh:
                first = fh.readline().strip()
            if first.startswith("gitdir: "):
                rel = first[len("gitdir: "):]
                repo_root = os.path.abspath(os.path.join(worktree_path, rel))
                while repo_root and repo_root != os.path.dirname(repo_root):
                    if os.path.isdir(os.path.join(repo_root, ".git")):
                        break
                    repo_root = os.path.dirname(repo_root)
        except Exception:
            repo_root = worktree_path
    return repo_root


def _merge_worktree(worktree_path: str, base_branch: str):
    """在仓库根目录把 worktree 分支合入基础分支,返回 (ok: bool, output: str)。"""
    repo_root = _get_repo_root(worktree_path)
    try:
        dispatch_id = int(os.path.basename(worktree_path))
    except ValueError:
        return False, f"cannot infer dispatch_id from worktree path: {worktree_path}"
    branch = f"tianji-dispatch-{dispatch_id}"
    try:
        # 先切到基础分支再合并:主工作区可能停在别的分支,直接 merge 会合错对象
        if not base_branch:
            return False, "base_branch is empty"
        p = _git(["checkout", base_branch], cwd=repo_root, check=False)
        if p.returncode != 0:
            return False, (p.stdout or "") + (p.stderr or "")
        p = _git(["merge", branch, "--no-edit"], cwd=repo_root, check=False)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "merge timeout"
    except Exception as e:
        return False, str(e)


def _worktree_merge_task(conn, task_id):
    """final_confirm 后机械合并该任务的所有活跃 worktree(事务外调用)。"""
    rows = conn.execute(
        "SELECT id, worktree_path, payload FROM dispatches WHERE task_id=? AND worker_role='worker'"
        " AND worktree_path != '' AND status='done'",
        (task_id,),
    ).fetchall()
    merged = []
    conflicts = []
    for r in rows:
        wt = r["worktree_path"]
        if not os.path.isdir(wt):
            continue
        payload = json.loads(r["payload"]) if r["payload"] else {}
        base_branch = payload.get("worktree_base") or _get_default_branch(wt)
        ok, out = _merge_worktree(wt, base_branch)
        if ok:
            _remove_worktree(wt)
            conn.execute(
                "UPDATE dispatches SET worktree_path='' WHERE worktree_path=?",
                (wt,))
            conn.execute(
                "INSERT INTO audit (ts, action, detail) VALUES (?,?,?)",
                (now(), "worktree_merge_ok",
                 json.dumps({"task_id": task_id, "dispatch_id": r["id"],
                             "worktree_path": wt, "base_branch": base_branch,
                             "output": out},
                            ensure_ascii=False)))
            # 删除已合并且无冲突的派单分支
            try:
                branch = f"tianji-dispatch-{r['id']}"
                repo_root = _get_repo_root(wt)
                _git(["branch", "-d", branch], cwd=repo_root, check=False)
            except Exception:
                pass
            merged.append({"worktree_path": wt, "output": out})
        else:
            conflicts.append({"worktree_path": wt, "output": out})
    if conflicts:
        conn.execute(
            "INSERT INTO audit (ts, action, detail) VALUES (?,?,?)",
            (now(), "worktree_merge_conflict",
             json.dumps({"task_id": task_id, "conflicts": conflicts}, ensure_ascii=False)))
        messages.send(conn, "escalation", "allocator",
                      {"task_id": task_id,
                       "reason": f"worktree 合并冲突: {len(conflicts)} 个 worktree 冲突",
                       "conflicts": conflicts},
                      "controller")
    return {"merged": merged, "conflicts": conflicts}


def worktree_merge(conn, task_id):
    """CLI 入口: 机械合并任务 worktree 回基础分支。"""
    with tx(conn) as c:
        def _do():
            return _worktree_merge_task(c, task_id)
        return _do()

# 实现期参数默认值(存 configs,总控可改,带审计)
DEFAULTS = {
    "controller_worker_id": "",
    "controller_secret_hash": "",
    "final_confirm_gate": "on",    # 成果确认闸门默认开(20.4)
    "max_retries": "3",            # 重派上限,共 4 次尝试(12.1)
    "expect_min_default": "30",    # 默认预期分钟(9.3 中档,实现期参数)
    "t1_seconds": "120",           # 静态双阶梯冷启动默认(7.3)
    "t2_seconds": "600",
    "quality_axis_checklist": json.dumps([
        {"dimension": "测试真伪", "checks": ["测试真实运行过(红→绿证据留存)", "断言有效", "不是装饰"]},
        {"dimension": "改动最小性", "checks": ["只改任务相关", "无顺手重构", "非 git 项目人工核对改动边界"]},
        {"dimension": "边界与错误处理", "checks": ["空输入/异常路径/资源释放"]},
        {"dimension": "死代码与重复", "checks": ["无死代码", "无重复实现"]},
        {"dimension": "机密与安全", "checks": ["密钥/敏感信息不入库", "无危险操作痕迹"]},
        {"dimension": "可维护性", "checks": ["命名/结构/注释符合任务书与项目惯例"]},
    ], ensure_ascii=False),
    "allocator_review_enabled": "0",  # 可选总控评估(9.2③): 用户同意才做,默认关
    "allocator_review_bonus": "10",   # 总控评估满分加成(参与软排序,相对 0-100 评估分折算)
    "review_template": json.dumps({
        "spec": "逐条核对任务书验收标准;审核报告须逐条附行为证据(输出对比/数据点核对),空泛'已验证'不收。",
        "quality": "按 6 维清单逐项核查;挑 1-2 个已知事实数据点与产物显示值交叉核对;行为级返修须附行为证据。"
    }, ensure_ascii=False),
}


def ensure_defaults(conn: sqlite3.Connection):
    for k, v in DEFAULTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO configs (key, value, updated_at) VALUES (?,?,?)",
            (k, v, now()),
        )


def _validate_instance_combo(conn, shell: str, key_name: str, model: str) -> tuple:
    """实例组合合法性机械校验(13.4 三条全过才允许)。

    空 key_name 跳过校验(向后兼容,演示数据不含 key)。
    返回 (ok: bool, reason: str)。
    """
    if not key_name:
        return True, ""
    # 1. 壳条目存在性
    shell_row = conn.execute(
        "SELECT value FROM configs WHERE key=?", (f"shell:{shell}",)
    ).fetchone()
    if shell_row is None:
        return False, f"壳条目 shell:{shell} 不存在(configs)"
    shell_cfg = json.loads(shell_row["value"])
    # 2. Key 条目存在性
    key_row = conn.execute(
        "SELECT value FROM configs WHERE key=?", (f"key:{key_name}",)
    ).fetchone()
    if key_row is None:
        return False, f"Key 条目 key:{key_name} 不存在(configs)"
    key_cfg = json.loads(key_row["value"])
    # ① 壳支持该 key 的协议类型
    shell_protocols = shell_cfg.get("protocols", [])
    key_protocol = key_cfg.get("protocol", "")
    if key_protocol and key_protocol not in shell_protocols:
        return False, (
            f"协议不兼容: 壳 {shell} 支持 {shell_protocols},"
            f" key {key_name} 需要 {key_protocol}")
    # ② 模型在该 key 清单内
    models = key_cfg.get("models", [])
    if models:
        found = any(m.get("id") == model for m in models)
        if not found:
            return False, (
                f"模型不在清单: {model} 不在 key:{key_name} 允许列表"
                f" {[m.get('id') for m in models]}")
    # ③ CodingPlan 类 key 不跨壳
    if key_cfg.get("coding_plan"):
        expected_shell = key_cfg.get("key_ref", "")
        if expected_shell and expected_shell != f"shell:{shell}":
            return False, (
                f"CodingPlan 跨壳: key:{key_name} 绑定 {expected_shell},"
                f" 不能用于壳 {shell}")
        # 同一 CodingPlan key 已被其他壳实例占用(允许同壳复用,换绑复活场景)
        existing = conn.execute(
            "SELECT shell FROM instances WHERE key_name=? AND is_active=1",
            (key_name,)).fetchone()
        if existing and existing["shell"] != shell:
            return False, (
                f"CodingPlan 跨壳: key:{key_name} 已被壳"
                f" {existing['shell']} 占用,不能用于壳 {shell}")
    return True, ""


def audit(conn: sqlite3.Connection, action: str, detail: dict):
    conn.execute(
        "INSERT INTO audit (ts, action, detail) VALUES (?,?,?)",
        (now(), action, json.dumps(detail, ensure_ascii=False)),
    )


def _with_idem(conn, request_id, operation, fn):
    """不可逆转换必带 request_id;重放返回原回执(3.3)。须在事务内调用。"""
    if not request_id:
        raise ValueError(f"{operation} 是不可逆转换,必须带 request_id(幂等回执 3.3)")
    return messages.idempotent(conn, request_id, operation, fn)


# ---------------------------------------------------------------- 任务域

def task_new(conn, ident, title, description="", priority=0, source="user",
             project_dir="", request_id=None):
    """new 创建者机械限定: 仅总控身份可建(10.1)。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("task new 仅总控身份可建(10.1,防绕过流程)")
    with tx(conn) as c:
        def _do():
            cur = c.execute(
                "INSERT INTO tasks (title, description, status, priority, source,"
                " project_dir, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (title, description, "new", priority, source, project_dir, now(), now()),
            )
            tid = cur.lastrowid
            audit(c, "task_new", {"task_id": tid, "by": ident["worker_id"]})
            return {"task_id": tid, "status": "new", "title": title}
        return _with_idem(c, request_id, "task_new", _do)


def task_get(conn, task_id) -> dict:
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        raise KeyError(f"任务 {task_id} 不存在")
    return dict(t)


def task_list(conn, status=None) -> list:
    if status:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status=? ORDER BY priority DESC, created_at",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY priority DESC, created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def _config(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM configs WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def _issue_locked(conn, task_id, worker_id, role, expect_min,
                  reason="", axis="", overrides=None):
    """派单创建(事务内): 写派单行+dispatch 消息。

    secret 由 spawn 时生成注入(11.4 launcher 一次性注入),派单 dcap_hash 此刻留空。
    """
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        raise KeyError(f"任务 {task_id} 不存在")
    if t["status"] not in ("dispatched", "reviewing"):
        raise ValueError(f"任务状态 {t['status']} 不可派单(须 dispatched/reviewing)")
    # 审核者派单: 最多 2 个活跃审核派单(各一轴),与任务状态无关
    if role == "reviewer":
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM dispatches WHERE task_id=? AND worker_role='reviewer'"
            " AND status IN ('issued','active')",
            (task_id,),
        ).fetchone()["n"]
        if existing >= 2:
            raise ValueError(f"任务 {task_id} 最多 2 个审核派单")
        # 两轴不同实例+不同模型硬校验
        inst = conn.execute(
            "SELECT * FROM instances WHERE name=? AND is_active=1", (worker_id,)
        ).fetchone()
        if inst is None:
            raise ValueError(f"实例 {worker_id} 未注册或不活跃")
        existing_reviewers = conn.execute(
            "SELECT d.worker_id, i.model FROM dispatches d"
            " JOIN instances i ON d.worker_id=i.name"
            " WHERE d.task_id=? AND d.worker_role='reviewer'"
            " AND d.status IN ('issued','active')",
            (task_id,),
        ).fetchall()
        for er in existing_reviewers:
            if er["worker_id"] == worker_id:
                raise ValueError(
                    f"审核者 {worker_id} 已有活跃审核派单(同一实例不可兼两轴)")
            if er["model"] == inst["model"]:
                raise ValueError(
                    f"模型 {inst['model']} 已被审核者 {er['worker_id']} 占用"
                    f"(双轴须不同实例+不同模型)")
        if not axis:
            raise ValueError("审核派单须指定 axis(spec|quality)")
    else:
        # 实施者派单: 任务唯一活跃派单
        row = conn.execute(
            "SELECT id FROM dispatches WHERE task_id=? AND status IN ('issued','active')",
            (task_id,),
        ).fetchone()
        if row is not None:
            raise ValueError(f"任务 {task_id} 已有活跃派单 {row['id']},不可重复派单")
    busy = conn.execute(
        "SELECT id FROM dispatches WHERE worker_id=? AND status IN ('issued','active')",
        (worker_id,),
    ).fetchone()
    if busy is not None:
        raise ValueError(f"实施者 {worker_id} 已有活跃派单 {busy['id']}(每实施者最多一个,10.3)")
    cur = conn.execute(
        "INSERT INTO dispatches (task_id, worker_id, worker_role, axis, status, dcap_hash,"
        " expect_min, task_dir, payload, worktree_path, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, worker_id, role, axis, "issued", "",
         expect_min, "", "{}", "", now(), now()),
    )
    did = cur.lastrowid
    conn.execute("UPDATE dispatches SET task_dir=? WHERE id=?",
                 (str(task_dir(did)), did))
    # 同项目并行/串行机械约束(16.2): git 项目用任务级 worktree 隔离,
    # 多实施者同项目可并行(互不干扰);非 git 项目无法 worktree,同项目退回串行。
    project_dir = t["project_dir"] if t else ""
    worktree_path = ""
    worktree_base = ""
    if role == "worker" and project_dir:
        if _is_git_repo(project_dir):
            # git 项目: 自动建 worktree(每派单独立分支),放行并行
            worktree_base = _get_default_branch(project_dir)
            worktree_path = _create_worktree(project_dir, did, worktree_base)
        else:
            # 非 git 项目: 同项目最多一个活跃实施者派单(串行)
            existing = conn.execute(
                "SELECT d.id FROM dispatches d JOIN tasks t2 ON d.task_id=t2.id"
                " WHERE t2.project_dir=? AND d.worker_role='worker'"
                " AND d.status IN ('issued','active') AND d.id != ?",
                (project_dir, did),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"项目 {project_dir} 已有活跃实施者派单 {existing['id']},同项目串行拒绝")
    elif role == "reviewer":
        # 审核派单复用被审核实施者的 worktree(票 05/16.3: 审核全程在树内)
        worker_dispatch = conn.execute(
            "SELECT payload FROM dispatches WHERE task_id=? AND worker_role='worker'"
            " AND status='done' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if worker_dispatch and worker_dispatch["payload"]:
            worker_payload = json.loads(worker_dispatch["payload"])
            worktree_path = worker_payload.get("worktree_path", "")
            worktree_base = worker_payload.get("worktree_base", "")
    payload = {
        "task_id": task_id, "expect_min": expect_min,
        "task_dir": str(task_dir(did)), "reason": reason,
        "axis": axis, "worktree_path": worktree_path,
        "worktree_base": worktree_base,
    }
    # 票 26: 显示模式/思考级别单点覆盖(不改实例默认,15.8/13.3)
    for k in ("display_mode", "thinking_level"):
        if overrides and overrides.get(k):
            payload[k] = overrides[k]
    # 分配不自动调级(13.3/14.5): 难活只在派单建议里提示
    if role == "worker" and _estimate_complexity(conn, task_id) == "hard":
        payload["thinking_hint"] = ("难活: 建议高档思考级别/换高档实例"
                                    "(13.3 分配不自动调级)")
    conn.execute("UPDATE dispatches SET payload=?, worktree_path=? WHERE id=?",
                 (json.dumps(payload, ensure_ascii=False), worktree_path, did))
    messages.send(
        conn, "dispatch", "allocator",
        {"dispatch_id": did, "task_id": task_id, "worker_id": worker_id,
         "expect_min": expect_min, "task_dir": str(task_dir(did)),
         "worktree_path": worktree_path, "axis": axis},
        "reviewer" if role == "reviewer" else "worker",
    )
    audit(conn, "dispatch_issue", {"dispatch_id": did, "task_id": task_id,
                                   "worker_id": worker_id, "role": role, "axis": axis,
                                   "worktree_path": worktree_path})
    return {"dispatch_id": did, "task_dir": str(task_dir(did)),
            "expect_min": expect_min, "worktree_path": worktree_path}


def dispatch_issue(conn, ident, task_id, worker_id, role="worker",
                   expect_min=None, request_id=None, axis="", reason="",
                   display_mode=None, thinking_level=None):
    """分配器派单(原型期由总控侧操作执行)。校验三件套+幂等+四元组合校验。

    reason=派单缘由(驳回重派时带驳回原因,4.3);2026-08-18 修复: 此前误把
    generate_secret() 位置参数传进 reason,手动派单载荷带乱码原因。
    票 26: display_mode/thinking_level=对单点临时覆盖(不改实例默认,15.8/13.3)。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("派单仅总控身份可执行")
    if display_mode is not None and display_mode not in ("前台", "后台"):
        raise ValueError(f"display_mode 只支持 前台|后台(15.8): {display_mode}")
    if thinking_level is not None and thinking_level not in ("低", "中", "高"):
        raise ValueError(f"thinking_level 只支持 低|中|高(13.3): {thinking_level}")
    inst = conn.execute(
        "SELECT * FROM instances WHERE name=? AND is_active=1", (worker_id,)
    ).fetchone()
    if inst is None:
        raise ValueError(f"实例 {worker_id} 未注册或不活跃(先 instance register)")
    # 四元组合合法性校验(13.4)
    ok, combo_reason = _validate_instance_combo(
        conn, inst["shell"], inst["key_name"], inst["model"])
    if not ok:
        raise ValueError(f"实例组合不合法({worker_id}): {combo_reason}")
    if expect_min is None:
        expect_min = _get_expect_min(conn, task_id, worker_id)
    with tx(conn) as c:
        def _do():
            return _issue_locked(c, task_id, worker_id, role, expect_min,
                                 reason=reason, axis=axis,
                                 overrides={"display_mode": display_mode,
                                            "thinking_level": thinking_level})
        return _with_idem(c, request_id, "dispatch_issue", _do)


def dispatch_get(conn, dispatch_id) -> dict:
    d = conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if d is None:
        raise KeyError(f"派单 {dispatch_id} 不存在")
    return dict(d)


def dispatch_revive(conn, ident, dispatch_id, reason="", request_id=None):
    """stale→active 复活(总控,带审计): 工人被误标 stale(停滞后已恢复干活)
    时给结算留合法回头路;误杀比漏报贵(0.3-4)。仅在工人确活着时由总控调用。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("派单复活仅总控身份可执行")
    with tx(conn) as c:
        def _do():
            d = c.execute("SELECT * FROM dispatches WHERE id=?",
                          (dispatch_id,)).fetchone()
            if d is None:
                raise KeyError(f"派单 {dispatch_id} 不存在")
            if not check_dispatch_transition(d["status"], "active"):
                raise ValueError(
                    f"派单 {dispatch_id} 状态 {d['status']} 不可复活(仅 stale 可回 active)")
            c.execute("UPDATE dispatches SET status='active', updated_at=? WHERE id=?",
                      (now(), dispatch_id))
            audit(c, "dispatch_revive",
                  {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                   "worker_id": d["worker_id"], "reason": reason,
                   "by": ident["worker_id"]})
            return {"dispatch_id": dispatch_id, "from": d["status"], "to": "active"}
        return _with_idem(c, request_id, "dispatch_revive", _do)


def dispatch_nudge(conn, ident, dispatch_id, reason="", request_id=None):
    """7.5 续推通道: 工人答完一轮停下后总控续推(身份校验+审计)。

    只翻译续跑命令+审计+实例档案,不改任务/派单状态机;
    续推不是失败: 不消耗重派计数、不扣表现分(与 _reschedule 完全无关);
    同一派单续推次数进实例档案(nudge_count)供参考,不设机械上限(0.3-4)。
    不支持续跑的壳(模板无 resume 原语)fail-loud 并记录实例档案,退回人工。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("nudge 续推仅总控身份可执行")
    with tx(conn) as c:
        def _do():
            d = c.execute("SELECT * FROM dispatches WHERE id=?",
                          (dispatch_id,)).fetchone()
            if d is None:
                raise KeyError(f"派单 {dispatch_id} 不存在")
            if d["status"] not in ("issued", "active"):
                raise ValueError(
                    f"派单 {dispatch_id} 状态 {d['status']} 不可续推"
                    "(仅 issued/active 可续推,7.5)")
            shell = "claude"
            inst = c.execute("SELECT shell FROM instances WHERE name=?",
                             (d["worker_id"],)).fetchone()
            if inst:
                shell = inst["shell"]
            # 续会话原语需要 session_id(模板占位符;无登记行则为空串)
            session_id = ""
            reg = c.execute(
                "SELECT session_id FROM instance_registrations"
                " WHERE instance_name=? AND status IN ('spawned','active')"
                " ORDER BY id DESC LIMIT 1", (d["worker_id"],)).fetchone()
            if reg and reg["session_id"]:
                session_id = reg["session_id"]
            task_path = str(Path(d["task_dir"]) / "task.md")
            from .adapters.template import resume_command
            r = resume_command(shell, task_path=task_path, session_id=session_id)
            # 实例档案: 续推计数+1(供参考不设上限)
            c.execute(
                "UPDATE ability_profiles SET nudge_count=nudge_count+1,"
                " last_nudge_at=? WHERE instance_name=?",
                (now(), d["worker_id"]))
            if not r["supported"]:
                update_profile_notes(
                    c, d["worker_id"],
                    f"nudge 失败(退回人工): {r['reason']}"
                    f" dispatch={dispatch_id}")
                audit(c, "dispatch_nudge_unsupported",
                      {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                       "worker_id": d["worker_id"], "shell": shell,
                       "reason": r["reason"], "by": ident["worker_id"]})
                return {"dispatch_id": dispatch_id, "supported": False,
                        "shell": shell, "reason": r["reason"],
                        "task_id": d["task_id"]}
            audit(c, "dispatch_nudge",
                  {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                   "worker_id": d["worker_id"], "shell": shell,
                   "cmd": r["cmd"], "prompt": r["prompt"],
                   "session_id": session_id, "reason": reason,
                   "by": ident["worker_id"]})
            return {"dispatch_id": dispatch_id, "supported": True,
                    "shell": shell, "cmd": r["cmd"], "prompt": r["prompt"],
                    "task_id": d["task_id"], "session_id": session_id}
        return _with_idem(c, request_id, "dispatch_nudge", _do)


def _activate_dispatch(conn, worker_id):
    """开工证据: 该 worker 最新 issued 派单→active;任务 dispatched→executing(5.1 联动)。

    由 ingest-event 在 pre_tool_use 时调用(事务内)。
    """
    d = conn.execute(
        "SELECT * FROM dispatches WHERE worker_id=? AND status='issued' "
        "ORDER BY id LIMIT 1", (worker_id,)
    ).fetchone()
    if d is None:
        return None
    if not check_dispatch_transition(d["status"], "active"):
        return None
    conn.execute("UPDATE dispatches SET status='active', updated_at=? WHERE id=?",
                 (now(), d["id"]))
    t = conn.execute("SELECT status FROM tasks WHERE id=?", (d["task_id"],)).fetchone()
    if t and t["status"] == "dispatched" and check_task_transition(
            "dispatched", "executing"):
        conn.execute("UPDATE tasks SET status='executing', updated_at=? WHERE id=?",
                     (now(), d["task_id"]))
    audit(conn, "dispatch_active", {"dispatch_id": d["id"], "task_id": d["task_id"],
                                    "worker_id": worker_id})
    return {"dispatch_id": d["id"], "task_id": d["task_id"]}


def _last_worker_by_role(conn, task_id, role="worker"):
    """取同任务最新指定 role 的派单工人(修复 _reschedule 误派审核者)。"""
    row = conn.execute(
        "SELECT worker_id FROM dispatches WHERE task_id=? AND worker_role=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, role)).fetchone()
    return row["worker_id"] if row else None


def _reschedule(conn, task_id, worker_id, reason):
    """驳回=重派(4.3): 计数+1,超限终止;否则回 dispatched 并自动发新派单。

    审核驳回(mechanical_fail/review_reject)、进程退出无结算重派、用户驳回均计入(12.1)。
    驳回后另一轴在途审核派单自然作废(先 cancel 再重派,防"已有活跃派单"
    门把驳回结算卡死——2026-08-18 司马懿实锤: 质量轴 reject 时 spec 轴还在
    issued,_issue_locked 唯一活跃派单门拒绝,结算失败)。
    """
    # 表现分联动: 进程退出无结算重派 -10(9.4)
    if worker_id and any(k in reason for k in ("process_exit", "stale", "进程退出", "无结算")):
        latest_dispatch = conn.execute(
            "SELECT expect_min FROM dispatches WHERE worker_id=? ORDER BY id DESC LIMIT 1",
            (worker_id,)).fetchone()
        em = latest_dispatch["expect_min"] if latest_dispatch else 30
        try:
            update_score(conn, worker_id, "process_dead", em)
        except KeyError:
            pass
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    max_retries = int(_config(conn, "max_retries") or 3)
    new_count = t["retry_count"] + 1
    if new_count > max_retries:
        conn.execute(
            "UPDATE tasks SET status='archived', retry_count=?, updated_at=? WHERE id=?",
            (new_count, now(), task_id))
        audit(conn, "terminate_max_retries",
              {"task_id": task_id, "retry_count": new_count, "reason": reason})
        messages.send(conn, "escalation", "allocator",
                      {"task_id": task_id,
                       "reason": f"重做超限终止: 第 {new_count} 次重派超过上限 {max_retries}({reason})"},
                      "controller")
        return {"task_id": task_id, "status": "archived", "terminated": True}
    conn.execute(
        "UPDATE tasks SET status='dispatched', retry_count=?, updated_at=? WHERE id=?",
        (new_count, now(), task_id))
    audit(conn, "reschedule", {"task_id": task_id, "retry_count": new_count,
                               "reason": reason})
    # 修复: 重派对象改为最新 worker_role='worker' 的工人,避免误派审核者
    target_worker = worker_id or _last_worker_by_role(conn, task_id, "worker")
    if not target_worker:
        target_worker = t.get("assignee") or "worker"
    # 驳回生效前先作废旧任务在途派单(另一轴审核等,已失去意义):
    # 不清掉会被 _issue_locked 的"唯一活跃派单"门卡死(见 docstring)
    stale_rows = conn.execute(
        "SELECT id FROM dispatches WHERE task_id=? AND status IN ('issued','active')",
        (task_id,)).fetchall()
    for sr in stale_rows:
        conn.execute(
            "UPDATE dispatches SET status='cancelled', updated_at=?, dcap_hash=''"
            " WHERE id=?", (now(), sr["id"]))
        conn.execute(
            "UPDATE instance_registrations SET status='closed', closed_at=?,"
            " abnormal=1 WHERE dispatch_id=? AND status IN ('spawned','active')",
            (now(), sr["id"]))
        audit(conn, "force_cancel_dispatch",
              {"dispatch_id": sr["id"], "task_id": task_id,
               "reason": f"reschedule_obsolete: {reason}"})
    _issue_locked(conn, task_id, target_worker, "worker",
                  _get_expect_min(conn, task_id, target_worker), reason=reason)
    return {"task_id": task_id, "status": "dispatched", "retry_count": new_count}


def dispatch_settle(conn, env, dispatch_id, report_path, outcome,
                    reason=""):
    """worker_done 单事务结算(5.4): 校验顺序 身份→最新派单→幂等→stale。

    实施者派单: outcome=ok,通过即 派单 done+任务 reviewing+审计+回执一次提交。
    审核派单: outcome=pass/reject;pass 等架构师确认,reject 驳回重派。
    幂等键=dispatch_id,重放返回原回执。
    """
    ident = auth.require_identity(env)
    with tx(conn) as c:
        def _do():
            d = c.execute("SELECT * FROM dispatches WHERE id=?",
                          (dispatch_id,)).fetchone()
            # ① 派单存在;② 身份(与派单 dcap_hash 恒定时间比较)
            if d is None:
                return {"rejected": "unknown", "detail": "派单不存在"}
            if not auth.check_dispatch_secret(
                    c, dispatch_id, ident["worker_id"], ident["secret"]):
                return {"rejected": "auth_fail", "detail": "身份令牌不匹配(冒名/伪造)"}
            # ② 派单存在且属该 worker 最新派单
            latest = c.execute(
                "SELECT MAX(id) AS m FROM dispatches WHERE worker_id=?",
                (ident["worker_id"],)).fetchone()["m"]
            if d["id"] != latest:
                return {"rejected": "unknown", "detail": "非该 worker 最新派单"}
            # ③ 幂等: 已结算返原回执(幂等键=dispatch_id)
            rc = c.execute("SELECT result FROM receipts WHERE request_id=?",
                           (f"settle:{dispatch_id}",)).fetchone()
            if rc is not None:
                return {"replay": True, **json.loads(rc["result"])}
            # ④ stale
            if d["status"] in ("stale", "requeue", "escalate", "done"):
                return {"rejected": "stale"}
            if d["status"] not in ("issued", "active"):
                return {"rejected": "stale"}
            # 载荷完整性: report_path 必填且已落盘(机械验证,实施者不自证)
            rp = os.path.abspath(report_path)
            if not os.path.isfile(rp):
                return {"rejected": "unknown", "detail": f"report_path 不存在: {rp}"}
            if d["worker_role"] == "reviewer":
                if outcome not in ("pass", "reject"):
                    return {"rejected": "unknown", "detail": "审核结论须 pass/reject"}
                payload = {
                    "verdict": outcome, "reason": reason,
                    "report_path": rp, "axis": d["axis"],
                }
                c.execute("UPDATE dispatches SET status='done', updated_at=?, "
                          "payload=? WHERE id=?",
                          (now(), json.dumps(payload, ensure_ascii=False), dispatch_id))
                messages.send(c, "review_verdict", ident["worker_id"],
                              {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                               "verdict": outcome, "reason": reason,
                               "report_path": rp, "axis": d["axis"]}, "controller")
                audit(c, "review_settle",
                      {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                       "verdict": outcome, "reason": reason, "axis": d["axis"]})
                if outcome == "reject":
                    update_score(c, d["worker_id"], "review_reject", d["expect_min"])
                    _reschedule(c, d["task_id"], None,
                                f"review_reject: {reason}")
                result = {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                          "verdict": outcome, "status": "done"}
            else:
                if outcome != "ok":
                    return {"rejected": "unknown", "detail": "实施者结算 outcome 须为 ok"}
                c.execute("UPDATE dispatches SET status='done', updated_at=? WHERE id=?",
                          (now(), dispatch_id))
                t = c.execute("SELECT status FROM tasks WHERE id=?",
                              (d["task_id"],)).fetchone()
                # 无钩子壳兜底(2026-08-20,票27/票15 两踩): dsh/cline 等壳事件不进账本,
                # 任务停在 dispatched 无开工证据;worker_done 结算即唯一完工信号,
                # dispatched→reviewing 自动落账+审计,不再要总控 force 手动补位。
                if t and check_task_transition(t["status"], "reviewing"):
                    c.execute("UPDATE tasks SET status='reviewing', updated_at=? WHERE id=?",
                              (now(), d["task_id"]))
                    if t["status"] == "dispatched":
                        audit(c, "settle_hookless_fallback",
                              {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                               "note": "无开工证据(dispatched 直结),无钩子壳自动兜底"})
                # 表现分更新(9.4): 按实际时长判档
                actual_minutes = max(0, (now() - d["created_at"]) // 60)
                if actual_minutes <= d["expect_min"]:
                    ev = "on_time"
                elif actual_minutes <= d["expect_min"] * 2:
                    ev = "overtime"
                else:
                    ev = "progress_exceed"
                update_score(c, d["worker_id"], ev, d["expect_min"], actual_minutes)
                messages.send(c, "worker_done", ident["worker_id"],
                              {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                               "report_path": rp, "outcome": outcome}, "controller")
                audit(c, "worker_done",
                      {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                       "report_path": rp,
                       "cleanup_note": "结算后按任务书回报纪律清空上下文(5.3)"})
                # 动态校准触发(7.3/9.3,票 07): 每单结算重算滑动统计;
                # 校准失败不阻塞结算(同一事务内,异常吞掉)
                try:
                    from .calibration import recalibrate
                    recalibrate(c, force=True)
                except Exception:
                    pass
                result = {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                          "status": "done", "task_status": "reviewing"}
            return result
        result = _do()
        # 幂等只覆盖成功结算(5.4): 失败(四拒绝码)不缓存,允许重试。
        # 2026-08 踩坑: 首次 report_path 未就绪被拒也进 receipts,之后重试
        # 永远 replay 旧失败,审核/实施死循环。
        if result.get("rejected") or result.get("replay"):
            return result
        c.execute(
            "INSERT INTO receipts (request_id, operation, result) VALUES (?,?,?)",
            (f"settle:{dispatch_id}", "dispatch_settle",
             json.dumps(result, ensure_ascii=False)))
        return result


def _dual_verdicts(conn, task_id):
    """汇聚同任务双轴 verdict,返回 (spec, quality, conclusion)。

    conclusion: consistent_pass / consistent_reject / disagree / incomplete
    """
    rows = conn.execute(
        "SELECT payload FROM dispatches WHERE task_id=? AND worker_role='reviewer'"
        " AND status='done' ORDER BY id",
        (task_id,)
    ).fetchall()
    verdicts = {"spec": None, "quality": None}
    for r in rows:
        p = json.loads(r["payload"])
        axis = p.get("axis") or ""
        if axis in ("spec", "quality"):
            verdicts[axis] = p.get("verdict")
    spec_v = verdicts.get("spec")
    quality_v = verdicts.get("quality")
    if spec_v and quality_v:
        if spec_v == "pass" and quality_v == "pass":
            return spec_v, quality_v, "consistent_pass"
        if spec_v == "reject" and quality_v == "reject":
            return spec_v, quality_v, "consistent_reject"
        return spec_v, quality_v, "disagree"
    return spec_v, quality_v, "incomplete"


def task_transition(conn, ident, task_id, to_state, request_id=None,
                    reason=""):
    """九态推进(4.2/4.5): 转换表机械校验+条件钩子(闸门/审核链/重派)。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("任务状态推进仅总控身份可执行")
    with tx(conn) as c:
        def _do():
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t is None:
                raise KeyError(f"任务 {task_id} 不存在")
            if not check_task_transition(t["status"], to_state):
                raise ValueError(f"非法转换: {t['status']}→{to_state}(4.2 转换表拒绝)")
            from_state = t["status"]
            # 条件钩子
            if to_state == "executing" and from_state == "dispatched":
                raise ValueError(
                    "dispatched→executing 由开工证据(事件联动)触发,不走手动转换(5.1)")
            if to_state == "dispatched" and from_state == "reviewing":
                # 驳回=重派: 计数+新派单(上限检查在 _reschedule 内,12.1)
                return _reschedule(c, task_id, _last_worker_by_role(c, task_id, "worker"),
                                   f"reschedule({reason})")
            if to_state == "discussing" and from_state == "awaiting_final_confirm":
                # 用户驳回最终确认(4.5): 回 discussing 重新对齐,不消耗机械
                # 重派计数、不触发上限终止;单独审计留痕
                audit(c, "user_reject", {"task_id": task_id, "reason": reason,
                                         "by": ident["worker_id"]})
            if to_state == "awaiting_final_confirm":
                v = _dual_verdicts(c, task_id)
                if v[2] != "consistent_pass":
                    raise ValueError(
                        f"双轴审核未一致通过({v[2]}),不能进入 awaiting_final_confirm(8.2)")
                architect_verdict = t["architect_verdict"] if "architect_verdict" in t.keys() else ""
                if not architect_verdict or architect_verdict != "confirm":
                    raise ValueError(
                        "架构师未确认,不能进入 awaiting_final_confirm(机械拒绝,8.2)")
            if to_state == "archived" and from_state == "reviewing":
                if _config(c, "final_confirm_gate") != "off":
                    raise ValueError("成果确认闸门开启中: reviewing 不能直接归档(20.4)")
            if to_state == "archived" and from_state == "awaiting_final_confirm":
                if _config(c, "final_confirm_gate") == "on":
                    messages.send(c, "final_confirm", ident["worker_id"],
                                  {"task_id": task_id, "reason": reason}, "controller")
            if to_state == "reopened":
                c.execute("UPDATE tasks SET retry_count=0 WHERE id=?",
                          (task_id,))  # 重派计数清零(10.6)
            c.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                      (to_state, now(), task_id))
            audit(c, "task_transition", {"task_id": task_id, "from": from_state,
                                         "to": to_state, "reason": reason,
                                         "by": ident["worker_id"]})
            result = {"task_id": task_id, "from": from_state, "to": to_state}
            # final_confirm 后机械合并 worktree(事务外执行,不阻塞状态转换)
            if to_state == "archived" and from_state == "awaiting_final_confirm":
                result["worktree_merge"] = _worktree_merge_task(c, task_id)
            return result
        return _with_idem(c, request_id, "task_transition", _do)


def _last_worker(conn, task_id):
    row = conn.execute(
        "SELECT worker_id FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,)).fetchone()
    return row["worker_id"] if row else None


def _latest_verdict(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM dispatches WHERE task_id=? AND worker_role='reviewer' "
        "AND status='done' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"])


def architect_confirm(conn, ident, task_id, reason="", request_id=None):
    """架构师二次确认(8.2): 仅总控/架构师身份可确认。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("架构师确认仅总控/架构师身份可执行")
    with tx(conn) as c:
        def _do():
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t is None:
                raise KeyError(f"任务 {task_id} 不存在")
            if t["status"] != "reviewing":
                raise ValueError("架构师确认仅 reviewing 态可执行")
            c.execute("UPDATE tasks SET architect_verdict='confirm', updated_at=? WHERE id=?",
                      (now(), task_id))
            messages.send(c, "architect_confirm", ident["worker_id"],
                          {"task_id": task_id, "reason": reason}, "controller")
            audit(c, "architect_confirm",
                  {"task_id": task_id, "reason": reason, "by": ident["worker_id"]})
            return {"task_id": task_id, "architect_verdict": "confirm"}
        return _with_idem(c, request_id, "architect_confirm", _do)


def architect_review(conn, ident, task_id, reason="", request_id=None):
    """架构师深审裁决(8.2): 双轴分歧时架构师读两份审核报告定夺。

    写 architect_verdict=reject(本票仅做裁决落账,分歧后驳回由后续转换处理)。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("架构师裁决仅总控/架构师身份可执行")
    with tx(conn) as c:
        def _do():
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t is None:
                raise KeyError(f"任务 {task_id} 不存在")
            c.execute("UPDATE tasks SET architect_verdict='reject', updated_at=? WHERE id=?",
                      (now(), task_id))
            messages.send(c, "architect_verdict", ident["worker_id"],
                          {"task_id": task_id, "verdict": "reject", "reason": reason,
                           "disagreement": True}, "controller")
            audit(c, "architect_review",
                  {"task_id": task_id, "verdict": "reject", "reason": reason,
                   "by": ident["worker_id"]})
            return {"task_id": task_id, "architect_verdict": "reject"}
        return _with_idem(c, request_id, "architect_review", _do)


def task_set_verify_cmd(conn, ident, task_id, cmd, request_id=None):
    """架构师在 await_plan_confirm 前写入验收命令(8.3,实施者不报不改)。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("写入验收命令仅总控身份可执行")
    with tx(conn) as c:
        def _do():
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t is None:
                raise KeyError(f"任务 {task_id} 不存在")
            if t["status"] not in ("new", "discussing", "awaiting_plan_confirm"):
                raise ValueError(
                    f"验收命令须在计划确认前写入(当前 {t['status']},8.3)")
            c.execute("UPDATE tasks SET verify_cmd=?, updated_at=? WHERE id=?",
                      (cmd, now(), task_id))
            audit(c, "task_verify_cmd", {"task_id": task_id, "cmd": cmd,
                                         "by": ident["worker_id"]})
            return {"task_id": task_id, "verify_cmd": cmd}
        return _with_idem(c, request_id, "task_verify_cmd", _do)


def _normalize_prefix(p: str) -> str:
    """路径前缀归一化(票 21 备注: Windows 正反斜杠统一为正斜杠)。"""
    return p.replace("\\", "/").strip("/")


def task_scope_set(conn, ident, task_id, prefixes, reason="", request_id=None):
    """改动边界声明(11.2/8.3,票 21): 架构师写计划时定,再小的任务也必写。

    扩界通道(5.6): 工人 worker_help 申请扩界→总控批准→本命令改声明(带审计)
    →按新边界继续;不问直接动=越界驳回。prefixes 空清单=清除声明。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("改动边界声明仅总控(兼架构师)身份可写")
    if isinstance(prefixes, str):
        prefixes = prefixes.split(",")
    norm = [_normalize_prefix(p) for p in prefixes if p.strip()]
    with tx(conn) as c:
        def _do():
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t is None:
                raise KeyError(f"任务 {task_id} 不存在")
            old = json.loads(t["scope_guard"]) if t["scope_guard"] else []
            c.execute("UPDATE tasks SET scope_guard=?, updated_at=? WHERE id=?",
                      (json.dumps(norm, ensure_ascii=False), now(), task_id))
            audit(c, "task_scope_set", {"task_id": task_id, "old": old,
                                        "new": norm, "reason": reason,
                                        "by": ident["worker_id"]})
            return {"task_id": task_id, "scope_guard": norm}
        return _with_idem(c, request_id, "task_scope_set", _do)


def _changed_paths_git(cwd: str, base: str = "") -> list:
    """git 项目实际改动路径清单(相对根,正斜杠): 未提交(porcelain)+已提交(diff base...HEAD)。"""
    paths = set()
    p = _git(["status", "--porcelain"], cwd=cwd, check=False)
    if p.returncode == 0:
        for line in (p.stdout or "").splitlines():
            rest = line[3:] if len(line) > 3 else ""
            if " -> " in rest:  # 改名取新路径
                rest = rest.split(" -> ", 1)[1]
            rest = rest.strip().strip('"')
            if rest:
                paths.add(rest)
    if base:
        p2 = _git(["diff", "--name-only", f"{base}...HEAD"], cwd=cwd, check=False)
        if p2.returncode == 0:
            for line in (p2.stdout or "").splitlines():
                if line.strip():
                    paths.add(line.strip())
    return sorted(paths)


def _scope_check(conn, t, worker_payload) -> dict:
    """干偏护栏比对(8.3,票 21): 实际改动路径 vs 边界声明(比对路径清单,不读内容 0.3-3)。

    返回 {"ok": bool, "out": [越界路径]} 或 {"skipped": reason}:
    未声明→放行如实标注(旧任务兼容);非 git→跳过并降级为审核者人工核对(8.4 维度 2)。
    """
    raw = t["scope_guard"] or ""
    prefixes = [_normalize_prefix(p) for p in json.loads(raw)] if raw else []
    if not prefixes:
        return {"skipped": "未声明改动边界(11.2 要求必写,旧任务兼容放行)"}
    wt = (worker_payload or {}).get("worktree_path", "")
    base = (worker_payload or {}).get("worktree_base", "")
    cwd = wt if wt and os.path.isdir(wt) else (t["project_dir"] or "")
    if not cwd or not _is_git_repo(cwd):
        return {"skipped": "非 git 项目: 边界比对降级为审核者人工核对(8.4 维度 2)",
                "degraded": True}
    paths = _changed_paths_git(cwd, base)
    out = [p for p in paths
           if not any(pre == "" or p == pre or p.startswith(pre + "/")
                      for pre in prefixes)]
    return {"ok": not out, "out": out, "paths": paths, "cwd": cwd}


def task_priority(conn, ident, task_id, priority, request_id=None):
    """优先级=总控操作,带审计(10.4)。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("改优先级仅总控身份可执行(10.4)")
    with tx(conn) as c:
        def _do():
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t is None:
                raise KeyError(f"任务 {task_id} 不存在")
            c.execute("UPDATE tasks SET priority=?, updated_at=? WHERE id=?",
                      (priority, now(), task_id))
            audit(c, "task_priority", {"task_id": task_id, "priority": priority,
                                       "by": ident["worker_id"]})
            return {"task_id": task_id, "priority": priority}
        return _with_idem(c, request_id, "task_priority", _do)


def task_reopen(conn, ident, task_id, reason="", request_id=None):
    """archived→reopened(10.6): 总控创建,第二入口,重派计数清零。"""
    return task_transition(conn, ident, task_id, "reopened",
                           request_id=request_id, reason=reason)


def task_force(conn, ident, task_id, to_state, reason, request_id=None,
               new_worker=None):
    """强制干预(4.4): 总控 CLI 特权例外转换+审计;不豁免重派计数。

    强制终止(to_state=archived): 任务 archived + 在途派单 cancelled + 关工人
    登记行 + 旧 secret 作废 + 审计行，一笔事务。
    改派/接管(to_state=dispatched): 原派单 cancelled + 关登记行 + 旧 secret 作废
    + 审计行 + 任务回 dispatched(重派计数+1, 自动发新派单)。
    new_worker=改派目标工人;缺省重派给原工人(2026-08-18 补: 原实现改派只能
    派回原工人,换人无门,4.4 语义不全)。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("强制干预仅总控身份可执行(4.4)")
    from .state import FORCE_TARGETS
    with tx(conn) as c:
        def _do():
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t is None:
                raise KeyError(f"任务 {task_id} 不存在")
            if to_state not in FORCE_TARGETS:
                raise ValueError(f"强制干预目标 {to_state} 非法")
            if to_state == t["status"] and to_state != "dispatched":
                raise ValueError("强制干预目标与当前状态相同")

            # 取最新活跃派单(issued/active/stale/escalate)
            d = c.execute(
                "SELECT * FROM dispatches WHERE task_id=? "
                "AND status IN ('issued','active','stale','escalate') "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()

            if to_state == "archived":
                # 强制终止: 任务 archived + 派单 cancelled + 关登记行 + 旧 secret 作废
                c.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                          ("archived", now(), task_id))
                audit(c, "force_intervention",
                      {"task_id": task_id, "from": t["status"], "to": "archived",
                       "reason": reason, "by": ident["worker_id"]})
                if d:
                    c.execute(
                        "UPDATE dispatches SET status='cancelled', updated_at=?, dcap_hash=''"
                        " WHERE id=?",
                        (now(), d["id"]))
                    c.execute(
                        "UPDATE instance_registrations SET status='closed', closed_at=?, abnormal=1"
                        " WHERE dispatch_id=? AND status IN ('spawned','active')",
                        (now(), d["id"]))
                    audit(c, "force_cancel_dispatch",
                          {"dispatch_id": d["id"], "task_id": task_id,
                           "reason": "force_terminate"})
                messages.send(c, "escalation", "controller",
                              {"task_id": task_id,
                               "reason": f"强制终止: {t['status']}→archived({reason})"},
                              "controller")
                return {"task_id": task_id, "from": t["status"], "to": "archived"}

            if to_state == "dispatched":
                # 改派/接管: 原派单 cancelled + 关登记行 + 旧 secret 作废
                # + 任务回 dispatched(重派计数+1, 自动新派单)
                if d is None:
                    raise ValueError("任务无活跃派单,无法改派")
                c.execute(
                    "UPDATE dispatches SET status='cancelled', updated_at=?, dcap_hash=''"
                    " WHERE id=?",
                    (now(), d["id"]))
                c.execute(
                    "UPDATE instance_registrations SET status='closed', closed_at=?, abnormal=1"
                    " WHERE dispatch_id=? AND status IN ('spawned','active')",
                    (now(), d["id"]))
                audit(c, "force_cancel_dispatch",
                      {"dispatch_id": d["id"], "task_id": task_id,
                       "reason": "force_reassign"})
                # 复用重派: 计数+1 + 新派单(重派计数不豁免)
                if new_worker:
                    inst = c.execute(
                        "SELECT name FROM instances WHERE name=? AND is_active=1",
                        (new_worker,)).fetchone()
                    if inst is None:
                        raise ValueError(f"改派目标 {new_worker} 未注册或不活跃")
                _reschedule(c, task_id, new_worker or d["worker_id"],
                            f"force_reassign: {reason}")
                messages.send(c, "escalation", "controller",
                              {"task_id": task_id,
                               "reason": f"强制改派: {t['status']}→dispatched({reason})"},
                              "controller")
                return {"task_id": task_id, "from": t["status"], "to": "dispatched"}

            # 其他强制干预(保留原简单逻辑)
            c.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                      (to_state, now(), task_id))
            audit(c, "force_intervention",
                  {"task_id": task_id, "from": t["status"], "to": to_state,
                   "reason": reason, "by": ident["worker_id"]})
            messages.send(c, "escalation", "controller",
                          {"task_id": task_id,
                           "reason": f"强制干预: {t['status']}→{to_state}({reason})"},
                          "controller")
            return {"task_id": task_id, "from": t["status"], "to": to_state}
        return _with_idem(c, request_id, "task_force", _do)


def mechanical_verify(conn, task_id, timeout=120):
    """机械验收门简化版(8.3): 声称触发,验收命令+reportPath 机械检查,实施者不自证。

    验收命令由架构师在计划确认前写入(verify_cmd);命令在任务目录执行;
    失败→mechanical_fail 驳回重派(不占双轴)。重复调用返回 already。
    去重粒度=(任务,最新已结算派单): 返修后的新结算会再触发验收
    (2026-08 踩坑: 按 task_id 去重导致返修结算永远命中"已验过")。
    """
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        raise KeyError(f"任务 {task_id} 不存在")
    if t["status"] != "reviewing":
        raise ValueError(f"验收只在 reviewing 触发(声称触发 8.3),当前 {t['status']}")
    if not t["verify_cmd"]:
        raise ValueError("验收命令未配置(架构师在计划确认前写入任务书,8.3)")
    d = conn.execute(
        "SELECT * FROM dispatches WHERE task_id=? AND status='done' "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    did = d["id"] if d else -1
    done = conn.execute(
        "SELECT id FROM audit WHERE action='mechanical_verify' AND detail LIKE ?",
        (f'%"task_id": {task_id}, "dispatch_id": {did}%',)).fetchone()
    if done is not None:
        return {"task_id": task_id, "already": True}
    cwd = d["task_dir"] if d else str(task_dir(0))
    # 审核验收命令在实施者 worktree 内执行(票 05/16.3)
    worker_d = conn.execute(
        "SELECT payload FROM dispatches WHERE task_id=? AND worker_role='worker'"
        " AND status='done' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if worker_d and worker_d["payload"]:
        wp = json.loads(worker_d["payload"]).get("worktree_path", "")
        if wp and os.path.isdir(wp):
            cwd = wp
    # 命令在事务外执行(不阻塞结算,结果写账本)
    # PYTHONUTF8=1: 子进程 stdout 强制 UTF-8 编码,否则 Windows 默认 GBK 控制台编码
    # 遇到 UTF-8 专属字符(如 ✓)时 print 直接抛 UnicodeEncodeError 退出非零
    # (2026-08-19 实证,验收命令 `python -c "print('中文§✓')"` 被 GBK 炸死)。
    env_utf8 = {**os.environ, "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run(
            t["verify_cmd"], shell=True, cwd=cwd, timeout=timeout,
            capture_output=True, text=True, env=env_utf8,
            encoding="utf-8", errors="replace")  # 同上: 防 GBK 炸 UTF-8 输出
        ok = proc.returncode == 0
        output = (proc.stdout or "")[-2000:]
    except subprocess.TimeoutExpired:
        ok, output = False, "验收命令超时"
    fail_reason = "" if ok else ("验收命令超时" if output == "验收命令超时"
                                 else "验收命令返回非零")
    # 干偏护栏(8.3,票 21): 验收命令过了仍须比对改动边界,越界=mechanical_fail
    wpayload = (json.loads(worker_d["payload"])
                if worker_d and worker_d["payload"] else None)
    scope = _scope_check(conn, t, wpayload)
    if ok and scope.get("ok") is False:
        ok = False
        fail_reason = "改动越界(8.3): " + ", ".join(scope["out"])
        output = (output + " | " + fail_reason)[-2000:]
    with tx(conn) as c:
        audit(c, "mechanical_verify",
              {"task_id": task_id, "dispatch_id": did, "ok": ok,
               "output": output, "cmd": t["verify_cmd"], "scope": scope})
        if not ok:
            _reschedule(c, task_id, _last_worker_by_role(c, task_id, "worker"),
                        f"mechanical_fail: {fail_reason}")
            return {"task_id": task_id, "ok": False, "rescheduled": True}
        result = {"task_id": task_id, "ok": True}
        if scope.get("skipped"):
            result["scope_skipped"] = scope["skipped"]
        return result


# ---------------------------------------------------------------- 实例域

def instance_register(conn, name, shell, model, isolated_dir="", launch_cmd="",
                      controller=False, skills="[]", context_window=0,
                      key_name="", permission_granularity="", profile_notes="",
                      display_mode="前台", thinking_level="", ident=None):
    """注册实例(会话域,四元组: shell/key_name/model/隔离目录)。生成 secret 明文仅此一次;
    controller 时配置总控身份。

    ident 为总控身份 dict(controller 越权保护);None 表示无身份(仅 bootstrap 首
    次注册允许)。

    换绑(3.2/10.1): 已下线(is_active=0)的同名实例可重新注册=复活更新,新 secret 生效;
    活跃同名实例仍拒绝(防误覆盖)。

    票 26(15.8/13.3): display_mode=前台|后台(默认前台,可见性优先);
    thinking_level=低|中|高(壳无关抽象,空=不注入用壳默认)。
    """
    if display_mode not in ("前台", "后台"):
        raise ValueError(f"display_mode 只支持 前台|后台(15.8): {display_mode}")
    if thinking_level not in ("", "低", "中", "高"):
        raise ValueError(f"thinking_level 只支持 低|中|高(13.3): {thinking_level}")
    with tx(conn) as c:
        row = c.execute("SELECT name, is_active FROM instances WHERE name=?", (name,)).fetchone()
        if row is not None and row["is_active"]:
            raise ValueError(f"实例 {name} 已注册且活跃")
        # 组合合法性校验(13.4): 同一 key 挂不同模型=不同实例组合
        ok, reason = _validate_instance_combo(c, shell, key_name, model)
        if not ok:
            raise ValueError(f"实例组合不合法: {reason}")
        if controller:
            cfg = auth._get_configs(c)
            cid = cfg.get(auth.CFG_CONTROLLER_ID, "")
            if cid:
                # 总控已存在: 须校验身份(防越权覆盖总控绑定)
                if ident is None or not auth.check_controller(c, ident):
                    raise PermissionError(
                        "instance register --controller 仅总控身份可执行"
                        "(防越权覆盖总控绑定)")
        if row is not None:
            # 换绑复活: 更新四元组+能力画像,is_active 置 1
            c.execute(
                "UPDATE instances SET shell=?, model=?, key_name=?, isolated_dir=?,"
                " launch_cmd=?, display_mode=?, thinking_level=?, is_active=1"
                " WHERE name=?",
                (shell, model, key_name, isolated_dir, launch_cmd,
                 display_mode, thinking_level, name))
            c.execute(
                "UPDATE ability_profiles SET shell=?, model=?, key_name=?,"
                " isolated_dir=?, skills=?, permission_granularity=?,"
                " context_window=?, notes=?, model_source_score=0, key_body_score=0"
                " WHERE instance_name=?",
                (shell, model, key_name, isolated_dir, skills,
                 permission_granularity, context_window, profile_notes, name))
        else:
            c.execute(
                "INSERT INTO instances (name, shell, model, key_name, isolated_dir,"
                " launch_cmd, display_mode, thinking_level, is_active, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,1,?)",
                (name, shell, model, key_name, isolated_dir, launch_cmd,
                 display_mode, thinking_level, now()))
            c.execute(
                "INSERT INTO ability_profiles (instance_name, shell, model, key_name,"
                " isolated_dir, skills, permission_granularity, context_window,"
                " score, model_source_score, key_body_score, notes, score_history)"
                " VALUES (?,?,?,?,?,?,?,?,60,0,0,?, '[]')",
                (name, shell, model, key_name, isolated_dir, skills,
                 permission_granularity, context_window, profile_notes))
        secret = auth.generate_secret()
        if controller:
            c.execute(
                "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (auth.CFG_CONTROLLER_ID, name, now()))
            c.execute(
                "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (auth.CFG_CONTROLLER_HASH, auth.secret_hash(secret), now()))
            audit(c, "controller_set", {"name": name})
        audit(c, "instance_register",
              {"name": name, "shell": shell, "model": model,
               "key_name": key_name})
        messages.send(c, "instance_register", "cli",
                      {"name": name, "shell": shell, "model": model,
                       "key_name": key_name})
        return {"name": name, "secret": secret,
                "note": "secret 明文仅本次输出,启动器注入 env 用"}


def instance_unbind(conn, name, request_id=None):
    """换绑/下线(10.1 旧 secret 自然作废): is_active=0+instance_unbind 消息。"""
    with tx(conn) as c:
        def _do():
            row = c.execute("SELECT name FROM instances WHERE name=?", (name,)).fetchone()
            if row is None:
                raise KeyError(f"实例 {name} 未注册")
            c.execute("UPDATE instances SET is_active=0 WHERE name=?", (name,))
            audit(c, "instance_unbind", {"name": name})
            messages.send(c, "instance_unbind", "cli", {"name": name})
            return {"name": name, "is_active": 0}
        return _with_idem(c, request_id, "instance_unbind", _do)


def instance_delete(conn, ident, name, request_id=None):
    """物理删除实例注册+能力画像(13.6 增删条目,总控专属+审计)。

    与 unbind 区别: unbind 下线保留记录可复活;delete 彻底移除注册与画像,
    同 name 后可重新 register 为全新实例。仅总控身份可执行;在途派单的
    工人禁止删除(防悬空派单)。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("instance delete 仅总控身份可执行")
    with tx(conn) as c:
        def _do():
            row = c.execute("SELECT name, is_active FROM instances WHERE name=?", (name,)).fetchone()
            if row is None:
                raise KeyError(f"实例 {name} 未注册")
            busy = c.execute(
                "SELECT id FROM dispatches WHERE worker_id=? AND status IN ('issued','active')",
                (name,)).fetchone()
            if busy is not None:
                raise ValueError(f"实例 {name} 有在途派单 #{busy['id']},不能删除(先取消)")
            c.execute("DELETE FROM instances WHERE name=?", (name,))
            c.execute("DELETE FROM ability_profiles WHERE instance_name=?", (name,))
            audit(c, "instance_delete", {"name": name, "by": ident["worker_id"]})
            messages.send(c, "instance_unbind", "cli", {"name": name, "deleted": True})
            return {"name": name, "deleted": True}
        return _with_idem(c, request_id, "instance_delete", _do)


def instance_update(conn, ident, name, model=None, key_name=None, launch_cmd=None,
                    isolated_dir=None, context_window=None, skills=None,
                    permission_granularity=None, display_mode=None,
                    thinking_level=None, request_id=None):
    """实例配置就地修改(13.6 增删改,票 28): 总控专属+审计。

    运维口径: 换 key 本体=覆盖 key_ref 文件、换 url=config key set,都不动实例;
    本命令只改实例四元/画像字段(model/key_name/launch_cmd/isolated_dir 等),
    不重建实例。改后复用 13.4 组合合法性机械校验。在途派单允许改——改动只
    影响下一次 spawn,在途派单的 env/dcap 已注入不受影响。
    票 26: 显示模式(前台|后台)/默认思考级别(低|中|高)同走本命令。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("instance update 仅总控身份可执行")
    if display_mode is not None and display_mode not in ("前台", "后台"):
        raise ValueError(f"display_mode 只支持 前台|后台(15.8): {display_mode}")
    if thinking_level is not None and thinking_level not in ("", "低", "中", "高"):
        raise ValueError(f"thinking_level 只支持 低|中|高(13.3): {thinking_level}")
    with tx(conn) as c:
        def _do():
            row = c.execute(
                "SELECT * FROM instances WHERE name=? AND is_active=1", (name,)).fetchone()
            if row is None:
                raise KeyError(f"实例 {name} 未注册或已下线")
            quad = {"model": model, "key_name": key_name,
                    "launch_cmd": launch_cmd, "isolated_dir": isolated_dir,
                    "display_mode": display_mode, "thinking_level": thinking_level}
            changed = {f: v for f, v in quad.items()
                       if v is not None and v != row[f]}
            prof = {"context_window": context_window, "skills": skills,
                    "permission_granularity": permission_granularity}
            pchanged = {f: v for f, v in prof.items() if v is not None}
            if not changed and not pchanged:
                raise ValueError("无变更字段(均为空或与现值相同)")
            # 组合合法性机械校验(13.4): 用改后的 model/key_name 复核
            ok, reason = _validate_instance_combo(
                c, row["shell"],
                changed.get("key_name", row["key_name"]),
                changed.get("model", row["model"]))
            if not ok:
                raise ValueError(f"实例组合不合法: {reason}")
            old = {f: row[f] for f in changed}
            if changed:
                sets = ", ".join(f"{f}=?" for f in changed)
                c.execute(f"UPDATE instances SET {sets} WHERE name=?",
                          (*changed.values(), name))
                # 能力画像四元同步(9.1 单一真源)
                psync = {f: changed[f] for f in ("model", "key_name", "isolated_dir")
                         if f in changed}
                if psync:
                    psets = ", ".join(f"{f}=?" for f in psync)
                    c.execute(f"UPDATE ability_profiles SET {psets}"
                              " WHERE instance_name=?", (*psync.values(), name))
            if pchanged:
                psets = ", ".join(f"{f}=?" for f in pchanged)
                c.execute(f"UPDATE ability_profiles SET {psets}"
                          " WHERE instance_name=?", (*pchanged.values(), name))
            audit(c, "instance_update",
                  {"name": name, "old": old,
                   "new": {**changed, **pchanged}, "by": ident["worker_id"]})
            return {"name": name, "old": old,
                    "updated": {**changed, **pchanged}}
        return _with_idem(c, request_id, "instance_update", _do)


def controller_recover(conn, name):
    """总控 secret 丢失恢复通道(规格书遗漏,D.3 分类③回写;本机操作者=信任根 0.3-5)。

    旧 secret 作废;新 secret 明文只返回一次(由 CLI 打印),SHA-256 摘要存账本;
    写 controller_recovery 审计行。不走幂等回执——回执会缓存含明文 secret 的回执
    JSON,机密不能进账本(8.4 维度 5);重复调用=再次轮换,天然安全。
    """
    with tx(conn) as c:
        row = c.execute(
            "SELECT name FROM instances WHERE name=? AND is_active=1", (name,)
        ).fetchone()
        if row is None:
            raise KeyError(f"实例 {name} 不存在或未激活")
        secret = auth.generate_secret()
        for k, v in ((auth.CFG_CONTROLLER_ID, name),
                     (auth.CFG_CONTROLLER_HASH, auth.secret_hash(secret))):
            c.execute(
                "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (k, v, now()))
        audit(c, "controller_recovery",
              {"name": name, "note": "总控 secret 丢失恢复: 旧 secret 作废"})
        return {"controller": name, "secret": secret,
                "note": "secret 明文仅此一次,立即保存;旧 secret 已作废"}


def instance_list(conn) -> list:
    rows = conn.execute("SELECT * FROM instances ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def instance_set_pid(conn, ident, instance_name: str, pid: int,
                     request_id=None):
    """手动回填 pid(外部拉起通道,7.4② 补位): 总控操作,带审计。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("instance set-pid 仅总控身份可执行")
    with tx(conn) as c:
        def _do():
            row = c.execute(
                "SELECT id FROM instance_registrations"
                " WHERE instance_name=? AND status IN ('spawned','active')"
                " ORDER BY id DESC LIMIT 1",
                (instance_name,)).fetchone()
            if row is None:
                raise ValueError(f"实例 {instance_name} 无活跃登记行(先 spawn 或 register)")
            c.execute(
                "UPDATE instance_registrations SET pid=? WHERE id=?",
                (pid, row["id"]))
            audit(c, "instance_set_pid",
                  {"instance_name": instance_name, "registration_id": row["id"],
                   "pid": pid})
            return {"instance_name": instance_name,
                    "registration_id": row["id"], "pid": pid}
        return _with_idem(c, request_id, "instance_set_pid", _do)


# ---------------------------------------------------------------- 配置与审计

def config_set(conn, ident, key, value, request_id=None):
    """configs 读写=总控+审计(零配置文件原则,2.3)。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("config set 仅总控身份可执行")
    with tx(conn) as c:
        def _do():
            # 特定 key 的 JSON 合法性预校验(fail-loud)
            if key == "quality_axis_checklist":
                try:
                    json.loads(value)
                except json.JSONDecodeError as e:
                    raise ValueError(f"quality_axis_checklist 须为合法 JSON: {e}")
            old = c.execute("SELECT value FROM configs WHERE key=?", (key,)).fetchone()
            old_value = old["value"] if old else None
            c.execute(
                "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now()))
            detail = {"key": key, "value": value, "old_value": old_value,
                      "by": ident["worker_id"]}
            audit(c, "config_set", detail)
            return {"key": key, "value": value, "old_value": old_value}
        return _with_idem(c, request_id, "config_set", _do)


def config_get(conn, key=None):
    if key:
        row = conn.execute("SELECT * FROM configs WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None
    rows = conn.execute("SELECT * FROM configs ORDER BY key").fetchall()
    return [dict(r) for r in rows]


def config_delete(conn, ident, key, request_id=None):
    """configs 条目删除=总控+审计;有活跃实例引用的壳/key 条目拒绝删除(13.4)。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("config delete 仅总控身份可执行")
    with tx(conn) as c:
        def _do():
            row = c.execute("SELECT key FROM configs WHERE key=?",
                            (key,)).fetchone()
            if row is None:
                raise KeyError(f"配置项 {key} 不存在")
            if key.startswith("shell:"):
                ref = c.execute(
                    "SELECT name FROM instances WHERE shell=? AND is_active=1",
                    (key[len("shell:"):],)).fetchone()
            elif key.startswith("key:"):
                ref = c.execute(
                    "SELECT name FROM instances WHERE key_name=? AND is_active=1",
                    (key[len("key:"):],)).fetchone()
            else:
                ref = None
            if ref is not None:
                raise ValueError(
                    f"配置项 {key} 被活跃实例 {ref['name']} 引用,拒绝删除")
            c.execute("DELETE FROM configs WHERE key=?", (key,))
            audit(c, "config_delete", {"key": key, "by": ident["worker_id"]})
            return {"key": key, "deleted": True}
        return _with_idem(c, request_id, "config_delete", _do)


def export_messages(conn, after=0, limit=1000):
    rows = conn.execute(
        "SELECT * FROM messages WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def update_profile_notes(conn, instance_name: str, notes_append: str):
    """实例档案追加录入(9.1): 在 notes 中追加文本,时间戳前缀。"""
    row = conn.execute(
        "SELECT notes FROM ability_profiles WHERE instance_name=?",
        (instance_name,)).fetchone()
    if row is None:
        raise KeyError(f"实例 {instance_name} 画像不存在")
    existing = row["notes"] or ""
    ts = now()
    if existing:
        new_notes = f"{existing}\n[{ts}] {notes_append}"
    else:
        new_notes = f"[{ts}] {notes_append}"
    conn.execute(
        "UPDATE ability_profiles SET notes=? WHERE instance_name=?",
        (new_notes, instance_name))
    return new_notes


def _estimate_complexity(conn, task_id: int) -> str:
    """按 priority 简单启发估计复杂度(9.3 静态部分,从简)。"""
    t = conn.execute("SELECT priority FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        raise KeyError(f"任务 {task_id} 不存在")
    p = t["priority"]
    if p >= 3:
        return "hard"
    if p >= 1:
        return "normal"
    return "simple"


def _get_expect_min(conn, task_id: int, worker_id: str | None = None) -> int:
    """从 configs 读取三档 expect_min,按任务复杂度返回默认值。

    票 07(9.3): 实例有实测校准(expect_min_calib:<instance>,p75+EMA+锚定)
    时按档位覆盖默认值。
    """
    c = _estimate_complexity(conn, task_id)
    if worker_id:
        row = conn.execute(
            "SELECT value FROM configs WHERE key=?",
            (f"expect_min_calib:{worker_id}",)).fetchone()
        if row:
            tiers = json.loads(row["value"])
            if c in tiers:
                return int(tiers[c])
    simple = int(_config(conn, "expect_min_simple") or 15)
    normal = int(_config(conn, "expect_min_normal") or 30)
    hard = int(_config(conn, "expect_min_hard") or 60)
    return {"simple": simple, "normal": normal, "hard": hard}[c]


def _task_expected_size(conn, task_id: int) -> int:
    """粗估任务预期规模(上下文窗口过滤用,从简)。"""
    c = _estimate_complexity(conn, task_id)
    return {"simple": 1000, "normal": 4000, "hard": 10000}.get(c, 4000)


def _task_review_scores(conn, task_id: int) -> dict:
    """读该任务最近一条总控评估结果(9.2③): {instance_name: 0-100 评估分}。"""
    rows = conn.execute(
        "SELECT payload FROM messages WHERE type='alloc_review_result'"
        " ORDER BY seq DESC").fetchall()
    for r in rows:
        payload = json.loads(r["payload"])
        if payload.get("task_id") == task_id:
            return payload.get("scores") or {}
    return {}


def alloc_review_submit(conn, ident, task_id: int, scores: dict,
                        request_id: str = None) -> dict:
    """总控把评估结果写账本(9.2③): alloc_review_result 消息,参与后续分配排序。

    scores: {instance_name: 0-100 评估分}(模型判断/联网公开评测产出,总控会话内评估)。
    """
    t = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        raise KeyError(f"任务 {task_id} 不存在")
    for name in scores:
        if conn.execute("SELECT 1 FROM instances WHERE name=?",
                        (name,)).fetchone() is None:
            raise ValueError(f"评估对象 {name} 未注册实例")

    def _do():
        with tx(conn) as c:
            msg = messages.send(
                c, "alloc_review_result", ident["worker_id"],
                {"task_id": task_id, "scores": scores}, "allocator")
            audit(c, "alloc_review_submit",
                  {"task_id": task_id, "scores": scores,
                   "by": ident["worker_id"]})
            return msg
    return _with_idem(conn, request_id, "alloc_review_submit", _do)


def allocator_pick(conn, task_id: int) -> str | None:
    """分配器选人(9.2): 硬过滤→软排序(可选总控评估参与)→升级总控。

    返回最佳实施者 instance_name,无合格候选返回 None 并写 escalation。
    可选总控评估(9.2③): 开关 allocator_review_enabled=1 时,把候选名单+
    任务特征写账本消息(alloc_review),若有已回写的 alloc_review_result
    则评估分参与软排序(按 allocator_review_bonus 折算加成)。
    """
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        raise KeyError(f"任务 {task_id} 不存在")
    expected_size = _task_expected_size(conn, task_id)
    # 候选: 活跃实例,非 busy,权限足够,窗口够大
    candidates = conn.execute(
        "SELECT * FROM instances WHERE is_active=1"
    ).fetchall()
    qualified = []
    for inst in candidates:
        name = inst["name"]
        # 忙者不可选
        busy = conn.execute(
            "SELECT id FROM dispatches WHERE worker_id=? AND status IN ('issued','active')",
            (name,)).fetchone()
        if busy is not None:
            continue
        # 上下文窗口过滤
        profile = conn.execute(
            "SELECT * FROM ability_profiles WHERE instance_name=?",
            (name,)).fetchone()
        if profile is None:
            continue
        if (profile["context_window"] or 0) < expected_size:
            continue
        # 权限粒度过滤
        perm = (profile["permission_granularity"] or "").lower()
        if perm == "readonly" and t["priority"] > 0:
            continue
        qualified.append({
            "name": name,
            "score": profile["score"] or 0,
            "skills": json.loads(profile["skills"] or "[]"),
        })
    if not qualified:
        messages.send(
            conn, "escalation", "allocator",
            {"task_id": task_id,
             "reason": "全候选硬过滤/软排序无合格实施者,请总控裁决"},
            "controller")
        return None
    # 可选总控评估(9.2③,用户同意才做): 候选名单+任务特征写账本→总控评估
    review_scores = {}
    if _config(conn, "allocator_review_enabled") == "1":
        # 同任务只发一次请求消息(幂等)
        dup = conn.execute(
            "SELECT 1 FROM messages WHERE type='alloc_review'"
            " AND json_extract(payload, '$.task_id')=? LIMIT 1",
            (task_id,)).fetchone()
        if dup is None:
            messages.send(
                conn, "alloc_review", "allocator",
                {"task_id": task_id,
                 "task": {"title": t["title"],
                          "description": t["description"],
                          "priority": t["priority"],
                          "expected_size": expected_size},
                 "candidates": qualified},
                "controller")
        review_scores = _task_review_scores(conn, task_id)
    # 软排序: 表现分降序 + 擅长面命中加分 + 总控评估加成
    review_bonus_max = float(_config(conn, "allocator_review_bonus") or 10)
    title_desc = (t["title"] or "") + " " + (t["description"] or "")
    for c in qualified:
        bonus = 0
        for sk in c["skills"]:
            if sk and sk.lower() in title_desc.lower():
                bonus += 5
                break
        rv = review_scores.get(c["name"])
        if rv is not None:
            bonus += round(review_bonus_max * float(rv) / 100.0, 2)
        c["effective_score"] = c["score"] + bonus
    qualified.sort(key=lambda x: x["effective_score"], reverse=True)
    return qualified[0]["name"]


def task_queue_next(conn) -> dict | None:
    """队列派生视图(10.3): 取单=(priority desc, created_at asc) 从无活跃派单任务里取。"""
    row = conn.execute(
        "SELECT * FROM tasks WHERE status='dispatched'"
        " AND id NOT IN (SELECT task_id FROM dispatches"
        " WHERE status IN ('issued','active'))"
        " ORDER BY priority DESC, created_at ASC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def update_score(conn, instance_name: str, event: str,
                 expect_min: int, actual_minutes: int | None = None) -> float:
    """表现分更新(9.4): 近 10 单加权移动平均,从简实现。

    event: on_time / overtime / progress_exceed / review_reject / process_dead
    """
    profile = conn.execute(
        "SELECT score, score_history FROM ability_profiles WHERE instance_name=?",
        (instance_name,)).fetchone()
    if profile is None:
        raise KeyError(f"实例 {instance_name} 画像不存在")
    current_score = profile["score"] or 60
    history = json.loads(profile["score_history"] or "[]")
    # 判档加分(仅按时/超时需要实际时长)
    if event == "on_time":
        delta = 10
    elif event == "overtime":
        delta = 5
    elif event == "progress_exceed":
        delta = -8
    elif event == "review_reject":
        delta = -15
    elif event == "process_dead":
        delta = -10
    else:
        delta = 0
    history.append({"event": event, "delta": delta, "ts": now()})
    # 只保留最近 10 条
    if len(history) > 10:
        history = history[-10:]
    # 加权移动平均: 最新权重 10, 次新 9 ... 最旧 1
    total_weight = sum(range(1, len(history) + 1))
    weighted_sum = sum((i + 1) * h["delta"] for i, h in enumerate(history))
    # 以 60 为基准 + 加权增量
    new_score = _clamp(60 + (weighted_sum / total_weight) * 10)
    conn.execute(
        "UPDATE ability_profiles SET score=?, score_history=? WHERE instance_name=?",
        (new_score, json.dumps(history, ensure_ascii=False), instance_name))
    return new_score


def post_hoc_stats(conn, instance_name: str | None = None) -> dict:
    """实测后验统计(9.1 后验): 简单 SQL 聚合,供画像修正。"""
    stats = {
        "success_rate": 0.0,
        "review_reject_rate": 0.0,
        "avg_first_tool_delay": 0.0,
        "task_duration_dist": {},
    }
    base_worker = " AND worker_id=?" if instance_name else ""
    params = [instance_name] if instance_name else []
    # 实施者派单统计
    total_worker = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches WHERE worker_role='worker'"
        + base_worker,
        params).fetchone()["n"]
    done_worker = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches WHERE worker_role='worker'"
        " AND status='done'" + base_worker,
        params).fetchone()["n"]
    stats["success_rate"] = done_worker / total_worker if total_worker else 0.0
    # 审核拒绝率
    total_reviewer = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches WHERE worker_role='reviewer'"
        + base_worker,
        params).fetchone()["n"]
    reject_reviewer = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches WHERE worker_role='reviewer'"
        " AND status='done' AND json_extract(payload, '$.verdict')='reject'"
        + base_worker,
        params).fetchone()["n"]
    stats["review_reject_rate"] = reject_reviewer / total_reviewer if total_reviewer else 0.0
    # 派单→首次工具调用延迟(按 worker 匹配派单时间窗内的 pre_tool_use)
    # 简单实现: 取派单 created_at 到最早 pre_tool_use 的平均值
    if instance_name:
        delays = []
        rows = conn.execute(
            "SELECT d.id, d.created_at FROM dispatches d"
            " JOIN instances i ON d.worker_id=i.name"
            " WHERE d.worker_role='worker' AND d.worker_id=?"
            " ORDER BY d.id",
            (instance_name,)).fetchall()
        for r in rows:
            first_tool = conn.execute(
                "SELECT ts FROM messages WHERE type='event'"
                " AND json_extract(payload, '$.event_type')='pre_tool_use'"
                " AND sender=?"
                " AND ts>=? ORDER BY ts ASC LIMIT 1",
                (instance_name, r["created_at"])).fetchone()
            if first_tool:
                delays.append(first_tool["ts"] - r["created_at"])
        stats["avg_first_tool_delay"] = sum(delays) / len(delays) if delays else 0.0
    # 任务时长分布(按 expect_min 档位,从 dispatches 和 session_states 粗估)
    duration_rows = conn.execute(
        "SELECT d.expect_min, d.created_at, d.status FROM dispatches d"
        " WHERE d.worker_role='worker'" + base_worker,
        params).fetchall()
    buckets = {}
    for dr in duration_rows:
        if dr["status"] != "done":
            continue
        # 粗估: 用派单 created_at + expect_min*60 作为参考完成时间
        # 真实时长需要 session_states/events,这里从简用固定值模拟分布
        est = dr["expect_min"]
        bucket = f"{est}-{est*2}" if est <= 60 else f"{est}-inf"
        buckets[bucket] = buckets.get(bucket, 0) + 1
    stats["task_duration_dist"] = buckets
    return stats




def dispatch_cancel(conn, ident, dispatch_id, reason, request_id=None):
    """总控取消派单(issued/active→cancelled,4.4 配套单派单版): 关登记行+
    旧 secret 作废+审计。不动任务状态、不计重派(误派/换将的纠正工具,
    2026-08-19 实证: 审核派单发错人堵双轴名额,无 CLI 可退)。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("取消派单仅总控身份可执行(4.4)")
    with tx(conn) as c:
        def _do():
            d = c.execute("SELECT * FROM dispatches WHERE id=?",
                          (dispatch_id,)).fetchone()
            if d is None:
                raise KeyError(f"派单 {dispatch_id} 不存在")
            if not check_dispatch_transition(d["status"], "cancelled"):
                raise ValueError(
                    f"派单 {dispatch_id} 状态 {d['status']} 不可取消(仅 issued/active)")
            c.execute(
                "UPDATE dispatches SET status='cancelled', updated_at=?, dcap_hash=''"
                " WHERE id=?", (now(), dispatch_id))
            c.execute(
                "UPDATE instance_registrations SET status='closed', closed_at=?,"
                " abnormal=1 WHERE dispatch_id=? AND status IN ('spawned','active')",
                (now(), dispatch_id))
            audit(c, "dispatch_cancel",
                  {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                   "worker_id": d["worker_id"], "reason": reason,
                   "by": ident["worker_id"]})
            return {"dispatch_id": dispatch_id, "from": d["status"],
                    "to": "cancelled"}
        return _with_idem(c, request_id, "dispatch_cancel", _do)
