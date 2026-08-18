"""The Observatory write API ( 7.3): the endpoints the interests workspace
calls to read the funnel, edit interests, decide offers, and undo an
auto-pause.

Nine routes, all under /observatory/api/, registered from plugin.py:

    GET  /observatory/api/offers                        the inbox
    GET  /observatory/api/offers/<key>/provenance       why this is offered
    POST /observatory/api/offers/<key>/decide           accept|reject|snooze
    POST /observatory/api/offers/generate               import/re-rank the artifact
    GET  /observatory/api/interests/stats               the funnel, per interest
    POST /observatory/api/interests                     create
    POST /observatory/api/interests/<key>               update / retire
    POST /observatory/api/interests/<key>/revive        the one-click undo
    GET  /observatory/api/edges                         connections (PR M fills it)

What this module owns, and what it does not
-------------------------------------------
It owns HTTP: routing, the auth posture, request parsing, error mapping, and
JSON serialization. Every decision about offers belongs to
`discovery/offers.py` (PR H) and every interests write belongs to
`observatory/interests_write.py`; this module calls them and translates their
exceptions into status codes. Accepting an offer is ONE call --
`offers.accept(..., sync=interests_write.sync_callable(...))` -- because
offers.py was built with that insertion point rather than writing interests
itself.

Auth posture, unchanged from the read plugin plus one rule
----------------------------------------------------------
Private (default, localhost-bound) mode is open; `--public` requires the
bearer token. On top of that, WRITES refuse outright in public mode unless
`DISCOVERY_UI_ALLOW_PUBLIC_WRITES=1` -- a tunnel that exists so the owner can
read the Observatory from a phone should not be able to rewrite interests.json
just because the token leaked into a URL.

CSRF: writes must be `Content-Type: application/json`. A cross-site form POST
cannot set that header without a preflight the Observatory never answers, so
the content-type check IS the CSRF boundary for a localhost-bound API; a
form-encoded write is refused with 415 rather than being parsed. plugin.py's
`skip_csrf` hook then keeps Datasette's own token machinery (built for its
HTML forms) out of the way of a JSON API.
"""
import json
import os
import sqlite3

from datasette import Response

from discovery import db as ddb
from discovery import offers

from . import db as odb
from . import funnel, interests_write

# Writes are refused in public mode unless this is explicitly set to 1.
ALLOW_PUBLIC_WRITES_ENV = "DISCOVERY_UI_ALLOW_PUBLIC_WRITES"

DECISIONS = ("accept", "reject", "snooze")

# Offer statuses a caller may ask for by name, plus 'all' and 'inbox'.
INBOX_STATUSES = (offers.OFFERED,)
MAX_OFFERS = 200


# --- responses -----------------------------------------------------------------

