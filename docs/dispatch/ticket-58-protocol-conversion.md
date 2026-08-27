# 票 58: 协议转换层 anthropic↔openai_chat(号池④)

> 先读 00-onboarding.md。前置:票 55 已合并(转换层产物被 56 引用,但本票可与 56 并行开发,联调在 59)。

## 背景

池内成员协议可能不同,跨协议时需要双向转换层。维护方已完成实现选型调研,结论:

- **自写为主**,两个文件收口:`tianji/proxy/convert.py`(非流式+请求侧双向)+`tianji/proxy/stream.py`(SSE 双向状态机)
- 体量参照:同类最小可行实现约 500 行(单方向 TS 版),双向预计 600-900 行;**硬红线 ≤1000 行**(超了说明过度设计,回到需求面重新审视)
- 语义对照物(许可合规): one-api 的 relay/adaptor/anthropic(MIT,可直接移植逻辑);musistudio/llms(无 LICENSE,只准参考思路不准搬代码);**new-api 为 AGPL-3.0,一行代码都不许参考移植**
- ⚠️ 许可纪律是本票红线:若确需粘贴外部代码片段,只允许 MIT 来源且在注释中标注出处

## 关键技术点(调研定稿)

**流式 SSE 双向(openai→anthropic 是重头)**:无块边界 chunk 流重建成块事件流;message_start 只发一次;content block 索引原子分配;openai 用 tool_calls[i].index 区分并行调用,anthropic 用 block index——必须维护 index→block_index 映射表;finish_reason 到达不立即关流,缓存 message_delta(stop_reason+usage)流尾统一补 content_block_stop+message_delta+message_stop;usage 可能只在末尾 chunk。
**partial_json 双向都不解析不拼 JSON,只做字符串增量透传**(非流式才 JSON.parse);流尾空 arguments 补 "{}"。
**tool_use/tool_result 请求侧**:anthropic→openai 一拆多(tool_result 块拆多条 role:"tool");openai→anthropic 多合一(连续 role:"tool" 合并回 tool_result 块数组,且 anthropic 要求 tool_result 紧跟对应 tool_use 所在 assistant 消息、同角色连续消息要合并——one-api 没做合并直连官方会 400,别继承这个 bug)。
**system prompt**:anthropic 顶层 system(字符串或 blocks)↔ openai 首条 system role;文本无损,cache_control 必丢。
**参数映射**:max_tokens anthropic 必填(openai→anthropic 缺省补 4096);stop_sequences↔stop;temperature/top_p 直传;top_k/openai 的 n/logprobs/penalties 无对应丢弃。
**降级规则**:cache_control/图片/thinking 块丢弃;图片替换占位文本"[图片已省略]"防上下文断裂;降级事件打"有损转换"标(给 57 的日志用)。

## 范围外(不做的事)

- openai_responses 协议(/responses API)不做——本轮只覆盖 anthropic↔openai_chat
- 不做池路由决策(56);不做持久化;能透传不转换(同协议在场时不启用转换层,判断归 56)

## Acceptance criteria

- [ ] 请求侧双向单测:system/tool_use/tool_result(一拆多+多合一)/tool_choice/参数映射
- [ ] 流式双向单测:块索引分配/并行 tool_calls 的 index 映射/usage 尾 chunk/partial_json 透传/流尾空 arguments 补 {}
- [ ] 降级路径测试:cache_control/图片/thinking 丢弃+打标
- [ ] 体量红线:convert.py+stream.py 合计 ≤1000 行(测试断言锁死)
- [ ] 全量 `python -m pytest tests -q` 全绿

## Git 交活流程

commit("ticket 58: 协议转换层 anthropic↔openai_chat") → push → 开 PR,六项交活清单同前。