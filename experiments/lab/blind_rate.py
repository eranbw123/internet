"""Blind stratified owner-verdict batch -- proposal 002.

Builds a frozen, evaluation-only label batch from the gen-2 rescore corpus
(all band items, all remaining above-bar items, below-bar items overweighting
the three near-zero-notify interests), presents each item BLIND (title +
snippet + url only -- never score, dimensions, bar, notify status, or
interest), and records one binary verdict per item through db.add_feedback --
the lab's single write path into discovery.db. ~10% duplicate re-shows are
inserted later in the sequence, tracked in the manifest and the responses log
only, invisible to the rater and never written as second feedback rows.

The batch is frozen as a test split: it evaluates the scorer, it never fits
bars or prompts in the generation that produced it (proposal 002).

Usage (from the repo root):
    python experiments/lab/blind_rate.py             # rate interactively (y/n/s/q)
    python experiments/lab/blind_rate.py --dry-run   # build manifest + print blind listing, no feedback writes
    python experiments/lab/blind_rate.py --metrics   # code-computed metrics once labels exist
    python experiments/lab/blind_rate.py --metrics --record
                                                     # also persist the scorecard to scoring state.json
"""
import argparse
import json
import random
import sys

import db_replay
from lab_common import ARTIFACTS, Lab, now, utf8_streams

from discovery import db  # noqa: E402
from discovery.config import load as load_cfg  # noqa: E402

SEED = 20260809          # fixed: order, sampling, and bootstrap are reproducible
BAND = 0.05              # |final_score - bar| <= BAND
OVERWEIGHT = ("behavioral-psychology", "personal-knowledge-learning",
              "emdr-trauma-therapy")
N_OVERWEIGHT_EACH = 7    # below-bar per overweighted interest
N_OTHER_EACH = 3         # below-bar per remaining interest
N_DUPES = 8              # ~10% re-shows for intra-rater agreement
NOTE = "blind-batch-002"
STAMP = {"model": "claude-opus-5", "prompt_hash": "92954b87de02"}
MIN_LABELS = 15          # the 15+15 separation gate
BOOTSTRAP_N = 2000

BATCH_DIR = ARTIFACTS / "blind_batch_002"
MANIFEST_PATH = BATCH_DIR / "manifest.json"
RESPONSES_PATH = BATCH_DIR / "responses.jsonl"
SCORING_STATE = ARTIFACTS / "scoring" / "state.json"


# --- batch construction ------------------------------------------------------

