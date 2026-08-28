"""初始化向导与动态新增(票 14,规格书 13 章): 四步走——收集→测试→呈现→确认生成。

- 收集: 壳+key+模型+隔离目录;支持声明已有实例(跳过);key 只存引用(key_ref),
  key 本体留各壳配置域/文件,不进账本(13.4 开源安全)
- 测试: 当场跑通验证(探测二进制/配置);测不通标"待测试",不入能力画像
- 呈现: 能力画像+分配策略+质量档位三件套;单 key 如实说明"流程能用但质量降级"
- 确认生成: 启动器 launch_cmd(按 13.2 分类法: env 注入型/壳内配置型)+
  隔离配置文件+实例注册+画像建档+应然清单,全进账本注册表,零配置文件
  (账本=单一真源;落盘的设置文件=可再生成物,指纹对账归票 23 管线)
- 动态新增(13.6): 本命令即总控会话一句话入口;驾驶舱抽屉渲染归票 03;
  新增后实例立即可分配(分配器读实例注册表)
"""

import json
import shutil
from pathlib import Path

from . import auth, integrations, ops, shellrender
from .db import now, tx, injected_dir

def _load_shell_entry(conn, shell: str) -> dict:
    """读壳条目(集成注册表优先,shell: 兼容);缺则读内置模板兜底。"""
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"integration_shell:{shell}",)).fetchone()
    if row is not None:
        return json.loads(row["value"])
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"shell:{shell}",)).fetchone()
    if row is not None:
        return json.loads(row["value"])
    # 内置模板兜底
    try:
        from .adapters.template import _BUILTIN, Template
        data = _BUILTIN.get(shell)
        if data:
            return dict(data)
    except (ImportError, AttributeError):
        pass
    return {}

