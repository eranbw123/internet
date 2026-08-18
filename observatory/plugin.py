"""The Datasette plugin itself: route registration + the auth boundary.

Registered once, at Datasette-construction time (see app.py's
build_datasette()), via `datasette.plugins.pm.register()` -- there is no
setuptools entry point and nothing on disk for Datasette's normal plugin
discovery to find, which is deliberate: this plugin only ever needs to run
inside a Datasette instance THIS repo constructs (the `ui` CLI command /
test_observatory.py), never one datasette itself discovers unprompted.

Every view function below is a plain (async) function taking `(request,
datasette)` -- Datasette's own `wrap_view()` introspects the signature and
supplies whichever of `scope`/`receive`/`send`/`request`/`datasette` it asks
for (see datasette.app.wrap_view), so a synchronous "async def" with just
`(request, datasette)` is enough.

Auth model (see `actor_from_request`/`permission_allowed` below): in the
default (private, localhost-bound) mode both hooks are permissive no-ops --
`ui`'s own `--host 127.0.0.1` default is the boundary. In `--public` mode
(`datasette._observatory_public = True`, set by app.py from `cfg.ui_token`)
they become a single shared gate: an actor is only ever resolved from a
correct bearer token, and permission is granted iff an actor was resolved --
this governs BOTH our own /observatory/* routes (checked explicitly, since
custom routes aren't auto-gated) AND every one of Datasette's native
table/row/SQL views (gated for free via the permission_allowed hook, which
core already calls before serving any of them).
"""
import hmac
import mimetypes
import pathlib

from datasette import Response, hookimpl

from . import db as odb

STATIC_DIR = pathlib.Path(__file__).parent / "static"


def _public(datasette):
    return bool(getattr(datasette, "_observatory_public", False))


@hookimpl
def actor_from_request(datasette, request):
    if not _public(datasette):
        return {"id": "local"}
    token = getattr(datasette, "_observatory_token", "") or ""
    supplied = _bearer_token(request)
    if token and supplied and hmac.compare_digest(supplied, token):
        return {"id": "observatory-public"}
    return None


def _bearer_token(request):
    header = request.headers.get("authorization") or ""
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return request.args.get("token")


# Datasette's write-action vocabulary (its own JSON write API, 1.x+; not
# registered as a route in the pinned 0.65.x, so this is inert today, but
# requirements.txt pins no upper bound and permission_allowed is the only
# gate a future datasette version would consult before honoring one).
# Denied unconditionally, in both private and public mode -- "the plugin
# registers no write routes" should hold even if datasette itself grows one.
_WRITE_ACTIONS = {
    "insert-row", "update-row", "delete-row",
    "create-table", "drop-table", "alter-table",
}


@hookimpl
def permission_allowed(datasette, actor, action, resource):
    """Single shared boundary: in private mode everything read-only is
    allowed (the bind-to-localhost default IS the boundary); in public mode
    nothing is, for anyone who didn't present the configured token -- covers
    our own routes (see `_guard` below) and every native Datasette view
    alike. Write actions are always denied, regardless of mode/actor."""
    if action in _WRITE_ACTIONS:
        return False
    if not _public(datasette):
        return True
    return actor is not None


def _guard(datasette, request):
    """Called at the top of every observatory view. Returns a 403 Response
    if this request must be rejected in public mode with no valid actor;
    None if the view should proceed."""
    if _public(datasette) and request.actor is None:
        return Response.json({"error": "forbidden -- missing or invalid token"}, status=403)
    return None


def _require_get(request):
    if request.method != "GET":
        return Response.text("method not allowed", status=405)
    return None


def _bad_request(message):
    return Response.json({"error": message}, status=400)


def _connect(datasette):
    return odb.open_ro(getattr(datasette, "_observatory_db_path"))


def _database_name(datasette):
    return odb.db_name(getattr(datasette, "_observatory_db_path"))


# --- shell page + static assets -----------------------------------------------

def _shell_html(bootstrap_json):
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = f'<script id="observatory-bootstrap" type="application/json">{bootstrap_json}</script>'
    return html.replace("<!--OBSERVATORY_BOOTSTRAP-->", script)


async def index_view(request, datasette):
    denied = _guard(datasette, request)
    if denied:
        return denied
    import json as _json
    return Response.html(_shell_html(_json.dumps({"focus": None, "public": _public(datasette)})))


async def static_view(request, datasette):
    denied = _guard(datasette, request)
    if denied:
        return denied
    rel = request.url_vars.get("path", "")
    target = (STATIC_DIR / rel).resolve()
    try:
        target.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return Response.text("not found", status=404)
    if not target.is_file():
        return Response.text("not found", status=404)
    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return Response(target.read_bytes(), content_type=content_type)


# --- JSON APIs -----------------------------------------------------------------

async def list_view(request, datasette):
    denied = _guard(datasette, request) or _require_get(request)
    if denied:
        return denied
    tab = request.args.get("tab") or "discoveries"
    if tab not in odb.TABS:
        return Response.json({"error": f"unknown tab {tab!r}"}, status=400)
    filters = {
        key: request.args.get(key)
        for key in (
            "interest", "layer", "source", "provider", "model", "mission", "sent",
            "feedback", "date_from", "date_to", "failure_stage", "trace_complete",
            "active",
        )
        if request.args.get(key)
    }
    conn = _connect(datasette)
    try:
        result = odb.list_rows(
            conn, tab, filters=filters, search=request.args.get("search") or "",
            limit=request.args.get("limit"), offset=request.args.get("offset"),
        )
    except ValueError as e:
        return _bad_request(str(e))
    finally:
        conn.close()
    return Response.json(result)


