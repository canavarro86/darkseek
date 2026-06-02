import logging
import os
import re
import time
import urllib.parse
import urllib.request
import uuid

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, g, jsonify, request
from flask_cors import CORS

from .models import db_size_mb, get_crawl_stats, get_db
from .search import search_pages

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(request_id)s] %(message)s",
)
logger = logging.getLogger(__name__)


class _RequestIdFilter(logging.Filter):
    """Inject the current request_id into every log record.

    Falls back to '-' for logs emitted outside a request context (startup,
    background work) so the format string never blows up.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.request_id = g.request_id
        except (RuntimeError, AttributeError):
            # No active request context (startup / background logging).
            record.request_id = "-"
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(_RequestIdFilter())

app = Flask(__name__)

# Lock CORS to explicit origins. Same-origin traffic (frontend served by nginx
# from the same host) needs no CORS at all; this only matters for other origins.
# Set CORS_ORIGINS to a comma-separated allowlist; default denies cross-origin.
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    CORS(app, resources={r"/api/*": {"origins": _cors_origins}})

CATEGORIES = {"forum", "market", "news", "wiki", "service", "other"}

# Control characters (incl. NUL) that must never reach the FTS engine or logs.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MAX_QUERY_LEN = 200

# v3 onion: 56 base32 chars [a-z2-7], optional path. Anchored to reject junk.
ONION_RE = re.compile(r"^https?://[a-z2-7]{56}\.onion(/.*)?$")

# In-memory per-IP submission rate limiter: 5 submissions per rolling hour.
_submit_rate: dict[str, list[float]] = {}
SUBMIT_LIMIT = 5
SUBMIT_WINDOW = 3600
# Hard cap on tracked IPs so the limiter can't be grown without bound by a
# stream of distinct source addresses. Past this, fully-expired entries are
# swept before admitting a new one.
SUBMIT_MAX_IPS = 10_000


def _sanitize_query(raw: str) -> str:
    """Strip NUL/control characters and clamp length on user search input."""
    cleaned = _CONTROL_CHARS.sub(" ", raw)
    return cleaned.strip()[:MAX_QUERY_LEN]


def _client_ip() -> str:
    """Real client IP behind the nginx reverse proxy."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _sweep_submit_rate(now: float) -> None:
    """Drop IPs whose timestamps have all aged out of the window.

    Keeps the limiter bounded: without this, every distinct source IP would add
    a permanent dict entry. Only triggered once the table grows past the cap, so
    it's O(n) at most once per burst of new IPs, not per request.
    """
    stale = [ip for ip, times in _submit_rate.items()
             if not any(now - t < SUBMIT_WINDOW for t in times)]
    for ip in stale:
        del _submit_rate[ip]


def _submit_allowed(ip: str) -> bool:
    """Sliding-window rate check. Prunes stale timestamps on every call."""
    now = time.time()
    times = [t for t in _submit_rate.get(ip, []) if now - t < SUBMIT_WINDOW]
    if len(times) >= SUBMIT_LIMIT:
        _submit_rate[ip] = times
        return False
    # Bound the table before admitting a brand-new IP.
    if ip not in _submit_rate and len(_submit_rate) >= SUBMIT_MAX_IPS:
        _sweep_submit_rate(now)
    times.append(now)
    _submit_rate[ip] = times
    return True


@app.before_request
def _assign_request_id() -> None:
    g.request_id = uuid.uuid4().hex[:8]


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/stats")
def stats():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM pages WHERE is_alive = 1").fetchone()[0]
        by_category = conn.execute(
            "SELECT category, COUNT(*) as cnt FROM pages WHERE is_alive = 1 GROUP BY category"
        ).fetchall()
    return jsonify({
        "total_pages": total,
        "by_category": {(r["category"] or "other"): r["cnt"] for r in by_category},
        "status": "ok"
    })


@app.get("/metrics")
def metrics():
    """Operational metrics for monitoring the crawler and DB footprint."""
    crawl = get_crawl_stats()
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM pages WHERE is_alive = 1").fetchone()[0]
    return jsonify({
        "last_run": crawl["last_run"],
        "pages_per_hour": crawl["pages_per_hour"],
        "pages_last_cycle": crawl["pages_last_cycle"],
        "db_size_mb": db_size_mb(),
        "total_pages": total,
        "status": "ok",
    })


