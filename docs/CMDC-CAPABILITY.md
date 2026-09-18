# Command Code 宿主能力与证据边界

本文件记录 Command Code 适配层**已实证的事实**与**证据上限**，供评审与后续维护核对。
它不是第二套天机说明：编排、账本、验收、补偿与证明语义全部来自共享核心。

## 真机实证（Command Code 1.53.0）

在真实 Command Code 会话中完整跑通最小自举闭环：

| 步骤 | 结果 | 证据 |
|---|---|---|
| 安装 | 51 个共享文件 + 9 个原生件 | 真机安装 |
| 二次安装 | 0 写入、0 备份 | 真机安装 |
| 角色渲染 | 共享 `agents/tianji-*.md` → 8 个 Command Code agent | 真机安装 |
| 模型菜单 | `cmdc --list-models` → 70 个模型（含 `:free` 标记 id） | 真机 |
| 角色绑定 | 8 个角色全部绑定，用户逐个从真实菜单选定 | 真机 |
| 晨检 | `tianji-init.py --host cmdc` → 走共享结论机 | 真机 |
| **事件桥** | mod 真实写出 14 条标准 envelope 事件 | 真机派发探针 |
| **路由证明** | 配对 probe start/stop → `host_dispatch` | 真机 |
| 终态验收 | 显式 EventKey 写入，重复写入被拒（exit 3） | 真机 |
| 看板 | 共享 `board.py` 渲染，cmdc 与 legacy 混合不串 | 真机 |
| 最终结论 | `READY` | 真机晨检 |

### 真机观测到的事件字段（权威）

```
subagent_start:    toolCallId, subagentType, description, showOutput
subagent_progress: toolCallId, subagentType, toolName, toolInput, tokensUsed
subagent_stop:     toolCallId, subagentType, tokensUsed
tool_completed:    toolCallId, toolName, result
run_start:         sessionId
```

写出的 envelope 实例（真机原样，取自角色收敛前的一次探针派发；`tianji-probe` 现已并入
`tianji-worker`，此处保留原样是为了不篡改采集记录）：

```
{"event":"subagent_start","host":"cmdc","run_id":"80224243-...","task_id":"probe-selfcheck",
 "attempt":1,"correlation_id":"call_00_zvPZ4bdXUY1qInwQNdlw2375","agent":"tianji-probe",
 "event_id":"cmdc:b81d1cf9295f6378beab8e4e22178c00","schema_version":2}
```

### 真机确认的两个设计要点

1. **角色绑定回退生效**：派工的 `toolCallId` 在派发前不存在，绑定按角色写入；真机上
   correlation 未绑，事件靠 `agent=tianji-probe` 正确解出了 EventKey。
2. **同名并发不合并**：同一角色同时派发两次时，适配层拒绝把两次调用并到同一个 EventKey，
   宁可把第二条标为未归属（`legacy:true`），也不静默合并污染账本。

## 证据上限（诚实声明）

```
proof_level = host_dispatch
actual_model_verified = false
```

Command Code 子代理事件不提供可信的上游模型标识，因此：

- `/model` 菜单 → 只证明模型**可选**；
- agent 角色文件 → 只证明**声明绑定**；
- start/stop 配对 → 只证明宿主**确实派发了该角色**；
- 以上都不等于"实际请求由目标模型执行"。`wire_verified` 保持 false。

真机实测再次验证了这句警告：探针被声明绑往 `deepseek/deepseek-v4-flash`，
但探针**看不到任何上游模型标识**，只能如实回答"未知"，且宿主写入的字段显式标注
`model_source: declared`。声明 ≠ 实际，这是设计上的诚实边界。

共享证明模型把三者拆成独立证据域（`integrity` / `dispatch` / `wire`），当前 Command Code 只能填到
`dispatch`。一旦角色文件、模型菜单、适配层内容或宿主版本漂移，`integrity` 立即判失效，旧 `dispatch`
证明自动作废，必须重跑探针。

## 已知限制与未实测项（明确标注）

- **mod 需 `/reload` 或下次会话才加载**（Command Code 产品规则，非本项目缺陷）。
- **kill / 父会话退出 / 异常中断是否发出 `subagent_stop`**：仍未真机捕获。缺失的 stop 应由共享补偿
  （`state-log.py --compensate`）补齐，适配层不猜测。
