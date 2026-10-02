"""FastAPI 应用与路由（OpenAI 兼容层）。"""

import hashlib
import re
import traceback
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from . import config
from .driver import (
    DEFAULT_SESSION_KEY,
    DeepSeekContextLimitError,
    DeepSeekTimeoutError,
    DeepSeekWebDriver,
)
from .models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChoiceMessage,
    ModelCard,
    ModelListResponse,
    SUPPORTED_MODELS,
    Usage,
)
from .prompting import build_prompt, estimate_tokens
from .streaming import _stream_chat_completion
from .toolcalls import _tool_names, parse_tool_calls, to_tool_call_models

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


def _session_key(request: ChatCompletionRequest, header_value: Optional[str]) -> Optional[str]:
    """确定本次请求属于哪个会话桶（按任务隔离会话）。

    优先用 ``X-DeepSeek-Session`` 请求头（可用 ``SESSION_KEY_HEADER`` 改名），
    其次退回 OpenAI 的 ``user`` 字段；都没有则返回 None（默认桶，全局共用）。
    取值会被消毒（只保留 ``[\\w.\\-:]``）并限长，避免变成非法文件名 / 超长 JSON 键。
    """
    if not config.SESSION_SCOPING:
        return None
    raw = header_value or ""
    if not raw:
        user = getattr(request, "user", None)
        raw = user if isinstance(user, str) else ""
    raw = raw.strip()
    if not raw:
        return None
    sanitized = re.sub(r"[^\w.\-:]", "_", raw)[: max(1, config.SESSION_KEY_MAX_LEN)]
    return sanitized or None


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """健康检查：Pi 等客户端可用来探活。"""
    ready = driver.page is not None
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "degraded",
            "browser_ready": ready,
            "headless": config.HEADLESS,
            "session_url": driver._current_session_url() if ready else None,
            "session": driver.session_stats(),
            "session_keys": driver.session_keys(),
            "session_scoping": config.SESSION_SCOPING,
            "init_error": driver.init_error,
        },
    )


@app.post("/session/reset", include_in_schema=False)
async def reset_session(session: Optional[str] = None):
    """手动逃生口：让指定会话桶的下一轮开新会话（历史会用“播种”重放，不丢上下文）。

    ``session`` 省略时重置默认桶；也可用 ``DEEPSEEK_NEW_SESSION=true`` 在启动时重置。
    """
    key = (session or "").strip() or None
    driver.reset_session(key)
    bucket = key or DEFAULT_SESSION_KEY
    return JSONResponse(
        content={
            "status": "ok",
            "session": bucket,
            "session_stats": driver.session_stats(bucket),
        }
    )


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "DeepSeek Web-to-API Bridge",
        "openai_compatible": True,
        "endpoints": [
            "/v1/models",
            "/v1/chat/completions",
            "/healthz",
            "/debug/dom",
            "/session/reset",
        ],
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

    nodes = await driver.page.query_selector_all(config.RESPONSE_SELECTORS)
    last_text = await nodes[-1].inner_text() if nodes else ""
    node_summaries: List[dict] = []
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
async def chat_completions(
    request: ChatCompletionRequest,
    x_deepseek_session: Optional[str] = Header(None, alias=config.SESSION_KEY_HEADER),
):
    if not request.messages:
        return _error_response(400, "messages 不能为空", "invalid_request_error")

    if driver.page is None:
        return _error_response(
            503,
            "浏览器尚未就绪，请确认已完成登录、且没有另一个实例占用 user_data。"
            f"初始化错误：{driver.init_error or '无'}",
            "unavailable",
        )

    # 两份文本：增量版（现有会话已有上下文）与播种版（新会话 / 轮转后需要重放历史）。
    # 到底用哪份由 driver 决定（只有它知道当前网页会话是否还有历史）。
    delta_prompt = build_prompt(request.messages, request.tools, request.tool_choice)
    seeded_prompt = build_prompt(
        request.messages,
        request.tools,
        request.tool_choice,
        seed=True,
        seed_max_chars=config.SEED_MAX_CHARS,
    )
    if not delta_prompt and not seeded_prompt:
        return _error_response(400, "需要包含至少一条 user / tool 消息", "invalid_request_error")

    # 按任务隔离会话：同一 X-DeepSeek-Session 取值的请求共用一条网页会话
    session_key = _session_key(request, x_deepseek_session)

    # 仅用于 usage 估算；真正的选型在 driver.send_chat 内部
    prompt = seeded_prompt if driver.needs_seed(session_key) else delta_prompt

    # ---------- 流式分支（Pi 默认 stream=true）----------
    if request.stream:
        return StreamingResponse(
            _stream_chat_completion(request, prompt, driver, seeded_prompt, session_key),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---------- 非流式分支 ----------
    try:
        reply_content, code_blocks = await driver.send_chat(
            prompt, seeded_prompt=seeded_prompt, key=session_key
        )
    except DeepSeekContextLimitError as exc:
        print("\n[ERR] 网页会话已达上下文长度上限:")
        traceback.print_exc()
        return _error_response(400, str(exc), "context_length_exceeded")
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
            reply_content, code_blocks, request.output_dir or config.OUTPUT_DIR
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
