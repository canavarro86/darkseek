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

---

### 🎯 Глобальные задачи (приоритет)

#### 1. Наполнение индекса (цель: 50k+ страниц)
- [ ] Собрать базу .onion URL из открытых каталогов (ahmia, torch, Daniel's list, GitHub списки)
- [ ] Bulk insert URL в БД (только url, без краулинга) — дать краулеру точки входа
- [ ] Цель: 10k страниц → 50k страниц

#### 2. Качество поиска
- [ ] PageRank по .onion ссылкам (поле score уже есть в схеме)
- [ ] Улучшить классификацию категорий (сейчас много "other")

#### 3. Домен + HTTPS
- [ ] Купить darkseek.com
- [ ] Let's Encrypt (certbot)
- [ ] Редирект HTTP → HTTPS

#### 4. Монетизация
- [ ] Donations: XMR/BTC адрес на главной странице
- [ ] Promoted listings: платное размещение в топе категории, $10-50/мес, оплата XMR/BTC
- [ ] API access: free tier 100 req/day, платный $20/мес безлимит (таргет: OSINT разработчики)
- [ ] Fast-track indexing: приоритетная индексация за $5 XMR (submit уже есть, добавить приоритет)

#### 5. Масштабирование (когда вырастем)
- [ ] Переезд на сервер 4GB RAM (Hetzner CX22, €4/mo)
- [ ] Публичный API для разработчиков
- [ ] Репутация: Reddit r/onions, форумы даркнета