def _json(body, status=200):
    """json.dumps with ensure_ascii=False, unlike Response.json.

    Interest titles, descriptions, signals and -- above all -- evidence quotes
    are routinely Hebrew (28% of the corpus). Escaped \\uXXXX is still correct
    JSON, but the response body is what a developer reads in a network tab and
    what `python -m app` prints, so emit real UTF-8.
    """
    return Response(
        json.dumps(body, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json; charset=utf-8",
    )


def _error(message, status=400, **extra):
    return _json({"ok": False, "error": message, **extra}, status=status)


# --- guards --------------------------------------------------------------------

def _public(datasette):
    return bool(getattr(datasette, "_observatory_public", False))


def _guard(datasette, request):
    """Read guard -- the same boundary plugin.py's own views apply. Duplicated
    here (four lines) rather than imported, because plugin.py imports this
    module to register its routes and a back-import would be a cycle."""
    if _public(datasette) and request.actor is None:
        return _error("forbidden -- missing or invalid token", status=403)
    return None


def _write_guard(datasette, request):
    """Read guard, plus: no writes over a public tunnel, and JSON only."""
    denied = _guard(datasette, request)
    if denied:
        return denied
    if request.method != "POST":
        return _error("method not allowed -- this endpoint is POST", status=405)
    if _public(datasette) and not _writes_allowed_in_public():
        return _error(
            "forbidden -- the write API is disabled in --public mode "
            f"(set {ALLOW_PUBLIC_WRITES_ENV}=1 to override)",
            status=403,
        )
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    if content_type != "application/json":
        return _error(
            "unsupported media type -- writes must be application/json",
            status=415,
        )
    return None


def _writes_allowed_in_public():
    return os.environ.get(ALLOW_PUBLIC_WRITES_ENV, "").strip() == "1"


async def _body(request):
    """Parsed JSON body, or ({}, error-response). An empty body is {} -- some
    writes (revive) legitimately carry nothing."""
    raw = await request.post_body()
    if not raw:
        return {}, None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None, _error("request body must be valid UTF-8")
    except ValueError as e:
        return None, _error(f"request body is not valid JSON: {e}")
    if not isinstance(payload, dict):
        return None, _error("request body must be a JSON object")
    return payload, None


# --- connections ---------------------------------------------------------------

def _cfg(datasette):
    return getattr(datasette, "_observatory_cfg", None)


def _db_path(datasette):
    return getattr(datasette, "_observatory_db_path")


def _open_ro(datasette):
    return odb.open_ro(_db_path(datasette))


def _open_rw(datasette):
    """The one dedicated read-write connection a write request gets, opened
    per request and closed in a finally.

    WAL + busy_timeout=5000 (the latter from db.connect()) so a write landing
    while a scheduled collect/digest tick holds the file waits it out instead
    of raising 'database is locked' -- the Observatory is not the only writer
    of discovery.db, and it is the one that must never fail noisily in front
    of a person.
    """
    conn = ddb.connect(_db_path(datasette))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass  # a read-only filesystem or an already-set mode: not fatal
    return conn


# --- error mapping -------------------------------------------------------------

def _store_error(exc):
    """offers.py / interests_write.py exceptions -> status codes.

    409 for InvalidTransition is deliberate and load-bearing: offers.py
    enforces that a decided offer can NEVER be re-decided, so a second
    accept -- a double-clicked button, a stale tab, a replayed request -- is a
    conflict the caller must see and stop retrying, not a 400 that reads like
    a malformed payload and not a 500.
    """
    if isinstance(exc, offers.UnknownOffer):
        return _error(str(exc) or "no such offer", status=404)
    if isinstance(exc, interests_write.NotFound):
        return _error(f"no such interest: {exc}", status=404)
    if isinstance(exc, (offers.InvalidTransition, interests_write.ConflictError)):
        return _error(str(exc), status=409)
    if isinstance(exc, (offers.OfferError, interests_write.ValidationError)):
        return _error(str(exc), status=400)
    raise exc


# --- GET /observatory/api/offers -----------------------------------------------

def _parse_status(raw):
    """?status= accepts 'all', 'inbox' (the default), or a comma-separated
    list of real statuses. An unknown status is a 400 rather than an empty
    list, so a typo'd filter cannot look like an empty inbox."""
    value = (raw or "inbox").strip().lower()
    if value == "all":
        return None
    if value == "inbox":
        return list(INBOX_STATUSES)
    wanted = [s.strip() for s in value.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in offers.STATUSES]
    if unknown:
        raise ValueError(
            f"unknown status {unknown}: valid statuses are {list(offers.STATUSES)}"
        )
    return wanted


async def offers_view(request, datasette):
    denied = _guard(datasette, request)
    if denied:
        return denied
    if request.method != "GET":
        return _error("method not allowed", status=405)
    try:
        status = _parse_status(request.args.get("status"))
    except ValueError as e:
        return _error(str(e))
    kind = request.args.get("kind")
    if kind and kind not in offers.KINDS:
        return _error(f"unknown kind {kind!r}: valid kinds are {list(offers.KINDS)}")
    try:
        limit = min(int(request.args.get("limit") or MAX_OFFERS), MAX_OFFERS)
    except (TypeError, ValueError):
        return _error("limit must be an integer")

    conn = _open_ro(datasette)
    try:
        rows = offers.list_offers(conn, status=status, kind=kind, limit=limit)
        counts = {
            r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) n FROM interest_offers GROUP BY status"
            ).fetchall()
        }
    finally:
        conn.close()
    return _json({
        "offers": rows,
        "total": len(rows),
        "counts": counts,
        "status": status or "all",
    })


# --- GET /observatory/api/offers/<key>/provenance ------------------------------

