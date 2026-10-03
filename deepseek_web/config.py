"""配置加载与全部可调参数。

所有可调参数集中在这里，默认值即 ``.env.example`` 中列出的那一套。
其他模块通过 ``config.<NAME>`` **在运行时取属性**（而不是 ``from config import NAME``），
这样测试可以直接 ``patch.object(config, "NAME", value)`` 生效。
"""

import os
import re
from pathlib import Path

# ==================== 0. 配置加载 (.env) ====================
# 所有可调参数集中在项目根目录的 .env（模板见 .env.example）。
# 这里用一个极简的 .env 解析器，避免为读取配置引入额外依赖：
#   * 已存在的真实环境变量优先于 .env（便于临时覆盖 / CI）；
#   * 支持 `KEY=value`、`#` 注释、空行、值两侧引号。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


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


# ==================== 服务监听 ====================
HOST = env_str("HOST", "127.0.0.1")
PORT = env_int("PORT", 8000)


# ==================== 路径 ====================
# DeepSeek 网页版的每次对话都归属一个固定会话地址，形如：
#   https://chat.deepseek.com/a/chat/s/<uuid>
# 把最近一次成功的会话地址落盘，超时后可用它重新进入同一个会话，
# 避免上下文丢失或停留在空白页。
SESSION_FILE = Path(env_str("SESSION_FILE", "./user_data/.deepseek_session"))
USER_DATA_DIR = env_str("USER_DATA_DIR", "./user_data")
OUTPUT_DIR = env_str("OUTPUT_DIR", "./output")
SESSION_URL_RE = re.compile(r"https://chat\.deepseek\.com/a/chat/s/[0-9a-fA-F-]+")


# ==================== 运行模式 / 调试 ====================
# 无显示环境（CI / 服务器）可用 HEADLESS=1 启动；首次登录仍需有头模式
HEADLESS = env_bool("HEADLESS")
# 打开后每轮轮询都打印一行状态，便于定位「为什么一直判不到结束」（DEEPSEEK_DEBUG=1）
DEBUG = env_bool("DEEPSEEK_DEBUG")


# ==================== 回复结束检测 / 超时 ====================
# 总超时（秒）：仅在「结束判定完全失灵 / 消息压根没发出去」时才会用到的兜底。
# 必须小于 Pi 侧 HTTP 客户端的超时，否则客户端会先报错。可用 DEEPSEEK_TIMEOUT 覆盖。
RESPONSE_TIMEOUT_S = env_float("DEEPSEEK_TIMEOUT", 180)
# 轮询间隔（秒）
POLL_INTERVAL_S = env_float("POLL_INTERVAL_S", 1.5)
# 兜底判定：内容（忽略首尾空白）完全相同连续这么多次即认为生成结束
STABLE_POLLS = env_int("STABLE_POLLS", 2)
# 次保守的兜底：仅凭“长度不再增长”收尾时要多等几轮，
# 避免生成中途的长停顿（如长思考）被误判成结束
LEN_STABLE_POLLS = env_int("LEN_STABLE_POLLS", 4)
# 快速失败：连续这么多轮「选择器一个回复节点都匹配不到」就判为异常并立刻报错，
# 而不是干等到 RESPONSE_TIMEOUT_S（选择器失效 / 消息没发出去时，等满超时毫无意义）。
NO_NODE_FAIL_POLLS = env_int("NO_NODE_FAIL_POLLS", 10)
# 看门狗：连续这么多轮「既没看到新回复、又没看到生成中状态」就提前结束，
# 避免一直耗到总超时。应显著大于 NO_NODE_FAIL_POLLS，给慢启动留出余地。
NO_PROGRESS_FAIL_POLLS = env_int("NO_PROGRESS_FAIL_POLLS", 40)
# 「继续生成」按钮：网页版一次回复被截断时会显示「继续」按钮，点击后接着生成。
# 打开后，判定本轮生成结束时若页面上还有该按钮，就自动点击并继续收集，
# 直到没有继续按钮为止（拼出完整回复）。
CONTINUE_BUTTON_ENABLED = env_bool("CONTINUE_BUTTON_ENABLED", True)
# 单个会话最多自动点击「继续」多少次，防止按钮常驻 / 反复出现导致无限点击。
CONTINUE_BUTTON_MAX = env_int("CONTINUE_BUTTON_MAX", 5)
# 「继续」按钮文案（大小写不敏感），用于识别底部区域可见的继续控件。
CONTINUE_BUTTON_TEXTS = [
    t.strip()
    for t in env_str("CONTINUE_BUTTON_TEXTS", "继续||continue||继续生成||continue generating").split("||")
    if t.strip()
]


