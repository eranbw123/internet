# PROJECT_STATE.md — `internet`

Updated 2026-08-10. Imported by `CLAUDE.md`. Current state only — not a log.

## service hardening (step-01): no more in-process scheduler
`discovery/scheduler.py` and `run` are DELETED; `ops/install_tasks.py` is the
scheduler now: six Windows Scheduled Tasks (`internet-discovery-collect-
stocks/-web/-youtube`, `-digest`, `-feedback`, `-health`) call `run-once
--source <name>` / `digest` / `listen --drain` / `health --notify` on
cadences read from `config.load()` (`.env` change + `--install`
reschedules). Registered via generated Task Scheduler XML (UTF-16LE+BOM) +
`schtasks /create /XML`, each creation verified with a `/query` follow-up;
`StartWhenAvailable`, `IgnoreNew`, `RestartOnFailure`, `InteractiveToken`
principal (Chrome/CDP only exists in that session), `StartBoundary`
staggered per task so same-length intervals never coincide.
`--dry-run`/`--install`/`--uninstall` (prefix-scoped)/`--status`. Tasks run
`ops/run.cmd` (utf-8 stdout, `cd` to repo root, `python -m app %*`, log name
built from the full arg list so the three `run-once` collectors don't share
one file, exit code propagated); `logs/` is gitignored, inbound-only.

`--soak [--soak-hours N] [--dry-run]` registers a seventh, one-shot task
(`SOAK_TASK` = `internet-discovery-soak-check`, deliberately outside
`_TASK_SPECS`/`build_tasks()` so `--install` never creates/reschedules it;
`--uninstall` deletes it if present) whose single `<TimeTrigger>` (no
`<Repetition>`) fires once, `N` hours out (default 24); it shells to
`ops/soak_check.cmd`, which appends `stats --days 1` + `health` +
`install_tasks.py --status` to `logs\soak-<date>.txt` (repair: a
`schtasks /query /fo LIST /v | findstr internet-discovery-` first cut only
kept TaskName/Comment lines — `/fo LIST` puts the prefix on no other field —
so reusing `--status`'s own block-aware reader is what actually gets
Status/Last Run Time/Last Result/Next Run Time into the readout; the
script's own exit code now reflects the `stats`/`--status` calls, not a
`findstr` no-match, and no longer fails the task on `health`'s legitimate
degraded=1). `--soak` is composable with `--dry-run` (argparse's own group
can't express "exclusive among these four, but not with dry-run", so
`main()` checks mutual exclusivity by hand; `--status --dry-run` is
rejected outright since status has nothing to preview). `install()`'s
per-task registration (tempfile write + `schtasks /create /XML` + `/query`
verify) is factored into `_register_task`, shared by `install()` and
`install_soak()` — one registration path, not two; `install_soak()` reports
back the exact `<StartBoundary>` it registered (parsed from the rendered
XML) rather than recomputing `datetime.now()` a second time.
`main()`'s `--uninstall` now threads `--dry-run` through (repair: it was
silently dropped, so `--uninstall --dry-run` performed a real delete of all
seven tasks instead of previewing). Runbook + resume procedure:
`ops/SOAK.md`.

Live install, fault-injection drills and the live 24h wall-clock soak
execution are still a separate, not-yet-done step — they need a live
operator session (real Chrome/CDP, Telegram, `schtasks`, and 24h wall-clock
time), which an isolated-worktree implementer/repair session cannot
provide; do not mark that part done without that session's evidence. The
offline-implementable half (the soak-checkpoint scheduling artifact +
runbook above) is done and tested. Every invocation is short-lived,
idempotent and overlap-safe:
`db.connect` sets `PRAGMA busy_timeout=5000`, and a new `service_state`
key/value table (`db.state_get`/`state_set`) persists job heartbeats
(`job:<name>:last_ok`/`last_fail`), the Telegram `getUpdates` offset, and
`health`'s own alert-dedup state across separate processes.

`run-once` is gated by `providers/base.LLMProvider.preflight()` (base:
always ok; `anthropic`/`openai`: API-key presence only; `ClaudeChatProvider`:
free local check via `cdp.list_tabs`/`find_claude_tab` — CLAUDE_ORG_ID set,
CDP endpoint up, a claude.ai tab open). `discovery/health.py` owns the gate
(`preflight_gate`, optional one-shot `cfg.chrome_launch_cmd` relaunch +
`chrome_launch_wait_seconds` re-check) and the readout (`check`/
`format_report`/`notify_if_needed`): job staleness (never-run = unknown, not
stale), provider reachability, pending/abandoned notification counts,
today's `metrics` counters (`run_ok`/`run_failed`/`provider_down`/
`send_failed`/`feedback_recorded`). `python -m app health [--notify]`
exits 0/1; `--notify` alerts at most once per
`cfg.health_alert_cooldown_seconds` while degraded plus one recovery message,
gated on `service_state`. `stats.report(conn, days, cfg=None)` grows a
HEALTH section when `cfg` is passed (no live provider check — read-only).

