"""E1 -- scorer stability at the notify band (see LAB.md, proposal 003).

The notify decision lives in a 0.70-0.85 threshold band. This experiment
measures whether the production scorer is reproducible enough for that band
to mean anything, on the items where it matters: those near their bars.

    python experiments/lab/exp_scoring.py band-repeat [--repeats 3] [--budget 300]
    python experiments/lab/exp_scoring.py distribution   # free corpus readout
    python experiments/lab/exp_scoring.py report

History (retired per proposal 003's complexity budget; results persist in
state.json): gen-1 far-from-bar baseline jitter, gen-2 full-corpus rescore
(mean drift 0.0569, 14/122 flips, unattributable at strict_within_version_n
0), verdict-separation reporting (label-gated; owner deferred labels).

band-repeat scores each item K times with the pinned production prompt
(hash asserted) and an empty feedback block, then reports per stratum --
band (|gen2 score - bar| <= 0.05) vs control (>= 0.15 away) -- mean_std,
max_std, mean absolute pairwise delta, flip rate (notify decision not
unanimous), per-dimension noise, and 1000-resample bootstrap CIs. The
control stratum doubles as a cache/session leak detector. Pre-registered
pass/fail intervals from proposal 003 are checked in code.
"""
import argparse
import itertools
import json
import random
import statistics
import sys

import db_replay
import prod_scorer
from lab_common import Lab, council_judge, utf8_streams

from discovery import db as ddb  # noqa: E402
from discovery import scoring  # noqa: E402
from discovery.config import load as load_cfg  # noqa: E402
from discovery.models import DIMENSIONS  # noqa: E402

PINNED_HASH = "92954b87de02"     # proposal 003: the run is void on any other prompt
PINNED_MODEL = "claude-opus-5"
BAND = 0.05
CONTROL_MIN_DIST = 0.15
N_CONTROLS = 22
BOOTSTRAP_N = 1000
SEED = 20260810

BRIEF = """\
Experiment E1 (proposal 003) of a personal discovery engine's lab. The LLM
scorer rates items 0-1 on six dimensions; a weighted final_score decides
notification against per-interest bars of 0.70-0.85. This generation
measures within-version reproducibility on band-proximate items (first
sample clearing the 15-item band gate) vs far-from-bar controls, under a
pinned prompt and model. Pre-registered intervals: overall
mean_abs_pairwise_delta <= 0.025 AND band mean_std <= 0.020; control
mean_std <= 0.015 AND control flip_rate <= 0.05; band flip_rate strictly
inside (0.10, 0.45) AND band mean_std <= 1.5x control mean_std. Run invalid
(not failed) if control mean_std < 0.003 (cache/session leak). Rollback:
band mean_std >= 0.045, band flip_rate >= 0.50, or overall
mean_abs_pairwise_delta >= 0.045."""


# --- item set (from the gen-2 rescore, per proposal 003) ---------------------

def band_and_controls(lab, cfg):
    """(rows, stratum_by_item_id) -- band = |gen2 score - bar| <= 0.05, all of
    them; controls = >= 0.15 away, N_CONTROLS sampled proportionally across
    interests. Frozen into state on first build so re-runs use the same set."""
    if "band_repeat_items" in lab.state:
        frozen = lab.state["band_repeat_items"]
    else:
        rescore = lab.state.get("rescore")
        if not rescore:
            sys.exit("gen-2 rescore state missing; band membership is defined on it")
        band, far_by_interest = [], {}
        conn = db_replay.open_ro(cfg.db_path)
        rows = {r["item_id"]: r for r in db_replay.scored_items(conn)}
        for key, entry in rescore.items():
            item_id = int(key)
            row = rows.get(item_id)
            if row is None:
                continue
            dist = abs(entry["new"] - entry["bar"])
            if dist <= BAND:
                band.append(item_id)
            elif dist >= CONTROL_MIN_DIST:
                far_by_interest.setdefault(row["interest_key"], []).append(item_id)

        total_far = sum(len(v) for v in far_by_interest.values())
        controls = []
        for key in sorted(far_by_interest):
            ids = sorted(far_by_interest[key])
            quota = max(1, round(N_CONTROLS * len(ids) / total_far))
            controls.extend(
                r for r in db_replay.spread_sample(
                    [{"item_id": i, "final_score": rescore[str(i)]["new"]} for i in ids],
                    quota)
            )
        controls = [c["item_id"] for c in controls][:N_CONTROLS]
        frozen = {"band": sorted(band), "control": sorted(controls)}
        lab.state["band_repeat_items"] = frozen
        lab.save()

    stratum = {i: "band" for i in frozen["band"]}
    stratum.update({i: "control" for i in frozen["control"]})
    return frozen, stratum


