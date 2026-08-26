# 票 46：壳差异数据化收敛（权限/转录定位/总控 settings/binding/隔离/思考级别/适配器 去壳名硬编码）

> 你现在应该在本票分支 `ticket-46-shell-convergence` 上（本文件就在分支里）。`git status` 确认。
> 注意：若你开工早于本文件入库，push 前先 `git pull --rebase origin ticket-46-shell-convergence`（分支上补充过派活材料，纯新增文件，无冲突）。

天机是一个管 AI 编程助手的协作框架：账本(SQLite)记角色/实例/任务/审计，把不同助手壳和模型供应商按稳定组合拉进"派活→监控→验收→返工"流水线。当前在 v0.7.0 基础上继续迭代。

## 目标

把通用控制流里残留的 `if shell == "xxx"` 硬编码**全部**改为数据驱动。验收口径一句话：**新增壳 = 只写一份壳条目数据，不改通用控制流**。参照基准 = 已收敛的 `tianji/shellrender.py` 的 `RENDERERS` / `tianji/ctrlprotocols.py` 的 `BACKENDS`（注册表分发形态）。

壳条目唯一真源 = 账本里的 `integration_shell:*` 条目（迁移期兼容读 `shell:*`）；出厂模板 `tianji/adapters/template.py`（现有 6 家：TEMPLATE_CLAUDE / TEMPLATE_CODEX / TEMPLATE_KIMI / TEMPLATE_ATOMCODE / TEMPLATE_CLINE / TEMPLATE_DSH）只作为"内置壳首次入库/旧条目升级"时的预填数据源；运行时一律读壳条目，缺必填字段 fail-loud（不回落壳名）。

## 收敛块（共 10 块，字段定义一律以下方附录 E 为准）

1. **permission.py 权限执行**：删 `HOOK_ALLOW_SHELLS` 元组与全部 `if shell ==` 分支；`hook_response()` / `_rewrite_rules_file()` / `decide()` 改读壳条目 `permission_slot`（附录 E.2 的 mechanism / hook_response_format / hook_action / rules / headless 完整 schema）。
2. **transcript 定位**：`tianji/adapters/transcript_parser.py` 的 `transcript_path()` 统一走壳条目 `transcript`（附录 E.3：roots 有序列表 + glob 有序列表 + source_type + authoritative_source），删 `_codex_path / _dsh_path / _kimi_path / _atomcode_path / _cline_path` 五个壳名路径函数；`tianji/monitor.py` 删 `if shell == "claude"` 分支与 codex 档3进程兜底特判（`if shell == "codex" and r["pid"]` 那处，改读 `capabilities.tier3_process_alive`），统一委托 transcript_parser，monitor 相关函数签名改成能传实例上下文。
3. **总控 settings 写入**：`tianji/wizard.py` 的 `_write_controller_settings()` 合并 claude/kimi/generic 三分支为单一数据驱动实现，读壳条目 `controller_settings`（role_text_target / permissions）+ `provider_env`（target / map，附录 E.4）；`tianji/webapp.py` 的 `api_setup_controller` 三分支删除；删 `ctrlprotocols._KEY_ENV_MAPPERS`（key_env_style 迁到 provider_env，迁移规则见附录 E.4 末段）。
4. **binding 分类**：删 `ENV_BINDING_SHELLS` / `CONFIG_BINDING_SHELLS` 元组；`add_instance()` 先读持久化壳条目再校验 binding / protocols / renderer。
5. **renderer 形态分发**：`RENDERERS` 键从壳名改为形态名（附录 E.5：claude / codex / config_binding 三形态）；kimi/atomcode/cline/dsh 四家复用 config_binding 形态。
6. **思考级别映射（13.3）**：统一进壳条目 `thinking_level_map`；支持的壳正确注入，不支持的如实记"不支持"进实例档案。
7. **codex 死配置清理（6.4）**：`TEMPLATE_CODEX` 的 `stop_is_completion=True` 与 `tianji/adapters/codex_hook.py` 注释"codex=Stop 生效"和实证修正相反，且该字段只被 kimi 的 wire 纠偏路径读 = 死配置。清掉，`completion` 字段语义按附录 E.7（codex 的 stop_is_completion=False）。
8. **适配器形态（6.2）**：适配器统一为"通用 runner + 壳模板翻译"（壳条目 `adapter.template`），不再要求每壳一个 `{shell}_hook.py` 文件；装钩子不因缺 `{shell}_hook.py` 而漏装。
9. **子agent事件缺口降级（6.3）**：壳条目声明子agent事件能力（能力静态声明），monitor 活性判定按此声明降级（不误杀）。
10. **hooks.py statusline 特判**：`tianji/hooks.py` 里 claude 的 statusline 特判分支改读 `capabilities.statusline`（附录 E.7），有 enabled 才注册 statusline artifact。

