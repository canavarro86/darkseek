"""Async TOR crawler for .onion sites — self-feeding from the database (v3).

The crawler no longer rebuilds a frontier from SEED_URLS every cycle. Instead it
runs one continuous loop:

  1. Pull a priority-tiered batch of URLs from the DB (crawler.models.get_next_batch):
       tier 1  never crawled            (last_seen IS NULL)        limit 100
       tier 2  alive, stale  > 7 days                              limit 50
       tier 3  dead, < 3 attempts, cooled down                     limit 20
     plus any user-submitted URLs claimed from crawl_queue.
  2. Crawl the batch with CRAWLER_WORKERS concurrent fetches.
  3. On success: accumulate the result and flush every BATCH_WRITE_SIZE rows in
     one transaction; extract .onion links and insert NEW ones as never-crawled
     rows (last_seen NULL) so the NEXT batch's tier 1 picks them up — this is the
     self-feeding mechanism.
  4. On failure: mark dead, bump crawl_attempts, push next_crawl_at out by
     attempts * 3 days (exponential-style back-off). The row is never removed.
  5. When every tier returns empty, sleep EMPTY_QUEUE_SLEEP and retry.

SEED_URLS are used ONLY to bootstrap a completely empty DB (first run / server
migration). Config (env-overridable):
  CRAWLER_WORKERS=2     parallel fetches (keep low: 1GB box)
  CRAWLER_DELAY=3       seconds a worker waits after saving a page
Plus a per-domain rate limit of 1 request / 10s.
"""

import asyncio
import hashlib
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Tuple
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.models import record_crawl_stats
from config.blocked import BLOCKED_KEYWORDS
from crawler.ai_describe import describe
from crawler.dead_cache import clear_dead, is_dead, record_dead
from crawler.models import (
    checkpoint_wal,
    claim_queue_batch,
    cleanup_inactive_pages,
    count_pages,
    get_next_batch,
    insert_discovered_links,
    normalize_url,
    reconcile_queue,
    record_crawl_failure,
    record_crawl_skip,
    write_crawl_batch,
)
from crawler.parser import parse_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOR_PROXY = os.environ.get("TOR_PROXY", "socks5h://tor:9050")
# Low defaults for a 1GB box (CONCURRENT_REQUESTS=2 / DOWNLOAD_DELAY=3 intent).
CRAWLER_DELAY = float(os.environ.get("CRAWLER_DELAY", "3"))
CRAWLER_WORKERS = int(os.environ.get("CRAWLER_WORKERS", "2"))
# Seconds to sleep when every refill tier is empty before retrying (PART A tier 4).
EMPTY_QUEUE_SLEEP = float(os.environ.get("EMPTY_QUEUE_SLEEP", "60"))
# Batch DB writes: accumulate this many successful results, then write once.
BATCH_WRITE_SIZE = int(os.environ.get("BATCH_WRITE_SIZE", "10"))

# --- Memory bounds (1GB box, crawler budget 256MB) --------------------------
RSS_LIMIT_MB = 250.0            # restart the crawler if RSS exceeds this
WATCHDOG_INTERVAL = 15.0        # seconds between RSS checks


def _rss_mb() -> float | None:
    """Resident set size of this process in MiB, or None if unavailable."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0  # kB -> MiB
    except (OSError, ValueError):
        return None
    return None


async def memory_watchdog() -> None:
    """Restart the crawler if its RSS exceeds RSS_LIMIT_MB.

    On breach it checkpoints the WAL so no indexed pages are stranded, then exits
    non-zero so Docker's restart policy brings up a clean process.
    """
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        rss = _rss_mb()
        if rss is None:
            continue
        if rss > RSS_LIMIT_MB:
            logger.error(
                "Memory watchdog: RSS %.0fMiB exceeds %.0fMiB limit — "
                "checkpointing WAL and restarting",
                rss, RSS_LIMIT_MB,
            )
            try:
                checkpoint_wal()
            except Exception:
                logger.exception("Watchdog WAL checkpoint failed")
            os._exit(1)


# Per-domain rate limit: never hit the same .onion more than once per 10s, even
# with multiple concurrent workers. Maps domain -> monotonic next-allowed time.
DOMAIN_RATE_LIMIT = 10.0
_domain_last_seen: dict[str, float] = {}


async def _domain_throttle(url: str) -> None:
    """Sleep so the URL's domain is hit at most once per DOMAIN_RATE_LIMIT secs."""
    domain = urlparse(url).netloc
    now = time.monotonic()
    last = _domain_last_seen.get(domain, 0.0)
    wait = DOMAIN_RATE_LIMIT - (now - last)
    # Reserve the next slot for this domain immediately.
    _domain_last_seen[domain] = max(now, last + DOMAIN_RATE_LIMIT)
    if wait > 0:
        await asyncio.sleep(wait)


