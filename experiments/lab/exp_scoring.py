"""E1 -- scorer stability, reduced to the one open question (proposal 004).

History, all retired with results persisted in state.json: gen-1 far-from-bar
jitter baseline; gen-2 full-corpus rescore (drift 0.0569 vs stored, 14/122
flips, unattributable); gen-3 band/control 3-repeat (jitter small: band
mean_std 0.0137, mapd 0.0223; flips track bar proximity, not variance;
REVERTED on a 0.0003 conjunctive miss). Proposal 004 deleted the repeat
apparatus and the conjunctive all_hold gate, leaving a single question:
is drift within-version? One more pinned corpus pass answers it against the
gen-2 stamped scores, decided on the 75 items gen-3 never sampled.

    python experiments/lab/exp_scoring.py pinned-pass [--budget 450]
    python experiments/lab/exp_scoring.py distribution   # free corpus readout
    python experiments/lab/exp_scoring.py report

No jitter, std, dimension-noise, separation, or AUC claim is made here:
mean_abs_pairwise_delta over two passes is the only variance-adjacent
statistic, and the label-gated metrics stay gated (owner deferred labels).
"""
import argparse
import json
import statistics
import sys

import db_replay
import prod_scorer
from lab_common import Lab, council_judge, utf8_streams

from discovery import db as ddb  # noqa: E402
from discovery import scoring  # noqa: E402
from discovery.config import load as load_cfg  # noqa: E402
from discovery.models import DIMENSIONS  # noqa: E402

PINNED_HASH = "92954b87de02"     # run void on any other prompt or model
PINNED_MODEL = "claude-opus-5"
BAND = 0.05

BRIEF = """\
Experiment E1 (proposal 004) of a personal discovery engine's lab. The LLM
scorer rates items 0-1; a weighted final_score decides notification against
per-interest bars of 0.70-0.85. This generation runs a second pinned pass
over the stored 122-item corpus and compares it to the gen-2 stamped pass
(same prompt hash, same model). Pre-registered, decided on the 75 held-out
items never in gen-3's band/control sample: held-out
mean_abs_pairwise_delta in [0.010, 0.035] and < 0.0341 (0.6x the 0.0569
cross-condition figure); corpus-wide notify disagreements <= 10 of 122; >=
60% of disagreeing items within 0.05 of their bar. Rollback: held-out mapd
>= 0.0341, corpus mapd >= 0.045, disagreements > 18, or < 50% of
disagreements near-bar. Invalid (not passing) if >= 90% of held-out deltas
are exactly 0.0 (caching/shared-context suspect). If the prediction holds,
drift closes and the next question is bar calibration."""


def gen3_sample_ids(lab):
    frozen = lab.state.get("band_repeat_items")
    if not frozen:
        sys.exit("gen-3 band/control sample missing from state; held-out arm undefined")
    return set(frozen["band"]) | set(frozen["control"])


