"""E2 -- discovery-yield A/B (proposal 005): collection vs scoring failure.

Three interests notify ~never (behavioral-psychology 0/18,
personal-knowledge-learning 0/11, emdr-trauma-therapy 0-1/13). Label-free
discriminator: collect a fresh window per interest under two arms -- S, the
production static web_search template; A, strategist-generated angles where
the strategist sees ONLY the owner-written interest definition (never the
scoring rubric, dimension names, bar values, or past scores) -- then score
every net-new item once with the frozen production scorer and compare
above-bar yield and score distributions. nbis-nebius runs as a saturated
positive control, excluded from the decision criteria.

    python experiments/lab/exp_discovery.py run [--budget 220]
    python experiments/lab/exp_discovery.py report

Carried diagnostic (non-decisive, n=2): items 40 and 113 re-scored 3x,
max |delta| reported; >= 0.08 voids the frozen-scorer assumption.
"""
import argparse
import json
import re
import statistics
import sys

import db_replay
import prod_scorer
from lab_common import Lab, council_judge, utf8_streams

from discovery import db as ddb  # noqa: E402
from discovery import scoring  # noqa: E402
from discovery.collectors import _search, web_search  # noqa: E402
from discovery.config import load as load_cfg  # noqa: E402
from discovery.providers import get_provider  # noqa: E402

PINNED_HASH = "92954b87de02"
PINNED_MODEL = "claude-opus-5"
STARVED = ("behavioral-psychology", "personal-knowledge-learning", "emdr-trauma-therapy")
CONTROL = "nbis-nebius"
TARGET_NEW = 15          # net-new items wanted per interest-arm
MIN_NEW = 12             # validity guard: fewer -> run void for that comparison
MAX_ANGLES = 5
BAND = 0.05
PROBE_ITEMS = (40, 113)
PROBE_CEILING = 0.08
# The scorer's vocabulary; strategist output containing any of these (or a
# bar value) breaches the Goodhart firewall and voids the run.
FIREWALL_TERMS = ("personal_relevance", "novelty", "depth", "specificity",
                  "importance", "surprise", "min_score", "final_score")

BRIEF = """\
Experiment E2 (proposal 005) of a personal discovery engine's lab. Three
interests notify ~never; this A/B discriminates collection failure from
scoring failure without owner labels. Arm S = production static search
template; Arm A = strategist-written angles (firewalled from the rubric,
dimensions, and bars). Every net-new item scored once by the frozen scorer
(hash-pinned). Pre-registered, on the three starved interests only:
PRIMARY Arm A above-bar count >= 4 AND > Arm S's; SECONDARY pooled p90(A) -
p90(S) >= 0.04. Both hold -> angles become the production collector. One
holds -> re-run before adopting. Neither AND p90 gap < 0.02 -> retrieval
hypothesis falsified; the starvation is a scorer/bar property; lab goes
idle until labels. Void (not negative) if: net-new < 12 for an interest-arm,
cross-arm URL overlap >= 0.50, firewall breach, or probe max|delta| >= 0.08."""

STRATEGIST_SYSTEM = (
    "You design web-search instructions that surface high-value content for "
    "one person's stated interest. You answer in strict JSON only."
)

# Firewall: the strategist gets the owner-written interest definition and
# nothing else -- no rubric, no dimension names, no bars, no past scores.
STRATEGIST_PROMPT = """\
Interest: {title}
Description: {description}
Worth surfacing: {positive}
Not worth surfacing: {negative}

A separate search-capable LLM will execute each prompt you write and return
recent, substantive web content for this interest. Design {k} search prompts,
each attacking discovery from a genuinely different angle (different source
types, communities, primary-source routes, query strategies). Be specific to
this interest: name real venues, journals, communities, or people where
useful. For each angle give a short label and the full prompt text.

Output ONLY this JSON object:
{{"angles": [{{"label": "...", "prompt": "..."}}]}}"""

STRATEGIST_SCHEMA = {"type": "object", "required": ["angles"],
                     "properties": {"angles": {"type": "array"}}}


def canon_url(url):
    return re.sub(r"[#?].*$", "", str(url or "").lower()).rstrip("/")


def canon_title(title):
    return re.sub(r"[^a-z0-9]+", "", str(title or "").lower())


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
    return round(values[idx], 4)


def collect_arm_s(interest, cfg, provider, lab):
    """Production template, limit raised to TARGET_NEW for arm parity
    (production limit is 6-7; recorded as a deviation in the proposal)."""
    interest.source_config = dict(interest.source_config)
    ws = dict(interest.source_config.get("web_search", {}))
    ws["limit"] = TARGET_NEW
    interest.source_config["web_search"] = ws
    return lab.call("collect_s", lambda: web_search.collect(interest, cfg, provider),
                    interest=interest.key) or []