def build_manifest(cfg):
    scoring = json.loads(SCORING_STATE.read_text(encoding="utf-8"))
    rescore = scoring["rescore"]
    flip_ids = sorted(int(k) for k, v in rescore.items() if v.get("flip"))

    ro = db_replay.open_ro(cfg.db_path)
    corpus = {r["item_id"]: r for r in db_replay.scored_items(ro)
              if str(r["item_id"]) in rescore}

    strata = {"band": [], "above_bar": [], "below_bar": []}
    by_interest_below = {}
    for item_id in sorted(corpus):
        r = corpus[item_id]
        delta = r["final_score"] - r["min_score"]
        if abs(delta) <= BAND:
            strata["band"].append(item_id)
        elif delta > 0:
            strata["above_bar"].append(item_id)
        else:
            by_interest_below.setdefault(r["interest_key"], []).append(r)

    below = []
    for key in sorted(by_interest_below):
        n = N_OVERWEIGHT_EACH if key in OVERWEIGHT else N_OTHER_EACH
        # spread_sample is deterministic and spans the score range, so the
        # below stratum stresses the whole scale, not just near-bar scores
        # (range restriction is a named overfitting risk in the proposal).
        below.extend(r["item_id"] for r in
                     db_replay.spread_sample(by_interest_below[key], n))
    strata["below_bar"] = sorted(below)

    unique = strata["band"] + strata["above_bar"] + strata["below_bar"]
    rng = random.Random(SEED)
    order = sorted(unique)
    rng.shuffle(order)

    # Duplicates: originals from the first half, re-shown at a random point
    # in the second half of the final sequence.
    sequence = [{"item_id": i, "dup": False} for i in order]
    for item_id in rng.sample(order[: len(order) // 2], N_DUPES):
        pos = rng.randrange(len(sequence) // 2, len(sequence) + 1)
        sequence.insert(pos, {"item_id": item_id, "dup": True})
    for pos, entry in enumerate(sequence):
        entry["pos"] = pos

    duplicate_pairs = []
    first_pos = {}
    for entry in sequence:
        if not entry["dup"]:
            first_pos[entry["item_id"]] = entry["pos"]
        else:
            duplicate_pairs.append({"item_id": entry["item_id"],
                                    "first_pos": first_pos[entry["item_id"]],
                                    "dup_pos": entry["pos"]})

    manifest = {
        "created": now(),
        "seed": SEED,
        "stamp": STAMP,          # labels stay attributable to these scores
        "note": NOTE,
        "frozen": "evaluation-only test split (proposal 002): never used to "
                  "fit bars or edit prompts in the generation that produced it",
        "strata": strata,
        "flip_item_ids": flip_ids,
        "items": {
            str(i): {
                "title": corpus[i]["title"],
                "url": corpus[i]["url"],
                "snippet": (corpus[i]["text"] or "")[:400],
                "interest_id": corpus[i]["interest_id"],
                "interest_key": corpus[i]["interest_key"],
                "final_score": corpus[i]["final_score"],
                "min_score": corpus[i]["min_score"],
            }
            for i in unique
        },
        "sequence": sequence,
        "duplicate_pairs": duplicate_pairs,
    }
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    return manifest


def load_manifest(cfg):
    # The batch is frozen: once built it is never resampled.
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return build_manifest(cfg)


# --- rating ------------------------------------------------------------------

def show_blind(entry, item, total):
    # BLIND by construction: no score, no dimensions, no bar, no notify
    # status, no interest key, no item id (an id would make re-shows obvious).
    print(f"\n--- {entry['pos'] + 1} / {total} ---")
    print(f"  {item['title']}")
    if item["snippet"]:
        print(f"  {item['snippet']}")
    print(f"  {item['url']}")


def answered_positions():
    if not RESPONSES_PATH.exists():
        return {}
    out = {}
    with RESPONSES_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            out[rec["pos"]] = rec
    return out


def log_response(rec):
    with RESPONSES_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now(), **rec}, ensure_ascii=False) + "\n")


def interactive(cfg, manifest):
    conn = db.connect(cfg.db_path)
    done = answered_positions()
    total = len(manifest["sequence"])
    print("Blind batch 002 -- would you want this surfaced to you?")
    print("  y = yes   n = no   s = skip   q = quit (resume later)")
    rated = 0
    for entry in manifest["sequence"]:
        if entry["pos"] in done:
            continue
        item = manifest["items"][str(entry["item_id"])]
        show_blind(entry, item, total)
        while True:
            answer = input("  verdict [y/n/s/q]> ").strip().lower()
            if answer in ("y", "n", "s", "q"):
                break
            print("  one of: y n s q")
        if answer == "q":
            break
        reason = ""
        if answer in ("y", "n"):
            reason = input("  reason (enter to skip)> ").strip()
        log_response({"pos": entry["pos"], "item_id": entry["item_id"],
                      "dup": entry["dup"], "verdict": answer, "reason": reason})
        if answer in ("y", "n") and not entry["dup"]:
            # Duplicate re-shows land in the responses log only -- one
            # feedback row per item, ever.
            db.add_feedback(
                conn, entry["item_id"], item["interest_id"],
                "up" if answer == "y" else "down",
                note=NOTE + (f": {reason}" if reason else ""),
                original_score=item["final_score"],  # evaluated score; never shown
            )
        rated += 1
    labels = primary_labels(cfg, manifest)
    n_pos = sum(labels.values())
    n_neg = len(labels) - n_pos
    print(f"\nthis sitting: {rated} answers | stored verdicts: "
          f"{n_pos} positive, {n_neg} negative (gate: >= {MIN_LABELS}+{MIN_LABELS})")
    if n_pos >= MIN_LABELS and n_neg >= MIN_LABELS:
        print("gate cleared -- run: python experiments/lab/blind_rate.py --metrics")


# --- metrics -----------------------------------------------------------------

