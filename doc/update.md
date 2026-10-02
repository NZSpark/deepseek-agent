# 项目改进建议 (doc/update.md)

> 基于对当前代码库的审查（`deepseek_api_server.py` / `deepseek_agent.py` / `client_test.py` / README / 配置）。
> 按优先级分层，P0 为正确性/可用性阻塞项，P1 为健壮性，P2 为工程质量。

## 修订记录

| 日期 | 内容 |
| --- | --- |
| 初版 | 首次审查，给出 P0–P2 分层建议 |
| 对齐当前代码 | 标注已完成项；修正两条不准确的建议（`tiktoken`、`baseline_count`）；补上调试工具与新增能力；更新行数等数字 |
| 新增 P0-0 | **会话无上限增长**（长期复用同一会话的后果与方案）；补 `.env` 配置中心；更新行数 |
| P0-0 补充设计原则 | 补充**设计原则与备选方案评估**（否决“每次启动新开会话”与“超时后复用旧会话”两种直觉方案）；重排实现顺序（**播种优先**） |
| 本次 | 完成 **P2-11 模块拆分**：单文件 → `deepseek_web` 包 + 薄入口；补模块结构回归测试；测试增至 54 例 |

> 状态标记：✅ 已完成　🔶 部分完成　⬜ 未完成
>
> ⚠️ **初版中的 P0-2（超时硬编码 60s）与 P1-6（`baseline_count` 不可靠）在初版提交时其实已经修好了** —— 初版是对旧快照的审查，本次已核对并修正。

---

## 一、项目现状概览

| 项 | 状态 |
| --- | --- |
| 入口 `deepseek_api_server.py` | **101 行薄封装**：重新导出历史公开名字 + 启动 uvicorn |
| 实现 `deepseek_web/` | 拆为 8 个模块（共约 1369 行）：`config` / `models` / `toolcalls` / `prompting` / `driver` / `streaming` / `server` / `__init__` |
| OpenAI 兼容 | `/v1/models`、`/v1/chat/completions`（含 SSE 流式）、OpenAI 兼容错误体 |
| Function Calling | 提示词注入 + 结构化解析模拟 |
| 会话恢复 | 保存 `user_data/.deepseek_session`，超时后重新进入 |
| **会话生命周期** | ⬜ **未管理**：所有请求共用一个全局会话，**永不轮转、无上限控制**（见 P0-0） |
| 结束判定 | 内容变化判定本轮回复出现 + 页面生成状态 / 内容稳定双重收尾 |
| 代码落盘 | 从 DOM `<pre>` 提取代码块并保存 |
| 可运维 | `GET /healthz`、`HEADLESS`、`DEEPSEEK_TIMEOUT` / `DEEPSEEK_RETRIES` / `DEEPSEEK_DEBUG`、`GET /debug/dom` |
| 测试 | **54 个用例（stdlib unittest）**，`tests/`（含模块结构 / 向后兼容冒烟） |
| 依赖声明 | `requirements.txt` 已存在（含 `openai`） |
| 配置 | `.env` / `.env.example` + `env_str/env_int/env_float/env_bool` 集中读取；DOM 选择器已集中到文件顶部 |

---

## 二、P0 — 阻塞正确性 / 可用性

### P0-0 ⬜ 会话无上限增长，且“到顶”会伪装成超时（本次新增，最高优先级）

