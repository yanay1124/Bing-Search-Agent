from dotenv import load_dotenv
import itertools
import logging
import os

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "lm-studio")
LLM_MODEL = os.getenv("LLM_MODEL", "microsoft/phi-4-mini-reasoning")

BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "msedge")
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
