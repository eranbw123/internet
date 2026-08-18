"""Sync v2 -- reconcile `interests.json` into the `interests` table.

v1 (`interests.sync`, still the public entry point and now a thin wrapper
around this module) could only ever INSERT or UPDATE. It never deactivated
anything, so an interest the owner deleted from the file kept collecting,
scoring and spending forever unless somebody hand-ran an UPDATE against the
live database. That is why 13 owner rows in the production DB are `active=0`
with no code path that put them there, and why the standing procedure for
retiring an interest was "open a JSON pull request AND run a live-DB op".

Sync v2 makes the file the single source of truth in both directions:

  * present in the file            -> upserted (created live)
  * present with `"active": false` -> retired, definition kept, so reviving it
                                      is one flag rather than re-authoring it
  * present with `"active": true`  -> forced live, whatever the sweep did
  * present, saying nothing        -> definition updated, liveness LEFT ALONE
                                      (an auto-paused interest stays paused) --
                                      except a retired one, which a re-add
                                      revives
  * absent from the file entirely  -> retired
  * retired either way             -> its PENDING search_missions are
                                      CANCELLED so the web tick stops handing
                                      out work for an interest nobody wants
  * every actual change            -> one `interest_events` row (actor
                                      `owner_sync`), nothing on a no-op run

Liveness is never written here directly. Retirement and revival go through
`offers.set_lifecycle()` -- the post-acceptance state machine
(active|decaying|paused|retired) the offers store owns -- so there is exactly
one deactivation mechanism in the engine and `lifecycle` and `active` can
never disagree. This module owns the reconciliation (what the file says);
`offers.py` owns the state machine (what happens to an interest once it
exists). The seam runs the other way too: `entry_writer()` is the callable
`offers.accept(sync=...)` expects, so accepting an offer writes the entry into
interests.json, syncs it into the DB, and activates the offer in one call.

and it is callable at runtime (`python -m app sync`, or `sync(conn, path)`
in-process) instead of only from `init`, so an edit takes effect on the next
pipeline cycle without a redeploy -- `db.active_interests()` reads the DB live.

Only `layer='owner'` rows are ever touched. Derived rows (`derived:` prefix)
belong to `interest_state.py`'s ladder and are invisible here, exactly as the
owner-immutability guards in `db.py` are invisible to that module.

Blast radius. `interests.json` is hand-edited, is rewritten atomically by the
Observatory write API, and is copied around by scheduled tasks -- a truncated
or half-written file must never be able to retire the whole engine. `plan()`
therefore flags (and `apply()` refuses, absent `force=True`) any run that
would deactivate an implausible share of the active set: see
`_guard_reason()`. Refusing is recoverable; silently retiring 33 interests is
not.

The additive migration this module needs lives here rather than in `db.py`'s
ALTER pass, so it stays out of the way of the concurrently-developed offers
store, which is adding its own columns to the same table. `migrate()` is
idempotent and is called by `sync()`/`plan()` before anything else, which also
means sync v2 works against an already-deployed database without an `init`.
"""
import dataclasses
import json
import os
import sqlite3

from . import db, offers
from .interests import load_file, load_stated_active

# Additive schema this module owns. Deliberately NOT part of db.init()'s ALTER
# pass: applied on demand by migrate() below so a running appliance picks it up
# from a plain `python -m app sync`, and so the offers store's own columns and
# this one can never end up in the same conflicting edit.
MIGRATION = (
    # When sync last reconciled this row against interests.json. Distinct from
    # `last_observed_at` (corpus evidence, derived rows) -- this is "the file
    # and the DB agreed at this instant", the timestamp the interest editor
    # shows and the one a drift check would compare against the file's mtime.
    ("interests", "synced_at", "TEXT"),
)

# The mass-deactivation guard. A run may retire up to GUARD_MIN interests
# freely (ordinary gardening); past that it may not retire more than
# GUARD_FRACTION of what is currently active without an explicit --force.
GUARD_MIN = 3
GUARD_FRACTION = 0.5

# Fields compared between the file and the stored row to decide whether an
# entry actually changed. `active` is tracked separately (it has its own
# events); layer/provenance/last_observed_at are not the file's business.
COMPARED = (
    "title", "description", "positive_signals", "negative_signals",
    "min_score", "sources", "source_config",
)

