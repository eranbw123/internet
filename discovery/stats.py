"""`python -m app stats` -- is this thing actually finding anything I care about?

Read-only report over a trailing window. Three questions it exists to answer
after a week of running:

  1. Where does the funnel lose candidates, and is the LLM seeing a sane number
     of them? (metrics table -- dedup and pre-filter destroy their candidates,
     so the funnel cannot be reconstructed from candidate_items afterwards.)
  2. Do the pushes land? (feedback rates, and average score per verdict -- if
     🔥 items don't score higher than 🗑 ones, the score is not measuring what
     it is supposed to and the weights need work.)
  3. What did it cost? (llm_usage, priced below.)

Every figure is a plain SQL aggregate; nothing here writes.
"""
from datetime import date, timedelta

from . import health

# USD per 1M tokens (input, output), from public list pricing. A model that is
# not here still gets its token counts reported -- it is just left out of the
# dollar total rather than guessed at.
TOKEN_PRICES = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
WEB_SEARCH_PER_1K = 10.00   # server-side web search, billed per request

VERDICT_LABELS = {"fire": "🔥 fire", "up": "👍 up", "down": "👎 down", "trash": "🗑 trash"}
NEGATIVE_VERDICTS = ("down", "trash")


def since_day(days):
    return (date.today() - timedelta(days=days - 1)).isoformat()


def report(conn, days=7, cfg=None):
    """The whole report as one string, so callers just print it. `cfg` is
    optional -- pass it (as `python -m app stats` does) to also get the
    HEALTH section (job staleness, pending/abandoned sends, today's ops
    counters); without it the report is the funnel/feedback/cost figures
    only, same as before this section existed."""
    since = since_day(days)
    lines = [
        f"Discovery stats -- last {days} day(s), from {since}",
        "",
    ]
    lines += _funnel(conn, since, days)
    lines += _per_interest(conn, since)
    lines += _exploration(conn, since, cfg)
    lines += _feedback(conn, since)
    lines += _score_by_verdict(conn, since)
    lines += _cost(conn, since, days)
    if cfg is not None:
        # No provider passed: a read-only report must not pay for a live
        # CDP/API reachability check just to print itself -- `python -m app
        # health` is the command for a real verdict.
        lines.append(health.format_report(health.check(conn, cfg)))
        lines.append("")
    return "\n".join(lines)


# --- funnel ------------------------------------------------------------------

