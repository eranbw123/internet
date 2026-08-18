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

There is no `run`/scheduler loop -- an OS scheduler (see
ops/install_tasks.py) calls the commands above on their own cadence instead;
a session-child tick loop gets reaped the moment the SSH session disconnects.
"""
import argparse
import json
import subprocess
import sys
from collections import Counter

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

    args = parser.parse_args(argv)
    if args.command == "trace-fixture" and not args.db:
        # trace_fixture.build() inserts fixture interests/items/scores/a real
        # feedback row through the real production code paths -- refusing to
        # fall back to cfg.db_path's default (REPO_ROOT/discovery.db) is what
        # stops an unflagged invocation from writing fixture rows into the
        # real database.
        print("trace-fixture requires --db PATH -- refusing to default to the production db", file=sys.stderr)
        return 2
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


def _dispatch(conn, cfg, args, provider):
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


def _web_tick_cmd(conn, provider, cfg, dry_run):
    """`provider` here is the scoring provider (built from cfg.provider by
    the CLI's shared provider() closure, same as run-once) -- web_tick()
    builds its own search-capable mission provider internally from
    cfg.mission_provider and gates on that provider's own preflight, so
    there is no separate health.preflight_gate() call here."""
    result = missions.web_tick(conn, cfg, provider=provider, dry_run=dry_run)
    if not result["preflight_ok"]:
        return 3
    print(result)
    return 0


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


def _offers_cmd(conn, cfg, args):
    """One subcommand over discovery/offers.py. Every branch is offline:
    importing reads a local artifact, the sweeps read the funnel, and the
    decisions write only the offer/interest tables -- no provider is ever
    built, no model is ever called."""
    if args.why:
        return _offer_why(conn, args.why)
    if args.import_path is not None:
        path = args.import_path or cfg.interest_candidates_path
        summary = offers.import_artifact(conn, path, interests_path=cfg.interests_path)
        print_safe(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.sweep:
        summary = offers.sweep(conn)
        for announcement in summary["announcements"]:
            print_safe(announcement["text"])
        print_safe(json.dumps(
            {k: v for k, v in summary.items() if k != "announcements"}, ensure_ascii=False
        ))
        return 0
    for key, action in (
        (args.accept, "accept"), (args.reject, "reject"),
        (args.snooze, "snooze"), (args.undo, "undo"),
    ):
        if key:
            return _offer_decide(conn, cfg, key, action, args.note)
    return _offers_list(conn, args.status)


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
