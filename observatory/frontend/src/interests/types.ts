/** The interests-workspace API contract.
 *
 * Source of truth, in order of authority:
 *   1. `discovery/offers.py` + `discovery/schema.sql` as SHIPPED BY PR H --
 *      the offers store. Every field below that names a column was read off
 *      that implementation, not guessed.
 *   2. Section 7.3 of the Interest Intelligence design (2026-08-17), which
 *      defines the HTTP surface PR J is building in observatory/manage.py.
 *
 * PR J did not exist when this was written; the workspace therefore runs on a
 * documented mock client (client.ts) that speaks exactly this shape. When PR J
 * lands, diff it against this file. The places where the wire shape is still
 * unsettled are marked CONTRACT NOTE.
 */

/** interest_offers.kind. 'retire' offers are raised locally by the decay
 * sweep; every other kind comes from the generator artifact. */
export type OfferKind = "new" | "bridge" | "merge" | "split" | "revive" | "retire";

/** interest_offers.status -- the lifecycle in offers.py's TRANSITIONS table. */
export type OfferStatus =
  | "proposed" | "offered" | "accepted" | "rejected" | "snoozed" | "expired";

/** offers.py's TRANSITIONS, mirrored so the UI can refuse a move the server
 * would refuse anyway -- rather than offering a button that 409s.
 *
 * The consequential entry is `accepted: []`: acceptance is TERMINAL. A decided
 * offer can never be re-decided, so the inbox must never render Accept/Reject
 * on one. `rejected`/`expired` -> `offered` exists for exactly one caller, the
 * decay sweep re-raising a retirement offer after its cool-off; it is not an
 * owner action, which is why no UI control maps to it. */
export const OFFER_TRANSITIONS: Record<OfferStatus, OfferStatus[]> = {
  proposed: ["offered", "rejected"],
  offered: ["accepted", "rejected", "snoozed", "expired"],
  snoozed: ["offered", "accepted", "rejected", "expired"],
  expired: ["offered"],
  rejected: ["offered"],
  accepted: [],
};

/** Can the owner still act on this offer? */
export function isDecidable(status: OfferStatus): boolean {
  return status === "offered" || status === "snoozed";
}

/** The prefix keeping a sweep-raised retirement offer in its own key
 * namespace (offers.py RETIRE_PREFIX), so it can never collide with a
 * new-interest offer proposing the same theme. */
export const RETIRE_PREFIX = "retire:";

/** The interest a retirement offer is about. */
export function retireTargetKey(offer: Pick<Offer, "key" | "kind" | "related_keys">): string {
  if (offer.related_keys.length > 0) return offer.related_keys[0];
  return offer.key.startsWith(RETIRE_PREFIX) ? offer.key.slice(RETIRE_PREFIX.length) : offer.key;
}

/** One entry of interest_offers.evidence.
 *
 * PR H's `_normalize_evidence` emits exactly {date, quote, lang, depth,
 * conversation_id} and drops anything else, so those five are what live data
 * contains.
 *
 * `lang` is load-bearing, not decorative. PR H fills it per quote (from the
 * producer, else `_guess_lang`), and the UI renders direction FROM THIS FIELD
 * rather than sniffing the characters -- see Bidi.tsx. 28% of this corpus is
 * Hebrew, so a mixed-direction inbox is the normal case. */
export interface EvidenceQuote {
  /** ISO-8601 date of the conversation the quote came from. */
  date: string;
  /** The owner's own words, verbatim. Never a paraphrase -- that is the point. */
  quote: string;
  /** "he" | "en" are the two that occur in this corpus. */
  lang: string;
  /** 0-1 conversation depth (the durability gate's deep-pair rule uses it). */
  depth: number;
  /** Which conversation. May be "" -- PR H defaults it when the producer
   * ships no id. */
  conversation_id: string;
  /** CONTRACT NOTE: not persisted today. PR H's `_normalize_evidence` builds
   * its dict from a fixed key list that does not include a title, so live
   * evidence references a conversation by opaque id. The design's own inbox
   * mockup shows TITLES ("Binding of Isaac Steam Deck"), and "which
   * conversations" is a stated provenance requirement, so this renders when
   * present and falls back to the id when not. Making it real is a one-line
   * widening in PR H plus the generator (PR G) shipping the field. */
  conversation_title?: string;
}

/** interest_offers.durability. Empty ({}) on sweep-raised retirement offers. */
export interface Durability {
  n_convs?: number;
  active_months?: number;
  span_days?: number;
  recency_days?: number;
}

/** interest_offers.score_terms.
 *
 * Two different payloads share this column, which is worth knowing before
 * rendering it:
 *   - generated offers carry the five ranking terms (weights in Rules:
 *     evidence .30, recurrence .15, recency .15, novelty .20, yield .20);
 *   - retirement offers carry the sweep's funnel snapshot instead
 *     (offers.py `_raise_retire_offer`): interest_key, silent_days, collected,
 *     scored, above_bar. That is where a retire offer's justification lives --
 *     there is no separate funnel column. */
