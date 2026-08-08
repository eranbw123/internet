# CLAUDE.md — `internet`

Repo contains `discovery/` (personal internet discovery) and `watch.py`, a
shared Yahoo Finance helper library the `stocks` collector calls into — not a
standalone tool with its own flow.

@PROJECT_STATE.md

## Work from maintained context

CLAUDE.md + PROJECT_STATE.md are the authoritative starting context and are already loaded.

- Do not rediscover, map, summarize, or broadly scan the repo.
- Trust PROJECT_STATE.md by default; verify only code relevant to the current task.
- Read the smallest possible set of files/regions needed to implement the task.
- Prefer targeted Grep / Read; no tree, recursive globs, explorer agents, or orientation sweeps.
- Do not open files just to learn project structure already documented in maintained context.
- Widen exploration only when required information is genuinely missing or touched code contradicts maintained state.
- Implement directly, run only the relevant tests, and stop. No summary unless asked.
- Update PROJECT_STATE.md after meaningful implementation or architecture changes; keep it concise (<500 words).

## Core constraints

- Python 3.14; run from repo root — `discovery` imports `watch`.
- `watch.py` stays stdlib-only and library-only: no CLI, no notification
  channel of its own. `stocks.py` and `discovery/config.py` are its only
  callers; alerting goes out through the discovery pipeline's Telegram flow.
- SQLite (`discovery.db`) is the discovery engine's only store.
- Vendor SDKs belong only in `discovery/providers/`; provider and model come from `DISCOVERY_PROVIDER` / `DISCOVERY_MODEL` (default `claude_chat` + `claude-opus-5` — claude.ai via an authenticated Chrome tab over CDP, no API key; `anthropic` is the opt-in direct-API path).
- Secrets come from environment / `.env`; never hardcode them.

## Discovery

- Provider boundary is `LLMProvider` (`complete_json` / `search_json`); a missing capability raises `UnsupportedCapability` and is skipped.
- Collector interface: `collect(interest, cfg, provider, conn=None) -> list[CandidateItem]`, registered in `discovery/collectors/__init__.py`. `conn` is read-only, only for `db.seen_dedup_keys` skip checks.
- Collectors fetch/shape only; normalization, dedup, relevance, scoring, and notification belong downstream.
- Pipeline remains explicit and resumable; persist decisions so work is not repeated.
- Never pay an LLM/API call for a candidate dedup will discard; check before spending, not after.
- Every LLM call is bounded: per-cycle score budget, per-source item caps.
- The model rates; code ranks.
- Failures are isolated and skipped rather than killing the cycle.
- Anything needed to judge whether the system is working goes through `stats.py`; counters are written as the pipeline runs.

## watch.py

- Comparison windows use trading bars, not calendar time.
- Library only: `price_change`/`WatchError`/`load_dotenv` are its public
  surface. Don't add back a CLI, ntfy, or any other standalone flow — a
  ticker move is just another discovery candidate, notified the same way as
  everything else (see `stocks` collector, `internet/CLAUDE.md` Discovery section).

## Testing

Run only:

```bash
python test_watch.py
python test_discovery.py
```

Tests stay fully offline. Use existing test files and existing fake-provider / patched-collector seams.

## Keep it simple

No ORM, migration framework, job queue, async rewrite, plugin framework, collector base classes, or abstractions for single call sites unless the task clearly requires one.

README is user-facing documentation. PROJECT_STATE.md stores only concise implementation state needed by future sessions.
