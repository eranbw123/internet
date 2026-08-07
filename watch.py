#!/usr/bin/env python3
"""Watchlist price alerter: Yahoo Finance -> ntfy push.

Reads a watchlist file of tickers, each tagged with a schedule (hourly /
daily / weekly). Run it once per schedule bucket from Task Scheduler, cron,
or a CI workflow:

    python watch.py --schedule daily

For every matching ticker it pulls Yahoo Finance's public chart endpoint,
computes the price change over that period, and pushes one ntfy
notification summarising the movers.

ntfy config is read from the environment ONLY -- an ntfy topic is
effectively a shared secret (anyone who knows the topic string can read
your notifications and send to them), and this repo is public, so there is
deliberately no default topic baked in here. Set NTFY_TOPIC in .env
(gitignored) or in the process environment.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

# Yahoo 403s the default urllib User-Agent, so send a browser-ish one.
USER_AGENT = "Mozilla/5.0 (compatible; watchlist-alerter/1.0)"

# Per-schedule fetch parameters and how far back to compare.
#
# `lookback` is counted in *bars*, not calendar time, which is what makes
# this correct across weekends and holidays: "weekly" means 5 trading bars
# back, so a Monday run compares against the previous Monday's close rather
# than against a non-existent weekend bar. We request a wider `range` than
# strictly needed so holidays never leave us short of bars.
SCHEDULES = {
    "hourly": {"range": "5d", "interval": "1h", "lookback": 1, "label": "1h"},
    "daily": {"range": "1mo", "interval": "1d", "lookback": 1, "label": "1d"},
    "weekly": {"range": "3mo", "interval": "1d", "lookback": 5, "label": "1w"},
}

DEFAULT_WATCHLIST = "watchlist.json"


class WatchError(Exception):
    """Anything that should abort one ticker without killing the whole run."""


def load_watchlist(path):
    """Parse the watchlist file into a list of normalised entries.

    Accepts either the long form::

        {"tickers": [{"ticker": "NBIS", "schedule": "weekly", "min_change_pct": 2}]}

    or the shorthand, where a bare string inherits the file-level default::

        {"default_schedule": "daily", "tickers": ["NBIS", "MSFT"]}
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise WatchError(
            f"watchlist not found: {path}\n"
            f"Copy watchlist.example.json to {path} and edit it."
        )
    except json.JSONDecodeError as e:
        raise WatchError(f"watchlist {path} is not valid JSON: {e}")

    default_schedule = raw.get("default_schedule", "daily")
    default_threshold = float(raw.get("default_min_change_pct", 0.0))

    entries = []
    for item in raw.get("tickers", []):
        if isinstance(item, str):
            item = {"ticker": item}
        if not isinstance(item, dict) or not item.get("ticker"):
            raise WatchError(f"watchlist entry missing a ticker: {item!r}")

        schedule = item.get("schedule", default_schedule)
        if schedule not in SCHEDULES:
            raise WatchError(
                f"{item['ticker']}: unknown schedule {schedule!r} "
                f"(expected one of {', '.join(SCHEDULES)})"
            )
        entries.append(
            {
                "ticker": item["ticker"].upper(),
                "schedule": schedule,
                # Suppress noise: only alert when the move is at least this
                # big in either direction. 0 means always alert.
                "min_change_pct": float(
                    item.get("min_change_pct", default_threshold)
                ),
            }
        )

    if not entries:
        raise WatchError(f"watchlist {path} has no tickers")
    return entries


def fetch_chart(ticker, rng, interval, timeout=15):
    """Return Yahoo's parsed chart payload for one ticker."""
    url = CHART_URL.format(ticker=urllib.parse.quote(ticker))
    url += "?" + urllib.parse.urlencode({"range": rng, "interval": interval})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise WatchError(f"{ticker}: Yahoo returned HTTP {e.code}")
    except Exception as e:  # noqa: BLE001 -- network/parse, same handling
        raise WatchError(f"{ticker}: fetch failed: {e}")

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise WatchError(f"{ticker}: {chart['error'].get('description', chart['error'])}")
    results = chart.get("result") or []
    if not results:
        raise WatchError(f"{ticker}: no data returned (bad symbol?)")
    return results[0]


def price_change(ticker, schedule):
    """Fetch `ticker` and compute its change over the `schedule` window."""
    spec = SCHEDULES[schedule]
    result = fetch_chart(ticker, spec["range"], spec["interval"])

    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []

    # Yahoo pads the series with nulls for bars it has no print for; drop
    # them so `lookback` counts real bars only.
    bars = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
    if len(bars) <= spec["lookback"]:
        raise WatchError(
            f"{ticker}: only {len(bars)} usable bars, need "
            f"{spec['lookback'] + 1} for a {schedule} comparison"
        )

    then_ts, then_price = bars[-1 - spec["lookback"]]
    now_ts, now_price = bars[-1]

    # Prefer the live quote over the last bar's close: intraday, the final
    # bar can lag the current print by up to one interval.
    live = meta.get("regularMarketPrice")
    if live is not None:
        now_price = float(live)
        now_ts = meta.get("regularMarketTime", now_ts)

    delta = now_price - then_price
    pct = (delta / then_price * 100.0) if then_price else 0.0
    tz = timezone(timedelta(seconds=meta.get("gmtoffset", 0)))

    return {
        "ticker": ticker,
        "schedule": schedule,
        "label": spec["label"],
        "currency": meta.get("currency", "USD"),
        "then_price": then_price,
        "then_at": datetime.fromtimestamp(then_ts, tz),
        "now_price": now_price,
        "now_at": datetime.fromtimestamp(now_ts, tz),
        "delta": delta,
        "pct": pct,
    }


