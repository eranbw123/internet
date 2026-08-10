"""SQLite access. Thin functions over sqlite3 -- no ORM, no query builder.

JSON-shaped columns (signals, metadata, source_config, matched_terms) are
stored as text and decoded here so callers only ever see Python values.
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import DIMENSIONS, CandidateItem, Interest

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Failed-send retry policy: a not-ok notification may be retried once its last
# attempt is older than RESEND_FAILED_AFTER_SECONDS, at most MAX_SEND_ATTEMPTS
# times in total. Small on purpose -- a persistent failure should give up, not
# queue forever.
MAX_SEND_ATTEMPTS = 3
RESEND_FAILED_AFTER_SECONDS = 15 * 60

# Backlog rescore cool-off: an item whose scoring attempt failed is left alone
# for this long before the backlog pass tries it again.
SCORE_RETRY_SECONDS = 30 * 60


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ago(seconds):
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def today():
    return datetime.now(timezone.utc).date().isoformat()


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Separately scheduled OS tasks (collect/digest/feedback/health) can now
    # overlap on the same discovery.db -- wait out a writer instead of
    # raising "database is locked" the instant two tasks land in the same
    # second.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init(conn):
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # schema.sql is CREATE TABLE IF NOT EXISTS only, so columns added after a
    # DB was created need their own additive ALTERs (no migration framework).
    for table, column, decl in (
        ("notifications", "attempts", "INTEGER NOT NULL DEFAULT 1"),
        ("candidate_items", "score_attempted_at", "TEXT"),
        ("scores", "prompt_hash", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists
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


def seen_dedup_keys(conn, source, prefix=""):
    """Dedup keys this source has already stored, optionally restricted to a
    prefix. Collectors use it to skip work *before* paying for it -- dedup.py
    only rejects a duplicate after the collector has already fetched (and, for
    stocks, explained) it."""
    # LIKE treats _ and % as wildcards, and YouTube video ids routinely contain
    # underscores -- unescaped, "abc_def:" would also match "abcXdef:..." and a
    # never-processed video would be skipped forever.
    escaped = re.sub(r"([\\%_])", r"\\\1", prefix)
    rows = conn.execute(
        "SELECT dedup_key FROM candidate_items"
        " WHERE source = ? AND dedup_key LIKE ? ESCAPE '\\'",
        (source, escaped + "%"),
    ).fetchall()
    return {row["dedup_key"] for row in rows}


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


def mark_score_attempt(conn, item_id):
    """Stamp a failed scoring attempt so the backlog pass can leave the item
    alone for SCORE_RETRY_SECONDS instead of re-failing it every cycle."""
    conn.execute(
        "UPDATE candidate_items SET score_attempted_at = ? WHERE id = ?", (now(), item_id)
    )
    conn.commit()


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
             reason, why_better_than_generic, provider, model, prompt_hash, created_at)
        VALUES (?, ?, {', '.join('?' * len(DIMENSIONS))}, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            score.item_id, score.interest_id, *dims, score.final_score, score.confidence,
            score.reason, score.why_better_than_generic, score.provider, score.model,
            score.prompt_hash, now(),
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


def pending_notifications(conn, max_attempts=MAX_SEND_ATTEMPTS, retry_after_seconds=RESEND_FAILED_AFTER_SECONDS):
    """Scores at or above their interest's bar that still need sending: never
    attempted, or last attempt failed and is old enough to retry (and under
    the attempt cap). A success is final; retries never duplicate it.

    `max_attempts`/`retry_after_seconds` default to this module's constants
    so a caller that doesn't pass them (tests, `score --notify`) keeps the
    old behavior; production goes through pipeline.notification_ready(),
    which passes cfg.send_max_attempts/cfg.send_retry_seconds."""
    return conn.execute(
        """
        SELECT s.id AS score_id, s.item_id, s.interest_id, s.final_score,
               s.confidence, s.reason, s.why_better_than_generic
        FROM scores s
        JOIN interests n ON n.id = s.interest_id
        WHERE s.final_score >= n.min_score
          AND n.active = 1
          AND NOT EXISTS (
              SELECT 1 FROM notifications x
              WHERE x.score_id = s.id
                AND (x.ok = 1 OR x.attempts >= ? OR x.sent_at > ?)
          )
        ORDER BY s.final_score DESC
        """,
        (max_attempts, ago(retry_after_seconds)),
    ).fetchall()


def record_notification(conn, score_id, channel, ok):
    """One row per score. A repeat attempt updates the row and bumps
    `attempts`, so pending_notifications() can cap and pace retries."""
    conn.execute(
        """
        INSERT INTO notifications (score_id, channel, sent_at, ok, attempts)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(score_id) DO UPDATE SET
            channel  = excluded.channel,
            sent_at  = excluded.sent_at,
            ok       = excluded.ok,
            attempts = attempts + 1
        """,
        (score_id, channel, now(), 1 if ok else 0),
    )
    conn.commit()


def add_feedback(conn, item_id, interest_id, verdict, note="", original_score=None):
    conn.execute(
        """
        INSERT INTO feedback (item_id, interest_id, verdict, note, original_score, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (item_id, interest_id, verdict, note, original_score, now()),
    )
    conn.commit()


