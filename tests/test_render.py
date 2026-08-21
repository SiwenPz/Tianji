"""启动器渲染(render): Windows launch_cmd 解析(引号参数不破坏)+无头开窗判定。

2026-08-19 实证: shlex.split(posix=False) 保留引号(参数带 \\" 被程序当字面量),
posix=True 吃掉反斜杠路径;自定义 _win_split 保留路径+去引号。
无头壳(dsh headless/atomcode -p/kimi -p)不该开新窗口(CREATE_NO_WINDOW),
否则每次 spawn 冒黑屏 PowerShell 空窗攒多卡机。
"""

import subprocess
import pytest

from tianji.render import _win_split, _is_headless_cmd, _spawn_flags


def test_win_split_keeps_backslash_path():
    """Windows 路径反斜杠保留,不被打散。"""
    cmd = r"D:\soft\atomcode\atomcode.exe --lang zh-CN -y"
    parts = _win_split(cmd)
    assert parts == [r"D:\soft\atomcode\atomcode.exe", "--lang", "zh-CN", "-y"]


def test_win_split_quoted_prompt_strips_quotes():
    """双引号包裹的提示词: 引号去掉、内部反斜杠与空格保留为单参数。"""
    cmd = (
        r'D:\soft\atomcode\atomcode.exe -p "你是天机实施者，读 '
        r'TIANJI_TASK_PATH 的任务书。"'
    )
    parts = _win_split(cmd)
    assert parts[0] == r"D:\soft\atomcode\atomcode.exe"
    assert parts[1] == "-p"
    assert parts[2] == "你是天机实施者，读 TIANJI_TASK_PATH 的任务书。"
    assert len(parts) == 3


def test_win_split_mixed_flags_and_quotes():
    """混合无引号 flag 与带引号长参数。"""
    cmd = (
        r'C:\node\bin.js --profile headless --patch D:\p\hl.yml '
        r'"你是张辽，中途不停。"'
    )
    parts = _win_split(cmd)
    assert parts == [
        r"C:\node\bin.js", "--profile", "headless",
        "--patch", r"D:\p\hl.yml", "你是张辽，中途不停。",
    ]


def test_headless_dsh_no_window():
    """dsh headless launch_cmd 判定为无头,拉起用 CREATE_NO_WINDOW(黑屏窗口消除)。"""
    cmd = (
        r'D:\soft\nodejs\node.exe C:\...\dsh\lib\bin.js --profile headless '
        r'--patch D:\p\hl.yml "你是张辽，中途不停。"'
    )
    assert _is_headless_cmd(cmd) is True
    assert _spawn_flags(cmd) == subprocess.CREATE_NO_WINDOW


def test_headless_atomcode_prompt_no_window():
    """atomcode -p 无头判定,静默后台。"""
    cmd = r'D:\soft\atomcode\atomcode.exe --lang zh-CN -y -p "你是实施者。"'
    assert _is_headless_cmd(cmd) is True
    assert _spawn_flags(cmd) == subprocess.CREATE_NO_WINDOW


def test_headless_kimi_prompt_no_window():
    """kimi -p 无头判定,静默后台。"""
    cmd = r'kimi -p "你是审核者。"'
    assert _is_headless_cmd(cmd) is True
    assert _spawn_flags(cmd) == subprocess.CREATE_NO_WINDOW


def test_interactive_claude_keeps_console():
    """claude 交互壳(无无头参数)保留 CREATE_NEW_CONSOLE 真窗口。"""
    cmd = (
        r'C:\...\claude.exe --settings D:\s.json '
        r'"你是审核者司马懿，保持窗口。"'
    )
    assert _is_headless_cmd(cmd) is False
    assert _spawn_flags(cmd) == subprocess.CREATE_NEW_CONSOLE