# Fallback seeds — used ONLY when the DB is empty (first run / fresh migration).
# Never consulted while the DB has any pages; the crawler self-feeds from links.
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

# --- Per-host fairness / trap protection ------------------------------------
# PER_HOST_CAP bounds how many pages from one host a single batch will save, so
# one large site can't dominate. TRAP_NUMERIC_THRESHOLD flags pagination traps
# (>100 URLs from a host differing only by a numeric segment) so their numeric
# links are dropped from discovery.
PER_HOST_CAP = 200
TRAP_NUMERIC_THRESHOLD = 100
_NUMERIC_SEG_RE = re.compile(r"\d+")


def _host(url: str) -> str:
    """Authority component (host[:port]) used for per-host caps."""
    return urlparse(url).netloc


def _numeric_template(url: str) -> str:
    """URL with digit runs masked, so numeric-only variants share a key."""
    p = urlparse(url)
    return f"{p.netloc}{_NUMERIC_SEG_RE.sub('#', p.path)}?{_NUMERIC_SEG_RE.sub('#', p.query)}"


class RunState:
    """Per-batch, in-memory bookkeeping (host caps, trap detection, counters).

    Not persisted — every batch starts clean. Bounded by the number of distinct
    hosts/templates in the batch, which the caps above keep small.
    """

    def __init__(self) -> None:
        self.saved = 0
        self.host_pages: Counter = Counter()
        self.host_templates: dict[str, Counter] = {}
        self.trapped_hosts: set[str] = set()
        self.capped_hosts: set[str] = set()
        self.skipped_media = 0
        self.skipped_illegal = 0

    def host_capped(self, host: str) -> bool:
        return self.host_pages[host] >= PER_HOST_CAP

    def note_saved(self, host: str) -> None:
        self.saved += 1
        self.host_pages[host] += 1

    def is_trap_link(self, url: str) -> bool:
        """Track numeric-variant volume per host; flag/skip pagination traps."""
        host = _host(url)
        template = _numeric_template(url)
        counts = self.host_templates.setdefault(host, Counter())
        counts[template] += 1
        if counts[template] > TRAP_NUMERIC_THRESHOLD:
            if host not in self.trapped_hosts:
                self.trapped_hosts.add(host)
                logger.warning(
                    "[TRAP] %s: >%d numeric variants of %s — de-prioritizing",
                    host, TRAP_NUMERIC_THRESHOLD, template,
                )
            return True
        return False


MAX_RETRIES = 3
# Base delay for exponential backoff between connection retries: 2s, 4s, ...
RETRY_BACKOFF_BASE = 2

# Tor circuits are slow to build but reads should not hang forever. Split the
# connect budget from the read budget so a dead peer fails fast.
HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)

# Endpoint used to confirm the request actually egresses through Tor.
TOR_CHECK_URL = "https://check.torproject.org/api/ip"
TOR_VERIFY_RETRIES = 5
TOR_VERIFY_BACKOFF = 5

# HTTP statuses that mean "temporarily unavailable", not "dead".
TRANSIENT_STATUSES = {403, 429, 503}

# Binary/non-HTML extensions the crawler must never download (OOM guard over Tor).
SKIP_EXTENSIONS = frozenset({
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.bmp', '.tiff',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt',
    '.zip', '.tar', '.gz', '.rar', '.7z', '.bz2',
    '.mp4', '.mp3', '.avi', '.mkv', '.mov', '.webm', '.ogg', '.wav', '.flac',
    '.exe', '.bin', '.dmg', '.apk', '.iso', '.torrent',
    '.css', '.js', '.woff', '.woff2', '.ttf', '.eot', '.json', '.xml',
})

# Budget for the pre-fetch HEAD probe. Kept short: a slow/unsupported HEAD must
# not stall the worker — on timeout we fall through to the normal GET.
HEAD_TIMEOUT = httpx.Timeout(connect=15.0, read=10.0, write=10.0, pool=10.0)


def _is_media_url(url: str) -> bool:
    """True if the URL path ends in a known binary/non-HTML extension."""
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    return ext in SKIP_EXTENSIONS


def _is_blocked_content(title: str, description: str, url: str) -> bool:
    """True if title, description, or URL contains any blocked CSAM keyword.

    The keyword list is the single source of truth in config/blocked.py. Called
    twice in the pipeline: with only the URL before a fetch (cheap pre-filter),
    and with the AI-derived title/description before a page is written.
    """
    haystack = f"{title} {description} {url}".lower()
    return any(keyword in haystack for keyword in BLOCKED_KEYWORDS)


# fetch() outcomes
FETCH_OK = "ok"        # html returned
FETCH_DEAD = "dead"    # connection-level failure -> caller marks dead
FETCH_SKIP = "skip"    # HTTP error / transient -> caller skips, no mark_dead


