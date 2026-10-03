"""发送消息 / 轮询回复 / 提取代码块（从 driver.py 拆出，作为 mixin 混入 DeepSeekWebDriver）。

依赖页面池（``_ensure_page`` / ``_page_for`` / ``_session_lock`` / ``_touch_page`` /
``_remember_session`` / ``_start_new_session`` / ``_recover_session`` / ``_state`` /
``_session_over_budget``）与生成检测（``_page_is_generating`` /
``_click_continue_if_present`` / ``_page_shows_context_limit`` /
``_mark_context_limit`` / ``_context_limit_error``）。
"""

import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional

from . import config
from .errors import DEFAULT_SESSION_KEY, DeepSeekContextLimitError, DeepSeekTimeoutError
from .prompting import _delta_piece, estimate_tokens


class ChatIOMixin:
    """发送 prompt、轮询至生成结束、提取回复与代码块。"""

    async def send_chat(
        self,
        prompt: str,
        on_delta=None,
        seeded_prompt: Optional[str] = None,
        key: Optional[str] = None,
    ) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应。

        :param prompt: 增量 prompt（网页会话已有上下文时使用）
        :param seeded_prompt: 带完整历史的“播种”prompt（需要新开会话时使用，
                              未提供则退回 ``prompt``）
        :param key: 会话桶标识（按任务隔离会话）。不同 key 各自持有一条独立
                    的网页会话与页面，互不污染上下文；None 表示默认桶。

        **重试阶梯**（避免在同一个已失效的会话上反复超时）：

        1. 首级：直接用现有会话（若已达体积预算或上次到顶，先轮转到新会话）；
        2. 中间级：按保存的会话链接恢复**同一个**会话（只重开页面）；
        3. 最高一级：**换新会话 + 用播种 prompt 重放历史**。

        为什么把“换新会话”当作最后手段，而不是一直恢复同一个会话：
        页面完全没有新回复（超时）最常见的原因就是会话已到顶 / 已失效，
        重复打开同一个会话注定再次超时。而换新会话只有在有“播种”能力时才安全。
        """
        bucket = key or DEFAULT_SESSION_KEY
        seeded = seeded_prompt or prompt
        max_attempts = max(1, config.MAX_UPSTREAM_RETRIES)
        last_error: Optional[RuntimeError] = None

        # 额外的会话桶需要自己的页面（默认桶就是 self.page，不涉及创建）
        await self._ensure_page(bucket)

        for attempt in range(1, max_attempts + 1):
            state = self._state(bucket)
            if state.pending_rotation:
                # 体积超预算或上次检测到“到顶”：先轮转，再播种
                await self._start_new_session(bucket)
            elif attempt == 1:
                pass
            elif attempt < max_attempts:
                print(f"[恢复] 第 {attempt}/{max_attempts} 次重试：恢复同一个会话……")
                if not await self._recover_session(bucket):
                    break
                await asyncio.sleep(config.RETRY_BACKOFF_S * attempt)
            else:
                print("[恢复] 恢复同一会话无效，改为开启新会话并重放历史……")
                await self._start_new_session(bucket)

            # 会话是新开的（或被轮转过）-> 必须播种，否则模型收不到任何上下文
            active_prompt = seeded if not self._state(bucket).has_history else prompt
            # 记录真正要发出的那份 prompt，供上层估算 usage（按桶隔离，避免并发串台）
            self._last_prompts[bucket] = active_prompt

            await self._remember_session(bucket)
            try:
                return await self._send_chat_locked(active_prompt, on_delta, key=bucket)
            except DeepSeekContextLimitError as exc:
                # 到顶了：下次不要再恢复同一个会话，直接轮转
                last_error = exc
                self._state(bucket).pending_rotation = True
                print(f"[恢复] 第 {attempt}/{max_attempts} 次失败：会话已达上下文上限。")
            except DeepSeekTimeoutError as exc:
                # 只有「超时 / 到顶」才可重试；找不到输入框、profile 被占用等不可重试
                last_error = exc
                self._state(bucket).last_error = str(exc)
                print(f"[恢复] 第 {attempt}/{max_attempts} 次失败：等待回复超时。")

        if last_error is not None:
            raise last_error
        raise RuntimeError("上游请求未能发送")

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

    async def _send_chat_locked(self, prompt: str, on_delta=None,
                                key: Optional[str] = None) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应及提取的代码块。

        :param on_delta: 可选异步回调，生成过程中实时吐出增量文本（用于 SSE 流式）。
        :param key: 会话桶标识（决定使用哪一条页面）。
        """
        bucket = key or DEFAULT_SESSION_KEY
        page = self._page_for(bucket)
        state = self._state(bucket)
        # 默认所有桶共用 self.lock（串行）；只有 PARALLEL_BUCKETS=true 才按桶各持一把锁
        async with self._session_lock(bucket):
            if page is None:
                raise RuntimeError("浏览器尚未初始化：找不到可用于发送的会话页面。")
            self._touch_page(bucket)  # 正在用的页面不会被空闲回收 / LRU 淘汰
            # 1. 定位并填入输入框
            chat_input = None
            for selector in config.INPUT_SELECTORS:
                try:
                    chat_input = await page.wait_for_selector(selector, timeout=3000)
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
            before_count = 0
            try:
                before_nodes = await page.query_selector_all(config.RESPONSE_SELECTORS)
                before_count = len(before_nodes)
                if before_nodes:
                    before_text = (await before_nodes[-1].inner_text()).strip()
            except Exception:
                before_text = ""

            await chat_input.fill(prompt)
            await page.keyboard.press("Enter")

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

            cap_check_every = max(1, config.CAP_CHECK_EVERY)
            no_node_fail_polls = max(2, config.NO_NODE_FAIL_POLLS)
            no_progress_fail_polls = max(
                no_node_fail_polls + 1, config.NO_PROGRESS_FAIL_POLLS
            )
            no_node_polls = 0           # 连续「一个回复节点都没匹配到」的轮数
            no_progress_polls = 0       # 连续「既无新回复又无生成中」的轮数
            continue_used = 0           # 本轮已自动点击「继续生成」的次数

            async def _maybe_continue() -> bool:
                """判定本轮结束时页面上是否有「继续」按钮；有则点击并继续收集。

                返回 True 表示已点击、调用方应**不**结束本轮，而是重置分段状态
                继续轮询（把后续内容拼进同一条回复）。返回 False 表示可以结束。
                超过 ``CONTINUE_BUTTON_MAX`` 次后不再点击，避免无限续接。
                """
                nonlocal continue_used, stable_count, last_normalized, last_len, saw_generating
                if continue_used >= max(0, config.CONTINUE_BUTTON_MAX):
                    return False
                label = await self._click_continue_if_present(bucket)
                if not label:
                    return False
                continue_used += 1
                # 分段重新开始：上一段的“稳定 / 长度 / 生成中”判定不能带到下一段，
                # 否则新一段刚出现就会被判成“没变化”而立刻结束。
                stable_count = 0
                last_normalized = ""
                last_len = -1
                saw_generating = False
                no_progress_polls = 0
                if config.DEBUG:
                    print(f"[debug] 检测到「{label}」按钮，点击后继续收集（第 {continue_used} 次）")
                return True

            while True:
                poll += 1
                responses = await page.query_selector_all(config.RESPONSE_SELECTORS)
                current_text = ""
                generating = None
                if responses:
                    latest_node = responses[-1]
                    current_text = await latest_node.inner_text()
                normalized = current_text.strip()

                # 0. 快速失败：连续多轮一个回复节点都没有。
                #    与「真的在生成但选择器没命中」不同，这里连旧回复都不存在，
                #    基本可断定是选择器失效 / 消息压根没发出去，再等只是浪费超时时间。
                if not responses:
                    no_node_polls += 1
                    if no_node_polls >= no_node_fail_polls:
                        raise DeepSeekTimeoutError(
                            "连续 {} 轮未匹配到任何回复节点（RESPONSE_SELECTORS={!r}），"
                            "疑似选择器失效或消息未成功发送。".format(
                                no_node_polls, config.RESPONSE_SELECTORS
                            )
                        )
                else:
                    no_node_polls = 0

                # 1. 本轮回复是否已经出现。判据（满足其一即可）：
                #    a) 末节点文本 != 发送前文本；
                #    b) 节点数变多（短会话常见）；
                #    c) 已经观测到过「生成中」——这说明本轮确已开始，
                #       此时即使文本暂时等于 before_text（首帧还没渲染完）也算已出现。
                #    注意：不能只看节点数——长会话下新回复会原地替换旧节点，数量不增长。
                reply_seen = (
                    (bool(normalized) and normalized != before_text)
                    or (len(responses) > before_count)
                    or saw_generating
                )

                # 1.1 还没有新回复时，周期性检查是否“会话到顶”。
                #     到顶与“真的卡住”在外表上完全一样（页面不再产生新回复），
                #     不主动看提示语就只能等到超时，而那时已经分不清原因了。
                if not reply_seen:
                    # 1.1 周期性检查是否「会话到顶」。
                    #     到顶与「真的卡住」在外表上完全一样（页面不再产生新回复），
                    #     不主动看提示语就只能等到超时，而那时已经分不清原因了。
                    if poll % cap_check_every == 0:
                        if await self._page_shows_context_limit(bucket):
                            self._mark_context_limit(bucket)
                            raise self._context_limit_error()
                    # 1.2 兜底：新回复迟迟不出现（页面既没生成中、文本也没变）。
                    #     可能是 DOM 选择器没命中新回复、或模型直接复用旧节点。
                    #     观察「是否生成中」一旦变 True 就交给下面的主逻辑。
                    generating = await self._page_is_generating(bucket)
                    if generating:
                        saw_generating = True

                    # 1.3 看门狗：连续多轮既无新回复也无生成中状态，提前结束，
                    #     不再干等到总超时（超时后错误信息也帮不上排查）。
                    no_progress_polls += 1
                    if no_progress_polls >= no_progress_fail_polls:
                        await self._remember_session(bucket)
                        raise DeepSeekTimeoutError(
                            "连续 {} 轮既未出现新回复、也未观测到生成中状态"
                            "（nodes={}，before_len={}），已放弃等待。".format(
                                no_progress_polls, len(responses), len(before_text)
                            )
                        )
                else:
                    no_progress_polls = 0

                if reply_seen:
                    # 2.1 主判定：页面「生成中」状态。一旦观测到过「停止生成」
                    #     控件、又发现它消失，就说明生成真正结束，可立即收尾
                    generating = await self._page_is_generating(bucket)
                    if generating:
                        saw_generating = True
                    elif generating is False and saw_generating:
                        last_text = current_text
                        if await _maybe_continue():
                            await asyncio.sleep(config.POLL_INTERVAL_S)
                            continue
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
                            if await _maybe_continue():
                                await asyncio.sleep(config.POLL_INTERVAL_S)
                                continue
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
                    await self._remember_session(bucket)
                    if last_text:
                        # 超时时若还有「继续」按钮，说明只是被截断而非到顶：先续接一轮
                        if await _maybe_continue():
                            deadline = (
                                asyncio.get_event_loop().time()
                                + config.RESPONSE_TIMEOUT_S
                            )
                            await asyncio.sleep(config.POLL_INTERVAL_S)
                            continue
                        print("[超时] 已读取到回复内容，直接返回，不重发。")
                        break
                    # 超时前最后确认一次是否“到顶”，否则错误信息会误导排查方向
                    if await self._page_shows_context_limit(bucket):
                        self._mark_context_limit(bucket)
                        raise self._context_limit_error()
                    raise DeepSeekTimeoutError(
                        "等待 DeepSeek 响应超时（{}s）：poll={} nodes={} "
                        "saw_generating={} before_len={}（选择器 {}）。".format(
                            int(config.RESPONSE_TIMEOUT_S), poll, len(responses),
                            saw_generating, len(before_text), config.RESPONSE_SELECTORS,
                        )
                    )

                await asyncio.sleep(config.POLL_INTERVAL_S)

            # 3. 从最新回复节点中提取代码块
            extracted_blocks = await self._extract_code_blocks(latest_node)

            # 4. 更新会话状态：已建立历史，并累计体积；超预算则下一轮轮转
            state.has_history = True
            state.turns += 1
            state.est_tokens += estimate_tokens(prompt) + estimate_tokens(last_text)
            state.last_error = None
            if self._session_over_budget(bucket):
                state.pending_rotation = True
                print(
                    f"[轮转] 会话已达预算（轮数={state.turns}，"
                    f"估算 token={state.est_tokens}），"
                    "下一轮将开启新会话并播种上下文。"
                )

            # 成功产生回复后：刷新会话状态（可能刚创建了新会话）并续期页面使用时间
            self._touch_page(bucket)
            await self._remember_session(bucket)
            return last_text, extracted_blocks

    def save_extracted_files(raw_text: str, code_blocks: List[dict], output_dir: str) -> List[str]:
        """将提取的代码落地为对应格式的文件"""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        saved = []

        ext_map = {
            "python": "py", "py": "py", "javascript": "js", "js": "js",
            "html": "html", "css": "css", "json": "json", "cpp": "cpp",
            "c": "c", "bash": "sh", "shell": "sh", "sql": "sql", "markdown": "md"
        }

        # 同一秒内的多个请求会拿到同样的 timestamp，必须再加一段随机后缀，
        # 否则 code_<ts>_1.py / response_<ts>.md 会互相覆盖（多任务并行后很常见）
        unique = uuid.uuid4().hex[:6]

        if code_blocks:
            for idx, block in enumerate(code_blocks, start=1):
                lang = block["lang"].lower().strip()
                code = block["code"]
                ext = ext_map.get(lang, "py" if "import " in code or "def " in code else "txt")

                timestamp = int(time.time())
                filename = f"code_{timestamp}_{idx}_{unique}.{ext}"
                filepath = Path(output_dir) / filename

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(code)
                saved.append(str(filepath))
                print(f"[已保存文件] {filepath}")
        else:
            filename = f"response_{int(time.time())}_{unique}.md"
            filepath = Path(output_dir) / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw_text)
            saved.append(str(filepath))

        return saved

