# 项目改进建议 (doc/update.md)

> 基于对当前代码库的**逐文件通读**（`deepseek_web/` 8 模块共 2132 行 / `deepseek_api_server.py` 113 行薄入口 / `tests/` 149 例 1831 行 / `.env` 体系）。
> 按优先级分层，P0 为正确性/可用性阻塞项，P1 为健壮性，P2 为工程质量。
>
> **读法建议**：想快速动手，直接看「§三 本轮新发现」+「§五 落地顺序」；
> 想了解为什么会是现在这个设计，看「附录 A 会话模型速查」。

## 修订记录

| 日期 | 内容 |
| --- | --- |
| 初版 | 首次审查，给出 P0–P2 分层建议 |
| 对齐当前代码 | 标注已完成项；修正两条不准确的建议（`tiktoken`、`baseline_count`）|
| 新增 P0-0 | 会话无上限增长；补 `.env` 配置中心 |
| P0-0 设计评估 | 否决“每次启动新开会话”“超时后复用旧会话”；**播种优先** |
| 完成模块拆分 | 单文件 → `deepseek_web` 包 |
| 完成 P0-0 1–4 步 | 播种 / 到顶检测 / 重试阶梯 / 状态与自动轮转 |
| 完成 P0-0 剩余 | 按任务隔离会话 + `/session/reset` + 路由层测试 → 129 例 |
| 删除 `deepseek_agent.py` | 功能重叠且无测试，直接删除（git 历史可查） |
| 本轮（第三次通读）| 核对全部已完成项；新增 6 条新发现（P1-A 桶页不回收、P1-B 落点未校验、P1-C 全局锁、P1-D `/debug/dom` 泄正文、P2-E 文件名碰撞、P2-F 死分支）|
| 审计 + 修正 | 逐条核对上轮新发现：修正 P1-B 的错误建议（引用了不存在的 `needs_reseed` 字段）与严重性描述；补充 P2-H；修正行数；重排落地顺序；恢复设计原则 / 已否决方案 / 状态文件格式（附录 A）|
| **本次（按本文件实施）** | **已落地 P1-A / P1-B / P1-C / P1-D / P2-E / P2-F / P2-H 七项**：桶页面空闲回收 + LRU 淘汰（不再永久失败）、goto 后反查落点、按桶加锁开关（默认仍串行）、`/debug/dom` 限调试且不回显正文、落盘文件名去重、删掉死分支并换成能真正拦住的检查、建页等待就绪与上限语义澄清。测试 129 → **149 例** |

> 状态标记：✅ 已完成　🔶 部分完成　⬜ 未完成

---

## 一、项目现状概览

| 项 | 状态 |
| --- | --- |
| 入口 `deepseek_api_server.py` | **113 行薄封装**：重导出历史公开名字 + 启动 uvicorn |
| 实现 `deepseek_web/` | 8 模块（共 **2132** 行）：`config`(170) / `models`(106) / `toolcalls`(188) / `prompting`(163) / `driver`(996) / `streaming`(163) / `server`(305) / `__init__`(41) |
| OpenAI 兼容 | `/v1/models`、`/v1/chat/completions`（SSE）、OpenAI 兼容错误体（400/502/503/504）|
| Function Calling | 提示词注入 + 结构化解析模拟 |
| **会话生命周期** | ✅ 体积预算自动轮转 + 轮转时**播种** + 到顶检测 + 重试阶梯 |
| **按任务隔离会话** | ✅ `X-DeepSeek-Session` 分桶，每桶独立页面与状态；`POST /session/reset`；`SESSION_SCOPING=false` 可关闭 |
| 结束判定 | 文本变化判定回复出现 + 生成状态 / 内容稳定双重收尾 + 周期性到顶检测 |
| 可运维 | `/healthz`（含 session 统计）、`/debug/dom`、`HEADLESS`、`DEEPSEEK_*` 系列环境变量 |
| 测试 | **149 例全部通过**（stdlib unittest，`Ran 149 tests ... OK`）|
| 配置 | `.env` + `.env.example` + `env_*` 助手，全部集中在 `config.py`（含 DOM 选择器）|

