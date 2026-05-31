"""Async TOR crawler for .onion sites.

Tuned config (env-overridable):
  CRAWLER_WORKERS=5     parallel workers
  CRAWLER_DELAY=1.5     seconds a worker waits after saving a page
  QUEUE_IDLE_TIMEOUT=60 seconds a worker waits on an empty queue before exiting
Plus a per-domain rate limit of 1 request / 10s so 5 workers don't hammer a
single onion.
"""

import asyncio
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Set, Tuple
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.models import record_crawl_stats
from crawler.ai_describe import describe
from crawler.models import (
    get_crawl_urls,
    mark_dead,
    revive_check,
    should_recrawl,
    upsert_page,
)
from crawler.parser import parse_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOR_PROXY = os.environ.get("TOR_PROXY", "socks5h://tor:9050")
CRAWLER_DELAY = float(os.environ.get("CRAWLER_DELAY", "1.5"))
CRAWLER_WORKERS = int(os.environ.get("CRAWLER_WORKERS", "5"))
QUEUE_IDLE_TIMEOUT = 60

# Per-domain rate limit: never hit the same .onion more than once per 10s, even
# with multiple concurrent workers. Maps domain -> monotonic time of its next
# allowed request.
DOMAIN_RATE_LIMIT = 10.0
_domain_last_seen: dict[str, float] = {}


async def _domain_throttle(url: str) -> None:
    """Sleep so the URL's domain is hit at most once per DOMAIN_RATE_LIMIT secs.

    Reserves the slot before sleeping so concurrent workers targeting the same
    domain serialize instead of all firing at once.
    """
    domain = urlparse(url).netloc
    now = time.monotonic()
    last = _domain_last_seen.get(domain, 0.0)
    wait = DOMAIN_RATE_LIMIT - (now - last)
    # Reserve the next slot for this domain immediately.
    _domain_last_seen[domain] = max(now, last + DOMAIN_RATE_LIMIT)
    if wait > 0:
        await asyncio.sleep(wait)

SEED_URLS = [
    "https://www.bbcnewsd73hkzno2ini43t4gblxvycyac5aw4gnv7t2rccijh7745uqd.onion/",
    "https://www.nytimesn7cgmftshazwhfgzm37qxb44r64ytbb2dj3x62d2lljsciiyd.onion/",
    "https://www.guardian2zotagl6tmjucg3lrhxdk4dw3lhbqnkvvkywawy3oqfoprid.onion/",
    "http://bellcatmbguthn3age23lrbseln2lryzv3mt7whis7ktjw4qrestbzad.onion/",
    "https://www.rferlo2zxgv23tct66v45s5mecftol5vod3hf4rqbipfp46fqu2q56ad.onion/",
    "https://www.dwnewsgngmhlplxy6o2twtfgjnrnjxbegbwqx6wnotdhkzt562tszfid.onion/en/",
    "https://www.voanews5aitmne6gs2btokcacixclgfl43cv27sirgbauyyjylwpdtqd.onion/",
    "https://p53lf57qovyuvwsc6xnrppyply3vtqm7l6pcobkmyqsiofyeznfu5uqd.onion/",
    "https://27m3p2uv7igmj6kvd4ql3cct5h3sdwrsajovkkndeufumzyfhlfev4qd.onion",
    "http://ciadotgov4sjwlzihbbgxnqg3xiyrg7so2r2o3lt5wz5ypk4sxyjstad.onion/index.html",
    "http://vww6ybal4bd7szmgncyruucpgfkqahzddi37ktceo3ah7ngmcopnpyyd.onion/",
    "http://7sk2kov2xwx6cbc32phynrifegg6pklmzs7luwcggtzrnlsolxxuyfyd.onion/en/index.html",
    "https://www.bbcweb3hytmzhn5d532owbu6oqadra5z3ar726vq5kgwwn6aucdccrad.onion/learningenglish/",
]

MAX_RETRIES = 3