- **现状**：`SESSION_FILE` 只保存**一个**会话地址，启动时 `goto` 它，每轮成功后再把当前地址写回同一个文件 —— **所有请求永远共用同一个网页会话，代码里没有任何新建 / 轮转 / 重置逻辑**。
- **为何必然增长**：`build_prompt` 只发送「最后一条 assistant 之后」的新增消息，依赖网页端自己保留全部历史。因此**每轮真正喂给模型的上下文 = 整个网页会话的累积历史**，而不是客户端发来的 `messages`。
- **量级感觉**：编码代理每轮的工具结果可能很大（整份文件、命令输出），几十轮就足以触及上限。
- **风险 1（最危险）**：网页版到顶是**硬墙**而非静默截断——会弹出「达到对话长度上限，请开启新对话」并**停止响应**（社区反馈见 DeepSeek-V3 issue #1418）。映射到本服务的表现是：页面上不再出现新回复 → 内容始终等于 `before_text` → 判定不出「本轮回复已出现」→ 等到 `DEEPSEEK_TIMEOUT` → 重试 → 返回 504。**即“会话到顶”与“真的卡住”目前无法区分**，用户只会看到反复超时（待在本机会话上验证）。
- **风险 2**：模型看到的上下文与客户端以为的上下文会**分叉**。客户端按自己的 `contextWindow` 压缩 / 丢弃早期消息时，网页会话里一条都不会少。
- **风险 3**：会话一旦丢失（恢复失败 / profile 被清 / 手动开新对话），`build_prompt` 的兜底只发**最后一条 user 消息** —— 模型会在完全没有上下文的情况下收到一条孤立工具结果，**不报错，只瞎答**。

#### 设计原则

**把“是否新开会话”交给会话自身的健康度与体积，而不是进程生命周期。** 会话会不会过长取决于任务跑了多少轮，与进程重启过几次无关。

#### 备选方案评估（已否决）

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 每次启动 `deepseek_api_server.py` 就新开会话 | ❌ 否决 | ① **轴选错了**：上下文长不长取决于任务轮数，与进程重启次数无关（可以重启 10 次而上下文很短，也可以不重启跑 100 轮撑爆）；② 在没有“播种”能力时会直接退化成风险 3 的**静默失败**；③ 本项目开发期重启频繁（每次改代码都要 `pkill`+重启），等于改完就丢上下文 |
| 保留上次错误信息，若为超时则复用上一个会话 | ❌ 否决 | ① **触发条件反了**：到顶最可能的表现就是超时，复用会把最可能已撑爆的会话继续拿来用；② “上次错误”跨进程不可靠（被 `SIGKILL` 时写不进去、陈旧文件可能长期生效）——**适合当遥测，不适合当决策依据** |
| 从上述想法提炼出的正确落点 | ✅ 采纳 | **连续超时且页面内容完全无变化 ⇒ 不是“慢”，而是“这个会话不干活了” ⇒ 换新会话 + 重放历史** |

> 看现有代码：`send_chat` 的每一次重试都调用 `_recover_session()`，而它重新打开的是**同一个 URL**。若该会话已到顶/失效，三次重试都在同一个坏会话上打转，注定全超时。这是实际存在的缺陷。

#### ⚠️ 前置依赖

**「新开会话」与「播种上下文」必须成对出现。** 没有播种能力之前，任何形式的自动新开会话都是危险的 —— 因为 `build_prompt` 只发增量，新会话里模型收不到任何历史。因此**实现顺序上播种必须先做**。

#### 建议（按性价比排序）

1. **先实现“播种”**：当会话不存在或需要新建时，用客户端完整历史（或摘要）重建上下文，而不是只发最后一条消息。**这是后面所有改动的前提。**
2. 检测「达到对话长度上限」类文案，返回可区分的 `context_length_exceeded` 错误，而不是让它伪装成超时。
3. **重试阶梯**：正常 → 恢复同一会话 → **新会话 + 重放历史**（取代现在“三次都在同一个会话上打转”）。
4. 会话状态从“一个 URL”升级为 `{url, turns, est_tokens, cap_hit, last_failure}`；启动时按状态决定复用还是新建（有了第 1 条，新建才是安全的）。默认仍应复用，以保留跨重启的上下文。
5. 按任务隔离会话（按 Pi 的 session / thread 标识分桶），不要全局共用一个。
6. `/healthz` 暴露轮数与估算 token；提供 `POST /session/reset` 或 `DEEPSEEK_NEW_SESSION=1` 作为手动逃生口。

- **测试思路**：会话轮转、“到顶”检测与播种策略都能用假 page 覆盖，与 `tests/test_end_detection.py` 同一套路，无需真实浏览器。

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
- **已增强**：这些选择器现在都可用 `.env` 覆盖而无需改代码（`INPUT_SELECTORS` 用 `||` 分隔多个候选）。
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

