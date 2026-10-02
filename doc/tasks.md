# Codex 支持 · 任务清单（tasks.md）

> 依据：`doc/codex_support.md`（设计稿 v1.0）
> 目标：让 Codex CLI 通过 `/v1/responses` 调用本项目，**且对 Pi 零影响**
> 硬约束：见 codex_support.md §9（Pi 兼容性保障）

---

## 图例

- `[ ]` 未开始 · `[~]` 进行中 · `[x]` 完成
- **验收** = 该任务的完成标准；**回归** = 必跑的 Pi 影响检查

---

## P1 · 非流式 `/v1/responses` + 纯文本（MVP）

### T1.1 新增 `deepseek_web/responses.py` 骨架
- [x] `ResponsesRequest`（`extra="allow"`）、`ResponsesTool` 模型
- [x] `handle_responses(req, session_key, ua)` 入口函数
- **验收**：模块可导入，`ResponsesRequest` 能宽松接收未知字段
- **回归**：不改 `models.py` 现有类

### T1.2 请求转换 `to_chat_request()`
- [x] `input`（string）→ 单条 user 消息
- [x] `input`（item 数组）→ 遍历转 `ChatMessage`
- [x] `instructions` → 前置 system 消息
- [x] `max_output_tokens` → `max_tokens`；`stream` → `stream`
- [x] 未知 item 类型 → 记日志跳过（不报 422）
- **验收**：`test_string_input_maps_to_user_message`、`test_instructions_become_system_message`、`test_input_item_array_conversion`

### T1.3 响应构造 `from_chat_response()`
- [x] 输出 `object=response`、`status=completed`
- [x] `output[].content[].text` 结构（不假设 `output[0]`）
- [x] `usage.input_tokens` / `output_tokens` / `total_tokens`
- **验收**：`test_nonstream_response_shape`

### T1.4 错误映射
- [x] `DeepSeekContextLimitError` → 400 `context_length_exceeded`
- [x] `DeepSeekBusyError` → 503 `upstream_busy`
- [x] `DeepSeekTimeoutError` → 504 `timeout`
- [x] 浏览器不可用 → 502 `upstream_error`；其他 → 500 `server_error`
- **验收**：`test_error_mapping`

### T1.5 `server.py` 新增路由
- [x] `@app.post("/v1/responses")`，复用 `_session_key()`（含 UA 分桶）
- [x] **不改动** `/v1/chat/completions` 与 `/v1/models`
- **验收**：`GET /` 路由表含 `/v1/responses`；旧路由行为不变
- **回归**：`python -m unittest discover -s tests` 全绿

### T1.6 P1 端到端验证
- [ ] `codex exec --skip-git-repo-check "Reply with exactly: OK"` 成功
- **回归**：Pi 冒烟（`/v1/chat/completions` 流式）正常

---

## P2 · 流式 SSE 命名事件

### T2.1 事件编码器 `_evt()`
- [x] 每个 data 载荷**自带 `type` 字段**（Codex Rust `#[serde(tag="type")]` 要求）
- [x] `sequence_number` 单调递增
- **验收**：`test_stream_event_sequence`（含 `type` 字段 + 递增）

### T2.2 事件序列编排
- [x] `response.created` → `output_item.added` → `content_part.added` → `output_text.delta`* → `output_text.done` → `content_part.done` → `output_item.done` → `response.completed`
- [x] keep-alive 注释（`: keep-alive\n\n`）抗慢生成
- **验收**：`test_stream_has_response_created_and_completed`

### T2.3 流式接入 `handle_responses`
- [x] `stream=true` → `StreamingResponse(stream_responses(...))`
- [x] 复用 `driver.send_chat()` 的 `on_delta` 回调
- **验收**：Codex 交互模式逐字输出
- **回归**：`streaming.py` **不改动**（Pi 流式零影响）

### T2.4 Codex 侧抗断配置验证
- [ ] `~/.codex/config.toml` 配 `stream_idle_timeout_ms=600000`、重试参数
- **验收**：长回答不断流

---

## P3 · 工具调用（function_call 事件）

### T3.1 工具定义转换
- [x] Responses 扁平工具 → Chat 嵌套 `function` 格式；`strict` 丢弃
- **验收**：`test_tools_are_wrapped`

### T3.2 tool_calls 流式事件
- [x] `function_call` 项 → `response.output_item.added`（type=function_call）
- [x] `response.function_call_arguments.delta` / `.done`
- **验收**：`test_tool_call_stream_events`

