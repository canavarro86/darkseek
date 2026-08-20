# Boot-time + watchdog units

Fixes the gap found in the infra audit: nothing brought the stack back up
after a host reboot, and nothing detected a container that was up-but-not-
running (`Exited`/`Restarting`-looping) between deploys. Two systemd units:

- `darkseek.service` — runs `docker compose up -d` once at boot, after
  `docker.service` is up. Without this, `restart: unless-stopped` /
  `on-failure` on individual containers means nothing: those policies only
  apply to containers that already exist, and a reboot with no compose project
  running yet creates none.
- `darkseek-watchdog.service` + `.timer` — every 10 minutes, runs
  `scripts/host_watchdog.sh` on the host, which asserts the crawler container
  is actually `running` (not just present) and brings the whole project up if
  it's down at all. Catches the case a container was manually stopped or OOM-
  killed outside a state Docker's own restart policy will resurrect on its own.

## Install (run on the VPS as root; not run by this change — apply when ready)

```bash
cp /opt/darkseek/deploy/darkseek.service \
   /opt/darkseek/deploy/darkseek-watchdog.service \
   /opt/darkseek/deploy/darkseek-watchdog.timer \
   /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now darkseek.service
systemctl enable --now darkseek-watchdog.timer

# Verify
systemctl status darkseek.service darkseek-watchdog.timer
journalctl -u darkseek-watchdog.service -n 20
tail -f /var/log/darkseek_watchdog.log
```

If the host doesn't run systemd, use cron instead of the two watchdog units
(`darkseek.service` has no cron equivalent needed — Docker's own `restart:
unless-stopped` containers DO come back after a `docker.service` restart, the
gap is only "does anything ever call `docker compose up -d` again after a full
host reboot / first-ever boot"; a `@reboot` cron line covers that):

```cron
@reboot cd /opt/darkseek && docker compose up -d --remove-orphans
*/10 * * * * DARKSEEK_DIR=/opt/darkseek /opt/darkseek/scripts/host_watchdog.sh
```
