# 天机初始化引导——所有宿主共用

目标：首次启用 → 检测模型来源 → 缺配置时引导 → 用户分配角色 → 实证 → 待命。只有用户给出具体任务后才进入主 skill 的业务工作循环。后续会话检查有效配置，不重复首次引导。

宿主分支：Kimi 把菜单写入 `[secondary_model]`；Codex 把主会话和原生子代理统一接到一个支持 OpenAI Responses API 的模型路由入口。两者共用下面的来源选择、用户逐角色拍板、实证和待命语义，绝不另设“四角色默认随主模型”的捷径。

本引导负责把用户的 key 配成宿主可用的模型菜单，两条路线都管。key 只由配置脚本接收；引导回复不得复述完整 key。

## 第 0 步|路线分岔(必须问用户,不许默认)

> "你的 key 怎么接?两条路线:
> 1. **号池**:本地跑一个代理(如 new-api),所有 key 汇进去,天机统一走本地入口——key 不稳时代理层轮盘,工人无感
> 2. **直连混合 key**:每个 key 直连各家,天机按别名区分(如同名 deepseek 分 A/B 两路)"

- 选 1 → 走【号池路线】
- 选 2 → 走【直连路线】

## 号池路线

1. **探测**:跑 `python scripts/pool-detect.py`(本目录下)
   - 有活端点 → 报告给用户:"发现池在 <地址>,有 N 个模型",进第 3 步
   - 只有"活-需认证" → 向用户要这个池的 key,带 `--key` 重跑,进第 3 步
   - 全死 → 进第 2 步
2. **装池**:本地得有池后端(如 new-api、cc-switch)。有 → 让用户给池地址,先 GET `<地址>/models` 确认活着再继续,不重复装。没有 → 推荐 **tianji-proxy**(单文件纯标准库,零依赖,多渠道多 key 汇成一个总入口,429 自动换 key)。最小上手:
   - `python ~/.agents/skills/tianji-proxy/scripts/tianji-proxy.py add-channel --name <渠道名> --base-url <上游地址> --models <模型逗号分隔> --keys <key逗号分隔>`
   - `python ~/.agents/skills/tianji-proxy/scripts/tianji-proxy.py serve`(默认 `localhost:8317`)
   - 装完回第 1 步探测,pool-detect 会发现 `localhost:8317` 这个活端点
   - 实在不想装任何东西 → 转【直连路线】
3. **确认协议（Codex 必做）**：入口必须原生支持 `POST /v1/responses`，只支持 Chat Completions 的入口不能直接给 Codex 使用。Kimi 无此附加步骤。
4. **选模型**:见下方【选模型·硬步骤】
5. **写配置（Kimi）**:跑 `python scripts/pool-configure.py --base-url <池地址> --key <key> --model 别名=模型id(每个模型一次) --default <主力工人别名>`
   - 脚本自动备份+校验+回滚;真实 config 是 `~/.kimi-code/config.toml`,不许手工乱改
   - 上下文窗口默认写 128000;实测过真实窗口的模型用 `--ctx "别名=实测值"` 覆盖,全局默认用 `--max-context-size N` 调(实测方法见主 skill SKILL.md【实测模型上下文】)
   - pool-configure.py 现在同时写角色花名册:选模型拍板后把角色指派一并带上,见下方【选模型·硬步骤】第 2-3 步
6. **写配置（Codex）**：先把准备执行的变更告诉用户：Codex 的主会话和所有 Tianji 子代理都会改走该统一入口，现有 `model`/`model_provider` 会保存用于卸载恢复；这一步需要重启 Codex。只有用户明确确认后，当前会话才运行：
   ```
   python scripts/codex-role-configure.py --codex-home <CODEX_HOME> \
     --base-url <以/v1结尾的入口> [--env-key <只含变量名>] \
     --main-model <主模型id> --model "别名=模型id"（每个模型一次） \
     --role "worker=别名或主模型" --role "verifier=别名或主模型" \
     --role "referee=别名或主模型" \
     --allow-main-provider-change
   ```
   - 不得把完整 key 放进命令行；入口需要 bearer token 时，只写 `--env-key` 对应的环境变量名，并确保重启后的 Codex 能读取该变量。
   - 脚本对 `config.toml`、角色 TOML 和适配状态执行统一预检、备份、原子写入、TOML 复验和失败回滚。
