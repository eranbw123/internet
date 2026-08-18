"""Read-only query layer over discovery.db's operational + trace tables.

Pure stdlib (sqlite3, json, difflib) -- no datasette import here, see this
package's __init__.py. Every connection this module opens is a genuine
SQLite read-only connection (`file:...?mode=ro`), independent of whatever
mode Datasette itself opens the same file with -- belt and suspenders on top
of task 1's own append-only discipline: nothing in this module can write.

Every JSON-shaped value handed back to a caller (plugin.py's routes) is run
through discovery.trace.redact_json before being returned -- defense in
depth on top of task 1's at-write redaction, per this step's own acceptance
criteria (a planted secret must not survive a second, independent pass).
"""
import difflib
import json
import pathlib
import sqlite3

from discovery import models
from discovery import trace as trace_mod

# Sibling-set node_type groups bigger than this are collapsed to one
# {group, child_count} placeholder in graph()'s response -- large traces
# (a real Council generation has 5 advisors + 5 peer reviewers; a busy
# mission can return dozens of raw results) would otherwise make opening a
# single trace read (and render) every row it ever produced.
COLLAPSE_THRESHOLD = 3

# api/children is the lazy-load escape hatch collapsing exists for -- it must
# stay bounded itself, or a genuinely huge sibling set (a busy mission's raw
# results) makes opening it exactly as unusable as never collapsing at all.
MAX_CHILDREN = 500

# A candidate can fan out to several score attempts across retried runs; a
# borrowed prompt is a convenience, so cap how many neighbours we will pull
# calls from rather than letting one node's detail response grow unbounded
# (prompts average ~14kB each).
MAX_RELATED_CALL_NODES = 4

# node_type -> swimlane, exactly the six named in the plan. Anything not
# listed here (there shouldn't be anything -- this is meant to cover every
# node_type discovery/{missions,pipeline,council,feedback_listener}.py
# actually write) falls back to 'other' rather than raising, since a trace
# row must never become un-renderable.
SWIMLANES = {
    "interest-state": "interest-state",
    "generation": "council",
    "council-context": "council",
    "council": "council",
    "advisor": "council",
    "peer-review": "council",
    "aggregate-ranking": "council",
    "rejected-angle": "council",
    "chairman": "council",
    "mission": "mission",
    "mission-execution": "mission",
    "raw-result": "mission",
    "raw-result-dropped": "mission",
    "tool-event": "mission",
    "collector-item": "candidate-pipeline",
    "duplicate": "candidate-pipeline",
    "candidate": "candidate-pipeline",
    "match": "candidate-pipeline",
    "prefilter": "candidate-pipeline",
    "score-attempt": "scoring",
    "score-debug": "scoring",
    "threshold": "scoring",
    "render": "delivery-feedback",
    "notification": "delivery-feedback",
    "feedback": "delivery-feedback",
}


def swimlane(node_type):
    return SWIMLANES.get(node_type, "other")


def db_name(db_path):
    """The name Datasette gives this file -- its stem, exactly how
    `Datasette(files=[path])` derives it -- so row URLs
    ('/<db_name>/<table>/<pk>') resolve to the same instance."""
    return pathlib.Path(db_path).stem


def open_ro(db_path):
    """A genuine read-only SQLite connection: `file:...?mode=ro`, not just a
    connection nobody happens to write through. Raises OperationalError if
    the file doesn't exist yet (mode=ro can't create one) -- callers should
    only reach this once discovery.db has been created by `init`."""
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r is not None else None


def _redact_row(row):
    return trace_mod.redact_json(row)


# --- /api/list ---------------------------------------------------------------

TABS = ("discoveries", "interests", "generations", "missions", "failed")
DEFAULT_LIMIT = 50
MAX_LIMIT = 50


def list_rows(conn, tab, filters=None, search="", limit=None, offset=0):
    if tab not in TABS:
        raise ValueError(f"unknown tab {tab!r}")
    # SQLite treats a negative LIMIT as "no upper bound" -- clamp the low end
    # too, not just the high one, or `limit=-1` returns the entire result set
    # against the plugin's most expensive query (the discoveries join).
    limit = max(min(int(limit or DEFAULT_LIMIT), MAX_LIMIT), 1)
    offset = max(int(offset or 0), 0)
    filters = filters or {}
    builder = {
        "discoveries": _discoveries_query,
        "interests": _interests_query,
        "generations": _generations_query,
        "missions": _missions_query,
        "failed": _failed_query,
    }[tab]
    base_sql, where_sql, params, order_sql = builder(filters, search)
    total = conn.execute(f"SELECT COUNT(*) c FROM ({base_sql} {where_sql})", params).fetchone()["c"]
    rows = _rows(
        conn, f"{base_sql} {where_sql} {order_sql} LIMIT ? OFFSET ?", (*params, limit, offset)
    )
    return {
        "tab": tab, "limit": limit, "offset": offset, "total": total,
        "rows": [_redact_row(r) for r in rows],
    }


