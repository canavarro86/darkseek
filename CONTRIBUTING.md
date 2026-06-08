# Contributing to DarkSeek

Thank you for your interest in contributing.

## How to contribute

1. Fork the repository
2. Create a branch: `git checkout -b feature/your-feature`
3. Make your changes (see dev setup below)
4. Open a Pull Request against `dev` — not `main`

`main` is protected and only updated via PRs from `dev`.

## Development setup

```bash
git clone https://github.com/canavarro86/darkseek.git
cd darkseek
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env
docker compose up -d
```

## Code style

- Python: PEP 8, English comments only
- No external frontend frameworks (vanilla HTML/CSS/JS only)
- Verify the API still responds (`/health`, `/api/search`) after changes

## What we accept

- Bug fixes
- Performance improvements
- New .onion seed URLs (add to `crawler/spider.py` SEED_URLS)
- Documentation improvements
- Security issues — please open a private GitHub issue

## What we do NOT accept

- Content that violates the project's content policy (see `about.html`)
- Changes that increase memory footprint above 700MB total
- Dependencies that require non-Docker installation

## Running the crawler locally

```bash
make crawl
```
