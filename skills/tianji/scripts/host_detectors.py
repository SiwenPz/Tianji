#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主检测器：把每个宿主的本地事实读成结论机要的输入。

这里只回答"事实是什么"，不判定就绪——判定只在 doctor.py 一处。晨检 CLI 与
install.py 的 status 共用这些检测器，保证两边看到的是同一批事实。
"""

import hashlib
import json
import os
import tomllib

from role_contract import all_roles


# Codex names its role files tianji-<role>.toml and keys its adapter state by the
# bare role name. Both spellings derive from the shared contract: a retyped
# roster is how one host keeps a name the contract no longer has.
CODEX_ROLES = tuple(name.removeprefix("tianji-") for name in all_roles())


class HostDetector:
    """宿主环境检测器基类。子类需实现以下方法。"""

    def config_path(self):
        """返回 config.toml 路径。"""
        raise NotImplementedError

    def hooks_ok(self):
        """检查 hooks 受管块是否在位。返回 (bool, str)。"""
        raise NotImplementedError

    def menu_models(self):
        """返回模型菜单列表 [(alias, provider_key, base_url), ...]。空列表=无菜单。"""
        raise NotImplementedError

    def state_file_exists(self):
        """检查 state.jsonl 是否存在。返回 bool。"""
        raise NotImplementedError

    def status_line_ok(self):
        """本宿主的状态栏事实，返回 (bool, str)。

        没有自定义状态栏的宿主报规范化成功：共享结论机不解析任何宿主的配置格式，
        解析 tui.toml 之类是宿主检测器自己的事。
        """
        return True, "本宿主不支持自定义状态栏，本层不安装"

    def requires_model_menu(self):
        return True

    def roles_ok(self):
        return True, "宿主角色由默认安装检查覆盖"

    def role_bindings_ok(self):
        return True, "宿主角色绑定由默认配置覆盖"

    def routing_supported(self):
        return True, "宿主模型接入已实现"

    def probe_ok(self):
        return True, "宿主无需单独路由证明"

    def supports_menu_reconciliation(self):
        return True


class KimiDetector(HostDetector):
    """Kimi Code 宿主检测器。数据根默认 ~/.kimi-code,尊重 KIMI_CODE_HOME 环境变量。"""

    def __init__(self, root=None):
        self._root = root if root is not None else os.environ.get(
            "KIMI_CODE_HOME", os.path.expanduser("~/.kimi-code"),
        )

    def config_path(self):
        return os.path.join(self._root, "config.toml")

    def tui_path(self):
        return os.path.join(self._root, "tui.toml")

    def status_line_ok(self):
        """tui.toml 的 [status_line] 段是 Kimi 自己的配置格式，由本检测器解析。"""
        try:
            with open(self.tui_path(), "rb") as f:
                has_status_line = "status_line" in tomllib.load(f)
        except Exception:
            has_status_line = False
        return (
            has_status_line,
            "tui.toml 含 [status_line] 段" if has_status_line else "tui.toml 缺少 [status_line]",
        )

    def hooks_ok(self):
        """检查 config.toml 中 # >>> tianji-managed >>> 块内是否有 SubagentStart 和 SubagentStop。"""
        path = self.config_path()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return False, "无法读取 config.toml"

        start_marker = "# >>> tianji-managed >>>"
        end_marker = "# <<< tianji-managed <<<"
        start_idx = content.find(start_marker)
        end_idx = content.find(end_marker)
        if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
            return False, "受管块标记缺失"

        block = content[start_idx:end_idx + len(end_marker)]
        has_start = "SubagentStart" in block
        has_stop = "SubagentStop" in block
        if has_start and has_stop:
            return True, "受管块在位 (SubagentStart/SubagentStop)"
        missing = []
        if not has_start:
            missing.append("SubagentStart")
        if not has_stop:
            missing.append("SubagentStop")
        return False, f"受管块内缺 {', '.join(missing)}"

    def menu_models(self):
        """从 [secondary_model.models] 读取模型,推断 provider 和 base_url。"""
        path = self.config_path()
        try:
            with open(path, "rb") as f:
                cfg = tomllib.load(f)
        except Exception:
            return []

        secondary = cfg.get("secondary_model", {})
        models_dict = secondary.get("models", {})
        if not models_dict:
            return []

        # 读取各模型定义的 provider 引用
        resolved = {}   # alias -> (provider_key, base_url)
        all_providers = cfg.get("providers", {})

        for alias in models_dict:
            # 优先从 [models."alias"] 找 provider
            model_entries = cfg.get("models", {})
            provider_key = None
            base_url = None

            # 直接匹配（可能带 provider 前缀也可能不带）
            for mk, mv in model_entries.items():
                if mk == alias or mk.endswith("/" + alias):
                    provider_key = mv.get("provider")
                    break

            if provider_key and provider_key in all_providers:
                base_url = all_providers[provider_key].get("base_url", "")
            else:
                # 兜底:找第一个有 base_url 的 provider
                for pk, pv in all_providers.items():
                    bu = pv.get("base_url", "")
                    if bu:
                        base_url = bu
                        provider_key = pk
                        break

            resolved[alias] = (provider_key or "", base_url or "")

        result = []
        for alias, (prov, bu) in resolved.items():
            result.append((alias, prov, bu))
        return result

    def state_file_exists(self):
        cwd = os.getcwd()
        return os.path.isfile(os.path.join(cwd, ".tianji", "state.jsonl"))


