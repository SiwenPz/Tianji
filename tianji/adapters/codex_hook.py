"""codex 壳适配器(6.2/6.5): 通用模板 + 通用运行器。

装钩子: codex config.toml features.hooks 配置指向本脚本。
0.146.1 实证有 SessionEnd;Stop=每轮结束(非会话结束)。
完成判定: codex 的 Stop 不作完成信号(每轮结束≠任务完成,模板 completion.stop_is_completion=False),以 SessionEnd 为准。
多实例=CODEX_HOME 隔离(模板 transcript.glob 取 ~/.codex/sessions/)。
失败放行(fail-open,6.2): 钩子执行失败不阻塞助手干活。
"""

from .runner import main

if __name__ == "__main__":
    import sys
    sys.exit(main("codex"))
