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
            # shell=True's cwd handling; /d suppresses AutoRun. `timeout`
            # bounds how long we wait on the launcher command itself:
            # cfg.chrome_launch_cmd must be a detached form (e.g. `start ""
            # chrome.exe ...`) so it returns immediately, but if it isn't,
            # this is what stops it from hanging run-once until the
            # scheduled task's own ExecutionTimeLimit kills the whole
            # process -- reuses chrome_launch_wait_seconds rather than
            # adding another knob, since that's already "how long we're
            # willing to wait for Chrome to come up".
            subprocess.run(
                ["cmd", "/d", "/c", cfg.chrome_launch_cmd],
                check=False, timeout=cfg.chrome_launch_wait_seconds,
                # DEVNULL on all three, and this is not cosmetic. Under Task
                # Scheduler this process's stdout IS the job's log file, opened
                # by ops/run.cmd's `>>` without FILE_SHARE_WRITE. A child
                # inherits those handles, and the child here is a BROWSER that
                # deliberately outlives us -- so the Chrome we launch keeps the
                # log file locked for its entire life, and every later run of
                # this job dies inside cmd at the redirect, before python
                # starts: no output, no heartbeat, no counter, nothing to see.
                # That is exactly how logs/web-tick-20260818.log stopped at
                # 14:44 (Chrome pid 7324, launched 14:43:47, held it) while the
                # scheduler cheerfully went on reporting success every minute.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
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

    # While paused (see __main__.PAUSE_GATED) the gated jobs deliberately
    # don't run, so their growing heartbeat age is the intended state, not an
    # incident -- and nothing spends, so provider reachability doesn't matter
    # either (skipping it also avoids preflight's CDP touch during a freeze).
    paused = db.state_get(conn, "paused") == "1"
    if paused:
        provider_ok, provider_detail = None, "not checked (paused)"
    elif provider is None:
        provider_ok, provider_detail = None, "not checked"
    else:
        try:
            provider_ok, provider_detail = provider.preflight()
        except Exception as e:  # noqa: BLE001 -- must not crash the report
            provider_ok, provider_detail = False, f"preflight raised: {e}"

    pending_count, pending_oldest = db.pending_notification_stats(conn)
    # Reported, but deliberately NOT a degraded-gating signal: the retry
    # policy is designed to eventually abandon a send after enough failed
    # attempts (5 attempts x 30-min cool-off by default), so an abandoned
    # row is an expected steady-state artifact of an outage that has already
    # passed, not evidence the system is currently broken. There is also no
    # ack/clear path for these rows (only `score --force` removes one), so
    # gating on an unbounded, all-time count would latch `degraded` forever
    # the first time an outage outlasts the retry window -- `health` would
    # never report OK again, `--notify` would re-alert every cooldown
    # indefinitely, and the required one-time recovery message could never
    # fire.
    abandoned = db.abandoned_notifications(conn, cfg.send_max_attempts)
    degraded = not paused and bool(any(job["stale"] for job in jobs) or provider_ok is False)
    return {
        "paused": paused,
        "paused_why": db.state_get(conn, "paused_why") or "",
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
    if result.get("paused"):
        why = result.get("paused_why") or ""
        lines.append(
            "  PAUSED" + (f" ({why})" if why else "")
            + " -- run-once/web-tick/digest skip; `python -m app resume` lifts it"
        )
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
        provider_line = result.get("provider_detail") or "not checked"
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
