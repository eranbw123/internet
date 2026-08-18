"""Interest offers: the store, the lifecycle state machine, and the sweeps.

An *offer* is a proposed interest the owner has not decided on yet. Offers
arrive in the `ai` repo's contract-v2 artifact (`candidates[]`, read through
discovery/personal_state.py -- the only reader of that file) or are raised
locally by the decay sweep when an interest stops earning its place.

Lifecycle, one append-only `offer_events` row per transition:

    proposed --importer--> offered --accept--> accepted --sync--> ACTIVE
                              |--reject------> rejected
                              |--snooze 30d--> snoozed --wakes--> offered
                              `--45d silence-> expired

and the interest half of it, on `interests.lifecycle` (one `interest_events`
row per transition, actor 'offers'):

    active --30d, 0 above-bar--> decaying --items return--> active
                                     |--45d, 0 above-bar--> paused (reversible)
                                     `--owner click / 3x negative feedback--> retired

Two measured facts shape this module:

* Ranking is arithmetic, never a model call -- this module makes NO LLM/network
  call of any kind, and holds no provider. Semantic similarity between a
  candidate and the existing interests is *computed by the producer* and shipped
  in the artifact (`similarity_to_existing`); here it is only read. Dedup is
  therefore semantic, not exact-hash: a paraphrase of an interest the owner
  already follows (in either language) is rejected by the similarity floor,
  which is the bug class exact-hash dedup missed.
* The decay sweep refuses to judge silence it cannot attribute to the interest:
  if the pipeline itself scored nothing in the window (it can be paused for
  days), or an interest has never had enough items attributed to it, nothing is
  paused. Auto-pause is always reversible with `undo_auto_pause()` and always
  announces itself -- it is never a silent deactivation.

Fully offline and stdlib-only, like interest_state.py.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from . import db, matching, personal_state
from .interests import load_blocked

# --- offer status ------------------------------------------------------------

PROPOSED = "proposed"
OFFERED = "offered"
ACCEPTED = "accepted"
REJECTED = "rejected"
SNOOZED = "snoozed"
EXPIRED = "expired"
STATUSES = (PROPOSED, OFFERED, ACCEPTED, REJECTED, SNOOZED, EXPIRED)

# The whole state machine, as data. Every write goes through _transition(),
# which refuses anything not listed here -- an offer can never skip the inbox
# into 'accepted', and a decided offer can never be silently re-decided.
# 'rejected'/'expired' -> 'offered' exists for exactly one caller: the decay
# sweep re-raising a retirement offer after its cool-off (see
# Rules.retire_reoffer_days).
TRANSITIONS = {
    PROPOSED: frozenset({OFFERED, REJECTED}),
    OFFERED: frozenset({ACCEPTED, REJECTED, SNOOZED, EXPIRED}),
    SNOOZED: frozenset({OFFERED, ACCEPTED, REJECTED, EXPIRED}),
    EXPIRED: frozenset({OFFERED}),
    REJECTED: frozenset({OFFERED}),
    ACCEPTED: frozenset(),
}

# Offer kinds. 'retire' offers are raised locally by the sweep; the rest come
# from the artifact.
KINDS = ("new", "bridge", "merge", "split", "revive", "retire")

# --- interest lifecycle ------------------------------------------------------

ACTIVE = "active"
DECAYING = "decaying"
PAUSED = "paused"
RETIRED = "retired"
LIFECYCLES = (ACTIVE, DECAYING, PAUSED, RETIRED)

LIFECYCLE_TRANSITIONS = {
    ACTIVE: frozenset({DECAYING, PAUSED, RETIRED}),
    DECAYING: frozenset({ACTIVE, PAUSED, RETIRED}),
    PAUSED: frozenset({ACTIVE, RETIRED}),
    RETIRED: frozenset({ACTIVE}),
}

# Prefix keeping a sweep-raised retirement offer in its own namespace, so it
# can never collide with a new-interest offer proposing the same theme.
RETIRE_PREFIX = "retire:"

# Actors, mirroring interest_events' vocabulary.
GENERATOR, IMPORTER, OWNER_UI, TIMER, PIPELINE = (
    "generator", "importer", "owner_ui", "timer", "pipeline"
)


class OfferError(Exception):
    """Base for every refusal this module makes."""


class UnknownOffer(OfferError):
    """No offer row with that key."""


class InvalidTransition(OfferError):
    """The requested status/lifecycle move is not in the state machine."""


@dataclass(frozen=True)
class Rules:
    """Every threshold in one place, constructed directly -- no new env vars
    (same posture as interest_state.Rules). The weights are the measured
    design's: evidence .30, recurrence .15, recency .15, novelty .20,
    expected_yield .20."""

    # Ranking weights (must sum to 1; asserted by score_candidate()).
    w_evidence_strength: float = 0.30
    w_recurrence: float = 0.15
    w_recency: float = 0.15
    w_novelty: float = 0.20
    w_expected_yield: float = 0.20

    evidence_saturation: int = 8        # log-saturation point, in conversations
    recurrence_months: float = 4.0      # active months for a full recurrence term
    recency_half_life_days: float = 90.0

    # Floors an offer must clear before it is ever shown.
    min_offer_score: float = 0.45
    min_convs: int = 3
    min_active_months: int = 2
    deep_pair_convs: int = 2            # a two-conversation research dive qualifies...
    deep_pair_depth: float = 0.6        # ...only if the owner pushed past the surface
    revive_multiplier: int = 2          # a re-offer of a retired theme clears 2x the bar

    # Semantic dedup against the existing interest set (from the artifact).
    semantic_dup_similarity: float = 0.70
    signal_overlap_attach: float = 0.50

    # Dedup between two OFFERS, which is a different question from dedup
    # between a candidate and an interest -- see _evidence_overlap(). Measured
    # on the live inbox 2026-08-18: the four duplicate pairs sat at exactly
    # .50 shared evidence and the nearest distinct pair at .25, a clean 2x gap
    # with nothing in between, so the bar sits at the bottom of the duplicate
    # cluster. `min_shared` stops a pair of two-conversation offers tripping
    # the ratio on a single shared conversation.
    evidence_overlap_duplicate: float = 0.50
    evidence_overlap_min_shared: int = 2

    # Inbox volume. `target_inbox_size` is the standing invariant the owner
    # asked for: the inbox aims to hold this many LIVE offers at all times --
    # topped up on the hourly import tick and again the moment he decides one,
    # so working through the inbox refills it instead of draining it. Only
    # status 'offered' counts (see live_offer_count).
    #
    # `max_offers_per_run` is the per-run burst cap and is deliberately equal
    # to the target: a cap BELOW the target would just mean the inbox fills in
    # two steps inside one tick, which is two numbers describing one rule.
    # Both budgets are additionally clamped to the room left under the target,
    # so neither an import nor a top-up can overshoot it.
    target_inbox_size: int = 10
    max_offers_per_run: int = 10
    serendipity_slots: int = 1

    # Offer clocks, in days.
    expire_days: int = 45
    snooze_days: int = 30
    reject_block_days: int = 180

    # Interest decay clocks, in days of zero above-bar items.
    decay_idle_days: int = 30
    auto_pause_days: int = 45
    retire_reoffer_days: int = 90       # cool-off before a declined retire offer returns
    retire_negative_feedback: int = 3   # negatives that retire an already-decaying interest

    # Refusals to judge (see the module docstring).
    min_attributed_items: int = 5       # items/scores an interest needs before it can be paused
    pipeline_activity_days: int = 7     # window in which the pipeline must have scored *something*


DEFAULT_RULES = Rules()


# --- time helpers ------------------------------------------------------------

def _now(now=None):
    return now or datetime.now(timezone.utc)


def _stamp(dt):
    return dt.isoformat(timespec="seconds")


def _parse(ts):
    """Timestamps in this DB are db.now() (UTC, offset-suffixed), but the
    artifact's evidence dates are the producer's -- plain dates and 'Z' forms
    included. Everything naive is read as UTC; anything unparseable is None,
    never an exception."""
    if not ts:
        return None
    text = str(ts).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _days_between(later, earlier):
    if earlier is None or later is None:
        return None
    return (later - earlier).total_seconds() / 86400.0


# --- normalization & tokens ---------------------------------------------------

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_HEBREW = re.compile(r"[\u0590-\u05FF]{2,}")


def normalize_key(key):
    """Slug normalization -- dedup layer 1. Lowercase, split on anything
    non-word, drop the matcher's stopwords, singularize ASCII plurals, sort.
    Deliberately unicode-aware: a Hebrew-titled theme normalizes to its own
    letters rather than to the empty string."""
    words = []
    for raw in _WORD.findall((key or "").lower()):
        word = raw
        if word in matching.STOPWORDS:
            continue
        if word.isascii() and len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return "-".join(sorted(words))


def signal_tokens(*parts):
    """Distinctive tokens of a title/description/signal list, for dedup layer
    2. `matching._tokens` is the repo's one tokenizer and stays authoritative
    for Latin text (4+ chars, stopwords dropped); Hebrew is added on top of it
    because that regex is ASCII-only and a quarter of this corpus is Hebrew."""
    text = " ".join(
        " ".join(str(x) for x in part) if isinstance(part, (list, tuple, set)) else str(part or "")
        for part in parts
    )
    return matching._tokens(text) | {t for t in _HEBREW.findall(text)}


# --- ranking (pure) ------------------------------------------------------------

def score_candidate(candidate, rules=None, now=None):
    """(score, terms) for one artifact candidate. Pure: no DB, no clock of its
    own, no model call -- the model rates (expected_yield, similarity), code
    ranks. Every term is returned alongside the score because the inbox has to
    show its work."""
    rules = rules or DEFAULT_RULES
    now = _now(now)
    weights = (
        rules.w_evidence_strength + rules.w_recurrence + rules.w_recency
        + rules.w_novelty + rules.w_expected_yield
    )
    assert abs(weights - 1.0) < 1e-9, f"offer weights must sum to 1, got {weights}"

    durability = candidate.get("durability") or {}
    n_convs = _as_number(durability.get("n_convs"), 0)
    active_months = _as_number(durability.get("active_months"), 0)
    recency_days = durability.get("recency_days")
    if recency_days is None:
        recency_days = _recency_from_evidence(candidate, now)
    recency_days = _as_number(recency_days, 10_000)

    evidence_strength = _clamp(
        math.log1p(max(n_convs, 0)) / math.log1p(max(rules.evidence_saturation, 1))
    )
    recurrence = _clamp(active_months / rules.recurrence_months) if rules.recurrence_months else 0.0
    recency = _clamp(0.5 ** (max(recency_days, 0) / rules.recency_half_life_days))
    novelty = _clamp(1.0 - max_similarity(candidate))
    expected_yield = _clamp(_as_number(candidate.get("expected_yield"), 0.0))

    terms = {
        "evidence_strength": round(evidence_strength, 4),
        "recurrence": round(recurrence, 4),
        "recency": round(recency, 4),
        "novelty": round(novelty, 4),
        "expected_yield": round(expected_yield, 4),
    }
    score = (
        rules.w_evidence_strength * evidence_strength
        + rules.w_recurrence * recurrence
        + rules.w_recency * recency
        + rules.w_novelty * novelty
        + rules.w_expected_yield * expected_yield
    )
    terms["weights"] = {
        "evidence_strength": rules.w_evidence_strength,
        "recurrence": rules.w_recurrence,
        "recency": rules.w_recency,
        "novelty": rules.w_novelty,
        "expected_yield": rules.w_expected_yield,
    }
    terms["recency_days"] = round(recency_days, 2)
    return round(_clamp(score), 4), terms


def passes_floors(candidate, score, rules=None):
    """(ok, reason). The durability gate is the anti-errand rule: three
    conversations across two months, OR a deep two-conversation dive. A
    'revive' offer -- a theme that was already retired once -- clears the
    evidence bar `revive_multiplier` times over, which is what stops a
    retired interest from flapping straight back in."""
    rules = rules or DEFAULT_RULES
    # The revive multiplier is an EVIDENCE bar, not a score bar (same shape as
    # interest_state's re-entry rule): a theme that was already retired once
    # comes back only on twice the conversations, never on a prettier score.
    multiplier = rules.revive_multiplier if candidate.get("kind") == "revive" else 1
    if score < rules.min_offer_score:
        return False, f"score {score:.3f} below floor {rules.min_offer_score:.2f}"

    durability = candidate.get("durability") or {}
    n_convs = _as_number(durability.get("n_convs"), 0)
    active_months = _as_number(durability.get("active_months"), 0)
    depth = max(
        [_as_number(e.get("depth"), 0.0) for e in candidate.get("evidence") or []] or [0.0]
    )
    durable = n_convs >= rules.min_convs * multiplier and active_months >= rules.min_active_months
    deep_pair = n_convs >= rules.deep_pair_convs * multiplier and depth >= rules.deep_pair_depth
    if not (durable or deep_pair):
        return False, (
            f"durability gate: n_convs={n_convs:g}, active_months={active_months:g}, "
            f"max depth={depth:.2f}"
        )
    return True, ""


def evidence_conversations(row):
    """Every conversation id an offer or candidate rests on, from both places
    the contract puts them: the `source_conversations` list and the per-quote
    `conversation_id` on each evidence entry."""
    ids = set()
    raw = row.get("source_conversations") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            raw = []
    ids |= {str(c) for c in raw if c}
    evidence = row.get("evidence") or []
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence or "[]")
        except ValueError:
            evidence = []
    for entry in evidence:
        if isinstance(entry, dict) and entry.get("conversation_id"):
            ids.add(str(entry["conversation_id"]))
    return ids


def _evidence_overlap(a_convs, b_convs):
    """(share, shared) -- the share of the SMALLER side's conversations that
    both sides rest on.

    This is the dedup layer that judges one offer against another, and it is
    deliberately NOT lexical. Two offers written from the same handful of
    conversations are the same idea however differently the producer worded
    them, and two offers with similar words drawn from different conversations
    usually are not. Measured on the live inbox (2026-08-18, all 45 pairs of
    the 10 offers), this separates the four duplicate pairs at .50 from the
    nearest distinct pair at .25 with nothing in between -- while the repo's
    lexical rule found only two of the four:

      * 'extraction-shooters-competitive-fps' vs 'competitive-shooter-
        performance' overlap only .35 lexically, yet both come out of the same
        aim-training and mouse-sensitivity conversations.
      * 'roguelike-souls-run-design' vs 'game-systems-theorycrafting' share no
        key token at all, yet both describe tear-delay stat math and build
        interaction, from the same two conversations.

    And it correctly leaves alone the pair that only LOOKS duplicate:
    'roguelike-souls-run-design' vs 'roguelike-run-progression-design' share a
    key stem but just one conversation, and are two real interests.

    Being evidence-based it is also language-independent, which a quarter of
    this corpus needs -- token overlap is least reliable exactly on the Hebrew
    conversations.
    """
    if not a_convs or not b_convs:
        return 0.0, 0
    shared = a_convs & b_convs
    return len(shared) / min(len(a_convs), len(b_convs)), len(shared)


def _similarity_to(candidate, key):
    """The producer's similarity between this candidate and one named key, 0
    when it reported none. The producer scores similarity against the INTEREST
    set, so this is usually 0 for an offer peer -- it is read anyway so that
    the rare case where a peer's key does appear (a theme that was an interest
    when the artifact was generated) is honoured rather than ignored."""
    sims = [
        _as_number(entry.get("sim", entry.get("similarity")), 0.0)
        for entry in candidate.get("similarity_to_existing") or []
        if isinstance(entry, dict) and entry.get("key") == key
    ]
    return max(sims) if sims else 0.0


def max_similarity(candidate):
    """The strongest similarity the producer reported against any existing
    interest, 0 when it reported none."""
    sims = [
        _as_number(entry.get("sim", entry.get("similarity")), 0.0)
        for entry in candidate.get("similarity_to_existing") or []
        if isinstance(entry, dict)
    ]
    return max(sims) if sims else 0.0


def rank(scored, rules=None):
    """Pick the run's offers: the top `max_offers_per_run` by score, except
    that `serendipity_slots` of them are reserved for the producer's
    deliberately exploratory pick(s). The exploratory pick bypasses rank but
    NOT the floors -- it is already floor-checked by the caller."""
    rules = rules or DEFAULT_RULES
    ordered = sorted(scored, key=lambda c: (-c["score"], c["key"]))
    if rules.max_offers_per_run <= 0:
        return []
    top = ordered[: rules.max_offers_per_run]
    if not rules.serendipity_slots or any(c.get("exploratory") for c in top):
        return top
    exploratory = [c for c in ordered if c.get("exploratory") and c not in top]
    if not exploratory:
        return top
    keep = max(rules.max_offers_per_run - rules.serendipity_slots, 0)
    return top[:keep] + exploratory[: rules.max_offers_per_run - keep]


def _clamp(value):
    return 0.0 if value < 0 else (1.0 if value > 1 else float(value))


def _as_number(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def _recency_from_evidence(candidate, now):
    dates = [_parse(e.get("date")) for e in candidate.get("evidence") or []]
    newest = max([d for d in dates if d], default=None)
    days = _days_between(now, newest)
    return 10_000 if days is None else max(days, 0)


# --- reading the store ---------------------------------------------------------

_JSON_COLUMNS = (
    "positive_signals", "negative_signals", "suggested_sources", "related_keys",
    "score_terms", "evidence", "source_conversations", "durability", "similarity",
)


def _row_to_offer(row):
    offer = {k: row[k] for k in row.keys()}
    for column in _JSON_COLUMNS:
        offer[column] = json.loads(offer.get(column) or ("{}" if column in
                                                         ("score_terms", "durability") else "[]"))
    offer["exploratory"] = bool(offer.get("exploratory"))
    return offer


def get_offer(conn, key):
    row = conn.execute("SELECT * FROM interest_offers WHERE key = ?", (key,)).fetchone()
    return _row_to_offer(row) if row else None


def list_offers(conn, status=None, kind=None, limit=None):
    """Offers as render-ready dicts (JSON columns already decoded), strongest
    first. This is the read half PRs J/L call."""
    sql = "SELECT * FROM interest_offers"
    where, params = [], []
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if kind:
        kinds = [kind] if isinstance(kind, str) else list(kind)
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY score DESC, created_at DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_row_to_offer(r) for r in conn.execute(sql, params).fetchall()]


def inbox(conn, limit=None):
    """What the owner is being asked to decide right now."""
    return list_offers(conn, status=OFFERED, limit=limit)


def offer_events(conn, key):
    """The full append-only chain for one offer, oldest first."""
    rows = conn.execute(
        "SELECT at, actor, action, from_status, to_status, detail FROM offer_events"
        " WHERE offer_key = ? ORDER BY id",
        (key,),
    ).fetchall()
    return [
        {
            "at": r["at"], "actor": r["actor"], "action": r["action"],
            "from_status": r["from_status"], "to_status": r["to_status"],
            "detail": json.loads(r["detail"]),
        }
        for r in rows
    ]


def offer_detail(conn, key):
    """Offer + its provenance chain in one payload -- what the inbox renders:
    evidence quotes, the conversations they came from, every score term, the
    similarity list, and every transition so far."""
    offer = get_offer(conn, key)
    if offer is None:
        return None
    offer["events"] = offer_events(conn, key)
    return offer


def add_offer_event(conn, key, actor, action, from_status=None, to_status=None, detail=None):
    """Append one row. Nothing ever UPDATEs or DELETEs offer_events."""
    conn.execute(
        """
        INSERT INTO offer_events (at, offer_key, actor, action, from_status, to_status, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (db.now(), key, actor, action, from_status, to_status,
         json.dumps(detail or {}, ensure_ascii=False)),
    )
    conn.commit()