---

## 二、历史问题结清情况（P0/P1 均已给出结论，唯一遗留是协议层面的 P0-3）

| 编号 | 问题 | 状态 |
| --- | --- | --- |
| P0-0 | 会话无上限增长 / 到顶伪装成超时 | ✅ 播种 + 到顶检测 + 重试阶梯 + 自动轮转 + 分桶 |
| P0-1 | `requirements.txt` 缺失 | ✅ |
| P0-2 | 超时硬编码 60s | ✅ `DEEPSEEK_TIMEOUT`（默认 180）|
| P0-3 | 流式 tool_calls 语义 | 🔶 无法避免（需缓冲），已在 README 说明 |
| P0-4 | `on_delta` 仅认 `startswith` | ✅ `_delta_piece` 公共前缀 diff |
| P1-5 | 选择器硬编码 | ✅ 集中 `config.py`，可用 `.env` 覆盖 |
| P1-6 | `baseline_count` 不可靠 | ✅ 纯文本对比 |
| P1-7 | 重试 / 降级 / 裸 500 | ✅ 阶梯重试 + OpenAI 兼容错误体 |
| P1-8 | 全局单例 + 强制有头 | ✅ `HEADLESS` + `init_error` 不阻塞启动 |
| P1-9 | 缺 `/healthz` | ✅ |
| P1-10 | token 估算粗糙 | ✅ CJK/4-char 估算；**明确不引入 tiktoken** |
| P2-11 | 单文件职责过载 | ✅ 拆为包 |
| P2-12 | 零测试 | ✅ 129 例 |
| P2-13 | `deepseek_agent.py` 重叠 | ✅ 已删除 |
| P2-14 | `cmdlog.md` 不一致 | ✅ |
| P2-15 | 安全合规 | ✅（`user_data/` 忽略、禁绑 `0.0.0.0`）|

---

## 三、本轮新发现

### ✅ P1-A（已修复）　会话桶页面曾**只增不减**，达到上限后新 key **永久失败**

- **已落地**：新增 `driver._recycle_idle_pages()`（空闲超 `BUCKET_IDLE_TTL_S` 秒则关页）与 `_evict_lru_page()`（达上限时淘汰最久未用页），**只关页面、状态保留**（`url` / `turns` 不动，下次自动重开同一会话并按需播种）；正在生成回复的桶（锁被持有）永不被回收。错误提示同步改正：不再指向无效的 `/session/reset`，而是说明“正在生成回复的会话不会被淘汰，请稍后重试”；`MAX_SESSION_BUCKETS=0` 现在有明确语义（不允许额外桶）与可操作提示。
- **位置**：`driver._ensure_page` / `driver.close`。
- **现状**：`self._pages` 为每个新 key 惰性 `context.new_page()`；上限 `MAX_SESSION_BUCKETS`（默认 8）。达到上限后：
  ```python
  if len(self._pages) >= max(1, config.MAX_SESSION_BUCKETS):
      raise RuntimeError(f"会话桶数量已达上限（{config.MAX_SESSION_BUCKETS}），请用 POST /session/reset 回收…")
  ```
- **问题**：**没有任何回收路径**。`reset_session` 只改状态（`pending_rotation=True`），**不释放页面**；`close()` 只在进程退出时关整个 context。因此：
  1. 一旦 8 个不同的 `X-DeepSeek-Session` 值出现，第 9 个 key **永久 502**，直到重启进程；
  2. 错误信息建议“用 `/session/reset` 回收”，但该端点**并不能回收桶** —— 属于**误导性提示**。
