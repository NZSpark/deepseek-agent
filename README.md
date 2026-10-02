# DeepSeek Web-to-API Bridge

把 **DeepSeek 网页版（chat.deepseek.com）** 包装成一个 **OpenAI 兼容的本地 API 服务**，让任何支持 OpenAI 协议的客户端（Pi Coding Agent、OpenAI SDK、LangChain、Cline 等）都能直接使用 DeepSeek 网页版的能力——包括 **流式输出** 和 **function calling（工具调用）**。

> 本项目通过 Playwright 驱动一个真实的 Chromium 浏览器，复用本地登录态，把网页对话“桥接”成标准 `/v1/chat/completions` 接口。无需官方 API Key。

---

## ✨ 特性

- **OpenAI 兼容接口**：完整实现 `/v1/models` 与 `/v1/chat/completions`，支持 `messages`、`tools`、`stream` 等标准字段；错误也以 OpenAI 兼容的 `error` 结构返回。
- **可运维**：`GET /healthz` 健康检查、`HEADLESS` 无头模式、`/debug/dom` DOM 诊断端点（需 `DEEPSEEK_DEBUG=1`，且不回显正文）。
- **会话生命周期**：自动识别网页版「对话长度上限」（不再伪装成超时），超预算时自动轮转到新会话，并**播种**已有上下文。
- **流式响应（SSE）**：以 `text/event-stream` 逐字吐出内容，兼容 OpenAI 流式解析器。
- **模拟 Function Calling**：网页版本身不支持 function calling，本项目通过「提示词注入 + 结构化解析」模拟出 OpenAI 的 `tool_calls` 语义。
- **代码块自动落盘**：直接从网页 DOM 的 `<pre><code>` 提取代码，自动按语言保存为 `.py` / `.js` / `.json` 等文件到 `output/`。
- **登录态持久化**：基于 `launch_persistent_context`，登录一次即可长期复用（`user_data/` 目录）。
- **宽松字段校验**：对客户端发来的未知字段（`temperature`、`reasoning_effort`、内容分片数组等）全量兼容，绝不返回 422。
- **健壮的错误提示**：浏览器 profile 被占用、等待超时等情况都会给出可操作的提示。

---

## 📁 项目结构

```
.
├── deepseek_api_server.py   # 入口（薄封装）：重新导出历史公开名字并启动服务
├── deepseek_web/            # 真正的实现
│   ├── config.py            #   .env 加载 + 全部可调参数（超时 / 重试 / 选择器 / 路径）
│   ├── models.py            #   OpenAI 兼容的 Pydantic 数据模型
│   ├── toolcalls.py         #   工具注入与解析（模拟 function calling）
│   ├── prompting.py         #   消息数组 -> 网页输入框文本
│   ├── driver.py            #   Playwright 浏览器 Driver + 会话持久化
│   ├── streaming.py         #   SSE 流式编码
│   └── server.py            #   FastAPI 应用与路由
├── client_test.py           # 使用官方 openai SDK 测试本地服务的示例客户端
├── requirements.txt         # 运行时依赖（含 client_test.py 需要的 openai）
├── .env.example             # 配置模板（复制为 .env）
├── INSTALL.md               # 安装说明
├── cmdlog.md                # 环境搭建 / 运行 / 测试命令备忘
├── tests/                   # 解析层、结束判定、会话生命周期、路由层的回归测试（stdlib unittest）
├── doc/update.md            # 项目改进建议与进度
├── output/                  # 自动提取的代码 / 回复文件输出目录
├── user_data/               # Chromium 持久化用户目录（保存登录态，勿提交到 git）
└── .venv/                   # Python 虚拟环境
```

---

## 🚀 快速开始

### 1. 环境准备

```bash
python3 -m venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
playwright install chromium
```

> 若未安装 `uv`，可将 `uv pip install ...` 替换为 `pip install ...`。依赖统一以 `requirements.txt` 为准。

### 2. 启动服务

```bash
python deepseek_api_server.py
```

启动后会弹出一个 Chromium 窗口并打开 `https://chat.deepseek.com/`。
**首次运行时请在窗口内手动完成登录**，登录态会保存到 `user_data/`，之后无需重复登录。

服务默认监听：`http://127.0.0.1:8000`

> ⚠️ **同一时间只能运行一个实例**。因为 Chromium 的持久化 profile 无法被两个进程共用，重复启动会报「profile is already in use」。请先结束旧实例：`pkill -f deepseek_api_server.py`。