def _provenance(offer_row):
    """ 7.5's checklist, assembled from the offer row: verbatim quotes with
    their date and language, regrouped by the conversation they came from,
    every score term, durability, similarity, the parent/related keys, and the
    artifact identity -- everything the inbox needs to answer "why?" without a
    second request and without joining back to the `ai` repo."""
    detail = {
        "key": offer_row["key"],
        "kind": offer_row["kind"],
        "status": offer_row["status"],
        "title": offer_row["title"],
        "description": offer_row["description"],
        "score": offer_row["score"],
        "score_terms": offer_row["score_terms"],
        "evidence": offer_row["evidence"],
        "conversations": group_evidence(offer_row["evidence"],
                                        offer_row["source_conversations"]),
        "source_conversations": offer_row["source_conversations"],
        "durability": offer_row["durability"],
        "similarity": offer_row["similarity"],
        "parent_key": offer_row["parent_key"],
        "related_keys": offer_row["related_keys"],
        "exploratory": offer_row["exploratory"],
        "artifact": {
            "sha256": offer_row["artifact_sha256"],
            "generated_at": offer_row["generated_at"],
        },
        "events": offer_row.get("events", []),
        "funnel": None,
    }
    if offer_row["kind"] == "retire":
        # The sweep stores the numbers that triggered it in score_terms
        # (see offers._raise_retire_offer); surface them under their own
        # name so the UI does not have to know that.
        terms = offer_row["score_terms"] or {}
        detail["funnel"] = {
            k: terms[k] for k in
            ("interest_key", "silent_days", "collected", "scored", "above_bar")
            if k in terms
        }
    return detail


def group_evidence(evidence, source_conversations=()):
    """[{date, quote, lang, depth, conversation_id}] -> one group per
    conversation, so the UI can say "5 quotes from 3 conversations" without a
    second round trip.

    A quote with no conversation_id becomes its own group (a bare quote is
    still provenance) -- never an error, because an older artifact may not
    carry ids at all.
    """
    groups, index = [], {}
    for i, quote in enumerate(evidence or []):
        conv_id = quote.get("conversation_id")
        bucket = conv_id or f"__quote__{i}"
        if bucket not in index:
            index[bucket] = {
                "conversation_id": conv_id,
                "date": quote.get("date"),
                "depth": quote.get("depth"),
                "quotes": [],
            }
            groups.append(index[bucket])
        index[bucket]["quotes"].append({
            "quote": quote.get("quote", ""),
            "lang": quote.get("lang", ""),
            "date": quote.get("date"),
        })
    # Conversations the extractor credited but quoted nothing from still count
    # as provenance -- list them, empty, rather than losing them.
    for conv_id in source_conversations or []:
        if conv_id not in index:
            index[conv_id] = {"conversation_id": conv_id, "date": None,
                              "depth": None, "quotes": []}
            groups.append(index[conv_id])
    return groups


async def offer_provenance_view(request, datasette):
    denied = _guard(datasette, request)
    if denied:
        return denied
    if request.method != "GET":
        return _error("method not allowed", status=405)
    key = request.url_vars["key"]
    conn = _open_ro(datasette)
    try:
        offer_row = offers.offer_detail(conn, key)
    finally:
        conn.close()
    if offer_row is None:
        return _error(f"no such offer: {key}", status=404)
    return _json(_provenance(offer_row))


# --- POST /observatory/api/offers/<key>/decide ---------------------------------

# Edit fields offers.accept() understands, mapped onto the interests.json
# entry they end up in. Used ONLY by the pre-flight below -- offers.py remains
# the single implementation of what an edit does.
_EDIT_TO_ENTRY = {
    "title": "title",
    "description": "description",
    "positive_signals": "positive_signals",
    "negative_signals": "negative_signals",
    "sources": "sources",
    "suggested_sources": "sources",
    "min_score": "min_score",
    "suggested_min_score": "min_score",
    "parent_key": "parent_key",
}


