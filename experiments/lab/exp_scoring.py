"""E1 -- scorer stability & calibration (see LAB.md).

The notify decision lives in a 0.70-0.85 threshold band. This experiment
measures whether the production scorer is precise enough for that band to
mean anything, and whether its scores agree with recorded human verdicts.

    python experiments/lab/exp_scoring.py baseline [--items 10] [--repeats 3]
    python experiments/lab/exp_scoring.py variant anchored [--repeats 3]
    python experiments/lab/exp_scoring.py separation      # free, no LLM calls
    python experiments/lab/exp_scoring.py report

baseline: score a spread of stored items K times each with the production
prompt -> per-item std of final_score (jitter), flip rate of the notify
decision, drift vs the stored score, per-dimension noise. The sampled item
ids persist in state.json so later variants re-score the same items.

variant: same items, same metrics, under a named prompt variant; deltas vs
the baseline generation are the result.

Jitter runs score with an empty feedback block: production feedback context
varies over time and would confound repeat-to-repeat variance.
"""
import argparse
import json
import statistics
import sys

import db_replay
import prod_scorer
from lab_common import Lab, council_judge, utf8_streams

from discovery import scoring  # noqa: E402
from discovery.config import load as load_cfg  # noqa: E402
from discovery.models import DIMENSIONS  # noqa: E402

POSITIVE = ("fire", "up")
NEGATIVE = ("down", "trash")

BRIEF = """\
Experiment E1 of a personal discovery engine's lab. The engine's LLM scorer
rates candidate items 0-1 on six dimensions; a weighted final_score decides
notification against per-interest bars of 0.70-0.85. We measure: jitter (std
of final_score when the SAME item is scored repeatedly -- above ~0.05 the
band decisions are noise), notify-decision flip rate, drift vs the stored
production score, per-dimension noise, and separation between items the owner
rated positively vs negatively."""

# Calibration anchors inserted into the production prompt. The hypothesis:
# absolute anchors reduce repeat-to-repeat variance without changing ranking.
ANCHORS = """\
Calibration anchors for every dimension: 0.1 = clearly absent; 0.3 = weak or
incidental; 0.5 = genuinely middling; 0.7 = strong, would hold up under
scrutiny; 0.9 = exceptional, top few percent for this interest. Anchor each
rating to these absolute definitions, not to other items you have seen, and
rate in steps of 0.05.

"""


def _anchored_prompt():
    marker = "Also return:"
    assert marker in scoring.PROMPT, "production prompt changed; re-derive variant"
    return scoring.PROMPT.replace(marker, ANCHORS + marker)


VARIANTS = {
    "anchored": lambda: {"prompt": _anchored_prompt()},
}


# --- measurement -------------------------------------------------------------

def score_repeats(lab, prov, rows, interests, repeats, variant=None, kind="baseline"):
    """Score each sampled row `repeats` times; returns per-item records.
    `interests` maps item_id -> full Interest definition (the re-score must
    show the scorer exactly what production shows it)."""
    overrides = VARIANTS[variant]() if variant else {}
    results = []
    for row in rows:
        item = db_replay.to_candidate(row)
        interest = interests[row["item_id"]]
        runs = []
        for k in range(repeats):
            def one():
                with prod_scorer.prompt_variant(**overrides):
                    return prod_scorer.score_item(
                        prov, item, interest, match_score=row["match_score"]
                    )
            result = lab.call("score", one, kind=kind, item_id=row["item_id"], repeat=k)
            if result is None:
                continue
            lab.log(call="score", kind=kind, item_id=row["item_id"], repeat=k,
                    final=result.final_score, dims=result.dimensions,
                    reason=result.reason)
            runs.append(result)
        if not runs:
            continue
        finals = [r.final_score for r in runs]
        bar = row["min_score"]
        results.append({
            "item_id": row["item_id"],
            "interest_key": row["interest_key"],
            "title": row["title"][:80],
            "bar": bar,
            "stored": row["final_score"],
            "finals": finals,
            "mean": round(statistics.mean(finals), 4),
            "std": round(statistics.pstdev(finals), 4),
            "spread": round(max(finals) - min(finals), 4),
            "notify_flip": len({f >= bar for f in finals}) > 1,
            "drift": round(abs(statistics.mean(finals) - row["final_score"]), 4),
            "dim_std": {
                d: round(statistics.pstdev([r.dimensions[d] for r in runs]), 4)
                for d in DIMENSIONS
            },
        })
    return results


def aggregate(results):
    if not results:
        return {}
    stds = [r["std"] for r in results]
    return {
        "items": len(results),
        "mean_std": round(statistics.mean(stds), 4),
        "max_std": round(max(stds), 4),
        "flip_rate": round(sum(r["notify_flip"] for r in results) / len(results), 2),
        "mean_drift": round(statistics.mean(r["drift"] for r in results), 4),
        "dim_noise": {
            d: round(statistics.mean(r["dim_std"][d] for r in results), 4)
            for d in DIMENSIONS
        },
    }


