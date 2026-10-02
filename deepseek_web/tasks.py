"""任务快照：让网页会话轮转（上下文到顶）后仍能无损续接任务。

背景：网页版会话一旦到顶，driver 会轮转到新会话并“播种”历史。但播种只依赖
**当前这一次请求**的 messages，且 ``SEED_MAX_CHARS`` 会从尾部截断历史——若任务目标
（“把这个仓库重构为 X”）出现在很早的消息里，轮转后就会被截掉，表现为“丢了任务”。

本模块为每个会话桶维护一份轻量快照：
  * ``goal``：该任务最初的目标（第一条 user 消息），**永不被截断**；
  * ``recent``：最近若干条消息的纯文本，作为历史的额外保险；
  * ``updated_at`` / ``turns``：观测用。

轮转播种时，``resume_block()`` 生成一段 ``[任务状态]`` 文本，由 prompting 放在
播种内容的最前面，确保目标始终传达给新会话。
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .models import ChatMessage
from .prompting import _content_to_text


def _dir() -> Path:
    return Path(config.TASK_FILE_DIR)


def _file(bucket: str) -> Path:
    # bucket 已经过 _session_key 消毒（只保留 [\w.\-:]），这里再兜底一次文件名安全
    safe = "".join(ch if ch.isalnum() or ch in ".-_:" else "_" for ch in bucket) or "default"
    return _dir() / f"{safe}.json"


def _goal_from_messages(messages: List[ChatMessage]) -> str:
    """取任务目标：第一条 user 消息（跳过 system）。"""
    for message in messages:
        if message.role == "user":
            text = _content_to_text(message.content).strip()
            if text:
                return text[: max(1, config.TASK_GOAL_MAX_CHARS)]
    return ""


def _recent_texts(messages: List[ChatMessage]) -> List[Dict[str, str]]:
    """最近 N 条消息（role + 文本），滚动保留。"""
    keep = max(0, config.TASK_KEEP_MESSAGES)
    picked = [m for m in messages if m.role != "system"][-keep:] if keep else []
    out: List[Dict[str, str]] = []
    for message in picked:
        text = _content_to_text(message.content).strip()
        if text:
            out.append({"role": message.role, "text": text})
    return out


def load(bucket: str) -> Dict[str, Any]:
    try:
        raw = _file(bucket).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record(bucket: str, messages: List[ChatMessage]) -> None:
    """每轮把当前 messages 快照写入任务文件（goal 只首次写入，之后保留）。"""
    if not config.TASK_SNAPSHOT_ENABLED:
        return
    data = load(bucket)
    goal = data.get("goal") or _goal_from_messages(messages)
    payload = {
        "bucket": bucket,
        "goal": goal,
        "recent": _recent_texts(messages),
        "turns": int(data.get("turns") or 0) + 1,
        "updated_at": int(time.time()),
    }
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        _file(bucket).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def resume_block(bucket: str) -> str:
    """生成轮转播种用的“任务续接”块；无任务文件时返回空串。

    放在播种内容最前面，保证即使历史被 SEED_MAX_CHARS 截断，任务目标仍能传达。
    """
    data = load(bucket)
    goal = (data.get("goal") or "").strip()
    recent = data.get("recent")
    if not goal and not recent:
        return ""
    lines = ["[任务状态] 这是一个正在进行的任务，请据此继续，不要从头重做。"]
    if goal:
        lines.append(f"任务目标：{goal}")
    if isinstance(recent, list) and recent:
        lines.append("最近进展：")
        for item in recent[-max(1, config.TASK_KEEP_MESSAGES):]:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or "?"
            text = (item.get("text") or "").strip()
            if text:
                lines.append(f"- [{role}] {text}")
    return "\n".join(lines)
