"""Design council -- the lab iteratively improves its own design, gated.

Experiment runs leave scorecards and judge guidance in artifacts/*/state.json.
This module turns that accumulated evidence into DESIGN PROPOSALS: minimal,
concrete changes to the lab (sampling, metrics, variants, new experiments),
each pre-registered with a predicted effect and a validation plan, written to
experiments/lab/proposals/NNN-<slug>.md and notified to the owner.

NOTHING EXECUTES WITHOUT OWNER APPROVAL. Ledger states:
PROPOSED -> APPROVED -> EXECUTED -> VALIDATED | REVERTED.

Overfitting guardrails (enforced here and in LAB.md):
- every claim in a proposal must cite a measured number from lab state;
- one proposal at a time, smallest change that tests the idea;
- predictions are registered BEFORE the validating run, on data the change
  was not designed against (later generations / holdout labels);
- a change that misses its registered prediction is reverted, not re-argued;
- the council proposes, code metrics decide, the owner approves.

    python experiments/lab/design_council.py propose [--notify]
    python experiments/lab/design_council.py status
    python experiments/lab/design_council.py mark <nnn> <STATUS>
    python experiments/lab/design_council.py validate <nnn>
"""
import argparse
import json
import re
import sys

from lab_common import ARTIFACTS, Lab, council_judge, now, utf8_streams

from discovery.config import load as load_cfg  # noqa: E402
from discovery.providers import get_provider  # noqa: E402

PROPOSALS = ARTIFACTS.parent / "proposals"
STATUSES = ("PROPOSED", "APPROVED", "EXECUTED", "VALIDATED", "REVERTED")

BRIEF = """\
You are the design council of a prompt-optimization lab for a personal
discovery engine (collect -> score 0-1 on six dimensions -> notify past
per-interest bars of 0.70-0.85). The lab runs budget-capped experiments;
each generation produces code-computed metrics and judge guidance. Your job
is to improve THE LAB'S OWN DESIGN so its next runs answer the highest-value
question about the engine.

Hard rules for any proposal you make:
- Cite only the measured numbers given below; never invent evidence.
- ONE proposal: the smallest change that tests the most valuable idea next.
- The proposal must name a pre-registered prediction: which code-computed
  metric moves, in which direction, by roughly how much, measured on data
  the change was not designed against (a future generation, new items, or
  held-out labels). If its prediction fails, the change gets reverted.
- Respect minimum-sample gates: no verdict-separation claims below 15
  positive + 15 negative labels; no jitter claims from repeats < 3; no
  threshold-band claims from items far from their bars.
- Do not re-propose anything in the existing ledger below unless its status
  is REVERTED and you have new evidence.
- Complexity budget: prefer proposals that DELETE or merge code, metrics,
  process steps, or whole experiments whose question is answered. Any
  proposal that adds something must name what it removes or retires in
  exchange; growth of the lab with no offsetting reduction is itself a
  defect, and "add nothing, delete X" is a perfectly good proposal."""

PROPOSAL_SCHEMA = {
    "type": "object",
    "required": ["title", "slug", "evidence", "changes", "prediction",
                 "validation_plan", "overfitting_risks", "rollback_trigger"],
    "properties": {
        "title": {"type": "string"},
        "slug": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "changes": {"type": "array", "items": {"type": "object"}},
        "prediction": {"type": "string"},
        "validation_plan": {"type": "string"},
        "overfitting_risks": {"type": "string"},
        "rollback_trigger": {"type": "string"},
    },
}


# --- evidence gathering ------------------------------------------------------