- **建议**（任一）：
  - a) 给 `_ensure_page` 增加**空闲页面回收**：记录每桶 `last_used`，超时（如 `BUCKET_IDLE_TTL`）后 `await page.close()` 并移出 `_pages`（保留 `_sessions` 状态，下次按 url 恢复）；
  - b) 或采用 **LRU**：超过上限时关闭最久未用的页面再建新的（状态仍保留）；
  - c) 至少修正错误提示，说明“需重启或关闭 `SESSION_SCOPING`”，不要指向无效端点。
- **测试建议**：假 `context` 记录 `new_page`/`close` 次数，断言 LRU/空闲回收确实释放页面、且状态（`url`/`turns`）不丢。

### ✅ P1-B（已修复）　`_ensure_page` 曾用“有 url”代替“页面真的落在会话里”，goto 之后未校验落点

- **已落地**：`_ensure_page` 改为 `state.has_history = not state.cap_hit and self._current_session_url(bucket) is not None`；`_restore_session_on_startup` 同样以**页面真实落点**为准（判不出来就让本轮播种），并在未打开成功时打印原因。
- **位置**：`driver._ensure_page` 末尾：
  ```python
  state.has_history = bool(state.url) and not state.cap_hit
  ```
- **问题**：它**忽略了 `SessionState.has_history` 本身**，改用“有 url 且未到顶”重新推导。当出现以下情况时会误判为“有历史”，从而**不播种**：
  - 该桶上次会话已被网页端清空 / 手动新开（`url` 仍在状态文件里，但网页会话其实没有上下文）；
  - 状态文件中 `url` 是旧的、而页面 `goto` 后停在首页（`_current_session_url` 为 None，但 `state.url` 仍是旧值）。
- **后果**：正好命中 P0-0 的**风险 3** —— 模型收到一条没有前因的孤立消息，不报错，只瞎答。
- **建议**：不要“重新推导”，而是 **goto 之后反查真实落点**：
  ```python
  target = HOME if state.cap_hit else (state.url or HOME)
  await page.goto(target, wait_until="domcontentloaded")
  # 只有页面确实停在某个会话上才算“有历史”；否则本轮必须播种
  state.has_history = not state.cap_hit and self._current_session_url(bucket) is not None
  ```
  同一校验也应加到 `_restore_session_on_startup` —— 默认桶走的是同一套推导（`self.session_has_history = bool(saved_session)`），缺口一模一样。
- **⚠️ 勘误（初版这条建议本身是错的）**：不要写成 `state.has_history = ... and not state.needs_reseed` —— **`SessionState` 没有 `needs_reseed` 字段**（那是推测出来的 API）。同样地，单纯把推导换成“直接信任持久化字段”**不会带来任何区别**：在本服务能写出的所有状态里二者等价（轮转后是 `url=None, has_history=False`，成功一轮后是 `url=…, has_history=True`）。真正缺的是对**页面落点**的校验。
- **备注**：`test_multi_session.py` 已有“惰性建页并回到旧会话”的用例，但**未覆盖“url 存在却无上下文”**这一分支。

### ✅ P1-C（已修复）　全局 `self.lock` 使“会话分桶”**只隔离上下文，不提供并发**

- **已落地**：新增 `driver._lock_for(bucket)`，默认（`PARALLEL_BUCKETS=false`）全部退回 `self.lock`（**行为不变，仍串行**）；显式打开开关后才按桶各持一把锁，且默认桶始终用全局锁。README / `.env.example` / 附录 A 均已写明“分桶 ≠ 并发”与风控风险。
- **位置**：`driver._send_chat_locked` 开头 `async with self.lock:`（`self.lock` 是**单个** `asyncio.Lock`）。
- **现状**：即便两个请求携带不同的 `X-DeepSeek-Session`（不同页面），它们仍会**在同一个锁上串行**。
- **影响**：
  - 每个桶的“独立页面”带来的并发收益为 0；8 个 key 的请求排队，单轮 180s 超时下队尾延迟可达分钟级；
  - 与 `MAX_SESSION_BUCKETS` 的“隔离”预期不符，容易让使用者误以为分桶可并行。
