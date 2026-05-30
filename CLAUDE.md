# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Dark web search engine. Crawls `.onion` sites via TOR proxy, uses Anthropic API to generate page descriptions, stores results in SQLite with FTS5, serves search via Python API.

## Architecture

Three Python services + static frontend:

```
frontend/index.html  →  api/ (HTTP)  →  db/darkseek.db (SQLite)
                                             ↑
                         crawler/  →  TOR proxy (socks5://tor:9050)
                         crawler/ai_describe.py  →  Anthropic API
```

- **`api/`** — HTTP server (search, results). Files: `main.py` (routing), `search.py` (FTS5 queries), `models.py` (DB layer)
- **`crawler/`** — Spider logic. Files: `spider.py` (TOR crawl), `parser.py` (HTML extract), `ai_describe.py` (Anthropic descriptions)
- **`db/schema.sql`** — SQLite schema: `pages` table + FTS5 virtual table `pages_fts` with auto-sync triggers
- **`frontend/index.html`** — Static HTML, no framework

## Database

SQLite at path from `DATABASE_PATH` env var. Schema in `db/schema.sql`.

Key table: `pages` — stores URL, title, description, category (forum/market/news/wiki/service/other), lang, score, is_alive flag.

FTS5 index on `title` + `description` kept in sync via INSERT/DELETE/UPDATE triggers on `pages`.

## Environment

Copy `.env.example` → `.env`. Required vars:

```
ANTHROPIC_API_KEY=   # Anthropic API key
DATABASE_PATH=       # e.g. /app/db/darkseek.db
TOR_PROXY=           # e.g. socks5://tor:9050
CRAWLER_DELAY=       # seconds between requests
CRAWLER_WORKERS=     # parallel crawler workers
```

## Commands

No Makefile or docker-compose defined yet. As services get implemented:

```bash
# Run API
python -m api.main

# Run crawler
python -m crawler.spider

# Init/reset DB
sqlite3 $DATABASE_PATH < db/schema.sql
```

No `requirements.txt` yet — define dependencies as services are implemented.
