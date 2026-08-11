"""Terminal-only teaching interface: ranks already-scored, not-yet-labeled
items by expected information value and records labels through the existing
feedback store (db.add_feedback). No new table, no LLM/provider call --
`teach` stays in the no-provider command family with `items`/`feedback`/
`stats`.

Ranking rationale (PROJECT_STATE.md "engine lab", proposals 003/004): notify
flips track bar proximity, not scorer variance, and corpus band_density is
only .148 -- a near-bar label teaches more than one 0.4 from its bar.
`information`, in [0,1], is WEIGHTS-combined from bar_proximity (1.0 at
gap=0, decaying to 0.0 at gap>=BAND_WIDTH), uncertainty (1 - confidence),
and scarcity (1 / (1 + labels already recorded for that interest), so the
queue spreads across interests). Set from this rationale, not tuned against
the test fixture -- see test_discovery.py's TeachTests docstring.

Skips are session-local: an item skipped this run just isn't labeled, so it
reappears next time build_queue runs -- persisting a skip list would need
its own storage for a purely cosmetic ordering nicety, out of scope here.
"""
from . import db, notify
from .notify import FEEDBACK_VERDICTS, print_safe

# Half-width of the "near the bar" band; also queue_metrics' band definition.
BAND_WIDTH = 0.08
WEIGHTS = {"bar_proximity": 0.5, "uncertainty": 0.3, "scarcity": 0.2}

_POOL_SQL = """
    SELECT s.id AS score_id, s.item_id, s.interest_id, s.final_score, s.confidence,
           s.reason, s.why_better_than_generic,
           i.title AS item_title, i.url AS item_url,
           n.key AS interest_key, n.title AS interest_title, n.min_score
    FROM scores s
    JOIN candidate_items i ON i.id = s.item_id
    JOIN interests n ON n.id = s.interest_id
    WHERE n.active = 1
      AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.item_id = s.item_id)
"""


def information(gap, confidence, interest_id, label_counts):
    proximity = max(0.0, 1.0 - gap / BAND_WIDTH)
    scarcity = 1.0 / (1.0 + label_counts.get(interest_id, 0))
    total = (
        WEIGHTS["bar_proximity"] * proximity
        + WEIGHTS["uncertainty"] * (1.0 - confidence)
        + WEIGHTS["scarcity"] * scarcity
    )
    return round(max(0.0, min(1.0, total)), 6)


def _pool(conn, interest=None):
    """Active-interest scores with no feedback row yet -- both arms' shared
    input, recomputed (not cached) so repeated calls on an unchanged DB stay
    byte-identical and reflect any label recorded since."""
    sql, params = _POOL_SQL, []
    if interest is not None:
        sql += " AND n.key = ?"
        params.append(interest)
    counts = {
        row["interest_id"]: row["n"] for row in conn.execute(
            "SELECT interest_id, COUNT(*) AS n FROM feedback GROUP BY interest_id"
        ).fetchall()
    }
    out = []
    for row in conn.execute(sql, params).fetchall():
        gap = abs(row["final_score"] - row["min_score"])
        out.append(dict(
            row, gap=gap,
            information=information(gap, row["confidence"], row["interest_id"], counts),
        ))
    return out


def build_queue(conn, limit=10, interest=None):
    """Ranked by information descending; item id descending breaks ties."""
    pool = _pool(conn, interest)
    pool.sort(key=lambda r: (-r["information"], -r["item_id"]))
    return pool[:limit]


def baseline_queue(conn, limit=10, interest=None):
    """The honest comparison arm: the same pool, ordered by recency."""
    pool = _pool(conn, interest)
    pool.sort(key=lambda r: -r["score_id"])
    return pool[:limit]


def _arm_stats(rows):
    n = len(rows)
    if n == 0:
        return dict(n=0, band_share=0.0, mean_gap=0.0, mean_confidence=0.0, interests_covered=0)
    band = sum(1 for r in rows if r["gap"] <= BAND_WIDTH)
    return dict(
        n=n, band_share=band / n,
        mean_gap=sum(r["gap"] for r in rows) / n,
        mean_confidence=sum(r["confidence"] for r in rows) / n,
        interests_covered=len({r["interest_key"] for r in rows}),
    )


