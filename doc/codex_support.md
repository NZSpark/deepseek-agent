# Codex CLI 支持方案（Responses API 兼容层）

> 版本：v1.0
> 状态：设计稿
> 最后更新：2026-10-02
> 依据：`deepseek_web/server.py`（现有 `/v1/chat/completions`）、`deepseek_web/models.py`、`deepseek_web/streaming.py`
> 目标：让 **OpenAI Codex CLI** 直接调用本项目，无需官方 API Key（延续「免 Key 驱动网页版 DeepSeek」的硬约束）

---

## 0. 背景与结论

### 0.1 为什么不兼容

| 维度 | 本项目现状 | Codex CLI 要求 | 结论 |
|------|-----------|---------------|------|
| 端点 | `POST /v1/chat/completions` | `POST /v1/responses` | ❌ 404 |
| 协议 | Chat Completions | **Responses API** | ❌ 结构不同 |
| 请求体 | `messages[]` | `input`（string 或 item 数组）+ `instructions` | ❌ 需转换 |
| 流式 | `data: {chat.completion.chunk}` + `[DONE]` | **命名 SSE 事件**：`response.created` / `response.output_text.delta` / `response.completed` | ❌ 需转换 |
| 配置 | —— | `wire_api = "responses"`（**唯一支持值**） | ❌ chat 已被移除 |

Codex CLI 自 2026-02 起**移除了 `wire_api = "chat"`**，只发送 Responses API 请求[citation:4][citation:10]。因此不能只改 `base_url` 指向本项目，必须在**本项目侧新增 `/v1/responses` 路由**，或在中间加一层转换代理。

### 0.2 选定方案

**在本项目 `deepseek_web/` 内新增 Responses 兼容层**（新文件 `responses.py` + `server.py` 加路由），**不改动**现有 `/v1/chat/completions`（Pi 等客户端继续用）。

```
Codex CLI ──(Responses API: /v1/responses)──▶ 本项目 responses.py（转换层）
                                                      │
                                                      ├─ 请求：Responses → Chat 内部模型
                                                      ├─ 复用：driver.send_chat() / build_prompt()
                                                      └─ 响应：Chat 结果 → Responses 对象 / 命名 SSE 事件
```

---

## 1. 端点清单

| 端点 | 方法 | 用途 | Codex 是否必需 |
|------|------|------|---------------|
| `/v1/responses` | POST | 核心对话（流式 + 非流式） | ✅ 必需 |
| `/v1/models` | GET | 已存在，Codex 启动时探测 | ✅ 已有 |
| `/healthz` | GET | 已存在 | 可选 |

> **注意**：Codex 的 provider ID `openai` / `ollama` / `lmstudio` 是保留字[citation:4]，配置时须用自定义名（如 `deepseekbridge`）。

---

## 2. 请求映射：Responses → 内部 Chat 模型

### 2.1 请求字段映射表

| Responses 字段 | 内部处理 | 说明 |
|---------------|---------|------|
| `model` | → `model` | 直接透传（如 `deepseek-chat`） |
| `input`（string） | → 一条 `user` 消息 | 简写形式 |
| `input`（item 数组） | → 遍历转 `ChatMessage` | 见 §2.2 |
| `instructions` | → 前置 `system` 消息 | Responses 的 system 指令 |
| `input` 中的 `input_image` | → 解码为临时图片文件，注入网页版附件（见 §2.4） | Codex 发图片（data URI） |
| `tools` | → `ChatCompletionRequest.tools` | 函数工具，格式需转换（见 §2.3） |
| `tool_choice` | → `tool_choice` | 透传 |
| `max_output_tokens` | → `max_tokens` | 语义一致[citation:1] |
| `stream` | → `stream` | 决定流式/非流式 |
| `temperature` / `top_p` | 接收但**不生效** | 网页版无法控制 |
| `previous_response_id` | 忽略（用本会话上下文） | 我们靠网页会话维持多轮 |
| `store` / `metadata` / `reasoning` | 忽略 | 网页版无对应能力 |

### 2.2 `input` item 转换

Responses 的 `input` 可以是字符串或 item 数组。item 常见类型：

