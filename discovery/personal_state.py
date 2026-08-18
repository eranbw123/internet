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

v2 adds an evidence-bearing `candidates` array beside v1's `topics` (see
`PERSONAL_STATE_CONTRACT.md`'s dated v2 section, still authoritative):
    {"contract_version": 2, ..., "topics": [...],   # unchanged, may be absent
     "candidates": [{"kind": "new|bridge|merge|split|revive",
                     "key": "<slug>", "title": "...", "description": "...",
                     "positive_signals": [...], "negative_signals": [...],
                     "suggested_min_score": <float>, "parent_key": "<key>",
                     "related_keys": [...],
                     "evidence": [{"date": "<iso>", "quote": "<=140 chars,
                                   may be Hebrew", "lang": "he|en",
                                   "depth": <float>,
                                   "conversation_id": "<id>"}],
                     "durability": {"n_convs": <int>, "active_months": <int>,
                                    "span_days": <int>, "recency_days": <int>},
                     "expected_yield": <float 0..1>,
                     "similarity_to_existing": [{"key": "...", "sim": 0..1}]}]}
Only `discovery/offers.py` consumes `candidates`; the v1 token ladder keeps
reading `topics` and never sees them.

Forward compatibility: unknown top-level keys and unknown per-topic keys are
ignored, never rejected -- `ai` may add optional fields without a version
bump. Topics are consumed in the order given; never re-sorted here.
"""
import json
import sys
from dataclasses import dataclass, field

SUPPORTED_VERSIONS = frozenset({1, 2})


class PersonalStateError(Exception):
    """The personal-state artifact is missing, malformed, or an unsupported version."""


@dataclass(frozen=True)
class PersonalState:
    contract_version: int
    generated_at: str
    topics: list = field(default_factory=list)
    # v2 only; always [] for a v1 artifact, so a v1 consumer is byte-identical.
    candidates: list = field(default_factory=list)

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

    if "contract_version" not in data:
        raise PersonalStateError(
            f"personal-state artifact at {path} is missing required key "
            f"'contract_version'"
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
    # v1 must carry `topics`. v2 may ship either half -- a weekly reduce run
    # publishes `candidates` with no fresh token pass, and the nightly map
    # publishes `topics` with no candidates -- so the missing half is [],
    # never an error.
    if version == 1 and "topics" not in data:
        raise PersonalStateError(
            f"personal-state artifact at {path} is contract_version 1 but is "
            f"missing required key 'topics'"
        )
    if version >= 2 and "topics" not in data and "candidates" not in data:
        raise PersonalStateError(
            f"personal-state artifact at {path} is contract_version {version} "
            f"but carries neither 'topics' nor 'candidates'"
        )

    topics = data.get("topics", [])
    if not isinstance(topics, list) or any(
        not isinstance(t, dict) or not isinstance(t.get("key"), str) for t in topics
    ):
        raise PersonalStateError(
            f"personal-state artifact at {path} has malformed 'topics': "
            f"expected a list of objects each with a string 'key'"
        )

    # Same posture one level down for v2's candidates: offers.py indexes
    # candidate["key"] while ranking, well outside load_optional()'s
    # fail-soft boundary, so a candidate without a string key voids the
    # artifact here rather than surfacing as a KeyError mid-import. Every
    # other candidate field is optional and defaulted by offers.py -- the
    # producer may add fields without a bump.
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list) or any(
        not isinstance(c, dict) or not isinstance(c.get("key"), str) or not c["key"].strip()
        for c in candidates
    ):
        raise PersonalStateError(
            f"personal-state artifact at {path} has malformed 'candidates': "
            f"expected a list of objects each with a non-empty string 'key'"
        )

    return PersonalState(
        contract_version=version,
        generated_at=data.get("generated_at", ""),
        topics=topics,
        candidates=candidates,
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
