---
name: tianji-proxy
description: >
  本地模型池——多渠道多 key 聚合出一个 OpenAI 兼容总入口，缓解单 key 429。
  用户没有 new-api 类池后端时，由天机引导推荐安装/本 skill 管理。
  天机只认它暴露的总入口，不关心内部。
---

# 天机号池代理 (tianji-proxy)

本地轻量号池代理，单文件纯标准库，零外部依赖。将多个渠道/多个 key
聚合为一个 OpenAI 兼容总入口，解决单 key 频繁 429 的问题。

## 前置

- Python ≥ 3.11（`tomllib` 标准库）
- 本机可访问各上游 provider API

## 安装

```bash
python install.py install --host <kimi|codex|cmdc>
```

`--host` 可以省略，但只在宿主运行时信号唯一时才会自动识别；识别不出会报错而不是替你选一个。

安装后 `skills/tianji-proxy/scripts/tianji-proxy.py` 被部署到 `~/.agents/skills/tianji-proxy/`。

## 快速开始

### 1. 添加渠道

```bash
python skills/tianji-proxy/scripts/tianji-proxy.py add-channel \
  --name "deepseek" \
  --base-url "https://api.deepseek.com/v1" \
  --models "deepseek-chat,deepseek-reasoner" \
  --keys "sk-key1,sk-key2,sk-key3"
```

渠道可共享同一 model：同一个 model 可挂在多个渠道（不同 base_url）上，
代理会在多个渠道间轮盘。

### 2. 启动代理

```bash
# 前台（Windows 用户用任务管理器 / 另起终端管理进程）
python skills/tianji-proxy/scripts/tianji-proxy.py serve

# 或指定端口 / 配置文件
python skills/tianji-proxy/scripts/tianji-proxy.py serve --port 8317 --config /path/to/proxy.toml
```

默认监听 `127.0.0.1:8317`。

### 3. 在天机配置中使用

将天机模型菜单的 base_url 指向代理总入口：

```toml
[providers]
[providers.deepseek]
base_url = "http://127.0.0.1:8317/v1"

[secondary_model.models]
deepseek-chat = { provider = "deepseek" }
```

## 配置说明

配置文件默认 `~/.tianji/proxy.toml`，环境变量 `TJ_PROXY_CONFIG` 可覆盖。

```toml
[server]
port = 8317
token = "调用方用的总key"   # 留空 = 不认证（不推荐生产使用）

[[channels]]
name = "渠道A"
base_url = "https://api.xxx.com/v1"
models = ["model-x", "model-y"]
keys = ["key1", "key2"]
protocols = ["responses", "chat/completions"]  # 可选；限制该厂商渠道支持的协议
```

### 配置铁律

- 每次写入先备份 `.bak-YYYYMMDD-HHMMSS`
- 写入后 `tomllib` 校验，失败自动回滚
- 不碰渠道之外的段

## 管理命令

### 渠道管理

```bash
# 新增渠道
add-channel --name X --base-url URL --models a,b,c --keys k1,k2

# 删除渠道
remove-channel --name X
```

### Key 管理

```bash
# 加 key
add-key --channel X --key K

# 删 key
remove-key --channel X --key K
```

### 查看状态

```bash
# 渠道/模型/key 数一览（key 掩码）
list

# 每 key 请求数/失败数/429 数/熔断状态
status
```

## 代理行为（语义摘要）

| 场景 | 行为 |
|------|------|
| 调用方无 / 错 Authorization | 401 Unauthorized |
| `GET /v1/models` | 聚合各渠道 models，熔断的 key 跳过 |
| `POST /v1/chat/completions` | 按 model 选渠道 → 轮盘选 key → 转发 |
| 上游 429 | 同请求内换 key 重试；耗尽 → 透传 429 |
| 上游 ≥ 500 | 同请求内换 key 重试；耗尽 → 502 all_members_failed |
| 上游 2xx / 4xx (非 429) | 直接透传，不重试 |
| `stream=true` | 字节级 SSE 透传；流中断不重试 |
| 熔断触发 | 失败率 ≥ 70% 且样本 ≥ 15 → 开闸 90s → 半开探测 |

### 超时

- 连接超时: 10s
- 读超时: 600s

### Key 安全

所有日志和状态输出中 key 均掩码显示（前 4 位 + *** + 后 4 位），
状态文件 `proxy-state.json` 中完整 key 仅在内存中出现，文件内不存明文。

## 熔断状态文件

持久化到 `~/.tianji/proxy-state.json`（原子写：先写临时文件再 rename）。

重启后自动恢复熔断状态和轮盘游标。

## 与天机的关系

- **天机晨检没池时** → 推荐安装本 skill，引导 add-channel
- **天机只认总入口** → 不关心里面有几个渠道/几个 key，只认
  `http://127.0.0.1:8317/v1` 这一个地址
- **工人无感** → key 轮盘、熔断、重试全在代理层完成，子代理
  只看到标准的 OpenAI 兼容接口

## 限制

- 不跨 model 选路（model 绑死渠道）
- 不做计费/限额/主动测活
- SSE 流式仅支持 OpenAI SSE 格式字节透传
- 单进程多池并发安全性依赖 GIL（典型使用场景无问题）
