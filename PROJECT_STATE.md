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

## layered interest state (step-07)
`interests` gains `layer` (owner/inferred/emerging/exploratory/retired,
default 'owner'), `provenance` (JSON), `last_observed_at`; append-only
`interest_events` (indexed on `interest_key`) is the provenance log —
nothing ever UPDATEs/DELETEs it. Off by default: `DISCOVERY_DYNAMIC_INTERESTS`
(`cfg.dynamic_interests`, default False) gates everything —
`interest_state.apply_transitions()` is a zeroed no-op with it off; no
derived row, query, or LLM/network call happens either way. Owner rows are
immutable to automation three ways: `db.upsert_interest`'s ON CONFLICT is
`WHERE layer='owner'`; the two derived-only write helpers
(`upsert_derived_interest`, `set_interest_layer`, in `db.py`, the ONLY
functions allowed to write a non-owner row) carry `WHERE layer != 'owner'`
and raise `OwnerInterestImmutable` on a zero rowcount; two SQLite triggers
(`db.TRIGGERS_SQL`, applied in `db.init` AFTER the additive-ALTER pass since
they reference `layer`) abort any raw UPDATE/DELETE touching an owner row.
Derived keys are namespaced `derived:<term>` (`db.DERIVED_KEY_PREFIX`);
`interests.load_file` raises ValueError if an owner key carries it —
structurally prevents owner/derived collision.

