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
import re
import sys
import time
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Tuple
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.models import record_crawl_stats
from crawler.ai_describe import describe
from crawler.dead_cache import clear_dead, is_dead, record_dead, revive_candidates
from crawler.models import (
    checkpoint_wal,
    claim_queue_batch,
    get_crawl_urls,
    mark_dead,
    reconcile_queue,
    requeue_pending,
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
# Seconds to pause between back-to-back crawl cycles. Set low (e.g. 30) for
# continuous indexing; the default keeps the queue from being hammered empty.
CYCLE_SLEEP = float(os.environ.get("CYCLE_SLEEP", "30"))
QUEUE_IDLE_TIMEOUT = 60

# --- Memory bounds (1GB box, crawler budget 256MB) --------------------------
# Hard ceilings on the two structures that would otherwise grow with the link
# graph rather than with the page cap, plus an RSS watchdog as the backstop.
VISITED_MAX = 50_000            # max URLs tracked for dedup; LRU eviction past this
QUEUE_MAX = 10_000              # max pending frontier items; overflow links dropped
RSS_LIMIT_MB = 250.0            # restart the crawler if RSS exceeds this
WATCHDOG_INTERVAL = 15.0        # seconds between RSS checks
WAL_CHECKPOINT_INTERVAL = 1000  # checkpoint the WAL every N saved pages


class BoundedVisited:
    """Visited-URL membership set with a hard cap and LRU eviction.

    The per-cycle visited set would otherwise grow with every URL dequeued
    (pages *and* all their extracted links), unbounded by MAX_PAGES_PER_RUN.
    Cap it at VISITED_MAX; when full, evict the least-recently-added URL. An
    evicted URL may be re-crawled later, which is an acceptable trade for a
    bounded footprint on a 1GB box.
    """

    def __init__(self, maxsize: int = VISITED_MAX) -> None:
        self._maxsize = maxsize
        self._data: "OrderedDict[str, None]" = OrderedDict()

    def __contains__(self, url: str) -> bool:
        return url in self._data

    def add(self, url: str) -> None:
        if url in self._data:
            return
        self._data[url] = None
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)  # evict oldest

    def __len__(self) -> int:
        return len(self._data)


def _rss_mb() -> float | None:
    """Resident set size of this process in MiB, or None if unavailable.

    Reads /proc/self/status (present in the Linux container). Returns None off
    Linux or on any read error so the watchdog simply no-ops instead of raising.
    """
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

    Runs for the whole process lifetime (including between cycles). On breach it
    checkpoints the WAL so no indexed pages are stranded, then exits non-zero so
    Docker's restart policy brings up a clean process. This is the backstop for
    the bounded queue/visited set above.
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

# --- Bounded-execution limits (crawl-trap & runaway protection) -------------
# Every value is a deliberate ceiling so an unattended run can't grow without
# bound (memory) or loop forever (a paginating marketplace ate a whole cycle).
#
# MAX_DEPTH: links are followed at most 5 hops from a seed. News/wiki content is
#   reachable within a few hops; beyond that is mostly low-value pagination.
# MAX_PAGES_PER_RUN: hard stop at 10k saved pages/run. At ~1.5s/page that is a
#   multi-hour cycle — enough to make progress, bounded enough to release memory.
# PER_HOST_CAP: 200 pages/host/run. One host (BBC) had reached 1,603 pages (4%
#   of the index); 200 keeps any single site from dominating search results.
# TRAP_NUMERIC_THRESHOLD: >100 URLs from one host that differ only by a numeric
#   path/query segment (…/category/1.html, /2.html, …) flags pagination-trap
#   behaviour; that host's numeric links are then de-prioritized (dropped).
MAX_DEPTH = 5
MAX_PAGES_PER_RUN = 10_000
PER_HOST_CAP = 200
TRAP_NUMERIC_THRESHOLD = 100

# Matches a run of digits in a URL path/query so /category/12.html and
# /category/13.html collapse to the same template /category/#.html.
_NUMERIC_SEG_RE = re.compile(r"\d+")


def _host(url: str) -> str:
    """Authority component (scheme-stripped host[:port]) used for per-host caps."""
    return urlparse(url).netloc


def _numeric_template(url: str) -> str:
    """URL with digit runs masked, so numeric-only variants share a key."""
    p = urlparse(url)
    return f"{p.netloc}{_NUMERIC_SEG_RE.sub('#', p.path)}?{_NUMERIC_SEG_RE.sub('#', p.query)}"


