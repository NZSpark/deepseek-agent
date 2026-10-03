"""Responses API 兼容层回归测试（Codex CLI 专用 /v1/responses）。

不依赖 httpx：直接 await responses.handle_responses / stream_responses，
用假 driver 替换。覆盖 §6 测试计划中 P1-P3 的用例（图片 T3.5 未实现，暂不覆盖）。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepseek_web import responses  # noqa: E402
from deepseek_web.driver import (  # noqa: E402
    DeepSeekBusyError,
    DeepSeekContextLimitError,
    DeepSeekTimeoutError,
)

TOOL_REPLY = '```tool_call\n{"name": "bash", "arguments": {"command": "ls"}}\n```'


class FakeDriver:
    """只实现 responses 路径会用到的那部分 driver 接口。"""

    def __init__(self, reply="完成", error=None, browser_ready=True):
        self.page = object() if browser_ready else None
        self.reply = reply
        self.error = error
        self.last_prompt = None
        self.chats = []

    def needs_seed(self, key=None):
        return False

    async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None):
        self.chats.append({"prompt": prompt, "key": key, "seeded": seeded_prompt})
        self.last_prompt = prompt
        if self.error is not None:
            raise self.error
        if on_delta is not None:
            for i in range(0, len(self.reply), 2):
                await on_delta(self.reply[i:i + 2])
        return self.reply, []

    def sent_prompt(self, key=None):
        return self.last_prompt

    def save_extracted_files(self, raw_text, code_blocks, output_dir):
        return []


def make_request(**extra):
    payload = {"model": "deepseek-chat", "input": "hi"}
    payload.update(extra)
    return responses.ResponsesRequest(**payload)


def body(response):
    return json.loads(response.body)


def run(coro):
    return asyncio.run(coro)


class RequestMappingTests(unittest.TestCase):
    def test_string_input_maps_to_user_message(self):
        chat = responses.to_chat_request(make_request(input="hi"))
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(chat.messages[0].role, "user")
        self.assertEqual(chat.messages[0].content, "hi")

    def test_instructions_become_system_message(self):
        chat = responses.to_chat_request(make_request(input="hi", instructions="你是助手"))
        self.assertEqual(chat.messages[0].role, "system")
        self.assertEqual(chat.messages[0].content, "你是助手")
        self.assertEqual(chat.messages[1].role, "user")

    def test_input_item_array_conversion(self):
        chat = responses.to_chat_request(make_request(input=[
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "跑一下"}]},
            {"type": "function_call", "call_id": "c1", "name": "bash",
             "arguments": '{"command": "ls"}'},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]))
        roles = [m.role for m in chat.messages]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        self.assertEqual(chat.messages[0].content, "跑一下")
        self.assertEqual(chat.messages[1].tool_calls[0].function.name, "bash")
        self.assertEqual(chat.messages[2].tool_call_id, "c1")
        self.assertEqual(chat.messages[2].content, "ok")

    def test_unknown_item_is_skipped(self):
        chat = responses.to_chat_request(make_request(input=[
            {"type": "weird_thing", "x": 1},
            {"type": "message", "role": "user", "content": "ok"},
        ]))
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(chat.messages[0].content, "ok")

    def test_tools_are_wrapped(self):
        chat = responses.to_chat_request(make_request(input="hi", tools=[
            {"type": "function", "name": "bash",
             "description": "run", "parameters": {"type": "object"}, "strict": True},
        ]))
        self.assertEqual(chat.tools[0]["type"], "function")
        self.assertEqual(chat.tools[0]["function"]["name"], "bash")
        self.assertNotIn("strict", chat.tools[0]["function"])

    def test_max_output_tokens_maps_to_max_tokens(self):
        chat = responses.to_chat_request(make_request(input="hi", max_output_tokens=123))
        self.assertEqual(chat.max_tokens, 123)


class NonStreamTests(unittest.TestCase):
    def test_nonstream_response_shape(self):
        result = responses.from_chat_response("你好", "deepseek-chat", 10)
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"][0]["type"], "message")
        self.assertEqual(result["output"][0]["content"][0]["type"], "output_text")
        self.assertEqual(result["output"][0]["content"][0]["text"], "你好")
        self.assertIn("input_tokens", result["usage"])
        self.assertIn("output_tokens", result["usage"])
        self.assertIn("total_tokens", result["usage"])

    def test_nonstream_tool_call_output(self):
        result = responses.from_chat_response(
            TOOL_REPLY, "deepseek-chat", 10,
            tool_calls=[{"name": "bash", "arguments": {"command": "ls"}}],
        )
        self.assertEqual(result["output"][0]["type"], "function_call")
        self.assertEqual(result["output"][0]["name"], "bash")
        self.assertEqual(json.loads(result["output"][0]["arguments"])["command"], "ls")

    def test_handle_responses_nonstream(self):
        driver = FakeDriver(reply="OK")
        resp = run(responses.handle_responses(make_request(input="hi"), None, driver))
        data = resp if isinstance(resp, dict) else body(resp)
        self.assertEqual(data["output"][0]["content"][0]["text"], "OK")

    def test_handle_responses_dsml_reply_becomes_function_call(self):
        """DeepSeek 退回 DSML invoke 格式时，Codex 也必须收到 function_call。"""
        dsml = (
            '<｜｜DSML｜｜ calls>\n'
            '<｜｜DSML｜｜ invoke name="exec_command">\n'
            '<｜｜DSML｜｜ parameter name="cmd" string="true">ls -la</｜｜DSML｜｜ parameter>\n'
            '</｜｜DSML｜｜ invoke>\n'
            '</｜｜DSML｜｜ calls>'
        )
        driver = FakeDriver(reply=dsml)
        req = make_request(input="hi", tools=[
            {"type": "function", "name": "exec_command",
             "parameters": {"type": "object"}},
        ])
        resp = run(responses.handle_responses(req, None, driver))
        data = resp if isinstance(resp, dict) else body(resp)
        self.assertEqual(data["output"][0]["type"], "function_call")
        self.assertEqual(data["output"][0]["name"], "exec_command")
        self.assertEqual(json.loads(data["output"][0]["arguments"]), {"cmd": "ls -la"})

    def test_handle_responses_browser_not_ready(self):
        driver = FakeDriver(browser_ready=False)
        resp = run(responses.handle_responses(make_request(input="hi"), None, driver))
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(body(resp)["error"]["type"], "upstream_error")

    def test_handle_responses_empty_input(self):
        driver = FakeDriver()
        resp = run(responses.handle_responses(make_request(input=[]), None, driver))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body(resp)["error"]["type"], "invalid_request_error")


class ErrorMappingTests(unittest.TestCase):
    def test_error_mapping(self):
        cases = [
            (DeepSeekContextLimitError("too long"), 400, "context_length_exceeded"),
            (DeepSeekBusyError("busy"), 503, "upstream_busy"),
            (DeepSeekTimeoutError("timeout"), 504, "timeout"),
            (RuntimeError("browser"), 502, "upstream_error"),
            (ValueError("bad"), 400, "invalid_request_error"),
        ]
        for exc, status, err_type in cases:
            driver = FakeDriver(error=exc)
            resp = run(responses.handle_responses(make_request(input="hi"), None, driver))
            self.assertEqual(resp.status_code, status, msg=repr(exc))
            self.assertEqual(body(resp)["error"]["type"], err_type, msg=repr(exc))


class StreamTests(unittest.TestCase):
    def collect(self, driver, req=None):
        req = req or make_request(input="hi", stream=True)
        chat = responses.to_chat_request(req)

        async def go():
            events = []
            async for chunk in responses.stream_responses(chat, driver, None):
                events.append(chunk)
            return events

        return run(go())

    @staticmethod
    def parse(events):
        parsed = []
        for chunk in events:
            if chunk.startswith(":"):
                continue
            lines = chunk.strip().splitlines()
            ev = next(l[7:] for l in lines if l.startswith("event: "))
            data = json.loads(next(l[6:] for l in lines if l.startswith("data: ")))
            parsed.append((ev, data))
        return parsed

    def test_stream_has_response_created_and_completed(self):
        parsed = self.parse(self.collect(FakeDriver(reply="你好世界")))
        names = [e for e, _ in parsed]
        self.assertEqual(names[0], "response.created")
        self.assertEqual(names[-1], "response.completed")
        completed = parsed[-1][1]["response"]
        text = completed["output"][0]["content"][0]["text"]
        self.assertEqual(text, "你好世界")

    def test_stream_event_sequence(self):
        parsed = self.parse(self.collect(FakeDriver(reply="abc")))
        for ev, data in parsed:
            self.assertEqual(data["type"], ev)
        seqs = [data["sequence_number"] for _, data in parsed]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))
        names = [e for e, _ in parsed]
        self.assertIn("response.output_item.added", names)
        self.assertIn("response.content_part.added", names)
        self.assertIn("response.output_text.delta", names)
        self.assertIn("response.output_text.done", names)
        self.assertIn("response.output_item.done", names)

    def test_tool_call_stream_events(self):
        parsed = self.parse(self.collect(FakeDriver(reply=TOOL_REPLY),
                                         make_request(input="hi", stream=True, tools=[
                                             {"type": "function", "name": "bash",
                                              "parameters": {"type": "object"}},
                                         ])))
        names = [e for e, _ in parsed]
        self.assertIn("response.function_call_arguments.delta", names)
        self.assertIn("response.function_call_arguments.done", names)
        completed = parsed[-1][1]["response"]
        self.assertEqual(completed["output"][0]["type"], "function_call")
        self.assertEqual(completed["output"][0]["name"], "bash")

    def test_stream_failed_on_error(self):
        parsed = self.parse(self.collect(FakeDriver(error=DeepSeekTimeoutError("t"))))
        self.assertEqual(parsed[-1][0], "response.failed")
        self.assertEqual(parsed[-1][1]["response"]["error"]["type"], "timeout")


if __name__ == "__main__":
    unittest.main()