```
{ "type": "message", "role": "user", "content": "文本" }
{ "type": "message", "role": "user", "content": [{"type":"input_text","text":"..."}] }
{ "type": "message", "role": "user", "content": [
    {"type":"input_text","text":"描述这张图"},
    {"type":"input_image","image_url":"data:image/png;base64,iVBOR..."}
  ] }
{ "type": "function_call", "call_id": "...", "name": "...", "arguments": "..." }
{ "type": "function_call_output", "call_id": "...", "output": "..." }
```

**转换规则**：
- `message` + `content`（string 或 `input_text` 数组）→ `ChatMessage(role, content=纯文本)`
- `message` 中同时含 `input_image` → 文本照常归一化，图片单独提取为附件（见 §2.4）
- `function_call` → `ChatMessage(role="assistant", tool_calls=[...])`
- `function_call_output` → `ChatMessage(role="tool", tool_call_id=call_id, content=output)`
- 未知类型 → 记日志跳过（宽松，不报 422）

> **约束**：`input_image` 只在 **user 角色** 的 content 里有效；出现在 tool-result 里的图片 Codex 会丢弃（上游限制）。

### 2.3 工具定义转换

Responses 工具格式与 Chat 不同：

```jsonc
// Responses
{ "type": "function", "name": "get_weather", "description": "...", "parameters": {...}, "strict": true }

// Chat Completions（本项目内部）
{ "type": "function", "function": { "name": "get_weather", "description": "...", "parameters": {...} } }
```

**转换**：把扁平字段包进 `function` 对象；`strict` 丢弃（网页版不支持）。

> **复用现有逻辑**：`toolcalls.py` 的 `parse_tool_calls()` 已能解析网页版回复中的工具调用，转换后可直接复用。

### 2.4 文件 / 图片上传（**方案 A：paste/drop 事件注入**）

#### 2.4.1 背景与范围

Codex **不调用** OpenAI 的 `/v1/files` 上传端点（该流程不支持）。它只把图片作为 **`input_image`（base64 data URI）** 放进 `input` 数组。本项目需把这张图**注入网页版 DeepSeek 的输入区**，让网页版自身的附件管线接管——这就是**方案 A**。

**支持范围（首版）**：

| 类型 | 支持 | 说明 |
|------|------|------|
| 图片（PNG/JPEG/WEBP/GIF） | ✅ | 走 `input_image` → 网页版附件 |
| 纯文本文件 | ✅（间接） | Codex 本地读取后作为文本包含在消息里，**不走上传** |
| PDF / Office | ❌ 首版 | 取决于网页版能力，风险高，留二期 |
| 音频 / 视频 | ❌ | 上游不支持 |

#### 2.4.2 处理流程

```
Codex: input[{type:input_image, image_url:"data:image/png;base64,iVBOR..."}]
   │
   ▼ responses.py
1. 解析 data URI → 提取 MIME + base64
2. 解码 → 写临时文件 /tmp/smsproxy_upload_<hex>.<ext>
3. 把文字 prompt 填入网页版输入框（复用 build_prompt）
4. 【方案 A】通过 WebView 注入图片：
   - 读临时文件 → base64 → 传入 JS
   - JS 侧：
       const bytes = Uint8Array.from(atob(B64), c => c.charCodeAt(0));
       const file  = new File([bytes], NAME, {type: MIME});
       const dt    = new DataTransfer();
       dt.items.add(file);
       // 优先 paste，失败再 drop
       const target = document.querySelector(INPUT_SELECTOR);
       target.dispatchEvent(new ClipboardEvent('paste', {
         bubbles: true, cancelable: true, clipboardData: dt
       }));
   - 等待网页版显示附件缩略图 / 文件名（轮询探测上传完成节点）
5. 点击发送
6. 取回复（复用现有轮询/结束判定）
7. 清理临时文件（finally）
```

#### 2.4.3 关键实现点

| 点 | 做法 |
|----|------|
| **优先 paste** | `ClipboardEvent('paste')` 携带 `DataTransfer`，最贴近用户真实操作，网页版附件管线最易接管 |
| **fallback drop** | paste 无效时，对输入区发 `DragEvent('drop')` + `dataTransfer` |
| **多图** | 一次 `DataTransfer` 加多个 `File`，或按序多次注入 |
| **上传完成探测** | 轮询附件缩略图 / 文件 chip 节点（选择器集中配置，见 §2.4.4） |
| **临时文件** | 写 `/tmp`，`finally` 删除；文件名保留原扩展名以助网页版判类型 |
| **大小限制** | 网页版限制（待实测），超限回 `E:` 提示而非静默失败 |

