### ✅ Сделано
- [x] Концепция и архитектура системы
- [x] Выбор стека технологий (адаптирован под 1GB RAM)
- [x] Создан GitHub репозиторий: canavarro86/darkseek (публичный)
- [x] Создан VPS сервер — IP: 95.179.142.200
- [x] Защита ветки `main` через GitHub Ruleset (PR обязателен)
- [x] Создана ветка `dev` для разработки
- [x] Структура проекта — все папки и файлы созданы
- [x] Коммит в `dev`, PR → merge в `main`
- [x] Схема БД (db/schema.sql) с FTS5 и триггерами
- [x] .gitignore настроен (.env защищён от коммита)
- [x] DARKSEEK_PROJECT.md и INSTRUCTIONS.md добавлены в проект
- [x] Сервер настроен
- [x] Весь код написан Artem Gatchenko с частичным (30%) применением Claude Code
- [x] Все 4 контейнера запущены на сервере (tor, api, nginx, crawler)
- [x] Сайт доступен по http://95.179.142.200 ✅
- [x] .onion адрес зафиксирован: 37mj2uc7sls76pah7op7xeq7nrskfpircrvycpceyifvwftrxiydubyd.onion ✅
- [x] Ключи Tor Hidden Service сохранены адрес постоянный ✅
- [x] Сайт доступен через Tor Browser, переходы по ссылкам работают ✅
- [x] БД сохранена на физическом диске: darkseek_db/darkseek.db ✅
- [x] API /health, /stats, /metrics, /api/search, /api/submit, /api/search-stats работают ✅
- [x] Поиск работает: фронтенд рендерит результаты с датами ✅
- [x] БД: 74,550 страниц проиндексировано (растёт) ✅
- [x] Три Tor Hidden Service настроены:
  - Старый поисковик (legacy): 37mj2uc7sls76pah7op7xeq7nrskfpircrvycpceyifvwftrxiydubyd.onion
  - Новый vanity поисковик: darkszcni4tmrlpociezfczxh3b3zemlmcfvu7iiv7n242u2eaff5wyd.onion
  - Админ панель (приватный): adminov3whi5tnbdh2nh4436ozaysyotcsxkjtfn4koyygusmcaphrad.onion
- [x] Nginx роутинг по Host заголовку — 3 server блока, default_server возвращает 444, прямой доступ по IP заблокирован
- [x] Админ панель (frontend/admin.html) — терминальный SPA с вкладками: dashboard, pages, removals, users, audit
- [x] Admin API — JWT авторизация (httpOnly cookie), bcrypt пароли, все эндпоинты /api/admin/*
- [x] Новые таблицы БД: admins, admin_sessions, removal_requests, admin_audit_log, notifications
- [x] Superadmin создаётся при первом запуске, пароль выводится в лог контейнера один раз
- [x] Legal страницы: frontend/terms.html (с формой удаления), frontend/disclaimer.html
- [x] Ссылки в footer на index.html: Terms of Service | Disclaimer
- [x] Публичный эндпоинт POST /api/removal-request — rate limit, пишет в removal_requests + notifications
- [x] БД: 112,119 страниц проиндексировано (75,805 живых, 36,314 мёртвых)

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

### 📋 Очередь
- [ ] Запустить scripts/normalize_lang.py — схлопнуть en-GB, en-US в en
- [ ] Запустить scripts/reprocess_ai.py — переобогатить 41,924 страниц с lang='unknown'
- [ ] Протестировать все вкладки админки в продакшене (поиск страниц, workflow удалений, экспорт audit)
- [ ] Сменить пароль superadmin после первого входа

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
- [ ] Let's Encrypt (certbot)
- [ ] Редирект HTTP → HTTPS

#### 4. Масштабирование (когда вырастем)
- [ ] Переезд на сервер 4GB RAM (Hetzner CX22, €4/mo)
- [ ] Публичный API для разработчиков