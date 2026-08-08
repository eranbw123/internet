"""The background scheduler: a tick loop, on purpose.

No cron, no APScheduler, no job table. One process, four jobs, each with its
own cadence, tracked as plain "next due" timestamps:

    stocks    every cfg.interval_stocks_seconds   (default 1h)
    web       every cfg.interval_web_seconds       (default 4h) -- web_search + web
    youtube   every cfg.interval_youtube_seconds   (default 4h)
    digest    once a day at cfg.digest_time (local HH:MM)

If the process dies, the next start just re-derives "due now" from these
defaults and SQLite's persisted state (dedup/prefilter/scores already done
are never redone). Swap in the OS scheduler (Task Scheduler / cron) by
calling `run-once --source <name>` / `digest` instead of `run`.
"""
import time
from datetime import datetime, timedelta

from .pipeline import run_once, send_digest

# Collector job name -> (collectors it runs, the Config field holding its
# cadence). "web" is one cadence bucket for both web-ish collectors;
# stocks/youtube stand alone.
JOBS = {
    "stocks": (["stocks"], "interval_stocks_seconds"),
    "web": (["web_search", "web"], "interval_web_seconds"),
    "youtube": (["youtube"], "interval_youtube_seconds"),
}

TICK_SECONDS = 60  # how often the loop wakes up to check what's due


def _seconds_until_digest(cfg, now):
    """Seconds until the next occurrence of cfg.digest_time ('HH:MM', local),
    today if it hasn't passed yet, otherwise tomorrow."""
    try:
        hour, minute = (int(p) for p in cfg.digest_time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(cfg.digest_time)
    except ValueError:
        raise SystemExit(
            f"invalid DISCOVERY_DIGEST_TIME {cfg.digest_time!r} (expected HH:MM)"
        ) from None
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_forever(conn, provider, cfg, dry_run=False, cycles=None):
    """Runs until stopped, checking every TICK_SECONDS which jobs are due.
    `cycles` bounds the number of *ticks* (tests), not calendar time -- every
    job is due on the first tick, so `cycles=1` alone already exercises all
    four."""
    now = time.monotonic()
    next_due = {name: now for name in JOBS}
    next_digest = now + _seconds_until_digest(cfg, datetime.now())

    ticks = 0
    while cycles is None or ticks < cycles:
        now = time.monotonic()
        for name, (sources, interval_field) in JOBS.items():
            if now >= next_due[name]:
                summary = run_once(conn, provider, cfg, sources=sources, dry_run=dry_run)
                print(f"[{_stamp()}] {name}: {summary}", flush=True)
                next_due[name] = now + getattr(cfg, interval_field)
        if now >= next_digest:
            sent = send_digest(conn, cfg, dry_run=dry_run)
            print(f"[{_stamp()}] digest: sent {sent}", flush=True)
            next_digest = now + 24 * 3600

        ticks += 1
        if cycles is not None and ticks >= cycles:
            break
        time.sleep(TICK_SECONDS)
    return ticks


def _stamp():
    return datetime.now().isoformat(timespec="seconds")