7. **实证**：
   - Kimi：派一个已绑定角色（通常 `tianji-worker`，任务书写只读模式=是）走新配模型，查 wire 的 `modelAlias`，不信自报。
   - Codex：配置写完后要求用户重启 Codex；新会话说“天机”时晨检应为 `NEED_ROUTE_PROOF`，当前会话直接运行 `python scripts/codex-routing-probe.py`。脚本必须真实执行 Codex 原生 parent→`tianji-worker`，成功后写入与当前配置/角色哈希绑定的证明，再跑晨检得到 `READY`。不得让用户手敲脚本。
   - 任一实证失败 → 停下来告诉用户，不能带病开工。
8. **待命**:告诉用户配置和实证已完成，等待具体任务；用户已给出任务时才进入主 skill 的工作循环，无需再次说“天机”。

## 直连路线

1. **收集**:问用户每个 key:平台名(作 provider 名)、base_url、key 本体;再问每个 key 下要用哪些模型(同模型多 key 要用别名区分,如 `direct/deepseek-a` / `direct/deepseek-b`)
2. **选模型**:见下方【选模型·硬步骤】
3. **写配置（Kimi）**:跑
   ```
   python scripts/pool-configure.py --direct \
     --provider "名称=type=base_url=key" (每个 key 一次) \
     --model "别名=provider名/模型id" (每个模型一次) \
     --default <主力工人别名>
   ```
   - 上下文窗口同上:`--max-context-size N` 全局默认,`--ctx "别名=N"` per-model 覆盖
4. **写配置（Codex）**：Codex 原生子代理不能各自切换 provider。把每个直连渠道作为 tianji-proxy channel 接入，由统一 Responses 入口按请求模型路由；然后执行号池路线第 6 步。上游本身必须支持 Responses，代理不做 Chat Completions/Responses 协议转换。
5. **实证+交接**:同号池路线第 7-8 步

## 选模型·硬步骤(两条路线共用,红线级)

1. 把可用模型列成**编号清单**展示给用户(每个带一句能力/稳定性备注)
   - **备注规范(防开发痕迹泄漏)**:备注只写客观信息——厂商/档位/本机探针实测结论;本机没实测过的一律写"未实测"。**禁止引用开发档案里的历史使用记录**,那是开发组内部文档,不该出现在新用户的引导里
2. 问用户要哪些角色:预设建议 **工人 + 审核员 + 裁判**;用户可加自定义角色(架构师/文档员等)——自定义角色用主 skill 的 `templates/role-template.md` 现场生成 agent 文件
3. **逐角色等用户明确指派模型**。禁止默认绑定,禁止"我帮你选",用户没拍板之前一个字符都不许写进配置
4. 约束:审核员必须和工人不同模型(最好不同厂商);裁判用池里最强的模型
5. Kimi 的角色指派由 `pool-configure.py --role "角色名=模型别名"` 写进 `~/.tianji/roles.toml`；Codex 的四个原生角色由 `codex-role-configure.py --role` 写入角色 TOML 和适配状态。两者都要求同次调用重复角色时报错、重配时替换旧指派。**角色也可绑定字面值“主模型”**：这是活绑定，不固化当时的模型；主模型以后改变时，该角色随之改变。Codex 对活绑定角色省略角色 TOML 的 `model`，固定角色则写用户所选别名对应的模型 ID。

## 红线

- key 只进 config.toml,不许出现在聊天明文、日志、状态板里(脚本输出已掩码,你也不许补全)
- 审核/裁判不买自己人的账:角色间模型必须不同
- 模型不好用/抽风时:报告用户给选项,**换模型必须用户拍板**,禁止自作主张
- 任何一步失败:停下来报告用户,不许静默降级/跳过实证