### 11. ✅ 单文件职责过载（原 1239 行）
- **现状**：已拆为 `deepseek_web` 包，`deepseek_api_server.py`（101 行）保留为薄入口，向后兼容地重新导出历史公开名字。

  ```
  deepseek_web/
    config.py       .env 加载 + 全部可调参数（超时 / 重试 / 选择器 / 路径）
    models.py       OpenAI 兼容的 Pydantic 数据模型
    toolcalls.py    工具注入与解析（模拟 function calling）
    prompting.py    消息数组 -> 网页输入框文本
    driver.py       Playwright Driver + 会话持久化
    streaming.py    SSE 流式编码
    server.py       FastAPI 应用与路由（持有 driver 单例）
  ```

- **两个关键设计决定**（后续改代码请遵守）：
  1. **可调参数统一放 `config.py`，其他模块按 `config.<NAME>` 运行期取属性**（而不是 `from config import NAME`）。这样 `patch.object(config, "X", v)` 才能生效；测试若去 patch 入口模块的重导出名字会**静默失效**（已在 `tests/test_end_detection.py` 注释里写明）。
  2. **`streaming` 不再依赖全局 driver，改为参数注入**（`_stream_chat_completion(request, prompt, driver)`），以避开 `server <-> streaming` 循环依赖。
- **验证方式**：用 AST 对拍新旧源码——逐个函数/方法比较「函数体」，把新代码的 `config.` 前缀归一化后与旧单文件对比。最终 35 项中 6 项有差异，逐条确认均为**有意改动**（`_SESSION_URL_RE` 重命名为 `config.SESSION_URL_RE`、docstring 措辞、`_stream_chat_completion` 增参、`build_prompt` 改为委托、一处类型标注），其余函数体完全一致。
- **新增测试**：`tests/test_package_layout.py` 守护重导出名字、`config` 属性类型、`.env` 仍指向项目根（拆分后 `__file__` 变了，很容易踩坑）、路由存在、`streaming` 签名可注入 driver。
- **注意**：拆分本身就是纯重构，已由 P2-12 的测试 + AST 对拍保住行为不变。

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
| 待验证 | 会话到顶时反复超时 | 网页版停止响应，与"真的卡住"表现一致（见 P0-0 风险 1） |

结论：
1. 对接别人的 DOM 时，**任何依赖"节点数量 / 句柄身份"的假设都是不安全的**，应以内容为准；
2. 启发式逻辑必须配回归测试，本次已补齐；
3. 未验证的"看起来更宽容"的参数改动（如把超时 60s→300s）会放大既有故障，改动前应先确认失效路径；
4. **多种失败原因会收敛到同一个症状（超时）**。凡是复用外部会话/进程的地方，都要把"上游明确拒绝"与"上游无响应"区分开，否则排查成本极高。

---

## 六、建议的落地顺序（仅剩未完成项）

1. **P0-0 第 1 步**：会话播种（所有轮转功能的**前置依赖**）。
2. **P0-0 第 2 步**：到顶检测（改动小、直接消掉“伪超时”）。
3. **P0-0 第 3 步**：重试阶梯改为“最后一级换新会话 + 重放历史”。
4. **P0-0 第 4 步**：会话状态持久化 + 基于体积的轮转。
5. **P2-12 剩余** 路由层测试（可选引入 httpx / mock driver）。
6. **P2-13 剩余** 处理 `deepseek_agent.py` 的功能重叠。
7. **P1-10 剩余** 如需更准的 usage，接入 DeepSeek 自己的分词器（而非 tiktoken）。

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
- [ ] **会话播种**（新会话时用完整历史 / 摘要重建上下文）——其余轮转项的前置
- [ ] 检测「达到对话长度上限」并返回可区分的 `context_length_exceeded` 错误
- [ ] 重试阶梯最后一级改为“新会话 + 重放历史”
- [ ] 会话自动轮转（轮数 / token 阈值 + 摘要播种）
- [ ] 按任务隔离会话；`/healthz` 暴露会话轮数与估算 token
- [ ] `POST /session/reset` / `DEEPSEEK_NEW_SESSION=1` 手动逃生口
- [x] 拆分单文件为多模块（`deepseek_web` 包 + 薄入口）
- [ ] 补路由层测试
