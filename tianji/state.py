"""状态机: 任务九态合法转换表(4.2/4.5)+派单五态(5.1)。

转换表逻辑在 CLI 层(账本 CLI 唯一写入口),DB CHECK 只兜底;否决触发器。
特殊转换(重派/闸门/审核链)的条件校验在 ops.py 钩子里完成。
"""

from .schema import TASK_STATES, DISPATCH_STATES

# 九态合法转换(4.2 全表 + 4.5 补齐点)
TASK_TRANSITIONS = {
    "new": {"discussing"},                    # triage 通过进讨论
    "discussing": {"awaiting_plan_confirm"},  # 共享理解确认+计划产出
    "awaiting_plan_confirm": {"dispatched", "discussing"},  # plan_confirm / plan_reject
    "dispatched": {"executing", "reviewing"},   # →executing: 开工证据(派单 issued→active 联动)
    #   →reviewing: 无钩子壳兜底(dsh/cline 等事件不进账本,无开工证据,
    #   worker_done 结算即唯一完工信号;2026-08-20,票27/票15 两踩后补)
    "executing": {"reviewing"},               # worker_done 单事务结算通过
    "reviewing": {"awaiting_final_confirm", "dispatched", "archived"},
    #   →awaiting_final_confirm: 机械门+审核+架构师确认通过
    #   →dispatched: mechanical_fail/review_reject 驳回重派(重派前检查上限)
    #   →archived: 成果确认闸门关闭模式
    "awaiting_final_confirm": {"archived", "discussing"},
    #   →archived: final_confirm
    #   →discussing: 用户驳回最终确认(4.5,重新对齐;不消耗机械重派计数)
    "archived": {"reopened"},                 # reopen 异议通道(总控创建)
    "reopened": {"reviewing"},                # 唯一后继;重派计数清零(10.6)
}

# 强制干预(4.4): 总控 CLI 特权+审计,例外转换不在此表
FORCE_TARGETS = set(TASK_STATES) - {"new"}

# 派单七态(5.1);requeue 标记旧派单,新派单=新行(dispatch_id 变化)
DISPATCH_TRANSITIONS = {
    "issued": {"active", "stale", "cancelled"},   # active=开工证据;stale=进程退出/双阶梯超时;cancelled=强制干预
    "active": {"done", "stale", "cancelled"},     # done=worker_done 结算通过
    "stale": {"requeue", "escalate", "cancelled", "active"},
    #  requeue=进程退出无结算(确定性重派)或短活超时;escalate=长活超时(只警告不判死)
    #  active=复活(总控确认工人其实活着在干,误标 stale 可逆,2026-08-18 实证:
    #  工人停滞被误标 stale 后用户手工续推,完工结算被 stale 拒绝,无合法回头路)
    "requeue": set(),
    "escalate": {"cancelled"},
    "done": set(),
    "cancelled": set(),  # 强制干预专属终态
}


def check_task_transition(from_state: str, to_state: str) -> bool:
    return to_state in TASK_TRANSITIONS.get(from_state, set())


def check_dispatch_transition(from_state: str, to_state: str) -> bool:
    return to_state in DISPATCH_TRANSITIONS.get(from_state, set())
