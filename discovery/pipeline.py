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

STAGES = ("collected", "duplicate", "filtered", "already_scored", "scored", "errors", "notified")


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


def run_once(conn, provider, cfg, dry_run=False):
    """One full cycle over every active interest. Returns a counts summary."""
    interests = db.active_interests(conn)
    counts = Counter()

    for interest in interests:
        for item in _collect(interest, cfg, provider):
            counts["collected"] += 1
            outcome = ingest(conn, provider, cfg, item, interests, origin_interest=interest.key)
            counts[outcome.stage] += 1

    # Items stored on an earlier cycle that passed the filter but never got a
    # score (the run died, the API was down): pick them up before spending on
    # anything new.
    counts["scored"] += _score_backlog(conn, provider, cfg, interests)
    counts["notified"] = deliver(conn, cfg, dry_run)
    return {stage: counts[stage] for stage in STAGES}


# --- stage 1: collect --------------------------------------------------------

def _collect(interest, cfg, provider):
    items = []
    for source in interest.sources:
        collect = COLLECTORS.get(source)
        if collect is None:
            print(f"{interest.key}: unknown source '{source}'", file=sys.stderr)
            continue
        try:
            items.extend(collect(interest, cfg, provider))
        except Exception as e:  # noqa: BLE001
            print(f"{interest.key}/{source}: collect failed: {e}", file=sys.stderr)
    return items


# --- stages 2-7: one candidate through the chain -----------------------------

def ingest(conn, provider, cfg, item, interests, origin_interest=None, force=False):
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

    return _score(conn, provider, item, matches, reason)


def _score(conn, provider, item, matches, detail=""):
    feedback = db.recent_feedback(conn, matches[0][0].id)
    try:
        score = scoring.score_candidate(provider, item, matches, feedback)
    except Exception as e:  # noqa: BLE001
        print(f"scoring item {item.id} failed: {e}", file=sys.stderr)
        return Outcome("errors", item, str(e), matches)
    db.save_score(conn, score)
    return Outcome("scored", item, detail, matches, score)


def _score_backlog(conn, provider, cfg, interests):
    rows = conn.execute(
        """
        SELECT id FROM candidate_items
        WHERE prefilter_ok = 1
          AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.item_id = candidate_items.id)
        ORDER BY id
        """
    ).fetchall()
    scored = 0
    for row in rows:
        item = db.get_item(conn, row["id"])
        matches = matching.match_interests(item, interests)
        if not matches:
            continue
        if _score(conn, provider, item, matches).stage == "scored":
            scored += 1
    return scored


# --- stage 8: threshold + notification ---------------------------------------

def notification_ready(conn):
    """Scores that cleared their interest's bar and have not been sent.

    The threshold lives in SQL (`final_score >= interests.min_score`) so a
    lowered bar picks up items scored under the old one automatically.
    """
    ready = []
    for row in db.pending_notifications(conn):
        item = db.get_item(conn, row["item_id"])
        interest = next(
            (i for i in db.active_interests(conn) if i.id == row["interest_id"]), None
        )
        if item is None or interest is None:
            continue
        ready.append((row, item, interest))
    return ready


def deliver(conn, cfg, dry_run=False):
    sent = 0
    for row, item, interest in notification_ready(conn):
        text = notify.format_message(
            interest,
            item,
            row["final_score"],
            row["reason"],
            row["why_better_than_generic"],
            row["confidence"],
        )
        ok = notify.send(cfg, text, dry_run=dry_run)
        # Recorded either way: a failed send stays recorded as not-ok rather
        # than being retried forever on every cycle.
        db.record_notification(conn, row["score_id"], "telegram", ok)
        sent += 1
    return sent
