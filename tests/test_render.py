"""启动器渲染(render): Windows launch_cmd 解析(引号参数不破坏)+无头开窗判定。

2026-08-19 实证: shlex.split(posix=False) 保留引号(参数带 \\" 被程序当字面量),
posix=True 吃掉反斜杠路径;自定义 _win_split 保留路径+去引号。
无头壳(dsh headless/atomcode -p/kimi -p)不该开新窗口(CREATE_NO_WINDOW),
否则每次 spawn 冒黑屏 PowerShell 空窗攒多卡机。
"""

import json
import subprocess
import pytest

from tianji.render import _win_split, _is_headless_cmd, _spawn_flags, \
    _resolve_language, _render_taskbook, _TASKBOOK_TEMPLATES, \
    TASKBOOK_TEMPLATE_ZH, TASKBOOK_TEMPLATE_EN


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
    """dsh headless launch_cmd 判定为无头,拉起用 CREATE_NO_WINDOW(黑屏窗口消除)
    + CREATE_NEW_PROCESS_GROUP(脱离宿主控制台进程组,防信号串扰)。"""
    cmd = (
        r'D:\soft\nodejs\node.exe C:\...\dsh\lib\bin.js --profile headless '
        r'--patch D:\p\hl.yml "你是张辽，中途不停。"'
    )
    assert _is_headless_cmd(cmd) is True
    assert _spawn_flags(cmd) == (
        subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)


def test_headless_atomcode_prompt_no_window():
    """atomcode -p 无头判定,静默后台。"""
    cmd = r'D:\soft\atomcode\atomcode.exe --lang zh-CN -y -p "你是实施者。"'
    assert _is_headless_cmd(cmd) is True
    assert _spawn_flags(cmd) == (
        subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)


def test_headless_kimi_prompt_no_window():
    """kimi -p 无头判定,静默后台。"""
    cmd = r'kimi -p "你是审核者。"'
    assert _is_headless_cmd(cmd) is True
    assert _spawn_flags(cmd) == (
        subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)


def test_interactive_claude_keeps_console():
    """claude 交互壳(无无头参数)保留 CREATE_NEW_CONSOLE 真窗口。"""
    cmd = (
        r'C:\...\claude.exe --settings D:\s.json '
        r'"你是审核者司马懿，保持窗口。"'
    )
    assert _is_headless_cmd(cmd) is False
    assert _spawn_flags(cmd) == subprocess.CREATE_NEW_CONSOLE


# ---------------------------------------------------------------- 票 52: 语言跟随

def _seed_minimal_task(conn):
    """插入最小任务+派单记录供 _render_taskbook 测试。"""
    from tianji.db import now
    conn.execute(
        "INSERT OR IGNORE INTO tasks (id, title, description, verify_cmd, "
        "scope_guard, project_dir, created_at, updated_at) "
        "VALUES (1,'测试任务','请执行测试','echo ok','[]','',?,?)", (now(), now()))
    conn.execute(
        "INSERT OR IGNORE INTO dispatches (id, task_id, worker_id, worker_role, "
        "status, payload, task_dir, expect_min, dcap_hash, created_at, updated_at) "
        "VALUES (1,1,'测试员','worker','issued','{}','/tmp/task1',30,'',?,?)",
        (now(), now()))
    conn.commit()


def test_resolve_language_default(conn):
    """默认语言为中文(zh)。"""
    lang = _resolve_language(conn)
    assert lang == "zh"


def test_resolve_language_set(conn):
    """设置语言后读取正确。"""
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) "
        "VALUES ('user_language','en',1)")
    lang = _resolve_language(conn)
    assert lang == "en"


def test_render_taskbook_zh(conn):
    """用户语言=中文时,任务书含中文回报纪律/求助纪律。"""
    from tianji.ops import ensure_defaults
    ensure_defaults(conn)
    _seed_minimal_task(conn)
    task = conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()
    dispatch = conn.execute("SELECT * FROM dispatches WHERE id=1").fetchone()
    # 确保语言=中文
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) "
        "VALUES ('user_language','zh',1)")
    result = _render_taskbook(conn, dict(dispatch), dict(task), "/tmp/report.md")
    assert "回报纪律" in result
    assert "求助纪律" in result
    assert "任务书" in result
    assert "语言回退" not in result


def test_render_taskbook_en(conn):
    """用户语言=英文时,任务书含英文回报纪律/求助纪律。"""
    from tianji.ops import ensure_defaults
    ensure_defaults(conn)
    _seed_minimal_task(conn)
    task = conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()
    dispatch = conn.execute("SELECT * FROM dispatches WHERE id=1").fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) "
        "VALUES ('user_language','en',1)")
    result = _render_taskbook(conn, dict(dispatch), dict(task), "/tmp/report.md")
    assert "Reporting Discipline" in result
    assert "Help Discipline" in result
    assert "Dispatch" in result
    assert "语言回退" not in result


def test_render_taskbook_fallback_language(conn):
    """不支持的语种回退中文并标注回退。"""
    from tianji.ops import ensure_defaults
    ensure_defaults(conn)
    _seed_minimal_task(conn)
    task = conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()
    dispatch = conn.execute("SELECT * FROM dispatches WHERE id=1").fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) "
        "VALUES ('user_language','ja',1)")
    result = _render_taskbook(conn, dict(dispatch), dict(task), "/tmp/report.md")
    assert "回报纪律" in result  # 中文内容
    assert "语言回退" in result  # 标注回退
    assert "ja" in result


def test_render_taskbook_en_reviewer(conn):
    """英文下审核者任务书含英文审核段。"""
    from tianji.ops import ensure_defaults
    from tianji.db import now
    ensure_defaults(conn)
    # 先有一个已结算的工人派单作被审对象
    conn.execute(
        "INSERT OR IGNORE INTO tasks (id, title, description, verify_cmd, "
        "scope_guard, project_dir, created_at, updated_at) "
        "VALUES (2,'审核测试','test','echo ok','[]','',?,?)", (now(), now()))
    conn.execute(
        "INSERT OR IGNORE INTO dispatches (id, task_id, worker_id, worker_role, "
        "status, payload, task_dir, expect_min, dcap_hash, created_at, updated_at) "
        "VALUES (2,2,'工人甲','worker','done','{}','/tmp/task2',30,'',?,?)",
        (now(), now()))
    conn.execute(
        "INSERT OR IGNORE INTO dispatches (id, task_id, worker_id, worker_role, "
        "status, payload, task_dir, expect_min, dcap_hash, created_at, updated_at, axis) "
        "VALUES (3,2,'审核员','reviewer','issued','{}','/tmp/task3',30,'',?,?,?)",
        (now(), now(), "spec"))
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) "
        "VALUES ('user_language','en',1)")
    task = conn.execute("SELECT * FROM tasks WHERE id=2").fetchone()
    dispatch = conn.execute("SELECT * FROM dispatches WHERE id=3").fetchone()
    result = _render_taskbook(conn, dict(dispatch), dict(task), "/tmp/report.md")
    assert "Artifact Under Review" in result
    assert "Reporting Discipline" in result
    assert "语言回退" not in result