`discovery/interest_state.py` (stdlib only): `Rules` (frozen dataclass,
8 thresholds, construct directly — no new env vars), `Evidence`
(observations/distinct_days/first_seen/last_seen/pos+neg feedback/sources),
`gather_evidence()` (title tokens only, via `matching._tokens` — no second
tokenizer — of `candidate_items` in `evidence_window_days`, excluding
owner-covered and already-tracked-non-retired terms, deterministic
truncation to `max_candidates`), `decide()` (pure, no DB/clock: absent→
exploratory→emerging→inferred on observations/distinct_days/feedback bars;
idle `decay_idle_days` demotes one rung; negative-feedback-dominant retires
immediately; retired re-enters only at exploratory at
`promote_observations*reentry_multiplier` observations — anti-flapping;
blocklisted terms never enter and retire if already tracked; NEVER emits
layer='owner', asserted). `apply_transitions()` snapshots already-tracked
rows before writing so one call advances a term at most one ladder rung
(new-entry and progression are separate passes). Optional
`personal_state`-seeded rows land at exploratory with zero observations —
can never promote on their own (step-05's carried-forward constraint:
knowledge-state signals stay non-predictive-validated). Staleness (repair:
was measured off this-pass evidence alone, so a seeded or between-window
row with no fresh observation this pass decayed on its very next
re-evaluation regardless of `decay_idle_days` — a seed was actively
counterproductive, harder to ever adopt than never seeding at all) is now
measured against `interests.last_observed_at` as the fallback baseline
(`apply_transitions()` merges it into `Evidence.last_seen` before calling
`decide()`, which itself stays pure/DB-free); `upsert_derived_interest`
stamps it on every write, seed included, precisely so a freshly written row
isn't idle before it's ever had a chance to be observed. Operational meaning:
exploratory/emerging are `active=0, sources='[]'` (reviewable, zero spend);
inferred is `active=1, min_score=max(cfg.derived_min_score, owner floor),
positive_signals=[term]` (participates in matching against items owner
collectors already fetch — no new collector call); promotion past
`cfg.derived_max_active` inferred rows is skipped and logged as
`promotion_capped` rather than dropped silently. Config: exactly
`dynamic_interests`/`derived_max_active` (5)/`derived_min_score` (0.80,
at/above today's owner bars). CLI: `python -m app interests [--layer L]
[--why KEY] [--refresh]` — list (owner first), provenance chain, or run
`apply_transitions` (prints the off-message and changes nothing if the flag
is off). `interests.sync` now appends one 'owner_sync' event per interest.
Scheduling the refresh on a cadence is deferred to a later step — not
wired into `ops/install_tasks.py` here.

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

**E5 — connector evidence (step-09a + step-09, `exp_connectors.py`)**:
read-only recon for step-09's connector decision. step-09a's H1 pass (14
free HTTP requests, 0 provider calls) returned `VOID_NO_BASELINE`, but its
own records showed it had measured the query rule, not the connectors:
title+3-signals concatenated into 300 chars was over-constrained for
Algolia (hackernews 0 hits) and topically wrong for relevance-ranked
arxiv/pubmed. **DECIDED.** step-09's separately pre-registered H2 pass
(`PREREGISTRATION_PASS2`, frozen before any run) reran hackernews/arxiv/
pubmed under a corrected mechanical rule (`build_query_v2`: first 4
distinctive title tokens, `matching._tokens`' own rule) and measured
`usable_yield` — records built into a `CandidateItem` with `origin_interest`
UNSET (no free `ORIGIN_MATCH_FLOOR` pass) scoring ≥ `cfg.min_match_score`
via `matching.match_interests`. Real run (10 HTTP requests, 0 provider
calls): hackernews 2/20, arxiv 1/10 (2/3 queries timed out — recorded in the
new `aborted_attempts`/`verdict_detail`, not retried per "no re-runs"),
pubmed 6/20 — genuinely on-topic records (narcolepsy/orexin trials, EMDR
studies). **Mixed, not an aggregate improvement**: the new rule helped
hackernews (0→2) and pubmed (0→6) but arxiv REGRESSED (10→1, mostly from
those 2/3 timeouts cutting n from a designed 30 to 10) — pooled new-rule
yield is 9 vs the old rule's pooled 10. Under the identical USABLE
definition the old-rule arxiv arm alone already clears the gate's 8-record
bar (10 of 30); only the new-rule arm feeds the gate, per pre-registration,
and every connector there stays under it. **Verdict `H2_FALSIFIED`** —
decisive, not a shortfall.
`apply_promotion_gate` (G1 unique max ≥8, G2 ≥2x runner-up, G3
`marginal_unique_rate` ≥0.40 against a reachable corpus) returned
**`NO_PROMOTION` (G1: max=6)**; G3 was separately unreachable too
(`discovery.db` absent from this worktree). Dispositions: hackernews/arxiv/
pubmed `NOT_PROMOTED_VOID_BASELINE`; reddit `RETIRED_UNREACHABLE` (403 on
both step-09a's 5-interest sweep and step-09's one-request re-check —
`reddit_url`/`parse_reddit` deleted, `sample_reddit_pass2` now a zero-network
stub, so the current tree can no longer reproduce the persisted reddit
entry's one live HTTP call; see the dossier's own `reproducibility_note`);
x `DEFERRED_NEEDS_PROVIDER` (still needs a `provider.search_json`
sampler + a live operator session; only the unreplicated `x_prompt_lab`
prior exists). Gate returned NO_PROMOTION, so **no `discovery/` changes**.
Before a decisive promotion is possible: a reachable `discovery.db`, and a
`web_search` baseline sample (call the existing
`discovery/collectors/web_search.py` `collect()` from a live claude.ai
session — not a second sampler). `exp_connectors.py`'s local
canonicalization/percentile helpers still duplicate `exp_discovery.py`'s
(collapse once proposal 005 completes — unchanged this step). Dossier:
`experiments/lab/connector_evidence.json` (tracked, both passes).

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

## teach: information-value labeling queue (step-06)
`discovery/teach.py` (no new table, no LLM call) ranks already-scored,
not-yet-labeled items by expected `information` value — WEIGHTS-combined
bar proximity (gap to `interests.min_score`, decaying over `BAND_WIDTH`),
model self-uncertainty (`1 - confidence`), and per-interest label scarcity —
rationale: proposals 003/004 found notify flips track bar proximity, not
scorer variance, and corpus band_density is only .148. `build_queue`/
`baseline_queue`/`queue_metrics` compare the ranker against the honest
recency baseline over the same pool; both arms are always reported, even if
the baseline wins. `python -m app teach` is the interactive labeling loop
(records via the existing `db.add_feedback`, same call the Telegram
listener makes); `--list` prints without prompting; `--explain` prints
`queue_metrics`; `--send` pushes the top of the queue to Telegram by reusing
`notify.format_message`/`feedback_keyboard`/`send`, so labels come back
through the existing `listen`/`listen --drain` flow with no new callback
format. The acceptance evidence (`band_lift >= 2.0`, band_share strictly
higher) is measured on a **synthetic planted fixture** in `test_discovery.py`
(recency deliberately anti-correlated with bar proximity) — this worktree
has no `discovery.db`, so no real-corpus number is claimed. Live readout,
once `discovery.db` exists: `python -m app teach --explain --limit 20`.

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
`python test_discovery.py` (323) + `python test_watch.py` (10), offline, both
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

## product loop closure + anti-self-amplification guard (step-08)
The personal_state seed path (step-07) was already reachable from the CLI --
`apply_transitions()` itself calls `personal_state.load_optional()` and
`interests --refresh` already calls `apply_transitions()` -- so no new
wiring was needed. What was missing was provenance and a proven guard:

**Provenance.** Every seed's origin -- `origin='personal_state'`,
`artifact_sha256` (sha256 of the artifact file's bytes, read fresh at seed
time), the artifact's own `generated_at`/`contract_version`, the `topic_key`,
and `seeded_at` -- is now recorded on BOTH the interest's `provenance` JSON
column and its `interest_events` seed row (`interest_state._write_transition`'s
new `provenance_extra` param, threaded from `apply_transitions()`'s seed
loop). `interests --why <key>` already prints every event's evidence JSON
verbatim, so the seed origin shows up there with no CLI change needed. A
documented SQL query walking notification → score → item → interest →
interest_events → seed event (with the artifact hash) is in README.md's new
"Provenance chain" section, and a test executes that exact query and asserts
every hop resolves.

**Leakage guard (the core of this step).** `interest_state._window_stats()`
was a pure title-token count, blind to *why* an item exists -- it didn't
distinguish genuine independent corpus evidence from an item whose only
attribution (via `item_interests`) is the derived interest's own matching.
Structurally this can't yet fire in production (a lower-than-`inferred`
layer is never in `active_interests()`, so it can't have matched anything
yet), but a directly-constructed fixture proved the evidence-gathering path
itself would have counted it as promotion evidence once it could. Fixed:
`_window_stats()` now keeps per-item hits (not pre-aggregated counts), and
`apply_transitions()`'s step 3 (progression of already-tracked rows) excludes,
per term, any item whose ONLY `item_interests` row is a match to that same
derived interest (`_self_matched_item_ids()`). A test drives the real
`apply_transitions()` over 3 cycles of self-referential-only evidence with no
feedback: the row never leaves `exploratory`, no `promote` event appears, no
owner row or score row changes, and it demotes/retires on schedule once idle.
A companion test proves the positive path is untouched: independent
owner-collector evidence (no `item_interests` involved at all) plus feedback
via `db.add_feedback` promotes `exploratory` → `emerging` → `inferred`, one
rung per pass, and an above-bar match on the now-`inferred` interest is
delivered through the real pipeline (`pipeline.send_digest`).

**Default-off safety.** `test_default_off_is_a_true_noop` (step-07,
unmodified) still passes: with the flag off, `apply_transitions()` never
even calls `personal_state.load_optional()` or reads `item_interests`.

**Real-data posture.** This worktree has no `discovery.db`/`personal_state.json`
-- every test above is a synthetic fixture. The live-session command
sequence for the real loop is in README.md's "Real-data loop demo" section;
it has not been run here.

## exploration engine (step-10)
Exploitation (owner interests) and exploration (derived/inferred interests,
see "layered interest state (step-07)" above) are now separated at the
scoring boundary, not just at promotion time. Lane rule -- the one thing
everything hangs on: an item is 'explore' iff `matches[0]` (the strongest
match from `matching.match_interests()`, sorted strongest-first) is a
non-owner interest; `pipeline.classify_lane()` is the single, trivially-total
implementation, computed once per item before dedup so it's stable across
that item's whole `ingest()` path. A weaker derived match alongside a
stronger owner one still charges exploitation, byte-identical to before this
step.

Two `pipeline.Budget` instances per cycle (`run_once`/`__main__._discover`,
the only construction sites): the existing exploit one (`DISCOVERY_MAX_SCORES`)
and a new explore one (`explore_max_scores_per_cycle`, env
`DISCOVERY_EXPLORE_MAX_SCORES`, default `5`) -- `Budget(cfg.explore_max_scores_per_cycle
if cfg.dynamic_interests else 0)`, so the flag off makes it structurally
zero, not merely filtered. `ingest()`'s `explore_budget=None` kwarg default
preserves every pre-existing caller (`score`, `teach`, tests) untouched.
`_score_backlog()` takes both budgets and pages through the backlog with an
id cursor (repair: a single `ORDER BY id DESC LIMIT budget+explore_budget`
select could permanently starve the exploit lane -- lane is only known
after fetching+matching a row, so a batch that happened to be entirely
explore-classified while explore_budget was 0/spent would `continue` past
every row and return with the exploit backlog never even reached, and
since a lane-blocked item is deferred rather than attempted it re-occupies
that same newest-first window on every future cycle too); each page still
`continue`s (not `break`s) past an exhausted lane's rows so the other lane
keeps draining, and paging stops once both budgets are spent or a page
returns fewer rows than requested (backlog exhausted). `Outcome.lane`
(default 'exploit') drives `db.bump()`'s metric
name (`explore_<stage>` vs `<stage>`; 'collected' stays unprefixed --
collection is always owner-driven since a derived row's `sources` is always
`[]`) at all three per-item/trailing bump sites. `deliver()`/`send_digest()`
gained an optional `lane_counts` Counter (default None -- every existing
caller's plain-int return is unchanged) so `run_once` can bump
`notified`/`explore_notified` by the *actually persisted* score's interest
layer (a join, not `Outcome.lane` -- the model can pick a different
shortlisted interest than the match-time best).

`stats.py`: `_funnel`'s `notified` scalar and `_per_interest` now join
notifications -> scores -> interests and restrict to `layer = 'owner'` (a
real repair -- before this step a derived/inferred notification silently
inflated exploitation's own numbers). A new EXPLORATION section (interest
counts by layer, `explore_scored`/`explore_deferred`/`explore_errors`/
`explore_notified`, and a "NOTIFICATIONS PER DERIVED INTEREST" table shaped
like the owner one) prints only when there's a non-owner interest row, an
`explore_*` metric, or `cfg.dynamic_interests` -- a default-off report is
byte-identical to before this step existed.

**No new threshold.** `derived_min_score` (step-07, floor `0.80`) already
gates which derived scores can notify at all; this step only needed distinct
*budgets* and distinct *metrics*, not a distinct bar. Do not re-add one.

**Real-data posture.** This worktree has no `discovery.db` -- every number
in `test_discovery.py`'s `ExplorationLaneTests` is a synthetic in-memory
fixture. Live readout once dynamic interests are running for real:
`python -m app stats --days 7`, EXPLORATION section.