# ==================== 重试 ====================
# 上游超时的最大尝试次数与退避基数（秒）
MAX_UPSTREAM_RETRIES = env_int("DEEPSEEK_RETRIES", 2)
RETRY_BACKOFF_S = env_float("RETRY_BACKOFF_S", 1.0)


# ==================== 会话生命周期 ====================
# 启动时忽略已保存的会话，直接开一个新会话。搭配“播种”使用才安全（首轮会重放历史）。
NEW_SESSION_ON_START = env_bool("DEEPSEEK_NEW_SESSION")
# 「会话到顶」提示语的匹配规则（"||" 分隔多条正则，大小写不敏感）。
# 网页版到顶时会弹提示并停止响应，必须能与“真的卡住”区分开。
CAP_NOTICE_PATTERNS = [
    p.strip()
    for p in env_str(
        "CAP_NOTICE_PATTERNS",
        "达到对话长度上限||对话长度上限||已达到长度限制||达到长度限制||"
        "开启新对话||开始新的聊天||context length limit||start a new chat",
    ).split("||")
    if p.strip()
]
# 每 N 轮轮询检查一次“是否到顶”（避免每轮都对整页做 innerText 扫描）
CAP_CHECK_EVERY = env_int("CAP_CHECK_EVERY", 4)
# 「按任务隔离会话」使用的请求头：同一取值的请求共用一条网页会话，
# 不同取值各自维护独立的会话状态与页面（互不污染上下文）。
SESSION_KEY_HEADER = env_str("SESSION_KEY_HEADER", "X-DeepSeek-Session")
# 关闭后所有请求共用默认会话（旧行为）
SESSION_SCOPING = env_bool("SESSION_SCOPING", True)
# 当请求头与 user 字段都缺失时，是否允许**按 User-Agent 自动分桶**（不同客户端自动隔离）。
# 默认开：不同 AI 编程助手自动各用一条 DeepSeek 会话。关闭后退回旧的「默认桶，全局共用」行为。
# 注意：自动分桶会使桶数随客户端数量增长，实际受 MAX_SESSION_BUCKETS 约束（超出按 LRU 回收页面，状态保留）。
SESSION_SCOPING_BY_UA = env_bool("SESSION_SCOPING_BY_UA", True)
# 单个 key 的长度上限（防止超长头部变成文件名/JSON 键）
SESSION_KEY_MAX_LEN = env_int("SESSION_KEY_MAX_LEN", 64)
# 同时在用的会话桶数量上限。超出时**回收最久未用**的页面（状态保留，下次按 URL 恢复）。
# 0 表示不允许额外会话桶（所有请求都走默认桶）；想彻底关闭分桶用 SESSION_SCOPING=false。
MAX_SESSION_BUCKETS = env_int("MAX_SESSION_BUCKETS", 8)
# 空闲页面的回收间隔（秒）：超过这个时间没被用过的桶页面会被关闭（0 = 不按空闲回收）。
# 页面关掉不等于丢上下文：状态里的 url / turns 仍在，下次会重新打开并决定是否播种。
BUCKET_IDLE_TTL_S = env_float("BUCKET_IDLE_TTL_S", 900)
# 是否允许**按桶并发**（每个会话桶一把锁）。默认 false = 所有桶串行（更安全）。
# 打开后会同时驱动多个网页会话，可能触发风控，请自行评估。
PARALLEL_BUCKETS = env_bool("PARALLEL_BUCKETS")
# 等待某个会话桶锁的最长时间（秒）：0 = 一直等（默认，保持旧行为）。
# >0 时，若同一会话桶已有请求在跑（同一 key 并发/重试堆叠），超过该时间就快速失败，
# 返回「上游繁忙」而不是无限排队、拖到客户端自己超时。不同桶互不影响。
BUCKET_LOCK_TIMEOUT_S = env_float("BUCKET_LOCK_TIMEOUT_S", 0)
# 新建/恢复页面后等待输入框就绪的超时（毫秒）
READY_TIMEOUT_MS = env_int("READY_TIMEOUT_MS", 15000)
# 播种（新会话时重放历史）的最大字符数预算；超出时保留最近的消息
SEED_MAX_CHARS = env_int("SEED_MAX_CHARS", 12000)
# 网页会话超过以下任一阈值后，下一轮自动轮转到新会话（0 表示禁用该维度）
SESSION_MAX_TURNS = env_int("SESSION_MAX_TURNS", 60)
SESSION_MAX_TOKENS = env_int("SESSION_MAX_TOKENS", 60000)