_JSON_FIELDS = ("positive_signals", "negative_signals", "sources", "source_config")

CANCEL_REASON = "cancelled: interest deactivated by interests.json sync"


class SyncRefused(Exception):
    """apply() would have deactivated an implausible share of the active
    interest set -- almost always a truncated or half-written file rather
    than an intentional purge. Re-run with force=True (`--force`) to mean it."""


@dataclasses.dataclass
class SyncPlan:
    """What a sync would do, computed without writing anything."""
    created: list = dataclasses.field(default_factory=list)        # keys
    updated: list = dataclasses.field(default_factory=list)        # (key, [changed fields])
    reactivated: list = dataclasses.field(default_factory=list)    # keys
    deactivated: list = dataclasses.field(default_factory=list)    # (key, reason)
    unchanged: list = dataclasses.field(default_factory=list)      # keys
    guard_reason: str = ""
    _writes: list = dataclasses.field(default_factory=list, repr=False)  # (Interest, active)

    @property
    def changes(self):
        return (len(self.created) + len(self.updated)
                + len(self.reactivated) + len(self.deactivated))

    def describe(self):
        lines = [
            f"created {len(self.created)}  updated {len(self.updated)}  "
            f"reactivated {len(self.reactivated)}  deactivated {len(self.deactivated)}  "
            f"unchanged {len(self.unchanged)}"
        ]
        for key in self.created:
            lines.append(f"  + {key}")
        for key, fields in self.updated:
            lines.append(f"  ~ {key}  ({', '.join(fields)})")
        for key, from_lifecycle in self.reactivated:
            lines.append(f"  ^ {key}  (was {from_lifecycle})")
        for key, reason in self.deactivated:
            lines.append(f"  - {key}  ({reason})")
        if self.guard_reason:
            lines.append(f"  ! {self.guard_reason}")
        return "\n".join(lines)


@dataclasses.dataclass
class SyncResult:
    """What a sync did. `written` is the v1 return value (entries loaded from
    the file), kept so `interests.sync()`'s int contract is unchanged."""
    written: int = 0
    created: list = dataclasses.field(default_factory=list)
    updated: list = dataclasses.field(default_factory=list)
    reactivated: list = dataclasses.field(default_factory=list)
    deactivated: list = dataclasses.field(default_factory=list)
    unchanged: list = dataclasses.field(default_factory=list)
    missions_cancelled: int = 0

    @property
    def changes(self):
        return (len(self.created) + len(self.updated)
                + len(self.reactivated) + len(self.deactivated))

    def describe(self):
        return (
            f"{self.written} interests loaded, "
            f"{len(self.created)} created, {len(self.updated)} updated, "
            f"{len(self.reactivated)} reactivated, {len(self.deactivated)} deactivated "
            f"({self.missions_cancelled} pending missions cancelled), "
            f"{len(self.unchanged)} unchanged"
        )


def migrate(conn):
    """Additive, idempotent, safe against a live database -- the repo's
    ALTER-on-demand convention (no migration framework), just owned by this
    module instead of db.init()."""
    for table, column, decl in MIGRATION:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def plan(conn, path, state=None):
    """Read the file, read the owner rows, and return what would change.
    Writes nothing. Raises the same load errors `interests.load_file` does --
    a malformed file must abort before any deactivation is considered."""
    migrate(conn)
    interests = load_file(path, state)
    stated = load_stated_active(path)
    _reject_duplicates(interests)

    stored = {
        row["key"]: row
        for row in conn.execute(
            "SELECT * FROM interests WHERE layer = 'owner'"
        ).fetchall()
    }
    result = SyncPlan()

    for interest in interests:
        row = stored.get(interest.key)
        says = stated.get(interest.key)          # True / False / None ("no opinion")
        if row is None:
            # A new row is born live unless the file says otherwise; the
            # retirement below is what makes it consistent with `lifecycle`.
            result._writes.append((interest, 1))
            result.created.append(interest.key)
            if says is False:
                result.deactivated.append((interest.key, "marked inactive in the file"))
            continue

        # Definition edits must never move liveness: carry the row's own
        # `active` through the upsert and let the lifecycle decide below.
        result._writes.append((interest, row["active"]))
        changed = _changed_fields(interest, row)
        if changed:
            result.updated.append((interest.key, changed))

        lifecycle = _lifecycle(row)
        if says is False:
            if lifecycle != offers.RETIRED:
                result.deactivated.append((interest.key, "marked inactive in the file"))
        elif says is True:
            if lifecycle != offers.ACTIVE:
                result.reactivated.append((interest.key, lifecycle))
        elif lifecycle == offers.RETIRED:
            # Silent on liveness, but back in the file after being retired --
            # re-adding an entry IS the revival. A merely paused or decaying
            # interest is left where the sweep put it: the file is authority
            # over what exists, not over what the decay machinery decided.
            result.reactivated.append((interest.key, lifecycle))
        elif not changed:
            result.unchanged.append(interest.key)

    file_keys = {i.key for i in interests}
    for key, row in stored.items():
        if key not in file_keys and _lifecycle(row) != offers.RETIRED:
            result.deactivated.append((key, "absent from the file"))

    result.guard_reason = _guard_reason(len(result.deactivated), stored)
    return result


