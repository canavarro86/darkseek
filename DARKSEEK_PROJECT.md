_Последнее обновление: 2026-08-20_

### ✅ Сделано
- [x] Концепция и архитектура системы
- [x] Выбор стека технологий (адаптирован под 1GB RAM)
- [x] Создан GitHub репозиторий: canavarro86/darkseek (публичный)
- [x] Защита ветки `main` через GitHub Ruleset (PR обязателен)
- [x] Создана ветка `dev` для разработки
- [x] Структура проекта — все папки и файлы созданы
- [x] Коммит в `dev`, PR → merge в `main`
- [x] Схема БД (db/schema.sql) с FTS5 и триггерами
- [x] .gitignore настроен (.env защищён от коммита)
- [x] DARKSEEK_PROJECT.md и INSTRUCTIONS.md добавлены в проект
- [x] Сервер настроен
- [x] Весь код написан canavarro86 с частичным (30%) применением Claude Code
- [x] Все 4 контейнера запущены на сервере (tor, api, nginx, crawler)
- [x] .onion адрес зафиксирован: 37mj2uc7sls76pah7op7xeq7nrskfpircrvycpceyifvwftrxiydubyd.onion ✅
- [x] Ключи Tor Hidden Service сохранены адрес постоянный ✅
- [x] Сайт доступен через Tor Browser, переходы по ссылкам работают ✅
- [x] БД сохранена на физическом диске: darkseek_db/darkseek.db ✅
- [x] API /health, /stats, /metrics, /api/search, /api/submit, /api/search-stats работают ✅
- [x] Поиск работает: фронтенд рендерит результаты с датами ✅
- [x] БД: 74,550 страниц проиндексировано (растёт) ✅

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

### ✅ Session 2026-08-20 — стабилизация продакшена
- [x] **Краулер**: 2 недели был Exited(137) (OOM — неограниченная загрузка тела ответа на "враждебных"/битых .onion страницах). Фикс: жёсткий кап 5MB на fetch через streaming (client.stream() с ранним abort'ом), memory watchdog ужесточён (250MB→200MB лимит, интервал проверки 15s→10s). На 2026-08-20 работает, здоров ✅
- [x] **Backup-контейнер**: был в бесконечном OOM restart-loop (`apk add` выполнялся в рантайме контейнера вместо build-time, упирался в mem_limit 20MB на каждом старте). Фикс: зависимости теперь запечены в Dockerfile.backup на этапе сборки. Подтверждено: реальные ночные бэкапы всё это время шли отдельным host-level cron скриптом (/opt/darkseek_backup/backup.sh), он никогда не был сломан. Сейчас стабилен, не рестартует ✅
- [x] **Race condition в миграциях**: api/models.py гонял DB-миграции на момент импорта модуля; 2 gunicorn-воркера + crawler-контейнер (отдельный процесс) могли гонять insert в schema_migrations одновременно → случайные крэши `UNIQUE constraint failed` при деплое (вызвало продакшен-аутаж 2026-08-20 — deploy.yml сносил старые контейнеры до подтверждения здоровья новых). Фикс: миграции теперь через BEGIN IMMEDIATE + re-check под write lock + INSERT OR IGNORE, безопасно при конкурентных процессах между контейнерами ✅
- [x] Новый maintenance: еженедельный FTS5 optimize + ANALYZE (scripts/maintenance.sh, по воскресеньям 04:00 UTC в backup-контейнере) ✅
- [x] Новый индекс на pages.indexed_at (админ-дашборд/листинг делал full scan на 441k+ строках) ✅

### ⏳ Known gaps (не сделано)
- [ ] systemd unit'ы для boot-time автостарта и периодического host watchdog подготовлены (deploy/darkseek.service, deploy/darkseek-watchdog.service/.timer, scripts/host_watchdog.sh), но НЕ установлены на сервере — если хост перезагрузится, docker compose stack сам не поднимется. Установка ручная, см. deploy/README.md
- [ ] deploy.yml риск не пофикшен: деплой-скрипт делает `docker compose down` до сборки/старта нового стека — при упавшем healthcheck прод полностью падает вместо того чтобы остаться на last-known-good контейнерах. Это вызвало сегодняшний аутаж-window. Требует фикса в будущей сессии

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