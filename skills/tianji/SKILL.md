---

name: tianji
description: 天机——多宿主、多模型来源的 orchestrator-workers 编排入口；启动时只做环境晨检，按状态按需读取引导或运行协议。
---

# 天机：薄入口

## 触发与分流

用户明确说“天机”时：

1. 先运行一次晨检，不自行读取配置或猜测环境；`--host` 传当前宿主（Command Code
   可省略——宿主会注入运行时信号自动识别；kimi/codex 没有这种信号，必须显式传，
   识别不出直接报错）：
   python <skill-dir>/scripts/tianji-init.py --host <kimi|codex|cmdc>
2. 按晨检状态处理：
   - READY：若用户已给出具体任务，读取 RUNTIME-PROTOCOL.md 后进入编排；没有任务则报告就绪。
   - NEED_INSTALL：引导用户运行仓库根目录的 `python install.py install --host <kimi|codex|cmdc>`，完成后 /reload。
   - NEED_HOST_ADAPTER、NEED_ROLE_CONFIG、NEED_ROUTE_PROOF、NEED_POOL：只读取 BOOTSTRAP.md 中对应章节，完成引导后重新晨检。
   - DEGRADED：报告不可用渠道并停止派发。
3. 安装、同步、卸载天机本身由当前会话直接处理，不派 worker，也不套业务验收闭环。

## 业务编排总则

进入业务任务后，先读取 RUNTIME-PROTOCOL.md；角色定义或新增角色时再读取 ROLE-TOPOLOGY.md。不要在启动晨检阶段加载这些长文档。

不可变约束：

- 主会话拥有拆解、路由、重试判断、验收和最终整合权。
- worker 只执行明确任务，不嵌套派发；verifier 独立验收；冲突或重复失败才启用 referee。
- 每个派发任务必须有明确输入、输出、绝对路径/工作区、机械验收命令和实际模型证明要求。
- 验收必须有命令、退出码、输出或 diff 证据；PASS 才能交付。
- 失败必须有明确返工指向，最多两轮；仍失败则按故障处置升级，不把猜测留给用户。
- 模型切换、降级、跨厂商路由必须遵守当前宿主与用户授权，不能静默替换。
- 状态账本、失败补偿、真实模型证明和验收记录必须闭环。
- 派工与审核强度按**产物可逆性**分档（T0 自己做 / T1 只读产出免审核，主控直接收口 / T2 三轴 / T3 三轴评审按轴三席+裁判），不按工作量；预算同样按档写进账（T1 50 万 / T2 100 万 / T3 200 万，`open --token-budget`），收口超支**只报不杀**，状态栏最左显示 `tok 花了/顶`；档位判据见 RUNTIME-PROTOCOL.md「派工模式」。

## 宿主兼容

宿主差异只放在适配层：

- Codex：使用原生 named subagent、hooks、统一 Responses provider 和 agents/*.toml。约束见 CODEX-BOOTSTRAP.md。
- Kimi：使用共享 skill、角色包和宿主配置。
- Command Code：使用宿主自带模型菜单和原生 subagent，角色为 agents/*.md，事件桥由 status mod 承担。约束见 CMDC-BOOTSTRAP.md。
- 其他宿主：复用角色契约、路由、账本和验收协议，不复制宿主专属配置。

Command Code 不做号池引导（菜单够用即直接绑定角色），且其子代理事件不含上游模型标识，路由证明最高为
host_dispatch、actual_model_verified 恒为 false。

## 按需参考

- BOOTSTRAP.md：安装、号池/直连、角色绑定、路由证明和重新加载。
- CODEX-BOOTSTRAP.md / CMDC-BOOTSTRAP.md：宿主已实证约束，不是第二套流程。
- docs/CMDC-CAPABILITY.md：Command Code 已验证能力与证据上限。
- ROLE-TOPOLOGY.md：主控、worker、verifier、probe、referee 及扩展角色。
- RUNTIME-PROTOCOL.md：任务拆解、派发、机械验收、失败补偿、账本、上下文和模型降级的完整规则。
- USER-VALIDATION.md：用户只用自然语言验证天机时的黑盒流程；脚本由天机内部自动调用，用户不需要手动执行。
- templates/：新增角色时使用宿主无关模板，再由安装器生成宿主适配文件。

只要是业务任务，必须先读运行协议；只要是配置或宿主问题，按晨检状态只读对应引导章节。

用户要求“验证/测试天机”时，不要让用户敲 Python 命令。读取 USER-VALIDATION.md，在当前宿主内自动完成黑盒任务；用户只需提供正常业务请求并观察最终交付。