def _preflight_accept(conn, key, edits):
    """Validate what accepting WOULD write, before anything is written.

    This matters because of offers.py's own rule: accept is a one-way
    transition and a decided offer can never be re-decided. If the interests
    write failed after the transition, the offer would be stuck 'accepted'
    with no interest behind it and no way to retry. So the entry is built and
    validated first; only a payload that will survive interests_write.save()
    is allowed to reach offers.accept().

    Returns None when the accept may proceed, or an error Response.
    """
    offer_row = offers.get_offer(conn, key)
    if offer_row is None:
        return _error(f"no such offer: {key}", status=404)
    if offers.ACCEPTED not in offers.TRANSITIONS.get(offer_row["status"], frozenset()):
        return _error(
            f"offer {key!r} is {offer_row['status']} -- it has already been decided "
            f"and cannot be decided again",
            status=409,
        )
    if offer_row["kind"] == "retire":
        return _error(
            f"offer {key!r} is a retirement proposal -- accept it with "
            f"POST /observatory/api/interests/<key> {{\"active\": false}}",
            status=400,
        )
    entry = offers.interest_entry(offer_row)
    for field, value in (edits or {}).items():
        target = _EDIT_TO_ENTRY.get(field)
        if target is not None and value is not None:
            entry[target] = value
    payload = {k: v for k, v in entry.items() if k in interests_write.EDITABLE_FIELDS}
    payload["active"] = True
    try:
        interests_write.validate(payload, existing_key=None)
    except interests_write.ValidationError as e:
        return _error(f"accepting this offer would write an invalid interest: {e}")
    return None


def _resolve_offer_key(conn, ident):
    """The path segment -> an offer key.

    The route is documented and tested as /offers/<key>/decide, and a key is
    what the CLI and the provenance endpoint use. The workspace, though, holds
    offers as rows with an `id` and reaches for that -- so an all-digit segment
    is looked up as an id and translated. Offer keys are slugs
    ('retire:<interest>', 'sleep-architecture-wearables'), never bare integers,
    so the two namespaces cannot collide and the key lookup still wins.
    """
    if offers.get_offer(conn, ident) is not None:
        return ident
    if str(ident).isdigit():
        row = conn.execute(
            "SELECT key FROM interest_offers WHERE id = ?", (int(ident),)
        ).fetchone()
        if row is not None:
            return row["key"]
    return ident


async def offer_decide_view(request, datasette):
    denied = _write_guard(datasette, request)
    if denied:
        return denied
    payload, bad = await _body(request)
    if bad:
        return bad
    key = request.url_vars["key"]
    action = (payload.get("action") or "").strip().lower()
    if action not in DECISIONS:
        return _error(f"action must be one of {list(DECISIONS)}")
    note = payload.get("note") or ""
    if not isinstance(note, str):
        return _error("note must be a string")
    edits = payload.get("edits")
    if edits is not None and not isinstance(edits, dict):
        return _error("edits must be an object")

    cfg = _cfg(datasette)
    conn = _open_rw(datasette)
    try:
        key = _resolve_offer_key(conn, key)
        if action == "snooze":
            days = payload.get("days")
            if days is not None:
                try:
                    days = int(days)
                except (TypeError, ValueError):
                    return _error("days must be an integer")
                if not 1 <= days <= 365:
                    return _error("days must be between 1 and 365")
            result = offers.snooze(conn, key, days=days, note=note)
            return _json({"ok": True, "action": action, "key": key,
                          "status": result["status"],
                          "snoozed_until": result["snoozed_until"]})

        if action == "reject":
            result = offers.reject(conn, key, note=note,
                                   interests_path=cfg.interests_path)
            return _json({"ok": True, "action": action, "key": key,
                          "status": result["status"],
                          "blocked_terms": result["blocked_terms"],
                          "blocked_terms_written": result["blocked_terms_written"]})

        # accept, with or without edits -- "edit, then accept" is one request.
        failed = _preflight_accept(conn, key, edits)
        if failed:
            return failed
        synced = {}

        def _sync(entry):
            # PR I already implemented the callable offers.accept() expects;
            # calling anything else here would be a second write path into
            # interests.json, which is exactly the drift sync v2 removes.
            result = interests_write.entry_writer(conn, cfg.interests_path)(entry)
            synced["result"] = result

        result = offers.accept(conn, key, edits=edits, note=note, sync=_sync)
        sync_result = synced.get("result")
        return _json({
            "ok": True, "action": action, "key": key,
            "interest_key": result["interest_key"],
            "entry": result["entry"],
            "status": result["offer"]["status"],
            "lifecycle": result.get("activated", {}).get("lifecycle"),
            "created": getattr(sync_result, "created", []),
            "deactivated": getattr(sync_result, "deactivated", []),
            "missions_cancelled": getattr(sync_result, "missions_cancelled", 0),
            "mtime": interests_write.file_mtime(cfg.interests_path),
        })
    except (offers.OfferError, interests_write.ValidationError,
            interests_write.ConflictError, interests_write.NotFound) as e:
        return _store_error(e)
    finally:
        conn.close()


