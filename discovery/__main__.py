"""CLI: python -m discovery <command>  (run from the repo root).

`python -m app <command>` is the same CLI under a shorter name.

    init          create/upgrade discovery.db and load interests.json
    run-once      one collect -> score -> notify cycle (--source to limit
                  collection to one collector; Alerts still send immediately,
                  Discovery items still just queue for `digest`). Gated by a
                  provider preflight (health.py) -- a dead Chrome/CDP exits 3
                  before touching a single collector or LLM call.
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

There is no `run`/scheduler loop -- an OS scheduler (see
ops/install_tasks.py) calls the commands above on their own cadence instead;
a session-child tick loop gets reaped the moment the SSH session disconnects.
"""
import argparse
import json
import sys
from collections import Counter

from datetime import datetime, timezone

from . import config, db, feedback_listener, health, interests, personal_state, providers, stats, teach
from .collectors import COLLECTORS
from .models import CandidateItem
from .notify import FEEDBACK_VERDICTS, print_safe
from .personal_state import PersonalStateError
from .pipeline import Budget, deliver, ingest, outcome_metric, run_once, send_digest
from . import interest_state


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
    ro = sub.add_parser("run-once", help="one cycle")
    ro.add_argument(
        "--source", choices=sorted(COLLECTORS), help="collect only this collector's items"
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

    args = parser.parse_args(argv)
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
        try:
            count = interests.sync(conn, cfg.interests_path)
        except FileNotFoundError:
            print(f"interests file not found: {cfg.interests_path}", file=sys.stderr)
            return 2
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"malformed interests file {cfg.interests_path}: {e}", file=sys.stderr)
            return 2
        print(f"{cfg.db_path}: schema ready, {count} interests loaded")
    elif args.command == "run-once":
        sources = [args.source] if args.source else None
        job_name = health.job_name_for_source(args.source)
        return _run_job(
            conn, job_name, lambda: _run_once_cmd(conn, provider(), cfg, sources, args.dry_run, job_name)
        )
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


def _digest_cmd(conn, cfg, dry_run):
    print(f"sent {send_digest(conn, cfg, dry_run=dry_run)} digest item(s)")
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


if __name__ == "__main__":
    sys.exit(main())