def collect_arm_a(interest, cfg, provider, lab):
    sprompt = STRATEGIST_PROMPT.format(
        title=interest.title, description=interest.description,
        positive=", ".join(interest.positive_signals) or "(unspecified)",
        negative=", ".join(interest.negative_signals) or "(unspecified)",
        k=MAX_ANGLES)
    result = lab.call("strategist", lambda: provider.complete_json(
        STRATEGIST_SYSTEM, sprompt, STRATEGIST_SCHEMA), interest=interest.key)
    if not result:
        return [], None
    angles = [a for a in result.get("angles", [])
              if isinstance(a, dict) and a.get("prompt")][:MAX_ANGLES]
    lab.log(call="strategist", interest=interest.key, prompt=sprompt, raw=result)

    blob = json.dumps(result).lower()
    if any(t in blob for t in FIREWALL_TERMS):
        return [], "firewall_breach"

    items = []
    for angle in angles:
        prompt = (f"{angle['prompt']}\n\nReturn AT MOST 8 items as a JSON array.\n"
                  f"{_search.RESULT_SPEC}")
        raw = lab.call("collect_a", lambda p=prompt: provider.search_json(p, max_searches=3),
                       interest=interest.key, angle=angle.get("label"))
        if raw:
            got = _search.to_items(raw, "web_search", 8)
            lab.log(call="collect_a", interest=interest.key,
                    angle=angle.get("label"), n_items=len(got))
            items.extend(got)
        if len(items) >= TARGET_NEW * 2:
            break
    return items, None


def dedupe(items, seen_urls, seen_titles):
    out = []
    for it in items:
        u, t = canon_url(it.url), canon_title(it.title)
        if not u or u in seen_urls or (t and t in seen_titles):
            continue
        seen_urls.add(u)
        seen_titles.add(t)
        out.append(it)
    return out


def arm_metrics(scored, bar):
    finals = [s for s, _ in scored]
    return {
        "n_scored": len(finals),
        "above_bar": sum(f >= bar for f in finals),
        "band": sum(abs(f - bar) <= BAND for f in finals),
        "median": percentile(finals, 0.5), "p75": percentile(finals, 0.75),
        "p90": percentile(finals, 0.90), "max": max(finals) if finals else None,
        "dist_to_bar": sorted(round(f - bar, 3) for f in finals),
    }


def probe(lab, prov, conn):
    """Non-decisive frozen-scorer check: items 40 & 113, 3x, max |delta| vs
    their gen-4 pass2 scores (state of the scoring lab)."""
    scoring_state = Lab("scoring").state
    pass2 = {r["item_id"]: r["pass2"] for r in scoring_state.get("pinned_pass_results", [])}
    rows = {r["item_id"]: r for r in db_replay.scored_items(conn)}
    by_id = {i.id: i for i in ddb.active_interests(conn)}
    deltas = []
    for item_id in PROBE_ITEMS:
        row = rows.get(item_id)
        base = pass2.get(item_id)
        if row is None or base is None:
            continue
        item = db_replay.to_candidate(row)
        for k in range(3):
            result = lab.call("probe", lambda: prod_scorer.score_item(
                prov, item, by_id[row["interest_id"]], match_score=row["match_score"]),
                item_id=item_id, repeat=k)
            if result:
                deltas.append(round(abs(result.final_score - base), 4))
    return {"max_abs_delta": max(deltas) if deltas else None, "n": len(deltas),
            "ceiling": PROBE_CEILING, "decisive": False}


