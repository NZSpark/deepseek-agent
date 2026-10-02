"""`_send_chat_locked` 结束判定的回归测试（用假 page 驱动，不开浏览器）。

这些用例覆盖的是真实踩过的坑：
  * DeepSeek 会回收/替换回复节点，节点数不变（所以绝不能靠“节点数变多”判断）；
  * 文本在完成后完全稳定；
  * 停止按钮在当前 DOM 上识别不到。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv  # noqa: E402


class FakeInput:
    async def fill(self, text):
        return None


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
    """query_selector_all 的第一次调用是发送前的 baseline，之后按脚本返回。"""

    def __init__(self, baseline, script, generating=(False,)):
        self.baseline = list(baseline)
        self.script = list(script)
        self.generating = list(generating)
        self.query_calls = 0
        self.eval_calls = 0
        self.url = "https://chat.deepseek.com/"
        self.keyboard = FakeKeyboard()

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        return FakeInput()

    async def query_selector_all(self, selector):
        index = self.query_calls
        self.query_calls += 1
        if index == 0:
            return [FakeNode(text) for text in self.baseline]
        texts = self.script[min(index - 1, len(self.script) - 1)]
        return [FakeNode(text) for text in texts]

    async def evaluate(self, script):
        index = self.eval_calls
        self.eval_calls += 1
        return self.generating[min(index, len(self.generating) - 1)]


class EndDetectionTestCase(unittest.TestCase):
    def setUp(self):
        # 注意：可调参数现在集中在 deepseek_web.config，运行期按属性读取，
        # 因此必须 patch 真正的定义处（srv.config），patch 入口模块的重导出名字不会生效。
        self._tmp_session = Path(self.id().replace(".", "_") + ".session")
        patch = unittest.mock.patch.object(srv.config, "SESSION_FILE", self._tmp_session)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(lambda: self._tmp_session.exists() and self._tmp_session.unlink())

        self._patch_poll = unittest.mock.patch.object(srv.config, "POLL_INTERVAL_S", 0)
        self._patch_poll.start()
        self.addCleanup(self._patch_poll.stop)

        self._patch_timeout = unittest.mock.patch.object(srv.config, "RESPONSE_TIMEOUT_S", 5.0)
        self._patch_timeout.start()
        self.addCleanup(self._patch_timeout.stop)

        # 固定并发相关配置，避免依赖开发机上的 .env 取值
        self._patch_parallel = unittest.mock.patch.object(srv.config, "PARALLEL_BUCKETS", False)
        self._patch_parallel.start()
        self.addCleanup(self._patch_parallel.stop)

        self._patch_lock_wait = unittest.mock.patch.object(srv.config, "BUCKET_LOCK_TIMEOUT_S", 0)
        self._patch_lock_wait.start()
        self.addCleanup(self._patch_lock_wait.stop)

    @staticmethod
    def driver_for(page):
        driver = srv.DeepSeekWebDriver()
        driver.page = page
        return driver

    def run_chat(self, driver, prompt="go", on_delta=None):
        return asyncio.run(driver.send_chat(prompt, on_delta))


class ConstantNodeCountTests(EndDetectionTestCase):
    """真实故障回归：节点数恒为 2，新回复只是把内容换掉。"""

    def test_replaced_content_is_detected(self):
        page = FakePage(
            baseline=["旧答案", "旧答案"],
            script=[["旧答案", "新答案一段"], ["旧答案", "新答案一段"]],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "新答案一段")

    def test_does_not_resend_prompt(self):
        page = FakePage(
            baseline=["旧答案"],
            script=[["新答案"], ["新答案"]],
        )
        driver = self.driver_for(page)
        sent = []
        original = driver._send_chat_locked

        async def counting(prompt, on_delta=None, **kwargs):
            sent.append(prompt)
            return await original(prompt, on_delta)

        driver._send_chat_locked = counting
        self.run_chat(driver)
        self.assertEqual(len(sent), 1)


class StabilityTests(EndDetectionTestCase):
    def test_streaming_then_stable(self):
        page = FakePage(
            baseline=["旧"],
            script=[["A"], ["AB"], ["ABC"], ["ABC"], ["ABC"]],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "ABC")

    def test_same_length_only_needs_more_polls(self):
        # 每轮文本都不同但长度一致：要连续 LEN_STABLE_POLLS 次才收尾
        script = [[t] for t in ["abc12345x", "abd12345y", "abe12345z", "abf12345w"]]
        page = FakePage(baseline=["旧"], script=script)
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "abf12345w")

    def test_unchanged_content_is_not_reported_as_the_reply(self):
        page = FakePage(baseline=["旧答案"], script=[["旧答案"]])
        with unittest.mock.patch.object(srv.config, "RESPONSE_TIMEOUT_S", 0.05):
            with unittest.mock.patch.object(srv.config, "MAX_UPSTREAM_RETRIES", 1):
                with self.assertRaises(srv.DeepSeekTimeoutError):
                    self.run_chat(self.driver_for(page))

    def test_timeout_returns_content_already_read(self):
        # 内容持续增长（长度与文本都在变）、永远判不到结束：
        # 超时应返回已读到内容，而不是把 prompt 重发一遍
        page = FakePage(
            baseline=["旧"],
            script=[[f"ans{i}" + "x" * i] for i in range(200)],
            generating=[True],
        )
        driver = self.driver_for(page)
        sent = []
        original = driver._send_chat_locked

        async def counting(prompt, on_delta=None, **kwargs):
            sent.append(prompt)
            return await original(prompt, on_delta)

        driver._send_chat_locked = counting
        with unittest.mock.patch.object(srv.config, "RESPONSE_TIMEOUT_S", 0.05):
            text, _ = self.run_chat(driver)
        self.assertTrue(text.startswith("ans"))
        self.assertEqual(len(sent), 1)


class GeneratingStateTests(EndDetectionTestCase):
    def test_stop_button_disappearing_ends_immediately(self):
        page = FakePage(
            baseline=["旧"],
            script=[["答案1"], ["答案2"], ["答案3"], ["答案4"]],
            generating=[True, True, False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "答案3")

    def test_never_seen_generating_falls_back_to_stability(self):
        page = FakePage(
            baseline=["旧"],
            script=[["答案"], ["答案"], ["答案"]],
            generating=[None, None, None],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "答案")


class StreamingDeltaTests(EndDetectionTestCase):
    def test_deltas_reconstruct_the_final_text(self):
        page = FakePage(
            baseline=["旧"],
            script=[["A"], ["AB"], ["ABC"], ["ABC"]],
        )
        pieces = []

        async def collect(piece):
            pieces.append(piece)

        text, _ = self.run_chat(self.driver_for(page), on_delta=collect)
        self.assertEqual("".join(pieces), text)
        self.assertEqual(text, "ABC")

    def test_delta_survives_node_rewrite(self):
        # 中途重排时不再“静默丢字”：公共前缀之后的差异部分一定会补发。
        # SSE 无法撤回已发送的内容，所以客户端的代价是可能多出几个字符，
        # 但绝不会比真实回复更短（漏字对编码代理是致命的）。
        page = FakePage(
            baseline=["旧"],
            script=[["AB"], ["AC"], ["AC"], ["AC"]],
        )
        pieces = []

        async def collect(piece):
            pieces.append(piece)

        self.run_chat(self.driver_for(page), on_delta=collect)
        streamed = "".join(pieces)
        self.assertTrue(streamed.endswith("C"), streamed)
        self.assertGreaterEqual(len(streamed), len("AC"))


if __name__ == "__main__":
    unittest.main()