def run_pinned_pass(lab, args):
    cfg = load_cfg()
    prov = prod_scorer.provider()
    if prov.model != PINNED_MODEL:
        sys.exit(f"model {prov.model} != pinned {PINNED_MODEL}; run void")
    if scoring.prompt_fingerprint() != PINNED_HASH:
        sys.exit("production prompt changed since gen-2; re-pin before running")

    gen2 = lab.state.get("rescore")
    if not gen2:
        sys.exit("gen-2 rescore state missing; nothing to compare a pinned pass against")
    gen3_ids = gen3_sample_ids(lab)

    conn = db_replay.open_ro(cfg.db_path)
    rows = [r for r in db_replay.scored_items(conn) if str(r["item_id"]) in gen2]
    by_id = {i.id: i for i in ddb.active_interests(conn)}

    partial = lab.state.setdefault("pinned_pass_partial", {})
    for row in rows:
        key = str(row["item_id"])
        if key in partial:
            continue
        item = db_replay.to_candidate(row)
        interest = by_id[row["interest_id"]]
        result = lab.call(
            "score",
            lambda: prod_scorer.score_item(prov, item, interest,
                                           match_score=row["match_score"]),
            kind="pinned_pass", item_id=row["item_id"])
        if result is None:
            continue
        if result.prompt_hash != PINNED_HASH:
            sys.exit(f"prompt hash {result.prompt_hash} != pinned {PINNED_HASH}; run void")
        g2 = gen2[key]
        bar = g2["bar"]
        rec = {
            "item_id": row["item_id"],
            "interest_key": row["interest_key"],
            "bar": bar,
            "pass1": g2["new"],
            "pass2": result.final_score,
            "delta": round(result.final_score - g2["new"], 4),
            "disagree": (result.final_score >= bar) != (g2["new"] >= bar),
            "dist_to_bar": round(abs(g2["new"] - bar), 4),
            "held_out": row["item_id"] not in gen3_ids,
        }
        partial[key] = rec
        lab.save()
        lab.log(call="score", kind="pinned_pass", **rec)

    recs = list(partial.values())
    if not recs:
        sys.exit("no comparisons produced")

    def mapd(subset):
        return round(statistics.mean(abs(r["delta"]) for r in subset), 4) if subset else None

    held = [r for r in recs if r["held_out"]]
    gen3 = [r for r in recs if not r["held_out"]]
    disagree = [r for r in recs if r["disagree"]]
    near = [r for r in disagree if r["dist_to_bar"] <= BAND]
    zero_frac = round(sum(r["delta"] == 0 for r in held) / len(held), 3) if held else None

    def notify_rates(pass_key):
        per = {}
        for r in recs:
            b = per.setdefault(r["interest_key"], [0, 0])
            b[1] += 1
            b[0] += r[pass_key] >= r["bar"]
        return {k: f"{v[0]}/{v[1]}" for k, v in sorted(per.items())}

    held_mapd, corpus_mapd = mapd(held), mapd(recs)
    near_share = round(len(near) / len(disagree), 3) if disagree else None
    agg = {
        "pinned": {"prompt_hash": PINNED_HASH, "model": PINNED_MODEL},
        "n": {"corpus": len(recs), "held_out": len(held), "gen3_arm": len(gen3)},
        "mapd": {"held_out": held_mapd, "corpus": corpus_mapd, "gen3_arm": mapd(gen3)},
        "notify_disagreements": len(disagree),
        "disagreement_near_bar_share": near_share,
        "disagreeing_items": [
            {k: r[k] for k in ("item_id", "interest_key", "pass1", "pass2", "dist_to_bar")}
            for r in sorted(disagree, key=lambda r: r["dist_to_bar"])
        ],
        "held_out_zero_delta_fraction": zero_frac,
        "notify_rate_pass1": notify_rates("pass1"),
        "notify_rate_pass2": notify_rates("pass2"),
        "preregistered_checks": {
            "held_out_mapd_in_[0.010,0.035]": held_mapd is not None and 0.010 <= held_mapd <= 0.035,
            "held_out_mapd<0.0341": held_mapd is not None and held_mapd < 0.0341,
            "disagreements<=10": len(disagree) <= 10,
            "near_bar_share>=0.60": near_share is not None and near_share >= 0.60,
        },
        "rollback_triggers": {
            "held_out_mapd>=0.0341": held_mapd is not None and held_mapd >= 0.0341,
            "corpus_mapd>=0.045": corpus_mapd is not None and corpus_mapd >= 0.045,
            "disagreements>18": len(disagree) > 18,
            "near_bar_share<0.50": near_share is not None and near_share < 0.50,
            "invalid_zero_delta>=0.90": zero_frac is not None and zero_frac >= 0.90,
        },
    }
    record = {"kind": "pinned_pass", "aggregate": agg, "delta_vs_baseline": None}

    judged = council_judge(
        prov, lab, BRIEF, {"aggregate": agg},
        "Question: did the pre-registered prediction hold on the held-out arm, "
        "is the run valid per the zero-delta guard, and per proposal 004's "
        "triggers does the change stand or revert? If drift is closed, state "
        "what the bar-calibration generation should measure first.",
        kind="pinned_pass")
    if judged:
        record["verdict"] = judged["verdict"]
        record["guidance"] = judged["guidance"]

    lab.state.pop("pinned_pass_partial", None)
    lab.state["pinned_pass_results"] = recs
    lab.add_generation(record)
    print(json.dumps({k: record[k] for k in ("gen", "kind", "aggregate", "verdict", "guidance")
                      if k in record}, ensure_ascii=False, indent=1))
    print(f"budget used: {lab.state['budget_used']}/{lab.budget_cap}")


# --- distribution (free) -----------------------------------------------------

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
    ap.add_argument("mode", choices=["pinned-pass", "distribution", "report"])
    ap.add_argument("--budget", type=int, default=450)
    args = ap.parse_args()

    lab = Lab("scoring", budget_cap=args.budget)
    if args.mode == "distribution":
        cfg = load_cfg()
        print(json.dumps(distribution(db_replay.open_ro(cfg.db_path)), indent=1))
    elif args.mode == "report":
        for g in lab.state["generations"]:
            print(json.dumps({k: g.get(k) for k in
                              ("gen", "ts", "kind", "aggregate", "verdict", "guidance")},
                             ensure_ascii=False, indent=1))
        print(f"budget used: {lab.state['budget_used']}/{lab.budget_cap}")
    else:
        run_pinned_pass(lab, args)


if __name__ == "__main__":
    main()
