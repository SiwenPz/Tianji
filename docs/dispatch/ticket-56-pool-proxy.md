# 票 56: tianji proxy 常驻进程与请求路由(号池②)

> 先读同目录 00-onboarding.md。前置:票 55 已合并(池数据模型已在账本)。

## 背景(维护方已定稿的设计裁决)

号池的运行形态:**独立常驻子进程 `tianji proxy`**(HTTP 服务绑 127.0.0.1),由现有 supervisor 守护——daemon 现在拉 monitor+web 两常驻,proxy 是第三个孩子。UI 面(web)与数据面(proxy)隔离:UI 崩不带崩工人。

核心裁决(定稿):
- **透明重试边界**(new-api 思路):响应开始前的失败(429/5xx/连接失败/首字节超时)→ 沿轮盘顺序换**同模型**成员重发(上限默认 5 次,进账本可配),壳无感;**绝不跨模型降级**;流式响应中途断→不重试,错误原样上抛。
- **熔断器**(滑动窗口):错误率≥0.7 且样本≥15 → 熔断 90 秒 → 半开探测连续 3 次成功恢复;样本不足不熔断防误杀。全部进账本可配。
- **超时三件套**:首字节 90s / 流中空闲 180s / 非流式 600s。
- 路由:按"模型+协议"过滤候选 → 纯轮盘选成员(熔断跳过) → 明文 key 从成员 credential 的 key_ref 文件读出转发。
- 同协议透传优先(含 SSE 原样);跨协议转换是票 58 的事,**本票只留挂点不做实现**。

## 现状锚点

- `tianji/daemon.py` supervisor 主循环(约L137起,现拉 monitor+web 两常驻)——照同样模式加 proxy
- `tianji/ops.py` `_config()` 读熔断/端口/重试上限配置
- `tianji/integrations.py` registry——成员 credential → key_ref 解析参考 `shellrender.resolve_credential()`

## 范围外(不做的事)

- 不实现协议转换(58);不落请求日志表(57 只在本票做最小的日志埋点接口留给 57 接,或留 TODO 挂点)
- 不做 web UI(59);不做池间互备;不做主动测活;明文 key 只在请求内过手,严禁写入任何日志或报错消息

## Acceptance criteria

- [ ] 单成员瞬时 429 → 同请求内换牌重发成功,壳无感;全员 429 → 429 原样冒泡
- [ ] 熔断全生命周期:连续失败摘牌→90s 冷却→半开→3 连成功恢复;样本<15 不熔断
- [ ] 无令牌/错令牌请求拒绝;端口冲突有明确报错
- [ ] `tianji daemon stop` 干净停掉 proxy;手动 kill proxy 后 daemon 自动重拉且有审计行
- [ ] 全量 `python -m pytest tests -q` 全绿(开工记基线,收工不许多挂)

## Git 交活流程

commit("ticket 56: tianji proxy 常驻进程与请求路由") → push → 开 PR(base=main),描述必含:①方案概述 ②改动清单 ③验收自检 ④pytest 输出 ⑤待裁决区。