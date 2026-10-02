# 环境搭建命令备忘

依赖以 `requirements.txt` 为准，不要单独手装某一个包。

```bash
python3 -m venv .venv
source .venv/bin/activate

# 安装全部依赖
uv pip install -r requirements.txt
# 若未安装 uv，可替换为：pip install -r requirements.txt

# 下载 Playwright 所需的 Chromium
playwright install chromium
```

## 运行

```bash
python deepseek_api_server.py            # 有头模式（首次登录需要）
HEADLESS=1 python deepseek_api_server.py # 无显示环境（需已登录过）
```

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

## 自检 / 诊断

```bash
curl -s localhost:8000/healthz | python -m json.tool          # 就绪状态 + 各会话桶进度
curl -s localhost:8000/debug/dom | python -m json.tool        # 真实 DOM：回复节点 / 疑似停止按钮
curl -X POST "localhost:8000/session/reset?session=my-task"   # 重置某个任务的会话（仍会播种历史）
```
