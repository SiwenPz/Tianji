# 号池规格书(v1.0,票59 收尾回写)

> 维护方底稿要点回写:池条目 schema/池代理进程/池日志表/转换层进入 spec 对应章节+新旧信号语义对照表。

---

## 1. 池条目 Schema

池数据以纯配置条目存账本 `configs` 表,零新表。

### 1.1 池配置 `pool:<name>`

```json
{
  "members":     ["credential-A", "credential-B", "..."],
  "cursor":      0,                       // 轮盘游标(纯序号,failover 下同模型顺序递增)
  "circuit":     {},                       // 熔断参数覆盖(空=用默认)
  "created_at":  "2026-08-20T00:00:00.000",
  "updated_at":  "2026-08-20T00:00:00.000"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `members` | string[] | 成员 credential 名称列表(须已在 integrations 注册表登记) |
| `cursor` | int | 轮盘游标,路由时递增;0=从数组头部开始 |
| `circuit` | object | 私有熔断参数覆盖(空=全局默认)同电路表 schema |
| `created_at` | ISO 8601 | 建池时间 |
| `updated_at` | ISO 8601 | 成员变更时间(增/删) |

### 1.2 池令牌 `pool:token:<name>`

```json
"hex-64-char-token"
```

明文令牌,建池时机械生成(secrets.token_hex(32));轮换时丢弃旧值写新值。令牌明文仅在建/轮换时返回一次,不落日志。

### 1.3 实例 `key_name` 语义

实例注册时 `key_name` 字段接纳两类值:

| key_name | 含义 | 验证路径 |
|----------|------|----------|
| 已登记 `credential:` 名 | 单 key 直绑 | _validate_instance_combo 检查 `key:<name>` 存在+壳协议兼容 |
| 已登记 `pool:` 名 | 池绑定 | `_validate_instance_combo` 跳过 key 条目检查(池=等价物,零改动);后期 `validate_worker_card` 执行模型并集+协议兼容性校验 |

---

## 2. 池代理进程

### 2.1 进程拓扑

```
┌──────────────────────────────────────────────────┐
│              tianji daemon (supervisor)            │
│  ┌──────────┐ ┌──────────┐ ┌────────────────────┐│
│  │  monitor  │ │   web    │ │   tianji proxy     ││
│  │ (票07)   │ │ (票03)   │ │   (票56/59)        ││
│  └──────────┘ └──────────┘ └────────────────────┘│
│                     │                              │
│         bind 127.0.0.1:<daemon.proxy_port>         │
└──────────────────────────────────────────────────┘
```

- proxy 进程独立于 daemon 主进程,崩溃不影响 web 或 monitor
- daemon `stop` → 所有子进程干净退出;手动 kill proxy → daemon 自动重拉并
- 首字节超时 90s / 流中空闲 180s / 非流式 600s
- 透明重试边界:响应开始前的失败(429/5xx/连接失败/首字节超时) → 沿轮盘顺序同模型换牌重发(上限 5 次);流式响应中途断 → 不重试

### 2.2 路由决策

```
收到请求(model, protocol, API key(pool token)) →
  按 model+protocol 过滤候选池成员 →
  清除熔断成员 -->
  按 cursor 轮盘选主成员 →
  读其 credential 的 key_ref 文件取明文 key →
  根据协议透传/转换 →
  转发给供应商 base_url