#### 2.4.4 新增选择器（集中配置）

```python
# config.py：网页版附件相关选择器（改版时只改这里）
ATTACHMENT_INPUT   = env_str("ATTACHMENT_INPUT",   'div[contenteditable="true"], textarea')
ATTACHMENT_PREVIEW = env_str("ATTACHMENT_PREVIEW", 'img[src^="blob:"], [class*="attachment"], [class*="upload"]')
```

#### 2.4.5 能力探测与降级

- **启动/首用时探测**：网页版是否存在「识图 / 上传」入口（`ATTACHMENT_INPUT` 可交互、或上传按钮存在）。
- **不支持图片时**：
  - **默认**：回 `E: 当前网页版不支持图片输入`，明确失败（不静默丢图）。
  - **可选降级**（二期）：接 VLM 把图片转文字描述后并入 prompt（多一个外部依赖）。
- **注入失败**（网页版改版 / 事件被拦）：回 `E: 图片注入失败，请检查网页版附件入口`，并记日志 + 截图便于修复。

#### 2.4.6 限制与风险

| 限制 / 风险 | 对策 |
|------------|------|
| `input_image` 仅在 **user 角色** 有效 | 非 user 角色的图片记日志丢弃 |
| 网页版识图模式与快速模式行为不同 | 探测当前模式；快速模式仅 OCR，行为差异记文档 |
| `paste`/`drop` 事件可能被网页版安全策略拦截 | fallback drop；仍失败则回 `E:` |
| 大图 base64 撑大请求 | 设大小上限；超限报错 |
| 临时文件残留 | `finally` 清理；启动时扫 `/tmp/smsproxy_upload_*` 兜底清理 |

> **为何不用 `<input type="file">` + `onShowFileChooser`（方案 B）**：WebView 无法直接设文件路径，需经 `onShowFileChooser` 回调 + 本地文件，链路更长、更易被网页版判为异常；且方案 A 更贴近真实用户操作。故首版只用 **方案 A**。

---

## 3. 响应映射：Chat 结果 → Responses 对象

### 3.1 非流式响应

```jsonc
{
  "id": "resp_<hex>",
  "object": "response",
  "created_at": 1700000000,
  "status": "completed",
  "model": "deepseek-chat",
  "output": [
    {
      "type": "message",
      "id": "msg_<hex>",
      "status": "completed",
      "role": "assistant",
      "content": [
        { "type": "output_text", "text": "回答正文", "annotations": [] }
      ]
    }
  ],
  "usage": {
    "input_tokens": 123,
    "output_tokens": 456,
    "total_tokens": 579
  }
}
```

**关键点**：
- `output` 是**数组**，文本在 `output[].content[].text`，**不能假设 `output[0]`**[citation:7]。
- 工具调用时，`output` 里放 `{type: "function_call", call_id, name, arguments}` 项。
- `usage` 字段名是 `input_tokens` / `output_tokens`（**不是** `prompt_tokens`）[citation:1]。

### 3.2 流式响应（**最容易踩坑**）

Codex 用 Rust 反序列化 SSE，是 `#[serde(tag = "type")]`，**每个事件的 data 载荷必须自带 `type` 字段**，且事件名要与 type 一致。发错会报 `stream disconnected before completion`。

**必须发的事件序列**：

```
event: response.created
data: {"type":"response.created","response":{...初始状态...},"sequence_number":0}

event: response.output_item.added
data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message",...},"sequence_number":1}

event: response.content_part.added
data: {"type":"response.content_part.added","item_id":"msg_...","output_index":0,"content_index":0,"part":{"type":"output_text","text":""},"sequence_number":2}

event: response.output_text.delta
data: {"type":"response.output_text.delta","item_id":"msg_...","output_index":0,"content_index":0,"delta":"文字片段","sequence_number":3}
...（多个 delta）

event: response.output_text.done
data: {"type":"response.output_text.done","item_id":"msg_...","output_index":0,"content_index":0,"text":"完整文字","sequence_number":N}

event: response.content_part.done
data: {"type":"response.content_part.done",...,"sequence_number":N+1}

event: response.output_item.done
data: {"type":"response.output_item.done",...,"sequence_number":N+2}

event: response.completed
data: {"type":"response.completed","response":{...完整对象含 usage...},"sequence_number":N+3}
```

