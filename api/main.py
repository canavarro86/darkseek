import hashlib
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, Response, g, jsonify, request
from flask_cors import CORS

from config.blocked import BLOCKED_SEARCH_TERMS
from .models import (
    db_size_mb,
    get_crawl_stats,
    get_db,
    get_search_stats,
    get_visitor_stats,
    log_search_query,
)
from .openapi import OPENAPI_SPEC, SWAGGER_UI_HTML
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

# Illegal-content search blocklist (CSAM + illegal goods). Single source of truth
# now lives in config/blocked.py (FIX 2), shared with the crawler. A query
# containing any of these substrings is refused before it ever hits the index.

def _is_blocked_query(query: str) -> bool:
    """True if the query contains any blocked CSAM search term (substring)."""
    lowered = query.lower()
    return any(term in lowered for term in BLOCKED_SEARCH_TERMS)

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


# --- Per-endpoint rate limiting (FIX 3) -------------------------------------
# SQLite-backed (cross-worker: gunicorn runs 2 workers). The key is a daily-
# rotating, IP-derived hash so we never store a raw IP. 10 requests / 60s window
# per endpoint; on exceed -> HTTP 429 + Retry-After. Rows older than an hour are
# swept lazily on each call.
RATE_WINDOW = 60
RATE_MAX = 10


def _rate_limited(endpoint: str):
    """Return a 429 Response if the caller is over the limit, else None.

    Privacy: the stored key is sha256(IP + endpoint + date); the raw IP is never
    written. The date component also rotates the key every 24h.
    """
    ip = _client_ip()
    today = date.today().isoformat()
    key = hashlib.sha256(f"{ip}|{endpoint}|{today}".encode()).hexdigest()
    now = int(time.time())
    window_start = now - (now % RATE_WINDOW)
    try:
        with get_db() as conn:
            # Lazy cleanup of windows older than one hour.
            conn.execute("DELETE FROM rate_limits WHERE window_start < ?", (now - 3600,))
            conn.execute(
                "INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1) "
                "ON CONFLICT(key, window_start) DO UPDATE SET count = count + 1",
                (key, window_start),
            )
            count = conn.execute(
                "SELECT count FROM rate_limits WHERE key = ? AND window_start = ?",
                (key, window_start),
            ).fetchone()[0]
            conn.commit()
    except Exception:
        # Fail open: a limiter DB hiccup must never block legitimate traffic.
        logger.debug("rate limiter error on %s", endpoint, exc_info=True)
        return None

    if count > RATE_MAX:
        retry_after = max(1, window_start + RATE_WINDOW - now)
        resp = jsonify({"ok": False, "error": "rate limit exceeded"})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        logger.info("rate limited endpoint=%s (count=%d)", endpoint, count)
        return resp
    return None