def _like(value):
    # Escape SQLite LIKE's own wildcards so a literal '%'/'_'/'\' in a search
    # term is matched literally instead of over-matching (e.g. search="_"
    # would otherwise match nearly every row). Every LIKE clause built with
    # this value must carry a matching ESCAPE '\' clause.
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _discoveries_query(filters, search):
    base = """
        SELECT
            ci.id AS item_id, ci.source, ci.type, ci.title, ci.url,
            ci.published_at, ci.first_seen_at, ci.origin_interest,
            s.id AS score_id, s.interest_id, s.final_score, s.confidence,
            s.reason, s.provider, s.model, s.created_at AS scored_at,
            it.key AS interest_key, it.layer AS interest_layer,
            no.id AS notification_id, no.ok AS notification_ok, no.sent_at,
            json_extract(ci.metadata, '$.mission_id') AS mission_id,
            json_extract(ci.metadata, '$.mission_label') AS mission_label,
            json_extract(ci.metadata, '$.generation_id') AS generation_id,
            (SELECT tn.id FROM trace_nodes tn WHERE tn.entity_type = 'candidate_items'
                AND tn.entity_id = CAST(ci.id AS TEXT) AND tn.node_type = 'candidate'
                ORDER BY tn.id DESC LIMIT 1) AS trace_node_id,
            (SELECT tn.run_id FROM trace_nodes tn WHERE tn.entity_type = 'candidate_items'
                AND tn.entity_id = CAST(ci.id AS TEXT) AND tn.node_type = 'candidate'
                ORDER BY tn.id DESC LIMIT 1) AS run_id,
            (SELECT verdict FROM feedback f WHERE f.item_id = ci.id
                ORDER BY f.id DESC LIMIT 1) AS feedback_verdict
        FROM candidate_items ci
        LEFT JOIN scores s ON s.item_id = ci.id
        LEFT JOIN interests it ON it.id = s.interest_id
        LEFT JOIN notifications no ON no.score_id = s.id
    """
    clauses, params = [], []
    if filters.get("interest"):
        clauses.append("it.key = ?"); params.append(filters["interest"])
    if filters.get("layer"):
        clauses.append("it.layer = ?"); params.append(filters["layer"])
    if filters.get("source"):
        clauses.append("ci.source = ?"); params.append(filters["source"])
    if filters.get("provider"):
        clauses.append("s.provider = ?"); params.append(filters["provider"])
    if filters.get("model"):
        clauses.append("s.model = ?"); params.append(filters["model"])
    if filters.get("mission"):
        clauses.append("json_extract(ci.metadata, '$.mission_label') = ?"); params.append(filters["mission"])
    sent = filters.get("sent")
    if sent == "yes":
        clauses.append("no.ok = 1")
    elif sent == "no":
        clauses.append("(no.id IS NULL OR no.ok = 0)")
    feedback = filters.get("feedback")
    if feedback == "none":
        clauses.append("NOT EXISTS (SELECT 1 FROM feedback f WHERE f.item_id = ci.id)")
    elif feedback:
        clauses.append("EXISTS (SELECT 1 FROM feedback f WHERE f.item_id = ci.id AND f.verdict = ?)")
        params.append(feedback)
    if filters.get("date_from"):
        clauses.append("COALESCE(s.created_at, ci.first_seen_at) >= ?"); params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("COALESCE(s.created_at, ci.first_seen_at) <= ?"); params.append(filters["date_to"])
    trace_complete = filters.get("trace_complete")
    _cand_exists = (
        "EXISTS (SELECT 1 FROM trace_nodes tn WHERE tn.entity_type = 'candidate_items' "
        "AND tn.entity_id = CAST(ci.id AS TEXT) AND tn.node_type = 'candidate')"
    )
    if trace_complete == "yes":
        clauses.append(_cand_exists)
    elif trace_complete == "no":
        clauses.append(f"NOT {_cand_exists}")
    failure_stage = filters.get("failure_stage")
    if failure_stage == "scoring_error":
        clauses.append(
            "EXISTS (SELECT 1 FROM trace_nodes sa WHERE sa.entity_type = 'candidate_items' "
            "AND sa.entity_id = CAST(ci.id AS TEXT) AND sa.node_type = 'score-attempt' AND sa.status = 'error')"
        )
    elif failure_stage == "below_threshold":
        clauses.append(
            "EXISTS (SELECT 1 FROM trace_edges e JOIN trace_nodes th ON th.id = e.to_node_id "
            "WHERE th.node_type = 'threshold' AND th.entity_type = 'scores' "
            "AND th.entity_id = CAST(s.id AS TEXT) AND e.relationship = 'rejected')"
        )
    elif failure_stage == "prefilter":
        # Must match the actual prefilter-rejection node (node_type='prefilter',
        # label='filtered' -- see pipeline.ingest()'s filtered_node, and
        # _FAILED_UNION's own 'prefilter_rejected' branch above), not just
        # "this item has ANY trace_nodes row" -- every non-duplicate item has
        # a 'candidate' node regardless of outcome, so the original
        # entity-only EXISTS matched any unscored item (e.g. a future
        # budget-deferred one), not specifically a prefiltered one.
        clauses.append(
            "EXISTS (SELECT 1 FROM trace_edges e JOIN trace_nodes pf ON pf.id = e.to_node_id "
            "JOIN trace_nodes c ON c.id = e.from_node_id "
            "WHERE c.entity_type = 'candidate_items' AND c.entity_id = CAST(ci.id AS TEXT) "
            "AND c.node_type = 'candidate' AND e.relationship = 'rejected' "
            "AND pf.node_type = 'prefilter' AND pf.label = 'filtered')"
        )
    if search:
        q = _like(search)
        clauses.append(
            "(ci.title LIKE ? ESCAPE '\\' OR ci.url LIKE ? ESCAPE '\\' OR ci.text LIKE ? ESCAPE '\\' "
            "OR s.reason LIKE ? ESCAPE '\\' OR EXISTS ("
            "SELECT 1 FROM model_calls mc JOIN trace_nodes tn3 ON tn3.id = mc.trace_node_id "
            "WHERE ((tn3.entity_type = 'candidate_items' AND tn3.entity_id = CAST(ci.id AS TEXT)) "
            "OR (tn3.entity_type = 'search_missions' "
            "AND tn3.entity_id = CAST(json_extract(ci.metadata, '$.mission_id') AS TEXT))) "
            "AND (mc.exact_user_prompt LIKE ? ESCAPE '\\' OR mc.exact_system_prompt LIKE ? ESCAPE '\\' "
            "OR mc.raw_response_text LIKE ? ESCAPE '\\' OR mc.parsed_response_json LIKE ? ESCAPE '\\')))"
        )
        params.extend([q, q, q, q, q, q, q, q])
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return base, where_sql, params, "ORDER BY ci.id DESC"


def _interests_query(filters, search):
    base = """
        SELECT it.*,
            (SELECT COUNT(*) FROM search_generations g WHERE g.interest_key = it.key) AS generations_count,
            (SELECT COUNT(*) FROM search_missions m WHERE m.interest_key = it.key) AS missions_count,
            (SELECT COUNT(*) FROM scores s2 WHERE s2.interest_id = it.id) AS discoveries_count,
            (SELECT COUNT(*) FROM feedback f2 WHERE f2.interest_id = it.id) AS feedback_count
        FROM interests it
    """
    clauses, params = [], []
    if filters.get("layer"):
        clauses.append("it.layer = ?"); params.append(filters["layer"])
    if search:
        q = _like(search)
        clauses.append("(it.key LIKE ? ESCAPE '\\' OR it.title LIKE ? ESCAPE '\\' OR it.description LIKE ? ESCAPE '\\')")
        params.extend([q, q, q])
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return base, where_sql, params, "ORDER BY it.id ASC"


