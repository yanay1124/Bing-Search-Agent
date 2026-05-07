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
import random
import time
import itertools
import json
from collections import deque, OrderedDict
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 環境變數 ────────────────────────────────────────
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "lm-studio")
LLM_MODEL = os.getenv("LLM_MODEL", "microsoft/phi-4-mini-reasoning")
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "msedge")  # msedge / chrome
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "61500"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
)
REQUESTS_PER_MINUTE = int(os.getenv("REQUESTS_PER_MINUTE", "40"))
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "3"))
RANDOM_DELAY_MIN = float(os.getenv("RANDOM_DELAY_MIN", "1.0"))
RANDOM_DELAY_MAX = float(os.getenv("RANDOM_DELAY_MAX", "3.0"))
PROXY_LIST = [
    p.strip()
    for p in os.getenv("PROXY_LIST", "").replace(";", ",").replace("\n", ",").split(",")
    if p.strip()
]
PROXY_CYCLE = itertools.cycle(PROXY_LIST) if PROXY_LIST else None
STEALTH_MODE = os.getenv("STEALTH_MODE", "true").lower() in ("1", "true", "yes")

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))
CACHE_MAX_ITEMS = int(os.getenv("CACHE_MAX_ITEMS", "500"))
CACHE_CLEAN_INTERVAL = int(os.getenv("CACHE_CLEAN_INTERVAL", "60"))

# ── 全域瀏覽器 ────────────────────────────────────────
_playwright = None
_browser: Browser = None
_rate_limiter = None
_CACHE_CLEANER_TASK: asyncio.Task | None = None

# ── 快取 ────────────────────────────────────────
SEARCH_CACHE: OrderedDict[str, tuple[float, list[dict], list[str]]] = OrderedDict()
PAGE_CACHE: OrderedDict[str, tuple[float, dict]] = OrderedDict()
SUBQUERY_CACHE: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()
LINK_CACHE: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()

SEARCH_CACHE_LOCK = asyncio.Lock()
PAGE_CACHE_LOCK = asyncio.Lock()
SUBQUERY_CACHE_LOCK = asyncio.Lock()
LINK_CACHE_LOCK = asyncio.Lock()


async def start_browser():
    global _playwright, _browser, _rate_limiter, _CACHE_CLEANER_TASK
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        channel=BROWSER_CHANNEL,
        headless=HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-features=IsolateOrigins,site-per-process",
            "--window-position=-32000,-32000",
            "--window-size=1280,900",
        ],
    )
    _rate_limiter = RateLimiter(REQUESTS_PER_MINUTE, 60, MAX_CONCURRENT_REQUESTS)
    _CACHE_CLEANER_TASK = asyncio.create_task(_run_cache_cleaner())
    logger.info(
        f"瀏覽器已啟動（{'無頭' if HEADLESS else '真視窗（螢幕外）'}模式，channel={BROWSER_CHANNEL}），"
        f"限速={REQUESTS_PER_MINUTE}/分鐘，最大併發={MAX_CONCURRENT_REQUESTS}，隨機延遲={RANDOM_DELAY_MIN}-{RANDOM_DELAY_MAX}s"
    )


async def stop_browser():
    global _browser, _playwright, _CACHE_CLEANER_TASK
    if _CACHE_CLEANER_TASK:
        _CACHE_CLEANER_TASK.cancel()
        try:
            await _CACHE_CLEANER_TASK
        except asyncio.CancelledError:
            pass
        _CACHE_CLEANER_TASK = None
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()


class RateLimiter:
    def __init__(self, max_calls: int, period: float, max_concurrency: int):
        self.max_calls = max_calls
        self.period = period
        self.timestamps = deque()
        self.lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(max_concurrency)

    async def acquire(self):
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.timestamps and now - self.timestamps[0] >= self.period:
                    self.timestamps.popleft()
                if len(self.timestamps) < self.max_calls:
                    self.timestamps.append(now)
                    return
                wait = self.period - (now - self.timestamps[0])
            await asyncio.sleep(wait)

    @asynccontextmanager
    async def limit(self):
        async with self.semaphore:
            await self.acquire()
            yield

    async def run(self, coro, *args, **kwargs):
        async with self.limit():
            return await coro(*args, **kwargs)


