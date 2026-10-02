# 项目改进建议 (doc/update.md)

> 基于对当前代码库的审查（`deepseek_api_server.py` / `deepseek_agent.py` / `client_test.py` / README / 配置）。
> 按优先级分层，P0 为正确性/可用性阻塞项，P1 为健壮性，P2 为工程质量。

## 修订记录

| 日期 | 内容 |
| --- | --- |
| 初版 | 首次审查，给出 P0–P2 分层建议 |
| 本次 | **对齐当前代码**：标注已完成项；修正两条不准确的建议（`tiktoken`、`baseline_count`）；补上调试工具与新增能力；更新行数等数字 |

> 状态标记：✅ 已完成　🔶 部分完成　⬜ 未完成
>
> ⚠️ **初版中的 P0-2（超时硬编码 60s）与 P1-6（`baseline_count` 不可靠）在初版提交时其实已经修好了** —— 初版是对旧快照的审查，本次已核对并修正。

---

## 一、项目现状概览

| 项 | 状态 |
| --- | --- |
| 核心服务 `deepseek_api_server.py` | 单文件 **1175 行**，含常量 / 数据模型 / 解析层 / 浏览器 Driver / FastAPI 路由 |
| OpenAI 兼容 | `/v1/models`、`/v1/chat/completions`（含 SSE 流式）、OpenAI 兼容错误体 |
| Function Calling | 提示词注入 + 结构化解析模拟 |
| 会话恢复 | 保存 `user_data/.deepseek_session`，超时后重新进入 |
| 结束判定 | 内容变化判定本轮回复出现 + 页面生成状态 / 内容稳定双重收尾 |
| 代码落盘 | 从 DOM `<pre>` 提取代码块并保存 |
| 可运维 | `GET /healthz`、`HEADLESS`、`DEEPSEEK_TIMEOUT` / `DEEPSEEK_RETRIES` / `DEEPSEEK_DEBUG`、`GET /debug/dom` |
| 测试 | **46 个用例（stdlib unittest）**，`tests/` |
| 依赖声明 | `requirements.txt` 已存在（含 `openai`） |
| 配置 | 关键参数已环境变量化；DOM 选择器已集中到文件顶部 |

---

## 二、P0 — 阻塞正确性 / 可用性

### 1. ✅ `requirements.txt` 缺失，`INSTALL.md` 与 README 的命令跑不通
- **结论**：已修复。`requirements.txt` 存在且包含 `fastapi / uvicorn / playwright / pydantic / openai`；`README` 与 `INSTALL.md` 均改为 `pip install -r requirements.txt`。
- **已完成**：`cmdlog.md` 也已对齐（不再只装 playwright），并补上运行与测试命令。

### 2. ✅ 超时被硬改为 60s，与 README 矛盾
- **结论**：已修复，且采用的正是初版建议的方案。
- **现状**：`RESPONSE_TIMEOUT_S = float(os.environ.get("DEEPSEEK_TIMEOUT", "180"))`。
- **补充约束（重要）**：该值**必须小于 Pi 侧 HTTP 客户端的超时**，否则客户端会先报错，服务端的兜底反而变成负担。

### 3. 🔶 流式与非流式对 `tool_calls` 的处理不一致
- **结论**：现象成立，但这是**无法避免**的：带 `tools` 时只有拿到完整回复才能判断是不是 `tool_calls`，所以必须缓冲。
- **已完成**：在 README 中明确写出该语义（带 `tools` 期间只发 `: keep-alive`，随后一次性给出内容或 tool_calls），并在 `DEEPSEEK_DEBUG=1` 时打印缓冲轮次，保证可观测。
- **剩余可选**：若希望调用方有更明确的反馈，可在缓冲期间发送带 `role` 的空 delta；但 OpenAI 协议下没有标准的"进度"语义，收益有限，暂不做。

### 4. ✅ `on_delta` 增量只认 `startswith`，DOM 重排时静默丢字
- **结论**：已修复。
- **现状**：新增 `_delta_piece(streamed, current)`，按「已发送内容」的**公共前缀**计算真正新增的部分；重排时不会再静默丢字，代价是极端情况下可能多出几个字符（SSE 无法撤回）。
- **测试**：`tests/test_parsing.py::DeltaPieceTests`、`tests/test_end_detection.py::StreamingDeltaTests`。

---

## 三、P1 — 健壮性

### 5. ✅ 选择器全硬编码、无集中管理
- **现状**：`RESPONSE_SELECTORS` / `INPUT_SELECTORS` / `READY_SELECTOR` / `CODE_BLOCK_SELECTOR` / `CODE_TAG_SELECTOR` 集中定义在文件顶部；README 的"选择器适配"一节点明只需改这一处。
- **剩余可选**：如需进一步拆成独立模块，见 P2-11。

### 6. ✅ `baseline_count` 方案不可靠（**这是初版最有价值的一条**）
- **结论**：判断完全正确，而且它精确预言了后来真实发生的故障。
- **已完成**：彻底删掉节点数量比较，改为**文本对比**：发送前记录最后一条回复的文本，之后只要内容不同就认为本轮回复已出现。
- **⚠️ 初版建议本身需要修正**：初版建议用「节点句柄 + 文本变化」联合判定。实测证明**节点句柄和数量同样不可靠** —— DeepSeek 会回收/替换节点，长会话下节点数恒为 2，新回复只把旧节点内容换掉。**必须以纯文本为准**。

### 7. ✅ 会话恢复只重试一次，且失败后无降级 / 裸 500
- **现状**：
  - 可重试与不可重试已区分：只有 `DeepSeekTimeoutError` 才重试，最多 `DEEPSEEK_RETRIES` 次（默认 2），每次先恢复会话再退避重试；找不到输入框、profile 被占用等直接抛出；
  - 错误统一改为 OpenAI 兼容的 `{"error": {"message", "type", "code"}}`：超时 → 504，上游不可用 → 502，请求非法 → 400，其余 → 500。