> **注意**：`sequence_number` 必须单调递增，Codex 用它做顺序校验。

**保活**：本项目现有流式用 `: keep-alive\n\n` 注释。Responses 同样支持 SSE 注释，可沿用（Codex 会忽略注释行）。

### 3.3 工具调用的流式事件

若上游回复被解析为 `tool_calls`，则不发 `output_text.delta`，改发：

```
event: response.output_item.added
data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","call_id":"call_...","name":"get_weather","arguments":""},"sequence_number":1}

event: response.function_call_arguments.delta
data: {"type":"response.function_call_arguments.delta","item_id":"...","output_index":0,"delta":"{\"city\":","sequence_number":2}
...（分片发 arguments）

event: response.function_call_arguments.done
data: {"type":"response.function_call_arguments.done",...,"arguments":"{...}","sequence_number":N}

event: response.completed
data: {"type":"response.completed","response":{...status:"completed"...}}
```

---

## 4. Codex 侧配置

### 4.1 `~/.codex/config.toml`

```toml
model = "deepseek-chat"
model_provider = "deepseekbridge"
# 流式抗断（网页版生成慢，建议调大）
request_max_retries = 6
stream_max_retries = 8
stream_idle_timeout_ms = 600000

[model_providers.deepseekbridge]
name = "deepseekbridge / DeepSeek Web Bridge"
base_url = "http://127.0.0.1:8000/v1"   # 本项目监听地址
wire_api = "responses"                  # 必须；chat 已移除
env_key = "DEEPSEEK_BRIDGE_KEY"               # 本项目免 Key，填占位值即可
```

> **注意**：`model_provider` / `model_providers` **只在用户级 `~/.codex/config.toml` 生效**，项目级 `.codex/config.toml` 会被忽略并告警[citation:4]。

### 4.2 环境变量（占位即可）

```bash
export DEEPSEEK_BRIDGE_KEY="none"   # 本项目不做鉴权，仅满足 Codex 的 env_key 校验
```

### 4.3 启动本项目 + Codex

```bash
# 1) 启动本项目（确保 DeepSeek 网页版已登录）
cd /path/to/CLI4Chat
python -m deepseek_web.server          # 或 uvicorn / 现有启动脚本

# 2) 冒烟测试
codex exec --skip-git-repo-check "Reply with exactly: OK"
```

---

## 5. 实现计划

### 5.1 新增文件 `deepseek_web/responses.py`

```
responses.py
├── class ResponsesRequest(BaseModel)        # 宽松接收（extra="allow"）
├── class ResponsesTool(BaseModel)           # 工具定义
├── to_chat_request(req) -> ChatCompletionRequest   # 请求转换
├── extract_images(req) -> List[ImagePart]   # 提取 input_image（§2.4）
├── from_chat_response(reply, usage, model) -> dict  # 非流式响应构造
├── stream_responses(...)                    # 流式 SSE 生成器（命名事件）
│   ├── _evt(type, payload, seq) -> str      # 统一事件编码
│   └── 事件序列编排（§3.2 / §3.3）
└── 错误映射（复用 server._error_response 语义）

uploads.py（新增，配合 §2.4 方案 A）
├── class ImagePart / decode_data_uri()      # data URI → 字节 + MIME
├── save_temp_image() -> Path                # 写 /tmp 临时文件
├── cleanup_temp()                           # finally 清理 + 启动兜底
└── JS 注入脚本（paste/drop + DataTransfer）
```

### 5.2 `server.py` 改动（最小）

```python
from .responses import ResponsesRequest, handle_responses

@app.post("/v1/responses")
async def responses(
    request: ResponsesRequest,
    x_deepseek_session: Optional[str] = Header(None, alias=config.SESSION_KEY_HEADER),
    user_agent: Optional[str] = Header(None, alias="User-Agent"),
):
    return await handle_responses(request, x_deepseek_session, user_agent)
```

