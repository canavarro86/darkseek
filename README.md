# DarkSeek

> Independent darknet search engine. No ads. No trackers. No censorship.

**Live:** [37mj2uc7sls76pah7op7xeq7nrskfpircrvycpceyifvwftrxiydubyd.onion](http://37mj2uc7sls76pah7op7xeq7nrskfpircrvycpceyifvwftrxiydubyd.onion)
**Clearnet:** [http://95.179.142.200](http://95.179.142.200)

---

## What is DarkSeek

DarkSeek is a full-text search engine for the Tor network. It crawls `.onion` sites, indexes their content using SQLite FTS5, and serves results through a fast API. Everything runs inside Docker on a single 1GB VPS — lean by design.

---

## Release v1.0.0

### Infrastructure
- VPS: Vultr Amsterdam, Debian 12, 1GB RAM, $5/mo
- All services run via Docker Compose with memory limits
- Persistent SQLite database at `/opt/darkseek_db/darkseek.db`
- Tor Hidden Service with permanent `.onion` address (keys backed up)
- GitHub Actions CI/CD — auto deploy on merge to `main`
- Branch protection: `main` requires PR approval
- Daily DB backup via cron, 7-day retention
- UptimeRobot monitoring with email alerts
- Server hardening: SSH key-only on port 2020, UFW firewall, fail2ban, unattended-upgrades

### Services (Docker)
| Container | Image | Memory limit | Purpose |
|-----------|-------|-------------|---------|
| `tor` | dperson/torproxy | 120MB | SOCKS5 proxy + Hidden Service |
| `api` | custom Python | 180MB | FastAPI search backend |
| `nginx` | nginx:alpine | 32MB | Reverse proxy + static files |
| `crawler` | custom Python | 256MB | Scrapy spider via Tor |

### Search
- SQLite FTS5 full-text search with BM25 ranking
- Russian language support via PyStemmer snowball stemmer
- OR-fallback for zero-result queries
- FTS5 injection protection
- API endpoints: `/api/search`, `/stats`, `/health`, `/metrics`
- Rate limiting: 10 requests/minute per IP
- Request deduplication by `content_hash`

### Crawler
- Scrapy spider over SOCKS5 Tor proxy
- 5 concurrent workers, 1.5s delay, 10s per-domain throttle
- Dead site handling with periodic revive checks
- Forum pagination support
- Freshness ranking (recently seen pages ranked higher)
- Scheduled run at 00:00 UTC daily

### AI Integration
- Claude Haiku (`claude-haiku-4-5`) for page description and categorization
- Budget cap: $5/month
- Output: `title`, `description`, `category`, `lang` per page
- Categories: `forum` | `market` | `news` | `wiki` | `service` | `other`

### Frontend
- Pure HTML/CSS/JS — zero frameworks, zero dependencies
- Dark terminal aesthetic: `#0a0a0a` background, `#00ff41` green, monospace font
- Debounced search input, retry button, keyboard shortcuts
- Category filter badges
- Paginated results with dates
- Submit page for new `.onion` URLs

### Database
- SQLite WAL mode + PRAGMA optimizations
- FTS5 virtual table with auto-sync triggers (INSERT / UPDATE / DELETE)
- Indexes on `last_seen`, `category`, `is_alive`
- `score` field for future PageRank implementation

---

## Release v1.1.0

- Claude API connected (`ai_describe.py`) with $5/mo budget guard
- Crawler upgraded: 5 workers, per-domain throttling
- Russian search with PyStemmer snowball + OR-fallback
- Content deduplication by `content_hash`
- CI/CD health + search + stats verification after every deploy

---

## Release v1.2.0 — Hardening & Resilience

Unattended-operation hardening after the corpus reached ~39.5k pages. Each
external dependency can now fail without halting or corrupting ingestion.

### AI enrichment — graceful degradation
- Two strategies behind one interface: `AIEnricher` (Claude) and
  `HeuristicEnricher` (local, zero-network).
- **Circuit breaker:** after 5 consecutive API failures (credit exhaustion / 429
  / 5xx / timeout) the API is skipped for 15 min and every page is enriched
  locally; one probe re-closes the circuit on recovery.
- New `enrichment_method` column (`ai` | `heuristic` | `pending`) replaces
  NULL-as-state and drives the backfill job. Ingestion never writes NULL
  `category`/`lang` again.
- Single `normalize_lang()` source of truth (ISO-639-1, lowercase) used by both
  enrichers and the migration script.

### Crawler resilience
- **Per-host fairness cap:** 200 pages/host/run (BBC had reached 1,603).
- **Crawl-trap protection:** depth limit 5, global ceiling 10,000 pages/run, and
  a numeric-pagination trap detector that de-prioritizes runaway hosts.
- **Dead-onion negative cache** (`dead_onions` table): resolve failures are
  cached with exponential back-off (7→14→28… days, capped 90), saving ~30s per
  dead onion per cycle. Revived and cleared on recovery.

### Data hygiene
- Content dedup enforced by a partial `UNIQUE` index on `content_hash` plus an
  app-level upsert that bumps `last_seen` instead of inserting a copy.
- One-shot scripts: `scripts/dedupe.py` (collapse 1,215 historical dupes),
  `scripts/normalize_lang.py` (canonicalize lang tags), `scripts/reprocess_ai.py`
  (upgrade `heuristic` rows to `ai` when credits return — resumable, rate-limited).

### Memory envelope
- tor `mem_limit` 120m→180m (+`mem_reservation` 150m); crawler 256m.
  tor 180 + api 180 + nginx 32 + crawler 256 = **648m / 1024m**.

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

## Project Structure

```
darkseek/
├── api/
│   └── main.py          # FastAPI app — search, stats, health, ip, shorten
├── crawler/
│   ├── spider.py        # Scrapy async spider over Tor SOCKS5
│   ├── parser.py        # BeautifulSoup HTML parser
│   └── ai_describe.py   # Claude Haiku categorization
├── db/
│   └── schema.sql       # SQLite schema with FTS5 and triggers
├── frontend/
│   ├── index.html       # Main search page
│   ├── submit.html      # Submit new .onion URL
│   ├── donate.html      # Donation page with crypto wallets
│   └── faq.html         # Commands reference
├── docker-compose.yml
├── nginx.conf
├── CLAUDE.md            # Instructions for Claude Code
└── README.md
```

---

## API

```
GET /api/search?q=<query>&limit=20&offset=0
GET /api/ip
GET /api/shorten?url=<url>
GET /stats
GET /health
GET /metrics
```

---

## Roadmap

- [ ] Collect 50k+ indexed pages (seed from ahmia, torch, Daniel's list)
- [ ] PageRank scoring by `.onion` link graph
- [ ] Improve category classification (reduce "other")
- [ ] Domain + HTTPS (darkseek.com + Let's Encrypt)
- [ ] Donations: XMR/BTC on front page
- [ ] Promoted listings: paid top placement per category
- [ ] Public API: free tier 100 req/day, paid $20/mo unlimited
- [ ] Scale to 4GB RAM server (Hetzner CX22) when needed

---

## Support

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

## License

Private repository. All rights reserved.