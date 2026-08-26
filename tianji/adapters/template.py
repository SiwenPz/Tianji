"""壳模板引擎(6.2/6.4): 模板定义+翻译+注册表。

每壳=模板+实例参数,渲染生成薄翻译脚本。模板与具体壳解耦,新壳可插。
模板为 Python 字典(避免 pyproject.toml package_data 打包问题)。
"""

from __future__ import annotations

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Template:
    """壳模板(不可变): 翻译钩子载荷→统一事件 JSON。"""

    name: str
    version: str
    hook_map: dict[str, str]
    session_id_keys: list[str]
    payload_exclude_keys: list[str]
    interrupt: dict
    thinking_level_map: dict | None = None
    transcript: dict = field(default_factory=dict)
    sandbox_allowlist: list = field(default_factory=list)
    completion: dict = field(default_factory=dict)
    permission_slot: dict | None = None
    resume: dict | None = None
    renderer: str = ""
    binding: str = ""
    protocols: list = field(default_factory=list)
    isolated_dir_mode: str = ""
    adapter: dict = field(default_factory=dict)
    hooks: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    controller_settings: dict = field(default_factory=dict)
    provider_env: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Template":
        _check_required(data)
        return cls(
            name=data["name"],
            version=data.get("version", "v1"),
            hook_map=data["hook_map"],
            session_id_keys=data["session_id_keys"],
            payload_exclude_keys=data["payload_exclude_keys"],
            interrupt=data["interrupt"],
            thinking_level_map=data.get("thinking_level_map"),
            transcript=data.get("transcript") or {},
            sandbox_allowlist=data.get("sandbox_allowlist") or [],
            completion=data.get("completion") or {},
            permission_slot=data.get("permission_slot"),
            resume=data.get("resume"),
            renderer=data.get("renderer", ""),
            binding=data.get("binding", ""),
            protocols=data.get("protocols", []),
            isolated_dir_mode=data.get("isolated_dir_mode", ""),
            adapter=data.get("adapter") or {},
            hooks=data.get("hooks") or {},
            capabilities=data.get("capabilities") or {},
            controller_settings=data.get("controller_settings") or {},
            provider_env=data.get("provider_env") or {},
        )


# ---------------------------------------------------------------------------
# 内置模板: claude
# ---------------------------------------------------------------------------

TEMPLATE_CLAUDE: dict = {
    "name": "claude",
    "version": "v1",
    "hook_map": {
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "Stop": "stop",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PermissionRequest": "permission_request",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
    },
    "session_id_keys": ["session_id"],
    "payload_exclude_keys": [
        "session_id", "hook_event_name",
    ],
    "interrupt": {
        "stop_event": "stop",
        "reason_field": "stop_reason",
        "interrupt_reasons": {"interrupt", "cancelled", "other"},
        "interrupt_on_empty": True,
    },
    "thinking_level_map": None,
    "renderer": "claude",
    "binding": "env",
    "protocols": ["anthropic"],
    "isolated_dir_mode": "settings-file",
    "transcript": {
        "roots": [{"type": "home", "subpath": ".claude"}],
        "glob": ["projects/*/{session_id}.jsonl"],
        "source_type": "jsonl",
        "authoritative_source": None,
    },
    "sandbox_allowlist": [],
    "completion": {
        "session_end_event": "session_end",
        "stop_is_completion": False,
    },
    "resume": {
        "cmd": "claude -c -p",
        "prompt": "继续任务书: {task_path}",
    },
    # 6.6 权限位: 钩子 allow 类(claude/codex/atomcode)放行三路之一
    "permission_slot": {
        "mechanism": "hook_allow",
        "hook_response_format": "cc",
        "hook_action": "deny",
        "headless": {"policy": "deny", "fallback": "hook_deny"},
    },
    # 总控 settings 写入
    "controller_settings": {
        "role_text_target": "appendSystemPrompt",
        "permissions": {"allow": [
            "Bash(python -m tianji:*)",
            "Bash(python -m tianji)",
            "Bash(tianji:*)",
        ]},
        "ctrl_session": {
            "protocol": "stream-json",
            "launch": ["claude"],
        },
    },
    "provider_env": {
        "target": "settings_env",
        "map": {
            "ANTHROPIC_AUTH_TOKEN": "{key}",
            "ANTHROPIC_BASE_URL": "{base_url}",
            "ANTHROPIC_MODEL": "{model}",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "{model}",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "{model}",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "{model}",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "{model}",
        },
    },
    # 适配器
    "adapter": {
        "template": "generic",
        "output_file": "tianji_claude_hook.py",
    },
    # 钩子清单
    "hooks": {
        "manifest_merge": "deep_merge_managed",
        "managed_keys": ["tianji"],
    },
    # 能力声明
    "capabilities": {
        "statusline": {
            "enabled": True,
            "template": "claude_statusline",
            "output_file": "tianji_statusline.py",
        },
        "quota_sources": ["statusline", "transcript", "ccswitch"],
        "tier3_process_alive": False,
    },
}