### 3. 调用测试

```bash
python client_test.py
```

或直接用 `curl`：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "用 Python 写一个 FastAPI Hello World。"}]
  }'
```

---

## 🔌 接口说明

### `GET /v1/models`

模型发现端点，返回可用模型列表（供 Pi 等客户端的 `models.json` 使用）。

```json
{
  "object": "list",
  "data": [
    { "id": "deepseek-chat", "object": "model", "owned_by": "deepseek-web-bridge" },
    { "id": "deepseek-reasoner", "object": "model", "owned_by": "deepseek-web-bridge" }
  ]
}
```

### `POST /v1/chat/completions`

标准 OpenAI Chat Completions 接口。

**请求字段（常用）**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `model` | string | 模型 id，默认 `deepseek-chat` |
| `messages` | array | 标准 OpenAI 消息数组，支持 `system` / `user` / `assistant` / `tool` |
| `stream` | bool | 是否流式返回，默认 `false` |
| `tools` | array | OpenAI tools 描述，用于触发模拟 function calling |
| `tool_choice` | any | 设为 `"none"` 可禁用工具调用 |
| `save_files` | bool | **本地扩展**，是否把提取到的代码落盘，默认 `true` |
| `output_dir` | string | **本地扩展**，输出目录，默认 `./output` |

其余标准字段（`reasoning_effort`、未知字段等）均会被接收，不会返回 422。

> ⚠️ 网页版无法控制生成参数，因此 `temperature` / `top_p` / `max_tokens` / `stop` 会被接收但**不生效**（仅作兼容占位）；`stream_options.include_usage` 有效，会在流末尾附上 usage。

**错误响应**：返回 OpenAI 兼容的 `{"error": {"message", "type", "code"}}`，而不是裸字符串。

| HTTP | `type` | 含义 |
| --- | --- | --- |
| 400 | `invalid_request_error` | 请求不合法（缺 messages 等） |
| 400 | `context_length_exceeded` | 网页会话已达上下文上限（正常会自动轮转，仍失败时才返回） |
| 502 | `upstream_error` | 上游浏览器不可用 / 找不到输入框 |
| 503 | `unavailable` | 浏览器尚未就绪（未登录或 profile 被占用） |
| 503 | `upstream_busy` | 同一会话桶已有请求在跑，等锁超过 `BUCKET_LOCK_TIMEOUT_S`（只在该值 >0 时出现） |
| 504 | `timeout` | 等待网页版回复超时（已按重试阶梯重试过） |
| 500 | `server_error` | 其他未预期错误 |

**非流式响应示例**

```json
{
  "id": "chatcmpl-xxxxxxxxxxxx",
  "object": "chat.completion",
  "model": "deepseek-chat",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "..." },
      "finish_reason": "stop"
    }
  ],
  "usage": { "prompt_tokens": 12, "completion_tokens": 88, "total_tokens": 100 },
  "saved_files": ["output/code_1790891215_1.py"]
}
```

**流式响应**：以 `data: {...}\n\n` 的 SSE 格式输出，最后以 `data: [DONE]` 结束；等待模型生成期间会发送 `: keep-alive` 注释保活。

**按任务隔离会话（可选请求头）**

| 请求头 | 说明 |
| --- | --- |
| `X-DeepSeek-Session` | 任务标识。同一取值的请求共用一条网页会话，不同取值各自持有独立会话与页面，互不污染上下文；不传则使用默认会话（旧行为） |

也可用请求体的 `user` 字段代替该请求头。请求头名可用 `.env` 的 `SESSION_KEY_HEADER` 改名，`SESSION_SCOPING=false` 可整体关闭分桶。

### `POST /session/reset`

**本地扩展**，手动逃生口：让指定会话的下一轮开新会话（历史会照旧**播种**回来，不丢上下文）。

```bash
curl -X POST "http://127.0.0.1:8000/session/reset?session=pi-task-1"
# 省略 session 参数则重置默认会话
```

响应会返回该会话桶的状态快照（等同 `/healthz` 里的 `session` 字段）。

---

## 🛠 Function Calling 原理

DeepSeek 网页版不支持原生 function calling，本项目采用三步模拟：

1. **注入**：把客户端的 `tools` 描述转成自然语言指令（[`format_tools_instruction`](deepseek_api_server.py)），追加到 prompt 末尾，要求模型用 ```` ```tool_call ```` 代码块回话。
2. **解析**：从模型回复中解析工具调用（[`parse_tool_calls`](deepseek_api_server.py)）。解析器兼容两种形态：
   - 带围栏的 ```` ```tool_call ... ``` ```` 代码块；
   - **无围栏**的 `tool_call` 标签 + 裸 JSON（网页 DOM 提取后的常见形态，代码块被渲染成 `<pre>`，围栏退化为标题文字）。

   同时使用平衡括号扫描正确处理字符串与转义，并按合法工具名过滤误报。
