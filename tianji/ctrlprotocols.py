"""总控会话协议 backend: 按协议封装壳子进程通信。

协议是通信方式的抽象——同一协议的不同壳共享同一个 backend。
当前协议: stream-json (claude), acp (kimi/cursor-agent/gemini-cli ...)。

实现注意事项(评审定死的):
- call-time 属性查找: `subprocess.Popen(...)` + `shutil.which(...)`(from 形式
  会破 monkypatch;test_real_command_composition 靠 patch 模块级 subprocess/shu
  生效)
- _build_claude_env 死代码已删(claude 的 env 走 --settings 一体文件)
- _apply_env_direct 返空(不塞 ANTHROPIC_AUTH_TOKEN)
- _read_secret 读 ctrl-secret.txt
- import shutil(ClaudeStreamBackend.start 里 shutil.which 用)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .db import injected_dir, tianji_home


def _subprocess_flags() -> int:
    """Windows 拉起总控会话子进程的 creationflags。

    CREATE_NEW_PROCESS_GROUP: 会话子进程脱离父进程(webapp/测试)的控制台进程组,
    防 Ctrl+C/控制台事件在进程组内串扰——2026-08-25 实证 npx 绑终端启动 dsh 时,
    子进程共享其进程组,清理触发信号串扰会崩掉宿主(npx 批处理弹 Terminate batch job)。
    """
    if os.name == "nt":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


# ===================================================================
# 基类 (共享骨架)
# ===================================================================

class BaseBackend:
    """所有总控会话 backend 的接口约定。

    子类实现 start()/send()/get_events()。
    session_id property: 各 backend 自己管存储,薄壳委托用。

    类方法 from_config: 读 settings-controller.json 的 ctrl_session 块,
    自动分发到对应协议后端(stream-json → ClaudeStreamBackend, acp → ACPBackend,
    其他/缺块 → BaseBackend 空壳)。
    """

    def __init__(self, home: Path, launch: list[str],
                 data_root_env: str | None, provider_env: dict,
                 key_ref: str = "", model: str = "",
                 base_url: str = "", protocol: str = "anthropic",
                 role_text: str = "", cwd: str | None = None):
        self.home = home
        self.launch = launch
        self.data_root_env = data_root_env
        self.provider_env = provider_env or {}
        self._key_ref = key_ref
        self._model = model
        self._base_url = base_url
        self._protocol = protocol
        self._role_text = role_text
        self.cwd = cwd  # 总控会话工作目录(票 40: 默认 None=账本根)
        self._role_injected = False
        self.proc: subprocess.Popen | None = None

    @classmethod
    def from_config(cls, home: Path, settings_path: Path) -> BaseBackend:
        """读 settings-controller.json → 返回协议后端。

        - 文件不存在/解析失败 → BaseBackend(空壳)。
        - 有 env 但无 ctrl_session → 视为 claude(无 ctrl_session 块 = 旧版,
          v0.5 前只有 claude)。
        - 有 ctrl_session → 按 ctrl_session.protocol 分发: stream-json → ClaudeStreamBackend,
          acp → ACPBackend, 未知 → BaseBackend。
        """
        try:
            doc = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(home=home, launch=["claude"], data_root_env=None,
                       provider_env={})
        cs = doc.get("ctrl_session")
        if cs:
            protocol = cs.get("protocol") or "stream-json"
        elif doc.get("env"):
            # 有 env 无极 Session 块 → 旧版仅支持 claude
            return ClaudeStreamBackend(
                home=home, launch=["claude"], data_root_env=None,
                provider_env={})
        else:
            return cls(home=home, launch=["claude"], data_root_env=None,
                       provider_env={})
        try:
            backend_cls = get_backend_class(protocol)
        except KeyError:
            return cls(home=home, launch=["claude"], data_root_env=None,
                       provider_env={})
        launch = cs.get("launch") or (["kimi", "acp"] if protocol == "acp" else ["claude"])
        return backend_cls(
            home=home, launch=launch,
            data_root_env=cs.get("data_root_env"),
            provider_env=cs.get("provider_env", {}),
            key_ref=cs.get("key_ref", ""),
            model=cs.get("model", ""),
            base_url=cs.get("base_url", ""),
            protocol=protocol,
            role_text=cs.get("role_text", ""),
        )

    # ---- session_id 属性(各 backend 自己管存储) ----
    @property
    def session_id(self) -> str | None:
        return getattr(self, "_session_id", None)

    @session_id.setter
    def session_id(self, value):
        self._session_id = value

    # ---- 子类必须实现 ----
    def start(self, home=None): raise NotImplementedError
    def send(self, text: str): raise NotImplementedError
    def get_events(self, after: int = 0) -> tuple[list, int]: raise NotImplementedError

    # ---- 共享实现 ----
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self):
        """stdin EOF → 宽限 5s → terminate → Windows 整树 taskkill /T /F。"""
        p = self.proc
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if p.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                               capture_output=True)
            else:
                p.kill()
        self.proc = None


# ===================================================================
# stream-json 协议 (claude)
# ===================================================================

class ClaudeStreamBackend(BaseBackend):
    """claude stream-json 双向封装。

    ★ 启动命令从 ctrlsession.py 逐行搬过来,一个字不改(见下方 start())。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._started_with_resume = False
        self._started_at = 0.0

    def start(self, home=None):
        """拉起子进程;cwd=self.cwd(总控工作目录,未设则账本根,票 40)。

        ★ launch 产物永远用(不走 shutil.which("claude"))。
        若 launch 不含 "claude" (cmd_override 测试注入),直接起,不碰 settings。
        """
        home = Path(home) if home else (self.home or tianji_home())
        self.home = home
        shell_flag = False
        # cmd_override 标识: launch 不含 "claude" → 跳过 settings 逻辑
        exe0 = self.launch[0].lower() if self.launch else ""
        _override = not any(
            exe0.endswith(part) for part in ("claude", "claude.exe", "claude.cmd")
        )
        if _override:
            cmd = list(self.launch)
        else:
            settings = injected_dir() / "settings-controller.json"
            if not settings.exists():
                raise FileNotFoundError(f"{settings} 不存在,先跑 tianji start")
            exe = self.launch[0]  # 裸 "claude"; Windows 走 shell=True 由 cmd.exe 解析
            cmd = [exe, "--print",
                   "--input-format", "stream-json",
                   "--output-format", "stream-json",
                   "--verbose",
                   "--include-partial-messages",
                   "--settings", str(settings)]
            role_text = json.loads(
                settings.read_text(encoding="utf-8")).get("appendSystemPrompt")
            if role_text:
                cmd += ["--append-system-prompt", role_text]
            # 模型 pin 与 resume 续命是本地会话能力;zip 分支未包含,合并时保留。
            if self._model:
                cmd += ["--model", self._model]
            self._started_with_resume = False
            resume_id = _load_persisted_session(home)
            if resume_id:
                cmd += ["--resume", resume_id]
                self._started_with_resume = True
            # Windows 的 claude 是 .cmd shim,要走 cmd.exe
            shell_flag = os.name == "nt"
        self._started_at = time.monotonic()
        self.proc = subprocess.Popen(
            cmd, cwd=str(self.cwd) if self.cwd else str(home),
            shell=shell_flag, env=os.environ,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_subprocess_flags(),
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump, args=(self.proc,),
                         daemon=True).start()

    def send(self, text: str):
        """组 user 消息写 stdin;进程没起/死了就重拉(死了先记重启事件)。"""
        for _ in range(2):
            if not self.is_alive():
                if self.proc is not None:
                    with self._lock:
                        note = "会话进程重启,上文丢了"
                        if self._started_with_resume and self._started_at and \
                                time.monotonic() - self._started_at < _RESUME_DEATH_GRACE:
                            _clear_persisted_session(self.home)
                            note = "resume 失败,清盘重开(上文丢了)"
                        self._events.append({
                            "type": "system", "subtype": "restart",
                            "note": note})
                        self._session_id = None
                self.start()
            msg = {"type": "user",
                   "message": {"role": "user",
                               "content": [{"type": "text", "text": text}]}}
            if self._session_id:
                msg["session_id"] = self._session_id
            try:
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                return
            except OSError:
                pass  # 写的当口断了,再走一轮重拉
        raise RuntimeError("总控会话进程写不进去")

    def get_events(self, after=0) -> tuple[list, int]:
        with self._lock:
            return list(self._events[after:]), len(self._events)

    def _pump(self, proc):
        """读线程: 逐行收 stdout,JSON 坏的行丢弃;进程死了循环自然到头。"""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                if ev.get("session_id"):
                    if ev["session_id"] != self._session_id:
                        _persist_session(self.home, ev["session_id"])
                    self._session_id = ev["session_id"]
                self._events.append(ev)
                self._maybe_clear_on_overflow(ev)

    def _maybe_clear_on_overflow(self, ev: dict) -> None:
        """上下文压缩/溢出后清 resume 存档,防止下次启动回旧上下文。"""
        if ev.get("type") != "system":
            return
        text = f"{ev.get('subtype', '')} {ev.get('text', '')}".lower()
        if any(h in text for h in _OVERFLOW_HINTS):
            _clear_persisted_session(self.home)
            self._events.append({
                "type": "system", "subtype": "session_reset",
                "note": "检测到上下文压缩/溢出,已清 resume 存档"})


