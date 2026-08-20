"""启动器(13.3/11.1/11.2/11.5): spawn 前应然核查→任务书渲染→登记行→身份注入。

任务书=落盘文件,env 只给路径+secret,不塞全文;secret 明文只在 env。
"""

import json
import os
import subprocess
import sys
import sqlite3
from pathlib import Path

from . import auth, messages
from .db import now, task_dir, tx

TASKBOOK_TEMPLATE = """# 任务书 dispatch #{dispatch_id}(task #{task_id}): {title}

## 任务描述

{description}
{rework_section}{review_section}
## 验收命令(架构师写入,实施者不报不改)

```
{verify_cmd}
```

## 改动边界声明(11.2,必写;越界=mechanical_fail 驳回;合理越界先 worker_help 申请扩界,总控批准后按新边界继续)

{scope_section}

## 报告路径(reportPath)

{report_path}

## 预期时长

expect_min = {expect_min} 分钟(进度阶梯 = expect_min × 2;超时由监控器升级,人工裁决)

## 回报纪律(机械流程,不可省略)

1. 干完活,把成果报告写到报告路径;
2. 后台任务清单: worker_done 报告须列出所有后台子代理/异步任务及其各自结果(未完成项标注原因);
3. 结算(唯一权威完成信号): {settle_cmd};
4. 结算成功后清空上下文(/clear 或重启会话;5.3 清理机制,结算单已含清理要求)。

## 求助纪律(5.6,机械注入)

能自己查证的不许问;"超出能力/反复试错不收敛/须动改动边界声明之外的文件"才该求助。求助方向总控,经 worker_help 账本消息沟通;要思路→总控写 worker_help_reply 答复;要换人→总控裁决走既有驳回重派/强制干预通道。
"""

# 应然清单基本形态(11.5): claude 壳条目(期望 env 集+任务书模板+登记行字段+钩子清单)
SHOULD_LIST_CLAUDE = {
    "shell": "claude",
    "env_vars": [auth.ENV_WORKER_ID, auth.ENV_SECRET,
                 auth.ENV_DISPATCH_ID, auth.ENV_TASK_PATH],
    "taskbook": "TASKBOOK_TEMPLATE",
    "registration_fields": ["instance_name", "dispatch_id", "dcap_hash",
                            "task_path", "status"],
    "hooks": ["SessionStart", "SessionEnd", "Stop", "UserPromptSubmit",
              "PreToolUse", "PostToolUse", "PermissionRequest",
              "SubagentStart", "SubagentStop"],
}


def _review_section(conn, dispatch: dict) -> str:
    """审核派单专用(8.5): 被审对象=同任务最近已结算的实施者派单。

    2026-08 演示踩坑: 通用任务书不含被审对象,审核者在自己目录找产物误 reject。
    票 04 扩展: 按 axis 分支渲染审核指令(spec/quality)。
    """
    row = conn.execute(
        "SELECT id, worker_id, task_dir FROM dispatches WHERE task_id=?"
        " AND worker_role='worker' AND status='done' ORDER BY id DESC LIMIT 1",
        (dispatch["task_id"],)).fetchone()
    if row is None:
        return ("\n## 被审对象\n\n(未找到本任务已结算的实施者派单,"
                "无可审对象,请升级总控,不要自行猜测产物位置)\n")
    tdir = Path(row["task_dir"])
    artifacts = sorted(
        p.name for p in tdir.iterdir()
        if p.is_file() and p.name != "task.md") if tdir.is_dir() else []
    axis = (dispatch["axis"] if "axis" in dispatch.keys() else "") or "spec"
    template_row = conn.execute(
        "SELECT value FROM configs WHERE key='review_template'"
    ).fetchone()
    template = json.loads(template_row["value"]) if template_row else {}
    axis_guide = template.get(axis, "逐条核对任务书验收标准;审核报告须附行为证据。")
    lines = [
        "",
        "## 被审对象(你是审核者: 审查以下实施者产出,不要自己动手干活)",
        "",
        f"- 被审派单: dispatch #{row['id']}(实施者 {row['worker_id']})",
        f"- 产物目录: {row['task_dir']}",
        f"- 实施者报告: {tdir / 'report.md'}",
        f"- 产物清单: {', '.join(artifacts) if artifacts else '(空)'}",
        "",
        f"### 审核轴: {axis}",
        axis_guide,
        "",
        '逐条核对"任务描述"与"验收命令";审核报告写入你的报告路径。',
        "",
    ]
    # 非 git 降级标注(8.3/8.4 维度 2,票 21): 机械边界比对不可用→人工核对项
    trow = conn.execute(
        "SELECT scope_guard, project_dir FROM tasks WHERE id=?",
        (dispatch["task_id"],)).fetchone()
    raw_scope = (trow["scope_guard"] or "") if trow else ""
    if raw_scope:
        payload = (json.loads(dispatch["payload"])
                   if dispatch["payload"] else {})
        wt = payload.get("worktree_path", "")
        from .ops import _is_git_repo
        is_git = bool(wt and os.path.isdir(wt)) or bool(
            trow["project_dir"] and _is_git_repo(trow["project_dir"]))
        if not is_git:
            lines += [
                "- **非 git 项目降级项(8.4 维度 2)**: 机械边界比对不可用,"
                "请人工核对产物实际改动路径是否都在改动边界声明内: "
                + ", ".join(f"`{p}`" for p in json.loads(raw_scope)),
                "",
            ]
    return "\n".join(lines)