def primary_labels(cfg, manifest):
    """{item_id: 1|0} from feedback rows carrying this batch's note. Latest
    row per item wins; rows are read back mode=ro."""
    ro = db_replay.open_ro(cfg.db_path)
    in_batch = {int(i) for i in manifest["items"]}
    labels = {}
    for r in db_replay.feedback_rows(ro):  # ordered by feedback id
        if (r["note"] or "").startswith(NOTE) and r["item_id"] in in_batch \
                and r["verdict"] in ("up", "down"):
            labels[r["item_id"]] = 1 if r["verdict"] == "up" else 0
    return labels


def auc(pairs):
    """Mann-Whitney AUC for [(score, label)] with ties at 0.5."""
    pos = [s for s, y in pairs if y == 1]
    neg = [s for s, y in pairs if y == 0]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def bootstrap_ci(pairs, rng):
    """Percentile 95% CI from BOOTSTRAP_N resamples (single-class resamples
    are redrawn)."""
    values = []
    while len(values) < BOOTSTRAP_N:
        sample = [rng.choice(pairs) for _ in pairs]
        a = auc(sample)
        if a is not None:
            values.append(a)
    values.sort()
    return values[int(0.025 * BOOTSTRAP_N)], values[int(0.975 * BOOTSTRAP_N) - 1]


def duplicate_agreement(manifest):
    """(agreement or None, n_pairs_rated) from the responses log -- verdicts
    on both showings of each duplicate pair."""
    by_pos = answered_positions()
    rated = agree = 0
    for pair in manifest["duplicate_pairs"]:
        first = by_pos.get(pair["first_pos"])
        second = by_pos.get(pair["dup_pos"])
        if not first or not second:
            continue
        if first["verdict"] in ("y", "n") and second["verdict"] in ("y", "n"):
            rated += 1
            agree += first["verdict"] == second["verdict"]
    return (agree / rated if rated else None), rated