def score_by_id(conn, score_id):
    """Looked up from a Telegram feedback button's callback_data, which only
    carries the score id -- this is how the listener recovers item_id,
    interest_id, and the score to attribute the feedback to."""
    return conn.execute(
        "SELECT id, item_id, interest_id, final_score FROM scores WHERE id = ?", (score_id,)
    ).fetchone()


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


# --- service state -----------------------------------------------------------
# Durable key/value store (see schema.sql) for the handful of things that
# make a short-lived, overlap-safe invocation resumable: job heartbeats
# (job:<name>:last_ok / last_fail, written by __main__.py and health.py),
# the persisted Telegram getUpdates offset, and health's own alert-dedup
# state (health:last_status / health:last_alert_at).

def state_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM service_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def state_set(conn, key, value):
    conn.execute(
        """
        INSERT INTO service_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, str(value), now()),
    )
    conn.commit()


# --- metrics -----------------------------------------------------------------

def bump(conn, counts):
    """Add `counts` ({name: n}) to today's funnel counters. Called once per
    cycle with an accumulated Counter, not once per item."""
    rows = [(today(), name, n) for name, n in counts.items() if n]
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO metrics (day, name, count) VALUES (?, ?, ?)
        ON CONFLICT(day, name) DO UPDATE SET count = count + excluded.count
        """,
        rows,
    )
    conn.commit()


def today_counts(conn):
    """Today's ops/funnel counters as a plain dict -- what `health` and
    stats.report's HEALTH section show as "today: run_ok=3, ..."."""
    rows = conn.execute("SELECT name, count FROM metrics WHERE day = ?", (today(),)).fetchall()
    return {row["name"]: row["count"] for row in rows}


def pending_notification_stats(conn):
    """Count and oldest score-creation time of notifications not yet
    delivered (cleared their interest's bar, no successful send yet) --
    regardless of retry cool-off, unlike pending_notifications() which is
    scoped to what's eligible to (re)send *right now*. Used by `health`."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n, MIN(s.created_at) AS oldest
        FROM scores s
        JOIN interests n ON n.id = s.interest_id
        WHERE s.final_score >= n.min_score AND n.active = 1
          AND NOT EXISTS (SELECT 1 FROM notifications x WHERE x.score_id = s.id AND x.ok = 1)
        """
    ).fetchone()
    return row["n"], row["oldest"]


def abandoned_notifications(conn, max_attempts):
    """Notifications that gave up: never delivered and out of retries."""
    return conn.execute(
        "SELECT COUNT(*) c FROM notifications WHERE ok = 0 AND attempts >= ?", (max_attempts,)
    ).fetchone()["c"]


def record_usage(conn, provider):
    """Persist and reset a provider's in-process token counters (see
    providers/base.py). Safe to call when nothing was spent."""
    usage = getattr(provider, "usage", None)
    if not usage:
        return
    conn.execute(
        """
        INSERT INTO llm_usage
            (day, provider, model, calls, input_tokens, output_tokens, web_searches)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(day, provider, model) DO UPDATE SET
            calls         = calls         + excluded.calls,
            input_tokens  = input_tokens  + excluded.input_tokens,
            output_tokens = output_tokens + excluded.output_tokens,
            web_searches  = web_searches  + excluded.web_searches
        """,
        (
            today(), provider.name, provider.model,
            usage["calls"], usage["input_tokens"],
            usage["output_tokens"], usage["web_searches"],
        ),
    )
    conn.commit()
    usage.clear()
