"""CLI: python -m discovery <command>  (run from the repo root).

`python -m app <command>` is the same CLI under a shorter name.

    init          create/upgrade discovery.db and load interests.json
    run-once      one collect -> score -> notify cycle
    run           the same, on a loop (--interval seconds)
    discover      run one collector across all active interests, print
                  candidates and scores, never sends (e.g. `discover web`)
    score         push one candidate through the pipeline and print the verdict
    items         list recently scored items
    feedback      record a thumbs up/down on an item, by item id
"""
import argparse
import json
import sys

from . import config, db, interests, providers, scheduler
from .collectors import COLLECTORS
from .models import CandidateItem
from .pipeline import deliver, ingest, run_once


def main(argv=None):
    parser = argparse.ArgumentParser(prog="discovery", description=__doc__)
    parser.add_argument("--db", help="override DISCOVERY_DB")
    parser.add_argument("--provider", help="override DISCOVERY_PROVIDER (anthropic|openai)")
    parser.add_argument("--model", help="override DISCOVERY_MODEL")
    parser.add_argument("--dry-run", action="store_true", help="print pushes instead of sending")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the DB and load interests.json")
    sub.add_parser("run-once", help="one cycle")
    run = sub.add_parser("run", help="cycle on a loop")
    run.add_argument("--interval", type=int, help="seconds between cycles")
    run.add_argument("--cycles", type=int, help="stop after N cycles")
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
    fb = sub.add_parser("feedback", help="rate an item")
    fb.add_argument("item_id", type=int)
    fb.add_argument("verdict", choices=["up", "down"])
    fb.add_argument("--interest-id", type=int)
    fb.add_argument("--note", default="")

    args = parser.parse_args(argv)
    cfg = config.load()
    if args.db:
        cfg.db_path = args.db
    if args.provider:
        cfg.provider = args.provider
        cfg.model = args.model or config.DEFAULT_MODELS.get(args.provider, cfg.model)
    if args.model:
        cfg.model = args.model
    if getattr(args, "interval", None):
        cfg.interval_seconds = args.interval

    conn = db.connect(cfg.db_path)
    db.init(conn)

    # Built only for the commands that talk to a model -- constructing a
    # provider needs an API key, and `init`/`items`/`feedback` don't need one.
    def provider():
        return providers.get_provider(cfg)

    if args.command == "init":
        count = interests.sync(conn, cfg.interests_path)
        print(f"{cfg.db_path}: schema ready, {count} interests loaded")
    elif args.command == "run-once":
        print(run_once(conn, provider(), cfg, dry_run=args.dry_run))
    elif args.command == "run":
        scheduler.run_forever(conn, provider(), cfg, dry_run=args.dry_run, cycles=args.cycles)
    elif args.command == "discover":
        return _discover(conn, provider(), cfg, args)
    elif args.command == "score":
        return _score_one(conn, provider(), cfg, args)
    elif args.command == "items":
        _list_items(conn, args.limit, args.min_score)
    elif args.command == "feedback":
        db.add_feedback(conn, args.item_id, args.interest_id, args.verdict, args.note)
        print(f"recorded {args.verdict} on item {args.item_id}")
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
    for interest in active:
        try:
            candidates = collect(interest, cfg, provider)
        except Exception as e:  # noqa: BLE001
            print(f"{interest.key}/{args.source}: collect failed: {e}", file=sys.stderr)
            continue
        for item in candidates:
            total += 1
            outcome = ingest(conn, provider, cfg, item, active, origin_interest=interest.key)
            _print_discovered(interest, item, outcome)
    print(f"\n{total} candidate(s) from '{args.source}'", file=sys.stderr)
    return 0


def _print_discovered(interest, item, outcome):
    query = item.metadata.get("query") if item.metadata else None
    header = f"[{interest.key}] {outcome.stage}"
    if query:
        header += f"  (query: {query!r})"
    print(header)
    print(f"      {item.title}")
    print(f"      {item.url}")
    if outcome.score is not None:
        print(f"      score={outcome.score.final_score:.2f}  {outcome.score.reason}")


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
        print(f"[{row['final_score'] * 100:>3.0f}] #{row['id']} {row['interest']}: {row['title']}")
        print(f"      {row['reason']}")
        print(f"      {row['url']}")


if __name__ == "__main__":
    sys.exit(main())
