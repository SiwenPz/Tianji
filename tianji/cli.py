"""账本 CLI(18.7 命令面,原型部分): typer 实现,唯一写入口+校验三件套。

身份=env 注入(TIANJI_WORKER_ID/TIANJI_SECRET);未注入的写操作被拒。
"""

import json
import os
import sys

import typer

from . import auth, daemon, events, messages, ops, permission, plugins, render, wizard
from .db import connect, now, tx
from .cockpit import render_snapshot, snapshot

app = typer.Typer(help="天机: 多 CLI 编程助手协作框架(最小原型)")
task_app = typer.Typer(help="任务域")
dispatch_app = typer.Typer(help="派单域")
message_app = typer.Typer(help="消息域")
instance_app = typer.Typer(help="会话域")
config_app = typer.Typer(help="账本配置")
ledger_app = typer.Typer(help="账本导出")
app.add_typer(task_app, name="task")
app.add_typer(dispatch_app, name="dispatch")
app.add_typer(message_app, name="message")
app.add_typer(instance_app, name="instance")
app.add_typer(config_app, name="config")
app.add_typer(ledger_app, name="ledger")

architect_app = typer.Typer(help="架构师裁判(8.2)")
app.add_typer(architect_app, name="architect")

shell_config_app = typer.Typer(help="壳条目配置")
key_config_app = typer.Typer(help="Key 条目配置")
config_app.add_typer(shell_config_app, name="shell")
config_app.add_typer(key_config_app, name="key")


cockpit_app = typer.Typer(help="驾驶舱(只读快照)")
app.add_typer(cockpit_app, name="cockpit")


daemon_app = typer.Typer(help="daemon 守护(18.2/18.3): start/stop/status")
app.add_typer(daemon_app, name="daemon")


def _conn():
    conn = connect()
    ops.ensure_defaults(conn)
    return conn


def _ident():
    return auth.require_identity()


