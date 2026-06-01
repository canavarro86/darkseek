### ✅ Сделано
- [x] Концепция и архитектура системы
- [x] Выбор стека технологий (адаптирован под 1GB RAM)
- [x] Создан GitHub репозиторий: canavarro86/darkseek (приватный)
- [x] Создан VPS сервер на Vultr — IP: 95.179.142.200, Amsterdam, Debian 12, $5/mo
- [x] Защита ветки `main` через GitHub Ruleset (PR обязателен)
- [x] Создана ветка `dev` для разработки
- [x] Структура проекта — все папки и файлы созданы (17 файлов)
- [x] Коммит в `dev`, PR → merge в `main`
- [x] Схема БД (db/schema.sql) с FTS5 и триггерами
- [x] Python venv настроен, все зависимости установлены
- [x] Claude Code установлен и подключён (рабочий ПК Windows + домашний Debian)
- [x] CLAUDE.md создан — инструкции для Claude Code
- [x] .gitignore настроен (.env защищён от коммита)
- [x] DARKSEEK_PROJECT.md и INSTRUCTIONS.md добавлены в проект
- [x] Сервер настроен: пользователь darkseek, Docker 29.5.2, /opt/darkseek
- [x] Весь код написан Claude Code: docker-compose.yml, api, crawler, frontend, nginx
- [x] Все 4 контейнера запущены на сервере (tor, api, nginx, crawler)
- [x] Сайт доступен по http://95.179.142.200 ✅
- [x] .onion адрес зафиксирован: 37mj2uc7sls76pah7op7xeq7nrskfpircrvycpceyifvwftrxiydubyd.onion ✅
- [x] Ключи Tor Hidden Service сохранены в /opt/onion_hidden_keys — адрес постоянный ✅
- [x] Сайт доступен через Tor Browser, переходы по ссылкам работают ✅
- [x] БД сохранена на физическом диске: /opt/darkseek_db/darkseek.db ✅
- [x] API /health, /stats, /metrics, /api/search, /api/submit работают ✅
- [x] Поиск работает: фронтенд рендерит результаты с датами ✅
- [x] БД: 1165 страниц проиндексировано (растёт) ✅

### ✅ Hardening сервера
- [x] SSH по ключу, root отключён, порт 2020
- [x] UFW firewall: 2020, 80, 443
- [x] fail2ban: 2 IP уже забанено
- [x] unattended-upgrades настроен
- [x] MOTD с ASCII логотипом DarkSeek

### ✅ Production release v1.0
- [x] SQLite WAL mode + PRAGMA оптимизации
- [x] Индексы: last_seen, category, is_alive
- [x] nginx: security headers, rate limit, 1 worker
- [x] Docker mem_limits: tor=120m, api=180m, nginx=32m, crawler=256m
- [x] Logging: json-file driver, max-size 10m, max-file 3
- [x] FTS5 injection защита, CORS, request_id
- [x] Frontend: debounce, retry button, keyboard shortcuts

### ✅ Crawler v1.0
- [x] socks5h://, verify_tor(), расписание 00:00 UTC
- [x] Dead site handling, revive_check(), content_hash
- [x] Пагинация форумов, freshness ranking

### ✅ Инфраструктура
- [x] GitHub Actions CI/CD — автодеплой при merge в main ✅
- [x] Бэкап БД: cron 23:00 UTC, хранение 7 дней ✅
- [x] Мониторинг: UptimeRobot → email canavarro@yandex.ru ✅
- [x] Timezone сервера: UTC ✅

### ✅ Release v1.1
- [x] Claude API подключён в ai_describe.py (claude-haiku-4-5, бюджет $5/мес) ✅
- [x] Краулер: 5 воркеров, delay 1.5s, per-domain throttle 10s ✅
- [x] Русский поиск: PyStemmer snowball, OR-fallback (форум → найдено) ✅
- [x] Дедупликация по content_hash ✅
- [x] CI/CD: health + search + stats проверки после деплоя ✅

