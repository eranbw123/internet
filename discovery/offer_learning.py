"""Learning from offer decisions -- design §5.6, first bullet.

The generator proposes interests; the owner accepts, edits-then-accepts,
rejects or snoozes them in the offers inbox (`offers.py`). This module is the
only place that turns those judgements into a ranking effect on the *next*
batch of offers, and it is deliberately the only kind of learning it does:

  IN   accept / reject / edit / snooze on an interest OFFER.
  OUT  anything derived from DELIVERED ITEMS (clicks, digest engagement,
       up/down/fire on an article). §5.6's "delivery engagement" bullet is
       frozen pending the separate Output Layer brief, which proposes changing
       the unit of feedback entirely and rewrites the `feedback` table. This
       module never reads or writes `feedback`; its learning state lives in
       `offer_decision_log` (schema_offer_learning.sql). `Priors.domain_stats`
       is the recorded-but-unread socket that half plugs into.

Division of labour with `offers.py`, which owns the store
--------------------------------------------------------
`offers` owns "no means no": the lifecycle state machine, `blocked_offer_keys`
/`blocked_terms_for` (a rejection blocks its key and tokens for 180 days and is
appended to interests.json), `dedup_verdict` against the existing interests,
`score_candidate`/`passes_floors`/`rank`. None of that is reimplemented here --
it is imported.

This module owns the *preference* signal that only a history of decisions can
carry, and nothing else:

  * the accept prototype  -- a candidate resembling what the owner has
    accepted before gets a small ordering bonus (+.05 max, §5.6).
  * the suggestion prior  -- if the owner keeps lowering the suggested bar,
    future suggestions come down with it.
  * cold start            -- with no history, ranking is the evidence terms
    alone, and the exploratory slot is always filled.
  * snooze                -- "not now" is not "no": it teaches nothing about
    the theme, it only stops a paraphrase of the same theme sneaking past
    while the original sleeps.
  * a decision log        -- one decorated row per decision, so the terms the
    owner actually kept, the edit diff and the lifecycle context survive the
    offer row being updated in place.

Shape follows interest_state.py: one frozen `Rules` dataclass, pure functions
for the judgement, thin helpers around an append-only table. No LLM call, no
network, no clock of its own -- `now` is always injectable.

The seam
--------
`OfferDecisionSource` is the whole interface: `decisions()` and
`candidates()`. `StoreOfferDecisionSource` is the real one, reading
`offer_events` + `interest_offers`; `MemoryOfferDecisionSource` is the
in-memory fixture the tests use. PR J's write API can additionally call
`record_decision()` at decision time -- the only thing that buys is an exact
pre-edit bar (see `sync_from_offer_events`).
"""
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from math import sqrt
from pathlib import Path

from . import offers

SCHEMA_PATH = Path(__file__).resolve().parent / "schema_offer_learning.sql"

# Decisions are the terminal offer statuses. `expired` is a timer, not a
# judgement: it is logged for provenance but never read as a rejection --
# silence is not a decision, and the offer can legally be re-offered later.
ACCEPTED, REJECTED, SNOOZED, EXPIRED = (
    offers.ACCEPTED, offers.REJECTED, offers.SNOOZED, offers.EXPIRED
)
DECISIONS = (ACCEPTED, REJECTED, SNOOZED, EXPIRED)
OWNER_DECISIONS = (ACCEPTED, REJECTED, SNOOZED)

# Which edit fields carry the suggested bar, in `offers.accept(edits=...)`.
BAR_EDIT_FIELDS = ("min_score", "suggested_min_score")


