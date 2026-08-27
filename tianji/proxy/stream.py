"""SSE 双向转换: Anthropic Messages API ↔ OpenAI Chat Completion API。

Anthropic→OpenAI: Anthropic SSE → OpenAI SSE (data lines only).
OpenAI→Anthropic: OpenAI SSE → Anthropic SSE (event: + data: prefix).
同协议: pass-through。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

log = logging.getLogger(__name__)

_ANTH = "anthropic"
_OAI = "openai_chat"

_FR_A2O = {"end_turn": "stop", "max_tokens": "length",
           "tool_use": "tool_calls", "stop_sequence": "stop"}
_FR_O2A = {"stop": "end_turn", "length": "max_tokens",
           "tool_calls": "tool_use", "content_filter": "end_turn",
           "function_call": "tool_use"}


def is_conversion_needed(a: str, b: str) -> bool:
    return a != b and a in (_ANTH, _OAI) and b in (_ANTH, _OAI)


def stream_convert(source: Iterator[str], from_p: str, to_p: str,
                   model: str = "") -> Iterator[str]:
    if not is_conversion_needed(from_p, to_p):
        yield from source
        return
    if from_p == "anthropic" and to_p == "openai_chat":
        yield from _AnthropicToOpenAI().convert(source)
    elif from_p == "openai_chat" and to_p == "anthropic":
        yield from _OpenAIToAnthropic(model).convert(source)
    else:
        raise ValueError(f"流式转换不支持: {from_p} → {to_p}")


class _AnthropicToOpenAI:
    """Anthropic SSE → OpenAI SSE (data lines only)."""

    def convert(self, src: Iterator[str]) -> Iterator[str]:
        buf: dict[str, Any] = {
            "id": "", "model": "", "role": "",
            "stop_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        tcid_to_tcbid: dict[str, int] = {}
        emitted = False

        for raw in src:
            for line in raw.split("\n"):
                line = line.strip()
                if not line or line == "data: [DONE]":
                    continue
                data_str = _extract_data(line)
                if data_str is None:
                    continue
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")

                if etype == "message_start":
                    msg = evt.get("message", {})
                    buf["id"] = msg.get("id", "")
                    buf["model"] = msg.get("model", "")
                    buf["role"] = msg.get("role", "assistant")
                    buf["usage"] = msg.get("usage", {"input_tokens": 0,
                                                      "output_tokens": 0})
                    c = {
                        "id": buf["id"], "object": "chat.completion.chunk",
                        "created": 0, "model": buf["model"],
                        "choices": [{"index": 0, "delta": {"role": "assistant",
                                                            "content": ""},
                                     "finish_reason": None}],
                    }
                    yield _oa_chunk(json.dumps(c, separators=(',', ':')))
                    emitted = True

                elif etype == "content_block_start":
                    bidx = evt.get("index", 0)
                    cb = evt.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        tc_id = cb.get("id", "")
                        tc_name = cb.get("name", "")
                        oai_idx = len(tcid_to_tcbid)
                        tcid_to_tcbid[tc_id] = oai_idx
                        c = {
                            "id": buf["id"], "object": "chat.completion.chunk",
                            "created": 0, "model": buf["model"],
                            "choices": [{"index": 0,
                                         "delta": {"tool_calls": [{
                                             "index": oai_idx,
                                             "id": tc_id, "type": "function",
                                             "function": {"name": tc_name,
                                                          "arguments": ""},
                                         }]},
                                         "finish_reason": None}],
                        }
                        yield _oa_chunk(json.dumps(c, separators=(',', ':')))

                elif etype == "content_block_delta":
                    delta = evt.get("delta", {})
                    dtype = delta.get("type", "")
                    bidx = evt.get("index", 0)
                    c = {
                        "id": buf["id"], "object": "chat.completion.chunk",
                        "created": 0, "model": buf["model"],
                        "choices": [{"index": 0,
                                     "delta": {},
                                     "finish_reason": None}],
                    }
                    if dtype == "text_delta":
                        c["choices"][0]["delta"]["content"] = delta.get(
                            "text", "")
                    elif dtype == "input_json_delta":
                        pj = delta.get("partial_json", "")
                        tc_id = _tcbid_for_block(tcid_to_tcbid, bidx)
                        tc = {"index": 0, "id": tc_id, "type": "function",
                              "function": {"name": "", "arguments": pj}}
                        c["choices"][0]["delta"]["tool_calls"] = [tc]
                    if c["choices"][0]["delta"]:
                        yield _oa_chunk(json.dumps(c, separators=(',', ':')))

                elif etype == "message_delta":
                    stop = evt.get("delta", {}).get("stop_reason", "")
                    u = evt.get("usage", {})
                    buf["stop_reason"] = stop
                    buf["usage"] = u
                    fr = _FR_A2O.get(stop, "stop")
                    c = {
                        "id": buf["id"], "object": "chat.completion.chunk",
                        "created": 0, "model": buf["model"],
                        "choices": [{"index": 0, "delta": {},
                                     "finish_reason": fr}],
                    }
                    ot = u.get("output_tokens", 0)
                    if ot:
                        it = u.get("input_tokens", 0)
                        c["usage"] = {
                            "prompt_tokens": it,
                            "completion_tokens": ot,
                            "total_tokens": it + ot,
                        }
                    yield _oa_chunk(json.dumps(c, separators=(',', ':')))

                elif etype == "message_stop":
                    yield _oa_done()

        if not emitted:
            yield _oa_chunk(json.dumps({
                "id": "conv", "object": "chat.completion.chunk",
                "created": 0, "model": buf["model"],
                "choices": [{"index": 0,
                             "delta": {"role": "assistant",
                                       "content": ""},
                             "finish_reason": None}],
            }, separators=(',', ':')))
            yield _oa_done()


def _oa_chunk(data: str) -> str:
    return f"data: {data}\n\n"


def _oa_done() -> str:
    return "data: [DONE]\n\n"


def _tcbid_for_block(tcid_map: dict[str, int], bidx: int) -> str:
    for tid, bi in tcid_map.items():
        if bi == bidx:
            return tid
    return ""


def _extract_data(line: str) -> str | None:
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    return data


class _OpenAIToAnthropic:
    """OpenAI SSE(data: 行) → Anthropic SSE(event: 前缀)。"""

    def __init__(self, model: str = ""):
        self._model = model

    def convert(self, src: Iterator[str]) -> Iterator[str]:
        s = _OAI2ACtx(self._model)
        for raw in src:
            raw = raw.strip()
            if not raw:
                continue
            if raw.startswith("data:"):
                data_str = raw[5:].strip()
            elif raw.startswith("{"):
                data_str = raw
            else:
                continue
            if data_str == "[DONE]":
                gen = s.feed_finish()
                for line in gen:
                    yield line
                continue
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            obj = chunk.get("object")
            if obj is not None and obj != "chat.completion.chunk":
                continue
            gen = s.feed(chunk)
            for line in gen:
                yield line
        gen = s.feed_finish()
        for line in gen:
            yield line


class _OAI2ACtx:
    def __init__(self, model: str):
        self._model = model
        self._reset()

    def _reset(self):
        self._state = "INIT"
        self._block_idx = 1
        self._tc_idx_to_bidx: dict[int, int] = {}
        self._active: list[int] = []
        self._tc_info: dict[int, dict] = {}
        self._cached_stop = "end_turn"
        self._cached_usage: dict[str, int] = {}
        self._started = False
        self._msg_id = ""

    def feed(self, chunk: dict) -> Iterator[str]:
        lines: list[str] = []
        choice = _first_choice(chunk)

        if self._state == "INIT":
            delta = (choice.get("delta") or {}) if choice else {}
            if delta.get("role") == "assistant":
                self._msg_id = chunk.get("id", "")
                lines.append(self._msg_start_ev())
                self._state = "STREAMING"
                self._started = True

        if self._state == "STREAMING":
            delta = (choice.get("delta") or {}) if choice else {}
            if "content" in delta:
                bidx = 0
                if bidx not in self._active:
                    self._active.append(bidx)
                lines.append(self._text_ev(bidx, delta["content"] or ""))

            for tc_d in (delta.get("tool_calls") or []):
                if isinstance(tc_d, str):
                    try:
                        tc_d = json.loads(tc_d)
                    except json.JSONDecodeError:
                        continue
                oi = tc_d.get("index", 0)
                bidx = self._alloc_block(oi)
                fc = tc_d.get("function") or {}
                tc_id = tc_d.get("id", "")
                tc_nm = fc.get("name", "")
                if bidx not in self._tc_info:
                    self._tc_info[bidx] = {"id": tc_id, "nm": tc_nm,
                                            "args": ""}
                    lines.append(self._tc_start_ev(bidx, self._tc_info[bidx]))
                partial = fc.get("arguments", "") or ""
                if partial:
                    self._tc_info[bidx]["args"] += partial
                    lines.append(self._tc_delta_ev(bidx, partial))

            fr = (choice.get("finish_reason") if choice else None)
            if fr:
                self._cached_stop = _FR_O2A.get(fr, "end_turn")
                u = (delta.get("usage") or choice.get("usage") or
                     chunk.get("usage") or {})
                if u:
                    self._cached_usage = u
                self._state = "FINISHED"

        elif self._state == "FINISHED" and choice:
            u = (choice.get("usage") or
                 ((choice.get("delta") or {}).get("usage") or {}) or
                 chunk.get("usage") or {})
            if u:
                self._cached_usage.update({k: v for k, v in u.items() if v})

        for line in lines:
            yield line

    def feed_finish(self) -> Iterator[str]:
        if not self._started:
            return
        if self._state == "INIT":
            lines = [self._msg_start_ev()]
            self._state = "STREAMING"
            for line in lines:
                yield line
            yield from self._closing()
            self._state = "DONE"
            return
        if self._state in ("STREAMING", "FINISHED"):
            yield from self._closing()
        self._state = "DONE"

    def _alloc_block(self, oi: int) -> int:
        if oi not in self._tc_idx_to_bidx:
            bidx = self._block_idx
            self._block_idx += 1
            self._tc_idx_to_bidx[oi] = bidx
            self._active.append(bidx)
        return self._tc_idx_to_bidx[oi]

    def _closing(self) -> Iterator[str]:
        for bidx in sorted(self._active):
            yield self._a_sse(
                json.dumps({"type": "content_block_stop", "index": bidx},
                           separators=(',', ':')),
                event="content_block_stop",
            )
        delta_bundle = {"stop_reason": self._cached_stop}
        u: dict = {}
        if self._cached_usage:
            u = {
                "input_tokens": self._cached_usage.get("prompt_tokens", 0),
                "output_tokens": self._cached_usage.get("completion_tokens", 0),
            }
        yield self._a_sse(json.dumps({
            "type": "message_delta",
            "delta": delta_bundle,
            "usage": u,
        }, separators=(',', ':')), event="message_delta")
        yield self._a_sse(json.dumps({"type": "message_stop"},
                           separators=(',', ':')),
                          event="message_stop")

    def _a_sse(self, data: str, event: str = "") -> str:
        lines = []
        if event:
            lines.append(f"event: {event}")
        lines.append(f"data: {data}")
        lines.append("")
        return "\n".join(lines)

    def _msg_start_ev(self) -> str:
        return self._a_sse(json.dumps({
            "type": "message_start",
            "message": {
                "id": self._msg_id, "type": "message", "role": "assistant",
                "content": [], "model": self._model,
                "stop_reason": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }, separators=(',', ':')), event="message_start")

    def _text_ev(self, bidx: int, text: str) -> str:
        return self._a_sse(json.dumps({
            "type": "content_block_delta",
            "index": bidx,
            "delta": {"type": "text_delta", "text": text},
        }, separators=(',', ':')), event="content_block_delta")

    def _tc_start_ev(self, bidx: int, info: dict) -> str:
        return self._a_sse(json.dumps({
            "type": "content_block_start",
            "index": bidx,
            "content_block": {
                "type": "tool_use",
                "id": info["id"],
                "name": info["nm"],
                "input": {},
            },
        }, separators=(',', ':')), event="content_block_start")

    def _tc_delta_ev(self, bidx: int, partial: str) -> str:
        return self._a_sse(json.dumps({
            "type": "content_block_delta",
            "index": bidx,
            "delta": {"type": "input_json_delta",
                      "partial_json": partial},
        }, separators=(',', ':')), event="content_block_delta")


def _first_choice(chunk: dict) -> dict:
    cs = chunk.get("choices")
    return cs[0] if cs else {}


def _sse_lines(*events: str) -> list[str]:
    parts = []
    for ev in events:
        parts.append(f"event: {ev['type']}\n")
        parts.append("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n")
    return parts
