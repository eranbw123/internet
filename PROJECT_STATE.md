# PROJECT_STATE.md — `internet`

Updated 2026-08-09. Imported by `CLAUDE.md`. Current state only — not a log.

## engine lab (`experiments/lab/`, branch `engine-lab`)
Reusable prompt-optimization loop for the whole engine, generalized from the
x prompt lab: `lab_common.py` (budget cap, runs.jsonl full-prompt log,
state.json generations, council judge), `db_replay.py` (mode=ro sampling),
`prod_scorer.py` (production scoring incl. prompt variants), `rate_batch.py`
(golden-set feedback writer — the lab's ONLY DB write), `exp_scoring.py`
(E1), `exp_weights.py` (E4, free). Catalog/triggers/promotion path:
`experiments/lab/LAB.md`. Long-running lab jobs must run as a one-shot
Scheduled Task, not a session child — SSH-session children get reaped.

E1 baseline (2026-08-09, 31 calls, 10 items × 3 repeats): jitter fine
(mean_std .011, max .022, 0 notify flips) but sampled items sat far from
bars, so flips were never stressed; **drift vs stored scores .054 ≈ the
spacing between interest bars — the largest real effect**; separation
unmeasurable at 4 verdicts. Judge guidance (in `artifacts/scoring/state.json`):
band-proximate sampling next, ≥15+15 labels before trusting AUC, corpus-wide
notify-rate check — bar calibration may be the binding constraint, not
scorer noise.

Meta-loop `design_council.py` (the powerhouse): reads all experiments'
state, emits ONE pre-registered proposal per cycle into
`experiments/lab/proposals/` (evidence → predicted metric move → validation
on untouched data → rollback trigger), ntfy to owner (.env NTFY_TOPIC),
`validate` checks predictions post-run. Ledger PROPOSED→APPROVED→EXECUTED→
VALIDATED|REVERTED; guardrails in LAB.md.

Proposal 001 EXECUTED then **REVERTED by its own trigger** (2026-08-09):
`scores.prompt_hash` stamping kept (`scoring.prompt_fingerprint()`), but the
drift-is-version-attributable interpretation is falsified — full-corpus
rescore (122/122, same git-verified prompt + model) measured mean drift
0.0569 / median 0.0345 with **14/122 notify flips**: same-version
non-determinism. Prime suspect (recorded in 001): lab replay scores one
interest + empty feedback vs production's full shortlist + feedback block.
Routing readouts held: corpus notify_rate .197, band_density .148 (18
near-bar items); behavioral/knowledge/emdr (bars 0.78–0.80) notify ≈0;
dimensions discriminating (22–34 distinct values).

Proposal 002 EXECUTED then **mothballed by owner decision**: rating pass
deferred indefinitely — `blind_rate.py` + frozen 67-item blind batch remain
available (`artifacts/blind_batch_002/`), every label-gated metric stays
gated. Owner directive: proceed label-free, trust the council (standing
approval for council proposals; `propose --context` passes directives in).

Proposal 003 EXECUTED (auto-approved under that directive), **running
detached** as Scheduled Task `engine-lab-003`: pinned within-version
3-repeat on band items + far controls (band by gen2 scores = 25 items, not
the 18 estimated — drift moved items in; +22 controls = 141 calls, cap 320),
pre-registered intervals + cache-leak detector coded into
`exp_scoring.py band-repeat`; validate + ntfy chained. Complexity budget
delivered: exp_scoring rewritten — baseline/variant/rescore/separation
modes DELETED (results persist in state.json), band-repeat/distribution/
report remain. Lab rules in CLAUDE.md: iterations run detached; every
iteration must shrink the lab (also enforced in the design-council brief).

## youtube: graceful degradation to video-level items
Stages 1–2 unchanged (LLM-first `search_json` discovery, 0 quota; one batched
`videos.list` verify, 1 unit/≤50 ids, drops hallucinated/dead/stale ids).
Stage 3 used to discard a video on any transcript miss (no captions,
breaker-tripped, over the fetch budget), so a live IP block silently zeroed
the source. A miss now emits ONE video-level `CandidateItem` (`type="video"`,
`dedup_key="<id>:video"`, text = title + description). Seen-prefix check: a
video is processed once, at that day's fidelity; a video-level row is never
later upgraded to segments. Chunking unchanged when a transcript exists.

Incidental fixes kept in scope:
- `pipeline.py`/`__main__.py`: funnel counters (`db.bump`) flush per-item,
  not at cycle end — a mid-cycle crash (hit live: codepage crash, now behind
  `print_safe`) silently lost counts for already-committed items.
- `stocks.py`: `market_event` URLs carry `?event=<date>` — otherwise dedup's
  url-hash check treated every day's alert after the first as a duplicate of
  day one, forever.

## Live verification (2026-08-08, production DB, real spend)
Transcript IP block still active (`TranscriptBlocked` raised live). Two
back-to-back production youtube `run_once` runs: run 1 stored 3 video-level
items (0 hallucinated, 1 stale dropped); run 2 stored different new videos;
1 re-discovered id was skipped by the seen-check before `videos.list` spend.
Net: 5 `type="video"` items, all real, all scored by live claude.ai, **0
notified** — best 0.55 vs bar 0.76, rest 0.14–0.40 vs 0.74–0.76; no digest
sent, correctly. Honest finding, not a shortfall: title+description is thin
evidence against 0.74–0.80 bars; real-transcript segments will likely score
higher once the block clears — unverified. Spend: 4 `videos.list` units, 6
`search_json` + 5 `complete_json` LLM calls.

Verdict: MOSTLY fixed — silent discard resolved; items stored, scored, would
notify past the bar; none cleared it here, expected from description-only
evidence.

## x collector: prompt-lab verdict (2026-08-08, live spend, no code yet)
Search-prompts-only X discovery (via `search_json`, no scraping/API) is
VIABLE: 2 interests × 3 generations, 91/91 items valid status URLs, 0
hallucinated (15 ids independently re-found = realness proof), judge-ranked
main news. Freshness floor: D-1 broad topics, D-2 single ticker → digest
source, NOT ALERT. Winning angles: article-embed harvesting + aggregator
backtrace; IR/capex/funding tweet hunts always empty. Production shape:
cached strategist prompt + 2–4 angle searches, dedup_key=status id, add
`"x"` to SHORT_FORM_SOURCES. Full data + harness + conclusions.md:
`experiments/x_prompt_lab/` (untracked). Fallback transports if ever needed:
twitterapi.io ($0.15/1k) or t.me/s/walter_bloomberg scrape.

## Open decision
`recency_days` is both prompt bias and HARD verify drop. Proposal (not
implemented): per-interest `strict_recency` (default true); false = keep old
videos (narcolepsy/behavioral want old gems per their definitions), rank +
novelty judge instead. Awaiting user approval.

## Implemented
`watch.py` Yahoo helper (library-only, no CLI/ntfy). `discovery/`:
staged pipeline, 0–1 scoring, providers `claude_chat` (default; claude.ai via
CDP Chrome :9222 + `CLAUDE_ORG_ID`, no key) / `anthropic` / `openai`; score
budget; backlog rescore w/ 30-min backoff; Telegram ALERT (market_event,
immediate) vs DISCOVERY digest (daily, capped); failed sends retried (15-min
cool-off, max 3); feedback listener; scheduler (60s tick). Collectors:
`web_search`, `stocks` (NBIS 6%/12% thresholds), `youtube`.

## Non-obvious decisions
`final_score` in code from `models.WEIGHTS`; dedup URL/title/content hashes +
`(source, dedup_key)`; threshold in SQL; provider lazy; run from repo root.
All timestamps UTC via `db.now()`/`db.ago()`. No token metering on
claude_chat (calls only).

## Tests
`python test_discovery.py` (160) + `python test_watch.py` (10), offline, both
green; CI on push/PR.

## Known issues
claude.ai endpoints undocumented/ToS-gray (volume bounded by
DISCOVERY_MAX_SCORES). YouTube transcript path live-unverified end-to-end
(IP block). PR #1 (`add-discovery-engine`) still open; youtube redesign on
top of it in PR #4 (`youtube-video-level-fallback`).

## Commands
```bash
# once per boot: chrome --remote-debugging-port=9222 (+ claude.ai login)
python test_discovery.py && python test_watch.py
python -m app run-once   |   python -m app run   |   python -m app digest
python -m app listen     |   python -m app stats --days 7
```
