"""The provider preflight gate + the operational health readout.

Two jobs, both about one question -- "would this invocation actually get
anything done, or is it about to burn a cycle for nothing":

  * preflight_gate() runs before run-once touches a single collector or LLM
    call. A dead Chrome/CDP/claude.ai tab today means every collect/score
    call fails one at a time, noisily, after real work (fetches, prompts)
    was already spent. The gate makes that one cheap, free check instead.
  * check()/format_report() are what `python -m app health` and
    stats.report's HEALTH section share: job heartbeat staleness, provider
    reachability, pending/abandoned notifications, today's ops counters.

No job table, no long-running watchdog -- every call here is a handful of
SQLite reads plus (for the gate) one free local HTTP GET.
"""
import subprocess
import time
from datetime import datetime, timezone

from . import db, notify
from .notify import print_safe

# Collector-name -> job-name mapping. Job names match the Config field that
# holds their cadence (interval_web_seconds etc.) rather than the collector
# name registered in COLLECTORS, so JOB_INTERVALS below can look the field up
# directly. `run-once` with no --source (all collectors in one cycle) has no
# single cadence of its own, so it gets the generic "run-once" job name and
# is not staleness-tracked.
SOURCE_JOB_NAMES = {"web_search": "web", "stocks": "stocks", "youtube": "youtube"}

# Job name -> the Config field holding its cadence. The feedback drain's own
# cadence (every 5 minutes, see ops/install_tasks.py) is not a Config field,
# so it is reported without a staleness verdict rather than a hardcoded
# number duplicating what the installer owns.
JOB_INTERVALS = {
    "stocks": "interval_stocks_seconds",
    "web": "interval_web_seconds",
    "youtube": "interval_youtube_seconds",
}
DIGEST_INTERVAL_SECONDS = 24 * 3600


def job_name_for_source(source):
    return SOURCE_JOB_NAMES.get(source, source or "run-once")


def preflight_gate(conn, provider, cfg, job_name):
    """True if the provider is reachable; False if run-once must bail out
    (exit 3) without calling a single collector or LLM.

    On failure, optionally relaunches Chrome once (`cfg.chrome_launch_cmd`,
    empty by default -- never spawns anything), waits
    `cfg.chrome_launch_wait_seconds`, and re-checks once before giving up.
    """
    ok, detail = provider.preflight()
    if not ok and cfg.chrome_launch_cmd:
        print_safe(f"job '{job_name}': provider down ({detail}); launching Chrome once")
        try:
            # cmd /d /c -- this machine has a cmd AutoRun hook that breaks
            # shell=True's cwd handling; /d suppresses AutoRun.
            subprocess.run(["cmd", "/d", "/c", cfg.chrome_launch_cmd], check=False)
        except OSError as e:
            print_safe(f"job '{job_name}': chrome_launch_cmd failed to start: {e}")
        time.sleep(cfg.chrome_launch_wait_seconds)
        ok, detail = provider.preflight()

    if ok:
        return True
    db.bump(conn, {"provider_down": 1})
    db.state_set(conn, f"job:{job_name}:last_fail", db.now())
    print_safe(f"job '{job_name}': provider preflight failed -- {detail}")
    return False


