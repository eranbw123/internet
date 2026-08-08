"""The pipeline, in stages:

    collect -> normalize -> dedup -> persist -> interest matching
            -> cheap pre-filter -> LLM scoring -> threshold -> notification

`ingest()` is that whole chain for one candidate and is what both the
scheduler and `python -m app score` call, so a manual run exercises exactly
the production path.

Two properties everything else leans on:

  * Each stage is failure-isolated. A dead collector, an unscoreable item, or
    a failed push is reported and skipped; the cycle continues.
  * Each stage's verdict is persisted. Items are collected once, filtered
    once, scored once, and pushed once -- a cycle that dies halfway resumes on
    the next one instead of re-paying for the same LLM calls.
"""
import sys
from collections import Counter
from dataclasses import dataclass, field

from . import db, dedup, matching, normalize, notify, scoring
from .collectors import COLLECTORS
from .models import CandidateItem, ScoreResult

STAGES = (
    "collected", "duplicate", "filtered", "already_scored",
    "scored", "deferred", "errors", "notified",
)


@dataclass
class Outcome:
    """Where one candidate stopped, and why."""
    stage: str
    item: CandidateItem
    detail: str = ""
    matches: list = field(default_factory=list)   # [(interest, match_score, terms)]
    score: ScoreResult = None

    def as_dict(self):
        return {
            "stage": self.stage,
            "detail": self.detail,
            "item": {"id": self.item.id, "title": self.item.title, "url": self.item.url},
            "matched_interests": [
                {"key": i.key, "match_score": s, "terms": t} for i, s, t in self.matches
            ],
            "score": None
            if self.score is None
            else {
                "interest": self.score.interest_key,
                "final_score": self.score.final_score,
                "confidence": self.score.confidence,
                "dimensions": self.score.dimensions,
                "reason": self.score.reason,
                "why_better_than_generic": self.score.why_better_than_generic,
                "provider": self.score.provider,
                "model": self.score.model,
            },
        }


def run_once(conn, provider, cfg, sources=None, dry_run=False):
    """One full cycle over every active interest. `sources`, if given,
    restricts collection to those collector names (scheduler.py's per-job
    cadence, or `run-once --source`) -- an interest that doesn't list a given
    source in its own `sources` still skips it. Returns a counts summary.

    At most `cfg.max_scores_per_cycle` items are scored. Past that the cycle
    keeps collecting and filtering but stops paying: the surplus is stored
    unscored and picked up by _score_backlog() next cycle, so a collector that
    suddenly returns hundreds of candidates costs one cycle's budget, not
    hundreds of LLM calls.

    Always ends by delivering ALERT-type notifications immediately; DISCOVERY
    ones are left pending for send_digest() to batch."""
    interests = db.active_interests(conn)
    counts = Counter()
    budget = Budget(cfg.max_scores_per_cycle)

    for interest in interests:
        for item in _collect(conn, interest, cfg, provider, sources):
            counts["collected"] += 1
            outcome = ingest(
                conn, provider, cfg, item, interests,
                origin_interest=interest.key, budget=budget,
            )
            counts[outcome.stage] += 1
            # Flushed per item, not once at the end of the whole cycle: this
            # loop does real per-item I/O (LLM calls, external fetches), each
            # a chance to throw. Every item's own DB rows are already durably
            # committed by ingest() regardless -- only the funnel counters
            # were at risk of vanishing for a cycle that scored real items
            # and then died before reaching the trailing db.bump() below.
            db.bump(conn, {"collected": 1, outcome.stage: 1})

    # Items stored on an earlier cycle that passed the filter but never got a
    # score (the run died, the API was down, the budget ran out) -- scored with
    # whatever budget this cycle's own candidates left over.
    backlog_scored = _score_backlog(conn, provider, interests, budget)
    counts["scored"] += backlog_scored
    counts["notified"] = deliver(conn, cfg, dry_run)
    db.bump(conn, {"scored": backlog_scored, "notified": counts["notified"]})
    db.record_usage(conn, provider)   # per cycle, so `run` reports without exiting
    return {stage: counts[stage] for stage in STAGES}


class Budget:
    """How many more LLM scores this cycle may pay for. A plain counter rather
    than a rate limiter -- the cap only has to be per-cycle, because anything
    it defers is already durable and gets scored on the next one."""

    def __init__(self, limit):
        self.remaining = limit

    def spend(self):
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


# --- stage 1: collect --------------------------------------------------------

def _collect(conn, interest, cfg, provider, sources=None):
    items = []
    for source in interest.sources:
        if sources is not None and source not in sources:
            continue
        collect = COLLECTORS.get(source)
        if collect is None:
            print(f"{interest.key}: unknown source '{source}'", file=sys.stderr)
            continue
        try:
            items.extend(collect(interest, cfg, provider, conn))
        except Exception as e:  # noqa: BLE001
            print(f"{interest.key}/{source}: collect failed: {e}", file=sys.stderr)
    return items


# --- stages 2-7: one candidate through the chain -----------------------------

