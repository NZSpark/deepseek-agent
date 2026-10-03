"""Driver 层异常与常量（从 driver.py 拆出，避免循环依赖）。

这些异常与常量原先定义在 ``deepseek_web.driver`` 里，外部（server / tests）
一直从该处导入；``driver.py`` 会原样 re-export，保持导入路径不变。
"""


class DeepSeekTimeoutError(RuntimeError):
    """等待网页版回复超时。区别于普通运行时错误，可触发会话恢复。"""


class DeepSeekContextLimitError(RuntimeError):
    """网页会话已达上下文长度上限（网页版会停止响应，必须换新会话）。"""


class DeepSeekBusyError(RuntimeError):
    """某个会话桶正忙（同一会话已有请求在跑且等待超时）。

    与「上游出错」区分开：这是本地的排队保护，客户端稍后重试即可，
    因此会被映射成 HTTP 503 / SSE ``upstream_busy``，而**不会**触发重试阶梯。
    """


# 未指定任务标识时使用的会话桶（保持与历史行为一致：全局共用一条会话）
DEFAULT_SESSION_KEY = "default"
# DeepSeek 首页（新会话的入口）
HOME_URL = "https://chat.deepseek.com/"
