"""CLI: python -m discovery <command>  (run from the repo root).

`python -m app <command>` is the same CLI under a shorter name.

    init          create/upgrade discovery.db and load interests.json
    sync          reconcile interests.json into the DB at runtime: upsert
                  edits, deactivate entries the file dropped or marked
                  "active": false, and cancel those interests' pending
                  missions. --dry-run prints the plan; --force overrides
                  the truncated-file guard. No re-init, no manual DB op
    run-once      one collect -> score -> notify cycle (--source to limit
                  collection to one collector; Alerts still send immediately,
                  Discovery items still just queue for `digest`). Gated by a
                  provider preflight (health.py) -- a dead Chrome/CDP exits 3
                  before touching a single collector or LLM call. stocks and
                  youtube still run here; web discovery no longer does (see
                  web-tick) -- `--source web_search` still works standalone.
    web-tick      the continuous Council-driven web discovery tick (see
                  discovery/council.py, discovery/missions.py): replenish at
                  most one interest's mission queue, lease+execute a fair
                  slice of pending missions, feed discoveries through the
                  same pipeline. Meant to run every interval_web_seconds
                  (default 60s); gated on the mission provider's own
                  preflight, exit 3 on failure, same as run-once.
    discover      run one collector across all active interests, print
                  candidates and scores, never sends (e.g. `discover web_search`)
    score         push one candidate through the pipeline and print the verdict
    digest        send the pending Discovery digest now (Alerts are unaffected)
    listen        long-poll Telegram for feedback-button presses (blocking);
                  --drain runs one bounded pass instead, for a scheduled task
    items         list recently scored items
    feedback      record feedback on an item, by item id
    stats         funnel, feedback and cost over a trailing window
    interests     layered interest state (discovery/interest_state.py):
                  list (owner rows first), --layer to filter, --why <key>
                  for the provenance chain, --refresh to run
                  promotion/decay (a no-op unless DISCOVERY_DYNAMIC_INTERESTS)
    health        job staleness, provider reachability, pending sends;
                  --notify alerts on degraded/recovery (rate-limited)
    pause         freeze the LLM-spending scheduled commands: run-once,
                  web-tick and digest exit immediately (0 tokens, no
                  provider construction) until `resume`; `listen --drain`
                  and `health` keep running so feedback buttons and the
                  remote resume path stay alive. --why records a note.
    resume        lift `pause`
    personal-state  print the ai repo's personal-state contract artifact
    teach         rank scored-but-unlabeled items by expected information
                  value and record labels; --list/--explain/--send
    ui            serve the Observatory (step-13 task 2): a read-only
                  Datasette UI + JSON API over the trace tables, bound to
                  localhost by default; --public additionally requires
                  DISCOVERY_UI_TOKEN and DISCOVERY_NGROK_CMD (see README)
    offers        AI-generated interest offers (discovery/offers.py): the
                  inbox (--status), one offer's provenance (--why KEY),
                  --import [PATH] a contract-v2 candidates artifact,
                  --sweep the expiry/decay/auto-pause timers, and the
                  decisions --accept/--reject/--snooze KEY, plus --undo KEY
                  for the one-click undo of an auto-pause
    extract-interests  run the `ai` repo's interest extractor (map, then
                  reduce) to refresh the candidates artifact this repo
                  imports. The only command here that drives a browser it
                  does not own; see _extract_interests_cmd

There is no `run`/scheduler loop -- an OS scheduler (see
ops/install_tasks.py) calls the commands above on their own cadence instead;
a session-child tick loop gets reaped the moment the SSH session disconnects.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from datetime import datetime, timezone

from . import config, db, feedback_listener, health, interests, missions, offers, personal_state, providers, stats, teach, trace
from .collectors import COLLECTORS
from .models import CandidateItem
from .notify import FEEDBACK_VERDICTS, print_safe
from .personal_state import PersonalStateError
from .pipeline import Budget, deliver, ingest, outcome_metric, run_once, send_digest
from . import interest_state
from . import interest_sync


def main(argv=None):
    parser = argparse.ArgumentParser(prog="discovery", description=__doc__)
    parser.add_argument("--db", help="override DISCOVERY_DB")
    parser.add_argument(
        "--provider", help="override DISCOVERY_PROVIDER (claude_chat|anthropic|openai)"
    )
    parser.add_argument("--model", help="override DISCOVERY_MODEL")
    parser.add_argument("--dry-run", action="store_true", help="print pushes instead of sending")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the DB and load interests.json")
    sy = sub.add_parser(
        "sync",
        help="reconcile interests.json into the DB now -- upsert, and deactivate what the file dropped",
    )
    sy.add_argument(
        "--force", action="store_true",
        help="apply even when the mass-deactivation guard trips (truncated-file protection)",
    )
    sy.add_argument(
        # default=SUPPRESS so the flag can also be given before the subcommand
        # (`--dry-run sync`), same reason trace-fixture's --db does it.
        "--dry-run", action="store_true", default=argparse.SUPPRESS,
        help="print the plan and write nothing",
    )
    ro = sub.add_parser("run-once", help="one cycle")
    ro.add_argument(
        "--source", choices=sorted(COLLECTORS), help="collect only this collector's items"
    )
    sub.add_parser(
        "web-tick", help="one continuous Council-driven web discovery tick (see PROJECT_STATE.md)"
    )
    sub.add_parser("digest", help="send the pending Discovery digest now")
    disc = sub.add_parser(
        "discover", help="run a collector across all active interests; print, never send"
    )
    disc.add_argument("source", choices=sorted(COLLECTORS), help="collector name")
    one = sub.add_parser("score", help="score a single candidate and print the outcome")
    one.add_argument("--url", help="candidate url")
    one.add_argument("--title", help="candidate title")
    one.add_argument("--text", default="", help="body text the scorer reads")
    one.add_argument("--source", default="manual", help="collector name to record it under")
    one.add_argument("--type", default="article", help="article | video | price_move | ...")
    one.add_argument("--item-id", type=int, help="re-score an item already in the DB")
    one.add_argument("--force", action="store_true", help="ignore dedup and any existing score")
    one.add_argument("--notify", action="store_true", help="also run delivery afterwards")
    listing = sub.add_parser("items", help="list recently scored items")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--min-score", type=float, default=0.0, help="0-1 final score")
    ln = sub.add_parser("listen", help="long-poll Telegram for feedback-button presses (blocking)")
    ln.add_argument(
        "--drain", action="store_true",
        help="one bounded pass instead of blocking (for a scheduled task)",
    )
    fb = sub.add_parser("feedback", help="rate an item")
    fb.add_argument("item_id", type=int)
    fb.add_argument("verdict", choices=sorted(FEEDBACK_VERDICTS))
    fb.add_argument("--interest-id", type=int)
    fb.add_argument("--note", default="")
    st = sub.add_parser("stats", help="funnel, feedback rates and estimated cost")
    st.add_argument("--days", type=int, default=7, help="trailing window (default 7)")
    he = sub.add_parser(
        "health", help="job staleness, provider reachability, pending sends"
    )
    he.add_argument(
        "--notify", action="store_true",
        help="alert on degraded/recovery over Telegram, rate-limited",
    )
    pa = sub.add_parser(
        "pause", help="freeze run-once/web-tick/digest until `resume` (0 LLM spend)"
    )
    pa.add_argument("--why", default="", help="optional note echoed by health and the skip message")
    sub.add_parser("resume", help="lift `pause`")
    it = sub.add_parser(
        "interests", help="layered interest state: list, --why <key>, --refresh"
    )
    it.add_argument("--layer", choices=list(interest_state.LAYERS), help="filter the listing")
    it.add_argument("--why", metavar="KEY", help="print the provenance chain for one interest")
    it.add_argument(
        "--refresh", action="store_true",
        help="run promotion/decay and write changes (no-op unless DISCOVERY_DYNAMIC_INTERESTS)",
    )
    ps = sub.add_parser(
        "personal-state", help="print the ai repo's personal-state contract artifact"
    )
    ps.add_argument("--path", help="override DISCOVERY_PERSONAL_STATE / cfg.personal_state_path")
    te = sub.add_parser(
        "teach", help="rank scored-but-unlabeled items by expected information value"
    )
    te.add_argument("--limit", type=int, help="default 10, or 5 for --send")
    te.add_argument("--interest", help="restrict to one interest key")
    mode = te.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="print the ranked queue and exit")
    mode.add_argument("--explain", action="store_true", help="print queue_metrics and exit")
    mode.add_argument("--send", action="store_true", help="push the top items to Telegram")
    tf = sub.add_parser(
        "trace-fixture",
        help="build the deterministic trace acceptance fixture (offline, fake providers)",
    )
    # default=SUPPRESS: a subparser argument's own default is written into
    # the namespace even when the flag isn't given on the command line,
    # which would silently blow away a --db already parsed before the
    # subcommand (e.g. `python -m app --db PATH trace-fixture`). SUPPRESS
    # means "leave the namespace alone unless this flag is actually passed",
    # so both `--db PATH trace-fixture` and `trace-fixture --db PATH` work.
    tf.add_argument(
        "--db", default=argparse.SUPPRESS,
        help="required -- path to a fresh/fixture-only db, never the production default",
    )
    ui = sub.add_parser(
        "ui", help="serve the Observatory: read-only Datasette UI + JSON API over the trace tables"
    )
    ui.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    ui.add_argument("--port", type=int, default=8001)
    ui.add_argument(
        "--public", action="store_true",
        help="expose via ngrok, token-gated -- requires DISCOVERY_UI_TOKEN and DISCOVERY_NGROK_CMD",
    )
    of = sub.add_parser(
        "offers", help="interest offers: inbox, import, decisions, and the lifecycle sweeps"
    )
    of.add_argument("--status", help="filter the listing (default: the 'offered' inbox)")
    of.add_argument("--why", metavar="KEY", help="print one offer's evidence, scores and event chain")
    of.add_argument(
        "--import", dest="import_path", nargs="?", const="", metavar="PATH",
        help="import a contract-v2 candidates artifact (default: cfg.interest_candidates_path)",
    )
    of.add_argument("--sweep", action="store_true", help="run the expiry/decay/auto-pause timers")
    of.add_argument("--accept", metavar="KEY", help="accept an offer (prints the interests.json entry)")
    of.add_argument("--reject", metavar="KEY", help="reject an offer and block its terms")
    of.add_argument("--snooze", metavar="KEY", help="snooze an offer back into the inbox later")
    of.add_argument("--undo", metavar="KEY", help="undo an interest's auto-pause")
    of.add_argument("--note", default="", help="note recorded with a decision")

    ex = sub.add_parser(
        "extract-interests",
        help="refresh the candidates artifact: the ai repo's extractor, map then reduce",
    )
    ex.add_argument(
        "--skip-map", action="store_true",
        help="reduce only -- re-propose from the digests already collected",
    )
    ex.add_argument(
        "--map-limit", type=int, default=None,
        help="cap `map` at N conversations (smoke-testing the wiring; default: all pending)",
    )

    args = parser.parse_args(argv)
    if args.command == "trace-fixture" and not args.db:
        # trace_fixture.build() inserts fixture interests/items/scores/a real
        # feedback row through the real production code paths -- refusing to
        # fall back to cfg.db_path's default (REPO_ROOT/discovery.db) is what
        # stops an unflagged invocation from writing fixture rows into the
        # real database.
        print("trace-fixture requires --db PATH -- refusing to default to the production db", file=sys.stderr)
        return 2
    # Task Scheduler redirects stdout to a file (ops/run.cmd), and a redirected
    # stdout is block-buffered: a job killed by its ExecutionTimeLimit loses
    # everything it printed. That is why logs/web-tick-*.log simply stopped
    # having lines while the tick was failing every single minute. Line
    # buffering costs nothing here and means whatever a job managed to say
    # survives being killed.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # a replaced/duck-typed stream in tests
        pass

    cfg = config.load()
    if args.db:
        cfg.db_path = args.db
    if args.provider:
        cfg.provider = args.provider
        cfg.model = args.model or config.DEFAULT_MODELS.get(args.provider, cfg.model)
    if args.model:
        cfg.model = args.model

    conn = db.connect(cfg.db_path)
    db.init(conn)

    # Built once, and only for the commands that talk to a model -- constructing
    # a provider needs an API key, and `init`/`items`/`feedback`/`stats` don't
    # need one. The single instance is also what accumulates token usage, which
    # is why it is cached rather than rebuilt per call.
    built = {}

    def provider():
        if "provider" not in built:
            built["provider"] = providers.get_provider(cfg)
        return built["provider"]

    try:
        return _dispatch(conn, cfg, args, provider)
    except providers.ProviderError as e:
        # e.g. an unknown DISCOVERY_PROVIDER -- a config mistake, not a crash.
        print(f"provider error: {e}", file=sys.stderr)
        return 2
    finally:
        # Whatever the command spent, on the way out -- `run` flushes per cycle
        # from run_once() instead, since it never gets here.
        if "provider" in built:
            db.record_usage(conn, built["provider"])
        conn.close()   # Windows keeps the .db file locked otherwise


# Commands that spend LLM/API calls when the OS scheduler fires them. `pause`
# freezes exactly these; `listen --drain` (one free Telegram getUpdates -- it
# keeps feedback buttons responsive while paused) and `health` (free local
# checks, pause-aware in health.check) deliberately keep running.
# `extract-interests` belongs here for the same reason the other three do: it
# drives a real claude.ai tab for minutes at a time. `offers --import` and
# `offers --sweep` deliberately do NOT -- both are offline file/SQLite work,
# and freezing the sweep would freeze the 30/45-day lifecycle clocks, which is
# precisely the failure this repo just spent a scheduling pass fixing.
PAUSE_GATED = ("run-once", "web-tick", "digest", "extract-interests")


def _dispatch(conn, cfg, args, provider):
    # Checked before any provider construction or _run_job heartbeat: a gated
    # invocation is a deliberate no-op, not a run -- run_ok/last_ok would lie.
    if args.command in PAUSE_GATED and db.state_get(conn, "paused") == "1":
        why = db.state_get(conn, "paused_why") or ""
        note = f" ({why})" if why else ""
        print(f"paused{note} -- {args.command} skipped; `python -m app resume` lifts it")
        return 0
    if args.command == "init":
        result = _sync_interests(conn, cfg)
        if result is None:
            return 2
        print(f"{cfg.db_path}: schema ready, {result.describe()}")
    elif args.command == "sync":
        return _sync_cmd(conn, cfg, args)
    elif args.command == "run-once":
        sources = [args.source] if args.source else None
        job_name = health.job_name_for_source(args.source)
        return _run_job(
            conn, job_name, lambda: _run_once_cmd(conn, provider(), cfg, sources, args.dry_run, job_name)
        )
    elif args.command == "web-tick":
        return _run_job(conn, "web", lambda: _web_tick_cmd(conn, provider(), cfg, args.dry_run))
    elif args.command == "discover":
        return _discover(conn, provider(), cfg, args)
    elif args.command == "score":
        return _score_one(conn, provider(), cfg, args)
    elif args.command == "digest":
        return _run_job(conn, "digest", lambda: _digest_cmd(conn, cfg, args.dry_run))
    elif args.command == "listen":
        if args.drain:
            return _run_job(conn, "feedback", lambda: _drain_cmd(conn, cfg))
        feedback_listener.listen(conn, cfg)
    elif args.command == "items":
        _list_items(conn, args.limit, args.min_score)
    elif args.command == "feedback":
        if db.get_item(conn, args.item_id) is None:
            print(f"no item with id {args.item_id}", file=sys.stderr)
            return 2
        row = conn.execute(
            "SELECT final_score FROM scores WHERE item_id = ?", (args.item_id,)
        ).fetchone()
        original_score = row["final_score"] if row else None
        db.add_feedback(conn, args.item_id, args.interest_id, args.verdict, args.note, original_score)
        print(f"recorded {args.verdict} on item {args.item_id}")
    elif args.command == "stats":
        print_safe(stats.report(conn, args.days, cfg))
    elif args.command == "health":
        return _health_cmd(conn, cfg, provider(), args)
    elif args.command == "pause":
        db.state_set(conn, "paused", "1")
        db.state_set(conn, "paused_why", args.why)
        print(
            "paused -- run-once/web-tick/digest exit immediately until `resume`"
            " (feedback drain and health keep running)"
        )
    elif args.command == "resume":
        db.state_set(conn, "paused", "0")
        db.state_set(conn, "paused_why", "")
        print("resumed")
    elif args.command == "interests":
        return _interests_cmd(conn, cfg, args)
    elif args.command == "personal-state":
        return _personal_state(cfg, args)
    elif args.command == "teach":
        return _teach_cmd(conn, cfg, args)
    elif args.command == "trace-fixture":
        from . import trace_fixture

        print(trace_fixture.build(conn, cfg))
    elif args.command == "ui":
        return _ui_cmd(cfg, args)
    elif args.command == "offers":
        return _offers_cmd(conn, cfg, args)
    elif args.command == "extract-interests":
        return _run_job(
            conn, "interest-extract", lambda: _extract_interests_cmd(conn, cfg, args)
        )
    return 0


def _run_job(conn, job_name, fn):
    """Wraps a job command with a `job:<name>:last_ok`/`last_fail` heartbeat
    and the matching run_ok/run_failed ops counter -- the failure path fires
    whether the job returns a non-zero exit code (e.g. run-once's preflight
    bailout) or raises outright, so a crash is never invisible to `health`."""
    try:
        code = fn() or 0
    except Exception:
        db.bump(conn, {"run_failed": 1})
        db.state_set(conn, f"job:{job_name}:last_fail", db.now())
        raise
    if code == 0:
        db.bump(conn, {"run_ok": 1})
        db.state_set(conn, f"job:{job_name}:last_ok", db.now())
    else:
        db.bump(conn, {"run_failed": 1})
        db.state_set(conn, f"job:{job_name}:last_fail", db.now())
    return code


def _run_once_cmd(conn, provider, cfg, sources, dry_run, job_name):
    if not health.preflight_gate(conn, provider, cfg, job_name):
        return 3
    print(run_once(conn, provider, cfg, sources=sources, dry_run=dry_run))
    return 0


# _run_job turns this into job:web:last_fail instead of last_ok. Distinct from
# 3 (preflight refused to start) so the two are told apart in a log: 3 means
# the tick never began, 4 means it began and got nothing done.
WEB_TICK_UNPRODUCTIVE = 4


def _web_tick_cmd(conn, provider, cfg, dry_run):
    """`provider` here is the scoring provider (built from cfg.provider by
    the CLI's shared provider() closure, same as run-once) -- web_tick()
    builds its own search-capable mission provider internally from
    cfg.mission_provider and gates on that provider's own preflight, so
    there is no separate health.preflight_gate() call here.

    Exit code is the tick's honesty mechanism. A tick that planned nothing,
    ran nothing and delivered nothing returns WEB_TICK_UNPRODUCTIVE when
    something went wrong, so _run_job records job:web:last_fail and `health`
    shows web as failing -- rather than 0, which stamps last_ok and tells
    every monitor the web collector is alive. Silent success here is what let
    the owner's web discovery die on 2026-08-13 and stay dead, invisibly,
    for five days: the log had no line, the heartbeat kept advancing, and the
    scheduler kept reporting LastTaskResult=0."""
    # Printed BEFORE the work, and line-buffered (see main()), so the log
    # proves the run started even if something kills it mid-tick. A log whose
    # last line is a `start` with no matching summary is a diagnosis; a log
    # with no line at all -- which is what five days of this outage looked
    # like -- is not.
    print_safe(
        f"web-tick: start budget={cfg.web_tick_budget_seconds}s "
        f"provider={cfg.mission_provider}/{cfg.mission_model}"
    )
    result = missions.web_tick(conn, cfg, provider=provider, dry_run=dry_run)
    if not result["preflight_ok"]:
        return 3
    print_safe(_web_tick_summary(result))
    for failure in result["failures"]:
        print_safe(f"  ! {failure}")
    if result["productive"]:
        return 0
    print_safe(f"web-tick did nothing: {result['reason']}")
    return WEB_TICK_UNPRODUCTIVE if result["failures"] else 0


def _web_tick_summary(result):
    return (
        "web-tick: planned={generated} leased={leased} executed={executed}"
        " ok={executed_ok} collected={collected} notified={notified}"
        " abandoned={abandoned}".format(**result)
    )


def _digest_cmd(conn, cfg, dry_run):
    """Same run-level trace_runs wrapping run-once/web-tick get (kind='digest',
    a no-op end to end when cfg.trace_enabled is off) -- this is the only
    production path that ever sends a DISCOVERY item, so without it the
    render/notification trace nodes real feedback would need to link back to
    (see feedback_listener._handle_callback) never existed outside the
    fixture's own hand-rolled run."""
    tracer = trace.Tracer(conn, cfg)
    run_id = tracer.begin_run("digest", provider=cfg.provider, model=cfg.model)
    try:
        sent = send_digest(conn, cfg, dry_run=dry_run, tracer=tracer)
    except Exception as e:  # noqa: BLE001 -- re-raised as-is right after
        tracer.finish_run(run_id, status="error", error=str(e))
        raise
    tracer.finish_run(run_id, status="done")
    print(f"sent {sent} digest item(s)")
    return 0


def _drain_cmd(conn, cfg):
    count = feedback_listener.drain(conn, cfg)
    if count is None:
        # A transport failure -- drain() already printed/counted it. Report
        # it as a job failure too so `job:feedback:last_ok` isn't stamped
        # (and run_ok isn't bumped) for an invocation that never actually
        # reached Telegram.
        print("feedback drain failed", file=sys.stderr)
        return 1
    print(f"drained {count} feedback update(s)")
    return 0


def _health_cmd(conn, cfg, provider, args):
    result = health.check(conn, cfg, provider)
    print_safe(health.format_report(result))
    if args.notify:
        health.notify_if_needed(conn, cfg, result)
    return 1 if result["degraded"] else 0


def _discover(conn, provider, cfg, args):
    """Run one collector for every active interest through the real pipeline
    (dedup, prefilter, scoring) and print what it found -- deliberately
    stopping short of `deliver()` so nothing is ever sent from here."""
    collect = COLLECTORS[args.source]
    active = db.active_interests(conn)
    if not active:
        print("no active interests -- run `init` first", file=sys.stderr)
        return 2

    total = 0
    counts = Counter()
    budget = Budget(cfg.max_scores_per_cycle)
    explore_budget = Budget(cfg.explore_max_scores_per_cycle if cfg.dynamic_interests else 0)
    for interest in active:
        try:
            candidates = collect(interest, cfg, provider, conn)
        except Exception as e:  # noqa: BLE001
            print(f"{interest.key}/{args.source}: collect failed: {e}", file=sys.stderr)
            continue
        for item in candidates:
            total += 1
            counts["collected"] += 1
            outcome = ingest(
                conn, provider, cfg, item, active,
                origin_interest=interest.key, budget=budget, explore_budget=explore_budget,
            )
            counts[outcome.stage] += 1
            # Flushed per item rather than once at the end (see pipeline.py's
            # run_once() for the same fix and its rationale): _print_discovered
            # below is exactly what killed a real run mid-loop once already
            # (a narrow console codepage choking on a model-generated
            # character) and silently lost every already-scored item's funnel
            # counts with it, even though their DB rows were already committed.
            db.bump(conn, {"collected": 1, outcome_metric(outcome): 1})
            _print_discovered(interest, item, outcome)
    print(f"\n{total} candidate(s) from '{args.source}'", file=sys.stderr)
    return 0


def _print_discovered(interest, item, outcome):
    query = item.metadata.get("query") if item.metadata else None
    header = f"[{interest.key}] {outcome.stage}"
    if query:
        header += f"  (query: {query!r})"
    print_safe(header)
    print_safe(f"      {item.title}")
    print_safe(f"      {item.url}")
    if outcome.score is not None:
        print_safe(f"      score={outcome.score.final_score:.2f}  {outcome.score.reason}")


def _score_one(conn, provider, cfg, args):
    """One candidate through the exact production path, verdict as JSON.

    Deliberately `pipeline.ingest` and not a shortcut to the scorer: this is
    how you check what the engine would really do with a given item, dedup and
    pre-filter included.
    """
    item = _candidate(conn, args)
    if item is None:
        return 2

    active = db.active_interests(conn)
    if not active:
        print("no active interests -- run `init` first", file=sys.stderr)
        return 2

    outcome = ingest(conn, provider, cfg, item, active, force=args.force)
    print(json.dumps(outcome.as_dict(), indent=2))
    if args.notify:
        sent = deliver(conn, cfg, dry_run=args.dry_run)
        print(f"delivered: {sent}", file=sys.stderr)
    return 0


def _candidate(conn, args):
    if args.item_id is not None:
        item = db.get_item(conn, args.item_id)
        if item is None:
            print(f"no item with id {args.item_id}", file=sys.stderr)
        return item
    if not args.url or not args.title:
        print("score needs --item-id, or both --url and --title", file=sys.stderr)
        return None
    return CandidateItem(
        source=args.source,
        type=args.type,
        title=args.title,
        url=args.url,
        text=args.text,
    )


def _list_items(conn, limit, min_score):
    rows = conn.execute(
        """
        SELECT s.final_score, s.confidence, s.reason, i.id, i.title, i.url,
               n.key AS interest
        FROM scores s
        JOIN candidate_items i ON i.id = s.item_id
        JOIN interests n ON n.id = s.interest_id
        WHERE s.final_score >= ?
        ORDER BY s.id DESC LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()
    for row in rows:
        # Shown 0-100 for the same reason notify.format_message does it.
        print_safe(f"[{row['final_score'] * 100:>3.0f}] #{row['id']} {row['interest']}: {row['title']}")
        print_safe(f"      {row['reason']}")
        print_safe(f"      {row['url']}")


def _sync_interests(conn, cfg, force=False):
    """Shared by `init` and `sync`: run sync v2, or print why it could not and
    return None so the caller exits 2. A malformed/absent file must never fall
    through to a deactivation pass."""
    try:
        return interest_sync.sync(conn, cfg.interests_path, force=force)
    except FileNotFoundError:
        print(f"interests file not found: {cfg.interests_path}", file=sys.stderr)
    except interest_sync.SyncRefused as e:
        print(f"sync refused: {e}", file=sys.stderr)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print(f"malformed interests file {cfg.interests_path}: {e}", file=sys.stderr)
    return None


def _sync_cmd(conn, cfg, args):
    """`python -m app sync` -- the runtime half of sync v2. Editing
    interests.json and running this takes effect on the next pipeline cycle
    (db.active_interests() reads the DB live); no re-init, no hand-written
    UPDATE, no redeploy."""
    if args.dry_run:
        try:
            planned = interest_sync.plan(conn, cfg.interests_path)
        except FileNotFoundError:
            print(f"interests file not found: {cfg.interests_path}", file=sys.stderr)
            return 2
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"malformed interests file {cfg.interests_path}: {e}", file=sys.stderr)
            return 2
        print(f"dry run -- {planned.changes} changes, nothing written")
        print(planned.describe())
        return 0
    result = _sync_interests(conn, cfg, force=args.force)
    if result is None:
        return 2
    print(result.describe())
    return 0


def _interests_cmd(conn, cfg, args):
    if args.why:
        return _interests_why(conn, args.why)
    if args.refresh:
        return _interests_refresh(conn, cfg)
    return _interests_list(conn, args.layer)


def _interests_list(conn, layer):
    rows = db.list_interests(conn, layer)
    if not rows:
        print_safe("no interests" + (f" at layer '{layer}'" if layer else ""))
        return 0
    for row in rows:
        print_safe(
            f"{row['key']}  layer={row['layer']}  active={row['active']}  "
            f"min_score={row['min_score']:.2f}  last_observed_at={row['last_observed_at'] or '-'}"
        )
    return 0


def _interests_why(conn, key):
    events = db.interest_events(conn, key)
    if not events:
        row = conn.execute("SELECT 1 FROM interests WHERE key = ?", (key,)).fetchone()
        if row is None:
            print(f"no interest with key {key!r}", file=sys.stderr)
            return 2
        print_safe(f"{key}: no events recorded")
        return 0
    print_safe(f"{key}:")
    for e in events:
        print_safe(
            f"  {e['at']}  {e['actor']}  {e['action']}  "
            f"{e['from_layer'] or '-'} -> {e['to_layer'] or '-'}  {json.dumps(e['evidence'])}"
        )
    return 0


def _interests_refresh(conn, cfg):
    if not cfg.dynamic_interests:
        print_safe("dynamic interests are off (DISCOVERY_DYNAMIC_INTERESTS)")
        return 0
    # apply_transitions() reads cfg.interests_path (blocked_derived_terms) via
    # a bare open()/json.loads -- same failure modes as `init`'s interests.sync,
    # so isolate them the same way rather than letting them traceback.
    try:
        summary = interest_state.apply_transitions(conn, cfg)
    except FileNotFoundError:
        print(f"interests file not found: {cfg.interests_path}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print(f"malformed interests file {cfg.interests_path}: {e}", file=sys.stderr)
        return 2
    print_safe(f"refresh: {json.dumps(summary)}")
    return 0


def _personal_state(cfg, args):
    """Human-checkable probe: load the ai repo's contract artifact and print
    what internet would see, without touching the pipeline."""
    path = args.path or cfg.personal_state_path
    try:
        state = personal_state.load(path)
    except PersonalStateError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        generated = datetime.fromisoformat(state.generated_at.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - generated).days
        age = f"{age_days}d old"
    except (AttributeError, TypeError, ValueError):
        age = "age unknown"

    print_safe(f"{path}: contract_version={state.contract_version}")
    print_safe(f"generated_at={state.generated_at} ({age})")
    print_safe(f"{len(state.topics)} topic(s)")
    for topic in state.topics[:10]:
        print_safe(f"  {topic.get('key')!r}  weight={topic.get('weight')}")
    return 0


def _teach_cmd(conn, cfg, args):
    """No provider() call anywhere on this path -- `teach` stays in the
    no-provider command family alongside `items`/`feedback`/`stats`."""
    limit = args.limit if args.limit is not None else (5 if args.send else 10)
    if args.list:
        print_safe(teach.format_queue(teach.build_queue(conn, limit, args.interest)))
        return 0
    if args.explain:
        print_safe(teach.format_metrics(teach.queue_metrics(conn, limit, args.interest)))
        return 0
    if args.send:
        teach.run_send(conn, cfg, limit, args.interest, dry_run=args.dry_run)
        return 0
    # `input` looked up here, not as teach.run_interactive's bound default --
    # a default value is captured once at import time, so patching
    # builtins.input in a test would never reach it.
    return teach.run_interactive(conn, limit, args.interest, read=input)


def _ui_cmd(cfg, args):
    """Serves the Observatory (step-13 task 2). `datasette`/`observatory/`
    are imported lazily, here and only here (plus inside observatory/ itself)
    -- every other discovery/ module, and test_discovery.py, must stay
    importable on a machine without datasette installed (see
    PROJECT_STATE.md)."""
    if args.public:
        if not cfg.ui_token:
            print(
                "ui --public requires DISCOVERY_UI_TOKEN -- refusing to expose the db without a token",
                file=sys.stderr,
            )
            return 2
        if not cfg.ngrok_cmd:
            print(
                "ui --public requires DISCOVERY_NGROK_CMD (a working ngrok binary/config)",
                file=sys.stderr,
            )
            return 2

    try:
        from observatory.app import build_datasette
    except ImportError as e:
        print(f"ui: datasette is not installed ({e}) -- see requirements.txt", file=sys.stderr)
        return 2

    ds = build_datasette(cfg, public=args.public)

    if args.public:
        if not _launch_ngrok(cfg, args.port):
            return 3
        print(
            "ui --public: ngrok launch requested -- live tunnel/token verification is an "
            "operator-session step, not exercised by this process or its tests",
            file=sys.stderr,
        )

    import uvicorn

    print(f"Observatory: http://{args.host}:{args.port}/observatory/"
          + (" (public -- token required)" if args.public else " (localhost only)"))
    # `--public` accepts the token as `?token=` (the only form a plain
    # Telegram URL button can carry -- it can't set a header) -- uvicorn's
    # access log records the full path+query, which would otherwise persist
    # the token to disk on every request, violating "never persist ...
    # tokens anywhere". access_log stays on in private mode (nothing
    # sensitive to leak there) and only turns off once a token exists.
    uvicorn.run(
        ds.app(), host=args.host, port=args.port, log_level="info",
        access_log=not args.public, lifespan="on", workers=1,
    )
    return 0


def _launch_ngrok(cfg, port):
    """`cmd /d /c` -- see health.py's own chrome_launch_cmd for why /d
    matters on this machine (an AutoRun hook breaks shell=True's cwd
    handling). `{port}` in DISCOVERY_NGROK_CMD, if present, is substituted;
    a command with no such placeholder is run as-is (e.g. one that already
    reads the port from its own ngrok.yml). Detached (`start ""...`) is the
    caller's responsibility, same convention as DISCOVERY_CHROME_LAUNCH_CMD --
    this never runs live in this worktree (no ngrok binary/network here);
    see PROJECT_STATE.md."""
    cmd_str = cfg.ngrok_cmd.replace("{port}", str(port))
    try:
        subprocess.Popen(["cmd", "/d", "/c", cmd_str], close_fds=True)
    except OSError as e:
        print(f"ui --public: failed to launch ngrok ({cmd_str!r}): {e}", file=sys.stderr)
        return False
    return True


# `import_artifact` is fail-soft by design -- it returns a summary carrying an
# `error` string instead of raising, because it runs inside a scheduled tick.
# That is right for the function and wrong for the exit code: a scheduled task
# whose artifact has been unreadable for a week must not keep reporting
# LastTaskResult=0 and stamping job:offers-import:last_ok. These two reasons
# are the ones that mean "the artifact was read fine, there was simply nothing
# new in it" -- the steady state of an hourly, idempotent job. Every OTHER
# reason means the artifact could not be read at all, and is a failure.
_IMPORT_BENIGN_ERRORS = ("already imported", "no candidates in contract_version")


def _import_is_failure(summary):
    error = summary.get("error") or ""
    if not error:
        return False
    return not error.startswith(_IMPORT_BENIGN_ERRORS)


def _offers_cmd(conn, cfg, args):
    """One subcommand over discovery/offers.py. Every branch is offline:
    importing reads a local artifact, the sweeps read the funnel, and the
    decisions write only the offer/interest tables -- no provider is ever
    built, no model is ever called.

    `--import` and `--sweep` are the two branches a Scheduled Task fires
    unattended, so they alone are wrapped in `_run_job`: they get the same
    `job:<name>:last_ok/last_fail` heartbeat and run_ok/run_failed counters
    every other scheduled command has, which is what puts them in front of
    the owner through `health --notify` (health.JOB_INTERVALS) instead of
    only in a log nobody opens. The interactive branches -- the inbox
    listing, `--why`, and the accept/reject/snooze decisions -- are the owner
    typing, not a job, and stay unwrapped so a typo at the keyboard never
    shows up as a failed scheduled run.
    """
    if args.why:
        return _offer_why(conn, args.why)
    if args.import_path is not None:
        path = args.import_path or cfg.interest_candidates_path
        return _run_job(conn, "offers-import", lambda: _offers_import_cmd(conn, cfg, path))
    if args.sweep:
        return _run_job(conn, "offers-sweep", lambda: _offers_sweep_cmd(conn))
    for key, action in (
        (args.accept, "accept"), (args.reject, "reject"),
        (args.snooze, "snooze"), (args.undo, "undo"),
    ):
        if key:
            return _offer_decide(conn, cfg, key, action, args.note)
    return _offers_list(conn, args.status)


def _offers_import_cmd(conn, cfg, path):
    """Import the candidates artifact, and say out loud what it did -- or why
    it did nothing. The summary is printed on EVERY path, including the ones
    that import zero offers: "already imported" and "no candidates" are the
    normal outcomes of an hourly idempotent job, and a log line saying so is
    the difference between a job that is working and a job that has been dead
    for a week. From the outside those two look identical otherwise."""
    print_safe(f"offers --import: reading {path}")
    summary = offers.import_artifact(conn, path, interests_path=cfg.interests_path)
    print_safe(json.dumps(summary, ensure_ascii=False, indent=2))
    if _import_is_failure(summary):
        # Non-zero, so _run_job stamps job:offers-import:last_fail and
        # `health` starts counting this job as failing.
        print_safe(f"offers --import: FAILED -- {summary['error']}")
        return 2
    if summary.get("error"):
        print_safe(f"offers --import: nothing to do -- {summary['error']}")
    else:
        print_safe(
            f"offers --import: {summary['offered']} offered from "
            f"{summary['imported']} candidates (artifact {summary['artifact_sha256'][:12]})"
        )
    return 0


def _offers_sweep_cmd(conn):
    """Every timer-driven transition in the offer/interest lifecycle: expiry,
    snooze wake-up, decay, auto-pause. `offers.sweep` either performs those
    transitions or reports, in `skipped`, that it deliberately refused to
    judge interest silence the pipeline itself caused. That refusal is a
    correct outcome, not an error, so it exits 0 -- but it is printed,
    because a sweep that has been skipping for weeks is something the owner
    should be able to read in a log rather than infer from interests that
    never decay."""
    summary = offers.sweep(conn)
    for announcement in summary["announcements"]:
        print_safe(announcement["text"])
    print_safe(json.dumps(
        {k: v for k, v in summary.items() if k != "announcements"}, ensure_ascii=False
    ))
    moved = sum(
        summary.get(key, 0) for key in
        ("expired", "woken", "decaying", "recovered", "auto_paused", "retired", "retire_offers")
    )
    if summary.get("skipped"):
        print_safe(f"offers --sweep: interest timers not evaluated -- {summary['skipped']}")
    elif not moved:
        print_safe("offers --sweep: nothing was due (no expiry, wake, decay or auto-pause)")
    else:
        print_safe(f"offers --sweep: {moved} lifecycle transition(s)")
    return 0


# `map` made no progress on work it had. Distinct from 2 (could not run at
# all) so a log tells the two apart, and deliberately mirrors _web_tick_cmd's
# WEB_TICK_UNPRODUCTIVE: a job that ran and achieved nothing must not stamp
# last_ok, because a heartbeat that keeps advancing while nothing happens is
# exactly how this appliance went blind for five days.
EXTRACT_UNPRODUCTIVE = 4


def _say(message):
    """print_safe, then flush. Task Scheduler redirects this job's stdout to a
    file (ops/run.cmd) and a redirected stdout is block-buffered, so a job
    killed by its ExecutionTimeLimit loses everything it printed. This is the
    task most exposed to that -- it is the one with a two-hour limit that
    spends most of it inside a child process -- and a log that goes silent
    tells nobody anything. Flushing per line costs nothing at this volume."""
    print_safe(message)
    try:
        sys.stdout.flush()
    except (AttributeError, ValueError):   # a replaced stream in tests
        pass


def _extractor_paths(cfg):
    """Locate the `ai` repo's extractor from the hop that is already wired.

    `cfg.interest_candidates_path` (DISCOVERY_INTEREST_CANDIDATES) is the
    artifact the extractor WRITES and this repo READS -- so its directory is
    the producer's repo root, and nothing new has to be configured to find the
    producer itself. Returns ((script, repo_root), "") or (None, reason)."""
    raw = cfg.interest_candidates_path or ""
    if not raw:
        return None, "cfg.interest_candidates_path is empty"
    repo_root = Path(raw).parent
    script = repo_root / "interest_extractor.py"
    if not script.is_file():
        return None, (
            f"no interest_extractor.py beside the candidates artifact ({script}) "
            f"-- DISCOVERY_INTEREST_CANDIDATES must point into the producer repo"
        )
    return (script, repo_root), ""


def _extractor_env():
    """The child's environment. PYTHONIOENCODING because the extractor prints
    the owner's Hebrew conversation titles and this console is cp1255."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _extractor_status(python, script, repo_root, timeout=180):
    """The extractor's own offline `status` subcommand, as a dict. Used as a
    before/after measurement so this command can tell "there was nothing to
    do" apart from "it did nothing" -- which is the whole point. Returns {}
    if it cannot be read: a status that fails is worth a log line, never
    worth failing the run over."""
    try:
        proc = subprocess.run(
            [python, str(script), "status"], cwd=str(repo_root),
            capture_output=True, text=True, errors="replace", timeout=timeout,
            env=_extractor_env(), stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _say(f"extract-interests: status unavailable ({e})")
        return {}
    if proc.returncode != 0:
        _say(
            f"extract-interests: status exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:400]}"
        )
        return {}
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        _say("extract-interests: status output was not JSON")
        return {}


def _drain_into(stream, queue_):
    """Reader thread body: every line the child prints goes onto the queue.
    A thread, rather than iterating the pipe on the main thread, is what makes
    the deadline enforceable -- a child that hangs without printing would
    block a direct `for line in proc.stdout` forever, and the budget that is
    supposed to stop it would never be looked at again."""
    try:
        for line in stream:
            queue_.put(line)
    except (OSError, ValueError):
        pass
    finally:
        queue_.put(None)   # sentinel: the child closed its end


def _run_extractor_stage(python, script, repo_root, stage_args, timeout):
    """Run one extractor stage, streaming its output into this job's log as it
    arrives. Returns (returncode, timed_out); a timeout reports (None, True).

    Streamed line by line, not collected and echoed at the end. `map` over a
    full backlog runs for tens of minutes and prints one line per batch, and a
    stage that says nothing until it finishes is invisible for exactly as long
    as it is interesting -- if the task is killed at minute 29 of 30, a
    buffered echo has nothing to hand over. Each line is prefixed with its
    stage and flushed, so the log always shows how far the run actually got.

    The child gets a PIPE rather than inheriting this process's stdout. Under
    Task Scheduler that stdout IS the job's log file, opened by ops/run.cmd's
    `>>` without FILE_SHARE_WRITE, and a child holding that handle is what kept
    logs/web-tick-*.log locked for days (see health.preflight_gate). stdin is
    closed for the same family of reasons: nothing here should be able to block
    on a read from a console that is not there.

    The budget is enforced against wall clock on the main thread, so it stops a
    stage that has gone quiet as surely as one that is chattering -- a hang is
    the failure most worth having a deadline for, and it is the one a
    read-driven loop cannot see.
    """
    label = stage_args[0]
    _say(f"extract-interests: {label} start (budget {timeout}s) -> {script}")
    started = time.monotonic()
    deadline = started + timeout
    try:
        proc = subprocess.Popen(
            [python, str(script), *stage_args], cwd=str(repo_root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, errors="replace",
            bufsize=1, env=_extractor_env(),
        )
    except OSError as e:
        _say(f"extract-interests: {label} could not start: {e}")
        return 2, False

    lines = queue.Queue()
    reader = threading.Thread(target=_drain_into, args=(proc.stdout, lines), daemon=True)
    reader.start()

    timed_out = False
    while True:
        if time.monotonic() > deadline:
            timed_out = True
            break
        try:
            line = lines.get(timeout=1.0)
        except queue.Empty:
            # No output for a second is normal -- a batch takes ~30s. The only
            # thing that matters here is that the loop comes back round and
            # re-checks the clock.
            if proc.poll() is not None and not reader.is_alive():
                break
            continue
        if line is None:
            break
        line = line.rstrip()
        if line:
            _say(f"  [{label}] {line}")

    if timed_out:
        _kill_stage(proc)
    else:
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 1))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_stage(proc)
    try:
        if proc.stdout:
            proc.stdout.close()
    except OSError:
        pass

    elapsed = (time.monotonic() - started) / 60
    if timed_out:
        _say(
            f"extract-interests: {label} hit its {timeout}s budget and was stopped "
            f"after {elapsed:.1f}m. `map` is checkpointed per batch, so finished "
            f"work is kept and the next run resumes."
        )
        return None, True
    _say(f"extract-interests: {label} exited {proc.returncode} in {elapsed:.1f}m")
    return proc.returncode, False


def _kill_stage(proc):
    """Stop a stage that overran, and do not hang doing it -- terminate, then
    kill if it will not go. A wrapper that blocks forever waiting on the child
    it just gave up on is the same outage in a different costume."""
    for stop in (proc.terminate, proc.kill):
        try:
            stop()
            proc.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return


def _extract_interests_cmd(conn, cfg, args):
    """Run the `ai` repo's interest extractor -- `map`, then `reduce` -- so the
    candidates artifact this repo imports is refreshed by a machine instead of
    by the owner remembering to type two commands in another repo.

    Why this lives in `internet` and not in `ai`: the scheduling convention is
    here. `ops/install_tasks.py`, `ops/hidden.vbs`, `ops/run.cmd` and the
    `internet-discovery-*` family are one mechanism with one installer, one
    log-naming rule and one uninstall whitelist. Standing up a second
    scheduler in `ai` would mean a second copy of all of that, plus an owner
    who has to remember which of two places a job is registered in. Running it
    as a `python -m app` subcommand instead means it needs no new launcher at
    all -- ops/run.cmd already handles it -- and it inherits `_run_job`'s
    heartbeat, so `health` (and therefore Telegram) can see it. `ai` keeps
    owning the extractor; this owns only when it runs.

    Exit codes: 0 worked, 2 could not run at all, EXTRACT_UNPRODUCTIVE (4) ran
    and got nowhere. `_run_job` turns every non-zero into
    job:interest-extract:last_fail.
    """
    located, why = _extractor_paths(cfg)
    if located is None:
        _say(f"extract-interests: cannot locate the extractor -- {why}")
        return 2
    script, repo_root = located
    python = sys.executable or "python"
    artifact = Path(cfg.interest_candidates_path)
    before_mtime = artifact.stat().st_mtime if artifact.exists() else None

    before = _extractor_status(python, script, repo_root)
    pending_before = before.get("pending_conversations")
    _say(
        f"extract-interests: repo={repo_root} pending={pending_before} "
        f"failed={before.get('failed_conversations')} themes={before.get('themes')}"
    )

    map_timed_out = False
    if args.skip_map:
        _say("extract-interests: --skip-map, reducing over the digests already collected")
    else:
        map_args = ["map"]
        if args.map_limit is not None:
            map_args += ["--limit", str(args.map_limit)]
        code, map_timed_out = _run_extractor_stage(
            python, script, repo_root, map_args, cfg.interest_extract_map_seconds
        )
        if code not in (0, None):
            # A map that refuses to start -- cmd_map raises SystemExit when the
            # claude.ai preflight fails -- is the single most likely failure
            # here, and it must never look like a good night's run.
            _say(f"extract-interests: map FAILED (exit {code}) -- not reducing")
            return 2

    # reduce runs even after a map timeout: a partial digest set still yields a
    # valid, if slightly staler, candidate list, and publishing something beats
    # publishing nothing.
    # --max-themes bounds what reduce sends claude.ai in one request. Without
    # it the extractor forwards its ENTIRE theme list -- its durability gate
    # filters nothing on the real corpus -- and the request grows with the
    # corpus until claude.ai answers with nothing at all. That is not a
    # hypothetical: the first scheduled run of this job mapped 240
    # conversations successfully and then failed reduce outright, twice, in
    # under ten seconds each time. See cfg.interest_extract_max_themes.
    reduce_args = ["reduce", "--out", str(artifact)]
    if cfg.interest_extract_max_themes:
        reduce_args += ["--max-themes", str(cfg.interest_extract_max_themes)]
    code, reduce_timed_out = _run_extractor_stage(
        python, script, repo_root, reduce_args,
        cfg.interest_extract_reduce_seconds,
    )
    if code == 2 and cfg.interest_extract_max_themes:
        # argparse exits 2 on an unknown flag. An `ai` checkout older than
        # eranbw123/ai#21 has no --max-themes, and refusing to reduce at all
        # because of a flag it has never heard of would be a worse failure
        # than the one the flag exists to prevent. Say so, then reduce
        # without it -- loudly, so the log records that the request went out
        # unbounded and may well come back empty.
        _say(
            "extract-interests: this ai checkout does not accept --max-themes "
            "(pre-#21); retrying reduce UNBOUNDED, which is the configuration "
            "that fails once the corpus is large"
        )
        code, reduce_timed_out = _run_extractor_stage(
            python, script, repo_root, ["reduce", "--out", str(artifact)],
            cfg.interest_extract_reduce_seconds,
        )
    if reduce_timed_out or code != 0:
        _say(
            f"extract-interests: reduce FAILED (exit {code}, timed_out={reduce_timed_out})"
        )
        return 2

    after = _extractor_status(python, script, repo_root)
    pending_after = after.get("pending_conversations")
    after_mtime = artifact.stat().st_mtime if artifact.exists() else None
    _say(
        f"extract-interests: pending {pending_before} -> {pending_after}, "
        f"failed {before.get('failed_conversations')} -> {after.get('failed_conversations')}, "
        f"artifact={'rewritten' if after_mtime != before_mtime else 'UNCHANGED'}"
    )

    if after_mtime is None:
        _say(f"extract-interests: reduce exited 0 but wrote no artifact at {artifact}")
        return EXTRACT_UNPRODUCTIVE
    # The honesty check. `reduce` exiting 0 is not proof anything happened: if
    # map had pending work and finished with just as much still pending, it
    # spent browser time and digested nothing, and the next import will
    # re-import a byte-identical artifact. Say so, and fail.
    if (not args.skip_map and pending_before and pending_after is not None
            and pending_after >= pending_before):
        _say(
            f"extract-interests: map made no progress -- {pending_before} conversations "
            f"were pending before and {pending_after} still are"
            + (" (map timed out)" if map_timed_out else "")
        )
        return EXTRACT_UNPRODUCTIVE
    return 0


def _offers_list(conn, status):
    rows = offers.list_offers(conn, status=status or offers.OFFERED)
    if not rows:
        print_safe(f"no offers with status '{status or offers.OFFERED}'")
        return 0
    for row in rows:
        score = "  -  " if row["score"] is None else f"{row['score']:.2f}"
        print_safe(
            f"{row['key']}  {row['kind']}  score={score}  status={row['status']}  "
            f"evidence={len(row['evidence'])} quote(s) from "
            f"{len(row['source_conversations'])} conversation(s)"
        )
    return 0


def _offer_why(conn, key):
    """The provenance answer for one offer: which conversations, which
    verbatim quotes, every score term, and the whole event chain."""
    detail = offers.offer_detail(conn, key)
    if detail is None:
        print(f"no offer with key {key!r}", file=sys.stderr)
        return 2
    print_safe(f"{detail['key']}  ({detail['kind']}, {detail['status']})")
    print_safe(f"  title: {detail['title']}")
    if detail["score"] is not None:
        print_safe(f"  score: {detail['score']:.3f}")
        print_safe(f"  terms: {json.dumps(detail['score_terms'], ensure_ascii=False)}")
    print_safe(f"  durability: {json.dumps(detail['durability'], ensure_ascii=False)}")
    print_safe(f"  similarity: {json.dumps(detail['similarity'], ensure_ascii=False)}")
    print_safe(f"  conversations: {', '.join(detail['source_conversations']) or '-'}")
    for quote in detail["evidence"]:
        print_safe(
            f"  [{quote['date']}] ({quote['lang']}, depth {quote['depth']:.2f}, "
            f"conv {quote['conversation_id'] or '-'}) {quote['quote']}"
        )
    for event in detail["events"]:
        print_safe(
            f"  {event['at']}  {event['actor']}  {event['action']}  "
            f"{event['from_status'] or '-'} -> {event['to_status'] or '-'}"
        )
    return 0


def _offer_decide(conn, cfg, key, action, note):
    try:
        if action == "accept":
            result = offers.accept(conn, key, note=note)
            print_safe(json.dumps(result["entry"], ensure_ascii=False, indent=2))
            print_safe(
                "accepted -- the interests sync writes it into interests.json and the DB "
                "(offers.activate() then starts its lifecycle)"
            )
        elif action == "reject":
            result = offers.reject(conn, key, note=note, interests_path=cfg.interests_path)
            print_safe(f"rejected {key} -- blocked terms: {', '.join(result['blocked_terms'])}")
        elif action == "snooze":
            result = offers.snooze(conn, key, note=note)
            print_safe(f"snoozed {key} until {result['snoozed_until']}")
        else:
            offers.undo_auto_pause(conn, key, note=note)
            print_safe(f"{key} is active again -- the silence clock restarts from now")
    except offers.OfferError as e:
        print(f"{action} failed: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