## 附录 E：壳条目完整 schema（字段定义唯一依据，不得偏离、不得重复定义）

### E.1 唯一真源与迁移

- 唯一真源=壳条目 `integration_shell:*`（迁移期兼容读 `shell:*`）。
- 壳模板=出厂预填数据源，只在"内置壳首次入库/旧条目升级"时写进壳条目。
- 迁移：内置旧 `shell:*` 缺新字段→用出厂模板补齐/升级（自动）；自定义壳缺必填字段→fail-loud 拒绝接入（不回落壳名）。

### E.2 permission_slot

```
permission_slot: {
  "mechanism": "hook_allow" | "rule_table" | "auto_approve",
  "hook_response_format": "cc" | "bare",
  "hook_action": "none" | "deny" | "cancel",
  "rules": {"file": "permission-rules.json" | "autoapprove.json",
             "format": "allow_deny_lists_v1" | "autoapprove_v1"},
  "headless": {"policy": "deny" | "approve" | "suspend",
                "fallback": "hook_deny" | "static_rules_deny" | "timeout_kill",
                "timeout_seconds": {"config": "permission_cline_timeout", "default": 120}}
}
```

规则文件体（定死）：
- `allow_deny_lists_v1`: `{"allow": [...], "deny": [...], "note": "..."}`
- `autoapprove_v1`: `{"autoApprove": [...], "deny": [...], "note": "..."}`

数组规则（通用）：写入前去重、字典序排序；空数组合法；未知字段值原样保留（天机只做白名单裁决，不解析语义）。

条件校验矩阵：

| mechanism | 必填 | 禁填 | hook_action |
|---|---|---|---|
| hook_allow | hook_response_format | rules | deny |
| rule_table | rules{file,format} | hook_response_format | deny |
| auto_approve | rules{file,format} | hook_response_format | cancel |

`format` 与 `file` 成对绑定（allow_deny_lists_v1↔permission-rules.json；autoapprove_v1↔autoapprove.json），不成对=fail-loud。

headless 组合矩阵：

| mechanism | policy | fallback |
|---|---|---|
| hook_allow | deny | hook_deny |
| rule_table | deny | static_rules_deny |
| auto_approve | suspend | timeout_kill |

suspend 时 timeout_seconds 必填，其余忽略。codex 的"沙箱"语义归 `sandbox_allowlist` 字段（见 E.7），不体现在 permission_slot。

六家预填：

| 壳 | mechanism | hook_response_format | hook_action | rules.file | rules.format | headless |
|---|---|---|---|---|---|---|
| claude | hook_allow | cc | deny | — | — | deny/hook_deny |
| codex | hook_allow | bare | deny | — | — | deny/hook_deny |
| atomcode | hook_allow | cc | deny | — | — | deny/hook_deny |
| kimi | rule_table | — | deny | permission-rules.json | allow_deny_lists_v1 | deny/static_rules_deny |
| cline | auto_approve | — | cancel | autoapprove.json | autoapprove_v1 | suspend/timeout_kill(默认120s) |

（dsh 走 auto_approve 同 cline 形态，按现有代码语义对齐。）

### E.3 transcript

```
transcript: {
  "roots": [{"type":"isolated_dir"} | {"type":"env","name":"DSH_HOME"} | {"type":"home","subpath":".claude"}],
  "glob": ["模式1", "模式2", ...],
  "source_type": "jsonl" | "zstd" | "sqlite",
  "authoritative_source": null | "wire"
}
```

定位算法：①roots 列表顺序解析（isolated_dir→env:name→home:subpath），首个可访问者为生效根；②生效根下按 glob 列表顺序依次 glob，`{session_id}` 替换，`*`/`**` 通配；③SQLite 时 glob 可不含 {session_id}（reader 用 SQL 过滤），不带 {session_id} 的 JSONL 候选（如 wire.jsonl）为共享/权威源由 authoritative_source 声明；④多命中按路径字典序稳定排序取第一；⑤source_type 查读取器注册表（jsonl/zstd/sqlite），新增 source_type=新增读取器条目。

六家定位表：

| 壳 | roots（顺序） | glob（顺序） | source_type | authoritative |
|---|---|---|---|---|
| claude | home:.claude | projects/*/{session_id}.jsonl | jsonl | null |
| codex | home:.codex | sessions/**/rollout-{session_id}.jsonl | jsonl | null |
| dsh | env:DSH_HOME→home:.dsh | sessions/*/{session_id}/session.jsonl.zstd, ...session.jsonl | zstd（回退 jsonl） | null |
| kimi | env:KIMI_HOME→home:.kimi | wire.jsonl, wire/wire.jsonl, wire/wire-{session_id}.jsonl, wire-{session_id}.jsonl | jsonl | wire |
| atomcode | env:ATOMCODE_HOME→home:.atomcode | sessions/*/{session_id}.jsonl | jsonl | null |
| cline | env:CLINE_HOME→home:.cline | data/db/sessions.db, sessions/sessions.db, sessions.db | sqlite | null |