- **交互 TUI 下的 footer 渲染**：mod 已接 `cmd.ui.setStatus`（API 已与官方文档核对）；headless 下不渲染，
  账本通道不受影响。真机 footer 外观尚未截图确认。footer 只报**原始事实**，不判"卡住 / 慢"——
  健康判定属共享层，适配层不自己下结论，也不写 `status_alert`。
  **格式（1 席）**：`tianji: <刻度> 角色@模型 任务 动作(工具+指向) 时长 步数`，**剥掉颜色码后逐字节保持原样**，例如
  `tianji: tok 1.3M/3.1M worker@deepseek-v4.1-flash 修瑕疵 read_file SKILL.md 1m20s 7步`。
  **格式（2 席及以上）**：`tianji: <刻度> ▸ 席位 │ 席位 │ …`，席位 = `角色 时长 步数 工具 指向[ !]`，例如
  `tianji: tok 10.2M/25.9M ▸ wor 10:00 12步 read_fi… run_registry_with_a… │ ver 9:00 13步 …`；
  此时**去掉 `@模型` 与任务号**——同角色的模型恒定（重复信息），任务号在最挤的时候最贵、信息密度最低
  （完整花名册看 `--board`）。**并行时所有在跑席位同时在行里**（不是轮播、不是只看前 2 个），
  席位**顺序按开始时间固定**（不跳动）。
  **整行有预算、由 mod 自己裁**（`FOOTER_WIDTH = 118` 列，按**可见宽度**算：CJK 占 2 列，`12步` 是 4 列）。
  宿主底栏只有一行、且**从右往左裁**，把长行交给宿主等于让最后一席整段消失（两席时第二席连 `+N`
  一起被吃掉）。**分额由总预算推出、不许手拍**：`角色 ≤3 · 时长 ≤5 · 步数 ≤4 · 工具 ≤13 · 指向可变`；
  工具那 13 是**本宿主最长工具名**（`shell_command`）的长度——预算 8 会把 `read_file` 剁成 `read_fi…`，
  工具名被剁就等于没说"这席在干什么"。超出就按**降级阶梯**削——指向 30→20→12→8 → 去掉指向 →
  工具名 13→9（9 是 `read_file` 的长度，低于它名字就不叫名字了）→ **整段丢掉动作、但一个席位都不减**
  （`+N` 会把人整个藏掉；"角色+时长+步数+标记"已足以看出谁卡住了）→ 席位数减到 4（保留**跑得最久**的，
  最可能是卡住的那个）+ `+N` → 整行自裁兜底。
  **卡住只看"多久没进度"**：`lastActivityMs` 距现在 >60s 加 `!`（琥珀）、>5min 加 `!!`（红-bold）；
  判定权仍属共享层，底栏只标这个原始事实。
  **颜色**：`setStatus` 的官方文档写明 "printed verbatim (**style it with ansi**)"，据此上色——
  前缀/刻度/分隔符/`+N`/指向 灰，角色按类分色（worker 绿 / verifier 蓝 / referee 棕），
  **耗时琥珀 · 步数紫 · 工具名青**（"在干什么"那句的主语该最亮 ✓），卡住标记琥珀/红，
  刻度自带 `!` 告警时整段转红。
  **颜色不占格子**：`displayWidth` 先剥逃逸序列再数；单测逐条断言"上色前后宽度相同"，并有暴力扫
  （4 角色 × 4 工具名 × 7 席位数 × 长指向 × 标记）证明没有组合能越过 118 列。
  **动作的"指向"**：非 shell 工具取路径末段；**shell 类工具（`shell_command`/`bash`/`cmd`/…）先归一成
  "命令第一个词 + 脚本名"**，不再塞整条命令行——实测一条 `cd …scratchpad && python -c "prin…`
  以前把 30 字的指向全花在临时路径上，等于没说，现在渲染成 `python`。
  **空闲（无席在跑）时整行只有 `tianji: 待命`**：没有在跑的派发就没有"此刻正在超"可言，
  刻度是运行时的仪表，停在待命上只是个要读过去的数字（顺带省掉每 5 秒问一次共享脚本的开销）。
  **最后动作**用宿主自己子代理视图的同款字段（`recentTools: {name, input}`）；
  每来一次子代理动作就立刻重画，所以它跟着子代理实时跳。**不重复报这条派发自己的 token**：
  行首刻度已经回答了"花了多少"（刻度是会话口径，逐席再报一遍在单席时就是同一个数）。
  模型取**认领时记录的绑定**（派工那一刻的事实），角色文件只在认领没带绑定时作显示兜底。
  代价：mod 重载后 footer 不再从账本恢复在跑工人（那等于第二个 reducer）。
  **未实测项**：`FOOTER_WIDTH = 118` 列未在真机底栏上实测宿主截断位置（照规划书取的数）；
  若宿主实际裁在别处，改这一个常量即可——其余分额都由它推导。118 列下 4 席会**丢掉动作但四席全在**
  （单测实测整行 88 列）；更挤才从第 5 席起 `+N`。保留席位的选择是刻意的：`+N` 藏人，丢动作不藏人。
