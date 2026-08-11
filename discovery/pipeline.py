"""The pipeline, in stages:

    collect -> normalize -> dedup -> persist -> interest matching
            -> cheap pre-filter -> LLM scoring -> threshold -> notification

`ingest()` is that whole chain for one candidate and is what both run_once()
and `python -m app score` call, so a manual run exercises exactly the
production path.

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

# Notification channel tag for immediate discovery sends. Distinct from plain
# "telegram" so the per-day immediate cap counts only immediate traffic, not
# digest/alert sends (both still record as "telegram").
IMMEDIATE_CHANNEL = "telegram_immediate"


@dataclass
class Outcome:
    """Where one candidate stopped, and why."""
    stage: str
    item: CandidateItem
    detail: str = ""
    matches: list = field(default_factory=list)   # [(interest, match_score, terms)]
    score: ScoreResult = None
    # 'explore' iff the best match (matches[0]) is a non-owner interest --
    # see classify_lane(). Drives which budget/counter this outcome charges.
    lane: str = "exploit"

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
    restricts collection to those collector names (`run-once --source`, one
    per OS-scheduled job -- see health.py/ops/install_tasks.py) -- an
    interest that doesn't list a given source in its own `sources` still
    skips it. Returns a counts summary.

    At most `cfg.max_scores_per_cycle` items are scored. Past that the cycle
    keeps collecting and filtering but stops paying: the surplus is stored
    unscored and picked up by _score_backlog() next cycle, so a collector that
    suddenly returns hundreds of candidates costs one cycle's budget, not
    hundreds of LLM calls.

    A second, wholly separate budget (`cfg.explore_max_scores_per_cycle`)
    pays for items whose best match is a non-owner (derived) interest --
    see Outcome.lane / classify_lane(). Structurally zero while
    `cfg.dynamic_interests` is off, so a stray derived row still can't spend
    a single exploration score. Exploration outcomes are bumped under
    `explore_<stage>` metric names (see outcome_metric()) so exploitation
    funnel/quality numbers in stats.py never include them.

    Always ends by delivering ALERT-type notifications immediately; DISCOVERY
    ones are left pending for send_digest() to batch."""
    interests = db.active_interests(conn)
    counts = Counter()
    budget, explore_budget = budgets_for(cfg)

    for interest in interests:
        for item in _collect(conn, interest, cfg, provider, sources):
            counts["collected"] += 1
            outcome = ingest(
                conn, provider, cfg, item, interests,
                origin_interest=interest.key, budget=budget, explore_budget=explore_budget,
            )
            counts[outcome.stage] += 1
            # Flushed per item, not once at the end of the whole cycle: this
            # loop does real per-item I/O (LLM calls, external fetches), each
            # a chance to throw. Every item's own DB rows are already durably
            # committed by ingest() regardless -- only the funnel counters
            # were at risk of vanishing for a cycle that scored real items
            # and then died before reaching the trailing db.bump() below.
            db.bump(conn, {"collected": 1, outcome_metric(outcome): 1})

    # Items stored on an earlier cycle that passed the filter but never got a
    # score (the run died, the API was down, the budget ran out) -- scored with
    # whatever budget this cycle's own candidates (of the matching lane) left
    # over.
    backlog = _score_backlog(conn, provider, interests, budget, explore_budget)
    counts["scored"] += backlog["exploit"] + backlog["explore"]
    lane_notified = Counter()
    counts["notified"] = deliver(conn, cfg, dry_run, lane_counts=lane_notified)
    db.bump(conn, {
        "scored": backlog["exploit"],
        "explore_scored": backlog["explore"],
        "notified": lane_notified["exploit"],
        "explore_notified": lane_notified["explore"],
    })
    db.record_usage(conn, provider)   # per cycle, so `run` reports without exiting
    return {stage: counts[stage] for stage in STAGES}


def outcome_metric(outcome):
    """The db.bump() name for one Outcome -- explore_<stage> for the
    exploration lane, <stage> unprefixed for exploit (today's behavior)."""
    return f"explore_{outcome.stage}" if outcome.lane == "explore" else outcome.stage


def budgets_for(cfg):
    """The (exploit, explore) Budget pair for one cycle -- shared by
    run_once() and missions.web_tick() so both draw from the same cfg knobs
    instead of duplicating this two-line construction."""
    return (
        Budget(cfg.max_scores_per_cycle),
        Budget(cfg.explore_max_scores_per_cycle if cfg.dynamic_interests else 0),
    )


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

def classify_lane(matches):
    """'explore' iff the best match (matches[0], match_interests()'s own
    strongest-first order) is a non-owner interest -- the best match is what
    drives the score's feedback block, min_score bar and notification
    attribution, so it's what decides the lane. A plain attribute compare,
    trivially total: no matches (never reached with prefilter already
    passed, but defensive) is exploit, same as an owner match."""
    return "explore" if matches and matches[0][0].layer != "owner" else "exploit"


