import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# Cost math (claude-haiku-4-5):
#   input:  $0.80 / 1M tokens   output: $4.00 / 1M tokens
#   800 chars ~= 200 input tokens, max_tokens=150 output
#   per call ~= 200/1e6 * 0.80 + 150/1e6 * 4.00 = 0.00016 + 0.0006 ~= $0.00076
#   $5 budget => ~6,500 calls/month => ~215 calls/day
# So only hit the API when the local parser fails (category == "other" or the
# title/description are too short). That skips ~70% of pages and stays in budget.

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 150
INPUT_CHARS = 800       # chars of page text sent to the model
GOOD_FIELD_LEN = 40     # title/description longer than this counts as "good"

VALID_CATEGORIES = {"forum", "market", "news", "wiki", "service", "other"}
VALID_LANGS = {"en", "ru", "de", "fr", "es", "other"}

# Static instructions — sent as a cached system block so repeated calls reuse
# the prompt prefix (cache_control: ephemeral) and only pay for the page text.
SYSTEM_PROMPT = (
    "You classify dark web (.onion) pages for a search index. "
    "Given the page text, respond with ONLY a single JSON object — no preamble, "
    "no explanation, no markdown fences. Use exactly this schema:\n"
    '{"title": "max 60 chars", '
    '"description": "max 160 chars, what the user will find on this page", '
    '"category": "one of: forum|market|news|wiki|service|other", '
    '"lang": "one of: en|ru|de|fr|es|other"}'
)

# --- Rate limit: token bucket, max 5 requests/second (thread-safe) ----------
_RATE = 5.0
_bucket_lock = threading.Lock()
_tokens = _RATE
_last_refill = time.monotonic()


def _acquire() -> None:
    """Block until a request slot is available (<= 5/sec across all threads)."""
    global _tokens, _last_refill
    while True:
        with _bucket_lock:
            now = time.monotonic()
            _tokens = min(_RATE, _tokens + (now - _last_refill) * _RATE)
            _last_refill = now
            if _tokens >= 1:
                _tokens -= 1
                return
            wait = (1 - _tokens) / _RATE
        time.sleep(wait)


# --- Lazy Anthropic client --------------------------------------------------
_client = None
_client_lock = threading.Lock()


def _get_client():
    """Return a cached Anthropic client, or None if unavailable/unconfigured."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                logger.warning("ANTHROPIC_API_KEY not set; using local parser only")
                return None
            try:
                from anthropic import Anthropic
                _client = Anthropic(api_key=api_key, timeout=20.0)
            except Exception:
                logger.exception("Failed to init Anthropic client")
                return None
    return _client


def _parse_response(raw: str, fallback: dict) -> dict:
    """Parse the model's JSON reply; fall back to local result on any problem."""
    if not raw:
        return fallback
    # Strip markdown fences if the model added them despite instructions.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Claude returned invalid JSON; using local parser")
        return fallback
    if not isinstance(data, dict):
        return fallback

    title = (str(data.get("title")) if data.get("title") else fallback["title"])[:60]
    description = (
        str(data.get("description")) if data.get("description") else fallback["description"]
    )[:160]
    category = data.get("category")
    if category not in VALID_CATEGORIES:
        category = fallback["category"]
    lang = data.get("lang")
    if lang not in VALID_LANGS:
        lang = fallback["lang"] if fallback["lang"] in VALID_LANGS else "other"
    return {"title": title, "description": description, "category": category, "lang": lang}


def _call_api(text: str, url: str, fallback: dict) -> dict:
    """Call Claude for a description; on any error return the local fallback."""
    client = _get_client()
    if client is None:
        return fallback

    _acquire()
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"URL: {url}\n\nPage text:\n{text[:INPUT_CHARS]}",
                }
            ],
        )
    except Exception as e:
        logger.warning("Claude API call failed for %s: %s", url, e)
        return fallback

    # Log token usage so monthly cost can be monitored from the logs.
    try:
        u = resp.usage
        logger.info(
            "Claude usage [%s]: in=%d out=%d cache_read=%s cache_write=%s",
            url,
            u.input_tokens,
            u.output_tokens,
            getattr(u, "cache_read_input_tokens", 0),
            getattr(u, "cache_creation_input_tokens", 0),
        )
    except Exception:
        pass

    try:
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
    except Exception:
        return fallback
    return _parse_response(raw, fallback)


def describe(html: str, url: str) -> dict:
    """Return {title, description, category, lang} for a page.

    Runs the local parser first. If it already produced a confident result
    (good title + description and a known category) the API is skipped. Otherwise
    Claude fills the gaps. Any API/JSON/timeout failure falls back to the local
    result silently — this never raises.
    """
    from crawler.parser import parse_page, parse_metadata

    meta = parse_metadata(html, url)
    parsed = parse_page(html, url)
    text = ""
    if parsed:
        meta["category"] = parsed["category"]
        text = parsed.get("text", "")
        if not meta["description"] and text:
            meta["description"] = text[:160]

    # Skip the API when the local parser is already confident: solid title and
    # description (> 40 chars each) AND a non-"other" category. Saves ~70% of calls.
    if (
        len(meta.get("title", "")) > GOOD_FIELD_LEN
        and len(meta.get("description", "")) > GOOD_FIELD_LEN
        and meta.get("category", "other") != "other"
    ):
        return meta

    # Normalize the fallback language to the allowed set (parse_metadata may
    # return values like "en-US").
    if meta.get("lang") not in VALID_LANGS:
        meta["lang"] = "other"

    if not text:
        # No text to send — the local result is all we have.
        return meta

    return _call_api(text, url, meta)
