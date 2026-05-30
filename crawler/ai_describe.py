import json
import logging
import os
import threading
import time

from anthropic import Anthropic

logger = logging.getLogger(__name__)

_client: Anthropic | None = None
_rate_lock = threading.Lock()
_last_request_time: float = 0.0
_MIN_INTERVAL = 0.1  # 10 requests/second

CATEGORIES = {"forum", "market", "news", "wiki", "service", "other"}

SYSTEM_PROMPT = (
    "You analyze dark web page content and return structured JSON. "
    "Given a page title and text excerpt, return ONLY valid JSON with these fields:\n"
    '- "title": concise page title, max 60 characters\n'
    '- "description": concise 1-2 sentence factual description of the page (neutral tone, no illegal advice), max 160 characters\n'
    '- "category": one of forum/market/news/wiki/service/other\n'
    '- "lang": ISO 639-1 language code (e.g. en, ru, de, fr, es, other)\n'
    '- "score": float 0.0-1.0 reflecting content quality and relevance\n'
    "Return only the JSON object. No markdown fences, no explanation."
)


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _rate_limit() -> None:
    global _last_request_time
    with _rate_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


def describe_page(title: str, text: str, hint_category: str = "other") -> dict:
    """Call Claude Haiku to generate title, description, category, lang, score."""
    prompt = f"Title: {title}\n\nText excerpt:\n{text[:2000]}"

    try:
        _rate_limit()
        response = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(response.content[0].text)
        category = data.get("category", hint_category)
        return {
            "title": str(data.get("title", title))[:60] or title,
            "description": str(data.get("description", ""))[:160],
            "category": category if category in CATEGORIES else hint_category,
            "lang": str(data.get("lang", "en"))[:8],
            "score": float(data.get("score", 0.5)),
        }
    except Exception as e:
        logger.warning("AI describe failed for %r: %s", title[:60], e)
        return {
            "title": title[:60],
            "description": title,
            "category": hint_category,
            "lang": "en",
            "score": 0.0,
        }