# --- POST /observatory/api/offers/generate -------------------------------------

async def offers_generate_view(request, datasette):
    """Re-run the local selector over the latest candidates artifact.

    No LLM and no network: the artifact was produced by the `ai` repo on its
    own cadence, and importing it is ranking + dedup + floors, all local
    (design  5.6 -- learning stays consumer-side). Idempotent on the
    artifact's sha256, so a double click imports once.

    Always 200: offers.import_artifact is fail-soft by contract (a missing or
    malformed artifact is a summary carrying `error`, never an exception),
    and this endpoint keeps that posture rather than inventing a status code
    for "the producer has not run yet".
    """
    denied = _write_guard(datasette, request)
    if denied:
        return denied
    payload, bad = await _body(request)
    if bad:
        return bad
    cfg = _cfg(datasette)
    path = payload.get("path") or cfg.interest_candidates_path
    conn = _open_rw(datasette)
    try:
        summary = offers.import_artifact(conn, path, interests_path=cfg.interests_path)
    finally:
        conn.close()
    return _json({"ok": not summary.get("error"), "path": path, **summary})


# --- GET /observatory/api/interests/stats --------------------------------------

async def interests_stats_view(request, datasette):
    denied = _guard(datasette, request)
    if denied:
        return denied
    if request.method != "GET":
        return _error("method not allowed", status=405)
    conn = _open_ro(datasette)
    try:
        result = funnel.interest_stats(conn, window=request.args.get("window"))
    except ValueError as e:
        return _error(str(e))
    finally:
        conn.close()
    return _json(result)


# --- POST /observatory/api/interests (create) ----------------------------------

async def create_interest_view(request, datasette):
    """Called by plugin.py's interest_index_view when the method is POST: the
     7.3 surface puts the collection listing and the create on one path, so
    the method is what distinguishes them."""
    denied = _write_guard(datasette, request)
    if denied:
        return denied
    payload, bad = await _body(request)
    if bad:
        return bad
    cfg = _cfg(datasette)
    expected_mtime = payload.pop("expected_mtime", None)
    conn = _open_rw(datasette)
    try:
        result = interests_write.save(
            conn, cfg.interests_path, payload,
            existing_key=None, expected_mtime=expected_mtime,
        )
    except (interests_write.ValidationError, interests_write.ConflictError,
            interests_write.NotFound) as e:
        return _store_error(e)
    finally:
        conn.close()
    return _json({"ok": True, **result}, status=201)


# --- POST /observatory/api/interests/<key> (update / retire) -------------------

# lifecycle -> the `active` flag interests.json carries. Only 'active' and
# 'decaying' keep collecting; a decaying interest is live but on notice. This
# is offers.set_lifecycle()'s own derivation, restated here because the wire
# accepts the lifecycle and the file stores the flag.
_LIFECYCLE_ACTIVE = {
    offers.ACTIVE: True, offers.DECAYING: True,
    offers.PAUSED: False, offers.RETIRED: False,
}


def _close_retire_offer(conn, key, note):
    """Retiring an interest answers any open proposal to retire it.

    The mirror image of undo_auto_pause(), which closes the same offer on the
    way back up. Without this, accepting a retirement leaves the proposal
    sitting in the inbox asking a question the owner just answered.
    """
    retire_key = offers.RETIRE_PREFIX + key
    row = offers.get_offer(conn, retire_key)
    if row and row["status"] in (offers.OFFERED, offers.PROPOSED, offers.SNOOZED):
        offers.reject(conn, retire_key, note=note or "retired by owner")
        return retire_key
    return None