**复用点**（不重复造轮子）：
- `_session_key()`：会话分桶（含 UA 自动分桶）
- `driver.send_chat()` / `driver.needs_seed()` / `driver.sent_prompt()`
- `build_prompt()`、`parse_tool_calls()`、`estimate_tokens()`
- 错误类型：`DeepSeekContextLimitError` / `DeepSeekBusyError` / `DeepSeekTimeoutError`

### 5.3 错误映射（Responses 格式）

Responses API 的错误结构：
```json
{ "error": { "message": "...", "type": "...", "code": "..." } }
```

| 内部异常 | HTTP | type |
|---------|------|------|
| `DeepSeekContextLimitError` | 400 | `context_length_exceeded` |
| `DeepSeekBusyError` | 503 | `upstream_busy` |
| `DeepSeekTimeoutError` | 504 | `timeout` |
| 浏览器不可用 | 502 | `upstream_error` |
| 其他 | 500 | `server_error` |

---

## 6. 测试计划

新增 `tests/test_responses.py`：

| 用例 | 验证 |
|------|------|
| `test_string_input_maps_to_user_message` | `input="hi"` → 1 条 user 消息 |
| `test_instructions_become_system_message` | `instructions` → 前置 system |
| `test_input_item_array_conversion` | message / function_call / function_call_output 转换 |
| `test_tools_are_wrapped` | Responses 工具 → Chat 工具格式 |
| `test_input_image_is_extracted` | `input_image` → 提取为 ImagePart（MIME + 字节） |
| `test_invalid_data_uri_is_rejected` | 非法 data URI / 非图片 MIME → 明确报错 |
| `test_temp_file_cleanup` | 临时文件在 finally 被删除 |
| `test_image_injection_script` | 生成的 JS 含 paste + drop fallback + DataTransfer |
| `test_image_not_supported_returns_error` | 网页版不支持图片 → `E:` 明确失败 |
| `test_nonstream_response_shape` | `object=response`、`output[].content[].text`、`usage.input_tokens` |
| `test_stream_event_sequence` | 事件顺序 + **每个 data 含 `type`** + `sequence_number` 递增 |
| `test_stream_has_response_created_and_completed` | 首尾事件齐全 |
| `test_tool_call_stream_events` | function_call 事件序列 |
| `test_error_mapping` | 超时/繁忙/上下文超限 → 正确 status + type |

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| SSE 事件缺 `type` 字段 | Codex 报 `stream disconnected` | 统一 `_evt()` 编码；单测强制校验 |
| `sequence_number` 不递增 | 反序列化失败 | 生成器内维护计数器 |
| 网页版生成慢触发 Codex 超时 | 断流 | 配置 `stream_idle_timeout_ms=600000` + 发 keep-alive 注释 |
| Codex 发送未知 item 类型 | 转换崩溃 | 宽松跳过 + 记日志 |
| `instructions` 与网页版会话历史冲突 | 上下文重复 | `instructions` 仅在有历史时忽略（已有会话沿用） |
| 工具调用与网页版能力不匹配 | 工具不可用 | 首版先支持**纯文本对话**；工具调用作为二阶段 |
| 网页版 paste/drop 事件被拦 | 图片注入失败 | fallback drop；仍失败回 `E:` + 截图日志（§2.4.5） |
| `input_image` 出现在 tool-result | 图片被丢弃 | 记日志跳过（上游限制） |
| 大图 base64 撑大请求 | 内存/超时 | 设大小上限，超限报错 |
| 临时图片文件残留 | 磁盘堆积 | `finally` 清理 + 启动兜底扫描（§2.4.6） |

---

## 8. 分期

| 阶段 | 内容 | 可验证 |
|------|------|--------|
| **P1** | 非流式 `/v1/responses` + 纯文本 | `codex exec "OK"` 成功 |
| **P2** | 流式 SSE 命名事件 | Codex 交互模式正常逐字输出 |
| **P3** | 工具调用（function_call 事件） | Codex 能执行 shell 工具 |
| **P3.5** | **图片上传（方案 A：paste/drop 注入，§2.4）** | Codex 发图片 → 网页版识图并回答 |
| **P4** | 多轮上下文 + `previous_response_id` 优化 | 长会话不丢上下文 |