# --- writing the store ---------------------------------------------------------

def insert_offer(conn, offer, *, actor=IMPORTER, now=None):
    """Create one offer row at status 'proposed' and log its birth. Returns
    the stored offer, or None when the key already exists -- re-importing an
    artifact must never duplicate or mutate a decided offer.

    The read-then-insert is checked twice: once here, and once by the UNIQUE
    index on `key`. The second check is the one that matters now that an hourly
    import and a click-driven top-up can run at the same moment -- between the
    SELECT and the INSERT, the other run may have offered this very key. That
    is a legitimate outcome, not a failure: the row exists, which is what both
    runs wanted, so the loser returns None exactly as if it had lost the SELECT.
    """
    now = _now(now)
    if get_offer(conn, offer["key"]) is not None:
        return None
    columns = (
        offer["key"], offer.get("kind", "new"), offer.get("title") or offer["key"],
        offer.get("description", ""),
        _dump(offer.get("positive_signals", [])), _dump(offer.get("negative_signals", [])),
        offer.get("suggested_min_score"),
        _dump(offer.get("suggested_sources") or ["web_search"]),
        offer.get("parent_key"), _dump(offer.get("related_keys", [])),
        offer.get("score"), _dump(offer.get("score_terms", {})),
        _dump(offer.get("evidence", [])), _dump(offer.get("source_conversations", [])),
        _dump(offer.get("durability", {})), _dump(offer.get("similarity", [])),
        1 if offer.get("exploratory") else 0, PROPOSED,
        offer.get("artifact_sha256", ""), offer.get("generated_at", ""), _stamp(now),
    )
    try:
        conn.execute(
            """
            INSERT INTO interest_offers
                (key, kind, title, description, positive_signals, negative_signals,
                 suggested_min_score, suggested_sources, parent_key, related_keys,
                 score, score_terms, evidence, source_conversations, durability,
                 similarity, exploratory, status, artifact_sha256, generated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            columns,
        )
    except sqlite3.IntegrityError:
        # Another run offered this key between the SELECT above and this
        # INSERT. The row exists, which is what both runs wanted.
        conn.rollback()
        return None
    conn.commit()
    add_offer_event(
        conn, offer["key"], actor, "propose", None, PROPOSED,
        {
            "kind": offer.get("kind", "new"),
            "score": offer.get("score"),
            "artifact_sha256": offer.get("artifact_sha256", ""),
            "evidence_count": len(offer.get("evidence", [])),
            "source_conversations": offer.get("source_conversations", []),
        },
    )
    return get_offer(conn, offer["key"])


def _dump(value):
    # ensure_ascii=False: evidence quotes are the owner's own words and are
    # often Hebrew -- storing them escaped would make the DB unreadable by eye
    # and every LIKE query useless.
    return json.dumps(value, ensure_ascii=False)


def _transition(conn, key, to_status, *, actor, action, detail=None, sets=None, now=None):
    offer = get_offer(conn, key)
    if offer is None:
        raise UnknownOffer(key)
    from_status = offer["status"]
    if to_status not in TRANSITIONS.get(from_status, frozenset()):
        raise InvalidTransition(
            f"offer {key!r}: {from_status} -> {to_status} is not a legal transition"
        )
    assignments = ["status = ?"]
    params = [to_status]
    for column, value in (sets or {}).items():
        assignments.append(f"{column} = ?")
        params.append(value)
    params.append(key)
    conn.execute(f"UPDATE interest_offers SET {', '.join(assignments)} WHERE key = ?", params)
    conn.commit()
    add_offer_event(conn, key, actor, action, from_status, to_status, detail)
    return get_offer(conn, key)


def offer(conn, key, *, actor=IMPORTER, detail=None):
    """proposed -> offered: the offer enters the owner's inbox."""
    return _transition(conn, key, OFFERED, actor=actor, action="offer", detail=detail)


def snooze(conn, key, *, days=None, note="", actor=OWNER_UI, rules=None, now=None):
    """'not now' -- the offer sleeps and wakes back into the inbox."""
    rules = rules or DEFAULT_RULES
    now = _now(now)
    until = _stamp(now + timedelta(days=days if days is not None else rules.snooze_days))
    return _transition(
        conn, key, SNOOZED, actor=actor, action="snooze",
        detail={"until": until, "note": note},
        sets={"snoozed_until": until, "decided_note": note},
    )


def reject(conn, key, *, note="", actor=OWNER_UI, interests_path=None, rules=None, now=None):
    """'not this' -- and it stays rejected everywhere. The offer's key and
    signal tokens are appended to interests.json's `blocked_derived_terms`
    (the existing ladder blocklist, so the token ladder honours the decision
    too) when a path is given, and are suppressed by the importer for
    `reject_block_days` regardless, straight from this row."""
    rules = rules or DEFAULT_RULES
    now = _now(now)
    offer_row = get_offer(conn, key)
    if offer_row is None:
        raise UnknownOffer(key)
    # Checked before the JSON write below: a refused transition must leave
    # interests.json untouched, not block terms on its way to raising.
    if REJECTED not in TRANSITIONS.get(offer_row["status"], frozenset()):
        raise InvalidTransition(
            f"offer {key!r}: {offer_row['status']} -> {REJECTED} is not a legal transition"
        )
    terms = blocked_terms_for(offer_row)
    written = append_blocked_terms(interests_path, terms) if interests_path else []
    updated = _transition(
        conn, key, REJECTED, actor=actor, action="reject",
        detail={"note": note, "blocked_terms": terms, "written_to_json": written},
        sets={"decided_at": _stamp(now), "decided_note": note},
    )
    updated["blocked_terms"] = terms
    updated["blocked_terms_written"] = written
    return updated


def accept(conn, key, *, edits=None, note="", actor=OWNER_UI, sync=None, now=None):
    """Accept an offer, optionally with the owner's edits, and hand back the
    interests.json-shaped entry it becomes.

    This module deliberately does NOT write `interests`/interests.json itself:
    that is sync v2's job (PR I) behind the write API (PR J). Pass `sync` --
    any callable taking the entry -- to close the loop in one call; the offer
    is then activated (interest lifecycle 'active') as soon as the interest
    row exists. Without it, the caller runs its own sync and calls
    `activate()`.
    """
    now = _now(now)
    edits = dict(edits or {})
    allowed = {
        "title", "description", "positive_signals", "negative_signals",
        "min_score", "suggested_min_score", "sources", "suggested_sources", "parent_key",
    }
    unknown = set(edits) - allowed
    if unknown:
        raise OfferError(f"unsupported edit field(s): {sorted(unknown)}")

    sets = {"decided_at": _stamp(now), "decided_note": note}
    column_for = {
        "title": ("title", str), "description": ("description", str),
        "positive_signals": ("positive_signals", _dump),
        "negative_signals": ("negative_signals", _dump),
        "min_score": ("suggested_min_score", float),
        "suggested_min_score": ("suggested_min_score", float),
        "sources": ("suggested_sources", _dump),
        "suggested_sources": ("suggested_sources", _dump),
        "parent_key": ("parent_key", lambda v: v),
    }
    for name, value in edits.items():
        column, cast = column_for[name]
        sets[column] = cast(value)

    updated = _transition(
        conn, key, ACCEPTED, actor=actor, action="accept",
        # The edit diff is kept verbatim on the event: PR N's learning loop
        # reads it to notice, e.g., that the owner keeps lowering the bar.
        detail={"note": note, "edits": edits},
        sets=sets,
    )
    entry = interest_entry(updated, accepted_at=_stamp(now))
    result = {"ok": True, "interest_key": updated["key"], "entry": entry, "offer": updated}
    if sync is not None:
        sync(entry)
        result["activated"] = activate(conn, key, now=now)
    return result


def expire(conn, key, *, actor=TIMER, note="", now=None):
    """45 days in the inbox with no decision is itself a decision."""
    now = _now(now)
    return _transition(
        conn, key, EXPIRED, actor=actor, action="expire",
        detail={"note": note or "no decision within the offer window"},
        sets={"decided_at": _stamp(now)},
    )


def wake(conn, key, *, actor=TIMER):
    """A snoozed offer's timer fires: back into the inbox."""
    return _transition(
        conn, key, OFFERED, actor=actor, action="wake", sets={"snoozed_until": None}
    )


def duplicate_pairs(conn, rules=None, now=None):
    """Every pair of LIVE offers that rest on the same evidence, as
    (loser_key, winner_key, why), strongest duplicate first.

    Which of a pair survives is decided on how much each one is standing on,
    never on the composite score: that score was measured to be unanchored --
    the same document scored .640 and .943 on consecutive days, and the two
    producers clear a fixed bar at wildly different rates -- so comparing two
    offers written by different runs on it would be comparing two different
    rulers. Evidence count is the same quantity for both. Score is used only to
    break a tie between offers standing on exactly as much, where it is an
    ordinal within one comparison rather than a threshold, and the key breaks
    any remaining tie so the result is deterministic.
    """
    rules = rules or DEFAULT_RULES
    rows = list_offers(conn, status=OFFERED)
    convs = {r["key"]: evidence_conversations(r) for r in rows}
    rank_of = {
        r["key"]: (len(convs[r["key"]]), len(r["evidence"]), r["score"] or 0.0, r["key"])
        for r in rows
    }
    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            share, shared = _evidence_overlap(convs[a["key"]], convs[b["key"]])
            if share < rules.evidence_overlap_duplicate or shared < rules.evidence_overlap_min_shared:
                continue
            winner, loser = (a, b) if rank_of[a["key"]] >= rank_of[b["key"]] else (b, a)
            pairs.append((
                loser["key"], winner["key"],
                f"{share:.0%} of the smaller offer's evidence ({shared} shared "
                f"conversation(s)) is the same evidence",
                share, shared,
            ))
    pairs.sort(key=lambda t: (-t[3], -t[4], t[0]))
    return [(loser, winner, why) for loser, winner, why, _s, _n in pairs]


def reconcile_duplicates(conn, *, rules=None, now=None, actor=IMPORTER):
    """Fold every near-duplicate pair already sitting in the inbox into one
    offer each, keeping the evidence. Returns a summary; never raises.

    This is the repair half of the offer-vs-offer dedup: _promote() stops new
    duplicates arriving, and this clears the ones that arrived while the inbox
    check was exact-key only. It is idempotent -- once a pair is merged there
    is no pair left to find -- so it is safe to run on every top-up, which is
    where it runs, so the inbox heals itself rather than waiting to be repaired
    by hand.

    Pairs are recomputed after each merge rather than taken from one snapshot.
    Duplicates come in clusters, not only pairs (three offers can share one
    conversation set), and merging the first pair changes which of the rest are
    still duplicates -- and enlarges the survivor's evidence, which can reveal
    a duplicate that was below the bar a moment ago.
    """
    rules = rules or DEFAULT_RULES
    summary = {"merged": 0, "pairs": [], "reason": "", "error": ""}
    # Bounded: every pass retires exactly one offer, so it cannot outlast the
    # inbox even if the pair set were somehow unstable.
    for _ in range(len(list_offers(conn, status=OFFERED)) + 1):
        pairs = duplicate_pairs(conn, rules, now)
        if not pairs:
            break
        loser, winner, why = pairs[0]
        try:
            result = supersede(conn, loser, winner, actor=actor, now=now, why=why,
                               note="near-duplicate of an offer already in the inbox")
        except OfferError as e:
            summary["error"] = f"{type(e).__name__}: {e}"
            break
        summary["merged"] += 1
        summary["pairs"].append({
            "superseded": loser, "survivor": winner, "why": why,
            "evidence_added": result["evidence_added"],
        })
    if summary["merged"]:
        summary["reason"] = "; ".join(
            f"{p['superseded']} -> {p['survivor']} ({p['why']}, "
            f"+{p['evidence_added']} quote(s) kept)" for p in summary["pairs"]
        )
    else:
        summary["reason"] = summary["error"] or "no near-duplicate pairs in the inbox"
    return summary


def supersede(conn, loser_key, winner_key, *, actor=IMPORTER, note="", now=None, why=""):
    """Fold a duplicate offer into the one it duplicates, keeping the evidence.

    Two offers that rest on the same conversations are one idea the producer
    wrote up twice, and the owner should be asked about it once. Discarding the
    loser outright would throw away real quotes from real conversations, so its
    evidence and source conversations are merged onto the survivor first --
    the survivor comes out strictly better-evidenced than either was -- and
    only then is the loser retired from the inbox.

    The loser lands on 'expired', which is the one terminal status that carries
    no judgement: unlike 'rejected' it does not blocklist the theme's tokens
    for 180 days, which matters enormously here, because the survivor shares
    those very tokens and would blocklist itself. The `supersede` action and
    the `superseded_by` detail on the event chain are what make it readable
    later as a merge rather than as a timeout.

    Refuses to touch a decided offer: only an undecided one (proposed, offered
    or snoozed) can be superseded, and the lifecycle's own transition table is
    what enforces it. An accepted or rejected offer is the owner's word and is
    never rewritten.
    """
    now = _now(now)
    loser = get_offer(conn, loser_key)
    winner = get_offer(conn, winner_key)
    if loser is None:
        raise UnknownOffer(loser_key)
    if winner is None:
        raise UnknownOffer(winner_key)
    if loser["status"] not in (PROPOSED, OFFERED, SNOOZED):
        raise InvalidTransition(
            f"offer {loser_key!r} is {loser['status']}: a decided offer is never superseded"
        )

    # --- merge the evidence onto the survivor, oldest quote first -----------
    seen = {(e.get("conversation_id"), e.get("quote")) for e in winner["evidence"]}
    added = [e for e in loser["evidence"] if (e.get("conversation_id"), e.get("quote")) not in seen]
    evidence = list(winner["evidence"]) + added
    convs = list(winner["source_conversations"])
    convs += [c for c in loser["source_conversations"] if c not in set(convs)]
    conn.execute(
        "UPDATE interest_offers SET evidence = ?, source_conversations = ? WHERE key = ?",
        (_dump(evidence), _dump(convs), winner_key),
    )
    conn.commit()
    add_offer_event(
        conn, winner_key, actor, "absorb", winner["status"], winner["status"],
        {
            "absorbed": loser_key, "why": why, "note": note,
            "evidence_added": len(added), "conversations_added": len(convs) - len(winner["source_conversations"]),
        },
    )
    updated = _transition(
        conn, loser_key, EXPIRED, actor=actor, action="supersede",
        detail={
            "superseded_by": winner_key, "why": why, "note": note,
            "evidence_merged_into_winner": len(added),
        },
        sets={"decided_at": _stamp(now), "decided_note": note or f"superseded by {winner_key}"},
        now=now,
    )
    return {
        "loser": updated, "winner": get_offer(conn, winner_key),
        "evidence_added": len(added), "why": why,
    }


def interest_entry(offer_row, accepted_at=""):
    """The interests.json entry an accepted offer becomes, carrying an
    `offered_by` back-reference so the interest's origin survives in the file
    the owner edits by hand -- not only in the DB."""
    entry = {
        "key": offer_row["key"],
        "title": offer_row["title"],
        "description": offer_row["description"],
        "positive_signals": list(offer_row["positive_signals"]),
        "negative_signals": list(offer_row["negative_signals"]),
        "sources": list(offer_row["suggested_sources"] or ["web_search"]),
        "offered_by": {
            "offer_key": offer_row["key"],
            "kind": offer_row["kind"],
            "score": offer_row["score"],
            "artifact_sha256": offer_row["artifact_sha256"],
            "generated_at": offer_row["generated_at"],
            "accepted_at": accepted_at or offer_row.get("decided_at") or "",
            "evidence_count": len(offer_row["evidence"]),
            "source_conversations": list(offer_row["source_conversations"]),
        },
    }
    if offer_row["suggested_min_score"] is not None:
        entry["min_score"] = offer_row["suggested_min_score"]
    if offer_row["parent_key"]:
        entry["parent_key"] = offer_row["parent_key"]
    return entry


def blocked_terms_for(offer_row):
    """What a rejection blocks: the offer's own key plus its signal tokens.
    Sorted so a JSON write is stable.

    Nothing, for a retirement offer: declining "retire this?" means the owner
    wants to KEEP the interest, so blocking its key and terms would be the
    exact opposite of the answer given (and would poison every future
    candidate sharing a word with its title)."""
    if offer_row.get("kind") == "retire":
        return []
    tokens = signal_tokens(
        offer_row.get("title"), offer_row.get("positive_signals") or []
    )
    return sorted({offer_row["key"], *tokens})


def append_blocked_terms(path, terms):
    """Append to interests.json's optional top-level `blocked_derived_terms`,
    preserving everything else in the file byte-for-byte-ish (a re-dump with
    indent=2, the file's own style) and writing atomically. Returns the terms
    actually added."""
    with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    existing = list(data.get("blocked_derived_terms", []))
    lowered = {t.lower() for t in existing}
    added = [t for t in terms if t.lower() not in lowered]
    if not added:
        return []
    data["blocked_derived_terms"] = existing + added
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return added


def blocked_offer_keys(conn, rules=None, now=None):
    """Keys the owner rejected inside the block window, plus the tokens that
    came with them -- the importer's memory of 'no'."""
    rules = rules or DEFAULT_RULES
    now = _now(now)
    since = _stamp(now - timedelta(days=rules.reject_block_days))
    rows = conn.execute(
        "SELECT key, title, positive_signals FROM interest_offers"
        " WHERE status = ? AND kind != 'retire' AND COALESCE(decided_at, created_at) >= ?",
        (REJECTED, since),
    ).fetchall()
    blocked = set()
    for row in rows:
        blocked.add(row["key"])
        blocked.add(normalize_key(row["key"]))
        blocked |= signal_tokens(row["title"], json.loads(row["positive_signals"] or "[]"))
    return blocked


# --- importing the artifact -----------------------------------------------------

def artifact_sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _import_state_key(sha):
    return f"offers:artifact:{sha}"


# --- the inbox target ----------------------------------------------------------
#
# The owner's ask, in his words: "I want interests to like aim to always have 10
# suggestions ... it should fill automatically to 10 suggested interests. And
# when I accept or reject, it should run again."
#
# Two mechanisms carry that, and they are the same arithmetic run from two
# places: the hourly `offers --import` tick tops up on a clock, and
# observatory/manage.py tops up the instant a decision is recorded, so working
# through the inbox refills it instead of draining it.

TOPUP_STATE_KEY = "offers:topup:last"


def live_offer_count(conn):
    """How many offers the owner is actually being asked to decide right now --
    the number the target is about.

    ONLY status 'offered' counts. 'snoozed' is deliberately excluded even
    though a snoozed offer is undecided: the owner said "not now" and the
    inbox hides it until its timer wakes it, so counting it would leave the
    inbox he can SEE short by exactly the number of things he chose not to look
    at. 'proposed' is excluded because it is the momentary state between
    insert_offer() and offer(), never a resting place. Every decided status
    (accepted, rejected, expired) is out by definition.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM interest_offers WHERE status = ?", (OFFERED,)
    ).fetchone()[0]


def _reserved_count(conn):
    """Offers occupying a slot under the target: everything the owner can see,
    plus the momentary 'proposed' rows a run has just inserted and is about to
    show.

    Counting 'proposed' is what closes the one window in which two concurrent
    runs could together overshoot: run A inserts its row, run B counts the
    inbox before A has transitioned it, and both conclude there is room. This
    is the budget's number; live_offer_count() stays the owner's number, and
    the two differ only for the microseconds a promotion is in flight.

    _promote() never leaves a row parked at 'proposed' -- if the transition
    fails the row is removed again -- so a crashed run cannot leak a slot that
    nothing would ever fill or free.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM interest_offers WHERE status IN (?, ?)", (PROPOSED, OFFERED)
    ).fetchone()[0]


