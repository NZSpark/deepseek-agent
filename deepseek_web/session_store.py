"""会话状态与持久化（从 driver.py 拆出，作为 mixin 混入 DeepSeekWebDriver）。

包含 ``SessionState`` 数据类、按桶读写状态文件、默认桶的属性代理、
以及轮转 / 恢复等会话生命周期操作。依赖宿主提供 ``page`` / ``_sessions`` /
``_page_for`` / ``_wait_ready`` / ``_current_session_url`` / ``_last_prompts``。
"""

import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from . import config
from .errors import DEFAULT_SESSION_KEY, HOME_URL


@dataclass
class SessionState:
    """单个会话桶的状态。会话的“是否新开 / 能否复用”都由它决定。"""

    url: Optional[str] = None
    has_history: bool = False
    turns: int = 0
    est_tokens: int = 0
    cap_hit: bool = False
    pending_rotation: bool = False
    last_error: Optional[str] = None
    updated_at: int = 0

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SessionState":
        state = cls()
        if not isinstance(payload, dict):
            return state
        url = payload.get("url")
        if isinstance(url, str):
            state.url = url
        for name in ("has_history", "cap_hit", "pending_rotation"):
            if name in payload:
                setattr(state, name, bool(payload.get(name)))
        for name in ("turns", "est_tokens", "updated_at"):
            try:
                setattr(state, name, int(payload.get(name) or 0))
            except (TypeError, ValueError):
                setattr(state, name, 0)
        last_error = payload.get("last_error")
        state.last_error = last_error if isinstance(last_error, str) else None
        return state


