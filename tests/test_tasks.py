"""任务快照（轮转后续接任务）的回归测试。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepseek_web import config, tasks  # noqa: E402
from deepseek_web.models import ChatMessage  # noqa: E402


class TaskSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_dir = config.TASK_FILE_DIR
        self._orig_enabled = config.TASK_SNAPSHOT_ENABLED
        config.TASK_FILE_DIR = self.tmp.name
        config.TASK_SNAPSHOT_ENABLED = True
        self.addCleanup(self._restore)

    def _restore(self):
        config.TASK_FILE_DIR = self._orig_dir
        config.TASK_SNAPSHOT_ENABLED = self._orig_enabled

    @staticmethod
    def _msgs(*pairs):
        return [ChatMessage(role=r, content=c) for r, c in pairs]

    def test_record_writes_goal_and_recent(self):
        msgs = self._msgs(
            ("system", "你是助手"),
            ("user", "把仓库重构为 X"),
            ("assistant", "好的"),
        )
        tasks.record("task-a", msgs)
        data = tasks.load("task-a")
        self.assertEqual(data["goal"], "把仓库重构为 X")
        self.assertEqual(data["turns"], 1)
        self.assertTrue(any(m["text"] == "好的" for m in data["recent"]))

    def test_goal_is_sticky_across_turns(self):
        tasks.record("t", self._msgs(("user", "原始目标")))
        tasks.record("t", self._msgs(("user", "原始目标"), ("assistant", "步骤1"), ("user", "继续")))
        data = tasks.load("t")
        self.assertEqual(data["goal"], "原始目标")
        self.assertEqual(data["turns"], 2)

    def test_resume_block_contains_goal(self):
        tasks.record("t", self._msgs(("user", "重构仓库"), ("assistant", "进行中")))
        block = tasks.resume_block("t")
        self.assertIn("任务目标：重构仓库", block)
        self.assertIn("最近进展", block)

    def test_resume_block_empty_without_snapshot(self):
        self.assertEqual(tasks.resume_block("never-seen"), "")

    def test_disabled_writes_nothing(self):
        config.TASK_SNAPSHOT_ENABLED = False
        tasks.record("t", self._msgs(("user", "x")))
        self.assertEqual(tasks.load("t"), {})

    def test_bucket_name_is_filesystem_safe(self):
        tasks.record("../../etc/passwd", self._msgs(("user", "x")))
        # 不应逃逸出目录
        files = list(Path(self.tmp.name).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertTrue(str(files[0]).startswith(self.tmp.name))


class BuildPromptTaskBlockTests(unittest.TestCase):
    def test_task_block_prepended_before_history(self):
        from deepseek_web.prompting import build_prompt

        msgs = [ChatMessage(role="user", content="最新一条消息")]
        prompt = build_prompt(msgs, seed=True, task_block="[任务状态] 目标=重构")
        self.assertIn("[任务状态] 目标=重构", prompt)
        # 任务块必须出现在历史正文之前（保证不被尾部截断丢掉）
        self.assertLess(prompt.index("[任务状态]"), prompt.index("最新一条消息"))

    def test_task_block_ignored_when_not_seeding(self):
        from deepseek_web.prompting import build_prompt

        msgs = [ChatMessage(role="user", content="hi")]
        prompt = build_prompt(msgs, seed=False, task_block="[任务状态] 不该出现")
        self.assertNotIn("[任务状态]", prompt)


if __name__ == "__main__":
    unittest.main()