def queue_metrics(conn, limit=10, interest=None):
    """build_queue vs baseline_queue over the same pool/size -- both arms
    reported unconditionally, even if the baseline wins."""
    queue, baseline = build_queue(conn, limit, interest), baseline_queue(conn, limit, interest)
    q, b = _arm_stats(queue), _arm_stats(baseline)
    return {
        "pool_size": len(_pool(conn, interest)),
        "n": len(queue),
        "queue": q,
        "baseline": b,
        "band_lift": (q["band_share"] / b["band_share"]) if b["band_share"] > 0 else None,
    }


def format_item(row):
    return (
        f"[{row['interest_key']}] {row['final_score']:.2f} vs bar {row['min_score']:.2f}"
        f" (gap {row['gap']:.2f})  confidence={row['confidence']:.2f}"
        f"  info={row['information']:.2f}\n"
        f"  {row['reason']}\n  {row['item_title']}\n  {row['item_url']}"
    )


def format_queue(rows):
    if not rows:
        return "nothing to teach on -- no unlabeled scored items match"
    lines = [f"{len(rows)} queued item(s):"]
    for row in rows:
        lines += ["", format_item(row)]
    return "\n".join(lines)


def format_metrics(metrics):
    if metrics["pool_size"] == 0:
        return "nothing to teach on -- no unlabeled scored items match"
    lines = [f"pool_size={metrics['pool_size']}  n={metrics['n']}"]
    for arm in ("queue", "baseline"):
        a = metrics[arm]
        lines.append(
            f"{arm:8s} band_share={a['band_share']:.3f}  mean_gap={a['mean_gap']:.3f}  "
            f"mean_confidence={a['mean_confidence']:.3f}  interests_covered={a['interests_covered']}"
        )
    lift = metrics["band_lift"]
    lines.append(f"band_lift={lift:.3f}" if lift is not None else "band_lift=n/a (baseline band_share is 0)")
    return "\n".join(lines)


def run_interactive(conn, limit=10, interest=None, read=input):
    rows = build_queue(conn, limit, interest)
    if not rows:
        print_safe("nothing to teach on -- no unlabeled scored items match")
        return 0
    count, codes = 0, "/".join(FEEDBACK_VERDICTS)
    for row in rows:
        print_safe(format_item(row))
        while True:
            try:
                token = read(f"  [{codes}/s/q] > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print_safe(f"\nstopped -- {count} label(s) recorded")
                return 0
            if token == "q":
                print_safe(f"stopped -- {count} label(s) recorded")
                return 0
            if token == "s":
                break
            if token in FEEDBACK_VERDICTS:
                db.add_feedback(conn, row["item_id"], row["interest_id"], token, "", row["final_score"])
                count += 1
                break
            print_safe(f"  unrecognized {token!r} -- {codes}/s(kip)/q(uit)")
    print_safe(f"done -- {count} label(s) recorded")
    return 0


def run_send(conn, cfg, limit=5, interest=None, dry_run=False):
    """Pushes the top of the queue to Telegram via the exact
    format_message/feedback_keyboard/send path pipeline.py uses, so labels
    come back through the existing listen/listen --drain flow with no new
    callback format. Never calls db.record_notification -- that table drives
    delivery accounting (pending/abandoned counts, the digest), and a teach
    push must not be counted as a production notification."""
    rows = build_queue(conn, limit, interest)
    if not rows:
        print_safe("nothing to send -- no unlabeled scored items match")
        return 0
    sent = 0
    for row in rows:
        interest_obj = db.interest_by_key(conn, row["interest_key"])
        item_obj = db.get_item(conn, row["item_id"])
        text = notify.format_message(
            interest_obj, item_obj, row["final_score"], row["reason"],
            row["why_better_than_generic"], row["confidence"],
        )
        keyboard = notify.feedback_keyboard(row["score_id"], cfg.observatory_base_url)
        ok = notify.send(cfg, text, reply_markup=keyboard, dry_run=dry_run)
        sent += 1 if ok else 0
    print_safe(f"sent {sent} teaching prompt(s)")
    return sent
