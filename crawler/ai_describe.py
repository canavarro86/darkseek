"""Page enrichment: title / description / category / lang for a crawled page.

Two strategies sit behind one interface so ingestion never depends on the
Claude API being reachable:

  * ``AIEnricher``        — Claude API path (high quality).
  * ``HeuristicEnricher`` — local-only, zero-network deterministic fallback.

A lightweight circuit breaker routes every page through the heuristic path once
the API has failed N consecutive times (credit exhaustion, 429, 5xx, timeout),
so we don't burn 20s of latency per page on a known-dead dependency. The module
NEVER raises out of ``describe()`` — it always returns a fully populated record
carrying an ``enrichment_method`` of ``'ai'`` or ``'heuristic'``.
"""

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
# So only hit the API when the heuristic result is weak (category == "other" or
# the title/description are too short). That skips ~70% of pages and stays in
# budget. Pages skipped this way are recorded as 'heuristic' so the backfill job
# (scripts/reprocess_ai.py) can upgrade them later when credits allow.

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 150
INPUT_CHARS = 800       # chars of page text sent to the model
GOOD_FIELD_LEN = 40     # title/description longer than this counts as "good"

VALID_CATEGORIES = {"forum", "market", "news", "wiki", "service", "other"}

# Enrichment method values mirrored by the `enrichment_method` DB column.
METHOD_AI = "ai"
METHOD_HEURISTIC = "heuristic"

# --- Circuit breaker config -------------------------------------------------
# After this many consecutive API failures, stop calling the API and route every
# page through the heuristic enricher for COOLDOWN seconds, then re-probe once.
CIRCUIT_FAIL_THRESHOLD = int(os.environ.get("AI_CIRCUIT_THRESHOLD", "5"))
CIRCUIT_COOLDOWN = float(os.environ.get("AI_CIRCUIT_COOLDOWN", "900"))  # 15 min

# Static instructions — sent as a cached system block so repeated calls reuse
# the prompt prefix (cache_control: ephemeral) and only pay for the page text.
SYSTEM_PROMPT = (
    "You classify dark web (.onion) pages for a search index. "
    "Given the page text, respond with ONLY a single JSON object — no preamble, "
    "no explanation, no markdown fences. Use exactly this schema:\n"
    '{"title": "max 60 chars", '
    '"description": "max 160 chars, what the user will find on this page", '
    '"category": "one of: forum|market|news|wiki|service|other", '
    '"lang": "ISO 639-1 two-letter code, e.g. en, ru, de"}'
)


# --- Language normalization (single source of truth) ------------------------
# Used by both enrichers AND scripts/normalize_lang.py so the rule lives in
# exactly one place. ISO-639-1 set kept broad enough not to nuke real languages
# to "other"; anything not 2-letter / not recognized collapses to "other".
ISO_639_1 = {
    "ab", "aa", "af", "ak", "sq", "am", "ar", "an", "hy", "as", "av", "ae",
    "ay", "az", "bm", "ba", "eu", "be", "bn", "bh", "bi", "bs", "br", "bg",
    "my", "ca", "ch", "ce", "ny", "zh", "cv", "kw", "co", "cr", "hr", "cs",
    "da", "dv", "nl", "dz", "en", "eo", "et", "ee", "fo", "fj", "fi", "fr",
    "ff", "gl", "ka", "de", "el", "gn", "gu", "ht", "ha", "he", "hz", "hi",
    "ho", "hu", "ia", "id", "ie", "ga", "ig", "ik", "io", "is", "it", "iu",
    "ja", "jv", "kl", "kn", "kr", "ks", "kk", "km", "ki", "rw", "ky", "kv",
    "kg", "ko", "ku", "kj", "la", "lb", "lg", "li", "ln", "lo", "lt", "lu",
    "lv", "gv", "mk", "mg", "ms", "ml", "mt", "mi", "mr", "mh", "mn", "na",
    "nv", "nd", "ne", "ng", "nb", "nn", "no", "ii", "nr", "oc", "oj", "cu",
    "om", "or", "os", "pa", "pi", "fa", "pl", "ps", "pt", "qu", "rm", "rn",
    "ro", "ru", "sa", "sc", "sd", "se", "sm", "sg", "sr", "gd", "sn", "si",
    "sk", "sl", "so", "st", "es", "su", "sw", "ss", "sv", "ta", "te", "tg",
    "th", "ti", "bo", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw", "ty",
    "ug", "uk", "ur", "uz", "ve", "vi", "vo", "wa", "cy", "wo", "fy", "xh",
    "yi", "yo", "za", "zu",
}