async def interest_write_view(request, datasette):
    """Update one interest -- a full edit, a partial edit, or a lifecycle move.

    Three shapes, because the workspace sends all three:

      {"active": false}            retire; {"active": true} un-retires
      {"lifecycle": "retired"}     the same move, named the way the UI names
                                   it. `active` is DERIVED from lifecycle and
                                   never sent alongside it as a second switch.
      {...fields}                  an edit. Fields not present are carried
                                   over from the stored entry, so the editor
                                   can send just what changed -- a full entry
                                   still works and still means the same thing.

    A retire is not a column flip: it routes through interest_sync, which
    performs the move via offers.set_lifecycle(), so the interest's lifecycle
    and its append-only event chain say the same thing the decay sweep would
    have said -- which is what makes the undo a real state transition rather
    than a guess.
    """
    denied = _write_guard(datasette, request)
    if denied:
        return denied
    payload, bad = await _body(request)
    if bad:
        return bad
    key = request.url_vars["key"]
    cfg = _cfg(datasette)
    expected_mtime = payload.pop("expected_mtime", None)
    note = payload.pop("note", "") or ""

    lifecycle = payload.pop("lifecycle", None)
    if lifecycle is not None:
        if lifecycle not in _LIFECYCLE_ACTIVE:
            return _error(
                f"lifecycle must be one of {sorted(_LIFECYCLE_ACTIVE)}, not {lifecycle!r}"
            )
        if "active" in payload and bool(payload["active"]) != _LIFECYCLE_ACTIVE[lifecycle]:
            return _error(
                "active is derived from lifecycle -- send one or the other, not "
                "two switches that disagree"
            )
        payload["active"] = _LIFECYCLE_ACTIVE[lifecycle]

    conn = _open_rw(datasette)
    try:
        state = offers.interest_lifecycle(conn, key)
        if state is None:
            return _error(f"no such interest: {key}", status=404)
        current = state["lifecycle"] or offers.ACTIVE
        wanted = lifecycle
        if wanted is None and "active" in payload and isinstance(payload["active"], bool):
            # {"active": false} is the file's spelling of a retire. It only
            # names a destination when it disagrees with where we already are;
            # {"active": true} on a decaying interest means "keep collecting",
            # which is where it already is.
            if bool(payload["active"]) != _LIFECYCLE_ACTIVE[current]:
                wanted = offers.ACTIVE if payload["active"] else offers.RETIRED

        fields = {k: v for k, v in payload.items() if k != "active"}
        result = {}

        if fields:
            # A partial edit: everything the editor did not send is carried
            # over from the stored entry. A full entry still works and still
            # means the same thing.
            data, _ = interests_write.read_file(cfg.interests_path)
            index, existing = interests_write.find_entry(data, key)
            if index < 0:
                raise interests_write.NotFound(key)
            # The file's `defaults` block go UNDER the stored entry: 17 of the
            # 33 interests carry no `sources` of their own and inherit
            # ["web_search"] from it, so a merge that only saw the entry would
            # hand validate() an active interest with no sources and 400 on a
            # majority of the file. The effective value is also what the editor
            # displayed, so saving it is what the owner just looked at -- the
            # edited entry does end up spelling the inherited value out, which
            # is the honest record of what was saved.
            merged = {**data.get("defaults", {}), **existing, **fields}
            if wanted is not None:
                merged["active"] = _LIFECYCLE_ACTIVE[wanted]
            result = interests_write.save(
                conn, cfg.interests_path, merged,
                existing_key=key, expected_mtime=expected_mtime,
            )
            # The file has just been rewritten, so the caller's precondition
            # token is spent; the lifecycle move below must not re-check it.
            expected_mtime = None

        if wanted is not None and wanted != current:
            if _LIFECYCLE_ACTIVE[wanted] != _LIFECYCLE_ACTIVE[current]:
                # Collecting-ness changes, so the file's `active` flag changes
                # with it and interest_sync performs the transition -- which is
                # also what cancels the interest's PENDING missions on the way
                # down. The file is the source of truth; writing the column
                # here instead would be undone by the next sync.
                moved = interests_write.set_active(
                    conn, cfg.interests_path, key, _LIFECYCLE_ACTIVE[wanted],
                    expected_mtime=expected_mtime,
                )
                result = {**result, **moved}
            else:
                # Both states collect (decaying -> active, the "keep watching"
                # answer to a retirement proposal) or neither does (paused ->
                # retired). The file's flag is identical either way, so sync
                # sees nothing to do and the move has to be made directly.
                offers.set_lifecycle(
                    conn, key, wanted, actor=offers.OWNER_UI,
                    action="owner_lifecycle", detail={"note": note},
                )
                result.setdefault("key", key)
                result.setdefault("mtime", interests_write.file_mtime(cfg.interests_path))
            if not _LIFECYCLE_ACTIVE[wanted]:
                closed = _close_retire_offer(conn, key, note)
                if closed:
                    result["retire_offer_closed"] = closed
            current = wanted
        elif not fields:
            # Asking for the state it is already in is not a conflict. The
            # workspace's retire flow re-reads stats and only writes when the
            # lifecycle has not already moved, but a stale tab, a double-click
            # or a replayed request can still ask for where we already are --
            # and set_lifecycle() has no retired -> retired transition, so
            # without this that raises InvalidTransition and surfaces as a 409
            # the owner would have to interpret. Idempotent is the answer.
            result = {"key": key, "noop": True,
                      "mtime": interests_write.file_mtime(cfg.interests_path)}

        result["lifecycle"] = current
        result["active"] = _LIFECYCLE_ACTIVE[current]
    except (interests_write.ValidationError, interests_write.ConflictError,
            interests_write.NotFound, offers.OfferError) as e:
        return _store_error(e)
    finally:
        conn.close()
    return _json({"ok": True, **result})