def install_skills(conn, ident, target_dir, request_id=None) -> dict:
    """技能装入总控会话技能目录(19.3 交付,票 16): 复制内置技能包(10 技能)。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("技能安装仅总控身份可执行")
    src = Path(__file__).parent / "skills"
    target = Path(target_dir)
    installed = []
    for d in sorted(src.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            dest = target / d.name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(d / "SKILL.md", dest / "SKILL.md")
            installed.append(d.name)
    ops.audit(conn, "skills_install",
              {"target": str(target), "skills": installed,
               "by": ident["worker_id"]})
    return {"installed": installed, "target": str(target)}

# 壳条目默认(13.2,与内置壳模板一致;向导收集时缺则补建——产物五样之一)
SHELL_ENTRY_DEFAULTS = {
    "claude": {"binding": "env", "protocols": ["anthropic"],
               "isolated_dir_mode": "settings-file",
               "renderer": "claude",
               "ctrl_session": {"protocol": "stream-json",
                                "launch": ["claude"]}},
    "codex": {"binding": "env", "protocols": ["openai"],
              "isolated_dir_mode": "codex-home",
              "renderer": "codex"},
    "kimi": {"binding": "config", "protocols": ["anthropic"],
             "isolated_dir_mode": "workdir-grouping",
             "renderer": "config_binding",
             "ctrl_session": {"protocol": "acp",
                              "launch": ["kimi", "acp"],
                              "data_root_env": "KIMI_CODE_HOME",
                              "provider_env": {
                                  "target": "process_env",
                                  "map": {
                                      "KIMI_MODEL_NAME": "{model}",
                                      "KIMI_MODEL_API_KEY": "{key}",
                                      "KIMI_MODEL_BASE_URL": "{base_url}",
                                      "KIMI_MODEL_PROVIDER_TYPE": "anthropic",
                                  },
                              }}},
    "atomcode": {"binding": "config", "protocols": ["anthropic", "openai"],
                 "isolated_dir_mode": "atomcode-home",
                 "renderer": "config_binding"},
    "cline": {"binding": "config", "protocols": ["openai"],
              "isolated_dir_mode": "data-dir",
              "renderer": "config_binding"},
}

# 机械扫描候选(2026-08-20 用户裁决): init 的壳列表=扫用户机器实际装的,
# 不是模板清单;此时用户还没配 provider、无大模型,必须纯机械(shutil.which,
# 零 LLM/网络)。supported=True 的壳天机有模板可直接注册;False 的如实标注。
# Windows 坑: cline 是 npm 无扩展名 shim,PATHEXT 探不到,补 .cmd 探测。
SHELL_SCAN_PROBES = {
    "claude": ("claude",),
    "codex": ("codex",),
    "kimi": ("kimi",),
    "atomcode": ("atomcode",),
    "cline": ("cline", "cline.cmd"),
    "dsh": ("dsh", "dsh.cmd"),
    "gemini": ("gemini",),
    "aider": ("aider",),
    "cursor": ("cursor", "cursor-agent"),
}

def scan_shells() -> list:
    """机械扫描本机已装 AI 编程 CLI(纯 stdlib,无 LLM/网络)。

    返回 [{name, path, supported}] 按候选表顺序;supported=天机有模板
    (SHELL_ENTRY_DEFAULTS 键),init 只允许选 supported 的壳。
    """
    found = []
    for name, probes in SHELL_SCAN_PROBES.items():
        for probe in probes:
            p = shutil.which(probe)
            if p:
                found.append({"name": name, "path": p,
                              "supported": name in SHELL_ENTRY_DEFAULTS})
                break
    return found

def probe_models(base_url: str, key_value: str, timeout: int = 10):
    """拿 key+地址探测可用模型清单(13.1 探测为主: GET {base_url}/models)。

    两种认证头都试(Authorization: Bearer 与 x-api-key——anthropic 系端点
    认后者,2026-08-21 模拟实测 stepfun step_plan 单 Bearer 探不到)。
    返回模型 id 列表;任何失败(网络/非 200/格式不对/端点无清单接口)
    返回 None,调用方降级为用户手填。这是 init 机械引导里唯一联网的一步
    (2026-08-21 用户批准)。
    """
    import urllib.request
    url = base_url.rstrip("/") + "/models"
    for headers in ({"Authorization": "Bearer " + key_value.strip()},
                    {"x-api-key": key_value.strip()}):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue
        ids = [m["id"] for m in data.get("data", [])
               if isinstance(m, dict) and m.get("id")]
        if ids:
            return ids
    return None

def quality_tiers(conn, shell: str, key_name: str) -> dict:
    """质量四档标注(13.5): 新组合 vs 每个现存实例的配对档位。

    一档 不同key不同壳 > 二档 同壳不同key > 三档 同key不同壳 > 四档 同壳同key。
    """
    tiers = {}
    rows = conn.execute(
        "SELECT name, shell, key_name FROM instances WHERE is_active=1").fetchall()
    for r in rows:
        if r["shell"] != shell and r["key_name"] != key_name:
            tiers[r["name"]] = "一档(不同key不同壳)"
        elif r["shell"] == shell and r["key_name"] != key_name:
            tiers[r["name"]] = "二档(同壳不同key)"
        elif r["shell"] != shell and r["key_name"] == key_name:
            tiers[r["name"]] = "三档(同key不同壳)"
        else:
            tiers[r["name"]] = "四档(同壳同key,自查自审)"
    return tiers

def present(conn) -> dict:
    """呈现三件套(13.1③): 能力画像+分配策略+质量档位;单 key 如实标注降级。"""
    insts = [dict(r) for r in conn.execute(
        "SELECT name, shell, model, key_name FROM instances"
        " WHERE is_active=1").fetchall()]
    keys = [r["key"] for r in conn.execute(
        "SELECT key FROM configs WHERE key LIKE 'key:%'").fetchall()]
    note = ""
    if len(keys) <= 1:
        note = ("单 key 兜底(13.7): 流程能用但质量降级(同 key 多窗=自查自审);"
                "硬约束不放松(同任务内实施者≠审核者)")
    return {
        "instances": insts,
        "keys": [k[4:] for k in keys],
        "allocation": "硬过滤(窗口/权限粒度)→软排序(表现分+擅长面)→总控评估(可选)",
        "single_key_note": note,
    }

def _generate_launcher(conn, name, shell, model, key_name,
                       isolated_dir):
    """启动器生成(13.3/票34): 分发到壳渲染器注册表(shellrender.RENDERERS)
    ——通用控制流零壳名分支;凭据/供应商解析在渲染层统一走集成注册表
    (旧 key: 条目只读兜底)。返回 (launch_cmd, [生成物路径])。"""
    return shellrender.render(conn, shell, instance=name, model=model,
                              key_name=key_name, isolated_dir=isolated_dir)

def _require_integration_registry(conn, shell, key_name, base_url="",
                                  protocol="", key_ref=""):
    """装配前置校验(13.8): 凭据路径缺注册表条目且已给数据不足以显式
    登记时 fail-fast 指路;壳条目可由内置模板迁入,不在此拦。
    """
    return integrations.require_resolvable_credential(
        conn, key_name, base_url=base_url, protocol=protocol,
        key_ref=key_ref)

def add_instance(conn, ident, name, shell, model, key_name="",
                 base_url="", protocol="", key_ref="", isolated_dir="",
                 binary="", skip_test=False, confirm=False,
                 role_note="", request_id=None) -> dict:
    """向导四步走(13.1): 收集→测试→呈现→确认生成(confirm=True 才落注册)。

    测试=探测二进制/隔离配置(binary 给定时须能在 PATH/路径找到);
    测不通标"待测试",不注册不入画像(13.1② 先测能力再入画像)。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("向导新增实例仅总控身份可执行")
    # 壳条目读 binding 字段(6.2 data-driven): env/config 为合法值
    entry = _load_shell_entry(conn, shell)
    if entry.get("binding") not in ("env", "config"):
        raise ValueError(f"未知壳 {shell}(新壳先走 new-shell-onboarding 八问检查单)")

    # 票59:未给 key_name 且存在池时,默认进第一个池
    pool_default_note = ""
    if not key_name:
        pools = integrations._pool_list_conn(conn)
        if pools:
            key_name = pools[0]["name"]
            pool_default_note = f"未指定 key,默认进池 {key_name}"

    is_pool = bool(key_name) and integrations._is_pool_name(conn, key_name)
    # 票59:直绑路径加"不推荐"提示
    if key_name and not is_pool:
        pool_default_note = f"直绑 {key_name}(不推荐:建议通过池分配)"

    # 幂等(3.3): 各子操作各自单事务,本函数不包大事务(防 BEGIN 嵌套)
    if request_id:
        rc = conn.execute("SELECT result FROM receipts WHERE request_id=?",
                          (request_id,)).fetchone()
        if rc is not None:
            return {"replay": True, **json.loads(rc["result"])}
    # ① 收集: 壳条目缺则按内置模板默认补建(产物五样之一);key 条目同理
    srow = conn.execute("SELECT value FROM configs WHERE key=?",
                        (f"shell:{shell}",)).fetchone()
    if srow is None:
        ops.config_set(conn, ident, f"shell:{shell}",
                       json.dumps(SHELL_ENTRY_DEFAULTS[shell],
                                  ensure_ascii=False),
                       request_id=f"{request_id}-shell" if
                       request_id else None)
    if key_name and not is_pool:
        krow = conn.execute("SELECT value FROM configs WHERE key=?",
                            (f"key:{key_name}",)).fetchone()
        if krow is None:
            if not base_url:
                raise ValueError(f"key 条目 {key_name} 不存在且未给 base_url")
            ops.config_set(conn, ident, f"key:{key_name}", json.dumps({
                "base_url": base_url,
                "models": [integrations.model_entry({"id": model,
                                                     "display": model})],
                "protocol": protocol or "openai",
                "key_ref": key_ref or None,
                "coding_plan": False}, ensure_ascii=False),
                request_id=f"{request_id}-key" if request_id
                else None)
    # ①' 装配校验(13.8): 凭据路径缺注册表条目且数据不足以显式登记→指路,
    # 不静默拼装一次性混合配置(免 key 实例不要求凭据条目)
    _require_integration_registry(conn, shell, key_name, base_url=base_url,
                                  protocol=protocol, key_ref=key_ref)
    # ② 测试: 当场跑通验证
    test_note = "skip-test 声明跳过" if skip_test else "通过"
    if not skip_test:
        probe = binary or shell
        if shutil.which(probe) is None and not Path(probe).exists():
            ops.audit(conn, "wizard_add", {
                "name": name, "shell": shell, "model": model,
                "status": "待测试", "reason": f"探测不到 {probe}",
                "by": ident["worker_id"]})
            return {"name": name, "status": "待测试",
                    "reason": f"探测不到 {probe}", "registered": False}
    # ③ 呈现: 质量档位(配对现存实例)
    tiers = quality_tiers(conn, shell, key_name)
    if not confirm:
        resp = {"name": name, "status": "待确认",
                "quality_tiers": tiers, "present": present(conn),
                "registered": False}
        if pool_default_note:
            resp["pool_note"] = pool_default_note
        return resp
    # ④ 确认生成: 集成条目显式增量登记(13.8)+启动器+隔离配置+注册+画像+应然清单
    srow = conn.execute("SELECT value FROM configs WHERE key=?",
                        (f"shell:{shell}",)).fetchone()
    integrations.ensure_instance_entries(
        conn, ident, shell,
        shell_entry=json.loads(srow["value"]) if srow else None,
        shell_source="legacy" if srow else "template",
        key_name=key_name, base_url=base_url, protocol=protocol,
        key_ref=key_ref, request_id=request_id)
    launch_cmd, artifacts = _generate_launcher(
        conn, name, shell, model, key_name, isolated_dir)
    r = ops.instance_register(
        conn, name, shell, model, isolated_dir=isolated_dir,
        launch_cmd=launch_cmd, key_name=key_name,
        profile_notes=f"向导注册({now()});测试: {test_note}"
                      + (f";拟定角色: {role_note}" if role_note else ""))
    ops.config_set(conn, ident, f"expected:{name}", json.dumps({
        "shell": shell, "launch_cmd": launch_cmd,
        "artifacts": artifacts,
        "hooks": "内置壳模板钩子(票 08/09)"}, ensure_ascii=False),
        request_id=f"{request_id}-exp" if request_id else None)
    ops.audit(conn, "wizard_add", {
        "name": name, "shell": shell, "model": model,
        "key_name": key_name, "status": "已注册",
        "launch_cmd": launch_cmd, "artifacts": artifacts,
        "quality_tiers": tiers, "by": ident["worker_id"],
        "pool_note": pool_default_note})
    result = {"name": name, "status": "已注册",
              "launch_cmd": launch_cmd, "artifacts": artifacts,
              "quality_tiers": tiers,
              "secret_note": r["note"], "registered": True}
    if pool_default_note:
        result["pool_note"] = pool_default_note
    if request_id:
        conn.execute(
            "INSERT INTO receipts (request_id, operation, result) VALUES (?,?,?)",
            (request_id, "wizard_add",
             json.dumps(result, ensure_ascii=False)))
    return result

