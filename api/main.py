import logging
import os
import re
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


def _sanitize_query(raw: str) -> str:
    """Strip NUL/control characters and clamp length on user search input."""
    cleaned = _CONTROL_CHARS.sub(" ", raw)
    return cleaned.strip()[:MAX_QUERY_LEN]


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
        "by_category": {r["category"]: r["cnt"] for r in by_category},
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


def run() -> None:
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8000"))
    app.run(host=host, port=port)


if __name__ == "__main__":
    run()