@dataclass(frozen=True)
class Rules:
    """The learning knobs, and only those. Everything shared with the store --
    the reject window, the floors, the batch size, the serendipity slot --
    comes from `offers.Rules` so there is exactly one source of truth for it.
    """

    accept_bonus_max: float = 0.05      # §5.6: "a small similarity bonus (+.05 max)"
    bar_prior_min_edits: int = 2        # "repeatedly lowers" starts at two
    bar_prior_max_shift: float = 0.10   # a prior nudges the suggestion, it does not decide
    cold_start_runs: int = 2            # §5.6: "first two runs"
    min_overlap_tokens: int = 2         # one shared word is a coincidence, not a theme


DEFAULT_RULES = Rules()


class OfferLearningError(Exception):
    """Bad decision input -- an unknown decision or offer kind."""


# --- time (the store's helpers, re-exported so this module has one clock) ------

_parse = offers._parse
_stamp = offers._stamp


def _now(now=None):
    """`offers._now` expects a datetime; this one also accepts an ISO string,
    because every entry point here is reachable from a CLI and from a test."""
    if now is None or isinstance(now, datetime):
        return offers._now(now)
    return _parse(now) or offers._now()


def _days_since(now, then):
    then = _parse(then)
    return None if then is None else (now - then).total_seconds() / 86400.0


def _clamp(value, low=0.0, high=1.0):
    return low if value < low else (high if value > high else float(value))


# --- the decision record -------------------------------------------------------

@dataclass(frozen=True)
class OfferDecision:
    """One owner judgement on one offer, decorated with what learning needs and
    the offer row cannot keep: the signal tokens as they stood when the owner
    decided, the edit diff, and the interest's lifecycle stage at the time.

    Decisions are immutable, exactly like the `offer_events` rows they come
    from -- `offers` refuses to re-decide a decided offer. The same key can
    still appear more than once over time (a declined retirement offer is
    legally re-raised after its cool-off), so the log keys on
    (offer_key, decision, decided_at) rather than on the key alone.
    """

    offer_key: str
    decision: str
    decided_at: str
    offer_kind: str = "new"
    actor: str = offers.OWNER_UI
    interest_key: str = ""          # for kind='retire': whose retirement was proposed
    lifecycle: str = ""             # that interest's stage when the owner answered
    domain: str = ""                # the offer's parent_key -- the family, §5.3
    signal_terms: tuple = ()        # post-edit: what the owner KEPT
    proposed_min_score: float = None
    accepted_min_score: float = None
    edits: dict = field(default_factory=dict)
    snoozed_until: str = None
    offer_score: float = None
    score_terms: dict = field(default_factory=dict)
    artifact_sha256: str = ""
    note: str = ""

    def __post_init__(self):
        if self.decision not in DECISIONS:
            raise OfferLearningError(f"unknown decision {self.decision!r}")
        if self.offer_kind not in offers.KINDS:
            raise OfferLearningError(f"unknown offer kind {self.offer_kind!r}")
        object.__setattr__(self, "signal_terms", tuple(sorted(set(self.signal_terms or ()))))

    @property
    def is_owner_decision(self):
        return self.decision in OWNER_DECISIONS

    @property
    def polarity(self):
        """+1 toward the theme, -1 against it, 0 for "not now" / no answer.

        A `retire` offer proposes DROPPING an interest, so the owner's answer
        means the opposite of what it means everywhere else: declining one is
        the one-click undo of the auto-pause, i.e. "keep this". Note what that
        does NOT do -- see `learn()`: a declined retirement blocks nothing and
        teaches no terms, because an interest title's words are generic enough
        that propagating them in either direction poisons the candidate pool
        (`offers.blocked_terms_for` makes the same call for the same reason).
        """
        if self.decision == ACCEPTED:
            return -1 if self.offer_kind == "retire" else 1
        if self.decision == REJECTED:
            return 1 if self.offer_kind == "retire" else -1
        return 0

    @property
    def bar_delta(self):
        """How far the owner moved the suggested bar, or None if untouched."""
        if self.proposed_min_score is None or self.accepted_min_score is None:
            return None
        return round(self.accepted_min_score - self.proposed_min_score, 4)

    @classmethod
    def from_offer(cls, offer_row, decision, *, event=None, proposed_min_score=None,
                   lifecycle="", **overrides):
        """Build one from a decoded `interest_offers` row (`offers.get_offer`)
        plus, optionally, the `offer_events` row that decided it.

        The row is read AFTER the decision, which is what makes its
        title/positive_signals the post-edit truth. The one thing that read
        cannot recover is the bar the generator originally suggested, because
        `offers.accept()` overwrites that column with the owner's value: pass
        `proposed_min_score` (PR J has the pre-edit row in hand) or accept that
        this decision contributes nothing to the bar prior.
        """
        event = event or {}
        detail = event.get("detail") or {}
        edits = dict(detail.get("edits") or {})
        accepted_bar = next(
            (edits[f] for f in BAR_EDIT_FIELDS if edits.get(f) is not None), None)
        interest_key = ""
        if offer_row.get("kind") == "retire":
            related = offer_row.get("related_keys") or []
            interest_key = related[0] if related else offer_row["key"].removeprefix(
                offers.RETIRE_PREFIX)
        values = dict(
            offer_key=offer_row["key"],
            decision=decision,
            decided_at=(offer_row.get("decided_at") or event.get("at") or ""),
            offer_kind=offer_row.get("kind") or "new",
            actor=event.get("actor") or offers.OWNER_UI,
            interest_key=interest_key,
            lifecycle=lifecycle,
            # No `domain` column exists on the store; the hierarchy's parent is
            # the family this offer belongs to (§5.3), which is what a
            # per-domain prior would key on.
            domain=offer_row.get("parent_key") or "",
            signal_terms=offers.signal_tokens(
                offer_row.get("title"), offer_row.get("positive_signals") or []),
            proposed_min_score=_number(
                proposed_min_score if proposed_min_score is not None
                else (None if accepted_bar is not None
                      else offer_row.get("suggested_min_score"))),
            accepted_min_score=_number(
                accepted_bar if accepted_bar is not None
                else offer_row.get("suggested_min_score")),
            edits=edits,
            snoozed_until=offer_row.get("snoozed_until") or (detail.get("until") or None),
            offer_score=_number(offer_row.get("score")),
            score_terms=offer_row.get("score_terms") or {},
            artifact_sha256=offer_row.get("artifact_sha256") or "",
            note=offer_row.get("decided_note") or detail.get("note") or "",
        )
        values.update(overrides)
        return cls(**values)