# ---------------------------------------------------------------------------
# 内置模板: codex (0.146.1 实证)
# ---------------------------------------------------------------------------

TEMPLATE_CODEX: dict = {
    "name": "codex",
    "version": "v1",
    "hook_map": {
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "Stop": "stop",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PermissionRequest": "permission_request",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
    },
    "session_id_keys": [
        "session_id", "conversation_id", "sessionId",
    ],
    "payload_exclude_keys": [
        "session_id", "conversation_id", "sessionId",
        "hook_event_name", "event", "type",
    ],
    "interrupt": {
        "stop_event": "stop",
        "reason_field": None,
        "interrupt_reasons": set(),
        "interrupt_on_empty": False,
    },
    "thinking_level_map": {
        "low": {"config_key": "model_reasoning_effort", "value": "low"},
        "medium": {"config_key": "model_reasoning_effort", "value": "medium"},
        "high": {"config_key": "model_reasoning_effort", "value": "high"},
    },
    "renderer": "codex",
    "binding": "env",
    "protocols": ["openai"],
    "isolated_dir_mode": "codex-home",
    "transcript": {
        "roots": [{"type": "home", "subpath": ".codex"}],
        "glob": ["sessions/**/rollout-{session_id}.jsonl"],
        "source_type": "jsonl",
        "authoritative_source": None,
    },
    "sandbox_allowlist": [],
    "completion": {
        "session_end_event": "session_end",
        "stop_is_completion": False,
    },
    # 无头续跑原语暂未实证,codex 不支持无头续跑(返回 supported=False)
    # 6.6 权限位: 钩子 allow 类(codex/atomcode)放行三路之一
    "permission_slot": {
        "mechanism": "hook_allow",
        "hook_response_format": "bare",
        "hook_action": "deny",
        "headless": {"policy": "deny", "fallback": "hook_deny"},
    },
    # 总控 settings 写入: codex 无 ctrl_session(走 env)
    "controller_settings": {
        "role_text_target": "ctrl_session",
        "permissions": {},
    },
    "provider_env": {
        "target": "process_env",
        "map": {
            "CODEX_WIZARD_KEY": "${key}",
        },
    },
    # 适配器
    "adapter": {
        "template": "generic",
        "output_file": "tianji_codex_hook.py",
    },
    # 钩子清单
    "hooks": {
        "manifest_merge": "deep_merge_managed",
        "managed_keys": ["tianji"],
    },
    # 能力声明
    "capabilities": {
        "statusline": {"enabled": False},
        "quota_sources": ["transcript"],
        "tier3_process_alive": True,
    },
}

# ---------------------------------------------------------------------------
# 内置模板: dsh (素材: tianji-design-r2 票 11 Comments)
# ---------------------------------------------------------------------------