def get_next_proxy() -> str | None:
    if not PROXY_CYCLE:
        return None
    return next(PROXY_CYCLE)


async def random_delay():
    if RANDOM_DELAY_MAX <= 0 or RANDOM_DELAY_MIN <= 0:
        return
    delay = random.uniform(RANDOM_DELAY_MIN, RANDOM_DELAY_MAX)
    if delay > 0:
        logger.info(f"⏱ 隨機延遲 {delay:.2f} 秒")
        await asyncio.sleep(delay)


async def apply_stealth_tools(context):
    if not STEALTH_MODE:
        return
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => false});
        window.navigator.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters);
    """)


async def create_context(proxy_url: str | None = None, viewport: dict | None = None):
    options = {
        "viewport": viewport or {"width": 1280, "height": 900},
        "user_agent": USER_AGENT,
        "locale": "en-US",
        "timezone_id": "Asia/Taipei",
        "java_script_enabled": True,
        "bypass_csp": True,
        "ignore_https_errors": True,
    }
    if proxy_url:
        options["proxy"] = {"server": proxy_url}
        logger.info(f"🔀 使用代理: {proxy_url}")
    context = await _browser.new_context(**options)
    await apply_stealth_tools(context)
    return context


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

    if "bing.com/ck/a" in href:
        try:
            parsed = urlparse(href)
            params = parse_qs(parsed.query)
            u = params.get("u", [None])[0]
            if u and u.startswith("a1"):
                encoded = u[2:]
                encoded += "=" * (-len(encoded) % 4)
                decoded_bytes = base64.urlsafe_b64decode(encoded)
                decoded = decoded_bytes.decode("utf-8")
                if decoded.startswith("http"):
                    return decoded
        except Exception as e:
            logger.debug(f"decode bing ck/a 失敗: {e}")
        return ""

    if href.startswith("a1"):
        try:
            encoded = href[2:]
            encoded += "=" * (-len(encoded) % 4)
            decoded_bytes = base64.urlsafe_b64decode(encoded)
            decoded = decoded_bytes.decode("utf-8")
            if decoded.startswith("http"):
                return decoded
        except Exception as e:
            logger.debug(f"decode a1 失敗: {e}")
        return ""

    return href


def _is_cache_valid(entry: tuple[float, object]) -> bool:
    return entry[0] > time.monotonic()


async def _prune_ordered_dict(
    cache: OrderedDict, lock: asyncio.Lock, keep_max: int = CACHE_MAX_ITEMS
):
    async with lock:
        keys_to_remove = []
        now = time.monotonic()
        for key, value in list(cache.items()):
            if value[0] <= now:
                keys_to_remove.append(key)
        for key in keys_to_remove:
            cache.pop(key, None)
        while len(cache) > keep_max:
            cache.popitem(last=False)


async def _run_cache_cleaner():
    try:
        while True:
            await asyncio.sleep(CACHE_CLEAN_INTERVAL)
            await _prune_ordered_dict(SEARCH_CACHE, SEARCH_CACHE_LOCK)
            await _prune_ordered_dict(PAGE_CACHE, PAGE_CACHE_LOCK)
            await _prune_ordered_dict(SUBQUERY_CACHE, SUBQUERY_CACHE_LOCK)
            await _prune_ordered_dict(LINK_CACHE, LINK_CACHE_LOCK)
    except asyncio.CancelledError:
        pass


async def _get_cache(
    cache: OrderedDict,
    lock: asyncio.Lock,
    key: str,
    default=None,
):
    async with lock:
        entry = cache.get(key)
        if entry and entry[0] > time.monotonic():
            cache.move_to_end(key)
            return entry[1]
        if entry:
            cache.pop(key, None)
    return default


async def _set_cache(
    cache: OrderedDict,
    lock: asyncio.Lock,
    key: str,
    value: object,
    ttl: int = CACHE_TTL_SECONDS,
):
    async with lock:
        cache[key] = (time.monotonic() + ttl, value)
        cache.move_to_end(key)
        while len(cache) > CACHE_MAX_ITEMS:
            cache.popitem(last=False)


async def fetch_page(url: str) -> dict | None:
    cached = await _get_cache(PAGE_CACHE, PAGE_CACHE_LOCK, url)
    if cached is not None:
        return cached

    proxy_url = get_next_proxy()
    async with _rate_limiter.limit():
        await random_delay()
        context = await create_context(proxy_url=proxy_url, viewport={"width": 1280, "height": 900})
        try:
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await page.evaluate("window.scrollBy(0, 1200)")
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            html = await page.content()
            text = clean_html(html)
            if len(text) < 600:
                logger.info(f"內容太少跳過: {url[:60]}")
                return None
            result = {"url": url, "content": text}
            await _set_cache(PAGE_CACHE, PAGE_CACHE_LOCK, url, result)
            logger.info(f"✅ 成功抓取: {url[:80]} ({len(text)} 字)")
            return result
        except Exception as e:
            logger.warning(f"❌ 抓取失敗 {url[:60]}: {e}")
            return None
        finally:
            await context.close()


async def get_bing_links(query: str, candidate_count: int = 10) -> list[str]:
    cache_key = json.dumps(
        {"query": query, "candidate_count": candidate_count},
        sort_keys=True,
        ensure_ascii=False,
    )
    cached = await _get_cache(LINK_CACHE, LINK_CACHE_LOCK, cache_key)
    if cached is not None:
        return cached

    proxy_url = get_next_proxy()
    async with _rate_limiter.limit():
        await random_delay()
        context = await create_context(proxy_url=proxy_url, viewport={"width": 1920, "height": 1080})
        try:
            page = await context.new_page()
            url = f"https://www.bing.com/search?q={quote_plus(query)}"
            logger.info(f"🔍 Bing 搜尋：{query}")

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector("li.b_algo", timeout=8000)
            except Exception:
                pass
            await page.evaluate("window.scrollBy(0, 2000)")
            await asyncio.sleep(1)

            links = []

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
            await _set_cache(LINK_CACHE, LINK_CACHE_LOCK, cache_key, links)
            logger.info(f"總共取得 {len(links)} 個有效連結")
            for i, link in enumerate(links[:8]):
                logger.info(f"   {i+1}. {link}")
            return links
        finally:
            await context.close()


async def infer_subqueries(query: str, subquery_count: int = 3) -> list[str]:
    cache_key = json.dumps(
        {"query": query, "subquery_count": subquery_count},
        sort_keys=True,
        ensure_ascii=False,
    )
    cached = await _get_cache(SUBQUERY_CACHE, SUBQUERY_CACHE_LOCK, cache_key)
    if cached is not None:
        return cached

    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    prompt = (
        "你是一個專門把使用者查詢拆成搜尋子查詢的助手。\n"
        "請判斷下面的查詢是否包含多個問題，並回傳最多 "
        f"{subquery_count} 個適合直接送到搜尋引擎的子查詢。\n"
        "回傳格式請只使用 JSON 陣列，例如 [\"子查詢一\", \"子查詢二\"]。\n"
        "如果這個查詢只有一個問題，請回傳原始查詢本身。不要生成答案，不要附加說明。\n\n"
        f"使用者查詢：{query}"
    )
    try:
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": prompt}],
            temperature=0.0,
            max_tokens=256,
        )
        text = response.choices[0].message.content.strip()
        subqueries: list[str] = []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, str):
                parsed = [parsed]
            if isinstance(parsed, list):
                subqueries = [item.strip() for item in parsed if isinstance(item, str) and item.strip()]
        except Exception:
            pass
        if not subqueries:
            lines = [line.strip(" \t\n\r-•.1234567890") for line in text.splitlines() if line.strip()]
            subqueries = [line for line in lines if len(line) > 10]
        if not subqueries:
            subqueries = [query]
        subqueries = subqueries[:subquery_count]
        await _set_cache(SUBQUERY_CACHE, SUBQUERY_CACHE_LOCK, cache_key, subqueries)
        return subqueries
    except Exception as e:
        logger.warning(f"LLM 拆分子查詢失敗：{e}")
        return [query]


async def search_single_query(query: str, max_results: int = 5) -> list[dict]:
    links = await get_bing_links(query, candidate_count=max_results * 3)
    if not links:
        logger.warning("沒有抓到任何連結")
        return []
    tasks = [fetch_page(url) for url in links]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    contents: list[dict] = []
    for r in results:
        if len(contents) >= max_results:
            break
        if isinstance(r, dict):
            contents.append(r)
        elif isinstance(r, Exception):
            logger.warning(f"並發抓取發生例外: {r}")
    logger.info(f"最終成功收錄 {len(contents)}/{max_results} 頁")
    return contents


async def _get_search_cache(
    query: str,
    max_results: int,
    skip_llm_split: bool,
    subquery_count: int,
) -> tuple[list[dict], list[str]] | None:
    key = json.dumps(
        {
            "query": query,
            "max_results": max_results,
            "skip_llm_split": skip_llm_split,
            "subquery_count": subquery_count,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return await _get_cache(SEARCH_CACHE, SEARCH_CACHE_LOCK, key)


async def _set_search_cache(
    query: str,
    max_results: int,
    skip_llm_split: bool,
    subquery_count: int,
    results: list[dict],
    subqueries: list[str],
):
    key = json.dumps(
        {
            "query": query,
            "max_results": max_results,
            "skip_llm_split": skip_llm_split,
            "subquery_count": subquery_count,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    await _set_cache(SEARCH_CACHE, SEARCH_CACHE_LOCK, key, (results, subqueries))


async def search_bing_edge(
    query: str,
    max_results: int = 5,
    skip_llm_split: bool = False,
    subquery_count: int = 3,
) -> tuple[list[dict], list[str]]:
    cached = await _get_search_cache(query, max_results, skip_llm_split, subquery_count)
    if cached is not None:
        return cached

    if skip_llm_split:
        results = await search_single_query(query, max_results)
        await _set_search_cache(query, max_results, skip_llm_split, subquery_count, results, [query])
        return results, [query]

    subqueries = await infer_subqueries(query, subquery_count)
    if len(subqueries) <= 1:
        results = await search_single_query(query, max_results)
        await _set_search_cache(query, max_results, skip_llm_split, subquery_count, results, subqueries)
        return results, subqueries

    logger.info(f"LLM 判斷為多子查詢，共 {len(subqueries)} 個：{subqueries}")
    active_subqueries = subqueries[:subquery_count]
    slots = [max_results // len(active_subqueries)] * len(active_subqueries)
    for i in range(max_results % len(active_subqueries)):
        slots[i] += 1

    results: list[dict] = []
    seen_urls = set()
    for subquery, slot in zip(active_subqueries, slots):
        if slot <= 0:
            continue
        sub_results = await search_single_query(subquery, slot)
        for item in sub_results:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                results.append(item)
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    if len(results) < max_results:
        fallback = await search_single_query(query, max_results)
        for item in fallback:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                results.append(item)
            if len(results) >= max_results:
                break

    final_results = results[:max_results]
    await _set_search_cache(
        query, max_results, skip_llm_split, subquery_count, final_results, subqueries
    )
    return final_results, subqueries


async def summarize(query: str, contents: list[dict], subqueries: list[str] | None = None) -> str:
    if not contents:
        return "❌ 這次未能抓到有效內容，請再試一次。"
    per_page = min(8000 // max(len(contents), 1), 1800)
    context_text = "\n\n".join(
        [f"來源：{c['url']}\n{c['content'][:per_page]}" for c in contents]
    )
    extra_intro = ""
    if subqueries and len(subqueries) > 1:
        extra_intro = "使用者的查詢已拆成以下子查詢：\n"
        extra_intro += "\n".join(f"{idx+1}. {q}" for idx, q in enumerate(subqueries))
        extra_intro += "\n\n"

    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    try:
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一個知識豐富的問答助手。"
                        "請根據提供的資料回答使用者的問題。"
                        "如果問題已拆成多個子查詢，請逐一回答每一個子查詢，並標示清楚。"
                        "若問題是查詢單一參數（如核心數、價格、時間、版本號等），直接給出數值，不需要多餘說明。"
                        "若問題需要完整介紹，內容要具體，可包含規格、價格、比較、背景資訊等。"
                        "如果資料中有多個來源說法不同，請說明差異。"
                        "用繁體中文回答，條理清晰，避免不必要的廢話。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{extra_intro}"
                        f"使用者的問題是：「{query}」\n\n"
                        f"以下是搜尋到的資料，請根據這些資料回答：\n\n{context_text}"
                    ),
                },
            ],
            temperature=0.7,
            max_tokens=1400,
        )
        raw_content = response.choices[0].message.content
        logger.info(
            f"[LLM] 回傳內容長度：{len(raw_content) if raw_content else 0}"
        )
        logger.info(
            f"[LLM] 回傳前100字：{repr(raw_content[:100]) if raw_content else 'None'}"
        )
        return raw_content or "❌ LLM 回傳空內容"
    except Exception as e:
        return f"❌ LLM 處理失敗: {str(e)}"


# ══════════════════════════════════════════════════
# HTTP 模式（FastAPI）
# ══════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_browser()
    yield
    await stop_browser()


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
    raw, subqueries = await search_bing_edge(
        req.query,
        req.max_results,
        skip_llm_split=req.skip_llm_split,
        subquery_count=req.subquery_count,
    )
    summary = await summarize(
        req.query, raw, subqueries if len(subqueries or []) > 1 else None
    )
    return {
        "query": req.query,
        "summary": summary,
        "sources": [item["url"] for item in raw],
    }


@app.post("/fetch")
async def fetch(req: FetchRequest):
    if not req.url.strip().startswith("http"):
        raise HTTPException(status_code=400, detail="url 格式不正確")
    result = await fetch_page(req.url)
    if result is None:
        raise HTTPException(
            status_code=422, detail="無法抓取該頁面，內容太少或載入失敗"
        )
    return result


# ══════════════════════════════════════════════════
# MCP 模式
# ══════════════════════════════════

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
                    "使用 Bing 搜尋並回傳多個網頁的純文字內容。"
                    "適合需要最新資訊、新聞、產品規格或任何需要上網查詢的問題。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜尋關鍵字或問題",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "最多回傳幾個網頁結果（預設 5，上限 10）",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 10,
                        },
                        "skip_llm_split": {
                            "type": "boolean",
                            "description": "是否跳過 LLM 拆分，使用原始單次搜尋模式。",
                            "default": False,
                        },
                        "subquery_count": {
                            "type": "integer",
                            "description": "LLM 拆分時要生成的子查詢數量。",
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
                        "url": {
                            "type": "string",
                            "description": "要抓取的完整網址（需包含 https://）",
                        },
                    },
                    "required": ["url"],
                },
            ),
        ]

    @mcp_server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name == "bing_search":
            query = arguments["query"]
            max_results = min(int(arguments.get("max_results", 5)), 10)
            skip_llm_split = bool(arguments.get("skip_llm_split", False))
            subquery_count = min(max(int(arguments.get("subquery_count", 3)), 1), 5)
            contents, _ = await search_bing_edge(
                query,
                max_results,
                skip_llm_split=skip_llm_split,
                subquery_count=subquery_count,
            )
            if not contents:
                return [TextContent(type="text", text="❌ 未能抓到有效搜尋結果，請稍後再試。")]
            per_page = min(8000 // max(len(contents), 1), 4000)
            parts = [
                f"### 來源 {i}：{c['url']}\n\n{c['content'][:per_page]}"
                for i, c in enumerate(contents, 1)
            ]
            output = (
                f"搜尋關鍵字：{query}\n找到 {len(contents)} 個結果\n\n"
                + "\n\n---\n\n".join(parts)
            )
            return [TextContent(type="text", text=output)]

        elif name == "fetch_url":
            url = arguments["url"]
            result = await fetch_page(url)
            if result is None:
                return [
                    TextContent(
                        type="text",
                        text=f"❌ 無法抓取 {url}，內容太少或載入失敗。",
                    )
                ]
            return [
                TextContent(
                    type="text",
                    text=f"來源：{result['url']}\n\n{result['content']}",
                )
            ]

        else:
            return [TextContent(type="text", text=f"❌ 未知工具：{name}")]

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


# ══════════════════════════════════════════
# 入口點
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "http"

    if mode == "mcp":
        run_mcp()
    elif mode == "http":
        import uvicorn
        uvicorn.run(app, host=HOST, port=PORT)
    else:
        print(f"未知模式：{mode}")
        print("用法：python main.py [http|mcp]")
        sys.exit(1)