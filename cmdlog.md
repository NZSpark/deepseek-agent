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
