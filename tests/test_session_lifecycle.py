"""会话生命周期回归测试：播种、到顶检测、状态持久化、重试阶梯。

全部用假 page 驱动，不需要浏览器。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv  # noqa: E402
from deepseek_web import config  # noqa: E402
from deepseek_web.driver import DeepSeekContextLimitError, DeepSeekTimeoutError  # noqa: E402


class FakeInput:
    async def fill(self, text):
        self.text = text


class FakeKeyboard:
    async def press(self, key):
        return None


class FakeNode:
    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text

    async def query_selector_all(self, selector):
        return []

    async def query_selector(self, selector):
        return None

    async def get_attribute(self, name):
        return None


class FakePage:
    """query_selector_all 第一次调用是 baseline，之后按脚本返回。"""

    def __init__(self, baseline=None, script=None, generating=(False,), url="https://chat.deepseek.com/", page_text=""):
        self.baseline = list(baseline or [])
        self.script = list(script or [[None]])
        self.generating = list(generating)
        self.url = url
        self.page_text = page_text
        self.query_calls = 0
        self.eval_calls = 0
        self.keyboard = FakeKeyboard()
        self.gotos = []

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        return FakeInput()

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url

    async def reload(self, **kwargs):
        self.gotos.append("__reload__")

    async def query_selector_all(self, selector):
        index = self.query_calls
        self.query_calls += 1
        if index == 0:
            return [FakeNode(t) for t in self.baseline]
        texts = self.script[min(index - 1, len(self.script) - 1)]
        return [FakeNode(t) for t in texts if t is not None]

    async def evaluate(self, script):
        # 到顶检测与“生成中”检测共用 evaluate；用脚本内容区分
        if "innerText" in script and "replace" in script:
            return self.page_text
        index = self.eval_calls
        self.eval_calls += 1
        return self.generating[min(index, len(self.generating) - 1)]


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(self.id().replace(".", "_") + ".session")
        self.addCleanup(lambda: self._tmp.exists() and self._tmp.unlink())
        self._patches = [
            unittest.mock.patch.object(config, "SESSION_FILE", self._tmp),
            unittest.mock.patch.object(config, "POLL_INTERVAL_S", 0),
            unittest.mock.patch.object(config, "RESPONSE_TIMEOUT_S", 5.0),
            unittest.mock.patch.object(config, "RETRY_BACKOFF_S", 0),
            unittest.mock.patch.object(config, "CAP_CHECK_EVERY", 1),
            unittest.mock.patch.object(config, "SESSION_MAX_TURNS", 0),
            unittest.mock.patch.object(config, "SESSION_MAX_TOKENS", 0),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def driver_for(page):
        driver = srv.DeepSeekWebDriver()
        driver.page = page
        return driver


class SeedingTests(SessionTestCase):
    """种子 prompt：新会话时必须重放历史，而不是只发最后一条消息。"""

    @staticmethod
    def messages():
        return [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "把 README 补上 Pi 接入"},
            {"role": "assistant", "content": "好的，已完成第一步"},
            {"role": "tool", "content": "bash 输出", "tool_call_id": "call_1"},
            {"role": "user", "content": "继续"},
        ]

    def test_delta_prompt_only_contains_new_messages(self):
        prompt = srv.build_prompt(srv.ChatCompletionRequest(messages=self.messages()).messages)
        self.assertIn("继续", prompt)
        self.assertNotIn("你是助手", prompt)
        self.assertNotIn("把 README 补上 Pi 接入", prompt)

    def test_seeded_prompt_replays_history(self):
        prompt = srv.build_prompt(
            srv.ChatCompletionRequest(messages=self.messages()).messages, seed=True
        )
        self.assertIn("[上下文重建]", prompt)
        self.assertIn("你是助手", prompt)
        self.assertIn("把 README 补上 Pi 接入", prompt)
        self.assertIn("好的，已完成第一步", prompt)
        self.assertIn("bash 输出", prompt)
        self.assertIn("继续", prompt)

    def test_seeded_prompt_truncates_from_the_oldest(self):
        messages = [{"role": "user", "content": "旧" * 100} for _ in range(5)]
        messages.append({"role": "user", "content": "最新的这条"})
        prompt = srv.build_prompt(
            srv.ChatCompletionRequest(messages=messages).messages,
            seed=True,
            seed_max_chars=30,
        )
        self.assertIn("最新的这条", prompt)
        self.assertIn("已省略", prompt)
        self.assertLess(prompt.count("旧" * 100), 5)

    def test_seeded_prompt_keeps_system_and_latest(self):
        prompt = srv.build_prompt(
            srv.ChatCompletionRequest(messages=self.messages()).messages,
            seed=True,
            seed_max_chars=20,
        )
        self.assertIn("你是助手", prompt)
        self.assertIn("继续", prompt)

    def test_new_session_uses_seeded_prompt_in_driver(self):
        page = FakePage(script=[["答案"], ["答案"]])
        driver = self.driver_for(page)
        driver.session_has_history = False
        prompt, seeded = "DELTA", "SEEDED"
        text, _ = asyncio.run(driver.send_chat(prompt, seeded_prompt=seeded))
        self.assertEqual(text, "答案")
        self.assertEqual(driver.session_has_history, True)

    def test_existing_session_uses_delta_prompt(self):
        page = FakePage(baseline=["旧"], script=[["新答案"], ["新答案"]])
        driver = self.driver_for(page)
        driver.session_has_history = True
        seen = []
        original = driver._send_chat_locked

        async def spy(p, on_delta=None, **kwargs):
            seen.append(p)
            return await original(p, on_delta)

        driver._send_chat_locked = spy
        asyncio.run(driver.send_chat("DELTA", seeded_prompt="SEEDED"))
        self.assertEqual(seen, ["DELTA"])


class CapDetectionTests(SessionTestCase):
    def test_detects_context_limit_notice(self):
        page = FakePage(page_text="达到对话长度上限，请开启新对话")
        driver = self.driver_for(page)
        self.assertTrue(asyncio.run(driver._page_shows_context_limit()))

    def test_ignores_unrelated_page_text(self):
        page = FakePage(page_text="一切正常")
        driver = self.driver_for(page)
        self.assertFalse(asyncio.run(driver._page_shows_context_limit()))

    def test_context_limit_raises_specific_error(self):
        # 页面没有新回复，且出现“到顶”提示
        page = FakePage(baseline=["旧"], script=[[None]], page_text="已达到长度限制，请开始新的聊天。")
        driver = self.driver_for(page)
        driver.session_has_history = True
        with self.assertRaises(DeepSeekContextLimitError):
            asyncio.run(driver._send_chat_locked("go"))
        self.assertTrue(driver.session_cap_hit)

    def test_context_limit_error_is_not_a_timeout_error(self):
        self.assertFalse(issubclass(DeepSeekContextLimitError, DeepSeekTimeoutError))


class SessionStateTests(SessionTestCase):
    def test_state_round_trip(self):
        page = FakePage(url="https://chat.deepseek.com/a/chat/s/483878e7-7179-4e63-a19b-365ad11a686e")
        driver = self.driver_for(page)
        driver.session_turns = 7
        driver.session_est_tokens = 1234
        driver.session_cap_hit = True
        driver._save_session_state()

        data = json.loads(self._tmp.read_text(encoding="utf-8"))
        self.assertEqual(data["turns"], 7)
        self.assertEqual(data["est_tokens"], 1234)
        self.assertTrue(data["cap_hit"])
        self.assertTrue(data["url"].endswith("483878e7-7179-4e63-a19b-365ad11a686e"))

        other = self.driver_for(page)
        self.assertEqual(other._saved_session_url(), data["url"])

    def test_legacy_plain_url_file_is_still_readable(self):
        url = "https://chat.deepseek.com/a/chat/s/483878e7-7179-4e63-a19b-365ad11a686e"
        self._tmp.write_text(url, encoding="utf-8")
        driver = self.driver_for(FakePage())
        self.assertEqual(driver._saved_session_url(), url)
        self.assertEqual(driver._load_session_state(), {"url": url})

    def test_garbage_state_file_is_ignored(self):
        self._tmp.write_text("not-a-url-and-not-json", encoding="utf-8")
        driver = self.driver_for(FakePage())
        self.assertIsNone(driver._saved_session_url())

    def test_saving_on_home_page_keeps_previous_url(self):
        url = "https://chat.deepseek.com/a/chat/s/483878e7-7179-4e63-a19b-365ad11a686e"
        self._tmp.write_text(json.dumps({"url": url}), encoding="utf-8")
        driver = self.driver_for(FakePage(url="https://chat.deepseek.com/"))
        driver._save_session_state()
        self.assertEqual(json.loads(self._tmp.read_text(encoding="utf-8"))["url"], url)

    def test_clear_url_drops_previous_session(self):
        self._tmp.write_text(json.dumps({"url": "https://chat.deepseek.com/a/chat/s/x"}), encoding="utf-8")
        driver = self.driver_for(FakePage(url="https://chat.deepseek.com/"))
        driver._save_session_state(clear_url=True)
        self.assertIsNone(json.loads(self._tmp.read_text(encoding="utf-8"))["url"])

    def test_needs_seed_reflects_history(self):
        driver = self.driver_for(FakePage())
        self.assertTrue(driver.needs_seed())
        driver.session_has_history = True
        self.assertFalse(driver.needs_seed())

    def test_session_stats_shape(self):
        driver = self.driver_for(FakePage())
        stats = driver.session_stats()
        for key in ("url", "has_history", "needs_seed", "turns", "est_tokens", "cap_hit"):
            self.assertIn(key, stats)


class RotationTests(SessionTestCase):
    def test_size_budget_marks_pending_rotation(self):
        with unittest.mock.patch.object(config, "SESSION_MAX_TURNS", 1):
            page = FakePage(script=[["a"], ["a"]])
            driver = self.driver_for(page)
            driver.session_has_history = True
            asyncio.run(driver.send_chat("go"))
            self.assertEqual(driver.session_turns, 1)
            self.assertTrue(driver._pending_rotation)

    def test_within_budget_does_not_rotate(self):
        with unittest.mock.patch.object(config, "SESSION_MAX_TURNS", 10):
            page = FakePage(script=[["a"], ["a"]])
            driver = self.driver_for(page)
            driver.session_has_history = True
            asyncio.run(driver.send_chat("go"))
            self.assertEqual(driver.session_turns, 1)
            self.assertFalse(driver._pending_rotation)

    def test_token_budget_also_triggers_rotation(self):
        with unittest.mock.patch.object(config, "SESSION_MAX_TOKENS", 1):
            page = FakePage(script=[["a"], ["a"]])
            driver = self.driver_for(page)
            driver.session_has_history = True
            asyncio.run(driver.send_chat("go"))
            self.assertGreater(driver.session_est_tokens, 0)
            self.assertTrue(driver._pending_rotation)

    def test_pending_rotation_starts_new_session_and_uses_seed(self):
        page = FakePage(script=[["答案"], ["答案"]])
        driver = self.driver_for(page)
        driver.session_has_history = True
        driver._pending_rotation = True
        seen = []
        original = driver._send_chat_locked

        async def spy(p, on_delta=None, **kwargs):
            seen.append(p)
            return await original(p, on_delta)

        driver._send_chat_locked = spy
        asyncio.run(driver.send_chat("DELTA", seeded_prompt="SEEDED"))
        self.assertEqual(seen, ["SEEDED"])
        self.assertIn("https://chat.deepseek.com/", page.gotos)
        self.assertFalse(driver._pending_rotation)
        self.assertEqual(driver.session_turns, 1)

    def test_start_new_session_resets_state(self):
        page = FakePage(url="https://chat.deepseek.com/")
        driver = self.driver_for(page)
        driver.session_turns = 9
        driver.session_est_tokens = 9000
        driver.session_cap_hit = True
        asyncio.run(driver._start_new_session())
        self.assertEqual(driver.session_turns, 0)
        self.assertEqual(driver.session_est_tokens, 0)
        self.assertFalse(driver.session_cap_hit)
        self.assertFalse(driver.session_has_history)


class RetryLadderTests(SessionTestCase):
    def _driver_with_failures(self, failures, page=None):
        """让前 N 次 _send_chat_locked 失败，之后成功。"""
        page = page or FakePage(script=[["答案"], ["答案"]])
        # 写一个合法会话状态，让“恢复同一个会话”这一级真正跑起来
        self._tmp.write_text(
            json.dumps({
                "url": "https://chat.deepseek.com/a/chat/s/483878e7-7179-4e63-a19b-365ad11a686e",
                "turns": 3,
            }),
            encoding="utf-8",
        )
        driver = self.driver_for(page)
        driver.session_has_history = True
        prompts = []
        state = {"n": 0}

        async def fake(prompt, on_delta=None, **kwargs):
            prompts.append(prompt)
            state["n"] += 1
            if state["n"] <= failures:
                raise DeepSeekTimeoutError("boom")
            return "答案", []

        driver._send_chat_locked = fake
        return driver, prompts

    def test_single_attempt_when_retries_disabled(self):
        with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1):
            driver, prompts = self._driver_with_failures(1)
            with self.assertRaises(DeepSeekTimeoutError):
                asyncio.run(driver.send_chat("DELTA", seeded_prompt="SEEDED"))
            self.assertEqual(prompts, ["DELTA"])

    def test_middle_attempt_recovers_same_session(self):
        with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 3):
            driver, prompts = self._driver_with_failures(2)
            asyncio.run(driver.send_chat("DELTA", seeded_prompt="SEEDED"))
            # 第 2 次仍是增量 prompt（恢复同一个会话），第 3 次改为播种
            self.assertEqual(prompts, ["DELTA", "DELTA", "SEEDED"])

    def test_last_attempt_rotates_and_seeds(self):
        with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 2):
            driver, prompts = self._driver_with_failures(1)
            asyncio.run(driver.send_chat("DELTA", seeded_prompt="SEEDED"))
            self.assertEqual(prompts, ["DELTA", "SEEDED"])

    def test_cap_hit_short_circuits_to_rotation(self):
        page = FakePage(script=[["答案"], ["答案"]])
        driver = self.driver_for(page)
        driver.session_has_history = True
        prompts = []
        state = {"n": 0}

        async def fake(prompt, on_delta=None, **kwargs):
            prompts.append(prompt)
            state["n"] += 1
            if state["n"] == 1:
                raise DeepSeekContextLimitError("cap")
            return "答案", []

        driver._send_chat_locked = fake
        with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 3):
            asyncio.run(driver.send_chat("DELTA", seeded_prompt="SEEDED"))
        # 到顶后不应再恢复同一个会话，直接换新会话 + 播种
        self.assertEqual(prompts, ["DELTA", "SEEDED"])

    def test_unrecoverable_run_raises_last_error(self):
        with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 2):
            driver, prompts = self._driver_with_failures(5)
            with self.assertRaises(DeepSeekTimeoutError):
                asyncio.run(driver.send_chat("DELTA", seeded_prompt="SEEDED"))
            self.assertEqual(len(prompts), 2)


class StartupTests(SessionTestCase):
    def _init_driver(self, page, state):
        if state is not None:
            self._tmp.write_text(json.dumps(state), encoding="utf-8")
        driver = self.driver_for(page)
        asyncio.run(driver._restore_session_on_startup())
        return driver

    def test_restores_saved_session_by_default(self):
        url = "https://chat.deepseek.com/a/chat/s/483878e7-7179-4e63-a19b-365ad11a686e"
        page = FakePage(url=url)
        driver = self._init_driver(page, {"url": url, "turns": 5, "est_tokens": 500})
        self.assertTrue(driver.session_has_history)
        self.assertFalse(driver.needs_seed())
        self.assertEqual(driver.session_turns, 5)
        self.assertEqual(page.gotos[0], url)

    def test_cap_hit_state_starts_fresh_session(self):
        page = FakePage(url="https://chat.deepseek.com/")
        driver = self._init_driver(
            page,
            {"url": "https://chat.deepseek.com/a/chat/s/dead", "cap_hit": True, "turns": 30},
        )
        self.assertFalse(driver.session_has_history)
        self.assertTrue(driver.needs_seed())
        self.assertEqual(driver.session_turns, 0)
        self.assertFalse(driver.session_cap_hit)
        self.assertEqual(page.gotos[0], "https://chat.deepseek.com/")

    def test_new_session_env_var_starts_fresh(self):
        url = "https://chat.deepseek.com/a/chat/s/483878e7-7179-4e63-a19b-365ad11a686e"
        page = FakePage(url="https://chat.deepseek.com/")
        with unittest.mock.patch.object(config, "NEW_SESSION_ON_START", True):
            driver = self._init_driver(page, {"url": url, "turns": 5})
        self.assertFalse(driver.session_has_history)
        self.assertEqual(page.gotos[0], "https://chat.deepseek.com/")


class StreamingErrorTypeTests(SessionTestCase):
    """SSE 层必须把“到顶”与普通错误区分开，并总是以 finish_reason + [DONE] 收尾。"""

    @staticmethod
    def _drain(agen):
        async def collect():
            return [chunk async for chunk in agen]

        return asyncio.run(collect())

    @staticmethod
    def _request(**kwargs):
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        payload.update(kwargs)
        return srv.ChatCompletionRequest(**payload)

    def test_context_limit_error_is_typed(self):
        from deepseek_web.streaming import _stream_chat_completion

        class CapDriver:
            async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, **kwargs):
                raise DeepSeekContextLimitError("cap")

        out = "".join(self._drain(_stream_chat_completion(self._request(), "p", CapDriver(), "s")))
        self.assertIn("context_length_exceeded", out)
        self.assertIn("data: [DONE]", out)

    def test_normal_stream_ends_with_finish_and_done(self):
        from deepseek_web.streaming import _stream_chat_completion

        class OkDriver:
            async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, **kwargs):
                if on_delta:
                    await on_delta("答")
                return "答案", []

        out = "".join(self._drain(_stream_chat_completion(self._request(), "p", OkDriver(), "s")))
        self.assertIn("\"finish_reason\": \"stop\"", out)
        self.assertIn("data: [DONE]", out)


if __name__ == "__main__":
    unittest.main()
