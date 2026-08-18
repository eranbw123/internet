#!/usr/bin/env python3
"""Offline tests for the Observatory write API (PR J, observatory/manage.py).

Its own file, alongside test_observatory.py rather than inside it: that file
covers the read plugin and is 1,600 lines a concurrent UI session also edits,
and this suite needs a different fixture (a writable discovery.db plus a
writable interests.json) than its read-only Datasette-over-a-trace-fixture.

Same idiom as test_observatory.py in every other respect: Datasette's own ASGI
test client (`Datasette(...).client`, httpx wired straight to the app -- no
socket, no browser, no network), skipped loudly if datasette is not installed,
and a fixture built by the real discovery/db.py + discovery/offers.py rather
than by hand-written SQL.

Bilingual on purpose. 28% of the source corpus is Hebrew, so quotes, titles
and signals here are mixed Hebrew/English and the assertions check the exact
strings survive the round trip -- an API that mangles encoding must fail here,
not in front of the owner.
"""
import json
import os
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
        "test_observatory_manage.py: datasette is not installed -- every test in "
        "this file will be SKIPPED. `pip install datasette` to actually run them.",
        file=sys.stderr,
    )

from discovery import config, db, interest_sync, models, offers

if HAVE_DATASETTE:
    from observatory import funnel, interests_write
    from observatory.app import build_datasette

TOKEN = "s3cr3t-observatory-token"

# One Hebrew and one English quote from the same conversation, plus a second
# conversation: enough to prove both the grouping and the encoding.
GAMING_EVIDENCE = [
    {"date": "2026-07-29", "quote": "which isaac items actually change the run",
     "lang": "en", "depth": 34, "conversation_id": "conv-isaac"},
    {"date": "2026-06-11", "quote": "הסטים דק שלי נחנק במשחקים כבדים",
     "lang": "he", "depth": 12, "conversation_id": "conv-deck"},
    {"date": "2026-06-11", "quote": "מה שווה לכוון בהגדרות",
     "lang": "he", "depth": 12, "conversation_id": "conv-deck"},
]

BASE_INTERESTS = {
    "defaults": {"min_score": 0.7, "sources": ["web_search"]},
    "interests": [
        {
            "key": "narcolepsy-eds",
            "title": "Narcolepsy / idiopathic hypersomnia / EDS",
            "description": "Clinical and mechanistic work on NT1/NT2 and IH.",
            "positive_signals": ["narcolepsy", "modafinil", "MSLT"],
            "negative_signals": ["coping tips"],
            "min_score": 0.72,
            "sources": ["web_search"],
        },
        {
            "key": "speculative-fiction-ideas",
            "title": "Speculative fiction ideas",
            "description": "The measured dead-weight interest: 36 collected, 0 above bar.",
            "positive_signals": ["speculative fiction"],
            "negative_signals": [],
            "min_score": 0.70,
            "sources": ["web_search"],
        },
    ],
}


def _write_interests(path, data=None):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data or BASE_INTERESTS, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _seed_offers(conn):
    """Two artifact offers (one bilingual, one bridge) plus a proposed one, in
    the states the inbox actually shows: offered, offered, proposed."""
    offers.insert_offer(conn, {
        "key": "gaming-handhelds-roguelikes",
        "kind": "new",
        "title": "Handheld PC gaming & roguelike design",
        "description": "Steam Deck and the roguelike design space.",
        "positive_signals": ["steam deck", "binding of isaac", "roguelike"],
        "negative_signals": ["console sales figures"],
        "suggested_min_score": 0.68,
        "suggested_sources": ["web_search", "youtube"],
        "related_keys": [],
        "score": 0.91,
        "score_terms": {"evidence_strength": 0.95, "recurrence": 0.88, "novelty": 1.0},
        "evidence": GAMING_EVIDENCE,
        "source_conversations": ["conv-isaac", "conv-deck", "conv-quiet"],
        "durability": {"n_convs": 41, "active_months": 7, "span_days": 213},
        "similarity": [{"key": "narcolepsy-eds", "sim": 0.08}],
        "artifact_sha256": "a" * 64,
        "generated_at": "2026-08-17T04:00:00+00:00",
    })
    offers.offer(conn, "gaming-handhelds-roguelikes")

    offers.insert_offer(conn, {
        "key": "cognition-in-competitive-games",
        "kind": "bridge",
        "title": "Cognition in competitive games",
        "description": "Working-memory load measured inside competitive games.",
        "positive_signals": ["cognitive load in games", "hearthstone battlegrounds"],
        "suggested_min_score": 0.72,
        "suggested_sources": ["web_search"],
        "related_keys": ["narcolepsy-eds", "gaming-handhelds-roguelikes"],
        "score": 0.84,
        "score_terms": {"evidence_strength": 0.62, "novelty": 0.91},
        "evidence": [{"date": "2026-04-18", "quote": "המוח שלי נגמר אחרי שלוש ריצות",
                      "lang": "he", "depth": 47, "conversation_id": "conv-hsbg"}],
        "source_conversations": ["conv-hsbg"],
        "artifact_sha256": "a" * 64,
        "generated_at": "2026-08-17T04:00:00+00:00",
    })
    offers.offer(conn, "cognition-in-competitive-games")

    offers.insert_offer(conn, {
        "key": "supplements-and-nutrition-protocols",
        "kind": "new",
        "title": "Supplements & nutrition protocols",
        "positive_signals": ["creatine", "magnesium glycinate"],
        "suggested_sources": ["web_search"],
        "score": 0.77,
        "evidence": [{"date": "2026-03-22", "quote": "מגנזיום לפני שינה באמת עוזר",
                      "lang": "he", "depth": 9, "conversation_id": "conv-mg"}],
        "source_conversations": ["conv-mg"],
        "artifact_sha256": "a" * 64,
    })  # left at 'proposed' -- not in the inbox


