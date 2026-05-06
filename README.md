>本專部分程式碼與文件由 AI 產生，我負責整體架構、除錯、測試與強化。


# Bing Search Agent

Local Bing search service that fetches search results via Playwright and summarizes answers using local LLM. Supports both MCP Server and HTTP API access methods.

## Requirements

- Python 3.10+
- Microsoft Edge or Google Chrome
- Local LLM service (LM Studio, Ollama, etc., compatible with OpenAI API format)

## Installation

```bash
pip install fastapi uvicorn playwright beautifulsoup4 openai python-dotenv pydantic "mcp==1.6.0" "starlette>=0.40.0,<0.48.0"
playwright install
```

> mcp >= 1.7 will conflict with fastapi's starlette version, please use mcp==1.6.0.

## Configuration

Copy `.env.example` to `.env` and fill in the settings:

```env
LLM_BASE_URL=http://localhost:1234/v1
LLM_API_KEY=lm-studio
LLM_MODEL=your-model-name

BROWSER_CHANNEL=msedge   # msedge or chrome
HEADLESS=false

HOST=127.0.0.1
PORT=61500
```

## Starting

**MCP Mode** (Claude Desktop, Cursor, Windsurf)

```bash
python main.py mcp
```

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "bing-search": {
      "command": "python",
      "args": ["C:/path/to/main.py", "mcp"]
    }
  }
}
```

**HTTP Mode** (LangChain, AutoGen, OpenClaw)

```bash
python main.py http
```

After starting:
- API Docs: `http://127.0.0.1:61500/docs`
- Tool Manifest: `http://127.0.0.1:61500/.well-known/ai-plugin.json`

## API

`POST /search` — Search and summarize

```json
{ "query": "Taiwan AI startups", "max_results": 5 }
```

`POST /fetch` — Fetch specific page content

```json
{ "url": "https://example.com/article" }
```

## License

Apache-2.0


# 本地 Bing 搜尋服務

本地 Bing 搜尋服務，透過 Playwright 抓取搜尋結果並以本地 LLM 摘要回答問題。支援 MCP Server 與 HTTP API 兩種接入方式。

## 需求

- Python 3.10+
- Microsoft Edge 或 Google Chrome
- 本地 LLM 服務（LM Studio、Ollama 等，需相容 OpenAI API 格式）

## 安裝

```bash
pip install fastapi uvicorn playwright beautifulsoup4 openai python-dotenv pydantic "mcp==1.6.0" "starlette>=0.40.0,<0.48.0"
playwright install
```

> mcp >= 1.7 會與 fastapi 產生 starlette 版本衝突，請固定使用 mcp==1.6.0。

## 設定

複製 `.env.example` 為 `.env` 並填入設定：

```env
LLM_BASE_URL=http://localhost:1234/v1
LLM_API_KEY=lm-studio
LLM_MODEL=your-model-name

BROWSER_CHANNEL=msedge   # msedge 或 chrome
HEADLESS=false

HOST=127.0.0.1
PORT=61500
```

## 啟動

**MCP 模式**（Claude Desktop、Cursor、Windsurf）

```bash
python main.py mcp
```

在 `claude_desktop_config.json` 加入：

```json
{
  "mcpServers": {
    "bing-search": {
      "command": "python",
      "args": ["C:/path/to/main.py", "mcp"]
    }
  }
}
```

**HTTP 模式**（LangChain、AutoGen、OpenClaw）

```bash
python main.py http
```

啟動後：
- API 文件：`http://127.0.0.1:61500/docs`
- Tool Manifest：`http://127.0.0.1:61500/.well-known/ai-plugin.json`

## API

`POST /search` — 搜尋並摘要

```json
{ "query": "台灣 AI 新創", "max_results": 5 }
```

`POST /fetch` — 抓取指定頁面內容

```json
{ "url": "https://example.com/article" }
```

## License

[Apache-2.0](LICENSE)