TEMPLATE_DSH: dict = {
    "name": "dsh",
    "version": "v1",
    "hook_map": {
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "Stop": "stop",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PermissionRequest": "permission_request",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
    },
    "session_id_keys": ["session_id"],
    "payload_exclude_keys": [
        "session_id", "hook_event_name",
    ],
    "interrupt": {
        "stop_event": "stop",
        "reason_field": "exit_reason",
        "interrupt_reasons": {"interrupt", "cancelled"},
        "interrupt_on_empty": False,
    },
    "thinking_level_map": {
        "low": {"param": "--patch", "value": "low"},
        "medium": {"param": "--patch", "value": "medium"},
        "high": {"param": "--patch", "value": "high"},
    },
    "renderer": "config_binding",
    "binding": "config",
    "protocols": ["anthropic"],
    "isolated_dir_mode": "env-isolated",
    "transcript": {
        "roots": [
            {"type": "env", "name": "DSH_HOME"},
            {"type": "home", "subpath": ".dsh"},
        ],
        "glob": [
            "sessions/*/{session_id}/session.jsonl.zstd",
            "sessions/*/{session_id}/session.jsonl",
        ],
        "source_type": "zstd",
        "authoritative_source": None,
    },
    "sandbox_allowlist": ["%TIANJI_HOME%"],
    "completion": {
        "session_end_event": None,
        "stop_is_completion": True,
    },
    "resume": {
        "cmd": "dsh --profile headless --resume {session_id}",
        "prompt": "继续任务书: {task_path}",
    },
    "permission_slot": {
        "mechanism": "auto_approve",
        "hook_action": "cancel",
        "rules": {"file": "autoapprove.json", "format": "autoapprove_v1"},
        "headless": {"policy": "suspend", "fallback": "timeout_kill",
                     "timeout_seconds": {"config": "permission_cline_timeout", "default": 120}},
    },
    "controller_settings": {
        "role_text_target": "ctrl_session",
        "permissions": {},
    },
    "provider_env": {},
    "adapter": {
        "template": "generic",
        "output_file": "tianji_dsh_hook.py",
    },
    "hooks": {
        "manifest_merge": "standalone",
        "managed_keys": ["tianji"],
    },
    "capabilities": {
        "statusline": {"enabled": False},
        "quota_sources": ["transcript"],
        "tier3_process_alive": False,
    },
}


# ---------------------------------------------------------------------------
# 内置模板: kimi (素材: 票 09 / 规格 6.4 / 6.6)
# ---------------------------------------------------------------------------

# kimi 钩子事件名(20 个 → 8 类公共交集)
# 档 1 hooks Beta 为主;档 2 wire.jsonl(协议 1.5)作权威校验源
# 完成判定: SessionEnd / Stop
# 权限位: 规则表(kimi 钩子只能 deny,放行靠规则表)
TEMPLATE_KIMI: dict = {
    "name": "kimi",
    "version": "v1",
    "hook_map": {
        # 8 类公共交集(kimi 钩子 20 事件归一化映射)
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "Stop": "stop",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PermissionRequest": "permission_request",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
    },
    "session_id_keys": ["session_id", "sessionId"],
    "payload_exclude_keys": [
        "session_id", "sessionId",
        "hook_event_name", "event", "type",
    ],
    "interrupt": {
        "stop_event": "stop",
        "reason_field": "stop_reason",
        "interrupt_reasons": {"interrupt", "cancelled"},
        "interrupt_on_empty": True,
    },
    "thinking_level_map": None,  # kimi 暂无思考级别映射
    "renderer": "config_binding",
    "binding": "config",
    "protocols": ["anthropic"],
    "isolated_dir_mode": "workdir-grouping",
    "transcript": {
        # 档 1 = kimi hooks Beta;档 2 = wire.jsonl 权威校验源
        "roots": [
            {"type": "env", "name": "KIMI_HOME"},
            {"type": "home", "subpath": ".kimi"},
        ],
        "glob": [
            "wire.jsonl",
            "wire/wire.jsonl",
            "wire/wire-{session_id}.jsonl",
            "wire-{session_id}.jsonl",
        ],
        "source_type": "jsonl",
        "authoritative_source": "wire",
    },
    "sandbox_allowlist": [],
    # 权限位: 规则表(kimi 钩子只能 deny,放行靠规则表,6.6)
    "permission_slot": {
        "mechanism": "rule_table",
        "hook_action": "deny",
        "rules": {"file": "permission-rules.json",
                   "format": "allow_deny_lists_v1"},
        "headless": {"policy": "deny", "fallback": "static_rules_deny"},
    },
    # 总控 settings 写入
    "controller_settings": {
        "role_text_target": "ctrl_session.role_text",
        "permissions": {},
        "ctrl_session": {
            "protocol": "acp",
            "launch": ["kimi", "acp"],
            "data_root_env": "KIMI_CODE_HOME",
        },
    },
    "provider_env": {
        "target": "process_env",
        "map": {
            "KIMI_MODEL_NAME": "{model}",
            "KIMI_MODEL_API_KEY": "${key}",
            "KIMI_MODEL_BASE_URL": "{base_url}",
            "KIMI_MODEL_PROVIDER_TYPE": "{protocol}",
        },
    },
    # 适配器
    "adapter": {
        "template": "generic",
        "output_file": "tianji_kimi_hook.py",
    },
    # 钩子清单
    "hooks": {
        "manifest_merge": "deep_merge_managed",
        "managed_keys": ["tianji"],
    },
    # 能力声明
    "capabilities": {
        "statusline": {"enabled": False},
        "quota_sources": ["transcript", "ccswitch"],
        "tier3_process_alive": False,
    },
    # 完成判定
    "completion": {
        # 完成判定: SessionEnd / Stop
        "session_end_event": "session_end",
        "stop_is_completion": True,
    },
}


