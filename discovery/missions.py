"""The one-minute web-discovery tick.

Replaces the old periodic single-static-prompt `web_search` collector as the
scheduled path (see ops/install_tasks.py's collect-web task): instead of one
batch pass every few hours, `web_tick()` runs every minute, short-lived,
idempotent and safe to overlap. Every tick, in order:

    1. reclaim any RUNNING mission whose lease expired (a prior tick died
       mid-execution);
    2. replenish AT MOST ONE owner interest's mission queue via one Council
       call (discovery/council.py) -- never a burst across every interest;
    3. pick a fair slice of PENDING missions across owner interests (one per
       interest per round, round-robining only if missions_per_tick exceeds
       the number of interests with work);
    4. atomically lease them (discovery/db.py's lease_missions());
    5. execute each leased mission independently via the search-capable
       provider, one mission's failure never touching another;
    6. stamp provenance onto every returned CandidateItem;
    7. feed each one through the existing pipeline.ingest()/deliver() --
       normalize, dedup, persist, match, prefilter, score, threshold,
       notify -- no parallel scorer/deduper/matcher/notifier;
    8. persist each mission's completion/failure and provider usage.

All state lives in search_generations/search_missions; nothing is carried in
memory between ticks and there is no in-process scheduler.

Deliberately does NOT call pipeline._score_backlog(): at a one-minute
cadence that would spend cfg.max_scores_per_cycle every single minute.
Backlog draining stays on the run-once jobs (stocks/youtube), exactly as
before this step.
"""
import dataclasses
import sys
from collections import Counter

from . import council, db, pipeline, providers
from .collectors import _search
from .collectors.web_search import PROMPT as STATIC_FALLBACK_PROMPT

FALLBACK_LABEL = "static-fallback"

RESEARCH_FRAMING = """\
This is a RESEARCH MISSION, not a single literal query. Search iteratively \
({max_searches} searches max): follow the entities, terminology, names and \
references you find along the way instead of stopping at the first result. \
Return several genuinely distinct discoveries, not near-duplicates of each \
other.

{mission_prompt}

{result_spec}
"""


def web_tick(conn, cfg, provider=None, dry_run=False):
    """`provider`, if given, is the scoring provider (same meaning as every
    other pipeline entry point's `provider` argument) -- defaults to
    providers.get_provider(cfg), i.e. cfg.provider, never cfg.mission_provider.
    The search-capable mission provider is always built fresh here from
    cfg.mission_provider/cfg.mission_model."""
    scoring_provider = provider or providers.get_provider(cfg)

    db.recover_stale_missions(conn, cfg.mission_max_attempts)

    mission_cfg = dataclasses.replace(cfg, provider=cfg.mission_provider, model=cfg.mission_model)
    mission_provider = providers.get_provider(mission_cfg)
    ok, detail = mission_provider.preflight()
    if not ok:
        print(
            f"web_tick: mission provider '{cfg.mission_provider}' preflight failed -- {detail}",
            file=sys.stderr,
        )
        return {"leased": 0, "executed": 0, "notified": 0, "preflight_ok": False}

    all_interests = db.active_interests(conn)
    owner_interests = [i for i in all_interests if i.layer == "owner"]

    _replenish(conn, cfg, mission_provider, owner_interests)

    mission_ids = _select_fair(conn, cfg, owner_interests)
    leased_ids = db.lease_missions(conn, mission_ids, cfg.mission_lease_seconds)

    budget, explore_budget = pipeline.budgets_for(cfg)
    for mission_id in leased_ids:
        _execute_mission(
            conn, cfg, mission_provider, scoring_provider, all_interests,
            mission_id, budget, explore_budget,
        )

    lane_notified = Counter()
    notified = pipeline.deliver(conn, cfg, dry_run, lane_counts=lane_notified)
    db.bump(conn, {
        "notified": lane_notified["exploit"],
        "explore_notified": lane_notified["explore"],
    })
    db.record_usage(conn, mission_provider)
    db.record_usage(conn, scoring_provider)
    return {
        "leased": len(leased_ids), "executed": len(leased_ids),
        "notified": notified, "preflight_ok": True,
    }


# --- step 2: replenish at most one interest -----------------------------------

def _replenish(conn, cfg, mission_provider, owner_interests):
    """One Council call per tick, at most: the owner interest with the
    fewest PENDING missions below cfg.mission_low_water, tie-broken by
    longest-since-last-generation (never-generated sorts first), then by key
    for a deterministic pick among true ties. This is what makes an empty
    DB self-populate one interest per tick instead of bursting through all
    of them -- Council generation is a reservoir refill, not a per-interest-
    per-minute operation."""
    candidates = []
    for interest in owner_interests:
        pending = db.pending_mission_count(conn, interest.key)
        if pending >= cfg.mission_low_water:
            continue
        candidates.append((pending, _last_generation_at(conn, interest.key) or "", interest))
    if not candidates:
        return
    candidates.sort(key=lambda c: (c[0], c[1], c[2].key))
    _, _, interest = candidates[0]
    _generate_for(conn, cfg, mission_provider, interest)


def _last_generation_at(conn, interest_key):
    row = conn.execute(
        "SELECT MAX(created_at) AS t FROM search_generations WHERE interest_key = ?",
        (interest_key,),
    ).fetchone()
    return row["t"] if row else None


