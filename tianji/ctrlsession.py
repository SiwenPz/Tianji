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

import threading

from tianji.ctrlprotocols import ClaudeStreamBackend


class ControllerSession:
    """薄壳: 透明委托给 ctrlprotocols.ClaudeStreamBackend。

    兼容旧接口(旧 test / webapp 用):
    - `ControllerSession(cmd_override=None, home=None)` — 构造
    - `start(home=None)` — 挂后端;session_id/proc 全走 backend
    - `send / get_events / is_alive / close` — 委托

    ★ 参数说明:
    cmd_override: 测试注入的假进程命令(给了就不走 claude 那套)。
    home: 会话的 home 根(默认 None,start 时或走 TIANJI_HOME)。
    """

    __slots__ = ("_backend",)

    def __init__(self, cmd_override: list | None = None, home=None):
        # 不立刻 start(避免 __init__ 里读不到 settings 炸)
        # cmd_override → launch;None → 由 start() 时走默认 ["claude"]
        if cmd_override is not None:
            launch: list = list(cmd_override)
        else:
            launch = ["claude"]
        self._backend: ClaudeStreamBackend = ClaudeStreamBackend(
            home=home, launch=launch,
            data_root_env=None, key_env_style="cli-env",
        )

    # ------------------------------------------------------------ 生命周期

    def start(self, home=None):
        """拉起子进程;cwd=home(总控的账本根,不是仓库根)。"""
        self._backend.start(home=home)

    def is_alive(self) -> bool:
        return self._backend.is_alive()

    def close(self):
        """stdin EOF → 宽限 5s → terminate → Windows 整树 taskkill /T /F。"""
        self._backend.close()

    # ------------------------------------------------------------ 收发

    def send(self, text: str):
        """组 user 消息写 stdin;进程没起/死了就重拉(死了先记重启事件)。"""
        self._backend.send(text)

    def get_events(self, after: int = 0) -> tuple[list, int]:
        """游标拉增量: 返回 (下标>=after 的事件, 下一个游标)。"""
        return self._backend.get_events(after=after)

    # ------------------------------------------------------------ 兼容访问器 (读了不炸)

    # ★ 旧 test / webapp 直接读 .session_id/.proc/.session_id/.session_id
    # 透明委托;setter 不进 backend (backend 管自己的 _session_id)
    # 写入走 send() 里的 self._backend._session_id,不经过这里

    @property
    def session_id(self) -> str | None:
        return self._backend.session_id

    @property
    def proc(self):
        return self._backend.proc

    @property
    def home(self):
        return self._backend.home

    @home.setter
    def home(self, value):
        self._backend.home = value