def gather_state():
    """Compact view of every experiment's recent generations + budget."""
    out = {}
    for state_path in sorted(ARTIFACTS.glob("*/state.json")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        out[state_path.parent.name] = {
            "budget_used": state.get("budget_used"),
            "generations": [
                {k: g.get(k) for k in ("gen", "kind", "aggregate", "separation",
                                       "delta_vs_baseline", "verdict", "guidance")}
                for g in state.get("generations", [])[-3:]
            ],
        }
    return out


def ledger():
    """[(nnn, slug, status, title, path)] for every proposal on disk."""
    rows = []
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    for path in sorted(PROPOSALS.glob("[0-9][0-9][0-9]-*.md")):
        text = path.read_text(encoding="utf-8")
        status = _field(text, "Status") or "PROPOSED"
        title = _field(text, "Title") or path.stem
        rows.append((int(path.name[:3]), path.stem[4:], status, title, path))
    return rows


def _field(text, name):
    m = re.search(rf"^{name}:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None


# --- propose -----------------------------------------------------------------

def propose(notify, context=""):
    cfg = load_cfg()
    provider = get_provider(cfg)
    lab = Lab("design_council", budget_cap=10)

    entries = ledger()
    data = {
        "lab_state": gather_state(),
        "existing_proposals": [
            {"id": n, "slug": s, "status": st, "title": t} for n, s, st, t, _ in entries
        ],
    }
    if context:
        # Owner directives for this cycle (e.g. "labels deferred, propose
        # label-free work") -- constraints, not evidence.
        data["owner_directive"] = context
    result = council_judge(
        provider, lab, BRIEF, data,
        "Question: what single design change should the lab make next? "
        "Answer as the guidance field containing ONLY a JSON object matching "
        "this shape (the verdict field holds your reasoning summary): "
        + json.dumps({k: "..." for k in PROPOSAL_SCHEMA["required"]}),
    )
    if result is None:
        sys.exit("design council call failed twice")

    try:
        proposal = json.loads(_json_slice(result["guidance"]))
    except (ValueError, KeyError) as e:
        lab.log(call="propose_parse_error", error=str(e), raw=result)
        sys.exit(f"council returned unparseable proposal (logged): {e}")

    nnn = max((n for n, *_ in entries), default=0) + 1
    slug = re.sub(r"[^a-z0-9-]", "", proposal.get("slug", "proposal").lower())[:40] or "proposal"
    path = PROPOSALS / f"{nnn:03d}-{slug}.md"
    path.write_text(render(nnn, proposal, result.get("verdict", "")), encoding="utf-8")
    lab.save()
    print(f"wrote {path}")

    if notify:
        _ntfy(f"Lab design proposal {nnn:03d}",
              f"{proposal['title']}\nPrediction: {proposal['prediction']}\n"
              f"File: {path.name}")
    return path


def _json_slice(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in guidance")
    return text[start:end + 1]


def render(nnn, p, reasoning):
    # The council does not always respect the array types in the schema hint;
    # a string where a list belongs must not degrade to per-character bullets.
    def as_list(value):
        return value if isinstance(value, list) else [value]

    def change_line(c):
        if isinstance(c, dict):
            line = f"- **{c.get('target', '?')}**: {c.get('change', json.dumps(c, ensure_ascii=False))}"
            return line + (f" — {c['why']}" if c.get("why") else "")
        return f"- {c}"

    changes = "\n".join(change_line(c) for c in as_list(p["changes"]))
    evidence = "\n".join(f"- {e}" for e in as_list(p["evidence"]))
    return f"""Title: {p['title']}
Status: PROPOSED
Created: {now()}

# {nnn:03d} — {p['title']}

## Council reasoning
{reasoning}

## Evidence (measured, from lab state)
{evidence}

## Changes (smallest set that tests the idea)
{changes}

## Pre-registered prediction
{p['prediction']}

## Validation plan (data the change was not designed against)
{p['validation_plan']}

## Overfitting risks
{p['overfitting_risks']}

## Rollback trigger
{p['rollback_trigger']}
"""


def _ntfy(title, text):
    """Owner notification via ntfy (same channel ssh_watch uses). Topic and
    base come from the environment (.env: NTFY_TOPIC / NTFY_BASE) -- config
    load_dotenv has already run by the time this is called."""
    import os
    import urllib.request
    topic = os.environ.get("NTFY_TOPIC")
    base = os.environ.get("NTFY_BASE", "https://ntfy.sh")
    if not topic:
        print("NTFY_TOPIC not set; skipping notify")
        return
    try:
        request = urllib.request.Request(
            f"{base}/{topic}", data=text.encode("utf-8"),
            headers={"Title": title}, method="POST",
        )
        urllib.request.urlopen(request, timeout=15).read()
        print("owner notified via ntfy")
    except Exception as e:  # noqa: BLE001 -- notify failure must not lose the proposal
        print(f"ntfy notify failed: {e}", file=sys.stderr)


# --- status / mark / validate ------------------------------------------------

def show_status():
    rows = ledger()
    if not rows:
        print("no proposals yet")
    for n, slug, status, title, _ in rows:
        print(f"{n:03d}  {status:<9}  {title}")


def mark(nnn, status):
    if status not in STATUSES:
        sys.exit(f"status must be one of {STATUSES}")
    for n, _slug, _st, _t, path in ledger():
        if n == nnn:
            text = path.read_text(encoding="utf-8")
            path.write_text(re.sub(r"^Status:.*$", f"Status: {status}", text,
                                   count=1, flags=re.M), encoding="utf-8")
            print(f"{nnn:03d} -> {status}")
            return
    sys.exit(f"no proposal {nnn:03d}")


def validate(nnn):
    """Ask the council whether a proposal's registered prediction held in the
    lab state produced AFTER it was executed. Recommends only; the owner (or
    the session acting for them) flips the status with `mark`."""
    for n, _slug, status, _t, path in ledger():
        if n != nnn:
            continue
        if status not in ("EXECUTED", "APPROVED"):
            sys.exit(f"{nnn:03d} is {status}; validate expects EXECUTED")
        cfg = load_cfg()
        provider = get_provider(cfg)
        lab = Lab("design_council", budget_cap=10)
        result = council_judge(
            provider, lab, BRIEF,
            {"proposal": path.read_text(encoding="utf-8"), "lab_state": gather_state()},
            "Question: did this proposal's pre-registered prediction hold in "
            "the measured results? Verdict must compare predicted vs measured "
            "numbers explicitly; guidance must be exactly VALIDATED or "
            "REVERTED plus one sentence.",
        )
        if result is None:
            sys.exit("validation call failed twice")
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    sys.exit(f"no proposal {nnn:03d}")


def main():
    utf8_streams()
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["propose", "status", "mark", "validate", "notify"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--context", default="", help="owner directive passed to the council")
    args = ap.parse_args()

    if args.mode == "propose":
        propose(args.notify, args.context)
    elif args.mode == "status":
        show_status()
    elif args.mode == "mark":
        mark(int(args.rest[0]), args.rest[1].upper())
    elif args.mode == "validate":
        validate(int(args.rest[0]))
    else:  # notify <title> <text> -- used by detached task chains to ping the owner
        load_cfg()  # loads .env so NTFY_TOPIC is in the environment
        _ntfy(args.rest[0], args.rest[1] if len(args.rest) > 1 else "")


if __name__ == "__main__":
    main()
