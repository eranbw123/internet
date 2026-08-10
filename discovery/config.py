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
DEFAULT_MODELS = {"claude_chat": "claude-opus-5", "anthropic": "claude-opus-5", "openai": "gpt-5"}


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
    # Per-job cadence. No in-process scheduler reads these anymore (see
    # health.py/__main__.py's "run" removal) -- the OS scheduler's triggers
    # are derived from these same fields instead.
    interval_stocks_seconds: int = 3600
    interval_web_seconds: int = 4 * 3600
    interval_youtube_seconds: int = 4 * 3600
    digest_time: str = "08:00"      # local HH:MM, once per day
    digest_max_items: int = 10      # Discovery items per digest; Alerts are unbounded and immediate
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


def load():
    # watch.py lives at the repo root, so this import only resolves when the
    # process was started from there (`python -m app ...`).
    from watch import load_dotenv

    load_dotenv(str(REPO_ROOT / ".env"))
    provider = os.environ.get("DISCOVERY_PROVIDER", "claude_chat")
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
        interval_stocks_seconds=int(os.environ.get("DISCOVERY_INTERVAL_STOCKS", "3600")),
        interval_web_seconds=int(os.environ.get("DISCOVERY_INTERVAL_WEB", str(4 * 3600))),
        interval_youtube_seconds=int(os.environ.get("DISCOVERY_INTERVAL_YOUTUBE", str(4 * 3600))),
        digest_time=os.environ.get("DISCOVERY_DIGEST_TIME", "08:00"),
        digest_max_items=int(os.environ.get("DISCOVERY_DIGEST_MAX", "10")),
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
    )
