import asyncio
import hashlib
import json
import os
import re
import time
import uuid
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from playwright.async_api import async_playwright


# ==================== 0. 配置加载 (.env) ====================
# 所有可调参数集中在项目根目录的 .env（模板见 .env.example）。
# 这里用一个极简的 .env 解析器，避免为读取配置引入额外依赖：
#   * 已存在的真实环境变量优先于 .env（便于临时覆盖 / CI）；
#   * 支持 `KEY=value`、`#` 注释、空行、值两侧引号。
ENV_FILE = Path(__file__).resolve().parent / ".env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:  # noqa: BLE001
        print(f"[配置] 读取 {path} 失败，将使用默认值：{exc}")
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(ENV_FILE)


def env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

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
    output_dir: Optional[str] = None  # None -> 使用 .env 的 OUTPUT_DIR

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
SESSION_FILE = Path(env_str("SESSION_FILE", "./user_data/.deepseek_session"))
USER_DATA_DIR = env_str("USER_DATA_DIR", "./user_data")
OUTPUT_DIR = env_str("OUTPUT_DIR", "./output")
_SESSION_URL_RE = re.compile(r"https://chat\.deepseek\.com/a/chat/s/[0-9a-fA-F-]+")


class DeepSeekTimeoutError(RuntimeError):
    """等待网页版回复超时。区别于普通运行时错误，可触发会话恢复。"""


# ==================== 2.6 回复结束检测 / 超时参数 ====================
# 总超时（秒）：仅在「结束判定完全失灵 / 消息压根没发出去」时才会用到的兜底。
# 必须小于 Pi 侧 HTTP 客户端的超时，否则客户端会先报错。可用 DEEPSEEK_TIMEOUT 覆盖。
RESPONSE_TIMEOUT_S = env_float("DEEPSEEK_TIMEOUT", 180)
# 打开后每轮轮询都打印一行状态，便于定位「为什么一直判不到结束」（DEEPSEEK_DEBUG=1）
DEBUG = env_bool("DEEPSEEK_DEBUG")
# 轮询间隔（秒）
POLL_INTERVAL_S = env_float("POLL_INTERVAL_S", 1.5)
# 兜底判定：内容（忽略首尾空白）完全相同连续这么多次即认为生成结束
STABLE_POLLS = env_int("STABLE_POLLS", 2)
# 次保守的兜底：仅凭“长度不再增长”收尾时要多等几轮，
# 避免生成中途的长停顿（如长思考）被误判成结束
LEN_STABLE_POLLS = env_int("LEN_STABLE_POLLS", 4)
# ---- DOM 选择器统一集中在这里，网页版改版时只需改这一处 ----
# 回复节点的候选选择器
RESPONSE_SELECTORS = env_str(
    "RESPONSE_SELECTORS", '.ds-markdown, .markdown-body, div[class*="markdown"]'
)
# 输入框候选选择器（.env 中用 "||" 分隔多个候选）
INPUT_SELECTORS = [
    s.strip()
    for s in env_str(
        "INPUT_SELECTORS",
        'textarea[placeholder*="发送"]||textarea[placeholder*="Send"]||#chat-input||textarea',
    ).split("||")
    if s.strip()
]
# 页面就绪（输入框出现）用的选择器
READY_SELECTOR = env_str("READY_SELECTOR", 'textarea, [contenteditable="true"]')
# 代码块 DOM
CODE_BLOCK_SELECTOR = env_str("CODE_BLOCK_SELECTOR", "pre")
CODE_TAG_SELECTOR = env_str("CODE_TAG_SELECTOR", "code")