# ---------------------------------------------------------------- 一键起步(18.6 首次运行)

def _write_controller_settings(home_p: Path, home: str, shell: str, secret: str,
                               provider: dict = None, ready: bool = False,
                               cards: list = None) -> str:
    """settings-controller.json 一体文件: 总控会话配置(壳条目 data-driven)。

    通用结构: env + 按壳条目 controller_settings 决定的 ctrl_session / appendSystemPrompt / permissions。
    provider 凭据映射按 provider_env.map 自动生成(E.2)。
    """
    intro = (
        "你是天机(Tianji)的总控——一个把多个 AI 编程助手编排成协作框架的工具,"
        f"账本在 {home},你的身份已在环境变量里(TIANJI_WORKER_ID=总控,TIANJI_SECRET)。")
    plain_talk = (
        "对用户说话一律大白话,不出现天机内部术语(壳/shell/key 条目/wizard/"
        "provider/账本/派单/票据等);要提就换人话: 壳=AI 助手,wizard=配置向导,"
        "provider=模型服务,账本=天机的记录文件。"
        "用户只需提供人话信息(助手名/服务商/key),登记配置的事你自己做。"
        "天机的机制命令(派单/审核/任务流转)是你的内部操作,不要教用户手敲。")
    controller_discipline = (
        "总控纪律(票 41,必须遵守): ①收下任务后,先把执行分工规划摆给用户看"
        "——逐个列清谁干什么(如: 实施1 改代码/审核者审/总控验收),用户点头"
        "后才建任务派单;②你只做管理(拆活/定计划/派单/监督/验收/裁决),"
        "不亲自写实现代码;有可派的工人实例时,实施必派单给工人。")
    role_text = intro + plain_talk + controller_discipline

    # 读壳条目(6.2 data-driven): 集成注册表优先,内置模板兜底;未知壳写最小 env
    from .adapters.template import get_template
    try:
        tpl = get_template(shell)
        cs = tpl.controller_settings or {}
        prov = tpl.provider_env or {}
    except KeyError:
        cs, prov = {}, {}

    # 通用 env
    env = {"TIANJI_HOME": home, "TIANJI_WORKER_ID": "总控",
           "TIANJI_SECRET": secret, "PYTHONIOENCODING": "utf-8"}
    # provider 凭据注入(按 provider_env.map 模板)
    if provider:
        fmt_ctx = {"key": provider.get("key_value", "").strip(),
                   "base_url": provider.get("base_url", ""),
                   "model": provider.get("model", "")}
        for env_key, tpl_str in prov.get("map", {}).items():
            try:
                val = tpl_str.format(**fmt_ctx)
            except KeyError:
                val = ""
            if val:
                env[env_key] = val

    doc = {"env": env}

    # ctrl_session 块
    ctrl_cs = cs.get("ctrl_session")
    if ctrl_cs:
        doc["ctrl_session"] = dict(ctrl_cs)
        # role_text_target 决定角色话术放在哪个字段
        rtt = cs.get("role_text_target", "")
        if rtt.startswith("ctrl_session."):
            key = rtt[len("ctrl_session."):]
            doc["ctrl_session"][key] = role_text
        # provider_env 写进 ctrl_session(target=process_env 的壳在运行时用)
        if prov.get("target") == "process_env":
            doc["ctrl_session"]["provider_env"] = prov
        # role_text_target = appendSystemPrompt 的壳(claude)在下面统一处理

    # role_text 在 appendSystemPrompt (claude stream-json)
    if cs.get("role_text_target") == "appendSystemPrompt":
        sp_text = role_text
        if ready or provider:
            worker_cards = [c for c in (cards or [])
                            if not c.get("is_controller_card")]
            if worker_cards:
                roster = ";".join(
                    "%s + %s(%s)" % (
                        c["shell"], c["model"],
                        ("key 名 %s" % c["key_name"])
                        if c.get("source") == "key" else "免 key")
                    for c in worker_cards)
                sp_text = (
                    intro +
                    "你的模型已就绪。用户的牌已在配置页盘点好、key 也已落地,"
                    "但角色分工还没定。跟他敲定分工(这是商量活,根据他的牌给建议,"
                    "他调整或确认): 牌面——" + roster + "。"
                    "分工要求: 总控=你,兼架构师和裁判(拆活/定计划/裁决分歧,不用单配);"
                    "审核是双轴交叉把关,要两个不同实例、最好不同源的模型——只配一个"
                    "=自查自审,质量降级要如实说;实施一个起步,同配置可多开几个。"
                    "摆分工清单时一行一个实例(角色/助手/模型/实例名都写全),"
                    "不合并、不省略,让他一眼看到完整编制。"
                    "敲定后你用 tianji wizard add <实例名> <助手名> <模型> "
                    "--key-name <key名> --confirm 逐个注册(免 key 的不带 --key-name;"
                    "多开就按个数多注册几个不同实例名),全部注册完告诉他编制齐了。"
                    "没敲定分工前不建任务。"
                    "他要加牌面之外的助手(天机没模板的,比如 dsh): 如实告诉他这个助手"
                    "天机暂不支持、接入是另一笔活(要走新壳检查单),别现场翻代码开搞,"
                    "先拿现有的牌把分工定了。"
                    "他明确指定的分工,照办,不要反驳、不要另推方案——有障碍就点明"
                    "需要做什么(比如'这个助手要先走接入流程,要不要现在配'),"
                    "让他确认,选择权在他。"
                    "命令用法一律用 tianji <命令> --help 现查,不要翻仓库源码学用法"
                    "(慢且烧 token)。" +
                    role_text)
            else:
                sp_text = (
                    intro +
                    "provider(模型服务)已经配好,可以正常工作: 用户有活就接,"
                    "按天机流程走。先跟用户打个招呼、一句话报一下当前编制。"
                    "他要加助手/加模型,让他去 web 配置页点选补配"
                    "(驾驶舱顶部'配置'按钮,或 tianji start 会自动打开)。" +
                    role_text)
        else:
            sp_text = (
                intro +
                "provider(模型服务)还没配齐。配置在 web 配置页点选完成,"
                "不要用对话引导他配置——让他打开配置页(驾驶舱顶部'配置'按钮,"
                "或重跑 tianji start 自动打开)接着配,配齐前不建任务。" +
                role_text)
        doc["appendSystemPrompt"] = sp_text
    elif not ctrl_cs:
        # 没有 ctrl_session 也没有 appendSystemPrompt 目标 → 兜底放 appendSystemPrompt
        doc["appendSystemPrompt"] = role_text

    # permissions (claude 权限位)
    perms = cs.get("permissions")
    if perms:
        doc["permissions"] = perms

    settings = injected_dir() / "settings-controller.json"
    settings.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return str(settings)

