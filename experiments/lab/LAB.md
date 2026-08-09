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

## Promotion path

Lab finding → normal edit to `discovery/` or `interests.json` → offline tests
(`python test_discovery.py`) → PR → PROJECT_STATE.md notes the finding and
retires the question. The lab never edits production code itself.
