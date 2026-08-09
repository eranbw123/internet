"""E1 -- scorer readouts. The measurement experiments are retired.

History (proposals 001-005, results persisted in artifacts/scoring/
state.json): gen-1 jitter baseline, gen-2 full-corpus rescore, gen-3
band/control repeat (REVERTED), gen-4 pinned pass (VALIDATED -- drift closed
for the pinned condition: held-out mapd 0.0245, 2/122 notify disagreements).
Proposal 005 retired the drift apparatus; the frozen-scorer sentinel now
lives inside exp_discovery.py as a non-decisive 2-item probe.

    python experiments/lab/exp_scoring.py distribution   # free corpus readout
    python experiments/lab/exp_scoring.py report         # past generations
"""
import argparse
import json

import db_replay
from lab_common import Lab, utf8_streams

from discovery.config import load as load_cfg  # noqa: E402
from discovery.models import DIMENSIONS  # noqa: E402

BAND = 0.05


def distribution(conn):
    """Corpus score-vs-bar readout: per-interest notify_rate, band density,
    per-dimension distinct-value counts."""
    rows = db_replay.scored_items(conn)
    per_interest = {}
    for r in rows:
        b = per_interest.setdefault(
            r["interest_key"], {"n": 0, "notify": 0, "band": 0, "bar": r["min_score"]}
        )
        b["n"] += 1
        b["notify"] += r["final_score"] >= r["min_score"]
        b["band"] += abs(r["final_score"] - r["min_score"]) <= BAND
    for b in per_interest.values():
        b["notify_rate"] = round(b["notify"] / b["n"], 3)
        b["band_density"] = round(b["band"] / b["n"], 3)
    total = len(rows)
    return {
        "corpus_n": total,
        "notify_rate": round(sum(r["final_score"] >= r["min_score"] for r in rows) / total, 3),
        "band_density": round(
            sum(abs(r["final_score"] - r["min_score"]) <= BAND for r in rows) / total, 3),
        "per_interest": per_interest,
        "dim_distinct_values": {d: len({round(r[d], 2) for r in rows}) for d in DIMENSIONS},
    }


def main():
    utf8_streams()
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["distribution", "report"])
    args = ap.parse_args()
    if args.mode == "distribution":
        cfg = load_cfg()
        print(json.dumps(distribution(db_replay.open_ro(cfg.db_path)), indent=1))
    else:
        lab = Lab("scoring")
        for g in lab.state["generations"]:
            print(json.dumps({k: g.get(k) for k in
                              ("gen", "ts", "kind", "aggregate", "verdict", "guidance")},
                             ensure_ascii=False, indent=1))
        print(f"budget used: {lab.state['budget_used']}/{lab.budget_cap}")


if __name__ == "__main__":
    main()