- **建议**：把锁按桶拆分（`self._locks: Dict[str, asyncio.Lock]`，默认桶沿用 `self.lock` 以兼容测试），并在 README 明确“分桶是上下文隔离，默认仍串行；如需并发需按桶加锁 + 确认网页端能承受多页面并行”。
- **风险提示**：多页面真并发会同时驱动多个网页会话，可能触发风控；是否放开需谨慎，建议**默认保持串行**、把“按桶加锁”作为可选开关。

### ✅ P1-D（已修复）　`/debug/dom` 曾会回显会话正文

- **已落地**：去掉 `head` / `tail`（只保留 `class` / `text_length` / `sha1` / 停止按钮结构，足以定位结束判定），并默认不开放：未设 `DEEPSEEK_DEBUG=1` 时返回 **404**。
- **位置**：`server.debug_dom` 返回每节点的 `head`（前 80 字）与 `last_node.tail`（末 200 字）。
- **问题**：这是**会泄露对话内容**的端点，且无访问控制。服务默认仅监听 `127.0.0.1`，但本机任何进程、容器、代理都能读。
- **建议**：默认不注册（仅 `DEEPSEEK_DEBUG=1` 时挂载）；或去掉 `head`/`tail`，只保留 `text_length` / `sha1` / `class` —— 诊断结束判定并不需要正文。

### ✅ P2-E（已修复）　`save_extracted_files` 文件名曾在同一秒内互相覆盖
- `filename = f"code_{int(time.time())}_{idx}.{ext}"`，`idx` 是**本批内**序号；两个请求同秒返回、各有第 1 个代码块时，`code_<ts>_1.py` 后者覆盖前者。`response_<ts>.md`（无 idx）更易撞。
- **建议**：文件名加 `uuid4().hex[:6]` 或 `os.getpid()`。

### ✅ P2-F（已修复）　`server.chat_completions` 的 400 分支曾是死代码

- **已落地**：不再判定“两份 prompt 都为空”（永不命中），改为判定**真正要发出去的那一份**（`prompt`）。它现在能真正拦住“空输入”：以前会把空消息发给网页版，页面什么都不做，客户端只能等到 180s 超时。
- `if not delta_prompt and not seeded_prompt:` —— 只要 `messages` 非空，播种分支总能产出内容，永不命中。已在文档中说明并用测试钉住行为，留着不影响正确性，但建议加注释或删除以免误导。

### ✅ P2-H（已修复）　几则小不一致
- **① 建页不等输入框就绪** —— ✅ 已落地：抽出 `driver._wait_ready(page)`，`_ensure_page` / `_restore_session_on_startup` / `_start_new_session` / `_recover_session` 四处统一使用，超时改为可配的 `READY_TIMEOUT_MS`。
- **② `MAX_SESSION_BUCKETS=0` 含义反直觉** —— ✅ 已落地：现在 `0` 明确表示“不允许额外会话桶”，报错信息直接给出两条可行出路（设 >=1 或 `SESSION_SCOPING=false`），`.env.example` 也写清楚了。
- **③（备忘）并发建页已经有保护**：`_ensure_page` 内有 `self._page_lock` 双重检查，两个并发请求带同一个新 key 不会重复建页 —— 本次修 P1-A 时保留了这个保护。

---

## 四、仍未完成 / 可选加强（低优先级）

### ⬜ P2-G　路由测试可升级为真正的 ASGI 端到端
- 现状：`test_routes.py` 直接 `await` 路由函数 + 假 driver（因环境无 httpx）。
- 建议：引入 `httpx` 后用 `ASGITransport` 走真实请求/响应，覆盖 header 透传、SSE 分块边界、状态码。

### ⬜ P1-10 剩余　如需更准的 usage
- 保持“明确标注为估算”的近似公式；若要精确，应接 DeepSeek 自己的分词器（**不是 tiktoken**）。

---

## 五、落地顺序与状态（本轮）

