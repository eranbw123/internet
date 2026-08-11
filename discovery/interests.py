"""Load interests.json into the interests table."""
import json

from .db import DERIVED_KEY_PREFIX, add_interest_event, upsert_interest
from .models import Interest


def load_file(path, state=None):
    # utf-8-sig: a Windows editor saving interests.json with a BOM must not
    # break `init`.
    data = json.loads(open(path, encoding="utf-8-sig").read())
    defaults = data.get("defaults", {})
    return [_to_interest(entry, defaults, state) for entry in data["interests"]]


def load_blocked(path):
    """Optional top-level "blocked_derived_terms" list interests.json may
    carry -- terms interest_state.py's ladder must never promote, and must
    retire if already tracked. Absent key = [], behavior unchanged. A
    separate helper (rather than widening load_file()'s return shape) so
    every existing call site stays untouched."""
    data = json.loads(open(path, encoding="utf-8-sig").read())
    return list(data.get("blocked_derived_terms", []))


def _to_interest(entry, defaults, state=None):
    key = entry["key"]
    if key.startswith(DERIVED_KEY_PREFIX):
        raise ValueError(
            f"owner interest key {key!r} must not start with the reserved "
            f"{DERIVED_KEY_PREFIX!r} prefix (reserved for derived interests)"
        )
    positive_signals = entry.get("positive_signals", [])
    top_n = entry.get("personal_state_top_terms")
    if top_n and state is not None and int(top_n) > 0:
        # De-duplicated, existing signals first, order stable -- an opt-in
        # augmentation that must stay byte-identical to today when the key
        # is absent or the artifact didn't load (see discovery/personal_state.py).
        positive_signals = list(positive_signals)
        for term in state.top_terms(top_n):
            if term not in positive_signals:
                positive_signals.append(term)
    return Interest(
        key=key,
        title=entry["title"],
        description=entry.get("description", ""),
        positive_signals=positive_signals,
        negative_signals=entry.get("negative_signals", []),
        min_score=_threshold(entry.get("min_score", defaults.get("min_score", 0.70))),
        sources=entry.get("sources", defaults.get("sources", [])),
        source_config=entry.get("source_config", {}),
    )


def _threshold(value):
    """Thresholds are 0-1, compared against `scores.final_score`.

    They used to be 0-100, and a hand-edited file is the likeliest place for
    the old scale to survive -- a stray `75` would silently mean "never notify",
    so treat anything above 1 as the old scale rather than trusting it.
    """
    value = float(value)
    return value / 100 if value > 1 else value


def sync(conn, path, state=None):
    """Write the file's interests into the DB, and append an 'owner_sync'
    provenance event per interest so owner rows are queryable the same way
    derived ones are (`python -m app interests --why <key>`). Returns how
    many were written."""
    interests = load_file(path, state)
    for interest in interests:
        upsert_interest(conn, interest)
        add_interest_event(conn, interest.key, "owner_sync", "sync", None, "owner")
    return len(interests)