### ✅ Release v1.2 — Hardening & Resilience
- [x] AI enrichment graceful fallback: HeuristicEnricher + circuit breaker (5 failures → 15min cooldown) ✅
- [x] enrichment_method column: ai / heuristic / pending ✅
- [x] Per-host cap: 200 страниц с домена за прогон ✅
- [x] Depth limit: max 5 уровней от seed ✅
- [x] Global run ceiling: 10k страниц за прогон ✅
- [x] Crawl-trap detector: numeric pagination throttle ✅
- [x] Dead-onion negative cache: таблица dead_onions, exponential backoff, revive после 7 дней ✅
- [x] Tor SocksTimeout 30 / CircuitStreamTimeout 30 ✅
- [x] Tor mem_limit: 120m → 180m + mem_reservation 150m ✅
- [x] Crawler mem_limit: 256m → 384m (OOM fix) ✅
- [x] Content dedup: UNIQUE index на content_hash + upsert last_seen ✅
- [x] scripts/dedupe.py: удалено 853 дубля ✅
- [x] scripts/normalize_lang.py: нормализовано 2452 строк (en-US→en, ru-RU→ru и т.д.) ✅
- [x] scripts/reprocess_ai.py: выборочный бэкфилл с бюджетным контролем (tiers 1-4) ✅
- [x] langdetect добавлен в requirements.crawler.txt ✅
- [x] Dockerfile.crawler: COPY scripts/ добавлен ✅
- [x] nginx: Slowloris защита (client_header_timeout, client_body_timeout) ✅
- [x] nginx: limit_conn 10 per IP ✅
- [x] nginx: /metrics закрыт извне (только Docker сеть) ✅
- [x] nginx: CSP обновлён — разрешён cdnjs.cloudflare.com для qrcodejs/spark-md5 ✅
- [x] БД: ~39.5k страниц (выросла с 1165 за ночь краулинга) ✅

### ✅ Release v1.3 — Search Quality
- [x] Query parser: "phrase", AND/OR, -exclude, injection protection
- [x] Stemmer: Snowball en/ru query-time (searching=search, магазины=магазин)
- [x] Synonym dictionary: ~50 darknet bilingual groups (btc=bitcoin=криптовалюта)
- [x] Composite scoring: BM25*0.6 + freshness*0.4 * alive_boost (фикс always-0 бага)
- [x] Frontend: подсветка совпадений в результатах (<mark> тег)

### ✅ Frontend
- [x] Donate страница: 6 кошельков (BTC/ETH/USDT/LTC/DOGE/SOL), QR коды, COPY кнопки ✅
- [x] FAQ/Commands страница: таблица команд, кликабельные строки ✅
- [x] Instant answers: qr, base64, hash, sha1, md5, passphrase, ip, shorten ✅
- [x] Навигация: + ADD ONION // COMMANDS ♥ SUPPORT DARKSEEK ✅
- [x] /api/ip и /api/shorten эндпоинты в api/main.py ✅

---

### 🎯 Глобальные задачи (приоритет)

#### 1. Наполнение индекса (цель: 50k+ страниц)
- [ ] Собрать базу .onion URL из открытых каталогов (ahmia, torch, Daniel's list, GitHub списки)
- [ ] Bulk insert URL в БД (только url, без краулинга) — дать краулеру точки входа
- [ ] Цель: 39.5k страниц → 50k страниц

#### 2. Качество поиска (следующая задача)
- [ ] PageRank по .onion ссылкам (поле score уже есть в схеме)
- [ ] Улучшить релевантность: FTS5 BM25 ранжирование
- [ ] Синонимы и семантический поиск (exchange=обменник=swap)
- [ ] Улучшить классификацию категорий (сейчас много "other")
- [ ] Фильтр BBC и других clearnet доменов из индекса

#### 3. Домен + HTTPS
- [ ] Купить darkseek.com
- [ ] Let's Encrypt (certbot)
- [ ] Редирект HTTP → HTTPS
- [ ] После HTTPS: hash/sha1 команды заработают (Web Crypto API требует secure context)

#### 4. Frontend fixes (отложено)
- [ ] calc команда: починить whitelist regex (invalid expression на calc 2+2)

#### 5. Монетизация
- [ ] Donations: XMR/BTC адрес на главной странице
- [ ] Promoted listings: платное размещение в топе категории, $10-50/мес, оплата XMR/BTC
- [ ] API access: free tier 100 req/day, платный $20/мес безлимит (таргет: OSINT разработчики)
- [ ] Fast-track indexing: приоритетная индексация за $5 XMR

#### 6. Масштабирование (когда вырастем)
- [ ] Переезд на сервер 4GB RAM (Hetzner CX22, €4/mo)
- [ ] Публичный API для разработчиков
- [ ] Репутация: Reddit r/onions, форумы даркнета