def _rework_section(dispatch: dict) -> str:
    """重派任务书须带驳回原因与返修要点(4.3): 否则工人不知前一轮差在哪。

    reason 来自派单载荷(驳回/重派时写入);首次派单 reason 为空则不渲染。
    """
    try:
        reason = json.loads(dispatch["payload"] or "{}").get("reason", "")
    except json.JSONDecodeError:
        reason = ""
    if not reason:
        return ""
    return ("\n## 上一轮驳回/重派原因(4.3,先读懂再动手)\n\n"
            f"{reason}\n")


def _win_split(cmd: str) -> list:
    """Windows 命令行解析: 空白分词,双引号包一段(引号去掉,保留内部反斜杠)。

    2026-08-19 实证替代 shlex:
      - shlex.split(posix=False) 保留引号 → 参数带字面 \" 被程序当参数内容
        (姜维 atomcode -p 收到 \"你是...\" 卡死);
      - shlex.split(posix=True) 吃掉反斜杠 → Windows 路径 D:\\soft → D:soft。
    """
    parts: list = []
    cur: list = []
    in_quote = False
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c == '"':
            in_quote = not in_quote
            i += 1
        elif c in " \t" and not in_quote:
            if cur:
                parts.append("".join(cur))
                cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    if cur:
        parts.append("".join(cur))
    return parts


def _is_headless_cmd(launch_cmd: str) -> bool:
    """判断 launch_cmd 是否无头模式(无需交互窗口)。

    无头壳跑完 prompt 就退出、不自绘 TUI,开新窗口只会留下黑屏空窗
    (2026-08-19 实证: dsh headless / atomcode -p / kimi -p 每次都冒出
    标题为 PowerShell 的黑屏 terminal,攒多卡机)。
    交互壳(claude 自绘 TUI)保留 CREATE_NEW_CONSOLE 真窗口。
    判断依据=launch_cmd 里的无头特征参数,与壳模板 6.1 档 3 无头支持一致。
    """
    if not launch_cmd:
        return False
    lower = launch_cmd.lower()
    markers = (
        "--profile headless", " --headless", " -p \"", " -p '", " --prompt ",
        " --prompt-file ", " --json", "--dangerously-skip-permissions",
        " -y ", " --yolo", " --auto", " -c -p",
    )
    return any(m in lower for m in markers)


def _spawn_flags(launch_cmd: str, display_mode: str = "前台") -> int:
    """Windows 拉起标志(15.8,票 26): 后台=无窗口进程;前台=无头壳静默/交互壳真窗口。"""
    if display_mode == "后台":
        return subprocess.CREATE_NO_WINDOW
    if _is_headless_cmd(launch_cmd):
        return subprocess.CREATE_NO_WINDOW
    return subprocess.CREATE_NEW_CONSOLE


# 思考级别中文→壳模板键(13.3 壳无关抽象,票 26)
_THINKING_ZH2EN = {"低": "low", "中": "medium", "高": "high"}