# Tor circuits are slow to build but reads should not hang forever. Split the
# connect budget from the read budget so a dead peer fails fast while a slow
# (but live) onion still gets time to respond.
HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)

# Endpoint used to confirm the request actually egresses through Tor.
TOR_CHECK_URL = "https://check.torproject.org/api/ip"
TOR_VERIFY_RETRIES = 5
TOR_VERIFY_BACKOFF = 5

# HTTP statuses that mean "temporarily unavailable", not "dead". Never mark a
# site dead on these — it's rate limiting / overload, not a missing service.
TRANSIENT_STATUSES = {403, 429, 503}

# fetch() outcomes
FETCH_OK = "ok"        # html returned
FETCH_DEAD = "dead"    # connection-level failure -> caller marks dead
FETCH_SKIP = "skip"    # HTTP error / transient -> caller skips, no mark_dead


async def verify_tor(client: httpx.AsyncClient) -> bool:
    """Confirm the proxy is up and traffic egresses through Tor.

    Retries with linear backoff because the tor container may still be building
    its first circuit when the crawler starts. Logs the exit IP so operators can
    confirm the circuit in the logs. Returns False if Tor never becomes ready.
    """
    for attempt in range(1, TOR_VERIFY_RETRIES + 1):
        try:
            r = await client.get(TOR_CHECK_URL, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            logger.info(
                "Tor circuit ready: exit IP %s (IsTor=%s)",
                data.get("IP"),
                data.get("IsTor"),
            )
            return True
        except Exception as e:
            logger.warning(
                "Tor not ready (attempt %d/%d): %s", attempt, TOR_VERIFY_RETRIES, e
            )
            await asyncio.sleep(TOR_VERIFY_BACKOFF * attempt)
    logger.error("Tor proxy unavailable after %d attempts", TOR_VERIFY_RETRIES)
    return False


async def fetch(client: httpx.AsyncClient, url: str) -> Tuple[str | None, str]:
    """Fetch a URL and classify the outcome.

    Returns (html, outcome):
      - (text, FETCH_OK)   on success
      - (None, FETCH_DEAD) only on connection-level failure (ConnectError /
                           TimeoutException) — the caller may mark the site dead
      - (None, FETCH_SKIP) on any HTTP status error (incl. 403/429/503) or other
                           error — transient/not-dead, caller just skips
    """
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
            r.raise_for_status()
            return r.text, FETCH_OK
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in TRANSIENT_STATUSES:
                logger.warning("HTTP %d (transient, not dead): %s", status, url)
            else:
                logger.warning("HTTP %d: %s", status, url)
            return None, FETCH_SKIP
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            # Connection-level failure: retry, and if it persists, it's dead.
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2)
            else:
                logger.warning("Connection failed after %d attempts: %s — %s", MAX_RETRIES, url, e)
                return None, FETCH_DEAD
        except Exception as e:
            # Anything else (proxy hiccup, malformed response): skip, don't kill.
            logger.warning("Fetch error (skipping): %s — %s", url, e)
            return None, FETCH_SKIP
    return None, FETCH_DEAD


async def worker(
    queue: asyncio.Queue,
    visited: Set[str],
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
    stats: dict,
) -> None:
    while True:
        try:
            url = await asyncio.wait_for(queue.get(), timeout=QUEUE_IDLE_TIMEOUT)
        except asyncio.TimeoutError:
            logger.info("Worker idle for %ds, exiting", QUEUE_IDLE_TIMEOUT)
            return

        try:
            if url in visited:
                continue
            visited.add(url)

            if not should_recrawl(url):
                logger.debug("Skip fresh URL: %s", url)
                continue

            async with semaphore:
                await _domain_throttle(url)
                logger.info("Crawling %s", url)
                html, outcome = await fetch(client, url)

                if html is None:
                    # Only connection-level failures count toward dead-marking;
                    # HTTP 403/429/503 and other errors are skipped silently.
                    if outcome == FETCH_DEAD:
                        mark_dead(url)
                    continue

                parsed = parse_page(html, url)

                if parsed is None:
                    logger.debug("Skipping thin page: %s", url)
                    continue

                content_hash = hashlib.md5(html.encode()).hexdigest()
                meta = describe(html, url)

                upsert_page(
                    url=url,
                    title=meta.get("title") or parsed["title"],
                    description=meta.get("description") or "",
                    category=meta.get("category") or "other",
                    lang=meta.get("lang") or "other",
                    score=0.0,
                    content_hash=content_hash,
                    page_type=parsed.get("page_type", "other"),
                )
                stats["saved"] += 1
                logger.info("Saved: [%s] %s", meta["category"], meta["title"][:80])

                for link in parsed["links"]:
                    if link not in visited:
                        await queue.put(link)

                await asyncio.sleep(CRAWLER_DELAY)
        except Exception:
            logger.exception("Worker error processing %s", url)
        finally:
            queue.task_done()