# --- Daily unique-visitor tracking (PART C) ---------------------------------
def _track_visitor() -> None:
    """Record one daily-unique visitor for the current request. Never raises.

    Privacy (non-negotiable): the raw IP is never stored or logged. We store only
    ip_hash = sha256(IP + daily_salt), where daily_salt = sha256(today + secret).
    The salt rotates every 24h, so hashes from different days cannot be linked.
    """
    try:
        ip = _client_ip()
        today = date.today().isoformat()
        salt = hashlib.sha256((today + "darkseek-salt").encode()).hexdigest()
        ip_hash = hashlib.sha256((ip + salt).encode()).hexdigest()
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO daily_visitors (date, ip_hash) VALUES (?, ?)",
                (today, ip_hash),
            )
            conn.commit()
    except Exception:
        # Best-effort analytics only — swallow everything.
        logger.debug("visitor tracking failed", exc_info=True)


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
    visitors = get_visitor_stats()  # {visitors_today, visitors_yesterday}
    return jsonify({
        # `total_pages` kept for backward compatibility with the frontend;
        # `pages_indexed` is the PART C alias.
        "total_pages": total,
        "pages_indexed": total,
        "by_category": {(r["category"] or "other"): r["cnt"] for r in by_category},
        "visitors_today": visitors["visitors_today"],
        "visitors_yesterday": visitors["visitors_yesterday"],
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
    # PART C: count this request as a daily-unique visitor (privacy-safe hash).
    _track_visitor()

    query = _sanitize_query(request.args.get("q", ""))
    if not query:
        return jsonify({"error": "q parameter required"}), 400

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    # Refuse illegal (CSAM) queries before touching the index. Return an empty,
    # well-formed result set (HTTP 200) so the UI degrades gracefully; log only
    # the query length, never the raw text (no-logs policy, see /privacy.html).
    if _is_blocked_query(query):
        logger.warning("Blocked illegal search query (qlen=%d)", len(query))
        return jsonify({
            "query": query,
            "page": page,
            "total": 0,
            "results": [],
            "blocked": True,
            "message": "This search is not available.",
        }), 200

    category = request.args.get("category")
    if category and category not in CATEGORIES:
        return jsonify({"error": f"invalid category, must be one of {sorted(CATEGORIES)}"}), 400

    # Content filtering is ON unless explicitly disabled with safe_mode=false.
    safe_mode = request.args.get("safe_mode", "true").lower() != "false"

    try:
        results, total = search_pages(
            query, page=page, category=category, safe_mode=safe_mode
        )
    except Exception:
        # No-logs policy (see /privacy.html): never log the raw query text. Log
        # only its length so a failure is still diagnosable without retaining it.
        logger.exception("Search failed (qlen=%d)", len(query))
        return jsonify({"error": "internal server error"}), 500

    # Query text is deliberately NOT logged — only metadata (length + hit count).
    logger.info(
        "search qlen=%d category=%s page=%d safe=%s -> %d hits",
        len(query), category, page, safe_mode, total,
    )
    resp = jsonify(
        {
            "query": query,
            "page": page,
            "total": total,
            "results": results,
        }
    )
    # Persist the query for index-quality analysis after the response is built.
    # log_search_query never raises, so this cannot break or block the response.
    log_search_query(query, total, safe_mode, category)
    return resp


@app.get("/api/search-stats")
def search_stats():
    """Public, no-auth aggregate search stats (popular / zero-result queries).

    Cached for 5 minutes at the edge/browser since the underlying aggregates
    move slowly and the query is a few cheap grouped counts.
    """
    resp = jsonify(get_search_stats())
    resp.headers["Cache-Control"] = "max-age=300"
    return resp


# --- v2.0 community trust: Proof-of-Work-gated voting / reporting -------------
# A PoW gate (no accounts, no IPs) makes ballot-stuffing costly: every vote/report
# requires a SHA256 partial pre-image. Challenges live in SQLite (pow_challenges)
# so any gunicorn worker validates a challenge another worker minted; pow_hash is
# UNIQUE across votes+reports, so a solved proof is single-use (replay-proof).
POW_DIFFICULTY = 4                       # SHA256(challenge+nonce) must start "0000"
POW_PREFIX = "0" * POW_DIFFICULTY
CHALLENGE_TTL = 300                      # seconds (5 min)
REPORT_REASONS = {"scam", "offline", "illegal", "spam"}
SCAM_AUTOTAG_THRESHOLD = 5               # >= this many 'scam' reports -> content_tag='scam'


def _verify_pow(data: dict):
    """Shared PoW gate for /api/vote and /api/report.

    Returns ((page_id, pow_hash), None) on success, or (None, (response, status))
    on any failure. Validates: challenge present+unexpired (DB-backed, cross-worker),
    SHA256 prefix difficulty, replay (pow_hash unused), page exists. Consumes the
    challenge and sweeps expired rows on success.
    """
    challenge = str(data.get("challenge") or "")
    nonce = str(data.get("nonce") or "")
    if not challenge or not nonce:
        return None, (jsonify({"ok": False, "error": "challenge and nonce required"}), 400)

    pow_hash = hashlib.sha256((challenge + nonce).encode()).hexdigest()
    if not pow_hash.startswith(POW_PREFIX):
        return None, (jsonify({"ok": False, "error": "invalid proof of work"}), 400)

    with get_db() as conn:
        row = conn.execute(
            "SELECT page_id FROM pow_challenges "
            "WHERE challenge = ? AND expires_at > CURRENT_TIMESTAMP",
            (challenge,),
        ).fetchone()
        if row is None:
            return None, (jsonify({"ok": False, "error": "challenge expired or unknown"}), 400)
        page_id = row["page_id"]
        # Replay guard: a solved proof may be spent exactly once, across both tables.
        used = (
            conn.execute("SELECT 1 FROM votes WHERE pow_hash = ?", (pow_hash,)).fetchone()
            or conn.execute("SELECT 1 FROM reports WHERE pow_hash = ?", (pow_hash,)).fetchone()
        )
        if used is not None:
            return None, (jsonify({"ok": False, "error": "proof already used"}), 409)
        if conn.execute("SELECT 1 FROM pages WHERE id = ?", (page_id,)).fetchone() is None:
            return None, (jsonify({"ok": False, "error": "page not found"}), 404)
        # Consume this challenge and opportunistically sweep expired ones.
        conn.execute("DELETE FROM pow_challenges WHERE challenge = ?", (challenge,))
        conn.execute("DELETE FROM pow_challenges WHERE expires_at <= CURRENT_TIMESTAMP")
        conn.commit()
    return (page_id, pow_hash), None


@app.get("/api/challenge")
def api_challenge():
    """Mint a PoW challenge for a page. Stored in SQLite with a 5-minute TTL."""
    try:
        page_id = int(request.args.get("page_id", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "page_id required"}), 400

    challenge = os.urandom(16).hex()
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM pages WHERE id = ?", (page_id,)).fetchone() is None:
            return jsonify({"error": "page not found"}), 404
        conn.execute(
            "INSERT OR REPLACE INTO pow_challenges (challenge, page_id, expires_at) "
            "VALUES (?, ?, datetime('now', ?))",
            (challenge, page_id, f"+{CHALLENGE_TTL} seconds"),
        )
        conn.commit()
    resp = jsonify({"challenge": challenge, "difficulty": POW_DIFFICULTY})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/vote")
def api_vote():
    limited = _rate_limited("vote")
    if limited:
        return limited
    data = request.get_json(silent=True) or {}
    vote_type = data.get("vote_type")
    if vote_type not in ("fresh", "rotten"):
        return jsonify({"ok": False, "error": "vote_type must be fresh|rotten"}), 400

    verified, err = _verify_pow(data)
    if err:
        return err
    page_id, pow_hash = verified

    col = "fresh_votes" if vote_type == "fresh" else "rotten_votes"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO votes (page_id, pow_hash, vote_type) VALUES (?, ?, ?)",
            (page_id, pow_hash, vote_type),
        )
        # Tally, then derive onion_score = (fresh / total) * 4 + 1 -> a 1..5 rating.
        conn.execute(f"UPDATE pages SET {col} = {col} + 1 WHERE id = ?", (page_id,))
        conn.execute(
            "UPDATE pages SET onion_score = "
            "CASE WHEN (fresh_votes + rotten_votes) > 0 "
            "THEN (CAST(fresh_votes AS REAL) / (fresh_votes + rotten_votes)) * 4 + 1 "
            "ELSE NULL END WHERE id = ?",
            (page_id,),
        )
        row = conn.execute(
            "SELECT onion_score FROM pages WHERE id = ?", (page_id,)
        ).fetchone()
        conn.commit()

    score = row["onion_score"]
    logger.info("vote page_id=%d type=%s -> onion_score=%s", page_id, vote_type, score)
    return jsonify({
        "ok": True,
        "onion_score": round(score, 2) if score is not None else None,
        "display": round(score) if score is not None else None,
    })


@app.post("/api/report")
def api_report():
    limited = _rate_limited("report")
    if limited:
        return limited
    data = request.get_json(silent=True) or {}
    reason = data.get("reason")
    if reason not in REPORT_REASONS:
        return jsonify(
            {"ok": False, "error": f"reason must be one of {sorted(REPORT_REASONS)}"}
        ), 400

    verified, err = _verify_pow(data)
    if err:
        return err
    page_id, pow_hash = verified

    with get_db() as conn:
        conn.execute(
            "INSERT INTO reports (page_id, reason, pow_hash) VALUES (?, ?, ?)",
            (page_id, reason, pow_hash),
        )
        scam_count = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE page_id = ? AND reason = 'scam'",
            (page_id,),
        ).fetchone()[0]
        # Community auto-tag: enough independent scam reports flips the tag, which
        # safe-mode search continues to surface but which the UI marks as scam.
        if scam_count >= SCAM_AUTOTAG_THRESHOLD:
            conn.execute(
                "UPDATE pages SET content_tag = 'scam' WHERE id = ?", (page_id,)
            )
        conn.commit()

    logger.info("report page_id=%d reason=%s scam_count=%d", page_id, reason, scam_count)
    return jsonify({"ok": True, "message": "Report recorded"})


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


