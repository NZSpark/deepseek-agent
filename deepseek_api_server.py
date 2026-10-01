import asyncio
import json
import re
import time
import uuid
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from playwright.async_api import async_playwright

# ==================== 1. 定义 OpenAI 兼容的数据结构 ====================
# 参考: https://platform.openai.com/docs/api-reference/chat
# Pi Coding Agent (pi.dev) 通过 models.json 里的 "api": "openai-completions"
# 接入任何 OpenAI 兼容端点，因此这里需要完整支持 messages / tools / stream。

class FunctionCall(BaseModel):
    name: str
    # OpenAI 的 arguments 是 JSON 字符串；这里以字符串对外暴露
    arguments: str = "{}"

class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:16]}")
    type: str = "function"
    function: FunctionCall

class ChatMessage(BaseModel):
    # Pi 会发送 tool / tool_call_id / name 等字段，宽松接收避免校验失败
    model_config = ConfigDict(extra="allow")

    role: str
    # content 可能是字符串，也可能是内容分片数组
    # （如 [{"type": "text", "text": "hi"}]，Pi / 新版 OpenAI 客户端会这样发），
    # 因此用 Any 宽松接收，构建 prompt 时再归一化为纯文本。
    content: Optional[Any] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    # Pi 会附带大量标准字段（temperature、top_p、reasoning_effort、...）
    # 用 extra="allow" 全量吞掉，绝不因未知字段报 422
    model_config = ConfigDict(extra="allow")

    model: str = "deepseek-chat"
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    stream_options: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[Any] = None
    # ---- 本地扩展字段（Pi 不会传，保持默认即可）----
    save_files: Optional[bool] = True
    output_dir: Optional[str] = "./output"

class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None

class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"

class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "deepseek-chat"
    choices: List[Choice]
    usage: Usage = Field(default_factory=Usage)
    saved_files: List[str] = []

class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "deepseek-web-bridge"

class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelCard]


# Pi 的 models.json 里引用的 id 需要与这里一致
SUPPORTED_MODELS = [
    {"id": "deepseek-chat", "context_window": 65536},
    {"id": "deepseek-reasoner", "context_window": 65536},
]


# ==================== 2.5 会话持久化 ====================
# DeepSeek 网页版的每次对话都归属一个固定会话地址，形如：
#   https://chat.deepseek.com/a/chat/s/<uuid>
# 把最近一次成功的会话地址落盘，超时后可用它重新进入同一个会话，
# 避免上下文丢失或停留在空白页。
SESSION_FILE = Path("./user_data/.deepseek_session")
_SESSION_URL_RE = re.compile(r"https://chat\.deepseek\.com/a/chat/s/[0-9a-fA-F-]+")


class DeepSeekTimeoutError(RuntimeError):
    """等待网页版回复超时。区别于普通运行时错误，可触发会话恢复。"""


# ==================== 2. 工具调用（function calling）桥接层 ====================
# DeepSeek 网页版并不原生支持 OpenAI 的 function calling，因此这里采用
# “提示词注入 + 结构化解析”的方式模拟：
#   1. 把 Pi 传来的 tools 描述注入到 prompt，要求模型用 ```tool_call 代码块回话；
#   2. 解析模型输出里的代码块，还原为 OpenAI 的 tool_calls；
#   3. 下一轮请求里 role=tool 的执行结果再拼回 prompt 喂给网页版。

