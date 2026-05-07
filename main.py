from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
from pydantic import BaseModel
import sys
import asyncio
import logging

from browser_utils import start_browser, stop_browser
from search_utils import search_bing_edge, summarize, fetch_page
from settings import HOST, PORT, logger

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Bing Search Agent 正在啟動...")
    await start_browser()
    yield
    await stop_browser()
    logger.info("🛑 Bing Search Agent 已關閉")

app = FastAPI(title="Bing-Search-Agent", lifespan=lifespan)

class SearchRequest(BaseModel):
    query: str
    max_results: int = 5
    skip_llm_split: bool = False
    subquery_count: int = 3

class FetchRequest(BaseModel):
    url: str

@app.post("/search")
async def search(req: SearchRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query 不可為空")

    try:
        raw, subqueries = await search_bing_edge(
            req.query,
            req.max_results,
            skip_llm_split=req.skip_llm_split,
            subquery_count=req.subquery_count,
        )

        summary = await summarize(
            req.query, 
            raw, 
            subqueries if len(subqueries or []) > 1 else None
        )

        return {
            "query": req.query,
            "summary": summary,
            "sources": [item["url"] for item in raw],
            "subqueries": subqueries
        }
    except Exception as e:
        logger.error(f"搜尋處理失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="內部伺服器錯誤")

@app.post("/fetch")
async def fetch(req: FetchRequest):
    if not req.url.strip().startswith("http"):
        raise HTTPException(status_code=400, detail="url 格式不正確")
    
    result = await fetch_page(req.url)
    if result is None:
        raise HTTPException(
            status_code=422, 
            detail="無法抓取該頁面，內容太少或載入失敗"
        )
    return result


# MCP 模式

def run_mcp():
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    mcp_server = Server("bing-search")

    @mcp_server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="bing_search",
                description=(
                    "使用 Bing 搜尋並回傳多個網頁的純文字內容。支援子查詢拆分與智慧搜尋變體。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜尋關鍵字或問題"},
                        "max_results": {
                            "type": "integer",
                            "description": "最多回傳幾個網頁結果（預設 5，上限 10）",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 10,
                        },
                        "skip_llm_split": {
                            "type": "boolean",
                            "description": "是否跳過 LLM 拆分",
                            "default": False,
                        },
                        "subquery_count": {
                            "type": "integer",
                            "description": "子查詢數量",
                            "default": 3,
                            "minimum": 1,
                            "maximum": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="fetch_url",
                description="直接抓取指定 URL 的網頁內容並回傳純文字。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "完整網址（需包含 https://）"},
                    },
                    "required": ["url"],
                },
            ),
        ]

    @mcp_server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if name == "bing_search":
                query = arguments["query"]
                max_results = min(int(arguments.get("max_results", 5)), 10)
                skip = bool(arguments.get("skip_llm_split", False))
                sub_count = min(max(int(arguments.get("subquery_count", 3)), 1), 5)

                contents, _ = await search_bing_edge(
                    query, max_results, skip_llm_split=skip, subquery_count=sub_count
                )

                if not contents:
                    return [TextContent(type="text", text="❌ 未能抓到有效搜尋結果，請稍後再試。")]

                per_page = min(8000 // max(len(contents), 1), 4000)
                parts = [f"### 來源 {i}：{c['url']}\n\n{c['content'][:per_page]}" 
                        for i, c in enumerate(contents, 1)]
                
                output = f"搜尋關鍵字：{query}\n找到 {len(contents)} 個結果\n\n" + "\n\n---\n\n".join(parts)
                return [TextContent(type="text", text=output)]

            elif name == "fetch_url":
                url = arguments["url"]
                result = await fetch_page(url)
                if result is None:
                    return [TextContent(type="text", text=f"❌ 無法抓取 {url}")]
                return [TextContent(type="text", text=f"來源：{result['url']}\n\n{result['content']}")]

        except Exception as e:
            logger.error(f"MCP Tool 執行失敗 {name}: {e}")
            return [TextContent(type="text", text=f"❌ 工具執行失敗: {str(e)}")]

    async def _run():
        await start_browser()
        try:
            async with stdio_server() as (read_stream, write_stream):
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_server.create_initialization_options(),
                )
        finally:
            await stop_browser()

    asyncio.run(_run())


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "http"

    if mode == "mcp":
        run_mcp()
    elif mode == "http":
        import uvicorn
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    else:
        print(f"未知模式：{mode}")
        print("用法：python main.py [http|mcp]")
        sys.exit(1)
