# PROJECT_STATE.md — `internet`

Updated 2026-08-08 (`web` collector removed). Imported by `CLAUDE.md`.
Current state only — not a log.

## `web` collector removed (2026-08-08)
Deleted `discovery/collectors/web.py` (LLM-generates all queries blind, then
one `search_json` call per query) and its `COLLECTORS`/import entries. No
interest configured it — all use `web_search` — and `web_search`'s freeform
single-pass search (model sees each search's results before writing the next
query) is strictly preferable on the claude_chat transport: adaptive, cheaper
(1 scratch conversation vs 1 query-gen + N search conversations), and stopping
early on a quiet week is correct. Its one edge, `metadata["query"]`
provenance, solved a problem `stats.py`'s funnel already covers (a
persistently-empty source shows up there). Removed: the module, its 4 tests in
`test_discovery.py`, README's `web` row and `discover web` mentions,
`_search.py`'s docstring reference, `youtube.py`'s stray comment pointer.
Kept: `DISCOVERY_INTERVAL_WEB`/`interval_web_seconds` and the scheduler's
`"web"` job name — that cadence bucket still drives `web_search` (renamed
nothing, just fixed the comments/docstrings to say so). `_search.py` (shared
`RESULT_SPEC`/`to_items`) stays — `web_search` and `stocks` still use it.
`test_discovery.py`: 141 tests (was 145). Recoverable from git history
(~100 lines) if ever needed again.

## watch.py is now library-only (2026-08-08)
Removed its standalone CLI/alerter flow (`main`, `run`, `ntfy_notify`,
`load_watchlist`, `format_line`, `watchlist.json`/`.example.json`, the
`NTFY_TOPIC`/`NTFY_BASE` env vars) — it was a second, redundant notification
path (ntfy) alongside discovery's Telegram flow. `watch.py` now only exports
`price_change`/`fetch_chart`/`WatchError`/`load_dotenv`, i.e. exactly what
`discovery/collectors/stocks.py` and `discovery/config.py` already called.
A ticker move surfaces the same way as everything else discovery finds: the
`stocks` collector → normal pipeline → Telegram ALERT. `test_watch.py` trimmed
to the surviving surface (10 tests, was 20). README/`.env.example`/`.gitignore`
updated to match; `watchlist.example.json` deleted. The user's local
`watchlist.json` (gitignored, untracked) was left untouched — it's just no
longer read by anything.

## claude_chat migration (2026-08-08)
Default provider is now `claude_chat` (`discovery/providers/claude_chat.py`):
claude.ai driven inside an authenticated Chrome tab over CDP — no
`ANTHROPIC_API_KEY`. Pattern reused from `../ai`'s `council_bot.py` browser
backend; `discovery/providers/cdp.py` is vendored verbatim from `../ai/cdp.py`
(keep in sync). Needs Chrome `--remote-debugging-port=9222` (+ logged-in
claude.ai tab) and `CLAUDE_ORG_ID` in `.env`. `anthropic` (direct API) and
`openai` remain opt-in; `anthropic` SDK line commented out in requirements.
- No structured outputs on claude.ai: `complete_json` prompts for strict JSON,
  slices first `{`…last `}`, validates required/type/enum against the caller's
  schema, retries once. `search_json` enables claude.ai's `web_search_v0` tool;
  `max_searches` is a prompt instruction, not a hard cap.
- One scratch conversation per call (create → completion SSE → delete);
  dropped CDP connection reconnects once. Missing org id / Chrome / tab are
  clean ProviderErrors.
- No token metering on this transport: usage records calls only; `stats`
  reports claude_chat as subscription-covered, never a dollar figure (mixed
  API-billed rows still get totalled).

## Fixed in this pass (former known issues)
- Failed Telegram sends are no longer consumed: `notifications.attempts`
  column; retry after 15-min cool-off, max 3 attempts (`db.MAX_SEND_ATTEMPTS`),
  success final, no duplicates. `pending_notifications` encodes the policy.
