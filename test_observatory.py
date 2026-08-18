#!/usr/bin/env python3
"""Offline tests for observatory/ (step-13 task 2): the Datasette plugin
serving the trace backbone (step-13 task 1) as JSON APIs + a placeholder
shell page.

Kept as its own top-level test file, NOT appended to test_discovery.py,
because it needs datasette -- the one sanctioned new dependency this step
adds (see PROJECT_STATE.md/README's "Observatory" section). If datasette
genuinely isn't installed, every test here is skipped with a loud message
(both a stderr print, so `python test_observatory.py` piped through a
summarizer still shows it, and unittest's own per-test skip reason) rather
than failing -- `pip install datasette` and rerun.

Every test runs fully offline via Datasette's own ASGI test client
(`Datasette(...).client`, an httpx client wired straight to the ASGI app
with `httpx.ASGITransport` -- no socket, no network) against a fixture db
built by the REAL discovery/trace_fixture.py (step-13 task 1) driving the
real web_tick/send_digest/feedback_listener code paths with fake providers,
same as test_discovery.py's own TraceFixtureTests.
"""
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

try:
    import datasette  # noqa: F401
    HAVE_DATASETTE = True
except ImportError:
    HAVE_DATASETTE = False

if not HAVE_DATASETTE:
    print(
        "test_observatory.py: datasette is not installed -- every test in this "
        "file will be SKIPPED. `pip install datasette` to actually run them "
        "(see requirements.txt).",
        file=sys.stderr,
    )

from discovery import config, db, models, trace, trace_fixture

if HAVE_DATASETTE:
    from observatory import db as odb
    from observatory.app import build_datasette

# All tables schema.sql defines -- the read-only proof snapshots every one
# of these, and the native-table-pages test expects a Datasette row/table
# view for every one of these too.
ALL_TABLES = (
    "interests", "interest_events", "candidate_items", "item_interests",
    "scores", "notifications", "feedback", "metrics", "llm_usage",
    "service_state", "search_generations", "search_missions",
    "trace_runs", "trace_nodes", "trace_edges", "model_calls",
)


def _fixture_cfg(db_path, **overrides):
    cfg = config.load()
    cfg.db_path = db_path
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _build_fixture_db(**cfg_overrides):
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "fixture.db")
    cfg = _fixture_cfg(db_path, **cfg_overrides)
    conn = db.connect(db_path)
    db.init(conn)
    trace_fixture.build(conn, cfg)
    conn.close()
    return db_path, cfg


def _table_counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ALL_TABLES}
    finally:
        conn.close()