# ---- 重试 / 运行模式 ----
# 上游超时的最大尝试次数与退避基数（秒）
MAX_UPSTREAM_RETRIES = env_int("DEEPSEEK_RETRIES", 2)
RETRY_BACKOFF_S = env_float("RETRY_BACKOFF_S", 1.0)
# 无显示环境（CI / 服务器）可用 HEADLESS=1 启动；首次登录仍需有头模式
HEADLESS = env_bool("HEADLESS")


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
    """估算 token 数（仅用于填充 OpenAI 的 usage 字段，不是精确值）。

    CJK 字符约 1 char/token，其余字符约 4 char/token。
    不引入 tiktoken：那是 OpenAI 的分词器，算 DeepSeek 的 token 只会
    得到一个“看起来很精确但其实是错的”数字，反而更容易误导客户端做上下文裁剪。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u30ff")
    other = len(text) - cjk
    return max(1, cjk + other // 4)


def _delta_piece(streamed: str, current: str) -> tuple[Optional[str], str]:
    """计算 ``current`` 相对「已经发给客户端的内容」真正新增的部分。

    网页版在生成中可能重排 / 替换回复节点，导致 ``current`` 不再以之前的内容为前缀。
    此时不能用简单的 ``startswith`` 判定（会静默丢字），也不应从头发一遍
    （会重复）。这里退回「公共前缀之后的部分」。

    :return: (需要补发的内容, 客户端补发后实际拥有的内容)；无新增时第一项为 None。
    """
    if current == streamed:
        return None, streamed
    if current.startswith(streamed):
        return current[len(streamed):], current
    limit = min(len(streamed), len(current))
    index = 0
    while index < limit and streamed[index] == current[index]:
        index += 1
    piece = current[index:] or None
    return piece, streamed[:index] + (piece or "")


# ==================== 3. 底层浏览器自动化 Driver ====================

class DeepSeekWebDriver:
    def __init__(self, user_data_dir: str = None):
        user_data_dir = user_data_dir or USER_DATA_DIR
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
                headless=HEADLESS,
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
                    READY_SELECTOR, timeout=15000, state="visible"
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
        则根据保存的会话链接重新进入会话并重试一次。
        如果超时前已经读到实质回复，``_send_chat_locked`` 会直接返回内容，
        不再触发重发，避免网页多出一轮、与 Pi 的状态错位。
        """
        last_error: Optional[DeepSeekTimeoutError] = None
        for attempt in range(1, MAX_UPSTREAM_RETRIES + 1):
            await self._remember_session()
            try:
                return await self._send_chat_locked(prompt, on_delta)
            except DeepSeekTimeoutError as exc:
                # 只有「超时」才可重试；找不到输入框、profile 被占用等属于不可重试
                last_error = exc
                if attempt >= MAX_UPSTREAM_RETRIES:
                    break
                print(
                    f"[恢复] 等待回复超时（第 {attempt}/{MAX_UPSTREAM_RETRIES} 次），"
                    "尝试根据保存的会话链接恢复会话后重试……"
                )
                if not await self._recover_session():
                    break
                await asyncio.sleep(RETRY_BACKOFF_S * attempt)
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
        code_elements = await element.query_selector_all(CODE_BLOCK_SELECTOR)
        for code_el in code_elements:
            code_tag = await code_el.query_selector(CODE_TAG_SELECTOR)
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
            for selector in INPUT_SELECTORS:
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
                before_nodes = await self.page.query_selector_all(RESPONSE_SELECTORS)
                if before_nodes:
                    before_text = (await before_nodes[-1].inner_text()).strip()
            except Exception:
                before_text = ""

            await chat_input.fill(prompt)
            await self.page.keyboard.press("Enter")

            # 2. 轮询等待回复完成
            await asyncio.sleep(POLL_INTERVAL_S)
            last_text = ""
            last_normalized = ""
            last_len = -1
            streamed = ""            # 已经通过 on_delta 发给客户端的内容
            stable_count = 0
            saw_generating = False      # 本轮是否观测到过页面「生成中」状态
            latest_node = None          # 本轮最新的回复节点
            poll = 0
            deadline = asyncio.get_event_loop().time() + RESPONSE_TIMEOUT_S

            while True:
                poll += 1
                responses = await self.page.query_selector_all(RESPONSE_SELECTORS)
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
                        if DEBUG:
                            print(f"[debug] poll={poll} 停止按钮已消失，判定结束")
                        break

                    # 2.2 兜底判定：文本一模一样算一轮不变；
                    #     仅长度不再增长也算，但要更保守（多等几轮），
                    #     以免尾部重排 / 工具栏插入导致永远等不到逐字相等
                    same_text = bool(normalized) and normalized == last_normalized
                    same_len = bool(normalized) and len(normalized) == last_len
                    if same_text or same_len:
                        stable_count += 1
                        threshold = STABLE_POLLS if same_text else LEN_STABLE_POLLS
                        if stable_count >= threshold:
                            last_text = current_text
                            if DEBUG:
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

                if DEBUG:
                    print(
                        f"[debug] poll={poll} nodes={len(responses)} len={len(normalized)} "
                        f"stable={stable_count} generating={generating} saw={saw_generating} "
                        f"before_len={len(before_text)}"
                    )

                # 总超时判定：若这期间其实已经读到实质回复，就直接返回已产生的内容，
                # 绝不再把同一句 prompt 重发一遍（避免网页多出一轮、与 Pi 状态错位）
                if asyncio.get_event_loop().time() > deadline:
                    await self._remember_session()
                    if last_text:
                        print("[超时] 已读取到回复内容，直接返回，不重发。")
                        break
                    raise DeepSeekTimeoutError(
                        f"等待 DeepSeek 响应超时（{int(RESPONSE_TIMEOUT_S)}s）。"
                    )

                await asyncio.sleep(POLL_INTERVAL_S)

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


# ==================== 4. FastAPI 应用与路由 ====================

driver = DeepSeekWebDriver()

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await driver.init()
        driver.init_error = None
    except Exception as exc:  # noqa: BLE001
        # 浏览器起不来时也让服务先启动：便于用 /healthz 定位问题，
        # 并让 /v1/chat/completions 返回可读错误，而不是整个进程直接挂掉
        driver.init_error = str(exc)
        print(
            f"\n[启动警告] 浏览器初始化失败：{exc}\n"
            "服务仍会启动，可用 GET /healthz 查看状态。\n"
        )
    yield
    await driver.close()


