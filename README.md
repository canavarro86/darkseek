# DarkSeek 🧅

> Privacy-first full-text search engine for the Tor network.
> No ads. No tracking. No censorship. Open source.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue.svg)](docker-compose.yml)

**Live (Tor):** `37mj2uc7sls76pah7op7xeq7nrskfpircrvycpceyifvwftrxiydubyd.onion`
**Live (Clearnet):** `http://95.179.142.200`

---

## What is DarkSeek

DarkSeek is a full-text search engine for the Tor network. It crawls `.onion`
sites over Tor, categorizes each page with Claude, and indexes the results in
SQLite FTS5 so they can be searched through a fast, no-logs API. Everything runs
inside Docker on a single 1GB VPS — lean by design, self-hostable by anyone.

---

## Features

- Full-text search via SQLite FTS5 with BM25 ranking
- Crawler over Tor SOCKS5 (async `httpx`)
- AI page categorization via Claude Haiku (`claude-haiku-4-5`)
- Russian language stemming (PyStemmer snowball) with OR-fallback
- Community voting with Proof-of-Work gate (no accounts, no IPs)
- Abuse reporting (scam/offline/illegal/spam) with PoW gate
- Safe mode toggle with one-time adult content warning
- Onion score (1–5 rating derived from votes)
- Content deduplication by `content_hash`
- Search query analytics (no IP, no user data stored)
- CSAM keyword blocklist (crawler + search)
- Instant answers: QR, base64, hash, passphrase, calc, ip, shorten
- Submit / bulk submit `.onion` URLs
- Dead site handling with exponential back-off
- Docker Compose, 1GB RAM VPS, ~670MB total memory footprint
- MIT licensed, self-hostable

---

## Architecture

```
Seed URLs (.onion)
    ↓
Async httpx crawler (over Tor SOCKS5 :9050)
    ↓ extract title, text, links
Parser + AI Describe (Claude Haiku)
    ↓ {title, description, category, lang}
SQLite FTS5 (WAL mode)
    ↓
Flask API  →  nginx  →  User
```

Everything runs in Docker Compose on a single 1GB VPS.

---

## Quick Start

```bash
git clone https://github.com/canavarro86/darkseek.git
cd darkseek
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY
docker compose up -d
# Open http://localhost
```

The crawler service runs continuously, cycling through its queue with a short
pause between cycles (a full re-crawl, including dead sites, runs weekly on
Sundays). To trigger a one-off crawl run manually:

```bash
make crawl
```

---

## Configuration

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key for AI categorization | required |
| `DATABASE_PATH` | Path to SQLite database | `/app/db/darkseek.db` |
| `TOR_PROXY` | Tor SOCKS5 proxy address | `socks5h://tor:9050` |
| `CRAWLER_DELAY` | Delay between requests (seconds) | `1.5` |
| `CRAWLER_WORKERS` | Concurrent crawler workers | `5` |
| `CORS_ORIGINS` | Allowed CORS origins (comma-separated) | empty (same-origin only) |

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/search?q=<query>` | Full-text search |
| GET | `/api/search-stats` | Aggregate search analytics |
| GET | `/api/challenge?page_id=<id>` | Mint PoW challenge for voting |
| POST | `/api/vote` | Cast vote (requires solved PoW) |
| POST | `/api/report` | Report a page (requires solved PoW) |
| POST | `/api/submit` | Submit a new `.onion` URL |
| POST | `/api/submit/bulk` | Bulk submit up to 50 URLs |
| GET | `/api/lookup?url=<url>` | Check if a URL is indexed |
| GET | `/api/ip` | Return caller's Tor exit node IP |
| GET | `/api/shorten?url=<url>` | Shorten a URL via TinyURL |
| GET | `/stats` | Index statistics |
| GET | `/metrics` | Crawler operational metrics (internal-only) |
| GET | `/health` | Health check |

All `/api/` endpoints are rate-limited by nginx (10 req/min per IP). `/metrics`
is restricted to the internal Docker network.

---

## Project Structure

```
darkseek/
├── api/
│   ├── main.py          # Flask app — all API endpoints
│   ├── models.py        # SQLite connection, migrations, analytics
│   ├── search.py        # FTS5 search logic, BM25 ranking
│   ├── scoring.py       # Onion score calculation
│   ├── stemmer.py       # Russian PyStemmer integration
│   ├── synonyms.py      # Search synonym expansion
│   └── search_parser.py # FTS5 query parser / sanitizer
├── crawler/
│   ├── spider.py        # Async httpx crawler over Tor SOCKS5
│   ├── parser.py        # BeautifulSoup HTML parser
│   ├── ai_describe.py   # Claude Haiku categorization
│   ├── models.py        # Crawler DB layer
│   └── dead_cache.py    # Dead onion negative cache
├── db/
│   └── schema.sql       # SQLite schema with FTS5 and triggers
├── frontend/
│   ├── index.html       # Main search page
│   ├── submit.html      # Submit .onion URLs
│   ├── about.html       # About page
│   ├── privacy.html     # Privacy policy
│   ├── donate.html      # Donation page
│   └── faq.html         # FAQ / instant commands reference
├── scripts/
│   ├── purge_illegal.py # One-time CSAM content purge
│   ├── dedupe.py        # Collapse duplicate content_hash rows
│   ├── normalize_lang.py# Canonicalize language tags
│   └── reprocess_ai.py  # Re-enrich heuristic rows with AI
├── tor/
│   └── torrc            # Tor daemon config + hidden service
├── docker-compose.yml
├── nginx.conf
├── Makefile
├── .env.example
└── .github/
    └── workflows/
        └── deploy.yml   # CI/CD: auto-deploy on merge to main