def _generations_query(filters, search):
    base = """
        SELECT g.*, it.layer AS interest_layer
        FROM search_generations g
        LEFT JOIN interests it ON it.key = g.interest_key
    """
    clauses, params = [], []
    if filters.get("interest"):
        clauses.append("g.interest_key = ?"); params.append(filters["interest"])
    if filters.get("provider"):
        clauses.append("g.provider = ?"); params.append(filters["provider"])
    if filters.get("model"):
        clauses.append("g.model = ?"); params.append(filters["model"])
    if filters.get("date_from"):
        clauses.append("g.created_at >= ?"); params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("g.created_at <= ?"); params.append(filters["date_to"])
    if search:
        q = _like(search)
        clauses.append("(g.interest_key LIKE ? ESCAPE '\\' OR g.error LIKE ? ESCAPE '\\')")
        params.extend([q, q])
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return base, where_sql, params, "ORDER BY g.id DESC"


def _missions_query(filters, search):
    base = """
        SELECT m.*, g.interest_key AS generation_interest_key
        FROM search_missions m
        LEFT JOIN search_generations g ON g.id = m.generation_id
    """
    clauses, params = [], []
    if filters.get("interest"):
        clauses.append("m.interest_key = ?"); params.append(filters["interest"])
    if filters.get("mission"):
        clauses.append("m.label = ?"); params.append(filters["mission"])
    if filters.get("date_from"):
        clauses.append("m.created_at >= ?"); params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("m.created_at <= ?"); params.append(filters["date_to"])
    if search:
        q = _like(search)
        clauses.append("(m.label LIKE ? ESCAPE '\\' OR m.prompt LIKE ? ESCAPE '\\' OR m.last_error LIKE ? ESCAPE '\\')")
        params.extend([q, q, q])
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return base, where_sql, params, "ORDER BY m.id DESC"


_FAILED_UNION = """
    SELECT 'scoring_error' AS kind, tn.id AS node_id, tn.label AS label, tn.error AS detail,
           tn.started_at AS at, tn.run_id AS run_id, tn.entity_type AS entity_type, tn.entity_id AS entity_id
    FROM trace_nodes tn WHERE tn.node_type = 'score-attempt' AND tn.status = 'error'
    UNION ALL
    SELECT 'mission_failed',
           (SELECT tn2.id FROM trace_nodes tn2 WHERE tn2.entity_type = 'search_missions'
                AND tn2.entity_id = CAST(m.id AS TEXT) AND tn2.node_type = 'mission-execution'
                ORDER BY tn2.id DESC LIMIT 1),
           m.label, m.last_error, COALESCE(m.finished_at, m.created_at),
           (SELECT tn2.run_id FROM trace_nodes tn2 WHERE tn2.entity_type = 'search_missions'
                AND tn2.entity_id = CAST(m.id AS TEXT) AND tn2.node_type = 'mission-execution'
                ORDER BY tn2.id DESC LIMIT 1),
           'search_missions', CAST(m.id AS TEXT)
    FROM search_missions m
    WHERE m.status = 'FAILED'
    UNION ALL
    SELECT 'generation_failed', tn3.id, g.interest_key, g.error, g.created_at,
           tn3.run_id, 'search_generations', CAST(g.id AS TEXT)
    FROM search_generations g
    LEFT JOIN trace_nodes tn3
        ON tn3.entity_type = 'search_generations' AND tn3.entity_id = CAST(g.id AS TEXT)
        AND tn3.node_type = 'generation'
    WHERE g.status = 'FAILED'
    UNION ALL
    SELECT 'duplicate', tn4.id, tn4.label, tn4.summary, tn4.started_at, tn4.run_id, tn4.entity_type, tn4.entity_id
    FROM trace_nodes tn4 WHERE tn4.node_type = 'duplicate'
    UNION ALL
    SELECT 'prefilter_rejected', tn5.id, tn5.label, tn5.summary, tn5.started_at, tn5.run_id, tn5.entity_type, tn5.entity_id
    FROM trace_nodes tn5 WHERE tn5.node_type = 'prefilter' AND tn5.label = 'filtered'
    UNION ALL
    SELECT 'below_threshold', th.id, th.label, th.summary, th.started_at, th.run_id, th.entity_type, th.entity_id
    FROM trace_edges e JOIN trace_nodes th ON th.id = e.to_node_id
    WHERE e.relationship = 'rejected' AND th.node_type = 'threshold'
"""


def _failed_query(filters, search):
    base = f"SELECT * FROM ({_FAILED_UNION})"
    clauses, params = [], []
    if filters.get("failure_stage"):
        clauses.append("kind = ?"); params.append(filters["failure_stage"])
    if filters.get("date_from"):
        clauses.append("at >= ?"); params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("at <= ?"); params.append(filters["date_to"])
    if search:
        q = _like(search)
        clauses.append("(label LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\')")
        params.extend([q, q])
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return base, where_sql, params, "ORDER BY at DESC"


# --- /api/graph ----------------------------------------------------------------

# A hard cap on how many nodes one graph() call ever walks to -- a single
# discovery's trace (one item's whole story, or one tick's worth of work)
# stays far under this in practice; it exists only so a pathological/
# unexpected edge chain can never turn one request into a full-table scan.
MAX_COMPONENT_NODES = 5000


def _seed_node_ids(conn, entity_type=None, entity_id=None, run_id=None):
    """The node(s) graph() starts its connected-component walk from, plus
    the one node (if any) that should be the emphasized `focus_node_id`."""
    if entity_type and entity_id is not None:
        row = _row(
            conn,
            "SELECT id FROM trace_nodes WHERE entity_type = ? AND entity_id = ? ORDER BY id DESC LIMIT 1",
            (entity_type, str(entity_id)),
        )
        if row is None:
            return [], None
        return [row["id"]], row["id"]
    if run_id is not None:
        ids = [r["id"] for r in _rows(conn, "SELECT id FROM trace_nodes WHERE run_id = ?", (int(run_id),))]
        return ids, None
    return [], None


