"""Stock price moves, reusing watch.py's Yahoo Finance fetch.

Two independent thresholds per ticker -- `daily_percent_move` and
`weekly_percent_move` -- because a move can be alert-worthy over one window
and noise over the other. Both windows are always fetched (watch.py's
`price_change` for "daily" and "weekly"); crossing either one raises a single
`market_event` candidate per ticker per poll, and its metadata carries both
changes so the picture is never partial even when only one window crossed.
A flat ticker on both windows produces nothing -- that's the point of having
a threshold at all.

When a provider is given (and `source_config["stocks"]["explain"]` is not
false), one `search_json` call looks for recent company news that might
explain the move, and one `complete_json` call grades those snippets -- and
only those snippets, per EXPLAIN_SYSTEM -- into confirmed / plausible / no
catalyst plus a confidence. This collector never invents a reason: with no
news, or a provider that can't search, the market_event still fires with
"no obvious catalyst found". The news search results also come back as their
own `article` CandidateItems so the normal pipeline (dedup, prefilter, LLM
relevance score) judges them exactly like any other collector's finds -- this
module only decides whether they're worth fetching, not whether they matter.

A ticker that stays past its threshold crosses it again on every poll, but its
dedup_key is per-day, so every poll after the first is thrown away downstream.
`conn` (when the caller has one) is used to skip those *before* the two LLM
calls: on an hourly stocks job that is the difference between 1 and 24
explanations a day per moving ticker.
"""
import sys

import watch  # repo-root module; only importable when run from the repo root

from ..db import seen_dedup_keys
from ..models import CandidateItem
from ..providers.base import ProviderError, UnsupportedCapability
from . import _search

DEFAULT_DAILY_PERCENT_MOVE = 6.0
DEFAULT_WEEKLY_PERCENT_MOVE = 12.0
DEFAULT_EXPLAIN_RECENCY_DAYS = 3
DEFAULT_NEWS_LIMIT = 5

EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "catalyst": {"type": "string", "enum": ["confirmed", "plausible", "none"]},
        "explanation": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["catalyst", "explanation", "confidence"],
    "additionalProperties": False,   # structured outputs require it on every object
}

EXPLAIN_SYSTEM = (
    "You explain stock price moves using ONLY the news snippets you are given. "
    "Never invent, guess, or infer a catalyst that is not directly supported by "
    "one of the snippets. If nothing in the snippets plausibly explains the "
    "move, say so -- 'no obvious catalyst found' is a correct and expected answer."
)

EXPLAIN_PROMPT = """\
{ticker} moved {pct:+.2f}% ({label}, current price {price:.2f} {currency}).

Candidate news, most recent first:
{snippets}

Classify the catalyst:
- "confirmed": one of the snippets is company-specific, dated within the move
  window, and plausibly market-moving (earnings, guidance, contract, filing,
  regulatory action, etc).
- "plausible": something in the snippets could explain it but it's indirect,
  sector-wide, unconfirmed, or the timing is uncertain.
- "none": nothing above supports a catalyst -- do not guess one.

Return JSON only: {{"catalyst": "confirmed"|"plausible"|"none",
"explanation": "1-2 sentences, grounded only in the snippets above",
"confidence": "low"|"medium"|"high"}}
"""

SEARCH_PROMPT = """\
Search for recent news about {ticker} ({company}) published in the last
{recency_days} days that could explain a {pct:+.2f}% price move.

Return AT MOST {limit} items as a JSON array, most relevant first. Only
include items that are actually about this company, not generic market or
sector commentary.

{result_spec}
"""


def collect(interest, cfg, provider=None, conn=None):
    opts = interest.source_config.get("stocks", {})
    explain = opts.get("explain", True)
    already_seen = seen_dedup_keys(conn, "stocks") if conn is not None else set()

    items = []
    for entry in _tickers(opts):
        checked = _check_ticker(entry)
        if checked is None:
            continue
        ticker, daily, weekly = checked
        if _event_key(ticker, daily) in already_seen:
            continue

        catalyst, news_items = (None, [])
        if explain and provider is not None:
            catalyst, news_items = _explain(interest, provider, opts, ticker, daily, weekly)

        items.append(_market_event(ticker, daily, weekly, catalyst, news_items))
        items.extend(news_items)
    return items


def _tickers(opts):
    """Normalize the tickers list: a bare string inherits the interest's
    defaults, matching the shorthand `watch.load_watchlist` already uses."""
    default_daily = float(opts.get("default_daily_percent_move", DEFAULT_DAILY_PERCENT_MOVE))
    default_weekly = float(opts.get("default_weekly_percent_move", DEFAULT_WEEKLY_PERCENT_MOVE))
    entries = []
    for raw in opts.get("tickers", []):
        item = {"ticker": raw} if isinstance(raw, str) else dict(raw)
        ticker = (item.get("ticker") or "").upper()
        if not ticker:
            continue
        entries.append(
            {
                "ticker": ticker,
                "daily_percent_move": float(item.get("daily_percent_move", default_daily)),
                "weekly_percent_move": float(item.get("weekly_percent_move", default_weekly)),
            }
        )
    return entries


