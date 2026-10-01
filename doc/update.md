# 项目改进建议 (doc/update.md)

> 基于对当前代码库的审查（`deepseek_api_server.py` / `deepseek_agent.py` / `client_test.py` / README / 配置）。
> 按优先级分层，P0 为正确性/可用性阻塞项，P1 为健壮性，P2 为工程质量。

---

## 一、项目现状概览

| 项 | 状态 |
| --- | --- |
| 核心服务 `deepseek_api_server.py` | 单文件 ~850 行，含数据模型 / 解析层 / 浏览器 Driver / FastAPI 路由 |
| OpenAI 兼容 | `/v1/models`、`/v1/chat/completions`（含 SSE 流式）|
| Function Calling | 提示词注入 + 结构化解析模拟 |
| 会话恢复 | 保存 `user_data/.deepseek_session`，超时后重新进入 |
| 代码落盘 | 从 DOM `<pre>` 提取代码块并保存 |
| 测试 | 无 |
| 依赖声明 | `requirements.txt` 曾缺失；`openai`（client_test 用）此前未声明 |
| 配置 | 全部硬编码（端口、超时、选择器、路径）|

---

## 二、P0 — 阻塞正确性 / 可用性

### 1. `requirements.txt` 缺失，`INSTALL.md` 与 README 的命令跑不通
- **现象**：`cat requirements.txt` 曾报 `No such file or directory`，但 INSTALL/README 都让用户 `pip install -r requirements.txt`。
- **另外**：`client_test.py` 依赖 `openai` SDK，但任何地方都没声明。
- **建议**：新增 `requirements.txt`（已补）：
  ```
  fastapi
  uvicorn
  playwright
  pydantic
  openai
  ```
  并在 `cmdlog.md` 中补充 `uv pip install fastapi uvicorn`（当前 cmdlog 只装了 playwright）。

### 2. 超时被硬改为 60s，与 README 的 180s 说明矛盾
- 代码：`deadline = ... + 60  # 总超时 60 秒`，而上一行 180s 被注释掉；README 却写“单轮生成总超时 180 秒”。
- **风险**：DeepSeek 长回答（尤其带推理/长代码）极易超过 60s，导致频繁触发超时 + 会话恢复，反而更慢。
- **建议**：改为可配置（环境变量 `DEEPSEEK_TIMEOUT`，默认 180），并同步 README 与 `client_test.py` 的 `timeout=240.0`。

### 3. 流式与非流式对 tool_calls 的处理不一致
- 非流式：解析出 `tool_calls` 时**不落盘**（合理）。
- 流式：`wants_tools` 为真时**完全缓冲不吐字**，但若解析不出 tool_calls，则把整段 `reply_content` 按 64 字符切块补发——用户在带 tools 的场景下会先长时间无输出、再一次性倒出。
- **建议**：明确策略并写进 README：要么“带 tools 就非流式语义”，要么在缓冲阶段也发 keep-alive 并提示“正在解析工具调用”。

### 4. `on_delta` 增量依赖 `startswith`，一旦 DOM 重渲染就漏字/重复
- `_send_chat_locked` 中：`if current_text.startswith(last_text)` 才吐增量。
- DeepSeek 网页在生成中可能重排/替换节点，导致 `current_text` 不再以上一轮为前缀，此时**静默丢增量**，且 `last_text` 仍被覆盖。
- **建议**：改用最长公共前缀 diff（`os.path.commonprefix` 思路）计算真正新增部分；或退化为按“长度增长”估计。

---

## 三、P1 — 健壮性

### 5. 选择器全硬编码、无集中管理
- 输入框、回复块（`.ds-markdown, .markdown-body, div[class*="markdown"]`）、`pre` 等散落在 `_send_chat_locked` 内。
- **建议**：抽到模块级常量 / `selectors.py`，并在 README 的“选择器适配”一节直接指向该文件。

### 6. `baseline_count` 方案在“并发/连续请求”下不可靠
- 用“回复块数量 > 发送前数量”判断新回复。若上一轮 DOM 尚未稳定、或页面复用同一节点，会误判。
- **建议**：发送前后各打一个快照（节点句柄 + 文本），用“新出现的句柄”或“文本变化”联合判定；`self.lock` 已串行化，可进一步在锁内做。