def metrics(cfg, manifest, record):
    labels = primary_labels(cfg, manifest)
    items = manifest["items"]
    pairs = [(items[str(i)]["final_score"], y) for i, y in labels.items()]
    n_pos = sum(y for _, y in pairs)
    n_neg = len(pairs) - n_pos
    print(f"labels: {len(pairs)} ({n_pos} positive, {n_neg} negative)")

    scorecard = {"kind": "blind_labels_002", "n_pos": n_pos, "n_neg": n_neg,
                 "stamp": STAMP, "delta_vs_baseline": None,
                 "measurement_only": True}

    # (5) intra-rater agreement first: below 0.80 is the rollback trigger and
    # suppresses any AUC claim (labels unusable).
    agreement, n_dup_rated = duplicate_agreement(manifest)
    scorecard["dup_agreement"] = agreement
    scorecard["dup_pairs_rated"] = n_dup_rated
    if agreement is None:
        print("intra-rater agreement: GATE NOT MET -- no duplicate pair has "
              "verdicts on both showings yet")
    else:
        print(f"intra-rater agreement: {agreement:.2f} on {n_dup_rated} duplicate pairs")
        if agreement < 0.80:
            print("ROLLBACK TRIGGER: intra-rater agreement < 0.80 -- labels "
                  "unusable, redesign the label protocol; no AUC is reported")

    # (1) AUC + bootstrap CI, only past the 15+15 gate and the agreement trigger.
    labels_usable = agreement is None or agreement >= 0.80
    if n_pos < MIN_LABELS or n_neg < MIN_LABELS:
        print(f"AUC: GATE NOT MET -- need >= {MIN_LABELS} positive AND >= "
              f"{MIN_LABELS} negative verdicts; extend the batch rather than report")
        scorecard["auc_gate"] = "unmet"
    elif not labels_usable:
        scorecard["auc_gate"] = "suppressed_by_agreement_trigger"
    else:
        rng = random.Random(SEED + 1)
        point = auc(pairs)
        lo, hi = bootstrap_ci(pairs, rng)
        scorecard["separation"] = {"auc": point, "ci95": [lo, hi],
                                   "bootstrap_n": BOOTSTRAP_N}
        print(f"AUC (stored final_score vs verdict): {point:.3f} "
              f"[95% CI {lo:.3f}, {hi:.3f}] ({BOOTSTRAP_N} bootstrap resamples)")
        # Decision rule fixed in advance (proposal 002 validation plan).
        if lo > 0.55:
            print("decision rule: CI lower bound > 0.55 -> next generation is "
                  "per-interest bar fitting on a calibration/held-out label split")
        else:
            print("ROLLBACK TRIGGER: CI includes 0.5 or lower bound <= 0.55 -> "
                  "scorer/dimension redesign (start: personal_relevance); "
                  "bar fitting is blocked")

    # (2) band-subset AUC only if the subset independently clears 15+15.
    band_ids = set(manifest["strata"]["band"])
    band_pairs = [(items[str(i)]["final_score"], y)
                  for i, y in labels.items() if i in band_ids]
    band_pos = sum(y for _, y in band_pairs)
    band_neg = len(band_pairs) - band_pos
    if band_pos >= MIN_LABELS and band_neg >= MIN_LABELS and labels_usable:
        b = auc(band_pairs)
        blo, bhi = bootstrap_ci(band_pairs, random.Random(SEED + 2))
        scorecard["band_separation"] = {"auc": b, "ci95": [blo, bhi]}
        print(f"band-subset AUC: {b:.3f} [95% CI {blo:.3f}, {bhi:.3f}]")
    else:
        print(f"band subset: separation GATE NOT MET (needs its own "
              f"{MIN_LABELS}+{MIN_LABELS}); descriptive count only: "
              f"{band_pos} positive / {len(band_pairs)} labeled band items")
        scorecard["band_positive"] = [band_pos, len(band_pairs)]

    # (3) owner-positive rate per interest, exact counts vs bar.
    print("\nowner-positive rate per interest:")
    per = {}
    for i, y in labels.items():
        key = items[str(i)]["interest_key"]
        pos_n, tot_n = per.get(key, (0, 0))
        per[key] = (pos_n + y, tot_n + 1)
    scorecard["per_interest_positive"] = {}
    for key in sorted(per):
        pos_n, tot_n = per[key]
        bar = next(items[str(i)]["min_score"] for i in labels
                   if items[str(i)]["interest_key"] == key)
        scorecard["per_interest_positive"][key] = [pos_n, tot_n]
        print(f"  {key:32s} {pos_n:2d}/{tot_n:2d} positive (bar {bar:.2f})")
    over_pos = sum(per.get(k, (0, 0))[0] for k in OVERWEIGHT)
    over_tot = sum(per.get(k, (0, 0))[1] for k in OVERWEIGHT)
    if over_tot:
        print(f"  near-zero-notify trio: {over_pos}/{over_tot} positive "
              f"(secondary prediction: rate >= 0.15 of their labeled items)")

    # (4) owner verdicts on the 14 notify-flip items.
    print("\nverdicts on notify-flip items (drift sign, measured not assumed):")
    flip_labeled = {}
    for i in manifest["flip_item_ids"]:
        verdict = labels.get(i)
        text = {1: "yes", 0: "no"}.get(verdict, "unlabeled")
        if verdict is not None:
            flip_labeled[i] = verdict
        in_batch = str(i) in items
        print(f"  item {i}: {text}" + ("" if in_batch else " (not in batch)"))
    scorecard["flip_verdicts"] = flip_labeled

    if record:
        lab = Lab("scoring")
        lab.add_generation(scorecard)
        print("\nscorecard recorded to scoring state.json")


# --- main --------------------------------------------------------------------

def main():
    utf8_streams()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="build manifest + print the blind listing; no feedback writes")
    ap.add_argument("--metrics", action="store_true",
                    help="compute proposal-002 metrics from stored labels")
    ap.add_argument("--record", action="store_true",
                    help="with --metrics: persist the scorecard to scoring state.json")
    args = ap.parse_args()

    cfg = load_cfg()
    manifest = load_manifest(cfg)

    if args.metrics:
        metrics(cfg, manifest, args.record)
        return
    if args.dry_run:
        total = len(manifest["sequence"])
        for entry in manifest["sequence"]:
            show_blind(entry, manifest["items"][str(entry["item_id"])], total)
        strata = manifest["strata"]
        print(f"\n{len(manifest['items'])} unique items "
              f"(band {len(strata['band'])}, above-bar {len(strata['above_bar'])}, "
              f"below-bar {len(strata['below_bar'])}), "
              f"{len(manifest['duplicate_pairs'])} duplicate re-shows, "
              f"{total} presentations; manifest: {MANIFEST_PATH}")
        return
    interactive(cfg, manifest)


if __name__ == "__main__":
    sys.exit(main())
