"""解析层与文本工具函数的回归测试。

只用标准库 unittest，不需要额外依赖：

    .venv/bin/python -m unittest discover -s tests -t . -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv  # noqa: E402


class ContentToTextTests(unittest.TestCase):
    def test_none_and_plain_string(self):
        self.assertEqual(srv._content_to_text(None), "")
        self.assertEqual(srv._content_to_text("hi"), "hi")

    def test_parts_array(self):
        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        self.assertEqual(srv._content_to_text(content), "a\nb")

    def test_non_text_parts_are_ignored(self):
        content = [{"type": "image_url", "image_url": {"url": "x"}}, {"text": "kept"}]
        self.assertEqual(srv._content_to_text(content), "kept")

    def test_dict_without_text(self):
        self.assertEqual(srv._content_to_text({"foo": 1}), "")


class EstimateTokensTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(srv.estimate_tokens(""), 0)

    def test_english_is_about_quarter(self):
        text = "a" * 400
        self.assertEqual(srv.estimate_tokens(text), 100)

    def test_cjk_counts_per_char(self):
        self.assertEqual(srv.estimate_tokens("你好世界"), 4)

    def test_never_zero_for_content(self):
        self.assertGreaterEqual(srv.estimate_tokens("a"), 1)


class DeltaPieceTests(unittest.TestCase):
    def test_first_piece(self):
        self.assertEqual(srv._delta_piece("", "abc"), ("abc", "abc"))

    def test_no_growth(self):
        self.assertEqual(srv._delta_piece("abc", "abc")[0], None)

    def test_prefix_extension(self):
        self.assertEqual(srv._delta_piece("abc", "abcdef"), ("def", "abcdef"))

    def test_rewrite_does_not_resend_from_scratch(self):
        piece, _ = srv._delta_piece("abc", "abd")
        self.assertEqual(piece, "d")

    def test_rewrite_with_no_common_prefix_sends_all(self):
        piece, _ = srv._delta_piece("abc", "xyz")
        self.assertEqual(piece, "xyz")


class BalancedObjectTests(unittest.TestCase):
    def test_nested_braces(self):
        text = '{"a": {"b": 1}}'
        self.assertEqual(list(srv._iter_balanced_objects(text)), [text])

    def test_brace_inside_string_is_not_counted(self):
        text = '{"cmd": "echo }"}'
        self.assertEqual(list(srv._iter_balanced_objects(text)), [text])

    def test_escaped_quote(self):
        text = '{"cmd": "say \\"hi\\""}'
        self.assertEqual(list(srv._iter_balanced_objects(text)), [text])

    def test_two_objects(self):
        text = '{"a": 1} 中间文字 {"b": 2}'
        self.assertEqual(
            list(srv._iter_balanced_objects(text)), ['{"a": 1}', '{"b": 2}']
        )


class ParseToolCallsTests(unittest.TestCase):
    def test_fenced_tool_call(self):
        text = '```tool_call\n{"name": "bash", "arguments": {"command": "ls"}}\n```'
        calls = srv.parse_tool_calls(text)
        self.assertEqual(calls, [{"name": "bash", "arguments": {"command": "ls"}}])

    def test_bare_label_form_from_dom(self):
        """真实形态：<pre> 把围栏渲染成标题文字，只剩 tool_call 标签 + 裸 JSON。"""
        text = 'tool_call\nCopy\nDownload\n{"name": "read", "arguments": {"path": "a.txt"}}'
        calls = srv.parse_tool_calls(text)
        self.assertEqual(calls, [{"name": "read", "arguments": {"path": "a.txt"}}])

    def test_two_bare_calls_in_one_reply(self):
        text = (
            'tool_call\nCopy\nDownload\n{"name": "read", "arguments": {"path": "a"}}\n'
            'tool_call\nCopy\nDownload\n{"name": "bash", "arguments": {"command": "ls"}}'
        )
        calls = srv.parse_tool_calls(text)
        self.assertEqual([c["name"] for c in calls], ["read", "bash"])

    def test_arguments_inside_command_with_braces(self):
        text = (
            'tool_call\n{"name": "bash", "arguments": '
            '{"command": "awk \'{print $1}\' file.txt"}}'
        )
        calls = srv.parse_tool_calls(text)
        self.assertEqual(calls[0]["arguments"]["command"], "awk '{print $1}' file.txt")

    def test_json_fence_fallback(self):
        text = '```json\n{"tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}]}\n```'
        calls = srv.parse_tool_calls(text)
        self.assertEqual(calls, [{"name": "bash", "arguments": {"command": "ls"}}])

    def test_arguments_as_json_string(self):
        text = '```tool_call\n{"name": "bash", "arguments": "{\\"command\\": \\"ls\\"}"}\n```'
        calls = srv.parse_tool_calls(text)
        self.assertEqual(calls[0]["arguments"], {"command": "ls"})

    def test_valid_names_filter_rejects_placeholders(self):
        text = '```tool_call\n{"name": "工具名", "arguments": {}}\n```'
        self.assertEqual(srv.parse_tool_calls(text, {"bash", "read"}), [])

    def test_plain_text_has_no_calls(self):
        self.assertEqual(srv.parse_tool_calls("已完成 README.md 的更新。"), [])

    def test_empty_input(self):
        self.assertEqual(srv.parse_tool_calls(""), [])

    def test_tool_call_without_json_is_ignored(self):
        self.assertEqual(srv.parse_tool_calls("tool_call 这里没有 JSON"), [])


class BuildPromptTests(unittest.TestCase):
    @staticmethod
    def _msg(role, content, **kw):
        return srv.ChatMessage(role=role, content=content, **kw)

    def test_only_delta_after_last_assistant(self):
        messages = [
            self._msg("system", "你是助手"),
            self._msg("user", "第一个问题"),
            self._msg("assistant", "第一个回答"),
            self._msg("user", "第二个问题"),
        ]
        prompt = srv.DeepSeekWebDriver.build_prompt(messages)
        self.assertEqual(prompt, "第二个问题")

    def test_tool_result_is_labelled(self):
        messages = [
            self._msg("user", "问"),
            self._msg("assistant", "答"),
            self._msg("tool", "输出内容", tool_call_id="call_1"),
        ]
        prompt = srv.DeepSeekWebDriver.build_prompt(messages)
        self.assertIn("[工具执行结果 call_1]", prompt)
        self.assertIn("输出内容", prompt)

    def test_parts_array_content_is_normalized(self):
        messages = [self._msg("user", [{"type": "text", "text": "分片内容"}])]
        prompt = srv.DeepSeekWebDriver.build_prompt(messages)
        self.assertEqual(prompt, "分片内容")

    def test_tools_instruction_is_appended(self):
        tools = [{"type": "function", "function": {"name": "bash", "description": "跑命令"}}]
        messages = [self._msg("user", "做点事")]
        prompt = srv.DeepSeekWebDriver.build_prompt(messages, tools)
        self.assertIn("做点事", prompt)
        self.assertIn("[工具调用说明]", prompt)
        self.assertIn("bash", prompt)

    def test_tool_choice_none_disables_instruction(self):
        tools = [{"type": "function", "function": {"name": "bash"}}]
        messages = [self._msg("user", "做点事")]
        prompt = srv.DeepSeekWebDriver.build_prompt(messages, tools, "none")
        self.assertNotIn("[工具调用说明]", prompt)

    def test_no_messages_after_assistant_falls_back_to_last_user(self):
        messages = [self._msg("user", "早"), self._msg("assistant", "晚")]
        prompt = srv.DeepSeekWebDriver.build_prompt(messages)
        self.assertEqual(prompt, "早")


class ToToolCallModelsTests(unittest.TestCase):
    def test_arguments_are_serialized_to_json_string(self):
        models = srv.to_tool_call_models([{"name": "bash", "arguments": {"command": "ls"}}])
        self.assertEqual(models[0].function.name, "bash")
        self.assertEqual(json.loads(models[0].function.arguments), {"command": "ls"})
        self.assertEqual(models[0].type, "function")
        self.assertTrue(models[0].id.startswith("call_"))


class RequestModelTests(unittest.TestCase):
    def test_unknown_fields_are_tolerated(self):
        request = srv.ChatCompletionRequest(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="high",
            some_future_field={"nested": True},
        )
        self.assertEqual(request.model, "deepseek-chat")
        self.assertEqual(request.messages[0].content, "hi")

    def test_parts_array_content_is_accepted(self):
        request = srv.ChatCompletionRequest(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        )
        self.assertEqual(request.messages[0].content, [{"type": "text", "text": "hi"}])


if __name__ == "__main__":
    unittest.main()
