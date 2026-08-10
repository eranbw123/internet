"""Reader for the `ai` repo's personal-state contract artifact.

The contract itself is owned and documented by `ai`'s `PERSONAL_STATE_CONTRACT.md`
(that file is authoritative -- do not copy its schema here, it would drift).
This module is the ONLY place in `internet` that knows the artifact's shape;
nothing else in this repo should parse `personal_state.json` directly, open
`ai`'s `conversations.db`, or import from `ai`.

v1 shape (per `PERSONAL_STATE_CONTRACT.md`):
    {"contract_version": 1, "generated_at": "<UTC ISO-8601 Z>",
     "window_days": <int>, "conversation_count": <int>,
     "sources": {"claude": <int>, "chatgpt": <int>},
     "topics": [{"key": "<token>", "weight": <float 0..1>,
                 "conversations": <int>, "last_seen": "<iso>"}]}

Forward compatibility: unknown top-level keys and unknown per-topic keys are
ignored, never rejected -- `ai` may add optional fields without a version
bump. Topics are consumed in the order given; never re-sorted here.
"""
import json
import sys
from dataclasses import dataclass, field

SUPPORTED_VERSIONS = frozenset({1})


class PersonalStateError(Exception):
    """The personal-state artifact is missing, malformed, or an unsupported version."""


@dataclass(frozen=True)
class PersonalState:
    contract_version: int
    generated_at: str
    topics: list = field(default_factory=list)

    def top_terms(self, n):
        """The first `n` topic keys, in artifact order (highest weight first)."""
        return [topic["key"] for topic in self.topics[:n]]


def load(path):
    """Load and validate a personal-state artifact. Raises PersonalStateError."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as e:
        raise PersonalStateError(f"personal-state artifact not found at {path}: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PersonalStateError(f"personal-state artifact at {path} is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise PersonalStateError(
            f"personal-state artifact at {path} must be a JSON object, got {type(data).__name__}"
        )

    if "contract_version" not in data or "topics" not in data:
        raise PersonalStateError(
            f"personal-state artifact at {path} is missing required key(s) "
            f"'contract_version'/'topics'"
        )

    version = data["contract_version"]
    if version not in SUPPORTED_VERSIONS:
        raise PersonalStateError(
            f"personal-state artifact at {path} is contract_version {version!r}, "
            f"but this reader only supports {sorted(SUPPORTED_VERSIONS)}. "
            f"Read ai's PERSONAL_STATE_CONTRACT.md and extend SUPPORTED_VERSIONS "
            f"in discovery/personal_state.py to add support for it."
        )

    # Deliberately stricter than the "ignore unknown keys" forward-compat
    # posture above: top_terms() indexes topic["key"] outside load_optional's
    # fail-soft boundary (called straight from interests.py/__main__.py), so
    # every topic having a string key is an invariant this reader must
    # guarantee up front rather than let a bad entry surface as a KeyError
    # deep in a caller. One malformed topic voids the whole artifact.
    topics = data["topics"]
    if not isinstance(topics, list) or any(
        not isinstance(t, dict) or not isinstance(t.get("key"), str) for t in topics
    ):
        raise PersonalStateError(
            f"personal-state artifact at {path} has malformed 'topics': "
            f"expected a list of objects each with a string 'key'"
        )

    return PersonalState(
        contract_version=version,
        generated_at=data.get("generated_at", ""),
        topics=topics,
    )


def load_optional(path):
    """Fail-soft wrapper for the pipeline: never raises, logs and returns None.

    Upholds the repo rule that failures are isolated and skipped rather than
    killing a cycle.
    """
    try:
        return load(path)
    except PersonalStateError as e:
        print(f"personal-state: {e}", file=sys.stderr)
        return None
