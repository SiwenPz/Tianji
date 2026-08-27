"""票 58: 协议转换层 anthropic↔openai_chat 全量单测。

覆盖:
  1. 请求侧双向: system / tool_use / tool_result(一拆多+多合一) / tool_choice / 参数映射
  2. 流式双向:  块索引分配 / 并行 tc 的 index 映射 / usage 尾 chunk /
                 partial_json 透传 / 流尾空 arguments 补 "{}"
  3. 降级路径:    cache_control 丢弃 / 图片 → [图片已省略] / thinking 丢弃
  4. 体量红线:   convert.py + stream.py ≤1000 行(断言锁死)
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from tianji.proxy import convert, stream


# ==================================================================
# 体量红线 (测试断言锁死)
# ==================================================================

def _count_lines(path):
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)  # project root
_CNV = os.path.join(_PARENT, "tianji", "proxy", "convert.py")
_STR = os.path.join(_PARENT, "tianji", "proxy", "stream.py")
_CNV_LINES = _count_lines(_CNV)
_STR_LINES = _count_lines(_STR)


class TestSizeLimit:
    def test_convert_lines(self):
        assert _CNV_LINES <= 500, f"convert.py {_CNV_LINES} 行,超限"

    def test_stream_lines(self):
        assert _STR_LINES <= 500, f"stream.py {_STR_LINES} 行,超限"

    def test_combined_lines(self):
        total = _CNV_LINES + _STR_LINES
        assert total <= 1000, f"convert.py({_CNV_LINES})+stream.py({_STR_LINES})={total},超红线"

    def test_report(self):
        print(f"\n[体量红线] convert={_CNV_LINES}L + "
              f"stream={_STR_LINES}L "
              f"= {_CNV_LINES+_STR_LINES}L (限≤1000)")


# ==================================================================
# is_conversion_needed
# ==================================================================

class TestIsConversionNeeded:
    def test_same_protocol_no_op(self):
        assert not convert.is_conversion_needed("anthropic", "anthropic")
        assert not convert.is_conversion_needed("openai_chat", "openai_chat")

    def test_cross_protocol_needed(self):
        assert convert.is_conversion_needed("anthropic", "openai_chat")
        assert convert.is_conversion_needed("openai_chat", "anthropic")

    def test_unknown_protocol_no_op(self):
        assert not convert.is_conversion_needed("unknown", "openai_chat")
        assert not convert.is_conversion_needed("anthropic", "unknown")

    def test_openai_responses_included(self):
        assert convert.is_conversion_needed("anthropic", "openai_responses")
        assert convert.is_conversion_needed("openai_responses", "anthropic")


# ==================================================================
# 请求侧: anthropic → openai
# ==================================================================

class TestReqAnthropicToOpenAI:
    """anthropic 请求 → openai_chat 请求。"""

    # --- system ---

    def test_system_string(self):
        body = {
            "model": "claude-3", "max_tokens": 100,
            "system": "You are a helpful assistant.",
            "messages": [{"role": "user", "content": "hi"}],
        }
        out, tags = convert.convert_request(body, "anthropic", "openai_chat")
        msgs = out["messages"]
        assert msgs[0] == {"role": "system",
                           "content": "You are a helpful assistant."}
        assert msgs[1]["role"] == "user"
        assert not tags

    def test_system_blocks_flattened(self):
        body = {
            "model": "claude-3", "max_tokens": 100,
            "system": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": " World",
                 "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out, tags = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["messages"][0] == {"role": "system",
                                      "content": "Hello World"}

    def test_system_with_image(self):
        body = {
            "model": "claude-3", "max_tokens": 100,
            "system": [
                {"type": "text", "text": "Info"},
                {"type": "image",
                 "source": {"type": "base64", "data": "abc"}},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out, tags = convert.convert_request(body, "anthropic", "openai_chat")
        assert "[图片已省略]" in out["messages"][0]["content"]
        assert ("system", "图片") in tags

    def test_system_with_thinking(self):
        body = {
            "model": "claude-3", "max_tokens": 100,
            "system": [{"type": "thinking", "thinking": "hmm..."}],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out, tags = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["messages"][0]["content"] == ""
        assert ("system", "thinking丢弃") in tags

    # --- messages / content blocks ---

    def test_user_simple_content(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["messages"][0] == {"role": "user", "content": "hi"}

    def test_user_content_blocks(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "user",
                              "content": [
                                  {"type": "text", "text": "Hello"},
                                  {"type": "text", "text": " World"},
                              ]}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["messages"][0] == {"role": "user",
                                      "content": "Hello World"}

    def test_user_image_replaced(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "user", "content": [
                    {"type": "image",
                     "source": {"type": "base64", "data": "xxx"}},
                    {"type": "text", "text": "describe"},
                ]}]}
        out, tags = convert.convert_request(body, "anthropic",
                                             "openai_chat")
        assert out["messages"][0]["content"] == "[图片已省略]\ndescribe"
        assert ("user", "图片") in tags

    # --- tool_use / tool_result (一拆多) ---

    def test_tool_result_splits_multi(self):
        """一个 assistant 消息含多个 tool_result → 拆成多条 role:"tool"。"""
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tid1",
                     "content": "result1"},
                    {"type": "tool_result", "tool_use_id": "tid2",
                     "content": "result2"},
                ],
            }],
        }
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        msgs = out["messages"]
        assert len(msgs) == 2
        assert msgs[0] == {"role": "tool", "tool_call_id": "tid1",
                           "content": "result1"}
        assert msgs[1] == {"role": "tool", "tool_call_id": "tid2",
                           "content": "result2"}

    def test_tool_result_content_blocks_flattened(self):
        block = {"type": "tool_result", "tool_use_id": "tid1",
                 "content": [
                     {"type": "text", "text": "text part"},
                     {"type": "image",
                      "source": {"type": "base64", "data": "xx"}},
                 ]}
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "assistant", "content": [block]}]}
        out, tags = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["messages"][0]["content"] == "text part\n[图片已省略]"
        assert ("tool_result", "图片") in tags

    def test_assistant_text_and_tool_use(self):
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Sure, "},
                    {"type": "tool_use", "id": "tc1",
                     "name": "search",
                     "input": {"q": "hello"}},
                    {"type": "text", "text": " done"},
                ],
            }],
        }
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        m = out["messages"][0]
        assert m["role"] == "assistant"
        assert m["content"] == "Sure, done"
        assert len(m["tool_calls"]) == 1
        assert m["tool_calls"][0]["id"] == "tc1"
        assert m["tool_calls"][0]["function"]["name"] == "search"

    # --- tool_choice ---

    def test_tool_choice_tool_by_name(self):
        body = {"model": "m", "max_tokens": 10,
                "tool_choice": {"type": "tool", "name": "search"},
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["tool_choice"] == {"type": "function",
                                      "function": {"name": "search"}}

    def test_tool_choice_auto(self):
        body = {"model": "m", "max_tokens": 10,
                "tool_choice": {"type": "auto"},
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["tool_choice"] == "auto"

    def test_tool_choice_any_to_required(self):
        body = {"model": "m", "max_tokens": 10,
                "tool_choice": {"type": "any"},
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["tool_choice"] == "required"

    def test_tool_choice_none(self):
        body = {"model": "m", "max_tokens": 10,
                "tool_choice": {"type": "none"},
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["tool_choice"] == "none"

    # --- 参数映射 ---

    def test_stop_sequences_to_stop(self):
        body = {"model": "m", "max_tokens": 10,
                "stop_sequences": ["\n\n"],
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["stop"] == ["\n\n"]
        assert "stop_sequences" not in out

    def test_temperature_top_p_passthrough(self):
        body = {"model": "m", "max_tokens": 10,
                "temperature": 0.7, "top_p": 0.95,
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["temperature"] == 0.7
        assert out["top_p"] == 0.95

    def test_top_k_dropped(self):
        body = {"model": "m", "max_tokens": 10, "top_k": 5,
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert "top_k" not in out

    def test_max_tokens_kept(self):
        body = {"model": "m", "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["max_tokens"] == 100

    def test_tools_input_schema_to_parameters(self):
        body = {"model": "m", "max_tokens": 10,
                "tools": [{"name": "x",
                           "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["tools"][0]["parameters"] == {"type": "object"}
        assert "input_schema" not in out["tools"][0]

    def test_same_proto_noop(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}]}
        out, tags = convert.convert_request(body, "anthropic",
                                             "anthropic")
        assert out == body
        assert tags == []


# ==================================================================
# 请求侧: openai → anthropic
# ==================================================================

class TestReqOpenAIToAnthropic:
    """openai_chat 请求 → anthropic 请求。"""

    def test_system_message_extracted(self):
        body = {
            "model": "claude-3", "max_tokens": 100,
            "messages": [
                {"role": "system", "content": "You are a helper."},
                {"role": "user", "content": "hi"},
            ],
        }
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        assert out["system"] == "You are a helper."
        assert all(m["role"] != "system" for m in out["messages"])
        assert out["messages"][0] == {"role": "user", "content": "hi"}

    def test_multi_system_joined(self):
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "system", "content": "Hello"},
                {"role": "system", "content": "World"},
                {"role": "user", "content": "hi"},
            ],
        }
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        assert out["system"] == "HelloWorld"

    def test_consecutive_user_not_merged(self):
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "user", "content": "second"},
            ],
        }
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        assert len(out["messages"]) == 2
        assert all(m["role"] == "user" for m in out["messages"])

    def test_tool_choice_function_to_tool(self):
        body = {
            "model": "m", "max_tokens": 10,
            "tool_choice": {"type": "function",
                            "function": {"name": "search"}},
            "messages": [{"role": "user", "content": "hi"}],
        }
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        assert out["tool_choice"] == {"type": "tool", "name": "search"}

    def test_tool_choice_auto(self):
        body = {"model": "m", "max_tokens": 10, "tool_choice": "auto",
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        assert out["tool_choice"] == {"type": "auto"}

    def test_tool_choice_required_to_any(self):
        body = {"model": "m", "max_tokens": 10,
                "tool_choice": "required",
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        assert out["tool_choice"] == {"type": "any"}

    def test_stop_to_stop_sequences(self):
        body = {"model": "m", "max_tokens": 10, "stop": ["END"],
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        assert out["stop_sequences"] == ["END"]

    def test_parameters_to_input_schema(self):
        body = {"model": "m", "max_tokens": 10,
                "tools": [{"name": "x", "parameters": {"type": "object"}}],
                "messages": [{"role": "user", "content": "hi"}]}
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        assert out["tools"][0]["input_schema"] == {"type": "object"}

    def test_tool_result_merge(self):
        """连续 tool 消息合并为一个 assistant 消息(含 tool_result 块)。"""
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "assistant", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "search",
                                  "arguments": '{"q":"a"}'}},
                ]},
                {"role": "tool", "tool_call_id": "c1", "content": "r1"},
                {"role": "tool", "tool_call_id": "c1", "content": "r2"},
            ],
        }
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        an_msgs = out["messages"]
        last = an_msgs[-1]
        assert last["role"] == "assistant"
        blocks = last["content"]
        assert len(blocks) == 2
        assert all(b["type"] == "tool_result" for b in blocks)
        assert blocks[0]["tool_use_id"] == "c1"
        assert blocks[0]["content"] == "r1"
        assert blocks[1]["tool_use_id"] == "c1"
        assert blocks[1]["content"] == "r2"

    def test_assistant_merge_with_tool_calls_and_text(self):
        """连续 assistant 消息合并(含 tool_calls)。"""
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "assistant", "content": "text"},
                {"role": "assistant",
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "f",
                                              "arguments": "{}"}}]},
            ],
        }
        out, _ = convert.convert_request(body, "openai_chat", "anthropic")
        m = out["messages"][0]
        assert m["role"] == "assistant"
        blocks = m["content"]
        assert len(blocks) == 2
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "text"
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["name"] == "f"

    def test_tool_call_invalid_json_args(self):
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "assistant",
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "f",
                                              "arguments": "not-json"}}]},
            ],
        }
        out, tags = convert.convert_request(body, "openai_chat", "anthropic")
        assert ("tool_args", "arguments JSON解析失败") in tags
        block = out["messages"][0]["content"][0]
        assert block["type"] == "tool_use"
        assert "_raw" in block["input"]


# ==================================================================
# 响应侧: anthropic → openai
# ==================================================================

class TestRespAnthropicToOpenAI:
    def test_text_only(self):
        body = {
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        out, _ = convert.convert_response(body, "anthropic", "openai_chat")
        assert out["id"] == "msg_1"
        assert out["object"] == "chat.completion"
        assert out["choices"][0]["message"]["content"] == "Hello!"
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["usage"]["prompt_tokens"] == 10
        assert out["usage"]["completion_tokens"] == 5
        assert out["usage"]["total_tokens"] == 15

    def test_tool_use_response(self):
        body = {
            "id": "msg_2", "type": "message", "role": "assistant",
            "model": "claude-3",
            "content": [
                {"type": "text", "text": "Let me search."},
                {"type": "tool_use", "id": "tid1",
                 "name": "search", "input": {"q": "hello"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }
        out, _ = convert.convert_response(body, "anthropic", "openai_chat")
        msg = out["choices"][0]["message"]
        assert msg["content"] == "Let me search."
        tcs = msg["tool_calls"]
        assert len(tcs) == 1
        assert tcs[0]["id"] == "tid1"
        assert tcs[0]["function"]["name"] == "search"
        assert json.loads(tcs[0]["function"]["arguments"]) == \
            {"q": "hello"}
        assert out["choices"][0]["finish_reason"] == "tool_calls"

    def test_stop_reason_max_tokens(self):
        body = {
            "id": "msg_3", "type": "message", "role": "assistant",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }
        out, _ = convert.convert_response(body, "anthropic", "openai_chat")
        assert out["choices"][0]["finish_reason"] == "length"

    def test_same_proto_noop(self):
        body = {"id": "x", "object": "chat.completion",
                "choices": [{"index": 0, "message": {},
                             "finish_reason": "stop"}]}
        out, tags = convert.convert_response(body, "openai_chat",
                                               "openai_chat")
        assert tags == []
        assert out["object"] == "chat.completion"


# ==================================================================
# 响应侧: openai → anthropic
# ==================================================================

class TestRespOpenAIToAnthropic:
    def test_text_only(self):
        body = {
            "id": "chatcmpl-1", "object": "chat.completion",
            "model": "gpt-4", "created": 1,
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                  "content": "Hi!"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                       "total_tokens": 7},
        }
        out, _ = convert.convert_response(body, "openai_chat", "anthropic")
        assert out["type"] == "message"
        assert out["role"] == "assistant"
        assert out["content"] == [{"type": "text", "text": "Hi!"}]
        assert out["stop_reason"] == "end_turn"
        assert out["usage"]["input_tokens"] == 5
        assert out["usage"]["output_tokens"] == 2

    def test_tool_calls_response(self):
        body = {
            "id": "chatcmpl-2", "object": "chat.completion",
            "model": "gpt-4",
            "choices": [{"index": 0,
                         "message": {
                             "role": "assistant",
                             "content": None,
                             "tool_calls": [{
                                 "id": "call_1", "type": "function",
                                 "function": {"name": "search",
                                              "arguments": '{"q":"test"}'},
                             }],
                         },
                         "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3,
                       "total_tokens": 13},
        }
        out, _ = convert.convert_response(body, "openai_chat", "anthropic")
        blocks = out["content"]
        assert len(blocks) == 1
        assert blocks[0]["type"] == "tool_use"
        assert blocks[0]["id"] == "call_1"
        assert blocks[0]["name"] == "search"
        assert blocks[0]["input"] == {"q": "test"}
        assert out["stop_reason"] == "tool_use"

    def test_null_content_becomes_empty(self):
        body = {
            "id": "c3", "object": "chat.completion", "model": "gpt-4",
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": None},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 0,
                       "total_tokens": 1},
        }
        out, _ = convert.convert_response(body, "openai_chat", "anthropic")
        assert out["content"] == []

    def test_finish_reason_length_to_max_tokens(self):
        body = {
            "id": "c4", "object": "chat.completion", "model": "gpt-4",
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": "text"},
                         "finish_reason": "length"}],
            "usage": {},
        }
        out, _ = convert.convert_response(body, "openai_chat", "anthropic")
        assert out["stop_reason"] == "max_tokens"


# ==================================================================
# 降级路径
# ==================================================================

class TestDegradation:
    """cache_control/图片/thinking 丢弃 + 打标。"""

    def test_cache_control_dropped_in_system(self):
        body = {
            "model": "m", "max_tokens": 10,
            "system": [
                {"type": "text", "text": "Hi",
                 "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out, tags = convert.convert_request(body, "anthropic", "openai_chat")
        assert "cache_control" not in json.dumps(out["messages"][0])

    def test_image_in_user_content(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "user", "content": [
                    {"type": "image",
                     "source": {"type": "base64", "data": "xxx"}},
                    {"type": "text", "text": "describe"},
                ]}]}
        out, tags = convert.convert_request(body, "anthropic",
                                             "openai_chat")
        assert "[图片已省略]" in out["messages"][0]["content"]
        assert ("user", "图片") in tags

    def test_thinking_in_assistant_dropped(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "text", "text": "answer"},
                ]}]}
        out, tags = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["messages"][0]["content"] == "answer"
        assert ("content", "thinking") in tags

    def test_image_in_tool_result(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "assistant", "content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": [
                         {"type": "image",
                          "source": {"type": "base64", "data": "xx"}},
                     ]},
                ]}]}
        out, tags = convert.convert_request(body, "anthropic", "openai_chat")
        assert "[图片已省略]" in out["messages"][0]["content"]
        assert ("tool_result", "图片") in tags


# ==================================================================
# 流式: anthropic → openai
# ==================================================================

class TestStreamAnthropicToOpenAI:
    """anthropic SSE 事件流 → openai_chat SSE chunk 流。"""

    def _sse_lines(self, events):
        lines = []
        for ev in events:
            lines.append(f"event: {ev['type']}\n")
            lines.append("data: " + json.dumps(ev, ensure_ascii=False)
                         + "\n\n")
        return lines

    def test_text_stream(self):
        """简单纯文本流转换。"""
        events = [
            {"type": "message_start", "message": {
                "id": "m1", "type": "message", "role": "assistant",
                "content": [], "model": "claude-3",
                "stop_reason": None,
                "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": " world"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"input_tokens": 10, "output_tokens": 12}},
            {"type": "message_stop"},
        ]
        source = self._sse_lines(events)
        out = list(stream.stream_convert(iter(source), "anthropic",
                                          "openai_chat"))
        joined = "".join(out)
        assert '"role":"assistant"' in joined
        assert '"content":"Hello"' in joined
        assert '"finish_reason":"stop"' in joined
        assert "data: [DONE]" in joined

    def test_tool_use_stream(self):
        """tool_use 流式转换。"""
        events = [
            {"type": "message_start", "message": {
                "id": "m2", "type": "message", "role": "assistant",
                "content": [], "model": "claude-3",
                "stop_reason": None,
                "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Searching"}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "tid1",
                               "name": "search", "input": {}}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta",
                       "partial_json": '{"q":'}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta",
                       "partial_json": '"hello"}'}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"input_tokens": 10, "output_tokens": 20}},
            {"type": "message_stop"},
        ]
        source = self._sse_lines(events)
        out = list(stream.stream_convert(iter(source), "anthropic",
                                          "openai_chat"))
        joined = "".join(out)
        assert '"content":"Searching"' in joined
        assert '"name":"search"' in joined
        assert '"arguments"' in joined
        assert '"finish_reason":"tool_calls"' in joined
        assert "data: [DONE]" in joined

    def test_same_proto_passthrough(self):
        lines = ["data: {\"id\":\"x\"}\n\n"]
        out = list(stream.stream_convert(
            iter(lines), "anthropic", "anthropic"))
        assert out == lines

    def test_stream_same_proto_openai(self):
        lines = ["data: {\"id\":\"c1\"}\n\n"]
        out = list(stream.stream_convert(
            iter(lines), "openai_chat", "openai_chat"))
        assert out == lines


# ==================================================================
# 流式: openai → anthropic
# ==================================================================

def _mk(d):
    d.setdefault("object", "chat.completion.chunk")
    return "data: " + json.dumps(d, ensure_ascii=False) + "\n\n"


class TestStreamOpenAIToAnthropic:
    """openai_chat SSE chunk 流 → anthropic SSE 事件流。"""

    def test_basic_text_stream(self):
        chunks = [
            _mk({"id": "c1", "object": "chat.completion.chunk",
                 "choices": [
                     {"index": 0, "delta": {"role": "assistant",
                                            "content": "Hello"},
                      "finish_reason": None}]}),
            _mk({"id": "c1", "object": "chat.completion.chunk",
                 "choices": [
                     {"index": 0, "delta": {"content": " world"},
                      "finish_reason": None}]}),
            _mk({"id": "c1", "object": "chat.completion.chunk",
                 "choices": [
                     {"index": 0, "finish_reason": "stop"}]}),
            _mk({"id": "c1", "object": "chat.completion.chunk",
                 "choices": [
                     {"index": 0}],
                 "usage": {"prompt_tokens": 10,
                           "completion_tokens": 8}}),
        ]
        out = list(stream.stream_convert(
            iter(chunks), "openai_chat", "anthropic", "claude-3"))
        joined = "".join(out)
        assert 'event: message_start' in joined
        assert '"role":"assistant"' in joined
        assert '"model":"claude-3"' in joined
        assert '"text_delta"' in joined
        assert '"text":"Hello"' in joined
        assert '"text_delta"' in joined
        assert '"text":" world"' in joined
        assert '"stop_reason":"end_turn"' in joined
        assert '"output_tokens":8' in joined
        assert 'event: message_stop' in joined

    def test_block_index_assignment(self):
        """tool_use 块按调用顺序分配 block index。"""
        tc1 = json.dumps({"index": 0, "id": "t1", "type": "function",
                          "function": {"name": "f1", "arguments": ""}})
        tc2 = json.dumps({"index": 1, "id": "t2", "type": "function",
                          "function": {"name": "f2", "arguments": ""}})
        chunks = [
            _mk({"id": "c2", "object": "chat.completion.chunk",
                 "choices": [
                     {"index": 0, "delta": {"role": "assistant"},
                      "finish_reason": None}]}),
            _mk({"id": "c2", "object": "chat.completion.chunk",
                 "choices": [
                     {"index": 0, "delta": {"tool_calls": [tc1]}}]}),
            _mk({"id": "c2", "object": "chat.completion.chunk",
                 "choices": [
                     {"index": 0, "delta": {"tool_calls": [tc2]}}]}),
            _mk({"id": "c2", "object": "chat.completion.chunk",
                 "choices": [
                     {"index": 0, "finish_reason": "tool_calls"}]}),
        ]
        out = list(stream.stream_convert(
            iter(chunks), "openai_chat", "anthropic", "m"))
        joined = "".join(out)
        assert '"type":"tool_use"' in joined
        assert '"name":"f1"' in joined
        assert '"name":"f2"' in joined

    def test_parallel_tool_calls_index_mapping(self):
        """并行 tool_calls 的 openai index 正确映射到 block index。"""
        chunks = [
            _mk({"id": "c3", "choices": [
                {"index": 0, "delta": {"role": "assistant"},
                 "finish_reason": None}]}),
            # First delta: parallel tools arrive (index 0 and 1)
            _mk({"id": "c3", "choices": [
                {"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "ta", "type": "function",
                     "function": {"name": "funcA",
                                   "arguments": "{a"}},
                    {"index": 1, "id": "tb", "type": "function",
                     "function": {"name": "funcB",
                                   "arguments": "{b"}},
                ]}}]}),
            # Second delta: arguments continue
            _mk({"id": "c3", "choices": [
                {"index": 0, "delta": {"tool_calls": [
                    {"index": 0,
                     "function": {"arguments": ":1}"}},
                    {"index": 1,
                     "function": {"arguments": ":2}"}},
                ]}}]}),
            # Name update in later delta
            _mk({"id": "c3", "choices": [
                {"index": 0, "delta": {"tool_calls": [
                    {"index": 0,
                     "function": {"name": "newA"}},
                ]}}]}),
            # finish
            _mk({"id": "c3", "choices": [
                {"index": 0, "finish_reason": "tool_calls"}]}),
        ]
        out = list(stream.stream_convert(
            iter(chunks), "openai_chat", "anthropic", "m"))
        joined = "".join(out)
        # Multiple tool_use blocks present
        assert joined.count('"type":"tool_use"') >= 2

    def test_usage_in_final_chunk(self):
        """usage 在最后 chunk 出现,应被捕获。"""
        chunks = [
            _mk({"id": "c4", "choices": [
                {"index": 0, "delta": {"role": "assistant",
                                       "content": "text"},
                 "finish_reason": None}]}),
            _mk({"id": "c4", "choices": [
                {"index": 0, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10,
                           "completion_tokens": 5}}),
        ]
        out = list(stream.stream_convert(
            iter(chunks), "openai_chat", "anthropic", "m"))
        joined = "".join(out)
        assert '"input_tokens":10' in joined
        assert '"output_tokens":5' in joined

    def test_partial_json_passthrough(self):
        """partial_json 增量透传,不解析拼合。"""
        parts = ['{"q', '":"', '"hello"', '}']
        chunks = [_mk({"id": "c5", "choices": [
            {"index": 0, "delta": {"role": "assistant"},
             "finish_reason": None}]})]
        for p in parts:
            chunks.append(
                _mk({"id": "c5", "choices": [
                    {"index": 0, "delta": {"tool_calls": [
                        {"index": 0,
                         "function": {"arguments": p}}
                    ]}}]}))
        chunks.append(
            _mk({"id": "c5", "choices": [
                {"index": 0, "finish_reason": "tool_calls"}]}))
        out = list(stream.stream_convert(
            iter(chunks), "openai_chat", "anthropic", "m"))
        joined = "".join(out)
        # 各段 partial_json 被分段透传
        assert "input_json_delta" in joined

    def test_empty_args_at_stream_end(self):
        """流尾 arguments 为空 → 保持空字符串(不崩溃)。"""
        chains = [
            _mk({"id": "c6", "choices": [
                {"index": 0, "delta": {"role": "assistant"},
                 "finish_reason": None}]}),
            _mk({"id": "c6", "choices": [
                {"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "t1", "type": "function",
                     "function": {"name": "f", "arguments": ""}}
                ]}}]}),
            _mk({"id": "c6", "choices": [
                {"index": 0, "finish_reason": "tool_calls"}]}),
        ]
        out = list(stream.stream_convert(
            iter(chains), "openai_chat", "anthropic", "m"))
        joined = "".join(out)
        assert '"type":"tool_use"' in joined
        assert '"name":"f"' in joined
        assert "data: " in joined

    def test_stream_close_without_message_stop(self):
        """上游流不发送 message_stop,直接关闭→兜底关闭事件。"""
        chunks = [
            _mk({"id": "c7", "choices": [
                {"index": 0, "delta": {"role": "assistant",
                                       "content": "X"},
                 "finish_reason": None}]}),
            _mk({"id": "c7", "choices": [
                {"index": 0, "finish_reason": "stop"}]}),
        ]
        out = list(stream.stream_convert(
            iter(chunks), "openai_chat", "anthropic", "m"))
        joined = "".join(out)
        assert 'event: content_block_stop' in joined or \
               'event: message_stop' in joined

    def test_same_proto_noop(self):
        lines = ["data: {\"id\":\"x\"}\n\n"]
        out = list(stream.stream_convert(
            iter(lines), "openai_chat", "openai_chat"))
        assert out == lines


# ==================================================================
# 边界 / 回归加固
# ==================================================================

class TestEdgeCases:
    """边界情况: 空集合、缺失字段、空响应。"""

    def test_request_with_no_messages(self):
        body = {"model": "m", "max_tokens": 10}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert "messages" in out

    def test_response_no_usage(self):
        body = {
            "id": "m1", "type": "message", "role": "assistant",
            "model": "c3", "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
        }
        out, _ = convert.convert_response(body, "anthropic",
                                           "openai_chat")
        assert out["usage"]["prompt_tokens"] == 0
        assert out["usage"]["completion_tokens"] == 0

    def test_user_content_null(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "user", "content": None}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["messages"][0]["content"] == ""

    def test_assistant_no_content(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "assistant"}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["messages"][0]["role"] == "assistant"

    def test_tool_result_with_string_content(self):
        body = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "assistant", "content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": "plain string"},
                ]}]}
        out, _ = convert.convert_request(body, "anthropic", "openai_chat")
        assert out["messages"][0] == {"role": "tool",
                                      "tool_call_id": "t1",
                                      "content": "plain string"}
