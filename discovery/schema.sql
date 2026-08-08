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
    sent_at   TEXT NOT NULL,
    ok        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES candidate_items(id),
    interest_id  INTEGER REFERENCES interests(id),
    verdict      TEXT NOT NULL,                     -- 'up' | 'down'
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scores_interest ON scores(interest_id, final_score);
CREATE INDEX IF NOT EXISTS idx_feedback_interest ON feedback(interest_id, created_at);