async def graph_view(request, datasette):
    denied = _guard(datasette, request) or _require_get(request)
    if denied:
        return denied
    conn = _connect(datasette)
    try:
        result = odb.graph(
            conn,
            entity_type=request.args.get("entity_type"),
            entity_id=request.args.get("entity_id"),
            run_id=request.args.get("run_id"),
        )
    except ValueError:
        return _bad_request("run_id must be an integer")
    finally:
        conn.close()
    if result is None:
        return Response.json({"error": "no matching trace"}, status=404)
    return Response.json(result)


async def children_view(request, datasette):
    denied = _guard(datasette, request) or _require_get(request)
    if denied:
        return denied
    node_id = request.args.get("node_id")
    group = request.args.get("group")
    if not node_id and not group:
        return Response.json({"error": "node_id or group required"}, status=400)
    conn = _connect(datasette)
    try:
        rows = odb.children(conn, node_id=node_id, group=group)
    except ValueError as e:
        return _bad_request(str(e))
    finally:
        conn.close()
    return Response.json({"children": rows})


async def node_view(request, datasette):
    denied = _guard(datasette, request) or _require_get(request)
    if denied:
        return denied
    node_id = int(request.url_vars["node_id"])
    conn = _connect(datasette)
    try:
        result = odb.node_detail(conn, node_id, _database_name(datasette))
    finally:
        conn.close()
    if result is None:
        return Response.json({"error": "no such node"}, status=404)
    return Response.json(result)


async def interest_view(request, datasette):
    denied = _guard(datasette, request) or _require_get(request)
    if denied:
        return denied
    key = request.url_vars["key"]
    conn = _connect(datasette)
    try:
        result = odb.interest_detail(conn, key)
    finally:
        conn.close()
    if result is None:
        return Response.json({"error": "no such interest"}, status=404)
    return Response.json(result)


async def compare_view(request, datasette):
    denied = _guard(datasette, request) or _require_get(request)
    if denied:
        return denied
    a, b = request.args.get("a"), request.args.get("b")
    if not a or not b:
        return _bad_request("a and b required")
    kind = request.args.get("kind") or "run"
    if kind not in odb.COMPARE_KINDS:
        return _bad_request(f"unknown kind {kind!r} -- must be one of {sorted(odb.COMPARE_KINDS)}")
    conn = _connect(datasette)
    try:
        result = odb.compare(conn, kind, a, b)
    except ValueError:
        return _bad_request("a and b must be integers")
    except LookupError as e:
        return Response.json({"error": str(e)}, status=404)
    finally:
        conn.close()
    return Response.json(result)


async def api_not_found_view(request, datasette):
    denied = _guard(datasette, request)
    if denied:
        return denied
    return Response.json({"error": f"no such endpoint: {request.path}"}, status=404)


async def run_index_view(request, datasette):
    denied = _guard(datasette, request) or _require_get(request)
    if denied:
        return denied
    conn = _connect(datasette)
    try:
        return Response.json(odb.run_index(conn))
    finally:
        conn.close()


async def interest_index_view(request, datasette):
    denied = _guard(datasette, request) or _require_get(request)
    if denied:
        return denied
    conn = _connect(datasette)
    try:
        return Response.json(odb.interest_index(conn))
    finally:
        conn.close()


async def prompt_template_view(request, datasette):
    denied = _guard(datasette, request) or _require_get(request)
    if denied:
        return denied
    call_id = request.args.get("call")
    if not call_id:
        return _bad_request("call required")
    try:
        call_id = int(call_id)
    except (TypeError, ValueError):
        return _bad_request("call must be an integer")
    conn = _connect(datasette)
    try:
        result = odb.prompt_template(conn, call_id)
    except LookupError as e:
        return Response.json({"error": str(e)}, status=404)
    finally:
        conn.close()
    return Response.json(result)


async def trace_score_view(request, datasette):
    denied = _guard(datasette, request)
    if denied:
        return denied
    score_id = int(request.url_vars["score_id"])
    conn = _connect(datasette)
    try:
        target = odb.score_trace_target(conn, score_id)
    finally:
        conn.close()
    if target is None:
        return Response.text(f"no trace found for score {score_id}", status=404)
    import json as _json
    focus = {"kind": "score", **target}
    return Response.html(_shell_html(_json.dumps({"focus": focus, "public": _public(datasette)})))


@hookimpl
def register_routes():
    return [
        (r"^/observatory/$", index_view),
        (r"^/observatory/static/(?P<path>.*)$", static_view),
        (r"^/observatory/api/list$", list_view),
        (r"^/observatory/api/graph$", graph_view),
        (r"^/observatory/api/children$", children_view),
        (r"^/observatory/api/node/(?P<node_id>[0-9]+)$", node_view),
        # Registered before the /interest/<key> route: "interests" would
        # otherwise be swallowed as a key by the pattern below.
        (r"^/observatory/api/interests$", interest_index_view),
        (r"^/observatory/api/interest/(?P<key>[^/]+)$", interest_view),
        (r"^/observatory/api/compare$", compare_view),
        (r"^/observatory/api/prompt-template$", prompt_template_view),
        (r"^/observatory/api/runs$", run_index_view),
        (r"^/observatory/trace/score/(?P<score_id>[0-9]+)$", trace_score_view),
        # Last: anything under /api/ that matched none of the above is a
        # mistake in the caller, and should say so in JSON. Without this it
        # fell through to Datasette's own routing and 302'd into the raw
        # database UI -- e.g. /api/node/12:matched:match (a synthetic group
        # id) misses the digits-only node route and redirected instead of
        # 404ing, which is a confusing thing to find in a network tab.
        (r"^/observatory/api/.*$", api_not_found_view),
    ]
