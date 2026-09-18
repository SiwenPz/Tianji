#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天机 v2 状态通道 — 钩子事件落账器 (state-log.py)

用途: 作为宿主的 SubagentStart / SubagentStop 钩子回调脚本,把子代理事件
      归一化后写入项目 .tianji/state.jsonl。

钩子配置示例（SubagentStart / SubagentStop 各一条）:
  [[hooks]]
  event = "SubagentStart"
  command = "python ~/.agents/skills/tianji/scripts/state-log.py"

身份规则（这是本脚本唯一重要的规则）:
  权威账本只接收身份完整的事件。身份来自共享 registry:
    1. 派工时铸的 claim token,由 marker 随任务描述带进来;
    2. 终端验收/补偿必须显式给出 EventKey(run_id/task_id/attempt)。
  两者都拿不到的事件,不是"退化成 legacy",而是**不写账本**——改写
  .tianji/diagnostics.jsonl,让人看得见、让 reducer 看不见。

任何异常均 catch 并以 exit 0 退出(hook 为 fail-open);但显式的
acceptance/compensate 子命令用非零退出码表达拒绝。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claim as claim_token  # noqa: E402
from ledger_schema import EventKey, build_event, require_canonical_uuid  # noqa: E402
from ledger_sink import append_diagnostic, append_event, synthesized_stop  # noqa: E402
from run_registry import RegistryError, RunRegistry  # noqa: E402


MARKER_TEXT_FIELDS = ("description", "prompt", "task", "task_description")

HOST = os.environ.get("TJ_HOST", "")


def _to_snake(s: str) -> str:
    """将驼峰/帕斯卡串转为小写下划线蛇形 (BusStopCase -> bus_stop_case)."""
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    return s2.lower()


def _find_agent(payload: dict) -> str:
    """从 payload 中尽力提取子代理名,依次尝试常见字段名。"""
    for key in ("subagent_name", "agent_name", "agent_type", "name", "agent", "agent_id"):
        if key in payload:
            return str(payload[key])
    return "unknown"


def _claim_token_from(payload: dict) -> str:
    """The marker a dispatch carried, if any.

    The description is the one channel a dispatch has, so that is searched
    first; an explicit field is accepted as a convenience for hosts that can
    pass structured data.
    """
    explicit = payload.get("claim_token") or payload.get("claimToken")
    if isinstance(explicit, str) and claim_token.is_well_formed(explicit):
        return explicit
    for field in MARKER_TEXT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            found = claim_token.extract_marker(value)
            if found:
                return found
    return ""


def _registry(cwd: str) -> RunRegistry:
    return RunRegistry(cwd)


def _resolve_invocation(registry: RunRegistry, key: EventKey, explicit: str) -> str:
    """The invocation a terminal event belongs to.

    An explicit id is verified to belong to this task: an event must never be
    attributed to an invocation the registry never tied to this EventKey.
    """
    if not explicit:
        return registry.task_invocation_id(key)
    invocation = registry.get_invocation(key, explicit)
    if invocation.event_key != key:
        raise RegistryError(
            f"invocation {explicit!r} does not belong to {key.as_dict()}"
        )
    return explicit


def _ledger_detail(payload: dict) -> dict:
    """The detail to record, with every capability removed.

    The claim marker travels in a text field, and a ledger is exactly the kind
    of place that gets read, copied and shared -- so it is stripped here, not
    only at the point the token was extracted.
    """
    excluded = {"cwd", "hook_event_name", "session_id", "claim_token", "claimToken"}
    detail = {}
    for key, value in payload.items():
        if key in excluded:
            continue
        if key in MARKER_TEXT_FIELDS and isinstance(value, str):
            value = claim_token.strip_marker(value)
        detail[key] = value
    return detail


def append_acceptance(cwd: str, key: EventKey, verdict: str, reason: str = "",
                      host: str = "", invocation_id: str = "",
                      session_id: str = "") -> bool:
    """Append one terminal acceptance for an EventKey; reject a duplicate.

    An acceptance without identity is not recorded: it is what "unknown" tasks
    came from. The registry decides whether a terminal outcome already exists,
    and closes the task when it accepts one.
    """
    registry = _registry(cwd)
    try:
        registry.verify_event_key(key)
    except RegistryError as exc:
        raise RegistryError(f"unknown EventKey {key.as_dict()}: {exc}") from exc

    invocation = _resolve_invocation(registry, key, invocation_id)
    now = datetime.now(timezone.utc).isoformat()
    event = build_event(
        event_id=f"acceptance:{key.run_id}:{key.task_id}:{key.attempt}",
        event="acceptance",
        host=host or HOST or "unknown",
        run_id=key.run_id,
        session_id=session_id,
        task_id=key.task_id,
        attempt=key.attempt,
        invocation_id=invocation,
        correlation_id=invocation,
        agent="tianji-verifier",
        occurred_at=now,
        recorded_at=now,
        detail={"verdict": verdict, "reason": claim_token.strip_marker(reason)},
    )
    written = append_event(cwd, event)
    if written:
        # Acceptance is terminal, so the registry closes the task: the ledger
        # must not be the only place that knows the task is over.
        registry.close_accepted_task(key)
    return written


