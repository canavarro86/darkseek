#!/bin/sh
# Host-level watchdog for the DarkSeek Docker Compose stack. Runs on the VPS
# itself (NOT inside a container — it drives `docker compose`, which needs the
# host's Docker socket), on a cron schedule (see deploy/README for the
# crontab line, or use deploy/darkseek-watchdog.timer instead).
#
# Why this exists: `restart: on-failure` / `unless-stopped` in docker-compose.yml
# only re-launches a container while the Docker daemon itself considers it
# "should be running". Two real gaps that policy alone does not cover, which is
# exactly how the crawler container was found sitting Exited(137) for two weeks
# with nothing bringing it back:
#   1. The compose PROJECT never gets `docker compose up -d` re-run after a
#      host reboot unless something (systemd unit, this cron job) does it —
#      Docker restarting individual containers on daemon start only works for
#      containers that already exist; it does nothing for a stack that was
#      never (re)created because dockerd itself wasn't up yet at boot ordering
#      time, or the host rebooted mid-incident.
#   2. A container that was manually `docker stop`-ped (including indirectly —
#      e.g. an OOM-kill during a `docker compose down`/`up` cycle, or an
#      operator debugging session) is intentionally NOT restarted by any
#      restart policy until something explicitly starts it again. Docker
#      remembers "the human wanted this stopped" and honours that forever.
# This script is the "something": it periodically asserts the crawler
# container is actually running (not just present) and brings the whole stack
# up if the compose project itself isn't up at all.
set -eu

COMPOSE_DIR="${DARKSEEK_DIR:-/opt/darkseek}"
LOG="${WATCHDOG_LOG:-/var/log/darkseek_watchdog.log}"

log() {
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" >> "$LOG"
}

cd "$COMPOSE_DIR"

# If the project has no containers up at all (e.g. right after a host reboot,
# before any boot unit ran, or this is the first tick after one), bring the
# whole stack up. `up -d` is idempotent — a no-op for services already healthy.
if [ -z "$(docker compose ps -q 2>/dev/null)" ]; then
  log "compose project is down; running 'docker compose up -d'"
  docker compose up -d >> "$LOG" 2>&1
  exit 0
fi

# Crawler specifically: check it's actually RUNNING, not just that a container
# with that name exists (it could be Exited/Created/Restarting-looping).
status="$(docker compose ps --status=running -q crawler 2>/dev/null)"
if [ -z "$status" ]; then
  log "crawler container is not running; running 'docker compose up -d crawler'"
  docker compose up -d crawler >> "$LOG" 2>&1
fi