def apply(conn, plan_, force=False):
    """Write a plan. Only owner rows are touched; nothing is ever DELETEd
    (a trigger in db.py forbids deleting an owner row anyway -- deactivation
    is the retirement mechanism, and it keeps the provenance chain intact)."""
    if plan_.guard_reason and not force:
        raise SyncRefused(plan_.guard_reason)

    result = SyncResult(
        written=len(plan_._writes),
        created=list(plan_.created),
        updated=list(plan_.updated),
        reactivated=[key for key, _ in plan_.reactivated],
        unchanged=list(plan_.unchanged),
        deactivated=[key for key, _ in plan_.deactivated],
    )
    created = set(plan_.created)
    updated = dict(plan_.updated)

    for interest, want_active in plan_._writes:
        db.upsert_interest(conn, interest, active=want_active)
        if interest.key in created:
            _event(conn, interest.key, "create", {})
        elif interest.key in updated:
            _event(conn, interest.key, "update", {"changed": updated[interest.key]})

    for key, from_lifecycle in plan_.reactivated:
        offers.set_lifecycle(
            conn, key, offers.ACTIVE, actor="owner_sync", action="reactivate",
            detail={"reason": "back in interests.json" if from_lifecycle == offers.RETIRED
                    else 'interests.json says "active": true'},
        )

    for key, reason in plan_.deactivated:
        cancelled = _cancel_pending_missions(conn, key)
        result.missions_cancelled += cancelled
        # offers.set_lifecycle owns the `active` column too, so lifecycle and
        # the pipeline's switch can never drift apart -- and it writes its own
        # interest_events row, which is why there is no _event() call here.
        offers.set_lifecycle(
            conn, key, offers.RETIRED, actor="owner_sync", action="deactivate",
            detail={"reason": reason, "missions_cancelled": cancelled},
        )

    _stamp_synced_at(conn, [i.key for i, _ in plan_._writes])
    conn.commit()
    return result


def sync(conn, path, state=None, force=False):
    """plan() + apply(). The one call the CLI, `init` and (next) the
    Observatory write API all go through."""
    return apply(conn, plan(conn, path, state), force=force)


# --- writing the file (the other half of "one save, no PR, no DB op") --------

def entry_writer(conn, path, state=None, force=False):
    """The callable `offers.accept(conn, key, sync=...)` expects.

    Accepting an offer hands us an interests.json-shaped entry and deliberately
    writes neither the file nor the interests table (see offers.accept's
    docstring). This closes that loop: the entry lands in interests.json, sync
    v2 reconciles the file into the DB, and offers.activate() -- which accept()
    calls straight after -- finds the interest row it requires. The whole file
    is reconciled, not just the accepted entry, because a partial write is
    exactly the drift this PR exists to remove."""
    def write(entry):
        write_entry(path, entry)
        return sync(conn, path, state, force=force)
    return write


def write_entry(path, entry):
    """Insert or replace one entry in interests.json, atomically, preserving
    the rest of the file (same shape as offers.append_blocked_terms: read
    utf-8-sig, re-dump indent=2 in the file's own style, os.replace).
    Returns "created" or "updated"."""
    data = _read(path)
    entries = data.setdefault("interests", [])
    for index, existing in enumerate(entries):
        if existing.get("key") == entry["key"]:
            entries[index] = entry
            _write(path, data)
            return "updated"
    entries.append(entry)
    _write(path, data)
    return "created"