export interface ScoreTerms {
  evidence_strength?: number;
  recurrence?: number;
  recency?: number;
  novelty?: number;
  expected_yield?: number;
  /** retirement offers only */
  interest_key?: string;
  silent_days?: number;
  collected?: number;
  scored?: number;
  above_bar?: number;
  [term: string]: number | string | undefined;
}

/** The five ranking terms and their weights, in display order. Kept beside the
 * contract because the UI shows the arithmetic, and the arithmetic is only
 * meaningful next to its weight. Mirrors offers.py Rules. */
export const SCORE_TERM_WEIGHTS: { term: string; weight: number; label: string }[] = [
  { term: "evidence_strength", weight: 0.30, label: "evidence" },
  { term: "novelty", weight: 0.20, label: "novelty" },
  { term: "expected_yield", weight: 0.20, label: "yield" },
  { term: "recurrence", weight: 0.15, label: "recurrence" },
  { term: "recency", weight: 0.15, label: "recency" },
];

/** One entry of interest_offers.similarity: how close this candidate sits to
 * an interest that already exists. Above Rules.semantic_dup_similarity (0.70)
 * the importer would have deduped it, so anything the inbox shows is
 * deliberately "close but kept separate". */
export interface Similarity {
  key: string;
  sim: number;
}

/** A row of interest_offers, as PR H's `_row_to_offer` hands it over with the
 * JSON columns already decoded. */
export interface Offer {
  id: number;
  /** UNIQUE. Retirement offers are namespaced `retire:<interest_key>`. */
  key: string;
  kind: OfferKind;
  title: string;
  description: string;
  positive_signals: string[];
  negative_signals: string[];
  suggested_min_score: number | null;
  suggested_sources: string[];
  parent_key: string | null;
  /** Bridge parents, merge targets -- and, for a retirement offer, the single
   * interest being proposed for retirement. */
  related_keys: string[];
  /** Composite 0-1. Null on retirement offers, which are not ranked. */
  score: number | null;
  score_terms: ScoreTerms;
  evidence: EvidenceQuote[];
  /** Distinct conversation ids across `evidence` -- PR H derives it so the UI
   * can say how many conversations produced an offer without re-deriving. */
  source_conversations: string[];
  durability: Durability;
  similarity: Similarity[];
  /** The run's deliberate serendipity pick (Rules.serendipity_slots = 1):
   * surfaced by lane rather than by rank. */
  exploratory: boolean;
  status: OfferStatus;
  snoozed_until: string | null;
  /** "" for sweep-raised offers, which have no artifact behind them. */
  artifact_sha256: string;
  generated_at: string;
  created_at: string;
  decided_at: string | null;
  decided_note: string;
}

export interface OffersResponse {
  offers: Offer[];
}

/** interests.lifecycle (PR H). Four states, not two: an interest that stops
 * producing spends 30 idle days `decaying` before the 45-day `auto_pause`, so
 * the owner sees it fading before it stops. */
export type Lifecycle = "active" | "decaying" | "paused" | "retired";

/** offers.py LIFECYCLE_TRANSITIONS -- what the UI is allowed to offer. */
export const LIFECYCLE_TRANSITIONS: Record<Lifecycle, Lifecycle[]> = {
  active: ["decaying", "paused", "retired"],
  decaying: ["active", "paused", "retired"],
  paused: ["active", "retired"],
  retired: ["active"],
};

/** `active` (the boolean column) is derived from lifecycle, not set
 * independently: set_lifecycle() writes active=1 for active/decaying and 0 for
 * paused/retired. The UI must not present them as separate switches. */
export function isCollecting(lifecycle: Lifecycle): boolean {
  return lifecycle === "active" || lifecycle === "decaying";
}

/** One row of the list view: an interest plus its live funnel. */
export interface InterestStat {
  key: string;
  title: string;
  /** OWNER/INFERRED/EMERGING/EXPLORATORY ladder (interest_state.py). */
  layer: string;
  lifecycle: Lifecycle;
  /** Mirrors the interests.active column; always isCollecting(lifecycle). */
  active: boolean;
  min_score: number;
  sources: string[];
  parent_key: string | null;
  /** Items collected with this interest as origin. */
  collected: number;
  /** Items that keyword-matched it. Deliberately far larger than `collected`
   * (mean 10.7 matches/item, 97% of items match at least two interests) -- the
   * gap IS the finding, so the column shows it rather than hiding it. */
  matched: number;
  /** Scored items clearing `min_score`, recomputed at today's bar. */
  above_bar: number;
  /** Notifications actually sent. */
  delivered: number;
  /** Daily above-bar counts across the window, oldest to newest. Sparkline. */
  daily_above_bar: number[];
  last_delivered_at: string | null;
  /** Consecutive days with zero above-bar items -- what drives decaying (30)
   * and auto-pause (45) in offers.py `silence_days`. Null when unknown. */
  silence_days: number | null;
  /** The server's dead-weight verdict, so the rule lives in one place. */
  dead_weight: boolean;
  /** The last N final_score values, newest first.
   *
   * CONTRACT NOTE: PR L's one ADDITION to section 7.3's stats response. The
   * editor must preview "at this bar, N of the last M scored items would
   * clear" -- the single most useful number when tuning a bar, and how the
   * 08-13 rebalance was done by hand -- but section 7.3 defines no endpoint
   * for it. Shipping the raw scores on the stats payload makes the preview
   * instant as the slider moves, which a per-keystroke endpoint could not.
   * Suggested N = 120 (MAX_SCORES per cycle). */
  recent_scores: number[];
}