档 3 兜底：`capabilities.tier3_process_alive`（codex=true，其余 false），monitor 按此决定档 3 进程存活豁免，不写死壳名。

### E.4 controller_settings + provider_env

```
"controller_settings": {
  "role_text_target": "appendSystemPrompt" | "ctrl_session",
  "permissions": {"allow": ["Bash(...)"]}     # 存储即最终形态,空则省略
},
"provider_env": {
  "target": "settings_env" | "process_env",
  "map": {"ENV键": "{model}" | "{base_url}" | "{protocol}" | "${key}"}   # 键禁通配
}
```

占位符解析：`{model}`/`{base_url}`/`{protocol}` 从实例/provider 元数据取，缺失则省略该键（不写空串）；`${key}` 从实例 key_ref 文件 UTF-8 读取 strip，缺失/空/不可读 fail-loud。键禁通配（如 ANTHROPIC_DEFAULT_* 必须列全）。

六家 provider_env：

| 壳 | target | map |
|---|---|---|
| claude | settings_env | ANTHROPIC_AUTH_TOKEN:${key}, ANTHROPIC_BASE_URL:{base_url}, ANTHROPIC_MODEL:{model}, ANTHROPIC_DEFAULT_HAIKU/SONNET/OPUS/FABLE_MODEL:{model} |
| kimi | process_env | KIMI_MODEL_NAME:{model}, KIMI_MODEL_API_KEY:${key}, KIMI_MODEL_BASE_URL:{base_url}, KIMI_MODEL_PROVIDER_TYPE:{protocol} |
| codex/atomcode/cline/dsh | （无） | — |

controller_settings 预填：claude=appendSystemPrompt + `{"allow":["Bash(python -m tianji:*)","Bash(python -m tianji)","Bash(tianji:)"]}`；kimi/generic=ctrl_session，无 permissions。

key_env_style 迁移：cli-env→target=settings_env；kimi-model→target=process_env+map；未知 fail-loud；迁移后删 `ctrl_session.key_env_style` 与 `_KEY_ENV_MAPPERS`。

### E.5 renderer 形态分发

壳条目 `renderer` 值为形态名（非壳名）：`"claude"`（env 注入+settings 文件）| `"codex"`（env 注入+config.toml）| `"config_binding"`（壳内配置型通用）。`RENDERERS` 按形态名为键分发。出厂：claude→claude，codex→codex，kimi/atomcode/cline/dsh→config_binding。新壳复用 config_binding 形态=零新 renderer 代码。`add_instance()` 先读持久化壳条目再校验 binding/protocols/renderer，缺必填 fail-loud。

### E.6 硬编码边界与架构守卫

通用控制流禁字面量壳名；只能以 shell（仅注册表键）/protocol/binding/mechanism/renderer/source_type 为键查注册表分发。架构守卫测试扫描 `tianji/` 通用模块壳名字面量，清单从壳条目/出厂模板动态收集，豁免精确到注册表定义行。

### E.7 其余字段（去占位符）

| 差异 | 壳条目字段 |
|---|---|
| scan probe | `scan: {"probes":["foo","foo.cmd"]}`；顺序 shutil.which 首个命中；supported 由必填字段完整性判定 |
| adapter | `adapter: {"template":"generic","output_file":"tianji_{shell}_hook.py"}`；契约=输入 hook payload、输出统一事件、失败 fail-open（return 0） |
| hooks manifest | `hooks: {"manifest_merge":"standalone"\|"deep_merge_managed","managed_keys":["tianji"]}`；standalone=整文件天机生成，deep_merge_managed=只合并 managed_keys、用户其余键保留、天机键覆盖、指纹只盯 managed_keys |
| statusline | `capabilities: {"statusline":{"enabled":bool,"template":"claude_statusline","output_file":"tianji_statusline.py"}}` |
| quota | `capabilities: {"quota_sources":["statusline","transcript","ccswitch"]}`（有序按优先级，各源对应读取器） |
| completion | `completion: {"session_end_event":...,"stop_is_completion":bool,"completion_source":"session_end"\|"db_and_process"}` |
| resume | `resume: {"cmd":...,"prompt":...}`（无则不支持续跑） |
| sandbox | `sandbox_allowlist: ["%TIANJI_HOME%",...]` |
| thinking | `thinking_level_map: {"low":{...},"medium":{...},"high":{...}}` |
| tier3 | `capabilities.tier3_process_alive: bool` |

