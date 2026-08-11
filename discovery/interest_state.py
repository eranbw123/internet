"""Layered interest state: OWNER / INFERRED / EMERGING / EXPLORATORY / RETIRED.

Owner interests (interests.json) are structurally immutable -- see db.py's
OwnerInterestImmutable guards and the two DB triggers in db.TRIGGERS_SQL.
Everything derived is written only through db.upsert_derived_interest() /
db.set_interest_layer(), one append-only discovery/interest_events.py row per
transition.

Fully offline: gather_evidence() reads candidate_items/feedback/interests
already in the DB, decide() is a pure function, and apply_transitions() is a
no-op (a zeroed summary) unless cfg.dynamic_interests is on. No new LLM or
network call, ever -- see PROJECT_STATE.md.
"""
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import db, interests, matching, notify, personal_state, stats
from .models import Interest

OWNER = "owner"
INFERRED = "inferred"
EMERGING = "emerging"
EXPLORATORY = "exploratory"
RETIRED = "retired"
LAYERS = (OWNER, INFERRED, EMERGING, EXPLORATORY, RETIRED)

_SUMMARY_KEYS = ("seeded", "entered", "promoted", "decayed", "retired", "reentered", "capped")
_ACTION_TO_SUMMARY_KEY = {
    "seed": "seeded", "enter": "entered", "promote": "promoted",
    "decay": "decayed", "reentry": "reentered",
}


@dataclass(frozen=True)
class Rules:
    promote_observations: int = 5
    promote_distinct_days: int = 3
    promote_positive_feedback: int = 2
    decay_idle_days: int = 30
    retire_negative_feedback: int = 3
    reentry_multiplier: int = 2
    evidence_window_days: int = 90
    max_candidates: int = 50


@dataclass
class Evidence:
    observations: int = 0
    distinct_days: int = 0
    first_seen: str = None
    last_seen: str = None
    positive_feedback: int = 0
    negative_feedback: int = 0
    sources: list = field(default_factory=list)


@dataclass
class Transition:
    from_layer: str          # None for absent -> exploratory
    to_layer: str
    action: str               # seed | enter | promote | decay | retire_* | reentry


# --- evidence gathering -------------------------------------------------------

def gather_evidence(conn, rules, now):
    """{term: Evidence} for terms worth considering as brand-new or as a
    retired-term re-entry candidate this pass -- title tokens (never body
    text) of candidate_items from the trailing `evidence_window_days`,
    excluding anything already covered by an owner interest or already an
    existing derived interest at a non-retired layer. Deterministic:
    truncated to `rules.max_candidates` by observations desc, term asc."""
    window_stats = _window_stats(conn, rules, now)
    feedback_index = _feedback_index(conn)
    excluded = _owner_terms(conn) | _derived_terms(conn, include_retired=False)
    return _select_candidates(window_stats, feedback_index, excluded, rules.max_candidates)


def _select_candidates(window_stats, feedback_index, excluded, max_candidates):
    """{term: Evidence}, excluded terms dropped, truncated to `max_candidates`
    by observations desc / term asc. The one place this selection happens --
    gather_evidence() and apply_transitions() both call it against the same
    precomputed window_stats/feedback_index, so they can never drift apart."""
    candidates = {
        term: _evidence_for(term, window_stats, feedback_index)
        for term in window_stats
        if term not in excluded
    }
    ordered = sorted(candidates.items(), key=lambda kv: (-kv[1].observations, kv[0]))
    return dict(ordered[:max_candidates])