export interface StatsTotals {
  collected: number;
  matched: number;
  above_bar: number;
  delivered: number;
  /** Rows whose lifecycle still collects (active + decaying). */
  active_interests: number;
  /** Every row, whatever its lifecycle. */
  total_interests: number;
  dead_weight: number;
}

export interface StatsResponse {
  window: string;
  /** Actual bounds the aggregates cover -- the live data window is finite. */
  from: string;
  to: string;
  interests: InterestStat[];
  totals: StatsTotals;
}

export type EdgeKind = "co_engagement" | "semantic" | "bridge_offer" | "parent";

export interface EdgeEvidence {
  /** Co-engagement lift, normalised. Raw co-match counts are NOT a source
   * here: with 97% of items matching two or more interests they measure the
   * matcher's looseness, not a relationship. */
  lift?: number;
  shared_items?: number;
  /** Bridging conversations, for a bridge edge. */
  quotes?: EvidenceQuote[];
  note?: string;
}

/** interest_edges. Columns are a_key/b_key; the section 7.3 wire shape is
 * {a,b,kind,weight,evidence}, and the wire shape wins. */
export interface InterestEdge {
  a: string;
  b: string;
  kind: EdgeKind;
  weight: number;
  evidence: EdgeEvidence;
  computed_at?: string;
}

export interface EdgesResponse {
  edges: InterestEdge[];
}

/** The editor's payload. */
export interface InterestPayload {
  key: string;
  title: string;
  description: string;
  positive_signals: string[];
  negative_signals: string[];
  min_score: number;
  sources: string[];
  parent_key: string | null;
  /** Lifecycle is the write; `active` is derived from it server-side. */
  lifecycle: Lifecycle;
}

/** The subset of the payload an offer decision may carry.
 *
 * Exactly PR H's `accept()` allowlist -- {title, description,
 * positive_signals, negative_signals, min_score, sources, parent_key}. Note
 * what is NOT in it: `lifecycle`/`active`. accept() sets the interest active
 * itself, and passing an unknown key raises rather than being ignored. */
export type OfferEdits = Partial<Pick<
  InterestPayload,
  "title" | "description" | "positive_signals" | "negative_signals"
  | "min_score" | "sources" | "parent_key"
>>;

export interface SaveResponse {
  ok: boolean;
  key: string;
  /** From sync v2 (PR I) running in-process. */
  synced_at: string;
  missions_cancelled: number;
}

export interface DecideRequest {
  action: "accept" | "reject" | "snooze";
  edits?: OfferEdits;
  note?: string;
  /** Snooze horizon. PR H's Rules.snooze_days default is 30; omitting the
   * field takes that default. */
  snooze_days?: number;
}

export interface DecideResponse {
  ok: boolean;
  /** Present on accept: the key of the interest that now exists. */
  interest_key?: string;
  status: OfferStatus;
  /** Rejecting an offer appends its key and signal tokens to interests.json's
   * `blocked_derived_terms` and suppresses them for Rules.reject_block_days
   * (180). The UI warns before rejecting, and echoes what was blocked after. */
  blocked_terms?: string[];
}

export interface GenerateResponse {
  ok: boolean;
  offered: number;
}

/** The editable half of an interest, as `GET /observatory/api/interest/<key>`
 * already serves it today (observatory/db.py::interest_detail).
 *
 * This is the one call in the workspace that needs NO new server code: the
 * read-only detail endpoint has existed since the Observatory shipped and
 * already returns the definition plus the signal lists. The editor loads it on
 * open, because the bulk stats payload deliberately does not carry every
 * interest's full description and signal arrays -- 46 of those would bloat a
 * list response that exists to be fast.
 *
 * Only the fields the editor actually needs are modelled; the live payload
 * also carries events, generations, missions, discoveries, failures and
 * feedback, which this workspace does not read. */
export interface InterestDetailResponse {
  definition: {
    key: string;
    title: string;
    description?: string;
    min_score?: number;
    active?: number | boolean;
    layer?: string;
    parent_key?: string | null;
  };
  signals: {
    positive: string[];
    negative: string[];
  };
}