# 仅匹配 "tool_call" / "tool-call" 围栏，避免误伤普通 ```json 代码块
_TOOL_CALL_FENCE_RE = re.compile(r"```(tool[-_]call)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# "json" 围栏仅在内容明显是工具调用时才采纳（兜底，兼容模型不听话的情况）
_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def format_tools_instruction(tools: List[Dict[str, Any]]) -> str:
    """把 OpenAI tools 描述转换成注入网页版的自然语言指令。"""
    lines = [
        "[工具调用说明]",
        "你可以调用下列工具来完成任务（本轮对话中有效）：",
    ]
    for tool in tools:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        lines.append(f"- {name}: {desc}")
        if params:
            lines.append(f"  参数(JSON Schema): {json.dumps(params, ensure_ascii=False)}")

    lines += [
        "",
        "需要调用工具时，只输出一个或多个如下格式的代码块（arguments 必须是合法 JSON）：",
        "```tool_call",
        '{"name": "工具名", "arguments": {参数对象}}',
        "```",
        "一次可输出多个 tool_call 代码块以并行调用多个工具；代码块之外不要输出多余解释。",
        "如果不需要调用任何工具，请直接给出最终回答，不要输出 tool_call 代码块。",
    ]
    return "\n".join(lines)


def _normalize_tool_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    function = entry.get("function") or {}
    name = entry.get("name") or entry.get("tool") or function.get("name")
    arguments = entry.get("arguments")
    if arguments is None:
        arguments = entry.get("parameters")
    if arguments is None:
        arguments = function.get("arguments")
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            pass
    if not name:
        return None
    return {"name": name, "arguments": arguments}


def _tool_names(tools: Optional[List[Dict[str, Any]]]) -> set:
    """从 OpenAI tools 描述里收集合法工具名，用于过滤误报。"""
    names = set()
    for tool in tools or []:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name")
        if name:
            names.add(name)
    return names


def _iter_balanced_objects(text: str):
    """扫描文本，产出顶层、括号平衡的 JSON 对象字面量（能正确处理字符串与转义）。"""
    in_string = False
    escaped = False
    depth = 0
    start = -1
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = index
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:index + 1]
                    start = -1


def parse_tool_calls(text: str, valid_names: Optional[set] = None) -> List[Dict[str, Any]]:
    """从模型回复中解析出工具调用列表。返回 [{"name": ..., "arguments": {...}}, ...]

    需要兼容两种形态：
      1. 带围栏的 ```tool_call ... ```代码块（模型直接输出 markdown 时）；
      2. **无围栏**的 ``tool_call`` 标签 + JSON 对象——这是从 DeepSeek 网页 DOM
         提取 inner_text 后的常见形态：代码块被渲染成 <pre>，围栏退化为标题文字，
         于是只剩 ``tool_call`` 标签与裸 JSON。
    """
    if not text:
        return []

    calls: List[Dict[str, Any]] = []

    def _consume(raw: str, allow_bare_object: bool) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
            entries = data["tool_calls"]
        elif isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            if not allow_bare_object:
                return
            entries = [data]
        else:
            return
        for entry in entries:
            normalized = _normalize_tool_entry(entry)
            if normalized:
                calls.append(normalized)

    for match in _TOOL_CALL_FENCE_RE.finditer(text):
        _consume(match.group(2), allow_bare_object=True)

    if not calls:
        for match in _JSON_FENCE_RE.finditer(text):
            _consume(match.group(1), allow_bare_object=True)

    if not calls:
        # 兜底：无围栏的 "tool_call" 标签 + 平衡 JSON 对象（网页 DOM 提取后的形态）
        marker_re = re.compile(r"tool[-_]?call\b", re.IGNORECASE)
        pos = 0
        while True:
            match = marker_re.search(text, pos)
            if not match:
                break
            segment = text[match.end():]
            parsed = False
            for obj in _iter_balanced_objects(segment):
                _consume(obj, allow_bare_object=True)
                pos = match.end() + segment.index(obj) + len(obj)
                parsed = True
                break
            if not parsed:
                pos = match.end()

    if valid_names:
        calls = [c for c in calls if c.get("name") in valid_names]

    return calls


def to_tool_call_models(calls: List[Dict[str, Any]]) -> List[ToolCall]:
    return [
        ToolCall(
            function=FunctionCall(
                name=call["name"],
                arguments=json.dumps(call["arguments"], ensure_ascii=False),
            )
        )
        for call in calls
    ]