def _loads(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- the learned state ---------------------------------------------------------

@dataclass(frozen=True)
class Priors:
    """Everything the decision log teaches, folded into one value. Pure output
    of `learn()`; nothing here is persisted, because recomputing a few hundred
    rows costs less than keeping a derived table honest."""

    prototype: dict = field(default_factory=dict)        # token -> weight, accepted offers
    blocked_keys: dict = field(default_factory=dict)     # normalized key -> blocked until
    blocked_terms: dict = field(default_factory=dict)    # token -> blocked until
    covered_keys: dict = field(default_factory=dict)     # normalized key -> accepted offer key
    snoozed: dict = field(default_factory=dict)          # normalized key -> {until, tokens}
    rescued: dict = field(default_factory=dict)          # interest key -> lifecycle when saved
    bar_shift: float = 0.0
    bar_shift_n: int = 0
    n_owner_decisions: int = 0
    n_runs: int = 0
    cold_start: bool = True
    # Recorded, never read by ranking: the socket the deferred delivery-
    # engagement half (§5.6, second bullet) plugs its expected_yield prior into.
    domain_stats: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "cold_start": self.cold_start,
            "n_owner_decisions": self.n_owner_decisions,
            "n_runs": self.n_runs,
            "prototype_terms": len(self.prototype),
            "blocked_keys": len(self.blocked_keys),
            "covered_keys": len(self.covered_keys),
            "snoozed": len(self.snoozed),
            "rescued": dict(self.rescued),
            "bar_shift": self.bar_shift,
            "bar_shift_n": self.bar_shift_n,
            "domain_stats": self.domain_stats,
        }


