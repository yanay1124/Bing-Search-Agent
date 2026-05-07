from contextlib import asynccontextmanager
import asyncio
import random
import time
from collections import deque
from playwright.async_api import async_playwright, Browser
from cache_utils import _run_cache_cleaner
from settings import (
    BROWSER_CHANNEL,
    HEADLESS,
    MAX_CONCURRENT_REQUESTS,
    PROXY_CYCLE,
    RANDOM_DELAY_MAX,
    RANDOM_DELAY_MIN,
    REQUESTS_PER_MINUTE,
    STEALTH_MODE,
    USER_AGENT,
    logger,
)

_playwright = None
_browser: Browser = None
_rate_limiter = None
_CACHE_CLEANER_TASK: asyncio.Task | None = None


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


def get_rate_limiter():
    return _rate_limiter


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