def _apply_thinking_level(conn, inst, payload: dict) -> dict:
    """思考级别翻译注入(13.3,票 26): 启动器按壳模板映射表翻译。

    派单载荷单点覆盖优先于实例默认;某壳/模型不支持=实例档案如实记+
    审计,不静默假装生效。返回 {"applied": bool, ...}。
    """
    from . import ops
    level_zh = payload.get("thinking_level") or inst["thinking_level"] or ""
    if not level_zh:
        return {"applied": False, "reason": "未指定"}
    level = _THINKING_ZH2EN[level_zh]
    from .adapters.template import get_template
    try:
        tmap = get_template(inst["shell"]).thinking_level_map
    except KeyError:
        tmap = None

    def _unsupported(reason):
        ops.update_profile_notes(
            conn, inst["name"],
            f"思考级别注入未生效({reason},壳 {inst['shell']},13.3)")
        ops.audit(conn, "thinking_apply",
                  {"instance": inst["name"], "level": level_zh,
                   "applied": False, "reason": reason})
        return {"applied": False, "reason": reason}

    if not tmap or level not in tmap:
        return _unsupported("壳模板无思考级别映射")
    rule = tmap[level]
    if "config_key" in rule:
        # codex 形态: 写隔离目录 config.toml(13.3 壳内配置型)
        if not inst["isolated_dir"]:
            return _unsupported("无隔离配置目录")
        cfg = Path(inst["isolated_dir"]) / "config.toml"
        if not cfg.exists():
            return _unsupported("隔离配置 config.toml 不存在")
        import re
        text = cfg.read_text(encoding="utf-8")
        line = f'{rule["config_key"]} = "{rule["value"]}"'
        if re.search(rf'^{rule["config_key"]}\s*=', text, flags=re.M):
            text = re.sub(rf'^{rule["config_key"]}\s*=.*$', line,
                          text, flags=re.M)
        else:
            text = line + "\n" + text  # TOML 顶层键须在各 section 之前
        cfg.write_text(text, encoding="utf-8")
        target = str(cfg)
    else:
        # dsh 形态: patch 覆盖文件(13.3;生效依赖 launch_cmd 引用该 patch)
        if not inst["isolated_dir"]:
            return _unsupported("无隔离配置目录")
        patch = Path(inst["isolated_dir"]) / "thinking.patch.yml"
        patch.write_text(
            f'agent-default-model:\n  reasoningEfforts: ["{rule["value"]}"]\n',
            encoding="utf-8")
        target = str(patch)
    ops.audit(conn, "thinking_apply",
              {"instance": inst["name"], "level": level_zh,
               "applied": True, "target": target})
    return {"applied": True, "target": target, "level": level_zh}


def _render_taskbook(conn, dispatch: dict, task: dict, report_path: str) -> str:
    if dispatch["worker_role"] == "reviewer":
        settle_cmd = (f'`tianji dispatch settle {dispatch["id"]} "{report_path}"'
                      f" pass`(通过)或 `... reject`(拒绝,报告附原因)")
        review_section = _review_section(conn, dispatch)
    else:
        settle_cmd = f'`tianji dispatch settle {dispatch["id"]} "{report_path}" ok`'
        review_section = ""
    description = task["description"] or "(无描述)"
    raw_scope = task["scope_guard"] if "scope_guard" in task.keys() else ""
    prefixes = json.loads(raw_scope) if raw_scope else []
    if prefixes:
        scope_section = ("只允许改动以下目录前缀内的文件(新增文件限同处):\n"
                         + "\n".join(f"- `{p}`" for p in prefixes))
    else:
        scope_section = "(未声明——架构师在计划确认前必写,11.2)"
    return TASKBOOK_TEMPLATE.format(
        dispatch_id=dispatch["id"], task_id=task["id"], title=task["title"],
        description=description,
        verify_cmd=task["verify_cmd"] or "(未配置)",
        scope_section=scope_section,
        report_path=report_path, expect_min=dispatch["expect_min"],
        settle_cmd=settle_cmd, review_section=review_section,
        rework_section=_rework_section(dispatch),
    )


