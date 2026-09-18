# Command Code 初始化入口说明

这不是第二套初始化流程。所有宿主统一执行同目录 `BOOTSTRAP.md`。

Command Code 的已验证约束是：宿主自带模型菜单（`cmdc --list-models`），子代理通过原生 `agent` 工具派发，
角色文件即 `~/.commandcode/agents/tianji-*.md`。因此**不需要号池**，也不返回 `NEED_POOL`：菜单够用就
直接进入角色绑定。

## 安装

```
python install.py install --host cmdc
```

分两个事务，互不覆盖：

- 共享核心（`tianji` skill 树）→ `~/.agents/skills/`，受管文件清单 `shared-core.manifest.json`；
- Command Code 原生件（8 个角色 + 状态 mod）→ `~/.commandcode/`，受管清单 `tianji-cmdc.manifest.json`。

**不安装号池 skill**：模型来自本宿主自带菜单，所以 `tianji-proxy` 既不安装也不检查——装了就等于引一条
没人要的号池路线。

只更新清单里记录的文件；未受管文件不删；同版本同摘要跳过；不接受降级覆盖；用户改过的受管文件视为冲突，
默认保留，只有显式 `--force-overwrite` 才覆盖。卸载 CMDC 适配层不会删除共享核心。

## 角色绑定

模型来自 Command Code 当前实时菜单，由用户确认，不写死进共享角色模板：

```
python <skill目录>/scripts/cmdc-role-configure.py plan            # 看分层与当前绑定
python <skill目录>/scripts/cmdc-role-configure.py list            # 看真实菜单
python <skill目录>/scripts/cmdc-role-configure.py set-tier economy <model-id>
python <skill目录>/scripts/cmdc-role-configure.py set-tier quality <model-id>
python <skill目录>/scripts/cmdc-role-configure.py set-tier escalation <model-id>
python <skill目录>/scripts/cmdc-role-configure.py reset tianji-worker
python <skill目录>/scripts/cmdc-role-configure.py check           # 还有哪些 tier / 核心角色没绑
```

`set-tier` 一次把该层**已安装**的角色绑到同一个模型——这是本宿主的常规用法：从实时菜单里选三个模型，
各管一层。菜单里没有的模型会被直接拒绝，不会静默回落到默认模型。

`READY` 前必须绑定全部三个核心角色：`tianji-worker`、`tianji-verifier`、`tianji-referee`。

分层定义在共享契约 `skills/tianji/scripts/role_contract.py`，只描述能力、不指定厂商，本适配层不再重复定义：

- economy（低成本、量大、可重试）：worker——勘探/调研/复现/实现都是它的任务模式，靠任务书的只读模式区分
- quality（审核把关，必须明显强于 economy 模型）：verifier——三轴评审（Spec 需求忠实度 / Standards 规范与坏味道 / 正确性与风险）
- escalation（最强、额度最珍贵，清误报与审核结论冲突裁定时用）：referee

controller 不是可派发的子角色：它是父会话自己，没有角色包，也不安装。

### 改绑（随时可做）

```
python <skill目录>/scripts/cmdc-role-configure.py set-tier quality <model-id>
python <skill目录>/scripts/cmdc-role-configure.py check
```

装完之后不需要再传 `--source-root`：安装时把角色包所在位置记进了 `tianji-runtime.json`，改绑时自动读回。

**改绑会让旧证明立刻失效**——角色绑定摘要是证明的组成部分，所以改完必须重立证明，三步走：

1. 派一次已绑定角色做**只读自检**（任务书写明 `只读模式:是`，用 `run_registry.py open` 拿的标记派发）
2. `python <skill目录>/scripts/cmdc-routing-probe.py --workspace .` 收证据
3. `python <skill目录>/scripts/tianji-init.py --host cmdc` 应回到 `READY`

第 1 步只能由总控派发（脚本代替不了），所以改绑不是一条命令的事，但也不该有别的坑。

## 派工与任务标识

每次派工前先登记任务身份，EventKey 随任务下发，不在事后靠"当前任务"猜：

先问宿主"这次派工的配置是什么"：

```
python <skill目录>/scripts/cmdc-role-configure.py facts <角色>
# {"role": "tianji-worker", "model": "...", "model_source": "declared",
#  "subject_digest": "..."}
```

把返回的 `model` / `model_source` / `subject_digest` 原样交给 `open`：

```
python <skill目录>/scripts/run_registry.py open --workspace . --host cmdc \
    --session <会话id> --role tianji-worker --task-id <任务名> \
    --model <model> --model-source <model_source> --subject-digest <subject_digest> \
    --token-budget <按档位: T1 500000 / T2 1000000 / T3 2000000>
```

`open` 返回的 `run_id` / `task_id` / `attempt` 就是该任务的 EventKey，`claim_token` 对应的 `marker` 放进派工描述末尾。任务跑完必须关闭：

```
python <skill目录>/scripts/run_registry.py close-task --workspace . \
    --run-id <run_id> --task-id <任务名>
```

- `--model` / `--model-source` / `--subject-digest` 记的是**派工那一刻**的配置：账本报告的是这个值，路由证明也拿它比对。不带这些参数不会出错（回退到读取角色文件），但拿不到 host_dispatch 级别的证明。

- `--token-budget` 是**派发时写下的顶**，按档位给（T1 50 万 / T2 100 万 / T3 200 万）。收口时 `close-task` / `close-run` 会读账本报超支（`[OVER] 任务 角色: 预算 X 实花 Y (+N%)`），**只报不杀**——半途掐掉一个快干完的派发比超支更贵；没设顶的派发单独列成「未设预算」，那不是「在预算内」。状态栏最左的 `tok 花了/顶` 是同一笔账。派发时忘了带，就只能事后从看板的 Tokens 列自己加总——正是这套账要消灭的动作。

- **同一角色可以同时开多个任务。** 实测（2026-09-16）：同角色两席在一条消息里派出，两条活跃窗口真重叠、账本各自独立、不串账——身份按 **call id** 解析，不按角色，两席各带各的 marker。早先"一个角色同时只能有一个未关闭任务"的说法是**按角色解析身份时代的旧规矩，已经过时**，别再拿它当串行的理由：并行是被支持的，串行才是拖慢整个系统的原因。
- `close-task` 关闭该任务的 invocation 并释放那一席的绑定，同角色的其他席不受影响。
- `--task-id` 支持中文等非 ASCII 名称；路径分隔符、点号等仍会被拒绝。
- 同一会话里继续派工可复用同一个 run：`open ... --run-id <run_id>`。

## 路由证明

```
python <skill目录>/scripts/cmdc-routing-probe.py --workspace .
```

在派发过一次已绑定角色（通常 `tianji-worker` 的只读自检）之后运行。它按 EventKey 配对那次派发的
start/stop，写入派发证据。**任意已绑定角色的完成派发都算**，不再需要专门的角色。

Command Code 的子代理事件（`subagent_start/progress/stop`）**不携带上游模型标识**，所以证明最高只到
`host_dispatch`，并显式记录 `actual_model_verified: false`。菜单只能证明模型可选，角色文件只能证明声明绑定，
start/stop 配对只能证明宿主确实派发了该角色——这些都不等于"实际请求到达某个上游模型"。

`NEED_HOST_ADAPTER`、`NEED_ROLE_CONFIG` 或 `NEED_ROUTE_PROOF` 均回到共享引导对应步骤。