def _funnel(conn, since, days):
    rows = conn.execute(
        "SELECT name, SUM(count) n FROM metrics WHERE day >= ? GROUP BY name", (since,)
    ).fetchall()
    m = {row["name"]: row["n"] for row in rows}

    collected = m.get("collected", 0)
    survived = collected - m.get("duplicate", 0) - m.get("filtered", 0)
    to_llm = m.get("scored", 0) + m.get("errors", 0)
    # Restricted to owner interests -- a derived/inferred row's notification
    # is exploration, not exploitation quality; see _exploration() below.
    notified = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM notifications x
        JOIN scores s     ON s.id = x.score_id
        JOIN interests n  ON n.id = s.interest_id
        WHERE x.ok = 1 AND x.sent_at >= ? AND n.layer = 'owner'
        """,
        (since,),
    )

    lines = ["FUNNEL"]
    lines.append(_row("candidates collected", collected, collected))
    lines.append(_row("  - already seen (dedup)", m.get("duplicate", 0), collected))
    lines.append(_row("  - dropped by cheap filter", m.get("filtered", 0), collected))
    lines.append(_row("survived cheap filtering", survived, collected))
    lines.append(_row("sent to the LLM", to_llm, collected))
    if m.get("errors"):
        lines.append(_row("  of which failed to score", m["errors"], collected))
    if m.get("deferred"):
        lines.append(
            _row("  deferred (over budget)", m["deferred"], collected)
            + "  raise DISCOVERY_MAX_SCORES if this is not draining"
        )
    lines.append(_row("notifications sent", notified, collected) + f"   {notified / days:.1f}/day")
    lines.append("")
    return lines


# --- per interest ------------------------------------------------------------

def _per_interest(conn, since):
    """Owner interests only -- a derived/inferred row's send rate is
    exploration, reported separately by _exploration() below so it can never
    quietly dilute the exploitation numbers this table exists to show."""
    return _per_interest_rows(conn, since, owner=True)


def _per_interest_rows(conn, since, owner):
    rows = conn.execute(
        f"""
        SELECT n.key,
               COUNT(x.id) AS sent,
               (SELECT COUNT(*) FROM scores s2 WHERE s2.interest_id = n.id
                  AND s2.created_at >= ?) AS scored
        FROM interests n
        LEFT JOIN scores s ON s.interest_id = n.id
        LEFT JOIN notifications x ON x.score_id = s.id AND x.ok = 1 AND x.sent_at >= ?
        WHERE n.active = 1 AND n.layer {'=' if owner else '!='} 'owner'
        GROUP BY n.id ORDER BY sent DESC, n.key
        """,
        (since, since),
    ).fetchall()
    if not rows:
        return []

    title = "NOTIFICATIONS PER INTEREST" if owner else "NOTIFICATIONS PER DERIVED INTEREST"
    lines = [title, f"{'interest':<26}{'sent':>6}{'scored':>8}{'rate':>8}"]
    for row in rows:
        rate = f"{row['sent'] / row['scored'] * 100:.0f}%" if row["scored"] else "-"
        lines.append(f"{row['key'][:25]:<26}{row['sent']:>6}{row['scored']:>8}{rate:>8}")
    lines.append("")
    return lines


# --- exploration (step-10) ----------------------------------------------------

def _exploration(conn, since, cfg):
    """Derived/inferred-layer lane -- discovery/interest_state.py's dynamic
    interests -- kept fully separate from FUNNEL/NOTIFICATIONS PER INTEREST
    above so exploitation quality is never diluted by exploration noise.
    Printed only when there's something to show (an explore_* metric row, a
    non-owner interest row, or the flag on); a report with none of that is
    byte-identical to before this section existed.

    The two halves below read differently on purpose right now: "explore:
    notified" only ever grows from pipeline.deliver()'s ALERT path (the
    only caller that passes lane_counts today); a DISCOVERY item sent via
    `digest` -> send_digest() bumps no funnel metric at all (true for
    exploitation too, unchanged from before this step). The per-derived-
    interest table below it is queried straight off the notifications
    table, so it shows every send regardless of channel. Until digest-time
    metrics exist, "explore: notified" undercounts what the table shows."""
    m = {
        row["name"]: row["n"]
        for row in conn.execute(
            "SELECT name, SUM(count) n FROM metrics WHERE day >= ? GROUP BY name", (since,)
        ).fetchall()
        if row["name"].startswith("explore_")
    }
    layers = conn.execute(
        "SELECT layer, COUNT(*) n FROM interests WHERE layer != 'owner' GROUP BY layer ORDER BY layer"
    ).fetchall()
    if not m and not layers and not (cfg and cfg.dynamic_interests):
        return []

    lines = ["EXPLORATION"]
    if layers:
        lines.append("interests by layer: " + ", ".join(f"{r['layer']}={r['n']}" for r in layers))
    for stage in ("scored", "deferred", "errors", "notified"):
        lines.append(_row(f"explore: {stage}", m.get(f"explore_{stage}", 0), None))
    lines.append("")
    lines += _per_interest_rows(conn, since, owner=False)
    return lines


# --- feedback ----------------------------------------------------------------

def _feedback(conn, since):
    rows = conn.execute(
        "SELECT verdict, COUNT(*) n FROM feedback WHERE created_at >= ? GROUP BY verdict",
        (since,),
    ).fetchall()
    counts = {row["verdict"]: row["n"] for row in rows}
    total = sum(counts.values())
    # Deliberately NOT restricted to layer='owner' like _funnel()'s notified
    # scalar above -- the 🔥/👍/👎/🗑 buttons work the same on a derived
    # notification as an owner one, so "% of sent" is honestly a rate over
    # every push that could have been rated, exploit and explore alike.
    sent = _scalar(
        conn, "SELECT COUNT(*) FROM notifications WHERE ok = 1 AND sent_at >= ?", (since,)
    )

    lines = ["FEEDBACK"]
    if not total:
        lines += ["nothing rated yet -- the 🔥/👍/👎/🗑 buttons need `python -m app listen`", ""]
        return lines

    rated = f"{total / sent * 100:.0f}% of sent" if sent else "no sends in window"
    lines.append(f"{total} rated ({rated})")
    for verdict, label in VERDICT_LABELS.items():
        lines.append(_row(label, counts.get(verdict, 0), total))
    negative = sum(counts.get(v, 0) for v in NEGATIVE_VERDICTS)
    lines.append(_row("negative (👎 + 🗑)", negative, total))
    lines.append("")
    return lines


def _score_by_verdict(conn, since):
    """The single most useful number here: if 🔥 items don't score meaningfully
    above 🗑 ones, `models.WEIGHTS` is not tracking what actually interests
    you, and no amount of collecting will fix that."""
    rows = conn.execute(
        """
        SELECT verdict, COUNT(*) n, AVG(original_score) avg_score
        FROM feedback
        WHERE created_at >= ? AND original_score IS NOT NULL
        GROUP BY verdict
        """,
        (since,),
    ).fetchall()
    if not rows:
        return []

    by_verdict = {row["verdict"]: row for row in rows}
    lines = ["AVERAGE SCORE BY FEEDBACK"]
    for verdict, label in VERDICT_LABELS.items():
        row = by_verdict.get(verdict)
        if row:
            lines.append(f"{label:<26}{row['avg_score']:>6.2f}   (n={row['n']})")
    lines.append("")
    return lines


# --- cost --------------------------------------------------------------------

def _cost(conn, since, days):
    rows = conn.execute(
        """
        SELECT provider, model, SUM(calls) calls, SUM(input_tokens) tin,
               SUM(output_tokens) tout, SUM(web_searches) searches
        FROM llm_usage WHERE day >= ? GROUP BY provider, model ORDER BY model
        """,
        (since,),
    ).fetchall()
    lines = ["ESTIMATED LLM / API COST"]
    if not rows:
        return lines + ["no usage recorded in this window", ""]

    total, unpriced, priced_rows = 0.0, [], 0
    for row in rows:
        # claude_chat rides the claude.ai subscription: no token counts on the
        # wire and no per-call billing, so pricing it in dollars would be a
        # fabrication. Calls are still worth seeing.
        if row["provider"] == "claude_chat":
            lines.append(
                f"{row['model']} (claude_chat): {row['calls']} calls via the claude.ai"
                " session -- covered by the subscription; tokens not reported,"
                " dollar cost not applicable"
            )
            continue
        priced_rows += 1
        price = TOKEN_PRICES.get(row["model"])
        search_cost = row["searches"] / 1000 * WEB_SEARCH_PER_1K
        lines.append(
            f"{row['model']} ({row['provider']}): {row['calls']} calls, "
            f"{row['tin'] / 1e6:.2f}M in / {row['tout'] / 1e6:.2f}M out, "
            f"{row['searches']} web searches"
        )
        if price is None:
            unpriced.append(row["model"])
            lines.append(f"{'':4}tokens: no list price on record; web search ${search_cost:.2f}")
            total += search_cost
            continue
        token_cost = row["tin"] / 1e6 * price[0] + row["tout"] / 1e6 * price[1]
        lines.append(
            f"{'':4}tokens ${token_cost:.2f} + web search ${search_cost:.2f}"
            f" = ${token_cost + search_cost:.2f}"
        )
        total += token_cost + search_cost

    if priced_rows:
        # Also deliberately lane-agnostic (see _feedback()'s comment above):
        # spend isn't tracked per interest/lane at all, so "per notification"
        # is honestly total spend over every push, not an owner-only rate.
        notified = _scalar(
            conn, "SELECT COUNT(*) FROM notifications WHERE ok = 1 AND sent_at >= ?", (since,)
        )
        per_push = f", ${total / notified:.2f} per notification" if notified else ""
        lines.append(f"TOTAL ${total:.2f}  (${total / days:.2f}/day{per_push})")
        if unpriced:
            lines.append(f"excludes token spend for: {', '.join(sorted(set(unpriced)))}")
    else:
        lines.append("no API-billed usage in this window")
    lines.append("")
    return lines


# --- formatting --------------------------------------------------------------

def _row(label, value, of):
    share = f"{value / of * 100:5.1f}%" if of else "     -"
    return f"{label:<28}{value:>6}  {share}"


def _scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0] or 0