SHORTEN_TIMEOUT = 5  # seconds


@app.get("/api/shorten")
def api_shorten():
    """Shorten a URL via TinyURL for the `shorten` instant command.

    Params: url (http/https only, validated before any outbound call).
    Returns: {"short": "<url>"} on success, {"error": ...} otherwise.

    FIX 4: results are cached in the `url_cache` table, so the cache survives
    container restarts and a repeated shorten never re-hits TinyURL.
    """
    limited = _rate_limited("shorten")
    if limited:
        return limited

    url = (request.args.get("url") or "").strip()

    # Validate before proxying: scheme + host required, sane length, no junk.
    if not url or len(url) > 2048:
        return jsonify({"error": "invalid url"}), 400
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({"error": "invalid url"}), 400

    # Persistent cache lookup first.
    with get_db() as conn:
        row = conn.execute(
            "SELECT short_url FROM url_cache WHERE original_url = ?", (url,)
        ).fetchone()
    if row is not None:
        return jsonify({"short": row["short_url"], "cached": True})

    api = "https://tinyurl.com/api-create.php?url=" + urllib.parse.quote(url, safe="")
    try:
        with urllib.request.urlopen(api, timeout=SHORTEN_TIMEOUT) as r:
            short = r.read().decode("utf-8", "replace").strip()
    except Exception:
        logger.exception("shorten failed for url=%r", url)
        return jsonify({"error": "shorten service unavailable"}), 502

    if not short.startswith("http"):
        return jsonify({"error": "shorten failed"}), 502

    # Persist for next time (INSERT OR IGNORE: a concurrent writer wins harmlessly).
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO url_cache (original_url, short_url) VALUES (?, ?)",
            (url, short),
        )
        conn.commit()
    logger.info("shorten url=%r -> %s", url, short)
    return jsonify({"short": short})


# --- OpenAPI documentation (FIX 5) ------------------------------------------
# The API is Flask, not FastAPI, so there is no built-in /docs. We serve a
# hand-maintained OpenAPI 3 spec at /openapi.json and a minimal Swagger-UI page
# at /docs (UI assets loaded from a CDN; the spec itself is fully local).
@app.get("/openapi.json")
def openapi_json():
    """Machine-readable OpenAPI 3.0 description of the public API."""
    resp = jsonify(OPENAPI_SPEC)
    resp.headers["Cache-Control"] = "max-age=3600"
    return resp


@app.get("/docs")
def api_docs():
    """Human-readable Swagger UI, rendered against /openapi.json."""
    return Response(SWAGGER_UI_HTML, mimetype="text/html")


def run() -> None:
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8000"))
    app.run(host=host, port=port)


if __name__ == "__main__":
    run()
