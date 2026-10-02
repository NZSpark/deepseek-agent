"""模块拆分后的结构 / 向后兼容性冒烟测试。

要点：
  * 入口 ``deepseek_api_server`` 必须仍然导出历史的公开名字（老用法不能失效）；
  * 所有可调参数必须能从 ``deepseek_web.config`` 取到，且运行期按属性读取
    （这样 ``patch.object(config, ...)`` 才有效 —— 见 test_end_detection）；
  * ``.env`` 必须仍然从项目根目录读取（拆分后 ``__file__`` 变了，容易踩坑）。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as entry  # noqa: E402
from deepseek_web import config  # noqa: E402


class EntryPointCompatTests(unittest.TestCase):
    """老代码 `from deepseek_api_server import X` 不能失效。"""

    HISTORICAL_NAMES = [
        # 数据模型
        "FunctionCall", "ToolCall", "ChatMessage", "ChatCompletionRequest",
        "ChoiceMessage", "Choice", "Usage", "ChatCompletionResponse",
        "ModelCard", "ModelListResponse", "SUPPORTED_MODELS",
        # 解析 / 文本工具
        "format_tools_instruction", "parse_tool_calls", "to_tool_call_models",
        "estimate_tokens", "build_prompt",
        # Driver 与错误
        "DeepSeekWebDriver", "DeepSeekTimeoutError",
        # 路由
        "app", "driver",
    ]

    def test_historical_names_are_reexported(self):
        missing = [name for name in self.HISTORICAL_NAMES if not hasattr(entry, name)]
        self.assertEqual(missing, [])

    def test_config_module_is_reachable_from_entry(self):
        # tests patch 的是 entry.config.*，必须是同一个模块对象
        self.assertIs(entry.config, config)


class ConfigTests(unittest.TestCase):
    def test_types_are_sane(self):
        self.assertIsInstance(config.RESPONSE_TIMEOUT_S, float)
        self.assertIsInstance(config.POLL_INTERVAL_S, float)
        self.assertIsInstance(config.STABLE_POLLS, int)
        self.assertIsInstance(config.LEN_STABLE_POLLS, int)
        self.assertIsInstance(config.MAX_UPSTREAM_RETRIES, int)
        self.assertEqual(config.HEADLESS, bool(config.HEADLESS))

    def test_selectors_are_usable(self):
        self.assertIn("markdown", config.RESPONSE_SELECTORS)
        self.assertGreaterEqual(len(config.INPUT_SELECTORS), 1)
        self.assertTrue(all(s.strip() for s in config.INPUT_SELECTORS))

    def test_session_url_regex_matches_expected_url(self):
        url = "https://chat.deepseek.com/a/chat/s/483878e7-7179-4e63-a19b-365ad11a686e"
        self.assertIsNotNone(config.SESSION_URL_RE.fullmatch(url))
        self.assertIsNone(config.SESSION_URL_RE.search("https://chat.deepseek.com/"))

    def test_env_file_points_at_project_root(self):
        project_root = Path(__file__).resolve().parent.parent
        self.assertEqual(config.PROJECT_ROOT, project_root)
        self.assertEqual(config.ENV_FILE, project_root / ".env")


class AppTests(unittest.TestCase):
    def test_routes_exist(self):
        paths = {r.path for r in entry.app.routes if hasattr(r, "path")}
        for expected in ("/v1/models", "/v1/chat/completions", "/healthz", "/debug/dom"):
            self.assertIn(expected, paths)

    def test_streaming_generator_accepts_injected_driver(self):
        # 拆分后 streaming 不再依赖全局 driver，必须能通过参数注入
        import inspect

        from deepseek_web.streaming import _stream_chat_completion

        params = list(inspect.signature(_stream_chat_completion).parameters)
        self.assertEqual(params, ["request", "prompt", "driver"])


if __name__ == "__main__":
    unittest.main()