# Frequent 3-letter / legacy aliases mapped down to their ISO-639-1 form.
_LANG_ALIASES = {
    "eng": "en", "rus": "ru", "deu": "de", "ger": "de", "fra": "fr",
    "fre": "fr", "spa": "es", "por": "pt", "ita": "it", "nld": "nl",
    "dut": "nl", "zho": "zh", "chi": "zh", "jpn": "ja", "kor": "ko",
    "ara": "ar", "ukr": "uk", "pol": "pl", "tur": "tr", "fas": "fa",
    "per": "fa", "in": "id",  # legacy Indonesian code
}

LANG_OTHER = "other"


def normalize_lang(raw) -> str:
    """Normalize any language tag to a lowercase ISO-639-1 2-letter code.

    Accepts the messy variants present in the corpus (``en-US``, ``en_GB``,
    ``zh-CN``, ``ru-RU``, ``EN``...). Invalid / empty / unrecognized input
    collapses to ``"other"``. This is the ONLY place the rule is defined.
    """
    if not raw:
        return LANG_OTHER
    # Primary subtag only: split on the BCP-47 / POSIX separators.
    primary = str(raw).strip().lower().replace("_", "-").split("-")[0]
    if not primary:
        return LANG_OTHER
    if primary in _LANG_ALIASES:
        return _LANG_ALIASES[primary]
    if len(primary) == 2 and primary in ISO_639_1:
        return primary
    return LANG_OTHER


# --- Heuristic category (single source of truth, spec rule) -----------------
# Keyword heuristic over URL + title. Ordered: first matching bucket wins, which
# is why market/forum come before the broader service bucket.
_CATEGORY_RULES = [
    ("market",  ("market", "shop", "buy", "cart")),
    ("forum",   ("forum", "board", "thread")),
    ("wiki",    ("wiki",)),
    ("news",    ("news", "press")),
    ("service", ("login", "account", "service", "panel")),
]


def heuristic_category(url: str, title: str) -> str:
    """Classify a page from URL + title keywords. Falls back to 'other'."""
    haystack = f"{url} {title}".lower()
    for category, keywords in _CATEGORY_RULES:
        if any(kw in haystack for kw in keywords):
            return category
    return "other"


def _detect_lang(text: str) -> str:
    """Best-effort language detection from body text -> ISO-639-1 or 'other'.

    Uses langdetect when available; any failure (missing dep, too-short text,
    detection error) degrades to 'other' rather than raising.
    """
    if not text or len(text.strip()) < 20:
        return LANG_OTHER
    try:
        from langdetect import detect, DetectorFactory

        # Deterministic output across runs for the same input.
        DetectorFactory.seed = 0
        return normalize_lang(detect(text))
    except Exception:
        return LANG_OTHER


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


# --- Circuit breaker --------------------------------------------------------
class _CircuitBreaker:
    """Trips open after N consecutive failures; re-probes after a cooldown.

    States:
      closed     — API calls allowed.
      open        — too many failures; skip the API until cooldown elapses.
      half-open   — cooldown elapsed; allow exactly one probe. Success closes
                    the circuit, failure re-opens it for another cooldown.
    Thread-safe: the crawler enriches from a thread pool / async workers.
    """

    def __init__(self, threshold: int, cooldown: float) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._probing = False

    def allow(self) -> bool:
        """Return True if a call may be attempted right now."""
        with self._lock:
            if self._consecutive_failures < self._threshold:
                return True
            # Circuit is open: only allow a single probe once cooldown passes.
            if time.monotonic() - self._opened_at < self._cooldown:
                return False
            if self._probing:
                return False  # a probe is already in flight
            self._probing = True
            logger.info("AI circuit half-open: probing API after cooldown")
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._consecutive_failures:
                logger.info("AI circuit closed: API recovered")
            self._consecutive_failures = 0
            self._opened_at = 0.0
            self._probing = False

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._probing = False
            if self._consecutive_failures == self._threshold:
                self._opened_at = time.monotonic()
                logger.warning(
                    "AI circuit OPEN: %d consecutive failures, cooling down %.0fs",
                    self._consecutive_failures,
                    self._cooldown,
                )
            elif self._consecutive_failures > self._threshold:
                # Failed probe in half-open: restart the cooldown window.
                self._opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return (
                self._consecutive_failures >= self._threshold
                and time.monotonic() - self._opened_at < self._cooldown
            )