def run(lab, args):
    cfg = load_cfg()
    provider = get_provider(cfg)
    if provider.model != PINNED_MODEL:
        sys.exit(f"model {provider.model} != pinned {PINNED_MODEL}; run void")
    if scoring.prompt_fingerprint() != PINNED_HASH:
        sys.exit("production prompt changed; re-pin before running")

    conn = db_replay.open_ro(cfg.db_path)
    corpus = db_replay.scored_items(conn)
    corpus_urls = {canon_url(r["url"]) for r in corpus}
    corpus_titles = {canon_title(r["title"]) for r in corpus}
    interests = {i.key: i for i in ddb.active_interests(conn)}

    keys = list(STARVED) + [CONTROL]
    results, voids = {}, []
    for key in keys:
        interest = interests[key]
        bar = interest.min_score
        per_arm, arm_urls = {}, {}
        for arm in ("S", "A"):
            seen_urls = set(corpus_urls)
            seen_titles = set(corpus_titles)
            if arm == "S":
                raw_items = collect_arm_s(interest, cfg, provider, lab)
                breach = None
            else:
                raw_items, breach = collect_arm_a(interest, cfg, provider, lab)
            if breach:
                voids.append(f"{key}/A: {breach}")
                continue
            new_items = dedupe(raw_items, seen_urls, seen_titles)[:TARGET_NEW]
            arm_urls[arm] = {canon_url(i.url) for i in new_items}
            if len(new_items) < MIN_NEW:
                voids.append(f"{key}/{arm}: net-new {len(new_items)} < {MIN_NEW}")
            scored = []
            for it in new_items:
                it.origin_interest = key
                result = lab.call("score", lambda i=it: prod_scorer.score_item(
                    provider, i, interest, match_score=0.5), interest=key, arm=arm)
                if result is None:
                    continue
                if result.prompt_hash != PINNED_HASH:
                    sys.exit("prompt hash changed mid-run; void")
                scored.append((result.final_score, it.title[:90]))
                lab.log(call="score", interest=key, arm=arm,
                        final=result.final_score, title=it.title[:120], url=it.url)
            per_arm[arm] = arm_metrics(scored, bar)
            per_arm[arm]["raw_collected"] = len(raw_items)
            per_arm[arm]["net_new"] = len(new_items)
        if {"S", "A"} <= set(arm_urls) and arm_urls["S"] | arm_urls["A"]:
            overlap = len(arm_urls["S"] & arm_urls["A"]) / len(arm_urls["S"] | arm_urls["A"])
            if overlap >= 0.50:
                voids.append(f"{key}: cross-arm overlap {overlap:.2f} >= 0.50")
            per_arm["cross_arm_url_overlap"] = round(overlap, 3)
        results[key] = {"bar": bar, **{f"arm_{a}": m for a, m in per_arm.items()
                                       if a in ("S", "A")},
                        "cross_arm_url_overlap": per_arm.get("cross_arm_url_overlap")}
        lab.state.setdefault("e2_partial", {})[key] = results[key]
        lab.save()

    starved_above_a = sum(results[k]["arm_A"]["above_bar"] for k in STARVED
                          if "arm_A" in results.get(k, {}))
    starved_above_s = sum(results[k]["arm_S"]["above_bar"] for k in STARVED
                          if "arm_S" in results.get(k, {}))
    pooled_a = [d + results[k]["bar"] for k in STARVED if "arm_A" in results.get(k, {})
                for d in results[k]["arm_A"]["dist_to_bar"]]
    pooled_s = [d + results[k]["bar"] for k in STARVED if "arm_S" in results.get(k, {})
                for d in results[k]["arm_S"]["dist_to_bar"]]
    p90_gap = (round(percentile(pooled_a, 0.9) - percentile(pooled_s, 0.9), 4)
               if pooled_a and pooled_s else None)

    agg = {
        "pinned": {"prompt_hash": PINNED_HASH, "model": PINNED_MODEL},
        "per_interest": results,
        "starved_above_bar": {"arm_A": starved_above_a, "arm_S": starved_above_s},
        "pooled_p90_gap_A_minus_S": p90_gap,
        "preregistered_checks": {
            "primary_A>=4_and_>S": starved_above_a >= 4 and starved_above_a > starved_above_s,
            "secondary_p90_gap>=0.04": p90_gap is not None and p90_gap >= 0.04,
            "falsified_if_gap<0.02_and_primary_fails": (
                p90_gap is not None and p90_gap < 0.02
                and not (starved_above_a >= 4 and starved_above_a > starved_above_s)),
        },
        "validity_voids": voids,
        "frozen_scorer_probe": probe(lab, provider, conn),
    }
    record = {"kind": "e2_discovery_ab", "aggregate": agg, "delta_vs_baseline": None}
    judged = council_judge(
        provider, lab, BRIEF, {"aggregate": agg},
        "Question: is the run valid (voids, firewall, probe), which "
        "pre-registered criteria held on the starved interests, and per "
        "proposal 005 does Arm A become the production collector, get one "
        "re-run, or revert with the lab going idle until labels?",
        kind="e2_discovery_ab")
    if judged:
        record["verdict"] = judged["verdict"]
        record["guidance"] = judged["guidance"]

    lab.state.pop("e2_partial", None)
    lab.add_generation(record)
    print(json.dumps({k: record[k] for k in ("gen", "kind", "aggregate", "verdict", "guidance")
                      if k in record}, ensure_ascii=False, indent=1))
    print(f"budget used: {lab.state['budget_used']}/{lab.budget_cap}")


def main():
    utf8_streams()
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["run", "report"])
    ap.add_argument("--budget", type=int, default=220)
    args = ap.parse_args()
    lab = Lab("discovery", budget_cap=args.budget)
    if args.mode == "report":
        for g in lab.state["generations"]:
            print(json.dumps({k: g.get(k) for k in
                              ("gen", "ts", "kind", "aggregate", "verdict", "guidance")},
                             ensure_ascii=False, indent=1))
        print(f"budget used: {lab.state['budget_used']}/{lab.budget_cap}")
    else:
        run(lab, args)


if __name__ == "__main__":
    main()
