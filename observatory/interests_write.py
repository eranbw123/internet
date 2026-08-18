"""Validating and staging the Observatory's interest writes.

This module owns exactly two things the rest of the stack does not:

  1. VALIDATION of an editor payload -- the rules mirror
     discovery/interests.py's loader, so nothing the API accepts can fail the
     next `sync` or `init`.
  2. The STALE-EDITOR PRECONDITION -- an mtime token handed to the editor and
     checked on the way back, so a form opened before someone hand-edited
     interests.json is refused rather than silently clobbering that edit (the
     design's own risk register calls this out).

Everything else is delegated, deliberately:

  * discovery/interest_sync.py (PR I) does every write. `write_entry()` puts
    one entry in the file, `set_entry_active()` flips one entry's liveness,
    and `sync()` reconciles the whole file into the DB -- creating, updating,
    reactivating, deactivating, cancelling PENDING missions and logging an
    interest_events row per change.
  * discovery/offers.py (PR H) owns the interest lifecycle:
    `set_lifecycle()` is the ONE deactivation mechanism, and interest_sync
    calls it. Nothing here writes `interests.active` directly.

The rule that makes this arrangement mandatory rather than tidy: sync v2
treats interests.json as the source of truth in BOTH directions, so a
database-only retirement is REVERTED by the next sync. Every retire, pause or
revive this API performs therefore writes the file too --
`set_entry_active()` alongside the lifecycle move -- or the owner's click
silently evaporates on the next cycle.

Encoding: the file is read utf-8-sig (a Windows editor may leave a BOM) and
written utf-8 with ensure_ascii=False by interest_sync, so Hebrew titles and
signals stay readable in the file the owner edits by hand.
"""
import json
import os
import re

from discovery import db as ddb
from discovery import interest_sync
from discovery import offers
from discovery.collectors import COLLECTORS

# Slug rule for an interest key. ASCII-only even though titles, descriptions
# and signals are routinely Hebrew: keys are identifiers -- they end up in
# URLs, mission labels, metric names and Telegram messages -- and house style
# keeps them English ( 7.4). A Hebrew title with an English key is the normal
# case, not an error.
KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

MAX_KEY_LEN = 64
MAX_TITLE_LEN = 200
MAX_SIGNALS = 60

# Interest fields the editor may set. Anything else in a payload is reported
# rather than ignored -- a typo'd "min_scores" that silently did nothing would
# be indistinguishable from a saved bar.
EDITABLE_FIELDS = frozenset({
    "key", "title", "description", "positive_signals", "negative_signals",
    "min_score", "sources", "source_config", "parent_key", "active",
})


class ValidationError(Exception):
    """A payload the editor must fix -- rendered as 400."""


class ConflictError(Exception):
    """interests.json changed under us (the mtime precondition failed), the
    key already exists on create, or sync's mass-deactivation guard refused --
    rendered as 409, because retrying the same request unchanged will not
    help."""


class NotFound(Exception):
    """No such interest -- rendered as 404."""


# --- validation ---------------------------------------------------------------

def _as_text(value, field, max_len=None, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"{field} is required")
    if max_len and len(value) > max_len:
        raise ValidationError(f"{field} is longer than {max_len} characters")
    return value


def _as_terms(value, field):
    """A signal list: strings, stripped, blanks dropped, duplicates removed,
    order preserved. Hebrew passes through untouched -- no casefolding, no
    normalization, because the matcher compares against raw item text."""
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field} must be a list of strings")
    out, seen = [], set()
    for raw in value:
        if not isinstance(raw, str):
            raise ValidationError(f"{field} must contain only strings")
        term = raw.strip()
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    if len(out) > MAX_SIGNALS:
        raise ValidationError(f"{field} has more than {MAX_SIGNALS} entries")
    return out


def _as_bar(value):
    """The 0-100 legacy-scale guard, the same rule as
    discovery/interests.py::_threshold: thresholds are compared against
    scores.final_score (0-1), and a hand-typed `75` used to mean "never
    notify". Anything above 1 is read as the old scale."""
    if value is None:
        return None
    try:
        bar = float(value)
    except (TypeError, ValueError):
        raise ValidationError("min_score must be a number")
    if bar != bar or bar in (float("inf"), float("-inf")):
        raise ValidationError("min_score must be a finite number")
    if bar > 1:
        bar = bar / 100
    if not 0 <= bar <= 1:
        raise ValidationError("min_score must be between 0 and 1")
    return bar


