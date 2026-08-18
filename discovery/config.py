"""Environment-variable configuration. No config file, no framework.

Values are read from the process environment first, then from the repo's
gitignored `.env` -- the same precedence (and the same loader) watch.py uses,
so CI secrets always win over the local file.
"""
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Per-provider default, so switching DISCOVERY_PROVIDER alone gives a sane model.
# chatgpt_browser's "latest-high" is a sentinel the provider resolves live to
# chatgpt.com's newest version at its High (max-reasoning) preset -- so it stays
# on the latest model with no code change; a concrete DISCOVERY_MODEL overrides.
DEFAULT_MODELS = {
    "claude_chat": "claude-opus-5",
    "chatgpt_browser": "latest-high",
    "anthropic": "claude-opus-5",
    "openai": "gpt-5",
}


@dataclass
class Config:
    db_path: str
    interests_path: str
    provider: str
    model: str
    max_items_per_source: int
    min_match_score: float      # pre-filter: weakest interest match worth scoring
    min_text_chars: int         # pre-filter: least text worth sending to an LLM
    telegram_bot_token: str
    telegram_chat_id: str
    youtube_api_key: str = ""   # YouTube Data API v3 key; only the youtube collector needs it
    max_scores_per_cycle: int = 25   # hard ceiling on LLM scoring calls per run_once()
    personal_state_path: str = ""    # ai repo's derived contract artifact; see discovery/personal_state.py
    # The contract-v2 artifact carrying interest candidates (same reader,
    # same fail-soft posture). Separate path from personal_state.json because
    # the producer publishes them on different cadences -- nightly map vs
    # weekly reduce. Gitignored, inbound, never committed.
    interest_candidates_path: str = ""
    # Extra read-only SQLite files mounted alongside discovery.db in the
    # Observatory's Datasette, so another project's data is browsable in the
    # SAME UI instead of needing a second server on another port. Semicolon
    # separated -- Windows paths contain ':', so ';' is the only safe
    # splitter. A missing file is skipped, never fatal: a sibling repo being
    # absent must not stop the Observatory from starting.
    ui_extra_dbs: tuple = ()
    # Per-job cadence. No in-process scheduler reads these anymore (see
    # health.py/__main__.py's "run" removal) -- the OS scheduler's triggers
    # are derived from these same fields instead.
    interval_stocks_seconds: int = 3600
    # Continuous Council-driven web discovery (see below) runs as a 1-minute
    # tick, not a periodic batch collector -- collect-web's Scheduled Task
    # cadence follows this field down to 60s (see ops/install_tasks.py).
    interval_web_seconds: int = 60
    interval_youtube_seconds: int = 4 * 3600
    digest_time: str = "08:00"      # local HH:MM, first digest of the day
    digest_max_items: int = 10      # Discovery items per digest; Alerts are unbounded and immediate
    # Repeats the digest every digest_interval_seconds from digest_time until
    # digest_window_end, same day, so pending items drain throughout waking
    # hours instead of piling up for one 8am shot. See ops/install_tasks.py
    # (Task Scheduler's own Repetition+Duration on the CalendarTrigger).
    digest_interval_seconds: int = 3600
    digest_window_end: str = "23:00"    # local HH:MM, last digest slot of the day

    # --- the interest-suggestion pipeline's own cadence (2026-08-18) ---
    # Three jobs, three very different costs, so three separate fields rather
    # than one shared "interest" interval. Until now NOTHING scheduled any of
    # them: the extractor only ever ran by hand, so no new suggestion was ever
    # produced, and -- the quiet one -- with no sweep timer every lifecycle
    # rule in the design (30d decay, 45d auto-pause, offer expiry, snooze
    # wake-up) was inert. A Stop button that can never fire is not a feature.
    #
    # interest_extract_time is a plain daily HH:MM, NOT a windowed repeat like
    # the digest: this is the one job here that spends an LLM budget and holds
    # a claude.ai tab for minutes at a time, and `map` is incremental, so a
    # second run the same day re-reduces the same digests for nothing. 03:30
    # is deliberate -- outside the digest window (digest_time..digest_window_end),
    # and at an hour the owner is not using Chrome interactively.
    interest_extract_time: str = "03:30"
    # Local, offline, and idempotent on the artifact's sha256, so running it
    # hourly costs a file hash and a state lookup. Hourly (not daily) so an
    # artifact produced by hand -- which is exactly how the first five offers
    # arrived -- reaches the inbox within the hour instead of the next night.
    offers_import_interval_seconds: int = 3600
    # Local and offline too. The brief is "at least daily"; 6h is four
    # chances a day, so a machine asleep or off through one slot still sweeps
    # that day instead of silently skipping a day of the 30/45-day clocks.
    offers_sweep_interval_seconds: int = 6 * 3600
    # Wall-clock budget handed to the extractor's `map` stage. `map` is
    # checkpointed per batch and resumable, so hitting this costs at most one
    # batch and the next night resumes -- and stopping map on a deadline is
    # what guarantees `reduce` still gets to run and publish an artifact.
    interest_extract_map_seconds: int = 60 * 60
    interest_extract_reduce_seconds: int = 20 * 60
    # Immediate discovery delivery (opt-in). Off by default: DISCOVERY items
    # wait for the daily digest, exactly as before. On (DISCOVERY_IMMEDIATE=1),
    # deliver() also pushes freshly-scored above-bar discoveries the moment a
    # cycle finds them -- bounded three ways so a backlog or a burst can't flood
    # the owner: only scores newer than `immediate_fresh_seconds` (the existing
    # backlog is never immediately sent), at most `immediate_max_per_cycle` per
    # deliver() call, and at most `immediate_max_per_day` successful sends in a
    # rolling 24h. Anything skipped by a cap still reaches the daily digest.
    immediate_discovery: bool = False
    immediate_max_per_cycle: int = 3
    immediate_max_per_day: int = 40
    immediate_fresh_seconds: int = 1800   # only discoveries scored in the last 30 min go out immediately
    # LLM-confirmed near-duplicate detection (dedup.llm_near_duplicate). The
    # exact-hash layers catch re-posts; this catches the same story re-told in
    # different words ("VPG down 25%" from three outlets). A judge call is only
    # spent when free lexical retrieval finds suspects among recently stored
    # articles, and a confirmed repeat skips the larger scoring call it would
    # otherwise buy. The window bounds how far back retrieval looks (cost),
    # not what counts as a duplicate -- the judge sees dates and decides.
    dedup_llm: bool = True
    dedup_window_days: int = 30
    dedup_max_candidates: int = 6
    # Failed-send retry policy (see db.pending_notifications); raised from the
    # smaller db.py module constants, which stay as that function's own
    # defaults so a call site that doesn't pass cfg still gets a sane policy.
    send_max_attempts: int = 5
    send_retry_seconds: int = 30 * 60
    # Provider preflight (see health.py). Empty command = never spawn
    # anything -- the default, and what every test and other machine gets.
    chrome_launch_cmd: str = ""
    chrome_launch_wait_seconds: int = 15
    # `health` staleness + alert throttling.
    health_stale_factor: int = 3
    health_alert_cooldown_seconds: int = 6 * 3600
    # Layered interest state (discovery/interest_state.py). Off by default --
    # with it off, apply_transitions() is a no-op and no derived interest is
    # ever created, matched or scored. See PROJECT_STATE.md.
    dynamic_interests: bool = False
    derived_max_active: int = 5
    derived_min_score: float = 0.80
    # Exploration lane (step-10): a separate per-cycle LLM budget for items
    # whose best interest match is non-owner (derived). No new threshold --
    # derived_min_score above already bars notification; see PROJECT_STATE.md.
    explore_max_scores_per_cycle: int = 5
    # Continuous Council-driven web discovery (discovery/council.py,
    # discovery/missions.py). See PROJECT_STATE.md for the tick's shape.
    council_missions_per_generation: int = 6    # missions requested per Council call
    mission_low_water: int = 3                  # replenish an interest below this many PENDING
    missions_per_tick: int = 2                   # missions leased+executed per web_tick()
    mission_max_searches: int = 6                # search_json's own max_searches, per mission
    mission_max_results: int = 6                 # CandidateItems kept per mission
    council_frontier_items: int = 15             # recent candidate_items shown to the Council
    council_feedback_items: int = 10             # recent feedback rows shown to the Council
    council_history_missions: int = 12           # recent missions (label+rationale) shown back
    mission_lease_seconds: int = 900             # RUNNING lease before recover_stale_missions() reclaims it
    mission_max_attempts: int = 3                # attempts before a mission is retired to FAILED
    mission_retry_seconds: int = 1800            # cool-off before a failed mission is retried
    council_max_consecutive_failures: int = 3    # generation failures before the static fallback kicks in
    mission_provider: str = "chatgpt_browser"    # search-capable provider the tick executes missions with
    mission_model: str = ""                      # resolved from DEFAULT_MODELS at load() time
    # Second provider tried when the primary raises ProviderError (see
    # providers/fallback.py). "" (the default) means no fallback; applies to
    # the scoring provider AND, via missions.py's dataclasses.replace(), the
    # mission provider.
    provider_fallback: str = ""
    provider_fallback_model: str = ""            # resolved from DEFAULT_MODELS at load() time
    # Trace backbone (discovery/trace.py). On by default -- the rollback
    # lever is turning it off, not deleting anything. observatory_base_url
    # is unused by this task (task 1 is storage + instrumentation only) but
    # threaded through now so the Datasette plugin/UI (tasks 2-3) don't need
    # a config change to find where their own read-only views live.
    trace_enabled: bool = True
    observatory_base_url: str = ""
    # Observatory server (discovery/../observatory/, step-13 task 2). Empty
    # ui_token is fine for `ui`'s default localhost-bound mode; `ui --public`
    # refuses to start without one (see __main__.py's `_ui_cmd`) -- there is
    # no default token, on purpose, so public exposure always requires an
    # explicit operator choice. ngrok_cmd is likewise empty by default (never
    # spawns anything) and only read by `ui --public`.
    ui_token: str = ""
    ngrok_cmd: str = ""