- Backlog rescore backoff: `candidate_items.score_attempted_at` stamped on
  scoring failure; backlog skips items attempted within 30 min
  (`db.SCORE_RETRY_SECONDS`). Never lost, just paced.
- Both columns added via guarded `ALTER TABLE` in `db.init` (schema.sql stays
  CREATE-IF-NOT-EXISTS-only).

## Verification (2026-08-08)
- 145 offline tests (`test_discovery.py`, +10: AnthropicProviderTests,
  OpenAIProviderTests -- the two opt-in providers had zero coverage; both take
  an injectable `client=`, same seam as `ClaudeChatProvider`'s `connect=`, so
  fake clients cover them with no SDK install), 10 (`test_watch.py`, trimmed
  to library surface — see watch.py section above).
- `.github/workflows/tests.yml` runs both suites on every PR/push to `main`
  (ubuntu, Python 3.14, no pip install -- both suites are stdlib-only at
  import time).
- 41/41-check E2E simulation rerun with the REAL ClaudeChatProvider over a
  scripted fake CDP connection: full funnel, alert + digest, Telegram-outage
  retry, backlog cool-off, feedback, stats, idempotent cycles, dead-provider
  isolation.
- LIVE smoke via real claude.ai session: contrast scoring — narcolepsy strong
  0.76 vs generic sleep-hygiene 0.03; NBIS material deal 0.85 vs generic AI
  commentary 0.05; behavioral research 0.78 vs dating advice 0.03. Live
  `discover web` cycle: query-gen → real web search → 2 current items scored
  (0.65 FDA approval, 0.46 year-old recap). Scorer is directionally sane.
- Note: over-specific generated queries + short recency windows legitimately
  return `[]`; widen `recency_days`/`num_queries` per interest if a cycle
  finds nothing.

## Implemented
- `watch.py` — shared Yahoo Finance helper library (no CLI/notification of
  its own); live Yahoo path verified.
- `discovery/` — staged pipeline, 0–1 dimension scoring, provider abstraction
  (`claude_chat`/`anthropic`/`openai`), score budget, backlog rescore w/
  backoff, funnel + llm_usage metrics, `stats.py`.
- Collectors: `web_search`, `web` (LLM query-gen), `stocks` (daily/weekly
  thresholds, search+grade catalyst — confirmed/plausible/none), `youtube`.
  Collectors take read-only `conn` for skip-before-spend.
- Telegram: ALERT immediate vs DISCOVERY digest; feedback buttons →
  `feedback_listener`; failed sends retried (see above).
- Scheduler: 60s tick loop; per-job cadences + daily digest.

## Non-obvious decisions
- `final_score` computed in code from `models.WEIGHTS`; one score row per item.
- Dedup: URL/title/content hashes + `(source, dedup_key)`; prefilter persists.
- Threshold in SQL (`final_score >= interests.min_score`).
- Provider built lazily; `init`/`items`/`feedback`/`stats` need no session/key.
- Run from repo root — `discovery` imports `watch`. CLI global flags before
  the subcommand.

## Known issues
- claude.ai endpoints are internal/undocumented — payload shapes can drift;
  heavy automated use sits uneasily with claude.ai ToS (volume bounded by
  `DISCOVERY_MAX_SCORES`). Live Telegram delivery and YouTube still unverified
  (no creds in `.env`).

## Next task
PR #1 (branch `add-discovery-engine`, commit a7ea1a9) is open with full-scope
title/body — awaiting merge. Then: add Telegram creds and let `run` cycle for
a few days; then `stats --days 7`.

## Commands to continue
```bash
# once per boot: chrome --remote-debugging-port=9222  (+ log into claude.ai)
python test_discovery.py && python test_watch.py
python -m app init
python -m app score --url https://example.com/x --title "..." --text "..."
python -m app --dry-run run-once
python -m app run      # scheduler
python -m app listen   # Telegram feedback buttons (separate process)
python -m app stats --days 7
```