def learn(decisions, rules=None, offer_rules=None, now=None):
    """Fold decisions into `Priors`. Pure, and independent of input order."""
    rules = rules or DEFAULT_RULES
    offer_rules = offer_rules or offers.DEFAULT_RULES
    now = _now(now)
    state = {"blocked_keys": {}, "blocked_terms": {}, "covered_keys": {},
             "snoozed": {}, "rescued": {}, "domain_stats": {}}
    prototype = Counter()
    bar_deltas = []
    runs, days, n_owner = set(), set(), 0

    for decision in sorted(decisions, key=lambda d: (d.decided_at or "", d.offer_key)):
        decided = _parse(decision.decided_at)
        key = offers.normalize_key(decision.offer_key)
        if not decision.is_owner_decision:
            continue  # `expired` is a timer; silence teaches nothing

        n_owner += 1
        runs.add(decision.artifact_sha256 or "")
        if decided:
            days.add(decided.date().isoformat())
        stats = state["domain_stats"].setdefault(
            decision.domain or "unknown", {"accepted": 0, "rejected": 0, "snoozed": 0})
        stats[decision.decision] = stats.get(decision.decision, 0) + 1

        if decision.decision == SNOOZED:
            # "Not now" is not "no". It teaches nothing about the theme; it
            # only holds the door shut while the original sleeps.
            if decision.snoozed_until and (_parse(decision.snoozed_until) or now) > now:
                state["snoozed"][key] = {"until": decision.snoozed_until,
                                         "tokens": set(decision.signal_terms)}
            continue

        if decision.offer_kind == "retire":
            # Recorded for provenance and for cold-start progress; deliberately
            # inert otherwise. `offers` owns the re-offer cool-off
            # (Rules.retire_reoffer_days) and blocks nothing for this kind.
            if decision.polarity > 0 and decision.interest_key:
                state["rescued"][decision.interest_key] = decision.lifecycle or ""
            continue

        if decision.polarity < 0:
            # Same window and same facts as `offers.blocked_offer_keys`; this
            # copy exists so the module still works against the in-memory fake
            # and without an offers store. `evaluate(blocked=...)` takes the
            # store's live set when there is one, and the two are unioned.
            if decided is None or _days_since(now, decided) <= offer_rules.reject_block_days:
                until = (_stamp(decided + timedelta(days=offer_rules.reject_block_days))
                         if decided else "")
                state["blocked_keys"][key] = until
                for token in decision.signal_terms:
                    state["blocked_terms"][token] = max(
                        state["blocked_terms"].get(token, ""), until)
        else:
            prototype.update(decision.signal_terms)
            # An accepted offer is an interest now. Until sync writes it,
            # `dedup_verdict` cannot see it, so it counts as existing here.
            state["covered_keys"][key] = decision.offer_key
            delta = decision.bar_delta
            if delta:
                bar_deltas.append(delta)

    # The owner's latest word stands: an accept clears an earlier block on the
    # same key (decisions were folded oldest-first).
    for key in state["covered_keys"]:
        state["blocked_keys"].pop(key, None)

    shift = 0.0
    if len(bar_deltas) >= rules.bar_prior_min_edits:
        shift = _clamp(round(sum(bar_deltas) / len(bar_deltas), 4),
                       -rules.bar_prior_max_shift, rules.bar_prior_max_shift)

    runs.discard("")
    n_runs = len(runs) or len(days)
    return Priors(
        prototype=_normalize_prototype(prototype),
        bar_shift=shift,
        bar_shift_n=len(bar_deltas),
        n_owner_decisions=n_owner,
        n_runs=n_runs,
        cold_start=n_runs < rules.cold_start_runs,
        **state,
    )