def _budget(conn, rules, cap=None):
    """(live, how many offers this run may add). Clamped to the room left under
    the target, so no single run can overshoot it -- and clamped at zero, so a
    target that has been lowered never removes anything. Offers are only ever
    added by a run; they leave only by the owner's decision or by the sweep's
    timers."""
    live = live_offer_count(conn)
    room = max(rules.target_inbox_size - _reserved_count(conn), 0)
    return live, min(room, rules.max_offers_per_run if cap is None else cap)


def _peer(key, title, signals, status, row=None):
    return {
        "key": key,
        "normalized": normalize_key(key),
        "tokens": signal_tokens(title, signals or []),
        "status": status,
        "convs": evidence_conversations(row or {}),
    }


def _pending_peers(conn, rules=None, now=None):
    """The offers a new candidate must not near-duplicate.

    Undecided offers (proposed/offered/snoozed) are here because two
    paraphrases of one idea sitting side by side in a ten-slot inbox is the
    failure this set exists to prevent. Offers rejected inside the block window
    are here because the owner already said no to that idea, and
    blocked_offer_keys() only catches an exact token hit -- a rephrasing of a
    rejected offer would otherwise walk straight back in.

    Accepted offers are NOT here: they became interests, so `existing` already
    covers them, through the same rules. Expired offers are NOT here either --
    expiry means "no decision in 45 days", which is not a refusal, and treating
    it as one would wall a theme off forever on the strength of the owner
    having been busy.
    """
    rules = rules or DEFAULT_RULES
    since = _stamp(_now(now) - timedelta(days=rules.reject_block_days))
    rows = conn.execute(
        """
        SELECT key, title, positive_signals, status, source_conversations, evidence
          FROM interest_offers
         WHERE status IN (?, ?, ?)
            OR (status = ? AND COALESCE(decided_at, created_at) >= ?)
        """,
        (PROPOSED, OFFERED, SNOOZED, REJECTED, since),
    ).fetchall()
    return [
        _peer(r["key"], r["title"], json.loads(r["positive_signals"] or "[]"), r["status"],
              row={"source_conversations": r["source_conversations"], "evidence": r["evidence"]})
        for r in rows
    ]