def _connected_component(conn, seed_ids):
    """Every node reachable from `seed_ids` by following trace_edges in
    EITHER direction -- edges legitimately cross trace_runs (e.g. a
    'threshold' node from a web-tick run 'rendered'-edges to a 'render' node
    on a later digest run, which in turn 'feedback_on'-edges to a feedback
    node from a still-later listener run), so "the connected trace graph"
    for one discovery is not just one run's own nodes. A plain SQL
    `WHERE run_id = ?` would silently truncate the story at whichever run
    happened to create the seed node."""
    if not seed_ids:
        return set()
    placeholders = ",".join("?" for _ in seed_ids)
    rows = conn.execute(
        f"""
        WITH RECURSIVE reachable(node_id) AS (
            SELECT id FROM trace_nodes WHERE id IN ({placeholders})
            UNION
            SELECT e.to_node_id FROM trace_edges e JOIN reachable r ON e.from_node_id = r.node_id
            UNION
            SELECT e.from_node_id FROM trace_edges e JOIN reachable r ON e.to_node_id = r.node_id
        )
        SELECT node_id FROM reachable LIMIT ?
        """,
        (*seed_ids, MAX_COMPONENT_NODES),
    ).fetchall()
    return {r["node_id"] for r in rows}


def graph(conn, entity_type=None, entity_id=None, run_id=None):
    seed_ids, focus_node_id = _seed_node_ids(conn, entity_type, entity_id, run_id)
    if not seed_ids:
        return None
    component = _connected_component(conn, seed_ids)
    if not component:
        return None
    placeholders = ",".join("?" for _ in component)
    component_list = list(component)
    all_nodes = _rows(
        conn, f"SELECT * FROM trace_nodes WHERE id IN ({placeholders}) ORDER BY id ASC", component_list
    )
    all_edges = _rows(conn, f"""
        SELECT * FROM trace_edges WHERE from_node_id IN ({placeholders}) AND to_node_id IN ({placeholders})
        ORDER BY ordinal ASC, from_node_id ASC
    """, component_list + component_list)
    nodes_by_id = {n["id"]: n for n in all_nodes}
    run_ids = sorted({n["run_id"] for n in all_nodes})
    resolved_run_id = int(run_id) if run_id is not None else (run_ids[0] if run_ids else None)

    # Emphasized path is computed BEFORE any collapsing -- a sibling set that
    # the selected entity's own branch runs through must never be eligible to
    # hide it, and this is the only thing that can tell us that.
    emphasized_path = _path_to_root(all_edges, focus_node_id) if focus_node_id is not None else []
    protected = set(emphasized_path)

    # Forward/inbound adjacency within the component, used only to find each
    # candidate sibling set's own subtree (its descendants) before deciding
    # whether to collapse it.
    forward = {}
    inbound = {}
    for e in all_edges:
        forward.setdefault(e["from_node_id"], []).append(e["to_node_id"])
        inbound.setdefault(e["to_node_id"], []).append(e["from_node_id"])

    def _subtree(start_ids):
        """Every node reachable forward from start_ids over ANY relationship
        -- an upper bound only, see _owned_subtree() below. Not every edge
        in the vocabulary is containment: `duplicate_of`/`retried_as`/
        `feedback_on` point AT an already-existing node that belongs to its
        own, differently-parented branch (e.g. a duplicate's `duplicate_of`
        edge points at the earlier, already-stored candidate it duplicates,
        which hangs off a totally different raw-result/collector-item).
        Treating those as containment would silently absorb that foreign
        branch into whichever sibling set happens to reach it first."""
        seen = set()
        stack = list(start_ids)
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            stack.extend(forward.get(nid, ()))
        return seen

    def _owned_subtree(child_ids, parent_id):
        """The subset of _subtree(child_ids) actually OWNED by this sibling
        set: a node is only absorbed if EVERY inbound edge into it comes
        from `parent_id` or another node already owned by this set. This is
        what stops a cross-link edge (duplicate_of/retried_as/feedback_on)
        from dragging a foreign, differently-parented branch into a group
        that then gets hidden (or wrongly refuses to collapse because that
        foreign branch happens to sit on the emphasized path) -- the
        foreign node keeps its own inbound edge from its REAL parent, which
        lies outside this set, so it can never become owned here.
        `child_ids` themselves are always owned (their only legitimate
        parent is `parent_id`, this set's own anchor)."""
        upper_bound = _subtree(child_ids)
        owned = set(child_ids)
        changed = True
        while changed:
            changed = False
            for nid in upper_bound - owned:
                preds = inbound.get(nid, ())
                if preds and all(p == parent_id or p in owned for p in preds):
                    owned.add(nid)
                    changed = True
        return owned

    # Group sibling children -- same from_node_id, same relationship, AND
    # same child node_type (a generation node's 'generated' children are one
    # council-context node plus three DIFFERENT mission nodes; grouping by
    # (parent, relationship) alone would lump those together and hide the
    # mission branches behind a single misleading placeholder) -- once the
    # group is bigger than COLLAPSE_THRESHOLD. Collapsed children AND their
    # own descendants are hidden from `nodes`/`edges` and replaced by one
    # placeholder node in `nodes` (also listed in `groups`);
    # /api/children?group=<id> lazy-loads the real one-level rows on demand.
    siblings = {}
    for e in all_edges:
        child = nodes_by_id.get(e["to_node_id"])
        if child is None:
            continue
        key = (e["from_node_id"], e["relationship"], child["node_type"])
        siblings.setdefault(key, []).append(e)

    # First pass: find every sibling set eligible to collapse (over
    # threshold, not touching the emphasized path), each with its own
    # subtree computed independently of the others.
    candidates = []
    for (parent_id, relationship, child_type), edges in siblings.items():
        if len(edges) <= COLLAPSE_THRESHOLD:
            continue
        child_ids = [e["to_node_id"] for e in edges]
        owned = _owned_subtree(child_ids, parent_id)
        if owned & protected:
            # The selected entity's own branch runs through this sibling
            # set -- never collapse it away.
            continue
        candidates.append({
            "parent_id": parent_id, "relationship": relationship, "child_type": child_type,
            "child_ids": child_ids, "subtree": owned,
        })

    # Second pass: a candidate set whose own PARENT node already sits inside
    # another candidate's subtree is nested inside a set that's collapsing
    # anyway -- skip it. Its whole subtree is already covered by the outer
    # group's hide, and giving it its own placeholder would either duplicate
    # that subtree under two groups (dict-order dependent, whichever group
    # is built last wins the node) or, if built first, leave it with no
    # inbound edge once the outer group takes over its parent. This check is
    # symmetric/order-independent and applies at any nesting depth.
    hidden_to_group = {}
    groups = []
    for c in candidates:
        if any(c["parent_id"] in other["subtree"] for other in candidates if other is not c):
            continue
        group_id = f"{c['parent_id']}:{c['relationship']}:{c['child_type']}"
        for nid in c["subtree"]:
            hidden_to_group[nid] = group_id
        groups.append({
            "group": group_id,
            "parent_node_id": c["parent_id"],
            "relationship": c["relationship"],
            "child_node_type": c["child_type"],
            "child_count": len(c["child_ids"]),
        })

    def _endpoint(nid):
        """Map a hidden node id to the id of the group pseudo-node that now
        stands in for it (and its whole subtree); anything not hidden maps
        to itself. This single mapping is what keeps the returned graph
        connected: it produces the parent->group edge AND rewires any edge
        that crosses a collapsed set's boundary (e.g. a hidden threshold
        node's own 'rendered' edge into a later digest run's render node)."""
        return hidden_to_group.get(nid, nid)

    out_nodes = [_node_summary(n) for n in all_nodes if n["id"] not in hidden_to_group]
    for g in groups:
        out_nodes.append({
            "id": g["group"], "run_id": None, "node_type": "group",
            "swimlane": swimlane(g["child_node_type"]), "entity_type": None, "entity_id": None,
            "label": f"{g['child_count']} {g['child_node_type']}", "status": None,
            "summary": None, "started_at": None, "finished_at": None, "error": None,
            "child_count": g["child_count"],
        })

    # `ordinal` disambiguates siblings under one (from, relationship) pair
    # (e.g. 6 'returned' raw-results, ordinal 0..5); once several siblings
    # collapse into the same group pseudo-node, those edges become several
    # (from, group, relationship) rows differing only by that now-meaningless
    # ordinal, so the dedup key below drops it -- one edge per logical
    # (from, to, relationship) is what a connected graph needs either way.
    seen_edge_keys = set()
    out_edges = []
    for e in all_edges:
        frm, to = _endpoint(e["from_node_id"]), _endpoint(e["to_node_id"])
        if frm == to:
            continue  # both ends collapsed into the same group -- an internal edge, not part of the shown graph
        edge_key = (frm, to, e["relationship"])
        if edge_key in seen_edge_keys:
            continue
        seen_edge_keys.add(edge_key)
        out_edges.append({"from": frm, "to": to, "relationship": e["relationship"], "ordinal": e["ordinal"]})

    mapped_path = [_endpoint(nid) for nid in emphasized_path]
    deduped_path = []
    for nid in mapped_path:
        if not deduped_path or deduped_path[-1] != nid:
            deduped_path.append(nid)

    result = {
        "run_id": resolved_run_id,
        "run_ids": run_ids,
        "focus_node_id": focus_node_id,
        "nodes": out_nodes,
        "edges": out_edges,
        "groups": groups,
        "emphasized_path": deduped_path,
    }
    return trace_mod.redact_json(result)