_breaker = _CircuitBreaker(CIRCUIT_FAIL_THRESHOLD, CIRCUIT_COOLDOWN)


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
                logger.warning("ANTHROPIC_API_KEY not set; using heuristic enricher only")
                return None
            try:
                from anthropic import Anthropic
                _client = Anthropic(api_key=api_key, timeout=20.0)
            except Exception:
                logger.exception("Failed to init Anthropic client")
                return None
    return _client


def _parse_response(raw: str, fallback: dict) -> dict | None:
    """Parse the model's JSON reply.

    Returns a populated record on success, or None when the reply is malformed
    so the caller can treat bad JSON as an API failure (heuristic fallback).
    """
    if not raw:
        return None
    # Strip markdown fences if the model added them despite instructions.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Claude returned invalid JSON")
        return None
    if not isinstance(data, dict):
        return None

    title = (str(data.get("title")) if data.get("title") else fallback["title"])[:60]
    description = (
        str(data.get("description")) if data.get("description") else fallback["description"]
    )[:160]
    category = data.get("category")
    if category not in VALID_CATEGORIES:
        category = fallback["category"]
    lang = normalize_lang(data.get("lang"))
    if lang == LANG_OTHER:
        lang = fallback["lang"]
    return {"title": title, "description": description, "category": category, "lang": lang}