def load():
    # watch.py lives at the repo root, so this import only resolves when the
    # process was started from there (`python -m app ...`).
    from watch import load_dotenv

    load_dotenv(str(REPO_ROOT / ".env"))
    provider = os.environ.get("DISCOVERY_PROVIDER", "claude_chat")
    mission_provider = os.environ.get("DISCOVERY_MISSION_PROVIDER", "chatgpt_browser")
    provider_fallback = os.environ.get("DISCOVERY_PROVIDER_FALLBACK", "")
    return Config(
        db_path=os.environ.get("DISCOVERY_DB", str(REPO_ROOT / "discovery.db")),
        interests_path=os.environ.get(
            "DISCOVERY_INTERESTS", str(REPO_ROOT / "interests.json")
        ),
        provider=provider,
        model=os.environ.get("DISCOVERY_MODEL", DEFAULT_MODELS.get(provider, "")),
        max_items_per_source=int(os.environ.get("DISCOVERY_MAX_ITEMS", "8")),
        min_match_score=float(os.environ.get("DISCOVERY_MIN_MATCH", "0.25")),
        min_text_chars=int(os.environ.get("DISCOVERY_MIN_TEXT_CHARS", "120")),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY", ""),
        max_scores_per_cycle=int(os.environ.get("DISCOVERY_MAX_SCORES", "25")),
        personal_state_path=os.environ.get(
            "DISCOVERY_PERSONAL_STATE", str(REPO_ROOT / "personal_state.json")
        ),
        interest_candidates_path=os.environ.get(
            "DISCOVERY_INTEREST_CANDIDATES", str(REPO_ROOT / "interest_candidates.json")
        ),
        ui_extra_dbs=tuple(
            part.strip()
            for part in os.environ.get("DISCOVERY_UI_EXTRA_DBS", "").split(";")
            if part.strip()
        ),
        interval_stocks_seconds=int(os.environ.get("DISCOVERY_INTERVAL_STOCKS", "3600")),
        interval_web_seconds=int(os.environ.get("DISCOVERY_INTERVAL_WEB", "60")),
        interval_youtube_seconds=int(os.environ.get("DISCOVERY_INTERVAL_YOUTUBE", str(4 * 3600))),
        digest_time=os.environ.get("DISCOVERY_DIGEST_TIME", "08:00"),
        digest_max_items=int(os.environ.get("DISCOVERY_DIGEST_MAX", "10")),
        digest_interval_seconds=int(os.environ.get("DISCOVERY_DIGEST_INTERVAL", "3600")),
        digest_window_end=os.environ.get("DISCOVERY_DIGEST_WINDOW_END", "23:00"),
        interest_extract_time=os.environ.get("DISCOVERY_INTEREST_EXTRACT_TIME", "03:30"),
        offers_import_interval_seconds=int(
            os.environ.get("DISCOVERY_OFFERS_IMPORT_INTERVAL", "3600")
        ),
        offers_sweep_interval_seconds=int(
            os.environ.get("DISCOVERY_OFFERS_SWEEP_INTERVAL", str(6 * 3600))
        ),
        interest_extract_map_seconds=int(
            os.environ.get("DISCOVERY_INTEREST_EXTRACT_MAP_SECONDS", str(60 * 60))
        ),
        interest_extract_reduce_seconds=int(
            os.environ.get("DISCOVERY_INTEREST_EXTRACT_REDUCE_SECONDS", str(20 * 60))
        ),
        immediate_discovery=os.environ.get("DISCOVERY_IMMEDIATE", "").strip().lower()
        in ("1", "true"),
        immediate_max_per_cycle=int(os.environ.get("DISCOVERY_IMMEDIATE_MAX_PER_CYCLE", "3")),
        immediate_max_per_day=int(os.environ.get("DISCOVERY_IMMEDIATE_MAX_PER_DAY", "40")),
        immediate_fresh_seconds=int(os.environ.get("DISCOVERY_IMMEDIATE_FRESH_SECONDS", str(30 * 60))),
        dedup_llm=os.environ.get("DISCOVERY_DEDUP_LLM", "1").strip().lower() in ("1", "true"),
        dedup_window_days=int(os.environ.get("DISCOVERY_DEDUP_WINDOW_DAYS", "30")),
        dedup_max_candidates=int(os.environ.get("DISCOVERY_DEDUP_MAX_CANDIDATES", "6")),
        send_max_attempts=int(os.environ.get("DISCOVERY_SEND_MAX_ATTEMPTS", "5")),
        send_retry_seconds=int(os.environ.get("DISCOVERY_SEND_RETRY_SECONDS", str(30 * 60))),
        chrome_launch_cmd=os.environ.get("DISCOVERY_CHROME_LAUNCH_CMD", ""),
        chrome_launch_wait_seconds=int(
            os.environ.get("DISCOVERY_CHROME_LAUNCH_WAIT_SECONDS", "15")
        ),
        health_stale_factor=int(os.environ.get("DISCOVERY_HEALTH_STALE_FACTOR", "3")),
        health_alert_cooldown_seconds=int(
            os.environ.get("DISCOVERY_HEALTH_ALERT_COOLDOWN_SECONDS", str(6 * 3600))
        ),
        dynamic_interests=os.environ.get("DISCOVERY_DYNAMIC_INTERESTS", "").strip().lower()
        in ("1", "true"),
        derived_max_active=int(os.environ.get("DISCOVERY_DERIVED_MAX_ACTIVE", "5")),
        derived_min_score=float(os.environ.get("DISCOVERY_DERIVED_MIN_SCORE", "0.80")),
        explore_max_scores_per_cycle=int(os.environ.get("DISCOVERY_EXPLORE_MAX_SCORES", "5")),
        council_missions_per_generation=int(
            os.environ.get("DISCOVERY_COUNCIL_MISSIONS_PER_GENERATION", "6")
        ),
        mission_low_water=int(os.environ.get("DISCOVERY_MISSION_LOW_WATER", "3")),
        missions_per_tick=int(os.environ.get("DISCOVERY_MISSIONS_PER_TICK", "2")),
        mission_max_searches=int(os.environ.get("DISCOVERY_MISSION_MAX_SEARCHES", "6")),
        mission_max_results=int(os.environ.get("DISCOVERY_MISSION_MAX_RESULTS", "6")),
        council_frontier_items=int(os.environ.get("DISCOVERY_COUNCIL_FRONTIER_ITEMS", "15")),
        council_feedback_items=int(os.environ.get("DISCOVERY_COUNCIL_FEEDBACK_ITEMS", "10")),
        council_history_missions=int(os.environ.get("DISCOVERY_COUNCIL_HISTORY_MISSIONS", "12")),
        mission_lease_seconds=int(os.environ.get("DISCOVERY_MISSION_LEASE_SECONDS", "900")),
        mission_max_attempts=int(os.environ.get("DISCOVERY_MISSION_MAX_ATTEMPTS", "3")),
        mission_retry_seconds=int(os.environ.get("DISCOVERY_MISSION_RETRY_SECONDS", "1800")),
        council_max_consecutive_failures=int(
            os.environ.get("DISCOVERY_COUNCIL_MAX_CONSECUTIVE_FAILURES", "3")
        ),
        mission_provider=mission_provider,
        mission_model=os.environ.get(
            "DISCOVERY_MISSION_MODEL", DEFAULT_MODELS.get(mission_provider, "")
        ),
        provider_fallback=provider_fallback,
        provider_fallback_model=os.environ.get(
            "DISCOVERY_PROVIDER_FALLBACK_MODEL", DEFAULT_MODELS.get(provider_fallback, "")
        ),
        trace_enabled=os.environ.get("DISCOVERY_TRACE", "1").strip().lower() in ("1", "true"),
        observatory_base_url=os.environ.get("DISCOVERY_OBSERVATORY_BASE_URL", ""),
        ui_token=os.environ.get("DISCOVERY_UI_TOKEN", ""),
        ngrok_cmd=os.environ.get("DISCOVERY_NGROK_CMD", ""),
    )