# ===================================================================
# ACP 协议 (kimi / cursor-agent / gemini-cli ...)
# ===================================================================

class ACPBackend(BaseBackend):
    """ACP JSON-RPC 2.0 over stdio 双向封装。

    三路帧:
      response    → 有 id 无 method → 回执,交给 _rpc_call 等待队列
      notification → 有 method 无 id → 事件,翻译后进 _events
      request     → 有 id 有 method → kimi 反向要 client 应答;
                     0.38.0 未实现(session/request_permission),
                     scaffold 留空实现待新版启用

    生命周期: initialize → session/new → session/prompt (循环)
    崩溃: 进程死 → 自动重启 + 重拉;session/load 续上文 (TODO)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, _Waiter] = {}
        self._prompt_ids: set[int] = set()  # prompt 的 id 集合,匹配 response

    # ---- start ----
    def start(self, home=None):
        """拉起子进程 + initialize + session/new。

        ctor 不自动 start(避免 ACP 握手 2×15s timeout 阻塞 _ctrl());
        由 send() 或显式 start() 触发。
        """
        if home is not None:
            self.home = Path(home)
        env = {**os.environ}
        if self.data_root_env:
            isolated = self.home / ".isolated"
            isolated.mkdir(parents=True, exist_ok=True)
            env[self.data_root_env] = str(isolated)
        env.update(_build_provider_env(
            self.provider_env,
            self._key_ref,
            model=self._model,
            base_url=self._base_url,
            protocol=self._protocol,
        ))
        env.update({
            "TIANJI_HOME": str(self.home),
            "TIANJI_WORKER_ID": "总控",
            "TIANJI_SECRET": _read_secret(self.home),
        })

        shell_flag = False
        if os.name == "nt":
            exe_path = self.launch[0] if self.launch else ""
            _, ext = os.path.splitext(exe_path)
            if ext.lower() in (".cmd", ".bat"):
                shell_flag = True
            elif not ext:
                # bare stem (npm-installed shim): resolve via PATHEXT
                resolved = shutil.which(exe_path)
                if resolved:
                    _, rext = os.path.splitext(resolved)
                    shell_flag = rext.lower() in (".cmd", ".bat")
        self.proc = subprocess.Popen(
            self.launch, cwd=str(self.cwd) if self.cwd else str(self.home),
            shell=shell_flag, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # ★ kimi acp 日志全走 stderr
            creationflags=_subprocess_flags(),
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump_stdout, args=(self.proc,),
                         daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(self.proc,),
                         daemon=True).start()
        time.sleep(0.1)  # 让读线程先挂上,否则 _rpc_call 的 response 可能在 pipe 里没人吃

        # ① initialize (三路: protocolVersion + clientCapabilities + clientInfo)
        try:
            self._rpc_call("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {},  # ★ 不声明 fs
                "clientInfo": {"name": "tianji", "version": "0.6.0"}
            })
        except (RuntimeError, TimeoutError) as e:
            with self._lock:
                self._events.append({
                    "type": "system", "subtype": "error",
                    "text": f"{self.launch[0]} 初始化失败: {e}"})
            return  # 不抛异常,让事件进 _events 前端可见
        persisted = _load_persisted_session(self.home)
        if persisted:
            try:
                result = self._rpc_call("session/load", {
                    "sessionId": persisted,
                    "cwd": str(self.home),
                    "mcpServers": {}
                })
                self._session_id = (result.get("sessionId") or result.get("id")
                                    or result.get("session_id") or persisted)
                _persist_session(self.home, self._session_id)
                return
            except (RuntimeError, TimeoutError):
                _clear_persisted_session(self.home)
                with self._lock:
                    self._events.append({
                        "type": "system", "subtype": "session_reset",
                        "note": "session/load 失败,降级新会话(上文丢了)"})
        # ③ session/new
        try:
            result = self._rpc_call("session/new", {
                "cwd": str(self.home),
                "mcpServers": {}
            })
        except (RuntimeError, TimeoutError) as e:
            with self._lock:
                self._events.append({
                    "type": "system", "subtype": "error",
                    "text": f"{self.launch[0]} 建会话失败: {e}"})
            return
        # ★ 规范推定: 字段为 sessionId；保守多字段兜底
        self._session_id = (result.get("sessionId") or result.get("id")
                            or result.get("session_id"))
        if self._session_id:
            _persist_session(self.home, self._session_id)

    # ---- send ----
    def send(self, text: str):
        """fire-and-forget: 写 prompt 不等回执。事件走 _pump_stdout 进 _events。

        ★ 角色话术注入: 每 session 首次 send 时把 role_text 拼到消息前面,
        不单独发一轮(省 token、不上屏额外 assistant 回显、不阻塞 HTTP)。
        """
        if not self.is_alive():
            with self._lock:
                self._events.append({
                    "type": "system", "subtype": "restart",
                    "note": "会话进程重启,上文丢了"})
                self._session_id = None
                self._role_injected = False  # ★ 新 session 要重新注入
            self.start()
        # ★ 首条消息注入角色话术(每 session 一次)
        if self._role_text and not self._role_injected:
            text = (f"[系统设定,请记住以下设定后照此执行,不要复述这段:"
                    f" {self._role_text}]\n\n用户说: {text}")
            self._role_injected = True
        rid = self._next_id
        self._next_id += 1
        self._prompt_ids.add(rid)  # ★ 记进 prompt 集合,匹配 response
        msg = {"jsonrpc": "2.0", "method": "session/prompt",
               "params": {"sessionId": self._session_id,
                          "prompt": [{"type": "text", "text": text}]},
               "id": rid}
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except OSError:
            pass  # 写的当口断了,下一 send 重拉

    # ---- RPC ----
    def _rpc_call(self, method: str, params: dict, timeout: float = 15):
        """同步等 response (initialize/session/new) 用;send 不用。"""
        rid = self._next_id
        self._next_id += 1
        waiter = _Waiter()
        self._pending[rid] = waiter
        msg = {"jsonrpc": "2.0", "method": method,
               "params": params, "id": rid}
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        if not waiter.wait(timeout):
            raise TimeoutError(f"acp {method} 超时")
        w = self._pending.pop(rid)
        if w.error:
            raise RuntimeError(f"acp {method}: {w.error.get('message', w.error)}")
        return w.result

    # ---- stdout 三路帧 ----
    def _pump_stdout(self, proc):
        """读 stdout: 三分支帧路由。

        response(有 id 无 method)    → _rpc_call 等待队列;
        error response(有 id 无 method,有 error) → prompt 的翻成 error 事件,
                                                  init 的抛给 _rpc_call;
        request(有 id 有 method)    → _handle_request;
        notification(有 method 无 id) → 翻译后进 _events。
        """
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue

            mid = obj.get("method")
            rid = obj.get("id")

            if rid is not None and mid is None:
                # ★ 无 method 有 id: response 或 error response
                if "result" in obj:
                    result = obj["result"]
                    if rid in self._prompt_ids:
                        # prompt 的 response → 翻译成 result 事件(前端收尾)
                        self._prompt_ids.discard(rid)
                        ev = _translate_result(result)
                        if ev:
                            with self._lock:
                                self._events.append(ev)
                    waiter = self._pending.get(rid)
                    if waiter:
                        waiter.result = result
                        waiter.set()
                elif "error" in obj:
                    err = obj["error"]
                    if rid in self._prompt_ids:
                        # prompt 的 error → 翻成 error 事件上屏
                        self._prompt_ids.discard(rid)
                        with self._lock:
                            self._events.append({
                                "type": "system", "subtype": "error",
                                "text": err.get("message", str(err))})
                    else:
                        # init/session 的 error → 抛给 _rpc_call
                        waiter = self._pending.get(rid)
                        if waiter:
                            waiter.error = err
                            waiter.set()
            elif mid:
                # ★ 有 method: notification(无 id) 或 request(有 id)
                if rid is not None:
                    self._handle_request(obj)
                else:
                    ev = _translate_kimi_event(obj)
                    if ev:
                        with self._lock:
                            self._events.append(ev)

    # ---- stderr 通道 ----
    def _pump_stderr(self, proc):
        """读 stderr: kimi acp 日志全走 stderr → 转 system 诊断事件。"""
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            with self._lock:
                self._events.append({
                    "type": "system", "subtype": "kimi_log",
                    "text": line[:500]  # 截长防爆
                })

    # ---- reverse-RPC ----
    def _handle_request(self, obj: dict):
        """处理 kimi → client 的 request (reverse-RPC)。

        ★ 0.38.0 未实现 session/request_permission;scaffold 留空。
        新版可用时,对已知 request type 应答。
        """
        method = obj.get("method", "")
        params = obj.get("params", {})
        rid = obj.get("id")

        if method == "session/request_permission":
            # TODO(版本检测后启用): 按 params.permission.type 分流
            # 工具审批 → allow;问题征询 → 需总控决策
            resp = {"jsonrpc": "2.0",
                    "result": {"action": "allow"},
                    "id": rid}
            try:
                self.proc.stdin.write(json.dumps(resp) + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass
        elif method.startswith("fs/"):
            # 文件系统操作: tianji 不打算实现,让 kimi 本地执行
            resp = {"jsonrpc": "2.0",
                    "error": {"code": -32601,
                               "message": "Not handled by client"},
                    "id": rid}
            try:
                self.proc.stdin.write(json.dumps(resp) + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass

    # ---- events ----
    def get_events(self, after=0) -> tuple[list, int]:
        with self._lock:
            return list(self._events[after:]), len(self._events)


# ===================================================================
# 辅助: key env 构造
# ===================================================================

def _build_provider_env(provider_env: dict, key_ref: str,
                        model: str = "", base_url: str = "",
                        protocol: str = "anthropic") -> dict:
    """按 provider_env.map 模板(format)构建进程级 env dict(E.2)。

    provider_env 来源: 壳模板 provider_env 字段→settings-controller.json。
    target="process_env" 的壳(kimi)在 start() 时调用本函数注入;
    target="settings_env" 的壳(claude)已在 env 块写好,本函数 return 空。
    """
    tgt = provider_env.get("target", "")
    if tgt != "process_env":
        return {}
    pmap = provider_env.get("map")
    if not pmap:
        return {}
    # fail-loud(票: key 占位符): key_ref 指了路径但读不到 = 配置错误,
    # 必须显式报错指路,不许静默空串(空 key 注入 env 会让 401 排障无从下手);
    # 空 key_ref(未配置 key)保持返回 "" 由模板自行兜底。
    if not key_ref:
        key_value = ""
    else:
        try:
            key_value = Path(key_ref).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise FileNotFoundError(
                f"key_ref 指向的 key 文件读不到: {key_ref}({e});"
                f"请检查 instance 的 key_ref 配置或重建该文件") from e
    ctx = {"key": key_value, "model": model, "base_url": base_url,
           "protocol": protocol}
    env = {}
    for var_name, tpl in pmap.items():
        val = tpl.format(**ctx)
        if val:
            env[var_name] = val
    return env


# ===================================================================
# 辅助: identity
# ===================================================================

def _read_secret(home: Path, default: str = "") -> str:
    secret_file = injected_dir() / "ctrl-secret.txt"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()
    return default


# ===================================================================
# 辅助: 会话续命(票 05 落盘 resume)
# ===================================================================

SESSION_FILE = ".ctrl-session.json"
_RESUME_DEATH_GRACE = 5.0   # 带 --resume 起后 5s 内死=resume 失败(旧 id 失效)
_OVERFLOW_HINTS = ("compact", "overflow", "溢出")


def _persist_session(home: Path, session_id: str) -> None:
    """session_id 变化即落盘 home/.ctrl-session.json(崩溃后 --resume 续命)。"""
    try:
        (Path(home) / SESSION_FILE).write_text(
            json.dumps({"session_id": session_id,
                        "updated_at": int(time.time())}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def _load_persisted_session(home: Path) -> str | None:
    try:
        doc = json.loads((Path(home) / SESSION_FILE).read_text(encoding="utf-8"))
        sid = doc.get("session_id")
        return sid if isinstance(sid, str) and sid else None
    except (OSError, ValueError):
        return None


def _clear_persisted_session(home: Path) -> None:
    try:
        (Path(home) / SESSION_FILE).unlink()
    except OSError:
        pass


# ===================================================================
# 辅助: 事件翻译
# ===================================================================

def _translate_kimi_event(raw: dict) -> dict | None:
    """ACP notification → 标准化 event dict (前端渲染用)。

    ★ 实测定稿(2026-08-23 kimi 0.38.0 实机): notification 一律
    method="session/update",params.update.sessionUpdate 是判别字段:
      agent_message_chunk → assistant 文本(流式 chunk,content.text)
      agent_thought_chunk → 思维链(同形状)
      tool_call           → 工具调用(title/rawInput)
      tool_call_update    → 工具结果(status=completed 时带 rawOutput)
      available_commands_update/session_info_update/usage_update/
      current_mode_update/config_option_update → 已知可忽略
    """
    if raw.get("method") != "session/update":
        return {"type": "system",
                "subtype": f"{raw.get('method', '?')}:",
                "raw": raw.get("params", raw)}
    update = (raw.get("params") or {}).get("update") or {}
    utype = update.get("sessionUpdate", "")

    if utype == "agent_message_chunk":
        text = (update.get("content") or {}).get("text", "")
        return {"type": "assistant", "text": text} if text else None
    if utype == "agent_thought_chunk":
        text = (update.get("content") or {}).get("text", "")
        if not text:
            return None
        return {"type": "system", "subtype": "thinking_tokens", "text": text}
    if utype == "tool_call":
        return {"type": "assistant",
                "tool": {"name": update.get("title", ""),
                         "input": update.get("rawInput", {})}}
    if utype == "tool_call_update":
        if update.get("status") != "completed":
            return None
        return {"type": "system", "subtype": "tool_result",
                "text": json.dumps(update.get("rawOutput", ""),
                                   ensure_ascii=False)[:2000]}
    if utype in ("available_commands_update", "session_info_update",
                 "usage_update", "current_mode_update",
                 "config_option_update", "plan"):
        return None  # 已知可忽略: 斜杠命令清单/会话标题/token 用量等
    # 兜底: 未知类型原样记录(新版本出新事件类型时可见,不静默丢)
    return {"type": "system", "subtype": f"session/update:{utype}",
            "raw": update}


def _translate_result(result: dict) -> dict | None:
    """prompt response 的 result → 前端收尾事件。"""
    stop_reason = result.get("stopReason", result.get("stop_reason", ""))
    usage = result.get("usage", {})
    return {"type": "result",
            "stop_reason": stop_reason,
            "usage": usage,
            "raw": result}


class _Waiter:
    """同步等待 RPC response 的具柄。"""
    def __init__(self):
        self.result = None
        self.error = None
        self._ev = threading.Event()

    def wait(self, timeout):
        return self._ev.wait(timeout)

    def set(self):
        self._ev.set()


# ===================================================================
# Backend 注册表 (按协议名做键)
# =parameter>

BACKENDS: dict[str, type[BaseBackend]] = {
    "stream-json": ClaudeStreamBackend,
    "acp": ACPBackend,
}


def register_backend(protocol: str, cls: type[BaseBackend]) -> None:
    BACKENDS[protocol] = cls


def get_backend_class(protocol: str) -> type[BaseBackend]:
    if protocol not in BACKENDS:
        raise KeyError(
            f"未知会话协议: {protocol}(已知: {', '.join(sorted(BACKENDS))})")
    return BACKENDS[protocol]
