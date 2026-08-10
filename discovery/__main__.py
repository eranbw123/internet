"""CLI: python -m discovery <command>  (run from the repo root).

`python -m app <command>` is the same CLI under a shorter name.

    init          create/upgrade discovery.db and load interests.json
    run-once      one collect -> score -> notify cycle (--source to limit
                  collection to one collector; Alerts still send immediately,
                  Discovery items still just queue for `digest`)
    run           the same, on a loop -- stocks/web_search/youtube each on
                  their own cadence plus a daily digest (see scheduler.py)
    discover      run one collector across all active interests, print
                  candidates and scores, never sends (e.g. `discover web_search`)
    score         push one candidate through the pipeline and print the verdict
    digest        send the pending Discovery digest now (Alerts are unaffected)
    listen        long-poll Telegram for feedback-button presses (blocking)
    items         list recently scored items
    feedback      record feedback on an item, by item id
    stats         funnel, feedback and cost over a trailing window
    personal-state  print the ai repo's personal-state contract artifact
"""
import argparse
import json
import sys
from collections import Counter

from datetime import datetime, timezone

from . import config, db, feedback_listener, interests, personal_state, providers, scheduler, stats
from .collectors import COLLECTORS
from .models import CandidateItem
from .notify import FEEDBACK_VERDICTS, print_safe
from .personal_state import PersonalStateError
from .pipeline import Budget, deliver, ingest, run_once, send_digest


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
    run = sub.add_parser("run", help="scheduler loop: per-job cadence + daily digest")
    run.add_argument("--cycles", type=int, help="stop after N scheduler ticks (tests)")
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
    sub.add_parser("listen", help="long-poll Telegram for feedback-button presses (blocking)")
    fb = sub.add_parser("feedback", help="rate an item")
    fb.add_argument("item_id", type=int)
    fb.add_argument("verdict", choices=sorted(FEEDBACK_VERDICTS))
    fb.add_argument("--interest-id", type=int)
    fb.add_argument("--note", default="")
    st = sub.add_parser("stats", help="funnel, feedback rates and estimated cost")
    st.add_argument("--days", type=int, default=7, help="trailing window (default 7)")
    ps = sub.add_parser(
        "personal-state", help="print the ai repo's personal-state contract artifact"
    )
    ps.add_argument("--path", help="override DISCOVERY_PERSONAL_STATE / cfg.personal_state_path")

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
        print(run_once(conn, provider(), cfg, sources=sources, dry_run=args.dry_run))
    elif args.command == "run":
        scheduler.run_forever(conn, provider(), cfg, dry_run=args.dry_run, cycles=args.cycles)
    elif args.command == "discover":
        return _discover(conn, provider(), cfg, args)
    elif args.command == "score":
        return _score_one(conn, provider(), cfg, args)
    elif args.command == "digest":
        print(f"sent {send_digest(conn, cfg, dry_run=args.dry_run)} digest item(s)")
    elif args.command == "listen":
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
        print_safe(stats.report(conn, args.days))
    elif args.command == "personal-state":
        return _personal_state(cfg, args)
    return 0


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
                origin_interest=interest.key, budget=budget,
            )
            counts[outcome.stage] += 1
            # Flushed per item rather than once at the end (see pipeline.py's
            # run_once() for the same fix and its rationale): _print_discovered
            # below is exactly what killed a real run mid-loop once already
            # (a narrow console codepage choking on a model-generated
            # character) and silently lost every already-scored item's funnel
            # counts with it, even though their DB rows were already committed.
            db.bump(conn, {"collected": 1, outcome.stage: 1})
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


if __name__ == "__main__":
    sys.exit(main())