class RunState:
    """Per-cycle, in-memory bookkeeping for the bounded-crawl invariants.

    Not persisted — every cycle starts clean. All counters are bounded by the
    number of distinct hosts/templates encountered, which the caps above keep
    small, so this never grows without limit.
    """

    def __init__(self) -> None:
        self.saved = 0                              # pages written this run
        self.host_pages: Counter = Counter()        # host -> pages saved
        self.host_templates: dict[str, Counter] = {}  # host -> template -> count
        self.trapped_hosts: set[str] = set()         # hosts flagged as traps
        self.capped_hosts: set[str] = set()          # hosts that hit PER_HOST_CAP (logged once)
        self.dropped_links = 0                        # links dropped on a full queue

    def run_ceiling_reached(self) -> bool:
        return self.saved >= MAX_PAGES_PER_RUN

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
# Base delay for the exponential backoff between connection retries (seconds):
# attempt 0 waits 2s, attempt 1 waits 4s, ...
RETRY_BACKOFF_BASE = 2

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
            # Connection-level failure: retry with exponential backoff
            # (2s, 4s, 8s ...) so a struggling circuit gets progressively more
            # breathing room; if it still fails, the site is treated as dead.
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
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
    visited: BoundedVisited,
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
    state: RunState,
) -> None:
    while True:
        try:
            url, depth = await asyncio.wait_for(queue.get(), timeout=QUEUE_IDLE_TIMEOUT)
        except asyncio.TimeoutError:
            logger.info("Worker idle for %ds, exiting", QUEUE_IDLE_TIMEOUT)
            return

        try:
            # Global run ceiling: stop doing work once we've saved enough. We keep
            # draining the queue (cheaply) so task_done balances and gather ends.
            if state.run_ceiling_reached():
                continue

            if url in visited:
                continue
            visited.add(url)

            host = _host(url)

            # Per-host fairness cap: once a host hits PER_HOST_CAP we stop
            # crawling it entirely for the rest of the run.
            if state.host_capped(host):
                if host not in state.capped_hosts:
                    state.capped_hosts.add(host)
                    logger.info("[HOST CAP] %s reached %d", host, PER_HOST_CAP)
                continue

            # Negative cache: skip onions known-dead and still within cooldown.
            if is_dead(url):
                logger.debug("Skip dead-cached onion: %s", url)
                continue

            if not should_recrawl(url):
                logger.debug("Skip fresh URL: %s", url)
                continue

            async with semaphore:
                await _domain_throttle(url)
                logger.info("Crawling %s (depth %d)", url, depth)
                html, outcome = await fetch(client, url)

                if html is None:
                    # Connection-level failures count toward dead-marking AND the
                    # negative cache; HTTP 403/429/503 are transient -> skip only.
                    if outcome == FETCH_DEAD:
                        mark_dead(url)
                        record_dead(url)
                    continue

                # Reachable again: drop any stale dead-cache entry.
                clear_dead(url)

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
                    enrichment_method=meta.get("enrichment_method", "heuristic"),
                )
                state.note_saved(host)
                logger.info("Saved: [%s] %s", meta["category"], meta["title"][:80])

                # Periodically truncate the WAL so the -wal sidecar can't grow
                # without bound across a long continuous cycle.
                if state.saved % WAL_CHECKPOINT_INTERVAL == 0:
                    try:
                        checkpoint_wal()
                        logger.info("WAL checkpoint at %d saved pages", state.saved)
                    except Exception:
                        logger.exception("Periodic WAL checkpoint failed")

                # Enqueue children unless we're at the depth limit. Depth caps
                # how far we wander from a seed and starves pagination traps.
                if depth < MAX_DEPTH and not state.run_ceiling_reached():
                    for link in parsed["links"]:
                        if link in visited:
                            continue
                        link_host = _host(link)
                        if state.host_capped(link_host):
                            continue
                        # Drop numeric-pagination links from trap-flagged hosts;
                        # is_trap_link also updates the per-host variant counter.
                        if state.is_trap_link(link):
                            continue
                        # Bounded frontier: if the queue is at QUEUE_MAX, drop the
                        # link rather than block. Bounded memory beats exhaustive
                        # coverage; dropped links resurface via seeds next cycle.
                        try:
                            queue.put_nowait((link, depth + 1))
                        except asyncio.QueueFull:
                            state.dropped_links += 1

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