# ---------------------------------------------------------------------------
# 内置模板: atomcode (素材: 票 09 / 规格 6.4)
# ---------------------------------------------------------------------------

# atomcode 钩子三套形态(TOML ScriptHook / Webhook / CC 兼容)
# 完成判定: session_end
# 档 2: sessions/<hex>/<uuid>.jsonl 按轮追加解析
# 多实例: ATOMCODE_HOME 隔离
TEMPLATE_ATOMCODE: dict = {
    "name": "atomcode",
    "version": "v1",
    "hook_map": {
        # 8 类公共交集(atomcode 三套钩子统一映射)
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "Stop": "stop",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PermissionRequest": "permission_request",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
    },
    "session_id_keys": ["session_id", "sessionId", "sessionUuid"],
    "payload_exclude_keys": [
        "session_id", "sessionId", "sessionUuid",
        "hook_event_name", "event", "type",
    ],
    "interrupt": {
        "stop_event": "stop",
        "reason_field": None,
        "interrupt_reasons": set(),
        "interrupt_on_empty": False,
    },
    "thinking_level_map": None,
    "renderer": "config_binding",
    "binding": "config",
    "protocols": ["anthropic", "openai"],
    "isolated_dir_mode": "atomcode-home",
    "transcript": {
        # 档 2: sessions/<hex>/<uuid>.jsonl 按轮追加解析
        # <hex> = 实例隔离目录哈希(ATOMCODE_HOME)
        # <uuid> = 会话 UUID
        "roots": [
            {"type": "env", "name": "ATOMCODE_HOME"},
            {"type": "home", "subpath": ".atomcode"},
        ],
        "glob": ["sessions/*/{session_id}.jsonl"],
        "source_type": "jsonl",
        "authoritative_source": None,
    },
    "sandbox_allowlist": [],
    # 6.6 权限位: 钩子 allow 类(atomcode)放行三路之一
    "permission_slot": {
        "mechanism": "hook_allow",
        "hook_response_format": "cc",
        "hook_action": "deny",
        "headless": {"policy": "deny", "fallback": "hook_deny"},
    },
    "controller_settings": {
        "role_text_target": "ctrl_session",
        "permissions": {},
    },
    "provider_env": {},
    "adapter": {
        "template": "generic",
        "output_file": "tianji_atomcode_hook.py",
    },
    "hooks": {
        "manifest_merge": "deep_merge_managed",
        "managed_keys": ["tianji"],
    },
    "capabilities": {
        "statusline": {"enabled": False},
        "quota_sources": ["transcript"],
        "tier3_process_alive": False,
    },
    "completion": {
        # 完成判定: session_end
        "session_end_event": "session_end",
        "stop_is_completion": False,
    },
    "resume": {
        "cmd": "atomcode -c -p",
        "prompt": "继续任务书: {task_path}",
    },
}


