"""The interests list view's numbers: one funnel row per interest.

collected -> scored -> above bar -> notified, over a selectable window, plus a
daily above-bar sparkline, the feedback tally, and the offers.py lifecycle
state that says whether an interest is auto-paused and revivable.

This is a pure read layer over a read-only connection -- it is separate from
observatory/db.py on purpose: that module's interests tab is a paginated
row-lister capped at MAX_LIMIT=50, while this answers "how is every interest
doing?" for the whole set in one request, and adding a fifth shape of query
to a 1,200-line module that a concurrent session is also editing buys nothing.

The definitions are the ones the design measured with, so a number here can be
compared against MEASUREMENTS.md directly:

  collected  candidate_items whose `origin_interest` is this key -- what the
             collectors fetched FOR it. (Not item_interests: 97% of items
             match >=2 interests, so match-based "collected" would count the
             same item for a dozen interests.)
  scored     scores attributed to this interest by the scorer.
  above_bar  those scores whose final_score cleared the interest's own
             min_score -- the number the bar preview tunes.
  notified   successful notifications for those scores.
"""
import re

from discovery import db as ddb
from discovery import offers

# ?window=7d|30d|45d|90d|all -- a short, closed vocabulary rather than a free
# number, so a typo is a 400 rather than a silently different window.
WINDOWS = {"7d": 7, "14d": 14, "30d": 30, "45d": 45, "90d": 90, "all": None}
DEFAULT_WINDOW = "7d"
_WINDOW_RE = re.compile(r"^(\d{1,3})d$")

# Sparkline resolution: at most this many daily buckets, so ?window=all does
# not return a thousand-point array nobody can render.
MAX_SPARK_DAYS = 90


def parse_window(value):
    """'7d' -> (7, '7d'); 'all' -> (None, 'all'). Raises ValueError on
    anything else."""
    value = (value or DEFAULT_WINDOW).strip().lower()
    if value in WINDOWS:
        return WINDOWS[value], value
    match = _WINDOW_RE.match(value)
    if match and 1 <= int(match.group(1)) <= 365:
        return int(match.group(1)), value
    raise ValueError(
        f"unknown window {value!r} -- use one of {sorted(WINDOWS)} or Nd (1-365)"
    )


def _since(days):
    return None if days is None else ddb.ago(days * 86400)


def _counts(conn, sql, params):
    return {row["key"]: row["n"] for row in conn.execute(sql, params).fetchall()}