3. **回填**：下一轮请求中，客户端发回的 `role: "tool"` 执行结果会被拼回 prompt 再喂给网页版。

**会话上下文优化**：网页版本身是持续存在的会话，因此 [`build_prompt`](deepseek_api_server.py) 只发送「最后一条 assistant 消息之后」的新增消息，而非每轮重发全部历史。

---

## ⚙️ 使用技巧与注意事项

- **保持浏览器窗口打开**：服务依赖浏览器实例，请不要关闭自动弹出的 Chromium 窗口。
- **选择器适配**：网页版改版时只需改 [`deepseek_web/config.py`](deepseek_web/config.py) 里集中定义的 `RESPONSE_SELECTORS` / `INPUT_SELECTORS` / `READY_SELECTOR` / `CODE_BLOCK_SELECTOR`（也可直接用 `.env` 覆盖，不改代码）。
- **改代码后注意**：所有可调参数都在 [`deepseek_web/config.py`](deepseek_web/config.py)，运行期按 `config.<NAME>` 取属性；因此改写入口模块 `deepseek_api_server.RESPONSE_TIMEOUT_S` 之类**不会生效**，请改 `.env` 或 `deepseek_web.config` 模块属性。
- **结束判定**：以「最后一条回复的内容是否变化」判断本轮回复是否出现（**不能用回复节点数量**：长会话下 DeepSeek 会回收/替换节点，数量可能恒定不变，实测恒为 2）。结束后先用页面「生成中」状态收尾，识别不到该控件时退回内容稳定判定（文本相同连续 2 次，或长度不再增长连续 4 次）。
- **超时**：单轮生成总超时默认 180 秒，可用环境变量 `DEEPSEEK_TIMEOUT` 覆盖（必须小于 Pi 侧 HTTP 客户端的超时，否则客户端会先报错）。若超时前已读到回复内容，会直接返回该内容而**不重发**；只有页面上完全没有产生新回复时才视为发送失败，恢复会话后重试一次。客户端建议设置较长 timeout（`client_test.py` 中为 240s）。
- **重试**：只有「等待超时」才会重试，最多 `DEEPSEEK_RETRIES` 次（默认 2），每次先尝试恢复会话再退避重试；找不到输入框、profile 被占用等属于不可重试，直接返回错误。
- **无头运行**：已登录过之后可用 `HEADLESS=1` 启动（适合 CI / 无显示环境）；首次登录必须有头模式。
- **健康检查**：`GET /healthz` 返回浏览器是否就绪、当前会话地址、会话状态（`session`）、在用会话桶（`session_keys`）与初始化错误，便于客户端探活。
- **诊断**：`DEEPSEEK_DEBUG=1` 启动后，每轮轮询都会打印节点数 / 文本长度 / 稳定计数 / 生成状态，可直接看出结束判定是否生效。`GET /debug/dom` 会返回回复节点的 class / 长度 / sha1 与疑似停止按钮控件结构 —— **不包含任何正文**，且只在 `DEEPSEEK_DEBUG=1` 时才注册（否则返回 404）。
- **流式语义**：不带 `tools` 时边生成边吐字；带 `tools` 时必须先缓冲完整回复才能判断是不是 `tool_calls`，因此调用方在生成期间只会收到 `: keep-alive` 注释，随后一次性收到内容或 tool_calls。
- **会话生命周期**（重要）：服务默认**复用同一个网页会话**，因此模型看到的是累积上下文，与客户端的 `contextWindow` 无关。为了不让它无限增长：
  - 超过 `SESSION_MAX_TURNS` / `SESSION_MAX_TOKENS`（默认 60 轮 / 6 万估算 token）时，下一轮自动**轮转**到新会话；
  - 轮转后会把**历史重新播种**进去（`SEED_MAX_CHARS` 控制字符预算，超出时保留最近的消息），所以轮转不会丢失任务上下文；
  - 网页版到顶时会弹出提示并停止响应，服务会识别它并返回 `context_length_exceeded`，而不是死等到超时；重试阶梯的**最后一级**就是“换新会话 + 重放历史”；
  - 想手动换一个干净会话：`POST /session/reset`、设 `DEEPSEEK_NEW_SESSION=true` 后重启，或删除 `user_data/.deepseek_session`；
  - 想知道当前会话涨到哪了：看 `GET /healthz` 的 `session` 字段（`turns` / `est_tokens` / `cap_hit` / `pending_rotation`）。
