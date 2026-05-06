from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, unquote, urlparse, parse_qs
import asyncio
import logging
import base64
import os
import sys
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 環境變數 ────────────────────────────────────────
LLM_BASE_URL   = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_API_KEY    = os.getenv("LLM_API_KEY", "lm-studio")
LLM_MODEL      = os.getenv("LLM_MODEL", "microsoft/phi-4-mini-reasoning")
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "msedge")   # msedge / chrome
# 注意：部署到無 display 的 Linux 伺服器時請設定 HEADLESS=true
HEADLESS       = os.getenv("HEADLESS", "false").lower() == "true"
HOST           = os.getenv("HOST", "127.0.0.1")
PORT           = int(os.getenv("PORT", "61500"))
USER_AGENT     = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
)

# ── 全域瀏覽器 ─────────────────────────────────────
_playwright = None
_browser: Browser = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _playwright, _browser
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        channel=BROWSER_CHANNEL,
        headless=HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--window-position=-32000,-32000",
            "--window-size=1,1",
        ],
    )
    logger.info(f"瀏覽器已啟動（{'無頭' if HEADLESS else '真視窗（螢幕外）'}模式，channel={BROWSER_CHANNEL}）")
    yield
    await _browser.close()
    await _playwright.stop()


app = FastAPI(title="本地搜尋 API", lifespan=lifespan)

client = AsyncOpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY,
)


class SearchRequest(BaseModel):
    query: str
    max_results: int = 5


def clean_html(html: str, max_chars: int = 13000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines)[:max_chars]


def decode_bing_url(href: str) -> str:
    """解碼 Bing 重定向 URL，失敗回傳空字串。只處理確定是 Bing 格式的 URL。"""
    if not href:
        return ""

    # 格式一：https://www.bing.com/ck/a?!&&p=...&u=a1BASE64...
    if "bing.com/ck/a" in href:
        try:
            parsed = urlparse(href)
            params = parse_qs(parsed.query)
            u = params.get("u", [None])[0]
            if u and u.startswith("a1"):
                encoded = u[2:]
                # 修正：使用更穩健的 padding 計算
                encoded += "=" * (-len(encoded) % 4)
                decoded_bytes = base64.urlsafe_b64decode(encoded)
                decoded = decoded_bytes.decode("utf-8")
                if decoded.startswith("http"):
                    return decoded
        except Exception as e:
            logger.debug(f"decode bing ck/a 失敗: {e}")
        return ""

    # 格式二：a1BASE64（直接 base64）
    if href.startswith("a1"):
        try:
            encoded = href[2:]
            # 修正：使用更穩健的 padding 計算
            encoded += "=" * (-len(encoded) % 4)
            decoded_bytes = base64.urlsafe_b64decode(encoded)
            decoded = decoded_bytes.decode("utf-8")
            if decoded.startswith("http"):
                return decoded
        except Exception as e:
            logger.debug(f"decode a1 失敗: {e}")
        return ""

    # 不是 Bing 重定向格式，直接回傳原始 href
    return href


async def fetch_page(url: str) -> dict | None:
    context = await _browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT)
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)

        # 修正：用 wait_for_load_state 取代硬等 sleep，更精確且更快
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # networkidle timeout 不算失敗，繼續執行

        await page.evaluate("window.scrollBy(0, 1200)")

        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass

        html = await page.content()
        text = clean_html(html)

        if len(text) < 600:
            logger.info(f"內容太少跳過: {url[:60]}")
            return None

        logger.info(f"✅ 成功抓取: {url[:80]} ({len(text)} 字)")
        return {"url": url, "content": text}
    except Exception as e:
        logger.warning(f"❌ 抓取失敗 {url[:60]}: {e}")
        return None
    finally:
        await context.close()


