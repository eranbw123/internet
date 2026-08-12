"""Observatory: a read-only Datasette plugin serving the trace backbone
(discovery/trace.py, step-13 task 1) as a browsable/queryable UI + JSON API.

Kept as its own top-level package, sibling to `discovery/`, for one reason:
`datasette` is the one sanctioned new dependency this step adds (see
PROJECT_STATE.md), and nothing in `discovery/` may require it --
`test_discovery.py` and every `discovery/` module must stay importable on a
machine without datasette installed. `datasette` is therefore only ever
imported inside this package (`observatory/plugin.py`, `observatory/app.py`)
and inside `discovery/__main__.py`'s `ui` command handler, which imports this
package lazily (the same pattern `trace-fixture` already uses for
`discovery.trace_fixture`).

`observatory/db.py` is the one module in here that stays datasette-free (pure
stdlib: sqlite3, json, difflib) -- it is the actual query layer, and having
no datasette import makes it trivially unit-testable on its own if that's
ever useful. `observatory/plugin.py` wires those queries into Datasette
routes; `observatory/app.py` is the `Datasette(...)` factory both the `ui`
CLI command and test_observatory.py's ASGI-client tests share.
"""