> **P1 是 MVP**：只要非流式能通，Codex 就能工作（只是无逐字输出）。建议先做 P1 验证链路，再做 P2 流式。

---

## 9. Pi 兼容性保障（**硬约束**）

> 本方案对 Pi Coding Agent 的现有使用**必须零影响**。Pi 走 `POST /v1/chat/completions`（`api: "openai-completions"`）+ `GET /v1/models`；Codex 走新增的 `POST /v1/responses`。以下规则在实施时**逐条遵守**。

### 9.1 隔离规则

| # | 规则 | 理由 |
|---|------|------|
| R1 | **不改动** `/v1/chat/completions` 的路由签名、响应模型、SSE 格式 | Pi 直接依赖 |
| R2 | **不改动** `/v1/models` 的返回结构 | Pi 的 `models.json` 发现依赖 |
| R3 | `/v1/responses` **新增独立文件** `responses.py`，不把 Responses 逻辑塞进 `server.py` 的 chat 分支 | 避免污染现有路径 |
| R4 | 共享组件（`driver` / `_session_key` / `prompting` / `toolcalls`）**只读复用**，不修改其签名与行为 | 一处改动可能同时影响两条路径 |
| R5 | 若必须改共享组件，**先跑 Pi 路径的现有测试**（`tests/test_routes.py`、`test_multi_session.py`）确认全绿 | 回归防护 |
| R6 | 新增配置项（`ATTACHMENT_*` 等）**给默认值**，不设默认值时不得让 chat 路径受影响 | 配置缺失不崩 |
| R7 | 图片注入是**Codex 路径专属**，不在 chat 路径触发 | Pi 不发 `input_image` |

### 9.2 共享组件清单（改动需谨慎）

| 组件 | Pi 是否依赖 | Codex 如何使用 | 改动规则 |
|------|-----------|--------------|----------|
| `driver.send_chat()` | ✅ | 复用 | 只调用，不改签名 |
| `driver.needs_seed()` / `sent_prompt()` | ✅ | 复用 | 只调用 |
| `_session_key()` | ✅ | 复用（含 UA 分桶） | 只调用；若扩展须保持默认行为 |
| `prompting.build_prompt()` | ✅ | 复用 | 只调用 |
| `toolcalls.parse_tool_calls()` | ✅ | 复用 | 只调用 |
| `streaming._stream_chat_completion()` | ✅ | **不复用**（Codex 用 `stream_responses()`） | 不改动 |
| `models.py` 的数据模型 | ✅ | 可能新增 Responses 模型 | **新增**，不改现有类 |
| `config.py` | ✅ | 新增键（带默认值） | 新增不改旧 |

### 9.3 验证清单（实施后必跑）

```bash
# 1) Pi 路径全量回归必须全绿（零影响的核心证据）
python -m unittest discover -s tests

# 2) Pi 冒烟：chat/completions 流式 + 非流式
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}],"stream":true}'

# 3) Pi 客户端实测
pi --provider deepseek-web --model deepseek-chat

# 4) Codex 冒烟（新增路径）
codex exec --skip-git-repo-check "Reply with exactly: OK"
```

### 9.4 当前状态

- **本文档为设计稿，尚未写任何实现代码** → 对 Pi **当前零影响**。
- 仓库当前唯一改动是新增 `doc/codex_support.md`（未跟踪文件），**不涉及** `deepseek_web/` 任何代码。
- 后续实施 PR 必须附「§9.3 验证清单」的执行结果。

---

## 附：与现有架构的关系

```
Codex CLI ─┐
           ├─▶ /v1/responses ──▶ responses.py ──┐
Pi / 其他 ─┘                    └─ uploads.py（图片注入，方案 A）
           └─▶ /v1/chat/completions ──────────▶│──▶ driver.send_chat() ──▶ DeepSeek 网页版
                                                 │        ▲
                                                 └────────┘ paste/drop 注入图片附件
                    （两条路径共用 driver / 会话分桶 / 工具解析）
```

- **不改动** `/v1/chat/completions`，Pi 等客户端零影响。
- **新增** `/v1/responses`，Codex 专用。
- 两条路径**共享** `driver`、`_session_key()`、`toolcalls.py`、`prompting.py`，无重复实现。
- 延续**免 API Key** 硬约束。