def _window_stats(conn, rules, now):
    """Raw per-term observation hits from candidate_items titles within the
    evidence window -- unfiltered vocabulary, reused both by gather_evidence()
    and by apply_transitions()'s re-evaluation of already-tracked terms.

    Kept as a per-item hit list (not a pre-aggregated count) so a caller can
    exclude specific item ids afterwards -- see _self_matched_item_ids() and
    its use in apply_transitions()'s step 3, which excludes items whose only
    matched interest is the very derived interest being re-evaluated, so a
    term can never manufacture its own promotion evidence purely by having
    matched an item (self-referential evidence)."""
    since = (now - timedelta(days=rules.evidence_window_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, title, source, first_seen_at FROM candidate_items WHERE first_seen_at >= ?",
        (since,),
    ).fetchall()
    acc = {}
    for row in rows:
        seen = row["first_seen_at"] or ""
        day = seen[:10]
        for term in matching._tokens(row["title"] or ""):
            entry = acc.setdefault(term, {"hits": []})
            entry["hits"].append(
                {"item_id": row["id"], "day": day, "seen": seen, "source": row["source"]}
            )
    return acc


def _feedback_index(conn):
    """(title_lower, is_negative) for every feedback row, fetched once --
    "positive_feedback/negative_feedback count feedback rows on items whose
    title contains the term", keyed by stats.NEGATIVE_VERDICTS. A verdict
    outside notify.FEEDBACK_VERDICTS is skipped rather than defaulted to
    positive -- polarity must be positively known, never assumed."""
    rows = conn.execute(
        "SELECT f.verdict, i.title FROM feedback f JOIN candidate_items i ON i.id = f.item_id"
    ).fetchall()
    return [
        ((row["title"] or "").lower(), row["verdict"] in stats.NEGATIVE_VERDICTS)
        for row in rows
        if row["verdict"] in notify.FEEDBACK_VERDICTS
    ]


def _evidence_for(term, window_stats, feedback_index, exclude_item_ids=frozenset()):
    """`exclude_item_ids` drops specific hits before aggregating -- see
    _window_stats()'s docstring; empty (the default) for every caller except
    apply_transitions()'s step 3."""
    pos = neg = 0
    for title, is_negative in feedback_index:
        if term not in title:
            continue
        if is_negative:
            neg += 1
        else:
            pos += 1
    entry = window_stats.get(term)
    hits = [h for h in entry["hits"] if h["item_id"] not in exclude_item_ids] if entry else []
    if not hits:
        return Evidence(positive_feedback=pos, negative_feedback=neg)
    days = {h["day"] for h in hits if h["day"]}
    seens = sorted(h["seen"] for h in hits if h["seen"])
    sources = {h["source"] for h in hits if h["source"]}
    return Evidence(
        observations=len(hits),
        distinct_days=len(days),
        first_seen=seens[0] if seens else None,
        last_seen=seens[-1] if seens else None,
        positive_feedback=pos,
        negative_feedback=neg,
        sources=sorted(sources),
    )


def _owner_terms(conn):
    """Tokens of every owner interest's title + positive_signals +
    negative_signals -- terms an owner interest already covers."""
    tokens = set()
    for row in conn.execute(
        "SELECT title, positive_signals, negative_signals FROM interests WHERE layer = 'owner'"
    ):
        text = " ".join([
            row["title"],
            " ".join(json.loads(row["positive_signals"])),
            " ".join(json.loads(row["negative_signals"])),
        ])
        tokens |= matching._tokens(text)
    return tokens


def _derived_terms(conn, include_retired):
    terms = set()
    for row in conn.execute("SELECT key, layer FROM interests WHERE layer != 'owner'"):
        if not include_retired and row["layer"] == RETIRED:
            continue
        terms.add(_term_from_key(row["key"]))
    return terms


def _term_from_key(key):
    return key[len(db.DERIVED_KEY_PREFIX):] if key.startswith(db.DERIVED_KEY_PREFIX) else key


def _current_layer(conn, term):
    row = conn.execute(
        "SELECT layer FROM interests WHERE key = ?", (db.DERIVED_KEY_PREFIX + term,)
    ).fetchone()
    return row["layer"] if row else None


def _owner_min_score_floor(conn):
    row = conn.execute("SELECT MIN(min_score) AS floor FROM interests WHERE layer = 'owner'").fetchone()
    return row["floor"] if row and row["floor"] is not None else 0.0


def _self_matched_item_ids(conn, interest_id):
    """Item ids whose ONLY row in item_interests is a match to this interest
    -- i.e. matching attributes the item to nothing else. Excluded from a
    derived interest's own progression evidence (apply_transitions() step 3)
    so a term can never manufacture its own promotion evidence purely by
    having matched an item itself; real progression needs evidence
    independent of the interest's own matching. In production this can only
    ever be non-empty for an already-'inferred' (active) interest -- a
    lower-layer interest is never in the `active_interests()` list passed to
    matching.match_interests(), so it can't yet have a row here at all."""
    rows = conn.execute(
        """
        SELECT ii.item_id FROM item_interests ii
        WHERE ii.interest_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM item_interests ii2
              WHERE ii2.item_id = ii.item_id AND ii2.interest_id != ?
          )
        """,
        (interest_id, interest_id),
    ).fetchall()
    return {r["item_id"] for r in rows}


# --- the ladder ----------------------------------------------------------------

def decide(current_layer, evidence, rules, now, blocked):
    """Pure: no DB, no clock of its own. `current_layer` is None for a term
    not yet tracked ("absent"). Returns a Transition or None (no change).
    Never returns to_layer == OWNER, from any state, ever."""
    transition = _decide(current_layer, evidence, rules, now, blocked)
    assert transition is None or transition.to_layer != OWNER
    return transition


def _decide(current_layer, evidence, rules, now, blocked):
    if blocked:
        # A blocked term never enters; if already tracked (any non-retired
        # layer), it is retired on this pass.
        if current_layer not in (None, RETIRED):
            return Transition(current_layer, RETIRED, "retire_blocked")
        return None

    if current_layer is None:
        if evidence.observations > 0:
            return Transition(None, EXPLORATORY, "enter")
        return None

    if current_layer == RETIRED:
        # Anti-flapping: re-entry only at EXPLORATORY, and only with
        # observations well above the ordinary entry bar.
        if evidence.observations >= rules.promote_observations * rules.reentry_multiplier:
            return Transition(current_layer, EXPLORATORY, "reentry")
        return None

    # INFERRED / EMERGING / EXPLORATORY from here.
    if evidence.negative_feedback >= rules.retire_negative_feedback \
            and evidence.negative_feedback > evidence.positive_feedback:
        return Transition(current_layer, RETIRED, "retire_negative_feedback")

    if _is_stale(evidence, rules, now):
        demote = {INFERRED: EMERGING, EMERGING: EXPLORATORY, EXPLORATORY: RETIRED}
        return Transition(current_layer, demote[current_layer], "decay")

    if current_layer == EXPLORATORY:
        if evidence.observations >= rules.promote_observations \
                and evidence.distinct_days >= rules.promote_distinct_days:
            return Transition(current_layer, EMERGING, "promote")
        return None

    if current_layer == EMERGING:
        emerging_ok = (
            evidence.observations >= rules.promote_observations
            and evidence.distinct_days >= rules.promote_distinct_days
        )
        if emerging_ok and evidence.positive_feedback >= rules.promote_positive_feedback \
                and evidence.positive_feedback > evidence.negative_feedback:
            return Transition(current_layer, INFERRED, "promote")
        return None

    return None  # INFERRED, nothing left above it but decay (already checked)


def _is_stale(evidence, rules, now):
    if not evidence.last_seen:
        return True
    last = datetime.fromisoformat(evidence.last_seen)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).days >= rules.decay_idle_days