def land_cards(conn, home_p: Path, ident: dict, cards: list) -> dict:
    """模型牌落地(web 配置页/机械引导的产物): key 文件/条目 upsert
    (同一把 key 的不同模型合并进清单)+ 总控牌就地更新(票 28 通道,不重建)。

    key 本体只落 home/keys 文件不进账本(13.4)。
    工人实例注册不进本函数(角色分工由 web 配置页/总控会话敲定后,
    经 add_instance 逐个注册)。
    """
    landed, errors = [], []
    for card in cards:
        shell, model = card["shell"], card["model"]
        # 壳条目缺则按内置模板补建(产物五样之一;访谈只收有模板的壳)
        if conn.execute("SELECT 1 FROM configs WHERE key=?",
                        (f"shell:{shell}",)).fetchone() is None:
            conn.execute(
                "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?)",
                (f"shell:{shell}", json.dumps(SHELL_ENTRY_DEFAULTS[shell],
                                              ensure_ascii=False), now()))
        key_name = card.get("key_name", "")
        if card.get("source") == "key" and key_name:
            kdir = injected_dir()
            kdir.mkdir(exist_ok=True)
            kfile = kdir / f"{key_name}.key"
            kfile.write_text(card["key_value"].strip(), encoding="utf-8")
            protocol = integrations.normalize_legacy_protocol(
                card.get("protocol")
                or SHELL_ENTRY_DEFAULTS.get(
                    shell, {}).get("protocols", ["openai"])[0])
            krow = conn.execute("SELECT value FROM configs WHERE key=?",
                                (f"key:{key_name}",)).fetchone()
            models = []
            if krow is not None:
                models = json.loads(krow["value"]).get("models", [])
            if model and all((m or {}).get("id") != model for m in models):
                models.append(integrations.model_entry(
                    {"id": model, "display": model}))
            conn.execute(
                "INSERT OR REPLACE INTO configs (key, value, updated_at)"
                " VALUES (?,?,?)",
                (f"key:{key_name}", json.dumps({
                    "base_url": card["base_url"], "models": models,
                    "protocol": protocol, "key_ref": str(kfile),
                    "coding_plan": False}, ensure_ascii=False), now()))
            landed.append({"kind": "key", "key_name": key_name,
                           "shell": shell, "model": model})
        if card.get("is_controller_card"):
            upd = {"shell": shell, "model": model}
            if key_name:
                upd["key_name"] = key_name
            # request_id 按内容派生: 总控牌可反复改(web 配置页点选),
            # 同内容重放幂等,换内容照改(旧固定 id 会把后续改动吞掉)
            ops.instance_update(conn, ident, "总控",
                                request_id=f"land-ctrl-{shell}/{model}/{key_name}",
                                **upd)
            landed.append({"kind": "总控", "shell": shell, "model": model})
    return {"landed": landed, "errors": errors}

