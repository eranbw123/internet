"""The background scheduler: a sleep loop, on purpose.

No cron, no APScheduler, no job table. One process, one interval; if it dies,
the next start picks up where SQLite left off. Swap in the OS scheduler
(Task Scheduler / cron) by calling `run-once` instead of `run`.
"""
import time
from datetime import datetime, timezone

from .pipeline import run_once


def run_forever(conn, provider, cfg, dry_run=False, cycles=None):
    """Run cycles `cfg.interval_seconds` apart. `cycles` bounds the loop (tests)."""
    completed = 0
    while cycles is None or completed < cycles:
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary = run_once(conn, provider, cfg, dry_run=dry_run)
        print(f"[{started}] {summary}", flush=True)
        completed += 1
        if cycles is not None and completed >= cycles:
            break
        time.sleep(cfg.interval_seconds)
    return completed