# --- applying it, guarded, to the DB -------------------------------------------

def apply_transitions(conn, cfg, rules=None, now=None):
    """Gathers evidence, decides, writes through the guarded db.py helpers
    only, appends one interest_events row per transition, and returns a
    summary dict. A no-op (zeroed summary) when cfg.dynamic_interests is
    off -- no derived row is ever created, no query beyond this function even
    runs, no LLM/network call either way."""
    if not cfg.dynamic_interests:
        return _summary(False)

    rules = rules or Rules()
    now = now or datetime.now(timezone.utc)
    summary = Counter()
    blocked_terms = {t.lower() for t in interests.load_blocked(cfg.interests_path)}

    window_stats = _window_stats(conn, rules, now)
    feedback_index = _feedback_index(conn)

    inferred_count = conn.execute(
        "SELECT COUNT(*) c FROM interests WHERE layer = ?", (INFERRED,)
    ).fetchone()["c"]
    # Snapshotted before any writes below, so a term entered (or re-entered)
    # by steps 1/2 of *this* pass is never also progressed by step 3 of the
    # same pass -- one ladder rung per term per apply_transitions() call.
    # last_observed_at is carried along as the staleness fallback for step 3
    # below -- a row with no corpus evidence *this pass* (e.g. a personal-
    # state seed, which contributes zero observations) must not be treated
    # as idle since forever; it is only idle once decay_idle_days has
    # actually elapsed since it was last written/observed.
    tracked_rows = conn.execute(
        "SELECT id, key, layer, last_observed_at FROM interests WHERE layer NOT IN ('owner', 'retired')"
    ).fetchall()

    # 1) Optional personal-state seeding: brand-new EXPLORATORY rows only,
    # zero observations -- can never by itself satisfy a promotion threshold
    # (see decide(): EXPLORATORY -> EMERGING needs promote_observations,
    # which a seeded row starts at 0 for). Every seed's origin -- the
    # artifact's own identity (hash of the bytes actually read, its
    # generated_at, its contract_version) plus the topic key and the instant
    # it was seeded -- is recorded on BOTH the interest's own provenance JSON
    # and its interest_events seed row, so `interests --why` can show the
    # full chain back to the artifact that suggested it.
    state = personal_state.load_optional(cfg.personal_state_path)
    if state is not None:
        artifact_sha256 = _artifact_sha256(cfg.personal_state_path)
        seeded_at = now.isoformat(timespec="seconds")
        seed_excluded = _owner_terms(conn) | _derived_terms(conn, include_retired=True)
        for raw in state.top_terms(rules.max_candidates):
            term = (raw or "").strip().lower()
            if not term or term in seed_excluded or term in blocked_terms:
                continue
            transition = Transition(None, EXPLORATORY, "seed")
            seed_origin = {
                "origin": "personal_state",
                "artifact_sha256": artifact_sha256,
                "generated_at": state.generated_at,
                "contract_version": state.contract_version,
                "topic_key": term,
                "seeded_at": seeded_at,
            }
            inferred_count = _write_transition(
                conn, cfg, term, transition, Evidence(), inferred_count, summary,
                provenance_source="personal_state", provenance_extra=seed_origin,
            )
            seed_excluded.add(term)

    # 2) New terms, and retired terms reconsidered for re-entry.
    excluded = _owner_terms(conn) | _derived_terms(conn, include_retired=False)
    candidates = _select_candidates(window_stats, feedback_index, excluded, rules.max_candidates)
    for term, evidence in candidates.items():
        current_layer = _current_layer(conn, term)  # None or 'retired' only, by construction
        transition = decide(current_layer, evidence, rules, now, term in blocked_terms)
        if transition is None:
            continue
        inferred_count = _write_transition(
            conn, cfg, term, transition, evidence, inferred_count, summary,
            provenance_source="corpus",
        )

    # 3) Progression/decay of the rows that were already tracked (non-owner,
    # non-retired) BEFORE this pass -- see the `tracked_rows` snapshot above.
    for row in tracked_rows:
        term = _term_from_key(row["key"])
        exclude_ids = _self_matched_item_ids(conn, row["id"])
        evidence = _evidence_for(term, window_stats, feedback_index, exclude_item_ids=exclude_ids)
        # No fresh observation this pass -> fall back to (or take the later
        # of) the row's own last_observed_at, so a term with real but
        # older-than-this-window evidence -- or none at all this pass --
        # isn't treated as idle since the beginning of time.
        baseline = row["last_observed_at"]
        if baseline and (not evidence.last_seen or baseline > evidence.last_seen):
            evidence.last_seen = baseline
        transition = decide(row["layer"], evidence, rules, now, term in blocked_terms)
        if transition is None:
            continue
        inferred_count = _write_transition(
            conn, cfg, term, transition, evidence, inferred_count, summary,
            provenance_source="corpus",
        )

    result = _summary(True)
    result.update(summary)
    return result


