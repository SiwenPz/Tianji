# Codex 接入：原语实证、实现与待验边界

日期：2026-09-07。宿主：Windows，Codex CLI 0.153.4。

## 验收目标

GitHub 下载 → Codex 安装 → 启用天机 skill → 首次走共享引导（模型来源、凭据、模型菜单、用户分配角色、真实路由实证）→ 待命 → 用户交代任务 → 天机自动执行、审核、必要的返工 → 交付。

代码侧兼容链已实现；尚需按本文末尾用真实 Responses 模型源完成新用户整链实机验收。安装文件、角色 TOML、hooks 和本地测试通过都不能替代该验收。

## 已实测

早期隔离实验启动两个仅监听 127.0.0.1 的假 Responses 服务，临时 CODEX_HOME/USERPROFILE/HOME 中配置主会话指向 A、自定义角色指向 B。运行真实 `codex exec --strict-config`，由假响应请求原生 `multi_agent_v1.spawn_agent` 派发角色。结果如下：

由此确定统一 provider 方案。当前 `tests/test_codex_native.py` 使用一个本地假 Responses 入口，验证父会话与命名子代理访问同一端点，同时子代理的模型覆盖仍为 `child-fixture`。测试没有复制真实 auth/config，没有真实 key，没有调用外部模型。临时目录由测试清理；服务在线程内关闭。正常单元测试发现时跳过此项，仅显式执行下列命令才运行。

```powershell
python tests/test_codex_native.py
```

早期结果（两个配置变体均复现）：

| 配置变体 | 子代理模型名 | 子代理实际端点 |
|---|---|---|
| 只在角色文件定义 B provider | child-fixture，生效 | A，仍为主会话端点 |
| 同时在用户级 config 注册 B provider | child-fixture，生效 | A，仍为主会话端点 |

若更换 Codex 版本后独立 provider 行为改变，应重新调查后再调整适配，不把本机结果视为永久限制。

该限制决定了适配结构：不尝试给每个角色切换 provider，而让主会话与所有 Tianji 原生子代理共用一个 Responses provider，再通过请求里的模型 ID 分流。

## 与官方文档的关系

[Codex 自定义子代理文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)说明角色文件按配置层加载，且 `model` 可覆盖；不能据此直接推断本机版本支持独立 provider。以实际请求记录核实接入。
[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)规定 provider 的 `wire_api` 为 `responses`。因此 Chat Completions 池不能仅修改模型名后直接使用。

## 已实现

- tianji-proxy 同时透明转发 `/v1/chat/completions` 与 `/v1/responses`；Responses 保留原路径完成多渠道/多 key 重试、全 429 `Retry-After` 和 SSE 流式转发，不做协议转换。
- `codex-role-configure.py` 只有在用户明确授权主 provider 变更后才执行；配置、全部角色和状态统一预检、备份、原子写入、TOML 复验、失败回滚。完整 key 不进入命令行，受保护入口只记录环境变量名。
- “主模型”角色是活绑定：角色 TOML 不固化模型；固定角色写用户选中别名对应的模型 ID。重新配置替换旧绑定并使旧证明失效。
- `codex-routing-probe.py` 真实运行 `codex exec`，要求原生 `spawn_agent(agent_type=tianji-worker)` 完成，并将证明绑定到当前 config/角色哈希。证明记录被派发的角色名与其绑定模型，读回时按该角色核对。改配置或角色后晨检不再 `READY`。
- 晨检状态链为 `NEED_INSTALL` → `NEED_HOST_ADAPTER`/`NEED_POOL` → `NEED_ROLE_CONFIG` → `NEED_ROUTE_PROOF` → `READY`。只有最后一态可进入业务派工。
- Codex 安装同步 `tianji` 与 `tianji-proxy`，但不在安装流程启动 worker/verifier；卸载先安全恢复 Tianji 前的主模型/provider，再移除受管角色和 hooks。
- 首次配置继续使用共享 `BOOTSTRAP.md`：来源、key、模型菜单、逐角色用户拍板、变更确认、重启、实证、待命。没有独立的 Codex 默认绑定流程。

## 仍需真实验收

1. 新目录从 GitHub clone，由新 Codex 会话仅按 README 安装；确认安装不派业务代理、不索取角色默认值。
2. 重启并信任 hooks 后只说“天机”，按引导给真实 Responses 模型源、菜单和四角色选择，明确同意主 provider 变更。
3. 再重启，确认原生路由探针通过、晨检为 `READY` 并待命。
4. 给一个可机械验收的小任务，确认原生 `tianji-worker` → 独立 `tianji-verifier` → PASS/返工 → 交付闭环。

真实入口若只支持 Chat Completions 仍不能使用；tianji-proxy 不承担 Responses/Chat Completions 语义转换。Codex 需要重启才能加载用户级 provider 变更，且首次 hooks 信任是宿主安全边界，不能自动绕过。

回归命令：`python -m unittest discover -s tests -p 'test_*.py'`；Responses 专项：`python -m unittest tests.test_tianji_proxy_responses tests.test_codex_adapter -v`。

本轮回归曾出现既有状态栏 120-wire 性能测试耗时 0.536s 超过 0.3s 门槛；原样复跑通过，未放宽断言，保留为性能波动风险。
Skill 格式验证命令：`python -X utf8 <skill-creator目录>/scripts/quick_validate.py skills/tianji`（Windows 默认 GBK 需显式 UTF-8）。