class SessionStoreMixin:
    """会话桶状态的读写、属性代理与轮转 / 恢复。"""

    def _state(self, key: Optional[str] = None) -> SessionState:
        """取出某个会话桶的状态；首次访问时从磁盘恢复。"""
        bucket = key or DEFAULT_SESSION_KEY
        state = self._sessions.get(bucket)
        if state is None:
            state = SessionState.from_payload(self._load_session_state(bucket))
            self._sessions[bucket] = state
        return state

    def sent_prompt(self, key: Optional[str] = None) -> Optional[str]:
        """某个会话桶最近一次真正发给网页版的 prompt（可能因轮转由增量改选播种版）。"""
        return self._last_prompts.get(key or DEFAULT_SESSION_KEY)

    @property
    def session_has_history(self) -> bool:
        return self._state().has_history

    @session_has_history.setter
    def session_has_history(self, value: bool) -> None:
        self._state().has_history = bool(value)

    @property
    def session_turns(self) -> int:
        return self._state().turns

    @session_turns.setter
    def session_turns(self, value: int) -> None:
        self._state().turns = int(value)

    @property
    def session_est_tokens(self) -> int:
        return self._state().est_tokens

    @session_est_tokens.setter
    def session_est_tokens(self, value: int) -> None:
        self._state().est_tokens = int(value)

    @property
    def session_cap_hit(self) -> bool:
        return self._state().cap_hit

    @session_cap_hit.setter
    def session_cap_hit(self, value: bool) -> None:
        self._state().cap_hit = bool(value)

    @property
    def last_error(self) -> Optional[str]:
        return self._state().last_error

    @last_error.setter
    def last_error(self, value: Optional[str]) -> None:
        self._state().last_error = value

    @property
    def _pending_rotation(self) -> bool:
        return self._state().pending_rotation

    @_pending_rotation.setter
    def _pending_rotation(self, value: bool) -> None:
        self._state().pending_rotation = bool(value)

    def _current_session_url(self, key: Optional[str] = None) -> Optional[str]:
        """某个会话桶的页面若处于会话中，返回其规范化会话地址。"""
        page = self._page_for(key)
        try:
            url = page.url if page else ""
        except Exception:
            url = ""
        match = config.SESSION_URL_RE.search(url or "")
        return match.group(0) if match else None

    # ---------- 会话状态（url + 轮数 / 体积 / 是否到顶）----------
    def _read_state_file(self) -> Dict[str, Any]:
        """读取原始状态文件（解析失败或非 JSON 时返回空字典）。"""
        try:
            if not config.SESSION_FILE.exists():
                return {}
            raw = config.SESSION_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            return {}
        if not raw.startswith("{"):
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _load_session_state(self, key: Optional[str] = None) -> Dict[str, Any]:
        """读取某个会话桶的状态。

        兼容两种旧格式：文件内容是单独的会话 URL 纯文本、或顶层直接放单会话对象。
        """
        bucket = key or DEFAULT_SESSION_KEY
        raw = ""
        try:
            if config.SESSION_FILE.exists():
                raw = config.SESSION_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            return {}
        if not raw:
            return {}
        if not raw.startswith("{"):
            # 旧格式：单独的会话 URL（只可能对应默认桶）
            if bucket == DEFAULT_SESSION_KEY and config.SESSION_URL_RE.fullmatch(raw):
                return {"url": raw}
            return {}
        data = self._read_state_file()
        if bucket == DEFAULT_SESSION_KEY:
            # 历史格式：默认桶的状态字段直接放在文件顶层
            return {k: v for k, v in data.items() if k not in ("sessions", "version")}
        extra = data.get("sessions")
        own = extra.get(bucket) if isinstance(extra, dict) else None
        return own if isinstance(own, dict) else {}

    def _save_session_state(self, clear_url: bool = False, key: Optional[str] = None) -> None:
        """落盘某个会话桶的状态，供后续恢复 / 轮转决策使用。

        文件格式（同时兼容历史格式）：

        * **默认桶**的状态字段直接放在顶层（与历史文件完全一致）；
        * 其余桶放在 ``sessions`` 下，各任务互不影响。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        # 优先取页面当前地址；页面不存在或停在首页时，保留状态里已知的 url，不覆盖
        url = None if clear_url else (self._current_session_url(bucket) or state.url)
        state.url = url
        state.updated_at = int(time.time())
        payload = state.to_payload()

        data = self._read_state_file()
        extra = data.get("sessions")
        extra = dict(extra) if isinstance(extra, dict) else {}
        if bucket == DEFAULT_SESSION_KEY:
            payload["sessions"] = extra
        else:
            extra[bucket] = payload
            # 顶层的默认桶状态原样保留（缺 url 时补 null，便于直接查看默认桶）
            data = {k: v for k, v in data.items() if k != "sessions"}
            data.setdefault("url", None)
            data["sessions"] = extra
            payload = data
        try:
            config.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.SESSION_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _saved_session_url(self, key: Optional[str] = None) -> Optional[str]:
        """读取已保存的会话地址（若存在且合法）。"""
        url = self._load_session_state(key).get("url")
        if isinstance(url, str) and config.SESSION_URL_RE.fullmatch(url):
            return url
        return None

    async def _remember_session(self, key: Optional[str] = None) -> None:
        """刷新落盘的会话状态（保留此名字，兼容既有调用）。"""
        self._save_session_state(key=key)

    def session_keys(self) -> List[str]:
        """当前在用的会话桶（至少包含默认桶）。"""
        return sorted({DEFAULT_SESSION_KEY, *self._sessions})

    def needs_seed(self, key: Optional[str] = None) -> bool:
        """某个会话桶的网页会话里没有可用上下文时，需要把完整历史播种进去。"""
        return not self._state(key).has_history

    def session_stats(self, key: Optional[str] = None) -> Dict[str, Any]:
        """供 /healthz 观察会话增长情况。"""
        state = self._state(key)
        return {
            "url": self._current_session_url(key),
            "has_history": state.has_history,
            "needs_seed": self.needs_seed(key),
            "turns": state.turns,
            "est_tokens": state.est_tokens,
            "cap_hit": state.cap_hit,
            "pending_rotation": state.pending_rotation,
            "last_error": state.last_error,
            "buckets": self.session_keys(),
        }

    def _session_over_budget(self, key: Optional[str] = None) -> bool:
        """会话体积是否已达到轮转阈值（0 表示禁用该维度）。"""
        state = self._state(key)
        if config.SESSION_MAX_TURNS and state.turns >= config.SESSION_MAX_TURNS:
            return True
        if config.SESSION_MAX_TOKENS and state.est_tokens >= config.SESSION_MAX_TOKENS:
            return True
        return False

    async def _start_new_session(self, key: Optional[str] = None) -> None:
        """轮转到新会话，并重置会话状态（调用方必须使用“播种”prompt）。"""
        page = self._page_for(key)
        if page is None:
            return
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        await self._wait_ready(page)
        state = self._state(key)
        state.has_history = False
        state.turns = 0
        state.est_tokens = 0
        state.cap_hit = False
        state.pending_rotation = False
        state.last_error = None
        self._save_session_state(clear_url=True, key=key)
        print("[轮转] 已开启新的网页会话（本轮会用完整历史播种上下文）。")

    def reset_session(self, key: Optional[str] = None) -> None:
        """把某个会话桶标记为“下一轮开新会话”（手动逃生口）。

        只改状态、不碰页面：下一轮的 ``send_chat`` 会先轮转，并用“播种”
        prompt 重放历史，所以不会丢上下文。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        state.pending_rotation = True
        state.cap_hit = False
        state.has_history = False
        self._save_session_state(clear_url=True, key=bucket)
        print(f"[会话] 已请求重置 key={bucket} 的会话，下一轮将开启新会话并播种上下文。")