def _write_transition(
    conn, cfg, term, transition, evidence, inferred_count, summary, provenance_source,
    provenance_extra=None,
):
    """`provenance_extra` (only ever set by the personal-state seed step) is
    merged into both the interest's own provenance JSON and this
    transition's interest_events row -- so the seed's origin (artifact hash,
    generated_at, contract_version, topic_key, seeded_at) is recorded on
    both, per PROJECT_STATE.md."""
    key = db.DERIVED_KEY_PREFIX + term
    evidence_dict = {**asdict(evidence), **(provenance_extra or {})}
    to_layer, action = transition.to_layer, transition.action
    was_inferred = transition.from_layer == INFERRED

    if to_layer == INFERRED:
        if inferred_count >= cfg.derived_max_active:
            db.add_interest_event(
                conn, key, "automation", "promotion_capped", transition.from_layer, None, evidence_dict
            )
            summary["capped"] += 1
            return inferred_count
        floor = _owner_min_score_floor(conn)
        interest = Interest(
            key=key, title=term, description=f"Derived interest: {term}",
            positive_signals=[term], min_score=max(cfg.derived_min_score, floor),
            sources=[], layer=INFERRED,
        )
        provenance = {"source": provenance_source, "term": term, **(provenance_extra or {})}
        db.upsert_derived_interest(conn, interest, provenance)
        db.add_interest_event(conn, key, "automation", action, transition.from_layer, to_layer, evidence_dict)
        summary["promoted"] += 1
        return inferred_count + 1

    if action in ("enter", "seed", "reentry"):
        interest = Interest(
            key=key, title=term, description=f"Derived interest: {term}",
            positive_signals=[term], min_score=cfg.derived_min_score,
            sources=[], layer=to_layer,
        )
        provenance = {"source": provenance_source, "term": term, **(provenance_extra or {})}
        db.upsert_derived_interest(conn, interest, provenance)
    else:
        db.set_interest_layer(conn, key, to_layer, evidence_dict)

    db.add_interest_event(conn, key, "automation", action, transition.from_layer, to_layer, evidence_dict)
    summary[_summary_key(action)] += 1
    return inferred_count - 1 if was_inferred else inferred_count


def _artifact_sha256(path):
    """sha256 of the personal-state artifact's bytes, read fresh at seed
    time -- the artifact-identity half of a seeded interest's provenance
    chain (paired with generated_at/contract_version, both already carried
    by the loaded PersonalState)."""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _summary_key(action):
    if action.startswith("retire"):
        return "retired"
    return _ACTION_TO_SUMMARY_KEY.get(action, action)


def _summary(enabled):
    return {"enabled": enabled, **{k: 0 for k in _SUMMARY_KEYS}}