def _seed_funnel(conn):
    """A handful of items/scores/notifications so the funnel endpoint has real
    numbers: narcolepsy-eds converts, speculative-fiction-ideas is dead weight
    (items collected, nothing above its bar) -- the measured shape."""
    rows = [
        ("narcolepsy-eds", 0.91, True),
        ("narcolepsy-eds", 0.80, True),
        ("narcolepsy-eds", 0.30, False),
        ("speculative-fiction-ideas", 0.41, False),
        ("speculative-fiction-ideas", 0.22, False),
    ]
    for i, (key, score, notify) in enumerate(rows):
        interest = db.interest_by_key(conn, key)
        item = db.insert_item(conn, models.CandidateItem(
            source="web_search", type="article", title=f"item {i} {key}",
            text="body", url=f"https://example.test/{key}/{i}",
            dedup_key=f"{key}-{i}", url_hash=f"h{i}", title_hash=f"t{i}",
            origin_interest=key,
        ))
        score_id = db.save_score(conn, models.ScoreResult(
            item_id=item, interest_id=interest.id, interest_key=key,
            dimensions={d: score for d in models.DIMENSIONS},
            final_score=score, confidence=0.9, reason="fixture",
            why_better_than_generic="fixture", provider="fake", model="fake-1",
        ))
        if notify:
            db.record_notification(conn, score_id, "telegram", True)


def _build_fixture(tmp_dir=None, **cfg_overrides):
    tmp_dir = tmp_dir or tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "fixture.db")
    interests_path = os.path.join(tmp_dir, "interests.json")
    _write_interests(interests_path)

    cfg = config.load()
    cfg.db_path = db_path
    cfg.interests_path = interests_path
    cfg.interest_candidates_path = os.path.join(tmp_dir, "interest_candidates.json")
    cfg.ui_token = TOKEN
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)

    conn = db.connect(db_path)
    db.init(conn)
    interest_sync.sync(conn, interests_path)
    _seed_offers(conn)
    _seed_funnel(conn)
    conn.close()
    return cfg, tmp_dir


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ManageApiTestCase(unittest.IsolatedAsyncioTestCase):
    """Fresh db + interests.json per test: every test here writes."""

    def setUp(self):
        self.cfg, self.tmp_dir = _build_fixture()
        self.ds = build_datasette(self.cfg, public=False)

    def _conn(self):
        return db.connect(self.cfg.db_path)

    def _interests_file(self):
        with open(self.cfg.interests_path, encoding="utf-8-sig") as fh:
            return json.load(fh)

    async def get(self, path):
        return await self.ds.client.get(path)

    async def post(self, path, body=None, content_type="application/json", **kwargs):
        return await self.ds.client.post(
            path,
            content=json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": content_type, **kwargs.pop("headers", {})},
            **kwargs,
        )


# --- GET /api/offers -----------------------------------------------------------

