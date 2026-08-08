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
DEFAULT_MODELS = {"anthropic": "claude-opus-5", "openai": "gpt-5"}


@dataclass
class Config:
    db_path: str
    interests_path: str
    provider: str
    model: str
    max_items_per_source: int
    interval_seconds: int
    min_match_score: float      # pre-filter: weakest interest match worth scoring
    min_text_chars: int         # pre-filter: least text worth sending to an LLM
    telegram_bot_token: str
    telegram_chat_id: str


def load():
    # watch.py lives at the repo root, so this import only resolves when the
    # process was started from there (`python -m app ...`).
    from watch import load_dotenv

    load_dotenv(str(REPO_ROOT / ".env"))
    provider = os.environ.get("DISCOVERY_PROVIDER", "anthropic")
    return Config(
        db_path=os.environ.get("DISCOVERY_DB", str(REPO_ROOT / "discovery.db")),
        interests_path=os.environ.get(
            "DISCOVERY_INTERESTS", str(REPO_ROOT / "interests.json")
        ),
        provider=provider,
        model=os.environ.get("DISCOVERY_MODEL", DEFAULT_MODELS.get(provider, "")),
        max_items_per_source=int(os.environ.get("DISCOVERY_MAX_ITEMS", "8")),
        interval_seconds=int(os.environ.get("DISCOVERY_INTERVAL", "3600")),
        min_match_score=float(os.environ.get("DISCOVERY_MIN_MATCH", "0.25")),
        min_text_chars=int(os.environ.get("DISCOVERY_MIN_TEXT_CHARS", "120")),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