### T3.3 非流式工具调用
- [x] `output` 放 `function_call` 项；`parse_tool_calls()` 复用
- **验收**：Codex 能执行 shell 工具
- **回归**：`toolcalls.py` **只调用不改**

---

## P3.5 · 图片上传（方案 A：paste/drop 注入）

### T3.5.1 `uploads.py` 基础
- [ ] `decode_data_uri()` → 字节 + MIME；`save_temp_image()` 写 `/tmp`
- [ ] `cleanup_temp()` finally 清理 + 启动兜底扫描
- **验收**：`test_temp_file_cleanup`、`test_invalid_data_uri_is_rejected`

### T3.5.2 提取 `input_image`
- [ ] `extract_images(req)` 从 user content 提取；tool-result 里的图片丢弃并记日志
- **验收**：`test_input_image_is_extracted`

### T3.5.3 JS 注入脚本（方案 A）
- [ ] `DataTransfer` + `File` 构造；优先 `ClipboardEvent('paste')`，fallback `DragEvent('drop')`
- [ ] 轮询附件缩略图探测上传完成
- **验收**：`test_image_injection_script`（含 paste + drop + DataTransfer）

### T3.5.4 选择器与能力探测
- [ ] `config.py` 新增 `ATTACHMENT_INPUT` / `ATTACHMENT_PREVIEW`（带默认值）
- [ ] 探测网页版是否支持图片；不支持回 `E:`
- **验收**：`test_image_not_supported_returns_error`
- **回归**：新配置**带默认值**，不影响 chat 路径

### T3.5.5 端到端图片问答
- [ ] Codex 发图片 → 网页版识图 → 回答
- **验收**：手动验证 + 截图日志

---

## P4 · 多轮上下文 + 优化

### T4.1 多轮上下文
- [ ] 复用网页会话（`_session_key`）；`instructions` 仅在有历史时忽略
- **验收**：长会话不丢上下文

### T4.2 `previous_response_id` 优化（可选）
- [ ] 评估是否需要（本项目靠网页会话维持多轮，可能不需要）
- **验收**：设计决策记录

### T4.3 文档与示例
- [ ] README 增补 Codex 接入章节
- [ ] `.env.example` 增补新配置项
- **验收**：新用户可照文档跑通

---

## 横切任务（贯穿所有阶段）

### X1 Pi 回归（每阶段必跑）
- [ ] `python -m unittest discover -s tests` 全绿
- [ ] `/v1/chat/completions` 流式冒烟正常
- [ ] `pi --provider deepseek-web --model deepseek-chat` 实测
- **触发时机**：任何触及共享组件（driver / _session_key / prompting / toolcalls / config）的改动后

### X2 共享组件只读复用
- [ ] `driver.send_chat()` / `needs_seed()` / `sent_prompt()`：只调用不改签名
- [ ] `_session_key()` / `build_prompt()` / `parse_tool_calls()`：只调用
- [ ] `streaming.py`：**不改动**
- [ ] `models.py`：只新增，不改现有类
- **验收**：`git diff` 中这些文件无签名/行为改动

### X3 测试新增
- [x] `tests/test_responses.py`（§6 P1–P3 用例；图片 T3.5 用例待 P3.5 实现）
- [x] 每个新用例覆盖一个设计点

### X4 文档同步
- [ ] `.env.example` / README 随实现更新
- [ ] `codex_support.md` 与实现偏差回填

---

## 完成定义（DoD）

一个阶段「完成」须同时满足：
1. 该阶段所有任务 `[x]`
2. 新增测试全绿（`test_responses.py`）
3. **Pi 回归全绿**（§9.3 四步）
4. `git diff` 中共享组件无破坏性改动
5. 文档同步

---

## 关键依赖与顺序

```
P1（非流式）──▶ P2（流式）──▶ P3（工具）──▶ P3.5（图片）──▶ P4（优化）
   │              │              │              │
   └──────────────┴──────────────┴──────────────┘
              每阶段结束跑 X1（Pi 回归）
```

- **P1 是 MVP**：非流式跑通即验证链路；先做 P1 再做 P2。
- **P3.5 依赖 P2**（图片仍需流式/非流式取答，复用取答逻辑）。
- **X1 是门禁**：任一阶段若 Pi 回归失败，禁止进入下一阶段。
