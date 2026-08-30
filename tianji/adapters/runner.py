"""通用钩子运行器(6.2): 读 stdin JSON → 翻译 → ingest-event,失败放行。

所有壳适配器共用此运行器,不再每个适配器复制 main() 逻辑。
权限请求(permission_request)先走 ingest 入库(进账本待裁决+留审计),
再查账本裁决把 allow/deny 按壳格式写 stdout(6.6 fail-closed 3s)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

try:
    from .template import get_template, translate
except ImportError:
    # hooks.py 分发的是 runner.py 的独立脚本副本(17.1),直接执行时无包上下文
    from tianji.adapters.template import get_template, translate


_PERM_DEADLINE = 3.0  # fail-closed 超时(6.6)


def main(shell: str) -> int:
    """壳钩子入口(fail-open): stdin 单行 JSON → 统一事件 JSON → ingest-event。

    权限请求(permission_request)同样先 ingest 入库(6.6 待裁决+审计),
    随后查账本裁决输出钩子应答(fail-closed 3s,无 allowed 裁决即拒)。

    返回:
        始终返回 0(fail-open,6.2);错误写 stderr 供排查。
    """
    try:
        line = sys.stdin.readline()
        if not line:
            return 0
        hook = json.loads(line.strip())
        event = translate(shell, hook)
        if event is None:
            return 0  # 非交集事件,忽略不阻塞
        _ingest(shell, event)
        # permission_request: 入库后查账本裁决应答(6.6 fail-closed 3s)
        if event.get("event_type") == "permission_request":
            return _handle_permission(shell, hook)
        return 0
    except Exception as e:
        sys.stderr.write(f"[tianji-hook:{shell}] fail-open: {e}\n")
        return 0


def _ingest(shell: str, event: dict) -> None:
    """统一事件 → ingest-event 子进程(fail-open: 失败只留 stderr,不阻断壳)。"""
    proc = subprocess.run(
        [sys.executable, "-m", "tianji", "ingest-event"],
        input=json.dumps(event, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"[tianji-hook:{shell}] ingest failed: {proc.stderr}\n"
        )


def _deny_response(shell: str) -> dict:
    """兜底 deny: 按壳模板 permission_slot.hook_response_format 生成(不依赖账本)。

    账本不可达/查询超时/参数异常时也要给出该壳能懂的 deny 格式:
    claude/atomcode=cc,codex=bare(6.6 data-driven,不再按壳名硬编码)。
    """
    fmt = "cc"
    try:
        slot = get_template(shell).permission_slot or {}
        fmt = slot.get("hook_response_format", "cc")
    except KeyError:
        pass
    if fmt == "bare":
        return {"decision": "deny"}
    return {"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "deny"}}}


def _handle_permission(shell: str, hook: dict) -> int:
    """permission_request 钩子应答(6.6 fail-closed 3s): 查账本裁决,无 allowed 即拒。

    connect+查询放线程内做(SQLite 连接不跨线程);连接一次失败即 fail-closed,
    不做重试风暴。tool 取真实载荷键 tool_name(与 events.py 入库口径一致)。
    """
    from tianji import permission
    from tianji.db import connect
    worker_id = os.environ.get("TIANJI_WORKER_ID", "")
    tool = hook.get("tool") or hook.get("tool_name") or ""
    resp_box = {}
    err_box = {}

    def _lookup():
        conn = None
        try:
            conn = connect()
            resp_box["r"] = permission.hook_response(
                conn, shell, worker_id, tool)
        except PermissionError as e:
            err_box["e"] = ("permission_deny", str(e))
        except Exception as e:
            err_box["e"] = ("fail_closed", str(e))
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    t = threading.Thread(target=_lookup, daemon=True)
    t.start()
    t.join(timeout=_PERM_DEADLINE)
    if t.is_alive():
        resp = _deny_response(shell)
        sys.stderr.write(
            f"[tianji-hook:{shell}] fail-closed: permission lookup timeout\n")
    elif "e" in err_box:
        kind, msg = err_box["e"]
        tag = "permission deny" if kind == "permission_deny" else "fail-closed"
        sys.stderr.write(f"[tianji-hook:{shell}] {tag}: {msg}\n")
        resp = _deny_response(shell)
    else:
        resp = resp_box.get("r") or _deny_response(shell)
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.environ.get("TIANJI_SHELL", "claude")))