def _generate_for(conn, cfg, mission_provider, interest):
    context = council.build_context(conn, interest, cfg)
    generation_id = db.insert_generation(
        conn, interest.key, mission_provider.name, mission_provider.model,
        cfg.council_missions_per_generation,
    )
    try:
        missions = council.plan_missions(
            mission_provider, interest, context, cfg.council_missions_per_generation
        )
    except (council.CouncilError, providers.ProviderError) as e:
        db.finish_generation(conn, generation_id, "FAILED", 0, str(e))
        _maybe_enqueue_fallback(conn, cfg, interest)
        return
    db.insert_missions(conn, generation_id, interest.key, missions)
    db.finish_generation(conn, generation_id, "DONE", len(missions))


def _consecutive_failures(conn, interest_key, limit):
    rows = conn.execute(
        "SELECT status FROM search_generations WHERE interest_key = ? ORDER BY id DESC LIMIT ?",
        (interest_key, limit),
    ).fetchall()
    return len(rows) >= limit and all(r["status"] == "FAILED" for r in rows)


def _maybe_enqueue_fallback(conn, cfg, interest):
    """The bounded static-fallback path (objective C's legacy role for
    discovery/collectors/web_search.py): only fires once the Council has
    failed cfg.council_max_consecutive_failures times in a row for this
    interest, and only enqueues one at a time (never piles up while the
    Council stays down)."""
    if not _consecutive_failures(conn, interest.key, cfg.council_max_consecutive_failures):
        return
    existing = conn.execute(
        "SELECT 1 FROM search_missions WHERE interest_key = ? AND label = ? AND status = 'PENDING'",
        (interest.key, FALLBACK_LABEL),
    ).fetchone()
    if existing:
        return
    prompt = STATIC_FALLBACK_PROMPT.format(
        title=interest.title,
        description=interest.description,
        positive=", ".join(interest.positive_signals) or "(unspecified)",
        negative=", ".join(interest.negative_signals) or "(unspecified)",
        limit=cfg.mission_max_results,
        max_uses=cfg.mission_max_searches,
        result_spec=_search.RESULT_SPEC,
    )
    db.insert_missions(conn, None, interest.key, [{
        "label": FALLBACK_LABEL,
        "rationale": "Council generation failed repeatedly; static fallback query.",
        "prompt": prompt,
    }])


# --- step 3: fair selection across owner interests ----------------------------

def _select_fair(conn, cfg, owner_interests):
    """Order owner interests by their own last mission started_at ascending
    (never-run first), then take at most one eligible PENDING mission per
    interest per round, wrapping to a second round only if
    cfg.missions_per_tick exceeds the number of interests with work. A
    200-mission queue on one interest can never starve another that has
    any work at all."""
    if not owner_interests:
        return []
    order = sorted(owner_interests, key=lambda i: _last_mission_started_at(conn, i.key) or "")
    queues = {i.key: _eligible_mission_ids(conn, i.key, cfg.missions_per_tick) for i in order}

    selected = []
    round_idx = 0
    while len(selected) < cfg.missions_per_tick:
        progressed = False
        for interest in order:
            if len(selected) >= cfg.missions_per_tick:
                break
            queue = queues[interest.key]
            if round_idx < len(queue):
                selected.append(queue[round_idx])
                progressed = True
        if not progressed:
            break
        round_idx += 1
    return selected


def _last_mission_started_at(conn, interest_key):
    row = conn.execute(
        "SELECT MAX(started_at) AS t FROM search_missions WHERE interest_key = ?",
        (interest_key,),
    ).fetchone()
    return row["t"] if row else None


def _eligible_mission_ids(conn, interest_key, limit):
    rows = conn.execute(
        """
        SELECT id FROM search_missions
        WHERE interest_key = ? AND status = 'PENDING'
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (interest_key, db.now(), limit),
    ).fetchall()
    return [r["id"] for r in rows]


# --- steps 5-8: execute one leased mission -------------------------------------

def _execute_mission(conn, cfg, mission_provider, scoring_provider, interests,
                      mission_id, budget, explore_budget):
    mission = db.mission_by_id(conn, mission_id)
    if mission is None:   # defensive -- lease_missions() only returns ids it just claimed
        return
    try:
        prompt = RESEARCH_FRAMING.format(
            max_searches=cfg.mission_max_searches,
            mission_prompt=mission["prompt"],
            result_spec=_search.RESULT_SPEC,
        )
        raw_items = mission_provider.search_json(prompt, max_searches=cfg.mission_max_searches)
        items = _search.to_items(raw_items, "web_search", cfg.mission_max_results)
    except Exception as e:  # noqa: BLE001 -- one mission's failure must not stop the others
        print(f"mission {mission_id} ({mission['label']}) failed: {e}", file=sys.stderr)
        db.fail_mission(conn, mission_id, str(e), cfg.mission_max_attempts, cfg.mission_retry_seconds)
        return

    for item in items:
        item.origin_interest = mission["interest_key"]
        item.metadata.update(
            generation_id=mission["generation_id"],
            mission_id=mission["id"],
            mission_label=mission["label"],
            prompt_sha256=mission["prompt_sha256"],
        )
        outcome = pipeline.ingest(
            conn, scoring_provider, cfg, item, interests,
            origin_interest=mission["interest_key"], budget=budget, explore_budget=explore_budget,
        )
        db.bump(conn, {"collected": 1, pipeline.outcome_metric(outcome): 1})

    db.finish_mission(conn, mission_id, len(items))
