# 票 59: 实例接线与池运营入口(号池⑤·收官票)

> 先读 00-onboarding.md。前置:56 与 58 均已合并。

## 背景

号池系列收官票:把池接进实例生命周期与用户入口,完工后随票完成规格书回写。

核心裁决:
- **新建实例默认进池**;直绑 key 保留为降级逃生口(排障用)但标"不推荐"(提示,不禁止)
- 分档(贵池/打杂池)由用户指定,机器不猜
- spec 回写并入本票收尾(维护方已有底稿要点:池条目 schema/池代理进程/池日志表/转换层进 spec 对应章节+新旧信号语义对照表一张)

## What to build

- `integrations.validate_worker_card()`(integrations.py 约 L521)接受池名:key 维度合法值扩展;模型清单=池成员模型并集(按壳协议过滤);协议兼容性="池内存在同协议成员或经转换可达成员"
- launcher 渲染:实例绑池时壳配置 base_url=池地址、credential=池令牌——走 `shellrender._render_config_binding()`(shellrender.py 约 L179)数据驱动,**不加壳名分支**
- 口径落地:`wizard.add_instance()`(wizard.py 约 L203)新建默认进池;直绑路径加"不推荐"提示(web+CLI 两处)
- web 抽屉加"池"分区(与 protocol/provider/shell/credential 并列,渲染挂在 peek 体系约 L882 起):建池/归 key 入池/摘除/成员健康/池日志摘要;写逻辑与 CLI 共用一套(零重复实现)
- 一键归池:已有 credential 收进池的机械操作,目标池用户指定
- e2e 回归:建池→归 key→建实例(绑池)→派单→工人经池出活,全链路测试一条

## 范围外(不做的事)

- 不做池间互备/自动借池;不做用量均衡策略(等真实数据);不做迁移工具(03 裁决无事可迁)
- 池代理内部逻辑修复属 56/57/58 范围,发现 bug 记待裁决区

## Acceptance criteria

- [ ] e2e 全链路测试通过(上述场景)
- [ ] 池内无壳所需协议成员时,建实例校验明确报错指路(fail-loud)
- [ ] 直绑仍可用带"不推荐"提示
- [ ] web 池分区操作与 CLI 等价,审计一致
- [ ] 规格书回写+新旧信号语义对照表落盘(spec.md)
- [ ] 全量 `python -m pytest tests -q` 全绿

## Git 交活流程

commit("ticket 59: 实例接线与池运营入口") → push → 开 PR,六项交活清单同前+规格书回写 diff 说明。