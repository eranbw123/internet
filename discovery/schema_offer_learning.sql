-- Offer-decision learning (design §5.6, first bullet). Additive, applied by
-- offer_learning.ensure_schema() rather than by db.init(): PRs H/I/J are
-- landing their own DDL into schema.sql at the same time, so this half keeps
-- its own file and never renumbers or edits theirs.
--
-- This is NOT a second event log. `offer_events` (PR H) is the source of
-- truth for what happened; this table is its decorated projection, holding
-- the three things learning needs and that chain cannot keep: the signal
-- tokens as they stood when the owner decided, the edit diff, and the
-- interest's lifecycle stage at the moment of a retirement answer.
--
-- Deliberately NOT the `feedback` table. `feedback` records verdicts on
-- delivered items and is being redesigned by the separate "Output Layer"
-- brief (rating the unit rather than the article, "press for depth",
-- "already knew this"). Offer decisions are a different event on a different
-- object -- an owner judgement about an *interest proposal*, before anything
-- has ever been delivered for it -- so they get their own append-only log and
-- the two can be reconciled later without a schema fight.

CREATE TABLE IF NOT EXISTS offer_decision_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,                          -- when this row was recorded
    decided_at TEXT NOT NULL,                  -- when the decision was made
    offer_key TEXT NOT NULL,
    offer_kind TEXT NOT NULL DEFAULT 'new',    -- new|bridge|merge|split|revive|retire
    decision TEXT NOT NULL,                    -- accepted|rejected|snoozed|expired
    actor TEXT NOT NULL DEFAULT 'owner_ui',    -- owner_ui|timer|importer|pipeline
    interest_key TEXT NOT NULL DEFAULT '',     -- kind='retire': whose retirement was proposed
    lifecycle TEXT NOT NULL DEFAULT '',        -- active|decaying|paused|retired, at decision time
    domain TEXT NOT NULL DEFAULT '',           -- the offer's parent_key (§5.3 family)
    -- +1 / -1 / 0 toward the theme, kind-aware: rejecting a `retire` offer is
    -- the owner rescuing an interest, which is a POSITIVE signal for it --
    -- though a positive that deliberately propagates no terms, matching
    -- offers.blocked_terms_for()'s reasoning in the other direction.
    polarity INTEGER NOT NULL DEFAULT 0,
    signal_terms TEXT NOT NULL DEFAULT '[]',   -- post-edit tokens (what the owner kept)
    proposed_min_score REAL,                   -- the bar the generator suggested
    accepted_min_score REAL,                   -- the bar the owner actually saved
    edits TEXT NOT NULL DEFAULT '{}',          -- offers.accept(edits=) verbatim: {field: new value}
    snoozed_until TEXT,
    offer_score REAL,                          -- §5.2 composite at decision time
    score_terms TEXT NOT NULL DEFAULT '{}',    -- each term, for provenance
    artifact_sha256 TEXT NOT NULL DEFAULT '',  -- which generator run offered it
    note TEXT NOT NULL DEFAULT '',
    -- Append-only, but replaying the same decision must not double-count it.
    UNIQUE(offer_key, decision, decided_at)
);

CREATE INDEX IF NOT EXISTS idx_offer_decision_log_key ON offer_decision_log(offer_key);
CREATE INDEX IF NOT EXISTS idx_offer_decision_log_decided ON offer_decision_log(decided_at);