| # | 项 | 状态 |
| --- | --- | --- |
| 1 | **P1-A** 会话桶页面回收（LRU / 空闲 TTL）+ 修正误导性错误提示 | ✅ |
| 2 | **P1-D** `/debug/dom` 去正文回显，默认仅 `DEEPSEEK_DEBUG=1` 开放 | ✅ |
| 3 | **P1-B** goto 后反查落点（含 `_restore_session_on_startup`） | ✅ |
| 4 | **P2-F** 死分支换成能真正拦住的检查 | ✅ |
| 5 | **P2-E** 落盘文件名去重 | ✅ |
| 6 | **P1-C** `_lock_for` + `PARALLEL_BUCKETS` 开关（**默认仍串行**） | ✅ |
| 7 | **P2-H** 统一就绪等待 + `MAX_SESSION_BUCKETS=0` 语义 | ✅ |
| 8 | **P2-G** 路由测试升级为真 ASGI（需 httpx；当前环境未安装） | ⬜ 可选 |

> 排序理由：先修“会让用户直接撞墙”的和“改动最小”的；P1-C 虽然写在 P1，但**默认不开启就不构成故障**，且开启带风控风险，所以放最后。

---

## 六、快速修复清单（本轮）

- [x] 历史 P0/P1 已全部给出结论（其中 P0-3 属协议固有局限，无法“修完”）
- [x] 149 个单元测试全部通过
- [x] 会话桶页面空闲回收 + LRU 淘汰，并修正“用 /session/reset 回收”的误导提示
- [x] `/debug/dom` 去掉 `head` / `tail`，默认仅 `DEEPSEEK_DEBUG=1` 时开放
- [x] `_ensure_page` / `_restore_session_on_startup` 在 goto 之后用 `_current_session_url()` 反查落点
- [x] `server` 死分支换成“判定真正要发出去的那份 prompt”
- [x] `save_extracted_files` 文件名加唯一后缀（`uuid4().hex[:6]`）
- [x] 按桶加锁（`PARALLEL_BUCKETS`，默认串行）+ 文档说明“分桶≠并发”
- [x] 统一 `_wait_ready()` 与可配的 `READY_TIMEOUT_MS`；写明 `MAX_SESSION_BUCKETS=0` 的含义
- [x] 新增 20 例回归测试（149 例全绿）
- [ ] （可选）引入 httpx 做 ASGI 端到端路由测试
- [ ] 文档侧：附录 A（设计原则 / 已否决方案 / 状态文件格式）在代码变动时同步

---

## 附 A：会话模型速查（设计与语义）

> 本节是上一版文档里被删掉的“设计原则 / 已否决方案 / 状态文件格式”，这里以精简形式恢复 ——
> 这几条是**防止讨论倒退**的依据，不该丢。

**设计原则**：把“是否新开会话”交给**会话自身的健康度与体积**，而不是进程生命周期。
会话会不会过长取决于任务跑了多少轮，与进程重启过几次无关。

**已否决的替代方案（勿重提）**：

| 方案 | 为什么否决 |
| --- | --- |
| 每次启动就新开会话 | 轴选错了（与重启次数无关）；在**没有播种能力**时会退化成“一条孤立消息”的静默失败；开发期重启频繁，等于改完就丢上下文 |
| 超时后用上一个会话继续 | 触发条件反了 —— 到顶最可能就表现为超时，复用等于继续用已经撑爆的会话；“上次错误”跨进程不可靠，**适合当遥测，不适合当决策依据** |

**⚠️ 前置约束**：「新开会话」与「播种上下文」必须**成对出现**，所以任何轮转/分桶之前先要有播种能力。

**当前决策链**：

