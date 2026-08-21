"""通用钩子运行器(6.2): 读 stdin JSON → 翻译 → ingest-event,失败放行。

所有壳适配器共用此运行器,不再每个适配器复制 main() 逻辑。
"""

from __future__ import annotations

import json
import subprocess
import sys

from .template import translate


def main(shell: str) -> int:
    """壳钩子入口(fail-open): stdin 单行 JSON → 统一事件 JSON → ingest-event。

    参数:
        shell: 壳名(模板注册名,如 claude/codex/dsh)。

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
        return 0
    except Exception as e:
        sys.stderr.write(f"[tianji-hook:{shell}] fail-open: {e}\n")
        return 0


if __name__ == "__main__":
    import os
    _shell = os.environ.get("TIANJI_SHELL", "claude")
    sys.exit(main(_shell))