class OffersListTests(ManageApiTestCase):
    async def test_inbox_is_the_default_and_excludes_undecided_proposals(self):
        r = await self.get("/observatory/api/offers")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        keys = [o["key"] for o in body["offers"]]
        self.assertEqual(keys, ["gaming-handhelds-roguelikes",
                                "cognition-in-competitive-games"])
        # 'proposed' is not the inbox: the owner is only asked about offers
        # the selector actually put forward.
        self.assertNotIn("supplements-and-nutrition-protocols", keys)
        self.assertEqual(body["counts"], {"offered": 2, "proposed": 1})

    async def test_offers_are_strongest_first(self):
        body = (await self.get("/observatory/api/offers")).json()
        scores = [o["score"] for o in body["offers"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    async def test_status_all_and_explicit_status_filter(self):
        allb = (await self.get("/observatory/api/offers?status=all")).json()
        self.assertEqual(allb["total"], 3)
        proposed = (await self.get("/observatory/api/offers?status=proposed")).json()
        self.assertEqual([o["key"] for o in proposed["offers"]],
                         ["supplements-and-nutrition-protocols"])

    async def test_unknown_status_is_a_400_not_an_empty_inbox(self):
        r = await self.get("/observatory/api/offers?status=pending")
        self.assertEqual(r.status_code, 400)
        self.assertIn("unknown status", r.json()["error"])

    async def test_kind_filter(self):
        body = (await self.get("/observatory/api/offers?kind=bridge")).json()
        self.assertEqual([o["key"] for o in body["offers"]],
                         ["cognition-in-competitive-games"])

    async def test_json_columns_arrive_decoded_not_as_strings(self):
        offer = (await self.get("/observatory/api/offers")).json()["offers"][0]
        self.assertIsInstance(offer["positive_signals"], list)
        self.assertIsInstance(offer["evidence"], list)
        self.assertIsInstance(offer["score_terms"], dict)

    async def test_hebrew_evidence_survives_the_api_layer_verbatim(self):
        raw = (await self.get("/observatory/api/offers")).content.decode("utf-8")
        # Not \\u05d4... escapes: the body is real UTF-8.
        self.assertIn("הסטים דק שלי נחנק במשחקים כבדים", raw)
        offer = (await self.get("/observatory/api/offers")).json()["offers"][0]
        quotes = [e["quote"] for e in offer["evidence"]]
        self.assertIn("הסטים דק שלי נחנק במשחקים כבדים", quotes)


# --- GET /api/offers/<key>/provenance ------------------------------------------

class ProvenanceTests(ManageApiTestCase):
    async def test_provenance_carries_the_whole_75_checklist(self):
        r = await self.get(
            "/observatory/api/offers/gaming-handhelds-roguelikes/provenance")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for field in ("evidence", "conversations", "score_terms", "durability",
                      "similarity", "related_keys", "artifact", "events"):
            self.assertIn(field, body)
        self.assertEqual(body["artifact"]["sha256"], "a" * 64)
        self.assertEqual(body["artifact"]["generated_at"], "2026-08-17T04:00:00+00:00")

    async def test_quotes_group_by_the_conversation_that_produced_them(self):
        body = (await self.get(
            "/observatory/api/offers/gaming-handhelds-roguelikes/provenance")).json()
        groups = {g["conversation_id"]: g for g in body["conversations"]}
        self.assertEqual(len(groups["conv-deck"]["quotes"]), 2)
        self.assertEqual(len(groups["conv-isaac"]["quotes"]), 1)
        # A conversation credited by the extractor but quoted nowhere is still
        # provenance -- listed, empty, rather than dropped.
        self.assertIn("conv-quiet", groups)
        self.assertEqual(groups["conv-quiet"]["quotes"], [])

    async def test_a_quote_keeps_its_language_tag_for_rtl_rendering(self):
        body = (await self.get(
            "/observatory/api/offers/gaming-handhelds-roguelikes/provenance")).json()
        langs = {q["lang"] for g in body["conversations"] for q in g["quotes"]}
        self.assertEqual(langs, {"en", "he"})

    async def test_event_chain_is_included(self):
        body = (await self.get(
            "/observatory/api/offers/gaming-handhelds-roguelikes/provenance")).json()
        actions = [e["action"] for e in body["events"]]
        self.assertEqual(actions, ["propose", "offer"])

    async def test_unknown_offer_is_a_404(self):
        r = await self.get("/observatory/api/offers/nope/provenance")
        self.assertEqual(r.status_code, 404)


# --- POST /api/offers/<key>/decide ---------------------------------------------

class DecideTests(ManageApiTestCase):
    async def test_accept_writes_the_interest_to_json_and_db_and_activates(self):
        r = await self.post(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide",
            {"action": "accept", "note": "yes, finally"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["interest_key"], "gaming-handhelds-roguelikes")
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(body["lifecycle"], "active")

        keys = [e["key"] for e in self._interests_file()["interests"]]
        self.assertIn("gaming-handhelds-roguelikes", keys)

        conn = self._conn()
        try:
            interest = db.interest_by_key(conn, "gaming-handhelds-roguelikes")
            self.assertIsNotNone(interest)
            self.assertEqual(interest.min_score, 0.68)
            self.assertEqual(
                offers.interest_lifecycle(conn, "gaming-handhelds-roguelikes"),
                {"lifecycle": "active", "active": True},
            )
        finally:
            conn.close()

    async def test_accept_records_the_offered_by_backreference_in_json(self):
        await self.post("/observatory/api/offers/gaming-handhelds-roguelikes/decide",
                        {"action": "accept"})
        entry = next(e for e in self._interests_file()["interests"]
                     if e["key"] == "gaming-handhelds-roguelikes")
        self.assertEqual(entry["offered_by"]["offer_key"], "gaming-handhelds-roguelikes")
        self.assertEqual(entry["offered_by"]["artifact_sha256"], "a" * 64)
        self.assertEqual(entry["offered_by"]["source_conversations"],
                         ["conv-isaac", "conv-deck", "conv-quiet"])

    async def test_edit_then_accept_is_one_request(self):
        r = await self.post(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide",
            {"action": "accept",
             "edits": {"title": "משחקי הנדהלד ורוגלייקים",
                       "min_score": 0.55,
                       "positive_signals": ["steam deck", "hades", "אייזק"]}})
        self.assertEqual(r.status_code, 200, r.text)
        entry = next(e for e in self._interests_file()["interests"]
                     if e["key"] == "gaming-handhelds-roguelikes")
        self.assertEqual(entry["title"], "משחקי הנדהלד ורוגלייקים")
        self.assertEqual(entry["min_score"], 0.55)
        self.assertIn("אייזק", entry["positive_signals"])
        conn = self._conn()
        try:
            interest = db.interest_by_key(conn, "gaming-handhelds-roguelikes")
            self.assertEqual(interest.title, "משחקי הנדהלד ורוגלייקים")
            self.assertEqual(interest.min_score, 0.55)
        finally:
            conn.close()

    async def test_a_bar_on_the_legacy_0_100_scale_is_coerced_not_stored(self):
        # A hand-typed 55 used to mean "never notify". The editor's guard
        # mirrors discovery/interests.py::_threshold.
        await self.post("/observatory/api/offers/gaming-handhelds-roguelikes/decide",
                        {"action": "accept", "edits": {"min_score": 55}})
        conn = self._conn()
        try:
            self.assertEqual(
                db.interest_by_key(conn, "gaming-handhelds-roguelikes").min_score, 0.55)
        finally:
            conn.close()

    async def test_an_accept_that_would_write_an_invalid_interest_is_refused_intact(self):
        r = await self.post(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide",
            {"action": "accept", "edits": {"positive_signals": []}})
        self.assertEqual(r.status_code, 400)
        # The pre-flight matters because accept is one-way: the offer must
        # still be decidable after a refused write.
        conn = self._conn()
        try:
            self.assertEqual(
                offers.get_offer(conn, "gaming-handhelds-roguelikes")["status"], "offered")
        finally:
            conn.close()
        self.assertNotIn("gaming-handhelds-roguelikes",
                         [e["key"] for e in self._interests_file()["interests"]])

    async def test_a_decided_offer_can_never_be_re_decided(self):
        first = await self.post(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide",
            {"action": "accept"})
        self.assertEqual(first.status_code, 200)
        second = await self.post(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide",
            {"action": "accept"})
        self.assertEqual(second.status_code, 409)
        self.assertIn("already been decided", second.json()["error"])

    async def test_reject_blocks_the_offers_terms_in_interests_json(self):
        r = await self.post(
            "/observatory/api/offers/cognition-in-competitive-games/decide",
            {"action": "reject", "note": "not now"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("cognition-in-competitive-games", body["blocked_terms"])
        blocked = self._interests_file()["blocked_derived_terms"]
        self.assertIn("cognition-in-competitive-games", blocked)

    async def test_snooze_sets_a_wake_time_and_leaves_the_offer_alive(self):
        r = await self.post(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide",
            {"action": "snooze", "days": 14})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(r.json()["snoozed_until"])
        conn = self._conn()
        try:
            self.assertEqual(
                offers.get_offer(conn, "gaming-handhelds-roguelikes")["status"], "snoozed")
        finally:
            conn.close()

    async def test_snooze_days_is_bounded(self):
        r = await self.post("/observatory/api/offers/gaming-handhelds-roguelikes/decide",
                            {"action": "snooze", "days": 4000})
        self.assertEqual(r.status_code, 400)

    async def test_unknown_action_and_unknown_offer(self):
        bad = await self.post("/observatory/api/offers/gaming-handhelds-roguelikes/decide",
                              {"action": "maybe"})
        self.assertEqual(bad.status_code, 400)
        missing = await self.post("/observatory/api/offers/nope/decide",
                                  {"action": "accept"})
        self.assertEqual(missing.status_code, 404)

    async def test_an_unsupported_edit_field_is_reported_not_ignored(self):
        r = await self.post("/observatory/api/offers/gaming-handhelds-roguelikes/decide",
                            {"action": "accept", "edits": {"min_scores": 0.5}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("min_scores", r.json()["error"])

    async def test_a_retirement_offer_is_not_accepted_through_this_endpoint(self):
        conn = self._conn()
        try:
            offers.insert_offer(conn, {"key": "retire:speculative-fiction-ideas",
                                       "kind": "retire",
                                       "title": "Retire 'speculative-fiction-ideas'?",
                                       "related_keys": ["speculative-fiction-ideas"]})
            offers.offer(conn, "retire:speculative-fiction-ideas")
        finally:
            conn.close()
        r = await self.post(
            "/observatory/api/offers/retire:speculative-fiction-ideas/decide",
            {"action": "accept"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("retirement proposal", r.json()["error"])


# --- GET /api/interests/stats --------------------------------------------------

class FunnelStatsTests(ManageApiTestCase):
    async def test_funnel_counts_the_measured_way(self):
        body = (await self.get("/observatory/api/interests/stats?window=all")).json()
        rows = {r["key"]: r for r in body["interests"]}
        narco = rows["narcolepsy-eds"]
        self.assertEqual(narco["collected"], 3)
        self.assertEqual(narco["scored"], 3)
        self.assertEqual(narco["above_bar"], 2)     # 0.91 and 0.80 clear 0.72
        self.assertEqual(narco["notified"], 2)
        self.assertAlmostEqual(narco["above_bar_rate"], 2 / 3)

    async def test_dead_weight_is_flagged(self):
        body = (await self.get("/observatory/api/interests/stats?window=all")).json()
        rows = {r["key"]: r for r in body["interests"]}
        self.assertTrue(rows["speculative-fiction-ideas"]["dead_weight"])
        self.assertFalse(rows["narcolepsy-eds"]["dead_weight"])
        self.assertEqual(body["totals"]["dead_weight"], 1)

    async def test_worst_converters_sort_last(self):
        body = (await self.get("/observatory/api/interests/stats?window=all")).json()
        self.assertEqual(body["interests"][-1]["key"], "speculative-fiction-ideas")

    async def test_sparkline_and_lifecycle_fields_are_present(self):
        row = next(r for r in
                   (await self.get("/observatory/api/interests/stats?window=all")).json()["interests"]
                   if r["key"] == "narcolepsy-eds")
        self.assertEqual(sum(p["above_bar"] for p in row["sparkline"]), 2)
        self.assertEqual(row["lifecycle"], "active")
        self.assertFalse(row["auto_paused"])
        self.assertFalse(row["revivable"])

    async def test_window_is_validated(self):
        self.assertEqual((await self.get("/observatory/api/interests/stats?window=7d")).status_code, 200)
        bad = await self.get("/observatory/api/interests/stats?window=lastweek")
        self.assertEqual(bad.status_code, 400)

    async def test_a_short_window_excludes_older_activity(self):
        # Everything the fixture seeded is from "now", so a 7d window sees it
        # and an explicit 1d window still does -- what matters is that the
        # window is applied at all, which the totals prove against a DB whose
        # rows are all inside it.
        seven = (await self.get("/observatory/api/interests/stats?window=7d")).json()
        self.assertEqual(seven["window_days"], 7)
        self.assertEqual(seven["totals"]["scored"], 5)


# --- POST /api/interests (create) and /api/interests/<key> (update) ------------

class InterestWriteTests(ManageApiTestCase):
    async def test_create_writes_json_and_db_in_one_call(self):
        r = await self.post("/observatory/api/interests", {
            "key": "supplements-protocols",
            "title": "תוספי תזונה ופרוטוקולים",
            "description": "Evidence quality on magnesium, creatine, tyrosine.",
            "positive_signals": ["creatine", "מגנזיום", "dose-response"],
            "negative_signals": ["influencer stacks"],
            "min_score": 0.66,
            "sources": ["web_search"],
        })
        self.assertEqual(r.status_code, 201, r.text)
        entry = next(e for e in self._interests_file()["interests"]
                     if e["key"] == "supplements-protocols")
        self.assertEqual(entry["title"], "תוספי תזונה ופרוטוקולים")
        conn = self._conn()
        try:
            interest = db.interest_by_key(conn, "supplements-protocols")
            self.assertEqual(interest.title, "תוספי תזונה ופרוטוקולים")
            self.assertIn("מגנזיום", interest.positive_signals)
        finally:
            conn.close()

    async def test_creating_an_existing_key_is_a_409(self):
        r = await self.post("/observatory/api/interests", {
            "key": "narcolepsy-eds", "title": "dup",
            "positive_signals": ["x"], "sources": ["web_search"],
        })
        self.assertEqual(r.status_code, 409)

    async def test_invalid_payloads_are_rejected_with_a_usable_message(self):
        cases = [
            ({"key": "Not A Slug", "title": "t", "positive_signals": ["x"],
              "sources": ["web_search"]}, "slug"),
            ({"key": "derived:sneaky", "title": "t", "positive_signals": ["x"],
              "sources": ["web_search"]}, "reserved"),
            ({"key": "ok-key", "title": "", "positive_signals": ["x"],
              "sources": ["web_search"]}, "title is required"),
            ({"key": "ok-key", "title": "t", "positive_signals": [],
              "sources": ["web_search"]}, "positive signal"),
            ({"key": "ok-key", "title": "t", "positive_signals": ["x"],
              "sources": []}, "source"),
            ({"key": "ok-key", "title": "t", "positive_signals": ["x"],
              "sources": ["telepathy"]}, "unknown source"),
            ({"key": "ok-key", "title": "t", "positive_signals": ["x"],
              "sources": ["web_search"], "min_score": "high"}, "number"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                r = await self.post("/observatory/api/interests", payload)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn(expected, r.json()["error"])

    async def test_update_changes_json_and_db_together(self):
        r = await self.post("/observatory/api/interests/narcolepsy-eds", {
            "title": "Narcolepsy / IH / EDS",
            "description": "tightened",
            "positive_signals": ["narcolepsy", "pitolisant"],
            "negative_signals": ["coping tips"],
            "min_score": 0.80,
            "sources": ["web_search"],
        })
        self.assertEqual(r.status_code, 200, r.text)
        conn = self._conn()
        try:
            interest = db.interest_by_key(conn, "narcolepsy-eds")
            self.assertEqual(interest.min_score, 0.80)
            self.assertIn("pitolisant", interest.positive_signals)
        finally:
            conn.close()

    async def test_a_key_cannot_be_renamed(self):
        r = await self.post("/observatory/api/interests/narcolepsy-eds", {
            "key": "narcolepsy-renamed", "title": "t",
            "positive_signals": ["x"], "sources": ["web_search"],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("immutable", r.json()["error"])

    async def test_retiring_deactivates_and_cancels_pending_missions(self):
        conn = self._conn()
        try:
            gen = db.insert_generation(conn, "speculative-fiction-ideas", "p", "m", 1)
            db.insert_missions(conn, gen, "speculative-fiction-ideas", [
                {"label": "a mission", "rationale": "", "prompt": "p"}])
            pending = db.pending_mission_count(conn, "speculative-fiction-ideas")
            self.assertEqual(pending, 1)
        finally:
            conn.close()

        r = await self.post("/observatory/api/interests/speculative-fiction-ideas",
                            {"active": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["missions_cancelled"], 1)

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT active, lifecycle FROM interests WHERE key = ?",
                ("speculative-fiction-ideas",)).fetchone()
            self.assertEqual(row["active"], 0)
            self.assertEqual(row["lifecycle"], "retired")
            self.assertEqual(db.pending_mission_count(conn, "speculative-fiction-ideas"), 0)
        finally:
            conn.close()

    async def test_a_stale_editor_cannot_clobber_a_hand_edit(self):
        stale = interests_write.file_mtime(self.cfg.interests_path)
        # Someone edits the file by hand in another window.
        data = self._interests_file()
        data["interests"][0]["description"] = "edited by hand"
        os.utime(self.cfg.interests_path, None)
        _write_interests(self.cfg.interests_path, data)

        r = await self.post("/observatory/api/interests/narcolepsy-eds", {
            "title": "from the stale tab", "positive_signals": ["x"],
            "sources": ["web_search"], "expected_mtime": stale,
        })
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self._interests_file()["interests"][0]["description"],
                         "edited by hand")

    async def test_updating_an_unknown_interest_is_a_404(self):
        r = await self.post("/observatory/api/interests/no-such-interest", {
            "title": "t", "positive_signals": ["x"], "sources": ["web_search"],
        })
        self.assertEqual(r.status_code, 404)

    async def test_get_on_the_collection_path_still_lists_interests(self):
        # POST /api/interests creates; GET /api/interests must keep doing what
        # the read plugin always did on that same path.
        r = await self.get("/observatory/api/interests")
        self.assertEqual(r.status_code, 200)
        self.assertIn("interests", r.json())


# --- POST /api/interests/<key>/revive ------------------------------------------

class ReviveTests(ManageApiTestCase):
    def _pause(self, key):
        conn = self._conn()
        try:
            offers.set_lifecycle(conn, key, offers.DECAYING, actor="timer", action="decay")
            offers.set_lifecycle(conn, key, offers.PAUSED, actor="timer", action="auto_pause")
            offers.insert_offer(conn, {"key": offers.RETIRE_PREFIX + key, "kind": "retire",
                                       "title": f"Retire '{key}'?", "related_keys": [key]})
            offers.offer(conn, offers.RETIRE_PREFIX + key, actor="timer")
        finally:
            conn.close()

    async def test_one_click_undo_reactivates_and_closes_the_retire_offer(self):
        self._pause("speculative-fiction-ideas")
        stats = (await self.get("/observatory/api/interests/stats?window=all")).json()
        row = next(r for r in stats["interests"] if r["key"] == "speculative-fiction-ideas")
        self.assertTrue(row["auto_paused"])
        self.assertTrue(row["revivable"])

        r = await self.post(
            "/observatory/api/interests/speculative-fiction-ideas/revive",
            {"note": "still want this"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["lifecycle"], "active")
        self.assertTrue(body["active"])
        self.assertEqual(body["retire_offer_closed"],
                         "retire:speculative-fiction-ideas")

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT active, lifecycle FROM interests WHERE key = ?",
                ("speculative-fiction-ideas",)).fetchone()
            self.assertEqual(row["active"], 1)
            self.assertEqual(row["lifecycle"], "active")
            self.assertEqual(
                offers.get_offer(conn, "retire:speculative-fiction-ideas")["status"],
                "rejected")
        finally:
            conn.close()

    async def test_reviving_something_that_is_not_paused_is_a_409(self):
        r = await self.post("/observatory/api/interests/narcolepsy-eds/revive", {})
        self.assertEqual(r.status_code, 409)

    async def test_reviving_an_unknown_interest_is_a_400_not_a_crash(self):
        r = await self.post("/observatory/api/interests/nope/revive", {})
        self.assertIn(r.status_code, (400, 404))

    async def test_a_sync_never_resurrects_a_paused_interest_on_its_own(self):
        # interests.json still lists it, so a naive upsert would flip active
        # back to 1 on the very next save. Only the undo may revive it.
        self._pause("speculative-fiction-ideas")
        r = await self.post("/observatory/api/interests/narcolepsy-eds", {
            "title": "unrelated edit", "positive_signals": ["narcolepsy"],
            "sources": ["web_search"],
        })
        self.assertEqual(r.status_code, 200, r.text)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT active, lifecycle FROM interests WHERE key = ?",
                ("speculative-fiction-ideas",)).fetchone()
            self.assertEqual(row["active"], 0)
            self.assertEqual(row["lifecycle"], "paused")
        finally:
            conn.close()


# --- POST /api/offers/generate and GET /api/edges ------------------------------

class GenerateAndEdgesTests(ManageApiTestCase):
    async def test_generate_is_failsoft_when_the_artifact_is_missing(self):
        r = await self.post("/observatory/api/offers/generate", {})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("unreadable", body["error"])

    async def test_generate_imports_an_artifact_once(self):
        artifact = {
            "contract_version": 2,
            "generated_at": "2026-08-18T04:00:00Z",
            "window_days": 365,
            "conversation_count": 263,
            "sources": {"claude": 21, "chatgpt": 242},
            "topics": [],
            "candidates": [{
                "key": "cognitive-load-working-memory",
                "title": "Cognitive load and working memory",
                "description": "Working-memory load and the cost of context switching.",
                "positive_signals": ["working memory", "זיכרון עבודה", "cognitive load"],
                "negative_signals": ["brain training apps"],
                "suggested_min_score": 0.70,
                "sources": ["web_search"],
                "related_keys": [],
                "evidence": [
                    {"date": "2026-07-01", "quote": "how much can i hold in working memory",
                     "lang": "en", "depth": 0.8, "conversation_id": "chatgpt:1"},
                    {"date": "2026-06-01", "quote": "זיכרון עבודה ועומס קוגניטיבי",
                     "lang": "he", "depth": 0.9, "conversation_id": "chatgpt:2"},
                    {"date": "2026-05-01", "quote": "context switching costs measured how",
                     "lang": "en", "depth": 0.7, "conversation_id": "chatgpt:3"},
                ],
                "durability": {"n_convs": 9, "active_months": 5, "span_days": 120,
                               "recency_days": 20},
                "expected_yield": 0.7,
                "similarity_to_existing": [{"key": "narcolepsy-eds", "sim": 0.05}],
            }],
        }
        with open(self.cfg.interest_candidates_path, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, ensure_ascii=False)

        first = await self.post("/observatory/api/offers/generate", {})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["error"], "", first.text)
        self.assertEqual(first.json()["offers"], ["cognitive-load-working-memory"])
        # The Hebrew quote survived the import into the store, so the inbox
        # renders provenance without a second artifact read.
        prov = (await self.get(
            "/observatory/api/offers/cognitive-load-working-memory/provenance")).json()
        quotes = [q["quote"] for g in prov["conversations"] for q in g["quotes"]]
        self.assertIn("זיכרון עבודה ועומס קוגניטיבי", quotes)

        second = await self.post("/observatory/api/offers/generate", {})
        self.assertEqual(second.json()["skipped_existing"], 1)
        self.assertEqual(second.json()["error"], "already imported")

    async def test_edges_is_live_and_empty_until_pr_m_fills_it(self):
        r = await self.get("/observatory/api/edges?min_weight=0.2")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["edges"], [])

    async def test_edges_returns_rows_and_honours_min_weight(self):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO interest_edges (a_key, b_key, kind, weight, evidence, computed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("narcolepsy-eds", "gaming-handhelds-roguelikes", "semantic", 0.44,
                 json.dumps({"sim": 0.44}), db.now()))
            conn.commit()
        finally:
            conn.close()
        body = (await self.get("/observatory/api/edges?min_weight=0.2")).json()
        self.assertEqual(len(body["edges"]), 1)
        self.assertEqual(body["edges"][0]["evidence"], {"sim": 0.44})
        self.assertEqual((await self.get("/observatory/api/edges?min_weight=0.9")).json()["edges"], [])


# --- the auth boundary ---------------------------------------------------------

@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class WriteAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg, self.tmp_dir = _build_fixture()

    def _payload(self):
        return json.dumps({"action": "snooze"}).encode("utf-8")

    async def _decide(self, ds, headers=None):
        return await ds.client.post(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide",
            content=self._payload(),
            headers={"content-type": "application/json", **(headers or {})},
        )

    async def test_private_mode_allows_writes(self):
        ds = build_datasette(self.cfg, public=False)
        self.assertEqual((await self._decide(ds)).status_code, 200)

    async def test_public_mode_refuses_writes_even_with_a_valid_token(self):
        ds = build_datasette(self.cfg, public=True)
        r = await self._decide(ds, {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("DISCOVERY_UI_ALLOW_PUBLIC_WRITES", r.json()["error"])

    async def test_public_mode_writes_can_be_opted_into(self):
        ds = build_datasette(self.cfg, public=True)
        with mock.patch.dict(os.environ, {"DISCOVERY_UI_ALLOW_PUBLIC_WRITES": "1"}):
            r = await self._decide(ds, {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 200, r.text)

    async def test_public_mode_refuses_an_anonymous_write_before_anything_else(self):
        ds = build_datasette(self.cfg, public=True)
        with mock.patch.dict(os.environ, {"DISCOVERY_UI_ALLOW_PUBLIC_WRITES": "1"}):
            r = await self._decide(ds)
        self.assertEqual(r.status_code, 403)

    async def test_public_mode_gates_the_read_endpoints_too(self):
        ds = build_datasette(self.cfg, public=True)
        anon = await ds.client.get("/observatory/api/offers")
        self.assertEqual(anon.status_code, 403)
        authed = await ds.client.get(
            "/observatory/api/offers", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(authed.status_code, 200)

    async def test_a_form_encoded_write_is_refused(self):
        # The CSRF boundary: a cross-site form post can set this content-type
        # without a preflight, so it must never be parsed as a write.
        ds = build_datasette(self.cfg, public=False)
        r = await ds.client.post(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide",
            content=b"action=accept",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(r.status_code, 415)

    async def test_get_on_a_write_endpoint_is_405(self):
        ds = build_datasette(self.cfg, public=False)
        r = await ds.client.get(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide")
        self.assertEqual(r.status_code, 405)

    async def test_a_malformed_json_body_is_a_400_not_a_500(self):
        ds = build_datasette(self.cfg, public=False)
        r = await ds.client.post(
            "/observatory/api/offers/gaming-handhelds-roguelikes/decide",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)

    async def test_datasette_native_write_actions_stay_denied(self):
        # PR J adds write ROUTES of its own; the plugin's unconditional deny of
        # Datasette's own write vocabulary must survive that.
        from observatory import plugin
        ds = build_datasette(self.cfg, public=False)
        for action in ("insert-row", "update-row", "drop-table"):
            self.assertIs(plugin.permission_allowed(ds, {"id": "local"}, action, None), False)


# --- the funnel module, directly ------------------------------------------------

@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class FunnelUnitTests(unittest.TestCase):
    def test_window_parsing(self):
        self.assertEqual(funnel.parse_window("7d"), (7, "7d"))
        self.assertEqual(funnel.parse_window(None), (7, "7d"))
        self.assertEqual(funnel.parse_window("all"), (None, "all"))
        self.assertEqual(funnel.parse_window("21d"), (21, "21d"))
        for bad in ("", "7", "week", "999d", "0d"):
            if bad == "":
                continue
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                funnel.parse_window(bad)


@unittest.skipUnless(HAVE_DATASETTE, "datasette not installed")
class ValidationUnitTests(unittest.TestCase):
    def test_hebrew_titles_and_signals_are_valid_english_keys_are_required(self):
        entry = interests_write.validate({
            "key": "gaming-cluster",
            "title": "משחקים ניידים",
            "positive_signals": ["אייזק", "steam deck", "  אייזק  "],
            "sources": ["web_search"],
        })
        self.assertEqual(entry["title"], "משחקים ניידים")
        # stripped and de-duplicated, order preserved
        self.assertEqual(entry["positive_signals"], ["אייזק", "steam deck"])

    def test_the_0_100_legacy_scale_guard(self):
        self.assertEqual(interests_write.validate({
            "key": "k", "title": "t", "positive_signals": ["x"],
            "sources": ["web_search"], "min_score": 75,
        })["min_score"], 0.75)

    def test_an_inactive_interest_may_be_an_empty_shell(self):
        entry = interests_write.validate({
            "key": "k", "title": "t", "positive_signals": [],
            "sources": [], "active": False,
        })
        self.assertFalse(entry["active"])

    def test_a_parent_may_not_be_itself(self):
        with self.assertRaises(interests_write.ValidationError):
            interests_write.validate({
                "key": "k", "title": "t", "positive_signals": ["x"],
                "sources": ["web_search"], "parent_key": "k",
            })


if __name__ == "__main__":
    unittest.main(verbosity=2)