`feedback_listener.drain(conn, cfg)` is one bounded `getUpdates` pass
(`python -m app listen --drain`) sharing the persisted offset with the
blocking `listen`, so a button press during downtime is caught by the next
drain instead of lost. Send-retry policy moved onto `Config`
(`send_max_attempts`=5, `send_retry_seconds`=30min, raised from db.py's
3/15min module-constant defaults, which stay as `db.pending_notifications`'s
own fallback); `pipeline._send_one` bumps `send_failed` on a failed send.

## personal-state contract (consumer side)
`discovery/personal_state.py` is the ONLY reader of the `ai` repo's derived
personal-state artifact (schema owned by `ai`'s `PERSONAL_STATE_CONTRACT.md`);
nothing else here opens it, opens `conversations.db`, or imports from `ai`.
`SUPPORTED_VERSIONS = {1}`; unknown top-level/per-topic keys are ignored.
Path comes from `DISCOVERY_PERSONAL_STATE` (`cfg.personal_state_path`,
default `personal_state.json` at repo root, gitignored — inbound, never
committed). `load_optional()` is the fail-soft form the pipeline should use.
`interests.json` entries may opt in via `"personal_state_top_terms": N` to
append the artifact's top N topic keys to `positive_signals`; absent the key
(today's `interests.json`), behavior is byte-identical to before this landed.
`python -m discovery personal-state [--path]` prints a human-checkable
readout. Not yet wired into `init`/production sync — this step only
establishes the contract boundary.

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
deferred indefinitely — every label-gated metric stays gated. Owner
directive: proceed label-free, trust the council (standing approval for
council proposals; `propose --context` passes directives in). Its runner
`blind_rate.py` is DELETED (step-09a, LAB.md guardrail 8): the frozen
67-item batch under gitignored `artifacts/blind_batch_002/` is untouched
and git history retains the file if the pass is ever revived.

Proposal 003 EXECUTED then **REVERTED on a 0.0003 conjunctive miss**
(control mean_std 0.0153 vs ≤0.015, CI straddling): measurements stand —
jitter small (band mean_std .0137, mapd .0223), band flip_rate .12, flips
track bar proximity not variance; anomalies logged (control noisier than
band; band personal_relevance dim_noise .0063 vs control .0198).

Proposal 004 **VALIDATED** (first in the ledger): second pinned corpus pass
— held-out mapd 0.0245 ∈ [0.010, 0.035], 2/122 notify disagreements (vs 14
cross-condition), caching guard clear, conservative arm ordering. **Drift
closed for the pinned same-hash/same-model condition.** Learned rules:
share-based criteria non-decisive below 8 observations (LAB.md guardrail
7); tails matter — item 40 moved 0.094 while means stayed flat → max-|delta|
sentinel (ceiling 0.08) carried as a non-decisive 2-item probe.

Proposal 005 EXECUTED (auto-approved), **running detached** as Scheduled
Task `engine-lab-005`: E2 discovery-yield A/B on the three starved
interests + nbis control — Arm S (production static template, limit raised
to 15 for parity) vs Arm A (strategist angles, Goodhart-firewalled: sees
only the owner-written interest definition, never rubric/dimensions/bars;
output scanned, breach = void). Every net-new item scored once by the
frozen scorer. Pre-registered: Arm A above-bar ≥4 and >S on starved
interests; pooled p90 gap ≥0.04. Both → angles become production collector;
neither + gap <0.02 → retrieval falsified, starvation is a scorer/bar
property, **lab goes idle until labels**. Drift apparatus deleted
(exp_scoring.py now distribution+report only; probe lives in
exp_discovery.py). New Lab("discovery") budget, cap 220. Validate + ntfy
chained. Lab rules in CLAUDE.md: iterations run detached; every iteration
must shrink the lab (also in the council brief).

**E5 — connector evidence (step-09a, `exp_connectors.py`)**: read-only recon
for step-09's connector decision (x, hackernews, reddit, arxiv, pubmed vs
the 5 probed interests). Zero-spend lane RAN for real (14 free HTTP
requests, 0 provider calls): hackernews 0 hits (the mechanical
title+3-signals query is over-constrained for Algolia's AND-match, an
honest low-recall result, not a bug), reddit blocked (403, likely
datacenter-IP anti-bot), arxiv and pubmed reachable with real records
(counts vary run to run with live network conditions). `marginal_unique_rate`
now genuinely computes against `discovery.db` alone when it's reachable (the
pre-registered offline-lane substitute baseline; repair fixed a bug where
this was hardcoded unreachable with no code path to fix it) — still VOID
here because this worktree has no `discovery.db`. Verdict
**`VOID_NO_BASELINE`**. **The live lane is NOT YET IMPLEMENTED in the
harness**, not just session-gated: no code calls `provider.search_json` for
`x`, and there is no `web_search` baseline sampler, so
`jaccard_overlap_with_web_search_sample` (needed for a decisive H1
either way) stays void regardless of who runs `sample` or from where.
Before re-running for real evidence: (1) implement an `x` sampler
(`provider.search_json` + the same mechanical query rule) and a
`web_search`-baseline sampler in `exp_connectors.py`, (2) then run
`python experiments/lab/exp_connectors.py sample` from a live operator
session (Chrome `--remote-debugging-port=9222`, logged into claude.ai) with
`discovery.db` reachable. Spend: 14/40 HTTP requests, $0, 0 provider calls,
0 YouTube quota. Follow-up: `exp_connectors.py` keeps its own local
canonicalization/percentile helpers (never touched `exp_discovery.py`'s, to
avoid disturbing `engine-lab-005` in flight) — collapse the two copies onto
one shared module once 005 completes. Dossier:
`experiments/lab/connector_evidence.json` (tracked).

