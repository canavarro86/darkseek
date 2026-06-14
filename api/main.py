import csv
import hashlib
import io
import logging
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date
from functools import wraps

import bcrypt
import jwt
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

# --- Admin panel: auth secret + decorators + helpers ------------------------
# Falls back to a per-process random secret if JWT_SECRET is unset; that logs
# every admin out on restart but never ships a hard-coded key. Set JWT_SECRET in
# .env for stable sessions across restarts.
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))


def require_admin(fn):
    """Gate an endpoint behind a valid admin session.

    Reads the httpOnly 'admin_token' JWT, verifies its signature, then confirms
    the referenced session row is unexpired and the admin is still active. On
    success populates g.current_admin and g.session_id; on any failure returns
    HTTP 401 with a JSON error.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.cookies.get("admin_token")
        if not token:
            return jsonify({"error": "Unauthorized"}), 401
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except Exception:
            return jsonify({"error": "Unauthorized"}), 401
        session_id = payload.get("session_id")
        admin_id = payload.get("sub")
        if not session_id or admin_id is None:
            return jsonify({"error": "Unauthorized"}), 401
        with get_db() as conn:
            row = conn.execute(
                "SELECT a.id AS aid, a.username, a.role "
                "FROM admin_sessions s JOIN admins a ON a.id = s.admin_id "
                "WHERE s.id = ? AND s.expires_at > CURRENT_TIMESTAMP "
                "AND a.is_active = 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return jsonify({"error": "Unauthorized"}), 401
        g.current_admin = {"id": row["aid"], "username": row["username"], "role": row["role"]}
        g.session_id = session_id
        return fn(*args, **kwargs)

    return wrapper


def require_superadmin(fn):
    """Like require_admin, but additionally requires the 'superadmin' role."""
    @require_admin
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if g.current_admin["role"] != "superadmin":
            return jsonify({"error": "Forbidden"}), 403
        return fn(*args, **kwargs)

    return wrapper


def log_audit(conn, admin_id, action, target=None, details=None, ip=None):
    """Append one row to admin_audit_log. The caller owns the commit.

    The raw IP is never stored — only sha256(ip) — to honour the no-logs policy.
    """
    ip_hash = hashlib.sha256(ip.encode()).hexdigest() if ip else None
    conn.execute(
        "INSERT INTO admin_audit_log (admin_id, action, target, details, ip_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (admin_id, action, target, details, ip_hash),
    )


def _admin_rate_limit(tag: str, window: int, max_attempts: int) -> bool:
    """Cross-worker rate limit on the SQLite rate_limits table.

    Returns True if the caller is still under the limit for this window, False if
    they have exceeded it. The stored key is sha256(IP + tag); the raw IP is never
    written. Fails open on any DB error so a hiccup cannot lock admins out.
    """
    ip = _client_ip()
    key = hashlib.sha256(f"{ip}|{tag}".encode()).hexdigest()
    now = int(time.time())
    window_start = now - (now % window)
    try:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM rate_limits WHERE window_start < ?", (now - window * 2,)
            )
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
        logger.debug("admin rate limiter error tag=%s", tag, exc_info=True)
        return True
    return count <= max_attempts


def _page_args():
    """Parse (page, per_page) from the query string with sane bounds."""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(100, max(1, int(request.args.get("per_page", 50))))
    except (ValueError, TypeError):
        per_page = 50
    return page, per_page

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


# --- Admin panel endpoints --------------------------------------------------
# All /api/admin/* routes are reachable only through the private admin onion
# address (nginx 404s them on the public addresses). Authentication is a signed,
# httpOnly JWT bound to a server-side admin_sessions row; every mutating action
# is written to admin_audit_log.

@app.post("/api/admin/login")
def admin_login():
    ip = _client_ip()
    # Brute-force throttle: 5 attempts per 15-minute window per IP.
    if not _admin_rate_limit("admin_login", 900, 5):
        return jsonify({"error": "Too many attempts. Try again later."}), 429

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Invalid credentials"}), 401

    with get_db() as conn:
        admin = conn.execute(
            "SELECT id, username, password_hash, role, is_active "
            "FROM admins WHERE username = ?",
            (username,),
        ).fetchone()
        if admin is None or not admin["is_active"]:
            return jsonify({"error": "Invalid credentials"}), 401
        try:
            ok = bcrypt.checkpw(password.encode(), admin["password_hash"].encode())
        except Exception:
            ok = False
        if not ok:
            return jsonify({"error": "Invalid credentials"}), 401

        session_id = str(uuid.uuid4())
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()
        conn.execute(
            "INSERT INTO admin_sessions (id, admin_id, expires_at, ip_hash) "
            "VALUES (?, ?, datetime('now', '+8 hours'), ?)",
            (session_id, admin["id"], ip_hash),
        )
        conn.execute(
            "UPDATE admins SET last_login = CURRENT_TIMESTAMP WHERE id = ?",
            (admin["id"],),
        )
        log_audit(conn, admin["id"], "login", ip=ip)
        conn.commit()

    token = jwt.encode(
        {"sub": admin["id"], "session_id": session_id, "role": admin["role"]},
        JWT_SECRET,
        algorithm="HS256",
    )
    resp = jsonify({"status": "ok", "username": admin["username"], "role": admin["role"]})
    # httpOnly so JS can never read it; SameSite=Strict; 8h lifetime. secure is
    # intentionally off — the onion service is served over plain HTTP inside Tor.
    resp.set_cookie(
        "admin_token", token,
        httponly=True, samesite="Strict", max_age=28800, path="/",
    )
    return resp


@app.post("/api/admin/logout")
@require_admin
def admin_logout():
    ip = _client_ip()
    with get_db() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE id = ?", (g.session_id,))
        log_audit(conn, g.current_admin["id"], "logout", ip=ip)
        conn.commit()
    resp = jsonify({"status": "ok"})
    resp.set_cookie("admin_token", "", expires=0, path="/")
    return resp


@app.get("/api/admin/dashboard")
@require_admin
def admin_dashboard():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        alive = conn.execute("SELECT COUNT(*) FROM pages WHERE is_alive = 1").fetchone()[0]
        pages_today = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE indexed_at >= datetime('now', '-1 day')"
        ).fetchone()[0]
        pages_7d = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE indexed_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        pages_30d = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE indexed_at >= datetime('now', '-30 days')"
        ).fetchone()[0]
        cat_rows = conn.execute(
            "SELECT category, COUNT(*) AS c FROM pages GROUP BY category"
        ).fetchall()
        lang_rows = conn.execute(
            "SELECT lang, COUNT(*) AS c FROM pages GROUP BY lang"
        ).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) FROM removal_requests WHERE status = 'pending'"
        ).fetchone()[0]
        unread = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE is_read = 0"
        ).fetchone()[0]
        audit = conn.execute(
            "SELECT l.action, a.username, l.target, l.created_at "
            "FROM admin_audit_log l LEFT JOIN admins a ON a.id = l.admin_id "
            "ORDER BY l.created_at DESC LIMIT 10"
        ).fetchall()

    by_category = {c: 0 for c in ("forum", "market", "news", "wiki", "service", "other")}
    for r in cat_rows:
        key = r["category"] or "other"
        by_category[key] = by_category.get(key, 0) + r["c"]
    by_lang = {(r["lang"] or "unknown"): r["c"] for r in lang_rows}

    return jsonify({
        "total_pages": total,
        "alive_pages": alive,
        "dead_pages": total - alive,
        "pages_today": pages_today,
        "pages_7d": pages_7d,
        "pages_30d": pages_30d,
        "by_category": by_category,
        "by_lang": by_lang,
        "pending_removals": pending,
        "unread_notifications": unread,
        "recent_audit": [
            {
                "action": r["action"],
                "username": r["username"],
                "target": r["target"],
                "created_at": r["created_at"],
            }
            for r in audit
        ],
    })


@app.get("/api/admin/pages")
@require_admin
def admin_pages():
    q = (request.args.get("q") or "").strip()
    category = request.args.get("category")
    lang = request.args.get("lang")
    alive = request.args.get("alive")
    page, per_page = _page_args()

    join = ""
    where = []
    params = []
    if q:
        if q.startswith("http"):
            where.append("p.url LIKE ?")
            params.append(f"%{q}%")
        else:
            join = " JOIN pages_fts ON pages_fts.rowid = p.id"
            where.append("pages_fts MATCH ?")
            params.append(q)
    if category in CATEGORIES:
        where.append("p.category = ?")
        params.append(category)
    if lang:
        where.append("p.lang = ?")
        params.append(lang)
    if alive in ("alive", "1"):
        where.append("p.is_alive = 1")
    elif alive in ("dead", "0"):
        where.append("p.is_alive = 0")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * per_page
    try:
        with get_db() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM pages p{join}{where_sql}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT p.id, p.url, p.title, p.description, p.category, p.lang, "
                f"p.is_alive, p.indexed_at, p.last_seen FROM pages p{join}{where_sql} "
                f"ORDER BY p.indexed_at DESC LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()
    except Exception:
        # Most likely a malformed FTS MATCH expression from the search box.
        return jsonify({"error": "invalid search query"}), 400

    total_pages = (total + per_page - 1) // per_page
    return jsonify({
        "pages": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "total_pages": total_pages,
    })


@app.get("/api/admin/pages/<int:page_id>")
@require_admin
def admin_page_get(page_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.put("/api/admin/pages/<int:page_id>")
@require_admin
def admin_page_update(page_id):
    ip = _client_ip()
    data = request.get_json(silent=True) or {}
    allowed = ("title", "description", "category", "lang", "is_alive")
    sets = []
    params = []
    for field in allowed:
        if field in data:
            sets.append(f"{field} = ?")
            value = data[field]
            if field == "is_alive":
                value = 1 if value in (1, True, "1", "true", "alive") else 0
            params.append(value)
    if not sets:
        return jsonify({"error": "no fields to update"}), 400

    with get_db() as conn:
        existing = conn.execute("SELECT url FROM pages WHERE id = ?", (page_id,)).fetchone()
        if existing is None:
            return jsonify({"error": "not found"}), 404
        conn.execute(
            f"UPDATE pages SET {', '.join(sets)} WHERE id = ?", params + [page_id]
        )
        log_audit(conn, g.current_admin["id"], "edit_page", target=existing["url"], ip=ip)
        conn.commit()
    return jsonify({"status": "ok"})


@app.delete("/api/admin/pages/<int:page_id>")
@require_superadmin
def admin_page_delete(page_id):
    ip = _client_ip()
    with get_db() as conn:
        existing = conn.execute("SELECT url FROM pages WHERE id = ?", (page_id,)).fetchone()
        if existing is None:
            return jsonify({"error": "not found"}), 404
        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        log_audit(conn, g.current_admin["id"], "delete_page", target=existing["url"], ip=ip)
        conn.commit()
    return jsonify({"status": "ok"})


@app.post("/api/admin/pages")
@require_admin
def admin_page_add():
    ip = _client_ip()
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if ".onion" not in url:
        return jsonify({"error": "url must be a .onion address"}), 400
    title = data.get("title") or "Pending scan..."
    description = data.get("description") or "Added by admin, pending crawl"
    category = data.get("category") if data.get("category") in CATEGORIES else "other"
    lang = data.get("lang") or "other"

    with get_db() as conn:
        if conn.execute("SELECT 1 FROM pages WHERE url = ?", (url,)).fetchone():
            return jsonify({"error": "already indexed"}), 409
        conn.execute(
            "INSERT INTO pages (url, title, description, category, lang, is_alive, fail_count) "
            "VALUES (?, ?, ?, ?, ?, 1, 0)",
            (url, title, description, category, lang),
        )
        log_audit(conn, g.current_admin["id"], "add_page", target=url, ip=ip)
        conn.commit()
    return jsonify({"status": "ok"})


@app.get("/api/admin/removals")
@require_admin
def admin_removals():
    status = request.args.get("status")
    page, per_page = _page_args()
    where = []
    params = []
    if status in ("pending", "reviewed", "removed", "rejected"):
        where.append("r.status = ?")
        params.append(status)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * per_page
    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM removal_requests r{where_sql}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT r.*, a.username AS reviewer FROM removal_requests r "
            f"LEFT JOIN admins a ON a.id = r.reviewed_by{where_sql} "
            f"ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
    total_pages = (total + per_page - 1) // per_page
    return jsonify({
        "removals": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "total_pages": total_pages,
    })


@app.put("/api/admin/removals/<int:removal_id>")
@require_admin
def admin_removal_review(removal_id):
    ip = _client_ip()
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    notes = data.get("notes") or ""
    if action not in ("approve", "reject"):
        return jsonify({"error": "action must be approve|reject"}), 400

    with get_db() as conn:
        removal = conn.execute(
            "SELECT id, url FROM removal_requests WHERE id = ?", (removal_id,)
        ).fetchone()
        if removal is None:
            return jsonify({"error": "not found"}), 404
        url = removal["url"]
        if action == "approve":
            conn.execute("DELETE FROM pages WHERE url = ?", (url,))
            new_status = "removed"
            audit_action = "approve_removal"
            note_msg = f"Removal approved for: {url}"
        else:
            new_status = "rejected"
            audit_action = "reject_removal"
            note_msg = f"Removal rejected for: {url}"
        conn.execute(
            "UPDATE removal_requests SET status = ?, reviewed_at = CURRENT_TIMESTAMP, "
            "reviewed_by = ?, notes = ? WHERE id = ?",
            (new_status, g.current_admin["id"], notes, removal_id),
        )
        conn.execute(
            "INSERT INTO notifications (type, message) VALUES ('removal_request', ?)",
            (note_msg,),
        )
        log_audit(conn, g.current_admin["id"], audit_action, target=url, ip=ip)
        conn.commit()
    return jsonify({"status": "ok"})


@app.get("/api/admin/users")
@require_superadmin
def admin_users():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at, last_login, is_active "
            "FROM admins ORDER BY id"
        ).fetchall()
    return jsonify({"users": [dict(r) for r in rows]})


@app.post("/api/admin/users")
@require_superadmin
def admin_user_create():
    ip = _client_ip()
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") if data.get("role") in ("admin", "superadmin") else "admin"
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM admins WHERE username = ?", (username,)).fetchone():
            return jsonify({"error": "username already exists"}), 409
        conn.execute(
            "INSERT INTO admins (username, password_hash, role) VALUES (?, ?, ?)",
            (username, password_hash, role),
        )
        log_audit(conn, g.current_admin["id"], "create_user", target=username, ip=ip)
        conn.commit()
    return jsonify({"status": "ok"})


@app.put("/api/admin/users/<int:user_id>/deactivate")
@require_superadmin
def admin_user_deactivate(user_id):
    ip = _client_ip()
    if user_id == g.current_admin["id"]:
        return jsonify({"error": "cannot deactivate yourself"}), 400
    with get_db() as conn:
        target = conn.execute(
            "SELECT username FROM admins WHERE id = ?", (user_id,)
        ).fetchone()
        if target is None:
            return jsonify({"error": "not found"}), 404
        conn.execute("UPDATE admins SET is_active = 0 WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (user_id,))
        log_audit(conn, g.current_admin["id"], "deactivate_user", target=target["username"], ip=ip)
        conn.commit()
    return jsonify({"status": "ok"})


@app.get("/api/admin/notifications")
@require_admin
def admin_notifications():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return jsonify({"notifications": [dict(r) for r in rows]})


@app.post("/api/admin/notifications/read-all")
@require_admin
def admin_notifications_read_all():
    with get_db() as conn:
        conn.execute("UPDATE notifications SET is_read = 1")
        conn.commit()
    return jsonify({"status": "ok"})


@app.get("/api/admin/audit")
@require_admin
def admin_audit():
    admin_id = request.args.get("admin_id")
    action = request.args.get("action")
    page, per_page = _page_args()
    where = []
    params = []
    if admin_id:
        try:
            where.append("l.admin_id = ?")
            params.append(int(admin_id))
        except (ValueError, TypeError):
            return jsonify({"error": "invalid admin_id"}), 400
    if action:
        where.append("l.action = ?")
        params.append(action)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * per_page
    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM admin_audit_log l{where_sql}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT l.id, l.action, l.target, l.details, l.ip_hash, l.created_at, "
            f"a.username FROM admin_audit_log l "
            f"LEFT JOIN admins a ON a.id = l.admin_id{where_sql} "
            f"ORDER BY l.created_at DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
    total_pages = (total + per_page - 1) // per_page
    return jsonify({
        "audit": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "total_pages": total_pages,
    })


@app.get("/api/admin/export/audit")
@require_superadmin
def admin_audit_export():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT l.created_at, a.username, l.action, l.target, l.ip_hash "
            "FROM admin_audit_log l LEFT JOIN admins a ON a.id = l.admin_id "
            "ORDER BY l.created_at DESC"
        ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "username", "action", "target", "ip_hash"])
    for r in rows:
        writer.writerow([r["created_at"], r["username"], r["action"], r["target"], r["ip_hash"]])
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=audit_log.csv"
    return resp


@app.post("/api/removal-request")
def removal_request():
    """Public link-removal request (no auth). Backs the /terms removal form."""
    if not _admin_rate_limit("removal", 3600, 3):
        return jsonify({"error": "Too many requests. Try again later."}), 429
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    reason = data.get("reason")
    description = data.get("description") or ""
    if not url:
        return jsonify({"error": "url required"}), 400
    if reason not in ("dmca", "illegal", "other"):
        return jsonify({"error": "reason must be dmca|illegal|other"}), 400

    with get_db() as conn:
        conn.execute(
            "INSERT INTO removal_requests (url, reason, description) VALUES (?, ?, ?)",
            (url, reason, description),
        )
        conn.execute(
            "INSERT INTO notifications (type, message) VALUES ('removal_request', ?)",
            (f"New removal request for: {url}",),
        )
        conn.commit()
    logger.info("removal request received (reason=%s)", reason)
    return jsonify({"status": "submitted"})


def run() -> None:
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8000"))
    app.run(host=host, port=port)


if __name__ == "__main__":
    run()
