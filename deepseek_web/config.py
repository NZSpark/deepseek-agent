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
# 单个 key 的长度上限（防止超长头部变成文件名/JSON 键）
SESSION_KEY_MAX_LEN = env_int("SESSION_KEY_MAX_LEN", 64)
MAX_SESSION_BUCKETS = env_int("MAX_SESSION_BUCKETS", 8)
# 播种（新会话时重放历史）的最大字符数预算；超出时保留最近的消息
SEED_MAX_CHARS = env_int("SEED_MAX_CHARS", 12000)
# 网页会话超过以下任一阈值后，下一轮自动轮转到新会话（0 表示禁用该维度）
SESSION_MAX_TURNS = env_int("SESSION_MAX_TURNS", 60)
SESSION_MAX_TOKENS = env_int("SESSION_MAX_TOKENS", 60000)


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