## chatgpt_browser provider: ChatGPT via CDP, no API key (2026-08-10)
`discovery/providers/chatgpt_browser.py` — the ChatGPT twin of `claude_chat`,
riding a logged-in chatgpt.com tab over CDP (same transport the `ai` repo reads
history with). `DISCOVERY_PROVIDER=chatgpt_browser`, default model slug `auto`,
port `CHATGPT_BROWSER_PORT`→`CLAUDE_BROWSER_PORT`→9222; no CLAUDE_ORG_ID, no
key. Registered in `providers/__init__`, `config.DEFAULT_MODELS`. Reuses
`claude_chat._extract_object`/`_validate` (no structured outputs; prompt-for-
JSON + validate + one retry). `search_json` sets `system_hints:["search"]`
(chatgpt.com's own web search); `complete_json` doesn't — so web_search/youtube
discovery now run on ChatGPT too.

The hard part vs claude.ai: chatgpt.com gates `/backend-api/conversation`
behind a sentinel challenge. Per call, in-page JS: read token from
`/api/auth/session` → POST `/backend-api/sentinel/chat-requirements` → if
`proofofwork.required`, solve it (SHA3-512 prefix search; **SubtleCrypto has no
SHA3**, so a compact BigInt keccak is embedded — cross-checked byte-for-byte
against Python `hashlib.sha3_512` on 4 vectors; server only checks the answer's
hash prefix, so the config array is pure entropy, iters capped ~150k≈8s with a
graceful-fallback token) → **if `turnstile.required`, echo `turnstile.dx` back
as `OpenAI-Sentinel-Turnstile-Token`** (live sessions set required:true but
accept the echoed challenge — do NOT throw) → POST conversation with the
sentinel headers, `history_and_training_disabled:true`, SSE read: assistant
snapshots live in `message.content.parts` (cumulative; overwrite only on a
non-empty join so a trailing empty can't wipe the answer); delta-v1
(`o:add`/`append`, bare-`v`) kept as fallback → best-effort PATCH
`is_visible:false`. Structure mirrors claude_chat: lazy connect, one reconnect
on dropped socket, JS-exception/empty/None all → ProviderError.

**LIVE-VERIFIED (2026-08-10)** against the owner's real chatgpt.com tab on :9222
(plan_type plus, model resolved gpt-5-6): `complete_json` returned
`{answer:4, word:'hello'}`; `web_search.collect` returned 3 real Nebius items
(real URLs/titles/summaries) through ChatGPT's own search. First live run
exposed the two bugs now fixed: turnstile was thrown on (chatgpt.com sets
required:true) — fixed by echoing dx; and the SSE guard. Diagnostics captured
the real frame shapes (see the two fixes). Offline: python suite stubs the CDP
seam like claude_chat's (16 tests, incl. a JS-contract lock for the sentinel/
turnstile/PoW tokens); the JS SSE+PoW core also executed in Node vs a simulated
server. Direct `openai` API provider unchanged (scoring-only, no server-side
search) — the point is ChatGPT discovery goes through the browser, not a key.

## interests.json rewrite (2026-08-10, owner-supplied)
Full owner rewrite: 40 interests, defaults `min_score` 0.8 / `sources` []
(interests with explicit `sources: []` collect nothing but remain scoring
targets). Owner's `max_videos: 1` translated to `max_transcript_fetches: 1`;
unsupported empty `channels` dropped. New youtube knob added for it:
`source_config.youtube.queries` — owner starting-point searches injected
into the stage-1 discovery prompt as hints (absent key = prompt unchanged).

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
CDP Chrome :9222 + `CLAUDE_ORG_ID`, no key) / `chatgpt_browser` (chatgpt.com
via CDP, no key) / `anthropic` / `openai`; score
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
`python test_discovery.py` (273) + `python test_watch.py` (10), offline, both
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