# ---------------------------------------------------------------------------
# 内置模板: cline (素材: 票 09 / 规格 6.4)
# ---------------------------------------------------------------------------

# cline 钩子 8 个 task 级事件
# 无 session 级事件 → 完成判定以档 2(sessions.db status/exit_code/ended_at)
# + 档 3 进程退出为主,TaskComplete hook 为辅
# 权限位: autoApprove 大类开关(cline 钩子只能 cancel)
TEMPLATE_CLINE: dict = {
    "name": "cline",
    "version": "v1",
    "hook_map": {
        # Cline 只有 task 级事件; TaskStart/TaskComplete 是会话边界证据。
        "TaskStart": "session_start",
        "TaskComplete": "session_end",
        "Stop": "stop",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PermissionRequest": "permission_request",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
    },
    "session_id_keys": ["session_id", "taskId", "task_id"],
    "payload_exclude_keys": [
        "session_id", "taskId", "task_id",
        "hook_event_name", "event", "type",
    ],
    "interrupt": {
        "stop_event": "stop",
        "reason_field": None,
        "interrupt_reasons": set(),
        "interrupt_on_empty": False,
    },
    "thinking_level_map": None,
    "renderer": "config_binding",
    "binding": "config",
    "protocols": ["openai"],
    "isolated_dir_mode": "data-dir",
    "transcript": {
        # 档 2: sessions.db(SQLite)含 pid/exit_code/status 可判活性
        # cline 无 session 级事件 → 完成判定以档 2 + 进程退出为主
        "roots": [
            {"type": "env", "name": "CLINE_HOME"},
            {"type": "home", "subpath": ".cline"},
        ],
        "glob": ["data/db/sessions.db", "sessions/sessions.db", "sessions.db"],
        "source_type": "sqlite",
        "authoritative_source": None,
    },
    "sandbox_allowlist": [],
    # 权限位: autoApprove 大类开关(cline 钩子只能 cancel)
    "permission_slot": {
        "mechanism": "auto_approve",
        "hook_action": "cancel",
        "rules": {"file": "autoapprove.json", "format": "autoapprove_v1"},
        "headless": {"policy": "suspend", "fallback": "timeout_kill",
                     "timeout_seconds": {"config": "permission_cline_timeout", "default": 120}},
    },
    "controller_settings": {
        "role_text_target": "ctrl_session",
        "permissions": {},
    },
    "provider_env": {},
    "adapter": {
        "template": "generic",
        "output_file": "tianji_cline_hook.py",
    },
    "hooks": {
        "manifest_merge": "deep_merge_managed",
        "managed_keys": ["tianji"],
    },
    "capabilities": {
        "statusline": {"enabled": False},
        "quota_sources": ["transcript"],
        "tier3_process_alive": False,
    },
    "completion": {
        # 无 session 级事件 → 完成判定以档 2(sessions.db) + 进程退出为主
        # TaskComplete hook 为辅(此处 stop 不作为完成判定)
        "session_end_event": "session_end",
        "stop_is_completion": False,
        # 完成判定主源: 档 2 sessions.db + 档 3 进程退出
        "completion_source": "db_and_process",
    },
}


def _check_required(data: dict) -> None:
    required = ["name", "hook_map", "session_id_keys",
                "payload_exclude_keys", "interrupt", "transcript",
                "renderer", "binding", "protocols"]
    for k in required:
        if k not in data:
            raise ValueError(f"模板缺少必填字段: {k}")


# ---------------------------------------------------------------------------
# 模板注册表
# ---------------------------------------------------------------------------

