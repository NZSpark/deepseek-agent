"""路由层回归测试（FastAPI 应用），不依赖 httpx。

当前环境没有 httpx，装不了 starlette 的 TestClient，因此这里直接 await 路由函数，
并用假 driver 替换 ``deepseek_web.server.driver`` 单例 —— 覆盖的是路由本身的逻辑：
参数校验、错误映射、会话桶透传、流式分支与 /session/reset。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import inspect
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv  # noqa: E402
from deepseek_web import config, server  # noqa: E402
from deepseek_web.driver import DeepSeekContextLimitError, DeepSeekTimeoutError  # noqa: E402

TOOL_REPLY = '```tool_call\n{"name": "bash", "arguments": {"command": "ls"}}\n```'


class FakeNode:
    def __init__(self, text, cls="ds-markdown"):
        self._text = text
        self._cls = cls

    async def inner_text(self):
        return self._text

    async def get_attribute(self, name):
        return self._cls if name == "class" else None


class FakePage:
    def __init__(self, texts=("机密正文：用户的完整对话内容",)):
        self.texts = list(texts)

    async def query_selector_all(self, selector):
        return [FakeNode(t) for t in self.texts]


class FakeDriver:
    """只实现路由会用到的那部分 driver 接口。"""

    def __init__(self, reply="完成", error=None, browser_ready=True):
        self.page = FakePage() if browser_ready else None
        self.init_error = None if browser_ready else "登录页未就绪"
        self.reply = reply
        self.error = error
        self.chats = []
        self.seed_queries = []
        self.resets = []
        self.saved = []

    def _current_session_url(self):
        return "https://chat.deepseek.com/a/chat/s/483878e7-7179-4e63-a19b-365ad11a686e"

    def needs_seed(self, key=None):
        self.seed_queries.append(key)
        return False

    async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None):
        self.chats.append({"prompt": prompt, "key": key, "seeded": seeded_prompt})
        if self.error is not None:
            raise self.error
        return self.reply, []

    def session_keys(self):
        return ["default"]

    async def _page_is_generating(self, key=None):
        return False

    async def debug_stop_candidates(self, key=None):
        return []

    def reset_session(self, key=None):
        self.resets.append(key)

    def session_stats(self, key=None):
        return {"url": None, "buckets": ["default", key or "default"], "asked": key}

    def save_extracted_files(self, raw_text, code_blocks, output_dir):
        self.saved.append((raw_text, code_blocks, output_dir))
        return ["/tmp/fake.md"]


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDriver()
        patch = unittest.mock.patch.object(server, "driver", self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def request(**extra):
        payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "改一下 README"}],
        }
        payload.update(extra)
        return srv.ChatCompletionRequest(**payload)

    @staticmethod
    def body(response):
        """JSONResponse -> dict；pydantic 响应 -> dict。"""
        if hasattr(response, "body"):
            return json.loads(response.body)
        return response.model_dump()

    def call(self, request, header=None):
        return asyncio.run(server.chat_completions(request, header))


class ValidationTests(RouteTestCase):
    def test_empty_messages_is_a_400(self):
        response = self.call(self.request(messages=[]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"]["type"], "invalid_request_error")

    def test_unavailable_when_browser_not_ready(self):
        self.fake.page = None
        self.fake.init_error = "profile 被占用"
        response = self.call(self.request())
        self.assertEqual(response.status_code, 503)
        self.assertIn("profile 被占用", self.body(response)["error"]["message"])

    def test_system_only_messages_do_not_crash(self):
        # 注意：只发 system 消息不会被拒绝（build_prompt 的播种分支总会产出内容），
        # 这里把当前行为钉住，避免以后误以为它在走 400 分支。
        payload = self.body(self.call(self.request(messages=[{"role": "system", "content": "x"}])))
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")


class EmptyPromptTests(RouteTestCase):
    def test_empty_effective_prompt_is_a_400(self):
        # 真正要发出去的那份 prompt 为空时必须立刻报错，而不是发一条空消息
        # （空输入会让网页版什么都不做，客户端只能等到超时）
        with unittest.mock.patch.object(server, "build_prompt", return_value=""):
            response = self.call(self.request())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"]["type"], "invalid_request_error")
        self.assertEqual(self.fake.chats, [])

    def test_non_empty_prompt_still_reaches_the_driver(self):
        self.assertEqual(self.call(self.request()).choices[0].finish_reason, "stop")
        self.assertEqual(len(self.fake.chats), 1)


class DebugDomTests(RouteTestCase):
    def test_hidden_by_default(self):
        with unittest.mock.patch.object(config, "DEBUG", False):
            with self.assertRaises(server.HTTPException) as ctx:
                asyncio.run(server.debug_dom())
        self.assertEqual(ctx.exception.status_code, 404)

    def test_enabled_under_debug(self):
        with unittest.mock.patch.object(config, "DEBUG", True):
            response = asyncio.run(server.debug_dom())
        payload = response
        self.assertEqual(payload["response_node_count"], 1)
        self.assertIn("sha1", payload["last_node"])
        self.assertEqual(payload["last_node"]["text_length"], len("机密正文：用户的完整对话内容"))

    def test_never_echoes_message_bodies(self):
        with unittest.mock.patch.object(config, "DEBUG", True):
            payload = asyncio.run(server.debug_dom())
        self.assertNotIn("head", payload["last_node"])
        self.assertNotIn("tail", payload["last_node"])
        self.assertNotIn("head", payload["nodes"][0])
        # 整份响应里不得出现正文片段
        self.assertNotIn("机密正文", json.dumps(payload, ensure_ascii=False))


class ErrorMappingTests(RouteTestCase):
    def test_context_limit_maps_to_400(self):
        self.fake.error = DeepSeekContextLimitError("到顶了")
        response = self.call(self.request())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"]["type"], "context_length_exceeded")

    def test_timeout_maps_to_504(self):
        self.fake.error = DeepSeekTimeoutError("超时")
        response = self.call(self.request())
        self.assertEqual(response.status_code, 504)
        self.assertEqual(self.body(response)["error"]["type"], "timeout")

    def test_unexpected_error_maps_to_502(self):
        self.fake.error = RuntimeError("找不到输入框")
        response = self.call(self.request())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.body(response)["error"]["type"], "upstream_error")


class SessionKeyTests(RouteTestCase):
    def test_header_key_is_forwarded_to_driver(self):
        self.call(self.request(), "pi-task-1")
        self.assertEqual(self.fake.chats[-1]["key"], "pi-task-1")
        self.assertEqual(self.fake.seed_queries[-1], "pi-task-1")

    def test_user_field_is_used_when_header_is_absent(self):
        self.call(self.request(user="pi-task-2"))
        self.assertEqual(self.fake.chats[-1]["key"], "pi-task-2")

    def test_absent_key_keeps_the_legacy_shared_session(self):
        self.call(self.request())
        self.assertIsNone(self.fake.chats[-1]["key"])

    def test_scoping_can_be_turned_off(self):
        with unittest.mock.patch.object(config, "SESSION_SCOPING", False):
            self.call(self.request(), "pi-task-3")
        self.assertIsNone(self.fake.chats[-1]["key"])

    def test_route_declares_the_configurable_header(self):
        param = inspect.signature(server.chat_completions).parameters["x_deepseek_session"]
        self.assertEqual(param.default.alias, config.SESSION_KEY_HEADER)


class CompletionShapeTests(RouteTestCase):
    def test_plain_reply_has_stop_finish_reason(self):
        response = self.call(self.request())
        payload = self.body(response)
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(payload["choices"][0]["message"]["content"], "完成")
        self.assertGreater(payload["usage"]["total_tokens"], 0)

    def test_tool_reply_becomes_tool_calls(self):
        self.fake.reply = TOOL_REPLY
        request = self.request(tools=[{
            "type": "function",
            "function": {"name": "bash", "parameters": {"type": "object"}},
        }])
        payload = self.body(self.call(request))
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "bash")
        self.assertIsNone(choice["message"]["content"])

    def test_delta_prompt_only_carries_new_messages(self):
        self.call(self.request(messages=[
            {"role": "system", "content": "你是助手"},
            {"role": "assistant", "content": "上一轮"},
            {"role": "user", "content": "这一轮"},
        ]))
        self.assertEqual(self.fake.chats[-1]["prompt"], "这一轮")


class StreamingRouteTests(RouteTestCase):
    def drain(self, response):
        async def consume():
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return chunks

        return asyncio.run(consume())

    def test_stream_returns_sse_and_forwards_key(self):
        response = self.call(self.request(stream=True), "pi-task-4")
        chunks = self.drain(response)
        text = "".join(chunks)
        self.assertIn("data: [DONE]", text)
        self.assertIn('"finish_reason": "stop"', text)
        self.assertEqual(self.fake.chats[-1]["key"], "pi-task-4")

    def test_stream_media_type_and_headers(self):
        response = self.call(self.request(stream=True))
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        self.drain(response)


class ResetRouteTests(RouteTestCase):
    def test_reset_without_argument_resets_the_default_bucket(self):
        response = asyncio.run(server.reset_session(None))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fake.resets, [None])
        self.assertEqual(json.loads(response.body)["session"], "default")

    def test_reset_targets_the_named_bucket(self):
        response = asyncio.run(server.reset_session("pi-task-5"))
        self.assertEqual(self.fake.resets, ["pi-task-5"])
        self.assertEqual(json.loads(response.body)["session"], "pi-task-5")

    def test_blank_argument_is_treated_as_the_default_bucket(self):
        response = asyncio.run(server.reset_session("  "))
        self.assertEqual(self.fake.resets, [None])
        self.assertEqual(json.loads(response.body)["session"], "default")


class HealthzTests(RouteTestCase):
    def test_healthz_reports_buckets_and_scoping(self):
        response = asyncio.run(server.healthz())
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["browser_ready"])
        self.assertEqual(payload["session_keys"], ["default"])
        self.assertTrue(payload["session_scoping"])

    def test_healthz_degrades_when_browser_is_missing(self):
        self.fake.page = None
        response = asyncio.run(server.healthz())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