def _build_seed_set(weekly: bool) -> set[str]:
    """Assemble the URL set for a cycle: seeds + revived + scheduled DB URLs."""
    urls: set[str] = set(SEED_URLS)

    # Always give long-dead sites a fresh chance at the top of a cycle.
    urls.update(revive_check())

    # Negative-cache revivals: onions past their dead-cache cooldown get one
    # retry. is_dead() returns False for them, so the worker won't re-skip them.
    urls.update(revive_candidates())

    # Daily: stale live sites. Weekly: everything, including currently-dead.
    urls.update(get_crawl_urls(include_dead=weekly))
    return urls


async def crawl_cycle(weekly: bool = False) -> None:
    # Bounded frontier: workers drop links once the queue holds QUEUE_MAX items.
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
    semaphore = asyncio.Semaphore(CRAWLER_WORKERS)
    # Fresh per-cycle visited set so each scheduled run re-crawls from scratch
    # (freshness is still gated by should_recrawl()). Bounded + LRU-evicting.
    visited = BoundedVisited(VISITED_MAX)
    state = RunState()
    started = time.monotonic()

    seeds = _build_seed_set(weekly)

    # Fold user-submitted URLs from crawl_queue into this cycle's frontier.
    # Tracked separately so we can reconcile their queue status afterwards.
    queued = claim_queue_batch()
    seeds.update(queued)

    logger.info(
        "%s crawl: queuing %d URLs (seeds + revived + scheduled + %d queued)",
        "Weekly" if weekly else "Daily",
        len(seeds),
        len(queued),
    )
    seeded = 0
    for url in seeds:
        # Seeds start at depth 0. Use put_nowait so a seed set larger than
        # QUEUE_MAX can't deadlock here (workers don't run yet); overflow seeds
        # are deferred to the next cycle, where get_crawl_urls() re-supplies them.
        try:
            queue.put_nowait((url, 0))
            seeded += 1
        except asyncio.QueueFull:
            break
    if seeded < len(seeds):
        logger.warning(
            "Queue cap %d reached while seeding; deferred %d URLs to next cycle",
            QUEUE_MAX, len(seeds) - seeded,
        )

    transport = httpx.AsyncHTTPTransport(proxy=TOR_PROXY)
    async with httpx.AsyncClient(transport=transport) as client:
        # Don't burn a cycle hammering dead onions if Tor isn't routing yet.
        if not await verify_tor(client):
            logger.error("Skipping crawl cycle: Tor proxy not ready")
            # Release the claim so these URLs are retried on the next cycle.
            requeue_pending(queued)
            return

        tasks = [
            asyncio.create_task(worker(queue, visited, semaphore, client, state))
            for _ in range(CRAWLER_WORKERS)
        ]
        await asyncio.gather(*tasks)

    # Mark claimed queue URLs 'done' (now in pages) or 'failed' (still missing).
    try:
        reconcile_queue(queued)
    except Exception:
        logger.exception("Failed to reconcile crawl queue")

    elapsed = max(time.monotonic() - started, 0.001)
    pages_per_hour = state.saved / elapsed * 3600
    logger.info(
        "Crawl cycle done. Visited %d URLs, saved %d pages in %.0fs (%.1f pages/hour). "
        "Hosts capped: %d, traps flagged: %d, links dropped (queue full): %d.",
        len(visited),
        state.saved,
        elapsed,
        pages_per_hour,
        len(state.capped_hosts),
        len(state.trapped_hosts),
        state.dropped_links,
    )
    if state.run_ceiling_reached():
        logger.warning("Run ceiling hit: stopped at %d pages", MAX_PAGES_PER_RUN)
    try:
        record_crawl_stats(state.saved, pages_per_hour, elapsed)
    except Exception:
        logger.exception("Failed to record crawl stats")


async def run() -> None:
    # Continuous mode: crawl cycle after cycle with only CYCLE_SLEEP seconds of
    # pause between them, so indexing runs as fast as the queue allows. Sundays
    # still trigger a weekly full crawl.
    # Always-on RSS backstop: restarts the process if it outgrows its budget.
    asyncio.create_task(memory_watchdog())
    logger.info("Starting bootstrap crawl")
    await crawl_cycle(weekly=is_weekly_crawl())
    while True:
        logger.info("Sleeping %.0fs until next crawl cycle", CYCLE_SLEEP)
        await asyncio.sleep(CYCLE_SLEEP)
        weekly = is_weekly_crawl()
        logger.info("Starting %s crawl", "weekly" if weekly else "daily")
        await crawl_cycle(weekly=weekly)


if __name__ == "__main__":
    asyncio.run(run())
