# CLAUDE.md

## 仓库结构

- `tianji/` — 原型实现(账本 CLI / 监控器 / daemon / 驾驶舱);核心纯标准库 sqlite3,外围 typer/FastAPI+uvicorn/pytest
- `tests/` — pytest 套件(813)
- `docs/agents/` — agent 工作约定(见下)
- `.scratch/<effort>/` — 设计与实现地图(**不公开**,见发布纪律)
- `demo/` — 演示账本与实例材料(gitignored,含 secret,绝不入库)

## Issue tracker(实现地图)

Issues live as local markdown files under `.scratch/<effort>/`, one directory per effort:

- `HANDOFF.md` — 交接文档,记录最新进度与踩坑;**任何会话开始前先读它**
- `map.md` — 票据依赖图(Blocked by 全清才可取票)与执行约定
- `issues/NN-*.md` — 每票一个文件,自带验收标准,`Status:` 行记状态

约定详见 `docs/agents/issue-tracker.md`;标签词汇见 `docs/agents/triage-labels.md`。

当前 effort:

- `.scratch/tianji-impl/` — 实现地图(活;HANDOFF 为最新进度入口)
- `.scratch/tianji-framework/` — 设计地图(已完成;`spec.md` 设计规格书=**实现的唯一依据**,发现漏洞先回写规格书,涉及裁决须用户点头)
- `.scratch/tianji-design-r2/` — 第二轮设计(历史,设计依据备查)

## 发布纪律

GitHub 公开仓是洗掉 `.scratch` 的单提交新历史(设计文档不公开)。**不要直接 `git push` 本地 main**;发布走 `git archive` 导出 → 剔 `.scratch` → 新历史强推。

## 测试

`python -m pytest tests -q`(813,全绿才可收尾一票)

## Domain docs

Single-context: one `CONTEXT.md` at the repo root plus `docs/adr/` for architecture decisions(本仓暂未建立,惰性创建). See `docs/agents/domain.md`.