def ingest(conn, provider, cfg, item, interests, origin_interest=None, force=False,
           budget=None, explore_budget=None):
    """Normalize, dedup, persist, match, filter, score. Returns an Outcome.

    `explore_budget` defaults to None so every existing caller (score --,
    teach.py, tests) keeps ingest()'s old single-budget behavior untouched --
    only run_once()/`_discover` pass both."""
    normalize.normalize(item, origin_interest)
    # Computed before dedup/insert -- match_interests() is a pure function of
    # item + interests (no item.id involved), so the lane a duplicate would
    # have charged is known even though a duplicate is never itself scored.
    matches = matching.match_interests(item, interests)
    lane = classify_lane(matches)

    duplicate = dedup.find_duplicate(conn, item)
    if duplicate is not None and not force:
        return Outcome("duplicate", duplicate.existing, duplicate.reason, lane=lane)

    item.id = db.insert_item(conn, item)
    db.save_matches(conn, item.id, matches)

    ok, reason = matching.prefilter(item, matches, cfg)
    db.set_prefilter(conn, item.id, ok, reason)
    if not ok:
        return Outcome("filtered", item, reason, matches, lane=lane)

    if db.is_scored(conn, item.id):
        if not force:
            return Outcome("already_scored", item, "scored on an earlier run", matches, lane=lane)
        db.delete_score(conn, item.id)

    lane_budget = explore_budget if lane == "explore" else budget
    if lane_budget is not None and not lane_budget.spend():
        return Outcome("deferred", item, "cycle score budget spent", matches, lane=lane)

    return _score(conn, provider, item, matches, reason, lane=lane)


def _score(conn, provider, item, matches, detail="", lane="exploit"):
    feedback = db.recent_feedback(conn, matches[0][0].id)
    try:
        score = scoring.score_candidate(provider, item, matches, feedback)
    except Exception as e:  # noqa: BLE001
        print(f"scoring item {item.id} failed: {e}", file=sys.stderr)
        # Stamp the failure so _score_backlog() waits out a cool-off instead
        # of re-failing this item on every cycle while the provider is down.
        db.mark_score_attempt(conn, item.id)
        return Outcome("errors", item, str(e), matches, lane=lane)
    # Mirrors item.id = db.insert_item(conn, item) above: the caller gets a
    # ScoreResult whose id actually reflects the row just written, the same
    # contract CandidateItem.id already carries. Nothing in the current
    # pipeline reads it back off this object (deliver()/send_digest() re-query
    # score_id from the DB), but leaving it None was still a real inconsistency
    # -- exactly the kind of stale-id bug that bites the next caller who
    # reasonably assumes it works like item.id.
    score.id = db.save_score(conn, score)
    return Outcome("scored", item, detail, matches, score, lane=lane)


def _score_backlog(conn, provider, interests, budget, explore_budget=None):
    """Newest first, paged in batches sized to whatever budget each lane has
    left this cycle. Without a cap a persistent scoring failure would
    re-attempt every unscored item ever stored, on every cycle, forever.
    Items whose last scoring attempt failed recently are skipped until the
    cool-off passes (never dropped) -- a provider outage otherwise re-fails
    the whole backlog every cycle.

    A single SELECT ... LIMIT <sum of both budgets> is NOT enough: lane is
    only known after fetching+matching a row, so a batch that happens to be
    all one lane (e.g. the newest rows are all explore-classified while
    explore_budget is 0/spent) would `continue` past every row and return
    with the exploit budget untouched and the exploit backlog never
    reached -- a real, not transient, starvation, since a lane-blocked
    backlog item is deferred, not attempted, and so keeps re-occupying the
    newest-first window on every future cycle too. Paging with an id cursor
    fixes this: a batch that makes no lane spend still advances the cursor,
    so the next page reaches older rows (which may belong to the
    unstarved lane) instead of re-fetching the same stuck rows forever.

    Returns a Counter of items actually scored per lane. One lane running
    out never stops the other from draining: an exhausted lane's items are
    skipped (`continue`), not a `break` of the whole pass."""
    scored = Counter()
    cursor_id = None
    while budget.remaining > 0 or (explore_budget is not None and explore_budget.remaining > 0):
        batch_limit = budget.remaining + (explore_budget.remaining if explore_budget is not None else 0)
        if batch_limit <= 0:
            break
        rows = conn.execute(
            """
            SELECT id FROM candidate_items
            WHERE prefilter_ok = 1
              AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.item_id = candidate_items.id)
              AND (score_attempted_at IS NULL OR score_attempted_at < ?)
              AND (? IS NULL OR id < ?)
            ORDER BY id DESC LIMIT ?
            """,
            (db.ago(db.SCORE_RETRY_SECONDS), cursor_id, cursor_id, batch_limit),
        ).fetchall()
        if not rows:
            break
        cursor_id = rows[-1]["id"]  # oldest id fetched this page -- next page starts below it
        for row in rows:
            item = db.get_item(conn, row["id"])
            matches = matching.match_interests(item, interests)
            if not matches:
                continue
            lane = classify_lane(matches)
            lane_budget = explore_budget if lane == "explore" else budget
            if lane_budget is not None and not lane_budget.spend():
                continue  # this lane is out for the cycle -- let the other lane keep draining
            if _score(conn, provider, item, matches, lane=lane).stage == "scored":
                scored[lane] += 1
        if len(rows) < batch_limit:
            break  # fewer rows than asked for -- the eligible backlog is exhausted
    return scored