def _node_summary(n):
    return {
        "id": n["id"], "run_id": n["run_id"], "node_type": n["node_type"],
        "swimlane": swimlane(n["node_type"]), "entity_type": n["entity_type"],
        "entity_id": n["entity_id"], "label": n["label"], "status": n["status"],
        "summary": n["summary"], "started_at": n["started_at"], "finished_at": n["finished_at"],
        "error": n["error"],
    }


def _path_to_root(edges, focus_node_id):
    """Node ids from the run's root down to `focus_node_id`, by walking
    inbound edges backwards (a node can have >1 parent in principle -- the
    first inbound edge found wins, which is all an "emphasized path" display
    needs)."""
    parent_of = {}
    for e in edges:
        parent_of.setdefault(e["to_node_id"], e["from_node_id"])
    path = [focus_node_id]
    seen = {focus_node_id}
    current = focus_node_id
    while current in parent_of:
        current = parent_of[current]
        if current in seen:   # defensive: a cycle should never exist, but never hang if one did
            break
        path.append(current)
        seen.add(current)
    return list(reversed(path))


def children(conn, node_id=None, group=None):
    if group:
        parent_id, relationship, child_type = group.split(":", 2)
        rows = _rows(conn, """
            SELECT n.* FROM trace_edges e JOIN trace_nodes n ON n.id = e.to_node_id
            WHERE e.from_node_id = ? AND e.relationship = ? AND n.node_type = ?
            ORDER BY e.ordinal ASC, n.id ASC
            LIMIT ?
        """, (int(parent_id), relationship, child_type, MAX_CHILDREN))
    elif node_id:
        rows = _rows(conn, """
            SELECT n.* FROM trace_edges e JOIN trace_nodes n ON n.id = e.to_node_id
            WHERE e.from_node_id = ?
            ORDER BY e.ordinal ASC, n.id ASC
            LIMIT ?
        """, (int(node_id), MAX_CHILDREN))
    else:
        raise ValueError("children() needs node_id or group")
    return trace_mod.redact_json([_node_summary(r) for r in rows])


# --- /api/node/<id> --------------------------------------------------------------

# Only three node types ever carry model calls (score-attempt, mission-execution
# and council), but the nodes a reader actually clicks are the ones telling the
# story: the candidate card with the item's title, the threshold card with the
# pass/fail verdict, the score-debug card holding the reasoning. Those had no
# calls of their own, so the prompt that produced them was simply absent from
# the panel -- the single most-asked-for thing in the whole UI.
#
# Each entry maps a node type to the neighbours worth borrowing calls from:
# (edge direction relative to this node, relationship or None for any,
# neighbour node_type). Kept to one hop, and to relationships that mean "this
# node was produced by that call", so a borrowed prompt is never a guess.
_CALL_NEIGHBORS = {
    "candidate": [("out", "scored", "score-attempt")],
    "threshold": [("in", None, "score-attempt")],       # cleared_threshold | rejected
    "score-debug": [("in", "generated", "score-attempt")],
    "mission": [("out", "executed", "mission-execution")],
    "raw-result": [("in", "returned", "mission-execution")],
    "raw-result-dropped": [("in", "returned", "mission-execution")],
    "council-context": [("out", "executed", "council")],
}