# --- measurement -------------------------------------------------------------

def pairwise_delta(finals):
    return round(statistics.mean(
        abs(a - b) for a, b in itertools.combinations(finals, 2)), 4)


def measure(lab, prov, rows, interests, repeats):
    results = []
    done = {r["item_id"] for g in lab.state["generations"]
            if g.get("kind") == "band_repeat" for r in g.get("items", [])}
    partial = lab.state.setdefault("band_repeat_partial", {})
    for row in rows:
        key = str(row["item_id"])
        if row["item_id"] in done:
            continue
        if key in partial:
            results.append(partial[key])
            continue
        item = db_replay.to_candidate(row)
        interest = interests[row["item_id"]]
        runs = []
        for k in range(repeats):
            result = lab.call(
                "score",
                lambda: prod_scorer.score_item(prov, item, interest,
                                               match_score=row["match_score"]),
                kind="band_repeat", item_id=row["item_id"], repeat=k)
            if result is None:
                continue
            if result.prompt_hash != PINNED_HASH:
                sys.exit(f"prompt hash {result.prompt_hash} != pinned {PINNED_HASH}; "
                         "run void per proposal 003")
            lab.log(call="score", kind="band_repeat", item_id=row["item_id"],
                    repeat=k, final=result.final_score, dims=result.dimensions)
            runs.append(result)
        if len(runs) < 2:
            continue
        finals = [r.final_score for r in runs]
        bar = row["min_score"]
        rec = {
            "item_id": row["item_id"],
            "interest_key": row["interest_key"],
            "bar": bar,
            "finals": finals,
            "mean": round(statistics.mean(finals), 4),
            "std": round(statistics.pstdev(finals), 4),
            "pairwise_delta": pairwise_delta(finals),
            "flip": len({f >= bar for f in finals}) > 1,
            "dim_std": {d: round(statistics.pstdev([r.dimensions[d] for r in runs]), 4)
                        for d in DIMENSIONS},
        }
        partial[key] = rec
        lab.save()
        results.append(rec)
    return results


def bootstrap_ci(values, statistic, rng):
    if not values:
        return None
    samples = sorted(
        statistic([rng.choice(values) for _ in values]) for _ in range(BOOTSTRAP_N)
    )
    return [round(samples[int(0.025 * BOOTSTRAP_N)], 4),
            round(samples[int(0.975 * BOOTSTRAP_N) - 1], 4)]


def stratum_metrics(items, rng):
    stds = [r["std"] for r in items]
    deltas = [r["pairwise_delta"] for r in items]
    flips = [r["flip"] for r in items]
    return {
        "n": len(items),
        "mean_std": round(statistics.mean(stds), 4),
        "max_std": round(max(stds), 4),
        "mean_std_ci95": bootstrap_ci(stds, statistics.mean, rng),
        "mean_abs_pairwise_delta": round(statistics.mean(deltas), 4),
        "mapd_ci95": bootstrap_ci(deltas, statistics.mean, rng),
        "flip_rate": round(sum(flips) / len(flips), 3),
        "flip_rate_ci95": bootstrap_ci(flips, lambda v: sum(v) / len(v), rng),
        "dim_noise": {d: round(statistics.mean(r["dim_std"][d] for r in items), 4)
                      for d in DIMENSIONS},
    }


