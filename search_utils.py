import asyncio
import base64
import json
import re
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
                    elements = await page.query_selector_all(selector)
                    for el in elements:
                        href = await el.get_attribute("href") or ""
                        href = decode_bing_url(href)
                        if (
                            href.startswith("http")
                            and "bing.com" not in href
                            and href not in seen_links
                        ):
                            seen_links.add(href)
                            links.append(href)
                        if len(links) >= candidate_count:
                            break
                    if len(links) >= candidate_count:
                        break

            links = links[:candidate_count]
            await _set_cache(LINK_CACHE, LINK_CACHE_LOCK, cache_key, links)
            logger.info(f"共收集 {len(links)} 個連結")
            return links
        except Exception as e:
            logger.warning(f"Bing 搜尋失敗：{e}")
            return []
        finally:
            await context.close()


async def infer_subqueries(query: str, subquery_count: int = 3) -> list[str]:
    """
    讓 LLM 判斷查詢是否包含多個獨立問題，若是則拆分成獨立子查詢。
    每個子查詢應可直接送入搜尋引擎。若只有一個問題，回傳原始查詢。
    """
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
        "你是一個搜尋查詢分析助手。\n"
        "請判斷下面的查詢是否同時包含多個彼此獨立的問題"
        "（例如：問了兩件不同的事情、比較兩個不同主題，或同時詢問多個不相關的資訊）。\n\n"
        "規則：\n"
        "- 若查詢只有一個核心問題，直接回傳原始查詢（包在 JSON 陣列裡）。\n"
        f"- 若查詢包含多個獨立問題，請拆分成最多 {subquery_count} 個子查詢，"
        "每個子查詢需能獨立送入搜尋引擎並取得有效結果。\n"
        "- 子查詢保留原始語言，不要翻譯或改寫語意。\n"
        "- 不要憑空新增查詢內容沒提到的資訊。\n\n"
        "只回傳 JSON 陣列，例如：[\"子查詢一\", \"子查詢二\"]。不要附加任何說明文字。\n\n"
        f"使用者查詢：{query}"
    )
    try:
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=256,
        )
        text = response.choices[0].message.content.strip()
        subqueries: list[str] = []
        try:
            # 嘗試擷取 JSON 陣列（處理模型可能在前後加說明文字的情況）
            match = re.search(r'\[.*?\]', text, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
            else:
                parsed = json.loads(text)
            if isinstance(parsed, str):
                parsed = [parsed]
            if isinstance(parsed, list):
                subqueries = [item.strip() for item in parsed if isinstance(item, str) and item.strip()]
        except Exception:
            pass
        if not subqueries:
            lines = [line.strip(" \t\n\r-•.1234567890") for line in text.splitlines() if line.strip()]
            subqueries = [line for line in lines if len(line) > 5]
        if not subqueries:
            subqueries = [query]
        subqueries = subqueries[:subquery_count]
        await _set_cache(SUBQUERY_CACHE, SUBQUERY_CACHE_LOCK, cache_key, subqueries)
        return subqueries
    except Exception as e:
        logger.warning(f"LLM 拆分子查詢失敗：{e}")
        return [query]


async def search_single_query(query: str, max_results: int = 5) -> list[dict]:
    """直接用單一查詢搜尋，不加任何後綴變體。"""
    logger.info(f"🔎 搜尋：{query}")
    links = await get_bing_links(query, candidate_count=max_results * 2)

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
        # 單一問題：直接搜尋
        results = await search_single_query(query, max_results)
        await _set_search_cache(query, max_results, skip_llm_split, subquery_count, results, subqueries)
        return results, subqueries

    # 多個獨立問題：依子查詢數量平均分配結果名額
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

    # 結果不足時用原始查詢補齊
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
                        "請根據提供的搜尋資料，盡可能完整且準確地回答使用者的問題。"
                        "如果問題包含多個子查詢，請逐一回答每一個，並清楚標示對應哪個子查詢。"
                        "若資料中有多個來源說法不同，請說明差異。"
                        "若資料不足以回答，請如實說明，不要捏造資訊。"
                        "請用與使用者相同的語言回答，條理清晰，避免冗長廢話。"
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
        logger.info(f"[LLM] 回傳內容長度：{len(raw_content) if raw_content else 0}")
        logger.info(f"[LLM] 回傳前100字：{repr(raw_content[:100]) if raw_content else 'None'}")
        return raw_content or "❌ LLM 回傳空內容"
    except Exception as e:
        return f"❌ LLM 處理失敗: {str(e)}"