def _check_ticker(entry):
    """Both windows, always -- a ticker that only crossed weekly still needs
    its daily change fetched to show the fuller picture. One bad ticker (bad
    symbol, Yahoo hiccup) is logged and skipped, not fatal to the others."""
    ticker = entry["ticker"]
    try:
        daily = watch.price_change(ticker, "daily")
        weekly = watch.price_change(ticker, "weekly")
    except watch.WatchError as e:
        print(f"stocks collector: {e}", file=sys.stderr)
        return None

    daily_crossed = entry["daily_percent_move"] > 0 and abs(daily["pct"]) >= entry["daily_percent_move"]
    weekly_crossed = entry["weekly_percent_move"] > 0 and abs(weekly["pct"]) >= entry["weekly_percent_move"]
    if not daily_crossed and not weekly_crossed:
        return None
    return ticker, daily, weekly


# --- explanation: search + grade, never invent -------------------------------

def _explain(interest, provider, opts, ticker, daily, weekly):
    """Returns (catalyst dict | None, list[CandidateItem]).

    Any provider failure here just means no explanation was found -- it must
    never drop the market_event itself, so both calls are best-effort."""
    recency_days = int(opts.get("explain_recency_days", DEFAULT_EXPLAIN_RECENCY_DAYS))
    news_limit = int(opts.get("news_limit", DEFAULT_NEWS_LIMIT))
    biggest = daily if abs(daily["pct"]) >= abs(weekly["pct"]) else weekly

    try:
        raw_items = provider.search_json(
            SEARCH_PROMPT.format(
                ticker=ticker,
                company=interest.title,
                recency_days=recency_days,
                pct=biggest["pct"],
                limit=news_limit,
                result_spec=_search.RESULT_SPEC,
            ),
            max_searches=1,
        )
    except UnsupportedCapability:
        return None, []
    except ProviderError as e:
        print(f"stocks collector: explanation search for {ticker} failed: {e}", file=sys.stderr)
        return None, []

    news_items = _search.to_items(
        raw_items, "stocks", news_limit, metadata={"ticker": ticker, "trigger": "price_move"}
    )
    if not news_items:
        return {"catalyst": "none", "explanation": "", "confidence": "low"}, []

    snippets = "\n".join(
        f"- [{n.published_at or 'undated'}] {n.title}: {n.text}" for n in news_items
    )
    try:
        catalyst = provider.complete_json(
            EXPLAIN_SYSTEM,
            EXPLAIN_PROMPT.format(
                ticker=ticker,
                pct=biggest["pct"],
                label=biggest["label"],
                price=biggest["now_price"],
                currency=biggest["currency"],
                snippets=snippets,
            ),
            EXPLAIN_SCHEMA,
        )
    except ProviderError as e:
        print(f"stocks collector: explanation classify for {ticker} failed: {e}", file=sys.stderr)
        return None, news_items

    return catalyst, news_items


# --- the market_event candidate + its combined summary ------------------------

def _event_key(ticker, daily):
    """The same ticker can cross its threshold again tomorrow, so the URL alone
    would collapse every day's event into one row."""
    return f"{ticker}:{daily['now_at'].date().isoformat()}"


def _market_event(ticker, daily, weekly, catalyst, news_items):
    return CandidateItem(
        source="stocks",
        type="market_event",
        title=f"{ticker} {daily['pct']:+.2f}% today ({weekly['pct']:+.2f}% this week)",
        url=f"https://finance.yahoo.com/quote/{ticker}",
        text=_summary(ticker, daily, weekly, catalyst, news_items),
        published_at=daily["now_at"].isoformat(),
        metadata={
            "ticker": ticker,
            "price": daily["now_price"],
            "currency": daily["currency"],
            "daily_pct": daily["pct"],
            "weekly_pct": weekly["pct"],
            "catalyst": catalyst["catalyst"] if catalyst else None,
            "confidence": catalyst["confidence"] if catalyst else None,
        },
        dedup_key=_event_key(ticker, daily),
    )


def _summary(ticker, daily, weekly, catalyst, news_items):
    """The example format from the spec: headline, then explanation /
    evidence / confidence, each clearly labelled so "no catalyst found" can
    never be mistaken for a confirmed one."""
    lines = [f"{ticker} {daily['pct']:+.2f}% today ({weekly['pct']:+.2f}% this week)", ""]

    lines.append("Potential explanation:")
    if catalyst is None:
        lines.append("not checked (no provider, or explanations disabled)")
    elif catalyst["catalyst"] == "none":
        lines.append("no obvious catalyst found")
    else:
        prefix = "Confirmed catalyst" if catalyst["catalyst"] == "confirmed" else "Plausible explanation"
        lines.append(f"{prefix}: {catalyst['explanation']}")
    lines.append("")

    lines.append("Material company news:")
    if news_items:
        lines.extend(f"- {n.title} ({n.url})" for n in news_items)
    else:
        lines.append("none found")
    lines.append("")

    lines.append("Confidence:")
    lines.append(catalyst["confidence"] if catalyst else "n/a")
    return "\n".join(lines)