def top_up(conn, path, *, interests_path=None, rules=None, now=None, actor=IMPORTER,
           trigger="topup"):
    """Refill the inbox toward `rules.target_inbox_size` from candidates the
    artifact on disk already holds. Returns a summary; never raises.

    This is import_artifact()'s other half, and the half that makes the inbox
    self-sustaining. import_artifact() is idempotent on the artifact's sha256 --
    which is right, because proposing from a NEW artifact and attaching its
    evidence must happen exactly once -- but that same idempotency is why the
    inbox could only ever shrink: once an artifact had been imported, nothing
    looked at it again, and every candidate that ranked below the run's cap sat
    unused on disk. Measured on production 2026-08-18: 30 candidates in the
    artifact, 24 of them dropped for no reason but "outranked this run".

    So top_up deliberately does NOT consult the sha. It re-reads the artifact
    every run and promotes the best still-qualified candidates until the inbox
    holds `target_inbox_size` live offers. That makes it cheap enough to run on
    every click: one file read, one JSON parse and pure arithmetic -- no
    network, no model, no browser, and no LLM call anywhere in the path.

    Every gate still applies, unchanged: the blocklist, the 180-day reject
    window, both dedup layers against existing interests, near-duplicate
    suppression against the offers already in the inbox, the durability gate
    and the score floor. **A short inbox is the correct outcome when nothing
    else qualifies.** Padding it with a candidate that fails the floors, or
    with a paraphrase of an offer already sitting there, would be a worse bug
    than an inbox of six. That case is reported as `exhausted: True`, which is
    the honest signal that what the inbox needs is new extraction, not more
    ranking -- and it is reported rather than acted on, because the extractor
    is a 25-minute browser job and nothing that runs on a click may start one.

    `reason` is set on EVERY path, including every path that adds nothing.
    Two multi-day outages in this system were jobs that did nothing and
    reported success; a top-up that ran and added no offers has to say which
    of "already full", "nothing qualifies" and "I could not read the artifact"
    it was.
    """
    rules = rules or DEFAULT_RULES
    now = _now(now)
    # Heal, then fill. A near-duplicate pair in the inbox is a wasted slot --
    # the owner is being asked the same question twice -- so the pairs are
    # merged first and this same run refills the slots that frees. Doing it
    # here rather than in a repair script of its own is what makes the inbox
    # self-healing: any duplicate that ever gets in is gone within the hour.
    reconciled = reconcile_duplicates(conn, rules=rules, now=now, actor=actor)
    live_before = live_offer_count(conn)
    summary = {
        "reconciled": reconciled,
        "imported": 0, "offered": 0, "skipped_existing": 0, "skipped_floor": 0,
        "skipped_dedup": 0, "skipped_blocked": 0, "attached": 0, "not_selected": 0,
        "offers": [], "promoted": [], "reasons": [], "artifact_sha256": "", "error": "",
        "live_before": live_before, "live_after": live_before,
        "target": rules.target_inbox_size, "deficit": max(rules.target_inbox_size - live_before, 0),
        "qualified": 0, "exhausted": False, "trigger": trigger, "reason": "",
    }

    def done(reason):
        if reconciled["merged"]:
            reason = (f"merged {reconciled['merged']} near-duplicate pair(s) "
                      f"[{reconciled['reason']}]; " + reason)
        summary["reason"] = reason
        summary["live_after"] = live_offer_count(conn)
        _record_topup(conn, summary)
        return summary

    if summary["deficit"] <= 0:
        return done(
            f"target already met: {live_before} live offer(s) >= target "
            f"{rules.target_inbox_size}; nothing promoted"
        )

    try:
        summary["artifact_sha256"] = artifact_sha256(path)
    except OSError as e:
        summary["error"] = f"artifact unreadable: {e}"
        return done(
            f"{summary['deficit']} short of the target and the artifact could not be "
            f"read ({e}) -- nothing promoted"
        )

    state = personal_state.load_optional(path)
    if state is None:
        summary["error"] = "artifact missing, malformed or unsupported version"
        return done(
            f"{summary['deficit']} short of the target and the artifact is missing, "
            f"malformed or an unsupported version -- nothing promoted"
        )
    if not state.candidates:
        summary["exhausted"] = True
        return done(
            f"{summary['deficit']} short of the target and the contract_version "
            f"{state.contract_version} artifact carries no candidates at all -- "
            f"this needs a new extraction, not more ranking"
        )

    blocked = blocked_offer_keys(conn, rules, now)
    if interests_path:
        try:
            blocked |= {t.lower() for t in load_blocked(interests_path)}
        except (OSError, ValueError):
            pass  # a missing/malformed interests.json must not stop a top-up
    existing = _existing_interests(conn)
    existing_offer_keys = {
        normalize_key(r["key"]) for r in conn.execute("SELECT key FROM interest_offers")
    }

    pool = _qualify(
        conn, state, summary["artifact_sha256"], blocked=blocked, existing=existing,
        existing_offer_keys=existing_offer_keys, rules=rules, now=now,
        summary=summary, attach=False,       # the attach write belongs to the import
    )
    summary["qualified"] = len(pool)

    _, budget = _budget(conn, rules)
    _promote(
        conn, pool, budget=budget, rules=rules, actor=actor, now=now,
        sha=summary["artifact_sha256"], summary=summary, trigger=trigger,
    )

    live_after = live_offer_count(conn)
    short = max(rules.target_inbox_size - live_after, 0)
    summary["exhausted"] = short > 0
    if summary["offered"] and not short:
        reason = (
            f"topped up {summary['offered']} offer(s) -- inbox {live_before} -> "
            f"{live_after} of {rules.target_inbox_size}: "
            + ", ".join(summary["offers"])
        )
    elif summary["offered"]:
        reason = (
            f"topped up {summary['offered']} offer(s) -- inbox {live_before} -> "
            f"{live_after}, still {short} short of {rules.target_inbox_size}: the "
            f"artifact is exhausted ({len(state.candidates)} candidates, none of the "
            f"rest qualify). New extraction is what this needs, not more ranking"
        )
    else:
        reason = (
            f"{short} short of the target and nothing was promoted: of "
            f"{len(state.candidates)} candidates in the artifact none qualified "
            f"(already offered {summary['skipped_existing']}, blocked "
            f"{summary['skipped_blocked']}, duplicate/near-duplicate "
            f"{summary['skipped_dedup']}, below the floors {summary['skipped_floor']}). "
            f"A short inbox is correct here -- new extraction is what this needs"
        )
    return done(reason)


