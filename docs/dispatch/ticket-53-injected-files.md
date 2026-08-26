# 票 53：外部助手注入文件收敛到统一目录（2.3 零配置口径收紧）

> 你现在应该在本票分支 `ticket-53-injected-files` 上（本文件就在分支里）。`git status` 确认。

天机是一个管 AI 编程助手的协作框架：账本(SQLite)记角色/实例/任务/审计，把不同助手壳和模型供应商按稳定组合拉进"派活→监控→验收→返工"流水线。

## 开工前提

**票 46（壳差异数据化收敛）需已合并进 main**——本票的注入文件路径要与 46 收敛后的壳条目路径口径对齐：

```bash
git fetch origin
git log --oneline origin/main | head -8   # 应能看到 ticket 46 的合并提交
```

未合并：等通知；已合并：先 `git rebase origin/main` 再开工。

## 背景

spec 2.3 口径（2026-08-25 用户裁决）："零配置文件"严格限定为**天机自身配置零文件**（全在账本 configs 表）；给外部助手注入用的临时文件（settings-controller.json / ctrl-secret.txt / key 引用文件等）允许存在，但必须**收敛到统一的一处**（单一目录，路径可预测），不得散落各处。

当前散落现状（代码锚点，都在 `TIANJI_HOME` 根下）：

- `tianji/wizard.py`：`settings-controller.json` 落 TIANJI_HOME 根（309/378/392/426 行附近多处写入）；`ctrl-secret.txt` 落根（534 行附近）；key 本体落 `home/keys/`（434 行附近）
- 读取方散在：`tianji/ctrlsession.py` / `tianji/webapp.py`（总控会话）/ `tianji/cli.py` / `tianji/ctrlprotocols.py`
- `tianji/db.py` 是路径派生的总入口（`tianji_home()` 在此），新路径常量应放这

## 目标

所有"给外部助手注入用的文件"统一收敛到一个固定目录（建议 `TIANJI_HOME/injected/`，实现期可定；原则=单一、可预测、可被 env 覆盖）。天机自身配置仍全在账本 configs 表、零配置文件。

## 工作块

1. 选定收敛目录（如 `TIANJI_HOME/injected/`，或其下再分子位），路径常量进 `tianji/db.py` 统一派生。
2. 改写入方：`wizard.py` 的 settings-controller.json / ctrl-secret.txt / keys 写入路径全部走新常量。
3. 改读取方：`ctrlsession.py` / `webapp.py` / `cli.py` / `ctrlprotocols.py` / `shellrender.py` 的读取路径全部走同一常量，消灭各处自拼路径。
4. 存量迁移：老目录已有文件的用户，首次运行自动搬到新目录（幂等、可重复执行、不静默丢、搬完留审计或日志痕迹）。
5. 若 46 已合并：注入文件路径若与壳条目 `controller_settings` 等字段相关，按 46 收敛后的字段口径实现（读壳条目拿路径模式，不硬编码壳名）。

## 验收标准

- [ ] 注入文件（settings-controller.json、ctrl-secret.txt、key 引用文件等）统一收敛到单一固定目录，路径可预测、不散落
- [ ] 天机自身运行时配置仍全在账本 configs 表，无新增散落配置文件
- [ ] 收敛后各读取方（webapp 总控会话、wizard、shellrender、daemon）路径一致，行为不回归
- [ ] 老路径存量文件自动迁移，幂等可重复
- [ ] 全量 `python -m pytest tests -q` 全绿

## 红线

- 只动与注入文件路径相关的代码，精准修改，别顺手重构无关东西。
- 账本(SQLite)是唯一写入口；注入文件是给外部助手的数据出口，不进账本。
- secret 明文只在 env / 注入文件里传递的老规矩不破坏。
- 安全红线：恒定时间比较别改回 `==`；身份校验 fail-closed 别退回 fail-open。
- 注释、文档一律大白话。

## 测试基线

- 命令：`python -m pytest tests -q`
- 你的环境 = Windows + Python 3.12（实测基准）。
- 开工先跑一遍全量，**以实际结果为基线**：全绿才许动手，不绿先修环境；收工时不许比开工基线多挂一条（主线随票合并增长，别认死数字）。

## Git 交活流程

```bash
git status                                          # 确认在 ticket-53-injected-files 分支
# ...干活...
git status && git diff --stat                       # 复查改动
git add <你实际改的文件>                             # 只加代码，别把无关文件带上去
git commit -m "ticket 53: 注入文件收敛到统一目录"
git push origin ticket-53-injected-files
```

推完在 GitHub 开 PR：base=main，head=ticket-53-injected-files。

## 交活清单（PR 描述必含）

1. **方案概述**：收敛目录选在哪、为什么；写入方/读取方怎么统一的
2. **改动清单**：文件 + 函数级
3. **验收自检**：逐条对照上面 5 条验收标准，过了的打勾 + 一句证据
4. **自测命令与输出**：pytest 全量结果
5. **迁移演示**：造一个老布局的 TIANJI_HOME，跑一次后文件搬到位、再跑一遍不重复搬

有问题（规格看不懂、路径对不上、发现规格疑似有洞）→ 停下来在交活前把问题写进 PR 描述顶部"待裁决"一节，别自己拍板发挥。
