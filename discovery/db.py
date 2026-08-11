"""SQLite access. Thin functions over sqlite3 -- no ORM, no query builder.

JSON-shaped columns (signals, metadata, source_config, matched_terms) are
stored as text and decoded here so callers only ever see Python values.
"""
import hashlib
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

# Namespaces every derived-interest key so it can never collide with an
# owner one -- interests.load_file() rejects an owner entry that carries it.
DERIVED_KEY_PREFIX = "derived:"

# DB-level backstop for owner immutability, on top of the two guarded write
# helpers below. References the `layer` column added by the ALTER pass in
# init(), so this must run AFTER that pass on a pre-existing DB -- schema.sql's
# executescript() runs first and would fail with "no such column: layer".
TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_owner_layer_immutable
BEFORE UPDATE OF layer ON interests
WHEN OLD.layer = 'owner' AND NEW.layer != 'owner'
BEGIN
    SELECT RAISE(ABORT, 'owner interest layer is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_delete_immutable
BEFORE DELETE ON interests
WHEN OLD.layer = 'owner'
BEGIN
    SELECT RAISE(ABORT, 'owner interest rows cannot be deleted');
END;
"""


class OwnerInterestImmutable(Exception):
    """Raised by upsert_derived_interest()/set_interest_layer() when the
    write would have touched a row whose layer is 'owner' -- both guard
    every UPDATE with `WHERE layer != 'owner'` and treat a zero rowcount
    (no matching non-owner row) as this."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ago(seconds):
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def future(seconds):
    """The `ago()` mirror -- a timestamp `seconds` from now, for lease
    expiries and retry cool-offs."""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
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
        ("interests", "layer", "TEXT NOT NULL DEFAULT 'owner'"),
        ("interests", "provenance", "TEXT NOT NULL DEFAULT '{}'"),
        ("interests", "last_observed_at", "TEXT"),
        ("candidate_items", "duplicate_of", "INTEGER REFERENCES candidate_items(id)"),
        ("candidate_items", "dup_reason", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    # Must come after the ALTER pass above -- see TRIGGERS_SQL's docstring.
    conn.executescript(TRIGGERS_SQL)
    conn.commit()


# --- interests ---------------------------------------------------------------

def upsert_interest(conn, interest):
    """Insert or update by `key`. interests.json is the source of truth, so a
    re-load overwrites the stored copy rather than merging. Always writes
    layer='owner'; the ON CONFLICT branch only fires `WHERE layer = 'owner'`,
    so this can never overwrite a derived row (structurally impossible
    anyway -- interests.load_file() rejects an owner key carrying
    DERIVED_KEY_PREFIX -- but guarded here too, defense in depth)."""
    conn.execute(
        """
        INSERT INTO interests
            (key, title, description, positive_signals, negative_signals,
             min_score, sources, source_config, active, layer)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'owner')
        ON CONFLICT(key) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            positive_signals = excluded.positive_signals,
            negative_signals = excluded.negative_signals,
            min_score = excluded.min_score,
            sources = excluded.sources,
            source_config = excluded.source_config,
            active = 1,
            layer = 'owner'
        WHERE interests.layer = 'owner'
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


def upsert_derived_interest(conn, interest, provenance):
    """Insert or update a non-owner interest row -- one of only two ways
    (see set_interest_layer) any automation may write a non-owner row.
    `interest.layer` decides `active` (1 only for 'inferred'; see
    interest_state.py's operational-meaning rules). Refuses to touch an
    owner row: the ON CONFLICT branch is `WHERE layer != 'owner'`, and a
    zero rowcount (also covering "no such key at all") raises
    OwnerInterestImmutable.

    `last_observed_at` is always stamped to now() here, including on a
    personal-state seed with zero real observations -- it doubles as the
    row's decay-staleness baseline (interest_state.apply_transitions() falls
    back to it when a pass has no fresh corpus evidence for the term), not
    only "last actually observed in the corpus". A freshly written row is
    correctly not idle yet, even before it has ever been observed."""
    if not interest.key.startswith(DERIVED_KEY_PREFIX):
        raise ValueError(
            f"derived interest key {interest.key!r} must start with {DERIVED_KEY_PREFIX!r}"
        )
    active = 1 if interest.layer == "inferred" else 0
    cur = conn.execute(
        """
        INSERT INTO interests
            (key, title, description, positive_signals, negative_signals,
             min_score, sources, source_config, active, layer, provenance,
             last_observed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            title            = excluded.title,
            description      = excluded.description,
            positive_signals = excluded.positive_signals,
            negative_signals = excluded.negative_signals,
            min_score        = excluded.min_score,
            sources          = excluded.sources,
            source_config    = excluded.source_config,
            active           = excluded.active,
            layer            = excluded.layer,
            provenance       = excluded.provenance,
            last_observed_at = excluded.last_observed_at
        WHERE interests.layer != 'owner'
        """,
        (
            interest.key, interest.title, interest.description,
            json.dumps(interest.positive_signals), json.dumps(interest.negative_signals),
            interest.min_score, json.dumps(interest.sources), json.dumps(interest.source_config),
            active, interest.layer, json.dumps(provenance), now(),
        ),
    )
    if cur.rowcount == 0:
        raise OwnerInterestImmutable(interest.key)
    conn.commit()


def set_interest_layer(conn, key, to_layer, evidence):
    """Change only the layer (and the `active` flag it implies) on an
    existing non-owner row -- decay, immediate retirement and re-entry never
    change title/signals/min_score/last_observed_at (no fresh observation
    backs them). Promotion into 'inferred' goes through
    upsert_derived_interest() instead, since that also sets
    min_score/positive_signals. `evidence` is not persisted here -- the
    caller (interest_state.apply_transitions) logs it to interest_events."""
    active = 1 if to_layer == "inferred" else 0
    cur = conn.execute(
        "UPDATE interests SET layer = ?, active = ? WHERE key = ? AND layer != 'owner'",
        (to_layer, active, key),
    )
    if cur.rowcount == 0:
        raise OwnerInterestImmutable(key)
    conn.commit()


def active_interests(conn):
    rows = conn.execute("SELECT * FROM interests WHERE active = 1 ORDER BY id").fetchall()
    return [_row_to_interest(r) for r in rows]


def interest_by_key(conn, key):
    row = conn.execute("SELECT * FROM interests WHERE key = ?", (key,)).fetchone()
    return _row_to_interest(row) if row else None


def list_interests(conn, layer=None):
    """Raw readout rows for `python -m app interests` -- key, layer, active,
    min_score, last_observed_at, owner rows first. Deliberately not routed
    through _row_to_interest(): models.Interest doesn't carry `active` (a
    DB-only bookkeeping column the pipeline reads straight from SQL)."""
    if layer:
        return conn.execute(
            "SELECT key, layer, active, min_score, last_observed_at FROM interests"
            " WHERE layer = ? ORDER BY id",
            (layer,),
        ).fetchall()
    return conn.execute(
        "SELECT key, layer, active, min_score, last_observed_at FROM interests"
        " ORDER BY (layer != 'owner'), id"
    ).fetchall()


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
        layer=row["layer"],
        provenance=json.loads(row["provenance"]),
    )


# --- interest provenance -----------------------------------------------------

def add_interest_event(conn, interest_key, actor, action, from_layer, to_layer, evidence=None):
    """Append one row. Nothing ever UPDATEs or DELETEs interest_events."""
    conn.execute(
        """
        INSERT INTO interest_events (at, interest_key, actor, action, from_layer, to_layer, evidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (now(), interest_key, actor, action, from_layer, to_layer, json.dumps(evidence or {})),
    )
    conn.commit()


def interest_events(conn, key):
    """The full append-only chain for one interest, oldest first -- the
    provenance answer behind `python -m app interests --why <key>`."""
    rows = conn.execute(
        "SELECT at, actor, action, from_layer, to_layer, evidence FROM interest_events"
        " WHERE interest_key = ? ORDER BY id",
        (key,),
    ).fetchall()
    return [
        {
            "at": r["at"], "actor": r["actor"], "action": r["action"],
            "from_layer": r["from_layer"], "to_layer": r["to_layer"],
            "evidence": json.loads(r["evidence"]),
        }
        for r in rows
    ]


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


def near_dup_pool(conn, since, exclude_id=None, limit=2000):
    """Recently stored articles the near-dup judge may compare against:
    newest first, never items already linked as duplicates (a story chains to
    its first telling, not to another repeat). `since` bounds cost, not
    correctness -- see dedup.llm_near_duplicate()."""
    return conn.execute(
        """
        SELECT id, title, substr(text, 1, 400) AS snippet, published_at,
               first_seen_at, source, metadata
        FROM candidate_items
        WHERE type = 'article' AND first_seen_at >= ? AND duplicate_of IS NULL
          AND id != ?
        ORDER BY id DESC LIMIT ?
        """,
        (since, exclude_id or 0, limit),
    ).fetchall()


def mark_near_duplicate(conn, item_id, original_id, reason):
    """Link a stored item to the earlier item that already tells its story.
    The link, not deletion, is the suppression: linked items are excluded from
    backlog scoring, delivery and the judge's own pool by SQL, and a count of
    links onto one item is its corroboration record."""
    conn.execute(
        "UPDATE candidate_items SET duplicate_of = ?, dup_reason = ? WHERE id = ?",
        (original_id, reason, item_id),
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
               s.confidence, s.reason, s.why_better_than_generic,
               s.created_at AS score_created_at
        FROM scores s
        JOIN interests n ON n.id = s.interest_id
        JOIN candidate_items i ON i.id = s.item_id
        WHERE s.final_score >= n.min_score
          AND n.active = 1
          AND i.duplicate_of IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM notifications x
              WHERE x.score_id = s.id
                AND (x.ok = 1 OR x.attempts >= ? OR x.sent_at > ?)
          )
        ORDER BY s.final_score DESC
        """,
        (max_attempts, ago(retry_after_seconds)),
    ).fetchall()


def successful_notifications_since(conn, ts, channel=None):
    """How many notifications were sent OK at/after `ts` -- the rolling-window
    count pipeline.deliver() debits the immediate-discovery per-day cap against
    so a busy run can't flood the owner's phone. `channel`, if given, restricts
    the count to that channel (so the immediate cap ignores digest/alert sends)."""
    if channel is None:
        return conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE ok = 1 AND sent_at > ?", (ts,)
        ).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE ok = 1 AND sent_at > ? AND channel = ?",
        (ts, channel),
    ).fetchone()[0]


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


# --- Council-driven search missions (discovery/council.py, discovery/missions.py) --

def insert_generation(conn, interest_key, provider, model, missions_requested):
    """One row per Council planning attempt, opened before the call is even
    made so a crash mid-call still leaves a PENDING row rather than no
    record at all. finish_generation() closes it out either way."""
    cur = conn.execute(
        """
        INSERT INTO search_generations
            (interest_key, created_at, status, provider, model, missions_requested,
             missions_returned, error)
        VALUES (?, ?, 'PENDING', ?, ?, ?, 0, NULL)
        """,
        (interest_key, now(), provider, model, missions_requested),
    )
    conn.commit()
    return cur.lastrowid


def finish_generation(conn, generation_id, status, missions_returned=0, error=None):
    conn.execute(
        "UPDATE search_generations SET status = ?, missions_returned = ?, error = ? WHERE id = ?",
        (status, missions_returned, error, generation_id),
    )
    conn.commit()


def insert_missions(conn, generation_id, interest_key, missions):
    """`missions`: [{label, rationale, prompt}, ...] (council.plan_missions()'s
    validated shape, or a single hand-built static-fallback entry -- see
    missions.py). `generation_id` is NULL for the fallback: it isn't a
    Council planning attempt, so it has no search_generations row."""
    ts = now()
    rows = [
        (
            generation_id, interest_key, m["label"], m.get("rationale", ""),
            m["prompt"], hashlib.sha256(m["prompt"].encode("utf-8")).hexdigest(), ts,
        )
        for m in missions
    ]
    conn.executemany(
        """
        INSERT INTO search_missions
            (generation_id, interest_key, label, rationale, prompt, prompt_sha256,
             status, attempts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def pending_mission_count(conn, interest_key):
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM search_missions WHERE interest_key = ? AND status = 'PENDING'",
        (interest_key,),
    ).fetchone()
    return row["c"]


def recover_stale_missions(conn, max_attempts):
    """Reclaim leases past their lease_expires_at. attempts was already
    incremented at lease time, so a mission that keeps timing out is retired
    to FAILED once it hits max_attempts instead of being handed out forever;
    everything else goes back to PENDING for the next tick to pick up."""
    ts = now()
    conn.execute(
        """
        UPDATE search_missions
        SET status = 'FAILED', finished_at = ?, last_error = 'stale lease: exceeded max attempts'
        WHERE status = 'RUNNING' AND lease_expires_at < ? AND attempts >= ?
        """,
        (ts, ts, max_attempts),
    )
    conn.execute(
        """
        UPDATE search_missions
        SET status = 'PENDING', leased_at = NULL, lease_expires_at = NULL
        WHERE status = 'RUNNING' AND lease_expires_at < ?
        """,
        (ts,),
    )
    conn.commit()


def lease_missions(conn, mission_ids, lease_seconds):
    """Atomically claim whichever of `mission_ids` are still PENDING, inside
    one BEGIN IMMEDIATE transaction -- two overlapping ticks (separate
    connections/processes on the same discovery.db) can never both claim the
    same mission. The PENDING check happens inside the same transaction as
    the UPDATE (not re-derived from a post-UPDATE SELECT keyed on the leased_
    at timestamp -- two leases within the same wall-clock second would
    otherwise be indistinguishable and wrongly re-claim an already-RUNNING
    row from an earlier call). Returns the subset of `mission_ids` actually
    leased, in the same order."""
    if not mission_ids:
        return []
    ts = now()
    expires = future(lease_seconds)
    placeholders = ",".join("?" * len(mission_ids))
    conn.execute("BEGIN IMMEDIATE")
    try:
        pending_ids = {
            r["id"] for r in conn.execute(
                f"SELECT id FROM search_missions WHERE id IN ({placeholders}) AND status = 'PENDING'",
                mission_ids,
            ).fetchall()
        }
        if pending_ids:
            claim_placeholders = ",".join("?" * len(pending_ids))
            cur = conn.execute(
                f"""
                UPDATE search_missions
                SET status = 'RUNNING', leased_at = ?, lease_expires_at = ?,
                    attempts = attempts + 1, started_at = ?
                WHERE id IN ({claim_placeholders})
                """,
                (ts, expires, ts, *pending_ids),
            )
            assert cur.rowcount == len(pending_ids)   # the atomicity contract, made explicit
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [mid for mid in mission_ids if mid in pending_ids]


def finish_mission(conn, mission_id, items_returned):
    conn.execute(
        """
        UPDATE search_missions
        SET status = 'DONE', finished_at = ?, items_returned = ?, last_error = NULL
        WHERE id = ?
        """,
        (now(), items_returned, mission_id),
    )
    conn.commit()


def fail_mission(conn, mission_id, error, max_attempts, retry_seconds):
    """Records a failed execution attempt. `attempts` was already incremented
    by lease_missions(), so once it reaches max_attempts the mission is
    retired to FAILED; otherwise it goes back to PENDING with
    next_attempt_at set retry_seconds out, so the tick's fairness selection
    doesn't just re-pick the same failing mission next tick."""
    row = conn.execute(
        "SELECT attempts FROM search_missions WHERE id = ?", (mission_id,)
    ).fetchone()
    attempts = row["attempts"] if row else max_attempts
    if attempts >= max_attempts:
        conn.execute(
            """
            UPDATE search_missions SET status = 'FAILED', finished_at = ?, last_error = ?
            WHERE id = ?
            """,
            (now(), error, mission_id),
        )
    else:
        conn.execute(
            """
            UPDATE search_missions SET status = 'PENDING', next_attempt_at = ?, last_error = ?
            WHERE id = ?
            """,
            (future(retry_seconds), error, mission_id),
        )
    conn.commit()


def recent_missions(conn, interest_key, limit):
    """Previous generated missions for one interest, newest first -- fed to
    the Council as planning history (label + rationale only) so it doesn't
    repeat an angle."""
    return conn.execute(
        """
        SELECT label, rationale FROM search_missions
        WHERE interest_key = ? ORDER BY id DESC LIMIT ?
        """,
        (interest_key, limit),
    ).fetchall()


def mission_by_id(conn, mission_id):
    return conn.execute(
        "SELECT * FROM search_missions WHERE id = ?", (mission_id,)
    ).fetchone()
