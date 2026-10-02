"""按任务隔离会话（会话桶）与手动重置的回归测试。

全部用假 page / 假 context 驱动，不需要浏览器。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import json
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv  # noqa: E402
from deepseek_web import config  # noqa: E402
from deepseek_web.driver import DEFAULT_SESSION_KEY, DeepSeekBusyError  # noqa: E402

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
        self.closed = False
        self.ready_waits = 0

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url

    async def reload(self, **kwargs):
        self.gotos.append("__reload__")

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        self.ready_waits += 1
        return FakeInput()

    async def close(self):
        self.closed = True

    async def press(self, key):
        return None


class HomeRedirectPage(FakePage):
    """goto 之后并不是停在目标会话上（会话被删 / 被登出重定向）。"""

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = "https://chat.deepseek.com/"


class FakeContext:
    def __init__(self, page_factory=None):
        self.pages = []
        self.page_factory = page_factory or FakePage

    async def new_page(self):
        page = self.page_factory()
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
            # 固定并发相关配置，避免依赖开发机上的 .env 取值
            unittest.mock.patch.object(config, "PARALLEL_BUCKETS", False),
            unittest.mock.patch.object(config, "BUCKET_LOCK_TIMEOUT_S", 0),
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

    def test_page_creation_waits_for_the_input_box(self):
        driver = self.driver_for()
        asyncio.run(driver._ensure_page("task-a"))
        self.assertEqual(driver._page_for("task-a").ready_waits, 1)

    def test_send_chat_requires_browser_context_for_extra_bucket(self):
        driver = self.driver_for()
        driver.context = None
        with self.assertRaises(RuntimeError):
            asyncio.run(driver.send_chat("go", key="task-a"))


class BucketEvictionTests(BucketTestCase):
    """桶页面回收（P1-A）：达上限时不再永久失败，而是 LRU 淘汰 / 空闲回收。"""

    def test_limit_evicts_lru_page_and_keeps_state(self):
        driver = self.driver_for()
        with unittest.mock.patch.object(config, "MAX_SESSION_BUCKETS", 1):
            driver._state("task-a").url = URL_A
            driver._state("task-a").turns = 3
            asyncio.run(driver._ensure_page("task-a"))
            page_a = driver._page_for("task-a")
            asyncio.run(driver._ensure_page("task-b"))  # 不应报错，而是腾位置

        self.assertIsNone(driver._page_for("task-a"))
        self.assertTrue(page_a.closed)
        self.assertIsNotNone(driver._page_for("task-b"))
        # 只关页面、状态保留：下次用 task-a 会回到同一个会话
        self.assertEqual(driver._state("task-a").turns, 3)
        self.assertEqual(driver._state("task-a").url, URL_A)

    def test_evicted_bucket_reopens_the_same_session_without_seeding(self):
        driver = self.driver_for()
        with unittest.mock.patch.object(config, "MAX_SESSION_BUCKETS", 1):
            driver._state("task-a").url = URL_A
            asyncio.run(driver._ensure_page("task-a"))
            asyncio.run(driver._ensure_page("task-b"))
            asyncio.run(driver._ensure_page("task-a"))

        page = driver._page_for("task-a")
        self.assertEqual(page.gotos, [URL_A])
        self.assertFalse(driver.needs_seed("task-a"))

    def test_idle_pages_are_recycled(self):
        driver = self.driver_for()
        driver._state("task-a").url = URL_A
        asyncio.run(driver._ensure_page("task-a"))
        page_a = driver._page_for("task-a")
        driver._page_last_used["task-a"] = time.monotonic() - 10_000
        with unittest.mock.patch.object(config, "BUCKET_IDLE_TTL_S", 60):
            closed = asyncio.run(driver._recycle_idle_pages())
        self.assertEqual(closed, 1)
        self.assertTrue(page_a.closed)
        self.assertIsNone(driver._page_for("task-a"))
        self.assertEqual(driver._state("task-a").url, URL_A)

    def test_idle_recycle_can_be_disabled(self):
        driver = self.driver_for()
        asyncio.run(driver._ensure_page("task-a"))
        driver._page_last_used["task-a"] = time.monotonic() - 10_000
        with unittest.mock.patch.object(config, "BUCKET_IDLE_TTL_S", 0):
            self.assertEqual(asyncio.run(driver._recycle_idle_pages()), 0)
        self.assertIsNotNone(driver._page_for("task-a"))

    def test_busy_bucket_is_never_recycled_or_evicted(self):
        driver = self.driver_for()
        asyncio.run(driver._ensure_page("task-a"))
        driver._page_last_used["task-a"] = time.monotonic() - 10_000

        async def hold():
            async with driver._lock_for("task-a"):
                return await driver._recycle_idle_pages()

        with unittest.mock.patch.object(config, "BUCKET_IDLE_TTL_S", 60):
            self.assertEqual(asyncio.run(hold()), 0)
        self.assertIsNotNone(driver._page_for("task-a"))

    def test_no_evictable_page_raises_with_honest_message(self):
        driver = self.driver_for()
        with unittest.mock.patch.object(config, "MAX_SESSION_BUCKETS", 1):
            asyncio.run(driver._ensure_page("task-a"))

            async def hold():
                async with driver._lock_for("task-a"):
                    with self.assertRaises(RuntimeError) as ctx:
                        await driver._ensure_page("task-b")
                    return str(ctx.exception)

            message = asyncio.run(hold())
        # 提示必须指向真实可行的出路，不能再说“用 /session/reset 回收”
        self.assertIn("请稍后重试", message)
        self.assertNotIn("/session/reset", message)

    def test_zero_limit_is_rejected_with_actionable_message(self):
        driver = self.driver_for()
        with unittest.mock.patch.object(config, "MAX_SESSION_BUCKETS", 0):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(driver._ensure_page("task-a"))
        message = str(ctx.exception)
        self.assertIn("MAX_SESSION_BUCKETS=0", message)
        self.assertIn("SESSION_SCOPING=false", message)


class LandingCheckTests(BucketTestCase):
    """goto 之后必须反查真实落点（P1-B）：落在首页就不能当作“有历史”。"""

    def test_redirected_bucket_needs_seed(self):
        driver = self.driver_for()
        driver.context = FakeContext(page_factory=HomeRedirectPage)
        driver._state("task-a").url = URL_A
        asyncio.run(driver._ensure_page("task-a"))
        self.assertTrue(driver.needs_seed("task-a"))

    def test_real_session_url_counts_as_history(self):
        driver = self.driver_for()
        driver.context = FakeContext()
        driver._state("task-a").url = URL_A
        asyncio.run(driver._ensure_page("task-a"))
        self.assertFalse(driver.needs_seed("task-a"))

    def test_startup_with_dead_session_url_requires_seed(self):
        self._tmp.write_text(
            json.dumps({"url": URL_A, "turns": 5, "est_tokens": 500}), encoding="utf-8"
        )
        driver = self.driver_for(HomeRedirectPage())
        asyncio.run(driver._restore_session_on_startup())
        self.assertTrue(driver.needs_seed())

    def test_startup_with_live_session_url_reuses_history(self):
        self._tmp.write_text(
            json.dumps({"url": URL_A, "turns": 5, "est_tokens": 500}), encoding="utf-8"
        )
        driver = self.driver_for(FakePage(url=URL_A))
        asyncio.run(driver._restore_session_on_startup())
        self.assertFalse(driver.needs_seed())
        self.assertEqual(driver.session_turns, 5)


class LockTests(BucketTestCase):
    """分桶 ≠ 并发（P1-C）：默认串行，只有显式开关才按桶各持一把锁。"""

    def test_serial_by_default(self):
        driver = self.driver_for()
        self.assertIs(driver._lock_for(), driver.lock)
        self.assertIs(driver._lock_for("task-a"), driver.lock)
        self.assertIs(driver._lock_for("task-b"), driver.lock)

    def test_parallel_buckets_get_their_own_locks(self):
        driver = self.driver_for()
        with unittest.mock.patch.object(config, "PARALLEL_BUCKETS", True):
            lock_a = driver._lock_for("task-a")
            self.assertIsNot(lock_a, driver.lock)
            self.assertIsNot(lock_a, driver._lock_for("task-b"))
            self.assertIs(lock_a, driver._lock_for("task-a"))  # 缓存的同一把
            self.assertIs(driver._lock_for(), driver.lock)      # 默认桶仍用全局锁


class SentPromptIsolationTests(BucketTestCase):
    """并发多 Agent：实发 prompt 必须按桶隔离，usage 不能串台。"""

    @staticmethod
    def _stub_send(driver):
        async def fake(prompt, on_delta=None, key=None):
            return "答案", []

        driver._send_chat_locked = fake

    def test_prompts_are_tracked_per_bucket(self):
        driver = self.driver_for()
        self._stub_send(driver)
        asyncio.run(driver.send_chat("A", seeded_prompt="SEED-A", key="agent-a"))
        asyncio.run(driver.send_chat("B", seeded_prompt="SEED-B", key="agent-b"))
        # 两个新桶都会播种，各记各的，互不覆盖
        self.assertEqual(driver.sent_prompt("agent-a"), "SEED-A")
        self.assertEqual(driver.sent_prompt("agent-b"), "SEED-B")
        # 默认桶没发过，不能拿到别人的
        self.assertIsNone(driver.sent_prompt())


class BusyLockTests(BucketTestCase):
    """同一会话桶锁的等待上限（BUCKET_LOCK_TIMEOUT_S）：超时快速失败，不无限排队。"""

    def test_same_bucket_second_request_fails_fast(self):
        driver = self.driver_for()
        driver._pages["agent-a"] = FakePage()  # 已有页面，send_chat 不会去建页

        async def scenario():
            with unittest.mock.patch.object(config, "PARALLEL_BUCKETS", True), \
                 unittest.mock.patch.object(config, "BUCKET_LOCK_TIMEOUT_S", 0.05):
                busy = driver._lock_for("agent-a")
                await busy.acquire()  # 模拟 agent-a 正在生成
                try:
                    with self.assertRaises(DeepSeekBusyError):
                        await driver.send_chat("X", seeded_prompt="SEED", key="agent-a")
                finally:
                    busy.release()
                # 等锁超时不能把锁泄漏掉：释放后必须可用
                self.assertFalse(busy.locked())

        asyncio.run(scenario())

    def test_wait_is_unlimited_by_default(self):
        driver = self.driver_for()
        self.assertEqual(driver.cluster_stats()["bucket_lock_timeout_s"], 0)


class ClusterStatsTests(BucketTestCase):
    """多 Agent 运维观测：/healthz 的 cluster 字段内容。"""

    def test_reports_parallel_and_bucket_usage(self):
        driver = self.driver_for()
        driver._state("agent-a")  # 触发一个会话桶
        with unittest.mock.patch.object(config, "PARALLEL_BUCKETS", True), \
             unittest.mock.patch.object(config, "MAX_SESSION_BUCKETS", 3):
            stats = driver.cluster_stats()
        self.assertTrue(stats["parallel"])
        self.assertEqual(stats["max_buckets"], 3)
        self.assertIn("agent-a", stats["keys"])
        self.assertEqual(stats["busy"], [])

    def test_busy_keys_tracks_the_active_bucket(self):
        driver = self.driver_for()

        async def scenario():
            with unittest.mock.patch.object(config, "PARALLEL_BUCKETS", True):
                async with driver._session_lock("agent-a"):
                    self.assertEqual(driver.busy_keys(), ["agent-a"])

        asyncio.run(scenario())
        self.assertEqual(driver.busy_keys(), [])  # 释放后不再算忙


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


class SaveFilesTests(unittest.TestCase):
    """落盘文件名去重（P2-E）：同一秒内的两个请求不能互相覆盖。"""

    def test_same_second_requests_do_not_overwrite(self):
        blocks = [{"lang": "python", "code": "print(1)"}]
        with tempfile.TemporaryDirectory() as out:
            first = srv.DeepSeekWebDriver.save_extracted_files("回复 A", blocks, out)
            second = srv.DeepSeekWebDriver.save_extracted_files("回复 B", blocks, out)
            self.assertNotEqual(first, second)
            for path in first + second:
                self.assertTrue(Path(path).exists())

    def test_plain_text_replies_also_get_unique_names(self):
        with tempfile.TemporaryDirectory() as out:
            first = srv.DeepSeekWebDriver.save_extracted_files("A", [], out)
            second = srv.DeepSeekWebDriver.save_extracted_files("B", [], out)
        self.assertNotEqual(first, second)
        self.assertTrue(first[0].endswith(".md"))


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

    def test_ua_is_used_when_header_and_user_missing(self):
        from deepseek_web.server import _session_key

        # 默认 SESSION_SCOPING_BY_UA=True：无 header / user 时按 UA 自动分桶
        self.assertEqual(
            _session_key(self.request(), None, "cline/3.2.1"), "ua:cline"
        )
        self.assertEqual(
            _session_key(self.request(), None, "python-httpx/0.27.0"), "ua:python-httpx"
        )

    def test_header_and_user_still_win_over_ua(self):
        from deepseek_web.server import _session_key

        self.assertEqual(
            _session_key(self.request(user="body"), "header", "cline/3.2"), "header"
        )
        self.assertEqual(
            _session_key(self.request(user="pi-1"), None, "cline/3.2"), "pi-1"
        )

    def test_ua_scoping_can_be_disabled(self):
        from deepseek_web.server import _session_key

        with unittest.mock.patch.object(config, "SESSION_SCOPING_BY_UA", False):
            self.assertIsNone(_session_key(self.request(), None, "cline/3.2.1"))

    def test_ua_match_respects_word_boundaries(self):
        from deepseek_web.server import _client_from_ua

        # 不应因子串命中已知客户端；无词边界时退回通用 product
        self.assertEqual(_client_from_ua("MyContinueBot/1.0"), "ua:mycontinuebot")
        self.assertEqual(_client_from_ua("openai-python/1.2"), "ua:openai-python")
        self.assertEqual(_client_from_ua("Cline/3.2 (darwin)"), "ua:cline")
        self.assertEqual(_client_from_ua(""), None)


if __name__ == "__main__":
    unittest.main()
