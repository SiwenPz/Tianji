"""claude 壳适配器(6.2/6.5): 通用模板 + 通用运行器。

装钩子: claude settings.json hooks 配置 SessionStart 等事件指向本脚本。
模板事件名与 SHOULD_LIST_CLAUDE["hooks"] 一致(8 类公共交集)。
失败放行(fail-open,6.2): 钩子执行失败不阻塞助手干活。
"""

from .runner import main

if __name__ == "__main__":
    import sys
    sys.exit(main("claude"))