def distribution(conn):
    """Corpus score-vs-bar readout (free): per-interest notify_rate, band
    density, and per-dimension distinct-value counts (proposal 001's routing
    readout -- decides bar fitting vs band-proximate prompt testing)."""
    rows = db_replay.scored_items(conn)
    per_interest = {}
    for r in rows:
        b = per_interest.setdefault(
            r["interest_key"], {"n": 0, "notify": 0, "band": 0, "bar": r["min_score"]}
        )
        b["n"] += 1
        b["notify"] += r["final_score"] >= r["min_score"]
        b["band"] += abs(r["final_score"] - r["min_score"]) <= 0.05
    for b in per_interest.values():
        b["notify_rate"] = round(b["notify"] / b["n"], 3)
        b["band_density"] = round(b["band"] / b["n"], 3)
    total = len(rows)
    return {
        "corpus_n": total,
        "notify_rate": round(sum(r["final_score"] >= r["min_score"] for r in rows) / total, 3),
        "band_density": round(
            sum(abs(r["final_score"] - r["min_score"]) <= 0.05 for r in rows) / total, 3),
        "band_count": sum(abs(r["final_score"] - r["min_score"]) <= 0.05 for r in rows),
        "per_interest": per_interest,
        "dim_distinct_values": {
            d: len({round(r[d], 2) for r in rows}) for d in DIMENSIONS
        },
    }


def run_rescore(lab, args):
    """Proposal 001's validating run: one fresh production-scored pass over
    the whole stored corpus. Drift strata are honest about what exists: every
    stored row predates prompt stamping, so `unstamped_model_match` is the
    closest available proxy for within-version and is labeled as such, never
    presented as the strict stratum. Partial runs resume from state."""
    cfg = load_cfg()
    prov = prod_scorer.provider()
    current_model = prov.model
    current_hash = scoring.prompt_fingerprint()

    conn, rows = full_rows(cfg)
    pairs = _attach_interests(cfg, rows)
    done = lab.state.setdefault("rescore", {})

    for row, interest in pairs:
        key = str(row["item_id"])
        if key in done:
            continue
        item = db_replay.to_candidate(row)

        def one():
            return prod_scorer.score_item(prov, item, interest, match_score=row["match_score"])

        result = lab.call("score", one, kind="rescore", item_id=row["item_id"])
        if result is None:
            continue
        stratum = ("unstamped_model_match" if row["score_model"] == current_model
                   else "unstamped_model_mismatch")
        done[key] = {
            "stored": row["final_score"], "new": result.final_score,
            "drift": round(abs(result.final_score - row["final_score"]), 4),
            "bar": row["min_score"], "stratum": stratum,
            "flip": (result.final_score >= row["min_score"]) != (row["final_score"] >= row["min_score"]),
            "dims": result.dimensions,
        }
        lab.save()
        lab.log(call="score", kind="rescore", item_id=row["item_id"],
                stored=row["final_score"], new=result.final_score, stratum=stratum)

    by_stratum = {}
    for d in done.values():
        by_stratum.setdefault(d["stratum"], []).append(d["drift"])
    agg = {
        "coverage": round(len(done) / len(pairs), 3) if pairs else 0,
        "corpus_n": len(pairs),
        "rescored": len(done),
        "current_model": current_model,
        "current_prompt_hash": current_hash,
        "strict_within_version_n": 0,  # no stored row carries a prompt stamp
        "drift_by_stratum": {
            s: {"n": len(v), "mean": round(statistics.mean(v), 4),
                "median": round(statistics.median(v), 4)}
            for s, v in by_stratum.items()
        },
        "mean_drift_overall": round(
            statistics.mean(d["drift"] for d in done.values()), 4) if done else None,
        "notify_flips": sum(d["flip"] for d in done.values()),
        "distribution": distribution(conn),
    }
    record = lab.add_generation({"kind": "rescore", "aggregate": agg})
    print(json.dumps({k: record[k] for k in ("gen", "kind", "aggregate")},
                     ensure_ascii=False, indent=1))
    print(f"budget used: {lab.state['budget_used']}/{lab.budget_cap}")


def separation(conn):
    """Stored-score separation between positive and negative verdicts. Free:
    uses scores already in the DB."""
    rows = db_replay.feedback_rows(conn)
    pos = [r for r in rows if r["verdict"] in POSITIVE]
    neg = [r for r in rows if r["verdict"] in NEGATIVE]

    def score_of(r):
        return r["final_score"] if r["final_score"] is not None else r["original_score"]

    pos_scores = [score_of(r) for r in pos if score_of(r) is not None]
    neg_scores = [score_of(r) for r in neg if score_of(r) is not None]
    out = {"n_positive": len(pos_scores), "n_negative": len(neg_scores)}
    if pos_scores and neg_scores:
        out.update({
            "mean_positive": round(statistics.mean(pos_scores), 4),
            "mean_negative": round(statistics.mean(neg_scores), 4),
            "gap": round(statistics.mean(pos_scores) - statistics.mean(neg_scores), 4),
            # Fraction of pos/neg pairs ranked correctly (AUC); 0.5 = chance.
            "auc": round(
                statistics.mean(
                    (p > q) + 0.5 * (p == q) for p in pos_scores for q in neg_scores
                ), 3),
        })
    return out


