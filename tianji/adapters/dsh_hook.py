"""dsh 壳适配器(6.2/6.5): 通用模板 + 通用运行器。

装钩子: dsh hook 配置指向本脚本。
启动形态(node.exe + bin.js,不用 .cmd shim)。
模型路由 --patch 覆盖。
沙箱默认 workspace-write 挡账本写入(模板 sandbox_allowlist 放行 TIANJI_HOME)。
中文 env GBK 坑: spawn 时 env 变量值须为 ASCII 或 UTF-8,避免 GBK 编码错误。
失败放行(fail-open,6.2): 钩子执行失败不阻塞助手干活。
"""

from .runner import main

if __name__ == "__main__":
    import sys
    sys.exit(main("dsh"))
