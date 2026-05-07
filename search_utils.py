import asyncio
import base64
import json
import re
from collections import deque
from urllib.parse import quote_plus, urlparse, parse_qs
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from browser_utils import create_context, get_next_proxy, get_rate_limiter, random_delay
from cache_utils import (
    LINK_CACHE,
    LINK_CACHE_LOCK,
    PAGE_CACHE,
    PAGE_CACHE_LOCK,
    SEARCH_CACHE,
    SEARCH_CACHE_LOCK,
    SUBQUERY_CACHE,
    SUBQUERY_CACHE_LOCK,
    _get_cache,
    _set_cache,
)
from settings import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, logger

TRANSPORT_KEYWORDS = [
    "火車",
    "高鐵",
    "台鐵",
    "捷運",
    "列車",
    "時刻表",
    "班次",
    "車次",
    "票",
    "時刻",
    "時間",
]

PRODUCT_KEYWORDS = [
    "手機",
    "筆電",
    "電腦",
    "相機",
    "耳機",
    "價格",
    "規格",
    "型號",
    "評測",
    "比較",
]

NEWS_KEYWORDS = [
    "新聞",
    "最新",
    "發布",
    "更新",
    "事件",
    "事故",
]

GENERAL_SUFFIXES = ["詳細資訊", "官方網站", "說明", "介紹"]


def clean_html(html: str, max_chars: int = 13000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines)[:max_chars]


def decode_bing_url(href: str) -> str:
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


def is_transport_query(query: str) -> bool:
    lower_query = query.lower()
    return any(keyword in lower_query for keyword in TRANSPORT_KEYWORDS)


def is_product_query(query: str) -> bool:
    lower_query = query.lower()
    return any(keyword in lower_query for keyword in PRODUCT_KEYWORDS)


def is_news_query(query: str) -> bool:
    lower_query = query.lower()
    return any(keyword in lower_query for keyword in NEWS_KEYWORDS)


def build_search_variants(query: str, max_variations: int = 4) -> list[str]:
    base = query.strip()
    if not base:
        return [base]

    variants = [base]
    if is_transport_query(base) or re.search(r"明天|後天|今天|上午|下午|晚上|\d{1,2}點|\d{1,2}:\d{2}", base):
        suffixes = ["時刻表", "班次", "票價", "查詢"]
    elif is_product_query(base):
        suffixes = ["價格", "規格", "評測", "比較"]
    elif is_news_query(base):
        suffixes = ["最新消息", "新聞", "更新", "事件"]
    else:
        suffixes = GENERAL_SUFFIXES

    for suffix in suffixes:
        if len(variants) >= max_variations:
            break
        if suffix not in base:
            candidate = f"{base} {suffix}"
            if candidate not in variants:
                variants.append(candidate)

    return variants[:max_variations]


async def fetch_page(url: str) -> dict | None:
    cached = await _get_cache(PAGE_CACHE, PAGE_CACHE_LOCK, url)
    if cached is not None:
        return cached

    proxy_url = get_next_proxy()
    rate_limiter = get_rate_limiter()
    async with rate_limiter.limit():
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
    rate_limiter = get_rate_limiter()
    async with rate_limiter.limit():
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
        "這些子查詢應該盡量讓搜尋結果精準找到具體時間、班次、價格、規格或其他數據。\n"
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
    variants = build_search_variants(query, max_variations=4)
    logger.info(f"🔎 搜尋變體：{variants}")

    seen_links = set()
    links: list[str] = []
    for variant in variants:
        if len(links) >= max_results * 3:
            break
        variant_links = await get_bing_links(variant, candidate_count=max_results * 2)
        for link in variant_links:
            if link not in seen_links:
                seen_links.add(link)
                links.append(link)
        if len(links) >= max_results * 3:
            break

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