def _age_seconds(iso_ts):
    if not iso_ts:
        return None
    ts = datetime.fromisoformat(iso_ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _job_status(conn, name, interval_seconds, stale_factor):
    last_ok = db.state_get(conn, f"job:{name}:last_ok")
    last_fail = db.state_get(conn, f"job:{name}:last_fail")
    age = _age_seconds(last_ok)
    # A job that has never run is "unknown", not "stale" -- staleness means a
    # job that *was* running has gone silent, which needs at least one
    # last_ok to measure from. A fresh `init` should not read as degraded.
    stale = interval_seconds is not None and age is not None and age > interval_seconds * stale_factor
    return {
        "name": name,
        "last_ok": last_ok,
        "last_fail": last_fail,
        "age_seconds": age,
        "interval_seconds": interval_seconds,
        "stale": stale,
    }


def check(conn, cfg, provider=None):
    """Everything `health` and stats.report's HEALTH section need, computed
    once. Never raises -- a health check that crashes is a worse failure
    than the one it's trying to report. `provider` is optional: stats.report
    is a read-only command and must not pay for a live CDP/API reachability
    check just to print a report; pass it (as `health` does) to get a real
    verdict instead of "not checked"."""
    jobs = [
        _job_status(conn, name, getattr(cfg, field), cfg.health_stale_factor)
        for name, field in JOB_INTERVALS.items()
    ]
    jobs.append(_job_status(conn, "digest", DIGEST_INTERVAL_SECONDS, cfg.health_stale_factor))
    jobs.append(_job_status(conn, "feedback", None, cfg.health_stale_factor))

    if provider is None:
        provider_ok, provider_detail = None, "not checked"
    else:
        try:
            provider_ok, provider_detail = provider.preflight()
        except Exception as e:  # noqa: BLE001 -- must not crash the report
            provider_ok, provider_detail = False, f"preflight raised: {e}"

    pending_count, pending_oldest = db.pending_notification_stats(conn)
    abandoned = db.abandoned_notifications(conn, cfg.send_max_attempts)
    degraded = bool(
        any(job["stale"] for job in jobs) or provider_ok is False or abandoned > 0
    )
    return {
        "jobs": jobs,
        "provider_ok": provider_ok,
        "provider_detail": provider_detail,
        "pending_count": pending_count,
        "pending_oldest": pending_oldest,
        "abandoned": abandoned,
        "counters": db.today_counts(conn),
        "degraded": degraded,
    }


def format_report(result):
    lines = ["HEALTH"]
    for job in result["jobs"]:
        age = "never" if job["age_seconds"] is None else f"{job['age_seconds'] / 3600:.1f}h ago"
        flag = ""
        if job["interval_seconds"] is not None:
            flag = "  STALE" if job["stale"] else ("  ok" if job["age_seconds"] is not None else "")
        line = f"  {job['name']:<10}last_ok={age}{flag}"
        if job["last_fail"]:
            line += f"  last_fail={job['last_fail']}"
        lines.append(line)

    if result["provider_ok"] is None:
        provider_line = "not checked"
    elif result["provider_ok"]:
        provider_line = "ok"
    else:
        provider_line = f"DOWN ({result['provider_detail']})"
    lines.append(f"  provider: {provider_line}")

    oldest = f" (oldest {result['pending_oldest']})" if result["pending_oldest"] else ""
    lines.append(f"  pending notifications: {result['pending_count']}{oldest}")
    lines.append(f"  abandoned (attempt cap): {result['abandoned']}")

    counters = result["counters"]
    counters_line = ", ".join(f"{k}={v}" for k, v in sorted(counters.items())) or "none"
    lines.append(f"  today: {counters_line}")
    lines.append(f"  overall: {'DEGRADED' if result['degraded'] else 'OK'}")
    return "\n".join(lines)


def notify_if_needed(conn, cfg, result):
    """At most one Telegram alert per cfg.health_alert_cooldown_seconds while
    degraded, and exactly one recovery message on the degraded->ok
    transition -- gated on health:last_status / health:last_alert_at so a
    `health --notify` firing every few hours doesn't spam. Depends only on
    Telegram, never on the provider being checked, so a dead Chrome can
    still be reported."""
    previous = db.state_get(conn, "health:last_status")

    if not result["degraded"]:
        if previous == "degraded":
            notify.send(cfg, "✅ discovery: recovered, health OK")
            db.state_set(conn, "health:last_alert_at", db.now())
        db.state_set(conn, "health:last_status", "ok")
        return

    last_alert_age = _age_seconds(db.state_get(conn, "health:last_alert_at"))
    due = previous != "degraded" or last_alert_age is None or last_alert_age > cfg.health_alert_cooldown_seconds
    if due:
        notify.send(cfg, f"⚠️ discovery: degraded\n\n{format_report(result)}")
        db.state_set(conn, "health:last_alert_at", db.now())
    db.state_set(conn, "health:last_status", "degraded")