- **按任务隔离会话**：默认所有请求共用一条网页会话。若同时跑多个任务（例如多个 Pi 会话），给每个任务带一个 `X-DeepSeek-Session: <任务 id>` 请求头，服务会为每个 id 维护独立的网页会话与页面；状态存在 `user_data/.deepseek_session` 里，默认会话仍在文件顶层、其余在 `sessions` 下。
  - **桶页面上限**：`MAX_SESSION_BUCKETS`（默认 8）。超出时不会报错，而是关闭**最久未用**的那条页面（`BUCKET_IDLE_TTL_S` 秒内没有任何请求的页面也会被关掉）。被关掉不等于丢上下文：会话状态还在，下次用到时会重新打开同一个会话并按需播种。设 `0` 表示不允许额外桶（一律走默认会话）。
  - ⚠️ **分桶 ≠ 并发**：默认所有桶共用一把锁，请求仍然**串行**执行（分桶只提供上下文隔离）。确实需要并行时设 `PARALLEL_BUCKETS=true` 按桶加锁，但这会同时驱动多个网页会话，**可能触发风控**，请自行评估。
  - **多 Agent 同时访问**：给每个 Agent 一个独立的会话标识，并打开并发。推荐配置：

    ```
    SESSION_SCOPING=true
    PARALLEL_BUCKETS=true
    MAX_SESSION_BUCKETS=3          # >= 同时访问的 Agent 数
    BUCKET_LOCK_TIMEOUT_S=15       # 同一会话桶排队上限（秒）；0 = 一直等
    ```

    这样 3 个 Agent 会各自驱动一条网页会话、**真正并行**，互不阻塞；上下文与 usage 都按桶隔离。`BUCKET_LOCK_TIMEOUT_S>0` 时，若有请求落到**已被占用的同一个桶**（例如客户端重试堆叠），它会快速返回 503 `upstream_busy` 而不是无限排队、拖到客户端自己超时；设 `0` 则一直等待（旧行为）。用 `GET /healthz` 的 `cluster` 字段可看到 `parallel` / `max_buckets` / `busy`（正在处理的桶）/ `open_pages`。
- **代码落盘**：当回复中包含代码块时，会从 DOM 提取并按语言保存；若无代码块则保存完整回复为 `.md`。
- **不要提交 `user_data/`**：其中包含登录 Cookie / Session，属于敏感数据。
- **不要绑定 `0.0.0.0`**：服务默认只监听 `127.0.0.1`，因为转发的是你的登录会话，暴露到网络等于把账号交出去。

---

## 🧪 测试

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

共 149 个用例，覆盖解析层、结束判定、会话生命周期（播种 / 到顶 / 轮转 / 重试阶梯 / 会话桶 / 页面回收 / 锁）、模块结构与路由层。全部用假 page / 假 driver 驱动，不需要启动浏览器，也不需要额外依赖（只用标准库 unittest）。

---

## 🧩 在 Pi Coding Agent 中接入

在 Pi 的配置文件中新增一个 provider，指向本地服务即可：

pi --provider deepseek-web --model deepseek-chat

建议给每个 Agent 任务带一个固定的 `X-DeepSeek-Session` 请求头（或让客户端填 `user` 字段），这样多个任务各自持有独立会话，不会互相污染上下文。

```json
{
  "providers": {
    "deepseek-web": {
      "baseUrl": "http://127.0.0.1:8000/v1",
      "api": "openai-completions",
      "apiKey": "none",
      "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
      "models": [
        { "id": "deepseek-chat", "name": "DeepSeek Chat (Web)", "input": ["text"], "contextWindow": 256000, "maxTokens": 65535 }
      ]
    }
  }
}
```

---

## 📄 License

本项目仅供学习与个人研究使用。请遵守 DeepSeek 的服务条款，勿用于商业用途或高频滥用。
