"""Shared Yahoo Finance helper: fetch a ticker's chart and compute its price
change over a trading-bar window.

Not a standalone tool -- this is a library used by the `stocks` collector
(`discovery/collectors/stocks.py`, via `price_change`/`WatchError`) and by
`discovery/config.py` (via `load_dotenv`). There is no CLI/notification flow
here; alerting lives in the discovery pipeline (Telegram ALERT/DISCOVERY),
not in this module.
"""

import os
import urllib.error
import urllib.parse
import urllib.request
import json
from datetime import datetime, timedelta, timezone

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

# Yahoo 403s the default urllib User-Agent, so send a browser-ish one.
USER_AGENT = "Mozilla/5.0 (compatible; internet-discovery/1.0)"

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


class WatchError(Exception):
    """Anything that should abort one ticker without killing the caller's run."""


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


def load_dotenv(path=".env"):
    """Minimal KEY=VALUE loader so secrets can live in a gitignored file.

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
