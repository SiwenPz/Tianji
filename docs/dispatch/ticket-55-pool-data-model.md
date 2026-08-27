# 票 55: 池条目数据模型与 CLI(号池①)

> 你现在应该在本票分支上(本文件就在分支里)。git status 确认,并先读同目录 00-onboarding.md。

天机是一个管 AI 编程助手的协作框架:账本(SQLite)记角色/实例/任务与审计,把不同助手壳和模型供应商按稳定组合拉进"派活→监控→验收→返工"流水线。

## 背景(维护方已定稿的设计裁决)

天机实例 = 壳+key+模型+隔离目录四元组。现状痛点:单 key 绑定单实例,key 质量差(瞬时429/额度不足/连接不稳)时实例瘫痪只能等人工。维护方已定稿"内建号池"方案——本批六步走,本票是第一步:先把池作为**数据条目**立起来。

核心设计(定稿,勿质疑方向):
- **池 = key 的等价物**:实例的 key 维度可填池名;分配器/表现分/配额检查零改动。
- 池配置进账本 configs 条目,零新增配置文件;全部写操作走现有审计(request_id 幂等口径)。
- 本票只做数据层与 CLI;proxy 进程/日志/UI 是后续三张票(56/57/59),本票一律不碰。

## What to build

- configs 新增 `pool:<名>` 条目(schema): {members: [credential 名...], cursor: 轮盘游标, circuit: 熔断参数覆盖(可空=用默认)}
- 池令牌:建池时机械生成并存账本(未来壳 credential 将指向它);支持签发/轮换。
- CLI 子命令:`tianji pool create / add-member / remove-member / list / status`
- 成员校验:add-member 时成员必须是 integrations 注册表里已登记的 credential;同池成员协议可混(路由期按"模型+协议"过滤,本票不管路由)。

## 现状锚点

- `tianji/ops.py` 的 `_config()`(读)/`config_set()`(带审计写)——池条目读写沿用同一套口径
- `tianji/integrations.py` 的注册表(credential 条目校验来源)
- 参考 `tests/test_integration_registry.py` 的测试组织方式

## 范围外(不做的事)

- 不碰 proxy 进程/网络转发(票 56);不做日志表(票 57);不做转换层(票 58);不做 web UI 与实例接线(票 59)
- 不做池级花费限额、不做主动周期测活(用户已裁决砍掉)
- 不动分配器/表现分/配额检查机制(池=等价物,零改动)
- 规格书回写在票 59 收尾时统一落,本票不改 spec.md

## Acceptance criteria

- [ ] 建池/加成员/摘成员/list/status/令牌轮换全链路可用,幂等回放安全
- [ ] 非法操作拒绝:成员不是已登记 credential、池名重复;摘光最后一个成员给出警告
- [ ] 全部写操作有审计行(audit 表可查)
- [ ] 全量 `python -m pytest tests -q` 全绿(失败集以开工基线为准,不许多挂)

## Git 交活流程

工作完: git add 相关文件 → commit("ticket 55: 池条目数据模型与CLI") → push origin ticket-55-pool-data-model → 在 GitHub 开 PR(base=main),PR 描述必含:①方案概述 ②改动清单(文件+函数) ③验收自检逐条勾选 ④pytest 输出 ⑤待裁决区(有问题写这里,别自由发挥)。