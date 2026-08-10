"""One-time golden-set rating pass over already-collected items.

Samples ~25 scored-but-unrated items (spread across interests and score
bands) and records your verdicts through db.add_feedback -- the same table
and codes the Telegram buttons write, so stats.py and the scorer's
past-verdicts block pick them up with no new mechanism.

This is the lab's ONLY write path into discovery.db, and it writes nothing
but feedback rows.

Usage (from the repo root):
    python experiments/lab/rate_batch.py             # interactive rating
    python experiments/lab/rate_batch.py --dump      # print the batch, save batch.json
    python experiments/lab/rate_batch.py --apply "101=f 105=t 92=u"
                                                     # rate by item id: f/u/d/t
"""
import argparse
import json
import sys

import db_replay
from lab_common import ARTIFACTS, utf8_streams

from discovery import db  # noqa: E402
from discovery.config import load as load_cfg  # noqa: E402

VERDICTS = {"f": "fire", "u": "up", "d": "down", "t": "trash"}
BATCH_PATH = ARTIFACTS / "golden" / "batch.json"


def build_batch(cfg, n):
    ro = db_replay.open_ro(cfg.db_path)
    rows = db_replay.golden_sample(ro, n)
    batch = [
        {
            "item_id": r["item_id"],
            "interest_id": r["interest_id"],
            "interest_key": r["interest_key"],
            "final_score": r["final_score"],
            "min_score": r["min_score"],
            "title": r["title"],
            "url": r["url"],
            "snippet": (r["text"] or "")[:280],
        }
        for r in rows
    ]
    BATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    BATCH_PATH.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    return batch


def load_batch(cfg, n):
    if BATCH_PATH.exists():
        return json.loads(BATCH_PATH.read_text(encoding="utf-8"))
    return build_batch(cfg, n)


def show(entry):
    print(f"\n[{entry['item_id']}] ({entry['interest_key']}, scored {entry['final_score']:.2f}"
          f" vs bar {entry['min_score']:.2f})")
    print(f"  {entry['title']}")
    print(f"  {entry['url']}")
    if entry["snippet"]:
        print(f"  {entry['snippet']}")


def record(conn, entry, verdict):
    db.add_feedback(
        conn, entry["item_id"], entry["interest_id"], verdict,
        note="golden-set", original_score=entry["final_score"],
    )


def already_rated(conn, batch):
    ids = {e["item_id"] for e in batch}
    rows = conn.execute(
        f"SELECT DISTINCT item_id FROM feedback WHERE item_id IN "
        f"({','.join('?' * len(ids))})", tuple(ids),
    ).fetchall()
    return {r["item_id"] for r in rows}


def interactive(conn, batch):
    done = already_rated(conn, batch)
    print("Rate each item: f=fire  u=up  d=down  t=trash  s=skip  q=quit")
    rated = 0
    for entry in batch:
        if entry["item_id"] in done:
            continue
        show(entry)
        while True:
            answer = input("  verdict> ").strip().lower()
            if answer in ("q", "s") or answer in VERDICTS:
                break
            print("  one of: f u d t s q")
        if answer == "q":
            break
        if answer == "s":
            continue
        record(conn, entry, VERDICTS[answer])
        rated += 1
    print(f"\nrecorded {rated} verdicts (note='golden-set')")


def apply_verdicts(conn, batch, spec):
    by_id = {e["item_id"]: e for e in batch}
    rated = 0
    for token in spec.split():
        item_id, _, letter = token.partition("=")
        entry = by_id.get(int(item_id))
        verdict = VERDICTS.get(letter.strip().lower(), letter.strip().lower())
        if entry is None or verdict not in VERDICTS.values():
            print(f"skipping bad token: {token!r}")
            continue
        record(conn, entry, verdict)
        rated += 1
    print(f"recorded {rated} verdicts (note='golden-set')")


def main():
    utf8_streams()
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--dump", action="store_true", help="print the batch and exit (no writes)")
    ap.add_argument("--apply", help="'<item_id>=<f|u|d|t> ...' to rate non-interactively")
    args = ap.parse_args()

    cfg = load_cfg()
    batch = load_batch(cfg, args.n)

    if args.dump:
        for entry in batch:
            show(entry)
        print(f"\n{len(batch)} items; batch saved to {BATCH_PATH}")
        return

    conn = db.connect(cfg.db_path)
    if args.apply:
        apply_verdicts(conn, batch, args.apply)
    else:
        interactive(conn, batch)


if __name__ == "__main__":
    sys.exit(main())
