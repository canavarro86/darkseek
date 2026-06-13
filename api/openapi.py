"""Hand-maintained OpenAPI 3.0 spec + a tiny Swagger-UI page (FIX 5).

DarkSeek's API is Flask, not FastAPI, so there is no auto-generated schema. This
module is the single place the API surface is described; api/main.py serves
``OPENAPI_SPEC`` at /openapi.json and ``SWAGGER_UI_HTML`` at /docs.

When you add or change an endpoint in api/main.py, update the matching entry
here so /docs stays accurate.
"""

API_VERSION = "3.0.0"

OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "DarkSeek API",
        "version": API_VERSION,
        "description": (
            "Search API for DarkSeek, a dark-web (.onion) search engine. "
            "No accounts, no cookies, no IP logging. Community voting/reporting "
            "is gated by client-side Proof-of-Work. Write endpoints are rate "
            "limited (10 req / 60s per endpoint)."
        ),
    },
    "servers": [{"url": "/", "description": "Same-origin (served behind nginx)"}],
    "tags": [
        {"name": "Search", "description": "Full-text search over the index"},
        {"name": "Community", "description": "PoW-gated voting and reporting"},
        {"name": "Submit", "description": "Submit .onion URLs for indexing"},
        {"name": "Tools", "description": "Instant-answer helper endpoints"},
        {"name": "System", "description": "Health, stats and metrics"},
    ],
    "paths": {
        "/health": {
            "get": {
                "tags": ["System"],
                "summary": "Liveness probe",
                "description": "Returns {\"status\": \"ok\"} when the API is up.",
                "responses": {"200": {"description": "Service is up"}},
            }
        },
        "/stats": {
            "get": {
                "tags": ["System"],
                "summary": "Index + visitor stats",
                "description": (
                    "Live page count, per-category breakdown, and daily-unique "
                    "visitor counts (today/yesterday). Visitor counts derive from "
                    "daily-salted IP hashes; no raw IP is stored."
                ),
                "responses": {
                    "200": {
                        "description": "Aggregate stats",
                        "content": {
                            "application/json": {
                                "example": {
                                    "total_pages": 29230,
                                    "pages_indexed": 29230,
                                    "by_category": {"news": 1200, "forum": 800},
                                    "visitors_today": 142,
                                    "visitors_yesterday": 98,
                                    "status": "ok",
                                }
                            }
                        },
                    }
                },
            }
        },
        "/metrics": {
            "get": {
                "tags": ["System"],
                "summary": "Crawler + DB footprint metrics",
                "responses": {"200": {"description": "Operational metrics"}},
            }
        },
        "/api/search": {
            "get": {
                "tags": ["Search"],
                "summary": "Search the index",
                "description": (
                    "FTS5 search with composite freshness ranking. Illegal (CSAM) "
                    "queries return an empty, blocked result set. Each request is "
                    "counted as a daily-unique visitor (privacy-safe hash)."
                ),
                "parameters": [
                    {"name": "q", "in": "query", "required": True,
                     "schema": {"type": "string", "maxLength": 200},
                     "description": "Search query (supports quotes, -exclude, OR)."},
                    {"name": "page", "in": "query", "required": False,
                     "schema": {"type": "integer", "default": 1, "minimum": 1}},
                    {"name": "category", "in": "query", "required": False,
                     "schema": {"type": "string",
                                "enum": ["forum", "market", "news", "wiki", "service", "other"]}},
                    {"name": "safe_mode", "in": "query", "required": False,
                     "schema": {"type": "string", "default": "true"},
                     "description": "Set to 'false' to disable content filtering."},
                ],
                "responses": {
                    "200": {"description": "Search results (page of 10)"},
                    "400": {"description": "Missing q or invalid category"},
                    "500": {"description": "Internal error"},
                },
            }
        },
        "/api/search-stats": {
            "get": {
                "tags": ["Search"],
                "summary": "Aggregate search analytics",
                "description": "Popular and zero-result queries. Cached 5 minutes.",
                "responses": {"200": {"description": "Query aggregates"}},
            }
        },
        "/api/challenge": {
            "get": {
                "tags": ["Community"],
                "summary": "Mint a Proof-of-Work challenge",
                "parameters": [
                    {"name": "page_id", "in": "query", "required": True,
                     "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {"description": "Challenge + difficulty (5-minute TTL)"},
                    "400": {"description": "page_id required"},
                    "404": {"description": "page not found"},
                },
            }
        },
        "/api/vote": {
            "post": {
                "tags": ["Community"],
                "summary": "Cast a fresh/rotten vote (PoW-gated)",
                "description": "Rate limited: 10 requests / 60s per client.",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"example": {
                        "page_id": 1, "vote_type": "fresh",
                        "challenge": "<hex>", "nonce": "<solved>",
                    }}},
                },
                "responses": {
                    "200": {"description": "Vote recorded; new onion_score"},
                    "400": {"description": "Bad vote_type or invalid PoW"},
                    "409": {"description": "Proof already used"},
                    "429": {"description": "Rate limit exceeded (Retry-After header)"},
                },
            }
        },
        "/api/report": {
            "post": {
                "tags": ["Community"],
                "summary": "Report a page (PoW-gated)",
                "description": "Reasons: scam|offline|illegal|spam. Rate limited 10/60s.",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"example": {
                        "page_id": 1, "reason": "scam",
                        "challenge": "<hex>", "nonce": "<solved>",
                    }}},
                },
                "responses": {
                    "200": {"description": "Report recorded"},
                    "400": {"description": "Bad reason or invalid PoW"},
                    "429": {"description": "Rate limit exceeded (Retry-After header)"},
                },
            }
        },
        "/api/submit": {
            "post": {
                "tags": ["Submit"],
                "summary": "Submit a single .onion URL",
                "description": "Max 5 submissions/hour per IP.",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"example": {"url": "http://<56-char>.onion"}}},
                },
                "responses": {
                    "200": {"description": "queued / exists"},
                    "400": {"description": "Invalid .onion URL"},
                    "429": {"description": "Submission rate limit exceeded"},
                },
            }
        },
        "/api/submit/bulk": {
            "post": {
                "tags": ["Submit"],
                "summary": "Submit up to 50 .onion URLs",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"example": {"urls": ["http://a.onion", "http://b.onion"]}}},
                },
                "responses": {
                    "200": {"description": "Per-URL status list"},
                    "400": {"description": "Bad body or too many URLs"},
                    "429": {"description": "Submission rate limit exceeded"},
                },
            }
        },
        "/api/lookup": {
            "get": {
                "tags": ["Submit"],
                "summary": "Look up a single .onion URL in the index",
                "parameters": [
                    {"name": "url", "in": "query", "required": True,
                     "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {"description": "Indexed card or {found:false}"},
                    "400": {"description": "url must contain .onion"},
                },
            }
        },
        "/api/ip": {
            "get": {
                "tags": ["Tools"],
                "summary": "Tor exit-node IP of the caller",
                "responses": {"200": {"description": "Exit node IP (uncacheable)"}},
            }
        },
        "/api/shorten": {
            "get": {
                "tags": ["Tools"],
                "summary": "Shorten a URL via TinyURL (cached)",
                "description": "Rate limited 10/60s. Results cached in url_cache.",
                "parameters": [
                    {"name": "url", "in": "query", "required": True,
                     "schema": {"type": "string", "maxLength": 2048}},
                ],
                "responses": {
                    "200": {"description": "Short URL"},
                    "400": {"description": "Invalid url"},
                    "429": {"description": "Rate limit exceeded (Retry-After header)"},
                    "502": {"description": "Shortener unavailable"},
                },
            }
        },
    },
}


# Minimal Swagger UI shell. UI assets come from a CDN; the spec is local.
SWAGGER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>DarkSeek API — Docs</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = () => {
      window.ui = SwaggerUIBundle({
        url: '/openapi.json',
        dom_id: '#swagger-ui',
        deepLinking: true,
      });
    };
  </script>
</body>
</html>
"""