@app.get("/api/search")
def search():
    query = _sanitize_query(request.args.get("q", ""))
    if not query:
        return jsonify({"error": "q parameter required"}), 400

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    category = request.args.get("category")
    if category and category not in CATEGORIES:
        return jsonify({"error": f"invalid category, must be one of {sorted(CATEGORIES)}"}), 400

    try:
        results, total = search_pages(query, page=page, category=category)
    except Exception:
        logger.exception("Search failed for query=%r", query)
        return jsonify({"error": "internal server error"}), 500

    logger.info("search q=%r category=%s page=%d -> %d hits", query, category, page, total)
    return jsonify(
        {
            "query": query,
            "page": page,
            "total": total,
            "results": results,
        }
    )


if os.environ.get("DEBUG") == "1":

    @app.get("/debug/fts-check")
    def fts_check():
        """One-time diagnostic: verify Russian text is actually stored/indexed.

        Only registered when DEBUG=1. Returns a few Russian rows as JSON.
        """
        with get_db() as conn:
            rows = conn.execute(
                "SELECT title, description FROM pages WHERE lang LIKE 'ru%' LIMIT 3"
            ).fetchall()
        return jsonify({"rows": [dict(r) for r in rows]})


@app.post("/api/submit")
def submit():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not ONION_RE.match(url):
        return jsonify({"status": "error", "message": "Invalid .onion URL"}), 400

    ip = _client_ip()
    if not _submit_allowed(ip):
        return jsonify({
            "status": "error",
            "message": "Rate limit exceeded — max 5 submissions per hour.",
        }), 429

    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM pages WHERE url = ?", (url,)).fetchone()
        if exists:
            return jsonify({"status": "exists", "message": "Already indexed"})
        conn.execute(
            """
            INSERT INTO pages (url, title, description, category, lang, is_alive, fail_count)
            VALUES (?, ?, ?, ?, ?, 1, 0)
            """,
            (url, "Pending scan...", "Submitted by user, pending crawl", "other", "other"),
        )
        conn.commit()

    logger.info("submit url=%r ip=%s -> queued", url, ip)
    return jsonify({
        "status": "queued",
        "message": "Site queued for indexing. Check back in ~1 hour.",
    })


@app.get("/api/lookup")
def lookup():
    """Look up a single .onion URL in the live index for the Check & Submit UI.

    Returns the indexed card if the URL already exists in `pages`, else
    {"found": false}. Validation is intentionally looser than /api/submit:
    any input containing ".onion" is accepted (so the UI can probe whatever a
    user typed); anything else is a 400. This is a read, so — like /api/search
    and the other GET endpoints — it is not behind the submission rate limiter
    (nginx still rate-limits /api/ at the edge).
    """
    url = (request.args.get("url") or "").strip()
    if ".onion" not in url:
        return jsonify({"error": "url must be a .onion address"}), 400

    with get_db() as conn:
        row = conn.execute(
            "SELECT url, title, description, category, lang, last_seen, "
            "is_alive, indexed_at FROM pages WHERE url = ?",
            (url,),
        ).fetchone()

    if row is None:
        return jsonify({"found": False, "url": url})

    return jsonify({
        "found": True,
        "url": row["url"],
        "title": row["title"],
        "description": row["description"],
        "category": row["category"],
        "lang": row["lang"],
        "last_seen": row["last_seen"],
        "is_alive": row["is_alive"],
        "indexed_at": row["indexed_at"],
    })


# Max URLs accepted in a single bulk submission.
MAX_BULK_URLS = 50