```

- 同协议透传优先(SSE 原样)
- 跨协议转换(anthropic↔openai_chat)走转换层(票58)
- 池 token 作为 API key 原文传给 proxy,proxy 内部时效不少于池 token 有效期

---

## 3. 池日志表

池操作审计走统一 `audit` 表,action= 以下的值:

| action | 触发 | 审计内容 |
|--------|------|----------|
| `pool_create` | 建池 | `{name, members, circuit, by}` |
| `pool_add_member` | 归 key 入池 | `{name, member, by}` |
| `pool_remove_member` | 摘除成员 | `{name, member, remaining, warning, by}` |
| `pool_rotate_token` | 令牌轮换 | `{name, by}` |
| `pool_delete` | 删池 | `{name, by}` |

池路由事件(选主/重试/熔断/恢复)日志计入 `messages` 表,type=`pool_routing`,payload:

```json
{
  "pool": "pool-name",
  "member": "credential-A",
  "event": "selected|retry|circuit_open|circuit_half_open|circuit_close",
  "model": "...",
  "attempt": 1,
  "detail": "..."
}
```

_idempotency*:建池/增删成员/轮换/删池均为幂等操作(带 request_id,重放返回原回执)。

---

## 4. 转换层进入 spec 章节(票58)

转换层实现位置: `tianji/proxy/convert.py` + `tianji/proxy/stream.py`

### 4.1 请求侧映射

| 方向 | 字段映射 | 备注 |
|------|----------|------|
| anthropic → openai_chat | system(顶层串/blocks) → role:system 首条; tool_use → tool_calls; tool_result → role:"tool" | 一拆多:一条 tool_result 拆多条 |
| openai_chat → anthropic | role:system → system; tool_calls → tool_use; 连续 role:"tool" → tool_result 块合并 | 多合一:同角色连续消息合并 |

### 4.2 响应侧映射

| 方向 | 字段映射 | 备注 |
|------|----------|------|
| anthropic → openai_chat | stop_reason → finish_reason; message_delta(usage)补尾chunk | SSE diff format 转换 |
| openai_chat → anthropic | finish_reason → stop_reason; delta(tool_calls) → content_block_start+input_json_delta | partial_json 只透传不拼接 |

### 4.3 降级规则

| 信号 | 处理 |
|------|------|
| cache_control | 丢弃(无对应 OpenAI 字段) |
| 图片 | 丢弃,替换为占位文本 `"[图片已省略]"` |
| thinking 块 | 丢弃,打 `"有损转换"` 标(给日志用) |
| max_tokens(anthropic 必填) | openai→anthropic 缺省补 4096 |
| top_k / n / logprobs / penalties | 无对应丢弃 |

---

## 5. 新旧信号语义对照表

本票核心变更:实例的 key_name 维度从"单 credential 绑定"扩展为"池或 credential"。

| 信号/概念 | 旧语义(票55 前) | 新语义(票59 后) | 变化 |
|-----------|-----------------|-----------------|------|
| `key_name` | 必为 `key:<name>` 条目名 | 可为 `credential:` 或 `pool:` 名 | 扩展语义 |
| `valid_credential` | 检查 `key:<key_name>` 存在 | 对 pool 名跳过 key 条目检查,只经过壳条目+模型协议兼容检查 | 池=等价物 |
| `validate_worker_card` | 核对壳协议×key 条目协议×模型 | 对池名:模型=池成员模型并集(按壳协议过滤);协议兼容性=池内存在同协议或经转换可达 | pool 模型兜底 |
| `_render_config_binding` | key 留各壳配置域;启动器只设 env/config | pool 绑定时 key=池 token(写入隔离目录);base_url=池代理地址;数据驱动 provider_env.map 注入 | 壳名零分支 |
| `wizard.add_instance` | 有 key_name 才配实例 | 无 key_name 且存在池 → 默认进第一个池;直绑 key 输出"不推荐" | 默认行为变更 |
| 池日志事件 | 无 | messages.type=`pool_routing` | 新增信号 |
| 审计 action | 无 pool_* | pool_create/add_member/remove_member/rotate_token/delete | 新增 action |
| web 右栏 peek | 四分区(protocol/provider/shell/credential) | 五分区(+池)与键值并排 | UI 扩展 |

### 5.1 实例生命周期变化

```
旧: 实例注册 → key 条目检查 → 排班 → spawn → 壳进程(单 key)
新: 实例注册 → 池名通过(跳 key 检查) → 排班 → spawn → proxy 代理(池 token+多成员负载均衡)
```

### 5.2 worker_card 验证信号变化

| 检查项 | 旧(单 key) | 新(pool) |
|--------|-----------|----------|
| 模型可达 | `key.models` 含 shell 所需模型 | 池成员模型并集含 shell 所需模型 |
| 协议兼容 | exact match | exact OR anthropic↔openai_chat 可转换 |
| base_url | key 条目的 base_url | 池代理地址(proxy_port 配置) |
| shell 可行性 | 不受影响 | 不受影响(壳条目检查始终执行) |