def _record_topup(conn, summary):
    """One heartbeat row per top-up, so "did the refill run, and what did it
    decide" is answerable from the DB rather than from log archaeology. Written
    on every path, including the ones that promote nothing."""
    try:
        db.state_set(conn, TOPUP_STATE_KEY, json.dumps({
            "at": _stamp(_now()),
            "trigger": summary.get("trigger"),
            "live_before": summary.get("live_before"),
            "live_after": summary.get("live_after"),
            "target": summary.get("target"),
            "offered": summary.get("offered"),
            "offers": summary.get("offers"),
            "exhausted": summary.get("exhausted"),
            "error": summary.get("error"),
            "reason": summary.get("reason"),
        }, ensure_ascii=False))
    except Exception:       # noqa: BLE001 -- a heartbeat must never fail a refill
        pass


def top_up_after_decision(conn, path, *, interests_path=None, rules=None, now=None):
    """The click-driven refill, wrapped so it can never cost the owner his
    decision.

    The decision is the important write and it is already committed by the time
    this runs; the refill is best-effort garnish on the same request. So every
    exception is swallowed here -- a locked DB, a half-written artifact, a
    disk that just filled -- and reported in the return value instead. The
    owner taps Accept, the accept stands, and the worst case is an inbox that
    stays one short until the hourly tick.

    Returns the top_up summary, or a dict carrying `error` when it could not
    run at all. Never raises.
    """
    try:
        return top_up(conn, path, interests_path=interests_path, rules=rules,
                      now=now, trigger="decision")
    except Exception as e:      # noqa: BLE001 -- see the docstring
        return {
            "ok": False, "offered": 0, "offers": [], "error": f"{type(e).__name__}: {e}",
            "reason": "the refill failed; the decision itself is unaffected",
        }