1. **启动**：默认复用 `SESSION_FILE` 里的会话；仅当 `cap_hit` 或 `DEEPSEEK_NEW_SESSION=true` 时开新会话（首轮播种）。
2. **运行**：超过 `SESSION_MAX_TURNS` / `SESSION_MAX_TOKENS` → 下一轮轮转 + 播种；页面出现「对话长度上限」提示 → `context_length_exceeded`（HTTP 400）+ 置 `pending_rotation`。
3. **重试阶梯**：现有会话 → 恢复**同一个**会话（中间级） → **新会话 + 播种**（最后一级）。
4. **按任务隔离**：`X-DeepSeek-Session`（可用 `SESSION_KEY_HEADER` 改名，`user` 字段兜底）→ 会话桶；每桶独立页面 + 独立状态；`SESSION_SCOPING=false` 关闭分桶。
5. **手动逃生口**：`POST /session/reset[?session=<key>]`，只改状态（`pending_rotation`）→ 下一轮轮转，**仍会播种**。
6. **桶页面回收**：`MAX_SESSION_BUCKETS`（默认 8）满时按 **LRU** 关掉最久未用的页面；空闲超过 `BUCKET_IDLE_TTL_S`（默认 900s）的页面也会被关掉。**只关页面、状态保留**，下次重开同一会话并按落点决定是否播种；正在生成回复的桶不会被回收。
7. **并发**：默认所有桶共用一把锁（**串行**，分桶只隔离上下文）；`PARALLEL_BUCKETS=true` 才按桶各持一把（同时驱动多个网页会话，有风控风险）。

**状态文件格式**（`user_data/.deepseek_session`）：

```jsonc
{
  "url": "https://chat.deepseek.com/a/chat/s/...",  // 默认桶：字段仍在顶层（兼容历史）
  "turns": 12, "est_tokens": 3456, "cap_hit": false,
  "pending_rotation": false, "last_error": null, "updated_at": 1790900000,
  "sessions": { "pi-task-1": { "url": "...", "turns": 3 } }   // 其余会话桶
}
```

旧格式（顶层单会话对象、乃至仅一行 URL 纯文本）仍然可读；默认桶永远在顶层，所以历史行为与旧测试无需改动。**相关配置项速查**：`SESSION_SCOPING` / `SESSION_KEY_HEADER` / `SESSION_KEY_MAX_LEN` / `MAX_SESSION_BUCKETS` / `BUCKET_IDLE_TTL_S` / `PARALLEL_BUCKETS` / `READY_TIMEOUT_MS` / `SESSION_MAX_TURNS` / `SESSION_MAX_TOKENS` / `SEED_MAX_CHARS` / `CAP_CHECK_EVERY` / `CAP_NOTICE_PATTERNS` / `DEEPSEEK_NEW_SESSION` / `DEEPSEEK_TIMEOUT` / `DEEPSEEK_RETRIES`。

---

## 附 B：经验教训（保留并扩充）

连续两次真实故障都出在“**没有测试的启发式逻辑**”，同源于一个错误假设：**用节点数量判断网页是否产生了新回复**。

1. 对接别人的 DOM 时，任何依赖“节点数量 / 句柄身份”的假设都不安全，**应以内容为准**；
2. 启发式逻辑必须配回归测试；
3. 未验证的“看起来更宽容”的参数改动会放大既有故障；
4. **多种失败原因会收敛到同一个症状（超时）**。凡复用外部会话/进程处，都要把“上游明确拒绝”与“上游无响应”区分开；
5. **给单例对象加字段前先问：它属于“进程”还是“任务”？** driver 是全局单例，而 `has_history`/`turns`/`cap_hit` 是**任务级**状态，写死在单例上多任务必互相覆盖 —— 已收进 `SessionState` 并按桶存放，用**属性代理**保住默认桶旧用法；
6. **“重新推导状态”前先问：推导出来的值，真的等于事实吗？** `_ensure_page` 用“有 url”当作“页面真的落在会话里”——这两件事在 99% 的情况下相等，但差距正好落在“会话被删 / 被登出重定向”这条**不会报错、只会瞎答**的路径上。结论：能用**观察**（`_current_session_url()`）验证的事实，就不要用**推导**替代；同时，审阅别人的修复建议时也要先确认它引用的字段/API **真实存在**（本轮就抓出一条引用虚构字段的建议）。