def ingest(conn, provider, cfg, item, interests, origin_interest=None, force=False, budget=None):
    """Normalize, dedup, persist, match, filter, score. Returns an Outcome."""
    normalize.normalize(item, origin_interest)

    duplicate = dedup.find_duplicate(conn, item)
    if duplicate is not None and not force:
        return Outcome("duplicate", duplicate.existing, duplicate.reason)

    item.id = db.insert_item(conn, item)
    matches = matching.match_interests(item, interests)
    db.save_matches(conn, item.id, matches)

    ok, reason = matching.prefilter(item, matches, cfg)
    db.set_prefilter(conn, item.id, ok, reason)
    if not ok:
        return Outcome("filtered", item, reason, matches)

    if db.is_scored(conn, item.id):
        if not force:
            return Outcome("already_scored", item, "scored on an earlier run", matches)
        db.delete_score(conn, item.id)

    if budget is not None and not budget.spend():
        return Outcome("deferred", item, "cycle score budget spent", matches)

    return _score(conn, provider, item, matches, reason)


def _score(conn, provider, item, matches, detail=""):
    feedback = db.recent_feedback(conn, matches[0][0].id)
    try:
        score = scoring.score_candidate(provider, item, matches, feedback)
    except Exception as e:  # noqa: BLE001
        print(f"scoring item {item.id} failed: {e}", file=sys.stderr)
        # Stamp the failure so _score_backlog() waits out a cool-off instead
        # of re-failing this item on every cycle while the provider is down.
        db.mark_score_attempt(conn, item.id)
        return Outcome("errors", item, str(e), matches)
    # Mirrors item.id = db.insert_item(conn, item) above: the caller gets a
    # ScoreResult whose id actually reflects the row just written, the same
    # contract CandidateItem.id already carries. Nothing in the current
    # pipeline reads it back off this object (deliver()/send_digest() re-query
    # score_id from the DB), but leaving it None was still a real inconsistency
    # -- exactly the kind of stale-id bug that bites the next caller who
    # reasonably assumes it works like item.id.
    score.id = db.save_score(conn, score)
    return Outcome("scored", item, detail, matches, score)


def _score_backlog(conn, provider, interests, budget):
    """Newest first and capped by whatever budget the cycle has left. Without
    the cap a persistent scoring failure would re-attempt every unscored item
    ever stored, on every cycle, forever. Items whose last scoring attempt
    failed recently are skipped until the cool-off passes (never dropped) --
    a provider outage otherwise re-fails the whole backlog every cycle."""
    if budget.remaining <= 0:
        return 0
    rows = conn.execute(
        """
        SELECT id FROM candidate_items
        WHERE prefilter_ok = 1
          AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.item_id = candidate_items.id)
          AND (score_attempted_at IS NULL OR score_attempted_at < ?)
        ORDER BY id DESC LIMIT ?
        """,
        (db.ago(db.SCORE_RETRY_SECONDS), budget.remaining),
    ).fetchall()
    scored = 0
    for row in rows:
        item = db.get_item(conn, row["id"])
        matches = matching.match_interests(item, interests)
        if not matches:
            continue
        if not budget.spend():
            break
        if _score(conn, provider, item, matches).stage == "scored":
            scored += 1
    return scored


# --- stage 8: threshold + notification ---------------------------------------

def notification_ready(conn):
    """Scores that cleared their interest's bar and have not been sent.

    The threshold lives in SQL (`final_score >= interests.min_score`) so a
    lowered bar picks up items scored under the old one automatically.
    """
    by_id = {i.id: i for i in db.active_interests(conn)}
    ready = []
    for row in db.pending_notifications(conn):
        item = db.get_item(conn, row["item_id"])
        interest = by_id.get(row["interest_id"])
        if item is None or interest is None:
            continue
        ready.append((row, item, interest))
    return ready


def deliver(conn, cfg, dry_run=False):
    """ALERT-type items only, sent the moment they clear the bar. DISCOVERY
    items are left pending -- send_digest() is what clears those."""
    sent = 0
    for row, item, interest in notification_ready(conn):
        if not notify.is_alert(item):
            continue
        sent += _send_one(conn, cfg, row, item, interest, dry_run)
    return sent


def send_digest(conn, cfg, dry_run=False):
    """DISCOVERY items only, sorted by final_score (already
    notification_ready()'s order), capped at cfg.digest_max_items. Anything
    past the cap simply stays pending for tomorrow's digest -- it was never
    marked notified. Alerts are never touched here; deliver() already sent
    them."""
    sent = 0
    for row, item, interest in notification_ready(conn):
        if notify.is_alert(item):
            continue
        if sent >= cfg.digest_max_items:
            break
        sent += _send_one(conn, cfg, row, item, interest, dry_run)
    return sent


def _send_one(conn, cfg, row, item, interest, dry_run):
    text = notify.format_message(
        interest,
        item,
        row["final_score"],
        row["reason"],
        row["why_better_than_generic"],
        row["confidence"],
    )
    ok = notify.send(cfg, text, reply_markup=notify.feedback_keyboard(row["score_id"]), dry_run=dry_run)
    # Recorded either way: a failed send stays recorded as not-ok rather
    # than being retried forever on every cycle.
    db.record_notification(conn, row["score_id"], "telegram", ok)
    return 1