def _normalize_prototype(counter):
    total = sum(counter.values())
    return {token: round(count / total, 6) for token, count in counter.items()} if total else {}


def _cosine(vector, tokens):
    """Cosine between the weighted prototype and a candidate's token set."""
    if not vector or not tokens:
        return 0.0
    dot = sum(weight for token, weight in vector.items() if token in tokens)
    norm = sqrt(sum(w * w for w in vector.values())) * sqrt(len(tokens))
    return _clamp(dot / norm) if norm else 0.0


def _overlap(tokens, other, rules):
    """Shared tokens over the candidate's own size -- the same direction
    `offers.dedup_verdict` uses, with a floor of two shared tokens so a single
    common word can never suppress a candidate."""
    tokens, other = set(tokens), set(other)
    shared = tokens & other
    if len(shared) < rules.min_overlap_tokens or not tokens:
        return 0.0
    return len(shared) / len(tokens)


# --- ranking -------------------------------------------------------------------

@dataclass(frozen=True)
class Evaluated:
    """One candidate, scored and judged against the priors."""

    candidate: dict
    base_score: float
    score: float
    terms: dict
    learning: dict
    ok: bool
    reason: str = ""
    exploratory: bool = False

    @property
    def key(self):
        return self.candidate.get("key", "")

    @property
    def suggested_min_score(self):
        return self.learning.get("suggested_min_score")


def evaluate(candidates, priors, *, rules=None, offer_rules=None, now=None,
             blocked=(), scorer=None):
    """Score every candidate and apply what the decisions taught.

    Scores are READ, not recomputed, when the candidate already carries them
    (an offer row from the store does); otherwise `offers.score_candidate`
    rates it. Which §5.2 terms decisions may move:

      evidence_strength / recurrence / recency   never -- corpus facts.
      novelty            an offer the owner accepted counts as an existing
                         interest until sync makes it one for real.
      expected_yield     untouched here: the delivery-derived prior is the
                         deferred half of this PR.
      serendipity        the exploratory pick keeps its reserved slot and opts
                         OUT of the accept bonus -- rewarding resemblance to
                         what the owner already likes is precisely what an
                         exploration lane must not do.

    The bonus moves ORDER only. Floors are checked against the base score in
    `select()`, so a learned preference can never lift a candidate over the
    evidence bar.
    """
    rules = rules or DEFAULT_RULES
    offer_rules = offer_rules or offers.DEFAULT_RULES
    now = _now(now)
    blocked = set(blocked or ())
    out = []
    for candidate in candidates:
        if candidate.get("score") is not None and candidate.get("score_terms"):
            score, terms = float(candidate["score"]), dict(candidate["score_terms"])
        else:
            score, terms = (scorer or offers.score_candidate)(candidate, offer_rules, now)
        key = offers.normalize_key(candidate.get("key"))
        tokens = offers.signal_tokens(
            candidate.get("title"), candidate.get("positive_signals") or [])
        exploratory = bool(candidate.get("exploratory"))
        learning = {"cold_start": priors.cold_start, "accept_bonus": 0.0,
                    "prototype_similarity": 0.0, "bar_shift": 0.0,
                    "suggested_min_score": _number(candidate.get("suggested_min_score"))}

        ok, reason = _admits(candidate, key, tokens, priors, rules, now, blocked)

        # Cold start (§5.6): ranking is purely the evidence terms. The hard
        # filters still apply -- something rejected in run 1 must not return
        # in run 2 just because the loop has not warmed up.
        if not priors.cold_start:
            if not exploratory:
                similarity = _cosine(priors.prototype, tokens)
                learning["prototype_similarity"] = round(similarity, 4)
                learning["accept_bonus"] = round(rules.accept_bonus_max * similarity, 4)
            if priors.bar_shift and learning["suggested_min_score"] is not None:
                learning["bar_shift"] = priors.bar_shift
                learning["suggested_min_score"] = round(
                    _clamp(learning["suggested_min_score"] + priors.bar_shift), 4)

        out.append(Evaluated(
            candidate=candidate, base_score=score,
            score=round(_clamp(score + learning["accept_bonus"]), 4),
            terms=dict(terms, learning=learning), learning=learning,
            ok=ok, reason=reason, exploratory=exploratory,
        ))
    return out


