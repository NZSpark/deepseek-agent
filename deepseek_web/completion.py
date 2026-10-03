"""生成结束判定（从 driver.py 拆出，作为 mixin 混入 DeepSeekWebDriver）。

只负责「页面是否仍在生成」「是否出现继续按钮」「会话是否到顶」这类纯检测，
不参与会话状态持久化与页面池管理；通过 ``self._page_for`` 拿到页面。
"""

import json
import re
from typing import List, Optional

from . import config
from .errors import DeepSeekContextLimitError


class CompletionMixin:
    """检测回复是否生成结束、是否有继续按钮、页面是否出现会话到顶提示。"""

    _GENERATING_JS = """
    () => {
      const words = ['\u505c\u6b62', 'stop', 'Stop', 'STOP'];
      const nodes = document.querySelectorAll(
        'button, [role="button"], div[class*="stop"], span[class*="stop"], svg[class*="stop"]'
      );
      for (const el of nodes) {
        const label = [
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
          (el.textContent || '').slice(0, 40),
        ].join(' ');
        const cls = typeof el.className === 'string' ? el.className : '';
        if (!words.some((w) => label.includes(w)) && !/stop/i.test(cls)) continue;
        const rect = el.getBoundingClientRect();
        // 必须可见，且位于视口下半部（停止按钮就在底部输入框区域），
        // 避免把正文里含有 stop / 停止 字样的元素误判成生成中
        if (rect.width > 0 && rect.height > 0 && rect.top > window.innerHeight * 0.5) {
          return true;
        }
      }
      return false;
    }
    """

    async def _page_is_generating(self, key: Optional[str] = None) -> Optional[bool]:
        """检测页面是否仍在生成回复。

        True=生成中；False=页面上找不到「停止生成」控件；None=检测失败/无法判断。
        注意：只有在观测到过 True 之后，False 才可信，调用方需自行记录。
        """
        page = self._page_for(key)
        if page is None:
            return None
        try:
            return bool(await page.evaluate(self._GENERATING_JS))
        except Exception:
            return None

    _STOP_CANDIDATES_JS = """
    () => {
      const words = ['\u505c\u6b62', 'stop', 'Stop', 'STOP'];
      const nodes = document.querySelectorAll(
        'button, [role="button"], div[class*="stop"], span[class*="stop"], svg[class*="stop"], [aria-label]'
      );
      const out = [];
      for (const el of nodes) {
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const text = (el.textContent || '').slice(0, 40);
        const cls = typeof el.className === 'string' ? el.className : '';
        const label = [aria, title, text].join(' ');
        if (!words.some((w) => label.includes(w)) && !/stop/i.test(cls)) continue;
        const r = el.getBoundingClientRect();
        out.push({
          tag: el.tagName,
          cls: cls.slice(0, 120),
          aria,
          title,
          text: text.slice(0, 40),
          visible: r.width > 0 && r.height > 0,
          top: Math.round(r.top),
          vh: window.innerHeight,
        });
        if (out.length >= 20) break;
      }
      return out;
    }
    """

    async def debug_stop_candidates(self, key: Optional[str] = None) -> List[dict]:
        """诊断用：列出页面上所有「可能表示生成中」的控件及其位置。"""
        page = self._page_for(key)
        if page is None:
            return []
        try:
            return await page.evaluate(self._STOP_CANDIDATES_JS)
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]

    # 识别并点击「继续生成」按钮：文案由 CONTINUE_BUTTON_TEXTS 注入。
    # 只在底部输入框区域（视口下半部）找可见控件，避免误点正文里出现的「继续」字样。
    _CONTINUE_BUTTON_JS_TEMPLATE = """
    (texts) => {
      const lower = texts.map((t) => t.toLowerCase());
      const nodes = document.querySelectorAll(
        'button, [role="button"], div[class*="continue"], span[class*="continue"]'
      );
      for (const el of nodes) {
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const text = (el.textContent || '').trim();
        const cls = typeof el.className === 'string' ? el.className : '';
        const label = [aria, title, text].join(' ').toLowerCase();
        const hit = lower.some((t) => t && label.includes(t)) || /continue/i.test(cls);
        if (!hit) continue;
        const r = el.getBoundingClientRect();
        // 必须可见，且位于视口下半部（继续按钮就在回复末尾 / 输入框上方）
        if (r.width <= 0 || r.height <= 0 || r.top <= window.innerHeight * 0.5) continue;
        el.click();
        return { clicked: true, text: (text || aria || title).slice(0, 40) };
      }
      return { clicked: false };
    }
    """

    async def _click_continue_if_present(self, key: Optional[str] = None) -> Optional[str]:
        """若页面存在「继续生成」按钮则点击它，返回按钮文案；没有则返回 None。

        用于网页版把一次回复截断成多段时自动续接，拼出完整回复。
        检测失败（页面不可用 / JS 异常）时返回 None，不影响主流程。
        """
        if not config.CONTINUE_BUTTON_ENABLED:
            return None
        page = self._page_for(key)
        if page is None:
            return None
        try:
            result = await page.evaluate(
                self._CONTINUE_BUTTON_JS_TEMPLATE, config.CONTINUE_BUTTON_TEXTS
            )
        except Exception:
            return None
        if isinstance(result, dict) and result.get("clicked"):
            return str(result.get("text") or "继续")
        return None

    _CAP_CHECK_JS_TEMPLATE = (
        "() => { let text = document.body ? (document.body.innerText || '') : '';"
        " for (const node of document.querySelectorAll(%s)) {"
        " const t = node.innerText || ''; if (t) text = text.replace(t, ' '); }"
        " return text; }"
    )

    async def _page_shows_context_limit(self, key: Optional[str] = None) -> bool:
        """页面是否出现“对话长度上限”类提示。

        先把模型回复节点的文本从整页文本里剔除，避免把回复正文里提到
        “长度上限”误判成网页版的提示。
        """
        page = self._page_for(key)
        if page is None:
            return False
        js = self._CAP_CHECK_JS_TEMPLATE % json.dumps(config.RESPONSE_SELECTORS)
        try:
            page_text = await page.evaluate(js)
        except Exception:
            return False
        for pattern in config.CAP_NOTICE_PATTERNS:
            try:
                if re.search(pattern, page_text or "", re.IGNORECASE):
                    return True
            except re.error:
                continue
        return False

    def _mark_context_limit(self, key: Optional[str] = None) -> None:
        state = self._state(key)
        state.cap_hit = True
        state.last_error = "context_length_exceeded"
        self._save_session_state(key=key)

    def _context_limit_error(self) -> "DeepSeekContextLimitError":
        return DeepSeekContextLimitError(
            "DeepSeek 网页会话已达上下文长度上限（网页版会停止响应）。"
            "本服务会自动轮转到新会话并播种历史；若仍失败，请检查登录状态。"
        )

    async def _recover_session(self, key: Optional[str] = None) -> bool:
        """超时后根据保存的会话地址重新进入会话，成功返回 True。"""
        page = self._page_for(key)
        saved = self._saved_session_url(key)
        if page is None or not saved:
            print("[恢复] 未找到已保存的会话链接，无法恢复。")
            return False
        try:
            current = self._current_session_url(key)
            print(f"[恢复] 正在根据保存的会话链接重新进入会话: {saved}")
            if current == saved:
                await page.reload(wait_until="domcontentloaded")
            else:
                await page.goto(saved, wait_until="domcontentloaded")
            if not await self._wait_ready(page):
                print("[恢复] 已打开会话，但未检测到输入框，请检查登录状态。")
                return False
            print("[恢复] 已成功回到之前的会话。")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[恢复] 重新进入会话失败: {exc}")
            return False
