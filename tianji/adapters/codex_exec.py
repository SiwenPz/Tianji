"""档 3 codex exec 进程兜底(6.1/7.4③): codex exec 进程级信号作为最后兜底。

档 1 钩子+档 2 转录均沉默时,监控器用本模块判定 codex 工人是否仍在执行。
- codex exec --json 输出的事件行翻译为统一事件 JSON(与档 1/2 同表同格式)。
- 进程存活检查: 平台相关实现,只读不干扰目标进程。

设计约束:
- fail-open: 本模块任何异常不阻塞监控器循环或钩子执行。
- 与档 1/2 解耦: 产出同一统一事件模型,ingest_event 内部保证 seq 单调+乱序不覆盖。
- 多实例: CODEX_HOME 隔离,转录路径由 transcript_parser 统一处理。
"""

from __future__ import annotations

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import template as tpl_mod


# ---------------------------------------------------------------------------
# 进程存活检查
# ---------------------------------------------------------------------------

def codex_exec_alive(pid: int) -> bool:
    """检查 codex exec 进程是否仍在运行(档 3 活性信号)。

    参数:
        pid: 进程 ID(来自 instance_registrations.pid)。

    返回:
        True=进程存活且为 codex exec; False=进程不存在/已退出/非 codex。
    """
    if not pid:
        return False
    try:
        if os.name == "nt":
            return _codex_alive_nt(pid)
        return _codex_alive_posix(pid)
    except Exception:
        return False


def _codex_alive_nt(pid: int) -> bool:
    """Windows: OpenProcess → WaitForSingleObject 消假活 → GetProcessImageFileName 确认 codex。"""
    import ctypes

    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102

    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not h:
        return False
    try:
        if k32.WaitForSingleObject(h, 0) == WAIT_OBJECT_0:
            return False  # 已退出
        # 查可执行文件路径(psapi.GetProcessImageFileNameA 兼容性优于
        # QueryFullProcessImageNameA: 后者在部分 Python/Windows 组合下
        # 返回 ERROR_INSUFFICIENT_BUFFER 且 needed 不填充)
        try:
            psapi = ctypes.windll.psapi
            buf = ctypes.create_string_buffer(260)
            n = psapi.GetProcessImageFileNameA(h, buf, 260)
            if n > 0:
                exe_path = buf.value.decode(errors="replace").lower()
                return "codex" in exe_path
        except Exception:
            pass
        return True  # 无法查路径但进程存活,保守按活
    finally:
        k32.CloseHandle(h)


def _codex_alive_posix(pid: int) -> bool:
    """Unix: /proc/<pid>/cmdline 或 ps 确认 codex exec。"""
    proc_dir = Path(f"/proc/{pid}")
    if proc_dir.is_dir():
        try:
            raw = (proc_dir / "cmdline").read_bytes().decode(errors="replace")
            parts = raw.split("\0")
            if parts:
                cmd = parts[0].lower()
                return "codex" in cmd
        except (OSError, PermissionError):
            pass
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            return "codex" in proc.stdout.strip().lower()
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# codex exec --json 输出事件读取(档 3 转录替代源)
# ---------------------------------------------------------------------------

def iter_codex_exec_events(
    session_id: str,
    home_dir: "Path | None" = None,
    max_lines: int = 200,
):
    """迭代 codex exec --json 输出中的事件行,翻译后 yield 统一事件 JSON。

    输出文件位置约定: CODEX_HOME/sessions/<session_id>/output.jsonl
    (与 rollout 转录同级目录,文件名基于 codex exec --json 实证布局)。

    参数:
        session_id: 会话 ID。
        home_dir: home 目录覆盖(测试用;生产默认为 Path.home())。
        max_lines: 最多读取行数(防无限增长会话撑爆内存)。

    生成:
        dict 统一事件 JSON(session_id, event_type, payload, is_interrupt)。
    """
    home = Path.home() if home_dir is None else home_dir
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    output_path = codex_home / "sessions" / session_id / "output.jsonl"

    if not output_path.is_file():
        return

    count = 0
    with open(output_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if count >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            count += 1
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = tpl_mod.translate("codex", raw)
            if event is not None:
                yield event