def import_artifact(conn, path, *, interests_path=None, rules=None, now=None, actor=IMPORTER):
    """Turn a contract-v2 artifact's `candidates[]` into offers, exactly once
    per artifact.

    Idempotency is on the artifact's sha256 (recorded in `service_state`), so
    a re-run, a double copy or a half-written file that is re-copied whole
    creates offers exactly once. Fail-soft throughout: a missing, malformed
    or v1-only artifact is a summary with a reason, never an exception --
    this runs inside a scheduled tick.

    Returns a summary dict: counts per outcome plus `offers` (the keys
    actually placed in the inbox) and `rejected` (why each dropped candidate
    dropped), which is what makes a dry post-mortem possible.
    """
    rules = rules or DEFAULT_RULES
    now = _now(now)
    summary = {
        "imported": 0, "offered": 0, "skipped_existing": 0, "skipped_floor": 0,
        "skipped_dedup": 0, "skipped_blocked": 0, "attached": 0, "not_selected": 0,
        "offers": [], "promoted": [], "reasons": [], "artifact_sha256": "", "error": "",
        "live_before": live_offer_count(conn), "live_after": live_offer_count(conn),
        "target": (rules or DEFAULT_RULES).target_inbox_size,
    }
    try:
        sha = artifact_sha256(path)
    except OSError as e:
        summary["error"] = f"artifact unreadable: {e}"
        return summary
    summary["artifact_sha256"] = sha
    if db.state_get(conn, _import_state_key(sha)):
        summary["error"] = "already imported"
        summary["skipped_existing"] = 1
        return summary

    state = personal_state.load_optional(path)
    if state is None:
        summary["error"] = "artifact missing, malformed or unsupported version"
        return summary
    if not state.candidates:
        # A v1 artifact (or a v2 map-only publish) carries no candidates. Mark
        # it imported anyway: it will never produce offers, and re-reading it
        # every tick is pure noise.
        db.state_set(conn, _import_state_key(sha), _stamp(now))
        summary["error"] = f"no candidates in contract_version {state.contract_version} artifact"
        return summary

    blocked = blocked_offer_keys(conn, rules, now)
    if interests_path:
        try:
            blocked |= {t.lower() for t in load_blocked(interests_path)}
        except (OSError, ValueError):
            pass  # a missing/malformed interests.json must not stop an import
    existing = _existing_interests(conn)
    existing_offer_keys = {
        normalize_key(r["key"]) for r in conn.execute("SELECT key FROM interest_offers")
    }

    selected_pool = _qualify(
        conn, state, sha, blocked=blocked, existing=existing,
        existing_offer_keys=existing_offer_keys, rules=rules, now=now,
        summary=summary, attach=True,
    )

    live_before, budget = _budget(conn, rules)
    summary["live_before"] = live_before
    summary["target"] = rules.target_inbox_size
    _promote(
        conn, selected_pool, budget=budget, rules=rules, actor=actor, now=now,
        sha=sha, summary=summary, trigger="import",
    )
    summary["live_after"] = live_offer_count(conn)

    db.state_set(conn, _import_state_key(sha), _stamp(now))
    return summary


def _qualify(conn, state, sha, *, blocked, existing, existing_offer_keys,
             rules, now, summary, attach):
    """The candidate funnel, shared by import_artifact() and top_up(): every
    candidate in the artifact is normalized, judged against the blocklist, the
    existing interests and the floors, and either dropped with a written
    reason or returned scored.

    `attach` is the one behavioural difference between the two callers. Dedup
    layer 2 has a side effect -- it writes the candidate's quotes onto the
    interest that already covers it -- and that belongs to the once-per-artifact
    import, not to a top-up that may run several times an hour. With
    attach=False the same verdict is recorded as a reason and nothing is
    written, so a click-driven refill can never spam interest_events.

    Dedup against PENDING offers is deliberately NOT done here: it depends on
    the set of offers that exists as each promotion happens, which is
    _promote's business, so that one run cannot promote two near-duplicates of
    each other.
    """
    pool = []
    existing_keys = {i["key"] for i in existing}
    for raw in state.candidates:
        candidate = _normalize_candidate(raw, sha, state.generated_at)
        # The producer scored similarity against the interest set as it was
        # when the artifact was generated. An interest the owner has since
        # removed must not go on suppressing candidates from beyond the
        # grave -- so only similarities to interests that still exist count
        # for novelty and dedup. The producer's full list is still stored on
        # the row, for provenance.
        candidate["similarity_to_existing"] = [
            entry for entry in candidate["similarity"]
            if not entry["key"] or entry["key"] in existing_keys
        ]
        key = candidate["key"]
        if get_offer(conn, key) is not None or normalize_key(key) in existing_offer_keys:
            summary["skipped_existing"] += 1
            summary["reasons"].append({"key": key, "why": "an offer with this key already exists"})
            continue
        verdict, detail = dedup_verdict(candidate, existing, blocked, rules)
        if verdict == "blocked":
            summary["skipped_blocked"] += 1
            summary["reasons"].append({"key": key, "why": detail})
            continue
        if verdict == "attach":
            if attach:
                _attach_evidence(conn, detail["interest_key"], candidate, sha)
                summary["attached"] += 1
                summary["reasons"].append({"key": key, "why": detail["why"]})
            else:
                summary["skipped_dedup"] += 1
                summary["reasons"].append({
                    "key": key,
                    "why": detail["why"].replace(
                        "attached as evidence, not offered",
                        "already covered by that interest, not offered",
                    ),
                })
            continue
        if verdict == "duplicate":
            summary["skipped_dedup"] += 1
            summary["reasons"].append({"key": key, "why": detail})
            continue
        score, terms = score_candidate(candidate, rules, now)
        ok, why = passes_floors(candidate, score, rules)
        if not ok:
            summary["skipped_floor"] += 1
            summary["reasons"].append({"key": key, "why": why})
            continue
        candidate["score"] = score
        candidate["score_terms"] = terms
        pool.append(candidate)
    return pool


def _promote(conn, pool, *, budget, rules, actor, now, sha, summary, trigger):
    """Turn the best DISTINCT candidates in `pool` into live offers, up to
    `budget` of them, and write down what happened to every one that did not
    make it.

    Two things make this a walk rather than a slice of rank():

    * Distinctness beats the number. Each promotion joins the peer set the
      next candidate is judged against, so one run can never place two
      near-duplicates of each other in the inbox -- and when the only
      candidates left near-duplicate something already there, the run stops
      SHORT of the target rather than padding it. Ten suggestions containing
      two duplicate pairs is a worse inbox than eight distinct ones.
    * The live count is re-read before every insert, so an hourly import and a
      click-driven top-up racing each other cannot together overshoot the
      target. They rank the same pool in the same deterministic order, so they
      choose the same keys, and `interest_offers.key` being UNIQUE makes each
      one land exactly once -- the loser of a race sees insert_offer() return
      None and moves on.
    """
    peers = list(_pending_peers(conn, rules, now))
    remaining = budget
    # rank() is asked for the whole pool in order, not for `budget` of it: the
    # walk below does the cutting, because a candidate skipped as a duplicate
    # must let the next one through rather than consuming a slot.
    for candidate in rank(pool, replace(rules, max_offers_per_run=max(len(pool), 1))):
        key = candidate["key"]
        if remaining <= 0:
            summary["not_selected"] += 1
            summary["reasons"].append(
                {"key": key, "why": "outranked this run; the artifact keeps it"}
            )
            continue
        verdict, detail = dedup_verdict(candidate, (), frozenset(), rules, pending=peers)
        if verdict == "duplicate":
            summary["skipped_dedup"] += 1
            summary["reasons"].append({"key": key, "why": detail})
            continue
        if _reserved_count(conn) >= rules.target_inbox_size:
            # A concurrent run filled the inbox while this one was ranking.
            summary["not_selected"] += 1
            summary["reasons"].append(
                {"key": key, "why": "the inbox reached the target before this run offered it"}
            )
            continue
        if insert_offer(conn, candidate, actor=actor, now=now) is None:
            summary["skipped_existing"] += 1
            summary["reasons"].append({"key": key, "why": "another run offered this key first"})
            continue
        summary["imported"] += 1
        try:
            offer(conn, key, actor=actor, detail={
                "score": candidate["score"], "artifact_sha256": sha, "trigger": trigger,
            })
        except Exception:       # noqa: BLE001 -- re-raised below
            # Never leave a row parked at 'proposed'. It is invisible to the
            # owner and yet counts against the target, so it would silently
            # shrink the inbox by one, for good.
            conn.execute(
                "DELETE FROM interest_offers WHERE key = ? AND status = ?", (key, PROPOSED)
            )
            conn.commit()
            raise
        summary["offered"] += 1
        summary["offers"].append(key)
        summary["promoted"].append({
            "key": key, "score": candidate["score"],
            "exploratory": bool(candidate.get("exploratory")),
        })
        peers.append(_peer(key, candidate.get("title"),
                           candidate.get("positive_signals") or [], OFFERED, row=candidate))
        remaining -= 1
    return summary


def dedup_verdict(candidate, existing, blocked, rules=None, pending=()):
    """Three dedup layers, cheapest first (the item pipeline's philosophy):

    1. key/slug normalization against the existing interests -- catches an
       exact re-proposal, including of a removed interest (which must come
       back as kind='revive' and clear the doubled evidence bar instead).
    2. signal-token overlap >= .5 with an active interest -- the candidate is
       not a new interest, it is evidence for one the owner already has, so
       its quotes are attached there.
    3. the producer's semantic similarity -- a paraphrase, in either
       language, of something already followed. This is the layer exact-hash
       dedup never had; the earlier redesign's dedup bug was exactly this
       gap at the item level.

    `pending` applies those same three layers to offers that are ALREADY in
    the inbox (see _pending_peers). Until it existed, dedup was semantic
    against interests but exact-key-only against offers, so two near-duplicate
    candidates could sit side by side in the inbox -- measured on production
    2026-08-18, two such pairs were live at once
    ('concentrated-portfolio-construction' vs
    'portfolio-construction-conviction-risk', .65 overlap;
    'handheld-pc-gaming-steamos' vs 'handheld-pc-gaming-linux', .50). That was
    survivable while the extractor ran by hand and was not survivable once it
    runs nightly into a ten-slot inbox: every night would add another pair and
    the owner would reject the same idea again and again.

    A pending peer produces 'duplicate', never 'attach': attaching means
    writing evidence onto an INTEREST row, and an offer is not an interest --
    it has no evidence sink and mutating a row the owner is mid-decision on
    would be the wrong kind of surprise. Suppressing is also the safe verdict
    with respect to the lifecycle: it only ever declines to create a NEW offer
    and never touches the peer, so a decided offer still cannot be re-decided.

    Returns (verdict, detail) with verdict in ok|blocked|attach|duplicate.
    """
    rules = rules or DEFAULT_RULES
    key = candidate["key"]
    normalized = normalize_key(key)
    tokens = signal_tokens(candidate.get("title"), candidate.get("positive_signals") or [])

    if key in blocked or normalized in blocked or any(t in blocked for t in tokens):
        return "blocked", "key or signal tokens are blocked (rejected offer or blocklist)"

    for peer in pending:
        if peer["normalized"] == normalized:
            return "duplicate", (
                f"normalizes onto offer {peer['key']!r} (status {peer['status']})"
            )
        if tokens and peer["tokens"]:
            overlap = len(tokens & peer["tokens"]) / len(tokens)
            if overlap >= rules.signal_overlap_attach:
                return "duplicate", (
                    f"{overlap:.0%} signal overlap with offer {peer['key']!r} "
                    f"(status {peer['status']}): near-duplicate of something already "
                    f"in the inbox, suppressed rather than offered alongside it"
                )
        share, shared = _evidence_overlap(
            evidence_conversations(candidate), peer.get("convs") or set()
        )
        if (share >= rules.evidence_overlap_duplicate
                and shared >= rules.evidence_overlap_min_shared):
            return "duplicate", (
                f"{share:.0%} of its evidence ({shared} conversation(s)) is the same "
                f"evidence as offer {peer['key']!r} (status {peer['status']}): the same "
                f"idea in different words, suppressed rather than offered alongside it"
            )
        peer_sim = _similarity_to(candidate, peer["key"])
        if peer_sim >= rules.semantic_dup_similarity:
            return "duplicate", (
                f"semantic similarity {peer_sim:.2f} to offer {peer['key']!r} "
                f"(>= {rules.semantic_dup_similarity:.2f})"
            )

    for interest in existing:
        if interest["normalized"] == normalized and candidate.get("kind") != "revive":
            return "duplicate", f"normalizes onto existing interest {interest['key']!r}"

    if candidate.get("kind") in ("new", "bridge", "revive"):
        for interest in existing:
            if not interest["active"] or not tokens or not interest["tokens"]:
                continue
            overlap = len(tokens & interest["tokens"]) / len(tokens)
            if overlap >= rules.signal_overlap_attach:
                return "attach", {
                    "interest_key": interest["key"],
                    "why": (
                        f"{overlap:.0%} signal overlap with active interest "
                        f"{interest['key']!r}: attached as evidence, not offered"
                    ),
                }

    similarity = max_similarity(candidate)
    if similarity >= rules.semantic_dup_similarity and candidate.get("kind") in ("new", "bridge"):
        return "duplicate", (
            f"semantic similarity {similarity:.2f} to an existing interest "
            f"(>= {rules.semantic_dup_similarity:.2f})"
        )
    return "ok", ""


