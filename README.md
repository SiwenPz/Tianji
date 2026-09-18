# 天机 Tianji

天机是一个多宿主、多模型来源的本地编排系统。

它把任务判断、拆解、路由、角色协作、真实模型证明、失败补偿、机械验收和运行账本统一起来；Kimi、Codex 等宿主只提供接入适配，不各自维护一套天机逻辑。

## 用户怎么用

完成一次安装和宿主重载后，用户只需要正常描述任务：

~~~text
天机，检查当前项目的登录逻辑，修复发现的问题，补充测试并运行相关测试，最后汇报修改和风险。
~~~

天机会在内部自动完成：

~~~text
晨检 -> 模型/路由确认 -> 主控拆解 -> worker 执行
    -> 只读产出(定位/调研): 主控直接收口  |  写入类: verifier 独立验收
    -> 失败返工或补偿 -> 账本记录 -> 结果交付
~~~

用户不需要手动运行探针、账本或验收脚本，也不需要指定 worker 或 verifier。

## 安装

### Kimi

在仓库根目录执行一次：

~~~bash
python install.py install --host kimi
python install.py status --host kimi
~~~

`--host` 传当前宿主。省略时只认宿主进程自己注入的运行时信号（Command Code 可自动识别；
kimi/codex 没有这种信号，必须显式传），识别不出就报错要求显式指定——不会默认成 kimi。
配置目录（`CODEX_HOME`/`KIMI_CODE_HOME`）只说明装过哪个宿主，不当作宿主身份。

然后重新加载 Kimi 宿主，在新会话中说：

~~~text
天机
~~~

天机会引导模型池、角色配置和首次验证。

### Codex

在仓库根目录执行：

~~~bash
python install.py install --host codex
python install.py status --host codex
~~~

重启 Codex，并在 /hooks 中确认 Tianji hooks 后，在新会话中说：

~~~text
天机
~~~

Codex 接入使用原生 named subagent、hooks 和 Responses provider。模型选择、角色绑定和路由切换不会被静默猜测，首次配置需要明确确认。

## 架构

~~~text
共享天机主体
├── 主控编排与任务拆解
├── RoleContract 角色契约
├── 模型池与跨厂商路由
├── 真实模型证明
├── 状态账本
├── 机械验收
├── 失败补偿
└── 用户黑盒验收流程

HostAdapter
├── Kimi：Markdown 角色包、Kimi 配置和宿主能力
└── Codex：TOML 角色、原生 subagent、hooks 和 Responses provider
~~~

角色定义只有一份共享来源：

~~~text
agents/tianji-*.md
        ↓
skills/tianji/scripts/role_contract.py
        ├── Kimi Markdown
        └── Codex TOML
~~~

## 角色

三个角色，一档一个。角色是"值班岗位"不是"工种"——勘探、调研、复现、实现都是 worker 的任务模式，由任务书的**只读模式**字段区分，不再各占一个角色名：

- tianji-worker：执行档。勘探/调研/复现（只读模式=是）与实现（只读模式=否）都派它
- tianji-verifier：质量档。三轴评审——Spec（对着需求原文查漏做/多做/做偏）、Standards（项目成文规范 + 坏味道基线）、正确性与风险（测试真伪 + diff 审查 + 独立挑错）。三轴**分开成文、不合榜**，一轴挂就带该轴返工指向打回
- tianji-referee：最高档。**清误报是常设工序**（只收被独立复现的发现，不许提出者自证）；多个审核结论冲突时另行终裁

机械验收是**主控跑的闸门**（确定性命令不必占审核席），审核员仍要自己复跑命令、不采信转述。安全面（认证/授权/会话/加密/权限模型/凭据）的改动，审核席必须含最强档。

路由实证不是角色：晨检或换绑后，由主控派一次 worker 做只读自检，再跑证明脚本收证据。证明认"任意已绑定角色的一次完成派发"。

主会话始终拥有拆解、路由、重试判断、验收和最终整合权。worker 不嵌套派发。**写入类（T2/T3）交付前必须有 verifier PASS；只读定位/调研（T1）免审核席，由主控当场收口**——审核按后果可逆性分档，不按产物类型。

## 派工分档与预算

派不派工、审到什么程度，按**产物可逆性**分档，不按工作量：

| 档 | 判据 | 流程 | 派工时写下的预算 |
|---|---|---|---|
| T0 | 一两眼能定（接线、一行级修复） | 主会话直接做，不派工不记账 | — |
| T1 | 只读产出（读码出报告/定位/调研/复现） | 1 个 worker，**免审核**：主控直接收口（结论要拿去行动时当场自核 ≤3 条） | 50 万 token |
| T2 | 改文件，不碰共享核心/配置 | worker + 三轴评审 + 闸门 | 100 万 |
| T3 | 核心机制/配置写入/删除/安全面 | worker + 三轴评审（大 diff 按轴三席并行）+ 裁判 | 200 万 |

预算在派发时写进账本（`open --token-budget`），收口时读账本按次报超支，**只报不杀**——半途掐掉一个快干完的派发比超支更贵。没设顶的派发单独列成"未设预算"：**"没设顶"和"在顶内"是两回事**。终端底栏的 `tok 花了/顶` 是同一笔账：**正在跑**的派发超顶时变红、显示那一次自己的比例、并移到整行最前（免得被截断）；已结束的超支不在底栏挂红——那是过去的事，由收口报告和看板讲。

## 用户可见的验收标准

一次正常任务必须能证明：

- 修改真实落盘
- 测试真实执行
- 能显示实际使用的模型和路由
- 测试失败时不会伪装成成功
- 失败后能有限返工或补偿
- 最终结果包含修改文件、测试结果和剩余风险

完整黑盒验收规则见 skills/tianji/USER-VALIDATION.md。

## 项目结构

~~~text
install.py                         # 多宿主安装、状态、卸载和幂等同步
agents/                            # 共享角色源文件
skills/tianji/                     # 天机主 Skill 和运行协议
skills/tianji/scripts/role_contract.py
                                   # 共享角色契约与宿主渲染
skills/tianji-proxy/               # 本地模型代理和跨厂商路由
docs/                              # 宿主能力说明
tests/                             # 共享协议与宿主回归测试
~~~

## 开发验证

~~~bash
python -m py_compile install.py
python -m unittest discover -s tests -p "test_*.py"
~~~

## 文档

- 宿主能力与接入证据：docs/CMDC-CAPABILITY.md、docs/CODEX-CAPABILITY.md
- 角色拓扑：skills/tianji/ROLE-TOPOLOGY.md
- 运行协议：skills/tianji/RUNTIME-PROTOCOL.md
- 用户黑盒验收：skills/tianji/USER-VALIDATION.md

## 依赖

- Kimi Code 或 Codex 宿主
- Python 3.11+
- 可用的模型池、代理或直连模型来源

## License

MIT License，全文见 [LICENSE](LICENSE)。仓库不含第三方代码。

MIT