# --- interest resolution needs full definitions for a fair re-score ----------

def full_rows(cfg, sample_ids=None):
    """Sampled rows, but with the FULL interest definition attached (the jitter
    re-score must show the scorer exactly what production shows it)."""
    conn = db_replay.open_ro(cfg.db_path)
    rows = db_replay.scored_items(conn)
    if sample_ids is not None:
        rows = [r for r in rows if r["item_id"] in sample_ids]
    return conn, rows


def _attach_interests(cfg, rows):
    from discovery import db as ddb
    conn = db_replay.open_ro(cfg.db_path)
    by_id = {i.id: i for i in ddb.active_interests(conn)}
    out = []
    for row in rows:
        interest = by_id.get(row["interest_id"])
        if interest is not None:
            out.append((row, interest))
    return out


def run_measurement(lab, args, variant=None):
    cfg = load_cfg()
    prov = prod_scorer.provider()
    kind = f"variant:{variant}" if variant else "baseline"

    sample_ids = lab.state.get("sample_ids")
    conn, rows = full_rows(cfg, set(sample_ids) if sample_ids else None)
    if sample_ids is None:
        rows = db_replay.spread_sample(rows, args.items)
        lab.state["sample_ids"] = [r["item_id"] for r in rows]
        lab.save()

    pairs = _attach_interests(cfg, rows)
    interests = {row["item_id"]: interest for row, interest in pairs}
    results = score_repeats(
        lab, prov, [row for row, _ in pairs], interests, args.repeats, variant, kind
    )

    agg = aggregate(results)
    sep = separation(conn)
    record = {"kind": kind, "repeats": args.repeats, "aggregate": agg,
              "separation": sep, "items": results}

    baseline = next((g for g in lab.state["generations"] if g["kind"] == "baseline"), None)
    if variant and baseline:
        record["delta_vs_baseline"] = {
            k: round(agg[k] - baseline["aggregate"][k], 4)
            for k in ("mean_std", "flip_rate", "mean_drift")
            if k in agg and k in baseline.get("aggregate", {})
        }

    judged = council_judge(
        prov, lab, BRIEF,
        {"aggregate": agg, "separation": sep, "delta_vs_baseline": record.get("delta_vs_baseline"),
         "noisiest_items": sorted(results, key=lambda r: -r["std"])[:3]},
        "Question: is the scorer stable enough for its threshold band, does it "
        "separate the owner's verdicts, and what should the next generation of "
        "this experiment change (prompt variant to try, or a different "
        "experiment if scoring is not the bottleneck)?",
        kind=kind,
    )
    if judged:
        record["verdict"] = judged["verdict"]
        record["guidance"] = judged["guidance"]

    lab.add_generation(record)
    print(json.dumps({k: record[k] for k in
                      ("kind", "aggregate", "separation", "delta_vs_baseline",
                       "verdict", "guidance") if k in record},
                     ensure_ascii=False, indent=1))
    print(f"budget used: {lab.state['budget_used']}/{lab.budget_cap}")


def main():
    utf8_streams()
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["baseline", "variant", "separation", "report",
                                     "distribution", "rescore"])
    ap.add_argument("name", nargs="?", help="variant name (for mode=variant)")
    ap.add_argument("--items", type=int, default=10)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--budget", type=int, default=40)
    args = ap.parse_args()

    lab = Lab("scoring", budget_cap=args.budget)

    if args.mode == "separation":
        cfg = load_cfg()
        print(json.dumps(separation(db_replay.open_ro(cfg.db_path)), indent=1))
    elif args.mode == "distribution":
        cfg = load_cfg()
        print(json.dumps(distribution(db_replay.open_ro(cfg.db_path)), indent=1))
    elif args.mode == "rescore":
        run_rescore(lab, args)
    elif args.mode == "report":
        for g in lab.state["generations"]:
            print(json.dumps({k: g.get(k) for k in
                              ("gen", "ts", "kind", "aggregate", "separation",
                               "delta_vs_baseline", "verdict", "guidance")},
                             ensure_ascii=False, indent=1))
        print(f"budget used: {lab.state['budget_used']}/{lab.budget_cap}")
    elif args.mode == "variant":
        if args.name not in VARIANTS:
            sys.exit(f"unknown variant {args.name!r}; have: {sorted(VARIANTS)}")
        run_measurement(lab, args, variant=args.name)
    else:
        run_measurement(lab, args)


if __name__ == "__main__":
    main()
