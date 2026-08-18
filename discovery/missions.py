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
import time
from collections import Counter

from . import council, db, health, pipeline, providers, trace
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
    cfg.mission_provider/cfg.mission_model.

    Wrapped in one trace_runs row (kind='web-tick'; a no-op end to end when
    cfg.trace_enabled is off -- see discovery/trace.py). An exception is
    recorded on the run and re-raised unchanged -- tracing observes the tick,
    it never changes what the tick does or returns."""
    tracer = trace.Tracer(conn, cfg)
    run_id = tracer.begin_run("web-tick", provider=cfg.mission_provider, model=cfg.mission_model)
    try:
        result = _web_tick(conn, cfg, provider, dry_run, tracer)
    except Exception as e:  # noqa: BLE001 -- re-raised as-is right after
        tracer.finish_run(run_id, status="error", error=str(e))
        raise
    tracer.finish_run(run_id, status="done" if result["preflight_ok"] else "preflight_failed")
    return result


def _web_tick(conn, cfg, provider, dry_run, tracer):
    scoring_provider = provider or providers.get_provider(cfg)

    db.recover_stale_missions(conn, cfg.mission_max_attempts)

    mission_cfg = dataclasses.replace(cfg, provider=cfg.mission_provider, model=cfg.mission_model)
    mission_provider = providers.get_provider(mission_cfg)
    mission_provider.trace_sink = tracer.sink
    scoring_provider.trace_sink = tracer.sink
    # Reuses run-once's own gate (not a hand-rolled preflight() call) so a
    # dead mission provider gets the same treatment a dead scoring provider
    # does: one optional cfg.chrome_launch_cmd relaunch + re-check, and the
    # provider_down/job:web:last_fail bookkeeping health.py/stats.py already
    # read -- a bare preflight() call would silently drop both.
    if not health.preflight_gate(conn, mission_provider, cfg, "web"):
        return _tick_result(preflight_ok=False, reason="provider preflight failed")

    deadline = time.monotonic() + cfg.web_tick_budget_seconds
    all_interests = db.active_interests(conn)
    owner_interests = [i for i in all_interests if i.layer == "owner"]
    interest_state_node = tracer.node(
        tracer.run_id, "interest-state", label="active owner interests",
        input_json={"owner_interests": [i.key for i in owner_interests]},
    )

    failures = []
    generated = _replenish(
        conn, cfg, mission_provider, owner_interests, tracer, interest_state_node, failures,
    )

    mission_ids = _select_fair(conn, cfg, owner_interests)
    leased_ids = db.lease_missions(conn, mission_ids, cfg.mission_lease_seconds)

    budget, explore_budget = pipeline.budgets_for(cfg)
    executed = executed_ok = collected = abandoned = 0
    for mission_id in leased_ids:
        # Checked BETWEEN missions, never mid-mission: a leased mission we do
        # not start stays leased until its lease expires and
        # recover_stale_missions() hands it back, which is exactly what the
        # lease is for. Stopping here is what keeps the tick inside the
        # scheduler's ExecutionTimeLimit -- and, unlike being killed, it gets
        # to say so.
        if time.monotonic() >= deadline:
            abandoned += 1
            continue
        executed += 1
        items, error = _execute_mission(
            conn, cfg, mission_provider, scoring_provider, all_interests,
            mission_id, budget, explore_budget, tracer,
        )
        collected += items
        if error:
            failures.append(f"mission {mission_id}: {error}")
        else:
            executed_ok += 1

    lane_notified = Counter()
    notified = pipeline.deliver(conn, cfg, dry_run, lane_counts=lane_notified, tracer=tracer)
    db.bump(conn, {
        "notified": lane_notified["exploit"],
        "explore_notified": lane_notified["explore"],
    })
    db.record_usage(conn, mission_provider)
    db.record_usage(conn, scoring_provider)
    if abandoned:
        failures.append(
            f"{abandoned} leased mission(s) left unexecuted -- the tick's "
            f"{cfg.web_tick_budget_seconds}s budget ran out"
        )
    return _tick_result(
        leased=len(leased_ids), executed=executed, executed_ok=executed_ok, notified=notified,
        generated=generated, collected=collected, abandoned=abandoned, failures=failures,
        owner_interests=len(owner_interests),
    )


def _tick_result(leased=0, executed=0, executed_ok=0, notified=0, generated=0, collected=0,
                  abandoned=0, failures=(), owner_interests=0, preflight_ok=True, reason=None):
    """One tick's outcome, and -- when the outcome was nothing -- WHY.

    `productive` is the honest answer to "did this tick do any work": it is
    what the CLI turns into an exit code, and therefore what decides whether
    `job:web:last_ok` moves. A tick that planned nothing, ran nothing and
    delivered nothing must never stamp a heartbeat that tells `health` the
    web collector is alive; that is the bug that hid five days of total
    web-discovery failure behind a green health report.

    `reason` is always populated when nothing happened, so no caller has to
    infer silence from a row of zeros."""
    failures = list(failures)
    # A mission that RAN and honestly found nothing is work; a mission that
    # ran and failed is not. Counting `executed` here instead of `executed_ok`
    # would let a tick whose every mission errored still stamp job:web:last_ok
    # -- the same lie, one layer down.
    productive = bool(generated or executed_ok or collected or notified)
    if reason is None and not productive:
        if failures:
            reason = "; ".join(failures)
        elif not owner_interests:
            reason = "no active owner interests -- nothing to plan or search for"
        else:
            reason = (
                f"nothing to do: every one of {owner_interests} owner interest(s) is at or "
                f"above its mission low-water mark and no mission was eligible to run"
            )
    return {
        "leased": leased, "executed": executed, "executed_ok": executed_ok,
        "notified": notified, "generated": generated, "collected": collected,
        "abandoned": abandoned, "failures": failures, "productive": productive,
        "reason": reason, "preflight_ok": preflight_ok,
    }


# --- step 2: replenish at most one interest -----------------------------------

def _replenish(conn, cfg, mission_provider, owner_interests, tracer,
                interest_state_node=None, failures=None):
    """One Council call per tick, at most: the owner interest with the
    fewest PENDING missions below cfg.mission_low_water, tie-broken by
    longest-since-last-generation (never-generated sorts first), then by key
    for a deterministic pick among true ties. This is what makes an empty
    DB self-populate one interest per tick instead of bursting through all
    of them -- Council generation is a reservoir refill, not a per-interest-
    per-minute operation.

    An interest whose most recent generation just FAILED is skipped until
    cfg.mission_retry_seconds has passed (same knob search_missions' own
    fail_mission() cool-off uses) -- otherwise a Council that keeps
    returning malformed output would burn one real provider call every
    single tick for as long as it stays broken.

    Returns how many missions this tick actually planned (0 when there was
    nothing to replenish, or when the Council call failed -- the failure is
    appended to `failures` so the caller can say so out loud rather than
    reporting a row of zeros)."""
    failures = [] if failures is None else failures
    candidates = []
    cooling = 0
    for interest in owner_interests:
        pending = db.pending_mission_count(conn, interest.key)
        if pending >= cfg.mission_low_water:
            continue
        if _generation_in_cooldown(conn, interest.key, cfg.mission_retry_seconds):
            cooling += 1
            continue
        candidates.append((pending, _last_generation_at(conn, interest.key) or "", interest))
    if not candidates:
        if cooling:
            failures.append(
                f"no interest could be replenished: {cooling} below low-water but still in "
                f"cool-off after a failed Council generation (mission_retry_seconds="
                f"{cfg.mission_retry_seconds})"
            )
        return 0
    candidates.sort(key=lambda c: (c[0], c[1], c[2].key))
    _, _, interest = candidates[0]
    planned, error = _generate_for(conn, cfg, mission_provider, interest, tracer, interest_state_node)
    if error:
        failures.append(f"Council generation for '{interest.key}' failed: {error}")
    return planned


def _last_generation_at(conn, interest_key):
    row = conn.execute(
        "SELECT MAX(created_at) AS t FROM search_generations WHERE interest_key = ?",
        (interest_key,),
    ).fetchone()
    return row["t"] if row else None


def _generation_in_cooldown(conn, interest_key, retry_seconds):
    row = conn.execute(
        "SELECT status, created_at FROM search_generations WHERE interest_key = ? ORDER BY id DESC LIMIT 1",
        (interest_key,),
    ).fetchone()
    if row is None or row["status"] != "FAILED":
        return False
    return row["created_at"] > db.ago(retry_seconds)


def _generate_for(conn, cfg, mission_provider, interest, tracer, interest_state_node=None):
    """Plan one interest's missions. Returns (missions_planned, error) --
    (n, None) on success, (0, "why") when the Council call failed, so the
    tick can report the failure instead of silently returning zeros.

    The generation row is inserted before build_context()/plan_missions()
    run, and the try/except wraps both -- any exception either can raise
    (CouncilError, a ProviderError, or something a live provider's own
    parsing didn't anticipate, e.g. a malformed non-dict CDP reply) is
    caught here so one interest's planning failure can never propagate out
    of web_tick() and abort every other interest's execution for the tick,
    and so this generation is always finalized rather than left orphaned at
    PENDING.

    Trace shape: 'interest-state' node -> (edge 'selected') -> one
    'generation' node (entity-linked to search_generations) -> 'council-context'
    node (build_context()'s own output) -> 'council' node, which is what the
    Council's one complete_json() call is attributed to via tracer.calls() --
    so its model_calls row (exact prompt, raw reply) lands there -- ->
    deliberation nodes (see _persist_deliberation) -> per-mission nodes, edge
    'generated', ordinal = mission order."""
    generation_id = db.insert_generation(
        conn, interest.key, mission_provider.name, mission_provider.model,
        cfg.council_missions_per_generation,
    )
    generation_node = tracer.node(
        tracer.run_id, "generation", entity_type="search_generations", entity_id=generation_id,
        label=f"generation:{interest.key}", input_json={"interest_key": interest.key},
    )
    tracer.edge(interest_state_node, generation_node, "selected")
    try:
        context = council.build_context(conn, interest, cfg)
        context_node = tracer.node(
            tracer.run_id, "council-context", entity_type="interests", entity_id=interest.id,
            label=f"context:{interest.key}",
            # The plan calls for build_context()'s own output here, not just
            # its shape -- frontier/feedback/history are already plain
            # dicts (council.build_context() converts each sqlite row via
            # dict(r)), so they serialize as-is; `interest` is dropped since
            # it's a dataclass already entity-linked via entity_id above.
            output_json={
                "frontier": context["frontier"], "feedback": context["feedback"],
                "history": context["history"],
            },
        )
        tracer.edge(generation_node, context_node, "generated")
        council_node = tracer.node(
            tracer.run_id, "council", entity_type="search_generations", entity_id=generation_id,
            label=f"council:{interest.key}",
        )
        tracer.edge(context_node, council_node, "executed")
        with tracer.calls("council", council_node):
            missions, deliberation = council.plan_missions(
                mission_provider, interest, context, cfg.council_missions_per_generation
            )
    except Exception as e:  # noqa: BLE001 -- one interest's planning failure must not crash the tick
        # Printed, not just persisted: the generation row and the trace node
        # are only visible to someone already looking. The tick's log is where
        # an outage gets noticed, so a dead Council has to appear there.
        print(f"Council generation for '{interest.key}' failed: {e}", file=sys.stderr)
        db.finish_generation(conn, generation_id, "FAILED", 0, str(e))
        tracer.finish_node(generation_node, status="error", error=str(e))
        _maybe_enqueue_fallback(conn, cfg, interest, tracer)
        return 0, str(e)
    tracer.finish_node(council_node, status="ok")
    _persist_deliberation(tracer, council_node, deliberation)

    db.insert_missions(conn, generation_id, interest.key, missions)
    db.finish_generation(conn, generation_id, "DONE", len(missions))
    tracer.finish_node(
        generation_node, status="ok", output_json={"missions_returned": len(missions)},
    )
    planned = len(missions)
    mission_rows = {r["label"]: r["id"] for r in conn.execute(
        "SELECT id, label FROM search_missions WHERE generation_id = ?", (generation_id,)
    ).fetchall()}
    for ordinal, m in enumerate(missions):
        mission_id = mission_rows.get(m["label"])
        if mission_id is None:
            continue
        mission_node = tracer.node(
            tracer.run_id, "mission", entity_type="search_missions", entity_id=mission_id,
            label=m["label"], summary=m["rationale"], input_json={"prompt": m["prompt"]},
        )
        tracer.edge(generation_node, mission_node, "generated", ordinal=ordinal)
    return planned, None


def _persist_deliberation(tracer, council_node, deliberation):
    """Turns council.plan_missions()'s lenient deliberation dict (see
    council._extract_deliberation) into trace nodes under the council node:
    one per advisor, one per peer-review entry, one aggregate-ranking node,
    one per rejected angle, and one chairman node carrying the synthesis +
    selection rationale. A section that came back {'unavailable': True, ...}
    (missing/malformed in the response) is persisted exactly as that marker
    -- never invented, never dropped silently either."""
    _persist_list_section(tracer, council_node, deliberation.get("advisors"),
                           "advisor", "name", "advisor")
    _persist_list_section(tracer, council_node, deliberation.get("peer_review"),
                           "peer-review", "reviewer", "reviewer")

    agg_node = tracer.node(
        tracer.run_id, "aggregate-ranking", label="aggregate_ranking",
        output_json=deliberation.get("aggregate_ranking"),
    )
    tracer.edge(council_node, agg_node, "generated")

    rejected = deliberation.get("rejected_angles")
    if isinstance(rejected, list) and rejected:
        for entry in rejected:
            angle = entry.get("angle", "angle") if isinstance(entry, dict) else str(entry)
            node = tracer.node(tracer.run_id, "rejected-angle", label=str(angle), output_json=entry)
            tracer.edge(council_node, node, "rejected")
    else:
        node = tracer.node(tracer.run_id, "rejected-angle", label="rejected_angles", output_json=rejected)
        tracer.edge(council_node, node, "rejected")

    chairman_node = tracer.node(
        tracer.run_id, "chairman", label="chairman_synthesis",
        output_json={
            "chairman_synthesis": deliberation.get("chairman_synthesis"),
            "selection_rationale": deliberation.get("selection_rationale"),
            "disagreements": deliberation.get("disagreements"),
        },
    )
    tracer.edge(council_node, chairman_node, "selected")


def _persist_list_section(tracer, council_node, section, node_type, name_field, default_label):
    """Shared shape for the two per-entry deliberation sections (advisors,
    peer_review): one node per list entry when the section came back as a
    real list, else one node carrying whatever malformed/unavailable value
    was there instead."""
    if isinstance(section, list) and section:
        for entry in section:
            label = entry.get(name_field, default_label) if isinstance(entry, dict) else str(entry)
            node = tracer.node(tracer.run_id, node_type, label=str(label), output_json=entry)
            tracer.edge(council_node, node, "generated")
    else:
        node = tracer.node(tracer.run_id, node_type, label=node_type, output_json=section)
        tracer.edge(council_node, node, "generated")


def _consecutive_failures(conn, interest_key, limit):
    rows = conn.execute(
        "SELECT status FROM search_generations WHERE interest_key = ? ORDER BY id DESC LIMIT ?",
        (interest_key, limit),
    ).fetchall()
    return len(rows) >= limit and all(r["status"] == "FAILED" for r in rows)


def _maybe_enqueue_fallback(conn, cfg, interest, tracer):
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
    row = conn.execute(
        "SELECT id FROM search_missions WHERE interest_key = ? AND label = ? ORDER BY id DESC LIMIT 1",
        (interest.key, FALLBACK_LABEL),
    ).fetchone()
    if row:
        tracer.node(
            tracer.run_id, "mission", entity_type="search_missions", entity_id=row["id"],
            label=FALLBACK_LABEL,
            summary="Council generation failed repeatedly; static fallback query.",
        )


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
                      mission_id, budget, explore_budget, tracer=None):
    """Returns (items_ingested, error): (n, None) when the mission ran --
    n may legitimately be 0, a search that honestly found nothing is still
    work -- and (0, "why") when it failed. The caller counts both, so a tick
    in which every mission errored cannot pass itself off as a healthy run."""
    tracer = tracer or trace.NULL_TRACER
    mission = db.mission_by_id(conn, mission_id)
    if mission is None:   # defensive -- lease_missions() only returns ids it just claimed
        return 0, f"mission {mission_id} vanished between lease and execution"
    mission_node = tracer.node(
        tracer.run_id, "mission-execution", entity_type="search_missions", entity_id=mission_id,
        label=mission["label"], summary=mission["rationale"],
    )
    # Links this execution back to the 'mission' node _generate_for() wrote
    # at planning time, when resolvable -- a mission leased on a LATER tick
    # than the one that generated it (or the static-fallback path, which
    # writes no 'mission' node) has none to find, and the execution simply
    # stands alone.
    planned_node = tracer.find_entity_node("search_missions", mission_id, node_type="mission")
    tracer.edge(planned_node, mission_node, "executed")
    try:
        prompt = RESEARCH_FRAMING.format(
            max_searches=cfg.mission_max_searches,
            mission_prompt=mission["prompt"],
            result_spec=_search.RESULT_SPEC,
        )
        with tracer.calls("mission_search", mission_node):
            raw_items = mission_provider.search_json(prompt, max_searches=cfg.mission_max_searches)
        _trace_tool_events(tracer, mission_node, mission_provider)
        items = _search.to_items(raw_items, "web_search", cfg.mission_max_results)
    except Exception as e:  # noqa: BLE001 -- one mission's failure must not stop the others
        print(f"mission {mission_id} ({mission['label']}) failed: {e}", file=sys.stderr)
        tracer.finish_node(mission_node, status="error", error=str(e))
        db.fail_mission(conn, mission_id, str(e), cfg.mission_max_attempts, cfg.mission_retry_seconds)
        return 0, f"{mission['label']}: {e}"

    # One node per RAW returned result, before dedup/normalization, so a
    # duplicate-rejected or junk (no-url) raw result still has a persistent
    # branch -- to_items() silently drops both.
    raw_nodes = []
    for ordinal, raw in enumerate(raw_items):
        raw_node = tracer.node(
            tracer.run_id, "raw-result", label=_raw_label(raw, ordinal),
            input_json=raw if isinstance(raw, dict) else {"value": raw},
        )
        tracer.edge(mission_node, raw_node, "returned", ordinal=ordinal)
        raw_nodes.append(raw_node)

    # Paired POSITIONALLY with `items`, not by url: to_items() keeps
    # raw_items[:limit] in order, dropping only non-dict/no-url entries, so
    # reapplying that exact filter here (same three conditions, in the same
    # order) gives a 1:1, order-preserving mapping even when two raw results
    # share a url (the duplicate case) -- a url keyed lookup would collapse
    # both onto the first raw-result node, leaving the second (the one
    # actually rejected as a duplicate) with no outcome edge at all. Every
    # raw result NOT kept gets an explicit 'rejected' edge below instead, so
    # nothing is left dangling with only the inbound 'returned' edge.
    source_nodes = []
    for ordinal, (raw, raw_node) in enumerate(zip(raw_items, raw_nodes)):
        if ordinal >= cfg.mission_max_results:
            reason = f"past mission_max_results ({cfg.mission_max_results})"
        elif not isinstance(raw, dict):
            reason = "not a JSON object"
        elif not (raw.get("url") or "").strip():
            reason = "missing url"
        else:
            source_nodes.append(raw_node)
            continue
        # to_items() silently drops this raw result -- give it a terminal
        # node/edge too, so every raw result maps to SOME outcome branch,
        # not just the ones that survive the filter.
        dropped_node = tracer.node(tracer.run_id, "raw-result-dropped", label="dropped", summary=reason)
        tracer.edge(raw_node, dropped_node, "rejected")

    for item, source_node in zip(items, source_nodes):
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
            tracer=tracer, source_node_id=source_node,
        )
        db.bump(conn, {"collected": 1, pipeline.outcome_metric(outcome): 1})

    tracer.finish_node(mission_node, status="ok", output_json={"items_returned": len(items)})
    db.finish_mission(conn, mission_id, len(items))
    return len(items), None


def _raw_label(raw, ordinal):
    if isinstance(raw, dict):
        return str(raw.get("title") or raw.get("url") or f"result {ordinal}")
    return f"result {ordinal}"


def _trace_tool_events(tracer, mission_node, mission_provider):
    """One node per retained search/tool event from the mission's own
    search_json() call (see providers.trace_sink / LLMProvider.last_events),
    or ONE explicit 'not exposed by provider' node when there is nothing to
    show -- never fabricated. `last_events` is a plain best-effort provider
    attribute, not part of the trace-attribution mechanism, so this works
    the same whether tracing is on or off (finish_node/node no-op when off)."""
    events = getattr(mission_provider, "last_events", None) or []
    if not events:
        node = tracer.node(tracer.run_id, "tool-event", label="not exposed by provider")
        tracer.edge(mission_node, node, "executed")
        return
    for ordinal, event in enumerate(events):
        label = event.get("type", "tool-event") if isinstance(event, dict) else "tool-event"
        node = tracer.node(tracer.run_id, "tool-event", label=str(label), output_json=event)
        tracer.edge(mission_node, node, "executed", ordinal=ordinal)