```

---

## Instant Answer Commands


Type directly in the search box:

| Command | Example | Result |
|---------|---------|--------|
| `qr <text>` | `qr darkseek.onion` | QR code image |
| `base64 encode <text>` | `base64 encode hello` | `aGVsbG8=` |
| `base64 decode <text>` | `base64 decode aGVsbG8=` | `hello` |
| `hash <text>` | `hash hello` | SHA-256 hex digest |
| `sha1 <text>` | `sha1 hello` | SHA-1 hex digest |
| `md5 <text>` | `md5 hello` | MD5 hex digest |
| `passphrase` | `passphrase` | 4-word random passphrase |
| `calc <expr>` | `calc 2+2*8` | `18` |
| `ip` | `ip` | Your Tor exit node IP |
| `shorten <url>` | `shorten https://example.com` | Shortened URL |

---

## Memory Footprint

```
tor      180 MB
api      180 MB
nginx     32 MB
crawler  280 MB (runs continuously)
─────────────────
Total   ~672 MB / 1024 MB
```

Tested on a $5/mo Vultr VPS (1 vCPU, 1GB RAM, Amsterdam).

---

## Roadmap

- [ ] Reach 50k+ indexed pages (seed from ahmia, Torch, Daniel's list)
- [ ] PageRank scoring from `.onion` link graph (`score` field ready in schema)
- [ ] Reduce "other" category — improve AI classification prompts
- [ ] HTTPS (darkseek.com + Let's Encrypt)
- [ ] Public API: free tier 100 req/day, paid $20/mo unlimited
- [ ] Promoted listings: paid top placement per category (XMR/BTC)
- [ ] Priority indexing: fast-track crawl for submitted URLs (for a fee)
- [ ] Scale to 4GB RAM server (Hetzner CX22) when needed

---

## Support / Donations

DarkSeek runs on donations. No investors. No ads.

| Coin | Address |
|------|---------|
| BTC | `1AakNM5jBAY7mds41WKs7tY2u1i5Zfkvow` |
| ETH (ERC20) | `0x69bcf463fc486666442102fc4e22d8603e4892e2` |
| USDT (TRC20) | `TZ27hcET4yWdQgeuQzRBDRsZi4f7HodzYp` |
| LTC | `LeiMrGa2rXRwToEwTygAZpwdvdcUbJgoEF` |
| DOGE | `DF59DkzYtP9QgwESLjco682bbzoqnHboEp` |
| SOL | `4akJrZ34ctcgopwhwqXEkVsyi57ZcfUm2sL1JS8oRzXo` |

---

## Privacy

DarkSeek does not log IP addresses, search queries, or any user data.
See [privacy.html](frontend/privacy.html) for the full policy.

---

## License

MIT — see [LICENSE](LICENSE).
