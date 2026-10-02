"""按任务隔离会话（会话桶）与手动重置的回归测试。

全部用假 page / 假 context 驱动，不需要浏览器。

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
from deepseek_web.driver import DEFAULT_SESSION_KEY  # noqa: E402

URL_A = "https://chat.deepseek.com/a/chat/s/aaaaaaaa-1111-2222-3333-444444444444"
URL_A2 = "https://chat.deepseek.com/a/chat/s/bbbbbbbb-5555-6666-7777-888888888888"


class FakeInput:
    async def fill(self, text):
        self.text = text


class FakePage:
    def __init__(self, url="https://chat.deepseek.com/"):
        self.url = url
        self.gotos = []
        self.keyboard = self

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url

    async def reload(self, **kwargs):
        self.gotos.append("__reload__")

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        return FakeInput()

    async def press(self, key):
        return None


class FakeContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


class BucketTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(self.id().replace(".", "_") + ".session")
        self.addCleanup(lambda: self._tmp.exists() and self._tmp.unlink())
        self._patches = [
            unittest.mock.patch.object(config, "SESSION_FILE", self._tmp),
            unittest.mock.patch.object(config, "POLL_INTERVAL_S", 0),
            unittest.mock.patch.object(config, "RETRY_BACKOFF_S", 0),
            unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1),
            unittest.mock.patch.object(config, "SESSION_MAX_TURNS", 0),
            unittest.mock.patch.object(config, "SESSION_MAX_TOKENS", 0),
            unittest.mock.patch.object(config, "SESSION_SCOPING", True),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def driver_for(self, page=None):
        driver = srv.DeepSeekWebDriver()
        driver.page = page or FakePage()
        driver.context = FakeContext()
        return driver


class StateIsolationTests(BucketTestCase):
    def test_buckets_keep_separate_state(self):
        driver = self.driver_for()
        driver._state("task-a").turns = 3
        self.assertEqual(driver._state().turns, 0)
        self.assertEqual(driver._state("task-a").turns, 3)
        self.assertEqual(driver._state("task-b").turns, 0)

    def test_default_bucket_stays_on_the_top_level(self):
        driver = self.driver_for(FakePage(url=URL_A))
        driver.session_has_history = True
        driver._save_session_state()
        data = json.loads(self._tmp.read_text(encoding="utf-8"))
        # 与历史格式完全一致：默认桶字段在顶层
        self.assertEqual(data["url"], URL_A)
        self.assertTrue(data["has_history"])
        self.assertIn("sessions", data)

    def test_extra_bucket_goes_under_sessions_and_survives_reload(self):
        driver = self.driver_for()
        self.driver_for()  # 只是确保默认桶不参与
        driver._state("task-a").url = URL_A
        driver._state("task-a").turns = 5
        driver._save_session_state(key="task-a")

        data = json.loads(self._tmp.read_text(encoding="utf-8"))
        self.assertIsNone(data["url"])
        self.assertEqual(data["sessions"]["task-a"]["url"], URL_A)
        self.assertEqual(data["sessions"]["task-a"]["turns"], 5)

        other = self.driver_for()
        self.assertEqual(other._saved_session_url("task-a"), URL_A)
        self.assertEqual(other._state("task-a").turns, 5)
        self.assertIsNone(other._saved_session_url())

    def test_legacy_plain_url_file_only_feeds_the_default_bucket(self):
        self._tmp.write_text(URL_A, encoding="utf-8")
        driver = self.driver_for()
        self.assertEqual(driver._saved_session_url(), URL_A)
        self.assertEqual(driver._load_session_state(), {"url": URL_A})
        self.assertIsNone(driver._saved_session_url("task-a"))

    def test_buckets_are_reported_by_healthz_stats(self):
        driver = self.driver_for()
        driver._state("task-a").has_history = True
        stats = driver.session_stats("task-a")
        self.assertTrue(stats["has_history"])
        self.assertIn("task-a", stats["buckets"])
        self.assertIn(DEFAULT_SESSION_KEY, stats["buckets"])

    def test_current_url_uses_the_bucket_page(self):
        driver = self.driver_for(FakePage(url=URL_A))
        driver._pages["task-a"] = FakePage(url=URL_A2)
        self.assertEqual(driver._current_session_url(), URL_A)
        self.assertEqual(driver._current_session_url("task-a"), URL_A2)


class BucketPageTests(BucketTestCase):
    def test_page_is_created_lazily_and_restores_saved_url(self):
        driver = self.driver_for()
        driver._state("task-a").url = URL_A
        self.assertIsNone(driver._page_for("task-a"))
        asyncio.run(driver._ensure_page("task-a"))
        page = driver._page_for("task-a")
        self.assertIsNotNone(page)
        self.assertIs(driver._page_for(), driver.page)  # 默认桶不变
        self.assertEqual(page.gotos, [URL_A])
        # 已有历史 -> 本轮不需要播种
        self.assertFalse(driver.needs_seed("task-a"))

    def test_cap_hit_bucket_starts_from_home_and_needs_seed(self):
        driver = self.driver_for()
        driver._state("task-a").url = URL_A
        driver._state("task-a").cap_hit = True
        asyncio.run(driver._ensure_page("task-a"))
        page = driver._page_for("task-a")
        self.assertEqual(page.gotos, ["https://chat.deepseek.com/"])
        self.assertTrue(driver.needs_seed("task-a"))

    def test_page_creation_is_cached(self):
        driver = self.driver_for()
        asyncio.run(driver._ensure_page("task-a"))
        asyncio.run(driver._ensure_page("task-a"))
        self.assertEqual(len(driver.context.pages), 1)

    def test_bucket_limit_is_enforced(self):
        driver = self.driver_for()
        with unittest.mock.patch.object(config, "MAX_SESSION_BUCKETS", 1):
            asyncio.run(driver._ensure_page("task-a"))
            with self.assertRaises(RuntimeError):
                asyncio.run(driver._ensure_page("task-b"))

    def test_send_chat_requires_browser_context_for_extra_bucket(self):
        driver = self.driver_for()
        driver.context = None
        with self.assertRaises(RuntimeError):
            asyncio.run(driver.send_chat("go", key="task-a"))


class ResetTests(BucketTestCase):
    def test_reset_marks_rotation_and_forgets_the_url(self):
        driver = self.driver_for()
        driver._state("task-a").url = URL_A
        driver._state("task-a").turns = 9
        driver._state("task-a").cap_hit = True
        driver._save_session_state(key="task-a")

        driver.reset_session("task-a")

        state = driver._state("task-a")
        self.assertTrue(state.pending_rotation)
        self.assertFalse(state.cap_hit)
        self.assertFalse(state.has_history)
        self.assertIsNone(driver._saved_session_url("task-a"))
        # 默认桶不受影响
        self.assertFalse(driver._state().pending_rotation)

    def test_reset_then_next_turn_rotates_and_seeds(self):
        driver = self.driver_for()
        driver._state("task-a").has_history = True
        driver.reset_session("task-a")

        seen = []

        async def fake(prompt, on_delta=None, key=None):
            seen.append((prompt, key))
            return "答案", []

        driver._send_chat_locked = fake
        asyncio.run(driver.send_chat("DELTA", seeded_prompt="SEEDED", key="task-a"))

        self.assertEqual(seen, [("SEEDED", "task-a")])
        page = driver._page_for("task-a")
        self.assertEqual(page.gotos[0], "https://chat.deepseek.com/")
        self.assertFalse(driver._state("task-a").pending_rotation)

    def test_reset_of_unknown_bucket_is_harmless(self):
        driver = self.driver_for()
        driver.reset_session("never-used")
        self.assertTrue(driver._state("never-used").pending_rotation)


class SessionKeyResolutionTests(BucketTestCase):
    """server._session_key：请求头 / user 字段 -> 会话桶。"""

    @staticmethod
    def request(**extra):
        payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        payload.update(extra)
        return srv.ChatCompletionRequest(**payload)

    def test_header_wins(self):
        from deepseek_web.server import _session_key

        self.assertEqual(_session_key(self.request(user="body"), "header"), "header")

    def test_user_field_is_the_fallback(self):
        from deepseek_web.server import _session_key

        self.assertEqual(_session_key(self.request(user="pi-task-1"), None), "pi-task-1")

    def test_missing_key_means_default_bucket(self):
        from deepseek_web.server import _session_key

        self.assertIsNone(_session_key(self.request(), None))
        self.assertIsNone(_session_key(self.request(), "   "))

    def test_key_is_sanitized_and_length_limited(self):
        from deepseek_web.server import _session_key

        with unittest.mock.patch.object(config, "SESSION_KEY_MAX_LEN", 8):
            # `.` `-` `:` 属于合法字符（便于用日期 / 任务号做 key），其余被替换成 _
            self.assertEqual(_session_key(self.request(), "../../etc/passwd"), ".._.._et")
            self.assertEqual(_session_key(self.request(), "a b\nc"), "a_b_c")

    def test_scoping_can_be_disabled(self):
        from deepseek_web.server import _session_key

        with unittest.mock.patch.object(config, "SESSION_SCOPING", False):
            self.assertIsNone(_session_key(self.request(), "task-a"))


if __name__ == "__main__":
    unittest.main()
