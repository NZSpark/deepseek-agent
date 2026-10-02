"""Playwright 浏览器 Driver：把网页对话驱动成可编程的请求/响应。

所有可调参数都从 ``config`` 模块按属性读取（``config.X``），
因此测试可以直接 ``patch.object(config, "X", ...)`` 生效，无需重新 import。
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright

from . import config, prompting
from .models import ChatMessage
from .prompting import _delta_piece


class DeepSeekTimeoutError(RuntimeError):
    """等待网页版回复超时。区别于普通运行时错误，可触发会话恢复。"""


class DeepSeekWebDriver:
    def __init__(self, user_data_dir: str = None):
        user_data_dir = user_data_dir or config.USER_DATA_DIR
        self.user_data_dir = user_data_dir
        self.playwright = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()
        # 浏览器初始化失败时记录原因，让服务仍能启动并对外暴露可读错误
        self.init_error: Optional[str] = None

    async def init(self):
        """初始化浏览器实例"""
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=config.HEADLESS,
                args=["--disable-blink-features=AutomationControlled"]
            )
        except Exception as exc:  # noqa: BLE001
            # persistent context 不能被两个进程共用；给出可操作的提示而不是原始堆栈
            await self.playwright.stop()
            self.playwright = None
            message = str(exc)
            if (
                "existing browser session" in message
                or "profile is already in use" in message
                or "SingletonLock" in message
            ):
                raise RuntimeError(
                    f"浏览器用户目录 {self.user_data_dir} 已被另一个 Chromium 实例占用。\n"
                    "通常是因为已有一个 deepseek_api_server.py 仍在运行，"
                    "或上一次的浏览器窗口没有关闭。\n"
                    "请先结束旧实例再重试：\n"
                    "  pkill -f deepseek_api_server.py\n"
                    "或直接关闭占用该 profile 的 Chromium 窗口。"
                ) from exc
            raise
        self.page = await self.context.new_page()
        saved_session = self._saved_session_url()
        target = saved_session or "https://chat.deepseek.com/"
        await self.page.goto(target, wait_until="domcontentloaded")
        if saved_session:
            print(f"\n[系统提示] 已恢复到上次的会话: {saved_session}")
        print("[系统提示] 服务启动成功！请确保 DeepSeek 页面保持登录状态。\n")

    # ---------- 会话上下文 -> 单条 prompt ----------
    @staticmethod
    def build_prompt(
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> str:
        """把客户端发来的消息数组转换成要发给网页输入框的文本。

        具体实现见 :func:`deepseek_web.prompting.build_prompt`（这里保留为
        静态方法是为了向后兼容既有的调用方式）。
        """
        return prompting.build_prompt(messages, tools, tool_choice)

    # ---------- 会话持久化与恢复 ----------
    def _current_session_url(self) -> Optional[str]:
        """当前页面若处于某个会话中，返回其规范化会话地址。"""
        try:
            url = self.page.url if self.page else ""
        except Exception:
            url = ""
        match = config.SESSION_URL_RE.search(url or "")
        return match.group(0) if match else None

    def _saved_session_url(self) -> Optional[str]:
        """读取已保存的会话地址（若存在且合法）。"""
        try:
            if config.SESSION_FILE.exists():
                url = config.SESSION_FILE.read_text(encoding="utf-8").strip()
                if config.SESSION_URL_RE.fullmatch(url):
                    return url
        except Exception:
            pass
        return None

    async def _remember_session(self) -> None:
        """把当前会话地址写入磁盘，供后续恢复使用。"""
        url = self._current_session_url()
        if not url:
            return
        try:
            config.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.SESSION_FILE.write_text(url, encoding="utf-8")
        except Exception:
            pass

    async def _recover_session(self) -> bool:
        """超时后根据保存的会话地址重新进入会话，成功返回 True。"""
        saved = self._saved_session_url()
        if not saved:
            print("[恢复] 未找到已保存的会话链接，无法恢复。")
            return False
        try:
            current = self._current_session_url()
            print(f"[恢复] 正在根据保存的会话链接重新进入会话: {saved}")
            if current == saved:
                await self.page.reload(wait_until="domcontentloaded")
            else:
                await self.page.goto(saved, wait_until="domcontentloaded")
            try:
                await self.page.wait_for_selector(
                    config.READY_SELECTOR, timeout=15000, state="visible"
                )
            except Exception:
                print("[恢复] 已打开会话，但未检测到输入框，请检查登录状态。")
                return False
            print("[恢复] 已成功回到之前的会话。")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[恢复] 重新进入会话失败: {exc}")
            return False

    async def send_chat(self, prompt: str, on_delta=None) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应。

        正常路径委托给 ``_send_chat_locked``；一旦等待回复超时
        （此时说明页面上完全没有产生新回复，消息很可能根本没发出去），
        则根据保存的会话链接重新进入会话并重试。
        如果超时前已经读到实质回复，``_send_chat_locked`` 会直接返回内容，
        不再触发重发，避免网页多出一轮、与状态错位。
        """
        last_error: Optional[DeepSeekTimeoutError] = None
        for attempt in range(1, config.MAX_UPSTREAM_RETRIES + 1):
            await self._remember_session()
            try:
                return await self._send_chat_locked(prompt, on_delta)
            except DeepSeekTimeoutError as exc:
                # 只有「超时」才可重试；找不到输入框、profile 被占用等属于不可重试
                last_error = exc
                if attempt >= config.MAX_UPSTREAM_RETRIES:
                    break
                print(
                    f"[恢复] 等待回复超时（第 {attempt}/{config.MAX_UPSTREAM_RETRIES} 次），"
                    "尝试根据保存的会话链接恢复会话后重试……"
                )
                if not await self._recover_session():
                    break
                await asyncio.sleep(config.RETRY_BACKOFF_S * attempt)
        if last_error is not None:
            raise last_error
        raise RuntimeError("上游请求未能发送")

    # 主判定所用的 JS：扫描页面上可见的「停止生成」控件
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

    async def _page_is_generating(self) -> Optional[bool]:
        """检测页面是否仍在生成回复。

        True=生成中；False=页面上找不到「停止生成」控件；None=检测失败/无法判断。
        注意：只有在观测到过 True 之后，False 才可信，调用方需自行记录。
        """
        try:
            return bool(await self.page.evaluate(self._GENERATING_JS))
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

    async def debug_stop_candidates(self) -> List[dict]:
        """诊断用：列出页面上所有「可能表示生成中」的控件及其位置。"""
        try:
            return await self.page.evaluate(self._STOP_CANDIDATES_JS)
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]

    async def _extract_code_blocks(self, element) -> List[dict]:
        """从某条回复的 DOM 节点中提取代码块（语言 + 纯代码文本）。"""
        extracted: List[dict] = []
        if element is None:
            return extracted
        code_elements = await element.query_selector_all(config.CODE_BLOCK_SELECTOR)
        for code_el in code_elements:
            code_tag = await code_el.query_selector(config.CODE_TAG_SELECTOR)
            lang = "txt"
            if code_tag:
                class_attr = await code_tag.get_attribute('class') or ""
                lang_match = re.search(r'language-(\w+)', class_attr)
                if lang_match:
                    lang = lang_match.group(1)

            code_content = await (code_tag or code_el).inner_text()
            clean_code = re.sub(
                r'^(?:' + lang + r'|bash|python|json|html|javascript)?\s*(?:Copy|Download)\s*\n',
                '', code_content, flags=re.IGNORECASE
            ).strip()

            extracted.append({"lang": lang, "code": clean_code})
        return extracted

    async def _send_chat_locked(self, prompt: str, on_delta=None) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应及提取的代码块。

        :param on_delta: 可选异步回调，生成过程中实时吐出增量文本（用于 SSE 流式）。
        """
        async with self.lock:
            # 1. 定位并填入输入框
            chat_input = None
            for selector in config.INPUT_SELECTORS:
                try:
                    chat_input = await self.page.wait_for_selector(selector, timeout=3000)
                    if chat_input:
                        break
                except Exception:
                    continue

            if not chat_input:
                raise RuntimeError("无法找到对话输入框，请检查 DeepSeek 网页是否打开或处于登录状态。")

            # 记录发送前最后一条回复的文本，用来判断“新回复是否已经出现”。
            # 注意：绝不能用“回复节点数量变多”来判断。
            # DeepSeek 的消息列表会回收/替换节点，长会话下节点数可能恒为 2，
            # 新回复只会把旧节点内容改掉而不会让数量增长，
            # 那样会导致永远读不到本轮回复直接等到超时。
            before_text = ""
            try:
                before_nodes = await self.page.query_selector_all(config.RESPONSE_SELECTORS)
                if before_nodes:
                    before_text = (await before_nodes[-1].inner_text()).strip()
            except Exception:
                before_text = ""

            await chat_input.fill(prompt)
            await self.page.keyboard.press("Enter")

            # 2. 轮询等待回复完成
            await asyncio.sleep(config.POLL_INTERVAL_S)
            last_text = ""
            last_normalized = ""
            last_len = -1
            streamed = ""            # 已经通过 on_delta 发给客户端的内容
            stable_count = 0
            saw_generating = False      # 本轮是否观测到过页面「生成中」状态
            latest_node = None          # 本轮最新的回复节点
            poll = 0
            deadline = asyncio.get_event_loop().time() + config.RESPONSE_TIMEOUT_S

            while True:
                poll += 1
                responses = await self.page.query_selector_all(config.RESPONSE_SELECTORS)
                current_text = ""
                generating = None
                if responses:
                    latest_node = responses[-1]
                    current_text = await latest_node.inner_text()
                normalized = current_text.strip()

                # 1. 本轮回复是否已经出现：只要最后一条回复的内容与发送前不同即可。
                #    （不看节点数量：长会话下新回复会原地替换旧节点，数量不增长）
                if normalized and normalized != before_text:
                    # 2.1 主判定：页面「生成中」状态。一旦观测到过「停止生成」
                    #     控件、又发现它消失，就说明生成真正结束，可立即收尾
                    generating = await self._page_is_generating()
                    if generating:
                        saw_generating = True
                    elif generating is False and saw_generating:
                        last_text = current_text
                        if config.DEBUG:
                            print(f"[debug] poll={poll} 停止按钮已消失，判定结束")
                        break

                    # 2.2 兜底判定：文本一模一样算一轮不变；
                    #     仅长度不再增长也算，但要更保守（多等几轮），
                    #     以免尾部重排 / 工具栏插入导致永远等不到逐字相等
                    same_text = bool(normalized) and normalized == last_normalized
                    same_len = bool(normalized) and len(normalized) == last_len
                    if same_text or same_len:
                        stable_count += 1
                        threshold = config.STABLE_POLLS if same_text else config.LEN_STABLE_POLLS
                        if stable_count >= threshold:
                            last_text = current_text
                            if config.DEBUG:
                                print(
                                    f"[debug] poll={poll} 内容稳定 {stable_count} 次"
                                    f"（same_text={same_text}），判定结束"
                                )
                            break
                    else:
                        stable_count = 0

                    # 2.3 生成过程中吐出增量，供 SSE 使用。
                    #     用「已发送内容」的公共前缀做 diff，即使节点中途重排也不会漏字
                    if on_delta is not None:
                        piece, streamed = _delta_piece(streamed, current_text)
                        if piece:
                            await on_delta(piece)

                    last_text = current_text
                    last_normalized = normalized
                    last_len = len(normalized)

                if config.DEBUG:
                    print(
                        f"[debug] poll={poll} nodes={len(responses)} len={len(normalized)} "
                        f"stable={stable_count} generating={generating} saw={saw_generating} "
                        f"before_len={len(before_text)}"
                    )

                # 总超时判定：若这期间其实已经读到实质回复，就直接返回已产生的内容，
                # 绝不再把同一句 prompt 重发一遍（避免网页多出一轮、与客户端状态错位）
                if asyncio.get_event_loop().time() > deadline:
                    await self._remember_session()
                    if last_text:
                        print("[超时] 已读取到回复内容，直接返回，不重发。")
                        break
                    raise DeepSeekTimeoutError(
                        f"等待 DeepSeek 响应超时（{int(config.RESPONSE_TIMEOUT_S)}s）。"
                    )

                await asyncio.sleep(config.POLL_INTERVAL_S)

            # 3. 从最新回复节点中提取代码块
            extracted_blocks = await self._extract_code_blocks(latest_node)

            # 成功产生回复后，刷新保存的会话地址（可能刚创建了新会话）
            await self._remember_session()
            return last_text, extracted_blocks

    @staticmethod
    def save_extracted_files(raw_text: str, code_blocks: List[dict], output_dir: str) -> List[str]:
        """将提取的代码落地为对应格式的文件"""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        saved = []

        ext_map = {
            "python": "py", "py": "py", "javascript": "js", "js": "js",
            "html": "html", "css": "css", "json": "json", "cpp": "cpp",
            "c": "c", "bash": "sh", "shell": "sh", "sql": "sql", "markdown": "md"
        }

        if code_blocks:
            for idx, block in enumerate(code_blocks, start=1):
                lang = block["lang"].lower().strip()
                code = block["code"]
                ext = ext_map.get(lang, "py" if "import " in code or "def " in code else "txt")

                timestamp = int(time.time())
                filename = f"code_{timestamp}_{idx}.{ext}"
                filepath = Path(output_dir) / filename

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(code)
                saved.append(str(filepath))
                print(f"[已保存文件] {filepath}")
        else:
            filename = f"response_{int(time.time())}.md"
            filepath = Path(output_dir) / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw_text)
            saved.append(str(filepath))

        return saved

    async def close(self):
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