# --- POST /observatory/api/interests/<key>/revive ------------------------------

async def interest_revive_view(request, datasette):
    """The one-click undo of an auto-pause (the owner's standing decision:
    dead interests auto-pause at 45 days, reversibly, with an announcement).

    offers.undo_auto_pause does the whole reversal in one call: lifecycle back
    to active, the silence clock reset from now (so the interest is not
    re-paused on the next sweep for the same 45 days it was already judged
    on), and any open retirement offer closed -- otherwise the inbox would
    immediately propose retiring what the owner just brought back.
    """
    denied = _write_guard(datasette, request)
    if denied:
        return denied
    payload, bad = await _body(request)
    if bad:
        return bad
    key = request.url_vars["key"]
    cfg = _cfg(datasette)
    note = payload.get("note") or ""
    conn = _open_rw(datasette)
    try:
        result = interests_write.revive(conn, cfg.interests_path, key, note=note)
    except (offers.OfferError, interests_write.ValidationError,
            interests_write.ConflictError, interests_write.NotFound) as e:
        return _store_error(e)
    finally:
        conn.close()
    return _json({"ok": True, **result})


# --- GET /observatory/api/edges ------------------------------------------------

async def edges_view(request, datasette):
    """Connections between interests. The table lands with PR H's schema and
    is filled by PR M's nightly lift + weekly semantic pass, so this returns
    an empty list until then -- which is the honest answer, and lets the
    connections view be built against a live endpoint rather than a mock."""
    denied = _guard(datasette, request)
    if denied:
        return denied
    if request.method != "GET":
        return _error("method not allowed", status=405)
    try:
        min_weight = float(request.args.get("min_weight") or 0.0)
    except (TypeError, ValueError):
        return _error("min_weight must be a number")
    kind = request.args.get("kind")
    sql = ("SELECT a_key, b_key, kind, weight, evidence, computed_at FROM interest_edges"
           " WHERE weight >= ?")
    params = [min_weight]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY weight DESC, a_key ASC, b_key ASC"
    conn = _open_ro(datasette)
    try:
        rows = [
            {"a": r["a_key"], "b": r["b_key"], "kind": r["kind"], "weight": r["weight"],
             "evidence": json.loads(r["evidence"] or "{}"), "computed_at": r["computed_at"]}
            for r in conn.execute(sql, params).fetchall()
        ]
    finally:
        conn.close()
    return _json({"edges": rows, "total": len(rows), "min_weight": min_weight})


# --- route table ---------------------------------------------------------------

def routes():
    """Spliced into plugin.py's register_routes() before its /api/ catch-all.

    Order matters twice: /api/offers/generate must precede the
    /api/offers/<key>/... patterns (a bare 'generate' would otherwise never
    match), and /api/interests/stats must precede /api/interests/<key> for the
    same reason. Both are expressed here rather than left to the caller.
    """
    return [
        (r"^/observatory/api/offers$", offers_view),
        (r"^/observatory/api/offers/generate$", offers_generate_view),
        (r"^/observatory/api/offers/(?P<key>[^/]+)/decide$", offer_decide_view),
        (r"^/observatory/api/offers/(?P<key>[^/]+)/provenance$", offer_provenance_view),
        (r"^/observatory/api/interests/stats$", interests_stats_view),
        (r"^/observatory/api/interests/(?P<key>[^/]+)/revive$", interest_revive_view),
        (r"^/observatory/api/interests/(?P<key>[^/]+)$", interest_write_view),
        (r"^/observatory/api/edges$", edges_view),
    ]
