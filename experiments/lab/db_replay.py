"""Read-only access to discovery.db for replay experiments.

Everything here opens the DB with mode=ro -- the lab's one deliberate write
path is rate_batch.py, which uses a normal connection but only ever calls
db.add_feedback. Nothing else in the lab may write.
"""
import json
import sqlite3

from lab_common import REPO  # noqa: F401  (imports fix sys.path for discovery.*)

from discovery.models import CandidateItem  # noqa: E402


def open_ro(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def scored_items(conn):
    """Every stored item with its (latest) score, judged interest, and the
    keyword match_score the production run saw. One row per item."""
    return conn.execute(
        """
        SELECT i.id AS item_id, i.source, i.type, i.title, i.text, i.url,
               i.author, i.published_at, i.metadata, i.dedup_key,
               s.final_score, s.personal_relevance, s.novelty, s.depth,
               s.specificity, s.importance, s.surprise, s.confidence, s.reason,
               s.model AS score_model, s.provider AS score_provider,
               n.key AS interest_key, n.id AS interest_id, n.min_score,
               COALESCE(m.match_score, 0.5) AS match_score
        FROM candidate_items i
        JOIN scores s ON s.id = (SELECT MAX(s2.id) FROM scores s2
                                 WHERE s2.item_id = i.id)
        JOIN interests n ON n.id = s.interest_id
        LEFT JOIN item_interests m
               ON m.item_id = i.id AND m.interest_id = s.interest_id
        ORDER BY i.id
        """
    ).fetchall()


def feedback_rows(conn):
    """All feedback verdicts joined to the dimensions of the score they rated,
    for separation and weight-fitting. original_score is the score shown when
    the verdict was given; the joined dimensions are the latest for that
    item+interest pair."""
    return conn.execute(
        """
        SELECT f.verdict, f.note, f.original_score, f.created_at,
               i.title, i.id AS item_id,
               n.key AS interest_key, n.min_score,
               s.final_score, s.personal_relevance, s.novelty, s.depth,
               s.specificity, s.importance, s.surprise
        FROM feedback f
        JOIN candidate_items i ON i.id = f.item_id
        JOIN interests n ON n.id = f.interest_id
        LEFT JOIN scores s ON s.id = (SELECT MAX(s2.id) FROM scores s2
                                      WHERE s2.item_id = f.item_id
                                        AND s2.interest_id = f.interest_id)
        ORDER BY f.id
        """
    ).fetchall()


def spread_sample(rows, n, key=lambda r: r["final_score"]):
    """n rows spanning the range of `key`: sort, then take evenly spaced
    positions. Deterministic, so a variant run re-scores the same items its
    baseline did."""
    rows = sorted(rows, key=key)
    if n <= 0 or not rows:
        return []
    if len(rows) <= n:
        return rows
    if n == 1:
        return [rows[len(rows) // 2]]
    step = (len(rows) - 1) / (n - 1)
    return [rows[round(i * step)] for i in range(n)]


def golden_sample(conn, n=25):
    """Diverse batch for the one-time rating pass: scored items with no
    feedback yet, spread across interests round-robin and across each
    interest's score range."""
    rated = {r["item_id"] for r in feedback_rows(conn)}
    by_interest = {}
    for row in scored_items(conn):
        if row["item_id"] not in rated:
            by_interest.setdefault(row["interest_key"], []).append(row)

    per = max(1, n // max(1, len(by_interest)))
    picked = []
    for rows in by_interest.values():
        picked.extend(spread_sample(rows, per))
    # Top up round-robin if integer division left slack.
    if len(picked) < n:
        chosen = {r["item_id"] for r in picked}
        leftovers = [r for rows in by_interest.values() for r in rows
                     if r["item_id"] not in chosen]
        picked.extend(spread_sample(leftovers, n - len(picked)))
    return sorted(picked, key=lambda r: r["item_id"])[:n]


def to_candidate(row):
    """Rebuild the CandidateItem the scorer originally saw."""
    try:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    except (TypeError, ValueError):
        metadata = {}
    return CandidateItem(
        id=row["item_id"],
        source=row["source"],
        type=row["type"],
        title=row["title"],
        url=row["url"],
        text=row["text"] or "",
        author=row["author"],
        published_at=row["published_at"],
        metadata=metadata,
        dedup_key=row["dedup_key"],
    )