_BUILTIN: dict[str, dict] = {
    "claude": TEMPLATE_CLAUDE,
    "codex": TEMPLATE_CODEX,
    "dsh": TEMPLATE_DSH,
    "kimi": TEMPLATE_KIMI,
    "atomcode": TEMPLATE_ATOMCODE,
    "cline": TEMPLATE_CLINE,
}

_REGISTRY: dict[str, Template] = {}

for _name, _data in _BUILTIN.items():
    _REGISTRY[_name] = Template.from_dict(_data)


def register(tpl: Template) -> None:
    """注册新壳模板(面向所有壳,不写死五家;新壳可插)。"""
    _REGISTRY[tpl.name] = tpl


def get_template(name: str) -> Template:
    if name not in _REGISTRY:
        raise KeyError(f"未知壳模板: {name}(已知: {', '.join(sorted(_REGISTRY))})")
    return _REGISTRY[name]


def list_templates() -> list[str]:
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# 通用翻译
# ---------------------------------------------------------------------------

def _hook_event_name(hook: dict) -> str | None:
    """从钩子载荷中取出事件名(兼容多名字段)。"""
    for k in ("hook_event_name", "event", "type"):
        v = hook.get(k)
        if v:
            return str(v)
    return None


def translate(shell: str, hook: dict) -> dict | None:
    """钩子载荷 → 统一事件 JSON(通用翻译,6.2)。

    返回 None 表示非交集事件,忽略不阻塞。
    壳原文全量保留进 payload(排除名字段后)。
    """
    tpl = get_template(shell)
    name = _hook_event_name(hook)
    if name is None:
        return None
    event_type = tpl.hook_map.get(name)
    if event_type is None:
        return None

    session_id = str(
        hook.get(tpl.session_id_keys[0]) or
        (hook.get(tpl.session_id_keys[1]) if len(tpl.session_id_keys) > 1 else "") or
        (hook.get(tpl.session_id_keys[2]) if len(tpl.session_id_keys) > 2 else "") or
        ""
    )
    payload = {k: v for k, v in hook.items()
               if k not in tpl.payload_exclude_keys}
    is_interrupt = _detect_interrupt(tpl, event_type, hook)
    return {
        "session_id": session_id,
        "event_type": event_type,
        "payload": payload,
        "is_interrupt": is_interrupt,
    }


def _detect_interrupt(tpl: Template, event_type: str, hook: dict) -> bool:
    ci = tpl.interrupt
    if event_type != ci["stop_event"]:
        return False
    reason = str(hook.get(ci["reason_field"]) or "") if ci["reason_field"] else ""
    if ci["interrupt_on_empty"] and not reason:
        return True
    return reason in ci["interrupt_reasons"]


def is_completion_event(tpl: Template, event_type: str) -> bool:
    """判断事件是否表示会话完成。"""
    cc = tpl.completion
    if event_type == cc.get("session_end_event"):
        return True
    if cc.get("stop_is_completion") and event_type == tpl.interrupt["stop_event"]:
        return True
    return False


def resume_command(shell: str, task_path: str = "", session_id: str = "") -> dict:
    """7.5 续推通道: 把"继续任务书"翻译成该壳的无头续跑原语。

    返回 {"supported": True, "cmd": ..., "prompt": ...} 或
    {"supported": False, "reason": ...}(fail-loud,调用方记实例档案退回人工)。

    翻译只做模板替换(0.3-3 不解析内容);模板未定义 resume=该壳不支持无头续跑。
    """
    tpl = get_template(shell)
    r = tpl.resume
    if not r or not r.get("cmd"):
        return {"supported": False, "shell": shell,
                "reason": f"{shell} 壳模板未定义续跑原语,不支持无头续跑,退回人工"}
    prompt = (r.get("prompt") or "继续任务书: {task_path}").format(
        task_path=task_path or "")
    cmd = r["cmd"].format(task_path=task_path or "", session_id=session_id or "")
    return {"supported": True, "shell": shell, "cmd": cmd, "prompt": prompt}
