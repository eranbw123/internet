-- Discovery engine schema. Applied with CREATE TABLE IF NOT EXISTS on every
-- start, so there is no migration tool: additive changes only, and anything
-- destructive means deleting discovery.db and re-running (it is a cache of
-- public content plus scores, not a system of record).

CREATE TABLE IF NOT EXISTS interests (
    id                INTEGER PRIMARY KEY,
    key               TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    positive_signals  TEXT NOT NULL DEFAULT '[]',   -- JSON array
    negative_signals  TEXT NOT NULL DEFAULT '[]',   -- JSON array
    min_score         REAL NOT NULL DEFAULT 0.7,    -- threshold on scores.final_score
    sources           TEXT NOT NULL DEFAULT '[]',   -- JSON array of collector names
    source_config     TEXT NOT NULL DEFAULT '{}',   -- JSON, keyed by collector name
    active            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS candidate_items (
    id               INTEGER PRIMARY KEY,
    source           TEXT NOT NULL,
    type             TEXT NOT NULL,
    title            TEXT NOT NULL,
    text             TEXT NOT NULL DEFAULT '',
    url              TEXT NOT NULL,                 -- canonical form
    author           TEXT,
    published_at     TEXT,
    metadata         TEXT NOT NULL DEFAULT '{}',    -- JSON
    origin_interest  TEXT,                          -- interest key the collector ran for
    dedup_key        TEXT NOT NULL,
    url_hash         TEXT NOT NULL,
    title_hash       TEXT NOT NULL,
    content_hash     TEXT,                          -- NULL when the item has no body
    -- Set by the cheap pre-filter so a rejected item is never re-filtered or
    -- sent to the LLM on a later cycle. NULL = not filtered yet.
    prefilter_ok     INTEGER,
    prefilter_reason TEXT,
    -- Last time an LLM scoring attempt failed for this item. The backlog
    -- rescorer skips items attempted within SCORE_RETRY_SECONDS (db.py) so a
    -- provider outage doesn't re-fail the same items every cycle.
    score_attempted_at TEXT,
    first_seen_at    TEXT NOT NULL,
    -- One row per distinct thing a collector saw. A stock move recurs daily,
    -- so the stocks collector puts the date in dedup_key; everything else
    -- dedups on the canonical URL.
    UNIQUE(source, dedup_key)
);

CREATE INDEX IF NOT EXISTS idx_items_url_hash ON candidate_items(url_hash);
CREATE INDEX IF NOT EXISTS idx_items_title_hash ON candidate_items(title_hash);
CREATE INDEX IF NOT EXISTS idx_items_content_hash ON candidate_items(content_hash);

-- Which interests a candidate matched, and how strongly, from the cheap
-- keyword matcher. Written before scoring so a filtered-out item still shows
-- why it was considered.
CREATE TABLE IF NOT EXISTS item_interests (
    id             INTEGER PRIMARY KEY,
    item_id        INTEGER NOT NULL REFERENCES candidate_items(id),
    interest_id    INTEGER NOT NULL REFERENCES interests(id),
    match_score    REAL NOT NULL,                   -- 0-1, cheap heuristic
    matched_terms  TEXT NOT NULL DEFAULT '[]',      -- JSON array
    created_at     TEXT NOT NULL,
    UNIQUE(item_id, interest_id)
);

CREATE TABLE IF NOT EXISTS scores (
    id                       INTEGER PRIMARY KEY,
    item_id                  INTEGER NOT NULL REFERENCES candidate_items(id),
    interest_id              INTEGER NOT NULL REFERENCES interests(id),
    -- All 0-1, straight from the model.
    personal_relevance       REAL NOT NULL,
    novelty                  REAL NOT NULL,
    depth                    REAL NOT NULL,
    specificity              REAL NOT NULL,         -- stored, not currently weighted
    importance               REAL NOT NULL,
    surprise                 REAL NOT NULL,
    final_score              REAL NOT NULL,         -- computed here, not by the model
    confidence               REAL NOT NULL,
    reason                   TEXT NOT NULL DEFAULT '',
    why_better_than_generic  TEXT NOT NULL DEFAULT '',
    provider                 TEXT NOT NULL DEFAULT '',
    model                    TEXT NOT NULL DEFAULT '',
    created_at               TEXT NOT NULL,
    -- One scoring pass per item: the scorer picks the most relevant interest
    -- itself, so a second row would mean re-paying for the same judgement.
    UNIQUE(item_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id        INTEGER PRIMARY KEY,
    score_id  INTEGER NOT NULL UNIQUE REFERENCES scores(id),
    channel   TEXT NOT NULL,
    sent_at   TEXT NOT NULL,             -- time of the latest attempt
    ok        INTEGER NOT NULL,
    -- Send attempts so far. A failed send (ok=0) stays eligible for retry --
    -- after a cool-off, up to MAX_SEND_ATTEMPTS (see db.py) -- instead of
    -- being silently consumed; a success (ok=1) is final either way.
    attempts  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY,
    item_id         INTEGER NOT NULL REFERENCES candidate_items(id),
    interest_id     INTEGER REFERENCES interests(id),
    verdict         TEXT NOT NULL,                  -- 'fire' | 'up' | 'down' | 'trash'
    note            TEXT NOT NULL DEFAULT '',
    -- scores.final_score at the moment feedback was given, so a later
    -- re-score (or a ranking-formula change) doesn't retroactively change
    -- what the user was actually reacting to.
    original_score  REAL,
    created_at      TEXT NOT NULL
);

-- Funnel counters, one row per (day, stage). Stages that end an item's life
-- (duplicate, filtered) leave no row in candidate_items, so the funnel cannot
-- be reconstructed from the other tables -- hence counting it as it happens.
CREATE TABLE IF NOT EXISTS metrics (
    day    TEXT NOT NULL,
    name   TEXT NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, name)
);

-- Token spend, per day and model, so `stats` can price a week's run. Written
-- from the provider's in-process counters at the end of each command.
CREATE TABLE IF NOT EXISTS llm_usage (
    day            TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    calls          INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    web_searches   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, model)
);

CREATE INDEX IF NOT EXISTS idx_scores_interest ON scores(interest_id, final_score);
CREATE INDEX IF NOT EXISTS idx_feedback_interest ON feedback(interest_id, created_at);

-- Durable key/value store for service-level bookkeeping: job heartbeats
-- (job:<name>:last_ok / job:<name>:last_fail), the persisted Telegram
-- getUpdates offset, and health's own alert-dedup state. Separately
-- scheduled OS tasks (see ops/install_tasks.py) can now overlap on this
-- same discovery.db -- this is what lets each one pick up where the last
-- one left off instead of racing or replaying.
CREATE TABLE IF NOT EXISTS service_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