def _out(d):
    print(json.dumps(d, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- task

@task_app.command("new")
def task_new(title: str, description: str = "", priority: int = 0,
             source: str = "user", project_dir: str = "",
             request_id: str = typer.Option(None, "--request-id")):
    """总控建任务(10.1 创建者机械限定;不可逆,必带 request-id)。"""
    _out(ops.task_new(_conn(), _ident(), title, description, priority,
                      source, project_dir, request_id))


@task_app.command("list")
def task_list(status: str = None):
    _out({"tasks": ops.task_list(_conn(), status)})


@task_app.command("show")
def task_show(task_id: int):
    _out(ops.task_get(_conn(), task_id))


@task_app.command("priority")
def task_priority(task_id: int, priority: int,
                  request_id: str = typer.Option(None, "--request-id")):
    """总控改优先级(10.4,带审计)。"""
    _out(ops.task_priority(_conn(), _ident(), task_id, priority, request_id))


@task_app.command("reopen")
def task_reopen(task_id: int, reason: str = "",
                request_id: str = typer.Option(None, "--request-id")):
    """archived→reopened(10.6,总控,重派计数清零)。"""
    _out(ops.task_reopen(_conn(), _ident(), task_id, reason, request_id))


@task_app.command("transition")
def task_transition(task_id: int, to: str, reason: str = "",
                    request_id: str = typer.Option(None, "--request-id")):
    """九态推进(4.2 转换表机械校验;驳回=重派含计数与新派单)。"""
    _out(ops.task_transition(_conn(), _ident(), task_id, to, request_id, reason))


@task_app.command("force")
def task_force(task_id: int, to: str, reason: str,
               new_worker: str = typer.Option(None, "--new-worker"),
               request_id: str = typer.Option(None, "--request-id")):
    """强制干预(4.4): 总控特权例外转换+审计,不豁免重派计数。改派可用 --new-worker 指定目标工人。"""
    _out(ops.task_force(_conn(), _ident(), task_id, to, reason, request_id,
                        new_worker=new_worker))


@task_app.command("verify-cmd")
def task_verify_cmd(task_id: int, cmd: str,
                    request_id: str = typer.Option(None, "--request-id")):
    """架构师在计划确认前写入验收命令(8.3;总控兼架构师操作)。"""
    _out(ops.task_set_verify_cmd(_conn(), _ident(), task_id, cmd, request_id))


@task_app.command("scope")
def task_scope(task_id: int, prefixes: str,
               reason: str = typer.Option("", "--reason"),
               request_id: str = typer.Option(None, "--request-id")):
    """改动边界声明(11.2/8.3,票 21): 逗号分隔目录前缀;扩界批准也走这里(带审计)。"""
    _out(ops.task_scope_set(_conn(), _ident(), task_id, prefixes,
                            reason=reason, request_id=request_id))


# ---------------------------------------------------------------- dispatch

@dispatch_app.command("issue")
def dispatch_issue(task_id: int, worker: str, role: str = "worker",
                   axis: str = "", reason: str = "",
                   expect_min: int = None,
                   display_mode: str = typer.Option(
                       None, "--display-mode",
                       help="显示模式单点覆盖 前台|后台(15.8,不改实例默认)"),
                   thinking_level: str = typer.Option(
                       None, "--thinking-level",
                       help="思考级别单点覆盖 低|中|高(13.3,不改实例默认)"),
                   request_id: str = typer.Option(None, "--request-id")):
    """分配器派单(原型期总控侧执行;不可逆,必带 request-id)。返修补派用 --reason 带驳回原因(4.3)。"""
    _out(ops.dispatch_issue(_conn(), _ident(), task_id, worker, role,
                            expect_min, request_id, axis=axis, reason=reason,
                            display_mode=display_mode,
                            thinking_level=thinking_level))


@dispatch_app.command("show")
def dispatch_show(dispatch_id: int):
    _out(ops.dispatch_get(_conn(), dispatch_id))


@dispatch_app.command("revive")
def dispatch_revive(dispatch_id: int, reason: str = "",
                    request_id: str = typer.Option(None, "--request-id")):
    """stale→active 复活(总控,审计): 工人确活着(误标停滞)时恢复结算通道。"""
    _out(ops.dispatch_revive(_conn(), _ident(), dispatch_id, reason, request_id))


@dispatch_app.command("nudge")
def dispatch_nudge(dispatch_id: int, reason: str = "",
                   request_id: str = typer.Option(None, "--request-id")):
    """工人停滞续推(7.5 续推通道): 总控专属花钱动作,翻译续跑命令+审计+实例档案。

    不改任务/派单状态机;续推不消耗重派计数、不扣表现分;
    不支持续跑的壳 fail-loud 并记录实例档案,退回人工。
    """
    _out(ops.dispatch_nudge(_conn(), _ident(), dispatch_id, reason, request_id))


@dispatch_app.command("settle")
def dispatch_settle(dispatch_id: int, report_path: str, outcome: str,
                    reason: str = ""):
    """worker_done 单事务结算(5.4): 身份 env 校验,四拒绝码,幂等键=dispatch_id。

    实施者 outcome=ok;审核派单 outcome=pass/reject(带 reason)。
    身份取 os.environ(启动器注入),与 ingest-event 同理由(ops 层按 env 格式读)。
    """
    _out(ops.dispatch_settle(_conn(), os.environ, dispatch_id, report_path,
                             outcome, reason))


# ---------------------------------------------------------------- message

@message_app.command("send")
def message_send(type_: str, to: str = None, payload: str = "{}",
                 request_id: str = typer.Option(None, "--request-id")):
    """发消息(3.1 类型白名单+收件人合法组合机械校验)。"""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"payload 非法 JSON: {e}")
    conn = _conn()
    ident = _ident()
    with tx(conn) as c:
        _out(messages.send(c, type_, ident["worker_id"], data, to))


@message_app.command("check")
def message_check(consumer: str, role: str, limit: int = 100):
    """读未读(游标之后+角色匹配);不推进游标(显式 ack)。"""
    _out({"unread": messages.check_unread(_conn(), consumer, role, limit)})


@message_app.command("ack")
def message_ack(consumer: str, up_to_seq: int):
    """显式 ack: 单事务推进游标(3.2,不 ack 即重放)。"""
    conn = _conn()
    with tx(conn) as c:
        _out(messages.ack(c, consumer, up_to_seq))


# ---------------------------------------------------------------- event

@app.command("ingest-event")
def ingest_event():
    """事件入口(6.5): stdin 收单行 JSON;身份=启动器 env 注入,防冒名。

    身份取 os.environ(启动器注入的 TIANJI_* 环境变量),非 _ident() 的 2 键 dict——
    events 层 require_identity(env) 按 env 格式读取。
    """
    line = sys.stdin.read()
    _out(events.ingest_event_line(_conn(), os.environ, line))


# ---------------------------------------------------------------- instance

@instance_app.command("register")
def instance_register(name: str, shell: str, model: str,
                      isolated_dir: str = "", launch_cmd: str = "",
                      controller: bool = False, skills: str = "[]",
                      context_window: int = 0,
                      key_name: str = "",
                      permission_granularity: str = typer.Option(
                          "", "--permission-granularity",
                          help="能力画像权限粒度(project/readonly 等,透传 ops)"),
                      profile_notes: str = typer.Option(
                          "", "--profile-notes",
                          help="能力画像初始档案备注(透传 ops)"),
                      display_mode: str = typer.Option(
                          "前台", "--display-mode",
                          help="显示模式 前台|后台(15.8,默认前台)"),
                      thinking_level: str = typer.Option(
                          "", "--thinking-level",
                          help="默认思考级别 低|中|高(13.3,空=壳默认)")):
    """注册实例(四元组:壳/key_name/模型/隔离目录)。生成 secret 明文仅此一次;--controller 配置总控身份。"""
    ident = auth.env_identity() if controller else None
    _out(ops.instance_register(_conn(), name, shell, model, isolated_dir,
                               launch_cmd, controller, skills, context_window,
                               key_name, permission_granularity, profile_notes,
                               display_mode, thinking_level, ident=ident))


@instance_app.command("unbind")
def instance_unbind(name: str,
                    request_id: str = typer.Option(None, "--request-id")):
    """换绑/下线(旧 secret 自然作废)。"""
    _out(ops.instance_unbind(_conn(), name, request_id))


@instance_app.command("delete")
def instance_delete(name: str,
                    request_id: str = typer.Option(None, "--request-id")):
    """物理删除实例注册+能力画像(13.6,总控专属+审计;在途派单工人禁删)。"""
    _out(ops.instance_delete(_conn(), _ident(), name, request_id))


@instance_app.command("update")
def instance_update(name: str, model: str = None, key_name: str = None,
                    launch_cmd: str = None, isolated_dir: str = None,
                    context_window: int = None, skills: str = None,
                    permission_granularity: str = None,
                    display_mode: str = None, thinking_level: str = None,
                    request_id: str = typer.Option(None, "--request-id")):
    """实例配置就地修改(13.6 增删改,票 28,总控专属+审计)。

    换 key 本体/换 url 走 key 条目(config key set / key_ref 文件),不动实例;
    本命令改实例四元/画像字段,不重建实例;在途派单允许改(只影响下一次 spawn)。
    """
    _out(ops.instance_update(_conn(), _ident(), name, model=model,
                             key_name=key_name, launch_cmd=launch_cmd,
                             isolated_dir=isolated_dir,
                             context_window=context_window, skills=skills,
                             permission_granularity=permission_granularity,
                             display_mode=display_mode,
                             thinking_level=thinking_level,
                             request_id=request_id))


@instance_app.command("controller-recover")
def instance_controller_recover(name: str):
    """总控 secret 丢失恢复(本机操作者=信任根,带审计;secret 明文仅打印一次)。"""
    _out(ops.controller_recover(_conn(), name))


@instance_app.command("list")
def instance_list():
    _out({"instances": ops.instance_list(_conn())})


@instance_app.command("set-pid")
def instance_set_pid(name: str, pid: int,
                     request_id: str = typer.Option(None, "--request-id")):
    """手动回填 pid(外部拉起通道,7.4②): 总控操作,带审计。"""
    _out(ops.instance_set_pid(_conn(), _ident(), name, pid, request_id))


@instance_app.command("profile-notes")
def instance_profile_notes(name: str, text: str):
    """实例档案追加录入(9.1): 在能力画像 notes 中追加一行(时间戳前缀),走 ops.update_profile_notes。"""
    _out({"instance": name, "notes": ops.update_profile_notes(_conn(), name, text)})


# ---------------------------------------------------------------- config

@config_app.command("get")
def config_get(key: str = None):
    _out(ops.config_get(_conn(), key))


@config_app.command("set")
def config_set(key: str, value: str,
               request_id: str = typer.Option(None, "--request-id")):
    """配置变更=总控+审计(2.3 零配置文件)。"""
    _out(ops.config_set(_conn(), _ident(), key, value, request_id))


# ---------------------------------------------------------------- config shell/key

@shell_config_app.command("get")
def shell_config_get(name: str):
    _out(ops.config_get(_conn(), f"shell:{name}"))


@shell_config_app.command("set")
def shell_config_set(name: str, binding: str = "env",
                     protocols: str = "stdio",
                     isolated_dir_mode: str = "env_home",
                     request_id: str = typer.Option(None, "--request-id")):
    """壳条目变更=总控+审计(13.2)。protocols 逗号分隔。"""
    value = json.dumps({
        "binding": binding,
        "protocols": [p.strip() for p in protocols.split(",") if p.strip()],
        "isolated_dir_mode": isolated_dir_mode,
    }, ensure_ascii=False)
    _out(ops.config_set(_conn(), _ident(), f"shell:{name}", value, request_id))


@shell_config_app.command("list")
def shell_config_list():
    rows = ops.config_get(_conn())
    shells = []
    for r in rows:
        if r["key"].startswith("shell:"):
            try:
                shells.append({"key": r["key"], **json.loads(r["value"])})
            except json.JSONDecodeError:
                shells.append({"key": r["key"], "value": r["value"]})
    _out(shells)


@shell_config_app.command("delete")
def shell_config_delete(name: str,
                        request_id: str = typer.Option(None, "--request-id")):
    """壳条目删除=总控+审计;有活跃实例引用拒绝删除(13.4)。"""
    _out(ops.config_delete(_conn(), _ident(), f"shell:{name}", request_id))


@key_config_app.command("get")
def key_config_get(name: str):
    _out(ops.config_get(_conn(), f"key:{name}"))


@key_config_app.command("set")
def key_config_set(name: str, base_url: str = "",
                   models: str = "",
                   protocol: str = "stdio",
                   key_ref: str = "",
                   coding_plan: bool = False,
                   request_id: str = typer.Option(None, "--request-id")):
    """Key 条目变更=总控+审计(13.4)。

    base_url 落账本(key 本体不留);models JSON 数组字符串;
    coding_plan=true 标记 CodingPlan 类 key(不跨壳)。
    """
    model_list = json.loads(models) if models else []
    value = json.dumps({
        "base_url": base_url,
        "models": model_list,
        "protocol": protocol,
        "key_ref": key_ref or None,
        "coding_plan": coding_plan,
    }, ensure_ascii=False)
    _out(ops.config_set(_conn(), _ident(), f"key:{name}", value, request_id))


@key_config_app.command("list")
def key_config_list():
    rows = ops.config_get(_conn())
    keys = []
    for r in rows:
        if r["key"].startswith("key:"):
            try:
                keys.append({"key": r["key"], **json.loads(r["value"])})
            except json.JSONDecodeError:
                keys.append({"key": r["key"], "value": r["value"]})
    _out(keys)


@key_config_app.command("delete")
def key_config_delete(name: str,
                      request_id: str = typer.Option(None, "--request-id")):
    """Key 条目删除=总控+审计;有活跃实例引用的壳/key 条目拒绝删除(13.4)。"""
    _out(ops.config_delete(_conn(), _ident(), f"key:{name}", request_id))


# ---------------------------------------------------------------- ledger

@ledger_app.command("export")
def ledger_export(after: int = 0, limit: int = 1000):
    """跨机预留(3.4): 按 seq 增量导出。"""
    _out({"messages": ops.export_messages(_conn(), after, limit)})


# ---------------------------------------------------------------- architect

@architect_app.command("confirm")
def architect_confirm(task_id: int, reason: str = "",
                      request_id: str = typer.Option(None, "--request-id")):
    """架构师二次确认(8.2): 双轴一致通过后确认,放行 awaiting_final_confirm。"""
    _out(ops.architect_confirm(_conn(), _ident(), task_id, reason, request_id))


@architect_app.command("review")
def architect_review(task_id: int, reason: str = "",
                     request_id: str = typer.Option(None, "--request-id")):
    """架构师深审裁决(8.2): 双轴分歧时读两份审核报告定夺,裁决消息进账本。"""
    _out(ops.architect_review(_conn(), _ident(), task_id, reason, request_id))


# ---------------------------------------------------------------- spawn / verify / monitor

@app.command("spawn")
def spawn(instance: str, dispatch_id: int, run: bool = False):
    """启动器(11.1/11.2): 任务书渲染+登记行+secret 生成+env 注入;--run 直接启动。"""
    _out(render.spawn(_conn(), instance, dispatch_id, run))


worktree_app = typer.Typer(help="worktree 原语(票 05)")
app.add_typer(worktree_app, name="worktree")


@worktree_app.command("merge")
def worktree_merge(task_id: int):
    """机械合并任务 worktree 回基础分支(挂 final_confirm 流程)。"""
    conn = _conn()
    try:
        r = ops.worktree_merge(conn, task_id)
        _out(r)
    finally:
        conn.close()


@app.command("verify")
def verify(task_id: int, timeout: int = 900):
    """机械验收门(8.3): 声称触发,验收命令+reportPath 机械检查;失败驳回重派。"""
    _out(ops.mechanical_verify(_conn(), task_id, timeout))


@app.command("monitor")
def monitor(interval: int = 30, once: bool = False):
    """监控器(7.x): tick 循环,活性双阶梯+对账三件+停滞分级。"""
    from .monitor import run_monitor
    run_monitor(interval, once)


@app.command("parse-transcript")
def parse_transcript(shell: str, session_id: str):
    """档 2 转录增量解析(6.3): 按壳模板定位转录文件,增量解析补事件。

    从上次游标位置继续,只处理新增内容。事件归一化后经 ingest-event 进账本。
    """
    conn = _conn()
    ident = _ident()
    from .adapters.transcript_parser import parse_transcript
    result = parse_transcript(conn, os.environ, shell, session_id)
    _out(result)


@cockpit_app.command("show")
def cockpit_show():
    """驾驶舱只读快照(15.5): 直接读账本,不写账本。"""
    conn = connect()
    try:
        snap = snapshot(conn)
        print(render_snapshot(snap, plugins.render_view_blocks(conn, snap)))
    finally:
        conn.close()


plugin_app = typer.Typer(help="插件管理(21.1): 注册表在账本 configs,变更带审计")
app.add_typer(plugin_app, name="plugin")

permission_app = typer.Typer(help="权限裁决(6.6): 决策入口唯一=总控,工人零参与")
app.add_typer(permission_app, name="permission")

wizard_app = typer.Typer(help="初始化向导(13.1): 收集→测试→呈现→确认生成,零配置文件")
app.add_typer(wizard_app, name="wizard")

quota_app = typer.Typer(help="额度与健康度(14.1/14.2): 已尽必知,将尽有提示")
app.add_typer(quota_app, name="quota")

hooks_app = typer.Typer(help="钩子分发(17 章): 副本+版本指纹+对账重装")
app.add_typer(hooks_app, name="hooks")

theme_app = typer.Typer(help="主题插件(21.5): 绰号/主题沉浸,默认关,纯装饰层")
app.add_typer(theme_app, name="theme")


@theme_app.command("list")
def theme_list():
    """主题清单与开关状态。"""
    from . import theme
    _out(theme.list_themes(_conn()))


@theme_app.command("on")
def theme_on(name: str = "三国",
             request_id: str = typer.Option(None, "--request-id")):
    """开主题(默认三国;带审计;话术经插件管线渲染)。"""
    from . import theme
    _out(theme.enable(_conn(), _ident(), name, request_id))


@theme_app.command("off")
def theme_off(request_id: str = typer.Option(None, "--request-id")):
    """关主题(带审计;已起名保留,话术退回大白话)。"""
    from . import theme
    _out(theme.disable(_conn(), _ident(), request_id))


@theme_app.command("next-name")
def theme_next_name():
    """按主题清单起新实例名(耗尽→提示进账本不阻塞)。"""
    from . import theme
    _out({"next_name": theme.next_name(_conn())})


@theme_app.command("guidance")
def theme_guidance():
    """总控会话话术(主题开=主题腔调;关/坏=大白话,fail-open)。"""
    from . import theme
    _out({"guidance": theme.guidance(_conn())})



@hooks_app.command("check")
def hooks_check(instance: str = ""):
    """对账(17.2③ 手动): 三态指纹——缺失/旧版机械补,用户改过不碰+升级。"""
    from . import hooks
    conn = _conn()
    if instance:
        _out(hooks.reconcile_instance(conn, instance))
    else:
        _out(hooks.scan_all(conn, throttle=0))


@hooks_app.command("reinstall")
def hooks_reinstall(instance: str,
                    request_id: str = typer.Option(None, "--request-id")):
    """强制重装官方版(17.3 用户裁决"重置"通道;升级后立刻刷一批)。"""
    from . import hooks
    auth_check = _ident()
    conn = _conn()
    if not auth.check_controller(conn, auth_check):
        raise PermissionError("hooks reinstall 仅总控身份可执行")
    _out(hooks.install_instance(conn, instance))



@quota_app.command("show")
def quota_show(instance: str = ""):
    """额度/上下文健康度快照(单实例或全部)。"""
    conn = _conn()
    if instance:
        from . import quota
        _out(quota.context_health(conn, instance))
    else:
        from . import quota
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM instances WHERE is_active=1").fetchall()]
        _out([quota.context_health(conn, n) for n in names])


@quota_app.command("report")
def quota_report(instance: str, pct: float):
    """statusline 机械上报入口(14.1①): 上下文占用百分比(身份=env 注入)。"""
    from . import quota
    auth.require_identity()  # 防冒名(与 ingest-event 同理由)
    _out(quota.report_context_pct(_conn(), instance, pct))



@wizard_app.command("add")
def wizard_add(name: str, shell: str, model: str, key_name: str = "",
               base_url: str = "", protocol: str = "", key_ref: str = "",
               isolated_dir: str = "", binary: str = "",
               skip_test: bool = False, confirm: bool = False,
               request_id: str = typer.Option(None, "--request-id")):
    """向导新增实例(四步走);--confirm 才确认生成注册,否则只呈现。"""
    _out(wizard.add_instance(_conn(), _ident(), name, shell, model,
                             key_name=key_name, base_url=base_url,
                             protocol=protocol, key_ref=key_ref,
                             isolated_dir=isolated_dir, binary=binary,
                             skip_test=skip_test, confirm=confirm,
                             request_id=request_id))


@wizard_app.command("present")
def wizard_present():
    """呈现三件套: 能力画像+分配策略+质量档位(单 key 如实标注降级)。"""
    _out(wizard.present(_conn()))


@wizard_app.command("install-skills")
def wizard_install_skills(target_dir: str):
    """技能装入总控会话技能目录(19.3 交付,票 16): 复制内置 10 技能。"""
    _out(wizard.install_skills(_conn(), _ident(), target_dir))



@permission_app.command("pending")
def permission_pending():
    """待裁决权限请求清单(permission_request 事件归一化产物)。"""
    _out(permission.pending(_conn()))


@permission_app.command("allow")
def permission_allow(ruling_id: int, reason: str = "",
                     request_id: str = typer.Option(None, "--request-id")):
    """总控批准(按壳机械执行: 钩子 allow/规则表/大类开关)。"""
    _out(permission.decide(_conn(), _ident(), ruling_id, True, reason,
                           request_id))


@permission_app.command("deny")
def permission_deny(ruling_id: int, reason: str = "",
                    request_id: str = typer.Option(None, "--request-id")):
    """总控拒绝(无头默认=天然拒绝,拒绝即常态)。"""
    _out(permission.decide(_conn(), _ident(), ruling_id, False, reason,
                           request_id))



@plugin_app.command("register")
def plugin_register(name: str, ptype: str, version: str,
                    config: str = typer.Option("{}", "--config"),
                    request_id: str = typer.Option(None, "--request-id")):
    """注册/更新插件(template|view,声明式;config 为 JSON 字符串)。"""
    _out(plugins.register(_conn(), _ident(), name, ptype, version,
                          json.loads(config), request_id))


@plugin_app.command("list")
def plugin_list():
    _out(plugins.list_plugins(_conn()))


@plugin_app.command("enable")
def plugin_enable(name: str,
                  request_id: str = typer.Option(None, "--request-id")):
    _out(plugins.set_enabled(_conn(), _ident(), name, True, request_id))


@plugin_app.command("disable")
def plugin_disable(name: str,
                   request_id: str = typer.Option(None, "--request-id")):
    _out(plugins.set_enabled(_conn(), _ident(), name, False, request_id))


@plugin_app.command("remove")
def plugin_remove(name: str,
                  request_id: str = typer.Option(None, "--request-id")):
    _out(plugins.remove(_conn(), _ident(), name, request_id))


@plugin_app.command("render")
def plugin_render(name: str):
    """渲染模板类插件生成物(模板+参数→目标文件,带版本指纹)。"""
    _out(plugins.render_template_plugin(_conn(), name))


@plugin_app.command("reconcile")
def plugin_reconcile(name: str):
    """对账(21.4 三态指纹): 缺失/旧版→机械重生成;用户改过→不碰+升级。"""
    _out(plugins.reconcile(_conn(), name))


if __name__ == "__main__":
    app()


@dispatch_app.command("cancel")
def dispatch_cancel(dispatch_id: int, reason: str = "",
                    request_id: str = typer.Option(None, "--request-id")):
    """总控取消派单(4.4 配套单派单版): issued/active→cancelled,审计,不动任务。"""
    _out(ops.dispatch_cancel(_conn(), _ident(), dispatch_id, reason, request_id))


# ---------------------------------------------------------------- daemon / web(票 15)

@app.command("init")
def init(home: str = typer.Option("", "--home", help="账本根目录(默认 ~/.tianji)"),
         shell: str = "claude", model: str = "",
         base_url: str = typer.Option("", "--base-url"),
         key_name: str = "主key",
         key: str = typer.Option("", "--key", help="key 本体(只落 home/keys 文件,不进账本)"),
         worker: str = typer.Option("", "--worker", help="顺手注册首个工人实例名"),
         start_daemon: bool = typer.Option(False, "--start-daemon")):
    """一键起步(裸跑即可): 建账+总控身份+settings 文件。

    provider(key/地址/模型)不必现在给——进会话后由总控引导你配;
    已有可用 AI 会话的,直接把它当总控入口(init 输出里有身份指引)。
    """
    _out(wizard.init_bootstrap(home=home, shell=shell, model=model,
                               base_url=base_url, key_name=key_name,
                               key_value=key, worker=worker,
                               start_daemon=start_daemon))


@app.command("console")
def console(print_only: bool = typer.Option(False, "--print",
                                            help="只打印启动命令不执行")):
    """一键开总控会话(claude --settings 一体文件,零手工环境变量)。"""
    import shutil as _sh
    import subprocess as _sp
    from .db import tianji_home
    settings = tianji_home() / "settings-controller.json"
    if not settings.exists():
        _out({"error": f"{settings} 不存在,先跑 tianji init"})
        return
    claude = _sh.which("claude") or "claude"
    cmd = [claude, "--settings", str(settings)]
    if print_only:
        _out({"cmd": " ".join(cmd)})
        return
    if os.name == "nt":  # Windows 的 claude 是 .cmd shim,要走 shell
        _sp.call(" ".join(f'"{c}"' for c in cmd), shell=True)
    else:
        _sp.call(cmd)


@daemon_app.command("start")
def daemon_start(interval: int = typer.Option(30, "--interval",
                                              help="监控器 tick 间隔(秒)"),
                 web_port: int = typer.Option(daemon.WEB_PORT_DEFAULT,
                                              "--web-port",
                                              help="驾驶舱 Web 起始端口(冲突顺延+1)")):
    """拉起 daemon supervisor + 监控器 + 驾驶舱 Web 两常驻(18.2/18.3)。"""
    _out(daemon.daemon_start(interval, web_port))


@daemon_app.command("stop")
def daemon_stop():
    """停止全部常驻(18.3 stop)。"""
    _out(daemon.daemon_stop())


@daemon_app.command("status")
def daemon_status():
    """查看常驻活性(18.3 status)。"""
    _out(daemon.daemon_status())


@daemon_app.command("run")
def daemon_run(interval: int = typer.Option(30, "--interval"),
               web_port: int = typer.Option(daemon.WEB_PORT_DEFAULT,
                                            "--web-port")):
    """supervisor 主循环(由 daemon start 后台拉起,勿手动长期运行)。"""
    daemon.run_daemon(interval, web_port)


@app.command("web")
def web(port: int = typer.Option(daemon.WEB_PORT_DEFAULT, "--port",
                                 help="驾驶舱 Web 端口(冲突顺延+1)")):
    """驾驶舱 Web 常驻(最小只读快照,18.2 常驻之二;页面扩展归票 03)。"""
    from .web import run_web
    run_web(port)