def _content_to_text(content: Any) -> str:
    """把 OpenAI 的 content 归一化为纯文本。

    content 可能是：
      - None
      - 字符串
      - 内容分片数组，例如 [{"type": "text", "text": "hi"}]
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: List[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(pieces)
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return str(content)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文按 ~1 char/token，英文按 ~4 char/token 混合近似）。"""
    if not text:
        return 0
    return max(1, len(text) // 3)


# ==================== 3. 底层浏览器自动化 Driver ====================

class DeepSeekWebDriver:
    def __init__(self, user_data_dir: str = "./user_data"):
        self.user_data_dir = user_data_dir
        self.playwright = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()

    async def init(self):
        """初始化浏览器实例"""
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=False,
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

    # ---------- 3.1 会话上下文 -> 单条 prompt ----------
    @staticmethod
    def build_prompt(
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> str:
        """把 Pi 发来的完整 OpenAI 消息数组，转换成要发给网页输入框的文本。

        网页版是一个持续存在的会话，因此无需每轮重发全部历史：
        只发送“最后一条 assistant 消息之后”的新增消息（新的 user 指令或 tool 结果）。
        """
        last_assistant = -1
        for index, message in enumerate(messages):
            if message.role == "assistant":
                last_assistant = index

        if last_assistant >= 0:
            delta = messages[last_assistant + 1:]
        else:
            delta = messages

        if not delta:
            # 兜底：没有新消息时，退回最后一条 user 消息
            delta = [m for m in messages if m.role == "user"][-1:]

        parts: List[str] = []
        for message in delta:
            content = _content_to_text(message.content).strip()
            if message.role == "system":
                parts.append(f"[系统指令]\n{content}")
            elif message.role == "tool":
                tag = f" {message.tool_call_id}" if message.tool_call_id else ""
                parts.append(f"[工具执行结果{tag}]\n{content}")
            elif message.role == "assistant":
                parts.append(f"[你之前的回复]\n{content}")
            elif content:
                parts.append(content)

        prompt = "\n\n".join(part for part in parts if part).strip()

        use_tools = bool(tools) and tool_choice != "none"
        if use_tools:
            prompt = (prompt + "\n\n" + format_tools_instruction(tools)).strip()

        return prompt

    # ---------- 3.2 会话持久化与恢复 ----------
    def _current_session_url(self) -> Optional[str]:
        """当前页面若处于某个会话中，返回其规范化会话地址。"""
        try:
            url = self.page.url if self.page else ""
        except Exception:
            url = ""
        match = _SESSION_URL_RE.search(url or "")
        return match.group(0) if match else None

    def _saved_session_url(self) -> Optional[str]:
        """读取已保存的会话地址（若存在且合法）。"""
        try:
            if SESSION_FILE.exists():
                url = SESSION_FILE.read_text(encoding="utf-8").strip()
                if _SESSION_URL_RE.fullmatch(url):
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
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(url, encoding="utf-8")
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
                    'textarea, [contenteditable="true"]', timeout=15000, state="visible"
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

        正常路径委托给 ``_send_chat_locked``；一旦等待回复超时，
        则根据保存的会话链接重新进入会话并重试一次。
        """
        await self._remember_session()
        try:
            return await self._send_chat_locked(prompt, on_delta)
        except DeepSeekTimeoutError:
            print("[恢复] 等待回复超时，尝试根据保存的会话链接恢复会话……")
            if await self._recover_session():
                await self._remember_session()
                return await self._send_chat_locked(prompt, on_delta)
            raise

    async def _send_chat_locked(self, prompt: str, on_delta=None) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应及提取的代码块。

        :param on_delta: 可选异步回调，生成过程中实时吐出增量文本（用于 SSE 流式）。
        """
        async with self.lock:
            # 1. 定位并填入输入框
            input_selectors = [
                'textarea[placeholder*="发送"]',
                'textarea[placeholder*="Send"]',
                '#chat-input',
                'textarea'
            ]

            chat_input = None
            for selector in input_selectors:
                try:
                    chat_input = await self.page.wait_for_selector(selector, timeout=3000)
                    if chat_input:
                        break
                except Exception:
                    continue

            if not chat_input:
                raise RuntimeError("无法找到对话输入框，请检查 DeepSeek 网页是否打开或处于登录状态。")

            # 记录发送前的回复数量，确保等待的是“新”回复而非旧回复
            baseline = await self.page.query_selector_all('.ds-markdown, .markdown-body, div[class*="markdown"]')
            baseline_count = len(baseline)

            await chat_input.fill(prompt)
            await self.page.keyboard.press("Enter")

            # 2. 轮询等待回复完成
            await asyncio.sleep(2)
            last_text = ""
            stable_count = 0
            retry_empty_count = 0
            deadline = asyncio.get_event_loop().time() + 180  # 总超时 180 秒

            while True:
                if asyncio.get_event_loop().time() > deadline:
                    await self._remember_session()
                    raise DeepSeekTimeoutError("等待 DeepSeek 响应超时（180s）。")

                responses = await self.page.query_selector_all('.ds-markdown, .markdown-body, div[class*="markdown"]')
                # 必须出现比发送前更多的新回复块，才认为是本轮响应
                if responses and len(responses) > baseline_count:
                    current_text = await responses[-1].inner_text()
                    if current_text == last_text and len(current_text) > 0:
                        stable_count += 1
                        # 连续两次内容不变才判定生成结束，避免过早截断
                        if stable_count >= 2:
                            break
                    else:
                        stable_count = 0
                        # 生成过程中吐出增量，供 SSE 使用
                        if on_delta is not None and current_text.startswith(last_text):
                            piece = current_text[len(last_text):]
                            if piece:
                                await on_delta(piece)
                        last_text = current_text
                else:
                    retry_empty_count += 1
                    if retry_empty_count > 120:
                        await self._remember_session()
                        raise DeepSeekTimeoutError("等待 DeepSeek 响应超时。")

                await asyncio.sleep(1.5)

            # 3. DOM 提取优化：直接从页面中的 <pre> 或代码块 DOM 提取纯代码文本
            extracted_blocks = []
            if responses:
                latest_response = responses[-1]
                code_elements = await latest_response.query_selector_all('pre')
                for code_el in code_elements:
                    code_tag = await code_el.query_selector('code')
                    lang = "txt"
                    if code_tag:
                        class_attr = await code_tag.get_attribute('class') or ""
                        lang_match = re.search(r'language-(\w+)', class_attr)
                        if lang_match:
                            lang = lang_match.group(1)

                    if code_tag:
                        code_content = await code_tag.inner_text()
                    else:
                        code_content = await code_el.inner_text()

                    clean_code = re.sub(
                        r'^(?:' + lang + r'|bash|python|json|html|javascript)?\s*(?:Copy|Download)\s*\n',
                        '', code_content, flags=re.IGNORECASE
                    ).strip()

                    extracted_blocks.append({"lang": lang, "code": clean_code})

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


# ==================== 4. FastAPI 应用与路由 ====================

driver = DeepSeekWebDriver()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await driver.init()
    yield
    await driver.close()

app = FastAPI(title="DeepSeek Web-to-API Bridge", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "DeepSeek Web-to-API Bridge",
        "openai_compatible": True,
        "endpoints": ["/v1/models", "/v1/chat/completions"],
    }


@app.get("/v1/models", response_model=ModelListResponse)
async def list_models():
    """Pi (models.json) 会用该端点做模型发现。"""
    candidates = {m["id"]: m for m in SUPPORTED_MODELS}
    candidates.setdefault("deepseek-chat", {"id": "deepseek-chat"})
    return ModelListResponse(
        data=[ModelCard(id=m["id"]) for m in candidates.values()]
    )


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest):
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    prompt = DeepSeekWebDriver.build_prompt(request.messages, request.tools, request.tool_choice)
    if not prompt:
        raise HTTPException(status_code=400, detail="需要包含至少一条 user / tool 消息")

    # ---------- 流式分支（Pi 默认 stream=true）----------
    if request.stream:
        return StreamingResponse(
            _stream_chat_completion(request, prompt),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---------- 非流式分支 ----------
    try:
        reply_content, code_blocks = await driver.send_chat(prompt)
    except Exception as e:
        print("\n[ERR] 处理请求失败:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

    wants_tools = bool(request.tools) and request.tool_choice != "none"
    tool_calls = parse_tool_calls(reply_content, _tool_names(request.tools)) if wants_tools else []

    if tool_calls:
        return ChatCompletionResponse(
            model=request.model,
            choices=[Choice(
                index=0,
                message=ChoiceMessage(role="assistant", content=None, tool_calls=to_tool_call_models(tool_calls)),
                finish_reason="tool_calls",
            )],
            usage=Usage(
                prompt_tokens=estimate_tokens(prompt),
                completion_tokens=estimate_tokens(reply_content),
                total_tokens=estimate_tokens(prompt) + estimate_tokens(reply_content),
            ),
        )

    saved_files = []
    if request.save_files:
        saved_files = driver.save_extracted_files(reply_content, code_blocks, request.output_dir)

    return ChatCompletionResponse(
        model=request.model,
        choices=[Choice(
            index=0,
            message=ChoiceMessage(role="assistant", content=reply_content),
            finish_reason="stop",
        )],
        usage=Usage(
            prompt_tokens=estimate_tokens(prompt),
            completion_tokens=estimate_tokens(reply_content),
            total_tokens=estimate_tokens(prompt) + estimate_tokens(reply_content),
        ),
        saved_files=saved_files,
    )


def _chunk_text(text: str, size: int = 64) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


async def _stream_chat_completion(request: ChatCompletionRequest, prompt: str):
    """以 OpenAI SSE 格式输出 chunk，兼容 Pi 的 openai-completions 流式解析。"""
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = request.model
    wants_tools = bool(request.tools) and request.tool_choice != "none"

    def encode(delta: Optional[Dict[str, Any]], finish: Optional[str] = None,
               choices: Optional[list] = None) -> str:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": choices if choices is not None else [
                {"index": 0, "delta": delta or {}, "finish_reason": finish}
            ],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # 先发 role 头
    yield encode({"role": "assistant"})

    queue: "asyncio.Queue[tuple]" = asyncio.Queue()

    async def on_delta(piece: str):
        await queue.put(("delta", piece))

    async def runner():
        try:
            # 需要工具时先缓冲（等解析出 tool_calls 再决定输出形态），因此不实时吐字
            reply, blocks = await driver.send_chat(
                prompt, on_delta=None if wants_tools else on_delta
            )
            await queue.put(("done", (reply, blocks, None)))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            await queue.put(("done", (None, [], str(exc))))

    task = asyncio.create_task(runner())

    streamed = False
    reply_content = ""
    error: Optional[str] = None

    while True:
        try:
            kind, payload = await asyncio.wait_for(queue.get(), timeout=10.0)
        except asyncio.TimeoutError:
            # 网页版生成较慢，发送 SSE 注释保活，避免 Pi 侧超时断连
            yield ": keep-alive\n\n"
            continue

        if kind == "delta":
            streamed = True
            yield encode({"content": payload})
        else:
            reply_content, _blocks, error = payload
            break

    await task

    if error:
        yield f"data: {json.dumps({'error': {'message': error, 'type': 'server_error'}}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    tool_calls = parse_tool_calls(reply_content, _tool_names(request.tools)) if wants_tools else []

    if tool_calls:
        for index, call in enumerate(tool_calls):
            yield encode({
                "tool_calls": [{
                    "index": index,
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": ""},
                }]
            })
            arguments_str = json.dumps(call["arguments"], ensure_ascii=False)
            for piece in _chunk_text(arguments_str):
                yield encode({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
        yield encode(None, finish="tool_calls")
    else:
        if not streamed and reply_content:
            for piece in _chunk_text(reply_content):
                yield encode({"content": piece})
        yield encode(None, finish="stop")

    include_usage = isinstance(request.stream_options, dict) and bool(
        request.stream_options.get("include_usage")
    )
    if include_usage:
        completion_tokens = estimate_tokens(reply_content)
        usage_payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": estimate_tokens(prompt),
                "completion_tokens": completion_tokens,
                "total_tokens": estimate_tokens(prompt) + completion_tokens,
            },
        }
        yield f"data: {json.dumps(usage_payload, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