### 7. 会话恢复只重试一次，且失败后无降级
- `send_chat` 捕获 `DeepSeekTimeoutError` 后重试一次，再失败直接抛出，用户看到 500。
- **建议**：区分“可重试”（网络/超时）与“不可重试”（未登录、找不到输入框）；对可重试做指数退避 + 最多 N 次；错误以 OpenAI 兼容的 `error` 对象返回而非裸 500 字符串。

### 8. 全局单例 `driver` + 启动时强制拉起有头浏览器
- `lifespan` 里 `driver.init()` 失败会导致整个服务起不来；有头模式在无显示环境（CI/服务器）直接崩。
- **建议**：
  - 支持 `HEADLESS=true` 环境变量（首次登录仍需有头）；
  - 浏览器初始化失败时服务仍启动，`/v1/chat/completions` 返回可读错误，`/healthz` 暴露状态。

### 9. 缺少 `/healthz` 与优雅关闭
- 无健康检查端点，Pi 等客户端无法探活。
- **建议**：加 `GET /healthz`，返回浏览器上下文是否存活、当前会话 URL、最近一次错误。

### 10. Token 估算过于粗糙
- `estimate_tokens = len(text)//3`，中英混排误差大；`usage` 字段会被客户端用于计费/上下文裁剪。
- **建议**：至少用 `tiktoken`（可选依赖）或按“CJK 字符数 + 非 CJK 字符数/4”更细的公式，并在 README 注明是估算。

---

## 四、P2 — 工程质量

### 11. 单文件 850 行，职责过载
- 建议拆分：
  ```
  app/
    models.py        # Pydantic 数据模型
    toolcalls.py     # 提示词注入 + 解析
    driver.py        # Playwright Driver + 会话持久化
    streaming.py     # SSE 编码
    server.py        # FastAPI 路由
  ```
  保持 `deepseek_api_server.py` 作为入口薄封装，向后兼容。

### 12. 零测试
- 解析层（`parse_tool_calls` / `_iter_balanced_objects` / `_content_to_text` / `build_prompt`）是纯函数，最易测且最易出 bug。
- **建议**：先补 `tests/test_parsing.py`，覆盖：
  - 带围栏 / 无围栏 / 多个 tool_call / 字符串内含 `}` / 转义引号 / 非法工具名过滤；
  - `build_prompt` 的“最后一条 assistant 之后”增量逻辑。
  再考虑用 `pytest-asyncio` + mock driver 测路由。

### 13. 未使用字段 / 死代码
- `ChatCompletionRequest.temperature/top_p/max_tokens/stop` 接收后从未使用（网页版无法控制），建议在 README 明确“接收但不生效”，或从模型移除以免误导。
- `deepseek_agent.py` 与 server 的 Driver 功能高度重叠，可标注为“独立示例”或合并复用解析/落盘逻辑。

### 14. `cmdlog.md` 与实际依赖不一致
- 只有 `uv pip install playwright`，缺 fastapi/uvicorn。
- 建议改为与 `requirements.txt` 一致的完整命令，或直接删除并指向 INSTALL。

### 15. 安全与合规
- `user_data/` 含登录 Cookie，`.gitignore` 已正确忽略；建议在 README 顶部加醒目警告，并考虑把 `SESSION_FILE` 也纳入忽略说明。
- 服务仅监听 `127.0.0.1`；建议显式写“不要绑定 0.0.0.0”。

---

## 五、建议的落地顺序

1. **P0-1** 补 `requirements.txt` + 修 `cmdlog.md`（否则新人跑不起来）。
2. **P0-2 / P0-3** 超时参数化 + 统一流式 tool_calls 语义，同步 README。
3. **P0-4** 增量 diff 修复。
4. **P1-9 / P1-8** 加 `/healthz` + headless 开关（提升可运维性）。
5. **P2-12** 补解析层单测（回归保护）。
6. 其余按需重构（P2-11 拆模块）。

---

## 六、快速修复清单（可直接开 issue）

- [ ] 新增 `requirements.txt`（含 `openai`）
- [ ] `cmdlog.md` 补全依赖安装命令
- [ ] 超时改为环境变量，默认 180s，README 同步
- [ ] 统一带 tools 时的流式行为并写文档
- [ ] `on_delta` 改用公共前缀 diff
- [ ] 抽出选择器常量
- [ ] 新增 `/healthz`
- [ ] `HEADLESS` 环境变量支持
- [ ] 新增 `tests/test_parsing.py`
- [ ] README 标注“temperature 等字段接收但不生效”