def seconds_until_midnight_utc() -> float:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (tomorrow - now).total_seconds()


def is_weekly_crawl() -> bool:
    # Sunday == 6. Weekly full crawl revisits dead sites too.
    return datetime.now(timezone.utc).weekday() == 6


def _build_seed_set(weekly: bool) -> Set[str]:
    """Assemble the URL set for a cycle: seeds + revived + scheduled DB URLs."""
    urls: Set[str] = set(SEED_URLS)

    # Always give long-dead sites a fresh chance at the top of a cycle.
    urls.update(revive_check())

    # Daily: stale live sites. Weekly: everything, including currently-dead.
    urls.update(get_crawl_urls(include_dead=weekly))
    return urls


async def crawl_cycle(weekly: bool = False) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    semaphore = asyncio.Semaphore(CRAWLER_WORKERS)
    # Fresh per-cycle visited set so each scheduled run re-crawls from scratch
    # (freshness is still gated by should_recrawl()).
    visited: Set[str] = set()
    stats = {"saved": 0}
    started = time.monotonic()

    seeds = _build_seed_set(weekly)
    logger.info(
        "%s crawl: queuing %d URLs (seeds + revived + scheduled)",
        "Weekly" if weekly else "Daily",
        len(seeds),
    )
    for url in seeds:
        await queue.put(url)

    transport = httpx.AsyncHTTPTransport(proxy=TOR_PROXY)
    async with httpx.AsyncClient(transport=transport) as client:
        # Don't burn a cycle hammering dead onions if Tor isn't routing yet.
        if not await verify_tor(client):
            logger.error("Skipping crawl cycle: Tor proxy not ready")
            return

        tasks = [
            asyncio.create_task(worker(queue, visited, semaphore, client, stats))
            for _ in range(CRAWLER_WORKERS)
        ]
        await asyncio.gather(*tasks)

    elapsed = max(time.monotonic() - started, 0.001)
    pages_per_hour = stats["saved"] / elapsed * 3600
    logger.info(
        "Crawl cycle done. Visited %d URLs, saved %d pages in %.0fs (%.1f pages/hour).",
        len(visited),
        stats["saved"],
        elapsed,
        pages_per_hour,
    )
    try:
        record_crawl_stats(stats["saved"], pages_per_hour, elapsed)
    except Exception:
        logger.exception("Failed to record crawl stats")


async def run() -> None:
    # Bootstrap crawl on startup, then run on the 00:00 UTC schedule:
    # every day a daily crawl, Sundays a weekly full crawl. Between runs the
    # crawler sleeps and the API serves results straight from the DB.
    logger.info("Starting bootstrap crawl")
    await crawl_cycle(weekly=is_weekly_crawl())
    while True:
        sleep_s = seconds_until_midnight_utc()
        logger.info("Sleeping %.0fs until next scheduled crawl (00:00 UTC)", sleep_s)
        await asyncio.sleep(sleep_s)
        weekly = is_weekly_crawl()
        logger.info("Starting %s crawl", "weekly" if weekly else "daily")
        await crawl_cycle(weekly=weekly)


if __name__ == "__main__":
    asyncio.run(run())
