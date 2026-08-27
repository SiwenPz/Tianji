# 票 48：额度/卫生/上下文窗口补齐（14.1 转录 usage + cc-switch + 14.4 摘要读取 + 13.1 上下文窗口）

> 你现在应该在本票分支 `ticket-48-quota-hygiene` 上（本文件就在分支里）。`git status` 确认。

天机是一个管 AI 编程助手的协作框架：账本(SQLite)记角色/实例/任务/审计，把不同助手壳和模型供应商按稳定组合拉进"派活→监控→验收→返工"流水线。

## 开工前提

**票 46（壳差异数据化收敛）需已合并进 main**——本票要往 monitor.py 巡检里接线，与 46 同文件改动区重叠，先合 46 省 rebase：

```bash
git fetch origin
git log --oneline origin/main | head -8   # 应能看到 ticket 46 的合并提交
```

未合并：等通知；已合并：先 `git rebase origin/main` 再开工。

## 背景

代码审计发现三处"实现了但没生效"的假象，外加一处漏做。**"看起来实现了、实际没生效"比明说没做更危险——本票必须消除这个假象。**

1. **转录 usage 累加（死代码）**：`tianji/quota.py:53` `scan_transcript_usage()` 已实现但全仓无调用方，未接进 monitor 巡检、无 CLI 命令。
2. **cc-switch 账目读取（死代码）**：`tianji/quota.py:83` `read_ccswitch()` 无调用方；只读了 proxy_request_logs 的 429，没读 usage_daily_rollups/provider_health；429→exhausted 标记只在它里面置位，故"额度已尽必知"当前休眠。
3. **换活打扫摘要（漏做）**：`tianji/hygiene.py:41` `cleanup()` 摘要只有任务元数据，没按规格"读转录尾部 N 行 + 任务书 + 产物清单"。
4. **上下文窗口（漏做）**：key 条目 models 只有 `{id, display}`（`tianji/integrations.py:248` 附近 `cur["models"] = [{"id": i} for i in ids]`），无 context_window 字段，discover/probe 不探测上下文窗口。

## 工作块

1. **14.1② 转录 usage 接线**：优先接线——挂进监控器巡检顺带累加（或明确删除并回写 spec 14.1 口径；若评估"转录 usage 累加"对第三方 key 无意义可裁决删除，PR 里写明理由，不要留死代码）。
2. **14.1③ cc-switch 接线**：接线后 cc-switch 的 429/403 错误码归类写实例档案，额度已尽置 exhausted 并触发"暂停派新活"；补读 usage_daily_rollups/provider_health。
3. **14.4 换活打扫摘要补齐**：摘要含转录尾部 N 行 + 任务书摘要 + 产物清单（N 存账本配置可改；复用 monitor 断点摘要的读尾部手法）。
4. **13.1 上下文窗口**：key 条目 models 加 context_window 字段（discover/probe 探测可得则填，探测不到标"待实测"），供 14.2 上下文健康度与 9.2 硬过滤读取。

## 验收标准

- [ ] 决定接线还是删除（优先接线，保持 14.1 三层信号完整）；若删除则回写 spec 14.1 口径
- [ ] 接线后：监控器巡检顺带累加转录 usage；cc-switch 429/403 错误码归类写实例档案，额度已尽置 exhausted 并触发"暂停派新活"
- [ ] 14.4：换活打扫的摘要含转录尾部 N 行 + 任务书摘要 + 产物清单（N 存账本配置可改）
- [ ] 13.1：key 条目 models 含 context_window（探测可得则填，否则标待实测），14.2/9.2 可读
- [ ] 全量 `python -m pytest tests -q` 全绿

## 红线

- "接线还是删除"若拿不准，写进 PR"待裁决"，别自己拍死。
- 账本(SQLite)是唯一写入口，巡检/归类结果全落账本。
- 简洁优先、精准修改：只动额度/卫生/上下文窗口相关的代码。
- 注释、文档一律大白话。

## 测试基线

- 命令：`python -m pytest tests -q`
- 你的环境 = Windows + Python 3.12（实测基准）。
- 开工先跑一遍全量，**以实际结果为基线**：全绿才许动手，不绿先修环境；收工时不许比开工基线多挂一条（主线随票合并增长，别认死数字）。

## Git 交活流程

```bash
git status                                          # 确认在 ticket-48-quota-hygiene 分支
# ...干活...
git status && git diff --stat                       # 复查改动
git add <你实际改的文件>                             # 只加代码，别把无关文件带上去
git commit -m "ticket 48: 额度/卫生/上下文窗口补齐"
git push origin ticket-48-quota-hygiene
```

推完在 GitHub 开 PR：base=main，head=ticket-48-quota-hygiene。

## 交活清单（PR 描述必含）

1. **方案概述**：三处死代码/漏做各自怎么处理的（接线/删除+理由）；上下文窗口怎么探测
2. **改动清单**：文件 + 函数级
3. **验收自检**：逐条对照上面 5 条验收标准，过了的打勾 + 一句证据
4. **自测命令与输出**：pytest 全量结果
5. **假象消除证明**：指出原来"看起来实现了实际没生效"的三处，说明现在如何可验证地生效（或已删干净）

有问题（规格看不懂、评估拿不准、发现规格疑似有洞）→ 停下来在交活前把问题写进 PR 描述顶部"待裁决"一节，别自己拍板发挥。