def interest_stats(conn, window=DEFAULT_WINDOW, include_inactive=True):
    """Every interest with its funnel. Returns::

        {"window": "7d", "window_days": 7|None, "generated_at": str,
         "totals": {"interests": n, "active": n, "collected": n, "scored": n,
                    "above_bar": n, "notified": n, "dead_weight": n,
                    "auto_paused": n},
         "interests": [row, ...]}          # worst converters last

    One row::

        {"key","title","layer","active","lifecycle","min_score","parent_key",
         "sources": [str], "positive_signals": n, "negative_signals": n,
         "collected","scored","above_bar","notified": int,
         "above_bar_rate": float|None,     # above_bar/scored, None when scored=0
         "feedback": {"fire":n,"up":n,"down":n,"trash":n},
         "sparkline": [{"date": "YYYY-MM-DD", "above_bar": n}, ...],
         "last_above_bar_at": str|None,
         "silence_days": int|None,         # offers.py's own clock
         "dead_weight": bool,              # collected>0 and above_bar==0
         "auto_paused": bool, "decaying": bool, "retired": bool,
         "revivable": bool}                # one click away from active again
    """
    days, label = parse_window(window)
    since = _since(days)

    # SELECT * plus _opt() below, rather than naming the columns: `lifecycle`
    # arrives with db.init()'s ALTER pass (offers, PR H) and `synced_at` with
    # interest_sync.migrate() (PR I), which runs on demand from a sync rather
    # than from init. A database that has been init'd but never synced is
    # therefore missing one of them, and this connection is read-only -- it
    # cannot add it. Degrading to a null beats a 500 on the list view.
    rows = conn.execute(
        "SELECT * FROM interests"
        + ("" if include_inactive else " WHERE active = 1")
        + " ORDER BY key ASC"
    ).fetchall()

    collected = _counts(conn, f"""
        SELECT origin_interest AS key, COUNT(*) AS n FROM candidate_items
        WHERE origin_interest IS NOT NULL {'AND first_seen_at >= ?' if since else ''}
        GROUP BY origin_interest
    """, (since,) if since else ())

    scored = _counts(conn, f"""
        SELECT i.key AS key, COUNT(*) AS n FROM scores s JOIN interests i ON i.id = s.interest_id
        {'WHERE s.created_at >= ?' if since else ''}
        GROUP BY i.key
    """, (since,) if since else ())

    above = _counts(conn, f"""
        SELECT i.key AS key, COUNT(*) AS n FROM scores s JOIN interests i ON i.id = s.interest_id
        WHERE s.final_score >= i.min_score {'AND s.created_at >= ?' if since else ''}
        GROUP BY i.key
    """, (since,) if since else ())

    notified = _counts(conn, f"""
        SELECT i.key AS key, COUNT(*) AS n FROM notifications n
        JOIN scores s ON s.id = n.score_id JOIN interests i ON i.id = s.interest_id
        WHERE n.ok = 1 {'AND n.sent_at >= ?' if since else ''}
        GROUP BY i.key
    """, (since,) if since else ())

    last_above = {
        r["key"]: r["ts"] for r in conn.execute("""
            SELECT i.key AS key, MAX(s.created_at) AS ts FROM scores s
            JOIN interests i ON i.id = s.interest_id
            WHERE s.final_score >= i.min_score GROUP BY i.key
        """).fetchall()
    }

    feedback = {}
    for r in conn.execute(f"""
        SELECT i.key AS key, f.verdict AS verdict, COUNT(*) AS n FROM feedback f
        JOIN interests i ON i.id = f.interest_id
        {'WHERE f.created_at >= ?' if since else ''}
        GROUP BY i.key, f.verdict
    """, (since,) if since else ()).fetchall():
        feedback.setdefault(r["key"], {})[r["verdict"]] = r["n"]

    spark = _sparklines(conn, since)

    out = []
    for row in rows:
        key = row["key"]
        lifecycle = _opt(row, "lifecycle") or offers.ACTIVE
        n_scored = scored.get(key, 0)
        n_above = above.get(key, 0)
        n_collected = collected.get(key, 0)
        out.append({
            "key": key,
            "title": row["title"],
            "layer": row["layer"],
            "active": bool(row["active"]),
            "lifecycle": lifecycle,
            "min_score": row["min_score"],
            "parent_key": _opt(row, "parent_key"),
            # interests.synced_at is interest_sync's column (PR I): when the
            # file and the DB last agreed. The editor shows it as "last
            # synced", and a drift check compares it against the file mtime.
            "synced_at": _opt(row, "synced_at"),
            "sources": _json_list(row["sources"]),
            "positive_signals": len(_json_list(row["positive_signals"])),
            "negative_signals": len(_json_list(row["negative_signals"])),
            "collected": n_collected,
            "scored": n_scored,
            "above_bar": n_above,
            "notified": notified.get(key, 0),
            "above_bar_rate": (n_above / n_scored) if n_scored else None,
            "feedback": {v: feedback.get(key, {}).get(v, 0)
                         for v in ("fire", "up", "down", "trash")},
            "sparkline": spark.get(key, []),
            "last_above_bar_at": last_above.get(key),
            "silence_days": offers.silence_days(conn, key),
            # The design's own dead-weight test, and the reason the sweep
            # exists: work went in (items were collected) and nothing came out.
            "dead_weight": bool(n_collected and not n_above),
            "auto_paused": lifecycle == offers.PAUSED,
            "decaying": lifecycle == offers.DECAYING,
            "retired": lifecycle == offers.RETIRED,
            "revivable": lifecycle in (offers.PAUSED, offers.DECAYING),
        })

    # Worst converters last: the list view's job is to make dead weight
    # visible, and sorting by yield does that without a filter.
    out.sort(key=lambda r: (-(r["above_bar"]), -r["collected"], r["key"]))
    totals = {
        "interests": len(out),
        "active": sum(1 for r in out if r["active"]),
        "collected": sum(r["collected"] for r in out),
        "scored": sum(r["scored"] for r in out),
        "above_bar": sum(r["above_bar"] for r in out),
        "notified": sum(r["notified"] for r in out),
        "dead_weight": sum(1 for r in out if r["dead_weight"]),
        "auto_paused": sum(1 for r in out if r["auto_paused"]),
    }
    return {"window": label, "window_days": days, "generated_at": ddb.now(),
            "totals": totals, "interests": out}


def _sparklines(conn, since):
    """{key: [{date, above_bar}]} -- daily above-bar counts, oldest first,
    only for days that actually have one (a UI drawing a sparkline supplies
    its own zeros; shipping 90 zeros per interest would dwarf the payload)."""
    sql = """
        SELECT i.key AS key, substr(s.created_at, 1, 10) AS day, COUNT(*) AS n
        FROM scores s JOIN interests i ON i.id = s.interest_id
        WHERE s.final_score >= i.min_score {window}
        GROUP BY i.key, day ORDER BY day ASC
    """.format(window="AND s.created_at >= ?" if since else "")
    spark = {}
    for row in conn.execute(sql, (since,) if since else ()).fetchall():
        bucket = spark.setdefault(row["key"], [])
        if len(bucket) < MAX_SPARK_DAYS:
            bucket.append({"date": row["day"], "above_bar": row["n"]})
    return spark


def _opt(row, column):
    """A column that may not exist yet on this database (see interest_stats).
    sqlite3.Row raises IndexError for an absent name -- the same shape
    interest_sync._lifecycle guards against."""
    try:
        return row[column]
    except IndexError:
        return None


def _json_list(text):
    import json
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