def spawn(conn: sqlite3.Connection, instance_name: str, dispatch_id: int,
          run: bool = False) -> dict:
    """spawn(18.7): 应然核查→渲染任务书→写登记行(spawned)→生成 secret→注入 env。

    secret 明文只在 env(不进文件);重启=新 secret 自然作废旧身份(11.3)。
    """
    with tx(conn) as c:
        inst = c.execute("SELECT * FROM instances WHERE name=?",
                         (instance_name,)).fetchone()
        if inst is None:
            raise KeyError(f"实例 {instance_name} 未注册")
        d = c.execute("SELECT * FROM dispatches WHERE id=?",
                      (dispatch_id,)).fetchone()
        if d is None:
            raise KeyError(f"派单 {dispatch_id} 不存在")
        if d["worker_id"] != instance_name:
            raise ValueError(f"派单 {dispatch_id} 不属于实例 {instance_name}")
        if d["status"] != "issued":
            raise ValueError(f"派单 {dispatch_id} 状态 {d['status']},非 issued 不可 spawn")
        t = c.execute("SELECT * FROM tasks WHERE id=?", (d["task_id"],)).fetchone()
        # 应然核查(11.5 基本形态): 任务书可渲染+登记行字段齐
        report_path = str(Path(d["task_dir"]) / "report.md")
        taskbook = _render_taskbook(c, d, t, report_path)
        tdir = Path(d["task_dir"])
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "task.md").write_text(taskbook, encoding="utf-8")
        # 生成 secret(摘要存派单+登记行;明文只进 env)
        secret = auth.generate_secret()
        c.execute("UPDATE dispatches SET dcap_hash=? WHERE id=?",
                  (auth.secret_hash(secret), dispatch_id))
        c.execute(
            "INSERT INTO instance_registrations (instance_name, dispatch_id,"
            " status, dcap_hash, task_path, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (instance_name, dispatch_id, "spawned", auth.secret_hash(secret),
             str(tdir / "task.md"), now()))
        messages.send(c, "dispatch", "launcher",
                      {"dispatch_id": dispatch_id, "task_id": d["task_id"],
                       "worker_id": instance_name,
                       "secret_hash": auth.secret_hash(secret)},
                      "reviewer" if d["worker_role"] == "reviewer" else "worker")

    # 运行命令(env 注入,11.4 一套命名)
    env = os.environ.copy()
    # 剔除 claude 内部 marker(误继承会把会话判为子会话、关闭转录保存,
    # 监控器档 2 字节活性与对账③都依赖转录;2026-08 演示踩坑)
    for k in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"):
        env.pop(k, None)
    payload = json.loads(d["payload"]) if d["payload"] else {}
    worktree_path = payload.get("worktree_path", "") or ""
    spawn_cwd = worktree_path if worktree_path and os.path.isdir(worktree_path) else d["task_dir"]
    # 票 26: 思考级别翻译注入(单点覆盖>实例默认);显示模式生效(覆盖>实例默认"前台")
    thinking = _apply_thinking_level(conn, inst, payload)
    eff_display = payload.get("display_mode") or inst["display_mode"] or "前台"
    env.update({
        auth.ENV_WORKER_ID: instance_name,
        auth.ENV_SECRET: secret,
        auth.ENV_DISPATCH_ID: str(dispatch_id),
        auth.ENV_TASK_PATH: str(Path(d["task_dir"]) / "task.md"),
    })
    pid = None
    if run and inst["launch_cmd"]:
        # Windows: cmd /k 包一层开独立交互窗口(不直启)。
        # 直启+CREATE_NEW_CONSOLE 时 stdio 仍继承父进程管道,claude 壳会判定
        # 非交互而静默 headless(无窗口);cmd /k 让子进程拿到新控制台句柄,
        # 窗口保持可见可交互,且 pid 随窗口生命周期(监控器对账②语义一致)。
        if os.name == "nt":
            # 直启(claude 壳自绘 TUI,CREATE_NEW_CONSOLE 下可正常显示);
            # cmd /k 包装会闪退,不要用。
            # close_fds=True → bInheritHandles=False → 新 console 自动分配
            # 交互 stdio(否则 stdio 继承父进程管道,claude 判定非交互即退出)。
            # launch_cmd 约定: 首 token 为可执行文件绝对路径(.exe),
            # 参数可带引号(Windows 命令行解析)。
            # 2026-08-19 实证: shlex.split(posix=False) 保留引号(参数带 \" 被
            # 程序当字面量),posix=True 吃掉路径反斜杠——两者都不行;
            # 用自定义 Windows 解析(空白分词+双引号包段去引号,保留反斜杠)。
            parts = _win_split(inst["launch_cmd"])
            proc = subprocess.Popen(parts, env=env,
                                    cwd=spawn_cwd,
                                    creationflags=_spawn_flags(inst["launch_cmd"], eff_display),
                                    close_fds=True)
        else:
            proc = subprocess.Popen(inst["launch_cmd"], shell=True, env=env,
                                    cwd=spawn_cwd)
        pid = proc.pid
        with tx(conn) as c:
            c.execute(
                "UPDATE instance_registrations SET pid=? WHERE instance_name=?"
                " AND dispatch_id=? AND status='spawned'",
                (pid, instance_name, dispatch_id))
    return {"dispatch_id": dispatch_id, "instance": instance_name,
            "taskbook": str(Path(d["task_dir"]) / "task.md"),
            "secret": secret, "pid": pid,
            "display_mode": eff_display, "thinking": thinking,
            "env": {auth.ENV_WORKER_ID: instance_name,
                    auth.ENV_SECRET: secret,
                    auth.ENV_DISPATCH_ID: str(dispatch_id),
                    auth.ENV_TASK_PATH: str(Path(d["task_dir"]) / "task.md")},
            "cmd": _run_cmd(inst["launch_cmd"] or "claude", env, spawn_cwd),
            "worktree_path": worktree_path}


def _run_cmd(launch: str, env: dict, cwd: str) -> str:
    """输出给用户/文档查看的运行命令(Windows cmd 形态;bash 同理)。"""
    lines = []
    for k, v in env.items():
        if k.startswith("TIANJI_"):
            lines.append(f'set {k}={v}')
    lines.append(f"cd /d {cwd}")
    lines.append(launch)
    return " && ".join(lines)