def format_line(change):
    """One-line summary of a single ticker's move.

    Deliberately ASCII-only: this string is both printed to the console and
    sent as the ntfy body, and Windows consoles on a non-UTF-8 codepage
    (cp1255 here) raise UnicodeEncodeError on arrows like U+25B2. The ntfy
    tag already renders a trend icon in the app, so the sign carries it.
    """
    sign = "+" if change["delta"] >= 0 else ""
    return (
        f"{change['ticker']:<6} {change['now_price']:>9.2f} {change['currency']}  "
        f"{sign}{change['pct']:.2f}% ({sign}{change['delta']:.2f} / {change['label']})"
    )


def ntfy_notify(title, message, priority="default", tags=None):
    """Best-effort push via ntfy. Never raises -- a failed push must not
    fail the run, and there is no default topic (see module docstring)."""
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        print("NTFY_TOPIC not set -- skipping push", file=sys.stderr)
        return False
    base = os.environ.get("NTFY_BASE", "https://ntfy.sh")
    headers = {"Title": title, "Priority": priority, "User-Agent": USER_AGENT}
    if tags:
        headers["Tags"] = tags
    req = urllib.request.Request(
        f"{base}/{topic}", data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"ntfy notify failed: {e}", file=sys.stderr)
        return False


def load_dotenv(path=".env"):
    """Minimal KEY=VALUE loader so NTFY_TOPIC can live in a gitignored file.

    Deliberately tiny (no python-dotenv dependency) -- existing environment
    variables always win, so CI secrets override the local file.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def run(entries, dry_run=False):
    """Fetch every entry, push one digest, return a process exit code."""
    changes, failures = [], []
    for entry in entries:
        try:
            changes.append(price_change(entry["ticker"], entry["schedule"]))
            changes[-1]["min_change_pct"] = entry["min_change_pct"]
        except WatchError as e:
            print(f"WARN {e}", file=sys.stderr)
            failures.append(str(e))

    for change in changes:
        print(format_line(change))

    alerts = [c for c in changes if abs(c["pct"]) >= c["min_change_pct"]]
    if not alerts:
        print("nothing crossed its threshold -- no push")
        return 1 if failures and not changes else 0

    biggest = max(alerts, key=lambda c: abs(c["pct"]))
    sign = "+" if biggest["delta"] >= 0 else ""
    if len(alerts) == 1:
        title = f"{biggest['ticker']} {sign}{biggest['pct']:.2f}% ({biggest['label']})"
    else:
        title = (
            f"{len(alerts)} movers -- {biggest['ticker']} "
            f"{sign}{biggest['pct']:.2f}% ({biggest['label']})"
        )

    body = "\n".join(format_line(c) for c in alerts)
    if failures:
        body += "\n\nFailed: " + "; ".join(failures)
    tags = "chart_with_upwards_trend" if biggest["delta"] >= 0 else "chart_with_downwards_trend"

    if dry_run:
        print(f"\n--- dry run, would push ---\n{title}\n{body}")
    else:
        ntfy_notify(title, body, tags=tags)

    return 1 if failures and not changes else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--schedule",
        choices=sorted(SCHEDULES),
        help="Only process watchlist entries with this schedule. Omit for all.",
    )
    parser.add_argument(
        "--watchlist", default=DEFAULT_WATCHLIST, help=f"Watchlist file (default: {DEFAULT_WATCHLIST})"
    )
    parser.add_argument(
        "--ticker",
        action="append",
        help="Check this ticker instead of the watchlist. Repeatable.",
    )
    parser.add_argument(
        "--min-change-pct",
        type=float,
        help="Override every entry's alert threshold.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print instead of pushing")
    args = parser.parse_args(argv)

    load_dotenv()

    try:
        if args.ticker:
            schedule = args.schedule or "daily"
            entries = [
                {"ticker": t.upper(), "schedule": schedule, "min_change_pct": 0.0}
                for t in args.ticker
            ]
        else:
            entries = load_watchlist(args.watchlist)
            if args.schedule:
                entries = [e for e in entries if e["schedule"] == args.schedule]
            if not entries:
                print(f"no {args.schedule} entries in {args.watchlist} -- nothing to do")
                return 0
    except WatchError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2

    if args.min_change_pct is not None:
        for e in entries:
            e["min_change_pct"] = args.min_change_pct

    return run(entries, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