async def get_bing_links(query: str, candidate_count: int = 10) -> list[str]:
    context = await _browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=USER_AGENT)
    try:
        page = await context.new_page()
        url = f"https://www.bing.com/search?q={quote_plus(query)}"
        logger.info(f"🔍 Bing 搜尋：{query}")

        await page.goto(url, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(6)
        await page.evaluate("window.scrollBy(0, 3000)")
        await asyncio.sleep(3)

        links = []

        # ── 方法一：用 JS 直接取 a.href（瀏覽器已解析好的完整 URL）──
        js_links: list[str] = await page.evaluate("""
            () => {
                const selectors = ['li.b_algo h2 a', 'li.b_algo .b_title a', 'li.b_algo a'];
                const seen = new Set();
                const results = [];
                for (const sel of selectors) {
                    for (const a of document.querySelectorAll(sel)) {
                        const href = a.href;
                        if (href && href.startsWith('http') && !href.includes('bing.com') && !seen.has(href)) {
                            seen.add(href);
                            results.push(href);
                        }
                    }
                }
                return results;
            }
        """)
        logger.info(f"JS 直接取得 {len(js_links)} 個連結")
        links.extend(js_links)

        # ── 方法二：若 JS 取得不足，改用 getAttribute + 手動解碼 ──
        if len(links) < candidate_count:
            seen_links = set(links)
            selectors = ["li.b_algo h2 a", "li.b_algo a", "h2 a"]
            for selector in selectors:
                elements = await page.locator(selector).all()
                logger.info(f"選擇器 '{selector}' 找到 {len(elements)} 個（備用解碼）")
                for el in elements:
                    href = await el.get_attribute("href")
                    if not href:
                        continue

                    # 修正：只有確定是 Bing 重定向格式才進解碼，避免正常 URL 被重複處理
                    if "bing.com/ck/a" in href or href.startswith("a1"):
                        href = decode_bing_url(href)

                    if not href or not href.startswith("http") or "bing.com" in href:
                        continue

                    if href not in seen_links:
                        links.append(href)
                        seen_links.add(href)

                    if len(links) >= candidate_count:
                        break
                if len(links) >= candidate_count:
                    break

        links = links[:candidate_count]
        logger.info(f"總共取得 {len(links)} 個有效連結")
        for i, link in enumerate(links[:8]):
            logger.info(f"   {i+1}. {link}")

        return links
    finally:
        await context.close()


async def search_bing_edge(query: str, max_results: int = 5) -> list[dict]:
    # 多抓 3 倍候選，確保過濾後仍有足夠結果
    links = await get_bing_links(query, candidate_count=max_results * 3)
    if not links:
        logger.warning("沒有抓到任何連結")
        return []

    # 修正：改為並發抓取，大幅提升速度
    tasks = [fetch_page(url) for url in links]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    contents = []
    for r in results:
        if len(contents) >= max_results:
            break
        if isinstance(r, dict):
            contents.append(r)
        elif isinstance(r, Exception):
            logger.warning(f"並發抓取發生例外: {r}")

    logger.info(f"最終成功收錄 {len(contents)}/{max_results} 頁")
    return contents


async def summarize(query: str, contents: list[dict]) -> str:
    if not contents:
        return "❌ 這次未能抓到有效內容，請再試一次。"

    # 修正：依實際 contents 數量均分，並設下限避免單頁拿太多
    per_page = min(8000 // max(len(contents), 1), 4000)
    context_text = "\n\n".join([f"來源：{c['url']}\n{c['content'][:per_page]}" for c in contents])

    try:
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
               {"role": "system", "content": (
                    "你是一個知識豐富的問答助手。"
                    "請根據提供的資料回答使用者的問題。"
                    "若問題是查詢單一參數（如核心數、價格、時間、版本號等），直接給出數值，不需要多餘說明。"
                    "若問題需要完整介紹，內容要具體，可包含規格、價格、比較、背景資訊等。"
                    "如果資料中有多個來源說法不同，請說明差異。"
                    "用繁體中文回答，條理清晰，避免不必要的廢話。"
               )},
                {"role": "user", "content": f"使用者的問題是：「{query}」\n\n以下是搜尋到的資料，請根據這些資料回答：\n\n{context_text}"}
            ],
            temperature=0.7,
            max_tokens=2048,
        )
        raw_content = response.choices[0].message.content
        logger.info(f"[LLM] 回傳內容長度：{len(raw_content) if raw_content else 0}")
        logger.info(f"[LLM] 回傳前100字：{repr(raw_content[:100]) if raw_content else 'None'}")
        return raw_content or "❌ LLM 回傳空內容"
    except Exception as e:
        return f"❌ LLM 處理失敗: {str(e)}"


@app.post("/search")
async def search(req: SearchRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query 不可為空")

    raw = await search_bing_edge(req.query, req.max_results)
    summary = await summarize(req.query, raw)

    return {
        "query": req.query,
        "summary": summary,
        "sources": [item["url"] for item in raw],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
