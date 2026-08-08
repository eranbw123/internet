"""Stock price moves, reusing watch.py's Yahoo Finance fetch.

Only interesting moves become candidates: a flat day is noise, so anything
below `min_change_pct` is dropped before scoring rather than spending an LLM
call on it. Scoring still decides whether the move is worth a push, because
"NBIS -4%" matters to one interest and not another.
"""

import watch  # repo-root module; only importable when run from the repo root

from ..models import CandidateItem


def collect(interest, cfg, provider=None):   # provider unused: no LLM needed here
    opts = interest.source_config.get("stocks", {})
    tickers = opts.get("tickers", [])
    schedule = opts.get("schedule", "daily")
    threshold = float(opts.get("min_change_pct", 0))

    items = []
    for ticker in tickers:
        change = watch.price_change(ticker, schedule)
        if abs(change["pct"]) < threshold:
            continue
        day = change["now_at"].date().isoformat()
        items.append(
            CandidateItem(
                source="stocks",
                type="price_move",
                title=f"{ticker} {change['pct']:+.2f}% ({change['label']})",
                url=f"https://finance.yahoo.com/quote/{ticker}",
                text=watch.format_line(change),
                published_at=change["now_at"].isoformat(),
                metadata={
                    "ticker": ticker,
                    "pct": change["pct"],
                    "price": change["now_price"],
                    "currency": change["currency"],
                    "schedule": schedule,
                },
                # The same ticker moves again tomorrow, so the URL alone would
                # collapse every day's move into one row.
                dedup_key=f"{ticker}:{schedule}:{day}",
            )
        )
    return items