### 8. ✅ 全局单例 `driver` + 启动时强制拉起有头浏览器
- **现状**：`HEADLESS=1` 可无头启动；`lifespan` 中浏览器初始化失败**不再让服务起不来**，而是记录 `init_error` 并继续启动，请求侧返回可读错误。

### 9. ✅ 缺少 `/healthz`
- **现状**：`GET /healthz` 返回 `status` / `browser_ready` / `headless` / `session_url` / `init_error`；未就绪时返回 503。

### 10. ✅ Token 估算过于粗糙
- **现状**：改为 CJK 字符按 1 char/token、其余按 4 char/token 估算，并在 docstring 与 README 中标注为估算值。
- **⚠️ 初版建议本身需要修正**：**不要引入 `tiktoken`**。它是 OpenAI 的分词器，用来估算 DeepSeek 的 token 只会得到一个"看起来很精确但其实是错的"数字，反而更容易误导客户端做上下文裁剪。保持明确标注的近似估算更诚实。

---

## 四、P2 — 工程质量

### 11. ⬜ 单文件职责过载（现 1175 行）
- **结论**：成立。
- **建议**：按 `models.py` / `toolcalls.py` / `driver.py` / `streaming.py` / `server.py` 拆分，`deepseek_api_server.py` 保留为入口薄封装。
- **注意**：拆分应先以 P2-12 的测试做保护，且属于纯重构，优先级低于功能类问题。

### 12. ✅ 零测试
- **现状**：`tests/` 下 46 个用例，全部使用**标准库 `unittest`**（不引入 pytest，零新依赖）：
  - `tests/test_parsing.py`：`_content_to_text`、`estimate_tokens`、`_delta_piece`、`_iter_balanced_objects`、`parse_tool_calls`（围栏 / 无围栏 / 多个调用 / 字符串含 `}` / 转义引号 / 合法名过滤）、`build_prompt` 增量逻辑、`to_tool_call_models`、请求模型宽松校验；
  - `tests/test_end_detection.py`：用假 page 驱动 `_send_chat_locked`，覆盖节点数恒定、流式增长后静止、停止按钮出现又消失、内容未变时不返回旧答案、超时返回已读内容不重发、增量可还原全文。
- **运行**：`.venv/bin/python -m unittest discover -s tests -t . -v`
- **剩余可选**：路由层（FastAPI）尚未覆盖，需要 mock driver 或 httpx 的 `ASGITransport`（当前环境未安装 httpx）。

### 13. 🔶 未使用字段 / 代码重叠
- **已完成**：README 明确标注 `temperature` / `top_p` / `max_tokens` / `stop` **接收但不生效**。
- **剩余可选**：`deepseek_agent.py` 与 Driver 功能重叠，可标注为"独立示例"或合并复用解析/落盘逻辑。

### 14. ✅ `cmdlog.md` 与实际依赖不一致
- **现状**：已改为 `uv pip install -r requirements.txt`，并补上运行与测试命令。

### 15. ✅ 安全与合规
- **现状**：`user_data/` 已被 `.gitignore` 忽略；README 增加"不要绑定 `0.0.0.0`"的明确警告。
- **澄清**：初版提到的"把 `SESSION_FILE` 也纳入忽略说明"其实是重复担心 —— `SESSION_FILE = ./user_data/.deepseek_session` 本来就在被忽略的 `user_data/` 目录内。

---

## 五、经验教训（值得写进文档的部分）

连续两次的真实故障都出在"**没有测试的启发式逻辑**"上，且两次都源于同一个错误假设：**用节点数量判断网页是否产生了新回复**。

| 时间线 | 现象 | 根因 |
| --- | --- | --- |
| 第一次 | DeepSeek 已答完，Pi 一直 working | 结束判定依赖"文本逐字相等"，尾部重排导致永远等不到 |
| 第二次 | 每轮都要等到超时才继续 | `len(responses) > baseline_count` 在节点被回收时永远为假，循环内读文本的分支从未执行 |

结论：
1. 对接别人的 DOM 时，**任何依赖"节点数量 / 句柄身份"的假设都是不安全的**，应以内容为准；
2. 启发式逻辑必须配回归测试，本次已补齐；
3. 未验证的"看起来更宽容"的参数改动（如把超时 60s→300s）会放大既有故障，改动前应先确认失效路径。

---

## 六、建议的落地顺序（仅剩未完成项）

1. **P2-11** 拆分模块（先由 P2-12 的测试兜底）。
2. **P2-12 剩余** 路由层测试（可选引入 httpx / mock driver）。
3. **P2-13 剩余** 处理 `deepseek_agent.py` 的功能重叠。
4. **P1-10 剩余** 如需更准的 usage，接入 DeepSeek 自己的分词器（而非 tiktoken）。

---

## 七、快速修复清单

- [x] 新增 `requirements.txt`（含 `openai`）
- [x] `cmdlog.md` 补全依赖安装命令
- [x] 超时改为环境变量，默认 180s，README 同步
- [x] 统一带 tools 时的流式行为并写文档
- [x] `on_delta` 改用公共前缀 diff
- [x] 抽出选择器常量
- [x] 新增 `/healthz`
- [x] `HEADLESS` 环境变量支持
- [x] 新增 `tests/test_parsing.py` 与 `tests/test_end_detection.py`
- [x] README 标注"temperature 等字段接收但不生效"
- [x] 错误响应改为 OpenAI 兼容结构 + 区分可重试/不可重试
- [ ] 拆分单文件为多模块
- [ ] 补路由层测试
