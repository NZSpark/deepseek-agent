# DeepSeek Web-to-API Bridge

把 **DeepSeek 网页版（chat.deepseek.com）** 包装成一个 **OpenAI 兼容的本地 API 服务**，让任何支持 OpenAI 协议的客户端（Pi Coding Agent、OpenAI SDK、LangChain、Cline 等）都能直接使用 DeepSeek 网页版的能力——包括 **流式输出** 和 **function calling（工具调用）**。

> 本项目通过 Playwright 驱动一个真实的 Chromium 浏览器，复用本地登录态，把网页对话“桥接”成标准 `/v1/chat/completions` 接口。无需官方 API Key。

---

## ✨ 特性

- **OpenAI 兼容接口**：完整实现 `/v1/models` 与 `/v1/chat/completions`，支持 `messages`、`tools`、`stream` 等标准字段；错误也以 OpenAI 兼容的 `error` 结构返回。
- **可运维**：`GET /healthz` 健康检查、`HEADLESS` 无头模式、`/debug/dom` DOM 诊断端点。
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
├── deepseek_api_server.py   # 核心：FastAPI 服务 + OpenAI 兼容层 + 浏览器 Driver
├── deepseek_agent.py        # 独立的 Playwright 脚本示例（脱离 API，直接驱动网页对话）
├── client_test.py           # 使用官方 openai SDK 测试本地服务的示例客户端
├── requirements.txt         # 运行时依赖（含 client_test.py 需要的 openai）
├── INSTALL.md               # 安装说明
├── cmdlog.md                # 环境搭建 / 运行 / 测试命令备忘
├── tests/                   # 解析层与结束判定的回归测试（stdlib unittest）
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

**错误响应**：返回 OpenAI 兼容的 `{"error": {"message", "type", "code"}}`，而不是裸字符串。`timeout` → 504，上游浏览器不可用 → 502，请求非法 → 400。

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
- **选择器适配**：网页版改版时只需改文件顶部集中定义的 `RESPONSE_SELECTORS` / `INPUT_SELECTORS` / `READY_SELECTOR` / `CODE_BLOCK_SELECTOR`。
- **结束判定**：以「最后一条回复的内容是否变化」判断本轮回复是否出现（**不能用回复节点数量**：长会话下 DeepSeek 会回收/替换节点，数量可能恒定不变，实测恒为 2）。结束后先用页面「生成中」状态收尾，识别不到该控件时退回内容稳定判定（文本相同连续 2 次，或长度不再增长连续 4 次）。
- **超时**：单轮生成总超时默认 180 秒，可用环境变量 `DEEPSEEK_TIMEOUT` 覆盖（必须小于 Pi 侧 HTTP 客户端的超时，否则客户端会先报错）。若超时前已读到回复内容，会直接返回该内容而**不重发**；只有页面上完全没有产生新回复时才视为发送失败，恢复会话后重试一次。客户端建议设置较长 timeout（`client_test.py` 中为 240s）。
- **重试**：只有「等待超时」才会重试，最多 `DEEPSEEK_RETRIES` 次（默认 2），每次先尝试恢复会话再退避重试；找不到输入框、profile 被占用等属于不可重试，直接返回错误。
- **无头运行**：已登录过之后可用 `HEADLESS=1` 启动（适合 CI / 无显示环境）；首次登录必须有头模式。
- **健康检查**：`GET /healthz` 返回浏览器是否就绪、当前会话地址与初始化错误，便于客户端探活。
- **诊断**：`DEEPSEEK_DEBUG=1` 启动后，每轮轮询都会打印节点数 / 文本长度 / 稳定计数 / 生成状态，可直接看出结束判定是否生效；`GET /debug/dom` 会返回页面上真实的回复节点（含 class / 长度 / sha1）与疑似停止按钮控件结构。
- **流式语义**：不带 `tools` 时边生成边吐字；带 `tools` 时必须先缓冲完整回复才能判断是不是 `tool_calls`，因此调用方在生成期间只会收到 `: keep-alive` 注释，随后一次性收到内容或 tool_calls。
- **代码落盘**：当回复中包含代码块时，会从 DOM 提取并按语言保存；若无代码块则保存完整回复为 `.md`。
- **不要提交 `user_data/`**：其中包含登录 Cookie / Session，属于敏感数据。
- **不要绑定 `0.0.0.0`**：服务默认只监听 `127.0.0.1`，因为转发的是你的登录会话，暴露到网络等于把账号交出去。

---

## 🧪 测试

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

回归测试用假 page 驱动结束判定，不需要启动浏览器，也不需要额外依赖（只用标准库 unittest）。

---

## 🧩 在 Pi Coding Agent 中接入

在 Pi 的配置文件中新增一个 provider，指向本地服务即可：

```json
{
  "providers": {
    "deepseek-web": {
      "baseUrl": "http://127.0.0.1:8000/v1",
      "api": "openai-completions",
      "apiKey": "none",
      "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
      "models": [
        { "id": "deepseek-chat", "name": "DeepSeek Chat (Web)", "input": ["text"], "contextWindow": 65536, "maxTokens": 8192 }
      ]
    }
  }
}
```

---

## 📄 License

本项目仅供学习与个人研究使用。请遵守 DeepSeek 的服务条款，勿用于商业用途或高频滥用。