def init_bootstrap(home: str = "", shell: str = "claude", model: str = "",
                   base_url: str = "", key_name: str = "主key",
                   key_value: str = "", worker: str = "",
                   start_daemon: bool = False, work_dir: str = "") -> dict:
    """一键起步(tianji start 的建账内核): 裸跑即可,从空目录到可用总控。

    设计口径: 启动命令必须简洁,key/地址/模型不准塞命令行(2026-08-20);
    配置(选总控/配实例/定角色)全在 web 配置页纯点选,终端零问答
    (2026-08-21 用户裁决: 会话引导慢、烧 token、会瞎猜);
    用户已有可用会话的,直接当总控入口(settings 只带 TIANJI_* env)。

    产出: TIANJI_HOME 账本 + 总控实例与 controller 身份 + ctrl-secret.txt
    + settings-controller.json(身份 env 必备;provider env 仅在给了
    key/base_url 时写入)+ 可选首个工人 + 可选起 daemon。
    重复执行幂等: 总控已注册则不轮换 secret(从 ctrl-secret.txt 读回),
    补给了 key/base_url 就就地更新 settings 与 key 条目。
    """
    import os
    if not home:
        home = os.environ.get("TIANJI_HOME") or str(Path.home() / ".tianji")
    home = str(Path(home).resolve())
    os.environ["TIANJI_HOME"] = home  # connect() 按 env 派生账本路径
    Path(home).mkdir(parents=True, exist_ok=True)
    from .db import connect
    conn = connect()
    ops.ensure_defaults(conn)
    out = {"home": home, "steps": []}
    home_p = Path(home)

    # ⑥ 工作目录(18.1/18.6): start 引导显式给 → 直接记(不要求 git 仓库);
    # 未给(web setup 等调用方)→ 回退 git 仓库根检测(票 39 行为)
    dpd = ""
    if work_dir:
        dpd = str(Path(work_dir).resolve())
        out["steps"].append(f"工作目录: {dpd}")
    else:
        cwd = str(Path.cwd().resolve())
        if cwd != home and ops._is_git_repo(cwd):
            dpd = cwd
            out["steps"].append(f"默认工作目录: {cwd}")
    if dpd:
        conn.execute(
            "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            ("default_project_dir", dpd, now()))

    # ① 总控注册+controller 身份(已注册→跳过,secret 从文件读回不轮换)
    secret_file = injected_dir() / "ctrl-secret.txt"
    row = conn.execute(
        "SELECT is_active FROM instances WHERE name='总控'").fetchone()
    if row and row["is_active"]:
        if secret_file.exists():
            secret = secret_file.read_text(encoding="utf-8").strip()
            out["steps"].append("总控已注册,跳过(secret 不轮换)")
        else:
            # secret 文件丢了: 走恢复通道轮换(11.4)
            r = ops.controller_recover(conn, "总控")
            secret = r["secret"]
            out["steps"].append("总控已注册,secret 文件缺失已走恢复通道轮换")
    else:
        # bootstrap 首次注册允许无身份(11.4 信任根=本机操作者)
        r = ops.instance_register(conn, "总控", shell, model or "未配置",
                                  controller=True)
        secret = r["secret"]
        out["steps"].append("总控已注册(controller 身份已绑)")
    secret_file.write_text(secret, encoding="utf-8")

    # ①½ 出厂集成注册表(13.8): 协议+常见供应商大类入库(幂等不覆盖)
    from . import integrations as _integrations
    _integrations.ensure_builtin_registry(
        conn, {"worker_id": "总控", "secret": secret},
        request_id=f"init-builtin-{now()}")

    # ② key/provider(可选,给了才写;key 本体只落 home/keys 文件不进账本)
    provider_configured = bool(key_value and base_url)
    if key_value:
        kdir = injected_dir()
        kdir.mkdir(exist_ok=True)
        kfile = kdir / f"{key_name}.key"
        kfile.write_text(key_value.strip(), encoding="utf-8")
        key_ref = str(kfile)
        # 协议从壳条目取首个(6.2 data-driven, init 的 shell 必在 SHELL_ENTRY_DEFAULTS)
        shell_defaults = SHELL_ENTRY_DEFAULTS.get(shell, {})
        protocol = (shell_defaults.get("protocols") or ["openai"])[0]
        conn.execute(
            "INSERT OR REPLACE INTO configs (key, value, updated_at)"
            " VALUES (?,?,?)",
            (f"key:{key_name}", json.dumps({
                "base_url": base_url,
                "models": [integrations.model_entry({"id": model, "display": model})]
                    if model else [],
                "protocol": protocol, "key_ref": key_ref,
                "coding_plan": False}, ensure_ascii=False), now()))
        out["steps"].append(f"key 条目 {key_name} 已建/更新")
        # 壳条目补建(6.1): 缺就按出厂模板登记
        skey = f"shell:{shell}"
        if conn.execute(
                f"SELECT 1 FROM configs WHERE key=?", (skey,)).fetchone() is None:
            entry = dict(SHELL_ENTRY_DEFAULTS.get(shell, {"binding": "env",
                                                           "protocols": ["openai"],
                                                           "isolated_dir_mode": "workdir-grouping"}))
            entry["renderer"] = shell_defaults.get("renderer", "config_binding")
            conn.execute(
                "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?)",
                (skey, json.dumps(entry, ensure_ascii=False), now()))
        # 总控实例补上模型/key 引用(票 28 就地改,不重建)
        ident = {"worker_id": "总控", "secret": secret}
        upd = {}
        if model:
            upd["model"] = model
        if key_name:
            upd["key_name"] = key_name
        if upd:
            ops.instance_update(conn, ident, "总控", request_id="init-ctrl-upd",
                                **upd)

    # ③ settings-controller.json 一体文件(身份 env 必备;provider env 仅配置后写入)
    provider = ({"key_value": key_value, "base_url": base_url, "model": model}
                if provider_configured else None)
    out["settings"] = _write_controller_settings(home_p, home, shell, secret,
                                                 provider=provider)
    out["provider_configured"] = provider_configured

    # ④ 可选首个工人(需要 provider 已配;同壳同 key=四档自查自审如实标注)
    if worker and provider_configured:
        ident = {"worker_id": "总控", "secret": secret}
        wiso = home_p / "instances" / f"{worker}-{shell}"
        r2 = add_instance(conn, ident, worker, shell, model,
                          key_name=key_name, isolated_dir=str(wiso),
                          skip_test=True, confirm=True,
                          request_id=f"init-worker-{worker}")
        out["worker"] = r2
        out["steps"].append(
            f"工人 {worker} 已注册(同壳同 key=质量四档,审核质量降级,如实标注)")

    # ⑤ 可选起 daemon(env 已带身份,web 审批可用)
    if start_daemon:
        os.environ["TIANJI_WORKER_ID"] = "总控"
        os.environ["TIANJI_SECRET"] = secret
        from . import daemon
        d = daemon.daemon_start()
        out["daemon"] = d
        out["steps"].append(f"daemon 已起(web: http://127.0.0.1:{d['web_port']})")

    if provider_configured:
        out["next"] = ("开总控会话: tianji console(或 claude --settings "
                       f"{out['settings']})")
    else:
        out["next"] = ("去 web 配置页选总控助手、配模型(tianji start 会"
                       "自动打开;驾驶舱顶部也有'配置'按钮)")
    conn.close()
    return out