def _existing_interests(conn):
    rows = conn.execute(
        "SELECT key, title, positive_signals, active FROM interests"
    ).fetchall()
    return [
        {
            "key": r["key"],
            "normalized": normalize_key(r["key"]),
            "tokens": signal_tokens(r["title"], json.loads(r["positive_signals"] or "[]")),
            "active": bool(r["active"]),
        }
        for r in rows
    ]


def _attach_evidence(conn, interest_key, candidate, sha):
    """Dedup layer 2's output: the candidate's quotes land on the interest
    that already covers it, as one append-only interest_events row -- so the
    evidence is not lost just because it did not deserve its own interest."""
    db.add_interest_event(
        conn, interest_key, "offers", "offer_evidence", None, None,
        {
            "candidate_key": candidate["key"],
            "evidence": candidate["evidence"],
            "source_conversations": candidate["source_conversations"],
            "durability": candidate["durability"],
            "artifact_sha256": sha,
        },
    )


def _normalize_candidate(raw, sha, generated_at):
    """One artifact candidate -> the shape insert_offer() stores. Every field
    but `key` is optional and defaulted here, so the producer can add fields
    without a contract bump."""
    evidence = _normalize_evidence(raw.get("evidence"))
    kind = raw.get("kind") if raw.get("kind") in KINDS else "new"
    similarity = [
        {"key": e.get("key", ""), "sim": _as_number(e.get("sim", e.get("similarity")), 0.0)}
        for e in raw.get("similarity_to_existing") or []
        if isinstance(e, dict)
    ]
    return {
        "key": raw["key"].strip(),
        "kind": kind,
        "title": (raw.get("title") or raw["key"]).strip(),
        "description": raw.get("description", ""),
        "positive_signals": list(raw.get("positive_signals") or []),
        "negative_signals": list(raw.get("negative_signals") or []),
        "suggested_min_score": _optional_number(raw.get("suggested_min_score")),
        "suggested_sources": list(raw.get("sources") or raw.get("suggested_sources") or
                                  ["web_search"]),
        "parent_key": raw.get("parent_key") or None,
        "related_keys": list(raw.get("related_keys") or []),
        "durability": dict(raw.get("durability") or {}),
        "similarity": similarity,
        "similarity_to_existing": similarity,
        "expected_yield": _as_number(raw.get("expected_yield"), 0.0),
        "exploratory": bool(raw.get("exploratory")),
        "evidence": evidence,
        "source_conversations": sorted(
            {e["conversation_id"] for e in evidence if e.get("conversation_id")}
        ),
        "artifact_sha256": sha,
        "generated_at": generated_at,
    }


def _normalize_evidence(raw):
    """Provenance is first-class: each quote keeps its date, its language, how
    deep the owner went, and -- when the producer ships it -- the id of the
    conversation it came from, so the inbox can say which conversations
    produced an offer, not just how many."""
    out = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        quote = str(entry.get("quote", "")).strip()
        if not quote:
            continue
        conversation_id = entry.get("conversation_id") or entry.get("conversation") or ""
        out.append({
            "date": str(entry.get("date", "")),
            "quote": quote,
            "lang": entry.get("lang") or _guess_lang(quote),
            "depth": _as_number(entry.get("depth"), 0.0),
            "conversation_id": str(conversation_id),
        })
    return out


def _guess_lang(text):
    return "he" if _HEBREW.search(text) else "en"


def _optional_number(value):
    if value is None or value == "":
        return None
    number = _as_number(value, None)
    if number is None:
        return None
    # interests.json's own 0-100 legacy scale guard, same rule as
    # interests._threshold: a stray 75 must not silently mean "never notify".
    return number / 100 if number > 1 else number


# --- the interest half of the lifecycle -------------------------------------------

def interest_lifecycle(conn, key):
    row = conn.execute(
        "SELECT lifecycle, active FROM interests WHERE key = ?", (key,)
    ).fetchone()
    return None if row is None else {"lifecycle": row["lifecycle"], "active": bool(row["active"])}


