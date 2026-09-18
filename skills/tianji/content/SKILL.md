---
name: content
description: 实测模型真实上下文窗口并把结果写回 config(仅用户显式要求时)
type: prompt
disableModelInvocation: true
---

用户要实测模型的上下文窗口。**花钱的事,只在本命令被用户执行时做,不许自动跑、不许顺手多测**。流程:

1. 读 config($KIMI_CODE_HOME 或 ~/.kimi-code/config.toml)的 [secondary_model.models] 与 [models],把模型列成编号清单:别名 + 当前声明的 max_context_size;default_model 对应的别名标注"当前主模型"。
2. **让用户选要实测哪个**,等用户拍板,不许替选。
3. 跑 `python "${KIMI_SKILL_DIR}/../scripts/ctx-probe.py" --model <用户选的别名>`(默认档 2 发近零成本:小 ping + 故意超限被拒不计费,从报错文本解析真实窗口)。
4. 成功(CTX_RESULT window>0):写回 `python "${KIMI_SKILL_DIR}/../scripts/pool-configure.py" --set-ctx <别名>=<实测值>`,向用户报"标称 X → 实测 Y"差值,提醒 `/reload` 生效。
5. 默认档失败(渠道报错不吐数字):如实报告,给选项:测下一个 / 用 --ladder 阶梯二分(**会计费,必须用户明确同意才跑**,跑前报预估消耗) / 放弃。
6. 测完一个问用户还要不要测别的,不测就收工。

使用场景:切主模型前先测目标模型真实窗口,避免声明值过小、一切换就触发压缩死锁;新模型进菜单后想知道真实窗口,也跑这个。
