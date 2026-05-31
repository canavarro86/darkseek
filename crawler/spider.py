import asyncio
import hashlib
import logging
import os
import sys
from typing import Set

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crawler.ai_describe import describe
from crawler.models import mark_dead, should_recrawl, upsert_page
from crawler.parser import parse_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOR_PROXY = os.environ.get("TOR_PROXY", "socks5h://tor:9050")
CRAWLER_DELAY = float(os.environ.get("CRAWLER_DELAY", "3"))
CRAWLER_WORKERS = int(os.environ.get("CRAWLER_WORKERS", "2"))
QUEUE_IDLE_TIMEOUT = 120

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

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
CYCLE_SLEEP = int(os.environ.get("CYCLE_SLEEP", "3600"))

_visited: Set[str] = set()


async def fetch(client: httpx.AsyncClient, url: str) -> str | None:
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
            r.raise_for_status()
            return r.text
        except httpx.HTTPStatusError as e:
            logger.warning("HTTP %d: %s", e.response.status_code, url)
            return None
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2)
            else:
                logger.warning("Failed after %d attempts: %s — %s", MAX_RETRIES, url, e)
    return None


async def worker(
    queue: asyncio.Queue,
    visited: Set[str],
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
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
                logger.info("Crawling %s", url)
                html = await fetch(client, url)

                if html is None:
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
                logger.info("Saved: [%s] %s", meta["category"], meta["title"][:80])

                for link in parsed["links"]:
                    if link not in visited:
                        await queue.put(link)

                await asyncio.sleep(CRAWLER_DELAY)
        except Exception:
            logger.exception("Worker error processing %s", url)
        finally:
            queue.task_done()


async def crawl_cycle() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    semaphore = asyncio.Semaphore(CRAWLER_WORKERS)

    # Remove seeds from visited so should_recrawl re-evaluates them each cycle
    for url in SEED_URLS:
        _visited.discard(url)
        await queue.put(url)

    transport = httpx.AsyncHTTPTransport(proxy=TOR_PROXY)
    async with httpx.AsyncClient(transport=transport) as client:
        tasks = [
            asyncio.create_task(worker(queue, _visited, semaphore, client))
            for _ in range(CRAWLER_WORKERS)
        ]
        await asyncio.gather(*tasks)

    logger.info("Crawl cycle done. Visited %d URLs.", len(_visited))


async def run() -> None:
    while True:
        logger.info("Starting crawl cycle")
        await crawl_cycle()
        logger.info("Sleeping %ds before next cycle", CYCLE_SLEEP)
        await asyncio.sleep(CYCLE_SLEEP)


if __name__ == "__main__":
    asyncio.run(run())