def append_compensation(cwd: str, key: EventKey, reason: str, *, host: str = "",
                        invocation_id: str = "", session_id: str = "") -> bool:
    """Record the stop the host never sent, for a task we can identify.

    The host's stop hook only fires on success, so a worker that was killed or
    wedged leaves no stop. Synthesizing one without an identity is what made
    dead workers look alive, so an EventKey is required and verified.
    """
    registry = _registry(cwd)
    try:
        registry.verify_event_key(key)
    except RegistryError as exc:
        raise RegistryError(f"unknown EventKey {key.as_dict()}: {exc}") from exc

    invocation = _resolve_invocation(registry, key, invocation_id)
    # One definition of "the stop the host never sent", shared with the
    # reconciler: two callers, one shape.
    event = synthesized_stop(
        key,
        host=host or HOST or "unknown",
        session_id=session_id,
        invocation_id=invocation,
        reason=claim_token.strip_marker(reason),
    )
    written = append_event(cwd, event)
    if written:
        registry.close_accepted_task(key)
    return written


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append a Tianji ledger event")
    parser.add_argument("--compensate", action="store_true")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--agent", default="unknown")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--acceptance", action="store_true")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--verdict", choices=("PASS", "FAIL"))
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--invocation-id", default="")
    parser.add_argument("--correlation-id", default="")
    return parser.parse_args()


def _terminal_request(args) -> tuple[EventKey | None, int]:
    """Validate the EventKey a terminal subcommand requires."""
    if not args.run_id or not args.task_id:
        print("需要 --run-id 与 --task-id：没有 EventKey 的终端事件不写账本",
              file=sys.stderr)
        return None, 2
    try:
        run_id = require_canonical_uuid("run_id", args.run_id)
    except ValueError as exc:
        print(f"run_id 非法: {exc}", file=sys.stderr)
        return None, 2
    if args.attempt < 1:
        print("attempt 必须 >= 1", file=sys.stderr)
        return None, 2
    return EventKey(run_id, args.task_id, args.attempt), 0


def _handle_hook(cwd: str, payload: dict, host: str) -> None:
    """Normalize one host hook event, or record why it could not be.

    Identity has exactly two sources, both verified by the registry: the claim
    marker a dispatch carried, or an explicit EventKey in the payload. Anything
    else is a diagnostic -- an unidentifiable event is not a ledger event.
    """
    hook_event = payload.get("hook_event_name", "")
    if not isinstance(hook_event, str):
        return
    event = _to_snake(hook_event)
    if event not in ("subagent_start", "subagent_stop"):
        return

    session_id = str(payload.get("session_id") or "")
    agent = _find_agent(payload)
    tool_call_id = str(payload.get("toolCallId") or payload.get("tool_call_id") or "")
    detail = _ledger_detail(payload)

    def diagnostic(reason: str) -> None:
        append_diagnostic(cwd, event=event, reason=reason, host=host, agent=agent,
                          tool_call_id=tool_call_id, detail=detail)

    registry = _registry(cwd)
    token = _claim_token_from(payload)
    if token:
        if not host:
            diagnostic("no host configured for the claim; refusing to guess one")
            return
        if not tool_call_id:
            # Binding a claim to a session-wide id would make two dispatches of
            # one session share a binding, so the second would overwrite the
            # first. Without a per-call id there is no claim to make.
            diagnostic("no host call id; a claim cannot be bound without one")
            return
        try:
            invocation = registry.claim_invocation(
                token=token, tool_call_id=tool_call_id,
                host=host, session_id=session_id,
            ).invocation
        except (RegistryError, ValueError) as exc:
            diagnostic(f"claim refused: {exc}")
            return
    else:
        claimed = claim_token.payload_event_key(payload)
        if claimed is None:
            diagnostic("no claim marker and no EventKey in the hook payload")
            return
        try:
            registry.verify_event_key(claimed)
            invocation_id = _resolve_invocation(registry, claimed, "")
            invocation = registry.get_invocation(claimed, invocation_id)
        except (RegistryError, ValueError) as exc:
            diagnostic(f"payload EventKey was not confirmed by the registry: {exc}")
            return

    now = datetime.now(timezone.utc).isoformat()
    append_event(cwd, build_event(
        event_id=f"{invocation.invocation_id}:{event}",
        event=event,
        host=host or invocation.host,
        run_id=invocation.run_id,
        session_id=session_id,
        task_id=invocation.task_id,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        correlation_id=invocation.tool_call_id or invocation.correlation_id,
        agent=agent,
        occurred_at=now,
        recorded_at=now,
        detail=detail,
    ))


def main() -> int:
    try:
        args = _args()

        if args.compensate:
            if not args.reason:
                return 2
            key, code = _terminal_request(args)
            if key is None:
                return code
            try:
                append_compensation(
                    args.cwd, key, args.reason, host=args.host,
                    invocation_id=args.invocation_id, session_id=args.session_id,
                )
            except RegistryError as exc:
                print(f"补偿被拒绝: {exc}", file=sys.stderr)
                return 1
            return 0

        if args.acceptance:
            if not args.verdict:
                return 2
            key, code = _terminal_request(args)
            if key is None:
                return code
            try:
                written = append_acceptance(
                    args.cwd, key, args.verdict, args.reason, host=args.host,
                    invocation_id=args.invocation_id, session_id=args.session_id,
                )
            except RegistryError as exc:
                print(f"验收被拒绝: {exc}", file=sys.stderr)
                return 1
            return 0 if written else 3

        # 1. 从 stdin 读完整 JSON(可能为空或非法)
        # 注意:必须按字节读再显式 UTF-8 解码——Windows 下 sys.stdin 默认按
        # GBK+surrogateescape 解码,payload 里的中文会变成孤代理字符,
        # 后面 json.dumps 写 UTF-8 时直接炸(被静默吞掉,表现为不写账)。
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        if not raw.strip():
            return 0  # 空输入,静默跳过

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return 0  # 非法 JSON,静默跳过

        if not isinstance(data, dict):
            return 0  # 非字典 payload,静默跳过

        cwd = data.get("cwd") or os.getcwd()
        _handle_hook(cwd, data, args.host or HOST)

    except Exception:
        pass  # catch 一切,不允许非零退出

    return 0


if __name__ == "__main__":
    sys.exit(main())
