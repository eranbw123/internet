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
    active            INTEGER NOT NULL DEFAULT 1,
    -- Layered interest state (discovery/interest_state.py). 'owner' rows
    -- come from interests.json and are structurally immutable by automation
    -- (see db.py's OwnerInterestImmutable guards + the triggers below).
    layer             TEXT NOT NULL DEFAULT 'owner',
    provenance        TEXT NOT NULL DEFAULT '{}',   -- JSON: how this row came to be
    last_observed_at  TEXT
);

-- Append-only provenance log for the layered interest state. Nothing ever
-- UPDATEs or DELETEs a row here -- `python -m app interests --why <key>`
-- reads it straight through. `actor` is 'owner_sync' (interests.sync, the
-- `init` path) or 'automation' (discovery/interest_state.py).
CREATE TABLE IF NOT EXISTS interest_events (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    interest_key  TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    from_layer    TEXT,
    to_layer      TEXT,
    evidence      TEXT NOT NULL DEFAULT '{}'         -- JSON
);

CREATE INDEX IF NOT EXISTS idx_interest_events_key ON interest_events(interest_key);

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
    -- The earlier item that already tells this story, per the LLM near-dup
    -- judge (dedup.llm_near_duplicate). A linked item is never scored or
    -- delivered; NULL = not a known repeat. dup_reason keeps the judge's own
    -- sentence for auditing.
    duplicate_of     INTEGER REFERENCES candidate_items(id),
    dup_reason       TEXT,
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

-- HELD: n-delivery -- do not alter this table or build delivered-item engagement
-- learning before reading entry 'n-delivery' in C:\Users\eranb\.claude\held\HELD.md.
-- The *unit* of feedback may change under the pending Output Layer decision.
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

-- Continuous Council-driven web discovery (discovery/council.py,
-- discovery/missions.py). One row per Council planning attempt for one
-- interest, success or failure -- the web tick reads this to decide both
-- whether an interest still needs replenishing and (via a run of
-- consecutive 'FAILED' rows) whether the static fallback query should kick
-- in for that interest.
CREATE TABLE IF NOT EXISTS search_generations (
    id                  INTEGER PRIMARY KEY,
    interest_key        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL,             -- PENDING | DONE | FAILED
    provider            TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL DEFAULT '',
    missions_requested  INTEGER NOT NULL DEFAULT 0,
    missions_returned   INTEGER NOT NULL DEFAULT 0,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS idx_search_generations_interest
    ON search_generations(interest_key, created_at);

-- One row per research mission -- either Council-planned (generation_id set)
-- or the bounded static fallback (generation_id NULL, label
-- 'static-fallback'). Durable runtime state: after a process death, machine
-- sleep, provider crash or restart, a fresh web_tick() resumes from these
-- rows alone -- there is no in-memory carry-over and no in-process
-- scheduler. See discovery/db.py's lease_missions()/recover_stale_missions()
-- for the atomicity/staleness contract.
CREATE TABLE IF NOT EXISTS search_missions (
    id                 INTEGER PRIMARY KEY,
    generation_id      INTEGER REFERENCES search_generations(id),
    interest_key       TEXT NOT NULL,
    label              TEXT NOT NULL,
    rationale          TEXT NOT NULL DEFAULT '',
    prompt             TEXT NOT NULL,
    prompt_sha256      TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING|RUNNING|DONE|FAILED
    attempts           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    leased_at          TEXT,
    lease_expires_at   TEXT,
    started_at         TEXT,
    finished_at        TEXT,
    next_attempt_at    TEXT,           -- retry cool-off after a failed attempt
    items_returned     INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_search_missions_interest_status
    ON search_missions(interest_key, status);
CREATE INDEX IF NOT EXISTS idx_search_missions_status_lease
    ON search_missions(status, lease_expires_at);

-- Trace backbone (discovery/trace.py, step-13 task 1). Append-only: rows are
-- INSERTed; the only permitted UPDATE is stamping status/finished_at/error/
-- output on the same row's own open run/node (finishing something already
-- started). model_calls is strictly insert-only -- every retry attempt is a
-- new row. No DELETE anywhere. See discovery/trace.py's module docstring for
-- the write API and the redaction discipline applied before anything here is
-- persisted.

CREATE TABLE IF NOT EXISTS trace_runs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,        -- 'web-tick' | 'run-once' | 'digest' | 'fixture' | ...
    provider     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    config_json  TEXT,                 -- redacted Config snapshot
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    error        TEXT
);

