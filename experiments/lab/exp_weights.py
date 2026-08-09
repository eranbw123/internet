"""E4 -- weights & threshold fit against feedback. Code-only, no LLM calls.

Dimensions are stored per score, so once verdicts accumulate the ranking
formula can be fitted offline: grid-search over weight combinations (0.05
steps summing to 1.0) maximizing AUC -- the fraction of positive/negative
verdict pairs the weighted score ranks correctly.

Prints a PROPOSAL only; models.WEIGHTS is changed by hand, never from here.

    python experiments/lab/exp_weights.py
"""
import itertools
import json
import statistics

import db_replay
from lab_common import utf8_streams

from discovery.config import load as load_cfg  # noqa: E402
from discovery.models import DIMENSIONS, WEIGHTS  # noqa: E402

POSITIVE = ("fire", "up")
NEGATIVE = ("down", "trash")
STEPS = 20  # weights in multiples of 1/20 = 0.05
MIN_LABELS = 30


def labeled(conn):
    pos, neg = [], []
    for r in db_replay.feedback_rows(conn):
        if r["final_score"] is None:
            continue
        dims = {d: r[d] for d in DIMENSIONS}
        if r["verdict"] in POSITIVE:
            pos.append(dims)
        elif r["verdict"] in NEGATIVE:
            neg.append(dims)
    return pos, neg


def auc(weights, pos, neg):
    def score(dims):
        return sum(w * dims[d] for d, w in weights.items())
    p_scores = [score(d) for d in pos]
    n_scores = [score(d) for d in neg]
    return statistics.mean(
        (p > n) + 0.5 * (p == n) for p in p_scores for n in n_scores
    )


def grid():
    """Every 6-dimension weight combination in 0.05 steps summing to 1."""
    for combo in itertools.product(range(STEPS + 1), repeat=len(DIMENSIONS) - 1):
        rest = STEPS - sum(combo)
        if rest < 0:
            continue
        parts = (*combo, rest)
        yield {d: p / STEPS for d, p in zip(DIMENSIONS, parts)}


def main():
    utf8_streams()
    cfg = load_cfg()
    conn = db_replay.open_ro(cfg.db_path)
    pos, neg = labeled(conn)
    n = len(pos) + len(neg)
    print(f"labels: {len(pos)} positive (fire/up), {len(neg)} negative (down/trash)")
    if not pos or not neg:
        print("need at least one of each side; nothing to fit")
        return
    if n < MIN_LABELS:
        print(f"WARNING: {n} labels < {MIN_LABELS}; treat everything below as "
              "directional, not a fit")

    current = dict(WEIGHTS, specificity=0.0)
    print(f"\ncurrent weights AUC: {auc(current, pos, neg):.3f}  {current}")

    scored = sorted(((auc(w, pos, neg), w) for w in grid()), key=lambda t: -t[0])
    print("\ntop weight combinations by AUC (proposal only):")
    for value, w in scored[:5]:
        print(f"  AUC {value:.3f}  "
              + json.dumps({d: v for d, v in w.items() if v}, sort_keys=True))


if __name__ == "__main__":
    main()
