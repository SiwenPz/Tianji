---
name: update
description: 手工同步号池模型菜单进宿主 config,菜单变化后引导重新分配角色
type: prompt
disableModelInvocation: true
---

用户要更新模型列表(池加了新渠道/新模型,或晨检报了 MENU_CHANGED)。按流程走,关键节点和用户确认:

1. 读 config 的 [secondary_model] default_model 拿到当前默认别名,跑 `python "${KIMI_SKILL_DIR}/../scripts/pool-configure.py" --sync --default <该别名>`(默认别名已消失时脚本会自动挑并提示)。
2. 用大白话报差异:新增哪些、消失哪些、哪些没动;无变化就直说无变化。
3. 菜单有变化时**问用户要不要重新分配角色**;用户要分,按主 skill SKILL.md【选模型·硬步骤】走:列模型清单带备注、逐角色等用户拍板。改绑既有角色时，使用 `--role "角色名=新模型别名"`；它会替换该角色的旧指派。
4. 提醒用户 `/reload` 让新菜单进模型列表。
5. 顺带提一句:想知道某个模型真实上下文窗口,可跑 /tianji.content 实测。