def preregistered_checks(band, control, overall_mapd):
    checks = {
        "primary_mapd<=0.025": overall_mapd <= 0.025,
        "primary_band_mean_std<=0.020": band["mean_std"] <= 0.020,
        "secondary_control_mean_std<=0.015": control["mean_std"] <= 0.015,
        "secondary_control_flip_rate<=0.05": control["flip_rate"] <= 0.05,
        "third_band_flip_in_(0.10,0.45)": 0.10 < band["flip_rate"] < 0.45,
        "third_band_std<=1.5x_control": band["mean_std"] <= 1.5 * control["mean_std"],
    }
    verdicts = {
        "all_hold": all(checks.values()),
        "invalid_leak_detector": control["mean_std"] < 0.003,
        "rollback_band_mean_std>=0.045": band["mean_std"] >= 0.045,
        "rollback_band_flip_rate>=0.50": band["flip_rate"] >= 0.50,
        "rollback_mapd>=0.045": overall_mapd >= 0.045,
    }
    return checks, verdicts


def run_band_repeat(lab, args):
    cfg = load_cfg()
    prov = prod_scorer.provider()
    if prov.model != PINNED_MODEL:
        sys.exit(f"model {prov.model} != pinned {PINNED_MODEL}; run void")
    if scoring.prompt_fingerprint() != PINNED_HASH:
        sys.exit("production prompt changed since gen-2; re-pin before running")

    frozen, stratum = band_and_controls(lab, cfg)
    conn = db_replay.open_ro(cfg.db_path)
    wanted = set(stratum)
    rows = [r for r in db_replay.scored_items(conn) if r["item_id"] in wanted]
    by_id = {i.id: i for i in ddb.active_interests(conn)}
    interests = {r["item_id"]: by_id[r["interest_id"]] for r in rows}

    results = measure(lab, prov, rows, interests, args.repeats)
    band_items = [r for r in results if stratum.get(r["item_id"]) == "band"]
    control_items = [r for r in results if stratum.get(r["item_id"]) == "control"]
    if not band_items or not control_items:
        sys.exit("a stratum came back empty; nothing to report")

    rng = random.Random(SEED)
    band_m = stratum_metrics(band_items, rng)
    control_m = stratum_metrics(control_items, rng)
    overall_mapd = round(statistics.mean(r["pairwise_delta"] for r in results), 4)
    checks, verdicts = preregistered_checks(band_m, control_m, overall_mapd)

    agg = {
        "pinned": {"prompt_hash": PINNED_HASH, "model": PINNED_MODEL},
        "repeats": args.repeats,
        "band": band_m,
        "control": control_m,
        "overall_mean_abs_pairwise_delta": overall_mapd,
        "preregistered_checks": checks,
        "trigger_verdicts": verdicts,
    }
    record = {"kind": "band_repeat", "aggregate": agg, "items": results,
              "delta_vs_baseline": None}

    judged = council_judge(
        prov, lab, BRIEF,
        {"aggregate": agg,
         "band_gate": {"band_n": len(band_items), "gate": 15,
                       "admissible": len(band_items) >= 15}},
        "Question: which pre-registered intervals held, is the run valid per "
        "the leak detector, and per proposal 003's triggers should the change "
        "stand or revert? State the next open question.",
        kind="band_repeat")
    if judged:
        record["verdict"] = judged["verdict"]
        record["guidance"] = judged["guidance"]

    lab.state.pop("band_repeat_partial", None)
    lab.add_generation(record)
    print(json.dumps({k: record[k] for k in
                      ("gen", "kind", "aggregate", "verdict", "guidance")
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
    ap.add_argument("mode", choices=["band-repeat", "distribution", "report"])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--budget", type=int, default=300)
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
        run_band_repeat(lab, args)


if __name__ == "__main__":
    main()
