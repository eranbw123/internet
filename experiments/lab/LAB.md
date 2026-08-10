# Engine Lab — iterative prompt optimization for the discovery engine

The lab is how the engine's prompts and parameters get improved: bounded,
logged experiments whose findings are promoted into production by normal
edits + offline tests + PR. It generalizes the loop the x prompt lab proved
(`experiments/x_prompt_lab/conclusions.md`): propose → execute → **measure in
code** → council judge writes guidance → scorecard persisted → next
generation starts from the scorecard.

## Ground rules

- Run from the repo root. Default provider is `claude_chat` (Chrome on :9222,
  logged into claude.ai) — zero marginal cost, but never run the lab while
  `python -m app run` is running (shared browser tab).
- Iterations run detached, always: anything that spends provider calls or
  outlives a few minutes goes in a one-shot Scheduled Task with validate +
  ntfy chained into its `.cmd` (see `artifacts/scoring/rescore_task.cmd` for
  the shape), never as an SSH-session child — those get reaped on disconnect.
  Results stream to `artifacts/` as produced so any session can resume.
- Every run has a hard budget cap (default 40 provider calls, `--budget`).
- The lab reads `discovery.db` strictly `mode=ro`. The single exception is
  `rate_batch.py`, which writes feedback rows through `db.add_feedback` —
  nothing else, ever.
- Every provider call's full prompt + raw response goes to
  `artifacts/<experiment>/runs.jsonl`; generations + scorecards to
  `state.json`. Both gitignored — conclusions get written into
  PROJECT_STATE.md or a committed conclusions file when they matter.
- Metrics come from code; the council judge interprets them and writes
  next-generation guidance. It never produces numbers.

## Experiment catalog

### E1 — scorer stability & calibration (`exp_scoring.py`)
Is the scorer precise enough for its 0.70–0.85 notify band, and does it agree
with the owner's verdicts?

```bash
python experiments/lab/exp_scoring.py baseline --items 10 --repeats 3  # ~31 calls
python experiments/lab/exp_scoring.py variant anchored                 # same items
python experiments/lab/exp_scoring.py separation                       # free
python experiments/lab/exp_scoring.py report
```

Reading the scorecard: `mean_std` / `flip_rate` — jitter; std above ~0.05 or
flips on >1 in 10 items means threshold decisions are noise and prompt
hardening (variants) comes before any other tuning. `separation.auc` — 1.0 is
perfect ranking of positive above negative verdicts, 0.5 is chance.
`dim_noise` — which dimension to anchor first. A variant wins on lower
jitter/flip_rate with AUC not worse; promote it by editing
`discovery/scoring.py` normally.

### E2 — discovery yield (runner built when first triggered)
Per interest: strategist generates angle prompts from the interest definition
+ past scorecards (the x-lab pattern; reuse its strategist/history templates);
the static `web_search` template runs as control. Execute via `search_json`,
normalize/dedup in-memory, score with `prod_scorer`. Headline metric: **% of
unique new candidates above the interest's own bar**, plus unique yield,
median age, cost per above-bar item. Winners become cached per-interest
discovery prompts.

### E3 — interest-definition tuning (runner built when first triggered)
Treat an `interests.json` entry as a prompt. Strategist proposes
description/signal rewrites; evaluate by (a) re-scoring the golden set under
the variant interest block — separation and bar-clearance of loved items —
and (b) one E2 generation with the variant. Winner is a human-reviewed edit
to `interests.json`.

### E4 — weights & threshold fit (`exp_weights.py`)
Free, no LLM. Grid-search `models.WEIGHTS` against feedback verdicts,
maximizing AUC. Prints a proposal; below 30 labels it is directional only.

```bash
python experiments/lab/exp_weights.py
```

### E5 — connector evidence, read-only (`exp_connectors.py`)
Step-09a: does any of the 5 candidate connectors (x, hackernews, reddit,
arxiv, pubmed) reach items the engine can't already reach? Bounded free-HTTP
probes (<=40 requests, <=10/connector) against the frozen probed-interest
set; never registers a collector, edits `interests.json`, or writes
`discovery.db`. `probe` = availability only; `sample` = bounded retrieval +
analysis, persists the tracked `experiments/lab/connector_evidence.json`
dossier; `report` = render the persisted dossier, zero spend.