# --- stage 8: threshold + notification ---------------------------------------

def notification_ready(conn, cfg):
    """Scores that cleared their interest's bar and have not been sent.

    The threshold lives in SQL (`final_score >= interests.min_score`) so a
    lowered bar picks up items scored under the old one automatically. The
    retry policy (attempt cap, cool-off) comes from cfg -- see
    db.pending_notifications().
    """
    by_id = {i.id: i for i in db.active_interests(conn)}
    ready = []
    for row in db.pending_notifications(
        conn, max_attempts=cfg.send_max_attempts, retry_after_seconds=cfg.send_retry_seconds
    ):
        item = db.get_item(conn, row["item_id"])
        interest = by_id.get(row["interest_id"])
        if item is None or interest is None:
            continue
        ready.append((row, item, interest))
    return ready


def deliver(conn, cfg, dry_run=False, lane_counts=None):
    """ALERT-type items the moment they clear the bar. DISCOVERY items are
    normally left pending for send_digest() -- unless cfg.immediate_discovery
    is on, in which case freshly-scored discoveries also go out here, bounded
    so a backlog or a burst can't flood the owner (see cfg.immediate_*).

    `lane_counts`, if given, is a Counter bumped with 'exploit'/'explore' per
    attempt (owner vs. derived interest) -- run_once() uses it to bump
    notified/explore_notified separately; every other caller (CLI, tests)
    leaves it None and gets the old plain int back, unchanged."""
    sent = 0
    cycle_left = cfg.immediate_max_per_cycle if cfg.immediate_discovery else 0
    # The per-day cap counts ONLY prior immediate sends (IMMEDIATE_CHANNEL), not
    # digest/alert traffic -- otherwise a busy digest day would zero the budget
    # and the feature would silently never fire.
    day_left = (
        max(0, cfg.immediate_max_per_day - db.successful_notifications_since(
            conn, db.ago(24 * 3600), channel=IMMEDIATE_CHANNEL))
        if cfg.immediate_discovery else 0
    )
    fresh_cutoff = db.ago(cfg.immediate_fresh_seconds)
    for row, item, interest in notification_ready(conn, cfg):
        if notify.is_alert(item):
            sent += _send_one(conn, cfg, row, item, interest, dry_run, lane_counts)
        elif (cycle_left > 0 and day_left > 0
              and row["score_created_at"] > fresh_cutoff):
            n = _send_one(conn, cfg, row, item, interest, dry_run, lane_counts,
                          channel=IMMEDIATE_CHANNEL)
            sent += n
            cycle_left -= n
            day_left -= n
    return sent


def send_digest(conn, cfg, dry_run=False, lane_counts=None):
    """DISCOVERY items only, sorted by final_score (already
    notification_ready()'s order), capped at cfg.digest_max_items. Anything
    past the cap simply stays pending for the next digest slot -- it was never
    marked notified. Alerts are never touched here; deliver() already sent
    them. `lane_counts`: see deliver() -- no caller passes one today (the
    `digest` CLI command bumps no funnel metric at all, same as before this
    step), so a digest send never increments notified/explore_notified; the
    parameter exists so a future caller that does want that split doesn't
    need a signature change to get it."""
    sent = 0
    for row, item, interest in notification_ready(conn, cfg):
        if notify.is_alert(item):
            continue
        if sent >= cfg.digest_max_items:
            break
        sent += _send_one(conn, cfg, row, item, interest, dry_run, lane_counts)
    return sent


def _send_one(conn, cfg, row, item, interest, dry_run, lane_counts=None,
              channel="telegram"):
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
    db.record_notification(conn, row["score_id"], channel, ok)
    if not ok:
        db.bump(conn, {"send_failed": 1})
    if lane_counts is not None:
        lane_counts["explore" if interest.layer != "owner" else "exploit"] += 1
    return 1
