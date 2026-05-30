import asyncio
import logging
import os
import sys
from typing import Set

import httpx
from dotenv import load_dotenv

load_dotenv()

# Add repo root to path so `api.models` is importable from the crawler container
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.models import mark_dead, upsert_page
from crawler.ai_describe import describe_page
from crawler.parser import parse_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOR_PROXY = os.environ.get("TOR_PROXY", "socks5://tor:9050")
CRAWLER_DELAY = float(os.environ.get("CRAWLER_DELAY", "3"))
CRAWLER_WORKERS = int(os.environ.get("CRAWLER_WORKERS", "2"))
QUEUE_IDLE_TIMEOUT = 120  # Seconds to wait for new URLs before a worker exits

SEED_URLS = [
    # The Hidden Wiki — main onion directory
    "http://zqktlwiuavvvqqt4ybvgvi7tyo4hjl5xgfuvpdf6otjiycgwqbym2qad.onion/wiki/",
    # Dark.fail — trusted onion link list
    "http://darkfailenbsdla5mal2mxn2uz66od5vtzd5qozslagrfzachha3f3id.onion/",
]

REQUEST_TIMEOUT = 30
MAX_RETRIES = 2
CYCLE_SLEEP = 3600  # Re-seed after all workers go idle


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
                queue.task_done()
                continue
            visited.add(url)

            async with semaphore:
                logger.info("Crawling %s", url)
                html = await fetch(client, url)

                if html is None:
                    mark_dead(url)
                    queue.task_done()
                    continue

                parsed = parse_page(html, url)
                ai = describe_page(parsed["title"], parsed["text"], parsed["category"])

                upsert_page(
                    url=url,
                    title=parsed["title"],
                    description=ai["description"],
                    category=ai["category"],
                    lang=ai["lang"],
                    score=ai["score"],
                )
                logger.info("Saved: [%s] %s", ai["category"], parsed["title"][:80])

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
    visited: Set[str] = set()
    semaphore = asyncio.Semaphore(CRAWLER_WORKERS)

    for url in SEED_URLS:
        await queue.put(url)

    transport = httpx.AsyncHTTPTransport(proxy=TOR_PROXY)
    async with httpx.AsyncClient(transport=transport) as client:
        tasks = [
            asyncio.create_task(worker(queue, visited, semaphore, client))
            for _ in range(CRAWLER_WORKERS)
        ]
        await asyncio.gather(*tasks)

    logger.info("Crawl cycle done. Visited %d URLs.", len(visited))


async def run() -> None:
    while True:
        logger.info("Starting crawl cycle")
        await crawl_cycle()
        logger.info("Sleeping %ds before next cycle", CYCLE_SLEEP)
        await asyncio.sleep(CYCLE_SLEEP)


if __name__ == "__main__":
    asyncio.run(run())