def _error_response(status_code: int, message: str, err_type: str):
    """以 OpenAI 兼容的 error 结构返回错误，而不是裸 500 字符串。"""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "code": status_code}},
    )


app = FastAPI(title="DeepSeek Web-to-API Bridge", lifespan=lifespan)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """健康检查：Pi 等客户端可用来探活。"""
    ready = driver.page is not None
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "degraded",
            "browser_ready": ready,
            "headless": HEADLESS,
            "session_url": driver._current_session_url() if ready else None,
            "init_error": driver.init_error,
        },
    )


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "DeepSeek Web-to-API Bridge",
        "openai_compatible": True,
        "endpoints": ["/v1/models", "/v1/chat/completions", "/healthz", "/debug/dom"],
    }


@app.get("/v1/models", response_model=ModelListResponse)
async def list_models():
    """Pi (models.json) 会用该端点做模型发现。"""
    candidates = {m["id"]: m for m in SUPPORTED_MODELS}
    candidates.setdefault("deepseek-chat", {"id": "deepseek-chat"})
    return ModelListResponse(
        data=[ModelCard(id=m["id"]) for m in candidates.values()]
    )


@app.get("/debug/dom", include_in_schema=False)
async def debug_dom():
    """诊断用：返回当前页面上「回复节点」与「疑似停止按钮控件」的真实结构。

    用法：在 Pi 发起一轮对话、DeepSeek 正在生成时反复 curl 该端点，
    即可看出两个结束判定信号（停止按钮 / 文本稳定）究竟有没有生效。
    """
    if driver.page is None:
        raise HTTPException(status_code=503, detail="浏览器尚未初始化")

    nodes = await driver.page.query_selector_all(RESPONSE_SELECTORS)
    last_text = await nodes[-1].inner_text() if nodes else ""
    node_summaries = []
    for index, node in enumerate(nodes):
        try:
            node_text = await node.inner_text()
        except Exception:
            node_text = ""
        try:
            cls = await node.get_attribute("class") or ""
        except Exception:
            cls = ""
        node_summaries.append({
            "index": index,
            "class": cls,
            "text_length": len(node_text),
            "sha1": hashlib.sha1(node_text.encode("utf-8")).hexdigest(),
            "head": node_text[:80],
        })
    return {
        "session_url": driver._current_session_url(),
        "response_node_count": len(nodes),
        "nodes": node_summaries,
        "last_node": {
            "text_length": len(last_text),
            "sha1": hashlib.sha1(last_text.encode("utf-8")).hexdigest(),
            "head": last_text[:200],
            "tail": last_text[-200:],
        },
        "generating": await driver._page_is_generating(),
        "stop_candidates": await driver.debug_stop_candidates(),
    }


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest):
    if not request.messages:
        return _error_response(400, "messages 不能为空", "invalid_request_error")

    if driver.page is None:
        return _error_response(
            503,
            "浏览器尚未就绪，请确认已完成登录、且没有另一个实例占用 user_data。"
            f"初始化错误：{driver.init_error or '无'}",
            "unavailable",
        )

    prompt = DeepSeekWebDriver.build_prompt(request.messages, request.tools, request.tool_choice)
    if not prompt:
        return _error_response(400, "需要包含至少一条 user / tool 消息", "invalid_request_error")

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
    except DeepSeekTimeoutError as exc:
        print("\n[ERR] 等待 DeepSeek 回复超时（已重试）:")
        traceback.print_exc()
        return _error_response(504, str(exc), "timeout")
    except RuntimeError as exc:
        # 浏览器不可用 / 找不到输入框等上游问题
        print("\n[ERR] 上游浏览器不可用:")
        traceback.print_exc()
        return _error_response(502, str(exc), "upstream_error")
    except Exception as exc:  # noqa: BLE001
        print("\n[ERR] 处理请求失败:")
        traceback.print_exc()
        return _error_response(500, str(exc), "server_error")

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
        saved_files = driver.save_extracted_files(
            reply_content, code_blocks, request.output_dir or OUTPUT_DIR
        )

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
    keepalives = 0

    while True:
        try:
            kind, payload = await asyncio.wait_for(queue.get(), timeout=10.0)
        except asyncio.TimeoutError:
            # 网页版生成较慢，发送 SSE 注释保活，避免 Pi 侧超时断连。
            # 带 tools 时回复必须先完整缓冲才能判断是不是 tool_calls，
            # 因此这段时间客户端看不到内容 —— 用注释保活 + 日志保持可观测。
            keepalives += 1
            if DEBUG:
                print(
                    f"[debug] 等待上游回复中（已发 {keepalives} 次 keep-alive，"
                    f"工具模式={wants_tools}）"
                )
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
    uvicorn.run(app, host=env_str("HOST", "127.0.0.1"), port=env_int("PORT", 8000))