def _as_key(value, field="key"):
    key = _as_text(value, field, MAX_KEY_LEN, required=True)
    if key.startswith(ddb.DERIVED_KEY_PREFIX):
        raise ValidationError(
            f"{field} must not start with the reserved {ddb.DERIVED_KEY_PREFIX!r} "
            f"prefix (reserved for derived interests)"
        )
    if key.startswith(offers.RETIRE_PREFIX):
        raise ValidationError(
            f"{field} must not start with the reserved {offers.RETIRE_PREFIX!r} prefix"
        )
    if not KEY_RE.match(key):
        raise ValidationError(
            f"{field} must be a lowercase slug (a-z, 0-9, single hyphens), got {key!r}"
        )
    return key


def validate(payload, existing_key=None):
    """Normalize + check one editor payload; returns the interests.json entry
    it becomes. `existing_key` is the key being updated -- its `key` field is
    then optional and may not be changed, because renaming an interest would
    orphan every score, mission and event already attributed to the old key.

    Rules, mirroring discovery/interests.py: slug key with no reserved prefix,
    bar coerced to 0-1, at least one KNOWN source and at least one positive
    signal for an ACTIVE interest (an inactive one may legitimately be an
    empty shell kept for its history).
    """
    if not isinstance(payload, dict):
        raise ValidationError("payload must be a JSON object")
    unknown = set(payload) - EDITABLE_FIELDS - {"offered_by", "expected_mtime"}
    if unknown:
        raise ValidationError(f"unsupported field(s): {sorted(unknown)}")

    if existing_key is not None:
        if "key" in payload and payload["key"] != existing_key:
            raise ValidationError(
                f"key is immutable: cannot rename {existing_key!r} to {payload['key']!r}"
            )
        key = existing_key
    else:
        key = _as_key(payload.get("key"))

    active = payload.get("active", True)
    if not isinstance(active, bool):
        raise ValidationError("active must be true or false")

    entry = {
        "key": key,
        "title": _as_text(payload.get("title"), "title", MAX_TITLE_LEN, required=True),
        "description": _as_text(payload.get("description"), "description"),
        "positive_signals": _as_terms(payload.get("positive_signals"), "positive_signals"),
        "negative_signals": _as_terms(payload.get("negative_signals"), "negative_signals"),
        "sources": _as_terms(payload.get("sources"), "sources"),
    }

    unknown_sources = [s for s in entry["sources"] if s not in COLLECTORS]
    if unknown_sources:
        raise ValidationError(
            f"unknown source(s) {unknown_sources}: valid sources are {sorted(COLLECTORS)}"
        )

    bar = _as_bar(payload.get("min_score"))
    if bar is not None:
        entry["min_score"] = bar

    source_config = payload.get("source_config")
    if source_config is not None:
        if not isinstance(source_config, dict):
            raise ValidationError("source_config must be an object")
        entry["source_config"] = source_config

    parent_key = payload.get("parent_key")
    if parent_key:
        parent = _as_key(parent_key, "parent_key")
        if parent == key:
            raise ValidationError("parent_key must not be the interest's own key")
        entry["parent_key"] = parent

    if active:
        if not entry["positive_signals"]:
            raise ValidationError("an active interest needs at least one positive signal")
        if not entry["sources"]:
            raise ValidationError("an active interest needs at least one source")
    else:
        # interest_sync reads liveness from this flag; a silent entry means
        # "no opinion", which is NOT the same as active (see
        # interests.load_stated_active).
        entry["active"] = False
    return entry


# --- the stale-editor precondition ---------------------------------------------

def read_file(path):
    """Returns (data, mtime). utf-8-sig, exactly as the loader reads it."""
    with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    data.setdefault("interests", [])
    return data, file_mtime(path)


def file_mtime(path):
    """The precondition token handed to the editor and back. A string, not a
    float: it round-trips through JSON and a UI without picking up float
    formatting differences."""
    return f"{os.path.getmtime(path):.6f}"


def check_mtime(path, expected_mtime):
    """Refuse a save from an editor that loaded the file before someone else
    changed it. Last-writer-wins is acceptable at n=1 users; silently losing a
    hand edit is not."""
    if expected_mtime is None:
        return
    if file_mtime(path) != str(expected_mtime):
        raise ConflictError(
            "interests.json changed since this editor loaded it -- reload and re-apply"
        )


def find_entry(data, key):
    for i, entry in enumerate(data.get("interests", [])):
        if entry.get("key") == key:
            return i, entry
    return -1, None


# --- the write paths the endpoints call ----------------------------------------

