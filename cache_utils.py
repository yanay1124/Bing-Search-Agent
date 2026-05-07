import asyncio
import time
from collections import OrderedDict
from settings import CACHE_TTL_SECONDS, CACHE_MAX_ITEMS, CACHE_CLEAN_INTERVAL, logger

SEARCH_CACHE: OrderedDict[str, tuple[float, list[dict], list[str]]] = OrderedDict()
PAGE_CACHE: OrderedDict[str, tuple[float, dict]] = OrderedDict()
SUBQUERY_CACHE: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()
LINK_CACHE: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()

SEARCH_CACHE_LOCK = asyncio.Lock()
PAGE_CACHE_LOCK = asyncio.Lock()
SUBQUERY_CACHE_LOCK = asyncio.Lock()
LINK_CACHE_LOCK = asyncio.Lock()


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