def _admits(candidate, key, tokens, priors, rules, now, blocked):
    """(ok, reason) for the hard filters a past decision imposes.

    Retirement offers are not filtered here at all: they never come from an
    artifact, and `offers` already owns their cool-off.
    """
    if candidate.get("kind") == "retire":
        return True, ""

    for token in (candidate.get("key"), key):
        if token and token in blocked:
            return False, "key is blocked by the offers store (rejected offer or blocklist)"
    if any(token in blocked for token in tokens):
        return False, "signal tokens are blocked by the offers store"

    if key in priors.blocked_keys:
        return False, (f"rejected before; blocked until "
                       f"{priors.blocked_keys[key] or 'further notice'}")
    if key in priors.covered_keys:
        return False, f"already accepted as {priors.covered_keys[key]}"

    blocked_overlap = _overlap(tokens, priors.blocked_terms, rules)
    if blocked_overlap >= offers.DEFAULT_RULES.signal_overlap_attach:
        return False, f"signal overlap {blocked_overlap:.2f} with rejected terms"

    asleep = priors.snoozed.get(key)
    if asleep is None:
        for entry in priors.snoozed.values():
            if _overlap(tokens, entry["tokens"], rules) >= offers.DEFAULT_RULES.signal_overlap_attach:
                asleep = entry
                break
    if asleep and (_parse(asleep["until"]) or now) > now:
        return False, f"snoozed until {asleep['until']}"
    return True, ""


def select(evaluated, *, rules=None, offer_rules=None):
    """Pick the run's offers: floors first, then `offers.rank` -- the store's
    own top-N-with-a-reserved-serendipity-slot, unchanged.

    The floor is the BASE score and the store's durability gate, never the
    learned score: the serendipity slot bypasses rank but not the floor, and
    neither does a learned preference.
    """
    rules = rules or DEFAULT_RULES
    offer_rules = offer_rules or offers.DEFAULT_RULES
    qualified, skipped = [], []
    for item in evaluated:
        if not item.ok:
            skipped.append(item)
            continue
        ok, reason = offers.passes_floors(item.candidate, item.base_score, offer_rules)
        if ok:
            qualified.append(item)
        else:
            skipped.append(replace(item, ok=False, reason=reason))

    by_key = {}
    rankable = []
    for item in qualified:
        by_key[id(item)] = item
        rankable.append({"key": item.key, "score": item.score,
                         "exploratory": item.exploratory, "_id": id(item)})
    chosen_ids = [row["_id"] for row in offers.rank(rankable, offer_rules)]
    chosen = [by_key[i] for i in chosen_ids]
    left_over = sorted((item for item in qualified if id(item) not in set(chosen_ids)),
                       key=lambda e: (-e.score, e.key))
    return chosen, skipped + left_over


def rank(candidates, priors, *, rules=None, offer_rules=None, now=None,
         blocked=(), scorer=None):
    """(chosen, skipped) -- the whole pass, for one call site."""
    evaluated = evaluate(candidates, priors, rules=rules, offer_rules=offer_rules,
                         now=now, blocked=blocked, scorer=scorer)
    return select(evaluated, rules=rules, offer_rules=offer_rules)


def explain(item):
    """One provenance line for the inbox, in the design's own format."""
    terms = item.terms
    parts = [f"{name.replace('_', ' ')} {terms[name]:.2f}" for name in
             ("evidence_strength", "recurrence", "recency", "novelty", "expected_yield")
             if isinstance(terms.get(name), (int, float))]
    if item.learning.get("accept_bonus"):
        parts.append(f"learned +{item.learning['accept_bonus']:.2f}")
    if item.learning.get("cold_start"):
        parts.append("cold start")
    if item.exploratory:
        parts.append("serendipity slot")
    return " · ".join(parts)