def _sync(conn, interests_path):
    """interest_sync.sync(), with its mass-deactivation guard surfaced as a
    ConflictError rather than a 500. The guard fires when a single sync would
    retire more than half the active set -- almost always a truncated file,
    and never something an editor save should be able to do silently."""
    try:
        return interest_sync.sync(conn, interests_path)
    except interest_sync.SyncRefused as e:
        raise ConflictError(str(e))


def _result(key, mtime, sync_result):
    return {
        "key": key,
        "mtime": mtime,
        "synced_at": ddb.now(),
        "created": sync_result.created,
        "updated": [k for k, _ in sync_result.updated],
        "reactivated": sync_result.reactivated,
        "deactivated": sync_result.deactivated,
        "missions_cancelled": sync_result.missions_cancelled,
    }


def save(conn, interests_path, payload, existing_key=None, expected_mtime=None,
         extra=None):
    """Validate -> write the entry -> sync. The single path behind POST
    /api/interests and POST /api/interests/<key>.

    `extra` is merged into the entry without being validated -- it exists for
    offers.py's `offered_by` provenance block, which the editor never sends
    and must never strip.
    """
    entry = validate(payload, existing_key=existing_key)
    if extra:
        entry = {**entry, **extra}
    data, _ = read_file(interests_path)
    index, _existing = find_entry(data, entry["key"])
    if existing_key is None and index >= 0:
        raise ConflictError(f"interest {entry['key']!r} already exists")
    if existing_key is not None and index < 0:
        raise NotFound(existing_key)
    check_mtime(interests_path, expected_mtime)

    interest_sync.write_entry(interests_path, entry)
    result = _sync(conn, interests_path)
    if entry.get("parent_key") is not None:
        set_parent_key(conn, entry["key"], entry["parent_key"])
    return {"entry": entry, **_result(entry["key"], file_mtime(interests_path), result)}


def set_active(conn, interests_path, key, active, expected_mtime=None):
    """Retire ({"active": false}) or un-retire one interest from the editor.

    The file is what is flipped -- not the column. interest_sync.sync() then
    performs the actual state change through offers.set_lifecycle(), which
    owns `active` and `lifecycle` together and cancels the interest's PENDING
    missions on the way down. Writing the column here instead would be undone
    by the next sync, because the file is the source of truth.
    """
    data, _ = read_file(interests_path)
    if find_entry(data, key)[0] < 0:
        raise NotFound(key)
    check_mtime(interests_path, expected_mtime)
    interest_sync.set_entry_active(interests_path, key, bool(active))
    result = _sync(conn, interests_path)
    return {"active": bool(active),
            **_result(key, file_mtime(interests_path), result)}


def revive(conn, interests_path, key, note=""):
    """The one-click undo of an auto-pause.

    Two writes, in this order, and the order is the whole point:

      1. offers.undo_auto_pause() -- the real reversal. Lifecycle back to
         active, the silence clock reset from now (so the sweep does not
         immediately re-pause on the same 45 days it already judged), and any
         open retirement offer closed, so the inbox does not propose retiring
         what the owner just brought back.
      2. interest_sync.set_entry_active(..., True) + sync -- the file agrees.
         Without this the next sync reads a file still saying "active": false
         (or nothing at all, after an owner retire) and reverts step 1. A
         database-only revival does not survive.
    """
    result = offers.undo_auto_pause(conn, key, note=note)
    data, _ = read_file(interests_path)
    if find_entry(data, key)[0] >= 0:
        interest_sync.set_entry_active(interests_path, key, True)
    synced = _sync(conn, interests_path)
    return {
        "lifecycle": result["lifecycle"],
        "active": result["active"],
        "retire_offer_closed": result.get("retire_offer_closed"),
        **_result(key, file_mtime(interests_path), synced),
    }


def set_parent_key(conn, key, parent_key):
    """The one column the sync's upsert does not carry (interest_sync.COMPARED
    is the file's fields; hierarchy is set by the editor and by
    offers.activate()). Guarded to owner rows: a derived interest's hierarchy
    is automation's business."""
    conn.execute(
        "UPDATE interests SET parent_key = ? WHERE key = ? AND layer = 'owner'",
        (parent_key or None, key),
    )
    conn.commit()


def entry_writer(conn, interests_path):
    """The callable offers.accept(conn, key, sync=...) expects.

    PR I implemented exactly this seam, so this is a one-line delegation
    rather than a second implementation -- there must be only one path from
    "an accepted offer became an entry" to "the file and the DB agree".
    """
    return interest_sync.entry_writer(conn, interests_path)