@app.post("/api/submit/bulk")
def submit_bulk():
    """Check a batch of .onion URLs and queue the unknown ones for crawling.

    Per URL the result status is one of:
      already_indexed — URL is already in `pages` (title included)
      already_queued  — URL is already a pending/processing crawl_queue row
      queued          — newly inserted into crawl_queue for the crawler
      invalid         — input does not contain ".onion"

    Rate-limited with the same per-IP limiter as /api/submit (one bulk request =
    one submission), since this is the write path that fills the crawl queue.
    """
    data = request.get_json(silent=True) or {}
    urls = data.get("urls")
    if not isinstance(urls, list) or not urls:
        return jsonify({"error": 'body must be {"urls": [...]}'}), 400
    if len(urls) > MAX_BULK_URLS:
        return jsonify({"error": f"max {MAX_BULK_URLS} urls per request"}), 400

    ip = _client_ip()
    if not _submit_allowed(ip):
        return jsonify({
            "status": "error",
            "message": "Rate limit exceeded — max 5 submissions per hour.",
        }), 429

    results = []
    with get_db() as conn:
        for raw in urls:
            url = raw.strip() if isinstance(raw, str) else ""
            if ".onion" not in url:
                # Echo the original input back so the UI can show what failed.
                results.append({"url": raw, "status": "invalid"})
                continue

            page = conn.execute(
                "SELECT title FROM pages WHERE url = ?", (url,)
            ).fetchone()
            if page is not None:
                results.append({
                    "url": url,
                    "status": "already_indexed",
                    "title": page["title"],
                })
                continue

            # Uncommitted inserts earlier in this loop are visible to these reads
            # (same connection), so duplicates within one request resolve to
            # 'already_queued' after the first 'queued'.
            queued = conn.execute(
                "SELECT 1 FROM crawl_queue WHERE url = ?", (url,)
            ).fetchone()
            if queued is not None:
                results.append({"url": url, "status": "already_queued"})
                continue

            # New URL: queue it. OR IGNORE guards against a concurrent request
            # racing in the same URL between our SELECT and INSERT.
            conn.execute(
                "INSERT OR IGNORE INTO crawl_queue (url, source) VALUES (?, 'user')",
                (url,),
            )
            results.append({"url": url, "status": "queued"})
        conn.commit()

    logger.info("bulk submit ip=%s urls=%d", ip, len(urls))
    return jsonify({"results": results})


@app.get("/api/ip")
def api_ip():
    """Return the caller's Tor exit-node IP for the `ip` instant command.

    Behind nginx the real client address arrives via X-Forwarded-For, which
    _client_ip() reads. Per-request and uncacheable, so disable caching.
    """
    resp = jsonify({"ip": _client_ip(), "note": "tor exit node"})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# Process-lifetime cache of url -> shortened url, to avoid duplicate TinyURL
# calls for the same input. Bounded so a hostile caller can't grow it forever.
_shorten_cache: dict[str, str] = {}
_SHORTEN_CACHE_MAX = 1000
SHORTEN_TIMEOUT = 5  # seconds


@app.get("/api/shorten")
def api_shorten():
    """Shorten a URL via TinyURL for the `shorten` instant command.

    Params: url (http/https only, validated before any outbound call).
    Returns: {"short": "<url>"} on success, {"error": ...} otherwise.
    """
    url = (request.args.get("url") or "").strip()

    # Validate before proxying: scheme + host required, sane length, no junk.
    if not url or len(url) > 2048:
        return jsonify({"error": "invalid url"}), 400
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({"error": "invalid url"}), 400

    if url in _shorten_cache:
        return jsonify({"short": _shorten_cache[url], "cached": True})

    api = "https://tinyurl.com/api-create.php?url=" + urllib.parse.quote(url, safe="")
    try:
        with urllib.request.urlopen(api, timeout=SHORTEN_TIMEOUT) as r:
            short = r.read().decode("utf-8", "replace").strip()
    except Exception:
        logger.exception("shorten failed for url=%r", url)
        return jsonify({"error": "shorten service unavailable"}), 502

    if not short.startswith("http"):
        return jsonify({"error": "shorten failed"}), 502

    if len(_shorten_cache) < _SHORTEN_CACHE_MAX:
        _shorten_cache[url] = short
    logger.info("shorten url=%r -> %s", url, short)
    return jsonify({"short": short})


def run() -> None:
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8000"))
    app.run(host=host, port=port)


if __name__ == "__main__":
    run()