async def verify_tor(client: httpx.AsyncClient) -> bool:
    """Confirm the proxy is up and traffic egresses through Tor."""
    for attempt in range(1, TOR_VERIFY_RETRIES + 1):
        try:
            r = await client.get(TOR_CHECK_URL, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            logger.info(
                "Tor circuit ready: exit IP %s (IsTor=%s)",
                data.get("IP"), data.get("IsTor"),
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
      - (None, FETCH_DEAD) only on connection-level failure (caller may mark dead)
      - (None, FETCH_SKIP) on any HTTP status error or other transient error
    """
    # 1) Extension fast-path: never even open a known binary asset.
    if _is_media_url(url):
        logger.debug("Skip media by extension: %s", url)
        return None, FETCH_SKIP

    # 2) HEAD probe: reject non-text Content-Type before pulling a body into RAM.
    #    HEAD failures are non-fatal — a 405 or a timeout falls through to GET.
    try:
        head = await client.head(url, timeout=HEAD_TIMEOUT, follow_redirects=True)
        if head.status_code != 405:
            content_type = head.headers.get("content-type", "")
            if content_type and not content_type.startswith("text/"):
                logger.debug("Skip non-HTML [%s]: %s", content_type, url)
                return None, FETCH_SKIP
    except (httpx.ConnectError, httpx.TimeoutException):
        pass
    except Exception:
        pass

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
            # Connection-level failure: retry with exponential backoff
            # (2s, 4s, 8s ...); if it still fails, the site is treated as dead.
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
            else:
                logger.warning(
                    "Connection failed after %d attempts: %s — %s", MAX_RETRIES, url, e
                )
                return None, FETCH_DEAD
        except Exception as e:
            logger.warning("Fetch error (skipping): %s — %s", url, e)
            return None, FETCH_SKIP
    return None, FETCH_DEAD


async def _process_url(
    client: httpx.AsyncClient,
    url: str,
    state: RunState,
    results: list,
    discovered: set,
    flush_lock: asyncio.Lock,
) -> None:
    """Crawl one URL: fetch -> parse -> enrich -> accumulate result + links.

    Writes are buffered into `results` (flushed in batches under flush_lock).
    Discovered .onion links are collected into `discovered` for one DB insert at
    batch end (the self-feeding step). Every terminal state updates the DB so the
    URL leaves the never-crawled tier and can't be re-fetched in a tight loop.
    """
    host = _host(url)
    if state.host_capped(host):
        if host not in state.capped_hosts:
            state.capped_hosts.add(host)
            logger.info("[HOST CAP] %s reached %d", host, PER_HOST_CAP)
        return

    # Negative cache: skip onions known-dead and still within cooldown.
    if is_dead(url):
        logger.debug("Skip dead-cached onion: %s", url)
        return

    # Media fast-path: not a dead site, just defer it.
    if _is_media_url(url):
        state.skipped_media += 1
        record_crawl_skip(url)
        return

    # CSAM blocklist (URL pre-check): never fetch; defer so it leaves tier 1.
    if _is_blocked_content("", "", url):
        state.skipped_illegal += 1
        logger.warning("Skip blocked URL (illegal content): %s", url)
        record_crawl_skip(url)
        return

    await _domain_throttle(url)
    logger.info("Crawling %s", url)
    html, outcome = await fetch(client, url)

    if html is None:
        # Connection-level failure -> dead cache + crawl failure (back-off).
        # HTTP 403/429/503 are transient -> defer, don't penalize as dead.
        if outcome == FETCH_DEAD:
            record_dead(url)
            record_crawl_failure(url)
        else:
            record_crawl_skip(url)
        return

    # Reachable again: drop any stale dead-cache entry.
    clear_dead(url)

    parsed = parse_page(html, url)
    if parsed is None:
        logger.debug("Thin page, deferring: %s", url)
        record_crawl_skip(url)
        return

    content_hash = hashlib.md5(html.encode()).hexdigest()
    meta = describe(html, url)

    # CSAM blocklist (content post-check): AI title/description can reveal what
    # the URL alone did not. Drop before write; defer so it leaves tier 1.
    if _is_blocked_content(meta.get("title", ""), meta.get("description", ""), url):
        state.skipped_illegal += 1
        logger.warning("Skip blocked page (illegal content): %s", url)
        record_crawl_skip(url)
        return

    record = {
        "url": url,
        "title": meta.get("title") or parsed["title"],
        "description": meta.get("description") or "",
        "category": meta.get("category") or "other",
        "lang": meta.get("lang") or "other",
        "score": 0.0,
        "content_hash": content_hash,
        "page_type": parsed.get("page_type", "other"),
        "enrichment_method": meta.get("enrichment_method", "heuristic"),
        "content_tag": meta.get("content_tag", "unknown"),
    }

    async with flush_lock:
        results.append(record)
        state.note_saved(host)
        logger.info("Queued for write: [%s] %s", record["category"], record["title"][:80])

        # Collect discovered .onion links for the next batch (self-feeding).
        for link in parsed["links"]:
            if state.host_capped(_host(link)):
                continue
            if state.is_trap_link(link):
                continue
            if _is_blocked_content("", "", link):
                continue
            discovered.add(link)

        # Batch DB writes: flush once we've accumulated BATCH_WRITE_SIZE rows.
        if len(results) >= BATCH_WRITE_SIZE:
            snapshot = results[:]
            results.clear()
            write_crawl_batch(snapshot)

    await asyncio.sleep(CRAWLER_DELAY)


async def crawl_batch(client: httpx.AsyncClient, batch: list, state: RunState) -> None:
    """Crawl one batch with CRAWLER_WORKERS concurrent fetches.

    Buffers successful writes (flushed in batches), then writes any remainder and
    inserts all newly-discovered links in one shot at the end.
    """
    visited: set = set()
    results: list = []
    discovered: set = set()
    flush_lock = asyncio.Lock()
    sem = asyncio.Semaphore(CRAWLER_WORKERS)

    async def handle(url: str) -> None:
        if url in visited:
            return
        visited.add(url)
        async with sem:
            try:
                await _process_url(client, url, state, results, discovered, flush_lock)
            except Exception:
                logger.exception("Worker error processing %s", url)

    await asyncio.gather(*(handle(u) for u in batch))

    # Flush any results below the batch threshold.
    if results:
        write_crawl_batch(results)

    # Self-feed: insert newly-discovered links not already crawled this batch.
    fresh = {normalize_url(u) for u in discovered} - {normalize_url(u) for u in batch}
    if fresh:
        insert_discovered_links(fresh)


async def run() -> None:
    """Continuous self-feeding crawl loop.

    Bootstraps from SEED_URLS only when the DB is empty, then loops forever:
    claim user submissions -> pull a tiered batch -> crawl -> (sleep if empty).
    """
    asyncio.create_task(memory_watchdog())

    transport = httpx.AsyncHTTPTransport(proxy=TOR_PROXY)
    async with httpx.AsyncClient(transport=transport) as client:
        # Don't do anything until traffic actually routes through Tor.
        while not await verify_tor(client):
            logger.error("Tor proxy not ready; retrying in %.0fs", EMPTY_QUEUE_SLEEP)
            await asyncio.sleep(EMPTY_QUEUE_SLEEP)

        # Fallback seeding ONLY on an empty DB (first run / server migration).
        if count_pages() == 0:
            seeded = insert_discovered_links(SEED_URLS)
            logger.info("Empty DB: seeded %d fallback URLs", seeded)

        last_cleanup_day = None
        while True:
            started = time.monotonic()
            state = RunState()

            # User-submitted URLs (crawl_queue) get top priority each round.
            queued = claim_queue_batch()
            batch = list(dict.fromkeys(queued + get_next_batch()))

            if not batch:
                logger.info("All tiers empty; sleeping %.0fs", EMPTY_QUEUE_SLEEP)
                await asyncio.sleep(EMPTY_QUEUE_SLEEP)
                continue

            logger.info(
                "Crawl batch: %d URLs (%d user-queued + tiers)", len(batch), len(queued)
            )
            await crawl_batch(client, batch, state)

            # Resolve user-submitted queue rows (done if now indexed, else failed).
            if queued:
                try:
                    reconcile_queue(queued)
                except Exception:
                    logger.exception("Failed to reconcile crawl queue")

            elapsed = max(time.monotonic() - started, 0.001)
            pages_per_hour = state.saved / elapsed * 3600
            logger.info(
                "Batch done: saved %d in %.0fs (%.1f pages/hour). "
                "hosts capped=%d traps=%d media=%d illegal=%d",
                state.saved, elapsed, pages_per_hour,
                len(state.capped_hosts), len(state.trapped_hosts),
                state.skipped_media, state.skipped_illegal,
            )
            try:
                record_crawl_stats(state.saved, pages_per_hour, elapsed)
            except Exception:
                logger.exception("Failed to record crawl stats")
            try:
                checkpoint_wal()
            except Exception:
                logger.exception("WAL checkpoint failed")

            # Run the inactive-page GC at most once per UTC day.
            today = datetime.now(timezone.utc).date()
            if today != last_cleanup_day:
                try:
                    cleanup_inactive_pages()
                except Exception:
                    logger.exception("Daily inactive-page cleanup failed")
                last_cleanup_day = today


if __name__ == "__main__":
    asyncio.run(run())