```bash
python experiments/lab/exp_connectors.py probe
python experiments/lab/exp_connectors.py sample
python experiments/lab/exp_connectors.py report
```

Retires `blind_rate.py` (guardrail 8): proposal 002 was mothballed
indefinitely by owner decision ("proceed label-free"), every label-gated
metric is already gated elsewhere, and the frozen 67-item batch under the
gitignored `artifacts/blind_batch_002/` is untouched — git history retains
the file if the rating pass is ever revived.

## Golden set

`rate_batch.py` samples ~25 scored-but-unrated items (spread across interests
and score bands) for a one-time rating pass; verdicts land in the normal
`feedback` table with note `golden-set`, and every later Telegram button press
grows the same base.

```bash
python experiments/lab/rate_batch.py --dump    # inspect the batch (no writes)
python experiments/lab/rate_batch.py           # rate interactively (f/u/d/t/s/q)
python experiments/lab/rate_batch.py --apply "101=f 105=t"
```

## Triggers — when to run what

| Signal (usually from `python -m app stats`) | Run |
|---|---|
| An interest's notify rate ≈ 0 over a week | E2 for that interest |
| ~10 new feedback verdicts since last fit | E4, then E1 `separation` |
| Adding a source/collector | E2 with that source's URL contract (x-lab pattern) |
| About to edit an `interests.json` entry | E3 first |
| Scores cluster oddly / notify decisions feel random | E1 baseline (or re-baseline after a scoring.py change) |

## Design evolution — the meta-loop (`design_council.py`)

The lab improves its own design from accumulated council decisions:

```bash
python experiments/lab/design_council.py propose --notify  # 1 call -> proposals/NNN-*.md + ntfy
python experiments/lab/design_council.py status            # ledger
python experiments/lab/design_council.py mark 1 APPROVED   # owner decision recorded
python experiments/lab/design_council.py validate 1        # did the prediction hold?
```

Each proposal is the *smallest* change that tests the highest-value idea,
pre-registered in `proposals/NNN-<slug>.md` with: cited measured evidence, a
predicted metric move, a validation plan on data the change was not designed
against, overfitting risks, and a rollback trigger. Ledger:
PROPOSED → APPROVED → EXECUTED → VALIDATED | REVERTED.

**Overfitting guardrails** (non-negotiable):
1. Owner approval gates execution — the council proposes, code metrics
   decide, the owner approves (may be pre-authorized for a stated window).
2. Pre-registration: the prediction is written before the validating run; a
   change that misses it is REVERTED, not re-argued.
3. One change at a time; no compound proposals.
4. Minimum-n gates: no separation/AUC claims under 15+15 labels; no jitter
   claims under 3 repeats; no band claims from items far from their bars.
5. Validation data must be untouched by design: later generations, new
   items, or held-out labels — never the sample the change was tuned on.
6. Judge scores never promote anything alone (judge and scorer share a model
   family); only code-computed metrics on untouched data do.
7. Share-based criteria (rates, proportions) are non-decisive below 8
   observations — fall through to the absolute count instead. Learned from
   004: a ≥60% share test over 2 disagreements can only pass at 2/2, which
   is a coin flip, not a test.
8. Complexity budget — the lab must shrink over time, not grow: every
   proposal prefers deleting/merging code, metrics, or process steps, and any
   addition must name what it removes or retires. Retire experiments whose
   question is answered; no frameworks, base classes, or abstractions for
   single call sites (repo ethos applies to the lab too). A lab too complex
   to hold in one head stops being trustworthy — treat that as a defect.

## Promotion path

Lab finding → normal edit to `discovery/` or `interests.json` → offline tests
(`python test_discovery.py`) → PR → PROJECT_STATE.md notes the finding and
retires the question. The lab never edits production code itself.
