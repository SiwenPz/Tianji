# 新同事起步指南（自举版）

你在天机(Tianji)项目领了一张票。本文件就是你的起步指南，照着走就行。

天机是一个管 AI 编程助手的协作框架：账本(SQLite)记角色/实例/任务/审计，把不同助手壳和模型供应商按稳定组合拉进"派活→监控→验收→返工"流水线，自举开发中。

## 第一步：切到你的票分支（不 clone！）

你的工作目录已经指向本地的 Tianji 项目仓库，**不要执行 git clone**：

```bash
# 0. 如果之前 clone 出了多余的嵌套目录（比如目录下又多了个 Tianji/），删掉它，回原项目目录
# 1. 确认远端指向正确（应为 github.com/SiwenPz/Tianji.git）
git remote -v
# 2. 拉取远端最新
git fetch origin
# 3. 切到你的票分支（-B 强制对齐远端，就算本地残留同名旧分支也不怕）
git checkout -B <你的票分支名> origin/<你的票分支名>
# 4. 确认人在票分支上（应显示 ticket-NN-xxx）
git status
```

提示：若 checkout 报"本地改动会被覆盖"，先 `git stash` 暂存再切，别强丢现场。

## 第二步：环境和基线

```bash
pip install -e .          # 装过会秒过，可跳过
python -m pytest tests -q # 全量测试
```

- 基准环境 = Windows + Python 3.12，**508 条全绿**。
- 不绿先修环境，别带病开工。

## 第三步：读文档（按序）

1. `README.md` — 项目是啥
2. `CLAUDE.md` — 仓库约定
3. `CONTRIBUTING.md` — 干活规矩
4. `docs/agents/issue-tracker.md` — 工作约定

## 第四步：读你的任务书

本目录（`docs/dispatch/`）里的 `ticket-NN-*.md` 就是你的活：目标、规格、验收标准、交活清单全在里面。逐条读完再动手。

任务书若写明"开工前提"（依赖前置票），先确认前提满足再动手：

```bash
git fetch origin
git log --oneline origin/main | head -8   # 看前置票是否已合并
```

未满足：等通知；已满足但本分支基线落后：`git rebase origin/main` 后再开工。

## 工作方式

- 一切工作在你的票分支上进行，**绝不碰 main**——main 是发布用的单提交快照，只有审核通过后由维护方合并。
- 做完推到 GitHub 对应分支，开 PR：base=main，head=你的票分支，PR 描述按任务书的交活清单写。
- 审核方在 PR 上审你的代码（对照任务书逐条验收），有问题在 PR 评论里提，你改完推同一分支。

## 纪律（硬的）

- **测试全绿才交**：`python -m pytest tests -q`（非 Windows 平台以开工基线为准，不得新增失败）。
- 账本(SQLite)是唯一写入口，任何状态变更走账本 CLI/ops，别旁路。
- 核心纯标准库，加依赖先在 PR 里说清楚为什么非它不可。
- 文档、注释、对话一律大白话，不堆英文术语；机制层的命令/黑话不许甩给用户当使用步骤。
- 简洁优先、精准修改：只动与当前票直接相关的代码。
- 跨平台写法，代码保持 Python 3.9 语法兼容（实测基准 Windows 10 + Python 3.12）。
- 安全红线：恒定时间比较别改回 `==`；事件身份校验 fail-closed 别退回 fail-open。
- 规格看不懂/字段对不上/发现疑似设计漏洞 → 停下来，写进 PR 描述"待裁决"一节，别自己拍板发挥。

## 就绪信号

环境装好、基线全绿后，直接按任务书开工，干完交 PR。