# --- Failure classification -------------------------------------------------
def _is_circuit_signal(exc: Exception) -> bool:
    """True for failures that mean 'the dependency is down' (credit/429/5xx).

    These are the canonical circuit-open signals. Logged distinctly so an
    operator can tell credit exhaustion apart from a transient blip.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    if status in (429,) or (status is not None and 500 <= status < 600):
        return True
    if status == 400 and "credit balance" in str(exc).lower():
        return True
    return False


def _call_api(text: str, url: str, fallback: dict) -> dict | None:
    """Call Claude for a description.

    Returns a populated record on success, or None on ANY failure (HTTP error,
    timeout, bad JSON). Records success/failure against the circuit breaker so a
    dead dependency stops being probed. Never raises.
    """
    client = _get_client()
    if client is None:
        return None

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
        if _is_circuit_signal(e):
            logger.warning("Claude API unavailable (circuit signal) for %s: %s", url, e)
        else:
            logger.warning("Claude API call failed for %s: %s", url, e)
        _breaker.record_failure()
        return None

    # Log token usage on every call so monthly cost/budget can be tracked from
    # the logs. Never let a usage-logging hiccup mask a successful API call.
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
        logger.debug("Could not read token usage for %s", url, exc_info=True)

    try:
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
    except Exception:
        raw = ""

    parsed = _parse_response(raw, fallback)
    if parsed is None:
        # A 200 with unusable content is still a failure for our purposes.
        _breaker.record_failure()
        return None
    _breaker.record_success()
    return parsed


# --- Enricher strategies ----------------------------------------------------
class HeuristicEnricher:
    """Local-only, deterministic enrichment. Zero network. Never raises."""

    def enrich(self, html: str, url: str) -> dict:
        from crawler.parser import parse_metadata, parse_page

        meta = parse_metadata(html, url)
        parsed = parse_page(html, url)
        body = parsed.get("text", "") if parsed else ""

        # title: <title> -> <h1> -> first 60 chars of cleaned text, trimmed to 60.
        title = meta.get("title") or ""
        if not title and body:
            title = body[:60]
        title = title[:60]

        # description: <meta name=description> -> first 160 chars of body text.
        description = meta.get("description") or ""
        if not description and body:
            description = body[:160]

        category = heuristic_category(url, title)
        lang = _detect_lang(body)

        cat_src = "keyword" if category != "other" else "default"
        lang_src = "langdetect" if lang != LANG_OTHER else "none"
        logger.info(
            "enrich method=heuristic category_src=%s lang_src=%s url=%s",
            cat_src, lang_src, url,
        )
        return {
            "title": title,
            "description": description,
            "category": category,
            "lang": lang,
            "enrichment_method": METHOD_HEURISTIC,
        }


class AIEnricher:
    """Claude API enrichment with a heuristic fallback on every failure path.

    Holds a reference to a HeuristicEnricher so it can (a) seed the API prompt
    fallback fields and (b) degrade gracefully when the circuit is open or the
    call fails. Returns a record marked 'ai' only when the API actually answered.
    """

    def __init__(self, heuristic: HeuristicEnricher) -> None:
        self._heuristic = heuristic

    def enrich(self, html: str, url: str) -> dict:
        from crawler.parser import parse_page

        base = self._heuristic.enrich(html, url)

        # Budget skip: a confident heuristic result (good title + description +
        # a known category) does not need the API. Recorded as 'heuristic' so
        # the backfill job can still upgrade it later when credits are flush.
        if (
            len(base.get("title", "")) > GOOD_FIELD_LEN
            and len(base.get("description", "")) > GOOD_FIELD_LEN
            and base.get("category", "other") != "other"
        ):
            return base

        if not _breaker.allow():
            # Circuit open: don't even try — heuristic record already computed.
            return base

        parsed = parse_page(html, url)
        text = parsed.get("text", "") if parsed else ""
        if not text:
            return base  # nothing to send; heuristic is all we have

        fallback = {k: base[k] for k in ("title", "description", "category", "lang")}
        result = _call_api(text, url, fallback)
        if result is None:
            return base  # failure already recorded against the breaker
        result["enrichment_method"] = METHOD_AI
        logger.info(
            "enrich method=ai category_src=ai lang_src=ai url=%s", url
        )
        return result


# Module-level singletons so the rate limiter / breaker state is shared.
_heuristic_enricher = HeuristicEnricher()
_ai_enricher = AIEnricher(_heuristic_enricher)


def describe(html: str, url: str) -> dict:
    """Return {title, description, category, lang, enrichment_method} for a page.

    Always returns a fully populated record — never NULL fields, never raises.
    Routes through the AI enricher (which self-degrades to heuristic on circuit
    open / API failure) unless the API is unconfigured, in which case it goes
    straight to the heuristic path.
    """
    try:
        if _get_client() is None:
            return _heuristic_enricher.enrich(html, url)
        return _ai_enricher.enrich(html, url)
    except Exception:
        # Absolute backstop: the enrichment layer must never break ingestion.
        logger.exception("Enrichment failed hard for %s; using minimal heuristic", url)
        try:
            return _heuristic_enricher.enrich(html, url)
        except Exception:
            return {
                "title": url[:60],
                "description": "",
                "category": "other",
                "lang": LANG_OTHER,
                "enrichment_method": METHOD_HEURISTIC,
            }


def classify_text(text: str, url: str) -> dict | None:
    """Classify pre-extracted text via the Claude API (used by the backfill job).

    Unlike describe(), this takes already-stored text (title + description) and
    returns the AI record or None on failure — no heuristic fallback, because the
    backfill job wants to leave a row as 'heuristic' when the API can't improve
    it. Respects the same rate limiter and circuit breaker.
    """
    if not text or not text.strip():
        return None
    if _get_client() is None or not _breaker.allow():
        return None
    fallback = {"title": text[:60], "description": text[:160], "category": "other", "lang": LANG_OTHER}
    result = _call_api(text, url, fallback)
    if result is None:
        return result
    result["enrichment_method"] = METHOD_AI
    return result
