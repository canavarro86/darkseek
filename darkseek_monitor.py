#!/usr/bin/env python3
"""
DarkSeek Monitor — live CLI dashboard
Run: python3 darkseek_monitor.py
Quit: Ctrl+C

CHANGELOG (2026-08-20):
- Added a separate "recheck activity" metric based on last_seen, alongside the
  original "discovery" metric based on indexed_at. The crawler mostly revisits
  already-known URLs to check if they're still alive — that only updates
  last_seen, not indexed_at. The old dashboard measured discovery rate only,
  so it showed 0.0 URL/min even while the crawler was actively working.
"""

import sqlite3
import time
import os
import sys
from datetime import datetime

DB_PATH = "/opt/darkseek_db/darkseek.db"
REFRESH = 5  # seconds


def clear():
    os.system("clear")


def query(con, sql, one=False):
    try:
        cur = con.execute(sql)
        return cur.fetchone() if one else cur.fetchall()
    except Exception:
        return None if one else []


def bar(value, max_value, width=20, char="█"):
    if max_value == 0:
        return "░" * width
    filled = int(width * value / max_value)
    return char * filled + "░" * (width - filled)


def sparkline(values, width=30):
    chars = " ▁▂▃▄▅▆▇█"
    if not values or max(values) == 0:
        return " " * width
    mx = max(values)
    return "".join(chars[int(v / mx * (len(chars) - 1))] for v in values[-width:])


def main():
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found: {DB_PATH}")
        sys.exit(1)

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    while True:
        now = datetime.utcnow()

        # --- totals ---
        total = (query(con, "SELECT COUNT(*) FROM pages", one=True) or (0,))[0]
        alive = (query(con, "SELECT COUNT(*) FROM pages WHERE is_alive=1", one=True) or (0,))[0]
        dead  = (query(con, "SELECT COUNT(*) FROM pages WHERE is_alive=0", one=True) or (0,))[0]

        categories = query(con, """
            SELECT category, COUNT(*) as cnt
            FROM pages WHERE is_alive=1
            GROUP BY category ORDER BY cnt DESC LIMIT 6
        """)

        # --- DISCOVERY metrics (new pages, based on indexed_at) ---
        new_1h  = (query(con, "SELECT COUNT(*) FROM pages WHERE indexed_at >= datetime('now','-1 hour')", one=True) or (0,))[0]
        new_24h = (query(con, "SELECT COUNT(*) FROM pages WHERE indexed_at >= datetime('now','-24 hours')", one=True) or (0,))[0]
        new_speed_raw = (query(con, "SELECT COUNT(*) FROM pages WHERE indexed_at >= datetime('now','-5 minutes')", one=True) or (0,))[0]
        new_speed = new_speed_raw / 5.0

        # --- RECHECK / ACTIVITY metrics (revisits, based on last_seen) ---
        # Most crawl cycles revisit already-known URLs to confirm they're still
        # alive — that only touches last_seen. This is the real "is the crawler
        # working right now" signal.
        seen_5m  = (query(con, "SELECT COUNT(*) FROM pages WHERE last_seen >= datetime('now','-5 minutes')", one=True) or (0,))[0]
        seen_1h  = (query(con, "SELECT COUNT(*) FROM pages WHERE last_seen >= datetime('now','-1 hour')", one=True) or (0,))[0]
        seen_24h = (query(con, "SELECT COUNT(*) FROM pages WHERE last_seen >= datetime('now','-24 hours')", one=True) or (0,))[0]
        activity_speed = seen_5m / 5.0

        # growth history (per 5min buckets, last 2.5h) — based on last_seen so
        # the sparkline actually reflects crawler activity, not just discoveries
        growth = query(con, """
            SELECT strftime('%Y-%m-%d %H:%M', last_seen, 'start of minute',
                   printf('-%d minutes', (strftime('%M', last_seen) % 5))) as bucket,
                   COUNT(*) as cnt
            FROM pages
            WHERE last_seen >= datetime('now', '-150 minutes')
            GROUP BY bucket ORDER BY bucket
        """)
        spark_values = [r[1] for r in growth] if growth else []

        # --- render ---
        clear()
        W = 60
        print("╔" + "═" * W + "╗")
        print("║" + "  🕷  DarkSeek Monitor".center(W) + "║")
        print("║" + f"  {now.strftime('%Y-%m-%d %H:%M:%S')} UTC".ljust(W) + "║")
        print("╠" + "═" * W + "╣")

        print("║" + "  INDEX".ljust(W) + "║")
        print("║" + f"  Total pages  : {total:,}".ljust(W) + "║")
        print("║" + f"  Alive        : {alive:,}  ({int(alive/total*100) if total else 0}%)".ljust(W) + "║")
        print("║" + f"  Dead/offline : {dead:,}  ({int(dead/total*100) if total else 0}%)".ljust(W) + "║")
        print("║" + f"  alive [{bar(alive, total, 28)}]".ljust(W) + "║")
        print("║" + f"  dead  [{bar(dead,  total, 28, '▒')}]".ljust(W) + "║")

        print("╠" + "═" * W + "╣")

        print("║" + "  ACTIVITY (last_seen — is the crawler working NOW)".ljust(W) + "║")
        print("║" + f"  Activity     : {activity_speed:.1f} URL/min".ljust(W) + "║")
        print("║" + f"  Last 5min    : {seen_5m:,} pages touched".ljust(W) + "║")
        print("║" + f"  Last 1h      : {seen_1h:,} pages touched".ljust(W) + "║")
        print("║" + f"  Last 24h     : {seen_24h:,} pages touched".ljust(W) + "║")

        print("╠" + "═" * W + "╣")

        print("║" + "  DISCOVERY (indexed_at — brand-new URLs only)".ljust(W) + "║")
        print("║" + f"  New speed    : {new_speed:.1f} URL/min".ljust(W) + "║")
        print("║" + f"  New last 1h  : +{new_1h:,} pages".ljust(W) + "║")
        print("║" + f"  New last 24h : +{new_24h:,} pages".ljust(W) + "║")

        print("╠" + "═" * W + "╣")

        print("║" + "  ACTIVITY (5min buckets, last 2.5h)".ljust(W) + "║")
        spark = sparkline(spark_values, width=W - 4)
        print("║" + f"  {spark}".ljust(W) + "║")

        print("╠" + "═" * W + "╣")

        print("║" + "  CATEGORIES".ljust(W) + "║")
        if categories:
            max_cnt = categories[0][1] if categories else 1
            for cat, cnt in categories:
                cat_str = (cat or "other").ljust(10)
                b = bar(cnt, max_cnt, width=18)
                line = f"  {cat_str} [{b}] {cnt:,}"
                print("║" + line.ljust(W) + "║")
        else:
            print("║" + "  no data yet".ljust(W) + "║")

        print("╠" + "═" * W + "╣")
        print("║" + f"  Refresh every {REFRESH}s  |  Ctrl+C to quit".ljust(W) + "║")
        print("╚" + "═" * W + "╝")

        time.sleep(REFRESH)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  bye.")