# ==================== Responses API（Codex CLI）====================
# 是否启用 /v1/responses 路由。默认开启；关闭后该端点返回 404，
# 且 /v1/chat/completions（Pi）完全不受影响。
ENABLE_RESPONSES_API = env_bool("ENABLE_RESPONSES_API", True)
# 流式生成期间发送 keep-alive 注释的间隔（秒）；0 = 关闭。
# 网页版生成慢，Codex 侧 stream_idle_timeout_ms 较大时用它保活连接。
RESPONSES_KEEPALIVE_S = env_float("RESPONSES_KEEPALIVE_S", 10.0)
# 工具模式下：是否先缓冲整段回复再判断 tool_calls（true = 需要缓冲，
# 因为要等完整文本才能解析出 function_call；false = 直接透传文本增量）。
RESPONSES_TOOL_BUFFER = env_bool("RESPONSES_TOOL_BUFFER", True)


# ==================== 任务快照（轮转后续接任务）====================
# 是否启用任务快照：每个会话桶在 user_data/.deepseek_tasks/ 下维护一份轻量任务状态
# （任务目标 + 最近进展）。网页会话轮转播种时优先注入它，确保任务目标不被
# SEED_MAX_CHARS 截断，从而“不丢任务”。关闭后回到纯历史播种的旧行为。
TASK_SNAPSHOT_ENABLED = env_bool("TASK_SNAPSHOT_ENABLED", True)
# 任务快照存放目录。
TASK_FILE_DIR = env_str("TASK_FILE_DIR", "./user_data/.deepseek_tasks")
# 任务快照命名空间：同一台机器上可能跑着多个「桥」（如 DeepseekBridge / GeminiBridge），
# 若共用同一个 TASK_FILE_DIR，同名会话桶（default、ua:codex-tui…）会互相覆盖，
# 表现为「A 项目读到 B 项目的任务目标」。
# 默认由本包所在目录名派生（deepseek_web -> deepseek），
# 使不同项目即使共用目录也天然隔离；也可用 TASK_NAMESPACE 显式覆盖。
TASK_NAMESPACE = env_str("TASK_NAMESPACE", "") or (
    Path(__file__).resolve().parent.name.split("_")[0] or "default"
)
# 任务目标（第一条 user 消息）保留的最大字符数；超出截断。
TASK_GOAL_MAX_CHARS = env_int("TASK_GOAL_MAX_CHARS", 2000)
# 快照里滚动保留的最近消息条数（用于“最近进展”）。
TASK_KEEP_MESSAGES = env_int("TASK_KEEP_MESSAGES", 8)


# ==================== DOM 选择器 ====================
# 统一集中在这里，网页版改版时只需改这一处（也可用 .env 覆盖而无需改代码）。
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