class CodexDetector(HostDetector):
    """Validate Codex's unified Responses provider and native-agent evidence."""

    def __init__(self, root=None):
        self._root = root if root is not None else os.environ.get(
            "CODEX_HOME", os.path.expanduser("~/.codex"),
        )

    def config_path(self):
        return os.path.join(self._root, "hooks.json")

    def _adapter_path(self):
        return os.path.join(self._root, "tianji-adapter.toml")

    def _adapter(self):
        try:
            with open(self._adapter_path(), "rb") as f:
                return tomllib.load(f)
        except Exception:
            return {}

    def _host_native_subject_ok(self, state):
        models = state.get("models", {})
        roles = state.get("roles", {})
        recorded_hashes = state.get("role_hashes", {})
        if not all(isinstance(value, dict) for value in (models, roles, recorded_hashes)):
            return False
        try:
            current_hashes = {
                role: hashlib.sha256(
                    open(
                        os.path.join(self._root, "agents", f"tianji-{role}.toml"),
                        "r", encoding="utf-8",
                    ).read().encode("utf-8")
                ).hexdigest()
                for role in CODEX_ROLES
            }
        except Exception:
            return False
        subject = {
            "model_source": "host_native",
            "models": models,
            "roles": roles,
            "role_hashes": current_hashes,
        }
        current_subject = hashlib.sha256(json.dumps(
            subject, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return (
            current_hashes == recorded_hashes
            and current_subject == state.get("subject_sha256")
        )

    def hooks_ok(self):
        try:
            with open(self.config_path(), "r", encoding="utf-8-sig") as f:
                hooks = json.load(f).get("hooks", {})
            for event in ("SubagentStart", "SubagentStop"):
                if not any(
                    isinstance(handler, dict) and handler.get("statusMessage") == "Tianji managed state ledger"
                    for matcher in hooks.get(event, []) if isinstance(matcher, dict)
                    for handler in matcher.get("hooks", []) if isinstance(handler, dict)
                ):
                    return False, f"hooks.json missing Tianji {event}"
            return True, "hooks.json has Tianji SubagentStart/SubagentStop"
        except Exception:
            return False, "cannot read hooks.json"

    def menu_models(self):
        state = self._adapter()
        if state.get("model_source") == "host_native":
            models = state.get("models", {})
            if not isinstance(models, dict):
                return []
            return [
                (alias, "codex-native", "host://codex")
                for alias, model in models.items()
                if isinstance(alias, str) and isinstance(model, str) and model
            ]
        base_url = state.get("base_url", "")
        models = state.get("models", {})
        if not isinstance(models, dict) or not isinstance(base_url, str):
            return []
        return [(alias, "tianji", base_url) for alias, model in models.items()
                if isinstance(alias, str) and isinstance(model, str) and model]

    def state_file_exists(self):
        return os.path.isfile(os.path.join(os.getcwd(), ".tianji", "state.jsonl"))

    def requires_model_menu(self):
        return True

    def routing_supported(self):
        state = self._adapter()
        if state.get("status") != "configured":
            return False, "Codex 统一 Responses provider 尚未配置"
        if state.get("model_source") == "host_native":
            models = state.get("models", {})
            if not isinstance(models, dict) or not models:
                return False, "Codex 宿主原生模型菜单为空"
            if not self._host_native_subject_ok(state):
                return False, "Codex 宿主原生角色或模型快照已漂移，需重新引导并复验"
            return True, f"Codex 宿主原生模型调度已配置 ({len(models)} 个模型)"
        config_path = os.path.join(self._root, "config.toml")
        try:
            text = open(config_path, "r", encoding="utf-8-sig").read()
            cfg = tomllib.loads(text)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            provider = cfg.get("model_providers", {}).get("tianji", {})
        except Exception:
            return False, "Codex config.toml 无法读取或解析"
        if digest != state.get("config_sha256"):
            return False, "Codex config.toml 在适配后已变化，需重新引导并复验"
        if (cfg.get("model_provider") != "tianji"
                or cfg.get("model") != state.get("main_model")
                or provider.get("wire_api") != "responses"
                or provider.get("base_url") != state.get("base_url")):
            return False, "Codex 当前主模型未走天机统一 Responses provider"
        return True, f"Codex 主模型与子代理统一接入 {state.get('base_url')}"

    def probe_ok(self):
        state = self._adapter()
        proof = state.get("proof", {})
        if state.get("model_source") == "host_native":
            models = state.get("models", {})
            roles = state.get("roles", {})
            # The proof names the role it observed; its binding is what the
            # dispatch has to match. A recorded role nothing binds is not
            # evidence, so it falls through to the failure branch.
            proof_role = proof.get("proof_role")
            expected_model = (
                models.get(roles.get(proof_role))
                if isinstance(models, dict) and isinstance(roles, dict)
                and isinstance(proof_role, str) and proof_role else None
            )
            if (proof.get("status") == "passed"
                    and proof.get("proof_level") == "host_dispatch"
                    and proof.get("wire_verified") is False
                    and self._host_native_subject_ok(state)
                    and isinstance(expected_model, str) and expected_model
                    and proof.get("expected_role_model") == expected_model
                    and proof.get("observed_model") == expected_model
                    and proof.get("subject_sha256") == state.get("subject_sha256")):
                return True, (
                    f"原生 parent→{proof_role} 已实跑 "
                    f"({proof.get('observed_model', '?')}, host_dispatch)"
                )
            return False, "尚无与当前原生角色绑定匹配的 Codex host_dispatch 证明"
        if (proof.get("status") == "passed"
                and proof.get("config_sha256") == state.get("config_sha256")):
            return True, (
                f"原生 parent→{proof.get('proof_role', '?')} 已实跑 "
                f"({proof.get('expected_role_model', '?')})"
            )
        return False, "尚无与当前配置匹配的 Codex 原生子代理路由证明"

    def supports_menu_reconciliation(self):
        return False

    def roles_ok(self):
        agents_dir = os.path.join(self._root, "agents")
        missing = [
            name for name in CODEX_ROLES
            if not os.path.isfile(os.path.join(agents_dir, f"tianji-{name}.toml"))
        ]
        if missing:
            return False, "缺少 Codex 角色: " + ", ".join(missing)
        return True, "Codex 天机角色已就位"

    def role_bindings_ok(self):
        state = self._adapter()
        recorded_roles = state.get("roles", {})
        recorded_hashes = state.get("role_hashes", {})
        agents_dir = os.path.join(self._root, "agents")
        unconfigured = []
        for name in CODEX_ROLES:
            path = os.path.join(agents_dir, f"tianji-{name}.toml")
            try:
                text = open(path, "r", encoding="utf-8").read()
                parsed = tomllib.loads(text)
            except Exception:
                unconfigured.append(name)
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if name not in recorded_roles or digest != recorded_hashes.get(name):
                unconfigured.append(name)
                continue
            if '# tianji-role-binding = "primary"' in text and "model" not in parsed:
                continue
            if '# tianji-role-binding = "fixed"' in text and isinstance(parsed.get("model"), str) and parsed["model"].strip():
                continue
            unconfigured.append(name)
        if unconfigured:
            return False, "未配置 Codex 角色模型: " + ", ".join(unconfigured)
        return True, "Codex 角色模型绑定已确认"


# 可靠运行时信号：由宿主进程自己注入，不是"这台机器上装了什么"。
#
# 只有宿主自己注入的东西才算身份。配置目录（CODEX_HOME / KIMI_CODE_HOME）只说明
# 这台机器装过该宿主，说明不了"现在跑在哪个宿主里"——把它们当身份，正是上一版把
# command code 会话报成"kimi 没装"的原因。codex/kimi 没有注入式信号，因此由各自的
# 宿主入口显式传 --host，而不是在这里猜。
HOST_SIGNALS = (
    ("COMMANDCODE_SCRATCHPAD", "cmdc"),
)


def detect_host(environ=None):
    """Identify the current host from a signal the host process itself injects.

    Returns ``(host_name, reason)``. ``host_name`` is ``None`` when the evidence
    is absent or ambiguous, and ``reason`` then says why the caller must refuse
    rather than pick one: the old ``--host kimi`` default is exactly how a
    Command Code machine came to be diagnosed as a missing Kimi install.

    Only injected runtime signals are consulted on purpose. "Which host am I
    running in" is not the same question as "which hosts happen to be installed
    on this machine", and treating a configuration directory as identity -- or
    guessing a precedence between several installed hosts -- would reintroduce
    the silent misdiagnosis this refuses.
    """
    env = os.environ if environ is None else environ
    hits = [(var, host) for var, host in HOST_SIGNALS if env.get(var)]
    if len(hits) == 1:
        var, host = hits[0]
        return host, f"{var} 已设置"
    if not hits:
        return None, "没有任何宿主运行时信号"
    return None, "同时命中多个宿主信号: " + ", ".join(
        f"{var}→{host}" for var, host in hits
    )