其余（session_id_keys/payload_exclude_keys/interrupt）同归壳条目。

### E.8 新增壳完整操作面

新增一个壳=只写一份壳条目，含：基础（binding/protocols/isolated_dir_mode/renderer）、permission_slot、transcript、controller_settings、provider_env、scan、adapter、hooks、capabilities（statusline/quota_sources/tier3）、completion、resume、sandbox_allowlist、thinking_level_map、session_id_keys、payload_exclude_keys、interrupt。后端协议不在 PROTOCOLS、或形态不在 RENDERERS（三形态）时，才新增协议条目/renderer 条目（数据/注册表，非 if 分支）。

## 验收标准（逐条自检，全过才算完）

1. 全仓通用控制流（装配/校验/执行）无壳名字面量分支；仅注册表查询（RENDERERS.get(形态名)/BACKENDS.get(protocol)/读取器注册表）用键分发
2. 架构守卫测试：扫描 `tianji/` 通用模块壳名字面量，清单从壳条目/出厂模板动态收集，豁免精确到注册表定义行
3. 六家出厂壳条目补齐附录 E 全部字段，新增壳只写壳条目即接入，不改通用代码
4. 权限：模拟一个"hook_allow 型但名字不在旧元组里"的壳，`hook_response` 走钩子 allow 机制且应答格式正确；缺失/非法 permission_slot 时 fail-closed
5. 转录：六壳转录定位行为与收敛前一致（字节级路径回归），monitor 字节计数不回归；monitor 能传实例上下文定位
6. 总控 settings：claude/kimi/generic 三壳生成的 settings-controller.json 与收敛前语义等价；key_env_style 迁移后无双真源
7. 四壳（kimi/atomcode/cline/dsh）经 config_binding 形态隔离 env 注入到位
8. 思考级别：支持的壳正确注入，不支持的如实记实例档案
9. codex：`stop_is_completion` 语义正确（codex=False），无死配置、注释与实证一致
10. 适配器：通用 runner 形态，装钩子不因缺 `{shell}_hook.py` 而漏装
11. 子agent降级：壳条目声明子agent事件能力，monitor 按声明降级活性判定
12. 全量 `python -m pytest tests -q` 全绿

## 纪律（硬的）

- 字段定义一律以附录 E 为准，本票不重复定义、不偏离。
- 不改 `ctrlprotocols.BACKENDS` / `integrations.PROTOCOLS`（已收敛协议层）；`shellrender.RENDERERS` 只改键（壳名→形态名），不改"注册表分发"形态。
- 账本（SQLite）是唯一写入口，状态变更走账本 CLI/ops，别旁路。
- 核心纯标准库（sqlite3），加依赖先在 PR 里说清楚为什么非它不可。
- 简洁优先、精准修改：只动与本票直接相关的代码。
- 跨平台写法，代码保持 Python 3.9 语法兼容（实测基准 Windows 10 + Python 3.12）。
- 安全红线：恒定时间比较别改回 `==`；身份校验 fail-closed 别退回 fail-open。
- 注释、文档一律大白话，不堆英文术语。

## 测试基线

- 命令：`python -m pytest tests -q`
- 你的环境 = Windows + Python 3.12（实测基准），**508 条全绿**，收工时一条不许挂。
- 开工前先跑一遍确认全绿；不绿先修环境，别带病开工。

## Git 交活流程

你已在本票分支上（本文件所在分支）：

```bash
git status                                          # 确认在 ticket-46-shell-convergence 分支
# ...干活...
git status && git diff --stat                       # 复查改动
git add <你实际改的文件>                             # 只加代码，别把无关文件带上去
git commit -m "ticket 46: 壳差异数据化收敛"
git pull --rebase origin ticket-46-shell-convergence  # 推前同步（分支上可能有派活材料补充）
git push origin ticket-46-shell-convergence
```

推完在 GitHub 开 PR：base=main，head=ticket-46-shell-convergence，PR 描述按下方的交活清单写。

## 交活清单（PR 描述必含）

1. **方案概述**：怎么收敛的，哪些壳条目字段接管了哪些硬编码
2. **改动清单**：文件 + 函数级
3. **验收自检**：逐条对照上面 12 条验收标准，过了的打勾 + 一句证据
4. **自测命令与输出**：pytest 全量结果
5. **新增壳演示**：用一个测试或示例证明"新壳只写壳条目数据就能接入，零通用代码改动"

有问题（规格看不懂、字段对不上、发现规格疑似有洞）→ 停下来在交活前把问题写进 PR 描述顶部"待裁决"一节，别自己拍板发挥。