def set_lifecycle(conn, key, to_lifecycle, *, actor, action, detail=None, active=None):
    """Move one interest through the post-acceptance state machine and log it
    to interest_events. `active` (the pipeline's own switch) is derived from
    the target state unless the caller overrides it: only 'active' and
    'decaying' interests keep collecting -- a decaying one is still live, it
    is merely on notice."""
    row = conn.execute("SELECT lifecycle FROM interests WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise OfferError(f"no interest with key {key!r}")
    current = row["lifecycle"] or ACTIVE
    if to_lifecycle not in LIFECYCLE_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(
            f"interest {key!r}: {current} -> {to_lifecycle} is not a legal transition"
        )
    if active is None:
        active = 1 if to_lifecycle in (ACTIVE, DECAYING) else 0
    conn.execute(
        "UPDATE interests SET lifecycle = ?, active = ? WHERE key = ?",
        (to_lifecycle, 1 if active else 0, key),
    )
    conn.commit()
    db.add_interest_event(
        conn, key, actor, action, None, None,
        {"from_lifecycle": current, "to_lifecycle": to_lifecycle, **(detail or {})},
    )
    return {"key": key, "lifecycle": to_lifecycle, "active": bool(active)}


def undo_auto_pause(conn, key, *, actor=OWNER_UI, note="", now=None):
    """The one click that undoes an auto-pause. Resets the silence clock (the
    undo event itself is the new baseline -- see `silence_days`) and closes
    any retirement offer the sweep raised on the way down, so the interest
    does not come back already on notice."""
    now = _now(now)
    state = interest_lifecycle(conn, key)
    if state is None:
        raise OfferError(f"no interest with key {key!r}")
    if state["lifecycle"] not in (PAUSED, DECAYING):
        raise InvalidTransition(
            f"interest {key!r} is {state['lifecycle']}, not paused -- nothing to undo"
        )
    result = set_lifecycle(
        conn, key, ACTIVE, actor=actor, action="auto_pause_undo",
        detail={"note": note, "undone_at": _stamp(now)},
    )
    retire_key = RETIRE_PREFIX + key
    retire_offer = get_offer(conn, retire_key)
    if retire_offer and retire_offer["status"] in (OFFERED, PROPOSED, SNOOZED):
        reject(conn, retire_key, note="auto-pause undone by owner", actor=actor, now=now)
        result["retire_offer_closed"] = retire_key
    return result


def retire_interest(conn, key, *, actor=OWNER_UI, note="", now=None):
    """The owner's explicit 'stop watching this'."""
    return set_lifecycle(
        conn, key, RETIRED, actor=actor, action="retire", detail={"note": note}
    )


def silence_days(conn, key, now=None):
    """Days since this interest last did anything worth keeping it for.

    The clock is the funnel's own measure -- the last item that cleared the
    interest's bar -- reset by an owner undo (an undone pause starts the
    countdown again, it does not resume mid-flight). An interest that has
    never produced an above-bar item is measured from its first scored item
    instead, which is exactly the dead-weight case (36 items collected, 0
    above bar). Returns None when there is no baseline at all: an interest
    the pipeline has never worked on cannot be judged silent.
    """
    now = _now(now)
    row = conn.execute("SELECT id FROM interests WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    interest_id = row["id"]
    baselines = []

    above = conn.execute(
        "SELECT MAX(s.created_at) ts FROM scores s JOIN interests i ON i.id = s.interest_id"
        " WHERE s.interest_id = ? AND s.final_score >= i.min_score",
        (interest_id,),
    ).fetchone()["ts"]
    if above:
        baselines.append(_parse(above))
    else:
        first_scored = conn.execute(
            "SELECT MIN(created_at) ts FROM scores WHERE interest_id = ?", (interest_id,)
        ).fetchone()["ts"]
        if first_scored:
            baselines.append(_parse(first_scored))

    reset = conn.execute(
        "SELECT MAX(at) ts FROM interest_events WHERE interest_key = ?"
        " AND action IN ('auto_pause_undo', 'offer_accepted')",
        (key,),
    ).fetchone()["ts"]
    if reset:
        baselines.append(_parse(reset))

    baselines = [b for b in baselines if b]
    if not baselines:
        return None
    return _days_between(now, max(baselines))


def _attributed_items(conn, interest_id, key):
    scored = conn.execute(
        "SELECT COUNT(*) c FROM scores WHERE interest_id = ?", (interest_id,)
    ).fetchone()["c"]
    collected = conn.execute(
        "SELECT COUNT(*) c FROM candidate_items WHERE origin_interest = ?", (key,)
    ).fetchone()["c"]
    return max(scored, collected)


def _negative_feedback(conn, interest_id):
    row = conn.execute(
        "SELECT SUM(verdict IN ('down', 'trash')) neg, SUM(verdict IN ('up', 'fire')) pos"
        " FROM feedback WHERE interest_id = ?",
        (interest_id,),
    ).fetchone()
    return (row["neg"] or 0), (row["pos"] or 0)


def _pipeline_scored_recently(conn, rules, now):
    """Did the pipeline itself do anything lately? It can be paused for days
    (it is, as this lands), and 'nobody scored anything' must never be read as
    'every interest went quiet'."""
    since = _stamp(now - timedelta(days=rules.pipeline_activity_days))
    return conn.execute(
        "SELECT COUNT(*) c FROM scores WHERE created_at >= ?", (since,)
    ).fetchone()["c"] > 0


# --- the sweeps ------------------------------------------------------------------

def sweep(conn, *, rules=None, now=None, announce=None):
    """Every timer-driven transition in one pass: expire stale offers, wake
    snoozed ones, flag decaying interests (raising a retirement offer), and
    auto-pause the ones that have been silent long enough -- announcing each
    pause rather than performing it quietly.

    `announce`, if given, is called once per announcement with the message
    text; announcements are returned either way, and every one of them is
    also on the append-only event log. Returns a summary dict.
    """
    rules = rules or DEFAULT_RULES
    now = _now(now)
    summary = {
        "expired": 0, "woken": 0, "decaying": 0, "recovered": 0, "auto_paused": 0,
        "retired": 0, "retire_offers": 0, "announcements": [], "skipped": "",
    }

    for row in conn.execute(
        "SELECT key, created_at FROM interest_offers WHERE status = ?", (OFFERED,)
    ).fetchall():
        created = _parse(row["created_at"])
        age = _days_between(now, created)
        if age is not None and age >= rules.expire_days:
            expire(conn, row["key"], now=now)
            summary["expired"] += 1

    for row in conn.execute(
        "SELECT key, snoozed_until FROM interest_offers WHERE status = ?", (SNOOZED,)
    ).fetchall():
        until = _parse(row["snoozed_until"])
        if until is None or until <= now:
            wake(conn, row["key"])
            summary["woken"] += 1

    if not _pipeline_scored_recently(conn, rules, now):
        # Refuse to judge silence the pipeline caused. Offer timers above are
        # about the owner's inbox and stay honest either way.
        summary["skipped"] = (
            f"no items scored in the last {rules.pipeline_activity_days} days -- "
            f"interest decay not evaluated"
        )
        return summary

    for row in conn.execute(
        "SELECT id, key, lifecycle FROM interests"
        " WHERE layer = 'owner' AND COALESCE(lifecycle, 'active') != 'retired'"
        " ORDER BY id"
    ).fetchall():
        key, lifecycle = row["key"], row["lifecycle"] or ACTIVE
        if _attributed_items(conn, row["id"], key) < rules.min_attributed_items:
            continue
        silent = silence_days(conn, key, now)
        if silent is None:
            continue

        if silent >= rules.auto_pause_days:
            if lifecycle in (ACTIVE, DECAYING):
                _auto_pause(conn, key, silent, rules, now, summary, announce)
            elif lifecycle == PAUSED and _raise_retire_offer(conn, key, silent, rules, now):
                # Already paused, and the retirement offer it was declined
                # with has now sat out its cool-off: ask once more.
                summary["retire_offers"] += 1
            continue
        if silent >= rules.decay_idle_days and lifecycle == ACTIVE:
            set_lifecycle(
                conn, key, DECAYING, actor=TIMER, action="decay",
                detail={"silent_days": round(silent, 1), "threshold_days": rules.decay_idle_days},
            )
            summary["decaying"] += 1
            if _raise_retire_offer(conn, key, silent, rules, now):
                summary["retire_offers"] += 1
            _announce(
                summary, announce, "decay", key,
                f"'{key}' has produced nothing above its bar for {silent:.0f} days -- "
                f"a retirement offer is waiting in the inbox.",
            )
            continue
        if silent < rules.decay_idle_days and lifecycle == DECAYING:
            set_lifecycle(
                conn, key, ACTIVE, actor=TIMER, action="recover",
                detail={"silent_days": round(silent, 1)},
            )
            summary["recovered"] += 1
            retire_key = RETIRE_PREFIX + key
            open_offer = get_offer(conn, retire_key)
            if open_offer and open_offer["status"] in (OFFERED, PROPOSED, SNOOZED):
                reject(conn, retire_key, note="items returned", actor=TIMER, now=now)
            continue
        if lifecycle == DECAYING:
            negatives, positives = _negative_feedback(conn, row["id"])
            if negatives >= rules.retire_negative_feedback and negatives > positives:
                set_lifecycle(
                    conn, key, RETIRED, actor=TIMER, action="retire",
                    detail={"negative_feedback": negatives, "positive_feedback": positives},
                )
                summary["retired"] += 1
                _announce(
                    summary, announce, "retire", key,
                    f"'{key}' retired: {negatives} negative reactions and nothing above its bar "
                    f"for {silent:.0f} days. Undo: python -m app offers --undo {key}",
                )

    return summary


def _auto_pause(conn, key, silent, rules, now, summary, announce):
    """The owner's decision, implemented: a dead interest pauses itself at 45
    days, reversibly, and says so."""
    set_lifecycle(
        conn, key, PAUSED, actor=TIMER, action="auto_pause",
        detail={
            "silent_days": round(silent, 1),
            "threshold_days": rules.auto_pause_days,
            "undo": f"python -m app offers --undo {key}",
        },
    )
    summary["auto_paused"] += 1
    if _raise_retire_offer(conn, key, silent, rules, now):
        summary["retire_offers"] += 1
    _announce(
        summary, announce, "auto_pause", key,
        f"Paused '{key}': {silent:.0f} days with zero items above its bar. "
        f"It stops collecting until you say otherwise. "
        f"Undo with one click, or: python -m app offers --undo {key}",
    )


def _raise_retire_offer(conn, interest_key, silent, rules, now):
    """One retirement offer per interest, re-raisable after a cool-off so a
    'no' is respected for a season without being forgotten forever."""
    key = RETIRE_PREFIX + interest_key
    existing = get_offer(conn, key)
    funnel = _funnel_snapshot(conn, interest_key)
    detail = {
        "interest_key": interest_key,
        "silent_days": round(silent, 1),
        **funnel,
    }
    if existing is None:
        insert_offer(conn, {
            "key": key,
            "kind": "retire",
            "title": f"Retire '{interest_key}'?",
            "description": (
                f"{interest_key} has gone {silent:.0f} days without a single item above its "
                f"bar ({funnel['collected']} collected, {funnel['scored']} scored, "
                f"{funnel['above_bar']} above bar all-time)."
            ),
            "related_keys": [interest_key],
            "score": None,
            "score_terms": detail,
            "evidence": [],
            "durability": {},
        }, actor=TIMER, now=now)
        offer(conn, key, actor=TIMER, detail=detail)
        return True

    if existing["status"] in (REJECTED, EXPIRED):
        decided = _parse(existing["decided_at"] or existing["created_at"])
        age = _days_between(now, decided)
        if age is not None and age >= rules.retire_reoffer_days:
            _transition(
                conn, key, OFFERED, actor=TIMER, action="reoffer", detail=detail,
                sets={"decided_at": None, "decided_note": ""},
            )
            return True
    return False


def _funnel_snapshot(conn, interest_key):
    row = conn.execute("SELECT id FROM interests WHERE key = ?", (interest_key,)).fetchone()
    if row is None:
        return {"collected": 0, "scored": 0, "above_bar": 0}
    interest_id = row["id"]
    collected = conn.execute(
        "SELECT COUNT(*) c FROM candidate_items WHERE origin_interest = ?", (interest_key,)
    ).fetchone()["c"]
    scored = conn.execute(
        "SELECT COUNT(*) c FROM scores WHERE interest_id = ?", (interest_id,)
    ).fetchone()["c"]
    above = conn.execute(
        "SELECT COUNT(*) c FROM scores s JOIN interests i ON i.id = s.interest_id"
        " WHERE s.interest_id = ? AND s.final_score >= i.min_score",
        (interest_id,),
    ).fetchone()["c"]
    return {"collected": collected, "scored": scored, "above_bar": above}


def _announce(summary, announce, kind, key, text):
    announcement = {"kind": kind, "interest_key": key, "text": text}
    summary["announcements"].append(announcement)
    if announce is not None:
        try:
            announce(text)
        except Exception as e:  # noqa: BLE001 -- a failed announcement never fails a sweep
            announcement["announce_error"] = str(e)


def activate(conn, key, *, now=None):
    """Called once the accepted offer's interest row actually exists (sync v2
    wrote it): the offer's own status stays 'accepted' -- it is decided -- and
    the interest starts its own lifecycle at 'active'."""
    now = _now(now)
    offer_row = get_offer(conn, key)
    if offer_row is None:
        raise UnknownOffer(key)
    if offer_row["status"] != ACCEPTED:
        raise InvalidTransition(f"offer {key!r} is {offer_row['status']}, not accepted")
    if interest_lifecycle(conn, key) is None:
        raise OfferError(
            f"offer {key!r} is accepted but no interest row exists yet -- "
            f"run the interests sync first"
        )
    conn.execute(
        "UPDATE interests SET lifecycle = ?, parent_key = COALESCE(?, parent_key) WHERE key = ?",
        (ACTIVE, offer_row["parent_key"], key),
    )
    conn.commit()
    db.add_interest_event(
        conn, key, OWNER_UI, "offer_accepted", None, None,
        {
            "offer_key": key,
            "kind": offer_row["kind"],
            "score": offer_row["score"],
            "artifact_sha256": offer_row["artifact_sha256"],
            "source_conversations": offer_row["source_conversations"],
            "activated_at": _stamp(now),
        },
    )
    add_offer_event(conn, key, OWNER_UI, "activate", ACCEPTED, ACCEPTED,
                    {"lifecycle": ACTIVE})
    return {"key": key, "lifecycle": ACTIVE}
