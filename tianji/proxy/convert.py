"""非流式请求/响应双向转换: Anthropic Messages API ↔ OpenAI Chat Completion API。

两条红线:
  ① 本文件 + stream.py 合计 ≤1000 行
  ② 语义对照仅允许 one-api(MIT),禁止移植 new-api(AGPL)任何代码
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

_ANTH = "anthropic"
_OAI = "openai_chat"
_MAPIS = {_ANTH, _OAI, "openai_responses"}
_FR_A2O = {"end_turn": "stop", "max_tokens": "length",
           "tool_use": "tool_calls", "stop_sequence": "stop"}
_FR_O2A = {"stop": "end_turn", "length": "max_tokens",
           "tool_calls": "tool_use", "content_filter": "end_turn",
           "function_call": "tool_use"}


def is_conversion_needed(a: str, b: str) -> bool:
    if a == b or a not in _MAPIS or b not in _MAPIS:
        return False
    return True


def convert_request(body: dict, from_p: str, to_p: str) -> tuple:
    body = copy.deepcopy(body)
    tags: list[tuple[str, str]] = []
    if not is_conversion_needed(from_p, to_p):
        return body, tags
    if from_p == _ANTH and to_p == _OAI:
        _an2oa_req(body, tags)
    elif from_p == _OAI and to_p == _ANTH:
        _oa2an_req(body, tags)
    else:
        raise ValueError(f"请求转换不支持: {from_p} → {to_p}")
    return body, tags


def convert_response(body: dict, from_p: str, to_p: str) -> tuple:
    body = copy.deepcopy(body)
    tags: list[tuple[str, str]] = []
    if not is_conversion_needed(from_p, to_p):
        return body, tags
    if from_p == _ANTH and to_p == _OAI:
        result = _an2oa_resp(body)
    elif from_p == _OAI and to_p == _ANTH:
        result = _oa2an_resp(body)
    else:
        raise ValueError(f"响应转换不支持: {from_p} → {to_p}")
    return result, tags


def _an2oa_req(body: dict, tags: list) -> None:
    msgs: list[dict] = []
    sys_txt = _extract_system(body, tags)
    for m in body.pop("messages", []):
        role = m.get("role")
        content = m.get("content")

        if role == "user":
            flat = _flatten_content_blocks(content, _USER_DROP, tags, "user") or ""
            msgs.append({"role": "user", "content": flat})

        elif role == "assistant":
            blocks = _to_blocks(content)
            texts, tcs, drops, result_blocks = _split_assistant(blocks)
            if drops:
                tags.extend([("content", d) for d in drops])
            has_real = bool(texts or tcs)
            if has_real:
                parts: list[str] = list(texts)
                for rb in result_blocks:
                    rc = _flatten_content_blocks(
                        rb.get("content", ""), _TOOL_DROP, tags, "tool_result")
                    if rc:
                        parts.append(rc)
                new_m: dict[str, Any] = {"role": "assistant"}
                if parts:
                    stripped = [p.strip() for p in parts if p.strip()]
                    new_m["content"] = " ".join(stripped) if stripped else None
                if tcs:
                    new_m["tool_calls"] = tcs
                msgs.append(new_m)
            elif not result_blocks:
                msgs.append({"role": "assistant"})
            if result_blocks:
                for rb in result_blocks:
                    rc = _flatten_content_blocks(
                        rb.get("content", ""), _TOOL_DROP, tags, "tool_result")
                    msgs.append({"role": "tool",
                                "tool_call_id": rb.get("tool_use_id",
                                                        rb.get("id", "")),
                                "content": rc if rc is not None else ""})

        elif role == "tool":
            result_blocks = _get_tool_result_blocks(
                content, m.get("tool_use_id", ""))
            for rb in result_blocks:
                rc = _flatten_content_blocks(rb.get("content", ""),
                                             _TOOL_DROP, tags, "tool_result")
                msgs.append({
                    "role": "tool",
                    "tool_call_id": rb.get("id", rb.get("tool_use_id", "")),
                    "content": rc if rc is not None else "",
                })

    if sys_txt is not None:
        msgs.insert(0, {"role": "system", "content": sys_txt})
    body["messages"] = msgs

    for t in body.get("tools", []):
        if "input_schema" in t:
            t["parameters"] = t.pop("input_schema")
    _normalize_tc_an2oa(body, tags)
    if "stop_sequences" in body:
        body["stop"] = body.pop("stop_sequences")
    _drop_keys(body, ["top_k", "tools_config", "metadata",
                      "thinking", "prompt_caching"])
    if tags:
        log.info("anthropic→openai 请求有损转换: %s",
                 ", ".join(f"{f}:{r}" for f, r in tags))


def _extract_system(body: dict, tags: list) -> str | None:
    sv = body.pop("system", None)
    if sv is None:
        return None
    if isinstance(sv, str):
        return sv
    if not isinstance(sv, list):
        return None
    parts: list[str] = []
    for blk in sv:
        bt = blk.get("type")
        if bt == "text":
            parts.append(blk.get("text", ""))
        elif bt == "image":
            parts.append("[图片已省略]")
            tags.append(("system", "图片"))
        elif bt == "thinking":
            tags.append(("system", "thinking丢弃"))
    return "".join(parts)


def _flatten_content_blocks(content: Any, drops: dict[str, str],
                             tags: list, ctx: str) -> str | None:
    blocks = _to_blocks(content)
    parts: list[str] = []
    has_non_text = False
    for b in blocks:
        bt = b.get("type")
        if bt == "text":
            parts.append(b.get("text", ""))
        elif bt == "image" or bt == "image_url":
            parts.append("[图片已省略]")
            has_non_text = True
            r = drops.get("image", "图片已省略")
            tags.append((ctx, r))
        elif bt == "thinking":
            tags.append((ctx, drops.get("thinking", "thinking丢弃")))
        elif bt == "tool_result":
            rc = _flatten_content_blocks(b.get("content", ""),
                                         drops, tags, ctx)
            parts.append(rc or "")
    if not parts:
        return None
    return "".join(parts) if not has_non_text else "\n".join(parts)


_USER_DROP = {"image": "图片", "thinking": "thinking丢弃"}
_TOOL_DROP = {"image": "图片", "thinking": "thinking丢弃"}


def _to_blocks(content: Any) -> list[dict]:
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def _split_assistant(blocks: list[dict]) -> tuple:
    texts: list[str] = []
    tcs: list[dict] = []
    drops: list[str] = []
    result_blocks: list[dict] = []
    for b in blocks:
        bt = b.get("type")
        if bt == "text":
            if b.get("text"):
                texts.append(b["text"])
        elif bt == "tool_use":
            tcs.append({
                "id": b["id"],
                "type": "function",
                "function": {
                    "name": b["name"],
                    "arguments": json.dumps(b.get("input", {}),
                                            ensure_ascii=False),
                },
            })
        elif bt == "tool_result":
            result_blocks.append({
                "id": b.get("tool_use_id", b.get("id", "")),
                "content": _to_blocks(b.get("content", "")),
            })
        elif bt == "image":
            drops.append("图片")
        elif bt == "thinking":
            drops.append("thinking")
    return texts, tcs, drops, result_blocks


def _get_tool_result_blocks(content: Any, fallback_id: str) -> list[dict]:
    if isinstance(content, list):
        results = []
        for b in content:
            if b.get("type") == "tool_result":
                results.append(b)
        return results if results else [
            {"type": "tool_result", "tool_use_id": fallback_id,
             "content": content}]
    return [{"type": "tool_result", "tool_use_id": fallback_id,
             "content": content or ""}]


def _normalize_tc_an2oa(body: dict, tags: list) -> None:
    tc = body.get("tool_choice")
    if tc is None:
        return
    if isinstance(tc, dict):
        tt = tc.get("type")
        if tt == "tool":
            body["tool_choice"] = {"type": "function",
                                   "function": {"name": tc["name"]}}
        elif tt == "auto":
            body["tool_choice"] = "auto"
        elif tt == "any":
            body["tool_choice"] = "required"
        elif tt == "none":
            body["tool_choice"] = "none"
    elif tc == {}:
        body.pop("tool_choice", None)


def _drop_keys(body: dict, keys: list[str]) -> None:
    for k in keys:
        if k in body:
            del body[k]


def _oa2an_req(body: dict, tags: list) -> None:
    msgs = body.pop("messages", [])
    sys_parts = []
    rest = []
    for m in msgs:
        if m.get("role") == "system":
            sys_parts.append(m.get("content", "") or "")
        else:
            rest.append(m)
    if sys_parts:
        body["system"] = "".join(p for p in sys_parts if p)
    merged = _merge_consecutive_messages(rest)
    an_msgs = []
    for m in merged:
        role = m.get("role")
        if role == "user":
            an_msgs.append({"role": "user", "content": m.get("content", "") or ""})
        elif role == "assistant":
            an_msgs.append(_build_an_assistant(m, tags))
        elif role == "tool":
            blocks = [{"type": "tool_result",
                       "tool_use_id": m.get("tool_call_id", ""),
                       "content": m.get("content", "")}]
            for tr in m.get("_tool_results", []):
                blocks.append({"type": "tool_result",
                               "tool_use_id": tr.get("call_id", ""),
                               "content": tr.get("content", "")})
            if blocks:
                an_msgs.append({"role": "assistant", "content": blocks})
    body["messages"] = an_msgs
    for t in body.get("tools", []):
        if "parameters" in t:
            t["input_schema"] = t.pop("parameters")
    _normalize_tc_oa2an(body)
    if "stop" in body:
        body["stop_sequences"] = body.pop("stop")
    if "max_tokens" not in body:
        body["max_tokens"] = 4096
    for k in ["top_k", "tools_config", "metadata", "thinking",
               "prompt_caching", "service_tier"]:
        if k in body:
            del body[k]


def _merge_consecutive_messages(msgs: list[dict]) -> list[dict]:
    if not msgs:
        return []
    merged: list[dict] = [copy.deepcopy(msgs[0])]
    for m in msgs[1:]:
        prev = merged[-1]
        if prev.get("role") == m.get("role") == "tool":
            prev.setdefault("_tool_results", []).append({
                "content": m.get("content", ""),
                "call_id": m.get("tool_call_id", ""),
            })
        elif prev.get("role") == m.get("role") == "assistant":
            if m.get("tool_calls"):
                prev.setdefault("tool_calls", []).extend(m["tool_calls"])
            c = m.get("content")
            if c:
                prev["content"] = (prev.get("content") or "") + c
        else:
            merged.append(copy.deepcopy(m))
    return merged


def _build_an_assistant(m: dict, tags: list) -> dict:
    blocks: list[dict] = []
    c = m.get("content") or ""
    if c:
        blocks.append({"type": "text", "text": c})
    for tc in m.get("tool_calls", []):
        fn = tc.get("function", {})
        args_str = fn.get("arguments", "")
        try:
            inp = json.loads(args_str) if args_str.strip() else {}
        except json.JSONDecodeError:
            inp = {"_raw": args_str}
            tags.append(("tool_args", "arguments JSON解析失败"))
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"call_{len(blocks)}"),
            "name": fn.get("name", ""),
            "input": inp,
        })
    return {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}


def _normalize_tc_oa2an(body: dict) -> None:
    tc = body.get("tool_choice")
    if tc is None:
        return
    if tc == "auto":
        body["tool_choice"] = {"type": "auto"}
    elif tc == "required":
        body["tool_choice"] = {"type": "any"}
    elif tc == "none":
        body["tool_choice"] = {"type": "none"}
    elif isinstance(tc, dict) and tc.get("type") == "function":
        body["tool_choice"] = {"type": "tool",
                               "name": tc["function"]["name"]}


def _an2oa_resp(body: dict) -> dict:
    content_blocks = body.pop("content", [])
    msg_content = ""
    tool_calls: list[dict] = []
    for b in content_blocks:
        bt = b.get("type")
        if bt == "text":
            msg_content += b.get("text", "")
        elif bt == "tool_use":
            tc_args = json.dumps(b.get("input", {}), ensure_ascii=False)
            tool_calls.append({
                "id": b["id"],
                "type": "function",
                "function": {"name": b["name"], "arguments": tc_args},
            })
    choice = {
        "index": 0,
        "message": {"role": "assistant"},
        "finish_reason": _FR_A2O.get(body.pop("stop_reason", ""), "stop"),
    }
    if msg_content:
        choice["message"]["content"] = msg_content
    else:
        choice["message"]["content"] = None
    if tool_calls:
        choice["message"]["tool_calls"] = tool_calls
    result: dict[str, Any] = {
        "id": body.pop("id", "conv"),
        "object": "chat.completion",
        "model": body.pop("model", ""),
        "choices": [choice],
    }
    usage = body.pop("usage", {})
    it = usage.get("input_tokens", 0)
    ot = usage.get("output_tokens", 0)
    result["usage"] = {
        "prompt_tokens": it,
        "completion_tokens": ot,
        "total_tokens": it + ot,
    }
    return result


def _oa2an_resp(body: dict) -> dict:
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    blocks: list[dict] = []
    msg_c = msg.get("content") or ""
    if msg_c:
        blocks.append({"type": "text", "text": msg_c})
    for tc in msg.get("tool_calls", []):
        fn = tc.get("function", {})
        args_str = fn.get("arguments", "")
        try:
            inp = json.loads(args_str) if args_str.strip() else {}
        except json.JSONDecodeError:
            inp = {"_raw": args_str}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": inp,
        })
    usage = body.get("usage", {})
    result: dict[str, Any] = {
        "id": body.get("id", "conv"),
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": body.get("model", ""),
        "stop_reason": _FR_O2A.get(choice.get("finish_reason", "stop"),
                                    "end_turn"),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
    return result
