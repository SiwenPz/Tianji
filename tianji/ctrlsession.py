"""总控真会话: claude 常驻双向 stream-json 子进程封装(web 总控对话面用)。

- 启动命令(2026-08-21 Windows 实机验证):
  claude --print --input-format stream-json --output-format stream-json --verbose
         --settings <home>/settings-controller.json --append-system-prompt <角色话术>
  settings 一体文件里的 appendSystemPrompt 键实测不被 --settings 加载,
  必须读出来经 CLI 参数显式注入(同 cli.py _launch_console 的口径,勿回退)
- stdin 一行一个 JSON user 消息;拿到首个事件的 session_id 后后续消息带上
- stdout 逐行吐事件: system/assistant/result;读线程解析,坏行丢弃不炸
- 进程死了不装活: 下一次 send 如实记一条"会话进程重启,上文丢了"再重拉新进程
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from .db import tianji_home


class ControllerSession:
    """常驻 claude 子进程 + 内存 append-only 事件列表(1.5s 轮询取增量)。

    cmd_override: 测试注入的假进程命令(如 python -c 罐头脚本),给了就不碰
    claude/settings 那套;home 默认走 TIANJI_HOME。"""

    def __init__(self, cmd_override: list | None = None, home=None):
        self.cmd_override = cmd_override
        self.home = Path(home) if home else None
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        self._events: list[dict] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期

    def start(self, home=None):
        """拉起子进程;cwd=home(总控的账本根,不是仓库根)。"""
        home = Path(home) if home else (self.home or tianji_home())
        self.home = home
        shell = False
        if self.cmd_override:
            cmd = list(self.cmd_override)
        else:
            settings = home / "settings-controller.json"
            if not settings.exists():
                raise FileNotFoundError(f"{settings} 不存在,先跑 tianji start")
            exe = shutil.which("claude") or "claude.cmd"
            cmd = [exe, "--print",
                   "--input-format", "stream-json",
                   "--output-format", "stream-json",
                   "--verbose",
                   "--include-partial-messages",  # 逐 token 增量(打字机/思维链原料)
                   "--settings", str(settings)]
            role = json.loads(settings.read_text(encoding="utf-8")
                              ).get("appendSystemPrompt")
            if role:
                cmd += ["--append-system-prompt", role]
            # Windows 的 claude 是 .cmd shim,要走 cmd.exe 才拉得起来;
            # 整树清理由 close() 的 taskkill /T 兜底
            shell = os.name == "nt"
        self.proc = subprocess.Popen(
            cmd, cwd=str(home), shell=shell,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump, args=(self.proc,),
                         daemon=True).start()

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

    # ------------------------------------------------------------ 收发

    def send(self, text: str):
        """组 user 消息写 stdin;进程没起/死了就重拉(死了先记重启事件)。"""
        for _ in range(2):
            if not self.is_alive():
                if self.proc is not None:  # 死过,如实记一笔,上文真丢了
                    with self._lock:
                        self._events.append({
                            "type": "system", "subtype": "restart",
                            "note": "会话进程重启,上文丢了"})
                        self.session_id = None
                self.start()
            msg = {"type": "user",
                   "message": {"role": "user",
                               "content": [{"type": "text", "text": text}]}}
            if self.session_id:
                msg["session_id"] = self.session_id
            try:
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                return
            except OSError:
                pass  # 写的当口进程正好断了,再走一轮重拉
        raise RuntimeError("总控会话进程写不进去")

    def get_events(self, after: int = 0) -> tuple[list, int]:
        """游标拉增量: 返回 (下标>=after 的事件, 下一个游标)。"""
        with self._lock:
            return list(self._events[after:]), len(self._events)

    # ------------------------------------------------------------ 内部

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
                    self.session_id = ev["session_id"]
                self._events.append(ev)
