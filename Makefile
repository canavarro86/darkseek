.PHONY: up down build logs ps init-db crawl api shell-api shell-crawler

up:
	docker-compose up -d

down:
	docker-compose down

build:
	docker-compose build --no-cache

logs:
	docker-compose logs -f

ps:
	docker-compose ps

# Initialize or reset the DB schema (run inside the api container)
init-db:
	docker-compose exec api sqlite3 $$DATABASE_PATH < db/schema.sql

# Trigger a one-off crawl run (the crawler service runs continuously on its own)
crawl:
	docker-compose exec crawler python -m crawler.spider

shell-api:
	docker-compose exec api bash

shell-crawler:
	docker-compose exec crawler bash

# Local dev — run API without Docker (needs .env and dependencies installed)
dev-api:
	python -m api.main
