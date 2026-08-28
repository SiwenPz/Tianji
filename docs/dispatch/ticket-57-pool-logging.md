# 票 57: 池日志与信号第四层(号池③)

> 先读 00-onboarding.md。前置:票 56 已合并(proxy 进程在跑并有日志埋点挂点)。

## 背景

proxy 每次转发的记账与本票落库,并接进天机现有信号体系。schema 同构 cc-switch(一个本地代理管理器的实战 schema),字段如下,不得增删主键:

- **pool_request_logs**(每次请求一行): request_id/池名/实际成员名/request_model(请求的模型)/model(实际路由的模型)/status_code/耗时/first_token_ms/input_tokens/output_tokens/is_stream/is_converted/session_id
- **pool_daily_rollups**(日聚合): 按 日期×池×成员×模型 聚合 请求数/成功率/token;跨天边界正确、重放不翻倍
- **pool_member_health**(成员健康): consecutive_failures/last_success_at/last_failure_at/last_error

信号接线:全员 429 冒泡时沿用现有"实例 exhausted → 分配器跳过"机制(池=key 等价物),**不新造状态**。cockpit snapshot 增加池摘要(池名/成员数/熔断中成员数),peek 可看健康详情。

## 现状锚点

- `tianji/schema.py`(建表处)/`tianji/db.py`(连接与迁移模式)
- 现有 exhausted 标记与分配器跳过逻辑(quota/monitor 侧,搜 exhausted)
- cockpit snapshot 组装处(`tianji/webapp.py` _PAGE 数据源函数)+ peek 渲染(`renderPeek`,约L970)

## 范围外(不做的事)

- 日志行严禁记录 key 本体(只有成员名)
- 日志保留期:进账本可配,默认 30 天,监控器巡检顺带清——不做独立的日志清理进程
- 不做日志可视化页面(摘要嵌现有 peek 即可);不动 quota 四层信号之外的解释逻辑

## Acceptance criteria

- [ ] 每次池请求落一行日志,双 model 字段与 is_converted 标齐全
- [ ] 日聚合跨天边界正确、重放不翻倍
- [ ] 熔断/恢复事件在 pool_member_health 留痕
- [ ] 全员 429 → 实例 exhausted → 分配器跳过,有回归测试覆盖这条链
- [ ] snapshot 池摘要与 peek 健康 visible;全量 `python -m pytest tests -q` 全绿

## Git 交活流程

commit("ticket 57: 池日志与信号第四层") → push → 开 PR,六项交活清单同前。