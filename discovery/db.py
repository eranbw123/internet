"""SQLite access. Thin functions over sqlite3 -- no ORM, no query builder.

JSON-shaped columns (signals, metadata, source_config, matched_terms) are
stored as text and decoded here so callers only ever see Python values.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import DIMENSIONS, CandidateItem, Interest

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn):
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


# --- interests ---------------------------------------------------------------

def upsert_interest(conn, interest):
    """Insert or update by `key`. interests.json is the source of truth, so a
    re-load overwrites the stored copy rather than merging."""
    conn.execute(
        """
        INSERT INTO interests
            (key, title, description, positive_signals, negative_signals,
             min_score, sources, source_config, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(key) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            positive_signals = excluded.positive_signals,
            negative_signals = excluded.negative_signals,
            min_score = excluded.min_score,
            sources = excluded.sources,
            source_config = excluded.source_config,
            active = 1
        """,
        (
            interest.key,
            interest.title,
            interest.description,
            json.dumps(interest.positive_signals),
            json.dumps(interest.negative_signals),
            interest.min_score,
            json.dumps(interest.sources),
            json.dumps(interest.source_config),
        ),
    )
    conn.commit()


def active_interests(conn):
    rows = conn.execute("SELECT * FROM interests WHERE active = 1 ORDER BY id").fetchall()
    return [_row_to_interest(r) for r in rows]


def interest_by_key(conn, key):
    row = conn.execute("SELECT * FROM interests WHERE key = ?", (key,)).fetchone()
    return _row_to_interest(row) if row else None


def _row_to_interest(row):
    return Interest(
        id=row["id"],
        key=row["key"],
        title=row["title"],
        description=row["description"],
        positive_signals=json.loads(row["positive_signals"]),
        negative_signals=json.loads(row["negative_signals"]),
        min_score=row["min_score"],
        sources=json.loads(row["sources"]),
        source_config=json.loads(row["source_config"]),
    )


# --- items -------------------------------------------------------------------

def insert_item(conn, item):
    """Insert if new. Returns the row id either way, so a stored-but-unscored
    item from a previous run is picked up rather than duplicated."""
    conn.execute(
        """
        INSERT OR IGNORE INTO candidate_items
            (source, type, title, text, url, author, published_at, metadata,
             origin_interest, dedup_key, url_hash, title_hash, content_hash,
             first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.source, item.type, item.title, item.text, item.url, item.author,
            item.published_at, json.dumps(item.metadata), item.origin_interest,
            item.key(), item.url_hash, item.title_hash, item.content_hash, now(),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM candidate_items WHERE source = ? AND dedup_key = ?",
        (item.source, item.key()),
    ).fetchone()
    return row["id"]


def get_item(conn, item_id):
    row = conn.execute("SELECT * FROM candidate_items WHERE id = ?", (item_id,)).fetchone()
    return _row_to_item(row) if row else None


def find_item_by_hash(conn, column, value):
    """Look up an existing item by one of the dedup hashes."""
    if not value:
        return None
    row = conn.execute(
        f"SELECT * FROM candidate_items WHERE {column} = ? ORDER BY id LIMIT 1", (value,)
    ).fetchone()
    return _row_to_item(row) if row else None


def _row_to_item(row):
    return CandidateItem(
        id=row["id"],
        source=row["source"],
        type=row["type"],
        title=row["title"],
        text=row["text"],
        url=row["url"],
        author=row["author"],
        published_at=row["published_at"],
        metadata=json.loads(row["metadata"]),
        origin_interest=row["origin_interest"],
        dedup_key=row["dedup_key"],
        url_hash=row["url_hash"],
        title_hash=row["title_hash"],
        content_hash=row["content_hash"],
    )


def set_prefilter(conn, item_id, ok, reason):
    conn.execute(
        "UPDATE candidate_items SET prefilter_ok = ?, prefilter_reason = ? WHERE id = ?",
        (1 if ok else 0, reason, item_id),
    )
    conn.commit()


def is_scored(conn, item_id):
    row = conn.execute("SELECT 1 FROM scores WHERE item_id = ?", (item_id,)).fetchone()
    return row is not None


# --- interest matches --------------------------------------------------------

def save_matches(conn, item_id, matches):
    """matches: [(interest, match_score, matched_terms)]"""
    conn.executemany(
        """
        INSERT OR REPLACE INTO item_interests
            (item_id, interest_id, match_score, matched_terms, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (item_id, interest.id, score, json.dumps(terms), now())
            for interest, score, terms in matches
        ],
    )
    conn.commit()


def matched_interest_ids(conn, item_id):
    rows = conn.execute(
        "SELECT interest_id FROM item_interests WHERE item_id = ? ORDER BY match_score DESC",
        (item_id,),
    ).fetchall()
    return [r["interest_id"] for r in rows]


# --- scores / notifications / feedback ---------------------------------------

def save_score(conn, score):
    dims = [score.dimensions.get(name, 0.0) for name in DIMENSIONS]
    conn.execute(
        f"""
        INSERT OR REPLACE INTO scores
            (item_id, interest_id, {', '.join(DIMENSIONS)}, final_score, confidence,
             reason, why_better_than_generic, provider, model, created_at)
        VALUES (?, ?, {', '.join('?' * len(DIMENSIONS))}, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            score.item_id, score.interest_id, *dims, score.final_score, score.confidence,
            score.reason, score.why_better_than_generic, score.provider, score.model, now(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM scores WHERE item_id = ?", (score.item_id,)).fetchone()
    return row["id"]


def delete_score(conn, item_id):
    """Used by `score --force` -- notifications cascade off score_id, so drop
    those first rather than leaving orphans."""
    conn.execute(
        "DELETE FROM notifications WHERE score_id IN (SELECT id FROM scores WHERE item_id = ?)",
        (item_id,),
    )
    conn.execute("DELETE FROM scores WHERE item_id = ?", (item_id,))
    conn.commit()


def pending_notifications(conn):
    """Scores at or above their interest's bar that were never sent."""
    return conn.execute(
        """
        SELECT s.id AS score_id, s.item_id, s.interest_id, s.final_score,
               s.confidence, s.reason, s.why_better_than_generic
        FROM scores s
        JOIN interests n ON n.id = s.interest_id
        WHERE s.final_score >= n.min_score
          AND n.active = 1
          AND NOT EXISTS (SELECT 1 FROM notifications x WHERE x.score_id = s.id)
        ORDER BY s.final_score DESC
        """
    ).fetchall()


def record_notification(conn, score_id, channel, ok):
    conn.execute(
        "INSERT OR REPLACE INTO notifications (score_id, channel, sent_at, ok) VALUES (?, ?, ?, ?)",
        (score_id, channel, now(), 1 if ok else 0),
    )
    conn.commit()


def add_feedback(conn, item_id, interest_id, verdict, note=""):
    conn.execute(
        "INSERT INTO feedback (item_id, interest_id, verdict, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (item_id, interest_id, verdict, note, now()),
    )
    conn.commit()


def recent_feedback(conn, interest_id, limit=10):
    """Latest verdicts for one interest, newest first -- fed to the scorer as
    worked examples so thumbs-down items stop coming back."""
    return conn.execute(
        """
        SELECT f.verdict, f.note, i.title
        FROM feedback f JOIN candidate_items i ON i.id = f.item_id
        WHERE f.interest_id = ?
        ORDER BY f.id DESC LIMIT ?
        """,
        (interest_id, limit),
    ).fetchall()