def _related_model_calls(conn, node, database):
    """Model calls belonging to an adjacent node that explains this one.

    Tagged with their provenance (`via_node_id`/`via_node_type`) so the UI can
    say "prompt from score-attempt #25686" rather than implying the call hung
    off the node the reader is looking at.
    """
    specs = _CALL_NEIGHBORS.get(node["node_type"])
    if not specs:
        return []
    out = []
    for direction, relationship, neighbor_type in specs:
        if direction == "out":
            sql = """
                SELECT n.id, n.node_type FROM trace_edges e JOIN trace_nodes n ON n.id = e.to_node_id
                WHERE e.from_node_id = ? AND n.node_type = ?
            """
        else:
            sql = """
                SELECT n.id, n.node_type FROM trace_edges e JOIN trace_nodes n ON n.id = e.from_node_id
                WHERE e.to_node_id = ? AND n.node_type = ?
            """
        params = [node["id"], neighbor_type]
        if relationship is not None:
            sql += " AND e.relationship = ?"
            params.append(relationship)
        sql += " ORDER BY n.id ASC LIMIT ?"
        params.append(MAX_RELATED_CALL_NODES)
        for neighbor in _rows(conn, sql, tuple(params)):
            calls = _rows(
                conn,
                "SELECT * FROM model_calls WHERE trace_node_id = ? ORDER BY id ASC",
                (neighbor["id"],),
            )
            for c in calls:
                detail = _model_call_detail(c, database)
                detail["via_node_id"] = neighbor["id"]
                detail["via_node_type"] = neighbor["node_type"]
                out.append(detail)
    return out


def node_detail(conn, node_id, database):
    node = _row(conn, "SELECT * FROM trace_nodes WHERE id = ?", (node_id,))
    if node is None:
        return None
    run = _row(conn, "SELECT * FROM trace_runs WHERE id = ?", (node["run_id"],))
    calls = _rows(
        conn, "SELECT * FROM model_calls WHERE trace_node_id = ? ORDER BY id ASC", (node_id,)
    )
    inbound = _rows(conn, """
        SELECT e.*, n.label AS from_label, n.node_type AS from_node_type
        FROM trace_edges e JOIN trace_nodes n ON n.id = e.from_node_id WHERE e.to_node_id = ?
    """, (node_id,))
    outbound = _rows(conn, """
        SELECT e.*, n.label AS to_label, n.node_type AS to_node_type
        FROM trace_edges e JOIN trace_nodes n ON n.id = e.to_node_id WHERE e.from_node_id = ?
    """, (node_id,))

    row_urls = {}
    if node["entity_type"] and node["entity_id"]:
        row_urls[f"{node['entity_type']}:{node['entity_id']}"] = _row_url(database, node["entity_type"], node["entity_id"])

    result = {
        "overview": {
            "id": node["id"], "run_id": node["run_id"], "node_type": node["node_type"],
            "swimlane": swimlane(node["node_type"]), "entity_type": node["entity_type"],
            "entity_id": node["entity_id"], "label": node["label"], "status": node["status"],
            "summary": node["summary"], "started_at": node["started_at"],
            "finished_at": node["finished_at"], "error": node["error"],
        },
        "input": _maybe_json(node["input_json"]),
        "output": _maybe_json(node["output_json"]),
        "exact_text": node["exact_text"],
        "model_calls": [_model_call_detail(c, database) for c in calls],
        # Only looked up when the node has no calls of its own, so the common
        # case costs nothing and no node ever shows a borrowed prompt next to
        # its own.
        "related_model_calls": [] if calls else _related_model_calls(conn, node, database),
        "entity_row": _entity_row(conn, node),
        # Shipped alongside a scores row so sub-scores can be shown with the
        # weighting that actually produced final_score. Read from the pipeline's
        # own module, so the UI can never drift from the real formula.
        "score_weights": dict(models.WEIGHTS) if node["entity_type"] == "scores" else None,
        "config": _maybe_json(run["config_json"]) if run else None,
        "run": {"id": run["id"], "kind": run["kind"], "status": run["status"]} if run else None,
        "inbound_edges": [
            {"from": e["from_node_id"], "relationship": e["relationship"],
             "from_label": e["from_label"], "from_node_type": e["from_node_type"]}
            for e in inbound
        ],
        "outbound_edges": [
            {"to": e["to_node_id"], "relationship": e["relationship"],
             "to_label": e["to_label"], "to_node_type": e["to_node_type"]}
            for e in outbound
        ],
        "row_urls": row_urls,
        "truncated": False,   # nothing in this module ever cuts a stored field -- see module docstring
    }
    return trace_mod.redact_json(result)


# The operational row behind a node was reachable only by leaving the app for
# raw Datasette: node_detail() knew entity_type/entity_id and built nothing but
# a link. So the six sub-scores and their weights, the item body the scorer
# actually read, dedup decisions, a delivery's retry count and a mission's
# rationale all existed one JOIN away and were shown nowhere.
#
# An explicit whitelist, never interpolation of entity_type into SQL -- the
# entity_type column is data, and data does not get to name tables.
_ENTITY_TABLES = frozenset({
    "scores", "candidate_items", "search_missions", "search_generations",
    "notifications", "interests",
})


def _entity_row(conn, node):
    entity_type, entity_id = node["entity_type"], node["entity_id"]
    if entity_type not in _ENTITY_TABLES or entity_id is None:
        return None
    row = _row(conn, f"SELECT * FROM {entity_type} WHERE id = ?", (entity_id,))  # noqa: S608 -- whitelisted above
    if row is None:
        return None
    data = dict(row)
    if entity_type == "candidate_items" and data.get("duplicate_of"):
        # "duplicate" nodes say only "same url"; naming the item it duplicated
        # is the part a reader actually wants.
        original = _row(
            conn, "SELECT id, title, url FROM candidate_items WHERE id = ?", (data["duplicate_of"],)
        )
        if original is not None:
            data["duplicate_of_item"] = dict(original)
    return data


def _model_call_detail(c, database):
    return {
        "id": c["id"], "call_role": c["call_role"], "attempt": c["attempt"],
        "provider": c["provider"], "model": c["model"],
        "exact_system_prompt": c["exact_system_prompt"], "exact_user_prompt": c["exact_user_prompt"],
        "exact_schema_json": _maybe_json(c["exact_schema_json"]),
        "exact_parameters_json": _maybe_json(c["exact_parameters_json"]),
        "raw_response_text": c["raw_response_text"],
        "parsed_response_json": _maybe_json(c["parsed_response_json"]),
        "validation_result": c["validation_result"],
        "usage": _maybe_json(c["usage_json"]),
        "provider_request_id": c["provider_request_id"],
        "started_at": c["started_at"], "finished_at": c["finished_at"], "error": c["error"],
        "row_url": _row_url(database, "model_calls", c["id"]),
    }