def set_entry_active(path, key, active):
    """Flip one entry's `active` flag in interests.json (dropping the key
    entirely when re-activating, so the common file stays free of noise).

    Retirement belongs in the file, not only in the database: an interest
    retired through offers.retire_interest() alone, while its entry still sits
    in interests.json saying nothing, is revived by the next sync. This is the
    one call that keeps a retirement durable, and the write API (PR J) is
    expected to make it alongside every retire/undo."""
    data = _read(path)
    for existing in data.get("interests", []):
        if existing.get("key") == key:
            if active:
                existing.pop("active", None)
            else:
                existing["active"] = False
            _write(path, data)
            return True
    return False


def _read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _write(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# --- internals ---------------------------------------------------------------

def _lifecycle(row):
    """`interests.lifecycle` belongs to the offers store's migration (db.init()'s
    ALTER pass, which every redeploy runs via ops/self_update.py). Defaulting
    when the column is absent keeps a read-only plan working on a database that
    predates it, rather than restating that migration here."""
    try:
        return row["lifecycle"] or offers.ACTIVE
    except IndexError:
        return offers.ACTIVE


def _reject_duplicates(interests):
    seen = set()
    for interest in interests:
        if interest.key in seen:
            # Two entries for one key means the file disagrees with itself
            # about what that interest is; picking the last one silently is
            # how a bad merge becomes a wrong bar nobody notices.
            raise ValueError(f"duplicate interest key in the file: {interest.key!r}")
        seen.add(interest.key)


def _changed_fields(interest, row):
    changed = []
    for field in COMPARED:
        want = getattr(interest, field)
        have = row[field]
        if field in _JSON_FIELDS:
            # Stored as JSON text with the default ensure_ascii=True, so a
            # Hebrew signal round-trips as \uXXXX escapes -- compare decoded
            # values, never the raw column text.
            have = json.loads(have)
        elif field == "min_score":
            # Both sides are already on the 0-1 scale (interests._threshold
            # rescales a legacy 0-100 value on load); compare as floats so
            # 0.7 and 0.70 are not a "change" that logs an event every run.
            want, have = float(want), float(have)
        if want != have:
            changed.append(field)
    return changed


def _guard_reason(deactivating, stored):
    if deactivating <= GUARD_MIN:
        return ""
    # Measured against everything not already retired, not against `active`:
    # an auto-paused interest is still one the owner is tending, and counting
    # it would make the guard fire earlier the more the sweep had paused.
    active = sum(1 for row in stored.values()
                 if _lifecycle(row) != offers.RETIRED) or 1
    if deactivating <= active * GUARD_FRACTION:
        return ""
    return (
        f"refusing to deactivate {deactivating} of {active} active interests in one "
        f"sync -- that is more than {int(GUARD_FRACTION * 100)}% of the active set and "
        f"looks like a truncated interests.json; re-run with --force if it is intended"
    )


def _cancel_pending_missions(conn, key):
    """PENDING only. A RUNNING mission is leased and mid-execution; its own
    finish/fail path owns that row, and stealing it out from under the tick
    would be a lost result, not a saved call. Its interest is inactive by the
    time it lands, so nothing further is scheduled for it."""
    cur = conn.execute(
        """
        UPDATE search_missions
        SET status = 'CANCELLED', finished_at = ?, last_error = ?
        WHERE interest_key = ? AND status = 'PENDING'
        """,
        (db.now(), CANCEL_REASON, key),
    )
    return cur.rowcount


def _stamp_synced_at(conn, keys):
    if not keys:
        return
    placeholders = ",".join("?" for _ in keys)
    conn.execute(
        f"UPDATE interests SET synced_at = ? WHERE layer = 'owner' AND key IN ({placeholders})",
        (db.now(), *keys),
    )


def _event(conn, key, action, evidence):
    # actor stays 'owner_sync' (the value `python -m app interests --why` and
    # the Observatory already render); only `action` is new and specific --
    # v1 wrote 'sync' for every interest on every run, 155 rows saying nothing.
    db.add_interest_event(conn, key, "owner_sync", action, None, "owner", evidence)