def _db_name(db_path):
    return os.path.splitext(os.path.basename(db_path))[0]


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryListTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()
        cls.ds = build_datasette(cls.cfg, public=False)

    async def _get(self, path):
        return await self.ds.client.get(path)

    async def test_default_list_returns_every_discovery_paginated(self):
        r = await self._get("/observatory/api/list?tab=discoveries")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["tab"], "discoveries")
        self.assertLessEqual(body["limit"], 50)
        self.assertEqual(body["total"], 4)   # duplicate(first), short(prefiltered), below-threshold, breakthrough
        self.assertEqual(len(body["rows"]), 4)

    async def test_limit_and_offset_paginate(self):
        r1 = await self._get("/observatory/api/list?tab=discoveries&limit=1&offset=0")
        r2 = await self._get("/observatory/api/list?tab=discoveries&limit=1&offset=1")
        b1, b2 = r1.json(), r2.json()
        self.assertEqual(len(b1["rows"]), 1)
        self.assertEqual(len(b2["rows"]), 1)
        self.assertEqual(b1["total"], 4)
        self.assertNotEqual(b1["rows"][0]["item_id"], b2["rows"][0]["item_id"])

    async def test_limit_is_capped_at_fifty(self):
        r = await self._get("/observatory/api/list?tab=discoveries&limit=999")
        self.assertLessEqual(r.json()["limit"], 50)

    async def test_negative_limit_does_not_bypass_pagination(self):
        # SQLite treats a negative LIMIT as "no upper bound" -- a naive
        # `min(limit, MAX_LIMIT)` with no floor lets `limit=-1` through
        # untouched and returns the entire result set.
        r = await self._get("/observatory/api/list?tab=discoveries&limit=-1")
        body = r.json()
        self.assertGreaterEqual(body["limit"], 1)
        self.assertLessEqual(body["limit"], 50)
        self.assertLessEqual(len(body["rows"]), body["limit"])

    async def test_unknown_tab_is_a_400(self):
        r = await self._get("/observatory/api/list?tab=nonsense")
        self.assertEqual(r.status_code, 400)

    async def test_filter_by_interest_key(self):
        # 'interest' filters on the SCORE's chosen interest -- the 'Short'
        # item never reaches scoring (prefiltered), so it's correctly
        # excluded here, same as the provider/model filters below.
        r = await self._get("/observatory/api/list?tab=discoveries&interest=fixture-interest")
        self.assertEqual(r.json()["total"], 3)
        r_none = await self._get("/observatory/api/list?tab=discoveries&interest=no-such-interest")
        self.assertEqual(r_none.json()["total"], 0)

    async def test_filter_by_layer(self):
        r = await self._get("/observatory/api/list?tab=discoveries&layer=owner")
        self.assertGreaterEqual(r.json()["total"], 1)
        r_none = await self._get("/observatory/api/list?tab=discoveries&layer=inferred")
        self.assertEqual(r_none.json()["total"], 0)

    async def test_filter_by_source(self):
        r = await self._get("/observatory/api/list?tab=discoveries&source=web_search")
        self.assertEqual(r.json()["total"], 4)
        r_none = await self._get("/observatory/api/list?tab=discoveries&source=youtube")
        self.assertEqual(r_none.json()["total"], 0)

    async def test_filter_by_provider_and_model_excludes_unscored_items(self):
        # The 'Short' item never reaches scoring (prefiltered on text length) --
        # provider/model are only set on scores, so filtering on either must
        # drop it, unlike the unfiltered total of 4.
        r = await self._get("/observatory/api/list?tab=discoveries&provider=fixture_scoring")
        self.assertEqual(r.json()["total"], 3)
        r2 = await self._get("/observatory/api/list?tab=discoveries&model=fixture-scoring-1")
        self.assertEqual(r2.json()["total"], 3)

    async def test_filter_by_mission_label(self):
        r = await self._get("/observatory/api/list?tab=discoveries&mission=recent-coverage")
        self.assertEqual(r.json()["total"], 2)   # below-threshold + breakthrough
        r2 = await self._get("/observatory/api/list?tab=discoveries&mission=primary-sources")
        self.assertEqual(r2.json()["total"], 1)   # the surviving duplicate occurrence

    async def test_filter_by_sent(self):
        sent = await self._get("/observatory/api/list?tab=discoveries&sent=yes")
        self.assertEqual(sent.json()["total"], 1)
        self.assertEqual(sent.json()["rows"][0]["title"], "Fixture Breakthrough Finding")
        not_sent = await self._get("/observatory/api/list?tab=discoveries&sent=no")
        self.assertEqual(not_sent.json()["total"], 3)

    async def test_filter_by_feedback_verdict(self):
        fired = await self._get("/observatory/api/list?tab=discoveries&feedback=fire")
        self.assertEqual(fired.json()["total"], 1)
        none_yet = await self._get("/observatory/api/list?tab=discoveries&feedback=none")
        self.assertEqual(none_yet.json()["total"], 3)

    async def test_filter_by_date_range(self):
        future = await self._get("/observatory/api/list?tab=discoveries&date_from=2999-01-01")
        self.assertEqual(future.json()["total"], 0)
        broad = await self._get("/observatory/api/list?tab=discoveries&date_from=2000-01-01")
        self.assertEqual(broad.json()["total"], 4)

    async def test_filter_by_failure_stage(self):
        below = await self._get("/observatory/api/list?tab=discoveries&failure_stage=below_threshold")
        self.assertEqual(below.json()["total"], 2)   # duplicate-survivor (0.5) + below-threshold (0.15)
        prefilter = await self._get("/observatory/api/list?tab=discoveries&failure_stage=prefilter")
        self.assertEqual(prefilter.json()["total"], 1)   # 'Short'
        errors = await self._get("/observatory/api/list?tab=discoveries&failure_stage=scoring_error")
        self.assertEqual(errors.json()["total"], 0)   # none in this fixture -- must not crash

    async def test_filter_by_trace_completeness(self):
        complete = await self._get("/observatory/api/list?tab=discoveries&trace_complete=yes")
        self.assertEqual(complete.json()["total"], 4)
        incomplete = await self._get("/observatory/api/list?tab=discoveries&trace_complete=no")
        self.assertEqual(incomplete.json()["total"], 0)

    async def test_search_matches_title(self):
        # The exact title, not just the word "breakthrough" -- that word
        # alone is also one of the fixture interest's own positive_signals,
        # which appears in every scoring prompt, so a bare-word search would
        # (correctly) also match via the model-output search path exercised
        # by test_search_matches_model_output_not_present_in_item_fields.
        r = await self._get("/observatory/api/list?tab=discoveries&search=Fixture+Breakthrough+Finding")
        self.assertGreaterEqual(r.json()["total"], 1)
        titles = {row["title"] for row in r.json()["rows"]}
        self.assertIn("Fixture Breakthrough Finding", titles)

    async def test_search_matches_model_output_not_present_in_item_fields(self):
        # "RESEARCH MISSION" only ever appears inside missions.RESEARCH_FRAMING,
        # the literal text framed into every mission's search_json prompt -- it
        # is not substring of any item's title/url/text/score.reason. A match
        # here can only come from the model_calls EXISTS subquery, proving
        # search really does span prompts, not just the item's own columns.
        r = await self._get("/observatory/api/list?tab=discoveries&search=RESEARCH+MISSION")
        self.assertGreater(r.json()["total"], 0)
        for row in r.json()["rows"]:
            self.assertNotIn("RESEARCH MISSION", row["title"])

    async def test_search_underscore_is_literal_not_a_wildcard(self):
        # A bare "_"/"%" is a SQLite LIKE wildcard and, unescaped, would
        # match virtually every row (a single search term standing in for
        # "any character"/"anything"). A nonsense term built around one
        # must behave like an ordinary (non-matching) substring search, not
        # match the whole fixture -- proves observatory.db._like()'s escaping
        # end-to-end, not just as a unit test of the helper in isolation.
        r = await self._get("/observatory/api/list?tab=discoveries&search=zz_yy%25xx")
        self.assertEqual(r.json()["total"], 0)

    async def test_interests_tab(self):
        r = await self._get("/observatory/api/list?tab=interests")
        self.assertEqual(r.status_code, 200)
        rows = r.json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "fixture-interest")
        self.assertEqual(rows[0]["missions_count"], 3)
        self.assertEqual(rows[0]["discoveries_count"], 3)

    async def test_generations_tab(self):
        r = await self._get("/observatory/api/list?tab=generations")
        rows = r.json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "DONE")
        self.assertEqual(rows[0]["missions_returned"], 3)

    async def test_missions_tab(self):
        r = await self._get("/observatory/api/list?tab=missions")
        rows = r.json()["rows"]
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["status"] for row in rows}, {"DONE"})

    async def test_failed_tab_covers_every_present_failure_kind(self):
        dup = await self._get("/observatory/api/list?tab=failed&failure_stage=duplicate")
        self.assertGreaterEqual(dup.json()["total"], 1)
        below = await self._get("/observatory/api/list?tab=failed&failure_stage=below_threshold")
        self.assertGreaterEqual(below.json()["total"], 1)
        prefilter = await self._get("/observatory/api/list?tab=failed&failure_stage=prefilter_rejected")
        self.assertGreaterEqual(prefilter.json()["total"], 1)
        # Not present in this fixture -- must return an empty, not-erroring result.
        for stage in ("scoring_error", "mission_failed", "generation_failed"):
            r = await self._get(f"/observatory/api/list?tab=failed&failure_stage={stage}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["total"], 0)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryDiscoveriesDuplicateCandidateNodeTests(unittest.IsolatedAsyncioTestCase):
    """Repair regression: __main__.py's `ingest(..., force=args.force)` skips
    dedup, so a re-ingest of an already-stored item writes a SECOND
    `candidate` trace_nodes row against the same item id. `_discoveries_query`
    used to LEFT JOIN that row (unconstrained to one), fanning the item out
    into two rows and inflating `total` to match -- since `total` drives the
    mandatory pagination, both the row count and the page arithmetic went
    wrong. This must not happen: one item is one row, however many
    `candidate` trace nodes it has."""

    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()

        conn = sqlite3.connect(cls.db_path)
        try:
            item_id, run_id = conn.execute(
                "SELECT entity_id, run_id FROM trace_nodes WHERE node_type = 'candidate' LIMIT 1"
            ).fetchone()
            # A second, later 'candidate' node for the SAME item -- exactly
            # what a force re-ingest produces (dedup skipped, entity unchanged).
            conn.execute(
                "INSERT INTO trace_nodes (run_id, node_type, entity_type, entity_id, label, "
                "status, summary, started_at) VALUES (?, 'candidate', 'candidate_items', ?, "
                "'reingested', 'ok', '', datetime('now'))",
                (run_id, item_id),
            )
            conn.commit()
        finally:
            conn.close()

        cls.ds = build_datasette(cls.cfg, public=False)

    async def test_total_and_row_count_are_not_inflated_by_a_second_candidate_node(self):
        r = await self.ds.client.get("/observatory/api/list?tab=discoveries")
        body = r.json()
        # Same total the un-duplicated fixture asserts in ObservatoryListTests
        # -- the extra trace_nodes row must not surface as a phantom item.
        self.assertEqual(body["total"], 4)
        self.assertEqual(len(body["rows"]), 4)
        item_ids = [row["item_id"] for row in body["rows"]]
        self.assertEqual(len(item_ids), len(set(item_ids)), "an item appeared more than once")


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryFailedTabMissionFanoutTests(unittest.IsolatedAsyncioTestCase):
    """Repair regression: missions._execute_mission writes one
    'mission-execution' trace node PER ATTEMPT, and a mission only reaches
    status='FAILED' after mission_max_attempts (default 3) attempts.
    _FAILED_UNION's mission_failed branch LEFT JOINed onto that node type
    with no one-row constraint, fanning a single FAILED mission out into N
    identical rows -- same class of bug repair 5 fixed for the discoveries
    tab's candidate node, left unfixed here."""

    @classmethod
    def setUpClass(cls):
        tmp_dir = tempfile.mkdtemp()
        cls.db_path = os.path.join(tmp_dir, "mission-fanout.db")
        cls.cfg = _fixture_cfg(cls.db_path)
        conn = db.connect(cls.db_path)
        db.init(conn)

        conn.execute(
            "INSERT INTO search_missions (id, interest_key, label, prompt, prompt_sha256, "
            "status, attempts, created_at, finished_at, last_error) VALUES "
            "(1, 'fixture-interest', 'm1', 'prompt', 'sha', 'FAILED', 3, datetime('now'), "
            "datetime('now'), 'boom')"
        )
        run_id = conn.execute(
            "INSERT INTO trace_runs (kind, status, started_at, config_json) VALUES "
            "('web-tick', 'done', datetime('now'), '{}')"
        ).lastrowid
        # One mission-execution node per attempt -- exactly what
        # missions._execute_mission writes on every retry.
        for attempt in range(3):
            conn.execute(
                "INSERT INTO trace_nodes (run_id, node_type, entity_type, entity_id, label, "
                "status, summary, started_at) VALUES (?, 'mission-execution', 'search_missions', "
                "'1', 'm1', 'error', ?, datetime('now'))",
                (run_id, f"attempt {attempt}"),
            )
        conn.commit()
        conn.close()
        cls.ds = build_datasette(cls.cfg, public=False)

    async def test_failed_mission_appears_once_not_once_per_attempt(self):
        r = await self.ds.client.get("/observatory/api/list?tab=failed&failure_stage=mission_failed")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["rows"]), 1)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryGraphTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()
        cls.ds = build_datasette(cls.cfg, public=False)
        cls.raw = sqlite3.connect(cls.db_path)
        cls.raw.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.raw.close()

    def _breakthrough_item_id(self):
        row = self.raw.execute(
            "SELECT id FROM candidate_items WHERE title = 'Fixture Breakthrough Finding'"
        ).fetchone()
        return row["id"]

    async def test_graph_for_the_successful_discovery_contains_every_branch(self):
        item_id = self._breakthrough_item_id()
        r = await self.ds.client.get(f"/observatory/api/graph?entity_type=candidate_items&entity_id={item_id}")
        self.assertEqual(r.status_code, 200)
        g = r.json()
        node_types = {n["node_type"] for n in g["nodes"]}
        labels = {n["label"] for n in g["nodes"]}

        # duplicate branch
        self.assertIn("duplicate", node_types)
        # prefilter rejection branch ('Short' never got far enough to be scored)
        self.assertIn("Short", labels)
        # failed-then-retried scoring call: one score-attempt node, two model_calls
        self.assertIn("Fixture Breakthrough Finding", labels)
        # below-threshold branch: at least one 'rejected' edge into a threshold node
        threshold_ids = {n["id"] for n in g["nodes"] if n["node_type"] == "threshold"}
        rejected_thresholds = [e for e in g["edges"] if e["relationship"] == "rejected" and e["to"] in threshold_ids]
        self.assertGreaterEqual(len(rejected_thresholds), 1)
        # sent + feedback branch
        self.assertIn("notification", node_types)
        self.assertIn("feedback", node_types)
        sent_edges = [e for e in g["edges"] if e["relationship"] == "sent"]
        self.assertGreaterEqual(len(sent_edges), 1)
        feedback_edges = [e for e in g["edges"] if e["relationship"] == "feedback_on"]
        self.assertGreaterEqual(len(feedback_edges), 1)
        # spans more than one trace_run (web-tick, digest, feedback)
        self.assertGreaterEqual(len(g["run_ids"]), 2)

    async def test_swimlane_is_set_per_node(self):
        item_id = self._breakthrough_item_id()
        r = await self.ds.client.get(f"/observatory/api/graph?entity_type=candidate_items&entity_id={item_id}")
        g = r.json()
        lanes = {n["node_type"]: n["swimlane"] for n in g["nodes"]}
        self.assertEqual(lanes.get("interest-state"), "interest-state")
        self.assertEqual(lanes.get("council"), "council")
        self.assertEqual(lanes.get("mission"), "mission")
        self.assertEqual(lanes.get("candidate"), "candidate-pipeline")
        self.assertEqual(lanes.get("threshold"), "scoring")
        self.assertEqual(lanes.get("notification"), "delivery-feedback")

    async def test_large_sibling_sets_are_collapsed_with_no_child_payload(self):
        item_id = self._breakthrough_item_id()
        r = await self.ds.client.get(f"/observatory/api/graph?entity_type=candidate_items&entity_id={item_id}")
        g = r.json()
        self.assertTrue(g["groups"], "expected at least one collapsed group (5 advisors, 5 peer-reviewers)")
        collapsed_types = {grp["child_node_type"] for grp in g["groups"]}
        self.assertIn("advisor", collapsed_types)
        self.assertIn("peer-review", collapsed_types)
        for grp in g["groups"]:
            self.assertGreater(grp["child_count"], odb.COLLAPSE_THRESHOLD)
        # None of the collapsed children's own node payloads leaked into `nodes`,
        # and no edge in the response points at one of them either.
        node_ids = {n["id"] for n in g["nodes"]}
        edge_endpoints = {e["from"] for e in g["edges"]} | {e["to"] for e in g["edges"]}
        for grp in g["groups"]:
            hidden = {
                row["id"] for row in self.raw.execute(
                    """
                    SELECT n.id AS id FROM trace_edges e JOIN trace_nodes n ON n.id = e.to_node_id
                    WHERE e.from_node_id = ? AND e.relationship = ? AND n.node_type = ?
                    """,
                    (grp["parent_node_id"], grp["relationship"], grp["child_node_type"]),
                ).fetchall()
            }
            self.assertTrue(hidden, "expected the raw query to find the collapsed children")
            self.assertFalse(hidden & node_ids, "a collapsed child's node payload leaked into `nodes`")
            self.assertFalse(hidden & edge_endpoints, "a collapsed child leaked into `edges`")
        # The three mission nodes (only 3, at the collapse threshold) must stay
        # individually visible -- proving the fix that groups by (parent,
        # relationship, child node_type), not just (parent, relationship).
        mission_labels = {n["label"] for n in g["nodes"] if n["node_type"] == "mission"}
        self.assertEqual(mission_labels, {"primary-sources", "cross-references", "recent-coverage"})

    async def test_children_lazy_loads_a_collapsed_group_with_no_earlier_payload(self):
        item_id = self._breakthrough_item_id()
        r = await self.ds.client.get(f"/observatory/api/graph?entity_type=candidate_items&entity_id={item_id}")
        g = r.json()
        advisor_group = next(grp for grp in g["groups"] if grp["child_node_type"] == "advisor")
        graph_node_ids = {n["id"] for n in g["nodes"]}

        r2 = await self.ds.client.get(f"/observatory/api/children?group={advisor_group['group']}")
        self.assertEqual(r2.status_code, 200)
        children = r2.json()["children"]
        self.assertEqual(len(children), advisor_group["child_count"])
        for child in children:
            self.assertEqual(child["node_type"], "advisor")
            self.assertNotIn(child["id"], graph_node_ids, "collapsed child leaked into the initial graph payload")

    async def test_run_id_query_reaches_the_same_connected_component(self):
        # run_id=1 is the web-tick run; the connected graph must still reach
        # forward into the later digest/feedback runs, not stop at run 1's
        # own nodes -- 'rendered'/'sent'/'feedback_on' edges legitimately
        # cross trace_runs.
        r = await self.ds.client.get("/observatory/api/graph?run_id=1")
        g = r.json()
        node_types = {n["node_type"] for n in g["nodes"]}
        self.assertIn("notification", node_types)
        self.assertIn("feedback", node_types)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryGraphCollapseConnectivityTests(unittest.IsolatedAsyncioTestCase):
    """Repair regression: trace_fixture.build()'s only sibling sets above
    COLLAPSE_THRESHOLD (5 advisors, 5 peer-reviewers) have zero descendants
    of their own, so the standard fixture can never exercise what happens
    when a COLLAPSED sibling has children -- exactly the shape
    missions._execute_mission produces in production (one mission-execution
    with `cfg.mission_max_results` 'raw-result' siblings, each with its own
    normalized_to/scored/cleared_threshold chain). Built directly via the
    real Tracer API, same primitives missions.py/pipeline.py use, so this
    doesn't depend on trace_fixture ever growing a wide-with-descendants
    branch of its own."""

    @classmethod
    def setUpClass(cls):
        tmp_dir = tempfile.mkdtemp()
        cls.db_path = os.path.join(tmp_dir, "collapse.db")
        cls.cfg = _fixture_cfg(cls.db_path)
        cls.conn = db.connect(cls.db_path)
        db.init(cls.conn)
        cls.ds = build_datasette(cls.cfg, public=False)

        tracer = trace.Tracer(cls.conn, cls.cfg)
        run = tracer.begin_run("collapse-test")
        gen = tracer.node(run, "generation", label="gen")
        mission = tracer.node(run, "mission", label="m1")
        tracer.edge(gen, mission, "generated")
        mx = tracer.node(run, "mission-execution", label="m1-exec")
        tracer.edge(mission, mx, "executed")
        for i in range(6):  # > COLLAPSE_THRESHOLD, matches cfg.mission_max_results default
            raw = tracer.node(run, "raw-result", label=f"r{i}")
            tracer.edge(mx, raw, "returned", ordinal=i)
            cand = tracer.node(
                run, "candidate", label=f"item{i}",
                entity_type="candidate_items", entity_id=str(i + 1),
            )
            tracer.edge(raw, cand, "normalized_to")
            sa = tracer.node(
                run, "score-attempt", label=f"item{i}",
                entity_type="candidate_items", entity_id=str(i + 1),
            )
            tracer.edge(cand, sa, "scored")
            th = tracer.node(
                run, "threshold", label=f"item{i}",
                entity_type="scores", entity_id=str(i + 1),
            )
            tracer.edge(sa, th, "cleared_threshold")
        tracer.finish_run(run, status="done")
        cls.conn.commit()
        cls.run_id = run

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _assert_connected(self, g):
        # The invariant a fix for this bug must guarantee: every node the
        # emphasized path names is actually present, and every node besides
        # the graph's genuine root has at least one inbound edge -- no node
        # is left floating with an untraceable parent.
        node_ids = {n["id"] for n in g["nodes"]}
        for nid in g["emphasized_path"]:
            self.assertIn(nid, node_ids, "an emphasized-path id is missing from `nodes`")
        inbound = {e["to"] for e in g["edges"]}
        roots = [n for n in g["nodes"] if n["id"] not in inbound]
        self.assertTrue(roots, "expected at least one root")
        for n in roots:
            self.assertEqual(n["node_type"], "generation", f"floating node with no inbound edge: {n}")

    async def test_focus_inside_a_collapsible_branch_stays_fully_expanded(self):
        # The selected entity lives inside the 6-wide raw-result sibling set
        # -- its own branch must never collapse away underneath it.
        r = await self.ds.client.get("/observatory/api/graph?entity_type=candidate_items&entity_id=1")
        self.assertEqual(r.status_code, 200)
        g = r.json()
        self._assert_connected(g)
        self.assertEqual(g["groups"], [])
        node_types = [n["node_type"] for n in g["nodes"]]
        self.assertEqual(node_types.count("raw-result"), 6)
        self.assertEqual(node_types.count("candidate"), 6)

    async def test_unfocused_graph_collapses_the_branch_without_disconnecting_it(self):
        # No focus node inside this branch -- the raw-result siblings DO
        # collapse, but their whole subtree (not just the 6 leaves) must be
        # hidden, and the group must stand in as a real, reachable node so
        # the rest of the graph never gets cut loose from it.
        r = await self.ds.client.get(f"/observatory/api/graph?run_id={self.run_id}")
        self.assertEqual(r.status_code, 200)
        g = r.json()
        self._assert_connected(g)
        self.assertEqual(len(g["groups"]), 1)
        group = g["groups"][0]
        self.assertEqual(group["child_node_type"], "raw-result")
        self.assertEqual(group["child_count"], 6)

        group_node = next(n for n in g["nodes"] if n["id"] == group["group"])
        self.assertEqual(group_node["node_type"], "group")
        self.assertEqual(group_node["child_count"], 6)
        inbound_to_group = [e for e in g["edges"] if e["to"] == group_node["id"]]
        self.assertEqual(len(inbound_to_group), 1, "the group must be reachable from its parent")

        hidden_labels = {f"r{i}" for i in range(6)} | {f"item{i}" for i in range(6)}
        self.assertFalse(hidden_labels & {n["label"] for n in g["nodes"]})
        node_types = {n["node_type"] for n in g["nodes"]}
        self.assertNotIn("raw-result", node_types)
        self.assertNotIn("candidate", node_types)
        self.assertNotIn("threshold", node_types)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryGraphNestedCollapseTests(unittest.IsolatedAsyncioTestCase):
    """Repair regression: a collapsed sibling set whose OWN subtree contains
    another collapsible sibling set (pipeline.py writes one 'match' node per
    matched interest under each candidate, so a wide raw-result branch with
    several matches apiece nests two collapsible sets) must hide the inner
    set's placeholder too, not emit it as a second, separately-hanging group
    -- exactly what mission_max_results/matching scale up in production."""

    @classmethod
    def setUpClass(cls):
        tmp_dir = tempfile.mkdtemp()
        cls.db_path = os.path.join(tmp_dir, "nested-collapse.db")
        cls.cfg = _fixture_cfg(cls.db_path)
        cls.conn = db.connect(cls.db_path)
        db.init(cls.conn)
        cls.ds = build_datasette(cls.cfg, public=False)

        tracer = trace.Tracer(cls.conn, cls.cfg)
        run = tracer.begin_run("nested-collapse-test")
        mx = tracer.node(run, "mission-execution", label="m1-exec")
        for i in range(6):  # > COLLAPSE_THRESHOLD -- the outer collapsible set
            raw = tracer.node(run, "raw-result", label=f"r{i}")
            tracer.edge(mx, raw, "returned", ordinal=i)
            cand = tracer.node(run, "candidate", label=f"item{i}")
            tracer.edge(raw, cand, "normalized_to")
            for j in range(5):  # > COLLAPSE_THRESHOLD -- an inner collapsible set per candidate
                match = tracer.node(run, "match", label=f"item{i}-match{j}")
                tracer.edge(cand, match, "matched", ordinal=j)
        tracer.finish_run(run, status="done")
        cls.conn.commit()
        cls.run_id = run

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    async def test_nested_collapsible_set_stays_hidden_inside_the_outer_group(self):
        r = await self.ds.client.get(f"/observatory/api/graph?run_id={self.run_id}")
        self.assertEqual(r.status_code, 200)
        g = r.json()

        # Only the outer raw-result set collapses into its own group -- the
        # inner match sets are already covered by that hide and must not
        # produce their own placeholders.
        self.assertEqual(len(g["groups"]), 1)
        self.assertEqual(g["groups"][0]["child_node_type"], "raw-result")
        node_types = {n["node_type"] for n in g["nodes"]}
        self.assertNotIn("candidate", node_types)
        self.assertNotIn("match", node_types)

        # Connectivity: every node besides the genuine root has an inbound
        # edge -- the inner set must not surface as a disconnected or
        # spuriously-parented placeholder.
        inbound = {e["to"] for e in g["edges"]}
        roots = [n for n in g["nodes"] if n["id"] not in inbound]
        self.assertTrue(roots)
        for n in roots:
            self.assertEqual(n["node_type"], "mission-execution")


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryGraphCrossLinkCollapseTests(unittest.IsolatedAsyncioTestCase):
    """Repair regression: `duplicate_of` (and structurally identical
    cross-links like `retried_as`/`feedback_on`) point AT an already-
    existing node that belongs to a DIFFERENT, visible parent -- not a
    child this sibling set owns. Before the fix, _subtree() followed that
    edge like containment, so collapsing a >COLLAPSE_THRESHOLD raw-result
    set that happened to contain one 'duplicate' node silently hid the
    earlier candidate's entire chain (threshold/render/notification) and
    emitted a fabricated normalized_to edge in no trace_edges row. Built
    directly via the real Tracer API -- trace_fixture.build()'s own
    duplicate never has enough raw-result siblings to cross
    COLLAPSE_THRESHOLD, so this shape is otherwise untested."""

    @classmethod
    def setUpClass(cls):
        tmp_dir = tempfile.mkdtemp()
        cls.db_path = os.path.join(tmp_dir, "cross-link-collapse.db")
        cls.cfg = _fixture_cfg(cls.db_path)
        cls.conn = db.connect(cls.db_path)
        db.init(cls.conn)
        cls.ds = build_datasette(cls.cfg, public=False)

        tracer = trace.Tracer(cls.conn, cls.cfg)
        run = tracer.begin_run("cross-link-collapse-test")

        # The earlier, already-stored candidate -- its own visible parent
        # and its own full downstream chain (score -> threshold -> render ->
        # notification), same shape a real sent+notified discovery has.
        earlier_source = tracer.node(run, "collector-item", label="earlier-source")
        earlier_cand = tracer.node(
            run, "candidate", label="earlier-item",
            entity_type="candidate_items", entity_id="100",
        )
        tracer.edge(earlier_source, earlier_cand, "normalized_to")
        earlier_sa = tracer.node(
            run, "score-attempt", label="earlier-item",
            entity_type="candidate_items", entity_id="100",
        )
        tracer.edge(earlier_cand, earlier_sa, "scored")
        earlier_th = tracer.node(
            run, "threshold", label="earlier-item", entity_type="scores", entity_id="100",
        )
        tracer.edge(earlier_sa, earlier_th, "cleared_threshold")
        earlier_render = tracer.node(run, "render", label="telegram message")
        tracer.edge(earlier_th, earlier_render, "rendered")
        earlier_notif = tracer.node(
            run, "notification", label="telegram",
            entity_type="notifications", entity_id="100", status="ok",
        )
        tracer.edge(earlier_render, earlier_notif, "sent")
        cls.earlier_ids = {
            "candidate": earlier_cand, "score-attempt": earlier_sa,
            "threshold": earlier_th, "render": earlier_render, "notification": earlier_notif,
        }

        # A >COLLAPSE_THRESHOLD raw-result set where one sibling is a
        # duplicate pointing (via `duplicate_of`) at the earlier candidate
        # above -- exactly what a real mission_max_results=6-scale mission
        # produces whenever one of its results re-finds an already-stored item.
        mx = tracer.node(run, "mission-execution", label="m1-exec")
        for i in range(4):  # > COLLAPSE_THRESHOLD
            raw = tracer.node(run, "raw-result", label=f"r{i}")
            tracer.edge(mx, raw, "returned", ordinal=i)
            if i == 0:
                dup = tracer.node(run, "duplicate", label="dup-of-earlier")
                tracer.edge(raw, dup, "normalized_to")
                tracer.edge(dup, earlier_cand, "duplicate_of")
            else:
                cand = tracer.node(
                    run, "candidate", label=f"item{i}",
                    entity_type="candidate_items", entity_id=str(i + 1),
                )
                tracer.edge(raw, cand, "normalized_to")
        tracer.finish_run(run, status="done")
        cls.conn.commit()
        cls.run_id = run

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    async def test_cross_linked_branch_survives_collapse_of_an_unrelated_sibling_set(self):
        r = await self.ds.client.get(f"/observatory/api/graph?run_id={self.run_id}")
        self.assertEqual(r.status_code, 200)
        g = r.json()

        # The unrelated raw-result set (4 siblings, one a duplicate) does
        # collapse -- that part is correct and expected.
        self.assertEqual(len(g["groups"]), 1)
        self.assertEqual(g["groups"][0]["child_node_type"], "raw-result")
        self.assertEqual(g["groups"][0]["child_count"], 4)

        # But the earlier candidate's ENTIRE chain -- reached only via the
        # duplicate's cross-link edge, never owned by the raw-result set --
        # must stay fully visible: none of it may be silently hidden.
        node_ids = {n["id"] for n in g["nodes"]}
        for node_type, nid in self.earlier_ids.items():
            self.assertIn(nid, node_ids, f"earlier {node_type} node was wrongly absorbed by the collapse")

        # No fabricated edge: every emitted edge's source is either a real
        # node or the group id that owns it -- specifically, nothing named
        # a bare 'collector-item' as directly wired to the group (the live
        # bug emitted a phantom normalized_to edge with no trace_edges row).
        group_id = g["groups"][0]["group"]
        dup_edge = [e for e in g["edges"] if e["relationship"] == "duplicate_of"]
        self.assertEqual(len(dup_edge), 1)
        self.assertEqual(dup_edge[0]["from"], group_id)
        self.assertEqual(dup_edge[0]["to"], self.earlier_ids["candidate"])

        # Connectivity: every node besides the genuine roots has an inbound edge.
        inbound = {e["to"] for e in g["edges"]}
        for n in g["nodes"]:
            if n["id"] in inbound:
                continue
            self.assertIn(n["node_type"], ("collector-item", "mission-execution"), f"floating node: {n}")


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryGraphCrossLinkSharedTargetOrderTests(unittest.IsolatedAsyncioTestCase):
    """Repair regression: TWO independent collapsible sibling sets (two
    missions in one web-tick, the ordinary shape) whose own duplicates both
    `duplicate_of`-point at the SAME earlier candidate must not fight over
    ownership -- with the old subtree walk, whichever set's dict-iteration
    happened last would 'win' the shared node and hide it out from under
    the other. Neither set legitimately owns it, so it must stay visible
    under both, regardless of dict order."""

    @classmethod
    def setUpClass(cls):
        tmp_dir = tempfile.mkdtemp()
        cls.db_path = os.path.join(tmp_dir, "cross-link-shared-target.db")
        cls.cfg = _fixture_cfg(cls.db_path)
        cls.conn = db.connect(cls.db_path)
        db.init(cls.conn)
        cls.ds = build_datasette(cls.cfg, public=False)

        tracer = trace.Tracer(cls.conn, cls.cfg)
        run = tracer.begin_run("cross-link-shared-target-test")

        earlier_source = tracer.node(run, "collector-item", label="earlier-source")
        earlier_cand = tracer.node(
            run, "candidate", label="earlier-item",
            entity_type="candidate_items", entity_id="200",
        )
        tracer.edge(earlier_source, earlier_cand, "normalized_to")
        cls.earlier_cand = earlier_cand

        for m in range(2):  # two independent missions, both find the same duplicate
            mx = tracer.node(run, "mission-execution", label=f"m{m}-exec")
            for i in range(4):  # > COLLAPSE_THRESHOLD each
                raw = tracer.node(run, "raw-result", label=f"m{m}-r{i}")
                tracer.edge(mx, raw, "returned", ordinal=i)
                if i == 0:
                    dup = tracer.node(run, "duplicate", label=f"m{m}-dup-of-earlier")
                    tracer.edge(raw, dup, "normalized_to")
                    tracer.edge(dup, earlier_cand, "duplicate_of")
                else:
                    cand = tracer.node(run, "candidate", label=f"m{m}-item{i}")
                    tracer.edge(raw, cand, "normalized_to")
        tracer.finish_run(run, status="done")
        cls.conn.commit()
        cls.run_id = run

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    async def test_shared_duplicate_target_stays_visible_regardless_of_group_order(self):
        r = await self.ds.client.get(f"/observatory/api/graph?run_id={self.run_id}")
        self.assertEqual(r.status_code, 200)
        g = r.json()
        self.assertEqual(len(g["groups"]), 2)
        node_ids = {n["id"] for n in g["nodes"]}
        self.assertIn(self.earlier_cand, node_ids, "shared duplicate target was hidden by one of the two groups")
        dup_edges = [e for e in g["edges"] if e["relationship"] == "duplicate_of"]
        self.assertEqual(len(dup_edges), 2)
        for e in dup_edges:
            self.assertEqual(e["to"], self.earlier_cand)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryNodeAndInterestTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()
        cls.ds = build_datasette(cls.cfg, public=False)
        cls.raw = sqlite3.connect(cls.db_path)
        cls.raw.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.raw.close()

    def _breakthrough_score_attempt_node(self):
        row = self.raw.execute(
            "SELECT id FROM trace_nodes WHERE node_type = 'score-attempt' AND label = 'Fixture Breakthrough Finding'"
        ).fetchone()
        return row["id"]

    async def test_node_inspector_has_byte_exact_prompt_and_both_responses_after_retry(self):
        node_id = self._breakthrough_score_attempt_node()
        raw_calls = self.raw.execute(
            "SELECT * FROM model_calls WHERE trace_node_id = ? ORDER BY id ASC", (node_id,)
        ).fetchall()
        self.assertEqual(len(raw_calls), 2, "expected one failed + one retried model_calls row")

        r = await self.ds.client.get(f"/observatory/api/node/{node_id}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["overview"]["node_type"], "score-attempt")
        self.assertEqual(len(body["model_calls"]), 2)

        first, second = body["model_calls"]
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["validation_result"], "invalid")
        self.assertIsNotNone(first["error"])
        self.assertEqual(first["raw_response_text"], "not valid json")
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["validation_result"], "valid")
        self.assertIsNone(second["error"])
        self.assertIsInstance(second["parsed_response_json"], dict)
        self.assertEqual(second["parsed_response_json"]["interest_key"], "fixture-interest")

        # Byte-exact round trip: the API's prompt text is identical to what's
        # actually stored on disk, not a reconstruction.
        self.assertEqual(second["exact_user_prompt"], raw_calls[1]["exact_user_prompt"])
        self.assertIn("Fixture Breakthrough Finding", second["exact_user_prompt"])

        for call in body["model_calls"]:
            self.assertIn("row_url", call)
            self.assertTrue(call["row_url"].startswith(f"/{_db_name(self.db_path)}/model_calls/"))

        self.assertIn("row_urls", body)
        self.assertFalse(body["truncated"])

    async def test_node_inspector_404s_for_a_missing_node(self):
        r = await self.ds.client.get("/observatory/api/node/999999")
        self.assertEqual(r.status_code, 404)

    async def test_interest_view_is_complete(self):
        r = await self.ds.client.get("/observatory/api/interest/fixture-interest")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["definition"]["key"], "fixture-interest")
        self.assertEqual(body["definition"]["layer"], "owner")
        self.assertIn("positive", body["signals"])
        self.assertEqual(len(body["generations"]), 1)
        self.assertEqual(len(body["missions"]), 3)
        self.assertEqual(len(body["discoveries"]), 3)
        self.assertEqual(len(body["feedback"]), 1)
        self.assertEqual(body["feedback"][0]["verdict"], "fire")
        # 'failures' -- below_threshold for the duplicate-survivor (0.5) and
        # the below-threshold item (0.15), both entity_type='scores' rows
        # genuinely owned by this interest.
        self.assertEqual(len(body["failures"]), 2)
        for f in body["failures"]:
            self.assertEqual(f["kind"], "below_threshold")
            self.assertEqual(f["entity_type"], "scores")

    async def test_interest_view_404s_for_an_unknown_key(self):
        r = await self.ds.client.get("/observatory/api/interest/no-such-key")
        self.assertEqual(r.status_code, 404)

    async def test_interest_failures_are_not_polluted_by_entity_id_collisions(self):
        # Repair 1 regression: the fixture's scores.id 1/2 numerically
        # collide with search_missions.id 1/2. The original query
        # correlated `failures` on entity_id alone (no entity_type
        # constraint), so the two below_threshold scores were only ever
        # included by that coincidence, and a real production db (hundreds
        # of missions per interest) would pull in unrelated candidates'
        # failures the same way. Every returned entity_id must actually
        # belong to *this* interest, checked the honest way: independently
        # re-derived from the raw db, not the same query under test.
        r = await self.ds.client.get("/observatory/api/interest/fixture-interest")
        body = r.json()
        own_score_ids = {
            str(row[0]) for row in self.raw.execute(
                "SELECT s.id FROM scores s JOIN interests i ON i.id = s.interest_id "
                "WHERE i.key = 'fixture-interest'"
            ).fetchall()
        }
        for f in body["failures"]:
            if f["entity_type"] == "scores":
                self.assertIn(f["entity_id"], own_score_ids)
            else:
                self.fail(f"unexpected entity_type in failures: {f['entity_type']}")

    async def test_deep_link_resolves_the_right_score(self):
        score_row = self.raw.execute(
            "SELECT s.id FROM scores s JOIN candidate_items ci ON ci.id = s.item_id "
            "WHERE ci.title = 'Fixture Breakthrough Finding'"
        ).fetchone()
        score_id = score_row["id"]
        threshold_node = self.raw.execute(
            "SELECT id, run_id FROM trace_nodes WHERE node_type = 'threshold' "
            "AND entity_type = 'scores' AND entity_id = ?",
            (str(score_id),),
        ).fetchone()

        r = await self.ds.client.get(f"/observatory/trace/score/{score_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("observatory-bootstrap", r.text)
        self.assertIn(f'"score_id": {score_id}', r.text)
        self.assertIn(f'"node_id": {threshold_node["id"]}', r.text)
        self.assertIn(f'"run_id": {threshold_node["run_id"]}', r.text)

    async def test_deep_link_404s_for_an_untracked_score(self):
        r = await self.ds.client.get("/observatory/trace/score/999999")
        self.assertEqual(r.status_code, 404)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryCompareTests(unittest.IsolatedAsyncioTestCase):
    """compare() is exercised directly against two hand-built trace runs (via
    the real Tracer API, same primitives missions.py/pipeline.py use) rather
    than two independent trace_fixture.build() calls -- the fixture's own
    Council fake asserts it is called exactly once per build, so reusing it
    twice against one conn isn't a safe way to get two genuinely different
    traces. This still exercises the exact same compare() code path/SQL a
    real two-run comparison would."""

    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()
        cls.ds = build_datasette(cls.cfg, public=False)
        cls.conn = db.connect(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _two_runs(self):
        tracer = trace.Tracer(self.conn, self.cfg)
        run_a = tracer.begin_run("compare-test")
        a1 = tracer.node(run_a, "candidate", label="Widget", status="ok", output_json={"v": 1})
        a2 = tracer.node(run_a, "match", label="only-in-a")
        tracer.edge(a1, a2, "matched")
        tracer.finish_run(run_a, status="done")

        run_b = tracer.begin_run("compare-test")
        b1 = tracer.node(run_b, "candidate", label="Widget", status="error", output_json={"v": 2})
        b2 = tracer.node(run_b, "match", label="only-in-b")
        tracer.edge(b1, b2, "matched")
        tracer.finish_run(run_b, status="done")
        return run_a, run_b

    async def test_compare_run_topology_diff(self):
        run_a, run_b = self._two_runs()
        r = await self.ds.client.get(f"/observatory/api/compare?a={run_a}&b={run_b}&kind=run")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # Keyed by (node_type, label, inbound_relationship, inbound_ordinal),
        # not just (node_type, label) -- same-labelled siblings would
        # otherwise collapse into one dict entry and silently vanish.
        self.assertIn(["match", "only-in-b", "matched", 0], body["nodes"]["added"])
        self.assertIn(["match", "only-in-a", "matched", 0], body["nodes"]["removed"])
        changed_keys = [c["key"] for c in body["nodes"]["changed"]]
        self.assertIn(["candidate", "Widget", None, None], changed_keys)
        widget_change = next(c for c in body["nodes"]["changed"] if c["key"] == ["candidate", "Widget", None, None])
        self.assertEqual(widget_change["a"]["status"], "ok")
        self.assertEqual(widget_change["b"]["status"], "error")

    async def test_compare_identical_run_against_itself_is_empty(self):
        run_a, _run_b = self._two_runs()
        r = await self.ds.client.get(f"/observatory/api/compare?a={run_a}&b={run_a}&kind=run")
        body = r.json()
        self.assertEqual(body["nodes"]["added"], [])
        self.assertEqual(body["nodes"]["removed"], [])
        self.assertEqual(body["nodes"]["changed"], [])

    async def test_compare_model_call_prompt_and_response_diff(self):
        node_id = self.conn.execute("SELECT id FROM trace_nodes LIMIT 1").fetchone()["id"]
        cur_a = self.conn.execute(
            "INSERT INTO model_calls (trace_node_id, attempt, exact_user_prompt, raw_response_text, started_at) "
            "VALUES (?, 1, 'line one\nline two\nline three', 'response A', ?)",
            (node_id, db.now()),
        )
        cur_b = self.conn.execute(
            "INSERT INTO model_calls (trace_node_id, attempt, exact_user_prompt, raw_response_text, started_at) "
            "VALUES (?, 1, 'line one\nCHANGED\nline three', 'response B', ?)",
            (node_id, db.now()),
        )
        self.conn.commit()
        r = await self.ds.client.get(
            f"/observatory/api/compare?a={cur_a.lastrowid}&b={cur_b.lastrowid}&kind=model_call"
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        prompt_diff_text = "\n".join(body["prompt_diff"])
        self.assertIn("-line two", prompt_diff_text)
        self.assertIn("+CHANGED", prompt_diff_text)
        response_diff_text = "\n".join(body["response_diff"])
        self.assertIn("-response A", response_diff_text)
        self.assertIn("+response B", response_diff_text)

    async def test_compare_model_call_404s_on_a_nonexistent_id(self):
        # A typo'd id must not silently diff against "" -- that renders as a
        # plausible-looking "everything was removed" answer instead of the
        # not-found it actually is.
        node_id = self.conn.execute("SELECT id FROM trace_nodes LIMIT 1").fetchone()["id"]
        cur = self.conn.execute(
            "INSERT INTO model_calls (trace_node_id, attempt, exact_user_prompt, raw_response_text, started_at) "
            "VALUES (?, 1, 'real prompt', 'real response', ?)",
            (node_id, db.now()),
        )
        self.conn.commit()
        r = await self.ds.client.get(
            f"/observatory/api/compare?a={cur.lastrowid}&b=999999&kind=model_call"
        )
        self.assertEqual(r.status_code, 404)

    async def test_compare_run_disambiguates_same_labelled_siblings(self):
        # Same node_type+label siblings (e.g. two runs each writing several
        # equally-labelled nodes) must not collapse into one dict entry and
        # silently drop the extras from added/removed.
        tracer = trace.Tracer(self.conn, self.cfg)
        run_a = tracer.begin_run("compare-sibling-test")
        parent_a = tracer.node(run_a, "generation", label="gen")
        for i in range(3):
            child = tracer.node(run_a, "raw-result", label="dup")
            tracer.edge(parent_a, child, "returned", ordinal=i)
        tracer.finish_run(run_a, status="done")

        run_b = tracer.begin_run("compare-sibling-test")
        parent_b = tracer.node(run_b, "generation", label="gen")
        for i in range(4):   # one more sibling than run_a
            child = tracer.node(run_b, "raw-result", label="dup")
            tracer.edge(parent_b, child, "returned", ordinal=i)
        tracer.finish_run(run_b, status="done")

        r = await self.ds.client.get(f"/observatory/api/compare?a={run_a}&b={run_b}&kind=run")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # Exactly the 4th sibling (ordinal 3) is new -- the first three must
        # NOT show up as added/removed just because they share a label.
        self.assertEqual(body["nodes"]["added"], [["raw-result", "dup", "returned", 3]])
        self.assertEqual(body["nodes"]["removed"], [])


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryInputValidationTests(unittest.IsolatedAsyncioTestCase):
    """Malformed query params must be a clean 400, not a Datasette HTML 500
    error page -- repair 1 regression (found live against the fixture)."""

    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()
        cls.ds = build_datasette(cls.cfg, public=False)

    async def _get(self, path):
        return await self.ds.client.get(path)

    async def test_list_rejects_non_integer_limit_and_offset(self):
        r = await self._get("/observatory/api/list?tab=discoveries&limit=abc")
        self.assertEqual(r.status_code, 400)
        r2 = await self._get("/observatory/api/list?tab=discoveries&offset=xyz")
        self.assertEqual(r2.status_code, 400)

    async def test_graph_rejects_a_non_integer_run_id(self):
        r = await self._get("/observatory/api/graph?run_id=notanint")
        self.assertEqual(r.status_code, 400)

    async def test_children_rejects_a_non_integer_node_id(self):
        r = await self._get("/observatory/api/children?node_id=abc")
        self.assertEqual(r.status_code, 400)

    async def test_children_rejects_a_malformed_group(self):
        r = await self._get("/observatory/api/children?group=bogus")
        self.assertEqual(r.status_code, 400)
        r2 = await self._get("/observatory/api/children?group=1:matched")
        self.assertEqual(r2.status_code, 400)

    async def test_compare_rejects_non_integer_ids(self):
        r = await self._get("/observatory/api/compare?a=x&b=y")
        self.assertEqual(r.status_code, 400)

    async def test_compare_rejects_an_unknown_kind(self):
        # Previously silently fell through to kind='run', diffing two RUN
        # ids that happen to equal the given generation ids -- a
        # plausible-looking but wrong response instead of a rejection.
        r = await self._get("/observatory/api/compare?a=1&b=2&kind=generation")
        self.assertEqual(r.status_code, 400)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()
        cls.ds = build_datasette(cls.cfg, public=False)

    async def test_every_route_leaves_every_table_row_count_unchanged(self):
        before = _table_counts(self.db_path)
        db_name = _db_name(self.db_path)

        raw = sqlite3.connect(self.db_path)
        item_id = raw.execute("SELECT id FROM candidate_items LIMIT 1").fetchone()[0]
        node_id = raw.execute("SELECT id FROM trace_nodes LIMIT 1").fetchone()[0]
        score_id = raw.execute("SELECT id FROM scores LIMIT 1").fetchone()[0]
        raw.close()

        routes = [
            "/observatory/",
            "/observatory/api/list?tab=discoveries",
            "/observatory/api/list?tab=interests",
            "/observatory/api/list?tab=generations",
            "/observatory/api/list?tab=missions",
            "/observatory/api/list?tab=failed",
            f"/observatory/api/graph?entity_type=candidate_items&entity_id={item_id}",
            f"/observatory/api/children?node_id={node_id}",
            f"/observatory/api/node/{node_id}",
            "/observatory/api/interest/fixture-interest",
            f"/observatory/trace/score/{score_id}",
            f"/{db_name}/trace_nodes",
            f"/{db_name}/scores/{score_id}",
        ]
        for path in routes:
            r = await self.ds.client.get(path)
            self.assertLess(r.status_code, 500, f"{path} -> {r.status_code}")

        after = _table_counts(self.db_path)
        self.assertEqual(before, after)

    async def test_post_to_an_api_route_is_rejected_not_executed(self):
        r = await self.ds.client.post("/observatory/api/list", data={})
        self.assertEqual(r.status_code, 405)

    async def test_write_attempt_via_native_sql_does_not_mutate_the_db(self):
        before = _table_counts(self.db_path)
        db_name = _db_name(self.db_path)
        # Datasette 0.65's native SQL-query surface is `/{db}?sql=...`
        # (`/{db}/-/query` is not a route at all in this version -- it falls
        # through to a row lookup for a table literally named "-" and 400s
        # for that reason, never parsing the SQL). Hit the real route so the
        # 400 below actually proves the "SELECT-only" guard, not routing.
        r = await self.ds.client.get(f"/{db_name}", params={"sql": "DELETE FROM candidate_items"})
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"Statement must be a SELECT", r.content)
        after = _table_counts(self.db_path)
        self.assertEqual(before, after)

    async def test_native_table_page_exists_for_every_operational_and_trace_table(self):
        db_name = _db_name(self.db_path)
        for table in ALL_TABLES:
            r = await self.ds.client.get(f"/{db_name}/{table}")
            self.assertEqual(r.status_code, 200, f"table page missing/broken for {table!r}")


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryAuthTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()
        cls.cfg.ui_token = "s3cr3t-observatory-token"
        cls.public_ds = build_datasette(cls.cfg, public=True)
        cls.private_ds = build_datasette(cls.cfg, public=False)

    def test_public_mode_refuses_to_build_without_a_token(self):
        cfg = _fixture_cfg(self.db_path, ui_token="")
        with self.assertRaises(ValueError):
            build_datasette(cfg, public=True)

    async def test_private_mode_is_open_with_no_token(self):
        r = await self.private_ds.client.get("/observatory/api/list?tab=discoveries")
        self.assertEqual(r.status_code, 200)

    async def test_public_mode_rejects_anonymous_requests(self):
        r = await self.public_ds.client.get("/observatory/api/list?tab=discoveries")
        self.assertEqual(r.status_code, 403)

    async def test_public_mode_rejects_wrong_token(self):
        r = await self.public_ds.client.get(
            "/observatory/api/list?tab=discoveries", headers={"Authorization": "Bearer wrong-token"}
        )
        self.assertEqual(r.status_code, 403)

    async def test_public_mode_accepts_correct_bearer_token(self):
        r = await self.public_ds.client.get(
            "/observatory/api/list?tab=discoveries",
            headers={"Authorization": "Bearer s3cr3t-observatory-token"},
        )
        self.assertEqual(r.status_code, 200)

    async def test_public_mode_gates_native_datasette_pages_too(self):
        db_name = _db_name(self.db_path)
        anon = await self.public_ds.client.get(f"/{db_name}/trace_nodes")
        self.assertEqual(anon.status_code, 403)
        authed = await self.public_ds.client.get(
            f"/{db_name}/trace_nodes", headers={"Authorization": "Bearer s3cr3t-observatory-token"}
        )
        self.assertEqual(authed.status_code, 200)

    async def test_public_mode_gates_the_shell_page(self):
        anon = await self.public_ds.client.get("/observatory/")
        self.assertEqual(anon.status_code, 403)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryPromptVisibilityTests(unittest.IsolatedAsyncioTestCase):
    """The prompt is stored byte-exact but was unreachable from the nodes a
    reader actually clicks: only score-attempt/mission-execution/council carry
    model calls, while the cards telling the story (candidate, threshold,
    score-debug) carry none. node_detail() now lends those nodes the calls of
    the adjacent node that produced them, tagged with provenance."""

    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()

    def setUp(self):
        self.ds = build_datasette(self.cfg, public=False)
        raw = sqlite3.connect(self.db_path)
        raw.row_factory = sqlite3.Row
        self.addCleanup(raw.close)
        self.raw = raw

    def _node_id(self, node_type):
        row = self.raw.execute(
            "SELECT id FROM trace_nodes WHERE node_type = ? ORDER BY id ASC LIMIT 1", (node_type,)
        ).fetchone()
        self.assertIsNotNone(row, f"fixture has no {node_type} node")
        return row["id"]

    async def test_threshold_node_borrows_the_score_attempts_prompt(self):
        r = await self.ds.client.get(f"/observatory/api/node/{self._node_id('threshold')}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["model_calls"], [], "threshold nodes carry no calls of their own")
        borrowed = body["related_model_calls"]
        self.assertTrue(borrowed, "threshold node borrowed no calls from its score-attempt")
        self.assertTrue(any(c["exact_user_prompt"] for c in borrowed))
        for c in borrowed:
            self.assertEqual(c["via_node_type"], "score-attempt")
            self.assertIsNotNone(c["via_node_id"])

    async def test_candidate_node_borrows_through_the_scored_edge(self):
        r = await self.ds.client.get(f"/observatory/api/node/{self._node_id('candidate')}")
        body = r.json()
        self.assertEqual(body["model_calls"], [])
        self.assertTrue(body["related_model_calls"], "candidate borrowed nothing via its scored edge")

    async def test_a_node_with_its_own_calls_borrows_nothing(self):
        r = await self.ds.client.get(f"/observatory/api/node/{self._node_id('score-attempt')}")
        body = r.json()
        self.assertTrue(body["model_calls"], "fixture score-attempt should carry calls")
        self.assertEqual(
            body["related_model_calls"], [],
            "a node with its own calls must not also show a neighbour's",
        )

    async def test_unrelated_node_type_borrows_nothing(self):
        r = await self.ds.client.get(f"/observatory/api/node/{self._node_id('interest-state')}")
        if r.status_code == 200:
            self.assertEqual(r.json()["related_model_calls"], [])

    async def test_prompt_template_diffs_a_scoring_call_against_the_live_template(self):
        call_id = self.raw.execute(
            "SELECT id FROM model_calls WHERE call_role = 'scoring' ORDER BY id ASC LIMIT 1"
        ).fetchone()["id"]
        r = await self.ds.client.get(f"/observatory/api/prompt-template?call={call_id}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["role"], "scoring")
        # The fixture's scores carry the current fingerprint, so a real diff is
        # produced rather than the "template moved on" degradation.
        self.assertTrue(body["matches_current"], body.get("reason"))
        # The template still carries its un-substituted placeholders, which is
        # what makes it a template rather than another copy of the prompt.
        template = body["template"] or ""
        self.assertIn("{interests}", template)
        self.assertIn("{item}" if "{item}" in template else "personal_relevance", template)
        self.assertTrue(body["diff"], "a matching fingerprint should still diff the substitutions")

    async def test_prompt_template_declines_roles_with_no_stable_template(self):
        call_id = self.raw.execute(
            "SELECT id FROM model_calls WHERE call_role = 'council' ORDER BY id ASC LIMIT 1"
        ).fetchone()["id"]
        body = (await self.ds.client.get(f"/observatory/api/prompt-template?call={call_id}")).json()
        self.assertFalse(body["available"])
        self.assertIn("council", body["reason"])

    async def test_prompt_template_reports_a_moved_template_instead_of_a_wrong_diff(self):
        call_id = self.raw.execute(
            "SELECT id FROM model_calls WHERE call_role = 'scoring' ORDER BY id ASC LIMIT 1"
        ).fetchone()["id"]
        writable = sqlite3.connect(self.db_path)
        writable.execute("UPDATE scores SET prompt_hash = 'deadbeefcafe'")
        writable.commit()
        writable.close()
        self.addCleanup(self._restore_prompt_hashes)

        body = (await self.ds.client.get(f"/observatory/api/prompt-template?call={call_id}")).json()
        self.assertFalse(body["matches_current"])
        self.assertEqual(body["diff"], [], "a diff across template versions would be misleading")
        self.assertIn("deadbeefcafe", body["reason"])

    def _restore_prompt_hashes(self):
        import discovery.scoring as scoring
        writable = sqlite3.connect(self.db_path)
        writable.execute("UPDATE scores SET prompt_hash = ?", (scoring.prompt_fingerprint(),))
        writable.commit()
        writable.close()

    async def test_prompt_template_404s_on_an_unknown_call(self):
        r = await self.ds.client.get("/observatory/api/prompt-template?call=999999")
        self.assertEqual(r.status_code, 404)

    async def test_prompt_template_rejects_a_non_integer_call(self):
        r = await self.ds.client.get("/observatory/api/prompt-template?call=abc")
        self.assertEqual(r.status_code, 400)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryRunIndexAndRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Compare asked for run ids as raw numbers and the UI displayed one
    nowhere, so the view built for comparing runs had no way to discover its
    own inputs."""

    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()

    def setUp(self):
        self.ds = build_datasette(self.cfg, public=False)

    async def test_run_index_lists_runs_newest_first(self):
        body = (await self.ds.client.get("/observatory/api/runs")).json()
        runs = body["runs"]
        self.assertTrue(runs)
        self.assertEqual(set(runs[0]), {"id", "kind", "status", "started_at", "node_count"})
        self.assertEqual([r["id"] for r in runs], sorted((r["id"] for r in runs), reverse=True))

    async def test_run_index_counts_nodes_per_run(self):
        runs = (await self.ds.client.get("/observatory/api/runs")).json()["runs"]
        raw = sqlite3.connect(self.db_path)
        for run in runs:
            expected = raw.execute(
                "SELECT COUNT(*) FROM trace_nodes WHERE run_id = ?", (run["id"],)
            ).fetchone()[0]
            self.assertEqual(run["node_count"], expected)
        raw.close()

    async def test_unknown_api_path_404s_as_json_instead_of_redirecting(self):
        # A synthetic group id ("12:matched:match") misses the digits-only node
        # route; it used to fall through to Datasette's own routing and 302 into
        # the raw database UI, which is a confusing thing to find in a network
        # tab when a fetch "fails".
        r = await self.ds.client.get("/observatory/api/node/12:matched:match")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.json())

    async def test_unknown_api_endpoint_404s(self):
        r = await self.ds.client.get("/observatory/api/not-a-thing")
        self.assertEqual(r.status_code, 404)
        self.assertIn("no such endpoint", r.json()["error"])

    async def test_real_endpoints_still_win_over_the_catch_all(self):
        for path in ("/observatory/api/list", "/observatory/api/runs", "/observatory/api/interests"):
            with self.subTest(path=path):
                self.assertEqual((await self.ds.client.get(path)).status_code, 200)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryPublicModeTests(unittest.IsolatedAsyncioTestCase):
    """In --public mode the raw-database button opened '/' with no bearer
    token, landing on a 403 behind the standing tunnel."""

    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db(ui_token="tok-123")

    async def test_bootstrap_reports_public_mode_so_the_ui_can_hide_that_button(self):
        ds = build_datasette(self.cfg, public=True)
        r = await ds.client.get("/observatory/?token=tok-123")
        self.assertEqual(r.status_code, 200)
        self.assertIn('"public": true', r.text.replace('"public":true', '"public": true'))

    async def test_private_mode_bootstrap_reports_not_public(self):
        ds = build_datasette(self.cfg, public=False)
        r = await ds.client.get("/observatory/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('"public": false', r.text.replace('"public":false', '"public": false'))

    async def test_catch_all_still_refuses_unauthenticated_requests(self):
        ds = build_datasette(self.cfg, public=True)
        r = await ds.client.get("/observatory/api/not-a-thing")
        self.assertEqual(r.status_code, 403, "the 404 catch-all must not become an auth bypass")


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryInterestFilteringTests(unittest.IsolatedAsyncioTestCase):
    """The interest filter was a free-text box that had to contain an exact,
    case-sensitive key, with no list of valid keys anywhere in the UI -- and on
    the Interests tab the backend ignored it entirely."""

    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()

    def setUp(self):
        self.ds = build_datasette(self.cfg, public=False)

    async def test_interest_index_lists_every_interest_cheaply(self):
        body = (await self.ds.client.get("/observatory/api/interests")).json()
        rows = body["interests"]
        self.assertTrue(rows)
        # Deliberately not the list_rows shape: no COUNT subqueries, no
        # MAX_LIMIT cap (46 live interests against a cap of 50).
        self.assertEqual(set(rows[0]), {"key", "title", "active", "layer"})

    async def test_interest_index_puts_active_interests_first(self):
        raw = sqlite3.connect(self.db_path)
        raw.execute("UPDATE interests SET active = 0 WHERE id = (SELECT MIN(id) FROM interests)")
        raw.commit()
        raw.close()
        rows = (await self.ds.client.get("/observatory/api/interests")).json()["interests"]
        actives = [bool(r["active"]) for r in rows]
        self.assertEqual(actives, sorted(actives, reverse=True), "inactive interests sorted above active ones")

    async def test_interests_tab_can_filter_by_active_state(self):
        raw = sqlite3.connect(self.db_path)
        raw.execute("UPDATE interests SET active = 0 WHERE id = (SELECT MIN(id) FROM interests)")
        raw.commit()
        total = raw.execute("SELECT COUNT(*) FROM interests").fetchone()[0]
        raw.close()

        active = (await self.ds.client.get("/observatory/api/list?tab=interests&active=yes")).json()
        inactive = (await self.ds.client.get("/observatory/api/list?tab=interests&active=no")).json()
        self.assertEqual(inactive["total"], 1)
        self.assertEqual(active["total"] + inactive["total"], total)
        self.assertTrue(all(r["active"] == 0 for r in inactive["rows"]))

    async def test_unknown_active_value_is_ignored_rather_than_erroring(self):
        total = (await self.ds.client.get("/observatory/api/list?tab=interests")).json()["total"]
        body = (await self.ds.client.get("/observatory/api/list?tab=interests&active=maybe")).json()
        self.assertEqual(body["total"], total)

    async def test_interests_endpoint_is_not_swallowed_by_the_interest_key_route(self):
        """/api/interests must not resolve as /api/interest/<key='s'>."""
        r = await self.ds.client.get("/observatory/api/interests")
        self.assertEqual(r.status_code, 200)
        self.assertIn("interests", r.json())


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryEntityRowTests(unittest.IsolatedAsyncioTestCase):
    """The operational row behind a node used to be reachable only by leaving
    the app for raw Datasette -- node_detail() knew entity_type/entity_id and
    built nothing but a link out of them."""

    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()

    def setUp(self):
        self.ds = build_datasette(self.cfg, public=False)
        self.raw = sqlite3.connect(self.db_path)
        self.raw.row_factory = sqlite3.Row
        self.addCleanup(self.raw.close)

    def _node_of_type(self, node_type):
        row = self.raw.execute(
            "SELECT id FROM trace_nodes WHERE node_type = ? AND entity_id IS NOT NULL ORDER BY id ASC LIMIT 1",
            (node_type,),
        ).fetchone()
        self.assertIsNotNone(row, f"fixture has no {node_type} node with an entity")
        return row["id"]

    async def test_threshold_node_carries_its_scores_row_and_the_weights(self):
        body = (await self.ds.client.get(f"/observatory/api/node/{self._node_of_type('threshold')}")).json()
        row = body["entity_row"]
        self.assertIsNotNone(row, "threshold node returned no scores row")
        for dimension in ("personal_relevance", "novelty", "depth", "specificity", "importance", "surprise"):
            self.assertIn(dimension, row)
        # The weighting must come from the pipeline's own module so the UI can
        # never drift from the formula that actually produced final_score.
        self.assertEqual(body["score_weights"], dict(models.WEIGHTS))
        self.assertNotIn("specificity", body["score_weights"], "specificity is deliberately unweighted")

    async def test_candidate_node_carries_the_item_text_the_scorer_read(self):
        body = (await self.ds.client.get(f"/observatory/api/node/{self._node_of_type('candidate')}")).json()
        row = body["entity_row"]
        self.assertIsNotNone(row)
        self.assertIn("text", row)
        self.assertIn("dedup_key", row)
        self.assertIsNone(body["score_weights"], "weights belong only to a scores row")

    async def test_duplicate_node_names_the_item_it_duplicated(self):
        row = self.raw.execute(
            "SELECT id FROM trace_nodes WHERE node_type = 'duplicate' AND entity_id IS NOT NULL LIMIT 1"
        ).fetchone()
        if row is None:
            self.skipTest("fixture has no duplicate node with an entity")
        body = (await self.ds.client.get(f"/observatory/api/node/{row['id']}")).json()
        entity = body["entity_row"] or {}
        if entity.get("duplicate_of"):
            self.assertIn("duplicate_of_item", entity)
            self.assertIn("title", entity["duplicate_of_item"])

    async def test_a_node_with_no_entity_gets_no_entity_row(self):
        row = self.raw.execute(
            "SELECT id FROM trace_nodes WHERE entity_id IS NULL ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            self.skipTest("fixture has no entity-less node")
        body = (await self.ds.client.get(f"/observatory/api/node/{row['id']}")).json()
        self.assertIsNone(body["entity_row"])

    def test_entity_type_is_whitelisted_never_interpolated(self):
        """entity_type is data, and data does not get to name tables."""
        conn = odb.open_ro(self.db_path)
        self.addCleanup(conn.close)
        hostile = {"entity_type": "candidate_items; DROP TABLE scores--", "entity_id": "1"}
        self.assertIsNone(odb._entity_row(conn, hostile))
        # And a plain unlisted table stays unreadable through this path.
        self.assertIsNone(odb._entity_row(conn, {"entity_type": "trace_runs", "entity_id": "1"}))
        self.assertTrue(
            conn.execute("SELECT count(*) FROM scores").fetchone()[0] >= 0,
            "scores table must still exist",
        )


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ObservatoryRedactionTests(unittest.IsolatedAsyncioTestCase):
    """Task 1's redaction happens at write time; this proves the SECOND,
    independent read-time pass (observatory/db.py routing everything through
    trace.redact_json before responding) also catches a secret -- planted
    directly into the raw db bytes, bypassing write-time redaction entirely,
    so this test cannot pass by accident just because task 1 already
    redacted it."""

    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.cfg = _build_fixture_db()

    def setUp(self):
        self.secret_value = "sk-plantedsecretvalue1234567890"
        raw = sqlite3.connect(self.db_path)
        node = raw.execute(
            "SELECT id FROM trace_nodes WHERE node_type = 'score-attempt' "
            "AND label = 'Fixture Breakthrough Finding'"
        ).fetchone()
        self.node_id = node[0]
        self.item_id = raw.execute(
            "SELECT id FROM candidate_items WHERE title = 'Fixture Breakthrough Finding'"
        ).fetchone()[0]
        raw.execute(
            "UPDATE trace_nodes SET summary = ? WHERE id = ?",
            (f"leaked during read-time test: {self.secret_value}", self.node_id),
        )
        raw.execute(
            "UPDATE model_calls SET raw_response_text = raw_response_text || ? WHERE trace_node_id = ?",
            (f" secret={self.secret_value}", self.node_id),
        )
        # Also planted in the prompt itself: the prompt-template endpoint echoes
        # exact_user_prompt back inside its diff, so that path needs its own
        # proof it goes through redaction rather than around it.
        raw.execute(
            "UPDATE model_calls SET exact_user_prompt = exact_user_prompt || ? WHERE trace_node_id = ?",
            (f" secret={self.secret_value}", self.node_id),
        )
        raw.commit()
        raw.close()
        self.env_patch = mock.patch.dict(os.environ, {"TEST_PLANTED_API_KEY": self.secret_value})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.ds = build_datasette(self.cfg, public=False)

    async def test_planted_secret_is_redacted_from_the_node_inspector(self):
        r = await self.ds.client.get(f"/observatory/api/node/{self.node_id}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.secret_value, r.text)
        self.assertIn("REDACTED:TEST_PLANTED_API_KEY", r.text)

    async def test_planted_secret_is_redacted_from_the_prompt_template_diff(self):
        raw = sqlite3.connect(self.db_path)
        raw.row_factory = sqlite3.Row
        call_id = raw.execute(
            "SELECT id FROM model_calls WHERE trace_node_id = ? ORDER BY id ASC LIMIT 1", (self.node_id,)
        ).fetchone()["id"]
        raw.close()
        r = await self.ds.client.get(f"/observatory/api/prompt-template?call={call_id}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.secret_value, r.text)

    async def test_planted_secret_is_redacted_from_a_borrowed_prompt(self):
        raw = sqlite3.connect(self.db_path)
        raw.row_factory = sqlite3.Row
        threshold_id = raw.execute(
            "SELECT t.id FROM trace_nodes t JOIN trace_edges e ON e.to_node_id = t.id "
            "WHERE t.node_type = 'threshold' AND e.from_node_id = ? LIMIT 1", (self.node_id,)
        ).fetchone()
        raw.close()
        if threshold_id is None:
            self.skipTest("planted node has no threshold neighbour in this fixture")
        r = await self.ds.client.get(f"/observatory/api/node/{threshold_id['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("related_model_calls", r.json())
        self.assertNotIn(self.secret_value, r.text)

    async def test_planted_secret_is_redacted_from_the_graph(self):
        r = await self.ds.client.get(
            f"/observatory/api/graph?entity_type=candidate_items&entity_id={self.item_id}"
        )
        self.assertNotIn(self.secret_value, r.text)


class ObservatoryThemeTokenTests(unittest.TestCase):
    """The design-token palette (observatory/frontend/src/tokens.css).

    Deliberately NOT gated on datasette, and deliberately here rather than in
    the vitest suite: this asserts against the stylesheet as a FILE, which is
    a stdlib-and-a-regex job, and the app tsconfig carries no node types to
    read a file with (Vite's `?raw` returns "" for a .css file, so there is no
    no-node way to do it on that side either).

    Two things are checked. First the cascade: a colour whose only definition
    lives inside a media query or a [data-theme] block is the classic
    wrong-theme-text bug -- the selector stops matching and the property falls
    back to inherit/initial rather than to the other theme -- so every token
    must exist on bare :root, and the theme blocks may only REDEFINE. Second
    the contrast, computed here rather than copied out of a design doc, so a
    hand-edit that drops a pair below WCAG AA fails the suite.
    """

    _HERE = os.path.dirname(os.path.abspath(__file__))
    CSS_PATH = os.path.join(_HERE, "observatory", "frontend", "src", "tokens.css")
    STYLES_PATH = os.path.join(_HERE, "observatory", "frontend", "src", "styles.css")
    INDEX_PATH = os.path.join(_HERE, "observatory", "frontend", "index.html")
    STATIC_DIR = os.path.join(_HERE, "observatory", "static")

    # WCAG 2.1: 4.5:1 for normal text, 3:1 for a non-text UI component or
    # graphical object.
    TEXT = 4.5
    UI = 3.0

    # Every (foreground, background) pair that actually renders together
    # somewhere in the Observatory -- status/severity colours and the chart
    # layer included, not just page chrome.
    PAIRS = [
        ("--fg", "--bg", TEXT), ("--fg", "--surface", TEXT), ("--fg", "--surface-raised", TEXT),
        ("--fg", "--hover", TEXT), ("--fg", "--accent-soft", TEXT),
        ("--fg-muted", "--bg", TEXT), ("--fg-muted", "--surface", TEXT),
        ("--fg-muted", "--hover", TEXT), ("--fg-muted", "--neutral-bg", TEXT),
        ("--fg-faint", "--bg", TEXT), ("--fg-faint", "--surface", TEXT),
        ("--accent", "--bg", TEXT), ("--accent", "--surface", TEXT),
        ("--accent", "--accent-soft", TEXT), ("--fg-on-accent", "--accent", TEXT),
        # status / severity: as text on the page, and as text on its own chip
        ("--ok", "--bg", TEXT), ("--ok", "--surface", TEXT), ("--ok", "--ok-bg", TEXT),
        ("--error", "--bg", TEXT), ("--error", "--surface", TEXT), ("--error", "--error-bg", TEXT),
        ("--active-text", "--bg", TEXT), ("--active-text", "--surface", TEXT),
        ("--active-text", "--active-bg", TEXT),
        ("--warn-text", "--bg", TEXT), ("--warn-text", "--surface", TEXT),
        ("--warn-text", "--warn-bg", TEXT),
        # status as a graphical object: the node card's left border stripe
        ("--ok", "--surface-raised", UI), ("--error", "--surface-raised", UI),
        ("--active", "--surface-raised", UI), ("--accent", "--surface-raised", UI),
        # grouped / derived nodes
        ("--group-fg", "--group-bg", TEXT), ("--group-label", "--group-bg", TEXT),
        ("--group-border", "--group-bg", UI),
        # JSON syntax highlighting, on the surface the viewer actually paints
        ("--syn-key", "--surface", TEXT), ("--syn-string", "--surface", TEXT),
        ("--syn-number", "--surface", TEXT), ("--syn-bool", "--surface", TEXT),
        # in-text search highlight
        ("--hit-fg", "--hit-bg", TEXT), ("--hit-active-fg", "--hit-active-bg", TEXT),
        # chart layer: swimlane tints are decorative fills, but text lands on them
        ("--lane-fg", "--lane-1", TEXT), ("--lane-fg", "--lane-2", TEXT),
        ("--lane-fg", "--lane-3", TEXT), ("--lane-fg", "--lane-4", TEXT),
        ("--lane-fg", "--lane-5", TEXT), ("--lane-fg", "--lane-6", TEXT),
        ("--lane-title", "--bg", TEXT), ("--edge-text", "--bg", TEXT),
        # graph chrome that has to stay visible as a graphical object
        ("--edge", "--bg", UI), ("--minimap-node", "--bg", UI), ("--border-strong", "--bg", UI),
    ]

    @classmethod
    def setUpClass(cls):
        cls.css = cls._read(cls.CSS_PATH)
        cls.index_html = cls._read(cls.INDEX_PATH)
        cls.light = cls._block(r'^:root \{(.*?)\n\}')
        cls.dark_media = cls._block(
            r'@media \(prefers-color-scheme: dark\) \{\s*'
            r':root:not\(\[data-theme="light"\]\) \{(.*?)\n  \}'
        )
        cls.dark_attr = cls._block(r'^:root\[data-theme="dark"\] \{(.*?)\n\}')

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    @classmethod
    def _block(cls, pattern):
        m = re.search(pattern, cls.css, re.S | re.M)
        assert m, "tokens.css: no block matching %s" % pattern
        return dict(re.findall(r'(--[a-z0-9-]+)\s*:\s*([^;]+);', m.group(1)))

    # -- colour maths (WCAG 2.1 relative luminance) ------------------------

    @staticmethod
    def _luminance(hexc):
        h = hexc.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        chans = []
        for i in (0, 2, 4):
            c = int(h[i:i + 2], 16) / 255
            chans.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]

    @classmethod
    def contrast(cls, a, b):
        la, lb = cls._luminance(a), cls._luminance(b)
        hi, lo = max(la, lb), min(la, lb)
        return (hi + 0.05) / (lo + 0.05)

    # -- cascade -----------------------------------------------------------

    def test_every_colour_is_defined_on_bare_root(self):
        # Nothing may have its ONLY definition inside a theme block.
        self.assertEqual(sorted(set(self.dark_attr) - set(self.light)), [])

    def test_every_light_token_is_redefined_for_dark(self):
        self.assertEqual(sorted(set(self.light) - set(self.dark_attr)), [])

    def test_the_two_dark_blocks_are_identical(self):
        # CSS cannot share them; this assertion is the only thing stopping the
        # media-query dark theme drifting from the explicit one.
        self.assertEqual(self.dark_media, self.dark_attr)

    def test_media_block_is_guarded_so_explicit_light_wins_on_a_dark_os(self):
        self.assertIn(
            '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"])',
            self.css,
        )

    def test_explicit_dark_block_comes_last_so_it_wins_on_a_light_os(self):
        self.assertGreater(
            self.css.index(':root[data-theme="dark"]'),
            self.css.index("@media (prefers-color-scheme: dark)"),
        )

    def test_color_scheme_is_set_in_all_three_states(self):
        # Without it the native controls -- checkboxes, scrollbars, the
        # viewer's search input -- stay light on a dark page.
        self.assertEqual(len(re.findall(r"color-scheme: light;", self.css)), 1)
        self.assertEqual(len(re.findall(r"color-scheme: dark;", self.css)), 2)

    def test_tokens_css_holds_no_component_rules(self):
        selectors = re.findall(r'^[ \t]*([^@{}\n][^{}\n]*)\{', self.css, re.M)
        self.assertTrue(all(s.strip().startswith(":root") for s in selectors), selectors)

    # -- contrast ----------------------------------------------------------

    def test_every_token_pair_meets_wcag_aa_in_both_themes(self):
        for label, tokens in (("light", self.light), ("dark", self.dark_attr)):
            for fg, bg, floor in self.PAIRS:
                with self.subTest(theme=label, fg=fg, bg=bg):
                    got = self.contrast(tokens[fg], tokens[bg])
                    self.assertGreaterEqual(
                        got, floor,
                        "%s: %s %s on %s %s = %.2f:1, needs %s:1"
                        % (label, fg, tokens[fg], bg, tokens[bg], got, floor),
                    )

    def test_the_three_light_theme_aa_failures_are_fixed(self):
        # Measured on the values this repo was actually shipping, against
        # #ffffff: --ok #2f9e44 was 3.45:1, the "active" amber #f0a500 was
        # 2.08:1 (below even the 3:1 non-text floor), and the muted text grey
        # #98a2b0 was 2.58:1. All three were live accessibility defects in the
        # LIGHT theme, independent of dark mode.
        self.assertLess(self.contrast("#2f9e44", "#ffffff"), self.TEXT)
        self.assertLess(self.contrast("#f0a500", "#ffffff"), self.UI)
        self.assertLess(self.contrast("#98a2b0", "#ffffff"), self.TEXT)
        self.assertGreaterEqual(self.contrast(self.light["--ok"], "#ffffff"), self.TEXT)
        self.assertGreaterEqual(self.contrast(self.light["--active-text"], "#ffffff"), self.TEXT)
        self.assertGreaterEqual(self.contrast(self.light["--fg-faint"], "#ffffff"), self.TEXT)

    def test_the_failing_literals_are_gone_from_the_stylesheet(self):
        styles = self._read(self.STYLES_PATH)
        for dead in ("#98a2b0", "#2f9e44", "#f0a500"):
            self.assertNotIn(dead, styles)

    def test_lane_tints_stay_decorative_rather_than_contrasty(self):
        # They group nodes; they never encode a value. A loud fill would
        # compete with the status colours that DO carry meaning.
        for tokens in (self.light, self.dark_attr):
            for i in range(1, 7):
                self.assertLess(self.contrast(tokens["--lane-%d" % i], tokens["--bg"]), 1.6)

    def test_edge_handles_stay_near_invisible_as_styles_css_intends(self):
        # pointer-events:none anchors that exist only so React Flow can draw an
        # edge: decoration under WCAG 1.4.11, so the requirement is the
        # opposite of the usual one.
        for tokens in (self.light, self.dark_attr):
            self.assertLess(self.contrast(tokens["--handle"], tokens["--surface-raised"]), 2.5)

    # -- the proof-of-concept surface --------------------------------------

    def test_the_monospace_viewer_surface_has_no_colour_literals_left(self):
        # PR K migrates exactly one surface end to end, as proof the token
        # system works; the rest is inventoried in THEME_MIGRATION.md. If a
        # literal reappears in this block, the proof stops being one.
        styles = self._read(self.STYLES_PATH)
        start = styles.index("/* --- monospace viewer ---")
        end = styles.index("/* --- compare ---", start)
        self.assertEqual(
            re.findall(r'#[0-9a-fA-F]{3,8}\b|rgba?\(', styles[start:end]), [],
            "the proof-of-concept surface must resolve every colour through a token",
        )

    # -- the no-flash bootstrap --------------------------------------------

    def test_bootstrap_script_matches_apply_theme_and_runs_before_the_bundle(self):
        self.assertIn('localStorage.getItem("observatory-theme")', self.index_html)
        self.assertIn('document.documentElement.setAttribute("data-theme", t)', self.index_html)
        # Before the module script, or it would not prevent the flash.
        self.assertLess(
            self.index_html.index("observatory-theme"),
            self.index_html.index('type="module"'),
        )
        # Never writes data-theme="system": that would leave
        # :root:not([data-theme="light"]) matching inside the media query and
        # pin a light-OS user to whatever the query happened to say.
        self.assertIn('t === "light" || t === "dark"', self.index_html)

    def test_built_bundle_carries_the_bootstrap_and_both_theme_blocks(self):
        # observatory/static/ is committed build output served by plugin.py, so
        # a stale build is the difference between the theme working and not.
        self.assertIn("observatory-theme", self._read(os.path.join(self.STATIC_DIR, "index.html")))
        built = self._read(os.path.join(self.STATIC_DIR, "assets", "index.css"))
        self.assertIn("prefers-color-scheme:dark", built.replace(" ", ""))
        # The minifier drops the attribute-value quotes, so match either form.
        self.assertRegex(built, r'\[data-theme=["\']?dark["\']?\]')
        # And the dark surface colour itself made it in, not merely a selector
        # that would have applied it.
        self.assertIn(self.dark_attr["--bg"].lower(), built.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