CREATE TABLE IF NOT EXISTS trace_nodes (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES trace_runs(id),
    node_type    TEXT NOT NULL,
    -- Deep-link back to the DB row this node corresponds to, e.g.
    -- entity_type='scores', entity_id=<scores.id> -- 'trace/score/<id>'
    -- resolves via the index below.
    entity_type  TEXT,
    entity_id    TEXT,
    label        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'ok',
    summary      TEXT NOT NULL DEFAULT '',
    input_json   TEXT,
    output_json  TEXT,
    exact_text   TEXT,                 -- byte-exact prompt/response/message text, redacted
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_trace_nodes_run ON trace_nodes(run_id);
CREATE INDEX IF NOT EXISTS idx_trace_nodes_entity ON trace_nodes(entity_type, entity_id);

-- Relationship vocabulary (also a module constant in trace.py, used
-- verbatim): generated, selected, executed, returned, normalized_to,
-- duplicate_of, matched, rejected, deferred, scored, cleared_threshold,
-- rendered, sent, failed, retried_as, feedback_on.
CREATE TABLE IF NOT EXISTS trace_edges (
    from_node_id  INTEGER NOT NULL,
    to_node_id    INTEGER NOT NULL,
    relationship  TEXT NOT NULL,
    ordinal       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trace_edges_from ON trace_edges(from_node_id);
CREATE INDEX IF NOT EXISTS idx_trace_edges_to ON trace_edges(to_node_id);

-- One row per provider call ATTEMPT (a retry is a new row, never an update),
-- attached to the trace_node it was made on behalf of.
CREATE TABLE IF NOT EXISTS model_calls (
    id                       INTEGER PRIMARY KEY,
    trace_node_id            INTEGER NOT NULL REFERENCES trace_nodes(id),
    call_role                TEXT NOT NULL DEFAULT '',  -- council|mission_search|scoring|value_distillation
    attempt                  INTEGER NOT NULL DEFAULT 1,
    provider                 TEXT NOT NULL DEFAULT '',
    model                    TEXT NOT NULL DEFAULT '',
    exact_system_prompt      TEXT,
    exact_user_prompt        TEXT,
    exact_schema_json        TEXT,
    exact_parameters_json    TEXT,
    raw_response_text        TEXT,
    parsed_response_json     TEXT,
    validation_result        TEXT,
    usage_json                TEXT,
    provider_request_id      TEXT,
    started_at               TEXT NOT NULL,
    finished_at               TEXT,
    error                     TEXT
);

CREATE INDEX IF NOT EXISTS idx_model_calls_node ON model_calls(trace_node_id);

-- AI-generated interest offers (discovery/offers.py). One row per proposed
-- interest, carrying the evidence that produced it: verbatim quotes from the
-- owner's own conversations plus the conversation ids they came from, the
-- durability counts behind them, and every term of the composite score, so
-- the inbox can answer "why is this being offered?" without joining back to
-- the `ai` repo. `key` is UNIQUE: an artifact re-proposing a theme that was
-- already offered, accepted or rejected updates nothing (the importer never
-- mutates a row past 'proposed'), which is what makes a repeated import a
-- no-op. Retirement offers raised by the decay sweep are keyed
-- 'retire:<interest_key>' so they can never collide with a new-interest
-- offer for the same theme.
CREATE TABLE IF NOT EXISTS interest_offers (
    id                   INTEGER PRIMARY KEY,
    key                  TEXT NOT NULL UNIQUE,
    kind                 TEXT NOT NULL DEFAULT 'new',   -- new|bridge|merge|split|revive|retire
    title                TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    positive_signals     TEXT NOT NULL DEFAULT '[]',    -- JSON array
    negative_signals     TEXT NOT NULL DEFAULT '[]',    -- JSON array
    suggested_min_score  REAL,
    suggested_sources    TEXT NOT NULL DEFAULT '["web_search"]',  -- JSON array
    parent_key           TEXT,                          -- single-level hierarchy
    related_keys         TEXT NOT NULL DEFAULT '[]',    -- JSON array (bridge parents, merge targets)
    score                REAL,                          -- composite, computed here
    score_terms          TEXT NOT NULL DEFAULT '{}',    -- JSON: every term of it, for the UI
    evidence             TEXT NOT NULL DEFAULT '[]',    -- JSON [{date, quote, lang, depth, conversation_id}]
    source_conversations TEXT NOT NULL DEFAULT '[]',    -- JSON array of the distinct conversation ids above
    durability           TEXT NOT NULL DEFAULT '{}',    -- JSON {n_convs, active_months, span_days, recency_days}
    similarity           TEXT NOT NULL DEFAULT '[]',    -- JSON [{key, sim}] against existing interests
    exploratory          INTEGER NOT NULL DEFAULT 0,    -- the run's deliberate serendipity pick
    status               TEXT NOT NULL DEFAULT 'proposed',
                         -- proposed|offered|accepted|rejected|snoozed|expired
    snoozed_until        TEXT,
    artifact_sha256      TEXT NOT NULL DEFAULT '',      -- '' for sweep-raised offers
    generated_at         TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    decided_at           TEXT,
    decided_note         TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_interest_offers_status ON interest_offers(status, score);

-- Append-only lifecycle log for offers, mirroring interest_events. Nothing
-- ever UPDATEs or DELETEs a row here. `actor` is generator|importer|owner_ui|
-- timer|pipeline.
CREATE TABLE IF NOT EXISTS offer_events (
    id           INTEGER PRIMARY KEY,
    at           TEXT NOT NULL,
    offer_key    TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    detail       TEXT NOT NULL DEFAULT '{}'             -- JSON
);

CREATE INDEX IF NOT EXISTS idx_offer_events_key ON offer_events(offer_key);

-- Connections between interests. Written by the nightly lift computation and
-- the weekly semantic pass (both later PRs); the table lands here so the
-- schema is complete in one migration. Raw keyword co-match is deliberately
-- NOT a source: 97% of items match >=2 interests, so it measures the
-- matcher's looseness, not a relationship.
CREATE TABLE IF NOT EXISTS interest_edges (
    id           INTEGER PRIMARY KEY,
    a_key        TEXT NOT NULL,
    b_key        TEXT NOT NULL,
    kind         TEXT NOT NULL,              -- co_engagement|semantic|bridge_offer|parent
    weight       REAL NOT NULL,
    evidence     TEXT NOT NULL DEFAULT '{}', -- JSON: lift, shared items, or bridging quotes
    computed_at  TEXT NOT NULL,
    UNIQUE(a_key, b_key, kind)
);