# --- storage -------------------------------------------------------------------

def ensure_schema(conn):
    """Additive DDL in its own file, applied here rather than from db.init():
    PRs H/I/J are adding their migrations to schema.sql concurrently and this
    half has no reason to share a file with them. Idempotent; every entry point
    calls it, so an older DB picks the table up on first use."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def record_decision(conn, decision, now=None):
    """Append one decision. Returns the row id, or None when that exact
    decision on that offer at that instant is already logged -- replaying an
    `offer_events` tail must never double-count it."""
    ensure_schema(conn)
    try:
        cursor = conn.execute(
            """INSERT INTO offer_decision_log
                   (at, decided_at, offer_key, offer_kind, decision, actor, interest_key,
                    lifecycle, domain, polarity, signal_terms, proposed_min_score,
                    accepted_min_score, edits, snoozed_until, offer_score, score_terms,
                    artifact_sha256, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_stamp(_now(now)), decision.decided_at, decision.offer_key, decision.offer_kind,
             decision.decision, decision.actor, decision.interest_key, decision.lifecycle,
             decision.domain, decision.polarity,
             json.dumps(sorted(decision.signal_terms), ensure_ascii=False),
             decision.proposed_min_score, decision.accepted_min_score,
             json.dumps(decision.edits, ensure_ascii=False), decision.snoozed_until,
             decision.offer_score, json.dumps(decision.score_terms, ensure_ascii=False),
             decision.artifact_sha256, decision.note),
        )
    except sqlite3.IntegrityError:
        return None
    conn.commit()
    return cursor.lastrowid


