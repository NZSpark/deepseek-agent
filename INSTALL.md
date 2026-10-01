# DeepSeek Web-to-API Bridge

A local bridge that exposes the DeepSeek web app as an OpenAI-compatible API.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python deepseek_api_server.py
```

Then complete login in the opened Chromium window. See README.md for details.