def _maybe_json(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _row_url(database, table, pk):
    return f"/{database}/{table}/{pk}"


# --- /api/interest/<key> ---------------------------------------------------------

def interest_detail(conn, key):
    interest = _row(conn, "SELECT * FROM interests WHERE key = ?", (key,))
    if interest is None:
        return None
    events = _rows(conn, "SELECT * FROM interest_events WHERE interest_key = ? ORDER BY id ASC", (key,))
    generations = _rows(
        conn, "SELECT * FROM search_generations WHERE interest_key = ? ORDER BY id DESC", (key,)
    )
    missions = _rows(
        conn, "SELECT * FROM search_missions WHERE interest_key = ? ORDER BY id DESC", (key,)
    )
    discoveries = _rows(conn, """
        SELECT s.*, ci.title, ci.url, ci.source
        FROM scores s JOIN candidate_items ci ON ci.id = s.item_id
        WHERE s.interest_id = ? ORDER BY s.id DESC
    """, (interest["id"],))
    feedback = _rows(conn, """
        SELECT f.*, ci.title, ci.url FROM feedback f JOIN candidate_items ci ON ci.id = f.item_id
        WHERE f.interest_id = ? ORDER BY f.id DESC
    """, (interest["id"],))
    # entity_id is only unique WITHIN one entity_type (a scores.id and a
    # search_missions.id routinely collide numerically) -- every branch
    # below pairs its IN-list with the matching entity_type so a candidate's
    # scoring_error can never be pulled in just because its item_id happens
    # to equal one of this interest's mission ids (repair 1: the original
    # query correlated on entity_id alone and did exactly that).
    failures = _rows(conn, f"""
        SELECT * FROM ({_FAILED_UNION})
        WHERE (entity_type = 'search_missions' AND entity_id IN (
                   SELECT CAST(id AS TEXT) FROM search_missions WHERE interest_key = :key))
           OR (entity_type = 'search_generations' AND entity_id IN (
                   SELECT CAST(id AS TEXT) FROM search_generations WHERE interest_key = :key))
           OR (entity_type = 'candidate_items' AND entity_id IN (
                   SELECT CAST(item_id AS TEXT) FROM item_interests WHERE interest_id = :interest_id))
           OR (entity_type = 'scores' AND entity_id IN (
                   SELECT CAST(id AS TEXT) FROM scores WHERE interest_id = :interest_id))
        ORDER BY at DESC
    """, {"key": key, "interest_id": interest["id"]})

    result = {
        "definition": interest,
        "provenance": _maybe_json(interest["provenance"]),
        "signals": {
            "positive": _maybe_json(interest["positive_signals"]),
            "negative": _maybe_json(interest["negative_signals"]),
        },
        "events": events,
        "generations": generations,
        "missions": missions,
        "discoveries": discoveries,
        "failures": failures,
        "feedback": feedback,
    }
    return trace_mod.redact_json(result)


# --- /api/compare ------------------------------------------------------------------

# The only two shapes compare() understands -- anything else must be
# rejected by the caller (plugin.py's compare_view checks this before ever
# calling in), not silently treated as 'run' (a `kind=generation` request,
# a shape the objective names, would otherwise diff two RUN ids that happen
# to equal those generation ids and return a plausible-looking wrong diff).
COMPARE_KINDS = frozenset({"run", "model_call"})


def compare(conn, kind, a, b):
    if kind not in COMPARE_KINDS:
        raise ValueError(f"unknown compare kind {kind!r}")
    if kind == "model_call":
        return trace_mod.redact_json(_compare_model_calls(conn, int(a), int(b)))
    return trace_mod.redact_json(_compare_runs(conn, int(a), int(b)))


def _compare_model_calls(conn, a_id, b_id):
    a = _row(conn, "SELECT * FROM model_calls WHERE id = ?", (a_id,))
    b = _row(conn, "SELECT * FROM model_calls WHERE id = ?", (b_id,))
    # A nonexistent id must 404, not silently diff against "" -- that would
    # otherwise render as a wrong-but-plausible "everything was removed"
    # answer for what's actually a typo'd id (the same class of bug the
    # COMPARE_KINDS whitelist above was added to prevent).
    if a is None or b is None:
        missing = [i for i, row in ((a_id, a), (b_id, b)) if row is None]
        raise LookupError(f"model_call id(s) not found: {missing}")
    return {
        "kind": "model_call", "a_id": a_id, "b_id": b_id,
        "prompt_diff": _line_diff(a["exact_user_prompt"] or "", b["exact_user_prompt"] or ""),
        "response_diff": _line_diff(a["raw_response_text"] or "", b["raw_response_text"] or ""),
    }


# --- /api/prompt-template --------------------------------------------------------

def prompt_template(conn, call_id):
    """The prompt template a stored call was built from, plus a diff.

    "What exactly did we ask the model, and is that still what we ask today?"
    is the question the Prompt tab exists to answer, and the second half of it
    can't be answered from the trace alone: model_calls stores the rendered
    prompt, never the template it came from.

    scores.prompt_hash is a fingerprint of SYSTEM+PROMPT at scoring time, so it
    tells us whether the template has moved since. When it has, the diff would
    be a mix of "the template changed" and "this item's substitutions" with no
    way to tell them apart, so we report the mismatch and skip the diff rather
    than render something wrong-but-plausible. The old template text is not
    stored anywhere, so reconstructing it is not on the table.
    """
    call = _row(conn, "SELECT * FROM model_calls WHERE id = ?", (call_id,))
    if call is None:
        raise LookupError(f"model_call id not found: {call_id}")
    if call["call_role"] != "scoring":
        # council/mission_search prompts are assembled from per-run context
        # rather than one module-level template -- there is nothing stable to
        # diff against, so say so instead of guessing.
        return {
            "call_id": call_id, "role": call["call_role"], "available": False,
            "reason": f"no stable template is recorded for {call['call_role']!r} calls",
        }

    from discovery import scoring

    template = f"{scoring.SYSTEM}\n\n{scoring.PROMPT}"
    fingerprint = scoring.prompt_fingerprint()
    stored_hash = _stored_prompt_hash(conn, call["trace_node_id"])
    matches = stored_hash is not None and stored_hash == fingerprint

    result = {
        "call_id": call_id, "role": "scoring", "available": True,
        "fingerprint": fingerprint, "stored_prompt_hash": stored_hash,
        "matches_current": matches, "template": template,
        "diff": _line_diff(template, call["exact_user_prompt"] or "") if matches else [],
    }
    if not matches:
        result["reason"] = (
            "template has changed since this score was written "
            f"(stored {stored_hash or 'none'}, current {fingerprint})"
        )
    return trace_mod.redact_json(result)


def _stored_prompt_hash(conn, trace_node_id):
    """scores.prompt_hash for the score a scoring call produced.

    A score-attempt node's entity is the candidate_item (verified live: all
    1,305 of them), not the score row -- the scores row is reachable through
    that item id.
    """
    if trace_node_id is None:
        return None
    node = _row(conn, "SELECT entity_type, entity_id FROM trace_nodes WHERE id = ?", (trace_node_id,))
    if node is None or node["entity_type"] != "candidate_items" or node["entity_id"] is None:
        return None
    row = _row(
        conn,
        "SELECT prompt_hash FROM scores WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (node["entity_id"],),
    )
    return row["prompt_hash"] if row else None


def _line_diff(a_text, b_text):
    return list(difflib.unified_diff(
        a_text.splitlines(), b_text.splitlines(), lineterm="", fromfile="a", tofile="b",
    ))


def _node_key_map(conn, run_id, nodes_list):
    """id -> (node_type, label, inbound_relationship, inbound_ordinal).
    Keying nodes by (node_type, label) alone collapses same-labelled
    siblings (e.g. two runs each with several equally-labelled 'raw-result'
    nodes) into one dict entry, silently dropping the extras from
    added/removed/changed -- the inbound edge's own relationship+ordinal is
    what already disambiguates edges elsewhere, so reuse it here too, per
    the objective's 'keyed by label/relationship/ordinal'. A node with no
    inbound edge (a run root) keys on (None, None) -- still unique in
    practice, since a run has exactly one root."""
    ids = [n["id"] for n in nodes_list]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    inbound_edges = _rows(
        conn,
        f"SELECT * FROM trace_edges WHERE to_node_id IN ({placeholders}) ORDER BY ordinal ASC, from_node_id ASC",
        ids,
    )
    inbound = {}
    for e in inbound_edges:
        inbound.setdefault(e["to_node_id"], (e["relationship"], e["ordinal"]))
    return {
        n["id"]: (n["node_type"], n["label"], *inbound.get(n["id"], (None, None)))
        for n in nodes_list
    }


def _compare_runs(conn, a_run_id, b_run_id):
    a_list = _rows(conn, "SELECT * FROM trace_nodes WHERE run_id = ?", (a_run_id,))
    b_list = _rows(conn, "SELECT * FROM trace_nodes WHERE run_id = ?", (b_run_id,))
    a_key_of = _node_key_map(conn, a_run_id, a_list)
    b_key_of = _node_key_map(conn, b_run_id, b_list)
    a_nodes = {a_key_of[n["id"]]: n for n in a_list}
    b_nodes = {b_key_of[n["id"]]: n for n in b_list}
    added_keys = [k for k in b_nodes if k not in a_nodes]
    removed_keys = [k for k in a_nodes if k not in b_nodes]
    changed = []
    for k in a_nodes.keys() & b_nodes.keys():
        an, bn = a_nodes[k], b_nodes[k]
        if an["status"] != bn["status"] or (an["output_json"] or "") != (bn["output_json"] or ""):
            changed.append({"key": list(k), "a": _node_summary(an), "b": _node_summary(bn)})

    def edge_key_set(run_id, key_of):
        edges = _rows(conn, """
            SELECT e.* FROM trace_edges e JOIN trace_nodes n ON n.id = e.from_node_id WHERE n.run_id = ?
        """, (run_id,))
        keyed = {}
        for e in edges:
            from_key = key_of.get(e["from_node_id"])
            to_key = key_of.get(e["to_node_id"])
            if from_key is None or to_key is None:
                continue
            keyed[(from_key, to_key, e["relationship"], e["ordinal"])] = e
        return keyed

    a_edges = edge_key_set(a_run_id, a_key_of)
    b_edges = edge_key_set(b_run_id, b_key_of)
    edges_added = [list(k) for k in b_edges if k not in a_edges]
    edges_removed = [list(k) for k in a_edges if k not in b_edges]

    context_diff = _compare_output(_find_by_type(a_nodes, "council-context"), _find_by_type(b_nodes, "council-context"))
    mission_a = sorted(k[1] for k in a_nodes if k[0] == "mission")
    mission_b = sorted(k[1] for k in b_nodes if k[0] == "mission")
    score_a = sorted(k[1] for k in a_nodes if k[0] == "threshold")
    score_b = sorted(k[1] for k in b_nodes if k[0] == "threshold")
    delivery_a = sorted((n["status"], n["label"]) for k, n in a_nodes.items() if k[0] == "notification")
    delivery_b = sorted((n["status"], n["label"]) for k, n in b_nodes.items() if k[0] == "notification")

    return {
        "kind": "run", "a_run_id": a_run_id, "b_run_id": b_run_id,
        "nodes": {"added": [list(k) for k in added_keys], "removed": [list(k) for k in removed_keys], "changed": changed},
        "edges": {"added": edges_added, "removed": edges_removed},
        "context_diff": context_diff,
        "mission_branches_diff": {"added": sorted(set(mission_b) - set(mission_a)), "removed": sorted(set(mission_a) - set(mission_b))},
        "score_decisions_diff": {"added": sorted(set(score_b) - set(score_a)), "removed": sorted(set(score_a) - set(score_b))},
        "delivery_outcome_diff": {"added": [list(x) for x in set(delivery_b) - set(delivery_a)], "removed": [list(x) for x in set(delivery_a) - set(delivery_b)]},
    }


def _find_by_type(nodes_dict, node_type):
    for k, v in nodes_dict.items():
        if k[0] == node_type:
            return v
    return None


def _compare_output(a_node, b_node):
    a_text = (a_node or {}).get("output_json") or ""
    b_text = (b_node or {}).get("output_json") or ""
    return _line_diff(a_text, b_text)


# --- trace/score/<id> deep link ---------------------------------------------------

def score_trace_target(conn, score_id):
    """Resolves a score id to its trace: the 'threshold' node (the one
    entity-linked to entity_type='scores', see PROJECT_STATE.md) plus its
    run -- what the trace/score/<id> route needs to bootstrap the graph view
    focused on the right node."""
    node = _row(conn, """
        SELECT * FROM trace_nodes WHERE entity_type = 'scores' AND entity_id = ? AND node_type = 'threshold'
        ORDER BY id DESC LIMIT 1
    """, (str(score_id),))
    if node is None:
        return None
    return {"node_id": node["id"], "run_id": node["run_id"], "score_id": score_id}
