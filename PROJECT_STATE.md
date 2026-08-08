# PROJECT_STATE.md — `internet`

Updated 2026-08-07. Imported by `CLAUDE.md`; maintained under its startup and
token-efficiency rules. Current state only — not a log, not an architecture doc.

## Implemented
- **`watch.py`** — watchlist price alerter, complete and working. 20 tests pass.
- **`discovery/`** — staged pipeline (collect → normalize → dedup → persist →
  match → pre-filter → LLM score → threshold → notify), 0–1 dimension scoring,
  provider abstraction. The rewrite is **finished**: the repo runs, and
  `python test_discovery.py` passes 59 tests.
- **`app/`** — thin alias package; `python -m app …` == `python -m discovery …`.
- **`discovery/collectors/web.py`** — generic web discovery collector
  (registered as `"web"`, distinct from `web_search`): one `complete_json`
  call turns an interest's description/positive_signals into a few query
  variations, then one `search_json` call per query, scoped by a
  `source_config["web"]` recency window. `metadata["query"]` on each
  `CandidateItem` records which query found it. `python -m app discover web`
  runs it across every active interest and prints candidates + scores
  (dedup/prefilter/scoring included) without ever calling `deliver()`.

## Non-obvious decisions
- `final_score` is computed in code from `models.WEIGHTS`, never returned by the
  model; `specificity` is scored and stored but deliberately unweighted so
  ranking can change without re-scoring.
- One score row per item (`UNIQUE(item_id)`): the scorer picks the most relevant
  interest itself from the shortlist `matching.py` supplies.
- Dedup is three layers — canonical-URL hash, title hash, content hash (only for
  bodies ≥ 200 chars) — checked *before* insert; `(source, dedup_key)` is the
  in-collector backstop.
- Pre-filter verdicts persist on `candidate_items.prefilter_ok/_reason`, so a
  rejected item is never re-filtered or re-sent to an LLM.
- `__main__.py` builds the provider **lazily** — `init`/`items`/`feedback` must
  work without an API key.
- `ingest(..., force=…)` bypasses dedup and an existing score, but *not* the
  pre-filter. `score --item-id` reaches `already_scored` because an item that
  carries its own id is excluded from its own dedup lookup.
- `interests.load_file` divides any `min_score > 1` by 100 — the 0–100 scale is
  gone, and a stale `75` would otherwise silently mean "never notify".
- `discovery` imports `watch` (dotenv loader, Yahoo fetch) — **only resolves
  when run from the repo root**.
- `watch.py`'s `SCHEDULES` counts lookback in trading bars, not calendar time.
- The threshold is applied in SQL, not Python: `db.py` selects
  `WHERE s.final_score >= n.min_score`, both on the 0–1 scale.
- `run-once --dry-run` runs the whole cycle — collect, score, persist — and
  skips only the pushes. It still calls the live LLM provider, so it is not a
  zero-cost rehearsal.

## Adding a collector (the recurring task — no file reads needed)
Three files: `discovery/collectors/<name>.py` with
`collect(interest, cfg, provider) -> list[CandidateItem]`; one line in
`discovery/collectors/__init__.py`'s `COLLECTORS` dict (`"web_search"`,
`"youtube"`, `"stocks"` → each module's `collect`); a test in
`test_discovery.py` with a fake provider. `CandidateItem` (`discovery/models.py`)
is a dataclass: required `source, type, title, url`; optional `text, author,
published_at, metadata, dedup_key, origin_interest`. `url_hash`/`title_hash`/
`content_hash`/`id` are filled in later by `normalize.py` — a collector leaves
them unset.

## Known issues
- **Nothing in `discovery/` has ever run against a live API** — every run so far
  used a fake provider. First live run is the real smoke test.
- `youtube` collector is a stub returning `[]`.
- `web_search` needs the `anthropic` provider; the openai one raises
  `UnsupportedCapability` and that collector is skipped.
- No `watchlist-*` scheduled tasks registered on this machine.

## Uncommitted (branch `main`, dirty)
Modified: `.env.example`, `.gitignore`, `README.md`, `requirements.txt`.
Untracked — i.e. the whole discovery engine has never been committed:
`discovery/`, `app/`, `interests.json`, `test_discovery.py`, `CLAUDE.md`,
`PROJECT_STATE.md`.

## Next task
1. Branch off `main`, commit the discovery engine, open a PR. Nothing is
   committed yet.
2. First live run: `.env` with a real `ANTHROPIC_API_KEY`, then
   `python -m app score --url … --title … --text …` on one known-good article
   before letting `run-once` loose on the API.

## Commands to continue
```bash
python test_discovery.py && python test_watch.py
python -m app init
python -m app score --url https://example.com/x --title "..." --text "..."
python -m app run-once --dry-run
```