- **底栏显示的是"步"，不是"轮次"——宿主不给轮次（已核对宿主源码）**：子代理的事件要过一层
  `subagentProgressTranslator` 才会变成 `subagent_progress`，而那层**只转发 `tool_queued`**
  （`text_delta`/`thinking_delta`/`tool_completed` 只用来累计 token，嵌套 run 的 `turn_start`/
  `turn_end` 直接被丢弃）；`subagent_stop` 的载荷也只有 `toolCallId`/`subagentType`/`tokensUsed`。
  宿主内部确有 `turnCount`，但它只用于拼给父会话看的那段 `<usage>` 文本，**不外发给 mod**。
  所以能归属、且随时可得的活数是**子代理的工具调用数**，footer 如实标成 `N步`——**不冒充轮次**。
  好处是它天然可归属：每个进度事件都带 `toolCallId`，并行多席各数各的（轮次做不到这点，若将来
  宿主转发轮边界，这一格应换回轮次）。
  另注：`tokensUsed` 同样是那层翻译器**估算**出来的（按流式文本 `estimateTokens2` 累加），
  与宿主工具结果里 `<usage>total_tokens` 的真实账**不是一个数**（实测同一趟派发：账本 71,956
  vs 工具自报 136,724）——底栏与预算用的都是前者，读到"接近上限"时应以收口报告的账本口径为准。
- **子代理的思维链 / 流式文本：mod 拿不到，宿主自己能看**（已核对宿主源码）。`agent` 工具在宿主内部
  确实在流式消费子代理的 `text_delta` / `thinking_delta`（还用它累计 token 估算），但那路数据喂给的是
  宿主**自己的子代理视图**；对 mod 只剩下被翻译过的 `subagent_progress`（只有工具名+入参）。
  子代理也**不落 transcript**：projects 目录里只多一个 `{"model": …}` 的 meta，没有 `.jsonl`。
  所以底栏做不到"逐字打印思维链"——能实时给的是**工具 + 指向哪**这一对事实（见上条）。
  要看流式输出，用宿主自己的视图：角色文件已渲染 `showOutput: true`（`role_renderer.py`），
  在该工具行展开即可看到子代理的实时输出。
- **同角色并发的完整支持**：当前是"拒绝合并"的保守行为；彻底支持需要共享核心提供
  "为角色预留一次调用、已占用则分配新 attempt"的原语。这是核心任务状态逻辑，不该由适配层决定。
- **探针角色无 shell 工具**（按设计只有 `file.read`）。给探针下带 shell 命令的任务书是任务书错误，
  不是系统缺陷——真机探针正确地拒绝了，没有编造输出。

## 边界不变量（有测试守护）

- 共享语义模块中不出现任何宿主名（`tests/test_shared_core_purity.py`）；
- `board.py` / `tianji-dash.py` 不感知宿主，不导入适配层；
- 适配层不定义结论机、任务 reducer、看板渲染、验收或补偿规则；
- 同一事件在 Python 参考 sink 与 Command Code mod 下派生同一 `event_id` 与 binding 路径
  （跨语言 fixture：`skills/tianji/schemas/ledger-identity.fixture.json`）。

## 复现路径

```
python install.py install --host cmdc
python <skill目录>/scripts/cmdc-role-configure.py plan
python <skill目录>/scripts/cmdc-role-configure.py set tianji-worker <真实菜单里的模型>
# 在宿主内派发一次 tianji-worker（任务书写明只读模式=是）
python <skill目录>/scripts/cmdc-routing-probe.py --workspace .
python <skill目录>/scripts/tianji-init.py --host cmdc   # 期望 READY
```