def sync_from_offer_events(conn, now=None):
    """Bring the decision log up to date from `offer_events` -- the store's
    append-only chain is the source of truth, this table is its decorated
    projection. Idempotent: returns the decisions actually appended.

    This is what makes PR J optional for the read path. The one thing a
    replay cannot recover is the bar the generator suggested before an edited
    accept overwrote it, so a write-through `record_decision()` at decision
    time (with the pre-edit row) is still worth doing for the bar prior.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT at, offer_key, actor, action, to_status, detail FROM offer_events"
        " WHERE to_status IN (?,?,?,?) ORDER BY id",
        DECISIONS,
    ).fetchall()
    appended = []
    for row in rows:
        offer_row = offers.get_offer(conn, row["offer_key"])
        if offer_row is None:
            continue
        event = {"at": row["at"], "actor": row["actor"], "action": row["action"],
                 "detail": _loads(row["detail"], {})}
        lifecycle = ""
        if offer_row.get("kind") == "retire":
            related = offer_row.get("related_keys") or []
            interest_key = related[0] if related else row["offer_key"].removeprefix(
                offers.RETIRE_PREFIX)
            state = offers.interest_lifecycle(conn, interest_key)
            lifecycle = (state or {}).get("lifecycle") or ""
        decision = OfferDecision.from_offer(
            offer_row, row["to_status"], event=event, lifecycle=lifecycle,
            # The event's own timestamp, not the row's: an offer can be
            # decided more than once over its life (a declined retirement is
            # re-raised after its cool-off) and each answer is its own row.
            decided_at=row["at"],
        )
        if record_decision(conn, decision, now=now) is not None:
            appended.append(decision)
    return appended


def decisions(conn, since=None, offer_key=None):
    """The log, oldest first."""
    ensure_schema(conn)
    sql = "SELECT * FROM offer_decision_log"
    where, args = [], []
    if since:
        where.append("decided_at >= ?")
        args.append(since)
    if offer_key:
        where.append("offer_key = ?")
        args.append(offer_key)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY decided_at, id"
    return [_row_to_decision(row) for row in conn.execute(sql, args)]


def _row_to_decision(row):
    row = dict(row)
    return OfferDecision(
        offer_key=row["offer_key"], decision=row["decision"], decided_at=row["decided_at"],
        offer_kind=row["offer_kind"], actor=row["actor"], interest_key=row["interest_key"],
        lifecycle=row["lifecycle"], domain=row["domain"],
        signal_terms=_loads(row["signal_terms"], []),
        proposed_min_score=row["proposed_min_score"],
        accepted_min_score=row["accepted_min_score"],
        edits=_loads(row["edits"], {}), snoozed_until=row["snoozed_until"],
        offer_score=row["offer_score"], score_terms=_loads(row["score_terms"], {}),
        artifact_sha256=row["artifact_sha256"], note=row["note"],
    )


def priors(conn, rules=None, offer_rules=None, now=None, sync=True):
    """The learned state as of `now`. Syncs from `offer_events` first unless
    the caller has already done it."""
    if sync:
        sync_from_offer_events(conn, now=now)
    return learn(decisions(conn), rules=rules, offer_rules=offer_rules, now=now)


# --- the seam ------------------------------------------------------------------

class OfferDecisionSource:
    """What this module needs in order to learn. Two methods, nothing else:

        decisions(since=None) -> [OfferDecision]   past owner judgements
        candidates()          -> [dict]            candidates to rank

    A candidate is the store's own decoded offer dict / the artifact's
    candidate shape: key, kind, title, description, positive_signals,
    negative_signals, suggested_min_score, parent_key, related_keys, evidence,
    durability, similarity, expected_yield, exploratory, and optionally
    score/score_terms (read in preference to recomputing them).
    """

    def decisions(self, since=None):
        raise NotImplementedError

    def candidates(self):
        raise NotImplementedError

    def blocked(self):
        return set()

    def rank(self, rules=None, offer_rules=None, now=None, scorer=None):
        """The whole pass, so no caller has to assemble it by hand."""
        learned = learn(self.decisions(), rules=rules, offer_rules=offer_rules, now=now)
        return rank(self.candidates(), learned, rules=rules, offer_rules=offer_rules,
                    now=now, blocked=self.blocked(), scorer=scorer)


class StoreOfferDecisionSource(OfferDecisionSource):
    """The real seam: decisions replayed out of `offer_events`, blocked terms
    from the store's own memory of 'no'. Candidates are injected because the
    caller that ranks them holds the artifact -- the importer passes its
    normalized candidate list, PR L passes `offers.list_offers(...)`."""

    def __init__(self, conn, candidates=(), now=None):
        self.conn = conn
        self._candidates = list(candidates)
        self._now = _now(now) if now is not None else None

    def decisions(self, since=None):
        sync_from_offer_events(self.conn, now=self._now)
        return decisions(self.conn, since=since)

    def candidates(self):
        return list(self._candidates)

    def blocked(self):
        return offers.blocked_offer_keys(self.conn, now=self._now)


class MemoryOfferDecisionSource(OfferDecisionSource):
    """In-memory fixture fake -- no DB, no clock, no store. Tests run against
    this so they keep passing whichever way the store is wired later."""

    def __init__(self, decisions=(), candidates=(), blocked=()):
        self._decisions = list(decisions)
        self._candidates = list(candidates)
        self._blocked = set(blocked)

    def decisions(self, since=None):
        if since is None:
            return list(self._decisions)
        return [d for d in self._decisions if (d.decided_at or "") >= since]

    def candidates(self):
        return list(self._candidates)

    def blocked(self):
        return set(self._blocked)

    def decide(self, offer_key, decision, **kwargs):
        kwargs.setdefault("decided_at", _stamp(_now()))
        recorded = OfferDecision(offer_key=offer_key, decision=decision, **kwargs)
        self._decisions.append(recorded)
        return recorded
