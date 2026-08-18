#!/usr/bin/env python3
"""Offline tests for the discovery engine.

Same shape as test_watch.py: stdlib unittest, run with `python test_discovery.py`,
network fully stubbed. Nothing here touches an LLM API, Telegram, or Yahoo --
the pipeline holds an LLMProvider, so a fake object with `complete_json` /
`search_json` is the whole seam.
"""
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from discovery import (
    config,
    council,
    db,
    dedup,
    feedback_listener,
    health,
    interest_state,
    interest_sync,
    interests,
    matching,
    missions,
    models,
    normalize,
    notify,
    offer_learning,
    offers,
    personal_state,
    pipeline,
    providers,
    scoring,
    stats,
    teach,
    trace,
    trace_fixture,
)
from discovery.personal_state import PersonalState, PersonalStateError
from discovery.collectors import COLLECTORS, stocks, web_search, youtube
from discovery.models import CandidateItem, Interest, ScoreResult
from discovery.providers import (
    PROVIDERS, FallbackProvider, chatgpt_browser, claude_chat, get_provider,
)
from discovery.providers.anthropic_provider import AnthropicProvider
from discovery.providers.base import LLMProvider, ProviderError, UnsupportedCapability
from discovery.providers.openai_provider import OpenAIProvider

# ops/install_tasks.py is a flat script (like app/, not a package) run as
# `python ops/install_tasks.py`, so it's imported here the same way that
# invocation would resolve it: `ops/` on sys.path, then a bare import.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops"))
import install_tasks  # noqa: E402

# Same pattern for the engine-lab's flat scripts.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "experiments", "lab"))
import db_replay  # noqa: E402
import exp_connectors  # noqa: E402

CFG = config.Config(
    db_path=":memory:",
    interests_path="interests.json",
    provider="fake",
    model="fake-1",
    max_items_per_source=5,
    min_match_score=0.25,
    min_text_chars=40,
    telegram_bot_token="",
    telegram_chat_id="",
    # Pinned to the db.py module constants (rather than Config's own raised
    # defaults) so the retry-policy tests below stay exact regardless of
    # what config.load()'s production defaults are.
    send_max_attempts=db.MAX_SEND_ATTEMPTS,
    send_retry_seconds=db.RESEND_FAILED_AFTER_SECONDS,
)

BODY = (
    "A phase 2 trial of an orexin agonist reported a 9.1-minute gain on the "
    "maintenance of wakefulness test against placebo, with effect sizes per arm."
)


def an_interest(**kw):
    base = dict(
        key="k",
        title="Test interest",
        description="A description.",
        positive_signals=["good stuff"],
        negative_signals=["bad stuff"],
        min_score=0.70,
        sources=["web_search"],
        source_config={},
    )
    base.update(kw)
    return Interest(**base)


def an_item(**kw):
    base = dict(
        source="web_search",
        type="article",
        title="A title",
        url="https://e.com/a",
        text=BODY,
    )
    base.update(kw)
    return CandidateItem(**base)


def a_score(item_id, interest_id, final_score, interest_key="k"):
    return ScoreResult(
        item_id=item_id,
        interest_id=interest_id,
        interest_key=interest_key,
        dimensions={name: final_score for name in models.DIMENSIONS},
        final_score=final_score,
        confidence=0.9,
        reason="because",
        why_better_than_generic="",
        provider="fake",
        model="fake-1",
    )


def stored_item(conn, **kw):
    """Normalize then insert -- the hashes are NOT NULL, so nothing reaches the
    DB without going through normalize() first."""
    item = normalize.normalize(an_item(**kw))
    item.id = db.insert_item(conn, item)
    return item


class FakeProvider(LLMProvider):
    """Stands in for anthropic/openai. `scores` maps a substring of the item
    title to the value every dimension gets back; an Exception value raises.
    `dup_answers` is the same idea for the near-dup judge's prompts
    (recognised by NEAR_DUP_PROMPT's <already_stored> marker): substring ->
    the duplicate_of id to return, or an Exception; an unmatched judge prompt
    gets the null verdict rather than raising. Judge prompts land in
    `dedup_prompts`, apart from `prompts`, so existing len(prompts)
    assertions keep counting scoring spend only."""

    name = "fake"

    def __init__(self, scores=None, search_results=None, model="fake-1", dup_answers=None):
        super().__init__(model)
        self.scores = scores or {}
        self.search_results = search_results
        self.prompts = []
        self.search_prompts = []
        self.dup_answers = dup_answers or {}
        self.dedup_prompts = []

    def complete_json(self, system, prompt, schema, max_tokens=2000):
        if "<already_stored>" in prompt:
            return self._dedup_verdict(prompt)
        self.prompts.append(prompt)
        for needle, value in self.scores.items():
            if needle in prompt:
                if isinstance(value, Exception):
                    self._emit_call(None, 1, system, prompt, schema, None, None, None,
                                     "error", str(value), "t0", "t1")
                    raise value
                payload = self._payload(value)
                self._emit_call(None, 1, system, prompt, schema, None, json.dumps(payload),
                                 payload, "valid", None, "t0", "t1")
                return payload
        raise AssertionError(f"FakeProvider got an unexpected prompt:\n{prompt}")

    def _dedup_verdict(self, prompt):
        self.dedup_prompts.append(prompt)
        for needle, value in self.dup_answers.items():
            if needle in prompt:
                if isinstance(value, Exception):
                    raise value
                return {"duplicate_of": value, "reason": "same story"}
        return {"duplicate_of": None, "reason": "distinct"}

    def search_json(self, prompt, max_searches=5, max_tokens=8000):
        self.search_prompts.append(prompt)
        if self.search_results is None:
            raise UnsupportedCapability("fake provider has no search")
        self._emit_call(None, 1, None, prompt, None, None, json.dumps(self.search_results),
                         self.search_results, "array", None, "t0", "t1")
        return self.search_results

    @staticmethod
    def _payload(value, interest_key="k"):
        payload = {name: value for name in models.DIMENSIONS}
        payload.update(
            interest_key=interest_key,
            confidence=0.9,
            reason="Names a specific effect size.",
            why_better_than_generic="Has the per-arm numbers.",
        )
        return payload


class ModelsTests(unittest.TestCase):
    def test_weights_sum_to_one_so_all_ones_score_one(self):
        self.assertAlmostEqual(sum(models.WEIGHTS.values()), 1.0)
        dims = {name: 1.0 for name in models.DIMENSIONS}
        self.assertEqual(models.final_score(dims), 1.0)

    def test_specificity_is_stored_but_unweighted(self):
        dims = {name: 0.0 for name in models.DIMENSIONS}
        dims["specificity"] = 1.0
        self.assertEqual(models.final_score(dims), 0.0)
        self.assertIn("specificity", models.DIMENSIONS)
        self.assertNotIn("specificity", models.WEIGHTS)

    def test_missing_dimensions_count_as_zero_and_values_clamp(self):
        self.assertEqual(models.final_score({"personal_relevance": 1.0}), 0.35)
        self.assertEqual(models.final_score({name: 9 for name in models.WEIGHTS}), 1.0)
        self.assertEqual(models.final_score({name: -3 for name in models.WEIGHTS}), 0.0)
        self.assertEqual(models.clamp01("not a number"), 0.0)


class NormalizeTests(unittest.TestCase):
    def test_canonical_url_drops_tracking_and_sorts_the_rest(self):
        self.assertEqual(
            normalize.canonical_url("HTTPS://WWW.E.com/Post/?utm_source=x&b=2&a=1#frag"),
            "https://e.com/Post?a=1&b=2",
        )

    def test_canonical_url_collapses_the_variants_that_mean_one_article(self):
        forms = [
            "https://e.com/post",
            "https://www.e.com/post/",
            "http://e.com/post?fbclid=123",
        ]
        canon = {normalize.canonical_url(u) for u in forms}
        self.assertEqual(len(canon), 2)  # http vs https stay distinct on purpose
        self.assertEqual(normalize.canonical_url("https://www.e.com/post/"), "https://e.com/post")

    def test_normalize_fills_hashes_and_collapses_whitespace(self):
        item = normalize.normalize(an_item(title="  A   Title!  ", text=" body  text "))
        self.assertEqual(item.title, "A Title!")
        self.assertEqual(item.text, "body text")
        self.assertTrue(item.url_hash and item.title_hash and item.content_hash)
        self.assertEqual(item.dedup_key, item.url)

    def test_title_hash_ignores_case_and_punctuation(self):
        first = normalize.normalize(an_item(title="Orexin agonist hits endpoint"))
        second = normalize.normalize(an_item(title="Orexin Agonist Hits Endpoint!"))
        self.assertEqual(first.title_hash, second.title_hash)

    def test_bodyless_item_has_no_content_hash(self):
        self.assertIsNone(normalize.normalize(an_item(text="")).content_hash)

    def test_unrecognised_dates_are_dropped_rather_than_guessed(self):
        self.assertEqual(normalize.normalize_date("2026-08-07"), "2026-08-07")
        self.assertEqual(normalize.normalize_date("2026-08-07T10:30:00"), "2026-08-07T10:30:00")
        self.assertIsNone(normalize.normalize_date("last Tuesday"))
        self.assertIsNone(normalize.normalize_date(None))

    def test_origin_interest_is_recorded_but_not_overwritten(self):
        self.assertEqual(normalize.normalize(an_item(), "k").origin_interest, "k")
        item = an_item(origin_interest="already")
        self.assertEqual(normalize.normalize(item, "k").origin_interest, "already")


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def test_same_url_after_canonicalisation_is_a_duplicate(self):
        stored_item(self.conn, url="https://e.com/a")
        fresh = normalize.normalize(an_item(url="https://www.e.com/a/?utm_source=t"))
        found = dedup.find_duplicate(self.conn, fresh)
        self.assertEqual(found.reason, "same url")

    def test_same_title_at_a_different_url_is_a_duplicate(self):
        stored_item(self.conn, title="Orexin agonist hits endpoint")
        fresh = normalize.normalize(
            an_item(url="https://other.com/x", title="Orexin Agonist Hits Endpoint")
        )
        self.assertEqual(dedup.find_duplicate(self.conn, fresh).reason, "same title")

    def test_same_long_body_under_a_new_headline_is_a_duplicate(self):
        long_body = BODY * 3
        stored_item(self.conn, text=long_body)
        fresh = normalize.normalize(
            an_item(url="https://other.com/x", title="Rewritten headline", text=long_body)
        )
        self.assertEqual(dedup.find_duplicate(self.conn, fresh).reason, "same body text")

    def test_short_bodies_do_not_collide(self):
        short = "Same short line."
        self.assertLess(len(short), dedup.MIN_CONTENT_CHARS_FOR_HASH)
        stored_item(self.conn, text=short)
        fresh = normalize.normalize(
            an_item(url="https://other.com/x", title="Different", text=short)
        )
        self.assertIsNone(dedup.find_duplicate(self.conn, fresh))

    def test_an_item_is_not_its_own_duplicate(self):
        item = stored_item(self.conn)
        self.assertIsNone(dedup.find_duplicate(self.conn, item))

    def test_a_genuinely_new_item_is_not_a_duplicate(self):
        stored_item(self.conn)
        fresh = normalize.normalize(
            an_item(url="https://other.com/x", title="Different", text="Different body.")
        )
        self.assertIsNone(dedup.find_duplicate(self.conn, fresh))


class NearDupTests(unittest.TestCase):
    """The fourth dedup layer: free lexical suspects, one small judge call to
    confirm, and a confirmed repeat linked (duplicate_of) instead of scored."""

    VPG_A = dict(
        url="https://a.com/vpg", title="VPG stock plunges 25% after earnings miss",
        text="Vishay Precision Group (VPG) shares dropped 25% on Tuesday after the "
             "sensor maker reported quarterly revenue well below expectations.")
    VPG_B = dict(
        url="https://b.com/vishay", title="Vishay Precision Group falls by a quarter",
        text="Shares of Vishay Precision Group tumbled about 25% following an "
             "earnings miss, with revenue guidance cut for the year.")
    VPG_C = dict(
        url="https://c.com/sensors", title="Sensor maker VPG sinks 25% on weak guidance",
        text="VPG stock lost a quarter of its value on Tuesday after quarterly "
             "earnings missed estimates; the sensor maker also cut revenue guidance.")

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        db.upsert_interest(self.conn, an_interest())
        self.interests = db.active_interests(self.conn)

    def _ingest(self, provider, **kw):
        return pipeline.ingest(self.conn, provider, CFG, an_item(**kw), self.interests, "k")

    def test_the_same_story_retold_is_linked_and_never_scored(self):
        provider = FakeProvider(
            {"VPG stock plunges": 0.9},
            dup_answers={"falls by a quarter": 1, "Sensor maker VPG sinks": 1},
        )
        first = self._ingest(provider, **self.VPG_A)
        self.assertEqual(first.stage, "scored")
        self.assertEqual(provider.dedup_prompts, [])  # nothing stored to compare against

        second = self._ingest(provider, **self.VPG_B)
        third = self._ingest(provider, **self.VPG_C)
        self.assertEqual((second.stage, third.stage), ("near_duplicate", "near_duplicate"))
        self.assertIn("same story as #1", second.detail)
        self.assertEqual(len(provider.prompts), 1)       # only the first telling paid a score
        self.assertEqual(len(provider.dedup_prompts), 2)
        rows = self.conn.execute(
            "SELECT id, duplicate_of FROM candidate_items ORDER BY id").fetchall()
        self.assertEqual([(r["id"], r["duplicate_of"]) for r in rows],
                         [(1, None), (2, 1), (3, 1)])
        # The second repeat was compared against the ORIGINAL only -- a linked
        # item leaves the judge's pool, so chains always point at the first telling.
        self.assertNotIn("falls by a quarter", provider.dedup_prompts[1])

    def test_a_distinct_development_about_the_same_company_still_scores(self):
        provider = FakeProvider(
            {"VPG stock plunges": 0.9, "VPG names next chief executive": 0.8})
        self._ingest(provider, **self.VPG_A)
        outcome = self._ingest(
            provider, url="https://d.com/ceo",
            title="VPG names next chief executive",
            text="Vishay Precision Group appointed a new chief executive on Tuesday; "
                 "shares of the sensor maker were little changed after the announcement.")
        # Lexically suspicious (same company vocabulary), so the judge IS
        # consulted -- and its null verdict lets the item through to scoring.
        self.assertEqual(len(provider.dedup_prompts), 1)
        self.assertEqual(outcome.stage, "scored")

    def test_unrelated_stories_never_consult_the_judge(self):
        provider = FakeProvider({"VPG stock plunges": 0.9, "Orexin agonist": 0.8})
        self._ingest(provider, **self.VPG_A)
        outcome = self._ingest(provider, url="https://e.com/orexin",
                               title="Orexin agonist hits phase 2 endpoint", text=BODY)
        self.assertEqual(outcome.stage, "scored")
        self.assertEqual(provider.dedup_prompts, [])

    def test_a_judge_outage_repeats_a_story_rather_than_losing_one(self):
        provider = FakeProvider(
            {"VPG stock plunges": 0.9, "Vishay Precision Group falls": 0.85},
            dup_answers={"falls by a quarter": RuntimeError("provider down")},
        )
        self._ingest(provider, **self.VPG_A)
        outcome = self._ingest(provider, **self.VPG_B)
        self.assertEqual(outcome.stage, "scored")

    def test_a_made_up_judge_id_is_ignored(self):
        provider = FakeProvider(
            {"VPG stock plunges": 0.9, "Vishay Precision Group falls": 0.85},
            dup_answers={"falls by a quarter": 999},
        )
        self._ingest(provider, **self.VPG_A)
        outcome = self._ingest(provider, **self.VPG_B)
        self.assertEqual(outcome.stage, "scored")
        row = self.conn.execute(
            "SELECT duplicate_of FROM candidate_items WHERE id = 2").fetchone()
        self.assertIsNone(row["duplicate_of"])

    def test_a_shared_ticker_makes_a_suspect_even_with_disjoint_wording(self):
        # The stocks collector stamps metadata.ticker on the articles it
        # fetches to explain a move -- two explanations of the same move can
        # share almost no words and must still meet the judge.
        provider = FakeProvider(
            {"Earnings shock": 0.9},
            dup_answers={"Sharp fall follows results": 1},
        )
        first = self._ingest(
            provider, source="stocks", url="https://s.com/a",
            title="Earnings shock at a precision measurement group",
            text="The company reported a steep quarterly loss, sending investors to the exits.",
            metadata={"ticker": "VPG"})
        self.assertEqual(first.stage, "scored")
        second = self._ingest(
            provider, source="stocks", url="https://s.com/b",
            title="Sharp fall follows results",
            text="Traders reacted badly and the share price slid in heavy volume on Tuesday.",
            metadata={"ticker": "VPG"})
        self.assertEqual(second.stage, "near_duplicate")

    def test_a_linked_item_is_excluded_from_delivery(self):
        original = stored_item(self.conn, url="https://x.com/1", title="First telling")
        repeat = stored_item(self.conn, url="https://x.com/2", title="Second telling")
        interest = self.interests[0]
        db.save_score(self.conn, a_score(repeat.id, interest.id, 0.95))
        self.assertEqual(len(db.pending_notifications(self.conn)), 1)
        db.mark_near_duplicate(self.conn, repeat.id, original.id, "same story as #1")
        self.assertEqual(db.pending_notifications(self.conn), [])

    def test_the_backlog_rescorer_skips_linked_items(self):
        original = stored_item(self.conn, url="https://x.com/1", title="First telling")
        repeat = stored_item(self.conn, url="https://x.com/2", title="Second telling")
        db.set_prefilter(self.conn, repeat.id, True, "")
        db.mark_near_duplicate(self.conn, repeat.id, original.id, "same story as #1")
        provider = FakeProvider()
        scored = pipeline._score_backlog(
            self.conn, provider, self.interests, pipeline.Budget(5))
        self.assertEqual(scored["exploit"] + scored["explore"], 0)
        self.assertEqual(provider.prompts, [])

    # Frozen from production discovery.db items 11/173 and 22/174 -- two real
    # stories each delivered twice on 2026-08-10 because the exact title
    # hashes differ on a parenthetical suffix.
    NEBIUS_DEBT = "Nebius raises $775 million in first secured debt financing to accelerate global buildout"
    NEBIUS_META = "Nebius signs new AI infrastructure agreement with Meta (company newsroom)"
    NEBIUS_DEBT_REPEAT = NEBIUS_DEBT + " (Form 6-K, Ex. 99.1)"
    NEBIUS_META_REPEAT = "Nebius signs new AI infrastructure agreement with Meta (Form 6-K and press release)"

    def test_the_prod_double_sends_are_now_retrieved_as_suspects(self):
        stored_item(self.conn, url="https://n.com/debt", title=self.NEBIUS_DEBT, text="")
        stored_item(self.conn, url="https://n.com/meta", title=self.NEBIUS_META, text="")
        for title, expected_id in ((self.NEBIUS_DEBT_REPEAT, 1), (self.NEBIUS_META_REPEAT, 2)):
            fresh = normalize.normalize(
                an_item(url="https://other.com/x", title=title, text=""))
            suspects = dedup.find_suspects(self.conn, fresh, CFG)
            self.assertEqual([s["id"] for s in suspects], [expected_id])


class MatchingTests(unittest.TestCase):
    def test_ranks_interests_and_drops_the_ones_with_no_signal(self):
        sleep = an_interest(
            id=1, key="sleep", title="Narcolepsy research",
            positive_signals=["orexin agonist"], negative_signals=[],
        )
        stocks_interest = an_interest(
            id=2, key="stocks", title="Semiconductor earnings",
            positive_signals=["quarterly guidance"], negative_signals=[],
        )
        item = normalize.normalize(
            an_item(title="Orexin agonist trial", text="Narcolepsy patients improved.")
        )
        matches = matching.match_interests(item, [stocks_interest, sleep])
        self.assertEqual([i.key for i, _s, _t in matches], ["sleep"])
        self.assertGreater(matches[0][1], 0.5)

    def test_the_collecting_interest_always_matches_at_the_floor(self):
        interest = an_interest(id=1, key="k", title="Zzz", positive_signals=[])
        item = normalize.normalize(an_item(title="Nothing in common", text="Nor here."), "k")
        (matched, score, terms) = matching.match_interests(item, [interest])[0]
        self.assertEqual(score, matching.ORIGIN_MATCH_FLOOR)
        self.assertEqual(terms, ["(collected for this interest)"])

    def test_a_negative_signal_in_the_title_pulls_the_score_down(self):
        kw = dict(id=1, key="k", title="Sleep research", positive_signals=["sleep research"])
        item = normalize.normalize(an_item(title="Sleep research listicle", text="Sleep research."))
        clean = matching.match_interests(item, [an_interest(negative_signals=[], **kw)])[0][1]
        penalised = matching.match_interests(item, [an_interest(negative_signals=["listicle"], **kw)])[0][1]
        self.assertAlmostEqual(clean - penalised, matching.NEGATIVE_TITLE_PENALTY, places=3)

    def test_prefilter_accepts_a_solid_match(self):
        interest = an_interest(id=1)
        item = normalize.normalize(an_item(), "k")
        matches = matching.match_interests(item, [interest])
        ok, reason = matching.prefilter(item, matches, CFG)
        self.assertTrue(ok)
        self.assertIn("matched 'k'", reason)

    def test_prefilter_rejects_thin_missing_unmatched_and_weak_items(self):
        interest = an_interest(id=1)
        item = normalize.normalize(an_item(), "k")
        matches = matching.match_interests(item, [interest])

        thin = normalize.normalize(an_item(text="tiny"), "k")
        ok, reason = matching.prefilter(thin, matches, CFG)
        self.assertFalse(ok)
        self.assertIn("chars of text", reason)

        ok, reason = matching.prefilter(normalize.normalize(an_item(url="")), matches, CFG)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing title or url")

        ok, reason = matching.prefilter(item, [], CFG)
        self.assertFalse(ok)
        self.assertEqual(reason, "matched no interest")

        ok, reason = matching.prefilter(item, [(interest, 0.1, [])], CFG)
        self.assertFalse(ok)
        self.assertIn("weak match", reason)

    def test_short_form_sources_are_exempt_from_the_length_floor(self):
        interest = an_interest(id=1)
        move = normalize.normalize(an_item(source="stocks", title="NBIS +5%", text="x"), "k")
        matches = matching.match_interests(move, [interest])
        self.assertTrue(matching.prefilter(move, matches, CFG)[0])


class DBTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def test_init_is_idempotent(self):
        db.init(self.conn)  # would raise if the schema were not IF NOT EXISTS
        self.assertEqual(db.active_interests(self.conn), [])

    def test_interest_roundtrip_preserves_json_fields_and_threshold(self):
        db.upsert_interest(self.conn, an_interest(sources=["web_search", "stocks"]))
        (stored,) = db.active_interests(self.conn)
        self.assertEqual(stored.positive_signals, ["good stuff"])
        self.assertEqual(stored.sources, ["web_search", "stocks"])
        self.assertEqual(stored.min_score, 0.70)

    def test_upsert_updates_in_place(self):
        db.upsert_interest(self.conn, an_interest())
        db.upsert_interest(self.conn, an_interest(title="Renamed", min_score=0.9))
        stored = db.active_interests(self.conn)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].title, "Renamed")
        self.assertEqual(stored[0].min_score, 0.9)

    def test_insert_item_dedupes_on_source_and_key(self):
        first = stored_item(self.conn).id
        again = stored_item(self.conn, title="Same URL, new title").id
        self.assertEqual(first, again)
        # The same URL from a different collector is a different candidate.
        self.assertNotEqual(first, stored_item(self.conn, source="youtube").id)

    def test_scores_are_one_per_item_and_survive_a_roundtrip(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        item = stored_item(self.conn)
        db.save_score(self.conn, a_score(item.id, interest.id, 0.8))
        db.save_score(self.conn, a_score(item.id, interest.id, 0.4))
        rows = self.conn.execute("SELECT * FROM scores WHERE item_id = ?", (item.id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["final_score"], 0.4)
        self.assertAlmostEqual(rows[0]["specificity"], 0.4)
        self.assertTrue(db.is_scored(self.conn, item.id))

    def test_matches_are_saved_strongest_first(self):
        db.upsert_interest(self.conn, an_interest(key="a"))
        db.upsert_interest(self.conn, an_interest(key="b"))
        first, second = db.active_interests(self.conn)
        item = stored_item(self.conn)
        db.save_matches(self.conn, item.id, [(first, 0.3, ["x"]), (second, 0.9, ["y"])])
        self.assertEqual(db.matched_interest_ids(self.conn, item.id), [second.id, first.id])

    def test_prefilter_verdict_persists(self):
        item = stored_item(self.conn)
        db.set_prefilter(self.conn, item.id, False, "too thin")
        row = self.conn.execute(
            "SELECT prefilter_ok, prefilter_reason FROM candidate_items WHERE id = ?", (item.id,)
        ).fetchone()
        self.assertEqual((row["prefilter_ok"], row["prefilter_reason"]), (0, "too thin"))

    def test_pending_notifications_respects_the_bar_and_the_history(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        low = stored_item(self.conn, url="https://e.com/low").id
        high = stored_item(self.conn, url="https://e.com/high").id
        best = stored_item(self.conn, url="https://e.com/best").id
        for item_id, value in ((low, 0.40), (high, 0.88), (best, 0.95)):
            db.save_score(self.conn, a_score(item_id, interest.id, value))

        pending = db.pending_notifications(self.conn)
        self.assertEqual([r["item_id"] for r in pending], [best, high])

        db.record_notification(self.conn, pending[0]["score_id"], "telegram", True)
        self.assertEqual([r["item_id"] for r in db.pending_notifications(self.conn)], [high])

    def test_delete_score_clears_its_notification_too(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        item = stored_item(self.conn)
        score_id = db.save_score(self.conn, a_score(item.id, interest.id, 0.9))
        db.record_notification(self.conn, score_id, "telegram", True)
        db.delete_score(self.conn, item.id)
        self.assertFalse(db.is_scored(self.conn, item.id))
        self.assertEqual(self.conn.execute("SELECT count(*) c FROM notifications").fetchone()["c"], 0)

    def test_feedback_is_returned_newest_first(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        first = stored_item(self.conn, url="https://e.com/1", title="First").id
        second = stored_item(self.conn, url="https://e.com/2", title="Second").id
        db.add_feedback(self.conn, first, interest.id, "up")
        db.add_feedback(self.conn, second, interest.id, "down", "off topic")

        rows = db.recent_feedback(self.conn, interest.id)
        self.assertEqual([r["title"] for r in rows], ["Second", "First"])
        self.assertEqual(rows[0]["note"], "off topic")

    def test_feedback_stores_the_original_score(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        item = stored_item(self.conn)
        db.add_feedback(self.conn, item.id, interest.id, "fire", original_score=0.91)
        row = self.conn.execute(
            "SELECT verdict, original_score FROM feedback WHERE item_id = ?", (item.id,)
        ).fetchone()
        self.assertEqual((row["verdict"], row["original_score"]), ("fire", 0.91))

    def test_score_by_id_round_trips_what_a_feedback_button_needs(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        item = stored_item(self.conn)
        score_id = db.save_score(self.conn, a_score(item.id, interest.id, 0.8))
        row = db.score_by_id(self.conn, score_id)
        self.assertEqual((row["item_id"], row["interest_id"]), (item.id, interest.id))
        self.assertAlmostEqual(row["final_score"], 0.8)
        self.assertIsNone(db.score_by_id(self.conn, score_id + 999))

    def test_metrics_accumulate_per_day_and_ignore_zero_counts(self):
        db.bump(self.conn, {"collected": 3, "filtered": 1, "errors": 0})
        db.bump(self.conn, {"collected": 2})
        rows = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(rows, {"collected": 5, "filtered": 1})

    def test_seen_dedup_keys_filters_by_source_and_prefix(self):
        stored_item(self.conn, source="youtube", url="https://y/1", dedup_key="vid1:0-360")
        stored_item(self.conn, source="youtube", url="https://y/2", dedup_key="vid2:0-360")
        stored_item(self.conn, source="stocks", url="https://s/1", dedup_key="NBIS:2026-08-08")
        self.assertEqual(db.seen_dedup_keys(self.conn, "youtube", "vid1:"), {"vid1:0-360"})
        self.assertEqual(db.seen_dedup_keys(self.conn, "youtube", "nope:"), set())
        self.assertEqual(db.seen_dedup_keys(self.conn, "stocks"), {"NBIS:2026-08-08"})

    def test_seen_dedup_keys_treats_like_wildcards_in_the_prefix_literally(self):
        """YouTube ids contain underscores; '_' must not match any character."""
        stored_item(self.conn, source="youtube", url="https://y/3", dedup_key="abcXdef:0-360")
        self.assertEqual(db.seen_dedup_keys(self.conn, "youtube", "abc_def:"), set())
        stored_item(self.conn, source="youtube", url="https://y/4", dedup_key="abc_def:0-360")
        self.assertEqual(
            db.seen_dedup_keys(self.conn, "youtube", "abc_def:"), {"abc_def:0-360"}
        )

    def test_record_usage_sums_per_model_and_resets_the_provider(self):
        provider = FakeProvider(model="m1")
        provider.record_usage(input_tokens=100, output_tokens=10, web_searches=2)
        provider.record_usage(input_tokens=50, output_tokens=5)
        db.record_usage(self.conn, provider)
        self.assertEqual(provider.usage, {})   # drained, so a second flush is a no-op
        db.record_usage(self.conn, provider)

        row = self.conn.execute("SELECT * FROM llm_usage").fetchone()
        self.assertEqual(
            (row["model"], row["calls"], row["input_tokens"], row["output_tokens"],
             row["web_searches"]),
            ("m1", 2, 150, 15, 2),
        )

    def test_state_get_defaults_and_set_round_trips(self):
        self.assertIsNone(db.state_get(self.conn, "job:stocks:last_ok"))
        self.assertEqual(db.state_get(self.conn, "job:stocks:last_ok", "none yet"), "none yet")
        db.state_set(self.conn, "job:stocks:last_ok", db.now())
        stamp = db.state_get(self.conn, "job:stocks:last_ok")
        self.assertIsNotNone(stamp)
        db.state_set(self.conn, "job:stocks:last_ok", "later")
        self.assertEqual(db.state_get(self.conn, "job:stocks:last_ok"), "later")
        # One row per key, not one per write.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM service_state WHERE key = 'job:stocks:last_ok'"
            ).fetchone()["c"],
            1,
        )

    def test_today_counts_reads_back_metrics(self):
        db.bump(self.conn, {"run_ok": 2, "send_failed": 1})
        self.assertEqual(db.today_counts(self.conn), {"run_ok": 2, "send_failed": 1})

    def test_pending_notification_stats_and_abandoned_notifications(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        older = stored_item(self.conn, url="https://e.com/older").id
        newer = stored_item(self.conn, url="https://e.com/newer").id
        db.save_score(self.conn, a_score(older, interest.id, 0.9))
        db.save_score(self.conn, a_score(newer, interest.id, 0.9))

        count, oldest = db.pending_notification_stats(self.conn)
        self.assertEqual(count, 2)
        older_created = self.conn.execute(
            "SELECT created_at FROM scores WHERE item_id = ?", (older,)
        ).fetchone()["created_at"]
        self.assertEqual(oldest, older_created)
        self.assertEqual(db.abandoned_notifications(self.conn, max_attempts=3), 0)

        score_id = self.conn.execute(
            "SELECT id FROM scores WHERE item_id = ?", (older,)
        ).fetchone()["id"]
        for _ in range(3):
            db.record_notification(self.conn, score_id, "telegram", False)
        self.assertEqual(db.abandoned_notifications(self.conn, max_attempts=3), 1)
        # A live retry candidate (under the cap) is not "abandoned".
        self.assertEqual(db.abandoned_notifications(self.conn, max_attempts=10), 0)

    # --- layered interest state: owner immutability + provenance -----------

    def test_owner_row_survives_a_derived_write_attempt_by_key_collision(self):
        """Structurally impossible in production (interests.load_file()
        rejects an owner key carrying DERIVED_KEY_PREFIX), but guarded here
        too -- upsert_derived_interest() refuses a non-prefixed key outright."""
        db.upsert_interest(self.conn, an_interest(key="owner1"))
        with self.assertRaises(ValueError):
            db.upsert_derived_interest(
                self.conn, an_interest(key="owner1", layer="inferred"), {}
            )
        stored = db.interest_by_key(self.conn, "owner1")
        self.assertEqual(stored.layer, "owner")

    def test_set_interest_layer_refuses_an_owner_row(self):
        db.upsert_interest(self.conn, an_interest(key="owner1"))
        with self.assertRaises(db.OwnerInterestImmutable):
            db.set_interest_layer(self.conn, "owner1", "inferred", {})
        self.assertEqual(db.interest_by_key(self.conn, "owner1").layer, "owner")

    def test_set_interest_layer_refuses_a_key_that_does_not_exist(self):
        with self.assertRaises(db.OwnerInterestImmutable):
            db.set_interest_layer(self.conn, "derived:nope", "emerging", {})

    def test_the_owner_layer_trigger_aborts_a_raw_update(self):
        db.upsert_interest(self.conn, an_interest(key="owner1"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE interests SET layer = 'inferred' WHERE key = 'owner1'")
        self.assertEqual(db.interest_by_key(self.conn, "owner1").layer, "owner")

    def test_the_owner_delete_trigger_aborts_a_raw_delete(self):
        db.upsert_interest(self.conn, an_interest(key="owner1"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM interests WHERE key = 'owner1'")
        self.assertIsNotNone(db.interest_by_key(self.conn, "owner1"))

    def test_upsert_derived_interest_round_trips_layer_and_provenance(self):
        interest = an_interest(
            key="derived:gizmo", title="gizmo", positive_signals=["gizmo"], layer="exploratory"
        )
        db.upsert_derived_interest(self.conn, interest, {"source": "corpus", "term": "gizmo"})
        stored = db.interest_by_key(self.conn, "derived:gizmo")
        self.assertEqual(stored.layer, "exploratory")
        self.assertEqual(stored.provenance, {"source": "corpus", "term": "gizmo"})
        row = self.conn.execute(
            "SELECT active FROM interests WHERE key = 'derived:gizmo'"
        ).fetchone()
        self.assertEqual(row["active"], 0)   # exploratory never spends provider budget

    def test_upsert_derived_interest_inferred_is_active(self):
        interest = an_interest(key="derived:gizmo", layer="inferred", min_score=0.8)
        db.upsert_derived_interest(self.conn, interest, {})
        row = self.conn.execute(
            "SELECT active FROM interests WHERE key = 'derived:gizmo'"
        ).fetchone()
        self.assertEqual(row["active"], 1)

    def test_interest_events_is_append_only_and_ordered(self):
        db.add_interest_event(self.conn, "derived:x", "automation", "enter", None, "exploratory", {"observations": 1})
        db.add_interest_event(self.conn, "derived:x", "automation", "promote", "exploratory", "emerging", {"observations": 5})
        events = db.interest_events(self.conn, "derived:x")
        self.assertEqual([e["action"] for e in events], ["enter", "promote"])
        self.assertEqual(events[0]["to_layer"], "exploratory")
        self.assertEqual(events[1]["evidence"], {"observations": 5})
        self.assertEqual(db.interest_events(self.conn, "derived:nothing-here"), [])

    def test_list_interests_filters_by_layer_and_puts_owner_first(self):
        db.upsert_interest(self.conn, an_interest(key="owner1"))
        db.upsert_derived_interest(
            self.conn, an_interest(key="derived:x", layer="exploratory"), {}
        )
        keys = [r["key"] for r in db.list_interests(self.conn)]
        self.assertEqual(keys, ["owner1", "derived:x"])
        self.assertEqual(
            [r["key"] for r in db.list_interests(self.conn, layer="exploratory")], ["derived:x"]
        )
        self.assertEqual(db.list_interests(self.conn, layer="inferred"), [])


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        db.upsert_interest(self.conn, an_interest())
        (self.interest,) = db.active_interests(self.conn)

    def _notified(self, url, score, verdict=None):
        item = stored_item(self.conn, url=url)
        score_id = db.save_score(self.conn, a_score(item.id, self.interest.id, score))
        db.record_notification(self.conn, score_id, "telegram", True)
        if verdict:
            db.add_feedback(
                self.conn, item.id, self.interest.id, verdict, original_score=score
            )
        return item

    def test_empty_db_still_reports_rather_than_crashing(self):
        text = stats.report(self.conn, days=7)
        self.assertIn("candidates collected", text)
        self.assertIn("nothing rated yet", text)
        self.assertIn("no usage recorded", text)
        self.assertNotIn("MISSIONS", text)   # nothing in search_generations/search_missions yet

    def test_missions_section_shows_generation_and_queue_status(self):
        gen_id = db.insert_generation(self.conn, "k", "fake_mission", "m1", 2)
        db.insert_missions(self.conn, gen_id, "k", [
            {"label": "a", "rationale": "r", "prompt": "do a"},
            {"label": "b", "rationale": "r", "prompt": "do b"},
        ])
        db.finish_generation(self.conn, gen_id, "DONE", 2)
        bad_gen = db.insert_generation(self.conn, "k", "fake_mission", "m1", 1)
        db.finish_generation(self.conn, bad_gen, "FAILED", 0, "boom")
        mission_id = self.conn.execute(
            "SELECT id FROM search_missions WHERE label = 'a'"
        ).fetchone()["id"]
        db.lease_missions(self.conn, [mission_id], 900)
        db.finish_mission(self.conn, mission_id, 3)

        text = stats.report(self.conn, days=7)
        self.assertIn("MISSIONS (continuous web discovery)", text)
        self.assertIn("generations in window: done=1 failed=1", text)
        self.assertIn("missions (all time): pending=1 running=0 done=1 failed=0", text)

    def test_funnel_shows_survivors_and_what_reached_the_llm(self):
        db.bump(self.conn, {"collected": 100, "duplicate": 40, "filtered": 30,
                            "scored": 25, "errors": 5})
        lines = stats.report(self.conn, days=7).splitlines()
        survived = next(l for l in lines if l.startswith("survived cheap filtering"))
        to_llm = next(l for l in lines if l.startswith("sent to the LLM"))
        self.assertIn("30", survived)          # 100 - 40 - 30
        self.assertIn("30", to_llm)            # scored + errors, both were LLM calls
        self.assertIn("30.0%", to_llm)

    def test_feedback_rates_and_average_score_per_verdict(self):
        self._notified("https://e.com/1", 0.90, "fire")
        self._notified("https://e.com/2", 0.80, "fire")
        self._notified("https://e.com/3", 0.75, "up")
        self._notified("https://e.com/4", 0.71, "trash")
        text = stats.report(self.conn, days=7)

        self.assertIn("4 rated (100% of sent)", text)
        self.assertRegex(text, r"🔥 fire\s+2\s+50\.0%")
        self.assertRegex(text, r"negative \(👎 \+ 🗑\)\s+1\s+25\.0%")
        # The point of the section: loved items must outscore rejected ones.
        self.assertRegex(text, r"🔥 fire\s+0\.85\s+\(n=2\)")
        self.assertRegex(text, r"🗑 trash\s+0\.71\s+\(n=1\)")

    def test_notifications_per_interest(self):
        self._notified("https://e.com/1", 0.90)
        self._notified("https://e.com/2", 0.85)
        text = stats.report(self.conn, days=7)
        self.assertRegex(text, r"(?m)^k\s+2\s+2\s+100%")

    def test_cost_prices_known_models_and_flags_unknown_ones(self):
        provider = FakeProvider(model="claude-opus-5")
        provider.record_usage(input_tokens=1_000_000, output_tokens=200_000, web_searches=100)
        db.record_usage(self.conn, provider)
        text = stats.report(self.conn, days=7)
        # 1M in * $5 + 0.2M out * $25 = $10.00; 100 searches at $10/1k = $1.00
        self.assertIn("tokens $10.00 + web search $1.00 = $11.00", text)
        self.assertIn("TOTAL $11.00", text)

        unknown = FakeProvider(model="some-new-model")
        unknown.record_usage(input_tokens=500_000, output_tokens=100_000)
        db.record_usage(self.conn, unknown)
        text = stats.report(self.conn, days=7)
        self.assertIn("no list price on record", text)
        self.assertIn("excludes token spend for: some-new-model", text)

    def test_claude_chat_usage_reports_calls_but_never_a_dollar_figure(self):
        # claude_chat rides the claude.ai subscription: no token counts on the
        # wire, so a priced total would be fabricated -- even though its model
        # name has an API list price.
        chat = FakeProvider(model="claude-opus-5")
        chat.name = "claude_chat"
        for _ in range(3):
            chat.record_usage()
        db.record_usage(self.conn, chat)
        text = stats.report(self.conn, days=7)
        self.assertIn("3 calls via the claude.ai session", text)
        self.assertIn("dollar cost not applicable", text)
        self.assertNotIn("TOTAL", text)
        self.assertIn("no API-billed usage", text)

        # Mixed usage: the API-billed rows are totalled, claude_chat is not.
        api = FakeProvider(model="claude-opus-5")
        api.name = "anthropic"
        api.record_usage(input_tokens=1_000_000, output_tokens=200_000)
        db.record_usage(self.conn, api)
        text = stats.report(self.conn, days=7)
        self.assertIn("TOTAL $10.00", text)
        self.assertIn("3 calls via the claude.ai session", text)

    def test_report_without_cfg_omits_the_health_section(self):
        self.assertNotIn("HEALTH", stats.report(self.conn, days=7))

    def test_report_with_cfg_adds_a_health_section_without_checking_the_provider(self):
        text = stats.report(self.conn, days=7, cfg=CFG)
        self.assertIn("HEALTH", text)
        self.assertIn("provider: not checked", text)
        self.assertIn("overall: OK", text)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def test_job_name_for_source_maps_collector_names_onto_config_fields(self):
        self.assertEqual(health.job_name_for_source("web_search"), "web")
        self.assertEqual(health.job_name_for_source("stocks"), "stocks")
        self.assertEqual(health.job_name_for_source("youtube"), "youtube")
        self.assertEqual(health.job_name_for_source(None), "run-once")
        self.assertEqual(health.job_name_for_source("manual"), "manual")

    def test_check_is_not_degraded_before_any_job_has_ever_run(self):
        # A job never run is "unknown", not "stale" -- a fresh `init` must
        # not read as degraded just because nothing has fired yet.
        result = health.check(self.conn, CFG)
        self.assertFalse(result["degraded"])
        self.assertIsNone(result["provider_ok"])
        self.assertTrue(all(not j["stale"] for j in result["jobs"]))

    def test_check_flags_a_job_whose_last_ok_is_older_than_the_stale_threshold(self):
        cfg = dataclasses.replace(CFG, interval_stocks_seconds=100, health_stale_factor=3)
        db.state_set(self.conn, "job:stocks:last_ok", db.ago(301))
        result = health.check(self.conn, cfg)
        stocks = next(j for j in result["jobs"] if j["name"] == "stocks")
        self.assertTrue(stocks["stale"])
        self.assertTrue(result["degraded"])

    def test_check_reflects_a_down_provider(self):
        provider = FakeProvider()
        provider.preflight = lambda: (False, "no CDP endpoint")
        result = health.check(self.conn, CFG, provider)
        self.assertEqual(result["provider_ok"], False)
        self.assertEqual(result["provider_detail"], "no CDP endpoint")
        self.assertTrue(result["degraded"])

    def test_check_reports_abandoned_notifications_without_gating_degraded(self):
        # Abandoned sends are an expected artifact of an outage that already
        # passed (the retry policy is designed to eventually give up), and
        # there is no ack/clear path for them -- gating `degraded` on an
        # unbounded, all-time count would latch it forever the first time an
        # outage outlasts the retry window, so it must be reported but not
        # count towards degraded.
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        item = stored_item(self.conn)
        score_id = db.save_score(self.conn, a_score(item.id, interest.id, 0.9))
        for _ in range(CFG.send_max_attempts):
            db.record_notification(self.conn, score_id, "telegram", False)
        result = health.check(self.conn, CFG)
        self.assertEqual(result["abandoned"], 1)
        self.assertFalse(result["degraded"])

    def test_format_report_is_plain_text_and_mentions_every_job(self):
        text = health.format_report(health.check(self.conn, CFG))
        self.assertIn("HEALTH", text)
        for name in ("stocks", "web", "youtube", "digest", "feedback"):
            self.assertIn(name, text)

    def test_preflight_gate_passes_through_a_healthy_provider_without_side_effects(self):
        provider = FakeProvider()
        self.assertTrue(health.preflight_gate(self.conn, provider, CFG, "stocks"))
        self.assertEqual(db.today_counts(self.conn), {})
        self.assertIsNone(db.state_get(self.conn, "job:stocks:last_fail"))

    def test_preflight_gate_records_the_failure_and_returns_false(self):
        provider = FakeProvider()
        provider.preflight = lambda: (False, "no claude.ai tab")
        self.assertFalse(health.preflight_gate(self.conn, provider, CFG, "stocks"))
        self.assertEqual(db.today_counts(self.conn), {"provider_down": 1})
        self.assertIsNotNone(db.state_get(self.conn, "job:stocks:last_fail"))

    def test_preflight_gate_launches_chrome_once_and_rechecks(self):
        provider = FakeProvider()
        calls = [(False, "down"), (True, "")]
        provider.preflight = lambda: calls.pop(0)
        cfg = dataclasses.replace(
            CFG, chrome_launch_cmd="chrome.cmd", chrome_launch_wait_seconds=0
        )
        with mock.patch("discovery.health.subprocess.run") as run, \
             mock.patch("discovery.health.time.sleep") as sleep:
            self.assertTrue(health.preflight_gate(self.conn, provider, cfg, "stocks"))
        # The DEVNULL trio is load-bearing, not tidiness. Under Task Scheduler
        # this process's stdout is the job's log file, opened by ops/run.cmd
        # without FILE_SHARE_WRITE; a Chrome that inherits it holds that file
        # for its whole life, and every later run of the job then dies inside
        # cmd at the redirect -- silently, with the scheduler still reporting
        # success. That is what killed logs/web-tick-20260818.log at 14:44.
        import subprocess as _sp

        run.assert_called_once_with(
            ["cmd", "/d", "/c", "chrome.cmd"], check=False, timeout=0,
            stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        sleep.assert_called_once_with(0)
        self.assertEqual(db.today_counts(self.conn), {})  # recovered -- no provider_down

    def test_preflight_gate_survives_a_launch_command_that_never_returns(self):
        # A non-detached chrome_launch_cmd (still running Chrome itself when
        # the timeout hits) must not hang run-once forever.
        import subprocess

        provider = FakeProvider()
        provider.preflight = lambda: (False, "down")
        cfg = dataclasses.replace(
            CFG, chrome_launch_cmd="chrome.cmd", chrome_launch_wait_seconds=0
        )
        with mock.patch(
            "discovery.health.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="chrome.cmd", timeout=0),
        ), mock.patch("discovery.health.time.sleep"):
            self.assertFalse(health.preflight_gate(self.conn, provider, cfg, "stocks"))
        self.assertEqual(db.today_counts(self.conn), {"provider_down": 1})

    def test_notify_if_needed_sends_once_while_degraded_then_respects_cooldown(self):
        cfg = dataclasses.replace(CFG, health_alert_cooldown_seconds=3600)
        result = {"degraded": True, "jobs": [], "provider_ok": False, "provider_detail": "down",
                  "pending_count": 0, "pending_oldest": None, "abandoned": 0, "counters": {}}
        with mock.patch.object(notify, "send", return_value=True) as send:
            health.notify_if_needed(self.conn, cfg, result)
        send.assert_called_once()
        self.assertEqual(db.state_get(self.conn, "health:last_status"), "degraded")

        with mock.patch.object(notify, "send", return_value=True) as send:
            health.notify_if_needed(self.conn, cfg, result)
        send.assert_not_called()  # still within the cooldown

    def test_notify_if_needed_sends_exactly_one_recovery_message_on_transition(self):
        db.state_set(self.conn, "health:last_status", "degraded")
        ok_result = {"degraded": False, "jobs": [], "provider_ok": True, "provider_detail": "",
                     "pending_count": 0, "pending_oldest": None, "abandoned": 0, "counters": {}}
        with mock.patch.object(notify, "send", return_value=True) as send:
            health.notify_if_needed(self.conn, CFG, ok_result)
        send.assert_called_once()
        self.assertIn("recovered", send.call_args.args[1])
        self.assertEqual(db.state_get(self.conn, "health:last_status"), "ok")

        with mock.patch.object(notify, "send", return_value=True) as send:
            health.notify_if_needed(self.conn, CFG, ok_result)
        send.assert_not_called()  # already ok -- no repeat recovery message


class InterestsFileTests(unittest.TestCase):
    def test_sample_file_loads_with_defaults_applied(self):
        loaded = interests.load_file("interests.json")
        keys = [i.key for i in loaded]
        # Anchor on the two interests other tests and collectors lean on rather
        # than the full list -- interests.json is real user config and changes.
        self.assertIn("narcolepsy-eds", keys)
        self.assertIn("nbis-nebius", keys)
        nbis = loaded[keys.index("nbis-nebius")]
        self.assertIn("stocks", nbis.sources)
        self.assertEqual(nbis.source_config["stocks"]["tickers"][0]["ticker"], "NBIS")
        youtube_cfg = loaded[keys.index("narcolepsy-eds")].source_config["youtube"]
        self.assertIn("max_transcript_fetches", youtube_cfg)
        self.assertTrue(youtube_cfg.get("queries"))  # owner hints the collector feeds the prompt

    def test_sample_file_thresholds_are_on_the_0_1_scale(self):
        for interest in interests.load_file("interests.json"):
            self.assertLessEqual(interest.min_score, 1.0, interest.key)
            self.assertGreater(interest.min_score, 0.0, interest.key)

    def test_defaults_fill_in_missing_fields(self):
        (loaded,) = self._load({
            "defaults": {"min_score": 0.6, "sources": ["web_search"]},
            "interests": [{"key": "x", "title": "X"}],
        })
        self.assertEqual(loaded.min_score, 0.6)
        self.assertEqual(loaded.sources, ["web_search"])

    def test_an_old_0_100_threshold_is_rescaled_rather_than_silencing_pushes(self):
        (loaded,) = self._load({
            "defaults": {"min_score": 70},
            "interests": [{"key": "x", "title": "X", "min_score": 75}],
        })
        self.assertEqual(loaded.min_score, 0.75)
        (from_defaults,) = self._load({
            "defaults": {"min_score": 70},
            "interests": [{"key": "x", "title": "X"}],
        })
        self.assertEqual(from_defaults.min_score, 0.70)

    def test_a_bom_from_a_windows_editor_does_not_break_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "i.json")
            with open(path, "w", encoding="utf-8-sig") as fh:
                json.dump({"interests": [{"key": "x", "title": "X"}]}, fh)
            (loaded,) = interests.load_file(path)
        self.assertEqual(loaded.key, "x")

    def _load(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "i.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            return interests.load_file(path)

    def test_personal_state_top_terms_are_appended_when_opted_in(self):
        state = PersonalState(
            contract_version=1,
            generated_at="2026-08-10T00:00:00Z",
            topics=[
                {"key": "orexin", "weight": 1.0},
                {"key": "wakefulness", "weight": 0.5},
                {"key": "narcolepsy", "weight": 0.3},
            ],
        )
        data = {
            "interests": [{
                "key": "x", "title": "X",
                "positive_signals": ["orexin", "existing"],
                "personal_state_top_terms": 2,
            }]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "i.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            (with_state,) = interests.load_file(path, state=state)
            (without_key,) = interests.load_file(path, state=None)
        # Existing signals first, order stable, de-duplicated against "orexin".
        self.assertEqual(with_state.positive_signals, ["orexin", "existing", "wakefulness"])
        # No state loaded -> the key is inert, byte-identical to no state at all.
        self.assertEqual(without_key.positive_signals, ["orexin", "existing"])

    def test_without_the_opt_in_key_behavior_is_unchanged_by_state(self):
        state = PersonalState(
            contract_version=1, generated_at="2026-08-10T00:00:00Z",
            topics=[{"key": "orexin", "weight": 1.0}],
        )
        data = {"interests": [{"key": "x", "title": "X", "positive_signals": ["a"]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "i.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            with_state = interests.load_file(path, state=state)
            no_state = interests.load_file(path, state=None)
            plain = interests.load_file(path)
        self.assertEqual(with_state, no_state)
        self.assertEqual(with_state, plain)

    def test_an_owner_key_carrying_the_derived_prefix_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._load({"interests": [{"key": "derived:x", "title": "X"}]})
        self.assertIn("derived:x", str(ctx.exception))

    def test_load_blocked_reads_the_optional_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "i.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"interests": [], "blocked_derived_terms": ["spam", "clickbait"]}, fh)
            self.assertEqual(interests.load_blocked(path), ["spam", "clickbait"])

    def test_load_blocked_defaults_to_empty_list_when_absent(self):
        self.assertEqual(interests.load_blocked("interests.json"), [])


class PersonalStateTests(unittest.TestCase):
    def _write(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "personal_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def _artifact(self, **overrides):
        base = {
            "contract_version": 1,
            "generated_at": "2026-08-10T00:00:00Z",
            "window_days": 180,
            "conversation_count": 12,
            "sources": {"claude": 8, "chatgpt": 4},
            "topics": [
                {"key": "orexin", "weight": 1.0, "conversations": 5, "last_seen": "2026-08-09T00:00:00Z"},
                {"key": "wakefulness", "weight": 0.6, "conversations": 3, "last_seen": "2026-08-08T00:00:00Z"},
                {"key": "narcolepsy", "weight": 0.4, "conversations": 2, "last_seen": "2026-08-01T00:00:00Z"},
            ],
        }
        base.update(overrides)
        return base

    def test_valid_v1_artifact_loads_and_top_terms_are_in_order(self):
        path = self._write(self._artifact())
        state = personal_state.load(path)
        self.assertEqual(state.contract_version, 1)
        self.assertEqual(state.top_terms(2), ["orexin", "wakefulness"])
        self.assertEqual(state.top_terms(10), ["orexin", "wakefulness", "narcolepsy"])

    def test_an_unsupported_version_names_found_and_supported_in_the_message(self):
        # 2 is supported since the offers store (contract v2); 99 stands in for
        # "a version this reader has never heard of".
        path = self._write(self._artifact(contract_version=99))
        with self.assertRaises(PersonalStateError) as ctx:
            personal_state.load(path)
        message = str(ctx.exception)
        self.assertIn("99", message)
        self.assertIn(str(sorted(personal_state.SUPPORTED_VERSIONS)), message)
        # load_optional never raises -- the fail-soft wrapper the pipeline uses.
        self.assertIsNone(personal_state.load_optional(path))

    def test_forward_compat_ignores_unknown_top_level_and_per_topic_keys(self):
        data = self._artifact(some_future_field="ignored")
        data["topics"][0]["some_future_topic_field"] = "ignored"
        path = self._write(data)
        state = personal_state.load(path)
        self.assertEqual(state.top_terms(1), ["orexin"])

    def test_malformed_json_raises_and_load_optional_returns_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(PersonalStateError):
            personal_state.load(path)
        self.assertIsNone(personal_state.load_optional(path))

    def test_missing_file_raises_and_load_optional_returns_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "does-not-exist.json")
        with self.assertRaises(PersonalStateError):
            personal_state.load(path)
        self.assertIsNone(personal_state.load_optional(path))

    def test_a_non_dict_artifact_raises_and_load_optional_returns_none(self):
        # A truncated/zeroed producer write (`null`, a bare number, ...) must
        # not escape as a raw TypeError -- `"contract_version" not in data`
        # only makes sense once `data` is known to be a mapping.
        for bad in (None, 5, [1, 2]):
            path = self._write(bad)
            with self.assertRaises(PersonalStateError):
                personal_state.load(path)
            self.assertIsNone(personal_state.load_optional(path))

    def test_malformed_topics_raise_instead_of_failing_later_at_top_terms(self):
        # `topics` not a list, and a topic missing its `key`, must be caught
        # in load() itself -- interests.load_file calls top_terms() after
        # load_optional() has already returned, outside any fail-soft guard.
        not_a_list = self._write(self._artifact(topics={"a": 1}))
        with self.assertRaises(PersonalStateError):
            personal_state.load(not_a_list)
        self.assertIsNone(personal_state.load_optional(not_a_list))

        missing_key = self._write(self._artifact(topics=[{"weight": 1}]))
        with self.assertRaises(PersonalStateError):
            personal_state.load(missing_key)
        self.assertIsNone(personal_state.load_optional(missing_key))


NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
STALE = (NOW - timedelta(days=40)).isoformat(timespec="seconds")


class InterestStateDecideTests(unittest.TestCase):
    """decide() is pure -- no DB, no clock of its own -- so the whole ladder
    is exercised as a table, current_layer/evidence/blocked in, Transition
    (or None) out."""

    RULES = interest_state.Rules()

    CASES = [
        ("absent, never observed -> stays absent",
         None, dict(observations=0), False, None),
        ("absent, observed once -> enters exploratory",
         None, dict(observations=1, last_seen=FRESH), False, ("exploratory", "enter")),
        ("absent, blocked -> never enters even though observed",
         None, dict(observations=9, last_seen=FRESH), True, None),
        ("exploratory, below the observation bar -> stays",
         "exploratory", dict(observations=4, distinct_days=3, last_seen=FRESH), False, None),
        ("exploratory, below the distinct-day bar -> stays",
         "exploratory", dict(observations=5, distinct_days=2, last_seen=FRESH), False, None),
        ("exploratory, both bars cleared -> promotes to emerging",
         "exploratory", dict(observations=5, distinct_days=3, last_seen=FRESH), False,
         ("emerging", "promote")),
        ("emerging, feedback bar not cleared -> stays",
         "emerging", dict(observations=5, distinct_days=3, positive_feedback=1,
                           negative_feedback=0, last_seen=FRESH), False, None),
        ("emerging, feedback tied (not strictly positive) -> stays",
         "emerging", dict(observations=5, distinct_days=3, positive_feedback=2,
                           negative_feedback=2, last_seen=FRESH), False, None),
        ("emerging, observation bar regressed even with feedback -> stays",
         "emerging", dict(observations=4, distinct_days=3, positive_feedback=5,
                           negative_feedback=0, last_seen=FRESH), False, None),
        ("emerging, every bar cleared -> promotes to inferred",
         "emerging", dict(observations=5, distinct_days=3, positive_feedback=2,
                           negative_feedback=0, last_seen=FRESH), False,
         ("inferred", "promote")),
        ("inferred, nothing above it and still fresh -> stays",
         "inferred", dict(observations=5, distinct_days=3, last_seen=FRESH), False, None),
        ("inferred, idle -> decays one rung to emerging",
         "inferred", dict(last_seen=STALE), False, ("emerging", "decay")),
        ("emerging, idle -> decays one rung to exploratory",
         "emerging", dict(last_seen=STALE), False, ("exploratory", "decay")),
        ("exploratory, idle -> decays to retired",
         "exploratory", dict(last_seen=STALE), False, ("retired", "decay")),
        ("exploratory, never actually observed this evidence -> treated as idle",
         "exploratory", dict(last_seen=None), False, ("retired", "decay")),
        ("exploratory, negative feedback dominates -> immediate retire",
         "exploratory", dict(negative_feedback=3, positive_feedback=0, last_seen=FRESH), False,
         ("retired", "retire_negative_feedback")),
        ("inferred, negative feedback dominates -> immediate retire even though active",
         "inferred", dict(negative_feedback=3, positive_feedback=1, last_seen=FRESH), False,
         ("retired", "retire_negative_feedback")),
        ("inferred, negative feedback at the bar but not dominant -> stays",
         "inferred", dict(negative_feedback=3, positive_feedback=3, last_seen=FRESH), False, None),
        ("retired, re-entry evidence below the anti-flap multiplier -> stays retired",
         "retired", dict(observations=9), False, None),
        ("retired, re-entry evidence clears the multiplier -> re-enters at exploratory",
         "retired", dict(observations=10), False, ("exploratory", "reentry")),
        ("exploratory, blocked -> retired",
         "exploratory", dict(last_seen=FRESH), True, ("retired", "retire_blocked")),
        ("retired, blocked -> stays retired, no duplicate event",
         "retired", dict(observations=100), True, None),
    ]

    def test_the_ladder(self):
        for name, current_layer, evidence_kwargs, blocked, expected in self.CASES:
            with self.subTest(name):
                evidence = interest_state.Evidence(**evidence_kwargs)
                transition = interest_state.decide(current_layer, evidence, self.RULES, NOW, blocked)
                if expected is None:
                    self.assertIsNone(transition, name)
                else:
                    to_layer, action = expected
                    self.assertEqual((transition.to_layer, transition.action), (to_layer, action), name)
                    self.assertEqual(transition.from_layer, current_layer)

    def test_decide_never_emits_a_transition_into_owner(self):
        """Never any transition to OWNER, from any state, ever."""
        evidences = [
            interest_state.Evidence(),
            interest_state.Evidence(observations=100, distinct_days=50, last_seen=FRESH),
            interest_state.Evidence(positive_feedback=10, negative_feedback=0, last_seen=FRESH),
            interest_state.Evidence(negative_feedback=10, positive_feedback=0, last_seen=FRESH),
            interest_state.Evidence(last_seen=STALE),
        ]
        for current_layer in (None, "exploratory", "emerging", "inferred", "retired"):
            for evidence in evidences:
                for blocked in (False, True):
                    transition = interest_state.decide(current_layer, evidence, self.RULES, NOW, blocked)
                    if transition is not None:
                        self.assertNotEqual(transition.to_layer, "owner")

    def test_personal_state_seed_evidence_can_never_promote_on_its_own(self):
        """Zero observations (what a seed transition's Evidence carries) can
        never clear the exploratory -> emerging bar by itself -- it may only
        decay (never observed = stale), never promote."""
        seeded = interest_state.Evidence()
        transition = interest_state.decide("exploratory", seeded, self.RULES, NOW, blocked=False)
        self.assertTrue(transition is None or transition.action != "promote")


class InterestStateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.rules = interest_state.Rules()

    _counter = 0

    def _item(self, title, days_ago=0):
        InterestStateEvidenceTests._counter += 1
        seen = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
        item = normalize.normalize(an_item(title=title, url=f"https://e.com/{self._counter}"))
        self.conn.execute(
            "INSERT INTO candidate_items (source, type, title, text, url, dedup_key, url_hash,"
            " title_hash, content_hash, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.source, item.type, item.title, item.text, item.url, item.dedup_key,
             item.url_hash, item.title_hash, item.content_hash, seen),
        )
        self.conn.commit()

    def test_gather_evidence_counts_observations_and_distinct_days(self):
        self._item("Gizmo breakthrough announced", days_ago=1)
        self._item("Another gizmo update lands", days_ago=1)
        self._item("Gizmo momentum continues", days_ago=3)
        evidence = interest_state.gather_evidence(self.conn, self.rules, NOW)
        self.assertEqual(evidence["gizmo"].observations, 3)
        self.assertEqual(evidence["gizmo"].distinct_days, 2)

    def test_gather_evidence_excludes_owner_covered_terms(self):
        db.upsert_interest(self.conn, an_interest(title="Gizmo watch", positive_signals=["gizmo"]))
        self._item("Gizmo breakthrough announced", days_ago=1)
        evidence = interest_state.gather_evidence(self.conn, self.rules, NOW)
        self.assertNotIn("gizmo", evidence)

    def test_gather_evidence_excludes_an_existing_non_retired_derived_term_but_not_a_retired_one(self):
        db.upsert_derived_interest(
            self.conn, an_interest(key="derived:gizmo", layer="exploratory"), {}
        )
        db.upsert_derived_interest(
            self.conn, an_interest(key="derived:widget", layer="retired"), {}
        )
        self._item("Gizmo breakthrough announced", days_ago=1)
        self._item("Widget breakthrough announced", days_ago=1)
        evidence = interest_state.gather_evidence(self.conn, self.rules, NOW)
        self.assertNotIn("gizmo", evidence)
        self.assertIn("widget", evidence)   # retired terms are still reconsidered (re-entry)

    def test_gather_evidence_ignores_items_outside_the_window(self):
        self._item("Gizmo breakthrough announced", days_ago=200)
        evidence = interest_state.gather_evidence(self.conn, self.rules, NOW)
        self.assertNotIn("gizmo", evidence)

    def test_gather_evidence_counts_feedback_on_matching_titles(self):
        item = normalize.normalize(an_item(title="Gizmo breakthrough announced", url="https://e.com/g1"))
        item.id = db.insert_item(self.conn, item)
        db.add_feedback(self.conn, item.id, None, "up")
        db.add_feedback(self.conn, item.id, None, "trash")
        self._item("Gizmo momentum continues", days_ago=1)
        evidence = interest_state.gather_evidence(self.conn, self.rules, NOW)
        self.assertEqual(evidence["gizmo"].positive_feedback, 1)
        self.assertEqual(evidence["gizmo"].negative_feedback, 1)

    def test_gather_evidence_skips_a_verdict_outside_feedback_verdicts_rather_than_assuming_positive(self):
        item = normalize.normalize(an_item(title="Gizmo breakthrough announced", url="https://e.com/g1"))
        item.id = db.insert_item(self.conn, item)
        db.add_feedback(self.conn, item.id, None, "not-a-real-verdict")
        self._item("Gizmo momentum continues", days_ago=1)
        evidence = interest_state.gather_evidence(self.conn, self.rules, NOW)
        self.assertEqual(evidence["gizmo"].positive_feedback, 0)
        self.assertEqual(evidence["gizmo"].negative_feedback, 0)

    def test_gather_evidence_is_truncated_deterministically(self):
        rules = dataclasses.replace(self.rules, max_candidates=1)
        self._item("Gizmo news today", days_ago=1)
        self._item("Gizmo update lands", days_ago=1)
        self._item("Widget only mention here", days_ago=1)
        evidence = interest_state.gather_evidence(self.conn, rules, NOW)
        # "gizmo" (2 observations) beats every single-observation term.
        self.assertEqual(list(evidence), ["gizmo"])


class InterestStateApplyTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.cfg = dataclasses.replace(CFG, dynamic_interests=False)

    def _seed_items(self, title, n, distinct_days=3):
        for i in range(n):
            day = i % distinct_days
            seen = (NOW - timedelta(days=day)).isoformat(timespec="seconds")
            self.conn.execute(
                "INSERT INTO candidate_items (source, type, title, text, url, dedup_key,"
                " url_hash, title_hash, content_hash, first_seen_at)"
                " VALUES ('web_search', 'article', ?, 'body', ?, ?, ?, ?, NULL, ?)",
                (title, f"https://e.com/{title}-{i}", f"k{title}-{i}", f"u{title}-{i}",
                 f"t{title}-{i}", seen),
            )
        self.conn.commit()

    def _positive_feedback(self, term, up=0, fire=0):
        """Feedback rows on an item whose title contains `term` -- gathered
        regardless of the evidence window (see interest_state._feedback_index)."""
        item = normalize.normalize(an_item(title=f"{term} feedback item", url=f"https://fb/{term}"))
        item.id = db.insert_item(self.conn, item)
        for _ in range(up):
            db.add_feedback(self.conn, item.id, None, "up")
        for _ in range(fire):
            db.add_feedback(self.conn, item.id, None, "fire")

    def test_default_off_is_a_true_noop(self):
        """Byte-identical to today with the flag off: no row, no query, no
        write -- not just a filtered result."""
        self._seed_items("Gizmo breakthrough", 10)
        before = self.conn.execute("SELECT COUNT(*) c FROM interests").fetchone()["c"]
        summary = interest_state.apply_transitions(self.conn, self.cfg)
        after = self.conn.execute("SELECT COUNT(*) c FROM interests").fetchone()["c"]
        self.assertEqual(summary, {
            "enabled": False, "seeded": 0, "entered": 0, "promoted": 0,
            "decayed": 0, "retired": 0, "reentered": 0, "capped": 0,
        })
        self.assertEqual(before, after)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM interest_events").fetchone()["c"], 0)

    def test_a_fresh_term_enters_exploratory_then_needs_a_second_pass_to_promote(self):
        cfg = dataclasses.replace(self.cfg, dynamic_interests=True)
        # "Gizmo breakthrough" also seeds a "breakthrough" candidate; only
        # "gizmo" is asserted on below, the point being one ladder rung per
        # apply_transitions() call, not a specific candidate count.
        self._seed_items("Gizmo breakthrough", 5, distinct_days=3)

        first = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertGreaterEqual(first["entered"], 1)
        self.assertEqual(first["promoted"], 0)
        row = self.conn.execute("SELECT layer, active FROM interests WHERE key = 'derived:gizmo'").fetchone()
        self.assertEqual((row["layer"], row["active"]), ("exploratory", 0))

        second = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertGreaterEqual(second["promoted"], 1)
        row = self.conn.execute("SELECT layer, active FROM interests WHERE key = 'derived:gizmo'").fetchone()
        self.assertEqual((row["layer"], row["active"]), ("emerging", 0))

        events = db.interest_events(self.conn, "derived:gizmo")
        self.assertEqual([e["action"] for e in events], ["enter", "promote"])

    def test_promotion_to_inferred_is_capped_and_recorded(self):
        cfg = dataclasses.replace(self.cfg, dynamic_interests=True, derived_max_active=0)
        db.upsert_derived_interest(
            self.conn, an_interest(key="derived:gizmo", layer="emerging"), {}
        )
        self._seed_items("Gizmo breakthrough", 5, distinct_days=3)
        self._positive_feedback("gizmo", up=1, fire=1)
        summary = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertEqual(summary["promoted"], 0)
        self.assertEqual(summary["capped"], 1)
        row = self.conn.execute("SELECT layer, active FROM interests WHERE key = 'derived:gizmo'").fetchone()
        self.assertEqual((row["layer"], row["active"]), ("emerging", 0))
        events = db.interest_events(self.conn, "derived:gizmo")
        self.assertEqual(events[-1]["action"], "promotion_capped")

    def test_inferred_interest_carries_the_derived_min_score_floor(self):
        cfg = dataclasses.replace(self.cfg, dynamic_interests=True, derived_min_score=0.9)
        db.upsert_derived_interest(
            self.conn, an_interest(key="derived:gizmo", layer="emerging"), {}
        )
        self._seed_items("Gizmo breakthrough", 5, distinct_days=3)
        self._positive_feedback("gizmo", up=1, fire=1)   # clears the emerging -> inferred bar
        interest_state.apply_transitions(self.conn, cfg, now=NOW)
        row = self.conn.execute(
            "SELECT layer, active, min_score, positive_signals FROM interests WHERE key = 'derived:gizmo'"
        ).fetchone()
        self.assertEqual(row["layer"], "inferred")
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["min_score"], 0.9)
        self.assertEqual(json.loads(row["positive_signals"]), ["gizmo"])

    def test_idle_derived_interest_decays_one_rung(self):
        cfg = dataclasses.replace(self.cfg, dynamic_interests=True)
        db.upsert_derived_interest(
            self.conn, an_interest(key="derived:gizmo", layer="inferred"), {}
        )
        # upsert_derived_interest() always stamps last_observed_at = db.now()
        # (real wall-clock); backdate it past decay_idle_days so this test's
        # "idle" is genuine rather than an artifact of a fixed injected `now`.
        stale = (NOW - timedelta(days=40)).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE interests SET last_observed_at = ? WHERE key = 'derived:gizmo'", (stale,)
        )
        self.conn.commit()
        summary = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertEqual(summary["decayed"], 1)
        row = self.conn.execute("SELECT layer, active FROM interests WHERE key = 'derived:gizmo'").fetchone()
        self.assertEqual((row["layer"], row["active"]), ("emerging", 0))

    def test_a_freshly_written_derived_interest_is_not_immediately_idle(self):
        """Regression: last_observed_at is the decay-staleness baseline for a
        row with no corpus evidence this pass (see upsert_derived_interest's
        docstring) -- a row written moments ago must not decay on its very
        next re-evaluation just because it has no fresh window evidence."""
        cfg = dataclasses.replace(self.cfg, dynamic_interests=True)
        db.upsert_derived_interest(
            self.conn, an_interest(key="derived:gizmo", layer="inferred"), {}
        )
        fresh = NOW.isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE interests SET last_observed_at = ? WHERE key = 'derived:gizmo'", (fresh,)
        )
        self.conn.commit()
        summary = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertEqual(summary["decayed"], 0)
        row = self.conn.execute("SELECT layer, active FROM interests WHERE key = 'derived:gizmo'").fetchone()
        self.assertEqual((row["layer"], row["active"]), ("inferred", 1))

    def test_blocked_term_already_tracked_is_retired(self):
        cfg = dataclasses.replace(self.cfg, dynamic_interests=True)
        path = os.path.join(tempfile.mkdtemp(), "interests.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"interests": [], "blocked_derived_terms": ["gizmo"]}, fh)
        cfg = dataclasses.replace(cfg, interests_path=path)
        db.upsert_derived_interest(
            self.conn, an_interest(key="derived:gizmo", layer="exploratory"), {}
        )
        self._seed_items("Gizmo breakthrough", 5, distinct_days=3)
        summary = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertEqual(summary["retired"], 1)
        row = self.conn.execute("SELECT layer, active FROM interests WHERE key = 'derived:gizmo'").fetchone()
        self.assertEqual((row["layer"], row["active"]), ("retired", 0))

    def test_personal_state_seed_never_promotes_by_itself(self):
        cfg = dataclasses.replace(
            self.cfg, dynamic_interests=True,
            personal_state_path=self._personal_state_artifact(["nocorpusterm"]),
        )
        summary = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertEqual(summary["seeded"], 1)
        row = self.conn.execute(
            "SELECT layer, active FROM interests WHERE key = 'derived:nocorpusterm'"
        ).fetchone()
        self.assertEqual((row["layer"], row["active"]), ("exploratory", 0))
        # Pin last_observed_at to this test's own fixed clock -- production
        # apply_transitions()'s `now` and db.upsert_derived_interest()'s
        # db.now() are both real wall-clock and so agree, but here `now` is
        # injected and fixed, so pin the row to match rather than lean on
        # the sandbox's real clock happening to agree with NOW.
        self.conn.execute(
            "UPDATE interests SET last_observed_at = ? WHERE key = 'derived:nocorpusterm'",
            (NOW.isoformat(timespec="seconds"),),
        )
        self.conn.commit()
        # A second pass, same instant, no new corpus evidence: must never
        # promote it (the hard requirement), and -- since no idle time has
        # actually elapsed since the seed -- must not decay it either. Zero
        # observations can only ever demote/hold, never advance the ladder.
        summary2 = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertEqual(summary2["promoted"], 0)
        row2 = self.conn.execute(
            "SELECT layer FROM interests WHERE key = 'derived:nocorpusterm'"
        ).fetchone()
        self.assertEqual(row2["layer"], "exploratory")

    def _personal_state_artifact(self, terms):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "personal_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "contract_version": 1,
                "generated_at": "2026-08-10T00:00:00Z",
                "topics": [{"key": t, "weight": 1.0} for t in terms],
            }, fh)
        return path

    def test_personal_state_seed_records_full_provenance_on_the_row_and_the_event(self):
        """Work item 1: the seed's origin -- artifact identity, contract
        version, topic key, seeded_at -- lands on BOTH the interest's own
        provenance JSON and its interest_events seed row, and seeding writes
        no score row (zero LLM/network calls -- apply_transitions() doesn't
        even take a provider argument)."""
        term = "zzqleaktest"
        path = self._personal_state_artifact([term])
        cfg = dataclasses.replace(self.cfg, dynamic_interests=True, personal_state_path=path)
        expected_hash = hashlib.sha256(open(path, "rb").read()).hexdigest()

        summary = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertEqual(summary["seeded"], 1)

        row = self.conn.execute(
            "SELECT provenance FROM interests WHERE key = ?", (f"derived:{term}",)
        ).fetchone()
        provenance = json.loads(row["provenance"])
        self.assertEqual(provenance["origin"], "personal_state")
        self.assertEqual(provenance["artifact_sha256"], expected_hash)
        self.assertEqual(provenance["generated_at"], "2026-08-10T00:00:00Z")
        self.assertEqual(provenance["contract_version"], 1)
        self.assertEqual(provenance["topic_key"], term)
        self.assertEqual(provenance["seeded_at"], NOW.isoformat(timespec="seconds"))

        events = db.interest_events(self.conn, f"derived:{term}")
        self.assertEqual([e["action"] for e in events], ["seed"])
        seed_evidence = events[0]["evidence"]
        self.assertEqual(seed_evidence["artifact_sha256"], expected_hash)
        self.assertEqual(seed_evidence["generated_at"], "2026-08-10T00:00:00Z")
        self.assertEqual(seed_evidence["contract_version"], 1)
        self.assertEqual(seed_evidence["topic_key"], term)

        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM scores").fetchone()["c"], 0)

    def test_personal_state_seeded_term_never_promotes_on_self_referential_matching_alone(self):
        """Work item 2 (the core of this step): the only legal influence
        channel of personal_state on discovery is a zero-weight exploratory
        seed (PROJECT_STATE.md's step-05 gate) -- prove the row can never
        promote off evidence that is only attributable to its own matching
        (an item whose ONLY row in item_interests points back at this very
        derived interest), with no independent corpus signal and no human
        feedback anywhere. Drives the real apply_transitions() over 3 cycles,
        not a mock of decide()."""
        term = "zzqleaktest"
        key = f"derived:{term}"
        cfg = dataclasses.replace(
            self.cfg, dynamic_interests=True,
            personal_state_path=self._personal_state_artifact([term]),
        )
        interest_state.apply_transitions(self.conn, cfg, now=NOW)  # pass 0: seeds
        row = self.conn.execute("SELECT id, layer, active FROM interests WHERE key = ?", (key,)).fetchone()
        self.assertEqual((row["layer"], row["active"]), ("exploratory", 0))
        # Pin last_observed_at to the fixed test clock (see
        # test_personal_state_seed_never_promotes_by_itself for why).
        self.conn.execute(
            "UPDATE interests SET last_observed_at = ? WHERE key = ?",
            (NOW.isoformat(timespec="seconds"), key),
        )
        self.conn.commit()
        owner_before = [
            dict(r) for r in self.conn.execute("SELECT * FROM interests WHERE layer = 'owner'").fetchall()
        ]

        for cycle in range(3):
            derived_interest = db.interest_by_key(self.conn, key)
            # Well past the ordinary observation/distinct-day bars by the
            # last cycle, but every single item's ONLY matched interest (via
            # item_interests -- the matching pipeline's own attribution) is
            # the seeded interest itself: self-referential, not independent.
            for day in range(cycle * 2, cycle * 2 + 2):
                seen = (NOW - timedelta(days=day)).isoformat(timespec="seconds")
                # Title is the term alone -- no second shared word, so this
                # fixture can't accidentally promote some *other* incidental
                # term (e.g. a repeated second word) instead of proving
                # anything about the seeded one.
                item = normalize.normalize(an_item(
                    title=term, url=f"https://e.com/{term}-{cycle}-{day}"
                ))
                item.id = db.insert_item(self.conn, item)
                self.conn.execute(
                    "UPDATE candidate_items SET first_seen_at = ? WHERE id = ?", (seen, item.id)
                )
                self.conn.commit()
                db.save_matches(self.conn, item.id, [(derived_interest, 0.5, [term])])

            summary = interest_state.apply_transitions(self.conn, cfg, now=NOW)
            self.assertEqual(summary["promoted"], 0)
            row = self.conn.execute("SELECT layer FROM interests WHERE key = ?", (key,)).fetchone()
            self.assertEqual(row["layer"], "exploratory")

        events = db.interest_events(self.conn, key)
        self.assertNotIn("promote", [e["action"] for e in events])
        self.assertEqual(
            [dict(r) for r in self.conn.execute("SELECT * FROM interests WHERE layer = 'owner'").fetchall()],
            owner_before,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM scores").fetchone()["c"], 0)

        # Now let it go genuinely idle (no fresh self-referential item this
        # pass) past decay_idle_days -- must demote/retire per the ordinary
        # decide() rules, exactly like any other exploratory row.
        stale = (NOW - timedelta(days=interest_state.Rules().decay_idle_days + 1)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE interests SET last_observed_at = ? WHERE key = ?", (stale, key))
        self.conn.commit()
        summary = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        # exploratory decays straight to retired (nothing below it); the
        # summary bucket is "decayed" -- see _summary_key(), which only
        # counts an explicit retire_* action (blocked/negative-feedback) as
        # "retired".
        self.assertEqual(summary["decayed"], 1)
        row = self.conn.execute("SELECT layer, active FROM interests WHERE key = ?", (key,)).fetchone()
        self.assertEqual((row["layer"], row["active"]), ("retired", 0))

    def _promote_seeded_term_to_inferred_and_notify(self, term):
        """The companion, positive-path fixture to the leakage test above:
        same seed, but with independent owner-collector corpus evidence (no
        item_interests involved at all) plus positive human feedback
        recorded through the existing db.add_feedback path -- promotes
        exploratory -> emerging -> inferred, one rung per
        apply_transitions() call, and then delivers an above-bar match on it
        through the real pipeline. Returns (key, item_id, score_id,
        notification_id)."""
        key = f"derived:{term}"
        cfg = dataclasses.replace(
            self.cfg, dynamic_interests=True,
            personal_state_path=self._personal_state_artifact([term]),
        )
        interest_state.apply_transitions(self.conn, cfg, now=NOW)  # pass 1: seeds
        self.conn.execute(
            "UPDATE interests SET last_observed_at = ? WHERE key = ?",
            (NOW.isoformat(timespec="seconds"), key),
        )
        self.conn.commit()

        # Independent corpus evidence: plain items, exactly as an owner
        # collector would have stored them -- no item_interests row at all.
        self._seed_items(term, 6, distinct_days=3)
        pass2 = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertEqual(pass2["promoted"], 1)
        row = self.conn.execute("SELECT layer, active FROM interests WHERE key = ?", (key,)).fetchone()
        self.assertEqual((row["layer"], row["active"]), ("emerging", 0))

        # Positive human feedback, through the existing recording path.
        self._positive_feedback(term, up=2)
        pass3 = interest_state.apply_transitions(self.conn, cfg, now=NOW)
        self.assertEqual(pass3["promoted"], 1)
        row = self.conn.execute("SELECT layer, active FROM interests WHERE key = ?", (key,)).fetchone()
        self.assertEqual((row["layer"], row["active"]), ("inferred", 1))

        events = db.interest_events(self.conn, key)
        self.assertEqual([e["action"] for e in events], ["seed", "promote", "promote"])

        active = db.active_interests(self.conn)
        self.assertIn(key, [i.key for i in active])
        provider = FakeProvider({f"{term} breaks through": 0.95})
        item = an_item(
            title=f"{term} breaks through", url=f"https://e.com/{term}-notify", text=BODY
        )
        outcome = pipeline.ingest(self.conn, provider, cfg, item, active, origin_interest=key)
        self.assertEqual(outcome.stage, "scored")
        sent = pipeline.send_digest(self.conn, cfg, dry_run=True)
        self.assertEqual(sent, 1)
        notification = self.conn.execute(
            "SELECT n.id, n.ok FROM notifications n JOIN scores s ON s.id = n.score_id"
            " WHERE s.item_id = ?",
            (outcome.item.id,),
        ).fetchone()
        self.assertEqual(notification["ok"], 1)
        return key, outcome.item.id, outcome.score.id, notification["id"]

    def test_personal_state_seeded_term_promotes_on_independent_evidence_plus_feedback(self):
        """Work item 3: the loop closes positively -- same seeded term as the
        leakage test, but with real independent evidence. once inferred, an
        above-bar match yields a notification through the real pipeline."""
        self._promote_seeded_term_to_inferred_and_notify("zzqleaktest")

    # The documented chain query from README.md's "Provenance chain" section
    # -- kept byte-identical to what's published there so this test proves
    # the exact query a human would copy-paste actually resolves every hop.
    PROVENANCE_CHAIN_QUERY = """
        SELECT
          n.id                                            AS notification_id,
          s.id                                            AS score_id,
          s.final_score                                   AS score,
          ci.id                                            AS item_id,
          ci.title                                         AS item_title,
          it.key                                           AS interest_key,
          it.layer                                         AS interest_layer,
          ev.id                                            AS seed_event_id,
          json_extract(ev.evidence, '$.artifact_sha256')   AS artifact_sha256,
          json_extract(ev.evidence, '$.generated_at')      AS artifact_generated_at,
          json_extract(ev.evidence, '$.contract_version')  AS contract_version
        FROM notifications n
        JOIN scores s           ON s.id = n.score_id
        JOIN candidate_items ci ON ci.id = s.item_id
        JOIN interests it       ON it.id = s.interest_id
        JOIN interest_events ev ON ev.interest_key = it.key AND ev.action = 'seed'
        WHERE n.id = ?
    """

    def test_provenance_chain_query_resolves_every_hop(self):
        """Work item 4: the documented SQL query walks notification -> score
        -> item -> matched interest -> interest_events -> seed event with
        the artifact hash, and every hop resolves non-empty against the
        positive-path fixture."""
        key, item_id, score_id, notification_id = self._promote_seeded_term_to_inferred_and_notify(
            "zzqleaktest"
        )
        row = self.conn.execute(self.PROVENANCE_CHAIN_QUERY, (notification_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["notification_id"], notification_id)
        self.assertEqual(row["score_id"], score_id)
        self.assertEqual(row["item_id"], item_id)
        self.assertEqual(row["interest_key"], key)
        self.assertEqual(row["interest_layer"], "inferred")
        self.assertIsNotNone(row["seed_event_id"])
        self.assertTrue(row["artifact_sha256"])
        self.assertEqual(row["artifact_generated_at"], "2026-08-10T00:00:00Z")
        self.assertEqual(row["contract_version"], 1)


class ScoringTests(unittest.TestCase):
    def _matches(self, *interests_):
        item = normalize.normalize(an_item(), "k")
        return item, [(i, 0.8, ["good stuff"]) for i in interests_]

    def test_returns_all_dimensions_and_a_code_computed_final_score(self):
        interest = an_interest(id=1)
        item, matches = self._matches(interest)
        item.id = 7
        score = scoring.score_candidate(FakeProvider({"A title": 0.8}), item, matches)
        self.assertEqual((score.item_id, score.interest_id, score.interest_key), (7, 1, "k"))
        self.assertEqual(set(score.dimensions), set(models.DIMENSIONS))
        self.assertAlmostEqual(score.final_score, 0.8)
        self.assertEqual((score.provider, score.model), ("fake", "fake-1"))
        self.assertEqual(score.why_better_than_generic, "Has the per-arm numbers.")

    def test_out_of_range_dimensions_are_clamped(self):
        interest = an_interest(id=1)
        item, matches = self._matches(interest)
        item.id = 1
        score = scoring.score_candidate(FakeProvider({"A title": 4.0}), item, matches)
        self.assertEqual(score.final_score, 1.0)
        self.assertEqual(score.dimensions["novelty"], 1.0)

    def test_an_unknown_interest_key_falls_back_to_the_strongest_match(self):
        best, other = an_interest(id=1, key="best"), an_interest(id=2, key="other")
        item = normalize.normalize(an_item())
        item.id = 1
        matches = [(best, 0.9, []), (other, 0.4, [])]
        provider = FakeProvider()
        provider.complete_json = lambda *a, **kw: FakeProvider._payload(0.5, "hallucinated")
        self.assertEqual(scoring.score_candidate(provider, item, matches).interest_key, "best")

    def test_no_matched_interests_raises_instead_of_calling_the_model(self):
        item = normalize.normalize(an_item())
        provider = FakeProvider()
        with self.assertRaises(scoring.ScoringError):
            scoring.score_candidate(provider, item, [])
        self.assertEqual(provider.prompts, [])

    def test_prompt_carries_signals_and_past_verdicts(self):
        rows = [
            {"verdict": "down", "title": "Sleep hygiene tips", "note": "listicle"},
            {"verdict": "trash", "title": "Supplement ad", "note": ""},
        ]
        item, matches = self._matches(an_interest(id=1))
        prompt = scoring._prompt(item, matches, rows)
        self.assertIn("good stuff", prompt)
        self.assertIn("bad stuff", prompt)
        self.assertIn("disliked: Sleep hygiene tips", prompt)
        self.assertIn("rejected as a bad match: Supplement ad", prompt)
        self.assertIn('key="k"', prompt)


class WebSearchCollectorTests(unittest.TestCase):
    def test_shapes_search_results_into_candidates(self):
        provider = FakeProvider(search_results=[
            {"title": "T", "url": "https://e.com/x", "summary": "S", "author": "A"},
        ])
        (item,) = web_search.collect(an_interest(), CFG, provider)
        self.assertEqual((item.source, item.type, item.url), ("web_search", "article", "https://e.com/x"))
        self.assertEqual(item.text, "S")
        self.assertIn("good stuff", provider.search_prompts[0])

    def test_drops_junk_entries_and_respects_the_limit(self):
        provider = FakeProvider(search_results=[
            {"title": "no url"},
            "not a dict",
            {"title": "A", "url": "https://e.com/a"},
            {"title": "B", "url": "https://e.com/b"},
        ])
        interest = an_interest(source_config={"web_search": {"limit": 3}})
        items = web_search.collect(interest, CFG, provider)
        self.assertEqual([i.url for i in items], ["https://e.com/a"])

    def test_a_provider_without_search_raises_unsupported(self):
        with self.assertRaises(UnsupportedCapability):
            web_search.collect(an_interest(), CFG, FakeProvider())


class StocksCollectorTests(unittest.TestCase):
    def _change(self, schedule, pct, label):
        from datetime import datetime, timezone

        return {
            "ticker": "NBIS",
            "schedule": schedule,
            "label": label,
            "currency": "USD",
            "then_price": 100.0,
            "then_at": datetime(2026, 8, 6, tzinfo=timezone.utc),
            "now_price": 100.0 + pct,
            "now_at": datetime(2026, 8, 7, tzinfo=timezone.utc),
            "delta": pct,
            "pct": pct,
        }

    def _prices(self, daily_pct=0.5, weekly_pct=0.5):
        def fake(ticker, schedule):
            return (
                self._change("daily", daily_pct, "1d")
                if schedule == "daily"
                else self._change("weekly", weekly_pct, "1w")
            )

        return fake

    def test_daily_threshold_crossed_produces_a_market_event(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={
                "stocks": {
                    "tickers": [{"ticker": "NBIS", "daily_percent_move": 6, "weekly_percent_move": 12}]
                }
            },
        )
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=-8.4, weekly_pct=-2.0)
        ):
            (item,) = stocks.collect(interest, CFG, None)
        self.assertEqual(item.type, "market_event")
        self.assertEqual(item.key(), "NBIS:2026-08-07")
        self.assertAlmostEqual(item.metadata["daily_pct"], -8.4)
        self.assertAlmostEqual(item.metadata["weekly_pct"], -2.0)
        self.assertIn("NBIS -8.40% today", item.title)
        self.assertIn("no provider", item.text)

    def test_market_events_on_different_days_are_not_url_deduped(self):
        # Regression: the market_event URL used to be ticker-only
        # (finance.yahoo.com/quote/NBIS), constant across every day the
        # ticker crossed its threshold. dedup.find_duplicate() checks
        # url_hash before dedup_key is ever consulted, so a second day's
        # genuinely distinct event was silently swallowed as "duplicate:
        # same url" and never scored or alerted -- forever, for that ticker.
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"]}},
        )
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=8.4, weekly_pct=8.4)
        ):
            (day1,) = stocks.collect(interest, CFG, None)
        day1 = normalize.normalize(day1)

        later = self._change("daily", -9.5, "1d")
        later["now_at"] = later["now_at"].replace(day=later["now_at"].day + 7)
        with mock.patch.object(
            stocks.watch, "price_change",
            side_effect=lambda ticker, schedule: later if schedule == "daily" else later,
        ):
            (day2,) = stocks.collect(interest, CFG, None)

        self.assertNotEqual(day1.url, day2.url)
        self.assertNotEqual(day1.url_hash, normalize.normalize(day2).url_hash)

    def test_weekly_threshold_alone_still_triggers(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={
                "stocks": {
                    "tickers": [{"ticker": "NBIS", "daily_percent_move": 6, "weekly_percent_move": 12}]
                }
            },
        )
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=1.0, weekly_pct=15.0)
        ):
            (item,) = stocks.collect(interest, CFG, None)
        self.assertEqual(item.type, "market_event")

    def test_move_below_both_thresholds_is_dropped(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"]}},
        )
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=0.5, weekly_pct=0.5)
        ):
            self.assertEqual(stocks.collect(interest, CFG, None), [])

    def test_bare_string_ticker_uses_default_thresholds(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"]}},
        )
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=6.5, weekly_pct=1.0)
        ):
            (item,) = stocks.collect(interest, CFG, None)
        self.assertEqual(item.metadata["ticker"], "NBIS")

    def test_an_event_already_in_the_db_costs_nothing_to_re_poll(self):
        """The hourly stocks job re-crosses the same threshold all day. Dedup
        would throw the repeat away -- but only after two LLM calls had already
        been paid for it, so the check has to happen here instead."""
        conn = db.connect(":memory:")
        db.init(conn)
        self.addCleanup(conn.close)
        interest = an_interest(sources=["stocks"], source_config={"stocks": {"tickers": ["NBIS"]}})
        provider = FakeProvider(
            search_results=[{"title": "Earnings beat", "url": "https://n/1", "summary": "x" * 60}]
        )
        provider.complete_json = lambda *a, **kw: {
            "catalyst": "confirmed", "explanation": "Earnings beat.", "confidence": "high",
        }

        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=8.0, weekly_pct=1.0)
        ):
            first = stocks.collect(interest, CFG, provider, conn)
            for item in first:
                normalize.normalize(item)
                db.insert_item(conn, item)
            self.assertEqual(len(provider.search_prompts), 1)

            again = stocks.collect(interest, CFG, provider, conn)
        self.assertEqual(again, [])
        self.assertEqual(len(provider.search_prompts), 1)   # no second explanation

    def test_no_provider_skips_explanation_but_still_fires(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"]}},
        )
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=8.0, weekly_pct=1.0)
        ):
            (item,) = stocks.collect(interest, CFG, None)
        self.assertIsNone(item.metadata["catalyst"])
        self.assertIn("not checked", item.text)

    def test_explain_false_skips_explanation_even_with_a_provider(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"], "explain": False}},
        )
        provider = FakeProvider()
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=8.0, weekly_pct=1.0)
        ):
            (item,) = stocks.collect(interest, CFG, provider)
        self.assertIsNone(item.metadata["catalyst"])
        self.assertEqual(provider.search_prompts, [])

    def test_no_news_found_reports_no_catalyst_without_inventing_one(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"]}},
        )
        provider = FakeProvider(search_results=[])
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=8.0, weekly_pct=1.0)
        ):
            items = stocks.collect(interest, CFG, provider)
        (event,) = items
        self.assertEqual(event.metadata["catalyst"], "none")
        self.assertIn("no obvious catalyst found", event.text)

    def test_news_grades_the_catalyst_from_snippets_and_flows_to_scoring(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"]}},
        )
        provider = FakeProvider(
            search_results=[
                {
                    "title": "Nebius signs large GPU capacity deal",
                    "url": "https://e.com/nbis-deal",
                    "summary": "A named hyperscaler signed a multi-year contract.",
                    "published_at": "2026-08-07",
                }
            ]
        )
        provider.complete_json = lambda *a, **kw: {
            "catalyst": "confirmed",
            "explanation": "A large signed GPU capacity contract was disclosed.",
            "confidence": "medium",
        }
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=-8.4, weekly_pct=-2.0)
        ):
            items = stocks.collect(interest, CFG, provider)

        event, news = items
        self.assertEqual(event.type, "market_event")
        self.assertEqual(event.metadata["catalyst"], "confirmed")
        self.assertIn("Confirmed catalyst", event.text)
        self.assertIn("Nebius signs large GPU capacity deal", event.text)
        self.assertIn("NBIS", provider.search_prompts[0])

        self.assertEqual(news.type, "article")
        self.assertEqual(news.url, "https://e.com/nbis-deal")
        self.assertEqual(news.metadata["ticker"], "NBIS")

    def test_a_provider_without_search_still_fires_the_event(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"]}},
        )
        provider = FakeProvider()  # search_results=None -> UnsupportedCapability
        with mock.patch.object(
            stocks.watch, "price_change", side_effect=self._prices(daily_pct=8.0, weekly_pct=1.0)
        ):
            (item,) = stocks.collect(interest, CFG, provider)
        self.assertIsNone(item.metadata["catalyst"])


class FakeSnippet:
    """Stands in for youtube_transcript_api's FetchedTranscriptSnippet."""

    def __init__(self, text, start, duration):
        self.text = text
        self.start = start
        self.duration = duration


# Real video ids are 11 chars of [A-Za-z0-9_-]; _video_id_from_url enforces
# that, so test ids must look real.
VID1, VID2, VID3 = "vid00000001", "vid00000002", "vid00000003"


def recent_ts(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def a_video(**kw):
    """An enriched video dict as _list_videos returns it (verification passed)."""
    base = dict(
        video_id=VID1,
        video_title="Some Podcast",
        channel="A Channel",
        description="",
        published_at=recent_ts(),
        duration_seconds=3600,
    )
    base.update(kw)
    base.setdefault("video_url", f"https://www.youtube.com/watch?v={base['video_id']}")
    return base


def a_discovery_entry(video_id=VID1, estimate=0.8, why="looks on-topic"):
    """One entry of the Stage-1 search_json reply."""
    return {"url": f"https://www.youtube.com/watch?v={video_id}", "estimate": estimate, "why": why}


def listed(*videos):
    return {v["video_id"]: v for v in videos}


class YoutubeCollectorTests(unittest.TestCase):
    def setUp(self):
        # Fetch pacing exists for the live endpoint, not for tests.
        patcher = mock.patch.object(youtube, "FETCH_SLEEP_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = dataclasses.replace(CFG, youtube_api_key="key")

    def interest(self, **kw):
        kw.setdefault("sources", ["youtube"])
        kw.setdefault("source_config", {"youtube": {}})
        return an_interest(**kw)

    def test_chunk_transcript_slides_with_overlap(self):
        snippets = [FakeSnippet(f"word{i}", i * 10, 10) for i in range(10)]  # 0..100s
        chunks = youtube._chunk_transcript(snippets, window_seconds=30, overlap_seconds=10)
        starts = [c[0] for c in chunks]
        self.assertEqual(starts, [0, 20, 40, 60, 80])
        self.assertIn("word0", chunks[0][2])

    def test_chunk_transcript_empty_input(self):
        self.assertEqual(youtube._chunk_transcript([], 30, 10), [])
        self.assertEqual(youtube._chunk_transcript([FakeSnippet("  ", 0, 5)], 30, 10), [])

    def test_missing_api_key_raises(self):
        with self.assertRaises(youtube.YoutubeCollectorError):
            youtube.collect(self.interest(), CFG, None)

    def test_no_provider_or_no_search_capability_is_a_clean_skip(self):
        """Discovery has no fallback path: without a searching provider the
        cycle skips youtube entirely -- and spends nothing."""
        with mock.patch.object(youtube, "_list_videos") as lv:
            self.assertEqual(youtube.collect(self.interest(), self.cfg, None), [])
            # FakeProvider with search_results=None raises UnsupportedCapability.
            self.assertEqual(youtube.collect(self.interest(), self.cfg, FakeProvider()), [])
            lv.assert_not_called()

    def test_owner_queries_ride_into_the_discovery_prompt(self):
        """`source_config.youtube.queries` are starting-point hints for the
        model's iterative search -- blank entries dropped, absent key means
        the prompt stays byte-identical to the hintless form."""
        interest = self.interest(
            source_config={"youtube": {"queries": ["orexin agonist trial MWT", "  ", ""]}}
        )
        provider = FakeProvider(search_results=[])
        self.assertEqual(youtube.collect(interest, self.cfg, provider), [])
        self.assertIn("orexin agonist trial MWT", provider.search_prompts[0])
        self.assertIn("Starting-point searches", provider.search_prompts[0])

        bare = FakeProvider(search_results=[])
        youtube.collect(self.interest(), self.cfg, bare)
        self.assertNotIn("Starting-point searches", bare.search_prompts[0])

    def test_collect_shapes_one_candidate_per_segment(self):
        interest = self.interest(
            source_config={"youtube": {"chunk_seconds": 30, "chunk_overlap_seconds": 10}}
        )
        provider = FakeProvider(search_results=[a_discovery_entry(estimate=0.9)])
        snippets = [FakeSnippet(f"segment about {i} orexin", i * 10, 10) for i in range(6)]
        with mock.patch.object(youtube, "_list_videos", return_value=listed(a_video())), \
             mock.patch.object(youtube, "_fetch_transcript", return_value=snippets):
            items = youtube.collect(interest, self.cfg, provider)

        self.assertGreaterEqual(len(items), 2)
        first, second = items[0], items[1]
        self.assertEqual(first.source, "youtube")
        self.assertEqual(first.type, "video_segment")
        self.assertEqual(first.metadata["video_id"], VID1)
        self.assertEqual(first.metadata["channel"], "A Channel")
        self.assertEqual(first.metadata["start_time"], 0)
        self.assertIn("orexin", first.metadata["transcript"])
        # Stage-1 provenance rides along for discovery-vs-score calibration.
        self.assertEqual(first.metadata["discovery_estimate"], 0.9)
        self.assertEqual(first.metadata["discovery_why"], "looks on-topic")
        self.assertTrue(first.url.startswith(f"https://www.youtube.com/watch?v={VID1}&t="))

        # Two segments of the same video must not collide on any dedup layer:
        # distinct urls (survives fragment-stripping normalize.canonical_url),
        # distinct titles (mm:ss range), distinct dedup_keys.
        self.assertNotEqual(normalize.canonical_url(first.url), normalize.canonical_url(second.url))
        self.assertNotEqual(first.title, second.title)
        self.assertNotEqual(first.dedup_key, second.dedup_key)

    def test_transcript_success_yields_only_segments_no_video_level_item(self):
        """A clean fetch never produces a video-level fallback alongside the
        segments -- a video is one or the other, never both."""
        provider = FakeProvider(search_results=[a_discovery_entry(estimate=0.9)])
        snippets = [FakeSnippet("orexin content", 0, 10)]
        with mock.patch.object(youtube, "_list_videos", return_value=listed(a_video())), \
             mock.patch.object(youtube, "_fetch_transcript", return_value=snippets):
            items = youtube.collect(self.interest(), self.cfg, provider)

        self.assertTrue(items)
        self.assertTrue(all(i.type == "video_segment" for i in items))
        self.assertFalse(any(i.type == "video" for i in items))
        self.assertFalse(any(i.dedup_key.endswith(":video") for i in items))

    def test_a_stored_video_level_row_blocks_re_listing_and_re_fetching(self):
        """A video-level fallback is a full seen row: the video is done for
        good, exactly like a segmented one -- no re-list, no re-fetch."""
        conn = db.connect(":memory:")
        db.init(conn)
        self.addCleanup(conn.close)
        provider = FakeProvider(search_results=[a_discovery_entry()])

        with mock.patch.object(youtube, "_list_videos", return_value=listed(a_video())) as lv, \
             mock.patch.object(youtube, "_fetch_transcript", return_value=None) as fetch:
            items = youtube.collect(self.interest(), self.cfg, provider, conn)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].type, "video")
            normalize.normalize(items[0])
            db.insert_item(conn, items[0])
            self.assertEqual(lv.call_count, 1)
            self.assertEqual(fetch.call_count, 1)

            self.assertEqual(youtube.collect(self.interest(), self.cfg, provider, conn), [])
            self.assertEqual(lv.call_count, 1)      # zero quota spent on a seen video
            self.assertEqual(fetch.call_count, 1)   # zero transcript requests either

    def test_an_already_chunked_video_is_not_re_listed_or_fetched(self):
        """Discovery legitimately re-finds the same video next cycle. The
        seen-prefix check must drop it BEFORE the videos.list spend, not just
        before the transcript fetch."""
        conn = db.connect(":memory:")
        db.init(conn)
        self.addCleanup(conn.close)
        provider = FakeProvider(search_results=[a_discovery_entry()])
        snippets = [FakeSnippet("real content about orexin agonists", 0, 10)]

        with mock.patch.object(youtube, "_list_videos", return_value=listed(a_video())) as lv, \
             mock.patch.object(youtube, "_fetch_transcript", return_value=snippets) as fetch:
            items = youtube.collect(self.interest(), self.cfg, provider, conn)
            for item in items:
                normalize.normalize(item)
                db.insert_item(conn, item)
            self.assertEqual(lv.call_count, 1)
            self.assertEqual(fetch.call_count, 1)

            self.assertEqual(youtube.collect(self.interest(), self.cfg, provider, conn), [])
            self.assertEqual(lv.call_count, 1)      # zero quota spent on a seen video
            self.assertEqual(fetch.call_count, 1)   # zero transcript requests either

    def test_hallucinated_and_stale_ids_are_dropped_by_verify(self):
        """videos.list is authoritative: ids it doesn't return don't exist
        (hallucinated/deleted), and its publishedAt overrules the model's
        recency claim."""
        provider = FakeProvider(search_results=[
            a_discovery_entry(VID1, estimate=0.9),
            a_discovery_entry(VID2, estimate=0.8),   # hallucinated: absent from videos.list
            a_discovery_entry(VID3, estimate=0.7),   # real but a year old
        ])
        returned = listed(a_video(video_id=VID1),
                          a_video(video_id=VID3, published_at=recent_ts(days=365)))
        snippets = [FakeSnippet("real content here today", 0, 10)]

        with mock.patch.object(youtube, "_list_videos", return_value=returned), \
             mock.patch.object(youtube, "_fetch_transcript", return_value=snippets) as fetch:
            items = youtube.collect(self.interest(), self.cfg, provider)

        fetch.assert_called_once_with(VID1, ["en"])
        self.assertTrue(items)
        self.assertTrue(all(i.metadata["video_id"] == VID1 for i in items))

    def test_verify_is_one_batched_call_with_every_unseen_id(self):
        provider = FakeProvider(search_results=[
            a_discovery_entry(VID1), a_discovery_entry(VID2), a_discovery_entry(VID3),
        ])
        with mock.patch.object(youtube, "_list_videos", return_value={}) as lv:
            self.assertEqual(youtube.collect(self.interest(), self.cfg, provider), [])
        lv.assert_called_once_with("key", [VID1, VID2, VID3])

    def test_list_videos_batches_50_per_request_and_unescapes(self):
        """1 quota unit per ≤50 ids; entities in API strings must not reach
        Telegram verbatim."""
        ids = [f"vid{i:08d}" for i in range(60)]
        calls = []

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=0):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            batch = query["id"][0].split(",")
            calls.append(batch)
            payload = {"items": [
                {
                    "id": vid,
                    "snippet": {
                        "title": "Let&#39;s Test Claude &amp; Friends",
                        "channelTitle": "A &amp; B",
                        "description": "details &amp; data",
                        "publishedAt": "2026-08-01T00:00:00Z",
                    },
                    "contentDetails": {"duration": "PT1H2M3S"},
                }
                for vid in batch
            ]}
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        with mock.patch.object(youtube.urllib.request, "urlopen", side_effect=fake_urlopen):
            found = youtube._list_videos("key", ids)

        self.assertEqual([len(batch) for batch in calls], [50, 10])
        self.assertEqual(len(found), 60)
        video = found[ids[0]]
        self.assertEqual(video["video_title"], "Let's Test Claude & Friends")
        self.assertEqual(video["channel"], "A & B")
        self.assertEqual(video["duration_seconds"], 3723)

    def test_rank_is_estimate_first_keyword_match_tiebreak(self):
        """The model rates, code ranks: fetch order is discovery-estimate
        descending, _match_one on title+description breaking ties."""
        interest = self.interest(
            title="Orexin research", positive_signals=["orexin agonist trial"]
        )
        provider = FakeProvider(search_results=[
            a_discovery_entry(VID1, estimate=0.5),   # tie, no keyword overlap
            a_discovery_entry(VID2, estimate=0.9),   # top estimate wins outright
            a_discovery_entry(VID3, estimate=0.5),   # tie, strong keyword match
        ])
        returned = listed(
            a_video(video_id=VID1, video_title="Unrelated chat", description="nothing much"),
            a_video(video_id=VID2, video_title="Also unrelated", description="nothing much"),
            a_video(video_id=VID3, video_title="Orexin agonist trial results",
                    description="deep dive on the orexin agonist trial data"),
        )
        order = []

        def fake_fetch(video_id, languages):
            order.append(video_id)
            return None

        with mock.patch.object(youtube, "_list_videos", return_value=returned), \
             mock.patch.object(youtube, "_fetch_transcript", side_effect=fake_fetch):
            youtube.collect(interest, self.cfg, provider)

        self.assertEqual(order, [VID2, VID3, VID1])

    def test_videos_over_the_fetch_budget_fall_back_to_video_level_same_cycle(self):
        """A video ranked below `max_transcript_fetches` is never fetched, but
        it isn't dropped either -- it lands as a video-level item this same
        cycle, and its seen row then blocks any future re-listing."""
        conn = db.connect(":memory:")
        db.init(conn)
        self.addCleanup(conn.close)
        interest = self.interest(source_config={"youtube": {"max_transcript_fetches": 1}})
        provider = FakeProvider(search_results=[
            a_discovery_entry(VID1, estimate=0.9), a_discovery_entry(VID2, estimate=0.5),
        ])
        returned = listed(a_video(video_id=VID1), a_video(video_id=VID2))
        snippets = [FakeSnippet("real content about orexin", 0, 10)]
        fetched = []

        def fake_fetch(video_id, languages):
            fetched.append(video_id)
            return snippets

        with mock.patch.object(
            youtube, "_list_videos",
            side_effect=lambda key, ids: {v: returned[v] for v in ids},
        ) as lv, mock.patch.object(youtube, "_fetch_transcript", side_effect=fake_fetch):
            items = youtube.collect(interest, self.cfg, provider, conn)
            self.assertEqual(fetched, [VID1])   # VID2 ranked below the budget: never fetched
            by_video = {i.metadata["video_id"]: i for i in items}
            self.assertEqual(by_video[VID1].type, "video_segment")
            self.assertEqual(by_video[VID2].type, "video")
            self.assertEqual(by_video[VID2].dedup_key, f"{VID2}:video")
            for item in items:
                normalize.normalize(item)
                db.insert_item(conn, item)

            second = youtube.collect(interest, self.cfg, provider, conn)

        self.assertEqual(fetched, [VID1])  # no further fetch attempt for VID2
        # Both videos are already seen (video-level counts too): nothing to
        # re-list, let alone re-fetch, next cycle.
        self.assertEqual(second, [])
        lv.assert_called_once()

    def test_circuit_breaker_stops_fetching_but_keeps_emitting_video_level_items(self):
        """One RequestBlocked/IpBlocked means every further fetch this cycle
        would fail and prolong the block: stop *fetching*, but the remaining
        ranked videos still cost nothing to emit as video-level items."""
        provider = FakeProvider(search_results=[
            a_discovery_entry(VID1, estimate=0.9),
            a_discovery_entry(VID2, estimate=0.8),
            a_discovery_entry(VID3, estimate=0.7),
        ])
        returned = listed(a_video(video_id=VID1), a_video(video_id=VID2),
                          a_video(video_id=VID3))
        calls = []

        def fake_fetch(video_id, languages):
            calls.append(video_id)
            if video_id == VID1:
                return [FakeSnippet("good content before the block", 0, 10)]
            raise youtube.TranscriptBlocked("ip blocked")

        with mock.patch.object(youtube, "_list_videos", return_value=returned), \
             mock.patch.object(youtube, "_fetch_transcript", side_effect=fake_fetch):
            items = youtube.collect(self.interest(), self.cfg, provider)

        self.assertEqual(calls, [VID1, VID2])   # the block trips on VID2; VID3 never attempted
        by_video = {i.metadata["video_id"]: i for i in items}
        self.assertEqual(len(items), 3)
        self.assertEqual(by_video[VID1].type, "video_segment")
        self.assertEqual(by_video[VID2].type, "video")   # blocked -> video-level fallback
        self.assertEqual(by_video[VID3].type, "video")   # never even attempted -> video-level

    def test_a_video_with_no_transcript_falls_back_to_a_video_level_item(self):
        """A transcript miss is not fatal: the video's title+description is
        emitted as a single video-level item instead of being discarded."""
        provider = FakeProvider(search_results=[
            a_discovery_entry(VID1, estimate=0.9), a_discovery_entry(VID2, estimate=0.5),
        ])
        returned = listed(
            a_video(video_id=VID1, video_title="Video One", description="all about orexin"),
            a_video(video_id=VID2),
        )
        snippets = [FakeSnippet("some real content here", 0, 10)]

        def fake_fetch(video_id, languages):
            return snippets if video_id == VID2 else None

        with mock.patch.object(youtube, "_list_videos", return_value=returned), \
             mock.patch.object(youtube, "_fetch_transcript", side_effect=fake_fetch):
            items = youtube.collect(self.interest(), self.cfg, provider)

        by_video = {i.metadata["video_id"]: i for i in items}
        self.assertEqual(len(items), 2)  # VID2's one segment + VID1's video-level fallback

        video_item = by_video[VID1]
        self.assertEqual(video_item.type, "video")
        self.assertEqual(video_item.dedup_key, f"{VID1}:video")
        self.assertEqual(video_item.url, f"https://www.youtube.com/watch?v={VID1}")
        self.assertEqual(video_item.title, "Video One")
        self.assertIn("Video One", video_item.text)
        self.assertIn("all about orexin", video_item.text)
        self.assertEqual(video_item.metadata["discovery_estimate"], 0.9)

        segment_item = by_video[VID2]
        self.assertEqual(segment_item.type, "video_segment")

    def test_limit_stops_at_video_boundaries_never_mid_video(self):
        """A video is chunked in full or not at all: the seen-prefix skip means
        a half-chunked video would permanently lose its later segments."""
        interest = self.interest(
            source_config={"youtube": {"limit": 1, "chunk_seconds": 30,
                                       "chunk_overlap_seconds": 10}},
        )
        provider = FakeProvider(search_results=[
            a_discovery_entry(VID1, estimate=0.9), a_discovery_entry(VID2, estimate=0.5),
        ])
        returned = listed(a_video(video_id=VID1), a_video(video_id=VID2))
        snippets = [FakeSnippet(f"word{i}", i * 10, 10) for i in range(10)]  # 5 chunks
        fetched = []

        def fake_fetch(video_id, languages):
            fetched.append(video_id)
            return snippets

        with mock.patch.object(youtube, "_list_videos", return_value=returned), \
             mock.patch.object(youtube, "_fetch_transcript", side_effect=fake_fetch):
            items = youtube.collect(interest, self.cfg, provider)

        # The first video overshoots the limit but is emitted whole; the
        # second is never even fetched.
        self.assertEqual(len(items), 5)
        self.assertEqual(fetched, [VID1])
        self.assertTrue(all(i.metadata["video_id"] == VID1 for i in items))

    def test_video_id_from_url_handles_watch_youtu_be_and_shorts(self):
        for url in (
            f"https://www.youtube.com/watch?v={VID1}",
            f"https://m.youtube.com/watch?v={VID1}&list=PL123&index=2",
            f"https://youtu.be/{VID1}?t=30",
            f"https://www.youtube.com/shorts/{VID1}",
            f"https://www.youtube.com/live/{VID1}",
            f"https://www.youtube.com/embed/{VID1}",
        ):
            self.assertEqual(youtube._video_id_from_url(url), VID1, url)
        for url in (
            f"https://example.com/watch?v={VID1}",       # not YouTube
            "https://www.youtube.com/watch?v=short",     # not an 11-char id
            "https://www.youtube.com/@somechannel",      # channel, not a video
            "https://www.youtube.com/playlist?list=PL1",
            "not a url at all",
            "",
        ):
            self.assertIsNone(youtube._video_id_from_url(url), url)

    def test_video_level_item_survives_prefilter_on_a_real_description(self):
        """The video-level fallback's text must actually clear the same
        cheap pre-filter every other item goes through (cfg.min_text_chars)."""
        video = a_video(
            video_title="Deep dive on orexin agonists",
            description="A " * 40,  # well past min_text_chars=40
        )
        item = youtube._to_video_item(video)
        matches = [(self.interest(), 0.5, ["orexin"])]
        ok, reason = matching.prefilter(item, matches, self.cfg)
        self.assertTrue(ok, reason)

    def test_video_level_item_dies_on_prefilter_with_an_empty_description(self):
        video = a_video(video_title="X", description="")
        item = youtube._to_video_item(video)
        matches = [(self.interest(), 0.5, ["orexin"])]
        ok, reason = matching.prefilter(item, matches, self.cfg)
        self.assertFalse(ok)
        self.assertIn("chars", reason)

    def _fake_transcript_api_modules(self, api_cls):
        """sys.modules stand-ins for youtube-transcript-api, which CI does not
        install -- _fetch_transcript imports it lazily, so patching sys.modules
        is the whole seam."""
        mod = types.ModuleType("youtube_transcript_api")
        errors = types.ModuleType("youtube_transcript_api._errors")

        class YouTubeTranscriptApiException(Exception):
            pass

        class NoTranscriptFound(YouTubeTranscriptApiException):
            pass

        errors.YouTubeTranscriptApiException = YouTubeTranscriptApiException
        errors.NoTranscriptFound = NoTranscriptFound
        mod.YouTubeTranscriptApi = api_cls
        mod._errors = errors
        return mod, errors

    def test_fetch_transcript_falls_back_to_regional_variant(self):
        """languages=["en"] must still fetch a video whose only English track
        is "en-US" -- the library alone matches codes exactly and misses it."""
        snippets = [FakeSnippet("real words", 0, 5)]

        class FakeTrack:
            language_code = "en-US"

            def fetch(self):
                return snippets

        holder = {}

        class FakeApi:
            def fetch(self, video_id, languages=None):
                raise holder["NoTranscriptFound"]()

            def list(self, video_id):
                return [FakeTrack()]

        mod, errors = self._fake_transcript_api_modules(FakeApi)
        holder["NoTranscriptFound"] = errors.NoTranscriptFound
        with mock.patch.dict(
            sys.modules,
            {"youtube_transcript_api": mod, "youtube_transcript_api._errors": errors},
        ):
            result = youtube._fetch_transcript("vid1", ["en"])
        self.assertEqual(result, snippets)

    def test_fetch_transcript_no_variant_matches_returns_none(self):
        class FakeTrack:
            language_code = "ko"

            def fetch(self):  # pragma: no cover -- must not be called
                raise AssertionError("fetched a non-matching language")

        holder = {}

        class FakeApi:
            def fetch(self, video_id, languages=None):
                raise holder["NoTranscriptFound"]()

            def list(self, video_id):
                return [FakeTrack()]

        mod, errors = self._fake_transcript_api_modules(FakeApi)
        holder["NoTranscriptFound"] = errors.NoTranscriptFound
        with mock.patch.dict(
            sys.modules,
            {"youtube_transcript_api": mod, "youtube_transcript_api._errors": errors},
        ):
            self.assertIsNone(youtube._fetch_transcript("vid1", ["en"]))

    def test_fetch_transcript_raises_transcript_blocked_on_ip_block(self):
        """RequestBlocked/IpBlocked must escape as TranscriptBlocked (for the
        circuit breaker), not be swallowed like an ordinary per-video miss."""
        holder = {}

        class FakeApi:
            def fetch(self, video_id, languages=None):
                raise holder["IpBlocked"]("YouTube is blocking requests from your IP")

        mod, errors = self._fake_transcript_api_modules(FakeApi)

        class IpBlocked(errors.YouTubeTranscriptApiException):
            pass

        holder["IpBlocked"] = IpBlocked
        with mock.patch.dict(
            sys.modules,
            {"youtube_transcript_api": mod, "youtube_transcript_api._errors": errors},
        ):
            with self.assertRaises(youtube.TranscriptBlocked):
                youtube._fetch_transcript("vid1", ["en"])


class NotifyTests(unittest.TestCase):
    def test_dry_run_prints_and_never_calls_the_network(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertTrue(notify.send(CFG, "hello", dry_run=True))
        urlopen.assert_not_called()

    def test_message_shows_the_score_reason_why_it_matches_and_url(self):
        text = notify.format_message(an_interest(), an_item(), 0.91, "Real data.", "Has numbers.")
        self.assertIn("91%", text)
        self.assertIn("Real data.", text)
        self.assertIn("Why this matches me:", text)
        self.assertIn("Has numbers.", text)
        self.assertIn("https://e.com/a", text)

    def test_an_article_is_labelled_discovery_a_market_event_alert(self):
        text = notify.format_message(an_interest(), an_item(), 0.91, "r")
        self.assertIn("DISCOVERY", text)
        item = an_item(type="market_event", text="NBIS -8.40% today")
        text = notify.format_message(an_interest(), item, 0.91, "r")
        self.assertIn("ALERT", text)
        self.assertNotIn("DISCOVERY", text)

    def test_low_confidence_is_flagged_in_the_header(self):
        text = notify.format_message(an_interest(), an_item(), 0.91, "r", confidence=0.3)
        self.assertIn("low confidence", text.splitlines()[0])
        text = notify.format_message(an_interest(), an_item(), 0.91, "r", confidence=0.8)
        self.assertNotIn("low confidence", text)

    def test_market_event_shows_the_combined_summary_not_the_score_reason(self):
        item = an_item(
            type="market_event",
            text="NBIS -8.40% today (-2.00% this week)\n\nConfidence:\nmedium",
        )
        text = notify.format_message(an_interest(), item, 0.91, "a generic scorer sentence")
        self.assertIn("NBIS -8.40% today", text)
        self.assertNotIn("a generic scorer sentence", text)

    def test_video_segment_shows_a_timestamp_range(self):
        item = an_item(type="video_segment", metadata={"start_time": 754, "end_time": 1082})
        text = notify.format_message(an_interest(), item, 0.91, "r")
        self.assertIn("12:34-18:02", text)

    def test_a_non_video_item_has_no_timestamp_range(self):
        text = notify.format_message(an_interest(), an_item(), 0.91, "r")
        self.assertNotIn("⏱", text)

    def test_is_alert_matches_only_registered_types(self):
        self.assertTrue(notify.is_alert(an_item(type="market_event")))
        self.assertFalse(notify.is_alert(an_item(type="article")))

    def test_feedback_keyboard_has_all_four_verdicts_keyed_to_the_score_id(self):
        markup = notify.feedback_keyboard(42)
        buttons = [b for row in markup["inline_keyboard"] for b in row]
        self.assertEqual(len(buttons), 4)
        codes = {b["callback_data"].split(":")[1] for b in buttons}
        self.assertEqual(codes, set(notify.FEEDBACK_VERDICTS))
        self.assertTrue(all(b["callback_data"].endswith(":42") for b in buttons))

    def test_feedback_keyboard_with_no_observatory_url_is_byte_identical_to_before(self):
        # Default (empty observatory_base_url): same as calling with just a
        # score id -- no fifth button, no third row, byte-identical keyboard.
        self.assertEqual(notify.feedback_keyboard(42), notify.feedback_keyboard(42, ""))
        markup = notify.feedback_keyboard(42, "")
        self.assertEqual(len(markup["inline_keyboard"]), 2)
        self.assertEqual(sum(len(row) for row in markup["inline_keyboard"]), 4)

    def test_feedback_keyboard_appends_a_trace_button_when_observatory_url_is_set(self):
        markup = notify.feedback_keyboard(42, "https://observatory.example.com")
        # The original four feedback buttons + their callback_data are
        # untouched -- only a new third row is appended.
        self.assertEqual(markup["inline_keyboard"][:2], notify.feedback_keyboard(42)["inline_keyboard"])
        self.assertEqual(len(markup["inline_keyboard"]), 3)
        trace_button = markup["inline_keyboard"][2][0]
        self.assertEqual(trace_button["url"], "https://observatory.example.com/observatory/trace/score/42")
        self.assertNotIn("callback_data", trace_button)

    def test_feedback_keyboard_strips_a_trailing_slash_on_the_base_url(self):
        markup = notify.feedback_keyboard(1, "https://observatory.example.com/")
        self.assertEqual(
            markup["inline_keyboard"][2][0]["url"],
            "https://observatory.example.com/observatory/trace/score/1",
        )

    def test_feedback_keyboard_skips_the_trace_button_for_a_schemeless_base_url(self):
        # Telegram rejects the WHOLE sendMessage (BUTTON_URL_INVALID) for a
        # malformed inline URL button -- an operator typo like
        # DISCOVERY_OBSERVATORY_BASE_URL='localhost:8001' (no scheme) must
        # not reach the keyboard at all; the other four buttons stay intact.
        markup = notify.feedback_keyboard(1, "localhost:8001")
        self.assertEqual(markup, notify.feedback_keyboard(1))
        self.assertEqual(len(markup["inline_keyboard"]), 2)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        db.upsert_interest(self.conn, an_interest(sources=["fake"], min_score=0.70))
        self.interests = db.active_interests(self.conn)

    def _collector(self):
        """A fresh pair of items per call -- ingest() mutates what it is given,
        so a shared list would make the second cycle test a lie."""
        return lambda interest, cfg, provider, conn=None: [
            an_item(source="fake", url="https://e.com/good", title="Good"),
            an_item(source="fake", url="https://e.com/meh", title="Meh"),
        ]

    def _run(self, provider, collector=None):
        with mock.patch.dict(COLLECTORS, {"fake": collector or self._collector()}):
            return pipeline.run_once(self.conn, provider, CFG, dry_run=True)

    def test_full_cycle_scores_everything_but_a_discovery_item_awaits_the_digest(self):
        # "Good"/"Meh" are plain article-type items -- DISCOVERY, not ALERT --
        # so run_once's own deliver() (Alerts only) sends nothing; the item
        # above the bar is left pending for send_digest() (see below).
        provider = FakeProvider({"Good": 0.9, "Meh": 0.2})
        summary = self._run(provider)
        self.assertEqual(
            summary,
            {"collected": 2, "duplicate": 0, "near_duplicate": 0, "filtered": 0,
             "already_scored": 0, "scored": 2, "deferred": 0, "errors": 0,
             "notified": 0},
        )
        self.assertEqual(pipeline.send_digest(self.conn, CFG, dry_run=True), 1)

    def test_a_second_cycle_re_collects_but_never_re_scores_or_re_notifies(self):
        provider = FakeProvider({"Good": 0.9, "Meh": 0.2})
        self._run(provider)
        summary = self._run(provider)
        self.assertEqual(summary["duplicate"], 2)
        self.assertEqual((summary["scored"], summary["notified"]), (0, 0))
        self.assertEqual(len(provider.prompts), 2)  # only the first cycle paid

    def test_a_failing_collector_does_not_abort_the_cycle(self):
        def boom(interest, cfg, provider, conn=None):
            raise RuntimeError("network down")

        summary = self._run(FakeProvider(), collector=boom)
        self.assertEqual(summary["collected"], 0)
        self.assertEqual(summary["errors"], 0)

    def test_a_failing_score_skips_only_that_item(self):
        provider = FakeProvider({"Good": RuntimeError("bad json"), "Meh": 0.95})
        summary = self._run(provider)
        self.assertEqual((summary["errors"], summary["scored"], summary["notified"]), (1, 1, 0))

    def test_an_item_left_unscored_by_a_dead_cycle_is_picked_up_next_time(self):
        provider = FakeProvider({"Good": RuntimeError("api down"), "Meh": 0.1})
        self._run(provider)
        unscored = self.conn.execute(
            "SELECT title FROM candidate_items WHERE prefilter_ok = 1 AND id NOT IN"
            " (SELECT item_id FROM scores)"
        ).fetchall()
        self.assertEqual([r["title"] for r in unscored], ["Good"])

        # Immediately after the failure the backlog leaves the item alone --
        # its cool-off has not passed, so an outage is not re-failed per cycle.
        summary = self._run(FakeProvider({"Good": 0.9, "Meh": 0.1}))
        self.assertEqual((summary["duplicate"], summary["scored"], summary["notified"]), (2, 0, 0))

        # Once the cool-off has elapsed the item is scored -- never lost.
        self.conn.execute(
            "UPDATE candidate_items SET score_attempted_at = ?",
            (db.ago(db.SCORE_RETRY_SECONDS + 60),),
        )
        summary = self._run(FakeProvider({"Good": 0.9, "Meh": 0.1}))
        self.assertEqual((summary["duplicate"], summary["scored"], summary["notified"]), (2, 1, 0))

    def test_scoring_sets_the_outcomes_score_id_to_the_saved_row(self):
        # Regression: _score() used to call db.save_score() and discard the
        # returned id, so outcome.score.id stayed None forever -- unlike
        # item.id, which ingest() already assigns from db.insert_item()'s
        # return value one line above it. Nothing in the production pipeline
        # happened to read it back this way (deliver()/send_digest() always
        # re-query score_id fresh from the DB), but any caller reasonably
        # expecting the same contract item.id already carries would get a
        # silent None instead of the real row id.
        provider = FakeProvider({"A title": 0.9})
        outcome = pipeline.ingest(self.conn, provider, CFG, an_item(), self.interests, "k")
        self.assertEqual(outcome.stage, "scored")
        self.assertIsNotNone(outcome.score.id)
        row = self.conn.execute(
            "SELECT id FROM scores WHERE item_id = ?", (outcome.item.id,)
        ).fetchone()
        self.assertEqual(outcome.score.id, row["id"])

    def test_a_duplicate_reports_the_item_it_collided_with(self):
        provider = FakeProvider({"A title": 0.9})
        first = pipeline.ingest(self.conn, provider, CFG, an_item(), self.interests, "k")
        self.assertEqual(first.stage, "scored")

        again = pipeline.ingest(
            self.conn, provider, CFG,
            an_item(url="https://www.e.com/a/?utm_source=x"), self.interests, "k",
        )
        self.assertEqual((again.stage, again.detail), ("duplicate", "same url"))
        self.assertEqual(again.item.id, first.item.id)
        self.assertEqual(len(provider.prompts), 1)

    def test_a_thin_item_is_filtered_before_any_model_call(self):
        provider = FakeProvider()
        outcome = pipeline.ingest(
            self.conn, provider, CFG, an_item(text="tiny"), self.interests, "k"
        )
        self.assertEqual(outcome.stage, "filtered")
        self.assertIn("chars of text", outcome.detail)
        self.assertEqual(provider.prompts, [])
        # The verdict is persisted, so a later cycle does not re-filter it.
        row = self.conn.execute(
            "SELECT prefilter_ok FROM candidate_items WHERE id = ?", (outcome.item.id,)
        ).fetchone()
        self.assertEqual(row["prefilter_ok"], 0)

    def test_re_ingesting_a_stored_item_reports_already_scored_unless_forced(self):
        provider = FakeProvider({"A title": 0.9})
        scored = pipeline.ingest(self.conn, provider, CFG, an_item(), self.interests, "k")

        # This is the `score --item-id` path: the item carries its own id, so
        # dedup skips itself and the existing score is what stops the call.
        reloaded = db.get_item(self.conn, scored.item.id)
        again = pipeline.ingest(self.conn, provider, CFG, reloaded, self.interests)
        self.assertEqual(again.stage, "already_scored")
        self.assertEqual(len(provider.prompts), 1)

        forced = pipeline.ingest(
            self.conn, FakeProvider({"A title": 0.3}), CFG,
            db.get_item(self.conn, scored.item.id), self.interests, force=True,
        )
        self.assertEqual(forced.stage, "scored")
        self.assertAlmostEqual(forced.score.final_score, 0.3)
        row = self.conn.execute(
            "SELECT count(*) c FROM scores WHERE item_id = ?", (scored.item.id,)
        ).fetchone()
        self.assertEqual(row["c"], 1)

    def test_outcome_as_dict_is_json_serialisable_and_carries_the_verdict(self):
        outcome = pipeline.ingest(
            self.conn, FakeProvider({"A title": 0.9}), CFG, an_item(), self.interests, "k"
        )
        payload = json.loads(json.dumps(outcome.as_dict()))
        self.assertEqual(payload["stage"], "scored")
        self.assertEqual(payload["item"]["url"], "https://e.com/a")
        self.assertEqual(payload["matched_interests"][0]["key"], "k")
        self.assertAlmostEqual(payload["score"]["final_score"], 0.9)
        self.assertEqual(payload["score"]["provider"], "fake")

    def test_an_unknown_source_is_reported_and_skipped(self):
        db.upsert_interest(self.conn, an_interest(key="bogus", sources=["nope"]))
        summary = self._run(FakeProvider({"Good": 0.9, "Meh": 0.2}))
        self.assertEqual(summary["collected"], 2)

    def test_deliver_sends_only_alert_type_items_immediately(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        alert = stored_item(self.conn, type="market_event", url="https://e.com/alert", title="Alert")
        discovery = stored_item(self.conn, url="https://e.com/disc", title="Disc")
        db.save_score(self.conn, a_score(alert.id, interest.id, 0.9))
        db.save_score(self.conn, a_score(discovery.id, interest.id, 0.9))

        self.assertEqual(pipeline.deliver(self.conn, CFG, dry_run=True), 1)
        pending = [r["item_id"] for r in db.pending_notifications(self.conn)]
        self.assertEqual(pending, [discovery.id])

    def test_immediate_discovery_delivers_fresh_discoveries_bounded_by_cycle(self):
        # With cfg.immediate_discovery on, deliver() also pushes freshly-scored
        # discoveries -- but no more than immediate_max_per_cycle per call, so a
        # big batch trickles out rather than flooding the owner.
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        for i in range(3):
            d = stored_item(self.conn, url=f"https://e.com/d{i}", title=f"D{i}")
            db.save_score(self.conn, a_score(d.id, interest.id, 0.9))
        cfg = dataclasses.replace(CFG, immediate_discovery=True, immediate_max_per_cycle=2)
        self.assertEqual(pipeline.deliver(self.conn, cfg, dry_run=True), 2)
        self.assertEqual(len(db.pending_notifications(self.conn)), 1)

    def test_immediate_discovery_skips_the_stale_backlog(self):
        # The existing pre-enable backlog must never be dumped immediately: only
        # scores newer than immediate_fresh_seconds go out; older ones stay
        # pending for the digest exactly as before.
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        old = stored_item(self.conn, url="https://e.com/old", title="Old")
        sid = db.save_score(self.conn, a_score(old.id, interest.id, 0.9))
        self.conn.execute("UPDATE scores SET created_at = ? WHERE id = ?", (db.ago(10_000), sid))
        self.conn.commit()
        cfg = dataclasses.replace(CFG, immediate_discovery=True)
        self.assertEqual(pipeline.deliver(self.conn, cfg, dry_run=True), 0)
        self.assertEqual([r["item_id"] for r in db.pending_notifications(self.conn)], [old.id])

    def test_immediate_day_cap_counts_only_immediate_sends(self):
        # A busy digest/alert day must not zero the immediate per-day budget:
        # the cap counts only prior immediate-channel sends, so the feature keeps
        # firing regardless of how many digest items already went out today.
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        for i in range(50):  # 50 prior NON-immediate (digest/alert) sends today
            it = stored_item(self.conn, url=f"https://e.com/old{i}", title=f"Old{i}")
            sid = db.save_score(self.conn, a_score(it.id, interest.id, 0.9))
            db.record_notification(self.conn, sid, "telegram", True)
        fresh = stored_item(self.conn, url="https://e.com/fresh", title="Fresh")
        db.save_score(self.conn, a_score(fresh.id, interest.id, 0.9))
        cfg = dataclasses.replace(CFG, immediate_discovery=True, immediate_max_per_day=40)
        self.assertEqual(pipeline.deliver(self.conn, cfg, dry_run=True), 1)

    def test_send_digest_sorts_by_score_and_caps_leaving_the_rest_pending(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        low = stored_item(self.conn, url="https://e.com/low", title="Low")
        high = stored_item(self.conn, url="https://e.com/high", title="High")
        db.save_score(self.conn, a_score(low.id, interest.id, 0.71))
        db.save_score(self.conn, a_score(high.id, interest.id, 0.95))

        cfg = dataclasses.replace(CFG, digest_max_items=1)
        self.assertEqual(pipeline.send_digest(self.conn, cfg, dry_run=True), 1)
        still_pending = {
            db.get_item(self.conn, r["item_id"]).title for r in db.pending_notifications(self.conn)
        }
        self.assertEqual(still_pending, {"Low"})

    def test_send_digest_never_touches_alert_items(self):
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        alert = stored_item(self.conn, type="market_event", url="https://e.com/alert", title="Alert")
        db.save_score(self.conn, a_score(alert.id, interest.id, 0.9))
        self.assertEqual(pipeline.send_digest(self.conn, CFG, dry_run=True), 0)

    def test_run_once_sources_restricts_which_collectors_run(self):
        db.upsert_interest(self.conn, an_interest(sources=["fake", "fake2"]))
        calls = []
        with mock.patch.dict(
            COLLECTORS,
            {
                "fake": lambda i, c, p, conn=None: calls.append("fake") or [],
                "fake2": lambda i, c, p, conn=None: calls.append("fake2") or [],
            },
        ):
            pipeline.run_once(self.conn, FakeProvider(), CFG, sources=["fake"], dry_run=True)
        self.assertEqual(calls, ["fake"])

    def test_the_cycle_score_budget_defers_the_surplus_instead_of_paying(self):
        cfg = dataclasses.replace(CFG, max_scores_per_cycle=1)
        provider = FakeProvider({"Good": 0.9, "Meh": 0.2})
        with mock.patch.dict(COLLECTORS, {"fake": self._collector()}):
            summary = pipeline.run_once(self.conn, provider, cfg, dry_run=True)
        self.assertEqual((summary["scored"], summary["deferred"]), (1, 1))
        self.assertEqual(len(provider.prompts), 1)

        # The deferred item was stored and pre-filtered, so the next cycle's
        # backlog pass picks it up rather than it being lost.
        with mock.patch.dict(COLLECTORS, {"fake": self._collector()}):
            summary = pipeline.run_once(self.conn, provider, cfg, dry_run=True)
        self.assertEqual((summary["duplicate"], summary["scored"]), (2, 1))
        self.assertEqual(len(provider.prompts), 2)

    def test_the_backlog_pass_is_capped_by_the_same_budget(self):
        cfg = dataclasses.replace(CFG, max_scores_per_cycle=2)
        for n in range(5):
            item = stored_item(self.conn, url=f"https://e.com/backlog-{n}", title="Meh")
            db.set_prefilter(self.conn, item.id, True, "ok")
        provider = FakeProvider({"Meh": 0.5})
        with mock.patch.dict(COLLECTORS, {"fake": lambda i, c, p, conn=None: []}):
            summary = pipeline.run_once(self.conn, provider, cfg, dry_run=True)
        self.assertEqual(summary["scored"], 2)
        self.assertEqual(len(provider.prompts), 2)

    def test_run_once_records_the_funnel_and_the_provider_spend(self):
        provider = FakeProvider({"Good": 0.9, "Meh": 0.2})
        provider.record_usage(input_tokens=40, output_tokens=4)
        self._run(provider)
        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(metrics["collected"], 2)
        self.assertEqual(metrics["scored"], 2)
        usage = self.conn.execute("SELECT * FROM llm_usage").fetchone()
        self.assertEqual((usage["model"], usage["input_tokens"]), ("fake-1", 40))

    def test_a_crash_after_the_first_item_still_leaves_its_metrics_recorded(self):
        # Regression: counts used to accumulate in a local Counter() and flush
        # via one db.bump() call at the very end of run_once() -- so an
        # exception anywhere after the first item (a real one already hit
        # this: notify.print_safe missing in __main__.py's own discover loop,
        # see the sibling fix there) silently discarded every already-scored
        # item's funnel counters, even though their DB rows were already
        # durably committed by ingest(). Metrics must now survive that.
        provider = FakeProvider({"Good": 0.9, "Meh": 0.5})

        def collector(interest, cfg, provider, conn=None):
            return [
                an_item(source="fake", url="https://e.com/good", title="Good"),
                an_item(source="fake", url="https://e.com/meh", title="Meh"),
            ]

        # Neither item needs to fail scoring itself -- ingest() already
        # handles that internally (an "errors" outcome, still bumped). What
        # this simulates is any *other* exception landing between items
        # (a print crash, a bad future change, an OS hiccup): patch db.bump
        # itself to blow up on its second call, i.e. right as "Meh"'s metrics
        # would be flushed -- after "Good"'s have already landed for real.
        calls = []
        real_bump = db.bump

        def flaky_bump(conn, counts):
            calls.append(counts)
            if len(calls) == 2:
                raise RuntimeError("simulated crash mid-cycle")
            real_bump(conn, counts)

        with mock.patch.dict(COLLECTORS, {"fake": collector}), \
             mock.patch.object(db, "bump", flaky_bump):
            with self.assertRaises(RuntimeError):
                pipeline.run_once(self.conn, provider, CFG, dry_run=True)

        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        # "Good"'s own collected/scored counts made it in before the crash --
        # they are not held hostage by whatever happens to "Meh" right after.
        self.assertEqual(metrics.get("collected"), 1)
        self.assertEqual(metrics.get("scored"), 1)
        score_row = self.conn.execute(
            "SELECT 1 FROM candidate_items WHERE title = 'Good'"
        ).fetchone()
        self.assertIsNotNone(score_row)  # and its DB row is there regardless

    def test_collectors_are_handed_the_connection_for_skip_checks(self):
        seen = []
        with mock.patch.dict(
            COLLECTORS, {"fake": lambda i, c, p, conn=None: seen.append(conn) or []}
        ):
            pipeline.run_once(self.conn, FakeProvider(), CFG, dry_run=True)
        self.assertEqual(seen, [self.conn])

    # --- failed-send retry ----------------------------------------------------

    def _pending_titles(self):
        return [
            db.get_item(self.conn, r["item_id"]).title
            for r in db.pending_notifications(self.conn)
        ]

    def _failed_send(self):
        """One alert whose Telegram send failed (send() returned False)."""
        db.upsert_interest(self.conn, an_interest())
        (interest,) = db.active_interests(self.conn)
        alert = stored_item(self.conn, type="market_event", url="https://e.com/alert", title="Alert")
        score_id = db.save_score(self.conn, a_score(alert.id, interest.id, 0.9))
        with mock.patch.object(notify, "send", return_value=False):
            self.assertEqual(pipeline.deliver(self.conn, CFG), 1)
        return score_id

    def test_a_failed_send_is_not_marked_delivered_and_retries_after_cooloff(self):
        score_id = self._failed_send()
        row = self.conn.execute(
            "SELECT ok, attempts FROM notifications WHERE score_id = ?", (score_id,)
        ).fetchone()
        self.assertEqual((row["ok"], row["attempts"]), (0, 1))

        # Fresh failure: not yet eligible, so the next cycle does not hammer it.
        self.assertEqual(self._pending_titles(), [])

        # After the cool-off it is pending again; a successful retry is final.
        self.conn.execute(
            "UPDATE notifications SET sent_at = ?",
            (db.ago(db.RESEND_FAILED_AFTER_SECONDS + 60),),
        )
        self.assertEqual(self._pending_titles(), ["Alert"])
        with mock.patch.object(notify, "send", return_value=True):
            self.assertEqual(pipeline.deliver(self.conn, CFG), 1)
        row = self.conn.execute(
            "SELECT ok, attempts FROM notifications WHERE score_id = ?", (score_id,)
        ).fetchone()
        self.assertEqual((row["ok"], row["attempts"]), (1, 2))
        self.assertEqual(self._pending_titles(), [])  # never re-sent after success

    def test_a_send_that_keeps_failing_gives_up_after_the_attempt_cap(self):
        score_id = self._failed_send()
        for _ in range(db.MAX_SEND_ATTEMPTS - 1):
            self.conn.execute(
                "UPDATE notifications SET sent_at = ?",
                (db.ago(db.RESEND_FAILED_AFTER_SECONDS + 60),),
            )
            self.assertEqual(self._pending_titles(), ["Alert"])
            with mock.patch.object(notify, "send", return_value=False):
                pipeline.deliver(self.conn, CFG)

        row = self.conn.execute(
            "SELECT ok, attempts FROM notifications WHERE score_id = ?", (score_id,)
        ).fetchone()
        self.assertEqual((row["ok"], row["attempts"]), (0, db.MAX_SEND_ATTEMPTS))
        # Cap reached: even a stale failure is no longer pending.
        self.conn.execute(
            "UPDATE notifications SET sent_at = ?",
            (db.ago(db.RESEND_FAILED_AFTER_SECONDS + 60),),
        )
        self.assertEqual(self._pending_titles(), [])

    # --- exploration lane (step-10): default-off no-op ------------------------

    def test_default_off_never_writes_an_explore_metric_or_shows_the_section(self):
        """With DISCOVERY_DYNAMIC_INTERESTS off (CFG's default) and no derived
        interest anywhere in play, run_once is byte-identical to before this
        step: same summary shape, and the metrics table never grows an
        explore_* name; stats.report has no EXPLORATION section either."""
        provider = FakeProvider({"Good": 0.9, "Meh": 0.2})
        summary = self._run(provider)
        self.assertEqual(
            summary,
            {"collected": 2, "duplicate": 0, "near_duplicate": 0, "filtered": 0,
             "already_scored": 0, "scored": 2, "deferred": 0, "errors": 0,
             "notified": 0},
        )
        names = {r["name"] for r in self.conn.execute("SELECT DISTINCT name FROM metrics").fetchall()}
        self.assertFalse(any(n.startswith("explore_") for n in names))
        self.assertNotIn("EXPLORATION", stats.report(self.conn, days=7, cfg=CFG))

    def test_default_off_a_stray_active_derived_row_still_cannot_spend_explore_budget(self):
        """Defense in depth: interest_state.py never creates a non-owner row
        while the flag is off, but even a manually-inserted active derived
        row can't spend an exploration score -- explore_budget is
        Budget(0) whenever cfg.dynamic_interests is False, structurally, not
        merely filtered out afterwards."""
        db.upsert_derived_interest(
            self.conn,
            an_interest(
                key="derived:zeta", title="Zeta signal", layer="inferred", sources=[],
                positive_signals=["exclusive zeta research"],
            ),
            {},
        )

        def collector(interest, cfg, provider, conn=None):
            return [an_item(source="fake", title="Exclusive zeta research finding",
                             url="https://e.com/zeta")]

        provider = FakeProvider({"zeta": 0.9})
        with mock.patch.dict(COLLECTORS, {"fake": collector}):
            summary = pipeline.run_once(self.conn, provider, CFG, dry_run=True)
        self.assertEqual((summary["deferred"], summary["scored"]), (1, 0))

        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(metrics.get("explore_deferred"), 1)
        self.assertNotIn("deferred", metrics)
        self.assertNotIn("scored", metrics)


class LaneProvider(FakeProvider):
    """Like FakeProvider, but each needle can also pick which interest_key
    the model claims (default 'k') -- the exploration tests need a score
    landing on a *specific* interest (owner vs. derived) to prove
    notification attribution stays split, not just the match-time lane."""

    def __init__(self, scores, keys=None):
        super().__init__(scores)
        self.keys = keys or {}

    def complete_json(self, system, prompt, schema, max_tokens=2000):
        if "<already_stored>" in prompt:
            return self._dedup_verdict(prompt)
        self.prompts.append(prompt)
        for needle, value in self.scores.items():
            if needle in prompt:
                if isinstance(value, Exception):
                    raise value
                return self._payload(value, self.keys.get(needle, "k"))
        raise AssertionError(f"LaneProvider got an unexpected prompt:\n{prompt}")


class ExplorationLaneTests(unittest.TestCase):
    """step-10: exploit (owner-layer) vs explore (derived/inferred-layer)
    lane separation at the scoring boundary. classify_lane()'s rule: an
    item's lane is 'explore' iff matches[0] (match_interests()'s own
    strongest-first order) is a non-owner interest."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        db.upsert_interest(self.conn, an_interest(
            key="k", positive_signals=["good stuff"], min_score=0.70, sources=["fake"],
        ))
        db.upsert_derived_interest(
            self.conn,
            an_interest(
                key="derived:zeta", title="Zeta signal", layer="inferred", sources=[],
                positive_signals=["exclusive zeta research"], min_score=0.80,
            ),
            {},
        )
        self.cfg = dataclasses.replace(CFG, dynamic_interests=True, explore_max_scores_per_cycle=2)

    def _owner_item(self, n):
        return an_item(source="fake", title=f"Owner item {n}", url=f"https://e.com/owner-{n}")

    def _explore_item(self, n):
        return an_item(
            source="fake", title=f"Exclusive zeta research finding {n}",
            url=f"https://e.com/explore-{n}",
        )

    # --- budget cap + isolation ------------------------------------------------

    def test_explore_budget_cap_is_enforced_without_starving_owner_items(self):
        owner_items = [self._owner_item(n) for n in range(3)]
        explore_items = [self._explore_item(n) for n in range(5)]  # cap is 2

        def collector(interest, cfg, provider, conn=None):
            return owner_items + explore_items if interest.key == "k" else []

        provider = FakeProvider({"Owner item": 0.9, "Exclusive zeta research": 0.9})
        with mock.patch.dict(COLLECTORS, {"fake": collector}):
            pipeline.run_once(self.conn, provider, self.cfg, dry_run=True)

        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(metrics.get("explore_scored"), 2)     # the cap, exactly
        self.assertEqual(metrics.get("explore_deferred"), 3)   # 5 - 2 overflow
        self.assertEqual(metrics.get("scored"), 3)             # every owner item, untouched
        self.assertNotIn("deferred", metrics)                  # exploit budget never ran dry
        explore_calls = sum(1 for p in provider.prompts if "Exclusive zeta research" in p)
        self.assertEqual(explore_calls, 2)

    def test_exhausted_exploit_budget_does_not_draw_from_explore_budget(self):
        cfg = dataclasses.replace(self.cfg, max_scores_per_cycle=1)
        owner_items = [self._owner_item(n) for n in range(3)]
        explore_items = [self._explore_item(n) for n in range(2)]

        def collector(interest, cfg, provider, conn=None):
            return owner_items + explore_items if interest.key == "k" else []

        provider = FakeProvider({"Owner item": 0.9, "Exclusive zeta research": 0.9})
        with mock.patch.dict(COLLECTORS, {"fake": collector}):
            pipeline.run_once(self.conn, provider, cfg, dry_run=True)

        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(metrics.get("scored"), 1)             # the exploit cap, exactly
        self.assertEqual(metrics.get("deferred"), 2)           # 3 owner - 1 scored
        self.assertEqual(metrics.get("explore_scored"), 2)     # both explore items, untouched
        self.assertNotIn("explore_deferred", metrics)

    # --- metrics isolation -------------------------------------------------------

    def test_metrics_isolation_owner_and_derived_notifications_split(self):
        owner_item = an_item(
            source="fake", type="market_event", title="Owner alert",
            url="https://e.com/owner-alert",
        )
        explore_item = an_item(
            source="fake", type="market_event",
            title="Exclusive zeta research breakthrough",
            url="https://e.com/explore-alert",
        )

        def collector(interest, cfg, provider, conn=None):
            return [owner_item, explore_item] if interest.key == "k" else []

        provider = LaneProvider(
            {"Owner alert": 0.95, "Exclusive zeta research": 0.95},
            keys={"Owner alert": "k", "Exclusive zeta research": "derived:zeta"},
        )
        with mock.patch.dict(COLLECTORS, {"fake": collector}):
            summary = pipeline.run_once(self.conn, provider, self.cfg, dry_run=True)
        self.assertEqual(summary["notified"], 2)   # both attempted, neither dropped

        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(metrics.get("notified"), 1)
        self.assertEqual(metrics.get("explore_notified"), 1)

        text = stats.report(self.conn, days=7)
        self.assertRegex(text, r"notifications sent\s+1\s")           # FUNNEL: owner only
        self.assertRegex(text, r"(?m)^k\s+1\s+1\s+100%")              # owner-only per-interest table
        self.assertIn("EXPLORATION", text)
        self.assertRegex(text, r"(?m)^derived:zeta\s+1\s+1\s+100%")   # derived-only table

    def test_containment_explore_error_does_not_touch_exploit(self):
        owner_item = an_item(
            source="fake", type="market_event", title="Owner alert",
            url="https://e.com/owner-alert",
        )
        explore_item = self._explore_item(0)

        def collector(interest, cfg, provider, conn=None):
            return [owner_item, explore_item] if interest.key == "k" else []

        provider = FakeProvider({
            "Owner alert": 0.95,
            "Exclusive zeta research": RuntimeError("boom"),
        })
        with mock.patch.dict(COLLECTORS, {"fake": collector}):
            summary = pipeline.run_once(self.conn, provider, self.cfg, dry_run=True)

        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["notified"], 1)
        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(metrics.get("explore_errors"), 1)
        self.assertNotIn("errors", metrics)
        self.assertEqual(metrics.get("scored"), 1)

    # --- backlog lane fairness ---------------------------------------------------

    def test_backlog_lane_fairness_zero_explore_budget_still_drains_owner(self):
        # Owner rows inserted first (lower ids); explore rows inserted after
        # (higher ids, so they sort newest-first) and outnumber the exploit
        # budget -- a fixture with 1-of-each can't distinguish a correct
        # per-lane page from a single `ORDER BY id DESC LIMIT
        # budget+explore_budget` pass that happens to land entirely on the
        # unbudgeted lane and never even reaches the owner rows.
        cfg = dataclasses.replace(self.cfg, explore_max_scores_per_cycle=0, max_scores_per_cycle=2)
        owner_items = []
        for n in range(3):
            item = stored_item(self.conn, url=f"https://e.com/owner-b{n}", title=f"Owner item {n}")
            db.set_prefilter(self.conn, item.id, True, "ok")
            owner_items.append(item)
        explore_items = []
        for n in range(5):  # > the exploit budget (2), all newer than every owner row
            item = stored_item(
                self.conn, url=f"https://e.com/explore-b{n}",
                title=f"Exclusive zeta research finding {n}",
            )
            db.set_prefilter(self.conn, item.id, True, "ok")
            explore_items.append(item)

        def scored_ids(items):
            return {
                item.id for item in items
                if self.conn.execute(
                    "SELECT 1 FROM scores WHERE item_id = ?", (item.id,)
                ).fetchone()
            }

        provider = FakeProvider({"Owner item": 0.9})
        with mock.patch.dict(COLLECTORS, {"fake": lambda i, c, p, conn=None: []}):
            summary = pipeline.run_once(self.conn, provider, cfg, dry_run=True)
        # The exploit budget (2) caps how many owner items land this cycle --
        # the point being that it drains at all despite the newer, unbudgeted
        # explore rows sitting on top of the backlog.
        self.assertEqual(summary["scored"], 2)
        self.assertEqual(len(scored_ids(owner_items)), 2)
        self.assertEqual(scored_ids(explore_items), set())

        # Next cycle: raise the exploit budget so the remaining owner item
        # drains too, still with explore budget at 0.
        cfg2 = dataclasses.replace(cfg, max_scores_per_cycle=5)
        with mock.patch.dict(COLLECTORS, {"fake": lambda i, c, p, conn=None: []}):
            pipeline.run_once(self.conn, provider, cfg2, dry_run=True)
        self.assertEqual(scored_ids(owner_items), {i.id for i in owner_items})
        self.assertEqual(scored_ids(explore_items), set())

        # Explore budget is back (self.cfg's cap of 2) -- skipped items are
        # scored, never dropped.
        provider2 = FakeProvider({"Exclusive zeta research": 0.9})
        with mock.patch.dict(COLLECTORS, {"fake": lambda i, c, p, conn=None: []}):
            summary2 = pipeline.run_once(self.conn, provider2, self.cfg, dry_run=True)
        self.assertEqual(summary2["scored"], 2)
        self.assertEqual(len(scored_ids(explore_items)), 2)

    # --- classification ------------------------------------------------------

    def test_classification_owner_best_match_wins_even_with_a_weaker_derived_match(self):
        item = an_item(
            source="fake", title="Good stuff about zeta research trends",
            url="https://e.com/mixed",
        )

        def collector(interest, cfg, provider, conn=None):
            return [item] if interest.key == "k" else []

        provider = FakeProvider({"Good stuff": 0.8})
        with mock.patch.dict(COLLECTORS, {"fake": collector}):
            summary = pipeline.run_once(self.conn, provider, self.cfg, dry_run=True)

        self.assertEqual(summary["scored"], 1)
        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(metrics.get("scored"), 1)
        self.assertNotIn("explore_scored", metrics)
        row = self.conn.execute(
            "SELECT n.key FROM scores s JOIN interests n ON n.id = s.interest_id"
        ).fetchone()
        self.assertEqual(row["key"], "k")

    # --- _discover CLI path ---------------------------------------------------

    def test_discover_cli_path_enforces_both_budgets(self):
        from discovery.__main__ import _discover

        def collector(interest, cfg, provider, conn=None):
            if interest.key != "k":
                return []
            return [self._explore_item(n) for n in range(4)]  # cap is 2

        provider = FakeProvider({"Exclusive zeta research": 0.9})
        with mock.patch.dict(COLLECTORS, {"fake": collector}), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            code = _discover(self.conn, provider, self.cfg, SimpleNamespace(source="fake"))
        self.assertEqual(code, 0)

        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(metrics.get("explore_scored"), 2)
        self.assertEqual(metrics.get("explore_deferred"), 2)
        self.assertNotIn("scored", metrics)


class FeedbackListenerTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        db.upsert_interest(self.conn, an_interest())
        (self.interest,) = db.active_interests(self.conn)
        self.item = stored_item(self.conn)
        self.score_id = db.save_score(self.conn, a_score(self.item.id, self.interest.id, 0.8))

    def _callback(self, data):
        return {"id": "cb1", "data": data, "message": {"chat": {"id": 123}}}

    def test_a_recognized_button_records_feedback_and_acks_with_the_label(self):
        with mock.patch.object(feedback_listener, "api_call") as api_call:
            recorded = feedback_listener._handle_callback(
                self.conn, "tok", self._callback(f"fb:fire:{self.score_id}")
            )
        self.assertTrue(recorded)
        row = self.conn.execute("SELECT * FROM feedback WHERE item_id = ?", (self.item.id,)).fetchone()
        self.assertEqual(row["verdict"], "fire")
        self.assertAlmostEqual(row["original_score"], 0.8)
        self.assertEqual(row["interest_id"], self.interest.id)
        api_call.assert_called_once_with(
            "tok", "answerCallbackQuery",
            {"callback_query_id": "cb1", "text": f"Recorded: {notify.FEEDBACK_VERDICTS['fire']}"},
        )

    def test_garbage_callback_data_is_acked_but_records_nothing(self):
        with mock.patch.object(feedback_listener, "api_call") as api_call:
            recorded = feedback_listener._handle_callback(
                self.conn, "tok", self._callback("not-a-real-payload")
            )
        self.assertFalse(recorded)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 0)
        api_call.assert_called_once_with("tok", "answerCallbackQuery", {"callback_query_id": "cb1"})

    def test_a_score_id_that_no_longer_exists_is_acked_without_a_crash(self):
        with mock.patch.object(feedback_listener, "api_call") as api_call:
            recorded = feedback_listener._handle_callback(self.conn, "tok", self._callback("fb:up:999999"))
        self.assertFalse(recorded)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 0)
        api_call.assert_called_once()

    def _cfg(self):
        return dataclasses.replace(CFG, telegram_bot_token="tok", telegram_chat_id="123")

    def test_drain_handles_every_update_and_persists_the_offset(self):
        updates = [
            {"update_id": 10, "callback_query": self._callback(f"fb:fire:{self.score_id}")},
            {"update_id": 11, "callback_query": self._callback("garbage")},
        ]
        def fake_api_call(token, method, params, timeout=15):
            if method == "getUpdates":
                return updates
            return {}  # answerCallbackQuery, from _handle_callback

        with mock.patch.object(feedback_listener, "api_call", side_effect=fake_api_call) as api_call:
            count = feedback_listener.drain(self.conn, self._cfg())
        # The garbage update is acked but must not count -- feedback_recorded
        # is evidence of real feedback, not "how many updates were seen".
        self.assertEqual(count, 1)
        self.assertEqual(db.state_get(self.conn, "telegram_offset"), "12")
        api_call.assert_any_call("tok", "getUpdates", {"offset": 0, "timeout": 0}, timeout=15)
        self.assertEqual(
            dict(self.conn.execute("SELECT name, count FROM metrics").fetchall()),
            {"feedback_recorded": 1},
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 1)

    def test_drain_resumes_from_a_previously_persisted_offset(self):
        db.state_set(self.conn, "telegram_offset", "5")
        with mock.patch.object(feedback_listener, "api_call", return_value=[]) as api_call:
            feedback_listener.drain(self.conn, self._cfg())
        api_call.assert_called_once_with(
            "tok", "getUpdates", {"offset": 5, "timeout": 0}, timeout=15
        )

    def test_drain_without_telegram_config_returns_zero_untouched(self):
        with mock.patch.object(feedback_listener, "api_call") as api_call:
            self.assertEqual(feedback_listener.drain(self.conn, CFG), 0)
        api_call.assert_not_called()

    def test_drain_swallows_a_transport_failure_and_counts_it(self):
        # None, not 0 -- a distinct sentinel so the caller (__main__._drain_cmd)
        # can tell a failed poll from "polled fine, nothing pending" and not
        # record the job as a success.
        with mock.patch.object(feedback_listener, "api_call", side_effect=OSError("down")):
            self.assertIsNone(feedback_listener.drain(self.conn, self._cfg()))
        self.assertEqual(
            dict(self.conn.execute("SELECT name, count FROM metrics").fetchall()),
            {"run_failed": 1},
        )
        self.assertIsNone(db.state_get(self.conn, "telegram_offset"))  # never advanced


class TeachTests(unittest.TestCase):
    """teach.py's WEIGHTS/BAND_WIDTH are set from the documented rationale
    (lab proposals 003/004: notify flips track bar proximity, not scorer
    variance) *before* this fixture was written, and are never adjusted
    afterwards to make these assertions pass -- that would be Goodharting
    the acceptance test. If the first honest measurement had shown
    band_lift < 2.0, the honest thing to do was report that, not tune the
    weights; it didn't, and what follows is that first honest measurement.

    Fixture: two interests with bars at 0.70 and 0.50. Five near-bar,
    low-confidence items are inserted FIRST (so recency ranks them last);
    three far-from-bar, high-confidence items are inserted LAST (so recency
    ranks them first). Recency is deliberately anti-correlated with bar
    proximity.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        db.upsert_interest(self.conn, an_interest(key="a", min_score=0.70))
        db.upsert_interest(self.conn, an_interest(key="b", min_score=0.50))
        self.a = db.interest_by_key(self.conn, "a")
        self.b = db.interest_by_key(self.conn, "b")
        self.near = [
            self._plant("near-a1", self.a, 0.705, 0.40),
            self._plant("near-a2", self.a, 0.695, 0.45),
            self._plant("near-b1", self.b, 0.505, 0.40),
            self._plant("near-b2", self.b, 0.495, 0.45),
            self._plant("near-b3", self.b, 0.510, 0.50),
        ]
        self.far = [
            self._plant("far-a1", self.a, 0.99, 0.95),
            self._plant("far-a2", self.a, 0.20, 0.95),
            self._plant("far-b1", self.b, 0.98, 0.95),
        ]

    def _plant(self, url, interest_row, final_score, confidence):
        item = stored_item(self.conn, url=url, title=url)
        score = a_score(item.id, interest_row.id, final_score, interest_key=interest_row.key)
        score.confidence = confidence
        db.save_score(self.conn, score)
        return item

    def test_build_queue_ranks_planted_near_bar_low_confidence_items_first(self):
        rows = teach.build_queue(self.conn, limit=100)
        near_ids = {item.id for item in self.near}
        far_ids = {item.id for item in self.far}
        positions = {row["item_id"]: i for i, row in enumerate(rows)}
        self.assertEqual(set(positions), near_ids | far_ids)
        self.assertLess(max(positions[i] for i in near_ids), min(positions[i] for i in far_ids))

    def test_queue_metrics_beats_the_recency_baseline_on_band_share(self):
        metrics = teach.queue_metrics(self.conn, limit=4)
        self.assertEqual(metrics["pool_size"], 8)
        self.assertEqual(metrics["n"], 4)
        self.assertGreater(metrics["queue"]["band_share"], metrics["baseline"]["band_share"])
        self.assertGreaterEqual(metrics["band_lift"], 2.0)

    def test_determinism_across_repeated_calls(self):
        self.assertEqual(
            teach.build_queue(self.conn, limit=4), teach.build_queue(self.conn, limit=4)
        )
        self.assertEqual(
            teach.queue_metrics(self.conn, limit=4), teach.queue_metrics(self.conn, limit=4)
        )


class TeachLabelFlowTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        db.upsert_interest(self.conn, an_interest())
        self.interest = db.interest_by_key(self.conn, "k")
        self.items = [
            stored_item(self.conn, url=f"https://e.com/t{i}", title=f"t{i}") for i in range(4)
        ]
        for i, item in enumerate(self.items):
            db.save_score(self.conn, a_score(item.id, self.interest.id, 0.90 - i * 0.01))

    def _feed(self, tokens):
        reader = iter(tokens)
        return lambda prompt="": next(reader)

    def test_labeling_the_four_verdicts_writes_one_feedback_row_each(self):
        rows_before = teach.build_queue(self.conn, limit=4)
        codes = list(notify.FEEDBACK_VERDICTS)
        code = teach.run_interactive(self.conn, limit=4, read=self._feed(codes))
        self.assertEqual(code, 0)
        feedback_rows = self.conn.execute(
            "SELECT item_id, interest_id, verdict, original_score FROM feedback ORDER BY id"
        ).fetchall()
        self.assertEqual(len(feedback_rows), 4)
        for expected, verdict, fb in zip(rows_before, codes, feedback_rows):
            self.assertEqual(fb["item_id"], expected["item_id"])
            self.assertEqual(fb["interest_id"], expected["interest_id"])
            self.assertEqual(fb["verdict"], verdict)
            self.assertAlmostEqual(fb["original_score"], expected["final_score"])

    def test_skip_writes_nothing_quit_stops_early(self):
        code = teach.run_interactive(self.conn, limit=4, read=self._feed(["s", "q"]))
        self.assertEqual(code, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 0)
        self.assertEqual(len(teach.build_queue(self.conn, limit=10)), 4)

    def test_an_unrecognized_token_reprompts_instead_of_crashing(self):
        code = teach.run_interactive(self.conn, limit=1, read=self._feed(["bogus", "fire"]))
        self.assertEqual(code, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 1)

    def test_eof_from_the_reader_exits_cleanly_with_zero(self):
        def raise_eof(prompt=""):
            raise EOFError

        code = teach.run_interactive(self.conn, limit=4, read=raise_eof)
        self.assertEqual(code, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 0)

    def test_a_labeled_item_is_absent_from_the_next_build_queue(self):
        teach.run_interactive(self.conn, limit=1, read=self._feed(["fire"]))
        remaining = {row["item_id"] for row in teach.build_queue(self.conn, limit=10)}
        self.assertEqual(len(remaining), 3)

    def test_an_item_labeled_via_the_telegram_listener_is_also_excluded(self):
        score_id = self.conn.execute(
            "SELECT id FROM scores WHERE item_id = ?", (self.items[0].id,)
        ).fetchone()["id"]
        callback = {"id": "cb1", "data": f"fb:up:{score_id}", "message": {"chat": {"id": 1}}}
        with mock.patch.object(feedback_listener, "api_call"):
            self.assertTrue(feedback_listener._handle_callback(self.conn, "tok", callback))
        remaining = {row["item_id"] for row in teach.build_queue(self.conn, limit=10)}
        self.assertNotIn(self.items[0].id, remaining)


class TeachIsolationTests(unittest.TestCase):
    """Labeling must grow `feedback` and touch nothing else -- no
    `notifications` row (that table drives delivery accounting, not
    teaching), no `metrics`/`service_state` write, no `scores`/
    `candidate_items` mutation."""

    TABLES = ("notifications", "metrics", "service_state", "scores", "candidate_items")

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        db.upsert_interest(self.conn, an_interest())
        interest = db.interest_by_key(self.conn, "k")
        self.items = [
            stored_item(self.conn, url=f"https://e.com/i{i}", title=f"i{i}") for i in range(3)
        ]
        for item in self.items:
            db.save_score(self.conn, a_score(item.id, interest.id, 0.9))

    def _snapshot(self):
        return {t: [tuple(r) for r in self.conn.execute(f"SELECT * FROM {t}").fetchall()]
                for t in self.TABLES}

    def test_a_full_run_only_grows_feedback(self):
        before = self._snapshot()
        reader = iter(["fire", "s", "q"])
        teach.run_interactive(self.conn, limit=3, read=lambda prompt="": next(reader))
        after = self._snapshot()
        self.assertEqual(before, after)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 1)


class TeachDegenerateTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def _no_read(self, prompt=""):
        raise AssertionError("an empty pool must never prompt for input")

    def test_an_empty_pool_exits_cleanly_everywhere(self):
        self.assertEqual(teach.build_queue(self.conn), [])
        self.assertEqual(teach.run_interactive(self.conn, read=self._no_read), 0)
        metrics = teach.queue_metrics(self.conn)
        self.assertEqual(metrics["pool_size"], 0)
        self.assertIn("nothing to teach on", teach.format_queue([]))
        self.assertIn("nothing to teach on", teach.format_metrics(metrics))

    def test_an_unknown_interest_key_behaves_like_an_empty_pool(self):
        db.upsert_interest(self.conn, an_interest())
        item = stored_item(self.conn)
        db.save_score(self.conn, a_score(item.id, db.interest_by_key(self.conn, "k").id, 0.9))
        self.assertEqual(teach.build_queue(self.conn, interest="nope"), [])

    def test_a_pool_smaller_than_the_limit_returns_the_whole_pool(self):
        db.upsert_interest(self.conn, an_interest())
        item = stored_item(self.conn)
        db.save_score(self.conn, a_score(item.id, db.interest_by_key(self.conn, "k").id, 0.9))
        self.assertEqual(len(teach.build_queue(self.conn, limit=10)), 1)


class FakeCDPConnection:
    """Stands in for cdp.CDPConnection. `replies` feeds the completion calls in
    order: a string/dict is returned as-is, an Exception is raised. Create and
    delete calls always succeed and are recorded in `calls`."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.closed = False

    def evaluate(self, js, timeout=None, **_kw):
        if "/completion" in js:
            self.calls.append("completion")
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        if "'DELETE'" in js:
            self.calls.append("delete")
            return True
        self.calls.append("create")
        return True

    def close(self):
        self.closed = True


def completion_reply(text):
    return json.dumps({"text": text})


class FakeChatGPTConnection:
    """Stands in for cdp.CDPConnection for the chatgpt.com provider. The whole
    send is one evaluate() (session + sentinel + PoW + conversation SSE), so a
    completion reply is a JSON string/dict `{text, conversation_id}` (or an
    Exception to raise); the follow-up hide is a second evaluate. Dispatch is on
    the JS the provider actually builds, and every js string is kept so a test
    can assert the reverse-engineered contract stays in the payload."""

    def __init__(self, replies, hide_error=None, poll_results=None):
        self.replies = list(replies)
        # Handed-off (thinking-model) answers are read back by polling; each
        # entry is one GET result {text, done}. Left empty for the inline path.
        self.poll_results = list(poll_results or [])
        self.calls = []
        self.js = []
        self.closed = False
        self.hide_error = hide_error

    def evaluate(self, js, timeout=None, **_kw):
        self.js.append(js)
        if "is_visible" in js:
            self.calls.append("hide")
            if self.hide_error is not None:
                raise self.hide_error
            return True
        if "/* poll */" in js:
            self.calls.append("poll")
            if self.poll_results:
                nxt = self.poll_results.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt if isinstance(nxt, str) else json.dumps(nxt)
            return json.dumps({"text": "", "done": True})
        if "text/event-stream" in js:
            self.calls.append("completion")
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        self.calls.append("other")
        return True

    def close(self):
        self.closed = True


def chatgpt_reply(text, conversation_id="c1"):
    return json.dumps({"text": text, "conversation_id": conversation_id})


def chatgpt_handoff(conversation_id="c1"):
    """A thinking-model send: the POST returns a stream_handoff with a
    conversation id but no inline text, so the provider must poll for the
    answer (see FakeChatGPTConnection.poll_results)."""
    return json.dumps({"text": "", "conversation_id": conversation_id, "handoff": True})


class ClaudeChatProviderTests(unittest.TestCase):
    """The claude.ai-over-CDP provider, with the browser faked out entirely."""

    SCHEMA = {
        "type": "object",
        "properties": {
            "a": {"type": "number"},
            "kind": {"type": "string", "enum": ["x", "y"]},
        },
        "required": ["a"],
        "additionalProperties": False,
    }

    def _provider(self, *replies, connections=None):
        conns = connections if connections is not None else [FakeCDPConnection(replies)]
        remaining = list(conns)
        self.connections = conns
        return claude_chat.ClaudeChatProvider(
            "claude-opus-5", org_id="org-123", port=9222,
            connect=lambda: remaining.pop(0),
        )

    def test_registered_and_constructable_without_touching_chrome(self):
        self.assertIn("claude_chat", PROVIDERS)
        provider = claude_chat.ClaudeChatProvider("claude-opus-5", org_id="", port=9222)
        self.assertEqual(provider.name, "claude_chat")  # lazy: no connection yet

    def test_complete_json_parses_a_clean_reply_and_cleans_up(self):
        provider = self._provider(completion_reply('{"a": 0.5, "kind": "x"}'))
        data = provider.complete_json("sys", "prompt", self.SCHEMA)
        self.assertEqual(data, {"a": 0.5, "kind": "x"})
        (conn,) = self.connections
        self.assertEqual(conn.calls, ["create", "completion", "delete"])
        self.assertEqual(provider.usage["calls"], 1)
        self.assertEqual(provider.usage["input_tokens"], 0)  # not reported, never guessed

    def test_complete_json_survives_prose_and_fences_around_the_object(self):
        provider = self._provider(
            completion_reply('Sure! Here it is:\n```json\n{"a": 1.0}\n```\nHope that helps.')
        )
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 1.0})

    def test_a_malformed_reply_is_retried_once_then_succeeds(self):
        provider = self._provider(
            completion_reply("I cannot answer in JSON, sorry."),
            completion_reply('{"a": 0.25}'),
        )
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 0.25})
        (conn,) = self.connections
        self.assertEqual(conn.calls.count("completion"), 2)

    def test_two_malformed_replies_fail_gracefully(self):
        provider = self._provider(
            completion_reply('{"wrong": true}'),   # missing required "a"
            completion_reply('{"a": "not a number"}'),
        )
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("attempt 2", str(ctx.exception))

    def test_an_enum_violation_counts_as_malformed(self):
        provider = self._provider(
            completion_reply('{"a": 1, "kind": "zebra"}'),
            completion_reply('{"a": 1, "kind": "zebra"}'),
        )
        with self.assertRaises(ProviderError):
            provider.complete_json("s", "p", self.SCHEMA)

    def test_complete_json_accepts_a_score_reply_with_no_debug_fields(self):
        """The real production schemas (scoring.SCORE_SCHEMA/council.MISSION_
        SCHEMA), not a hand-shrunk test schema, through the real provider --
        repair 2 broke this by putting the optional debug/deliberation
        fields into these schemas' top-level `required`, which claude_chat's
        _validate() enforces verbatim. A reply with only the production
        fields must succeed in one attempt, not fall back to the retry
        suffix or raise."""
        good_score = {
            "interest_key": "x",
            **{name: 0.5 for name in models.DIMENSIONS},
            "confidence": 0.5, "reason": "r", "why_better_than_generic": "w",
        }
        provider = self._provider(completion_reply(json.dumps(good_score)))
        self.assertEqual(provider.complete_json("s", "p", scoring.SCORE_SCHEMA), good_score)
        (conn,) = self.connections
        self.assertEqual(conn.calls.count("completion"), 1)  # no retry needed

    def test_complete_json_accepts_missions_reply_with_no_deliberation(self):
        good_missions = {"missions": [{"label": "a", "rationale": "b", "prompt": "c"}]}
        provider = self._provider(completion_reply(json.dumps(good_missions)))
        self.assertEqual(provider.complete_json("s", "p", council.MISSION_SCHEMA), good_missions)
        (conn,) = self.connections
        self.assertEqual(conn.calls.count("completion"), 1)  # no retry needed

    def test_search_json_returns_the_embedded_array_and_garbage_becomes_empty(self):
        provider = self._provider(
            completion_reply('I searched.\n[{"title": "T", "url": "https://e.com"}]\nDone.'),
            completion_reply("no array here at all"),
        )
        self.assertEqual(
            provider.search_json("find things"),
            [{"title": "T", "url": "https://e.com"}],
        )
        self.assertEqual(provider.search_json("find things"), [])

    def test_missing_org_id_is_a_clean_provider_error(self):
        provider = claude_chat.ClaudeChatProvider("claude-opus-5", org_id="", port=9222)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("CLAUDE_ORG_ID", str(ctx.exception))

    def test_no_chrome_endpoint_is_a_clean_provider_error(self):
        provider = claude_chat.ClaudeChatProvider("claude-opus-5", org_id="org", port=9222)
        with mock.patch.object(
            claude_chat.cdp, "find_claude_tab", side_effect=ConnectionRefusedError("refused")
        ):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("Chrome DevTools endpoint", str(ctx.exception))

    def test_no_claude_tab_is_a_clean_provider_error(self):
        provider = claude_chat.ClaudeChatProvider("claude-opus-5", org_id="org", port=9222)
        with mock.patch.object(claude_chat.cdp, "find_claude_tab", return_value=None):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("claude.ai tab", str(ctx.exception))

    def test_a_js_exception_in_the_tab_is_a_provider_error_not_a_crash(self):
        provider = self._provider(RuntimeError("JS exception: completion HTTP 429"))
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("HTTP 429", str(ctx.exception))

    def test_a_dropped_connection_reconnects_once_and_recovers(self):
        dead = FakeCDPConnection([ConnectionError("websocket closed")])
        alive = FakeCDPConnection([completion_reply('{"a": 0.75}')])
        provider = self._provider(connections=[dead, alive])
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 0.75})
        self.assertTrue(dead.closed)   # reset closed the dead connection
        self.assertFalse(alive.closed)

    def test_a_connection_that_keeps_dropping_fails_gracefully(self):
        dead1 = FakeCDPConnection([ConnectionError("closed")])
        dead2 = FakeCDPConnection([ConnectionError("closed again")])
        provider = self._provider(connections=[dead1, dead2])
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("twice", str(ctx.exception))

    def test_a_null_completion_result_blames_the_tab(self):
        # conn.evaluate hands back Python None when the tab's JS context was
        # torn down mid-request (navigation/reload) -- no exception raised.
        provider = self._provider(None)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("no text", str(ctx.exception))

    def test_a_dict_reply_from_cdp_is_accepted_as_is(self):
        # Some Chrome/CDP combinations hand back the JS return value already
        # deserialized (same quirk council_bot.parse_completion_result handles).
        provider = self._provider({"text": '{"a": 2}'})
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 2})

    def test_preflight_fails_cleanly_without_org_id_and_touches_no_network(self):
        provider = claude_chat.ClaudeChatProvider("claude-opus-5", org_id="", port=9222)
        with mock.patch.object(claude_chat.cdp, "find_claude_tab") as find_tab:
            ok, detail = provider.preflight()
        self.assertFalse(ok)
        self.assertIn("CLAUDE_ORG_ID", detail)
        find_tab.assert_not_called()

    def test_preflight_fails_cleanly_with_no_chrome_endpoint(self):
        provider = claude_chat.ClaudeChatProvider("claude-opus-5", org_id="org", port=9222)
        with mock.patch.object(
            claude_chat.cdp, "find_claude_tab", side_effect=ConnectionRefusedError("refused")
        ):
            ok, detail = provider.preflight()
        self.assertFalse(ok)
        self.assertIn("Chrome DevTools endpoint", detail)

    def test_preflight_fails_cleanly_with_no_claude_tab(self):
        provider = claude_chat.ClaudeChatProvider("claude-opus-5", org_id="org", port=9222)
        with mock.patch.object(claude_chat.cdp, "find_claude_tab", return_value=None):
            ok, detail = provider.preflight()
        self.assertFalse(ok)
        self.assertIn("claude.ai tab", detail)

    def test_preflight_succeeds_when_the_tab_is_open(self):
        provider = claude_chat.ClaudeChatProvider("claude-opus-5", org_id="org", port=9222)
        with mock.patch.object(
            claude_chat.cdp, "find_claude_tab", return_value={"id": "1"}
        ):
            self.assertEqual(provider.preflight(), (True, ""))


class ChatGPTBrowserProviderTests(unittest.TestCase):
    """The chatgpt.com-over-CDP provider, with the browser faked out entirely.
    The novel core it shares with no other provider -- the sentinel proof-of-
    work handshake and the delta-encoded SSE accumulation -- runs as JS inside
    the page, so it's verified separately at execution level; here the seam is
    the same as claude_chat's: the Python orchestration around evaluate()."""

    SCHEMA = {
        "type": "object",
        "properties": {
            "a": {"type": "number"},
            "kind": {"type": "string", "enum": ["x", "y"]},
        },
        "required": ["a"],
        "additionalProperties": False,
    }

    def _provider(self, *replies, connections=None, hide_error=None,
                  poll_results=None, model="auto"):
        conns = connections if connections is not None else [
            FakeChatGPTConnection(replies, hide_error=hide_error, poll_results=poll_results)
        ]
        remaining = list(conns)
        self.connections = conns
        return chatgpt_browser.ChatGPTBrowserProvider(
            model, port=9222, connect=lambda: remaining.pop(0),
        )

    def test_registered_and_constructable_without_touching_chrome(self):
        self.assertIn("chatgpt_browser", PROVIDERS)
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        self.assertEqual(provider.name, "chatgpt_browser")  # lazy: no connection yet

    def test_complete_json_parses_a_clean_reply_and_hides_the_conversation(self):
        provider = self._provider(chatgpt_reply('{"a": 0.5, "kind": "x"}'))
        data = provider.complete_json("sys", "prompt", self.SCHEMA)
        self.assertEqual(data, {"a": 0.5, "kind": "x"})
        (conn,) = self.connections
        self.assertEqual(conn.calls, ["completion", "hide"])
        self.assertEqual(provider.usage["calls"], 1)
        self.assertEqual(provider.usage["input_tokens"], 0)  # not reported, never guessed

    def test_complete_json_survives_prose_and_fences_around_the_object(self):
        provider = self._provider(
            chatgpt_reply('Sure! Here it is:\n```json\n{"a": 1.0}\n```\nHope that helps.')
        )
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 1.0})

    def test_a_malformed_reply_is_retried_once_then_succeeds(self):
        provider = self._provider(
            chatgpt_reply("I cannot answer in JSON, sorry."),
            chatgpt_reply('{"a": 0.25}'),
        )
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 0.25})
        (conn,) = self.connections
        self.assertEqual(conn.calls.count("completion"), 2)

    def test_two_malformed_replies_fail_gracefully(self):
        provider = self._provider(
            chatgpt_reply('{"wrong": true}'),        # missing required "a"
            chatgpt_reply('{"a": "not a number"}'),
        )
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("attempt 2", str(ctx.exception))

    def test_an_enum_violation_counts_as_malformed(self):
        provider = self._provider(
            chatgpt_reply('{"a": 1, "kind": "zebra"}'),
            chatgpt_reply('{"a": 1, "kind": "zebra"}'),
        )
        with self.assertRaises(ProviderError):
            provider.complete_json("s", "p", self.SCHEMA)

    def test_search_json_returns_the_embedded_array_and_garbage_becomes_empty(self):
        provider = self._provider(
            chatgpt_reply('I searched.\n[{"title": "T", "url": "https://e.com"}]\nDone.'),
            chatgpt_reply("no array here at all"),
        )
        self.assertEqual(
            provider.search_json("find things"),
            [{"title": "T", "url": "https://e.com"}],
        )
        self.assertEqual(provider.search_json("find things"), [])

    def test_search_json_asks_for_the_search_tool_but_complete_json_does_not(self):
        provider = self._provider(
            chatgpt_reply("[]"), chatgpt_reply('{"a": 1}'),
        )
        provider.search_json("find things")
        provider.complete_json("s", "p", self.SCHEMA)
        (conn,) = self.connections
        search_js, complete_js = conn.js[0], conn.js[2]  # [0]=search, [1]=its hide, [2]=complete
        self.assertIn('system_hints: ["search"]', search_js)
        self.assertIn("system_hints: []", complete_js)

    def test_the_completion_js_carries_the_sentinel_and_pow_contract(self):
        provider = self._provider(chatgpt_reply('{"a": 1}'))
        provider.complete_json("s", "p", self.SCHEMA)
        js = self.connections[0].js[0]
        for token in ("sha3_512_hex", "/sentinel/chat-requirements",
                      "OpenAI-Sentinel-Chat-Requirements-Token",
                      "OpenAI-Sentinel-Proof-Token",
                      "OpenAI-Sentinel-Turnstile-Token",  # forwarded, not thrown on
                      "/api/auth/session"):
            self.assertIn(token, js, f"completion JS lost {token!r}")
        # turnstile.required must NOT abort the send -- echoing dx works live
        self.assertNotIn("throw new Error('chatgpt.com demanded", js)

    def test_the_send_js_resolves_latest_high_and_carries_the_reasoning_contract(self):
        provider = self._provider(chatgpt_reply('{"a": 1}'))
        provider.complete_json("s", "p", self.SCHEMA)
        js = self.connections[0].js[0]
        for token in ("/backend-api/models",          # resolves the newest model live
                      "intelligence_presets",          # ...from the version's presets
                      "'thinking'",                    # ...picking the thinking lane
                      "body.thinking_effort = effort", # ...and sending its (High) effort
                      "stream_handoff"):               # ...and noticing the handoff
            self.assertIn(token, js, f"send JS lost {token!r}")

    def test_a_handoff_answer_is_polled_until_finished(self):
        # Thinking model: the send returns only a handoff, then two polls -- the
        # first still streaming, the second finished with the JSON answer.
        provider = self._provider(
            chatgpt_handoff("c9"),
            poll_results=[{"text": "", "done": False}, {"text": '{"a": 5}', "done": True}],
        )
        with mock.patch.object(chatgpt_browser.time, "sleep"):
            self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 5})
        (conn,) = self.connections
        self.assertEqual(conn.calls, ["completion", "poll", "poll", "hide"])

    def test_search_json_reads_a_handed_off_array_back_by_polling(self):
        provider = self._provider(
            chatgpt_handoff("c1"),
            poll_results=[{"text": '[{"title": "T", "url": "https://e.com"}]', "done": True}],
        )
        with mock.patch.object(chatgpt_browser.time, "sleep"):
            self.assertEqual(
                provider.search_json("find things"),
                [{"title": "T", "url": "https://e.com"}],
            )

    def test_a_handoff_that_never_finishes_times_out_to_a_provider_error(self):
        # Poll keeps returning unfinished; the deadline passes and it surfaces as
        # a normal empty-completion ProviderError rather than looping forever.
        provider = self._provider(
            chatgpt_handoff("c1"),
            poll_results=[{"text": "", "done": False}] * 50,
        )
        clock = iter([0.0, 1.0, 2.0, 999.0])  # monotonic: enters loop once, then past deadline
        with mock.patch.object(chatgpt_browser.time, "sleep"), \
                mock.patch.object(chatgpt_browser.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("empty completion", str(ctx.exception))

    def test_an_explicit_slug_effort_spec_is_passed_through(self):
        provider = self._provider(chatgpt_reply('{"a": 1}'), model="gpt-5-6-thinking:extended")
        provider.complete_json("s", "p", self.SCHEMA)
        js = self.connections[0].js[0]
        self.assertIn('"gpt-5-6-thinking:extended"', js)  # the pin reaches the page verbatim

    def test_no_chrome_endpoint_is_a_clean_provider_error(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(
            chatgpt_browser.cdp, "find_chatgpt_tab", side_effect=ConnectionRefusedError("refused")
        ):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("Chrome DevTools endpoint", str(ctx.exception))

    def test_no_chatgpt_tab_is_a_clean_provider_error(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(chatgpt_browser.cdp, "find_chatgpt_tab", return_value=None):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("chatgpt.com tab", str(ctx.exception))

    def test_a_js_exception_in_the_tab_is_a_provider_error_not_a_crash(self):
        provider = self._provider(RuntimeError("JS exception: conversation HTTP 429"))
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("HTTP 429", str(ctx.exception))

    def test_a_dropped_connection_reconnects_once_and_recovers(self):
        dead = FakeChatGPTConnection([ConnectionError("websocket closed")])
        alive = FakeChatGPTConnection([chatgpt_reply('{"a": 0.75}')])
        provider = self._provider(connections=[dead, alive])
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 0.75})
        self.assertTrue(dead.closed)     # reset closed the dead connection
        self.assertFalse(alive.closed)

    def test_a_connection_that_keeps_dropping_fails_gracefully(self):
        dead1 = FakeChatGPTConnection([ConnectionError("closed")])
        dead2 = FakeChatGPTConnection([ConnectionError("closed again")])
        provider = self._provider(connections=[dead1, dead2])
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("twice", str(ctx.exception))

    def test_a_null_completion_result_blames_the_tab(self):
        provider = self._provider(None)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("no text", str(ctx.exception))

    def test_a_dict_reply_from_cdp_is_accepted_as_is(self):
        # Some Chrome/CDP combinations hand back the JS return value already
        # deserialized; no conversation_id here, so no hide fires.
        provider = self._provider({"text": '{"a": 2}', "conversation_id": None})
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 2})
        self.assertEqual(self.connections[0].calls, ["completion"])

    def test_a_hide_failure_never_breaks_the_reply(self):
        provider = self._provider(
            chatgpt_reply('{"a": 3}'), hide_error=RuntimeError("hide blew up")
        )
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 3})
        self.assertIn("hide", self.connections[0].calls)

    def test_preflight_fails_cleanly_with_no_chrome_endpoint(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(
            chatgpt_browser.cdp, "find_chatgpt_tab", side_effect=ConnectionRefusedError("refused")
        ):
            ok, detail = provider.preflight()
        self.assertFalse(ok)
        self.assertIn("Chrome DevTools endpoint", detail)

    def test_preflight_fails_cleanly_with_no_chatgpt_tab(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(chatgpt_browser.cdp, "find_chatgpt_tab", return_value=None):
            ok, detail = provider.preflight()
        self.assertFalse(ok)
        self.assertIn("chatgpt.com tab", detail)

    def test_preflight_succeeds_when_the_tab_is_open(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(
            chatgpt_browser.cdp, "find_chatgpt_tab", return_value={"id": "1"}
        ):
            self.assertEqual(provider.preflight(), (True, ""))


class FakeAnthropicClient:
    """Stands in for anthropic.Anthropic() -- only .messages.create is used.
    Constructed with a fake client (never None), so these tests never need
    the anthropic SDK installed."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def anthropic_response(text_blocks, stop_reason="end_turn", input_tokens=0,
                        output_tokens=0, web_searches=None, usage=True):
    if isinstance(text_blocks, str):
        text_blocks = [text_blocks]
    content = [SimpleNamespace(type="text", text=t) for t in text_blocks]
    content.insert(0, SimpleNamespace(type="thinking", text="ignored"))
    server_tool_use = (
        SimpleNamespace(web_search_requests=web_searches) if web_searches is not None else None
    )
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens,
            server_tool_use=server_tool_use,
        ) if usage else None,
    )


class AnthropicProviderTests(unittest.TestCase):
    """The direct-API provider, with the SDK client faked out entirely."""

    SCHEMA = {
        "type": "object",
        "properties": {"a": {"type": "number"}},
        "required": ["a"],
        "additionalProperties": False,
    }

    def test_registered_under_its_config_name(self):
        self.assertIs(PROVIDERS["anthropic"], AnthropicProvider)

    def test_complete_json_parses_the_reply_and_records_token_usage(self):
        client = FakeAnthropicClient(
            anthropic_response('{"a": 1}', input_tokens=100, output_tokens=20)
        )
        provider = AnthropicProvider("claude-opus-5", client=client)
        self.assertEqual(provider.complete_json("sys", "prompt", self.SCHEMA), {"a": 1})
        self.assertEqual(provider.usage["input_tokens"], 100)
        self.assertEqual(provider.usage["output_tokens"], 20)
        self.assertEqual(provider.usage["calls"], 1)

    def test_complete_json_joins_multiple_text_blocks(self):
        client = FakeAnthropicClient(anthropic_response(['{"a": ', "1}"]))
        provider = AnthropicProvider("claude-opus-5", client=client)
        self.assertEqual(provider.complete_json("sys", "prompt", self.SCHEMA), {"a": 1})

    def test_a_refusal_or_truncation_raises_before_parsing(self):
        client = FakeAnthropicClient(anthropic_response('{"a"', stop_reason="max_tokens"))
        provider = AnthropicProvider("claude-opus-5", client=client)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("sys", "prompt", self.SCHEMA)
        self.assertIn("max_tokens", str(ctx.exception))

    def test_search_json_records_server_side_search_usage(self):
        client = FakeAnthropicClient(
            anthropic_response('[{"url": "https://e.com/a"}]', web_searches=3)
        )
        provider = AnthropicProvider("claude-opus-5", client=client)
        result = provider.search_json("prompt", max_searches=3)
        self.assertEqual(result, [{"url": "https://e.com/a"}])
        self.assertEqual(provider.usage["web_searches"], 3)
        kwargs = client.calls[0]
        self.assertEqual(kwargs["tools"][0]["max_uses"], 3)

    def test_missing_usage_on_the_response_is_not_fatal(self):
        client = FakeAnthropicClient(anthropic_response('{"a": 1}', usage=False))
        provider = AnthropicProvider("claude-opus-5", client=client)
        self.assertEqual(provider.complete_json("sys", "prompt", self.SCHEMA), {"a": 1})
        self.assertEqual(provider.usage["calls"], 0)  # nothing to record from

    def test_preflight_is_a_key_presence_check_only(self):
        provider = AnthropicProvider("claude-opus-5", client=FakeAnthropicClient())
        with mock.patch.dict(os.environ, {}, clear=True):
            ok, detail = provider.preflight()
        self.assertFalse(ok)
        self.assertIn("ANTHROPIC_API_KEY", detail)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}):
            self.assertEqual(provider.preflight(), (True, ""))


class FakeOpenAIClient:
    """Stands in for openai.OpenAI() -- only .chat.completions.create is used."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def openai_response(content, prompt_tokens=0, completion_tokens=0):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class OpenAIProviderTests(unittest.TestCase):
    """No search capability, so the pipeline's UnsupportedCapability skip path
    is what makes this provider usable at all for the web_search collector."""

    SCHEMA = {
        "type": "object",
        "properties": {"a": {"type": "number"}},
        "required": ["a"],
        "additionalProperties": False,
    }

    def test_registered_under_its_config_name(self):
        self.assertIs(PROVIDERS["openai"], OpenAIProvider)

    def test_complete_json_parses_the_reply_and_records_token_usage(self):
        client = FakeOpenAIClient(openai_response('{"a": 1}', prompt_tokens=50, completion_tokens=5))
        provider = OpenAIProvider("gpt-5", client=client)
        self.assertEqual(provider.complete_json("sys", "prompt", self.SCHEMA), {"a": 1})
        self.assertEqual(provider.usage["input_tokens"], 50)
        self.assertEqual(provider.usage["output_tokens"], 5)
        kwargs = client.calls[0]
        self.assertEqual(kwargs["response_format"]["json_schema"]["schema"], self.SCHEMA)
        self.assertTrue(kwargs["response_format"]["json_schema"]["strict"])

    def test_missing_usage_on_the_response_is_not_fatal(self):
        response = openai_response('{"a": 1}')
        response.usage = None
        client = FakeOpenAIClient(response)
        provider = OpenAIProvider("gpt-5", client=client)
        self.assertEqual(provider.complete_json("sys", "prompt", self.SCHEMA), {"a": 1})
        self.assertEqual(provider.usage["calls"], 1)  # record_usage still called, with zeros
        self.assertEqual(provider.usage["input_tokens"], 0)

    def test_search_json_always_raises_unsupported(self):
        provider = OpenAIProvider("gpt-5", client=FakeOpenAIClient())
        with self.assertRaises(UnsupportedCapability):
            provider.search_json("prompt")

    def test_preflight_is_a_key_presence_check_only(self):
        provider = OpenAIProvider("gpt-5", client=FakeOpenAIClient())
        with mock.patch.dict(os.environ, {}, clear=True):
            ok, detail = provider.preflight()
        self.assertFalse(ok)
        self.assertIn("OPENAI_API_KEY", detail)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}):
            self.assertEqual(provider.preflight(), (True, ""))


class SchemaContractTests(unittest.TestCase):
    def test_every_structured_output_schema_forbids_extra_properties(self):
        """The live structured-outputs API requires additionalProperties: false
        on every object -- a schema without it 400s on the first real call."""
        from discovery.collectors import stocks as stocks_module

        for schema in (scoring.SCORE_SCHEMA, stocks_module.EXPLAIN_SCHEMA):
            self.assertIs(schema.get("additionalProperties"), False, schema)

    def test_default_provider_validate_accepts_score_missing_all_debug_fields(self):
        """claude_chat/chatgpt_browser's hand-rolled _validate() is the ONLY
        schema enforcement the two default browser providers have, and it
        enforces `required` verbatim with no tolerance. The six debug fields
        (scoring.DEBUG_FIELDS) are documented as 'absent => unavailable, not
        an error' -- so a reply carrying every production field but none of
        the debug ones must still pass _validate() through the real
        SCORE_SCHEMA, on the real default provider's validator, not just via
        _debug_payload()'s own tolerant parsing."""
        good = {
            "interest_key": "x",
            **{name: 0.5 for name in models.DIMENSIONS},
            "confidence": 0.5, "reason": "r", "why_better_than_generic": "w",
        }
        claude_chat._validate(good, scoring.SCORE_SCHEMA)  # must not raise

    def test_default_provider_validate_accepts_missions_missing_deliberation(self):
        """Same contract on the Council side: a valid missions array with no
        deliberation object at all must still pass the real _validate()
        against the real MISSION_SCHEMA -- _extract_deliberation()'s
        {'unavailable': True, ...} fallback is only reachable if validation
        doesn't reject the reply first."""
        good = {"missions": [{"label": "a", "rationale": "b", "prompt": "c"}]}
        claude_chat._validate(good, council.MISSION_SCHEMA)  # must not raise

    def test_default_provider_validate_tolerates_wrong_typed_debug_fields(self):
        """The 'never fatal' contract's other half: a PRESENT-but-wrong-typed
        optional debug/deliberation field must not raise either. scoring.
        _debug_payload()/council._extract_deliberation() already tolerate
        None/wrong shapes on these fields (turning them into an
        {'unavailable': True, ...} marker) -- but that tolerant code is only
        reached if _validate() lets the reply through. Before this fix,
        _validate() type-checked every PRESENT property regardless of
        `required`, so a model returning e.g. uncertainties=None or
        dimension_rationale as a plain string burned a retry attempt (and a
        repeat on retry raised ProviderError outright)."""
        good_score = {
            "interest_key": "x",
            **{name: 0.5 for name in models.DIMENSIONS},
            "confidence": 0.5, "reason": "r", "why_better_than_generic": "w",
            "uncertainties": None,
            "evidence_used": ["a", "b"],
            "dimension_rationale": "mostly on topic",
        }
        claude_chat._validate(good_score, scoring.SCORE_SCHEMA)  # must not raise

        good_missions = {
            "missions": [{"label": "a", "rationale": "b", "prompt": "c"}],
            "deliberation": "none to report",
        }
        claude_chat._validate(good_missions, council.MISSION_SCHEMA)  # must not raise

    def test_openai_strict_transport_still_requires_every_property(self):
        """The inverse guarantee: OpenAI's structured-outputs API 400s unless
        every object in the schema has `required` == all of its properties.
        The shared schema constants are lenient (see the two tests above),
        so openai_provider must build its own strict copy for the wire, not
        rely on the shared constant."""
        from discovery.providers.openai_provider import _strict_schema

        for schema in (scoring.SCORE_SCHEMA, council.MISSION_SCHEMA):
            strict = _strict_schema(schema)
            self._assert_fully_strict(strict)

    def _assert_fully_strict(self, node):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" and "properties" in node:
            self.assertEqual(set(node["required"]), set(node["properties"]), node)
            self.assertIs(node.get("additionalProperties"), False, node)
            for prop in node["properties"].values():
                self._assert_fully_strict(prop)
        if "items" in node:
            self._assert_fully_strict(node["items"])

    def test_openai_strict_conversion_does_not_mutate_the_shared_schema(self):
        from discovery.providers.openai_provider import _strict_schema

        _strict_schema(scoring.SCORE_SCHEMA)
        _strict_schema(council.MISSION_SCHEMA)
        self.assertNotIn("evidence_used", scoring.SCORE_SCHEMA["required"])
        self.assertNotIn("deliberation", council.MISSION_SCHEMA["required"])


class CLITests(unittest.TestCase):
    """Config mistakes surface as clean exit codes, not tracebacks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "t.db")

    def _main(self, *argv, env=None):
        import contextlib
        import io

        from discovery.__main__ import main

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--db", self.db_path, *argv])
        return code, out.getvalue(), err.getvalue()

    def _interests_file(self, content):
        path = os.path.join(self.tmp.name, "interests.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def test_init_with_a_malformed_interests_file_exits_cleanly(self):
        path = self._interests_file("{ not json")
        code, _out, err = self._main("init", env={"DISCOVERY_INTERESTS": path})
        self.assertEqual(code, 2)
        self.assertIn("malformed interests file", err)

    def test_init_with_an_entry_missing_its_key_exits_cleanly(self):
        path = self._interests_file('{"interests": [{"title": "no key"}]}')
        code, _out, err = self._main("init", env={"DISCOVERY_INTERESTS": path})
        self.assertEqual(code, 2)
        self.assertIn("malformed interests file", err)

    def test_init_with_a_missing_interests_file_exits_cleanly(self):
        missing = os.path.join(self.tmp.name, "nope.json")
        code, _out, err = self._main("init", env={"DISCOVERY_INTERESTS": missing})
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_feedback_on_an_unknown_item_exits_cleanly(self):
        code, _out, err = self._main("feedback", "999", "up")
        self.assertEqual(code, 2)
        self.assertIn("no item with id 999", err)

    def test_an_unknown_provider_is_a_config_error_not_a_traceback(self):
        path = self._interests_file(
            '{"interests": [{"key": "x", "title": "X", "sources": ["web_search"]}]}'
        )
        self._main("init", env={"DISCOVERY_INTERESTS": path})
        code, _out, err = self._main(
            "--provider", "bogus", "score", "--url", "https://e.com/a", "--title", "T",
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown provider 'bogus'", err)

    def test_pause_gates_the_spending_commands_without_touching_a_provider(self):
        code, out, _err = self._main("pause", "--why", "token budget")
        self.assertEqual(code, 0)
        self.assertIn("paused", out)
        # The gate must fire before the provider() closure ever constructs
        # anything -- a paused tick that still builds a provider would defeat
        # the point of the freeze on machines where construction has side
        # effects (CDP probes, key checks).
        with mock.patch(
            "discovery.providers.get_provider",
            side_effect=AssertionError("provider built while paused"),
        ):
            for command in ("run-once", "web-tick", "digest"):
                code, out, _err = self._main(command)
                self.assertEqual(code, 0)
                self.assertIn("paused (token budget)", out)
                self.assertIn(f"{command} skipped", out)

    def test_resume_lifts_the_pause_gate(self):
        self._main("pause")
        code, out, _err = self._main("resume")
        self.assertEqual(code, 0)
        self.assertIn("resumed", out)
        # digest is the safe probe: no provider needed, empty DB sends nothing.
        code, out, _err = self._main("digest")
        self.assertEqual(code, 0)
        self.assertIn("sent 0 digest item(s)", out)

    def test_health_while_paused_is_not_degraded_and_skips_the_provider(self):
        self._main("pause", "--why", "token budget")
        conn = db.connect(self.db_path)
        db.init(conn)
        # A month-stale heartbeat would normally flip `degraded` (exit 1).
        db.state_set(conn, "job:web:last_ok", db.ago(30 * 24 * 3600))
        conn.close()
        code, out, _err = self._main("health")
        self.assertEqual(code, 0)
        self.assertIn("PAUSED (token budget)", out)
        self.assertIn("not checked (paused)", out)
        self.assertIn("overall: OK", out)
        # And the same stale heartbeat degrades again once resumed (provider
        # still skipped: patched to None via a fake that never preflights).
        self._main("resume")
        conn = db.connect(self.db_path)
        db.init(conn)
        result = health.check(conn, config.load(), provider=None)
        conn.close()
        self.assertTrue(result["degraded"])

    def test_personal_state_probe_survives_an_off_contract_generated_at(self):
        # valid JSON, valid v1 shape, but generated_at that isn't a proper
        # "Z"-suffixed UTC timestamp must fall back to "age unknown" rather
        # than tracebacking on offset-naive/None subtraction.
        for generated_at in ("2026-08-01T00:00:00", None):
            path = os.path.join(self.tmp.name, "ps.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"contract_version": 1, "topics": [], "generated_at": generated_at}, fh
                )
            code, out, _err = self._main("personal-state", "--path", path)
            self.assertEqual(code, 0)
            self.assertIn("age unknown", out)

    def test_personal_state_probe_prints_a_full_readout(self):
        # Valid v1 artifact, proper Z-suffixed timestamp, >10 topics -- the
        # normal-path shape, as opposed to the degenerate/error shape above.
        topics = [{"key": f"t{i}", "weight": round(1.0 - i * 0.05, 2)} for i in range(12)]
        path = os.path.join(self.tmp.name, "ps.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"contract_version": 1, "generated_at": "2026-08-01T00:00:00Z", "topics": topics},
                fh,
            )
        code, out, _err = self._main("personal-state", "--path", path)
        self.assertEqual(code, 0)
        self.assertIn("contract_version=1", out)
        self.assertIn("12 topic(s)", out)
        self.assertIn("d old", out)
        self.assertNotIn("age unknown", out)
        listed_keys = [
            line.split("'")[1] for line in out.splitlines() if line.strip().startswith("'t")
        ]
        self.assertEqual(listed_keys, [f"t{i}" for i in range(10)])

    def test_trace_fixture_without_db_refuses_to_run(self):
        # trace_fixture.build() writes fixture interests/items/scores and a
        # real feedback row through the production code paths -- without
        # this guard, an unflagged `trace-fixture` would silently fall back
        # to cfg.db_path's default (REPO_ROOT/discovery.db, the real db).
        import contextlib
        import io

        from discovery.__main__ import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["trace-fixture"])
        self.assertEqual(code, 2)
        self.assertIn("--db", err.getvalue())

    def test_ui_public_without_a_token_refuses_to_start(self):
        # This guard fires before observatory/datasette is ever imported, so
        # it's exercised here (test_discovery.py must stay importable/green
        # without datasette installed) rather than in test_observatory.py.
        code, _out, err = self._main(
            "ui", "--public", env={"DISCOVERY_UI_TOKEN": "", "DISCOVERY_NGROK_CMD": "ngrok http {port}"}
        )
        self.assertEqual(code, 2)
        self.assertIn("DISCOVERY_UI_TOKEN", err)

    def test_ui_public_without_ngrok_cmd_refuses_to_start(self):
        code, _out, err = self._main(
            "ui", "--public", env={"DISCOVERY_UI_TOKEN": "tok", "DISCOVERY_NGROK_CMD": ""}
        )
        self.assertEqual(code, 2)
        self.assertIn("DISCOVERY_NGROK_CMD", err)

    def test_discovery_modules_import_cleanly_without_datasette_installed(self):
        # datasette must stay confined to observatory/ + __main__.py's `ui`
        # command handler (see PROJECT_STATE.md's Observatory section) -- a
        # subprocess with `sys.modules['datasette'] = None` (the standard
        # trick that makes any `import datasette`/`import datasette.x` raise
        # ImportError, exactly as if the package were never installed) is
        # what actually proves it: the modules under test haven't been
        # imported yet in that fresh interpreter, so this can't pass by
        # accident off an already-cached import in this test process.
        import subprocess

        script = (
            "import sys\n"
            "sys.modules['datasette'] = None\n"
            "import discovery.__main__\n"
            "import discovery.trace\n"
            "import discovery.trace_fixture\n"
            "import discovery.notify\n"
            "import discovery.pipeline\n"
            "import discovery.teach\n"
            "import discovery.config\n"
            "print('IMPORTS_OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(config.REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("IMPORTS_OK", result.stdout)

    def test_observatory_app_genuinely_requires_datasette(self):
        # The converse of the test above -- proves the blocking trick itself
        # actually blocks (observatory/app.py DOES import datasette), so a
        # bug that silently no-ops the import couldn't make the previous
        # test pass vacuously.
        import subprocess

        script = (
            "import sys\n"
            "sys.modules['datasette'] = None\n"
            "try:\n"
            "    import observatory.app\n"
            "    print('IMPORTED_UNEXPECTEDLY')\n"
            "except ImportError:\n"
            "    print('BLOCKED_AS_EXPECTED')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(config.REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
        self.assertIn("BLOCKED_AS_EXPECTED", result.stdout)

    def test_trace_fixture_db_override_works_before_and_after_the_subcommand(self):
        # Repair-2 regression: `tf.add_argument("--db")` on the trace-fixture
        # subparser wrote its own default (None) over an already-parsed
        # global --db, so `--db PATH trace-fixture` silently fell back to
        # cfg.db_path's real default (REPO_ROOT/discovery.db) instead of
        # PATH. _main() always puts --db before the subcommand, which is
        # exactly the form that broke -- exercise both orders directly
        # against discovery.__main__.main so this can't go blind again.
        import contextlib
        import io

        from discovery.__main__ import main

        for argv in (
            ["--db", self.db_path, "trace-fixture"],
            ["trace-fixture", "--db", self.db_path],
        ):
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main(argv)
                self.assertEqual(code, 0, err.getvalue())
                conn = db.connect(self.db_path)
                self.addCleanup(conn.close)
                count = conn.execute("SELECT COUNT(*) c FROM trace_runs").fetchone()["c"]
                self.assertGreater(count, 0, "fixture did not write to the overridden --db path")
                conn.close()
                os.remove(self.db_path)

    def test_interests_list_is_empty_before_init(self):
        code, out, _err = self._main("interests")
        self.assertEqual(code, 0)
        self.assertIn("no interests", out)

    def test_interests_why_on_an_owner_key_shows_the_sync_event(self):
        path = self._interests_file('{"interests": [{"key": "x", "title": "X"}]}')
        self._main("init", env={"DISCOVERY_INTERESTS": path})
        code, out, _err = self._main("interests", "--why", "x")
        self.assertEqual(code, 0)
        self.assertIn("owner_sync", out)
        self.assertIn("-> owner", out)

    def test_interests_why_on_an_unknown_key_exits_cleanly(self):
        code, _out, err = self._main("interests", "--why", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no interest with key", err)

    def test_interests_layer_filter(self):
        path = self._interests_file('{"interests": [{"key": "x", "title": "X"}]}')
        self._main("init", env={"DISCOVERY_INTERESTS": path})
        code, out, _err = self._main("interests", "--layer", "owner")
        self.assertEqual(code, 0)
        self.assertIn("layer=owner", out)
        code, out, _err = self._main("interests", "--layer", "inferred")
        self.assertEqual(code, 0)
        self.assertIn("no interests", out)

    def test_interests_refresh_is_a_noop_when_the_flag_is_off(self):
        code, out, _err = self._main("interests", "--refresh")
        self.assertEqual(code, 0)
        self.assertIn("dynamic interests are off", out)

    def test_interests_refresh_runs_apply_transitions_when_the_flag_is_on(self):
        code, out, _err = self._main(
            "interests", "--refresh", env={"DISCOVERY_DYNAMIC_INTERESTS": "1"}
        )
        self.assertEqual(code, 0)
        self.assertIn('"enabled": true', out)

    def test_interests_refresh_with_a_missing_interests_file_exits_cleanly(self):
        missing = os.path.join(self.tmp.name, "nope.json")
        code, _out, err = self._main(
            "interests", "--refresh",
            env={"DISCOVERY_DYNAMIC_INTERESTS": "1", "DISCOVERY_INTERESTS": missing},
        )
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_interests_refresh_with_a_malformed_interests_file_exits_cleanly(self):
        path = self._interests_file("{ not json")
        code, _out, err = self._main(
            "interests", "--refresh",
            env={"DISCOVERY_DYNAMIC_INTERESTS": "1", "DISCOVERY_INTERESTS": path},
        )
        self.assertEqual(code, 2)
        self.assertIn("malformed interests file", err)

    def test_interests_refresh_seeds_from_personal_state_without_ever_building_a_provider(self):
        """Work items 1 + 4 at the CLI layer: `interests --refresh` seeds a
        personal-state topic (already reachable -- apply_transitions() calls
        personal_state.load_optional() itself, no separate wiring needed),
        never touches the provider factory (zero LLM/network calls), and
        `--why` on the seeded key prints the full seed origin."""
        path = self._interests_file('{"interests": []}')
        ps_path = os.path.join(self.tmp.name, "ps.json")
        with open(ps_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "contract_version": 1, "generated_at": "2026-08-10T00:00:00Z",
                    "topics": [{"key": "zzqleaktest", "weight": 1.0}],
                },
                fh,
            )
        self._main("init", env={"DISCOVERY_INTERESTS": path})

        with mock.patch.object(providers, "get_provider", side_effect=AssertionError("provider built")):
            code, out, _err = self._main(
                "interests", "--refresh",
                env={
                    "DISCOVERY_DYNAMIC_INTERESTS": "1", "DISCOVERY_INTERESTS": path,
                    "DISCOVERY_PERSONAL_STATE": ps_path,
                },
            )
        self.assertEqual(code, 0)
        self.assertIn('"seeded": 1', out)

        code, out, _err = self._main("interests", "--why", "derived:zzqleaktest")
        self.assertEqual(code, 0)
        self.assertIn("seed", out)
        self.assertIn("artifact_sha256", out)
        self.assertIn('"generated_at": "2026-08-10T00:00:00Z"', out)
        self.assertIn('"contract_version": 1', out)
        self.assertIn('"topic_key": "zzqleaktest"', out)


class TeachCLITests(unittest.TestCase):
    """`python -m app teach` wiring: --list/--explain, provider isolation,
    the interactive default, and --send reusing the existing Telegram flow
    end to end (see FeedbackListenerTests for the flow it reuses)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "t.db")
        conn = db.connect(self.db_path)
        db.init(conn)
        db.upsert_interest(conn, an_interest())
        interest = db.interest_by_key(conn, "k")
        item = stored_item(conn)
        db.save_score(conn, a_score(item.id, interest.id, 0.8))
        conn.close()

    def _main(self, *argv, read_inputs=None):
        import contextlib

        from discovery.__main__ import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if read_inputs is not None:
                stack.enter_context(mock.patch("builtins.input", side_effect=iter(read_inputs)))
            code = main(["--db", self.db_path, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_prints_the_ranked_queue_and_exits_zero(self):
        code, out, _err = self._main("teach", "--list")
        self.assertEqual(code, 0)
        self.assertIn("queued item(s)", out)

    def test_explain_prints_queue_metrics_and_exits_zero(self):
        code, out, _err = self._main("teach", "--explain")
        self.assertEqual(code, 0)
        self.assertIn("pool_size=", out)
        self.assertIn("band_lift", out)

    def test_teach_never_constructs_a_provider(self):
        with mock.patch.object(
            providers, "get_provider", side_effect=AssertionError("must not build a provider")
        ):
            code, _out, _err = self._main("teach", "--list")
        self.assertEqual(code, 0)

    def test_interactive_default_records_a_label_via_the_injected_reader(self):
        code, _out, _err = self._main("teach", "--limit", "1", read_inputs=["fire"])
        self.assertEqual(code, 0)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 1)

    def test_send_reuses_format_message_and_the_feedback_keyboard(self):
        sent = []

        def fake_send(cfg, text, reply_markup=None, dry_run=False):
            sent.append((text, reply_markup))
            return True

        with mock.patch.object(notify, "send", side_effect=fake_send):
            code, _out, _err = self._main("teach", "--send", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        text, markup = sent[0]
        self.assertIn("DISCOVERY", text)
        callback_datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertEqual(len(callback_datas), 4)
        score_id = callback_datas[0].split(":")[2]
        self.assertTrue(all(cd.endswith(f":{score_id}") for cd in callback_datas))

        # Proof the send path reuses the existing flow, not a new one: feed
        # one of those exact payloads into the real listener callback.
        callback = {"id": "cb", "data": callback_datas[0], "message": {"chat": {"id": 1}}}
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        with mock.patch.object(feedback_listener, "api_call"):
            self.assertTrue(feedback_listener._handle_callback(conn, "tok", callback))
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 1)

    def test_dry_run_send_never_touches_the_network(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            code, _out, _err = self._main("--dry-run", "teach", "--send")
        self.assertEqual(code, 0)
        urlopen.assert_not_called()


class TeachCLITests(unittest.TestCase):
    """`python -m app teach` wiring: --list/--explain, provider isolation,
    the interactive default, and --send reusing the existing Telegram flow
    end to end (see FeedbackListenerTests for the flow it reuses)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "t.db")
        conn = db.connect(self.db_path)
        db.init(conn)
        db.upsert_interest(conn, an_interest())
        interest = db.interest_by_key(conn, "k")
        item = stored_item(conn)
        db.save_score(conn, a_score(item.id, interest.id, 0.8))
        conn.close()

    def _main(self, *argv, read_inputs=None):
        import contextlib

        from discovery.__main__ import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if read_inputs is not None:
                stack.enter_context(mock.patch("builtins.input", side_effect=iter(read_inputs)))
            code = main(["--db", self.db_path, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_prints_the_ranked_queue_and_exits_zero(self):
        code, out, _err = self._main("teach", "--list")
        self.assertEqual(code, 0)
        self.assertIn("queued item(s)", out)

    def test_explain_prints_queue_metrics_and_exits_zero(self):
        code, out, _err = self._main("teach", "--explain")
        self.assertEqual(code, 0)
        self.assertIn("pool_size=", out)
        self.assertIn("band_lift", out)

    def test_teach_never_constructs_a_provider(self):
        with mock.patch.object(
            providers, "get_provider", side_effect=AssertionError("must not build a provider")
        ):
            code, _out, _err = self._main("teach", "--list")
        self.assertEqual(code, 0)

    def test_interactive_default_records_a_label_via_the_injected_reader(self):
        code, _out, _err = self._main("teach", "--limit", "1", read_inputs=["fire"])
        self.assertEqual(code, 0)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 1)

    def test_send_reuses_format_message_and_the_feedback_keyboard(self):
        sent = []

        def fake_send(cfg, text, reply_markup=None, dry_run=False):
            sent.append((text, reply_markup))
            return True

        with mock.patch.object(notify, "send", side_effect=fake_send):
            code, _out, _err = self._main("teach", "--send", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        text, markup = sent[0]
        self.assertIn("DISCOVERY", text)
        callback_datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertEqual(len(callback_datas), 4)
        score_id = callback_datas[0].split(":")[2]
        self.assertTrue(all(cd.endswith(f":{score_id}") for cd in callback_datas))

        # Proof the send path reuses the existing flow, not a new one: feed
        # one of those exact payloads into the real listener callback.
        callback = {"id": "cb", "data": callback_datas[0], "message": {"chat": {"id": 1}}}
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        with mock.patch.object(feedback_listener, "api_call"):
            self.assertTrue(feedback_listener._handle_callback(conn, "tok", callback))
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 1)

    def test_dry_run_send_never_touches_the_network(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            code, _out, _err = self._main("--dry-run", "teach", "--send")
        self.assertEqual(code, 0)
        urlopen.assert_not_called()


class CLIPrintSafetyTests(unittest.TestCase):
    """Regression: __main__.py's own output helpers must survive a narrow
    console codepage the same way notify.print_safe already does. They used
    plain print() instead of the already-imported print_safe, so a
    model-generated character outside e.g. Windows cp1255 (a real one seen
    live: '≥') crashed `discover`/`items` mid-run with a traceback and
    lost every remaining candidate's output instead of just substituting
    a '?' for the one unprintable character."""

    def _narrow_stdout(self):
        import io

        return io.TextIOWrapper(io.BytesIO(), encoding="cp1255", errors="strict")

    def test_print_discovered_survives_a_narrow_codepage(self):
        from discovery.__main__ import _print_discovered
        from discovery.pipeline import Outcome

        item = an_item(title="Non-ASCII ≥ in the title")
        score = a_score(item_id=1, interest_id=1, final_score=0.8)
        score.reason = "Effect size ≥ threshold"
        outcome = Outcome("scored", item, "", [], score)

        with mock.patch("sys.stdout", self._narrow_stdout()):
            _print_discovered(an_interest(), item, outcome)  # must not raise

    def test_list_items_survives_a_narrow_codepage(self):
        from discovery.__main__ import _list_items

        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.init(conn)
        db.upsert_interest(conn, an_interest())
        item = stored_item(conn, title="Item", text=BODY)
        interest_row = db.interest_by_key(conn, "k")
        score = a_score(item.id, interest_row.id, 0.8)
        score.reason = "Effect size ≥ threshold"
        db.save_score(conn, score)

        with mock.patch("sys.stdout", self._narrow_stdout()):
            _list_items(conn, limit=10, min_score=0.0)  # must not raise

    def test_personal_state_probe_survives_a_narrow_codepage(self):
        from discovery.__main__ import _personal_state

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "ps.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "contract_version": 1,
                    "generated_at": "2026-08-01T00:00:00Z",
                    "topics": [{"key": "topic ≥ threshold", "weight": 0.9}],
                },
                fh,
            )
        args = SimpleNamespace(path=path)
        with mock.patch("sys.stdout", self._narrow_stdout()):
            _personal_state(None, args)  # must not raise


class MainJobTests(unittest.TestCase):
    """The heartbeat/counter wrapping and job dispatch helpers, exercised
    directly rather than through argparse+a real provider -- a run-once/health
    CLI invocation would otherwise mean constructing a real ClaudeChatProvider
    and hitting localhost:9222 for real, which these tests must never do."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def test_run_job_success_records_ok_and_bumps_the_counter(self):
        from discovery.__main__ import _run_job

        code = _run_job(self.conn, "stocks", lambda: 0)
        self.assertEqual(code, 0)
        self.assertEqual(db.today_counts(self.conn), {"run_ok": 1})
        self.assertIsNotNone(db.state_get(self.conn, "job:stocks:last_ok"))

    def test_run_job_nonzero_exit_records_failure_without_raising(self):
        from discovery.__main__ import _run_job

        code = _run_job(self.conn, "stocks", lambda: 3)
        self.assertEqual(code, 3)
        self.assertEqual(db.today_counts(self.conn), {"run_failed": 1})
        self.assertIsNotNone(db.state_get(self.conn, "job:stocks:last_fail"))

    def test_run_job_an_exception_records_failure_and_still_propagates(self):
        from discovery.__main__ import _run_job

        def boom():
            raise RuntimeError("dead")

        with self.assertRaises(RuntimeError):
            _run_job(self.conn, "stocks", boom)
        self.assertEqual(db.today_counts(self.conn), {"run_failed": 1})
        self.assertIsNotNone(db.state_get(self.conn, "job:stocks:last_fail"))

    def test_run_once_cmd_exits_3_without_touching_run_once_when_preflight_fails(self):
        from discovery import __main__ as main_module
        from discovery.__main__ import _run_once_cmd

        provider = FakeProvider()
        provider.preflight = lambda: (False, "no CDP endpoint")
        with mock.patch.object(main_module, "run_once") as run_once_fn:
            code = _run_once_cmd(self.conn, provider, CFG, None, True, "stocks")
        self.assertEqual(code, 3)
        run_once_fn.assert_not_called()
        self.assertEqual(db.today_counts(self.conn), {"provider_down": 1})

    def test_run_once_cmd_runs_the_cycle_when_preflight_passes(self):
        from discovery.__main__ import _run_once_cmd

        db.upsert_interest(self.conn, an_interest(sources=[]))
        code = _run_once_cmd(self.conn, FakeProvider(), CFG, None, True, "stocks")
        self.assertEqual(code, 0)

    def test_health_cmd_prints_the_report_and_exits_1_when_degraded(self):
        from discovery.__main__ import _health_cmd

        provider = FakeProvider()
        provider.preflight = lambda: (False, "down")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = _health_cmd(self.conn, CFG, provider, SimpleNamespace(notify=False))
        self.assertEqual(code, 1)
        self.assertIn("DEGRADED", out.getvalue())

    def test_health_cmd_notify_flag_triggers_the_alert_path(self):
        from discovery.__main__ import _health_cmd

        provider = FakeProvider()
        provider.preflight = lambda: (False, "down")
        with mock.patch.object(notify, "send", return_value=True) as send, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            _health_cmd(self.conn, CFG, provider, SimpleNamespace(notify=True))
        send.assert_called_once()

    def test_drain_cmd_reports_the_count(self):
        from discovery.__main__ import _drain_cmd

        with mock.patch.object(feedback_listener, "drain", return_value=2), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = _drain_cmd(self.conn, CFG)
        self.assertEqual(code, 0)
        self.assertIn("drained 2", out.getvalue())

    def test_drain_cmd_reports_failure_when_drain_returns_the_none_sentinel(self):
        from discovery.__main__ import _drain_cmd

        with mock.patch.object(feedback_listener, "drain", return_value=None):
            code = _drain_cmd(self.conn, CFG)
        self.assertEqual(code, 1)

    def test_run_job_does_not_record_last_ok_for_a_failed_drain(self):
        # End-to-end through _run_job: a transport failure must not stamp
        # job:feedback:last_ok or bump run_ok alongside run_failed.
        from discovery.__main__ import _drain_cmd, _run_job

        with mock.patch.object(feedback_listener, "drain", return_value=None):
            code = _run_job(self.conn, "feedback", lambda: _drain_cmd(self.conn, CFG))
        self.assertEqual(code, 1)
        self.assertEqual(db.today_counts(self.conn), {"run_failed": 1})
        self.assertIsNone(db.state_get(self.conn, "job:feedback:last_ok"))
        self.assertIsNotNone(db.state_get(self.conn, "job:feedback:last_fail"))

    def test_digest_cmd_reports_the_count(self):
        from discovery.__main__ import _digest_cmd

        with mock.patch("discovery.__main__.send_digest", return_value=5), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = _digest_cmd(self.conn, CFG, True)
        self.assertEqual(code, 0)
        self.assertIn("sent 5", out.getvalue())


class InstallTasksTests(unittest.TestCase):
    """ops/install_tasks.py, entirely offline: a FakeRunner stands in for
    subprocess so no test here ever registers, deletes or queries a real
    Scheduled Task."""

    def test_triggers_are_derived_from_config_not_hardcoded(self):
        cfg = dataclasses.replace(
            CFG, interval_stocks_seconds=111, interval_web_seconds=222,
            interval_youtube_seconds=333, digest_time="13:45",
        )
        by_name = {t.name: t for t in install_tasks.build_tasks(cfg)}
        self.assertEqual(
            by_name["internet-discovery-collect-stocks"].trigger_value, 111
        )
        self.assertEqual(
            by_name["internet-discovery-collect-web"].trigger_value, 222
        )
        self.assertEqual(
            by_name["internet-discovery-collect-youtube"].trigger_value, 333
        )
        self.assertEqual(by_name["internet-discovery-digest"].trigger_value, "13:45")
        # Not derived from Config by design (see the plan): fixed cadences.
        self.assertEqual(by_name["internet-discovery-feedback"].trigger_value, 5 * 60)
        self.assertEqual(by_name["internet-discovery-health"].trigger_value, 3 * 3600)

    def test_tasks_share_the_prefix_and_carry_the_right_app_args(self):
        names = [t.name for t in install_tasks.build_tasks(CFG)]
        # 7 appliance tasks + the 3 interest-pipeline tasks (2026-08-18).
        self.assertEqual(len(names), 10)
        self.assertTrue(all(n.startswith("internet-discovery-") for n in names))
        by_name = {t.name: t for t in install_tasks.build_tasks(CFG)}
        self.assertEqual(
            by_name["internet-discovery-collect-stocks"].app_args,
            ["run-once", "--source", "stocks"],
        )
        self.assertEqual(by_name["internet-discovery-digest"].app_args, ["digest"])
        self.assertEqual(
            by_name["internet-discovery-feedback"].app_args, ["listen", "--drain"]
        )
        self.assertEqual(
            by_name["internet-discovery-health"].app_args, ["health", "--notify"]
        )
        # The updater is not a `python -m app` subcommand -- it shells to its own
        # script with no app args.
        update = by_name["internet-discovery-update"]
        self.assertEqual(update.app_args, [])
        self.assertEqual(update.script, "update.cmd")

    def test_the_update_task_action_shells_to_update_cmd(self):
        update = next(t for t in install_tasks.build_tasks(CFG)
                      if t.name == "internet-discovery-update")
        xml = install_tasks.render_xml(update)
        self.assertIn("update.cmd", xml)
        self.assertNotIn("run.cmd", xml)

    def test_rendered_xml_uses_the_d_flag_and_run_cmd(self):
        task = install_tasks.build_tasks(CFG)[0]
        xml = install_tasks.render_xml(task)
        self.assertIn("/d /c", xml)
        self.assertIn("run.cmd", xml)
        self.assertIn("StartWhenAvailable>true<", xml)
        self.assertIn("InteractiveToken", xml)
        self.assertIn("IgnoreNew", xml)

    def test_action_launches_hidden_via_wscript(self):
        # InteractiveToken console actions flash a cmd window on every
        # trigger; the action must go through wscript + ops/hidden.vbs
        # (GUI-subsystem host, hidden child window) instead.
        for task in install_tasks.build_tasks(CFG):
            xml = install_tasks.render_xml(task)
            self.assertIn("wscript.exe</Command>", xml)
            self.assertIn("hidden.vbs", xml)
            self.assertIn("cmd.exe /d /c", xml)

    def test_dry_run_install_spawns_no_process(self):
        calls = []
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            code = install_tasks.install(CFG, runner=lambda args: calls.append(args), dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_dry_run_prints_a_copy_pasteable_command_with_a_real_path(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = install_tasks.install(CFG, runner=lambda args: (0, "", ""), dry_run=True)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertNotIn("<generated>.xml", text)
        self.assertIn("schtasks /create /tn internet-discovery-collect-stocks", text)
        self.assertIn(".xml /f", text)

    def test_install_calls_schtasks_create_then_verifies_with_a_query(self):
        calls = []

        def fake_runner(args):
            calls.append(args)
            return 0, "SUCCESS", ""

        with mock.patch("sys.stdout", new_callable=io.StringIO):
            code = install_tasks.install(CFG, runner=fake_runner, dry_run=False)
        self.assertEqual(code, 0)
        # One /create + one /query per task -- install() no longer trusts
        # /create's exit code alone to mean the task actually exists. Derived
        # from _TASK_SPECS rather than restated, so adding a task cannot leave
        # this assertion quietly checking the wrong number.
        expected = len(install_tasks.build_tasks(CFG))
        self.assertEqual(len(calls), 2 * expected)
        creates = [a for a in calls if "/create" in a]
        queries = [a for a in calls if "/query" in a]
        self.assertEqual(len(creates), expected)
        self.assertEqual(len(queries), expected)
        for args in creates:
            self.assertEqual(args[0], "schtasks")
            self.assertIn("/xml", args)
        for args in queries:
            self.assertEqual(args, ["schtasks", "/query", "/tn", args[-1]])

    # --- the interest-suggestion pipeline's three tasks (2026-08-18) --------
    # Before these existed, `Get-ScheduledTask internet-discovery-*` covered
    # collectors, digest, feedback, health and update -- and nothing at all
    # ran the extractor, the importer or the lifecycle sweep. The sweep was
    # the costly omission: with no timer, offers never expired, snoozed offers
    # never woke, and interests never decayed (30d) or auto-paused (45d).

    def test_the_interest_pipeline_has_a_task_for_each_of_its_three_stages(self):
        by_name = {t.name: t for t in install_tasks.build_tasks(CFG)}
        for suffix, app_args in (
            ("interest-extract", ["extract-interests"]),
            ("offers-import", ["offers", "--import"]),
            ("offers-sweep", ["offers", "--sweep"]),
        ):
            task = by_name[f"internet-discovery-{suffix}"]
            self.assertEqual(task.app_args, app_args)
            # All three go through ops/run.cmd like every other `python -m app`
            # subcommand -- the extractor deliberately did NOT get a second
            # launcher of its own in the sibling `ai` repo.
            self.assertEqual(task.script, "run.cmd")

    def test_the_three_pipeline_cadences_come_from_config_not_literals(self):
        cfg = dataclasses.replace(
            CFG,
            offers_import_interval_seconds=1234,
            offers_sweep_interval_seconds=5678,
            interest_extract_time="04:44",
        )
        by_name = {t.name: t for t in install_tasks.build_tasks(cfg)}
        self.assertEqual(by_name["internet-discovery-offers-import"].trigger_value, 1234)
        self.assertEqual(by_name["internet-discovery-offers-sweep"].trigger_value, 5678)
        self.assertEqual(by_name["internet-discovery-interest-extract"].trigger_value, "04:44")

    def test_the_sweep_fires_at_least_daily(self):
        """The 30-day decay and 45-day auto-pause rules are only real if
        something evaluates them. Anything slower than daily makes the
        lifecycle timers approximate at best."""
        by_name = {t.name: t for t in install_tasks.build_tasks(CFG)}
        sweep = by_name["internet-discovery-offers-sweep"]
        self.assertEqual(sweep.trigger_kind, "interval")
        self.assertLessEqual(sweep.trigger_value, 24 * 3600)

    def test_the_extractor_fires_once_a_day_not_through_the_digest_window(self):
        """The regression this guards: build_tasks() used to hand the digest's
        Repetition (every digest_interval_seconds until digest_window_end) to
        EVERY task whose trigger kind was "daily". That was invisible while
        the digest was the only daily task. Applied to the extractor it would
        have re-fired an hour of browser work up to 30 times a day."""
        by_name = {t.name: t for t in install_tasks.build_tasks(CFG)}
        extract = by_name["internet-discovery-interest-extract"]
        self.assertEqual(extract.trigger_kind, "daily")
        self.assertEqual(extract.repeat_seconds, 0)
        self.assertEqual(extract.window_end, "")
        xml = install_tasks.render_xml(extract)
        self.assertIn("<CalendarTrigger>", xml)
        self.assertNotIn("<Repetition>", xml)
        # The digest keeps its window -- the opt-in did not silently drop it.
        digest = by_name["internet-discovery-digest"]
        self.assertEqual(digest.repeat_seconds, CFG.digest_interval_seconds)
        self.assertIn("<Repetition>", install_tasks.render_xml(digest))

    def test_the_extractor_runs_outside_the_digest_delivery_window(self):
        """Browser contention. The extractor is the only task here that holds
        a claude.ai tab for minutes at a time; scheduling it inside
        digest_time..digest_window_end would overlap the delivery burst."""
        extract_h, extract_m = (int(x) for x in CFG.interest_extract_time.split(":"))
        start_h = int(CFG.digest_time.split(":")[0])
        end_h = int(CFG.digest_window_end.split(":")[0])
        self.assertFalse(start_h <= extract_h < end_h,
                         f"{CFG.interest_extract_time} falls inside the digest window")
        self.assertIsInstance(extract_m, int)

    def test_every_task_gets_a_distinct_log_file(self):
        """ops/run.cmd names the log from the FULL argument list, and cmd's
        `>>` holds it without FILE_SHARE_WRITE for the whole run -- two tasks
        sharing a basename means the second one silently never runs. `offers
        --import` and `offers --sweep` share a %1, which is exactly the shape
        of the bug that once cost all three collect tasks their runs."""
        lognames = [
            "_".join(t.app_args) or t.script
            for t in install_tasks.build_tasks(CFG)
        ]
        self.assertEqual(len(lognames), len(set(lognames)), lognames)

    def test_install_fails_when_the_verification_query_cannot_find_the_task(self):
        def fake_runner(args):
            if "/create" in args:
                return 0, "SUCCESS", ""
            return 1, "", "ERROR: The system cannot find the file specified."

        with mock.patch("sys.stdout", new_callable=io.StringIO), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            code = install_tasks.install(CFG, runner=fake_runner, dry_run=False)
        self.assertEqual(code, 1)

    def test_install_reports_failure_without_raising(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            code = install_tasks.install(CFG, runner=lambda args: (1, "", "denied"), dry_run=False)
        self.assertEqual(code, 1)

    def test_installed_xml_file_is_written_utf16le_with_bom(self):
        # install() unlinks its temp XML file once /create + /query finish,
        # so read it from inside the fake runner, while it still exists.
        captured = {}

        def fake_runner(args):
            if "/create" in args:
                path = args[args.index("/xml") + 1]
                captured["bytes"] = open(path, "rb").read()
            return 0, "SUCCESS", ""

        with mock.patch("sys.stdout", new_callable=io.StringIO):
            install_tasks.install(CFG, runner=fake_runner, dry_run=False)
        data = captured["bytes"]
        self.assertEqual(data[:2], b"\xff\xfe")   # UTF-16LE BOM
        self.assertIn("<Task", data.decode("utf-16"))

    def test_collect_tasks_do_not_share_a_start_boundary(self):
        tasks = install_tasks.build_tasks(CFG)
        collect = [t for t in tasks if t.name.startswith("internet-discovery-collect-")]
        starts = set()
        for task in collect:
            xml = install_tasks.render_xml(task)
            start = xml.split("<StartBoundary>")[1].split("</StartBoundary>")[0]
            starts.add(start)
        self.assertEqual(len(starts), len(collect))

    def test_daily_trigger_rolls_forward_when_todays_time_has_passed(self):
        cfg = dataclasses.replace(CFG, digest_time="00:00")
        task = next(t for t in install_tasks.build_tasks(cfg) if t.trigger_kind == "daily")
        xml = install_tasks.render_xml(task)
        start = xml.split("<StartBoundary>")[1].split("</StartBoundary>")[0]
        start_dt = datetime.fromisoformat(start)
        # 00:00 has already elapsed by the time any test runs; the rendered
        # boundary must be in the future, not today's already-missed slot.
        self.assertGreater(start_dt, datetime.now())

    def test_digest_repeats_within_its_window_not_just_once_a_day(self):
        cfg = dataclasses.replace(
            CFG, digest_time="09:00", digest_window_end="12:00",
            digest_interval_seconds=1800,
        )
        task = next(t for t in install_tasks.build_tasks(cfg) if t.trigger_kind == "daily")
        xml = install_tasks.render_xml(task)
        self.assertIn("<Repetition>", xml)
        self.assertIn("<Interval>PT30M</Interval>", xml)
        # 09:00 -> 12:00 is a 3-hour window, regardless of which day the
        # (possibly rolled-forward) StartBoundary lands on.
        self.assertIn("<Duration>PT3H</Duration>", xml)

    def test_digest_window_end_before_start_wraps_to_the_next_day(self):
        cfg = dataclasses.replace(
            CFG, digest_time="20:00", digest_window_end="02:00",
            digest_interval_seconds=3600,
        )
        task = next(t for t in install_tasks.build_tasks(cfg) if t.trigger_kind == "daily")
        xml = install_tasks.render_xml(task)
        self.assertIn("<Duration>PT6H</Duration>", xml)

    def test_zero_digest_interval_falls_back_to_a_single_daily_fire(self):
        cfg = dataclasses.replace(CFG, digest_interval_seconds=0)
        task = next(t for t in install_tasks.build_tasks(cfg) if t.trigger_kind == "daily")
        xml = install_tasks.render_xml(task)
        self.assertNotIn("<Repetition>", xml)

    def test_interval_tasks_still_repeat_forever_with_no_duration(self):
        # Regression guard: the digest's new bounded Repetition must not leak
        # onto the plain interval-kind tasks, which repeat with no Duration
        # (i.e. forever) by design.
        task = next(
            t for t in install_tasks.build_tasks(CFG)
            if t.name == "internet-discovery-collect-stocks"
        )
        xml = install_tasks.render_xml(task)
        self.assertIn("<Repetition>", xml)
        self.assertNotIn("<Duration>", xml)

    def test_uninstall_is_scoped_to_the_six_task_names_plus_soak(self):
        calls = []
        install_tasks.uninstall(runner=lambda args: calls.append(args) or (0, "", ""))
        deleted = [args[args.index("/tn") + 1] for args in calls]
        self.assertEqual(
            sorted(deleted), sorted(install_tasks.TASK_NAMES + [install_tasks.SOAK_TASK])
        )
        self.assertTrue(all(name.startswith("internet-discovery-") for name in deleted))
        self.assertTrue(all(not name.startswith("ec-") for name in deleted))

    def test_status_parses_schtasks_query_output_for_our_prefix(self):
        output = (
            "HostName:                             HOST\r\n"
            "TaskName:                             \\internet-discovery-health\r\n"
            "Next Run Time:                        8/10/2026 11:00:00 PM\r\n"
            "Status:                               Ready\r\n"
            "Last Run Time:                        8/10/2026 8:00:00 PM\r\n"
            "Last Result:                          0\r\n"
            "\r\n"
            "TaskName:                             \\some-other-task\r\n"
            "Status:                               Ready\r\n"
        )
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = install_tasks.status(runner=lambda args: (0, output, ""))
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("internet-discovery-health:", text)
        self.assertIn("Ready", text)
        self.assertIn("internet-discovery-collect-stocks: not installed", text)
        self.assertNotIn("some-other-task", text)

    def test_soak_task_is_not_one_of_the_recurring_build_tasks(self):
        # SOAK_TASK must stay out of _TASK_SPECS/build_tasks -- otherwise
        # --install would create and recreate/reschedule it on every reinstall.
        names = [t.name for t in install_tasks.build_tasks(CFG)]
        self.assertNotIn(install_tasks.SOAK_TASK, names)

    def test_soak_trigger_is_a_single_time_trigger_with_no_repetition(self):
        task = install_tasks.TaskDef(
            install_tasks.SOAK_TASK, [], "once", 24, "PT15M", script="soak_check.cmd"
        )
        xml = install_tasks.render_xml(task)
        self.assertIn("<TimeTrigger>", xml)
        self.assertNotIn("<Repetition>", xml)
        self.assertIn("StartWhenAvailable>true<", xml)
        start = xml.split("<StartBoundary>")[1].split("</StartBoundary>")[0]
        start_dt = datetime.fromisoformat(start)
        self.assertGreater(start_dt, datetime.now() + timedelta(hours=23))
        self.assertLess(start_dt, datetime.now() + timedelta(hours=25))

    def test_soak_action_points_at_soak_check_cmd_not_run_cmd(self):
        task = install_tasks.TaskDef(
            install_tasks.SOAK_TASK, [], "once", 24, "PT15M", script="soak_check.cmd"
        )
        xml = install_tasks.render_xml(task)
        self.assertIn("soak_check.cmd", xml)
        self.assertNotIn("run.cmd", xml)

    def test_install_soak_creates_and_verifies_one_task(self):
        calls = []

        def fake_runner(args):
            calls.append(args)
            return 0, "SUCCESS", ""

        with mock.patch("sys.stdout", new_callable=io.StringIO):
            code = install_tasks.install_soak(CFG, runner=fake_runner, dry_run=False, hours=24)
        self.assertEqual(code, 0)
        creates = [a for a in calls if "/create" in a]
        queries = [a for a in calls if "/query" in a]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(queries), 1)
        self.assertEqual(creates[0][creates[0].index("/tn") + 1], install_tasks.SOAK_TASK)

    def test_install_soak_dry_run_spawns_no_process(self):
        calls = []
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            code = install_tasks.install_soak(
                CFG, runner=lambda args: calls.append(args), dry_run=True, hours=24
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_install_soak_prints_start_boundary_and_readout_path(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            install_tasks.install_soak(
                CFG, runner=lambda args: (0, "SUCCESS", ""), dry_run=False, hours=24
            )
        text = out.getvalue()
        self.assertIn("StartBoundary:", text)
        self.assertIn("readout path", text)
        self.assertIn("logs", text)

    def test_uninstall_deletes_soak_task_even_if_never_registered(self):
        # schtasks /delete on a name that was never created just fails --
        # uninstall() still tries it and reports the miss, same as any other
        # not-installed name; it must not skip it or raise.
        calls = []

        def fake_runner(args):
            calls.append(args)
            if install_tasks.SOAK_TASK in args:
                return 1, "", "ERROR: The system cannot find the file specified."
            return 0, "", ""

        with mock.patch("sys.stdout", new_callable=io.StringIO):
            code = install_tasks.uninstall(runner=fake_runner)
        self.assertEqual(code, 0)
        deleted = [args[args.index("/tn") + 1] for args in calls]
        self.assertIn(install_tasks.SOAK_TASK, deleted)

    def test_main_routes_argv_to_the_right_function_with_dry_run_threaded(self):
        # Regression: `--uninstall --dry-run` used to parse fine but dropped
        # dry_run on the way to uninstall(), so a preview performed a real
        # deletion of all seven tasks (including a live soak checkpoint).
        cases = [
            (["--install"], "install", False),
            (["--install", "--dry-run"], "install", True),
            (["--uninstall"], "uninstall", False),
            (["--uninstall", "--dry-run"], "uninstall", True),
            (["--soak"], "install_soak", False),
            (["--soak", "--dry-run"], "install_soak", True),
            (["--status"], "status", None),
        ]
        for argv, fn_name, expect_dry_run in cases:
            with self.subTest(argv=argv):
                with mock.patch.object(install_tasks, "install", return_value=0) as m_install, \
                     mock.patch.object(install_tasks, "uninstall", return_value=0) as m_uninstall, \
                     mock.patch.object(install_tasks, "install_soak", return_value=0) as m_soak, \
                     mock.patch.object(install_tasks, "status", return_value=0) as m_status, \
                     mock.patch.object(install_tasks.config, "load", return_value=CFG):
                    code = install_tasks.main(argv)
                self.assertEqual(code, 0)
                mocks = {
                    "install": m_install,
                    "uninstall": m_uninstall,
                    "install_soak": m_soak,
                    "status": m_status,
                }
                mocks[fn_name].assert_called_once()
                for other_name, other_mock in mocks.items():
                    if other_name != fn_name:
                        other_mock.assert_not_called()
                if expect_dry_run is not None:
                    self.assertEqual(mocks[fn_name].call_args.kwargs.get("dry_run"), expect_dry_run)

    def test_main_rejects_status_with_dry_run(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                install_tasks.main(["--status", "--dry-run"])

    def test_status_reports_soak_task_only_when_present(self):
        output_without_soak = (
            "TaskName:                             \\internet-discovery-health\r\n"
            "Status:                               Ready\r\n"
            "\r\n"
        )
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            install_tasks.status(runner=lambda args: (0, output_without_soak, ""))
        self.assertNotIn(install_tasks.SOAK_TASK, out.getvalue())

        output_with_soak = output_without_soak + (
            f"TaskName:                             \\{install_tasks.SOAK_TASK}\r\n"
            "Status:                               Ready\r\n"
            "Next Run Time:                        8/11/2026 8:00:00 PM\r\n"
            "\r\n"
        )
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            install_tasks.status(runner=lambda args: (0, output_with_soak, ""))
        self.assertIn(f"{install_tasks.SOAK_TASK}:", out.getvalue())


class _FakeHTTPResponse:
    def __init__(self, body=b'{"hits": []}', status=200, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _write_recon_interests_fixture(dirpath):
    """Write a minimal interests.json whose keys are exactly the ones
    exp_connectors.APPLICABILITY samples, so the connector-recon pass2 tests
    reproduce the step-09 pre-registered sample independently of the live
    product interests.json -- whose keys the owner's 40-interest rewrite
    renamed. Only key/title are required by discovery.interests.load_file;
    everything else defaults."""
    keys = sorted({k for vals in exp_connectors.APPLICABILITY.values() for k in vals})
    path = os.path.join(dirpath, "interests.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"defaults": {}, "interests":
                   [{"key": k, "title": k.replace("-", " ")} for k in keys]}, f)
    return path


class ConnectorReconTests(unittest.TestCase):
    """exp_connectors.py (step-09a): offline, no network, no provider, no
    real DB -- every test injects a fake fetcher into _http_get or exercises
    the pure analysis/rule functions directly."""

    def setUp(self):
        exp_connectors._reset_http_state()

    def tearDown(self):
        exp_connectors._reset_http_state()

    # (a) canonicalization + overlap/marginal-unique-rate math ------------

    def test_canon_url_and_title(self):
        self.assertEqual(exp_connectors.canon_url("HTTPS://Example.com/Path?x=1#frag"),
                         "https://example.com/path")
        self.assertEqual(exp_connectors.canon_url("https://example.com/path/"),
                         "https://example.com/path")
        self.assertEqual(exp_connectors.canon_title("Some, Title! 2026"), "sometitle2026")

    def test_jaccard_overlap_exact_tie(self):
        a = {"https://e.com/1", "https://e.com/2"}
        self.assertEqual(exp_connectors.jaccard_overlap(a, set(a)), 1.0)

    def test_jaccard_overlap_empty_sets(self):
        self.assertEqual(exp_connectors.jaccard_overlap(set(), set()), 0.0)

    def test_marginal_unique_rate_hand_computed(self):
        sample = {"a", "b", "c", "d"}
        baseline = {"a", "b"}
        self.assertAlmostEqual(exp_connectors.marginal_unique_rate(sample, baseline), 0.5)

    def test_marginal_unique_rate_empty_sample_is_void(self):
        self.assertIsNone(exp_connectors.marginal_unique_rate(set(), {"a"}))

    # (b) falsification rule ------------------------------------------------

    def test_falsification_no_baseline_is_void(self):
        metrics = {"hackernews": {"marginal_unique_rate": 0.9, "jaccard_overlap": 0.1}}
        self.assertEqual(exp_connectors.apply_falsification_rule(metrics, False), "VOID_NO_BASELINE")

    def test_falsification_no_measurable_connectors(self):
        metrics = {"hackernews": {"marginal_unique_rate": None, "jaccard_overlap": None},
                  "reddit": {"marginal_unique_rate": None, "jaccard_overlap": 0.2}}
        self.assertEqual(exp_connectors.apply_falsification_rule(metrics, True),
                         "VOID_NO_MEASURABLE_CONNECTORS")

    def test_falsification_all_below_threshold_yields_falsified_not_void(self):
        metrics = {
            "hackernews": {"marginal_unique_rate": 0.10, "jaccard_overlap": 0.50},
            "reddit": {"marginal_unique_rate": 0.39, "jaccard_overlap": 0.10},   # rate just under 0.40
            "arxiv": {"marginal_unique_rate": 0.90, "jaccard_overlap": 0.31},    # overlap just over 0.30
        }
        self.assertEqual(exp_connectors.apply_falsification_rule(metrics, True), "H1_FALSIFIED")

    def test_falsification_supported_when_one_connector_clears_bar(self):
        metrics = {
            "hackernews": {"marginal_unique_rate": 0.10, "jaccard_overlap": 0.50},
            "pubmed": {"marginal_unique_rate": 0.45, "jaccard_overlap": 0.20},
        }
        self.assertEqual(exp_connectors.apply_falsification_rule(metrics, True), "H1_SUPPORTED")

    # (c) request counter + per-host spacing --------------------------------

    def test_request_cap_refuses_41st(self):
        with mock.patch("exp_connectors.time.sleep"), \
             mock.patch("exp_connectors.urllib.request.urlopen",
                        return_value=_FakeHTTPResponse()) as m_urlopen:
            for i in range(exp_connectors.MAX_REQUESTS):
                exp_connectors._http_get(f"https://host{i}.example/path")
            self.assertEqual(m_urlopen.call_count, exp_connectors.MAX_REQUESTS)
            with self.assertRaises(RuntimeError):
                exp_connectors._http_get("https://host-over.example/path")
            self.assertEqual(m_urlopen.call_count, exp_connectors.MAX_REQUESTS)

    def test_per_host_spacing_honored(self):
        fake_times = iter([0.0, 0.5, 1.5])
        with mock.patch("exp_connectors.time.monotonic", side_effect=lambda: next(fake_times)), \
             mock.patch("exp_connectors.time.sleep") as m_sleep, \
             mock.patch("exp_connectors.urllib.request.urlopen", return_value=_FakeHTTPResponse()):
            exp_connectors._http_get("https://hn.algolia.com/api/v1/search_by_date?query=a")
            exp_connectors._http_get("https://hn.algolia.com/api/v1/search_by_date?query=b")
        m_sleep.assert_called_once()
        self.assertAlmostEqual(m_sleep.call_args[0][0], 0.5, places=3)

    def test_arxiv_has_its_own_wider_spacing(self):
        self.assertEqual(exp_connectors.HOST_MIN_GAP_SECONDS["export.arxiv.org"], 3.0)
        self.assertEqual(exp_connectors.DEFAULT_MIN_GAP_SECONDS, 1.0)

    def test_per_connector_cap_refuses_11th(self):
        with mock.patch("exp_connectors.time.sleep"), \
             mock.patch("exp_connectors.urllib.request.urlopen",
                        return_value=_FakeHTTPResponse()) as m_urlopen:
            for i in range(exp_connectors.PER_CONNECTOR_CAP):
                exp_connectors._http_get(f"https://hn.algolia.com/api/v1/search_by_date?query={i}",
                                         connector="hackernews")
            self.assertEqual(m_urlopen.call_count, exp_connectors.PER_CONNECTOR_CAP)
            with self.assertRaises(RuntimeError):
                exp_connectors._http_get("https://hn.algolia.com/api/v1/search_by_date?query=over",
                                         connector="hackernews")
            self.assertEqual(m_urlopen.call_count, exp_connectors.PER_CONNECTOR_CAP)
            # a different connector's own cap is untouched by hackernews's
            exp_connectors._http_get("https://export.arxiv.org/api/query?search_query=x",
                                     connector="arxiv")
            self.assertEqual(m_urlopen.call_count, exp_connectors.PER_CONNECTOR_CAP + 1)

    # (d) missing corpus / missing network / failed preflight -> VOID/PENDING

    def test_load_corpus_urls_missing_file_is_void(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "discovery.db")
            urls, status = exp_connectors.load_corpus_urls(missing)
        self.assertIsNone(urls)
        self.assertFalse(status["available"])
        self.assertIn(missing, status["reason"])

    def test_http_connector_entry_void_when_endpoint_unreachable(self):
        def boom(url, timeout=15, connector=None):
            raise exp_connectors.ConnectorUnreachable("host.example: connection refused")

        interests_by_key = {"ai-agents-dev-tools": an_interest(
            key="ai-agents-dev-tools", title="T", positive_signals=["s1", "s2", "s3"])}
        with mock.patch("exp_connectors._http_get", side_effect=boom):
            entry = exp_connectors.build_http_connector_entry(
                "hackernews", ["ai-agents-dev-tools"], interests_by_key, set(), False,
                datetime.now(timezone.utc), "deadbeef", provider=None, provider_ok=False, lab=None)
        self.assertEqual(entry["sample"]["status"], "VOID_UNREACHABLE")
        self.assertIn("connection refused", entry["failure_behavior"])
        self.assertIn("connection refused", entry["availability"]["detail"])
        self.assertEqual(entry["metrics"]["n_sampled"], 0)
        self.assertIsNone(entry["metrics"]["marginal_unique_rate"])
        self.assertEqual(entry["metrics"]["marginal_unique_rate_status"], "VOID_UNREACHABLE")

    def test_http_connector_entry_void_on_low_n(self):
        def empty_hits(url, timeout=15, connector=None):
            return 200, b'{"hits": []}', {}

        interests_by_key = {"ai-agents-dev-tools": an_interest(
            key="ai-agents-dev-tools", title="T", positive_signals=["s1", "s2", "s3"])}
        with mock.patch("exp_connectors._http_get", side_effect=empty_hits):
            entry = exp_connectors.build_http_connector_entry(
                "hackernews", ["ai-agents-dev-tools"], interests_by_key, set(), False,
                datetime.now(timezone.utc), "deadbeef", provider=None, provider_ok=False, lab=None)
        self.assertEqual(entry["sample"]["status"], "VOID_LOW_N")
        # availability is about reachability (http 200), not sample size --
        # repair: these two used to be conflated into one 'detail' field.
        self.assertIn("reachable", entry["availability"]["detail"])
        self.assertNotIn("< 5", entry["availability"]["detail"])
        self.assertIn("< 5", entry["sample"]["detail"])

    def test_http_connector_entry_computes_offline_baseline_when_corpus_available(self):
        # repair: baseline_available used to be hardcoded False with no code
        # path that could ever set it True. The offline lane's substitute
        # baseline is the corpus alone (pre-registration) when reachable.
        def five_hits(url, timeout=15, connector=None):
            hits = [{"title": f"T{i}", "url": f"https://e.com/{i}",
                    "created_at": "2026-08-01T00:00:00.000Z", "objectID": str(i)}
                   for i in range(1, 6)]
            return 200, json.dumps({"hits": hits}).encode(), {}

        interests_by_key = {"ai-agents-dev-tools": an_interest(
            key="ai-agents-dev-tools", title="T", positive_signals=["s1", "s2", "s3"])}
        corpus_urls = {"https://e.com/1", "https://e.com/2"}   # 2 of the 5 sampled urls known
        with mock.patch("exp_connectors._http_get", side_effect=five_hits):
            entry = exp_connectors.build_http_connector_entry(
                "hackernews", ["ai-agents-dev-tools"], interests_by_key, corpus_urls, True,
                datetime.now(timezone.utc), "deadbeef", provider=None, provider_ok=False, lab=None)
        self.assertEqual(entry["sample"]["status"], "OK")
        self.assertAlmostEqual(entry["metrics"]["marginal_unique_rate"], 0.6)
        self.assertEqual(entry["metrics"]["marginal_unique_rate_status"],
                         "OK_OFFLINE_LANE_CORPUS_ONLY_BASELINE")

    def test_http_connector_entry_void_no_baseline_when_corpus_unavailable(self):
        def five_hits(url, timeout=15, connector=None):
            hits = [{"title": f"T{i}", "url": f"https://e.com/{i}",
                    "created_at": "2026-08-01T00:00:00.000Z", "objectID": str(i)}
                   for i in range(1, 6)]
            return 200, json.dumps({"hits": hits}).encode(), {}

        interests_by_key = {"ai-agents-dev-tools": an_interest(
            key="ai-agents-dev-tools", title="T", positive_signals=["s1", "s2", "s3"])}
        with mock.patch("exp_connectors._http_get", side_effect=five_hits):
            entry = exp_connectors.build_http_connector_entry(
                "hackernews", ["ai-agents-dev-tools"], interests_by_key, set(), False,
                datetime.now(timezone.utc), "deadbeef", provider=None, provider_ok=False, lab=None)
        self.assertEqual(entry["sample"]["status"], "OK")
        self.assertIsNone(entry["metrics"]["marginal_unique_rate"])
        self.assertEqual(entry["metrics"]["marginal_unique_rate_status"], "VOID_NO_BASELINE")

    def test_above_bar_not_spent_when_baseline_unavailable(self):
        # repair: above-bar sub-metric used to fire on provider_ok alone,
        # spending provider budget even when the connector's own primary
        # metric was structurally void this pass.
        def five_hits(url, timeout=15, connector=None):
            hits = [{"title": f"T{i}", "url": f"https://e.com/{i}",
                    "created_at": "2026-08-01T00:00:00.000Z", "objectID": str(i)}
                   for i in range(1, 6)]
            return 200, json.dumps({"hits": hits}).encode(), {}

        interests_by_key = {"ai-agents-dev-tools": an_interest(
            key="ai-agents-dev-tools", title="T", positive_signals=["s1", "s2", "s3"])}
        with mock.patch("exp_connectors._http_get", side_effect=five_hits):
            entry = exp_connectors.build_http_connector_entry(
                "hackernews", ["ai-agents-dev-tools"], interests_by_key, set(), False,
                datetime.now(timezone.utc), "deadbeef", provider=object(), provider_ok=True, lab=None)
        self.assertIsNone(entry["metrics"]["above_bar_count"])
        self.assertEqual(entry["metrics"]["above_bar_status"], "VOID_NO_BASELINE")

    # (e) provenance on every dossier record --------------------------------

    def test_per_interest_records_carry_provenance(self):
        def one_hit(url, timeout=15, connector=None):
            body = json.dumps({"hits": [{"title": "T1", "url": "https://e.com/1",
                                        "created_at": "2026-08-01T00:00:00.000Z",
                                        "objectID": "1"}]}).encode()
            return 200, body, {}

        interests_by_key = {"ai-agents-dev-tools": an_interest(
            key="ai-agents-dev-tools", title="T", positive_signals=["s1", "s2", "s3"])}
        with mock.patch("exp_connectors._http_get", side_effect=one_hit):
            entry = exp_connectors.build_http_connector_entry(
                "hackernews", ["ai-agents-dev-tools"], interests_by_key, set(), False,
                datetime.now(timezone.utc), "deadbeef", provider=None, provider_ok=False, lab=None)
        pi = entry["sample"]["per_interest"][0]
        self.assertEqual(pi["lane"], "zero_spend")
        self.assertEqual(pi["git_commit"], "deadbeef")
        self.assertTrue(pi["collected_at"])
        self.assertEqual(entry["metrics"]["sample_validity_rate"], 1.0)

    # (f) discovery.db is opened mode=ro only --------------------------------

    def test_load_corpus_urls_opens_mode_ro(self):
        captured = {}
        real_connect = sqlite3.connect

        def spy_connect(*args, **kwargs):
            captured["uri"] = args[0]
            return real_connect(*args, **kwargs)

        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "discovery.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE candidate_items (id INTEGER PRIMARY KEY, url TEXT)")
            conn.execute("INSERT INTO candidate_items (url) VALUES ('https://e.com/x')")
            conn.commit()
            conn.close()
            with mock.patch("db_replay.sqlite3.connect", side_effect=spy_connect):
                urls, status = exp_connectors.load_corpus_urls(db_path)
        self.assertTrue(status["available"])
        self.assertIn("https://e.com/x", urls)
        self.assertIn("mode=ro", captured["uri"])

    # (g) E5b / H2 (step-09): revised query rule, USABLE definition, gate ---

    def test_build_query_v2_first_four_distinctive_tokens_in_title_order(self):
        interest = an_interest(title="Zebra Quantum Coding Wizard Extra Words Here")
        self.assertEqual(exp_connectors.build_query_v2(interest), "zebra quantum coding wizard")

    def test_build_query_v2_dedupes_within_title(self):
        interest = an_interest(title="Coding Coding Quantum Wizard")
        self.assertEqual(exp_connectors.build_query_v2(interest), "coding quantum wizard")

    def test_build_query_v2_extends_from_positive_signals_until_two(self):
        interest = an_interest(title="AI", positive_signals=["Great Learning Systems Here"])
        self.assertEqual(exp_connectors.build_query_v2(interest), "great learning")

    def test_build_query_v2_extend_stops_at_two_not_four(self):
        interest = an_interest(title="Zebra", positive_signals=["Wizard Coding Extra"])
        self.assertEqual(exp_connectors.build_query_v2(interest), "zebra wizard")

    def test_usable_records_rejects_missing_url_or_title(self):
        interest = an_interest(key="k", title="Zebra Quantum Coding")
        per_interest = [{"interest": "k", "records": [
            {"title": "Zebra quantum coding release", "url": None},
            {"title": None, "url": "https://e.com/1"},
        ]}]
        self.assertEqual(exp_connectors.usable_records(per_interest, {"k": interest}, CFG), [])

    def test_usable_records_rejects_weak_match_and_grants_no_origin_floor(self):
        # If origin_interest were mistakenly set, matching.ORIGIN_MATCH_FLOOR
        # (0.5) would pass this record despite zero shared tokens -- proves
        # the metric isn't made vacuous.
        interest = an_interest(key="k", title="Alpha Beta Gamma", positive_signals=[])
        per_interest = [{"interest": "k", "records": [
            {"title": "Totally unrelated words nothing shared", "url": "https://e.com/3"},
        ]}]
        self.assertEqual(exp_connectors.usable_records(per_interest, {"k": interest}, CFG), [])

    def test_usable_records_accepts_strong_match(self):
        interest = an_interest(key="k", title="Zebra Quantum Coding")
        per_interest = [{"interest": "k", "records": [
            {"title": "Zebra quantum coding release", "url": "https://e.com/1"},
        ]}]
        usable = exp_connectors.usable_records(per_interest, {"k": interest}, CFG)
        self.assertEqual(len(usable), 1)
        self.assertGreaterEqual(usable[0]["match_score"], CFG.min_match_score)

    def test_usable_records_dedups_pooled_across_interests_by_url_and_title(self):
        interest = an_interest(key="k", title="Zebra Quantum Coding")
        per_interest = [
            {"interest": "k", "records": [
                {"title": "Zebra quantum coding launch", "url": "https://e.com/x?ref=1"}]},
            {"interest": "k", "records": [
                {"title": "ZEBRA QUANTUM CODING LAUNCH", "url": "https://e.com/x?ref=2"}]},
        ]
        usable = exp_connectors.usable_records(per_interest, {"k": interest}, CFG)
        self.assertEqual(len(usable), 1)

    def test_apply_h2_falsification_rule(self):
        self.assertEqual(exp_connectors.apply_h2_falsification_rule({"a": 3, "b": 7}), "H2_FALSIFIED")
        self.assertEqual(exp_connectors.apply_h2_falsification_rule({"a": 9, "b": 2}), "H2_SUPPORTED")

    def test_gate_promote_when_all_three_clear(self):
        metrics = {"a": {"usable_yield": 10, "marginal_unique_rate": 0.5},
                  "b": {"usable_yield": 3, "marginal_unique_rate": 0.9}}
        result = exp_connectors.apply_promotion_gate(metrics, True)
        self.assertEqual(result["result"], "PROMOTE")
        self.assertEqual(result["winner"], "a")
        self.assertIsNone(result["failing_gate"])

    def test_gate_no_promotion_g1_tie(self):
        metrics = {"a": {"usable_yield": 8, "marginal_unique_rate": 0.9},
                  "b": {"usable_yield": 8, "marginal_unique_rate": 0.9}}
        result = exp_connectors.apply_promotion_gate(metrics, True)
        self.assertEqual(result["result"], "NO_PROMOTION")
        self.assertEqual(result["failing_gate"], "G1")

    def test_gate_no_promotion_g1_below_eight(self):
        metrics = {"a": {"usable_yield": 7, "marginal_unique_rate": 0.9}}
        result = exp_connectors.apply_promotion_gate(metrics, True)
        self.assertEqual(result["failing_gate"], "G1")

    def test_gate_no_promotion_g2_not_clear_winner(self):
        metrics = {"a": {"usable_yield": 10, "marginal_unique_rate": 0.9},
                  "b": {"usable_yield": 6, "marginal_unique_rate": 0.1}}
        result = exp_connectors.apply_promotion_gate(metrics, True)
        self.assertEqual(result["failing_gate"], "G2")

    def test_gate_no_promotion_g3_void_baseline(self):
        metrics = {"a": {"usable_yield": 10, "marginal_unique_rate": 0.9}}
        result = exp_connectors.apply_promotion_gate(metrics, False)
        self.assertEqual(result["result"], "NO_PROMOTION")
        self.assertEqual(result["failing_gate"], "G3")

    def test_gate_no_promotion_g3_below_threshold(self):
        metrics = {"a": {"usable_yield": 10, "marginal_unique_rate": 0.1}}
        result = exp_connectors.apply_promotion_gate(metrics, True)
        self.assertEqual(result["failing_gate"], "G3")

    def test_x_deferred_entry_shape(self):
        entry = exp_connectors.x_deferred_entry()
        self.assertEqual(entry["status"], "DEFERRED_NEEDS_PROVIDER")
        self.assertTrue(entry["detail"])

    def test_uniqueness_among_candidates(self):
        by_connector = {"a": {"u1", "u2"}, "b": {"u2", "u3"}}
        self.assertAlmostEqual(exp_connectors.uniqueness_among_candidates("a", by_connector), 0.5)
        self.assertIsNone(exp_connectors.uniqueness_among_candidates("c", by_connector))

    def test_sample_reddit_pass2_is_retired_and_makes_no_network_call(self):
        # step-09's own re-check 403'd (a second independent 403 after
        # step-09a's 5-interest sweep), so reddit_url/parse_reddit were
        # deleted and this always returns the retired state with zero HTTP.
        interests_by_key = {f"i{n}": an_interest(key=f"i{n}", title=f"Topic Number {n} Words")
                            for n in range(3)}

        def boom(*a, **kw):
            raise AssertionError("reddit is retired -- must not call _http_get")

        with mock.patch("exp_connectors._http_get", side_effect=boom):
            per_interest, availability = exp_connectors.sample_reddit_pass2(
                list(interests_by_key), interests_by_key)
        self.assertEqual(per_interest, [])
        self.assertFalse(availability["reachable"])
        self.assertIn("RETIRED_UNREACHABLE", availability["detail"])

    def test_build_connector_pass2_entry_computes_both_arms(self):
        interest = an_interest(key="k", title="Zebra Quantum Coding")
        old_per_interest = [{"interest": "k", "records": [
            {"title": "Zebra old quantum coding record", "url": "https://e.com/old1"},
            {"title": "unrelated old record entirely", "url": "https://e.com/old2"},
        ]}]

        def two_hits(url, timeout=15, connector=None):
            hits = [
                {"title": "Zebra quantum coding new release", "url": "https://e.com/new1",
                 "created_at": "2026-08-01T00:00:00.000Z", "objectID": "1"},
                {"title": "unrelated new item entirely", "url": "https://e.com/new2",
                 "created_at": "2026-08-01T00:00:00.000Z", "objectID": "2"},
            ]
            return 200, json.dumps({"hits": hits}).encode(), {}

        with mock.patch("exp_connectors._http_get", side_effect=two_hits):
            entry, usable_urls = exp_connectors.build_connector_pass2_entry(
                "hackernews", ["k"], {"k": interest}, CFG, old_per_interest,
                set(), False, datetime.now(timezone.utc), 0)

        self.assertEqual(entry["arm_new_rule"]["usable_yield"], 1)
        self.assertEqual(entry["arm_old_rule_recomputed"]["usable_yield"], 1)
        self.assertIsNone(entry["arm_new_rule"]["marginal_unique_rate"])
        self.assertEqual(entry["arm_new_rule"]["marginal_unique_rate_status"], "VOID_NO_BASELINE")
        self.assertEqual(usable_urls, {"https://e.com/new1"})
        # repair: promised by PREREGISTRATION_PASS2 but previously never emitted.
        self.assertIsNone(entry["arm_new_rule"]["jaccard_overlap_with_web_search_sample"])
        self.assertEqual(entry["arm_new_rule"]["jaccard_overlap_status"], "VOID_NO_WEB_SEARCH_SAMPLE")

    def test_query_level_network_failures_only_flags_no_status_errors(self):
        # A read timeout (no http_status at all) is a network failure; a
        # reachable-but-blocked response (e.g. reddit's 403, which DOES carry
        # a status) is not -- it's already surfaced via disposition/
        # availability and must not double up in aborted_attempts.
        per_interest = [
            {"interest": "a", "query": "q1", "http_status": None,
             "error": "host: The read operation timed out", "collected_at": "t1"},
            {"interest": "b", "query": "q2", "http_status": 403,
             "error": "http 403", "collected_at": "t2"},
            {"interest": "c", "query": "q3", "http_status": 200, "error": None, "collected_at": "t3"},
        ]
        failures = exp_connectors.query_level_network_failures("arxiv", per_interest)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["interest"], "a")
        self.assertEqual(failures[0]["connector"], "arxiv")

    def test_degraded_arms_note(self):
        self.assertEqual(exp_connectors.degraded_arms_note([]), "")
        note = exp_connectors.degraded_arms_note([
            {"connector": "arxiv", "interest": "a", "query": "q", "reason": "timeout", "at": "t"},
            {"connector": "arxiv", "interest": "b", "query": "q", "reason": "timeout", "at": "t"},
        ])
        self.assertIn("arxiv", note)
        self.assertIn("2 query failure(s)", note)

    def test_run_pass2_e5b_records_query_level_network_failures(self):
        # repair: a query-level network failure (no http_status at all, e.g.
        # a read timeout) used to only land in per_interest['error'] --
        # never in aborted_attempts or verdict_detail.
        def fake_http_get(url, timeout=15, connector=None):
            host = urllib.parse.urlparse(url).netloc
            if host == "hn.algolia.com":
                return 200, json.dumps({"hits": []}).encode(), {}
            if host == "export.arxiv.org":
                raise exp_connectors.ConnectorUnreachable(
                    "export.arxiv.org: The read operation timed out")
            if host == "eutils.ncbi.nlm.nih.gov":
                return 200, json.dumps({"esearchresult": {"idlist": []}}).encode(), {}
            raise AssertionError(f"unexpected host {host}")

        with tempfile.TemporaryDirectory() as d:
            missing_db = os.path.join(d, "discovery.db")
            # Pin the interest keys this pass samples to a fixture, not the live
            # product interests.json: exp_connectors.APPLICABILITY maps each
            # connector to the interest keys the step-09 experiment was
            # pre-registered against, and the owner's 40-interest rewrite renamed
            # those keys. Reading the live file would leave arxiv with zero
            # applicable interests, so the query-level failure under test never
            # fires. The fixture keeps this test reproducing the pre-registered
            # sample regardless of how the product interest set evolves.
            interests_path = _write_recon_interests_fixture(d)
            cfg = dataclasses.replace(CFG, db_path=missing_db, interests_path=interests_path)
            empty_dossier = {"connectors": [{"name": n, "sample": {"per_interest": []}}
                                            for n in ("hackernews", "arxiv", "pubmed", "reddit")]}
            with mock.patch("exp_connectors._http_get", side_effect=fake_http_get), \
                 mock.patch("exp_connectors.time.sleep"):
                pass2 = exp_connectors.run_pass2_e5b(empty_dossier, cfg)

        self.assertTrue(pass2["aborted_attempts"])
        self.assertTrue(all(a["connector"] == "arxiv" for a in pass2["aborted_attempts"]))
        self.assertIn("Degraded arms", pass2["verdict_detail"])
        self.assertIn("arxiv", pass2["verdict_detail"])

    def test_run_pass2_e5b_end_to_end_offline_no_promotion_on_void_baseline(self):
        # Fully offline: patches _http_get for every host the real pass would
        # hit, reads the real tracked dossier (read-only, for old-rule
        # baseline records) and a real interests.json, but never touches the
        # network or writes the dossier file.
        hn_bodies = iter([
            json.dumps({"hits": [{"title": "New AI coding agents automation research",
                                  "url": "https://e.com/hn1", "created_at": "2026-08-01T00:00:00.000Z",
                                  "objectID": "1"}]}).encode(),
            json.dumps({"hits": [{"title": "Personal knowledge memory learning system launch",
                                  "url": "https://e.com/hn2", "created_at": "2026-08-01T00:00:00.000Z",
                                  "objectID": "2"}]}).encode(),
        ])

        def atom(url_, title_):
            return (
                '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                f"<entry><id>{url_}</id><title>{title_}</title>"
                "<published>2026-01-01T00:00:00Z</published></entry></feed>"
            ).encode()

        arxiv_bodies = iter([
            atom("https://arxiv.org/abs/1", "Human interaction attraction behavioral study"),
            atom("https://arxiv.org/abs/2", "Personal knowledge memory learning systems"),
            atom("https://arxiv.org/abs/3", "Agents coding automation for personal tools"),
        ])
        pubmed_bodies = iter([
            json.dumps({"esearchresult": {"idlist": ["101"]}}).encode(),
            json.dumps({"result": {"uids": ["101"], "101": {
                "title": "Narcolepsy wakefulness orexin trial", "pubdate": "2026 Jan"}}}).encode(),
            json.dumps({"esearchresult": {"idlist": ["201"]}}).encode(),
            json.dumps({"result": {"uids": ["201"], "201": {
                "title": "EMDR trauma processing mechanisms study", "pubdate": "2026 Feb"}}}).encode(),
        ])

        def fake_http_get(url, timeout=15, connector=None):
            # reddit is retired (sample_reddit_pass2 makes no HTTP call at
            # all) -- www.reddit.com deliberately has no branch here.
            host = urllib.parse.urlparse(url).netloc
            if host == "hn.algolia.com":
                return 200, next(hn_bodies), {}
            if host == "export.arxiv.org":
                return 200, next(arxiv_bodies), {}
            if host == "eutils.ncbi.nlm.nih.gov":
                return 200, next(pubmed_bodies), {}
            raise AssertionError(f"unexpected host {host}")

        with tempfile.TemporaryDirectory() as d:
            missing_db = os.path.join(d, "discovery.db")
            cfg = dataclasses.replace(CFG, db_path=missing_db, interests_path="interests.json")
            dossier = json.loads(exp_connectors.DOSSIER_PATH.read_text(encoding="utf-8"))
            with mock.patch("exp_connectors._http_get", side_effect=fake_http_get), \
                 mock.patch("exp_connectors.time.sleep"):
                pass2 = exp_connectors.run_pass2_e5b(dossier, cfg)

        self.assertEqual(pass2["x"]["status"], "DEFERRED_NEEDS_PROVIDER")
        self.assertEqual(pass2["aborted_attempts"], [])
        self.assertFalse(pass2["corpus"]["available"])
        self.assertEqual(pass2["dispositions"]["reddit"], "RETIRED_UNREACHABLE")
        for name in ("hackernews", "arxiv", "pubmed"):
            self.assertEqual(pass2["dispositions"][name], "NOT_PROMOTED_VOID_BASELINE")
        self.assertEqual(pass2["gate"]["result"], "NO_PROMOTION")
        self.assertIn(pass2["verdict"], ("H2_SUPPORTED", "H2_FALSIFIED"))


class FakeChatGPTConnection:
    """Stands in for cdp.CDPConnection for the chatgpt.com provider. The whole
    send is one evaluate() (session + sentinel + PoW + conversation SSE), so a
    completion reply is a JSON string/dict `{text, conversation_id}` (or an
    Exception to raise); the follow-up hide is a second evaluate. Dispatch is on
    the JS the provider actually builds, and every js string is kept so a test
    can assert the reverse-engineered contract stays in the payload."""

    def __init__(self, replies, hide_error=None, poll_results=None):
        self.replies = list(replies)
        # Handed-off (thinking-model) answers are read back by polling; each
        # entry is one GET result {text, done}. Left empty for the inline path.
        self.poll_results = list(poll_results or [])
        self.calls = []
        self.js = []
        self.closed = False
        self.hide_error = hide_error

    def evaluate(self, js, timeout=None, **_kw):
        self.js.append(js)
        if "is_visible" in js:
            self.calls.append("hide")
            if self.hide_error is not None:
                raise self.hide_error
            return True
        if "/* poll */" in js:
            self.calls.append("poll")
            if self.poll_results:
                nxt = self.poll_results.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt if isinstance(nxt, str) else json.dumps(nxt)
            return json.dumps({"text": "", "done": True})
        if "text/event-stream" in js:
            self.calls.append("completion")
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        self.calls.append("other")
        return True

    def close(self):
        self.closed = True


def chatgpt_reply(text, conversation_id="c1"):
    return json.dumps({"text": text, "conversation_id": conversation_id})


def chatgpt_handoff(conversation_id="c1"):
    """A thinking-model send: the POST returns a stream_handoff with a
    conversation id but no inline text, so the provider must poll for the
    answer (see FakeChatGPTConnection.poll_results)."""
    return json.dumps({"text": "", "conversation_id": conversation_id, "handoff": True})


class ChatGPTBrowserProviderTests(unittest.TestCase):
    """The chatgpt.com-over-CDP provider, with the browser faked out entirely.
    The novel core it shares with no other provider -- the sentinel proof-of-
    work handshake and the delta-encoded SSE accumulation -- runs as JS inside
    the page, so it's verified separately at execution level; here the seam is
    the same as claude_chat's: the Python orchestration around evaluate()."""

    SCHEMA = {
        "type": "object",
        "properties": {
            "a": {"type": "number"},
            "kind": {"type": "string", "enum": ["x", "y"]},
        },
        "required": ["a"],
        "additionalProperties": False,
    }

    def _provider(self, *replies, connections=None, hide_error=None,
                  poll_results=None, model="auto"):
        conns = connections if connections is not None else [
            FakeChatGPTConnection(replies, hide_error=hide_error, poll_results=poll_results)
        ]
        remaining = list(conns)
        self.connections = conns
        return chatgpt_browser.ChatGPTBrowserProvider(
            model, port=9222, connect=lambda: remaining.pop(0),
        )

    def test_registered_and_constructable_without_touching_chrome(self):
        self.assertIn("chatgpt_browser", PROVIDERS)
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        self.assertEqual(provider.name, "chatgpt_browser")  # lazy: no connection yet

    def test_complete_json_parses_a_clean_reply_and_hides_the_conversation(self):
        provider = self._provider(chatgpt_reply('{"a": 0.5, "kind": "x"}'))
        data = provider.complete_json("sys", "prompt", self.SCHEMA)
        self.assertEqual(data, {"a": 0.5, "kind": "x"})
        (conn,) = self.connections
        self.assertEqual(conn.calls, ["completion", "hide"])
        self.assertEqual(provider.usage["calls"], 1)
        self.assertEqual(provider.usage["input_tokens"], 0)  # not reported, never guessed

    def test_complete_json_survives_prose_and_fences_around_the_object(self):
        provider = self._provider(
            chatgpt_reply('Sure! Here it is:\n```json\n{"a": 1.0}\n```\nHope that helps.')
        )
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 1.0})

    def test_a_malformed_reply_is_retried_once_then_succeeds(self):
        provider = self._provider(
            chatgpt_reply("I cannot answer in JSON, sorry."),
            chatgpt_reply('{"a": 0.25}'),
        )
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 0.25})
        (conn,) = self.connections
        self.assertEqual(conn.calls.count("completion"), 2)

    def test_two_malformed_replies_fail_gracefully(self):
        provider = self._provider(
            chatgpt_reply('{"wrong": true}'),        # missing required "a"
            chatgpt_reply('{"a": "not a number"}'),
        )
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("attempt 2", str(ctx.exception))

    def test_an_enum_violation_counts_as_malformed(self):
        provider = self._provider(
            chatgpt_reply('{"a": 1, "kind": "zebra"}'),
            chatgpt_reply('{"a": 1, "kind": "zebra"}'),
        )
        with self.assertRaises(ProviderError):
            provider.complete_json("s", "p", self.SCHEMA)

    def test_search_json_returns_the_embedded_array_and_garbage_becomes_empty(self):
        provider = self._provider(
            chatgpt_reply('I searched.\n[{"title": "T", "url": "https://e.com"}]\nDone.'),
            chatgpt_reply("no array here at all"),
        )
        self.assertEqual(
            provider.search_json("find things"),
            [{"title": "T", "url": "https://e.com"}],
        )
        self.assertEqual(provider.search_json("find things"), [])

    def test_search_json_asks_for_the_search_tool_but_complete_json_does_not(self):
        provider = self._provider(
            chatgpt_reply("[]"), chatgpt_reply('{"a": 1}'),
        )
        provider.search_json("find things")
        provider.complete_json("s", "p", self.SCHEMA)
        (conn,) = self.connections
        search_js, complete_js = conn.js[0], conn.js[2]  # [0]=search, [1]=its hide, [2]=complete
        self.assertIn('system_hints: ["search"]', search_js)
        self.assertIn("system_hints: []", complete_js)

    def test_the_completion_js_carries_the_sentinel_and_pow_contract(self):
        provider = self._provider(chatgpt_reply('{"a": 1}'))
        provider.complete_json("s", "p", self.SCHEMA)
        js = self.connections[0].js[0]
        for token in ("sha3_512_hex", "/sentinel/chat-requirements",
                      "OpenAI-Sentinel-Chat-Requirements-Token",
                      "OpenAI-Sentinel-Proof-Token",
                      "OpenAI-Sentinel-Turnstile-Token",  # forwarded, not thrown on
                      "/api/auth/session"):
            self.assertIn(token, js, f"completion JS lost {token!r}")
        # turnstile.required must NOT abort the send -- echoing dx works live
        self.assertNotIn("throw new Error('chatgpt.com demanded", js)

    def test_the_send_js_resolves_latest_high_and_carries_the_reasoning_contract(self):
        provider = self._provider(chatgpt_reply('{"a": 1}'))
        provider.complete_json("s", "p", self.SCHEMA)
        js = self.connections[0].js[0]
        for token in ("/backend-api/models",          # resolves the newest model live
                      "intelligence_presets",          # ...from the version's presets
                      "'thinking'",                    # ...picking the thinking lane
                      "body.thinking_effort = effort", # ...and sending its (High) effort
                      "stream_handoff"):               # ...and noticing the handoff
            self.assertIn(token, js, f"send JS lost {token!r}")

    def test_a_handoff_answer_is_polled_until_finished(self):
        # Thinking model: the send returns only a handoff, then two polls -- the
        # first still streaming, the second finished with the JSON answer.
        provider = self._provider(
            chatgpt_handoff("c9"),
            poll_results=[{"text": "", "done": False}, {"text": '{"a": 5}', "done": True}],
        )
        with mock.patch.object(chatgpt_browser.time, "sleep"):
            self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 5})
        (conn,) = self.connections
        self.assertEqual(conn.calls, ["completion", "poll", "poll", "hide"])

    def test_search_json_reads_a_handed_off_array_back_by_polling(self):
        provider = self._provider(
            chatgpt_handoff("c1"),
            poll_results=[{"text": '[{"title": "T", "url": "https://e.com"}]', "done": True}],
        )
        with mock.patch.object(chatgpt_browser.time, "sleep"):
            self.assertEqual(
                provider.search_json("find things"),
                [{"title": "T", "url": "https://e.com"}],
            )

    def test_a_handoff_that_never_finishes_times_out_to_a_provider_error(self):
        # Poll keeps returning unfinished; the deadline passes and it surfaces as
        # a normal empty-completion ProviderError rather than looping forever.
        provider = self._provider(
            chatgpt_handoff("c1"),
            poll_results=[{"text": "", "done": False}] * 50,
        )
        clock = iter([0.0, 1.0, 2.0, 999.0])  # monotonic: enters loop once, then past deadline
        with mock.patch.object(chatgpt_browser.time, "sleep"), \
                mock.patch.object(chatgpt_browser.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("empty completion", str(ctx.exception))

    def test_an_explicit_slug_effort_spec_is_passed_through(self):
        provider = self._provider(chatgpt_reply('{"a": 1}'), model="gpt-5-6-thinking:extended")
        provider.complete_json("s", "p", self.SCHEMA)
        js = self.connections[0].js[0]
        self.assertIn('"gpt-5-6-thinking:extended"', js)  # the pin reaches the page verbatim

    def test_no_chrome_endpoint_is_a_clean_provider_error(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(
            chatgpt_browser.cdp, "find_chatgpt_tab", side_effect=ConnectionRefusedError("refused")
        ):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("Chrome DevTools endpoint", str(ctx.exception))

    def test_no_chatgpt_tab_is_a_clean_provider_error(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(chatgpt_browser.cdp, "find_chatgpt_tab", return_value=None):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("chatgpt.com tab", str(ctx.exception))

    def test_a_js_exception_in_the_tab_is_a_provider_error_not_a_crash(self):
        provider = self._provider(RuntimeError("JS exception: conversation HTTP 429"))
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("HTTP 429", str(ctx.exception))

    def test_a_dropped_connection_reconnects_once_and_recovers(self):
        dead = FakeChatGPTConnection([ConnectionError("websocket closed")])
        alive = FakeChatGPTConnection([chatgpt_reply('{"a": 0.75}')])
        provider = self._provider(connections=[dead, alive])
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 0.75})
        self.assertTrue(dead.closed)     # reset closed the dead connection
        self.assertFalse(alive.closed)

    def test_a_connection_that_keeps_dropping_fails_gracefully(self):
        dead1 = FakeChatGPTConnection([ConnectionError("closed")])
        dead2 = FakeChatGPTConnection([ConnectionError("closed again")])
        provider = self._provider(connections=[dead1, dead2])
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("twice", str(ctx.exception))

    def test_a_null_completion_result_blames_the_tab(self):
        provider = self._provider(None)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json("s", "p", self.SCHEMA)
        self.assertIn("no text", str(ctx.exception))

    def test_a_dict_reply_from_cdp_is_accepted_as_is(self):
        # Some Chrome/CDP combinations hand back the JS return value already
        # deserialized; no conversation_id here, so no hide fires.
        provider = self._provider({"text": '{"a": 2}', "conversation_id": None})
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 2})
        self.assertEqual(self.connections[0].calls, ["completion"])

    def test_a_hide_failure_never_breaks_the_reply(self):
        provider = self._provider(
            chatgpt_reply('{"a": 3}'), hide_error=RuntimeError("hide blew up")
        )
        self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 3})
        self.assertIn("hide", self.connections[0].calls)

    def test_preflight_fails_cleanly_with_no_chrome_endpoint(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(
            chatgpt_browser.cdp, "find_chatgpt_tab", side_effect=ConnectionRefusedError("refused")
        ):
            ok, detail = provider.preflight()
        self.assertFalse(ok)
        self.assertIn("Chrome DevTools endpoint", detail)

    def test_preflight_fails_cleanly_with_no_chatgpt_tab(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(chatgpt_browser.cdp, "find_chatgpt_tab", return_value=None):
            ok, detail = provider.preflight()
        self.assertFalse(ok)
        self.assertIn("chatgpt.com tab", detail)

    def test_preflight_succeeds_when_the_tab_is_open(self):
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222)
        with mock.patch.object(
            chatgpt_browser.cdp, "find_chatgpt_tab", return_value={"id": "1"}
        ):
            self.assertEqual(provider.preflight(), (True, ""))


# --- step-12 task 2: continuous Council-driven web discovery ------------------

def mission_batch(*labels):
    """Council response shape: {"missions": [{label, rationale, prompt}, ...]}.
    Each mission's prompt embeds its own label so FakeCouncilProvider's
    search_results dict can key a canned search result off it."""
    return {"missions": [
        {"label": label, "rationale": f"why {label}", "prompt": f"research {label} thoroughly"}
        for label in labels
    ]}


def search_hits(*urls, title_prefix="Result"):
    return [
        {"title": f"{title_prefix} {n}", "url": url, "summary": "Enough body text. " * 10}
        for n, url in enumerate(urls, 1)
    ]


class FakeCouncilProvider(LLMProvider):
    """Fake search-capable provider standing in for cfg.mission_provider.
    complete_json serves queued Council mission batches, one consumed per
    call (queue exhaustion raises -- proves at most one generation call
    happened); search_json serves a canned result keyed by a needle in the
    executor prompt (which always embeds the mission's own prompt text)."""

    name = "fake_mission"

    def __init__(self, mission_batches=None, search_results=None, preflight_ok=True,
                 model="fake-mission-1"):
        super().__init__(model)
        self.mission_batches = list(mission_batches or [])
        self.search_results = dict(search_results or {})
        self._preflight_ok = preflight_ok
        self.complete_prompts = []
        self.search_prompts = []

    def complete_json(self, system, prompt, schema, max_tokens=8000):
        self.complete_prompts.append(prompt)
        if not self.mission_batches:
            raise AssertionError("FakeCouncilProvider ran out of queued mission batches")
        batch = self.mission_batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        self.search_prompts.append(prompt)
        for needle, value in self.search_results.items():
            if needle in prompt:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"FakeCouncilProvider got an unexpected search prompt:\n{prompt}")

    def preflight(self):
        return (self._preflight_ok, "" if self._preflight_ok else "mission provider down")


class CouncilTests(unittest.TestCase):
    """discovery/council.py: mission planning + validation + the Goodhart
    firewall, independent of the tick that drives it."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.interest = an_interest(
            key="narcolepsy", title="Narcolepsy research",
            description="Orexin agonists and MWT trial results.",
            positive_signals=["orexin agonist", "MWT"], negative_signals=["sleep hygiene listicle"],
            min_score=0.78,
        )
        db.upsert_interest(self.conn, self.interest)
        self.interest = db.interest_by_key(self.conn, "narcolepsy")

    def _context(self):
        return council.build_context(self.conn, self.interest, CFG)

    def test_plan_missions_returns_validated_missions(self):
        provider = FakeCouncilProvider(mission_batches=[mission_batch("trial-registries", "patent-filings")])
        missions_out, deliberation = council.plan_missions(provider, self.interest, self._context(), 2)
        self.assertEqual([m["label"] for m in missions_out], ["trial-registries", "patent-filings"])
        for m in missions_out:
            self.assertTrue(m["rationale"])
            self.assertTrue(m["prompt"])
        self.assertEqual(len(provider.complete_prompts), 1)   # exactly one call
        # No "deliberation" object in mission_batch()'s fixture response --
        # lenient, every section marked unavailable, never fatal.
        for name in council.DELIBERATION_SECTIONS:
            self.assertTrue(deliberation[name].get("unavailable"))

    def test_plan_missions_truncates_extras_past_count_without_failing(self):
        provider = FakeCouncilProvider(mission_batches=[mission_batch("a", "b", "c")])
        missions_out, _deliberation = council.plan_missions(provider, self.interest, self._context(), 2)
        self.assertEqual([m["label"] for m in missions_out], ["a", "b"])

    def test_plan_missions_raises_on_missing_missions_key(self):
        provider = FakeCouncilProvider(mission_batches=[{"nope": []}])
        with self.assertRaises(council.CouncilError):
            council.plan_missions(provider, self.interest, self._context(), 2)

    def test_plan_missions_raises_on_non_dict_response(self):
        provider = FakeCouncilProvider(mission_batches=[["not", "a", "dict"]])
        with self.assertRaises(council.CouncilError):
            council.plan_missions(provider, self.interest, self._context(), 2)

    def test_plan_missions_raises_on_empty_prompt(self):
        provider = FakeCouncilProvider(mission_batches=[
            {"missions": [{"label": "x", "rationale": "r", "prompt": "   "}]}
        ])
        with self.assertRaises(council.CouncilError):
            council.plan_missions(provider, self.interest, self._context(), 1)

    def test_plan_missions_raises_on_missing_field(self):
        provider = FakeCouncilProvider(mission_batches=[
            {"missions": [{"label": "x", "prompt": "do the thing"}]}
        ])
        with self.assertRaises(council.CouncilError):
            council.plan_missions(provider, self.interest, self._context(), 1)

    def test_plan_missions_raises_on_duplicate_labels_case_insensitive(self):
        provider = FakeCouncilProvider(mission_batches=[mission_batch("Angle-One", "angle-one")])
        with self.assertRaises(council.CouncilError):
            council.plan_missions(provider, self.interest, self._context(), 2)

    def test_plan_missions_raises_on_zero_missions(self):
        provider = FakeCouncilProvider(mission_batches=[{"missions": []}])
        with self.assertRaises(council.CouncilError):
            council.plan_missions(provider, self.interest, self._context(), 2)

    def test_build_context_is_bounded_by_cfg(self):
        for n in range(5):
            item = stored_item(
                self.conn, source="web_search", title=f"Frontier item {n}",
                url=f"https://e.com/f{n}", origin_interest="narcolepsy",
            )
        cfg = dataclasses.replace(CFG, council_frontier_items=2)
        ctx = council.build_context(self.conn, self.interest, cfg)
        self.assertEqual(len(ctx["frontier"]), 2)

    def test_build_context_includes_feedback_and_history(self):
        item = stored_item(self.conn, title="Some item", url="https://e.com/x")
        db.add_feedback(self.conn, item.id, self.interest.id, "up", note="great find")
        gen_id = db.insert_generation(self.conn, "narcolepsy", "fake_mission", "m1", 1)
        db.insert_missions(self.conn, gen_id, "narcolepsy", [
            {"label": "past-angle", "rationale": "worked before", "prompt": "do it"}
        ])
        ctx = council.build_context(self.conn, self.interest, CFG)
        self.assertEqual(ctx["feedback"][0]["verdict"], "up")
        self.assertEqual(ctx["history"][0]["label"], "past-angle")

    def test_goodhart_firewall_prompt_has_no_scoring_machinery(self):
        """The rendered planning prompt (system + user) must never leak
        downstream scoring machinery to the Council."""
        ctx = self._context()
        system = council.COUNCIL_INSTRUCTIONS.format(count=4)
        prompt = council.render_prompt(ctx, 4)
        rendered = system + "\n" + prompt

        self.assertNotIn(str(self.interest.min_score), rendered)
        self.assertNotIn("min_score", rendered)
        self.assertNotIn("derived_min_score", rendered)
        self.assertNotIn("final_score", rendered)
        self.assertNotIn("confidence", rendered)
        self.assertNotIn("notification", rendered.lower())
        for weight_name in models.WEIGHTS:
            self.assertNotIn(weight_name, rendered)
        for dim in models.DIMENSIONS:
            self.assertNotIn(dim, rendered)


class MissionDbTests(unittest.TestCase):
    """discovery/db.py's search_generations/search_missions helpers, in
    isolation from council.py and missions.py."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def _seed_mission(self, interest_key="k", label="m1"):
        gen_id = db.insert_generation(self.conn, interest_key, "fake_mission", "m1", 1)
        db.insert_missions(self.conn, gen_id, interest_key, [
            {"label": label, "rationale": "r", "prompt": f"do {label}"}
        ])
        row = self.conn.execute(
            "SELECT id FROM search_missions WHERE interest_key = ? AND label = ?",
            (interest_key, label),
        ).fetchone()
        return gen_id, row["id"]

    def test_lease_missions_claims_only_pending_and_is_atomic(self):
        _, mid = self._seed_mission()
        leased = db.lease_missions(self.conn, [mid], 900)
        self.assertEqual(leased, [mid])
        row = db.mission_by_id(self.conn, mid)
        self.assertEqual(row["status"], "RUNNING")
        self.assertEqual(row["attempts"], 1)
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["lease_expires_at"])

        # Already RUNNING -- a second overlapping lease attempt claims nothing.
        self.assertEqual(db.lease_missions(self.conn, [mid], 900), [])

    def test_lease_missions_returns_only_the_actually_claimed_subset(self):
        _, mid1 = self._seed_mission(label="m1")
        _, mid2 = self._seed_mission(label="m2")
        db.lease_missions(self.conn, [mid1], 900)   # mid1 already RUNNING
        leased = db.lease_missions(self.conn, [mid1, mid2], 900)
        self.assertEqual(leased, [mid2])

    def test_recover_stale_missions_reclaims_expired_lease_to_pending(self):
        _, mid = self._seed_mission()
        db.lease_missions(self.conn, [mid], -1)   # already-expired lease
        db.recover_stale_missions(self.conn, max_attempts=3)
        row = db.mission_by_id(self.conn, mid)
        self.assertEqual(row["status"], "PENDING")
        self.assertIsNone(row["leased_at"])
        self.assertEqual(row["attempts"], 1)   # preserved, not reset

        # No longer RUNNING, so it can be leased again -- no duplicate concurrent execution.
        self.assertEqual(db.lease_missions(self.conn, [mid], 900), [mid])

    def test_recover_stale_missions_retires_exhausted_attempts_to_failed(self):
        _, mid = self._seed_mission()
        db.lease_missions(self.conn, [mid], -1)
        db.recover_stale_missions(self.conn, max_attempts=1)   # attempts already at 1
        row = db.mission_by_id(self.conn, mid)
        self.assertEqual(row["status"], "FAILED")

    def test_fail_mission_retries_then_retires_at_max_attempts(self):
        _, mid = self._seed_mission()
        db.lease_missions(self.conn, [mid], 900)   # attempts -> 1
        db.fail_mission(self.conn, mid, "boom", max_attempts=2, retry_seconds=1800)
        row = db.mission_by_id(self.conn, mid)
        self.assertEqual(row["status"], "PENDING")
        self.assertIsNotNone(row["next_attempt_at"])

        db.lease_missions(self.conn, [mid], 900)   # attempts -> 2
        db.fail_mission(self.conn, mid, "boom again", max_attempts=2, retry_seconds=1800)
        row = db.mission_by_id(self.conn, mid)
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["last_error"], "boom again")

    def test_pending_mission_count_recent_missions_mission_by_id(self):
        self._seed_mission(label="m1")
        self._seed_mission(label="m2")
        self.assertEqual(db.pending_mission_count(self.conn, "k"), 2)
        history = db.recent_missions(self.conn, "k", 10)
        self.assertEqual([r["label"] for r in history], ["m2", "m1"])   # newest first


class WebTickTests(unittest.TestCase):
    """discovery/missions.py's web_tick(): the whole continuous
    Council-driven web discovery tick, offline (fake mission provider +
    fake scoring provider, no Chrome/CDP/network)."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.cfg = dataclasses.replace(
            CFG,
            mission_provider="fake_mission",
            mission_low_water=1,
            missions_per_tick=2,
            council_missions_per_generation=2,
            mission_max_attempts=2,
            mission_retry_seconds=1800,
            council_max_consecutive_failures=2,
        )

    def _interest(self, key):
        db.upsert_interest(self.conn, an_interest(key=key, title=key, sources=["web_search"]))
        return db.interest_by_key(self.conn, key)

    def _tick(self, mission_provider, scoring_provider=None, cfg=None, dry_run=False):
        scoring_provider = scoring_provider or FakeProvider({"Result": 0.9})
        with mock.patch.object(providers, "get_provider", return_value=mission_provider):
            return missions.web_tick(self.conn, cfg or self.cfg, provider=scoring_provider, dry_run=dry_run)

    def _seed_mission(self, interest_key, label, generation_id=None):
        db.insert_missions(self.conn, generation_id, interest_key, [
            {"label": label, "rationale": "seeded directly", "prompt": f"research {label}"}
        ])
        return self.conn.execute(
            "SELECT id FROM search_missions WHERE interest_key = ? AND label = ?",
            (interest_key, label),
        ).fetchone()["id"]

    def _generation_statuses(self, interest_key):
        return [
            r["status"] for r in self.conn.execute(
                "SELECT status FROM search_generations WHERE interest_key = ? ORDER BY id",
                (interest_key,),
            ).fetchall()
        ]

    # --- replenish: at most one interest per tick, only below low water -------

    def test_empty_startup_refill_plans_one_interest_per_tick_never_bursts(self):
        # missions_per_tick=0 -- this test is only about *how many interests
        # get a Council call per tick*, not mission execution.
        cfg = dataclasses.replace(self.cfg, missions_per_tick=0)
        self._interest("alpha")
        self._interest("bravo")
        self._interest("charlie")

        mp1 = FakeCouncilProvider(mission_batches=[mission_batch("a1", "a2")])
        self._tick(mp1, cfg=cfg)
        self.assertEqual(len(mp1.complete_prompts), 1)
        self.assertEqual(self._generation_statuses("alpha"), ["DONE"])
        self.assertEqual(self._generation_statuses("bravo"), [])
        self.assertEqual(self._generation_statuses("charlie"), [])

        mp2 = FakeCouncilProvider(mission_batches=[mission_batch("b1", "b2")])
        self._tick(mp2, cfg=cfg)
        self.assertEqual(len(mp2.complete_prompts), 1)
        self.assertEqual(self._generation_statuses("bravo"), ["DONE"])
        self.assertEqual(self._generation_statuses("charlie"), [])

        mp3 = FakeCouncilProvider(mission_batches=[mission_batch("c1", "c2")])
        self._tick(mp3, cfg=cfg)
        self.assertEqual(len(mp3.complete_prompts), 1)
        self.assertEqual(self._generation_statuses("charlie"), ["DONE"])

    def test_replenish_only_triggers_below_low_water_mark(self):
        cfg = dataclasses.replace(self.cfg, mission_low_water=2, missions_per_tick=0)
        self._interest("alpha")
        gen_id = db.insert_generation(self.conn, "alpha", "fake_mission", "m1", 1)
        self._seed_mission("alpha", "already-queued", generation_id=gen_id)
        db.finish_generation(self.conn, gen_id, "DONE", 1)
        # 1 PENDING mission, low_water=2 -- still under water, so a fresh
        # generation is still due.
        mp = FakeCouncilProvider(mission_batches=[mission_batch("x1")])
        self._tick(mp, cfg=cfg)
        self.assertEqual(len(mp.complete_prompts), 1)

        # Now at/above the low-water mark -- no further Council call.
        cfg2 = dataclasses.replace(cfg, mission_low_water=2)
        mp2 = FakeCouncilProvider(mission_batches=[])
        result = self._tick(mp2, cfg=cfg2)
        self.assertEqual(len(mp2.complete_prompts), 0)
        self.assertTrue(result["preflight_ok"])

    # --- fair selection ----------------------------------------------------

    def test_round_robin_fairness_across_interests_with_lopsided_queue(self):
        cfg = dataclasses.replace(self.cfg, missions_per_tick=2)
        self._interest("alpha")
        self._interest("bravo")
        for n in range(5):
            self._seed_mission("alpha", f"alpha-{n}")
        self._seed_mission("bravo", "bravo-0")

        mp = FakeCouncilProvider(search_results={
            "alpha-0": search_hits("https://e.com/a0"),
            "bravo-0": search_hits("https://e.com/b0"),
        })
        self._tick(mp, cfg=cfg)

        # missions_per_tick=2: one from each interest, not two from alpha --
        # bravo's single mission is never starved by alpha's queue of 5.
        alpha_done = self.conn.execute(
            "SELECT COUNT(*) c FROM search_missions WHERE interest_key='alpha' AND status='DONE'"
        ).fetchone()["c"]
        bravo_done = self.conn.execute(
            "SELECT COUNT(*) c FROM search_missions WHERE interest_key='bravo' AND status='DONE'"
        ).fetchone()["c"]
        self.assertEqual((alpha_done, bravo_done), (1, 1))

    # --- crash / recovery ----------------------------------------------------

    def test_crash_recovery_mission_resumes_and_executes_exactly_once(self):
        cfg = dataclasses.replace(self.cfg, missions_per_tick=1, mission_lease_seconds=-1)
        self._interest("alpha")
        self._seed_mission("alpha", "m1")

        mp = FakeCouncilProvider(search_results={"m1": search_hits("https://e.com/m1")})
        with mock.patch.object(providers, "get_provider", return_value=mp), \
             mock.patch.object(missions, "_execute_mission", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                missions.web_tick(self.conn, cfg, provider=FakeProvider({"Result": 0.9}))

        row = self.conn.execute("SELECT status FROM search_missions WHERE label='m1'").fetchone()
        self.assertEqual(row["status"], "RUNNING")   # leased before the simulated crash
        self.assertEqual(len(mp.search_prompts), 0)   # never actually executed

        # Fresh tick, same conn: the expired lease is reclaimed and this
        # time the mission actually runs -- exactly once.
        self._tick(mp, cfg=dataclasses.replace(cfg, mission_lease_seconds=900))
        row = self.conn.execute("SELECT status, items_returned FROM search_missions WHERE label='m1'").fetchone()
        self.assertEqual(row["status"], "DONE")
        self.assertEqual(row["items_returned"], 1)
        self.assertEqual(len(mp.search_prompts), 1)   # executed exactly once total

    def test_stale_lease_recovery_reclaims_running_mission_no_duplicate_execution(self):
        self._interest("alpha")
        mid = self._seed_mission("alpha", "m1")
        leased = db.lease_missions(self.conn, [mid], -1)   # already expired the moment it's leased
        self.assertEqual(leased, [mid])

        db.recover_stale_missions(self.conn, self.cfg.mission_max_attempts)
        row = self.conn.execute("SELECT status FROM search_missions WHERE id=?", (mid,)).fetchone()
        self.assertEqual(row["status"], "PENDING")

        # Reclaimed -- can be leased again; a second concurrent lease attempt
        # on the still-RUNNING copy (before recovery) would have claimed nothing.
        self.assertEqual(db.lease_missions(self.conn, [mid], 900), [mid])
        self.assertEqual(db.lease_missions(self.conn, [mid], 900), [])   # no duplicate claim

    # --- failure isolation ---------------------------------------------------

    def test_planner_failure_on_one_interest_leaves_others_executing(self):
        cfg = dataclasses.replace(self.cfg, missions_per_tick=1, mission_low_water=1)
        self._interest("alpha")   # will fail to plan
        self._interest("bravo")   # already has pending work
        self._seed_mission("bravo", "bravo-0")

        mp = FakeCouncilProvider(
            mission_batches=[providers.ProviderError("planner exploded")],
            search_results={"bravo-0": search_hits("https://e.com/b0")},
        )
        self._tick(mp, cfg=cfg)

        self.assertEqual(self._generation_statuses("alpha"), ["FAILED"])
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM search_missions WHERE interest_key='alpha'"
            ).fetchall(),
            [],
        )
        bravo_row = self.conn.execute(
            "SELECT status FROM search_missions WHERE interest_key='bravo'"
        ).fetchone()
        self.assertEqual(bravo_row["status"], "DONE")

    def test_planner_failure_with_a_non_provider_exception_leaves_others_executing(self):
        """A live provider's own response parsing can raise something other
        than CouncilError/ProviderError (e.g. TypeError/JSONDecodeError on a
        malformed non-dict reply) -- that must be isolated exactly like a
        ProviderError, not propagate out of web_tick() and abort every
        other interest's execution for the tick."""
        cfg = dataclasses.replace(self.cfg, missions_per_tick=1, mission_low_water=1)
        self._interest("alpha")   # will fail to plan, with a bare exception
        self._interest("bravo")   # already has pending work
        self._seed_mission("bravo", "bravo-0")

        mp = FakeCouncilProvider(
            mission_batches=[TypeError("unexpected non-dict CDP reply")],
            search_results={"bravo-0": search_hits("https://e.com/b0")},
        )
        self._tick(mp, cfg=cfg)

        self.assertEqual(self._generation_statuses("alpha"), ["FAILED"])
        bravo_row = self.conn.execute(
            "SELECT status FROM search_missions WHERE interest_key='bravo'"
        ).fetchone()
        self.assertEqual(bravo_row["status"], "DONE")

    def test_build_context_failure_still_finalizes_the_generation_and_others_execute(self):
        """A failure in council.build_context() itself (before plan_missions
        is even called) must not leave an orphan PENDING search_generations
        row, and must not abort other interests' execution either."""
        cfg = dataclasses.replace(self.cfg, missions_per_tick=1, mission_low_water=1)
        self._interest("alpha")
        self._interest("bravo")
        self._seed_mission("bravo", "bravo-0")

        mp = FakeCouncilProvider(search_results={"bravo-0": search_hits("https://e.com/b0")})
        with mock.patch.object(council, "build_context", side_effect=RuntimeError("db blew up")):
            self._tick(mp, cfg=cfg)

        self.assertEqual(self._generation_statuses("alpha"), ["FAILED"])   # never orphaned at PENDING
        bravo_row = self.conn.execute(
            "SELECT status FROM search_missions WHERE interest_key='bravo'"
        ).fetchone()
        self.assertEqual(bravo_row["status"], "DONE")

    def test_replenish_backs_off_after_a_recent_council_failure(self):
        """A Council that keeps failing must not burn one real provider call
        every single tick -- only cfg.mission_retry_seconds after the most
        recent failure is a retry due."""
        cfg = dataclasses.replace(self.cfg, mission_low_water=1, missions_per_tick=0,
                                   mission_retry_seconds=1800)
        self._interest("alpha")

        mp1 = FakeCouncilProvider(mission_batches=[providers.ProviderError("down")])
        self._tick(mp1, cfg=cfg)
        self.assertEqual(len(mp1.complete_prompts), 1)
        self.assertEqual(self._generation_statuses("alpha"), ["FAILED"])

        # Still within the cool-off -- no second Council call this tick.
        mp2 = FakeCouncilProvider(mission_batches=[mission_batch("should-not-be-requested")])
        self._tick(mp2, cfg=cfg)
        self.assertEqual(len(mp2.complete_prompts), 0)
        self.assertEqual(self._generation_statuses("alpha"), ["FAILED"])

        # Cool-off elapsed -- back-date the failed generation and retry succeeds.
        self.conn.execute(
            "UPDATE search_generations SET created_at = ? WHERE interest_key = 'alpha'",
            (db.ago(cfg.mission_retry_seconds + 60),),
        )
        mp3 = FakeCouncilProvider(mission_batches=[mission_batch("recovered")])
        self._tick(mp3, cfg=cfg)
        self.assertEqual(len(mp3.complete_prompts), 1)
        self.assertEqual(self._generation_statuses("alpha"), ["FAILED", "DONE"])

    def test_malformed_council_output_records_failed_generation_and_enqueues_nothing(self):
        self._interest("alpha")
        mp = FakeCouncilProvider(mission_batches=[{"missions": "not-a-list"}])
        self._tick(mp)
        self.assertEqual(self._generation_statuses("alpha"), ["FAILED"])
        self.assertEqual(db.pending_mission_count(self.conn, "alpha"), 0)

    def test_executor_failure_on_one_mission_does_not_abort_others(self):
        cfg = dataclasses.replace(self.cfg, missions_per_tick=2)
        self._interest("alpha")
        self._seed_mission("alpha", "bad-mission")
        self._seed_mission("alpha", "good-mission")

        mp = FakeCouncilProvider(search_results={
            "bad-mission": RuntimeError("search blew up"),
            "good-mission": search_hits("https://e.com/good"),
        })
        self._tick(mp, cfg=cfg)

        bad = self.conn.execute("SELECT status FROM search_missions WHERE label='bad-mission'").fetchone()
        good = self.conn.execute("SELECT status FROM search_missions WHERE label='good-mission'").fetchone()
        self.assertIn(bad["status"], ("PENDING", "FAILED"))   # never DONE
        self.assertEqual(good["status"], "DONE")

    def test_duplicate_discoveries_across_missions_dedup(self):
        cfg = dataclasses.replace(self.cfg, missions_per_tick=2)
        self._interest("alpha")
        self._seed_mission("alpha", "m1")
        self._seed_mission("alpha", "m2")

        same_hit = search_hits("https://e.com/same")
        mp = FakeCouncilProvider(search_results={"m1": same_hit, "m2": same_hit})
        self._tick(mp, cfg=cfg)

        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM candidate_items WHERE url = ?", ("https://e.com/same",)
        ).fetchone()
        self.assertEqual(rows["c"], 1)   # one stored item
        metrics = dict(self.conn.execute("SELECT name, count FROM metrics").fetchall())
        self.assertEqual(metrics.get("duplicate"), 1)

    # --- provenance ------------------------------------------------------------

    def test_provenance_resolves_generation_mission_label_prompt(self):
        interest = self._interest("alpha")
        mp = FakeCouncilProvider(
            mission_batches=[mission_batch("provenance-check")],
            search_results={"provenance-check": search_hits("https://e.com/prov")},
        )
        self._tick(mp, cfg=dataclasses.replace(self.cfg, council_missions_per_generation=1, missions_per_tick=1))

        item_row = self.conn.execute(
            "SELECT metadata FROM candidate_items WHERE url = ?", ("https://e.com/prov",)
        ).fetchone()
        metadata = json.loads(item_row["metadata"])
        mission = db.mission_by_id(self.conn, metadata["mission_id"])
        self.assertEqual(mission["label"], "provenance-check")
        self.assertEqual(metadata["mission_label"], "provenance-check")
        self.assertEqual(metadata["prompt_sha256"], mission["prompt_sha256"])
        self.assertEqual(
            hashlib.sha256(mission["prompt"].encode("utf-8")).hexdigest(), mission["prompt_sha256"]
        )
        generation = self.conn.execute(
            "SELECT interest_key FROM search_generations WHERE id = ?", (metadata["generation_id"],)
        ).fetchone()
        self.assertEqual(generation["interest_key"], "alpha")

    # --- provider outage -----------------------------------------------------

    def test_provider_outage_leases_nothing_and_spends_nothing(self):
        self._interest("alpha")
        self._seed_mission("alpha", "m1")
        mp = FakeCouncilProvider(preflight_ok=False)
        result = self._tick(mp)
        self.assertFalse(result["preflight_ok"])
        self.assertEqual(len(mp.complete_prompts), 0)
        self.assertEqual(len(mp.search_prompts), 0)
        row = self.conn.execute("SELECT status FROM search_missions WHERE label='m1'").fetchone()
        self.assertEqual(row["status"], "PENDING")   # never leased

    # --- end to end + fallback + default-behaviour ----------------------------

    def test_stubbed_end_to_end_web_tick_through_the_real_pipeline(self):
        self._interest("alpha")
        mp = FakeCouncilProvider(
            mission_batches=[mission_batch("e2e")],
            search_results={"e2e": search_hits("https://e.com/e2e")},
        )
        scoring = FakeProvider({"Result": 0.95})
        cfg = dataclasses.replace(self.cfg, council_missions_per_generation=1, missions_per_tick=1)
        self._tick(mp, scoring_provider=scoring, cfg=cfg, dry_run=True)

        # web_tick(), like run_once(), only *delivers* immediate ALERT-type
        # items here -- this default "article" item is DISCOVERY-lane and
        # stays pending for the digest, so the real assertion is that it
        # made it all the way to notification_ready() through the real
        # pipeline (matched -> scored -> above its interest's bar).
        ready = pipeline.notification_ready(self.conn, cfg)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0][1].url, "https://e.com/e2e")
        score_row = self.conn.execute(
            "SELECT final_score FROM scores s JOIN candidate_items i ON i.id = s.item_id"
            " WHERE i.url = ?", ("https://e.com/e2e",)
        ).fetchone()
        self.assertAlmostEqual(score_row["final_score"], 0.95)

    def test_static_fallback_enqueued_only_after_consecutive_council_failures(self):
        cfg = dataclasses.replace(
            self.cfg, council_max_consecutive_failures=2, mission_low_water=1, missions_per_tick=0,
            mission_retry_seconds=0,   # no replenish cooldown between these back-to-back ticks
        )
        interest = self._interest("alpha")

        # 1st failure: not enough yet -- no fallback.
        mp1 = FakeCouncilProvider(mission_batches=[providers.ProviderError("down")])
        self._tick(mp1, cfg=cfg)
        self.assertEqual(db.pending_mission_count(self.conn, "alpha"), 0)

        # 2nd consecutive failure: threshold reached -- fallback enqueued.
        mp2 = FakeCouncilProvider(mission_batches=[providers.ProviderError("still down")])
        self._tick(mp2, cfg=cfg)
        fallback = self.conn.execute(
            "SELECT label, generation_id FROM search_missions WHERE interest_key='alpha' AND status='PENDING'"
        ).fetchone()
        self.assertEqual(fallback["label"], missions.FALLBACK_LABEL)
        self.assertIsNone(fallback["generation_id"])

    def test_run_once_budgets_refactor_is_behavior_preserving(self):
        """budgets_for() is the only refactor pipeline.py permits itself
        this step -- run_once() for stocks/youtube-shaped collectors stays
        byte-identical to before it existed."""
        budget, explore_budget = pipeline.budgets_for(CFG)
        self.assertEqual(budget.remaining, CFG.max_scores_per_cycle)
        self.assertEqual(explore_budget.remaining, 0)   # dynamic_interests off by default

        db.upsert_interest(self.conn, an_interest(key="k", sources=["fake"]))

        def collector(interest, cfg, provider, conn=None):
            return [an_item(source="fake", title="Good stuff here", url="https://e.com/1")]

        provider = FakeProvider({"Good stuff": 0.9})
        with mock.patch.dict(COLLECTORS, {"fake": collector}):
            summary = pipeline.run_once(self.conn, provider, CFG, dry_run=True)
        self.assertEqual(
            summary,
            {"collected": 1, "duplicate": 0, "near_duplicate": 0, "filtered": 0,
             "already_scored": 0, "scored": 1, "deferred": 0, "errors": 0,
             "notified": 0},   # "article" isn't an ALERT type
        )


# --- step-13 task 1: trace backbone -------------------------------------------

class TraceRedactionTests(unittest.TestCase):
    def test_redact_replaces_only_matching_env_values(self):
        with mock.patch.dict(os.environ, {"MY_SECRET_TOKEN": "abc123456"}, clear=False):
            text = trace.redact("token is abc123456 but the interest text stays intact")
        self.assertNotIn("abc123456", text)
        self.assertIn("[REDACTED:MY_SECRET_TOKEN]", text)
        self.assertIn("the interest text stays intact", text)

    def test_redact_ignores_short_secret_looking_values(self):
        """A short value on a secret-shaped name (e.g. a flag, not a real
        token) must not be substring-replaced -- that would silently rewrite
        unrelated text anywhere it happens to appear."""
        with mock.patch.dict(os.environ, {"MY_AUTH_MODE": "on"}, clear=False):
            text = trace.redact("auth mode is on for this interest")
        self.assertEqual(text, "auth mode is on for this interest")

    def test_redact_is_a_noop_without_matching_env_vars(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(trace.redact("nothing secret here"), "nothing secret here")

    def test_redact_json_recurses_through_nested_structures(self):
        with mock.patch.dict(os.environ, {"THE_API_KEY": "sekrit-value"}, clear=False):
            out = trace.redact_json({"a": ["sekrit-value", {"b": "sekrit-value"}], "c": 1})
        self.assertEqual(out["a"][0], "[REDACTED:THE_API_KEY]")
        self.assertEqual(out["a"][1]["b"], "[REDACTED:THE_API_KEY]")
        self.assertEqual(out["c"], 1)

    def test_non_string_input_is_returned_unchanged(self):
        self.assertIsNone(trace.redact(None))
        self.assertEqual(trace.redact_json(42), 42)

    def test_longest_matching_value_wins_when_one_is_a_substring_of_another(self):
        with mock.patch.dict(
            os.environ, {"SHORT_KEY": "sek", "LONG_KEY": "sekrit-full"}, clear=False
        ):
            text = trace.redact("value: sekrit-full")
        self.assertEqual(text, "value: [REDACTED:LONG_KEY]")


class TraceConfigSnapshotFieldMaskingTests(unittest.TestCase):
    """value-substitution redact()/redact_json() misses two real cases for a
    Config snapshot: a short DISCOVERY_UI_TOKEN (under redact()'s 8-char
    floor) and DISCOVERY_NGROK_CMD (a free-form command whose field name
    doesn't match the secret-name regex, but which commonly embeds an inline
    --authtoken). _cfg_snapshot masks both by FIELD NAME, independent of the
    value's shape/length."""

    def test_short_ui_token_is_masked_by_field_name_not_left_verbatim(self):
        cfg = dataclasses.replace(CFG, ui_token="abc12")  # 5 chars: under redact()'s floor
        snapshot = trace._cfg_snapshot(cfg)
        self.assertEqual(snapshot["ui_token"], "[REDACTED:FIELD:ui_token]")

    def test_ngrok_cmd_with_inline_authtoken_is_masked_wholesale(self):
        cfg = dataclasses.replace(
            CFG, ngrok_cmd="ngrok http {port} --authtoken 2abcSecretTunnelToken"
        )
        snapshot = trace._cfg_snapshot(cfg)
        self.assertEqual(snapshot["ngrok_cmd"], "[REDACTED:FIELD:ngrok_cmd]")

    def test_empty_secret_named_fields_stay_empty_not_masked(self):
        snapshot = trace._cfg_snapshot(dataclasses.replace(CFG, ui_token="", ngrok_cmd=""))
        self.assertEqual(snapshot["ui_token"], "")
        self.assertEqual(snapshot["ngrok_cmd"], "")

    def test_non_secret_fields_survive_a_snapshot_unmasked(self):
        snapshot = trace._cfg_snapshot(dataclasses.replace(CFG, provider="claude_chat"))
        self.assertEqual(snapshot["provider"], "claude_chat")

    def test_masked_config_snapshot_reaches_trace_runs_and_stays_masked(self):
        conn = db.connect(":memory:")
        db.init(conn)
        self.addCleanup(conn.close)
        cfg = dataclasses.replace(
            CFG, trace_enabled=True, ui_token="abc12",
            ngrok_cmd="ngrok http {port} --authtoken tunnel-secret-xyz",
        )
        tracer = trace.Tracer(conn, cfg)
        run_id = tracer.begin_run("test")
        row = conn.execute("SELECT config_json FROM trace_runs WHERE id = ?", (run_id,)).fetchone()
        stored = json.loads(row["config_json"])
        self.assertEqual(stored["ui_token"], "[REDACTED:FIELD:ui_token]")
        self.assertEqual(stored["ngrok_cmd"], "[REDACTED:FIELD:ngrok_cmd]")
        self.assertNotIn("abc12", row["config_json"])
        self.assertNotIn("tunnel-secret-xyz", row["config_json"])


class _ProxyConn:
    """Forwards to a real sqlite3 connection, except that any statement
    containing `fail_on` raises -- used to prove Tracer._guard's fail-soft
    behavior without depending on whether sqlite3.Connection itself can be
    monkeypatched."""

    def __init__(self, real, fail_on):
        self._real = real
        self._fail_on = fail_on

    def execute(self, sql, *a, **kw):
        if self._fail_on in sql:
            raise sqlite3.OperationalError("disk full")
        return self._real.execute(sql, *a, **kw)

    def executemany(self, sql, *a, **kw):
        if self._fail_on in sql:
            raise sqlite3.OperationalError("disk full")
        return self._real.executemany(sql, *a, **kw)

    def commit(self):
        return self._real.commit()


class TraceEnableSwitchTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def test_disabled_tracer_writes_nothing_and_returns_none(self):
        cfg = dataclasses.replace(CFG, trace_enabled=False)
        tracer = trace.Tracer(self.conn, cfg)
        run_id = tracer.begin_run("test")
        self.assertIsNone(run_id)
        node_id = tracer.node(run_id, "x", label="y")
        self.assertIsNone(node_id)
        self.assertIsNone(tracer.edge(1, 2, "generated"))
        tracer.finish_node(node_id, status="ok")
        tracer.finish_run(run_id)
        for table in ("trace_runs", "trace_nodes", "trace_edges", "model_calls"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"], 0)

    def test_enabled_tracer_writes_a_run_and_a_node(self):
        cfg = dataclasses.replace(CFG, trace_enabled=True)
        tracer = trace.Tracer(self.conn, cfg)
        run_id = tracer.begin_run("test")
        self.assertIsNotNone(run_id)
        node_id = tracer.node(run_id, "x", label="y")
        self.assertIsNotNone(node_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM trace_runs").fetchone()["c"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM trace_nodes").fetchone()["c"], 1)

    def test_a_trace_write_failure_is_swallowed_and_bumps_a_metric(self):
        cfg = dataclasses.replace(CFG, trace_enabled=True)
        proxy = _ProxyConn(self.conn, "INSERT INTO trace_nodes")
        tracer = trace.Tracer(proxy, cfg)
        run_id = tracer.begin_run("test")
        node_id = tracer.node(run_id, "x", label="y")   # the write that fails
        self.assertIsNone(node_id)
        self.assertEqual(
            dict(self.conn.execute("SELECT name, count FROM metrics").fetchall()),
            {"trace_write_failed": 1},
        )
        # And the tick itself is untouched -- no exception escaped.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM trace_runs").fetchone()["c"], 1)


class TraceModelCallsTests(unittest.TestCase):
    """(a) byte-exact prompts including retry-suffix framing, and
    (b) a scoring failure + retry yields two model_calls rows, the failed
    one intact -- both via claude_chat's real provider/attempt machinery,
    not a hand-rolled stand-in."""

    SCHEMA = {
        "type": "object",
        "properties": {"a": {"type": "number"}},
        "required": ["a"],
        "additionalProperties": False,
    }

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.cfg = dataclasses.replace(CFG, trace_enabled=True)
        self.tracer = trace.Tracer(self.conn, self.cfg)
        self.run_id = self.tracer.begin_run("test")
        self.node_id = self.tracer.node(self.run_id, "score-attempt", label="x")

    def _provider(self, *replies):
        conn = FakeCDPConnection(list(replies))
        return claude_chat.ClaudeChatProvider(
            "claude-opus-5", org_id="org-123", port=9222, connect=lambda: conn,
        )

    def test_model_calls_store_the_byte_exact_prompt_including_retry_suffix(self):
        provider = self._provider(
            completion_reply("not json at all"), completion_reply('{"a": 1}'),
        )
        provider.trace_sink = self.tracer.sink
        with self.tracer.calls("scoring", self.node_id):
            provider.complete_json("SYS", "PROMPT", self.SCHEMA)

        rows = self.conn.execute(
            "SELECT attempt, exact_user_prompt, raw_response_text, validation_result "
            "FROM model_calls WHERE trace_node_id = ? ORDER BY attempt",
            (self.node_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        expected_base = "SYS\n\nPROMPT" + claude_chat.STRICT_JSON_SUFFIX.format(
            schema=json.dumps(self.SCHEMA)
        )
        self.assertEqual(rows[0]["exact_user_prompt"], expected_base)
        self.assertEqual(rows[1]["exact_user_prompt"], expected_base + claude_chat.RETRY_SUFFIX)

    def test_a_scoring_failure_and_retry_yields_two_rows_the_failed_one_intact(self):
        provider = self._provider(
            completion_reply("garbage, not an object"), completion_reply('{"a": 1}'),
        )
        provider.trace_sink = self.tracer.sink
        with self.tracer.calls("scoring", self.node_id):
            provider.complete_json("SYS", "PROMPT", self.SCHEMA)

        rows = self.conn.execute(
            "SELECT attempt, raw_response_text, validation_result, parsed_response_json, error "
            "FROM model_calls WHERE trace_node_id = ? ORDER BY attempt",
            (self.node_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["raw_response_text"], "garbage, not an object")
        self.assertIn("invalid", rows[0]["validation_result"])
        self.assertIsNone(rows[0]["parsed_response_json"])
        self.assertEqual(rows[1]["raw_response_text"], '{"a": 1}')
        self.assertEqual(rows[1]["validation_result"], "valid")
        self.assertEqual(json.loads(rows[1]["parsed_response_json"]), {"a": 1})

    def test_call_role_comes_from_the_tracer_context_not_the_provider(self):
        provider = self._provider(completion_reply('{"a": 1}'))
        provider.trace_sink = self.tracer.sink
        with self.tracer.calls("mission_search", self.node_id):
            provider.complete_json("SYS", "PROMPT", self.SCHEMA)
        row = self.conn.execute(
            "SELECT call_role FROM model_calls WHERE trace_node_id = ?", (self.node_id,)
        ).fetchone()
        self.assertEqual(row["call_role"], "mission_search")

    def test_a_provider_call_outside_any_calls_context_is_simply_unattributed(self):
        provider = self._provider(completion_reply('{"a": 1}'))
        provider.trace_sink = self.tracer.sink
        provider.complete_json("SYS", "PROMPT", self.SCHEMA)   # no tracer.calls() active
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM model_calls").fetchone()["c"], 0)


class TraceOnOffParityTests(unittest.TestCase):
    """(c) tracing ON vs OFF changes zero provider calls / production rows."""

    def _run(self, trace_enabled):
        conn = db.connect(":memory:")
        db.init(conn)
        cfg = dataclasses.replace(
            CFG, trace_enabled=trace_enabled, telegram_bot_token="", telegram_chat_id="",
        )
        db.upsert_interest(conn, an_interest(min_score=0.5))
        interests = db.active_interests(conn)
        provider = FakeProvider({"Good": 0.9, "Meh": 0.2})
        tracer = trace.Tracer(conn, cfg)
        provider.trace_sink = tracer.sink
        run_id = tracer.begin_run("test")

        items = [
            an_item(title="Good", url="https://e.com/good"),
            an_item(title="Good", url="https://e.com/good"),   # duplicate
            an_item(title="Meh", url="https://e.com/meh"),      # below threshold
            an_item(title="x", url="https://e.com/short", text="short"),   # filtered
        ]
        for item in items:
            node = tracer.node(run_id, "raw-result", label=item.title)
            outcome = pipeline.ingest(
                conn, provider, cfg, item, interests, origin_interest="k",
                tracer=tracer, source_node_id=node,
            )
            db.bump(conn, {"collected": 1, pipeline.outcome_metric(outcome): 1})
        pipeline.send_digest(conn, cfg, dry_run=True, tracer=tracer)
        tracer.finish_run(run_id)
        return conn, provider

    def test_tracing_on_or_off_changes_nothing_about_pipeline_behavior(self):
        conn_on, provider_on = self._run(True)
        conn_off, provider_off = self._run(False)

        self.assertEqual(len(provider_on.prompts), len(provider_off.prompts))
        self.assertGreater(len(provider_on.prompts), 0)

        volatile = ("created_at", "sent_at", "first_seen_at", "score_attempted_at")
        for table in ("candidate_items", "scores", "notifications", "feedback"):
            rows_on = [dict(r) for r in conn_on.execute(f"SELECT * FROM {table}")]
            rows_off = [dict(r) for r in conn_off.execute(f"SELECT * FROM {table}")]
            for row in rows_on + rows_off:
                for key in volatile:
                    row.pop(key, None)
            self.assertEqual(rows_on, rows_off, table)

        metrics_on = dict(conn_on.execute(
            "SELECT name, count FROM metrics WHERE name != 'trace_write_failed'"
        ).fetchall())
        metrics_off = dict(conn_off.execute(
            "SELECT name, count FROM metrics WHERE name != 'trace_write_failed'"
        ).fetchall())
        self.assertEqual(metrics_on, metrics_off)

        self.assertEqual(conn_off.execute("SELECT COUNT(*) c FROM trace_runs").fetchone()["c"], 0)
        self.assertEqual(conn_off.execute("SELECT COUNT(*) c FROM trace_nodes").fetchone()["c"], 0)
        self.assertEqual(conn_off.execute("SELECT COUNT(*) c FROM trace_edges").fetchone()["c"], 0)
        self.assertEqual(conn_off.execute("SELECT COUNT(*) c FROM model_calls").fetchone()["c"], 0)
        self.assertGreater(conn_on.execute("SELECT COUNT(*) c FROM trace_nodes").fetchone()["c"], 0)
        self.assertGreater(conn_on.execute("SELECT COUNT(*) c FROM model_calls").fetchone()["c"], 0)
        conn_on.close()
        conn_off.close()


class TracePlantedSecretTests(unittest.TestCase):
    """(d) planted secret values never appear in any trace table."""

    def test_planted_secrets_are_redacted_everywhere_they_could_land(self):
        secrets = {
            "TELEGRAM_BOT_TOKEN": "tg-planted-secret-001",
            "ANTHROPIC_API_KEY": "anthropic-planted-secret-002",
            "FIXTURE_SESSION_COOKIE": "cookie-planted-secret-003",
        }
        with mock.patch.dict(os.environ, secrets, clear=False):
            conn = db.connect(":memory:")
            db.init(conn)
            self.addCleanup(conn.close)
            cfg = dataclasses.replace(CFG, trace_enabled=True, telegram_bot_token=secrets["TELEGRAM_BOT_TOKEN"])
            tracer = trace.Tracer(conn, cfg)
            run_id = tracer.begin_run(
                "test", config_json={"telegram_bot_token": secrets["TELEGRAM_BOT_TOKEN"]},
            )
            node_id = tracer.node(
                run_id, "note", label=secrets["ANTHROPIC_API_KEY"],
                summary=f"contains {secrets['FIXTURE_SESSION_COOKIE']}",
                input_json={"k": secrets["TELEGRAM_BOT_TOKEN"]},
                output_json={"k": secrets["ANTHROPIC_API_KEY"]},
                exact_text=f"{secrets['TELEGRAM_BOT_TOKEN']} and {secrets['FIXTURE_SESSION_COOKIE']}",
            )
            tracer.finish_node(
                node_id, summary=f"done, saw {secrets['ANTHROPIC_API_KEY']}",
                error=f"failed with {secrets['FIXTURE_SESSION_COOKIE']}",
            )
            tracer.set_call_context("scoring", node_id)
            tracer.sink(
                attempt=1, provider="fake", model="fake-1",
                system=f"sys {secrets['ANTHROPIC_API_KEY']}",
                prompt=f"prompt {secrets['TELEGRAM_BOT_TOKEN']}",
                schema=None, params=None,
                raw_text=f"raw {secrets['FIXTURE_SESSION_COOKIE']}",
                parsed={"a": secrets["ANTHROPIC_API_KEY"]}, validation="valid",
                error=None, started="t1", finished="t2",
            )
            tracer.finish_run(run_id, error=f"run failed: {secrets['TELEGRAM_BOT_TOKEN']}")

        checks = (
            ("trace_runs", ("config_json", "error")),
            ("trace_nodes", ("label", "summary", "input_json", "output_json", "exact_text", "error")),
            ("model_calls", (
                "exact_system_prompt", "exact_user_prompt", "raw_response_text",
                "parsed_response_json", "error",
            )),
        )
        for table, cols in checks:
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                for col in cols:
                    value = row[col]
                    if value is None:
                        continue
                    for secret_value in secrets.values():
                        self.assertNotIn(secret_value, value, f"{table}.{col}: {value!r}")


class TracePipelineWiringTests(unittest.TestCase):
    """(e) duplicate persistence + duplicate_of edge, (i) threshold snapshot."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.cfg = dataclasses.replace(CFG, trace_enabled=True)
        db.upsert_interest(self.conn, an_interest(min_score=0.5))
        self.interests = db.active_interests(self.conn)
        self.tracer = trace.Tracer(self.conn, self.cfg)
        self.run_id = self.tracer.begin_run("test")

    def test_a_duplicate_raw_result_gets_a_persistent_node_and_duplicate_of_edge(self):
        provider = FakeProvider({"Good": 0.9})
        first_source = self.tracer.node(self.run_id, "raw-result", label="first")
        pipeline.ingest(
            self.conn, provider, self.cfg, an_item(title="Good", url="https://e.com/g"),
            self.interests, origin_interest="k", tracer=self.tracer, source_node_id=first_source,
        )
        second_source = self.tracer.node(self.run_id, "raw-result", label="second")
        outcome = pipeline.ingest(
            self.conn, provider, self.cfg, an_item(title="Good", url="https://e.com/g"),
            self.interests, origin_interest="k", tracer=self.tracer, source_node_id=second_source,
        )
        self.assertEqual(outcome.stage, "duplicate")

        dup_node = self.conn.execute(
            "SELECT id FROM trace_nodes WHERE node_type = 'duplicate'"
        ).fetchone()
        self.assertIsNotNone(dup_node)
        edges = self.conn.execute(
            "SELECT relationship FROM trace_edges WHERE from_node_id = ?", (dup_node["id"],)
        ).fetchall()
        self.assertIn("duplicate_of", [r["relationship"] for r in edges])
        # And the raw-result -> duplicate branch itself survives (normalized_to).
        normalized = self.conn.execute(
            "SELECT COUNT(*) c FROM trace_edges WHERE from_node_id = ? AND relationship = 'normalized_to'",
            (second_source,),
        ).fetchone()["c"]
        self.assertEqual(normalized, 1)

    def test_threshold_snapshot_survives_a_later_interest_bar_change(self):
        provider = FakeProvider({"Good": 0.6})   # clears 0.5, would NOT clear 0.9
        source = self.tracer.node(self.run_id, "raw-result", label="src")
        pipeline.ingest(
            self.conn, provider, self.cfg, an_item(title="Good", url="https://e.com/g"),
            self.interests, origin_interest="k", tracer=self.tracer, source_node_id=source,
        )
        threshold_node = self.conn.execute(
            "SELECT output_json FROM trace_nodes WHERE node_type = 'threshold'"
        ).fetchone()
        before = json.loads(threshold_node["output_json"])
        self.assertAlmostEqual(before["threshold"], 0.5)
        self.assertAlmostEqual(before["final_score"], 0.6)

        # Raise the bar well past the score that already cleared it.
        self.conn.execute("UPDATE interests SET min_score = 0.9 WHERE key = 'k'")
        self.conn.commit()

        after_node = self.conn.execute(
            "SELECT output_json FROM trace_nodes WHERE node_type = 'threshold'"
        ).fetchone()
        after = json.loads(after_node["output_json"])
        self.assertEqual(before, after)   # untouched -- append-only, no live re-query
        edge = self.conn.execute(
            "SELECT relationship FROM trace_edges WHERE relationship IN ('cleared_threshold', 'rejected')"
        ).fetchone()
        self.assertEqual(edge["relationship"], "cleared_threshold")   # true at the time it was scored


class TraceCouncilDeliberationTests(unittest.TestCase):
    """(f) deliberation nodes persisted when well-formed; missions stay
    strict and every section reads 'unavailable' when malformed."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.cfg = dataclasses.replace(
            CFG, trace_enabled=True, mission_provider="fake_mission",
            council_missions_per_generation=2, missions_per_tick=0, mission_low_water=1,
        )
        db.upsert_interest(self.conn, an_interest(key="alpha", title="alpha", sources=["web_search"]))

    def _tick(self, mission_provider):
        scoring_provider = FakeProvider()
        with mock.patch.object(providers, "get_provider", return_value=mission_provider):
            missions.web_tick(self.conn, self.cfg, provider=scoring_provider, dry_run=True)

    def test_well_formed_deliberation_is_persisted_as_trace_nodes(self):
        batch = mission_batch("a", "b")
        batch["deliberation"] = {
            "advisors": [{"name": f"Advisor {i}", "persona": "p", "analysis": "x"} for i in range(1, 6)],
            "peer_review": [{"reviewer": f"Advisor {i}", "critiques": "c", "ranking": ["A"]} for i in range(1, 6)],
            "aggregate_ranking": ["A", "B"],
            "disagreements": "none material",
            "rejected_angles": [{"angle": "social-sentiment", "reason": "too noisy"}],
            "chairman_synthesis": "go with A and B",
            "selection_rationale": "A generalizes best",
        }
        self._tick(FakeCouncilProvider(mission_batches=[batch]))

        counts = dict(self.conn.execute(
            "SELECT node_type, COUNT(*) c FROM trace_nodes GROUP BY node_type"
        ).fetchall())
        self.assertEqual(counts.get("advisor"), 5)
        self.assertEqual(counts.get("peer-review"), 5)
        self.assertEqual(counts.get("aggregate-ranking"), 1)
        self.assertEqual(counts.get("rejected-angle"), 1)
        self.assertEqual(counts.get("chairman"), 1)
        chairman = self.conn.execute(
            "SELECT output_json FROM trace_nodes WHERE node_type = 'chairman'"
        ).fetchone()
        self.assertIn("go with A and B", json.loads(chairman["output_json"])["chairman_synthesis"])
        # Missions themselves: strict, exactly what the batch specified.
        labels = [r["label"] for r in self.conn.execute(
            "SELECT label FROM search_missions ORDER BY id"
        ).fetchall()]
        self.assertEqual(labels, ["a", "b"])

    def test_malformed_deliberation_marks_every_section_unavailable_missions_stay_strict(self):
        batch = mission_batch("a", "b")
        batch["deliberation"] = {"advisors": "not-a-list"}   # everything else missing too
        self._tick(FakeCouncilProvider(mission_batches=[batch]))

        labels = [r["label"] for r in self.conn.execute(
            "SELECT label FROM search_missions ORDER BY id"
        ).fetchall()]
        self.assertEqual(labels, ["a", "b"])   # strict validation untouched by deliberation

        for node_type in ("advisor", "peer-review", "aggregate-ranking", "rejected-angle"):
            row = self.conn.execute(
                "SELECT output_json FROM trace_nodes WHERE node_type = ?", (node_type,)
            ).fetchone()
            self.assertIsNotNone(row, node_type)
            payload = json.loads(row["output_json"])
            self.assertTrue(payload.get("unavailable"), (node_type, payload))
        chairman = self.conn.execute(
            "SELECT output_json FROM trace_nodes WHERE node_type = 'chairman'"
        ).fetchone()
        chairman_payload = json.loads(chairman["output_json"])
        self.assertTrue(chairman_payload["chairman_synthesis"]["unavailable"])


class TraceMissionToolEventTests(unittest.TestCase):
    """(g) 'not exposed by provider' node when the mission provider surfaces
    no tool events for a search_json call."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.cfg = dataclasses.replace(
            CFG, trace_enabled=True, mission_provider="fake_mission",
            council_missions_per_generation=1, missions_per_tick=1, mission_low_water=1,
        )
        db.upsert_interest(self.conn, an_interest(key="alpha", title="alpha", sources=["web_search"]))

    def test_no_events_from_the_provider_writes_one_explicit_node(self):
        mp = FakeCouncilProvider(
            mission_batches=[mission_batch("solo")],
            search_results={"solo": search_hits("https://e.com/solo")},
        )
        self.assertIsNone(mp.last_events)   # FakeCouncilProvider never sets it
        with mock.patch.object(providers, "get_provider", return_value=mp):
            missions.web_tick(self.conn, self.cfg, provider=FakeProvider({"Result": 0.9}), dry_run=True)

        node = self.conn.execute(
            "SELECT label FROM trace_nodes WHERE node_type = 'tool-event'"
        ).fetchone()
        self.assertEqual(node["label"], "not exposed by provider")

    def test_events_the_provider_does_expose_become_one_node_each(self):
        mp = FakeCouncilProvider(
            mission_batches=[mission_batch("solo")],
            search_results={"solo": search_hits("https://e.com/solo")},
        )
        real_search_json = mp.search_json

        def search_json_with_events(prompt, **kw):
            result = real_search_json(prompt, **kw)
            mp.last_events = [{"type": "server_tool_use", "name": "web_search"}]
            return result

        mp.search_json = search_json_with_events
        with mock.patch.object(providers, "get_provider", return_value=mp):
            missions.web_tick(self.conn, self.cfg, provider=FakeProvider({"Result": 0.9}), dry_run=True)

        rows = self.conn.execute(
            "SELECT label FROM trace_nodes WHERE node_type = 'tool-event'"
        ).fetchall()
        self.assertEqual([r["label"] for r in rows], ["server_tool_use"])


class TraceMissionJunkResultTests(unittest.TestCase):
    """A raw mission result that to_items() silently drops (non-dict,
    missing url, or past mission_max_results) still gets a terminal
    outcome node/edge, not just the inbound 'returned' edge."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.cfg = dataclasses.replace(
            CFG, trace_enabled=True, mission_provider="fake_mission",
            council_missions_per_generation=1, missions_per_tick=1, mission_low_water=1,
        )
        db.upsert_interest(self.conn, an_interest(key="alpha", title="alpha", sources=["web_search"]))

    def _tick(self, raw_results):
        mp = FakeCouncilProvider(
            mission_batches=[mission_batch("solo")],
            search_results={"solo": raw_results},
        )
        with mock.patch.object(providers, "get_provider", return_value=mp):
            missions.web_tick(self.conn, self.cfg, provider=FakeProvider({"Result": 0.9}), dry_run=True)

    def test_a_no_url_result_gets_a_rejected_edge_not_a_dead_end(self):
        self._tick([
            {"title": "no url here", "url": "", "summary": "x" * 40},
            {"title": "Good", "url": "https://e.com/g", "summary": "x" * 40},
        ])
        dropped = self.conn.execute(
            "SELECT id, summary FROM trace_nodes WHERE node_type = 'raw-result-dropped'"
        ).fetchone()
        self.assertIsNotNone(dropped)
        self.assertEqual(dropped["summary"], "missing url")

        raw_node = self.conn.execute(
            "SELECT id FROM trace_nodes WHERE node_type = 'raw-result' AND label = 'no url here'"
        ).fetchone()
        edge = self.conn.execute(
            "SELECT relationship, to_node_id FROM trace_edges WHERE from_node_id = ?",
            (raw_node["id"],),
        ).fetchone()
        self.assertEqual(edge["relationship"], "rejected")
        self.assertEqual(edge["to_node_id"], dropped["id"])

    def test_a_non_dict_result_gets_a_rejected_edge(self):
        self._tick(["just a string, not an object"])
        dropped = self.conn.execute(
            "SELECT summary FROM trace_nodes WHERE node_type = 'raw-result-dropped'"
        ).fetchone()
        self.assertEqual(dropped["summary"], "not a JSON object")

    def test_a_result_past_mission_max_results_gets_a_rejected_edge(self):
        cfg = dataclasses.replace(self.cfg, mission_max_results=1)
        mp = FakeCouncilProvider(
            mission_batches=[mission_batch("solo")],
            search_results={"solo": [
                {"title": "Kept", "url": "https://e.com/kept", "summary": "x" * 40},
                {"title": "Overflow", "url": "https://e.com/overflow", "summary": "x" * 40},
            ]},
        )
        with mock.patch.object(providers, "get_provider", return_value=mp):
            missions.web_tick(self.conn, cfg, provider=FakeProvider({"Result": 0.9}), dry_run=True)

        dropped = self.conn.execute(
            "SELECT summary FROM trace_nodes WHERE node_type = 'raw-result-dropped'"
        ).fetchone()
        self.assertIn("mission_max_results", dropped["summary"])


class TraceFixtureTests(unittest.TestCase):
    """(h) the fixture is structurally deterministic: two builds against
    fresh DBs produce the same node/edge/model_call counts and labels."""

    def _build(self):
        conn = db.connect(":memory:")
        db.init(conn)
        cfg = dataclasses.replace(CFG, trace_enabled=True)
        result = trace_fixture.build(conn, cfg)
        return conn, result

    def test_two_builds_produce_the_same_shape(self):
        conn1, result1 = self._build()
        conn2, result2 = self._build()
        self.addCleanup(conn1.close)
        self.addCleanup(conn2.close)

        self.assertEqual(result1, result2)

        def shape(conn):
            node_counts = dict(conn.execute(
                "SELECT node_type, COUNT(*) c FROM trace_nodes GROUP BY node_type"
            ).fetchall())
            edge_counts = dict(conn.execute(
                "SELECT relationship, COUNT(*) c FROM trace_edges GROUP BY relationship"
            ).fetchall())
            call_role_counts = dict(conn.execute(
                "SELECT call_role, COUNT(*) c FROM model_calls GROUP BY call_role"
            ).fetchall())
            labels = sorted(r["label"] for r in conn.execute(
                "SELECT label FROM trace_nodes"
            ).fetchall())
            return node_counts, edge_counts, call_role_counts, labels

        self.assertEqual(shape(conn1), shape(conn2))

    def test_fixture_exercises_every_required_branch(self):
        conn, result = self._build()
        self.addCleanup(conn.close)
        self.assertEqual(result["missions_generated"], 3)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM trace_nodes WHERE node_type = 'duplicate'").fetchone()["c"], 1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) c FROM trace_nodes WHERE node_type = 'prefilter' AND status = 'ok'"
            ).fetchone()["c"],
            1,
        )
        scoring_calls = conn.execute(
            "SELECT COUNT(*) c FROM model_calls WHERE call_role = 'scoring'"
        ).fetchone()["c"]
        self.assertEqual(scoring_calls, 4)   # 3 score-attempts, one retried once
        self.assertEqual(result["digest_sent"], 1)
        self.assertTrue(result["feedback_recorded"])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 1,
        )
        # tracing off is not exercised by the fixture itself -- it always
        # runs with cfg.trace_enabled True (see _build()).

    def test_the_trace_graph_is_a_single_connected_component(self):
        """Repair regression: interest-state -> generation, mission ->
        mission-execution, score-attempt -> score-debug and threshold ->
        render edges were all missing, leaving the fixture's 54 nodes in 8
        disconnected islands -- a UI could never draw one connected graph
        from a candidate/score/notification back to the Council that
        planned it."""
        conn, _result = self._build()
        self.addCleanup(conn.close)
        node_ids = [r["id"] for r in conn.execute("SELECT id FROM trace_nodes")]
        adjacency = {n: set() for n in node_ids}
        for row in conn.execute("SELECT from_node_id, to_node_id FROM trace_edges"):
            adjacency[row["from_node_id"]].add(row["to_node_id"])
            adjacency[row["to_node_id"]].add(row["from_node_id"])

        seen = set()
        stack = [node_ids[0]]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adjacency[node] - seen)
        self.assertEqual(seen, set(node_ids))

    def test_both_raw_results_sharing_a_url_get_their_own_outcome_edge(self):
        """Repair regression: raw_node_by_url was first-wins, so BOTH raw
        results with the same (duplicate) url resolved to the first raw
        node -- the second (the one actually flagged as a duplicate) had no
        normalized_to edge of its own at all."""
        conn, _result = self._build()
        self.addCleanup(conn.close)
        raw_rows = conn.execute(
            "SELECT id, label FROM trace_nodes "
            "WHERE node_type = 'raw-result' AND label LIKE 'Duplicate Topic Finding%'"
        ).fetchall()
        self.assertEqual(len(raw_rows), 2)
        for row in raw_rows:
            outgoing = conn.execute(
                "SELECT relationship FROM trace_edges WHERE from_node_id = ? AND relationship = 'normalized_to'",
                (row["id"],),
            ).fetchall()
            self.assertEqual(len(outgoing), 1, row["label"])

    def test_notification_node_entity_id_is_the_notifications_row_not_the_score(self):
        conn, _result = self._build()
        self.addCleanup(conn.close)
        node = conn.execute(
            "SELECT entity_id FROM trace_nodes WHERE node_type = 'notification'"
        ).fetchone()
        notification_row = conn.execute("SELECT id, score_id FROM notifications").fetchone()
        self.assertEqual(int(node["entity_id"]), notification_row["id"])
        self.assertNotEqual(notification_row["id"], notification_row["score_id"])


class TraceDigestCmdTests(unittest.TestCase):
    """Repair regression: the CLI's `digest` command called send_digest()
    with no tracer/begin_run at all -- the only production path that ever
    delivers a DISCOVERY item was completely untraced, unlike run-once and
    web-tick which wrap themselves internally."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def test_digest_cmd_opens_and_closes_a_trace_run(self):
        from discovery.__main__ import _digest_cmd

        cfg = dataclasses.replace(CFG, trace_enabled=True)
        code = _digest_cmd(self.conn, cfg, True)
        self.assertEqual(code, 0)
        row = self.conn.execute("SELECT kind, status FROM trace_runs").fetchone()
        self.assertEqual((row["kind"], row["status"]), ("digest", "done"))


class TraceRunOnceCollectorItemRootTests(unittest.TestCase):
    """Repair regression: _run_once() called ingest() with no source_node_id,
    so candidate_node's 'normalized_to' edge had a None from-endpoint and
    Tracer.edge() silently skipped it (no-ops on a None endpoint) -- every
    candidate's subtree was reachable only by run_id, not by any edge. A
    'collector-item' root node per collected item, passed through as
    source_node_id, is what the ingest wiring spec means by 'the raw-result
    (or collector-item) node'."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.cfg = dataclasses.replace(CFG, trace_enabled=True)
        db.upsert_interest(self.conn, an_interest(key="k", sources=["fake"]))

    def test_candidate_node_is_reachable_from_a_collector_item_node(self):
        def collector(interest, cfg, provider, conn=None):
            return [an_item(source="fake", title="Good stuff here", url="https://e.com/1")]

        provider = FakeProvider({"Good stuff": 0.9})
        with mock.patch.dict(COLLECTORS, {"fake": collector}):
            pipeline.run_once(self.conn, provider, self.cfg, dry_run=True)

        source_node = self.conn.execute(
            "SELECT id FROM trace_nodes WHERE node_type = 'collector-item' AND label = 'Good stuff here'"
        ).fetchone()
        self.assertIsNotNone(source_node)
        edge = self.conn.execute(
            "SELECT to_node_id FROM trace_edges WHERE from_node_id = ? AND relationship = 'normalized_to'",
            (source_node["id"],),
        ).fetchone()
        self.assertIsNotNone(edge, "collector-item node has no outgoing normalized_to edge")
        candidate_node = self.conn.execute(
            "SELECT node_type FROM trace_nodes WHERE id = ?", (edge["to_node_id"],)
        ).fetchone()
        self.assertEqual(candidate_node["node_type"], "candidate")


# --- provider fallback (providers/fallback.py) --------------------------------

class _ScriptedProvider(LLMProvider):
    """Returns canned results, or raises the canned error, and logs calls."""

    def __init__(self, name, error=None):
        super().__init__(model=f"{name}-model")
        self.name = name
        self.error = error
        self.calls = []

    def complete_json(self, system, prompt, schema, max_tokens=8000):
        self.calls.append("complete_json")
        if self.error is not None:
            raise self.error
        self.record_usage(input_tokens=3, output_tokens=2)
        return {"from": self.name}

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        self.calls.append("search_json")
        if self.error is not None:
            raise self.error
        self.record_usage(web_searches=1)
        self.last_events = [{"type": "tool_message", "name": self.name}]
        return [{"from": self.name}]

    def preflight(self):
        if self.error is not None:
            return False, f"{self.name} down"
        return True, ""


class FallbackProviderTests(unittest.TestCase):
    def _pair(self, primary_error=None, fallback_error=None):
        primary = _ScriptedProvider("primary", primary_error)
        backup = _ScriptedProvider("backup", fallback_error)
        return FallbackProvider(primary, backup), primary, backup

    def test_primary_success_never_touches_the_fallback(self):
        wrapped, primary, backup = self._pair()
        with mock.patch("sys.stderr", new=io.StringIO()):
            out = wrapped.complete_json("s", "p", {})
        self.assertEqual(out, {"from": "primary"})
        self.assertEqual(backup.calls, [])
        self.assertEqual(wrapped.name, "primary")
        self.assertEqual(wrapped.model, "primary-model")

    def test_provider_error_falls_through_to_the_fallback(self):
        wrapped, primary, backup = self._pair(primary_error=ProviderError("rate limited"))
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            out = wrapped.complete_json("s", "p", {})
        self.assertEqual(out, {"from": "backup"})
        self.assertEqual(primary.calls, ["complete_json"])
        self.assertIn("falling back to backup", err.getvalue())
        # name/model/last_events now describe who actually served the call
        self.assertEqual(wrapped.name, "backup")
        self.assertEqual(wrapped.model, "backup-model")

    def test_unsupported_capability_borrows_the_fallbacks_search(self):
        wrapped, _, backup = self._pair(
            primary_error=UnsupportedCapability("no search here")
        )
        with mock.patch("sys.stderr", new=io.StringIO()):
            out = wrapped.search_json("find things")
        self.assertEqual(out, [{"from": "backup"}])
        self.assertEqual(wrapped.last_events, [{"type": "tool_message", "name": "backup"}])

    def test_non_provider_error_propagates_untouched(self):
        wrapped, _, backup = self._pair(primary_error=ValueError("a bug, not an outage"))
        with self.assertRaises(ValueError):
            wrapped.complete_json("s", "p", {})
        self.assertEqual(backup.calls, [])

    def test_both_failing_surfaces_the_fallbacks_error(self):
        wrapped, _, _ = self._pair(
            primary_error=ProviderError("primary down"),
            fallback_error=ProviderError("backup down too"),
        )
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(ProviderError) as ctx:
                wrapped.complete_json("s", "p", {})
        self.assertIn("backup down too", str(ctx.exception))

    def test_trace_sink_installs_on_both_real_providers(self):
        wrapped, primary, backup = self._pair()
        sink = lambda **kw: None  # noqa: E731
        wrapped.trace_sink = sink
        self.assertIs(primary.trace_sink, sink)
        self.assertIs(backup.trace_sink, sink)

    def test_preflight_passes_while_either_side_is_up(self):
        wrapped, _, _ = self._pair(primary_error=ProviderError("down"))
        ok, detail = wrapped.preflight()
        self.assertTrue(ok)
        self.assertIn("backup ready", detail)

    def test_preflight_fails_only_when_both_sides_are_down(self):
        wrapped, _, _ = self._pair(
            primary_error=ProviderError("down"), fallback_error=ProviderError("down")
        )
        ok, detail = wrapped.preflight()
        self.assertFalse(ok)
        self.assertIn("primary", detail)
        self.assertIn("backup", detail)

    def test_record_usage_drains_both_providers_under_their_own_names(self):
        wrapped, primary, backup = self._pair(primary_error=ProviderError("nope"))
        with mock.patch("sys.stderr", new=io.StringIO()):
            wrapped.complete_json("s", "p", {})
        conn = db.connect(":memory:")
        db.init(conn)
        self.addCleanup(conn.close)
        db.record_usage(conn, wrapped)
        rows = {
            r["provider"]: dict(r)
            for r in conn.execute("SELECT * FROM llm_usage").fetchall()
        }
        # The failed primary attempt recorded nothing (usage is only bumped on
        # success in _ScriptedProvider); the serving fallback recorded its call.
        self.assertEqual(list(rows), ["backup"])
        self.assertEqual(rows["backup"]["model"], "backup-model")
        self.assertEqual(rows["backup"]["calls"], 1)
        self.assertFalse(backup.usage)   # drained

    def test_get_provider_wraps_only_when_a_distinct_fallback_is_configured(self):
        base = dataclasses.replace(CFG, provider="claude_chat", model="m")
        self.assertNotIsInstance(get_provider(base), FallbackProvider)

        wrapped = get_provider(dataclasses.replace(
            base, provider_fallback="chatgpt_browser", provider_fallback_model="latest-high",
        ))
        self.assertIsInstance(wrapped, FallbackProvider)
        self.assertIsInstance(wrapped.primary, claude_chat.ClaudeChatProvider)
        self.assertIsInstance(wrapped.fallback, chatgpt_browser.ChatGPTBrowserProvider)
        self.assertEqual(wrapped.fallback.model, "latest-high")

        same = get_provider(dataclasses.replace(base, provider_fallback="claude_chat"))
        self.assertNotIsInstance(same, FallbackProvider)


import importlib.util as _ilu  # noqa: E402

_su_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops", "self_update.py")
_su_spec = _ilu.spec_from_file_location("self_update", _su_path)
self_update = _ilu.module_from_spec(_su_spec)
_su_spec.loader.exec_module(self_update)


class FakeGit:
    """Answers the read-only queries self_update.gather() makes, plus records
    the mutating ones (merge) so a test can assert whether a fast-forward ran."""

    def __init__(self, *, branch="main", dirty=False, local="a" * 40,
                 remote="a" * 40, ancestor=True, subjects=""):
        self.branch, self.dirty = branch, dirty
        self.local, self.remote = local, remote
        self.ancestor, self.subjects = ancestor, subjects
        self.calls = []

    def __call__(self, args, check=True):
        self.calls.append(args)
        if args[0] == "fetch":
            return 0, ""
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return 0, self.branch
        if args == ["status", "--porcelain"]:
            return 0, ("M file\n" if self.dirty else "")
        if args == ["rev-parse", "HEAD"]:
            return 0, self.local
        if args[0] == "rev-parse" and args[-1].startswith("origin/"):
            return 0, self.remote
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return (0 if self.ancestor else 1), ""
        if args[:2] == ["log", "--oneline"]:
            return 0, self.subjects
        if args[:2] == ["merge", "--ff-only"]:
            return 0, ""
        return 0, ""

    def merged(self):
        return any(a[:2] == ["merge", "--ff-only"] for a in self.calls)


class SelfUpdatePlanTests(unittest.TestCase):
    """The pure decision -- no git, no I/O."""

    def test_each_state_maps_to_the_right_action(self):
        L, R = "a" * 40, "b" * 40
        cases = [
            (dict(branch="feature", deploy_branch="main", dirty=False, local=L, remote=R, is_ancestor=True),
             self_update.SKIP_BRANCH),
            (dict(branch="main", deploy_branch="main", dirty=True, local=L, remote=R, is_ancestor=True),
             self_update.SKIP_DIRTY),
            (dict(branch="main", deploy_branch="main", dirty=False, local=L, remote=L, is_ancestor=True),
             self_update.CURRENT),
            (dict(branch="main", deploy_branch="main", dirty=False, local=L, remote=R, is_ancestor=False),
             self_update.DIVERGED),
            (dict(branch="main", deploy_branch="main", dirty=False, local=L, remote=R, is_ancestor=True),
             self_update.UPDATE),
        ]
        for kwargs, expected in cases:
            action, _note = self_update.plan(**kwargs)
            self.assertEqual(action, expected, kwargs)


class SelfUpdateRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.cfg = SimpleNamespace(telegram_bot_token="", telegram_chat_id="")
        self.sent = []
        p = mock.patch.object(self_update.notify, "send",
                              side_effect=lambda cfg, text, **_k: self.sent.append(text) or True)
        p.start()
        self.addCleanup(p.stop)

    def test_a_clean_fast_forward_merges_redeploys_and_announces(self):
        git = FakeGit(local="a" * 40, remote="b" * 40, subjects="b1 fix\nb2 more")
        redeployed = []
        action = self_update.run(
            self.root, self.cfg, git=git, deploy_branch="main",
            redeploy=lambda root, log=print: redeployed.append(root) or True, log=lambda *_: None)
        self.assertEqual(action, self_update.UPDATE)
        self.assertTrue(git.merged())
        self.assertEqual(redeployed, [self.root])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("updated on main", self.sent[0])

    def test_current_does_nothing_and_stays_silent(self):
        git = FakeGit(local="a" * 40, remote="a" * 40)
        action = self_update.run(self.root, self.cfg, git=git, redeploy=self._fail_redeploy,
                                 log=lambda *_: None)
        self.assertEqual(action, self_update.CURRENT)
        self.assertFalse(git.merged())
        self.assertEqual(self.sent, [])

    def test_diverged_notifies_once_and_never_merges(self):
        def make():
            return FakeGit(branch="main", local="a" * 40, remote="b" * 40, ancestor=False)
        self_update.run(self.root, self.cfg, git=make(), redeploy=self._fail_redeploy, log=lambda *_: None)
        self_update.run(self.root, self.cfg, git=make(), redeploy=self._fail_redeploy, log=lambda *_: None)
        self.assertEqual(len(self.sent), 1)          # deduped across cycles
        self.assertIn("diverged", self.sent[0])

    def test_a_feature_branch_is_left_untouched(self):
        git = FakeGit(branch="my-pr", local="a" * 40, remote="b" * 40)
        action = self_update.run(self.root, self.cfg, git=git, redeploy=self._fail_redeploy,
                                 log=lambda *_: None)
        self.assertEqual(action, self_update.SKIP_BRANCH)
        self.assertFalse(git.merged())
        self.assertEqual(self.sent, [])

    def test_dry_run_reports_but_changes_nothing(self):
        git = FakeGit(local="a" * 40, remote="b" * 40, subjects="b1 x")
        action = self_update.run(self.root, self.cfg, git=git, dry_run=True,
                                 redeploy=self._fail_redeploy, log=lambda *_: None)
        self.assertEqual(action, self_update.UPDATE)
        self.assertFalse(git.merged())
        self.assertEqual(self.sent, [])

    def _fail_redeploy(self, root, log=print):
        raise AssertionError("redeploy must not run in this case")


# --- interest offers (discovery/offers.py) ------------------------------------

OFFER_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _days_ago(days, now=OFFER_NOW):
    return (now - timedelta(days=days)).isoformat(timespec="seconds")


def _candidate(**overrides):
    """A candidate as the contract-v2 artifact ships it -- the gaming cluster
    the measured corpus is full of and interests.json covers nowhere."""
    base = {
        "kind": "new",
        "key": "binding-of-isaac-progression",
        "title": "Binding of Isaac progression and unlocks",
        "description": "Run strategy, unlock paths and mod-scene news for Isaac.",
        "positive_signals": ["binding of isaac", "isaac unlocks", "repentance"],
        "negative_signals": ["isaac newton"],
        "suggested_min_score": 0.72,
        "sources": ["web_search"],
        "related_keys": [],
        "evidence": [
            {"date": "2026-07-30", "quote": "Isaac Best Challenge Unlocks", "lang": "en",
             "depth": 0.7, "conversation_id": "chatgpt:8842"},
            {"date": "2026-06-11", "quote": "which Azazel run is fastest", "lang": "en",
             "depth": 0.6, "conversation_id": "chatgpt:8611"},
        ],
        "durability": {"n_convs": 6, "active_months": 3, "span_days": 120, "recency_days": 19},
        "expected_yield": 0.7,
        "similarity_to_existing": [{"key": "nbis-nebius", "sim": 0.05}],
    }
    base.update(overrides)
    return base


# Distinct enough to survive near-duplicate suppression: every candidate's
# signal tokens are disjoint from every other's. `_candidate(key=...)` alone is
# NOT distinct -- it keeps the shared title and signals, which is exactly the
# near-duplicate case, so a test that wants N separate offers must use this.
_DISTINCT_THEMES = [
    ("orexin-wakefulness", "Orexin wakefulness", ["orexin", "hypocretin"]),
    ("perovskite-tandem", "Perovskite tandem cells", ["perovskite", "tandem"]),
    ("mitochondrial-uncoupling", "Mitochondrial uncoupling", ["mitochondria", "uncoupling"]),
    ("harbour-dredging", "Harbour dredging", ["dredging", "harbour"]),
    ("basque-linguistics", "Basque linguistics", ["basque", "euskara"]),
    ("lattice-cryptography", "Lattice cryptography", ["lattice", "kyber"]),
    ("sourdough-fermentation", "Sourdough fermentation", ["sourdough", "levain"]),
    ("tokamak-confinement", "Tokamak confinement", ["tokamak", "stellarator"]),
    ("gregorian-chant", "Gregorian chant", ["gregorian", "plainsong"]),
    ("mangrove-restoration", "Mangrove restoration", ["mangrove", "estuary"]),
    ("cuneiform-tablets", "Cuneiform tablets", ["cuneiform", "sumerian"]),
    ("hydrofoil-ferries", "Hydrofoil ferries", ["hydrofoil", "catamaran"]),
    ("volcanic-tephra", "Volcanic tephra", ["tephra", "pyroclastic"]),
    ("bookbinding-vellum", "Bookbinding vellum", ["bookbinding", "vellum"]),
    ("axolotl-regeneration", "Axolotl regeneration", ["axolotl", "blastema"]),
    ("permafrost-methane", "Permafrost methane", ["permafrost", "clathrate"]),
]


def _distinct_candidates(n, **overrides):
    """`n` candidates that share no signal tokens, each comfortably over the
    floors, in descending score order (recency_days is the tie-breaker)."""
    assert n <= len(_DISTINCT_THEMES), "add more themes to _DISTINCT_THEMES"
    out = []
    for i, (key, title, signals) in enumerate(_DISTINCT_THEMES[:n]):
        base = dict(
            key=key, title=title, positive_signals=signals, negative_signals=[],
            description=f"{title} -- research thread.",
            durability={"n_convs": 8, "active_months": 4, "recency_days": i},
            evidence=[{"date": "2026-08-01", "quote": f"{title} question", "lang": "en",
                       "depth": 0.7, "conversation_id": f"chatgpt:{9000 + i}"}],
            similarity_to_existing=[],
        )
        base.update(overrides)
        out.append(_candidate(**base))
    return out


def _artifact(candidates, version=2, generated_at="2026-08-17T00:00:00Z"):
    return {
        "contract_version": version,
        "generated_at": generated_at,
        "window_days": 365,
        "conversation_count": 263,
        "sources": {"claude": 21, "chatgpt": 242},
        "topics": [],
        "candidates": candidates,
    }


class OfferRankingTests(unittest.TestCase):
    """score_candidate()/passes_floors()/rank() are pure -- no DB, no clock of
    their own, and never a model call. The model rates (expected_yield,
    similarity); code ranks."""

    def test_every_term_is_computed_and_weighted_as_designed(self):
        score, terms = offers.score_candidate(
            _candidate(durability={"n_convs": 8, "active_months": 4, "recency_days": 0},
                       expected_yield=1.0,
                       similarity_to_existing=[]),
            now=OFFER_NOW,
        )
        # Saturated evidence, full recurrence, today's recency, nothing like
        # it in the interest set, and the model expecting weekly items.
        self.assertEqual(terms["evidence_strength"], 1.0)
        self.assertEqual(terms["recurrence"], 1.0)
        self.assertEqual(terms["recency"], 1.0)
        self.assertEqual(terms["novelty"], 1.0)
        self.assertEqual(terms["expected_yield"], 1.0)
        self.assertEqual(score, 1.0)

    def test_recency_is_a_90_day_half_life(self):
        _score, terms = offers.score_candidate(
            _candidate(durability={"n_convs": 4, "active_months": 2, "recency_days": 90}),
            now=OFFER_NOW,
        )
        self.assertAlmostEqual(terms["recency"], 0.5, places=6)

    def test_recency_falls_back_to_the_newest_evidence_date(self):
        candidate = _candidate(durability={"n_convs": 4, "active_months": 2})
        _score, terms = offers.score_candidate(candidate, now=OFFER_NOW)
        # Newest quote is 2026-07-30, i.e. 19 days before OFFER_NOW.
        self.assertAlmostEqual(terms["recency_days"], 19.5, places=1)

    def test_novelty_is_the_inverse_of_the_producers_similarity(self):
        _score, terms = offers.score_candidate(
            _candidate(similarity_to_existing=[{"key": "a", "sim": 0.2},
                                               {"key": "b", "sim": 0.65}]),
            now=OFFER_NOW,
        )
        self.assertAlmostEqual(terms["novelty"], 0.35, places=6)

    def test_a_one_off_errand_never_clears_the_durability_gate(self):
        # Five AirPlay support tickets in one afternoon: recent, repeated,
        # and exactly what this system must not start following.
        errand = _candidate(
            key="airplay-troubleshooting", title="AirPlay troubleshooting",
            durability={"n_convs": 5, "active_months": 1, "recency_days": 1},
            evidence=[{"date": "2026-08-17", "quote": "airplay keeps dropping",
                       "lang": "en", "depth": 0.2}],
            expected_yield=0.9,
        )
        score, _terms = offers.score_candidate(errand, now=OFFER_NOW)
        ok, why = offers.passes_floors(errand, score, offers.DEFAULT_RULES)
        self.assertFalse(ok)
        self.assertIn("durability gate", why)

    def test_a_deep_two_conversation_dive_qualifies(self):
        dive = _candidate(
            durability={"n_convs": 2, "active_months": 1, "recency_days": 3},
            evidence=[{"date": "2026-08-15", "quote": "walk me through the proof",
                       "lang": "en", "depth": 0.8}],
        )
        score, _terms = offers.score_candidate(dive, now=OFFER_NOW)
        ok, why = offers.passes_floors(dive, score, offers.DEFAULT_RULES)
        self.assertTrue(ok, why)

    def test_a_revive_offer_must_clear_twice_the_evidence_bar(self):
        # Exactly the durability that qualifies a new offer is not enough to
        # bring a theme the owner already retired back -- anti-flapping, the
        # same shape as interest_state's re-entry multiplier.
        durability = {"n_convs": 3, "active_months": 2, "recency_days": 5}
        fresh = _candidate(durability=durability)
        revived = _candidate(kind="revive", durability=durability)
        score, _ = offers.score_candidate(fresh, now=OFFER_NOW)
        self.assertTrue(offers.passes_floors(fresh, score, offers.DEFAULT_RULES)[0])
        self.assertFalse(offers.passes_floors(revived, score, offers.DEFAULT_RULES)[0])

        strong = _candidate(kind="revive",
                            durability={"n_convs": 6, "active_months": 2, "recency_days": 5})
        score, _ = offers.score_candidate(strong, now=OFFER_NOW)
        self.assertTrue(offers.passes_floors(strong, score, offers.DEFAULT_RULES)[0])

    def test_a_weak_score_is_refused_even_with_good_durability(self):
        stale = _candidate(
            durability={"n_convs": 3, "active_months": 2, "recency_days": 900},
            expected_yield=0.0, similarity_to_existing=[{"key": "a", "sim": 0.6}],
        )
        score, _terms = offers.score_candidate(stale, now=OFFER_NOW)
        ok, why = offers.passes_floors(stale, score, offers.DEFAULT_RULES)
        self.assertFalse(ok)
        self.assertIn("below floor", why)

    def test_rank_caps_the_run_at_the_inbox_target_and_reserves_the_serendipity_slot(self):
        scored = [dict(_candidate(key=f"k{i}"), score=0.9 - i * 0.01) for i in range(14)]
        scored.append(dict(_candidate(key="wildcard", exploratory=True), score=0.46))
        chosen = offers.rank(scored, offers.DEFAULT_RULES)
        keys = [c["key"] for c in chosen]
        # The cap is the inbox target -- the owner asked for ten suggestions,
        # so ten is what a full run fills.
        self.assertEqual(len(keys), offers.DEFAULT_RULES.target_inbox_size)
        self.assertIn("wildcard", keys)                     # the reserved slot
        self.assertEqual(keys[:9], [f"k{i}" for i in range(9)])

    def test_rank_is_deterministic_on_ties(self):
        scored = [dict(_candidate(key=k), score=0.6) for k in ("zeta", "alpha", "mid")]
        self.assertEqual(
            [c["key"] for c in offers.rank(scored, offers.DEFAULT_RULES)],
            ["alpha", "mid", "zeta"],
        )

    def test_normalize_key_folds_plurals_stopwords_and_order(self):
        self.assertEqual(
            offers.normalize_key("Personal Knowledge Graphs"),
            offers.normalize_key("graph-personal-knowledge"),
        )

    def test_normalize_key_keeps_hebrew_instead_of_emptying_it(self):
        # An ASCII-only slug rule would normalize every Hebrew key to "" and
        # collapse unrelated interests onto each other.
        self.assertNotEqual(offers.normalize_key("\u05d6\u05d9\u05db\u05e8\u05d5\u05df \u05e2\u05d1\u05d5\u05d3\u05d4"), "")
        self.assertNotEqual(
            offers.normalize_key("\u05d6\u05d9\u05db\u05e8\u05d5\u05df \u05e2\u05d1\u05d5\u05d3\u05d4"),
            offers.normalize_key("\u05e9\u05d9\u05e0\u05d4 \u05d5\u05e2\u05d9\u05e8\u05e0\u05d5\u05ea"),
        )

    def test_signal_tokens_cover_both_languages(self):
        tokens = offers.signal_tokens("cognitive load", ["\u05d6\u05d9\u05db\u05e8\u05d5\u05df \u05e2\u05d1\u05d5\u05d3\u05d4"])
        self.assertIn("cognitive", tokens)
        self.assertIn("\u05d6\u05d9\u05db\u05e8\u05d5\u05df", tokens)


class OfferStoreTests(unittest.TestCase):
    """The DDL, the importer's idempotency, and the three dedup layers."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write_artifact(self, data, name="interest_candidates.json"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        return path

    def _seed_interest(self, key, title="T", signals=None, active=1, min_score=0.7):
        db.upsert_interest(self.conn, Interest(
            key=key, title=title, description="", positive_signals=signals or [],
            min_score=min_score, sources=["web_search"],
        ))
        if not active:
            self.conn.execute("UPDATE interests SET active = 0 WHERE key = ?", (key,))
            self.conn.commit()

    def test_migration_is_additive_and_idempotent(self):
        db.init(self.conn)   # a second init on an existing DB must be a no-op
        tables = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        self.assertIn("interest_offers", tables)
        self.assertIn("offer_events", tables)
        self.assertIn("interest_edges", tables)
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(interests)")}
        self.assertIn("parent_key", columns)
        self.assertIn("lifecycle", columns)

    def test_existing_interest_rows_default_to_the_active_lifecycle(self):
        self._seed_interest("nbis-nebius")
        self.assertEqual(offers.interest_lifecycle(self.conn, "nbis-nebius"),
                         {"lifecycle": "active", "active": True})

    def test_importing_the_same_artifact_twice_creates_offers_exactly_once(self):
        path = self._write_artifact(_artifact([_candidate()]))
        first = offers.import_artifact(self.conn, path, now=OFFER_NOW)
        second = offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(first["offered"], 1)
        self.assertEqual(second["offered"], 0)
        self.assertEqual(second["error"], "already imported")
        self.assertEqual(len(offers.list_offers(self.conn)), 1)
        # ...and one propose + one offer event, not two of each.
        events = offers.offer_events(self.conn, "binding-of-isaac-progression")
        self.assertEqual([e["action"] for e in events], ["propose", "offer"])

    def test_a_rewritten_but_identical_artifact_is_still_one_import(self):
        # The producer copies the file weekly; identical bytes under a new
        # name (or mtime) must not re-offer.
        data = _artifact([_candidate()])
        offers.import_artifact(self.conn, self._write_artifact(data), now=OFFER_NOW)
        again = offers.import_artifact(
            self.conn, self._write_artifact(data, name="copy.json"), now=OFFER_NOW
        )
        self.assertEqual(again["error"], "already imported")
        self.assertEqual(len(offers.list_offers(self.conn)), 1)

    def test_an_offer_carries_its_quotes_and_conversation_ids(self):
        path = self._write_artifact(_artifact([_candidate()]))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        offer = offers.get_offer(self.conn, "binding-of-isaac-progression")
        self.assertEqual(offer["status"], "offered")
        self.assertEqual([e["quote"] for e in offer["evidence"]],
                         ["Isaac Best Challenge Unlocks", "which Azazel run is fastest"])
        self.assertEqual(offer["source_conversations"], ["chatgpt:8611", "chatgpt:8842"])
        self.assertEqual(offer["artifact_sha256"], offers.artifact_sha256(path))
        self.assertEqual(offer["generated_at"], "2026-08-17T00:00:00Z")
        self.assertEqual(offer["score_terms"]["weights"]["evidence_strength"], 0.30)

    def test_hebrew_quotes_survive_the_round_trip_unescaped(self):
        hebrew = _candidate(
            key="working-memory-hebrew", title="Working memory",
            positive_signals=["working memory"],
            evidence=[{"date": "2026-08-01",
                       "quote": "\u05d0\u05d9\u05da \u05dc\u05e9\u05e4\u05e8 \u05d0\u05ea \u05d6\u05d9\u05db\u05e8\u05d5\u05df \u05d4\u05e2\u05d1\u05d5\u05d3\u05d4 \u05e9\u05dc\u05d9",
                       "depth": 0.8, "conversation_id": "chatgpt:7001"}],
            durability={"n_convs": 5, "active_months": 3, "recency_days": 10},
        )
        path = self._write_artifact(_artifact([hebrew]))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        offer = offers.get_offer(self.conn, "working-memory-hebrew")
        self.assertEqual(offer["evidence"][0]["quote"],
                         "\u05d0\u05d9\u05da \u05dc\u05e9\u05e4\u05e8 \u05d0\u05ea \u05d6\u05d9\u05db\u05e8\u05d5\u05df \u05d4\u05e2\u05d1\u05d5\u05d3\u05d4 \u05e9\u05dc\u05d9")
        self.assertEqual(offer["evidence"][0]["lang"], "he")   # inferred, not shipped
        # Stored as real characters, not \uXXXX escapes -- the DB stays
        # readable by eye and by LIKE.
        raw = self.conn.execute(
            "SELECT evidence FROM interest_offers WHERE key = ?", ("working-memory-hebrew",)
        ).fetchone()["evidence"]
        self.assertIn("\u05d6\u05d9\u05db\u05e8\u05d5\u05df", raw)

    def test_a_paraphrase_of_an_existing_interest_is_dropped_semantically(self):
        # The exact-hash class of bug, at the interest level: nothing about
        # this candidate's key or tokens matches, but the producer says it is
        # the same thing in another language.
        self._seed_interest("cognitive-load-working-memory", title="Cognitive load",
                            signals=["cognitive load"])
        paraphrase = _candidate(
            key="zikaron-avoda", title="\u05d6\u05d9\u05db\u05e8\u05d5\u05df \u05e2\u05d1\u05d5\u05d3\u05d4",
            positive_signals=["\u05d6\u05d9\u05db\u05e8\u05d5\u05df \u05e2\u05d1\u05d5\u05d3\u05d4"],
            similarity_to_existing=[{"key": "cognitive-load-working-memory", "sim": 0.86}],
        )
        path = self._write_artifact(_artifact([paraphrase]))
        summary = offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["skipped_dedup"], 1)
        self.assertEqual(summary["offered"], 0)
        self.assertIn("semantic similarity", summary["reasons"][0]["why"])

    def test_similarity_to_a_since_removed_interest_no_longer_suppresses(self):
        # The producer scored this candidate .86 similar to an interest that
        # has since been removed. Judging it against an interest the owner no
        # longer follows would silently keep the theme out forever.
        paraphrase = _candidate(
            key="zikaron-avoda", title="Working memory",
            similarity_to_existing=[{"key": "cognitive-load-working-memory", "sim": 0.86}],
        )
        path = self._write_artifact(_artifact([paraphrase]))
        summary = offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["offered"], 1)
        # ...but the producer's own reading is still stored, for provenance.
        self.assertEqual(
            offers.get_offer(self.conn, "zikaron-avoda")["similarity"],
            [{"key": "cognitive-load-working-memory", "sim": 0.86}],
        )

    def test_a_candidate_that_normalizes_onto_an_existing_key_is_dropped(self):
        self._seed_interest("personal-knowledge-graphs")
        path = self._write_artifact(_artifact([
            _candidate(key="Personal Knowledge Graph", similarity_to_existing=[])
        ]))
        summary = offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["skipped_dedup"], 1)
        self.assertIn("normalizes onto", summary["reasons"][0]["why"])

    def test_a_heavy_signal_overlap_becomes_evidence_not_an_offer(self):
        self._seed_interest(
            "narcolepsy-eds", title="Narcolepsy and excessive daytime sleepiness",
            signals=["narcolepsy", "modafinil", "orexin", "sleepiness"],
        )
        overlapping = _candidate(
            key="modafinil-dosing", title="Modafinil dosing",
            positive_signals=["modafinil", "orexin"],
            similarity_to_existing=[{"key": "narcolepsy-eds", "sim": 0.4}],
        )
        path = self._write_artifact(_artifact([overlapping]))
        summary = offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["attached"], 1)
        self.assertEqual(summary["offered"], 0)
        events = [e for e in db.interest_events(self.conn, "narcolepsy-eds")
                  if e["action"] == "offer_evidence"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["evidence"]["candidate_key"], "modafinil-dosing")
        self.assertEqual(events[0]["evidence"]["source_conversations"],
                         ["chatgpt:8611", "chatgpt:8842"])

    def test_a_rejected_theme_is_not_offered_again(self):
        path = self._write_artifact(_artifact([_candidate()]))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        offers.reject(self.conn, "binding-of-isaac-progression", note="not now", now=OFFER_NOW)
        # A later artifact proposes the same theme under a different key.
        again = self._write_artifact(_artifact(
            [_candidate(key="isaac-repentance-unlocks")], generated_at="2026-09-01T00:00:00Z"
        ))
        summary = offers.import_artifact(self.conn, again, now=OFFER_NOW + timedelta(days=14))
        self.assertEqual(summary["skipped_blocked"], 1)
        self.assertEqual(summary["offered"], 0)

    def test_the_block_expires_after_the_window(self):
        path = self._write_artifact(_artifact([_candidate()]))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        offers.reject(self.conn, "binding-of-isaac-progression", now=OFFER_NOW)
        blocked = offers.blocked_offer_keys(
            self.conn, now=OFFER_NOW + timedelta(days=181)
        )
        self.assertEqual(blocked, set())

    def test_interests_json_blocked_terms_are_honoured_and_appended_to(self):
        interests_path = os.path.join(self.tmp.name, "interests.json")
        with open(interests_path, "w", encoding="utf-8") as fh:
            json.dump({"interests": [], "blocked_derived_terms": ["crypto"]}, fh)
        blocked_by_file = _candidate(key="crypto-market-moves", title="Crypto market moves",
                                     positive_signals=["crypto"])
        path = self._write_artifact(_artifact([blocked_by_file]))
        summary = offers.import_artifact(
            self.conn, path, interests_path=interests_path, now=OFFER_NOW
        )
        self.assertEqual(summary["skipped_blocked"], 1)

        # And a rejection writes back into the same list.
        other = self._write_artifact(_artifact([_candidate()], generated_at="2026-09-02T00:00:00Z"))
        offers.import_artifact(self.conn, other, now=OFFER_NOW)
        result = offers.reject(self.conn, "binding-of-isaac-progression",
                               interests_path=interests_path, now=OFFER_NOW)
        self.assertIn("binding-of-isaac-progression", result["blocked_terms_written"])
        self.assertEqual(interests.load_blocked(interests_path)[0], "crypto")
        self.assertIn("binding-of-isaac-progression", interests.load_blocked(interests_path))

    def test_a_run_fills_the_inbox_to_the_target_and_stops(self):
        # Thirteen candidates with genuinely distinct signals -- distinct
        # matters, because near-duplicates are suppressed rather than counted
        # (see the near-duplicate tests below).
        path = self._write_artifact(_artifact(_distinct_candidates(13)))
        summary = offers.import_artifact(self.conn, path, now=OFFER_NOW)
        target = offers.DEFAULT_RULES.target_inbox_size
        self.assertEqual(summary["offered"], target)
        self.assertEqual(summary["not_selected"], 13 - target)
        self.assertEqual(len(offers.inbox(self.conn)), target)
        self.assertEqual(offers.live_offer_count(self.conn), target)
        # The ones that did not make it say so, and stay in the artifact.
        outranked = [r for r in summary["reasons"] if "outranked" in r["why"]]
        self.assertEqual(len(outranked), 13 - target)

    def test_a_v1_artifact_produces_no_offers_and_is_not_re_read(self):
        path = self._write_artifact(
            {"contract_version": 1, "generated_at": "x", "topics": [{"key": "orexin"}]}
        )
        summary = offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["offered"], 0)
        self.assertIn("no candidates", summary["error"])
        self.assertEqual(
            offers.import_artifact(self.conn, path, now=OFFER_NOW)["error"], "already imported"
        )

    def test_a_missing_or_malformed_artifact_is_fail_soft(self):
        missing = os.path.join(self.tmp.name, "nope.json")
        summary = offers.import_artifact(self.conn, missing, now=OFFER_NOW)
        self.assertIn("unreadable", summary["error"])

        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with contextlib.redirect_stderr(io.StringIO()):
            summary = offers.import_artifact(self.conn, bad, now=OFFER_NOW)
        self.assertIn("malformed", summary["error"])
        self.assertEqual(offers.list_offers(self.conn), [])

    def test_a_candidate_with_no_evidence_still_imports_but_shows_no_quotes(self):
        bare = _candidate(key="bare-theme", evidence=[])
        path = self._write_artifact(_artifact([bare]))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        offer = offers.get_offer(self.conn, "bare-theme")
        self.assertEqual(offer["evidence"], [])
        self.assertEqual(offer["source_conversations"], [])

    def test_a_legacy_0_100_suggested_bar_is_rescaled(self):
        path = self._write_artifact(_artifact([_candidate(suggested_min_score=75)]))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(
            offers.get_offer(self.conn, "binding-of-isaac-progression")["suggested_min_score"],
            0.75,
        )


class OfferLifecycleTests(unittest.TestCase):
    """The offer half of the state machine: every transition is legal-listed
    and logged, and nothing skips the inbox."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "a.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(_artifact([_candidate()]), fh)
        offers.import_artifact(self.conn, self.path, now=OFFER_NOW)
        self.key = "binding-of-isaac-progression"

    def test_accept_records_the_decision_and_hands_back_a_json_entry(self):
        result = offers.accept(self.conn, self.key, note="yes please", now=OFFER_NOW)
        entry = result["entry"]
        self.assertEqual(entry["key"], self.key)
        self.assertEqual(entry["min_score"], 0.72)
        self.assertEqual(entry["sources"], ["web_search"])
        self.assertEqual(entry["offered_by"]["source_conversations"],
                         ["chatgpt:8611", "chatgpt:8842"])
        self.assertEqual(offers.get_offer(self.conn, self.key)["status"], "accepted")
        self.assertEqual(offers.get_offer(self.conn, self.key)["decided_note"], "yes please")

    def test_accept_with_edits_applies_them_and_keeps_the_diff(self):
        offers.accept(
            self.conn, self.key, now=OFFER_NOW,
            edits={"title": "Isaac", "min_score": 0.65,
                   "positive_signals": ["isaac", "repentance"]},
        )
        offer = offers.get_offer(self.conn, self.key)
        self.assertEqual(offer["title"], "Isaac")
        self.assertEqual(offer["suggested_min_score"], 0.65)
        self.assertEqual(offer["positive_signals"], ["isaac", "repentance"])
        accept_event = [e for e in offers.offer_events(self.conn, self.key)
                        if e["action"] == "accept"][0]
        self.assertEqual(accept_event["detail"]["edits"]["min_score"], 0.65)

    def test_an_unsupported_edit_field_is_refused(self):
        with self.assertRaises(offers.OfferError):
            offers.accept(self.conn, self.key, edits={"key": "something-else"})

    def test_accept_can_close_the_loop_through_an_injected_sync(self):
        # PR I/J own the interests.json + DB write; offers.py only calls it.
        written = {}

        def fake_sync(entry):
            written.update(entry)
            db.upsert_interest(self.conn, Interest(
                key=entry["key"], title=entry["title"], description=entry["description"],
                positive_signals=entry["positive_signals"], min_score=entry["min_score"],
                sources=entry["sources"],
            ))

        result = offers.accept(self.conn, self.key, sync=fake_sync, now=OFFER_NOW)
        self.assertEqual(written["key"], self.key)
        self.assertEqual(result["activated"]["lifecycle"], "active")
        self.assertEqual(offers.interest_lifecycle(self.conn, self.key),
                         {"lifecycle": "active", "active": True})
        chain = [e["action"] for e in db.interest_events(self.conn, self.key)]
        self.assertIn("offer_accepted", chain)

    def test_activate_refuses_until_the_interest_row_exists(self):
        offers.accept(self.conn, self.key, now=OFFER_NOW)
        with self.assertRaises(offers.OfferError):
            offers.activate(self.conn, self.key)

    def test_an_offer_cannot_skip_the_inbox_or_be_decided_twice(self):
        fresh = offers.insert_offer(self.conn, {"key": "k2", "title": "K2"}, now=OFFER_NOW)
        self.assertEqual(fresh["status"], "proposed")
        with self.assertRaises(offers.InvalidTransition):
            offers.accept(self.conn, "k2")          # proposed -> accepted is not a transition
        offers.accept(self.conn, self.key, now=OFFER_NOW)
        with self.assertRaises(offers.InvalidTransition):
            offers.reject(self.conn, self.key)      # accepted is final

    def test_deciding_an_unknown_offer_raises_unknown_offer(self):
        with self.assertRaises(offers.UnknownOffer):
            offers.snooze(self.conn, "no-such-offer")

    def test_snooze_sleeps_then_wakes_back_into_the_inbox(self):
        offers.snooze(self.conn, self.key, now=OFFER_NOW)
        self.assertEqual(offers.inbox(self.conn), [])
        summary = offers.sweep(self.conn, now=OFFER_NOW + timedelta(days=31))
        self.assertEqual(summary["woken"], 1)
        self.assertEqual([o["key"] for o in offers.inbox(self.conn)], [self.key])
        self.assertIsNone(offers.get_offer(self.conn, self.key)["snoozed_until"])

    def test_a_snoozed_offer_stays_asleep_until_its_timer(self):
        offers.snooze(self.conn, self.key, now=OFFER_NOW)
        summary = offers.sweep(self.conn, now=OFFER_NOW + timedelta(days=29))
        self.assertEqual(summary["woken"], 0)

    def test_an_undecided_offer_expires_after_45_days(self):
        summary = offers.sweep(self.conn, now=OFFER_NOW + timedelta(days=45))
        self.assertEqual(summary["expired"], 1)
        self.assertEqual(offers.get_offer(self.conn, self.key)["status"], "expired")
        events = [e["action"] for e in offers.offer_events(self.conn, self.key)]
        self.assertEqual(events[-1], "expire")

    def test_every_transition_is_on_the_append_only_log(self):
        offers.snooze(self.conn, self.key, now=OFFER_NOW)
        offers.sweep(self.conn, now=OFFER_NOW + timedelta(days=31))
        offers.accept(self.conn, self.key, now=OFFER_NOW + timedelta(days=31))
        chain = [(e["actor"], e["action"], e["from_status"], e["to_status"])
                 for e in offers.offer_events(self.conn, self.key)]
        self.assertEqual(chain, [
            ("importer", "propose", None, "proposed"),
            ("importer", "offer", "proposed", "offered"),
            ("owner_ui", "snooze", "offered", "snoozed"),
            ("timer", "wake", "snoozed", "offered"),
            ("owner_ui", "accept", "offered", "accepted"),
        ])

    def test_offer_detail_carries_everything_the_inbox_renders(self):
        detail = offers.offer_detail(self.conn, self.key)
        for field in ("evidence", "source_conversations", "score_terms", "durability",
                      "similarity", "related_keys", "events"):
            self.assertIn(field, detail)
        self.assertIsNone(offers.offer_detail(self.conn, "nope"))


class OfferSweepTests(unittest.TestCase):
    """The interest half: decay at 30 days, a reversible auto-pause at 45,
    and two refusals to judge silence that isn't the interest's fault."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def _interest(self, key="speculative-fiction-ideas", min_score=0.78):
        db.upsert_interest(self.conn, Interest(
            key=key, title=key, description="", positive_signals=[key],
            min_score=min_score, sources=["web_search"],
        ))
        return self.conn.execute(
            "SELECT id FROM interests WHERE key = ?", (key,)
        ).fetchone()["id"]

    def _score(self, interest_id, *, final_score, created_at, key="k"):
        cur = self.conn.execute(
            "INSERT INTO candidate_items (source, type, title, url, dedup_key, url_hash,"
            " title_hash, origin_interest, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("web_search", "article", "t", f"https://e.com/{key}", key, key, key,
             self.conn.execute("SELECT key FROM interests WHERE id = ?",
                               (interest_id,)).fetchone()["key"], created_at),
        )
        item_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO scores (item_id, interest_id, personal_relevance, novelty, depth,"
            " specificity, importance, surprise, final_score, confidence, created_at)"
            " VALUES (?,?,0.5,0.5,0.5,0.5,0.5,0.5,?,0.8,?)",
            (item_id, interest_id, final_score, created_at),
        )
        self.conn.commit()
        return item_id

    def _dead_interest(self, silent_days, key="speculative-fiction-ideas"):
        """The measured dead-weight shape: items collected and scored, none of
        them ever above the bar."""
        interest_id = self._interest(key)
        for i in range(6):
            self._score(interest_id, final_score=0.4,
                        created_at=_days_ago(silent_days + i), key=f"{key}-old-{i}")
        return interest_id

    def _keep_pipeline_alive(self, key="nbis-nebius"):
        """A second, healthy interest scoring items right now -- otherwise the
        sweep correctly refuses to judge anything."""
        other = self._interest(key, min_score=0.6)
        self._score(other, final_score=0.9, created_at=_days_ago(0), key="fresh")
        return other

    def test_thirty_silent_days_flag_an_interest_and_raise_a_retirement_offer(self):
        self._dead_interest(31)
        self._keep_pipeline_alive()
        summary = offers.sweep(self.conn, now=OFFER_NOW)
        self.assertEqual(summary["decaying"], 1)
        self.assertEqual(summary["retire_offers"], 1)
        self.assertEqual(
            offers.interest_lifecycle(self.conn, "speculative-fiction-ideas"),
            {"lifecycle": "decaying", "active": True},   # on notice, still collecting
        )
        offer = offers.get_offer(self.conn, "retire:speculative-fiction-ideas")
        self.assertEqual(offer["status"], "offered")
        self.assertEqual(offer["kind"], "retire")
        self.assertEqual(offer["score_terms"]["above_bar"], 0)
        self.assertEqual(offer["score_terms"]["scored"], 6)

    def test_forty_five_silent_days_auto_pause_reversibly_and_announce_it(self):
        self._dead_interest(46)
        self._keep_pipeline_alive()
        said = []
        summary = offers.sweep(self.conn, now=OFFER_NOW, announce=said.append)
        self.assertEqual(summary["auto_paused"], 1)
        self.assertEqual(
            offers.interest_lifecycle(self.conn, "speculative-fiction-ideas"),
            {"lifecycle": "paused", "active": False},
        )
        # It announces itself, and the announcement carries the way back.
        self.assertEqual(len(said), 1)
        self.assertIn("speculative-fiction-ideas", said[0])
        self.assertIn("--undo", said[0])
        self.assertEqual(summary["announcements"][0]["kind"], "auto_pause")
        event = [e for e in db.interest_events(self.conn, "speculative-fiction-ideas")
                 if e["action"] == "auto_pause"][0]
        self.assertEqual(event["evidence"]["to_lifecycle"], "paused")
        self.assertGreaterEqual(event["evidence"]["silent_days"], 45)

    def test_a_failing_announcement_never_fails_the_sweep(self):
        self._dead_interest(46)
        self._keep_pipeline_alive()

        def broken(_text):
            raise RuntimeError("telegram down")

        summary = offers.sweep(self.conn, now=OFFER_NOW, announce=broken)
        self.assertEqual(summary["auto_paused"], 1)
        self.assertIn("announce_error", summary["announcements"][0])

    def test_undo_brings_it_back_closes_the_retire_offer_and_resets_the_clock(self):
        self._dead_interest(46)
        self._keep_pipeline_alive()
        offers.sweep(self.conn, now=OFFER_NOW)
        result = offers.undo_auto_pause(self.conn, "speculative-fiction-ideas", now=OFFER_NOW)
        self.assertEqual(result["lifecycle"], "active")
        self.assertEqual(
            offers.interest_lifecycle(self.conn, "speculative-fiction-ideas"),
            {"lifecycle": "active", "active": True},
        )
        self.assertEqual(
            offers.get_offer(self.conn, "retire:speculative-fiction-ideas")["status"], "rejected"
        )
        # The undo is the new baseline: the very next sweep must not re-pause
        # the interest it was just told to keep.
        again = offers.sweep(self.conn, now=OFFER_NOW + timedelta(days=1))
        self.assertEqual(again["auto_paused"], 0)
        self.assertEqual(again["decaying"], 0)

    def test_undo_on_an_interest_that_was_never_paused_is_refused(self):
        self._interest("nbis-nebius")
        with self.assertRaises(offers.InvalidTransition):
            offers.undo_auto_pause(self.conn, "nbis-nebius")

    def test_a_paused_pipeline_never_looks_like_a_dead_interest(self):
        # The live failure mode: the appliance was paused for days. Nothing
        # scored, so nothing may be judged silent.
        self._dead_interest(60)
        summary = offers.sweep(self.conn, now=OFFER_NOW)
        self.assertEqual(summary["auto_paused"], 0)
        self.assertEqual(summary["decaying"], 0)
        self.assertIn("not evaluated", summary["skipped"])
        self.assertEqual(
            offers.interest_lifecycle(self.conn, "speculative-fiction-ideas")["lifecycle"],
            "active",
        )

    def test_an_interest_the_pipeline_barely_touched_is_never_paused(self):
        interest_id = self._interest("brand-new-interest")
        self._score(interest_id, final_score=0.3, created_at=_days_ago(60), key="only-one")
        self._keep_pipeline_alive()
        summary = offers.sweep(self.conn, now=OFFER_NOW)
        self.assertEqual(summary["auto_paused"], 0)
        self.assertEqual(summary["decaying"], 0)

    def test_an_above_bar_item_returning_restores_a_decaying_interest(self):
        interest_id = self._dead_interest(31)
        self._keep_pipeline_alive()
        offers.sweep(self.conn, now=OFFER_NOW)
        self.assertEqual(
            offers.interest_lifecycle(self.conn, "speculative-fiction-ideas")["lifecycle"],
            "decaying",
        )
        self._score(interest_id, final_score=0.95, created_at=_days_ago(0), key="hit")
        summary = offers.sweep(self.conn, now=OFFER_NOW)
        self.assertEqual(summary["recovered"], 1)
        self.assertEqual(
            offers.interest_lifecycle(self.conn, "speculative-fiction-ideas")["lifecycle"],
            "active",
        )
        self.assertEqual(
            offers.get_offer(self.conn, "retire:speculative-fiction-ideas")["status"], "rejected"
        )

    def test_three_negative_reactions_retire_an_already_decaying_interest(self):
        interest_id = self._dead_interest(31)
        self._keep_pipeline_alive()
        offers.sweep(self.conn, now=OFFER_NOW)
        item_id = self._score(interest_id, final_score=0.2,
                              created_at=_days_ago(31), key="fb-item")
        for _ in range(3):
            db.add_feedback(self.conn, item_id, interest_id, "down")
        summary = offers.sweep(self.conn, now=OFFER_NOW)
        self.assertEqual(summary["retired"], 1)
        self.assertEqual(
            offers.interest_lifecycle(self.conn, "speculative-fiction-ideas"),
            {"lifecycle": "retired", "active": False},
        )

    def test_a_declined_retirement_offer_returns_only_after_its_cool_off(self):
        healthy = self._dead_interest(31) and self._keep_pipeline_alive()
        offers.sweep(self.conn, now=OFFER_NOW)
        offers.reject(self.conn, "retire:speculative-fiction-ideas",
                      note="keep it", now=OFFER_NOW)

        def sweep_at(days):
            # The pipeline has to be visibly alive at each instant, or the
            # sweep refuses to judge (see the paused-pipeline test).
            when = OFFER_NOW + timedelta(days=days)
            self._score(healthy, final_score=0.9,
                        created_at=when.isoformat(timespec="seconds"), key=f"fresh-{days}")
            return offers.sweep(self.conn, now=when)

        # Still dead a month later, but the owner already said no.
        self.assertEqual(sweep_at(30)["retire_offers"], 0)
        self.assertEqual(
            offers.get_offer(self.conn, "retire:speculative-fiction-ideas")["status"], "rejected"
        )
        later = sweep_at(91)
        self.assertEqual(later["retire_offers"], 1)
        self.assertEqual(
            offers.get_offer(self.conn, "retire:speculative-fiction-ideas")["status"], "offered"
        )

    def test_declining_a_retirement_offer_blocks_nothing(self):
        # "keep it" must not blocklist the interest's own key and words --
        # that would be the opposite of the answer, and would poison every
        # future candidate sharing a word with its title.
        self._dead_interest(31)
        self._keep_pipeline_alive()
        offers.sweep(self.conn, now=OFFER_NOW)
        result = offers.reject(self.conn, "retire:speculative-fiction-ideas",
                               note="keep it", now=OFFER_NOW)
        self.assertEqual(result["blocked_terms"], [])
        self.assertEqual(offers.blocked_offer_keys(self.conn, now=OFFER_NOW), set())

    def test_a_retired_interest_is_left_alone_by_later_sweeps(self):
        self._dead_interest(60)
        self._keep_pipeline_alive()
        offers.retire_interest(self.conn, "speculative-fiction-ideas", note="done")
        summary = offers.sweep(self.conn, now=OFFER_NOW)
        self.assertEqual(summary["auto_paused"], 0)
        self.assertEqual(summary["decaying"], 0)

    def test_a_healthy_interest_is_untouched(self):
        interest_id = self._interest("nbis-nebius", min_score=0.6)
        self._score(interest_id, final_score=0.9, created_at=_days_ago(1), key="a")
        for i in range(5):
            self._score(interest_id, final_score=0.5, created_at=_days_ago(i + 2), key=f"b{i}")
        summary = offers.sweep(self.conn, now=OFFER_NOW)
        self.assertEqual((summary["decaying"], summary["auto_paused"]), (0, 0))
        self.assertEqual(offers.interest_lifecycle(self.conn, "nbis-nebius")["lifecycle"],
                         "active")

    def test_derived_ladder_rows_are_left_to_interest_state(self):
        # Non-owner rows have their own ladder (discovery/interest_state.py);
        # this sweep must not touch them.
        db.upsert_derived_interest(self.conn, Interest(
            key="derived:orexin", title="orexin", description="Derived interest: orexin",
            positive_signals=["orexin"], min_score=0.8, sources=[], layer="inferred",
        ), {"source": "corpus"})
        derived_id = self.conn.execute(
            "SELECT id FROM interests WHERE key = ?", ("derived:orexin",)
        ).fetchone()["id"]
        for i in range(6):
            self._score(derived_id, final_score=0.3, created_at=_days_ago(60 + i), key=f"d{i}")
        self._keep_pipeline_alive()
        summary = offers.sweep(self.conn, now=OFFER_NOW)
        self.assertEqual((summary["decaying"], summary["auto_paused"]), (0, 0))

    def test_silence_days_is_none_for_an_interest_the_pipeline_never_worked_on(self):
        self._interest("untouched")
        self.assertIsNone(offers.silence_days(self.conn, "untouched", now=OFFER_NOW))


class OffersCLITests(unittest.TestCase):
    """`python -m app offers ...` -- offline end to end, no provider built."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.artifact = os.path.join(self.tmp.name, "interest_candidates.json")
        with open(self.artifact, "w", encoding="utf-8") as fh:
            json.dump(_artifact([_candidate()]), fh, ensure_ascii=False)

    def _main(self, *argv, env=None):
        from discovery.__main__ import main

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--db", self.db_path, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_import_then_list_then_why(self):
        code, out, _err = self._main("offers", "--import", self.artifact)
        self.assertEqual(code, 0)
        self.assertIn('"offered": 1', out)

        code, out, _err = self._main("offers")
        self.assertEqual(code, 0)
        self.assertIn("binding-of-isaac-progression", out)
        self.assertIn("2 quote(s) from 2 conversation(s)", out)

        code, out, _err = self._main("offers", "--why", "binding-of-isaac-progression")
        self.assertEqual(code, 0)
        self.assertIn("Isaac Best Challenge Unlocks", out)
        self.assertIn("chatgpt:8842", out)
        self.assertIn("evidence_strength", out)

    def test_import_defaults_to_the_configured_artifact_path(self):
        code, out, _err = self._main(
            "offers", "--import", env={"DISCOVERY_INTEREST_CANDIDATES": self.artifact}
        )
        self.assertEqual(code, 0)
        self.assertIn('"offered": 1', out)

    def test_why_on_an_unknown_offer_exits_cleanly(self):
        code, _out, err = self._main("offers", "--why", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no offer with key", err)

    def test_accept_prints_the_interests_json_entry(self):
        self._main("offers", "--import", self.artifact)
        code, out, _err = self._main("offers", "--accept", "binding-of-isaac-progression")
        self.assertEqual(code, 0)
        entry = json.loads(out[out.index("{"):out.rindex("}") + 1])
        self.assertEqual(entry["key"], "binding-of-isaac-progression")
        self.assertEqual(entry["offered_by"]["evidence_count"], 2)

    def test_a_second_decision_on_a_decided_offer_exits_cleanly(self):
        self._main("offers", "--import", self.artifact)
        self._main("offers", "--accept", "binding-of-isaac-progression")
        code, _out, err = self._main("offers", "--reject", "binding-of-isaac-progression")
        self.assertEqual(code, 2)
        self.assertIn("not a legal transition", err)

    def test_sweep_runs_and_reports(self):
        self._main("offers", "--import", self.artifact)
        code, out, _err = self._main("offers", "--sweep")
        self.assertEqual(code, 0)
        self.assertIn('"expired": 0', out)

    def test_import_of_a_missing_artifact_reports_it_and_exits_non_zero(self):
        """`offers.import_artifact` stays fail-soft -- it returns a summary
        with a reason instead of raising -- but the CLI must NOT translate
        that into a successful exit. This is now a scheduled task: an exit 0
        would stamp job:offers-import:last_ok and tell Task Scheduler,
        `health` and the owner that the import is fine while the artifact has
        been missing for a week. Two multi-day outages started exactly that
        way."""
        code, out, _err = self._main("offers", "--import", os.path.join(self.tmp.name, "no.json"))
        self.assertEqual(code, 2)
        self.assertIn("unreadable", out)
        self.assertIn("FAILED", out)

    def test_a_re_import_of_the_same_artifact_is_a_success_not_a_failure(self):
        """The steady state of an hourly idempotent job. "already imported"
        means the artifact was read fine and held nothing new -- the opposite
        of the unreadable case above -- so it must exit 0, keep the heartbeat
        fresh, and still say in the log why it did nothing."""
        first, _out, _err = self._main("offers", "--import", self.artifact)
        self.assertEqual(first, 0)
        code, out, _err = self._main("offers", "--import", self.artifact)
        self.assertEqual(code, 0)
        self.assertIn("already imported", out)
        self.assertIn("nothing to do", out)

    def test_scheduled_offer_branches_record_a_job_heartbeat(self):
        """`health` can only report a job it has a heartbeat for. Without
        these two keys the sweep could stop running for a month and nothing
        -- not `health`, not the Telegram alert it drives -- would know."""
        self._main("offers", "--import", self.artifact)
        self._main("offers", "--sweep")
        conn = db.connect(self.db_path)
        try:
            self.assertIsNotNone(db.state_get(conn, "job:offers-import:last_ok"))
            self.assertIsNotNone(db.state_get(conn, "job:offers-sweep:last_ok"))
        finally:
            conn.close()

    def test_a_failed_import_records_last_fail_not_last_ok(self):
        self._main("offers", "--import", os.path.join(self.tmp.name, "no.json"))
        conn = db.connect(self.db_path)
        try:
            self.assertIsNone(db.state_get(conn, "job:offers-import:last_ok"))
            self.assertIsNotNone(db.state_get(conn, "job:offers-import:last_fail"))
        finally:
            conn.close()

    def test_the_sweep_says_out_loud_when_it_did_nothing(self):
        """A sweep that transitions nothing is the normal case, and is
        indistinguishable from a sweep that never ran unless it says so."""
        self._main("offers", "--import", self.artifact)
        code, out, _err = self._main("offers", "--sweep")
        self.assertEqual(code, 0)
        self.assertTrue(
            "nothing was due" in out or "not evaluated" in out or "transition" in out,
            out,
        )

    def test_interactive_offer_branches_do_not_record_a_job_heartbeat(self):
        """The owner listing their inbox is not a scheduled run. If it stamped
        job:offers-import:last_ok, opening the inbox by hand would mask a
        scheduled importer that had been dead for days."""
        self._main("offers", "--import", self.artifact)
        conn = db.connect(self.db_path)
        try:
            before = db.state_get(conn, "job:offers-import:last_ok")
        finally:
            conn.close()
        self._main("offers")
        self._main("offers", "--why", "binding-of-isaac-progression")
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(db.state_get(conn, "job:offers-import:last_ok"), before)
        finally:
            conn.close()


class ExtractInterestsCLITests(unittest.TestCase):
    """`python -m app extract-interests` -- the scheduled wrapper around the
    sibling `ai` repo's interest_extractor.py.

    The command itself never talks to a browser; it shells out to the
    extractor, which does. So these tests stand a fake `interest_extractor.py`
    up in a temp directory and drive the wrapper's real job: locating the
    extractor from the already-wired candidates path, budgeting each stage,
    and -- the part that matters -- refusing to report success for a run that
    achieved nothing."""

    STATUS_TEMPLATE = (
        "import json, sys\n"
        "cmd = sys.argv[1]\n"
        "if cmd == 'status':\n"
        "    import os\n"
        "    n = int(open(os.path.join(os.path.dirname(__file__), 'pending.txt')).read())\n"
        "    print(json.dumps({'pending_conversations': n, 'failed_conversations': 0,\n"
        "                      'themes': 3, 'corpus': {}}))\n"
        "    raise SystemExit(0)\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.ai_repo = os.path.join(self.tmp.name, "ai")
        os.makedirs(self.ai_repo)
        self.artifact = os.path.join(self.ai_repo, "interest_candidates.json")
        self.script = os.path.join(self.ai_repo, "interest_extractor.py")
        self._set_pending(5)

    def _set_pending(self, n):
        with open(os.path.join(self.ai_repo, "pending.txt"), "w") as fh:
            fh.write(str(n))

    def _write_extractor(self, body):
        with open(self.script, "w", encoding="utf-8") as fh:
            # Plain concatenation, not str.format: the status stub is full
            # of JSON braces a format template would try to interpolate.
            fh.write(self.STATUS_TEMPLATE + body)

    def _main(self, *argv, env=None):
        from discovery.__main__ import main

        environ = {"DISCOVERY_INTEREST_CANDIDATES": self.artifact}
        environ.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--db", self.db_path, "extract-interests", *argv])
        return code, out.getvalue(), err.getvalue()

    # A working extractor: map digests everything, reduce writes the artifact.
    GOOD = (
        "if cmd == 'map':\n"
        "    import os\n"
        "    open(os.path.join(os.path.dirname(__file__), 'pending.txt'), 'w').write('0')\n"
        "    print('mapped 5')\n"
        "    raise SystemExit(0)\n"
        "if cmd == 'reduce':\n"
        "    out = sys.argv[sys.argv.index('--out') + 1]\n"
        "    open(out, 'w').write('{\"candidates\": []}')\n"
        "    print('reduced')\n"
        "    raise SystemExit(0)\n"
    )

    def test_a_good_run_maps_then_reduces_and_stamps_last_ok(self):
        self._write_extractor(self.GOOD)
        code, out, _err = self._main()
        self.assertEqual(code, 0, out)
        self.assertIn("map start", out)
        self.assertIn("reduce start", out)
        self.assertIn("pending 5 -> 0", out)
        self.assertIn("artifact=rewritten", out)
        self.assertTrue(os.path.exists(self.artifact))
        conn = db.connect(self.db_path)
        try:
            self.assertIsNotNone(db.state_get(conn, "job:interest-extract:last_ok"))
        finally:
            conn.close()

    def test_the_extractor_is_found_from_the_candidates_path_alone(self):
        """The `internet` -> `ai` hop is already wired through
        DISCOVERY_INTEREST_CANDIDATES; the scheduled job re-uses it instead of
        introducing a second path setting that could drift out of step."""
        self._write_extractor(self.GOOD)
        code, out, _err = self._main()
        self.assertEqual(code, 0)
        self.assertIn(f"repo={self.ai_repo}", out)

    def test_a_missing_extractor_fails_loudly_instead_of_doing_nothing(self):
        code, out, _err = self._main()   # nothing written to self.script
        self.assertEqual(code, 2)
        self.assertIn("no interest_extractor.py", out)
        conn = db.connect(self.db_path)
        try:
            self.assertIsNone(db.state_get(conn, "job:interest-extract:last_ok"))
            self.assertIsNotNone(db.state_get(conn, "job:interest-extract:last_fail"))
        finally:
            conn.close()

    def test_a_dead_claude_tab_fails_the_job_and_skips_reduce(self):
        """cmd_map raises SystemExit when its claude.ai preflight fails -- the
        single likeliest failure of this job, and the one that must never look
        like a good night's run."""
        self._write_extractor(
            "if cmd == 'map':\n"
            "    sys.stderr.write('map: claude.ai is not reachable -- no open tab\\n')\n"
            "    raise SystemExit(1)\n"
            "if cmd == 'reduce':\n"
            "    open(os.path.join('x'), 'w')\n"
        )
        code, out, _err = self._main()
        self.assertEqual(code, 2)
        self.assertIn("map FAILED", out)
        self.assertIn("not reducing", out)
        self.assertNotIn("reduce start", out)
        self.assertFalse(os.path.exists(self.artifact))

    def test_a_map_that_digests_nothing_is_not_a_success(self):
        """The honesty check. `reduce` exiting 0 proves an artifact was
        written, not that anything new went into it. If map had work pending
        and left just as much pending, the job burned browser time for a
        byte-identical artifact -- so it must stamp last_fail, not last_ok."""
        self._write_extractor(
            "if cmd == 'map':\n"
            "    print('nothing digested')\n"
            "    raise SystemExit(0)\n"
            "if cmd == 'reduce':\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "    open(out, 'w').write('{}')\n"
            "    raise SystemExit(0)\n"
        )
        code, out, _err = self._main()
        from discovery.__main__ import EXTRACT_UNPRODUCTIVE

        self.assertEqual(code, EXTRACT_UNPRODUCTIVE)
        self.assertIn("map made no progress", out)
        conn = db.connect(self.db_path)
        try:
            self.assertIsNone(db.state_get(conn, "job:interest-extract:last_ok"))
            self.assertIsNotNone(db.state_get(conn, "job:interest-extract:last_fail"))
        finally:
            conn.close()

    def test_an_empty_corpus_is_a_success_not_a_failure(self):
        """Nothing pending means there was nothing to digest -- the normal
        outcome of a nightly incremental run on a quiet day. That must not be
        confused with the unproductive case above."""
        self._set_pending(0)
        self._write_extractor(
            "if cmd == 'map':\n"
            "    print('nothing to do')\n"
            "    raise SystemExit(0)\n"
            "if cmd == 'reduce':\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "    open(out, 'w').write('{}')\n"
            "    raise SystemExit(0)\n"
        )
        code, out, _err = self._main()
        self.assertEqual(code, 0, out)

    def test_reduce_exiting_zero_without_writing_an_artifact_is_a_failure(self):
        self._write_extractor(
            "if cmd == 'map':\n"
            "    import os\n"
            "    open(os.path.join(os.path.dirname(__file__), 'pending.txt'), 'w').write('0')\n"
            "    raise SystemExit(0)\n"
            "if cmd == 'reduce':\n"
            "    print('pretending')\n"
            "    raise SystemExit(0)\n"
        )
        code, out, _err = self._main()
        from discovery.__main__ import EXTRACT_UNPRODUCTIVE

        self.assertEqual(code, EXTRACT_UNPRODUCTIVE)
        self.assertIn("wrote no artifact", out)

    def test_a_map_that_overruns_its_budget_still_lets_reduce_publish(self):
        """`map` is checkpointed per batch, so stopping it on a deadline costs
        one batch. Killing the whole task instead would mean no artifact at
        all -- publishing a slightly staler candidate list beats publishing
        nothing."""
        self._write_extractor(
            "if cmd == 'map':\n"
            "    import time, os\n"
            "    open(os.path.join(os.path.dirname(__file__), 'pending.txt'), 'w').write('2')\n"
            "    time.sleep(30)\n"
            "if cmd == 'reduce':\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "    open(out, 'w').write('{}')\n"
            "    raise SystemExit(0)\n"
        )
        code, out, _err = self._main(env={"DISCOVERY_INTEREST_EXTRACT_MAP_SECONDS": "2"})
        self.assertIn("hit its 2s budget", out)
        self.assertIn("reduce start", out)
        # It did make progress (5 -> 2), so a timeout alone is not a failure.
        self.assertEqual(code, 0, out)

    def test_a_stage_that_hangs_without_printing_is_still_stopped(self):
        """The budget has to be enforced against the wall clock, not against
        the arrival of output. A `map` that wedges -- a claude.ai call that
        never returns, a CDP socket that never answers -- prints nothing at
        all, and a loop that only re-checks the clock after reading a line
        would wait on it forever. That is the exact shape of the outage this
        job is supposed to make impossible, so it gets its own test."""
        self._write_extractor(
            "if cmd == 'map':\n"
            "    import time\n"
            "    time.sleep(120)\n"     # never prints, never exits
            "if cmd == 'reduce':\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "    open(out, 'w').write('{}')\n"
            "    raise SystemExit(0)\n"
        )
        started = time.monotonic()
        code, out, _err = self._main(env={"DISCOVERY_INTEREST_EXTRACT_MAP_SECONDS": "2"})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 60, "the deadline did not stop a silent child")
        self.assertIn("hit its 2s budget", out)
        # It printed nothing and digested nothing, so this is the
        # unproductive case, not a success.
        from discovery.__main__ import EXTRACT_UNPRODUCTIVE

        self.assertEqual(code, EXTRACT_UNPRODUCTIVE)
        self.assertIn("map made no progress", out)

    def test_child_output_is_streamed_with_its_stage_label(self):
        """Each line lands in the log as it arrives and carries the stage it
        came from, so a run killed part-way still shows how far it got."""
        self._write_extractor(
            "if cmd == 'map':\n"
            "    import os\n"
            "    print('batch 1 -- 3 digested')\n"
            "    print('batch 2 -- 3 digested')\n"
            "    open(os.path.join(os.path.dirname(__file__), 'pending.txt'), 'w').write('0')\n"
            "    raise SystemExit(0)\n"
            "if cmd == 'reduce':\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "    open(out, 'w').write('{}')\n"
            "    raise SystemExit(0)\n"
        )
        code, out, _err = self._main()
        self.assertEqual(code, 0, out)
        self.assertIn("[map] batch 1 -- 3 digested", out)
        self.assertIn("[map] batch 2 -- 3 digested", out)

    def test_reduce_is_given_a_bounded_theme_list(self):
        """Unbounded, `reduce` forwards the extractor's entire theme list --
        its durability gate filters nothing on the real corpus -- and the
        request grows until claude.ai answers with nothing at all. The first
        scheduled run of this job mapped 240 conversations successfully and
        then failed reduce outright, twice, in under ten seconds each."""
        self._write_extractor(
            "if cmd == 'map':\n"
            "    import os\n"
            "    open(os.path.join(os.path.dirname(__file__), 'pending.txt'), 'w').write('0')\n"
            "    raise SystemExit(0)\n"
            "if cmd == 'reduce':\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "    open(out, 'w').write(' '.join(sys.argv))\n"
            "    raise SystemExit(0)\n"
        )
        code, _out, _err = self._main(env={"DISCOVERY_INTEREST_EXTRACT_MAX_THEMES": "42"})
        self.assertEqual(code, 0)
        with open(self.artifact) as fh:
            self.assertIn("--max-themes 42", fh.read())

    def test_an_ai_checkout_without_the_flag_still_reduces(self):
        """argparse exits 2 on an unknown flag. An `ai` checkout older than
        eranbw123/ai#21 has never heard of --max-themes, and refusing to
        reduce at all over a flag it does not know would be a worse failure
        than the one the flag exists to prevent -- so it retries without it,
        and says loudly in the log that the request went out unbounded."""
        self._write_extractor(
            "if cmd == 'map':\n"
            "    import os\n"
            "    open(os.path.join(os.path.dirname(__file__), 'pending.txt'), 'w').write('0')\n"
            "    raise SystemExit(0)\n"
            "if cmd == 'reduce':\n"
            "    if '--max-themes' in sys.argv:\n"
            "        sys.stderr.write('unrecognized arguments: --max-themes\\n')\n"
            "        raise SystemExit(2)\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "    open(out, 'w').write('{}')\n"
            "    raise SystemExit(0)\n"
        )
        code, out, _err = self._main()
        self.assertEqual(code, 0, out)
        self.assertIn("does not accept --max-themes", out)
        self.assertIn("UNBOUNDED", out)

    def test_a_negative_pending_count_is_not_a_failure(self):
        """The extractor computes pending_conversations as conversations_in_db
        minus conversations_digested, and those count different things --
        digests are keyed by content hash, so a re-digested conversation is
        counted twice and the difference goes NEGATIVE once the corpus has
        been re-mapped. Measured on the live corpus: 355 in db, 503 digested,
        pending -148. A negative is truthy and -148 >= -148, so a bare
        truthiness guard failed a run where map correctly had nothing to do
        and reduce had just published a fresh artifact. A false failure
        discredits the honesty check as fast as a false success does."""
        self._set_pending(-148)
        self._write_extractor(
            "if cmd == 'map':\n"
            "    print('nothing to do')\n"
            "    raise SystemExit(0)\n"
            "if cmd == 'reduce':\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "    open(out, 'w').write('{}')\n"
            "    raise SystemExit(0)\n"
        )
        code, out, _err = self._main()
        self.assertEqual(code, 0, out)
        self.assertNotIn("made no progress", out)

    def test_skip_map_reduces_over_the_existing_digests(self):
        self._write_extractor(self.GOOD)
        code, out, _err = self._main("--skip-map")
        self.assertEqual(code, 0, out)
        self.assertNotIn("map start", out)
        self.assertIn("reduce start", out)

    def test_a_paused_appliance_does_not_drive_the_browser(self):
        """`extract-interests` is the fourth PAUSE_GATED command: it holds a
        claude.ai tab for minutes, which is exactly what `pause` exists to
        stop. The offline import and sweep are deliberately NOT gated --
        freezing the sweep would freeze the 30/45-day lifecycle clocks."""
        from discovery.__main__ import PAUSE_GATED

        self.assertIn("extract-interests", PAUSE_GATED)
        self.assertNotIn("offers", PAUSE_GATED)
        self._write_extractor(self.GOOD)
        conn = db.connect(self.db_path)
        try:
            db.init(conn)
            db.state_set(conn, "paused", "1")
            conn.commit()
        finally:
            conn.close()
        code, out, _err = self._main()
        self.assertEqual(code, 0)
        self.assertIn("paused", out)
        self.assertFalse(os.path.exists(self.artifact))


class InterestPipelineHealthTests(unittest.TestCase):
    """The three new jobs have to be visible to `health`, because
    `health --notify` is the only path that reaches the owner's phone. A
    scheduled job nobody can see failing is the failure mode this whole
    change exists to prevent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.connect(os.path.join(self.tmp.name, "h.db"))
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def test_health_reports_all_three_pipeline_jobs(self):
        names = [j["name"] for j in health.check(self.conn, CFG)["jobs"]]
        for name in ("offers-import", "offers-sweep", "interest-extract"):
            self.assertIn(name, names)

    def test_a_job_that_has_never_run_is_unknown_not_stale(self):
        """Adding these to `health` must not alarm the owner before the tasks
        have had their first chance to fire."""
        result = health.check(self.conn, CFG)
        for job in result["jobs"]:
            if job["name"] in ("offers-import", "offers-sweep", "interest-extract"):
                self.assertFalse(job["stale"], job)
        self.assertFalse(result["degraded"])

    def test_a_sweep_that_stopped_running_turns_health_degraded(self):
        """The 30-day decay and 45-day auto-pause clocks stop dead when the
        sweep does. That has to be an alert, not a silence."""
        stale = (datetime.now(timezone.utc)
                 - timedelta(seconds=CFG.offers_sweep_interval_seconds
                             * CFG.health_stale_factor + 3600))
        db.state_set(self.conn, "job:offers-sweep:last_ok", stale.isoformat())
        result = health.check(self.conn, CFG)
        sweep = next(j for j in result["jobs"] if j["name"] == "offers-sweep")
        self.assertTrue(sweep["stale"])
        self.assertTrue(result["degraded"])
        self.assertIn("offers-sweep", health.format_report(result))

    def test_a_fresh_sweep_keeps_health_ok(self):
        db.state_set(self.conn, "job:offers-sweep:last_ok",
                     datetime.now(timezone.utc).isoformat())
        result = health.check(self.conn, CFG)
        sweep = next(j for j in result["jobs"] if j["name"] == "offers-sweep")
        self.assertFalse(sweep["stale"])
        self.assertFalse(result["degraded"])

    def test_the_report_still_lines_up_with_the_longest_job_name(self):
        report = health.format_report(health.check(self.conn, CFG))
        columns = {line.index("last_ok=") for line in report.splitlines()
                   if "last_ok=" in line}
        self.assertEqual(len(columns), 1, report)


class InterestSyncV2Tests(unittest.TestCase):
    """Sync v2 (discovery/interest_sync.py): interests.json is the source of
    truth in BOTH directions. The bug these pin down: v1 only ever inserted,
    so an interest deleted from the file kept collecting and spending until
    somebody hand-ran an UPDATE against the live database."""

    HEBREW = {
        "key": "memory-retrieval-he",
        "title": "זיכרון ושליפה",
        "description": "עבודה על זיכרון",
        "positive_signals": ["זיכרון", "retrieval practice"],
    }

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "interests.json")

    def _write(self, entries, defaults=None):
        # ensure_ascii=False: the real file carries Hebrew titles and signals.
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"defaults": defaults or {"min_score": 0.7, "sources": ["web_search"]},
                       "interests": entries}, fh, ensure_ascii=False)
        return self.path

    def _entry(self, key, **kw):
        return {"key": key, "title": key.upper(), **kw}

    def _row(self, key):
        return self.conn.execute("SELECT * FROM interests WHERE key = ?", (key,)).fetchone()

    def _actions(self, key):
        return [e["action"] for e in db.interest_events(self.conn, key)]

    def _pending(self, key, label="m1", status="PENDING"):
        self.conn.execute(
            "INSERT INTO search_missions (interest_key, label, prompt, prompt_sha256,"
            " status, created_at) VALUES (?, ?, 'p', 'h', ?, ?)",
            (key, label, status, db.now()),
        )
        self.conn.commit()

    # --- the regression this PR exists for ---------------------------------

    def test_an_entry_removed_from_the_file_is_deactivated(self):
        """v1's never-deactivates bug: `sync` used to leave a dropped interest
        active forever, so it kept being collected for and scored."""
        self._write([self._entry("keeper"), self._entry("dropped")])
        interest_sync.sync(self.conn, self.path)
        self.assertEqual(self._row("dropped")["active"], 1)

        self._write([self._entry("keeper")])
        result = interest_sync.sync(self.conn, self.path)

        self.assertEqual(result.deactivated, ["dropped"])
        self.assertEqual(self._row("dropped")["active"], 0)
        self.assertEqual(self._row("keeper")["active"], 1)
        self.assertEqual([i.key for i in db.active_interests(self.conn)], ["keeper"])

    def test_deactivating_cancels_pending_missions_but_never_a_running_one(self):
        self._write([self._entry("dropped")])
        interest_sync.sync(self.conn, self.path)
        self._pending("dropped", "queued")
        self._pending("dropped", "queued-2")
        self._pending("dropped", "in-flight", status="RUNNING")

        self._write([])
        result = interest_sync.sync(self.conn, self.path, force=True)

        self.assertEqual(result.missions_cancelled, 2)
        rows = dict(self.conn.execute(
            "SELECT label, status FROM search_missions WHERE interest_key = 'dropped'"
        ).fetchall())
        # A RUNNING mission is leased and mid-execution -- its own finish/fail
        # path owns that row; cancelling it would lose a result already paid for.
        self.assertEqual(rows, {"queued": "CANCELLED", "queued-2": "CANCELLED",
                                "in-flight": "RUNNING"})
        self.assertEqual(
            self.conn.execute(
                "SELECT last_error FROM search_missions WHERE label = 'queued'"
            ).fetchone()["last_error"],
            interest_sync.CANCEL_REASON,
        )

    def test_deactivation_is_recorded_as_an_owner_sync_event_with_its_reason(self):
        self._write([self._entry("dropped")])
        interest_sync.sync(self.conn, self.path)
        self._pending("dropped")
        self._write([])
        interest_sync.sync(self.conn, self.path, force=True)

        events = db.interest_events(self.conn, "dropped")
        self.assertEqual([e["action"] for e in events], ["create", "deactivate"])
        self.assertEqual([e["actor"] for e in events], ["owner_sync", "owner_sync"])
        # Written by offers.set_lifecycle -- the reason and the mission count
        # ride along on the same event as the lifecycle move that caused them.
        self.assertEqual(events[-1]["evidence"], {
            "from_lifecycle": offers.ACTIVE, "to_lifecycle": offers.RETIRED,
            "reason": "absent from the file", "missions_cancelled": 1,
        })

    # --- idempotence -------------------------------------------------------

    def test_re_running_an_unchanged_file_writes_nothing_and_logs_nothing(self):
        """Safe to run repeatedly against the live DB -- v1 appended one row
        per interest per run (155 rows saying only 'sync' in production)."""
        self._write([self._entry("a"), self._entry("b"), self.HEBREW])
        first = interest_sync.sync(self.conn, self.path)
        self.assertEqual(len(first.created), 3)
        events_after_first = self.conn.execute(
            "SELECT COUNT(*) c FROM interest_events").fetchone()["c"]

        second = interest_sync.sync(self.conn, self.path)

        self.assertEqual(second.changes, 0)
        self.assertEqual(sorted(second.unchanged), ["a", "b", self.HEBREW["key"]])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM interest_events").fetchone()["c"],
            events_after_first,
        )

    def test_a_hebrew_entry_does_not_look_changed_on_every_run(self):
        """Signals are stored as JSON with ensure_ascii=True, so comparing the
        raw column text would see the escapes and log an update forever."""
        self._write([self.HEBREW])
        interest_sync.sync(self.conn, self.path)
        plan = interest_sync.plan(self.conn, self.path)
        self.assertEqual(plan.updated, [])
        self.assertEqual(plan.unchanged, [self.HEBREW["key"]])
        self.assertEqual(
            json.loads(self._row(self.HEBREW["key"])["positive_signals"]),
            self.HEBREW["positive_signals"],
        )

    def test_an_unchanged_bar_written_on_the_legacy_scale_is_not_a_change(self):
        self._write([self._entry("a", min_score=0.7)])
        interest_sync.sync(self.conn, self.path)
        self._write([self._entry("a", min_score=70)])   # legacy 0-100 scale
        self.assertEqual(interest_sync.plan(self.conn, self.path).changes, 0)

    # --- retire in place, and revival --------------------------------------

    def test_active_false_retires_the_entry_without_losing_its_definition(self):
        self._write([self._entry("paused", positive_signals=["keep me"])])
        interest_sync.sync(self.conn, self.path)
        self._pending("paused")

        self._write([self._entry("paused", positive_signals=["keep me"], active=False)])
        result = interest_sync.sync(self.conn, self.path)

        self.assertEqual(result.deactivated, ["paused"])
        self.assertEqual(result.missions_cancelled, 1)
        row = self._row("paused")
        self.assertEqual(row["active"], 0)
        self.assertEqual(json.loads(row["positive_signals"]), ["keep me"])
        self.assertEqual(db.interest_events(self.conn, "paused")[-1]["evidence"]["reason"],
                         "marked inactive in the file")

    def test_removing_the_active_flag_revives_a_retired_interest(self):
        self._write([self._entry("retired-then-back", active=False)])
        interest_sync.sync(self.conn, self.path)
        self.assertEqual(self._row("retired-then-back")["active"], 0)
        self.assertEqual(self._actions("retired-then-back"), ["create", "deactivate"])

        self._write([self._entry("retired-then-back")])
        result = interest_sync.sync(self.conn, self.path)

        self.assertEqual(result.reactivated, ["retired-then-back"])
        row = self._row("retired-then-back")
        self.assertEqual((row["active"], row["lifecycle"]), (1, offers.ACTIVE))
        self.assertEqual(self._actions("retired-then-back"),
                         ["create", "deactivate", "reactivate"])

    def test_an_edit_takes_effect_without_re_initialising(self):
        """The end-to-end promise: edit the file, call sync, and the next
        cycle's active_interests() already sees it -- no `init`, no DB op."""
        self._write([self._entry("bar-tuning", min_score=0.62)])
        interest_sync.sync(self.conn, self.path)
        self._write([self._entry("bar-tuning", min_score=0.78,
                                 positive_signals=["new signal"])])

        interest_sync.sync(self.conn, self.path)

        (live,) = db.active_interests(self.conn)
        self.assertEqual(live.min_score, 0.78)
        self.assertEqual(live.positive_signals, ["new signal"])
        self.assertEqual(db.interest_events(self.conn, "bar-tuning")[-1]["evidence"],
                         {"changed": ["positive_signals", "min_score"]})

    # --- blast radius ------------------------------------------------------

    def test_a_truncated_file_is_refused_rather_than_retiring_everything(self):
        self._write([self._entry(f"i{n}") for n in range(8)])
        interest_sync.sync(self.conn, self.path)
        self._write([self._entry("i0")])       # 7 of 8 gone: a half-written file

        with self.assertRaises(interest_sync.SyncRefused):
            interest_sync.sync(self.conn, self.path)

        self.assertEqual(len(db.active_interests(self.conn)), 8)   # nothing written

    def test_the_guard_is_overridable_when_the_purge_is_intended(self):
        self._write([self._entry(f"i{n}") for n in range(8)])
        interest_sync.sync(self.conn, self.path)
        self._write([self._entry("i0")])
        result = interest_sync.sync(self.conn, self.path, force=True)
        self.assertEqual(len(result.deactivated), 7)
        self.assertEqual([i.key for i in db.active_interests(self.conn)], ["i0"])

    def test_ordinary_gardening_is_never_blocked_by_the_guard(self):
        self._write([self._entry(f"i{n}") for n in range(33)])
        interest_sync.sync(self.conn, self.path)
        self._write([self._entry(f"i{n}") for n in range(33) if n > 4])   # retire 5 of 33
        result = interest_sync.sync(self.conn, self.path)
        self.assertEqual(len(result.deactivated), 5)

    def test_a_malformed_file_aborts_before_anything_is_deactivated(self):
        self._write([self._entry("a"), self._entry("b")])
        interest_sync.sync(self.conn, self.path)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        with self.assertRaises(json.JSONDecodeError):
            interest_sync.sync(self.conn, self.path)
        self.assertEqual(len(db.active_interests(self.conn)), 2)

    def test_a_file_disagreeing_with_itself_about_a_key_is_rejected(self):
        self._write([self._entry("a", min_score=0.6), self._entry("a", min_score=0.9)])
        with self.assertRaises(ValueError) as ctx:
            interest_sync.sync(self.conn, self.path)
        self.assertIn("duplicate interest key", str(ctx.exception))

    # --- boundaries with the layered ladder --------------------------------

    def test_derived_rows_are_invisible_to_sync(self):
        """interests.json has no authority over derived:* rows -- they belong
        to interest_state.py's ladder, whatever the file does or does not say."""
        db.upsert_derived_interest(
            self.conn, an_interest(key="derived:gizmo", layer="inferred"), {})
        self._write([self._entry("owned")])
        result = interest_sync.sync(self.conn, self.path)
        self.assertEqual(result.deactivated, [])
        self.assertEqual(self._row("derived:gizmo")["active"], 1)

    def test_plan_writes_nothing(self):
        self._write([self._entry("a")])
        interest_sync.sync(self.conn, self.path)
        self._write([self._entry("b")])
        before = self.conn.execute("SELECT COUNT(*) c FROM interest_events").fetchone()["c"]

        plan = interest_sync.plan(self.conn, self.path)

        self.assertEqual(plan.created, ["b"])
        self.assertEqual(plan.deactivated, [("a", "absent from the file")])
        self.assertIsNone(self._row("b"))
        self.assertEqual(self._row("a")["active"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM interest_events").fetchone()["c"], before)

    def test_migrate_is_idempotent_and_stamps_synced_at(self):
        interest_sync.migrate(self.conn)
        interest_sync.migrate(self.conn)   # would raise if it were not additive-safe
        self._write([self._entry("a")])
        interest_sync.sync(self.conn, self.path)
        self.assertTrue(self._row("a")["synced_at"])

    def test_sync_migrates_a_database_that_predates_the_column(self):
        """db.init() does not know about synced_at (the column is owned here,
        not by db.py's ALTER pass), so this is the already-deployed
        discovery.db case: sync applies its own migration on the way in."""
        columns = [c[1] for c in self.conn.execute("PRAGMA table_info(interests)")]
        self.assertNotIn("synced_at", columns)

        self._write([self._entry("a")])
        interest_sync.sync(self.conn, self.path)

        self.assertTrue(self._row("a")["synced_at"])

    # --- the offers store owns liveness; this module owns the file ---------

    def test_retiring_moves_the_lifecycle_rather_than_only_the_active_flag(self):
        """One deactivation mechanism, not two: `active` and `lifecycle` must
        never be able to disagree, so sync drives offers.set_lifecycle()."""
        self._write([self._entry("dropped")])
        interest_sync.sync(self.conn, self.path)
        self.assertEqual(self._row("dropped")["lifecycle"], offers.ACTIVE)

        self._write([])
        interest_sync.sync(self.conn, self.path)

        row = self._row("dropped")
        self.assertEqual((row["active"], row["lifecycle"]), (0, offers.RETIRED))
        self.assertEqual(offers.interest_lifecycle(self.conn, "dropped"),
                         {"lifecycle": offers.RETIRED, "active": False})

    def test_an_auto_paused_interest_stays_paused_across_a_sync(self):
        """The decay sweep paused it; the file says nothing about liveness, so
        a sync that only carries a definition edit must not silently un-pause
        it -- that would be sync v2 fighting the sweep every cycle."""
        self._write([self._entry("quiet")])
        interest_sync.sync(self.conn, self.path)
        offers.set_lifecycle(self.conn, "quiet", offers.PAUSED,
                             actor="timer", action="auto_pause")

        self._write([self._entry("quiet", min_score=0.9)])   # an ordinary edit
        result = interest_sync.sync(self.conn, self.path)

        self.assertEqual(result.reactivated, [])
        self.assertEqual(result.updated, [("quiet", ["min_score"])])
        row = self._row("quiet")
        self.assertEqual((row["active"], row["lifecycle"]), (0, offers.PAUSED))
        self.assertEqual(row["min_score"], 0.9)   # the edit still landed

    def test_active_true_is_the_owner_overruling_the_sweep(self):
        self._write([self._entry("quiet")])
        interest_sync.sync(self.conn, self.path)
        offers.set_lifecycle(self.conn, "quiet", offers.PAUSED,
                             actor="timer", action="auto_pause")

        self._write([self._entry("quiet", active=True)])
        result = interest_sync.sync(self.conn, self.path)

        self.assertEqual(result.reactivated, ["quiet"])
        row = self._row("quiet")
        self.assertEqual((row["active"], row["lifecycle"]), (1, offers.ACTIVE))

    def test_entry_writer_is_the_callable_offers_accept_expects(self):
        """PR H's accept() deliberately writes neither interests.json nor the
        interests table and takes a `sync` callable instead; this is it. One
        call: entry in the file, interest in the DB, offer activated."""
        self._write([self._entry("existing")])
        interest_sync.sync(self.conn, self.path)
        offers.insert_offer(self.conn, {
            "key": "handheld-gaming", "title": "Handheld and roguelike gaming",
            "description": "Steam Deck and roguelike progression",
            "positive_signals": ["steam deck"], "suggested_min_score": 0.8,
            "artifact_sha256": "abc", "generated_at": "2026-08-17T00:00:00+00:00",
        })
        offers.offer(self.conn, "handheld-gaming")

        result = offers.accept(self.conn, "handheld-gaming",
                               sync=interest_sync.entry_writer(self.conn, self.path))

        self.assertTrue(result["ok"])
        # the interest row exists, live, with the offer's suggested bar
        row = self._row("handheld-gaming")
        self.assertEqual((row["active"], row["lifecycle"]), (1, offers.ACTIVE))
        self.assertEqual(row["min_score"], 0.8)
        # ... and the file carries it, provenance included, without losing the
        # entry that was already there
        entries = json.load(open(self.path, encoding="utf-8-sig"))["interests"]
        keys = [e["key"] for e in entries]
        self.assertEqual(keys, ["existing", "handheld-gaming"])
        self.assertEqual(entries[1]["offered_by"]["artifact_sha256"], "abc")
        # activate() ran, which it can only do once the interest row exists
        self.assertEqual(offers.get_offer(self.conn, "handheld-gaming")["status"],
                         offers.ACCEPTED)
        self.assertIn("offer_accepted", self._actions("handheld-gaming"))

    def test_set_entry_active_is_what_makes_a_retirement_durable(self):
        """A retirement recorded only in the DB, while the file still carries
        the entry saying nothing, is undone by the next sync -- the file is the
        source of truth. This is the call that keeps the two agreeing."""
        self._write([self._entry("to-retire")])
        interest_sync.sync(self.conn, self.path)
        offers.retire_interest(self.conn, "to-retire")

        interest_sync.set_entry_active(self.path, "to-retire", False)
        result = interest_sync.sync(self.conn, self.path)

        self.assertEqual(result.reactivated, [])
        self.assertEqual(self._row("to-retire")["lifecycle"], offers.RETIRED)

    def test_a_db_only_retirement_is_reverted_by_the_file(self):
        """The other half of the rule above, pinned deliberately: without the
        file being told, the entry's continued presence revives it."""
        self._write([self._entry("to-retire")])
        interest_sync.sync(self.conn, self.path)
        offers.retire_interest(self.conn, "to-retire")

        result = interest_sync.sync(self.conn, self.path)

        self.assertEqual(result.reactivated, ["to-retire"])
        self.assertEqual(self._row("to-retire")["lifecycle"], offers.ACTIVE)

    def test_write_entry_replaces_by_key_and_keeps_the_rest_of_the_file(self):
        self._write([self._entry("a"), self.HEBREW])
        data = json.load(open(self.path, encoding="utf-8-sig"))
        data["blocked_derived_terms"] = ["nope"]
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

        self.assertEqual(interest_sync.write_entry(
            self.path, {"key": "a", "title": "A", "min_score": 0.9}), "updated")
        self.assertEqual(interest_sync.write_entry(
            self.path, {"key": "new", "title": "New"}), "created")

        after = json.load(open(self.path, encoding="utf-8-sig"))
        self.assertEqual([e["key"] for e in after["interests"]],
                         ["a", self.HEBREW["key"], "new"])
        self.assertEqual(after["interests"][0]["min_score"], 0.9)
        self.assertEqual(after["blocked_derived_terms"], ["nope"])
        self.assertEqual(after["defaults"]["min_score"], 0.7)
        # Hebrew survives the round trip unescaped, as offers.py writes it too.
        self.assertEqual(after["interests"][1]["title"], self.HEBREW["title"])

    def test_load_stated_active_answers_three_ways(self):
        """Silent is not active: an entry saying nothing must not appear here,
        or the sweep's auto-pause would be undone on every sync."""
        self._write([self._entry("silent"), self._entry("off", active=False),
                     self._entry("on", active=True)])
        self.assertEqual(interests.load_stated_active(self.path),
                         {"off": False, "on": True})

    def test_the_real_interests_file_is_readable_by_the_new_helper(self):
        # interests.json is real user config; this asserts the flag is
        # readable on it, not what it says.
        self.assertIsInstance(interests.load_stated_active("interests.json"), dict)


class SyncCLITests(unittest.TestCase):
    """`python -m app sync` -- the runtime half: an edit takes effect without
    a redeploy, an `init`, or a hand-written UPDATE."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "d.db")
        self.path = os.path.join(self.tmp.name, "interests.json")

    def _write(self, keys, **extra):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"interests": [{"key": k, "title": k, **extra} for k in keys]}, fh)

    def _main(self, *argv):
        import contextlib

        from discovery.__main__ import main

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"DISCOVERY_INTERESTS": self.path}), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--db", self.db_path, *argv])
        return code, out.getvalue(), err.getvalue()

    def _conn(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def test_sync_deactivates_a_dropped_interest_with_no_manual_db_op(self):
        self._write(["keeper", "dropped"])
        self.assertEqual(self._main("init")[0], 0)

        self._write(["keeper"])
        code, out, _err = self._main("sync")

        self.assertEqual(code, 0)
        self.assertIn("1 deactivated", out)
        self.assertEqual([i.key for i in db.active_interests(self._conn())], ["keeper"])

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        self._write(["keeper", "dropped"])
        self._main("init")
        self._write(["keeper"])
        code, out, _err = self._main("sync", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("nothing written", out)
        self.assertIn("dropped", out)
        self.assertEqual(len(db.active_interests(self._conn())), 2)

    def test_the_guard_exits_2_and_force_gets_through(self):
        self._write([f"i{n}" for n in range(8)])
        self._main("init")
        self._write(["i0"])

        code, _out, err = self._main("sync")
        self.assertEqual(code, 2)
        self.assertIn("sync refused", err)
        self.assertEqual(len(db.active_interests(self._conn())), 8)

        code, out, _err = self._main("sync", "--force")
        self.assertEqual(code, 0)
        self.assertIn("7 deactivated", out)

    def test_sync_on_a_malformed_file_exits_cleanly(self):
        self._write(["a"])
        self._main("init")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        code, _out, err = self._main("sync")
        self.assertEqual(code, 2)
        self.assertIn("malformed interests file", err)
        self.assertEqual(len(db.active_interests(self._conn())), 1)

    def test_init_reports_what_it_reconciled(self):
        self._write(["a", "b"])
        code, out, _err = self._main("init")
        self.assertEqual(code, 0)
        self.assertIn("schema ready", out)
        self.assertIn("2 interests loaded", out)
        self.assertIn("2 created", out)



# ---------------------------------------------------------------------------
# PR N (first half): learning from offer decisions -- discovery/offer_learning.py
# ---------------------------------------------------------------------------
# Every case below runs against either the pure functions or the in-memory
# `MemoryOfferDecisionSource`, so nothing here needs a browser, a provider or a
# live store. The store-backed cases use PR H's real `offers` API and read the
# decisions back out of `offer_events`, which is the seam this half consumes.


def _decision(key="binding-of-isaac-progression", decision=offer_learning.ACCEPTED,
              days_ago=1, **overrides):
    """One owner judgement, as the write API records it."""
    values = {
        "offer_key": key,
        "decision": decision,
        "decided_at": _days_ago(days_ago),
        "artifact_sha256": overrides.pop("sha", "sha-run-1"),
        "signal_terms": offers.signal_tokens(
            overrides.pop("title", "Binding of Isaac progression"),
            overrides.pop("signals", ["binding of isaac", "isaac unlocks", "repentance"]),
        ),
    }
    values.update(overrides)
    return offer_learning.OfferDecision(**values)


def _hebrew_decision(decision=offer_learning.REJECTED, days_ago=2, **overrides):
    """28% of the corpus is Hebrew-titled; a rejection has to bite in both
    scripts or the same theme walks back in wearing the other one."""
    return _decision(
        key="cognitive-load-working-memory", decision=decision, days_ago=days_ago,
        title="עומס קוגניטיבי וזיכרון עבודה",
        signals=["זיכרון עבודה", "עומס קוגניטיבי", "cognitive load"],
        **overrides)


class OfferDecisionRecordTests(unittest.TestCase):
    """What is recorded per decision, and what each decision means."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def test_polarity_is_kind_aware_and_a_retire_answer_is_inverted(self):
        self.assertEqual(_decision(decision=offers.ACCEPTED).polarity, 1)
        self.assertEqual(_decision(decision=offers.REJECTED).polarity, -1)
        # 'not now' and 'no answer' are not judgements about the theme.
        self.assertEqual(_decision(decision=offers.SNOOZED).polarity, 0)
        self.assertEqual(_decision(decision=offers.EXPIRED).polarity, 0)
        # A retirement offer proposes DROPPING an interest, so declining it is
        # the owner's one-click rescue -- positive for the interest.
        retire = _decision(key="retire:weird-science", offer_kind="retire")
        self.assertEqual(retire.polarity, -1)
        self.assertEqual(
            _decision(key="retire:weird-science", offer_kind="retire",
                      decision=offers.REJECTED).polarity, 1)

    def test_only_owner_answers_count_as_decisions(self):
        self.assertTrue(_decision(decision=offers.SNOOZED).is_owner_decision)
        self.assertFalse(_decision(decision=offers.EXPIRED).is_owner_decision)

    def test_an_unknown_decision_or_kind_is_refused(self):
        with self.assertRaises(offer_learning.OfferLearningError):
            offer_learning.OfferDecision("k", "maybe", _days_ago(1))
        with self.assertRaises(offer_learning.OfferLearningError):
            offer_learning.OfferDecision("k", offers.ACCEPTED, _days_ago(1), offer_kind="vibes")

    def test_the_bar_delta_is_the_edit_the_owner_made(self):
        lowered = _decision(proposed_min_score=0.78, accepted_min_score=0.70)
        self.assertEqual(lowered.bar_delta, -0.08)
        self.assertIsNone(_decision(proposed_min_score=0.78).bar_delta)

    def test_the_log_round_trips_hebrew_terms_and_the_edit_diff(self):
        recorded = _hebrew_decision(
            decision=offers.ACCEPTED, edits={"min_score": 0.7, "title": "זיכרון עבודה"},
            proposed_min_score=0.78, accepted_min_score=0.70)
        self.assertIsNotNone(offer_learning.record_decision(self.conn, recorded))
        (read_back,) = offer_learning.decisions(self.conn)
        self.assertIn("זיכרון", read_back.signal_terms)
        self.assertEqual(read_back.edits["title"], "זיכרון עבודה")
        self.assertEqual(read_back.bar_delta, -0.08)
        self.assertEqual(read_back.polarity, 1)

    def test_recording_the_same_decision_twice_appends_once(self):
        recorded = _decision()
        self.assertIsNotNone(offer_learning.record_decision(self.conn, recorded))
        self.assertIsNone(offer_learning.record_decision(self.conn, recorded))
        self.assertEqual(len(offer_learning.decisions(self.conn)), 1)

    def test_learning_never_touches_the_feedback_table(self):
        """`feedback` belongs to the delivered-item half, which is deliberately
        frozen pending the Output Layer decision. This half must be able to run
        a full pass without writing a row to it."""
        before = self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"]
        offer_learning.record_decision(self.conn, _decision())
        learned = offer_learning.priors(self.conn, now=OFFER_NOW)
        offer_learning.rank([_candidate(key="something-else")], learned, now=OFFER_NOW)
        after = self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"]
        self.assertEqual((before, after), (0, 0))


class OfferDecisionSyncTests(unittest.TestCase):
    """`offer_events` is the source of truth; the decision log is its decorated
    projection, and replaying it must be idempotent."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def _offer(self, key, **overrides):
        candidate = _candidate(key=key, **overrides)
        score, terms = offers.score_candidate(candidate, now=OFFER_NOW)
        offers.insert_offer(self.conn, dict(candidate, score=score, score_terms=terms,
                                            similarity=candidate["similarity_to_existing"]),
                            now=OFFER_NOW)
        offers.offer(self.conn, key)
        return key

    def test_an_accept_with_edits_becomes_one_decision_carrying_what_was_kept(self):
        key = self._offer("binding-of-isaac-progression")
        offers.accept(self.conn, key, edits={"min_score": 0.68,
                                             "positive_signals": ["binding of isaac",
                                                                  "tainted characters"]},
                      note="follow it, but wider", now=OFFER_NOW)
        (decision,) = offer_learning.sync_from_offer_events(self.conn)
        self.assertEqual((decision.offer_key, decision.decision), (key, offers.ACCEPTED))
        self.assertEqual(decision.accepted_min_score, 0.68)
        self.assertEqual(decision.edits["min_score"], 0.68)
        # Post-edit signals: the owner swapped 'repentance' for 'tainted
        # characters', and it is the kept wording that teaches.
        self.assertIn("tainted", decision.signal_terms)
        self.assertNotIn("repentance", decision.signal_terms)
        self.assertEqual(decision.polarity, 1)

    def test_replaying_the_event_chain_never_double_counts(self):
        key = self._offer("binding-of-isaac-progression")
        offers.reject(self.conn, key, note="not this", now=OFFER_NOW)
        self.assertEqual(len(offer_learning.sync_from_offer_events(self.conn)), 1)
        self.assertEqual(offer_learning.sync_from_offer_events(self.conn), [])
        self.assertEqual(len(offer_learning.decisions(self.conn)), 1)

    def test_a_snooze_records_its_wake_time(self):
        key = self._offer("binding-of-isaac-progression")
        offers.snooze(self.conn, key, days=30, now=OFFER_NOW)
        (decision,) = offer_learning.sync_from_offer_events(self.conn)
        self.assertEqual(decision.decision, offers.SNOOZED)
        self.assertTrue(decision.snoozed_until > _days_ago(-29))
        self.assertEqual(decision.polarity, 0)

    def test_an_expiry_is_logged_but_teaches_nothing(self):
        key = self._offer("binding-of-isaac-progression")
        offers.expire(self.conn, key, now=OFFER_NOW)
        (decision,) = offer_learning.sync_from_offer_events(self.conn)
        self.assertEqual(decision.decision, offers.EXPIRED)
        learned = offer_learning.learn([decision], now=OFFER_NOW)
        self.assertEqual(learned.n_owner_decisions, 0)
        self.assertEqual(learned.blocked_keys, {})
        self.assertEqual(learned.prototype, {})

    def test_a_rescued_interest_records_the_stage_it_was_rescued_from(self):
        """The interest lifecycle has a 30-day 'decaying' stage before the
        45-day pause, so a rescue is not always an undo of a pause -- the log
        keeps which stage the owner answered from."""
        db.upsert_interest(self.conn, Interest(
            key="weird-science", title="Weird science", description="",
            positive_signals=["weird science"], sources=["web_search"]))
        offers.set_lifecycle(self.conn, "weird-science", offers.DECAYING,
                             actor=offers.TIMER, action="decay")
        key = offers.RETIRE_PREFIX + "weird-science"
        offers.insert_offer(self.conn, {
            "key": key, "kind": "retire", "title": "Retire 'weird-science'?",
            "description": "", "related_keys": ["weird-science"], "score": None,
            "score_terms": {}, "evidence": [], "durability": {},
        }, actor=offers.TIMER, now=OFFER_NOW)
        offers.offer(self.conn, key, actor=offers.TIMER)
        offers.reject(self.conn, key, note="keep watching it", now=OFFER_NOW)

        (decision,) = offer_learning.sync_from_offer_events(self.conn)
        self.assertEqual(decision.offer_kind, "retire")
        self.assertEqual(decision.interest_key, "weird-science")
        self.assertEqual(decision.lifecycle, offers.DECAYING)
        self.assertEqual(decision.polarity, 1)

        learned = offer_learning.learn([decision], now=OFFER_NOW)
        self.assertEqual(learned.rescued, {"weird-science": offers.DECAYING})
        # A rescue propagates NO terms in either direction: blocking or
        # boosting an interest title's generic words would poison the pool,
        # which is the same call offers.blocked_terms_for() makes.
        self.assertEqual(learned.blocked_keys, {})
        self.assertEqual(learned.prototype, {})
        self.assertEqual(learned.n_owner_decisions, 1)


class InboxTopUpTests(unittest.TestCase):
    """The owner asked for one thing: "I want interests to like aim to always
    have 10 suggestions ... it should fill automatically to 10 suggested
    interests. And when I accept or reject, it should run again."

    These are the tests for the arithmetic that means, for the gates that must
    survive it (a short inbox beats a padded one), and for the two runners --
    the hourly tick and the click -- racing each other.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write_artifact(self, candidates, name="interest_candidates.json"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_artifact(candidates), fh, ensure_ascii=False)
        return path

    def _fill(self, n):
        """Put `n` live offers in the inbox, from `n` distinct candidates."""
        path = self._write_artifact(_distinct_candidates(n))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(offers.live_offer_count(self.conn), n)
        return path

    # --- what the target counts ------------------------------------------

    def test_only_offered_counts_toward_the_target(self):
        """Decided offers are out by definition; snoozed is out by intent --
        the owner said "not now" and the inbox hides it, so counting it would
        leave what he can SEE short by the number of things he set aside."""
        path = self._write_artifact(_distinct_candidates(4))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(offers.live_offer_count(self.conn), 4)
        offers.snooze(self.conn, "orexin-wakefulness", now=OFFER_NOW)
        offers.reject(self.conn, "perovskite-tandem", now=OFFER_NOW)
        offers.accept(self.conn, "mitochondrial-uncoupling", now=OFFER_NOW)
        offers.expire(self.conn, "harbour-dredging", now=OFFER_NOW)
        self.assertEqual(offers.live_offer_count(self.conn), 0)

    def test_a_snoozed_offer_is_replaced_and_still_wakes_later(self):
        # The whole point of excluding snoozed: setting one aside must refill
        # the visible inbox, and must not cost the owner the offer itself.
        path = self._write_artifact(_distinct_candidates(12))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        target = offers.DEFAULT_RULES.target_inbox_size
        self.assertEqual(offers.live_offer_count(self.conn), target)
        offers.snooze(self.conn, "orexin-wakefulness", now=OFFER_NOW)
        self.assertEqual(offers.live_offer_count(self.conn), target - 1)
        summary = offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["offered"], 1)
        self.assertEqual(offers.live_offer_count(self.conn), target)
        # ...and the snoozed one is still there, asleep, not lost.
        self.assertEqual(offers.get_offer(self.conn, "orexin-wakefulness")["status"],
                         offers.SNOOZED)

    # --- the top-up itself ------------------------------------------------

    def test_top_up_refills_after_a_decision_from_candidates_already_on_disk(self):
        """The measured production case: the artifact holds far more qualified
        candidates than the inbox, and refilling needs no extractor run at
        all -- it is local arithmetic over a file that is already there."""
        path = self._write_artifact(_distinct_candidates(14))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        target = offers.DEFAULT_RULES.target_inbox_size
        offers.accept(self.conn, "orexin-wakefulness", now=OFFER_NOW)
        offers.reject(self.conn, "perovskite-tandem", now=OFFER_NOW)
        self.assertEqual(offers.live_offer_count(self.conn), target - 2)

        summary = offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["offered"], 2)
        self.assertEqual(summary["live_before"], target - 2)
        self.assertEqual(summary["live_after"], target)
        self.assertEqual(offers.live_offer_count(self.conn), target)
        self.assertFalse(summary["exhausted"])
        self.assertIn("topped up 2 offer(s)", summary["reason"])

    def test_top_up_ignores_the_artifact_sha_the_importer_records(self):
        """import_artifact() is idempotent on the sha -- correct, and exactly
        why the inbox could only ever shrink. top_up() must NOT consult it."""
        path = self._write_artifact(_distinct_candidates(14))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(
            offers.import_artifact(self.conn, path, now=OFFER_NOW)["error"], "already imported"
        )
        offers.accept(self.conn, "orexin-wakefulness", now=OFFER_NOW)
        summary = offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["offered"], 1)
        self.assertEqual(summary["error"], "")

    def test_top_up_is_a_no_op_when_the_target_is_already_met_and_says_so(self):
        path = self._fill(offers.DEFAULT_RULES.target_inbox_size)
        summary = offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["offered"], 0)
        self.assertEqual(summary["deficit"], 0)
        self.assertIn("target already met", summary["reason"])

    def test_top_up_never_overshoots_the_target(self):
        path = self._write_artifact(_distinct_candidates(16))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        target = offers.DEFAULT_RULES.target_inbox_size
        for _ in range(4):
            offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertEqual(offers.live_offer_count(self.conn), target)

    # --- the gates survive the target ------------------------------------

    def test_the_floors_still_apply_and_a_short_inbox_is_the_correct_outcome(self):
        """Never pad. A candidate that fails the durability gate stays out
        even when the inbox is nine short and it is the only thing left."""
        weak = _distinct_candidates(1)
        weak[0]["durability"] = {"n_convs": 1, "active_months": 0, "recency_days": 2}
        weak[0]["evidence"] = [dict(weak[0]["evidence"][0], depth=0.1)]
        path = self._write_artifact(weak)
        summary = offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["offered"], 0)
        self.assertEqual(summary["skipped_floor"], 1)
        self.assertEqual(offers.live_offer_count(self.conn), 0)
        self.assertTrue(summary["exhausted"])
        self.assertIn("durability gate", summary["reasons"][0]["why"])

    def test_a_rejected_theme_is_not_re_offered_by_the_top_up(self):
        path = self._write_artifact(_distinct_candidates(3))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        offers.reject(self.conn, "orexin-wakefulness", now=OFFER_NOW)
        summary = offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertNotIn("orexin-wakefulness", summary["offers"])
        self.assertEqual(offers.get_offer(self.conn, "orexin-wakefulness")["status"],
                         offers.REJECTED)

    def test_the_top_up_never_re_decides_a_decided_offer(self):
        path = self._write_artifact(_distinct_candidates(6))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        for key, decide in (("orexin-wakefulness", offers.accept),
                            ("perovskite-tandem", offers.reject),
                            ("mitochondrial-uncoupling", offers.expire)):
            decide(self.conn, key, now=OFFER_NOW)
        before = {r["key"]: r["status"] for r in offers.list_offers(self.conn)}
        for _ in range(3):
            offers.top_up(self.conn, path, now=OFFER_NOW)
        after = {r["key"]: r["status"] for r in offers.list_offers(self.conn)}
        for key in ("orexin-wakefulness", "perovskite-tandem", "mitochondrial-uncoupling"):
            self.assertEqual(after[key], before[key])

    def test_the_top_up_does_not_attach_evidence_the_import_already_attached(self):
        """Dedup layer 2 writes the candidate's quotes onto the interest that
        covers it. That belongs to the once-per-artifact import; a refill that
        may run several times an hour must not re-write it every time."""
        db.upsert_interest(self.conn, Interest(
            key="sleep-neuropeptides", title="Sleep neuropeptides", description="",
            positive_signals=["orexin", "hypocretin"], min_score=0.7, sources=["web_search"],
        ))
        path = self._write_artifact(_distinct_candidates(3))
        imported = offers.import_artifact(self.conn, path, now=OFFER_NOW)
        self.assertEqual(imported["attached"], 1)
        events_after_import = self.conn.execute(
            "SELECT COUNT(*) FROM interest_events"
        ).fetchone()[0]
        summary = offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["attached"], 0)
        self.assertEqual(summary["skipped_dedup"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM interest_events").fetchone()[0],
            events_after_import,
        )

    # --- near-duplicate suppression, the measured production bug ----------

    def test_a_near_duplicate_of_a_live_offer_is_suppressed_not_offered_beside_it(self):
        """The production bug, reproduced: dedup was semantic against
        interests but exact-key-only against offers, so two paraphrases of one
        idea could sit in the inbox at once. Measured 2026-08-18 on the live
        inbox: 'concentrated-portfolio-construction' vs
        'portfolio-construction-conviction-risk' at 65% token overlap, and
        'handheld-pc-gaming-steamos' vs 'handheld-pc-gaming-linux' at 50%."""
        first = _candidate(
            key="portfolio-construction-conviction-risk",
            title="Portfolio construction, conviction and risk",
            positive_signals=["portfolio construction", "position sizing", "drawdown",
                              "conviction thesis"],
            durability={"n_convs": 12, "active_months": 4, "recency_days": 3},
        )
        offers.import_artifact(self.conn, self._write_artifact([first]), now=OFFER_NOW)
        self.assertEqual(offers.live_offer_count(self.conn), 1)

        near_dupe = _candidate(
            key="concentrated-portfolio-construction",
            title="Concentrated portfolio construction",
            positive_signals=["portfolio construction", "position sizing", "drawdown"],
            durability={"n_convs": 12, "active_months": 4, "recency_days": 1},
        )
        summary = offers.top_up(
            self.conn, self._write_artifact([first, near_dupe], name="second.json"),
            now=OFFER_NOW,
        )
        self.assertEqual(summary["offered"], 0)
        self.assertEqual(summary["skipped_dedup"], 1)
        self.assertEqual(offers.live_offer_count(self.conn), 1)
        why = [r["why"] for r in summary["reasons"]
               if r["key"] == "concentrated-portfolio-construction"][0]
        self.assertIn("signal overlap with offer", why)
        self.assertIn("portfolio-construction-conviction-risk", why)

    def test_one_run_cannot_place_two_near_duplicates_of_each_other(self):
        """Distinctness beats the number: the peer set grows as the run
        promotes, so the second paraphrase loses to the first."""
        pair = [
            _candidate(key="handheld-pc-gaming-steamos", title="Handheld PC gaming on SteamOS",
                       positive_signals=["handheld gaming", "steamos", "proton", "thermal"],
                       durability={"n_convs": 9, "active_months": 4, "recency_days": 1}),
            _candidate(key="handheld-pc-gaming-linux", title="Handheld PC gaming on Linux",
                       positive_signals=["handheld gaming", "steamos", "proton"],
                       durability={"n_convs": 9, "active_months": 4, "recency_days": 2}),
        ]
        summary = offers.import_artifact(self.conn, self._write_artifact(pair), now=OFFER_NOW)
        self.assertEqual(summary["offered"], 1)
        self.assertEqual(summary["skipped_dedup"], 1)
        self.assertEqual(offers.live_offer_count(self.conn), 1)

    def test_distinctness_wins_even_when_it_leaves_the_inbox_short(self):
        """Ten suggestions with two duplicate pairs is a worse inbox than
        three distinct ones -- so the run stops short rather than padding."""
        pool = _distinct_candidates(3) + [
            _candidate(key=f"orexin-wakefulness-variant-{i}", title="Orexin wakefulness",
                       positive_signals=["orexin", "hypocretin"],
                       durability={"n_convs": 9, "active_months": 4, "recency_days": 5 + i})
            for i in range(6)
        ]
        summary = offers.import_artifact(self.conn, self._write_artifact(pool), now=OFFER_NOW)
        self.assertEqual(summary["offered"], 3)
        self.assertEqual(offers.live_offer_count(self.conn), 3)
        self.assertLess(offers.live_offer_count(self.conn),
                        offers.DEFAULT_RULES.target_inbox_size)

    def test_a_near_duplicate_of_a_rejected_offer_stays_out_for_the_block_window(self):
        """blocked_offer_keys() catches an exact token hit; a rephrasing needs
        the peer rule, or the owner is asked the same question again."""
        original = _candidate(
            key="lattice-cryptography-schemes", title="Lattice cryptography schemes",
            positive_signals=["lattice cryptography", "kyber", "dilithium"],
            durability={"n_convs": 9, "active_months": 4, "recency_days": 1},
        )
        offers.import_artifact(self.conn, self._write_artifact([original]), now=OFFER_NOW)
        offers.reject(self.conn, "lattice-cryptography-schemes", now=OFFER_NOW)

        rephrased = _candidate(
            key="post-quantum-lattice-primitives", title="Post-quantum lattice primitives",
            positive_signals=["lattice cryptography", "kyber", "dilithium"],
            durability={"n_convs": 9, "active_months": 4, "recency_days": 1},
        )
        path = self._write_artifact([rephrased], name="rephrased.json")
        summary = offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["offered"], 0)
        self.assertEqual(offers.live_offer_count(self.conn), 0)

        # ...and it is free again once the 180-day window has passed.
        later = OFFER_NOW + timedelta(days=offers.DEFAULT_RULES.reject_block_days + 1)
        self.assertEqual(offers.top_up(self.conn, path, now=later)["offered"], 1)

    # --- the artifact runs dry -------------------------------------------

    def test_an_exhausted_artifact_is_reported_not_faked(self):
        path = self._write_artifact(_distinct_candidates(4))
        summary = offers.top_up(self.conn, path, now=OFFER_NOW)
        self.assertEqual(summary["offered"], 4)
        self.assertTrue(summary["exhausted"])
        self.assertIn("still 6 short", summary["reason"])
        self.assertIn("New extraction is what this needs", summary["reason"])
        self.assertEqual(offers.live_offer_count(self.conn), 4)

    def test_a_top_up_that_adds_nothing_always_says_why(self):
        """Silent failure is banned: two multi-day outages in this system were
        jobs that did nothing and reported success."""
        cases = {
            "target already met": lambda: self._fill(
                offers.DEFAULT_RULES.target_inbox_size),
            "could not be read": lambda: os.path.join(self.tmp.name, "nope.json"),
            "missing, malformed": lambda: self._write_bad(),
            "no candidates at all": lambda: self._write_artifact([]),
        }
        for expected, make in cases.items():
            with self.subTest(expected):
                # A fresh inbox per case -- the first one deliberately fills it.
                self.conn = db.connect(":memory:")
                db.init(self.conn)
                path = make()
                summary = offers.top_up(self.conn, path, now=OFFER_NOW)
                self.assertEqual(summary["offered"], 0)
                self.assertTrue(summary["reason"], "a silent run is the banned outcome")
                self.assertIn(expected, summary["reason"])

    def _write_bad(self):
        path = os.path.join(self.tmp.name, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        return path

    def test_every_run_leaves_a_heartbeat_naming_what_it_decided(self):
        path = self._write_artifact(_distinct_candidates(12))
        offers.top_up(self.conn, path, now=OFFER_NOW)
        beat = json.loads(db.state_get(self.conn, offers.TOPUP_STATE_KEY))
        self.assertEqual(beat["offered"], offers.DEFAULT_RULES.target_inbox_size)
        self.assertEqual(beat["live_after"], offers.DEFAULT_RULES.target_inbox_size)
        self.assertTrue(beat["reason"])
        # ...including the runs that add nothing.
        offers.top_up(self.conn, path, now=OFFER_NOW)
        beat = json.loads(db.state_get(self.conn, offers.TOPUP_STATE_KEY))
        self.assertEqual(beat["offered"], 0)
        self.assertIn("target already met", beat["reason"])

    # --- races between the hourly tick and the click ----------------------

    def test_a_refill_racing_the_hourly_tick_neither_duplicates_nor_overshoots(self):
        """The real interleave: the hourly tick is midway through promoting
        when the owner's click fires its own refill on the same DB. The second
        runner is re-entered from inside the first one's insert, which is the
        worst-case ordering -- the first run's row exists but has not yet been
        transitioned into the inbox."""
        path = self._write_artifact(_distinct_candidates(14))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        target = offers.DEFAULT_RULES.target_inbox_size
        for key in ("orexin-wakefulness", "perovskite-tandem", "mitochondrial-uncoupling"):
            offers.accept(self.conn, key, now=OFFER_NOW)
        self.assertEqual(offers.live_offer_count(self.conn), target - 3)

        real_insert = offers.insert_offer
        inner = []

        def racing_insert(conn, candidate, **kw):
            row = real_insert(conn, candidate, **kw)
            if not inner:
                inner.append(None)      # re-enter exactly once
                with mock.patch.object(offers, "insert_offer", real_insert):
                    inner[0] = offers.top_up(conn, path, now=OFFER_NOW)
            return row

        with mock.patch.object(offers, "insert_offer", racing_insert):
            outer = offers.top_up(self.conn, path, now=OFFER_NOW)

        # Together they filled the three empty slots and not one more.
        self.assertEqual(offers.live_offer_count(self.conn), target)
        self.assertEqual(outer["offered"] + inner[0]["offered"], 3)
        keys = [r["key"] for r in offers.inbox(self.conn)]
        self.assertEqual(len(keys), len(set(keys)), "no key was offered twice")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM interest_offers WHERE status = ?", (offers.PROPOSED,)
            ).fetchone()[0],
            0, "no row was left parked at 'proposed', silently eating a slot",
        )

    def test_a_key_offered_by_another_run_mid_flight_is_not_duplicated(self):
        """The narrow window insert_offer() guards: between its SELECT and its
        INSERT, the other run offered this very key. Losing that race returns
        None rather than raising -- the row exists, which is what both runs
        wanted."""
        candidate = dict(_distinct_candidates(1)[0], score=0.9, score_terms={})
        self.assertIsNotNone(offers.insert_offer(self.conn, candidate, now=OFFER_NOW))
        self.assertIsNone(offers.insert_offer(self.conn, candidate, now=OFFER_NOW))

        # ...and the same when the SELECT misses and the UNIQUE index is what
        # catches it, which is the actual concurrent case.
        with mock.patch.object(offers, "get_offer", return_value=None):
            self.assertIsNone(offers.insert_offer(self.conn, candidate, now=OFFER_NOW))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM interest_offers").fetchone()[0], 1
        )

    # --- the click path ---------------------------------------------------

    def test_a_failing_refill_can_never_cost_the_owner_his_decision(self):
        """The decision is the important write and is already committed by the
        time the refill runs; the refill is best-effort garnish on the same
        request."""
        path = self._write_artifact(_distinct_candidates(12))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        offers.accept(self.conn, "orexin-wakefulness", now=OFFER_NOW)

        class Exploding:
            """A connection that fails the way a contended SQLite does."""

            def execute(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")

        result = offers.top_up_after_decision(Exploding(), path)
        self.assertEqual(result["offered"], 0)
        self.assertIn("database is locked", result["error"])
        self.assertIn("the decision itself is unaffected", result["reason"])
        # The accepted offer is untouched by the failure.
        self.assertEqual(offers.get_offer(self.conn, "orexin-wakefulness")["status"],
                         offers.ACCEPTED)

    def test_the_click_path_makes_no_network_or_model_call(self):
        """The refill runs inside the owner's Accept request, so it has to be
        arithmetic over a local file and nothing else."""
        path = self._write_artifact(_distinct_candidates(12))
        offers.import_artifact(self.conn, path, now=OFFER_NOW)
        offers.accept(self.conn, "orexin-wakefulness", now=OFFER_NOW)

        def explode(*a, **k):
            raise AssertionError("the refill opened a socket")

        with mock.patch("socket.socket", explode), \
                mock.patch("urllib.request.urlopen", explode):
            summary = offers.top_up_after_decision(self.conn, path)
        self.assertEqual(summary["offered"], 1)
        self.assertEqual(summary["trigger"], "decision")


class OfferLearningColdStartTests(unittest.TestCase):
    """With no history, ranking is the §5.2 evidence terms and nothing else."""

    def test_no_decisions_means_no_adjustment_at_all(self):
        learned = offer_learning.learn([], now=OFFER_NOW)
        self.assertTrue(learned.cold_start)
        self.assertEqual((learned.prototype, learned.bar_shift), ({}, 0.0))
        (item,) = offer_learning.evaluate([_candidate()], learned, now=OFFER_NOW)
        self.assertEqual(item.score, item.base_score)
        self.assertEqual(item.suggested_min_score, 0.72)   # the generator's own number
        self.assertIn("cold start", offer_learning.explain(item))

    def test_cold_start_ends_after_two_generator_runs(self):
        first = [_decision(key="a", sha="sha-run-1", days_ago=20)]
        self.assertTrue(offer_learning.learn(first, now=OFFER_NOW).cold_start)
        second = first + [_decision(key="b", sha="sha-run-2", days_ago=10)]
        self.assertFalse(offer_learning.learn(second, now=OFFER_NOW).cold_start)

    def test_two_decisions_from_the_same_run_are_still_one_run(self):
        same_run = [_decision(key="a", sha="sha-run-1", days_ago=20),
                    _decision(key="b", sha="sha-run-1", days_ago=20)]
        learned = offer_learning.learn(same_run, now=OFFER_NOW)
        self.assertEqual((learned.n_owner_decisions, learned.n_runs), (2, 1))
        self.assertTrue(learned.cold_start)

    def test_a_rejection_still_bites_during_cold_start(self):
        """Warming up is not an excuse to re-offer something already refused."""
        learned = offer_learning.learn([_decision(decision=offers.REJECTED)], now=OFFER_NOW)
        self.assertTrue(learned.cold_start)
        (item,) = offer_learning.evaluate([_candidate()], learned, now=OFFER_NOW)
        self.assertFalse(item.ok)
        self.assertIn("rejected before", item.reason)

    def test_the_serendipity_slot_is_filled_even_when_it_ranks_last(self):
        strong = [_candidate(key=f"strong-{i}", expected_yield=1.0,
                             durability={"n_convs": 8, "active_months": 4, "recency_days": 0})
                  for i in range(12)]
        explorer = _candidate(key="one-deliberately-odd-pick", exploratory=True,
                              expected_yield=0.4,
                              durability={"n_convs": 4, "active_months": 2, "recency_days": 40})
        chosen, _skipped = offer_learning.rank(
            strong + [explorer], offer_learning.learn([], now=OFFER_NOW), now=OFFER_NOW)
        # The learning path ranks through offers.rank(), so it inherits the
        # inbox target rather than carrying a second copy of the number.
        self.assertEqual(len(chosen), offers.DEFAULT_RULES.target_inbox_size)
        self.assertIn("one-deliberately-odd-pick", [item.key for item in chosen])
        (slot,) = [item for item in chosen if item.exploratory]
        self.assertIn("serendipity slot", offer_learning.explain(slot))


class OfferLearningRankingTests(unittest.TestCase):
    """How past decisions re-rank the next batch."""

    def _warm(self, decisions):
        """Priors past cold start -- two generator runs of history."""
        learned = offer_learning.learn(decisions, now=OFFER_NOW)
        self.assertFalse(learned.cold_start, "fixture must be past cold start")
        return learned

    def test_an_accepted_theme_lifts_a_similar_candidate_by_at_most_five_points(self):
        accepted = ["binding of isaac", "isaac unlocks", "repentance"]
        learned = self._warm([
            _decision(key="isaac-runs", sha="sha-run-1", days_ago=30, signals=accepted,
                      title="Binding of Isaac progression"),
            _decision(key="isaac-mods", sha="sha-run-2", days_ago=10, signals=accepted,
                      title="Binding of Isaac progression"),
        ])
        (twin,) = offer_learning.evaluate(
            [_candidate(key="isaac-daily-runs", title="Binding of Isaac progression",
                        positive_signals=accepted)], learned, now=OFFER_NOW)
        self.assertEqual(twin.learning["prototype_similarity"], 1.0)
        self.assertEqual(twin.learning["accept_bonus"], 0.05)
        self.assertEqual(twin.score, round(twin.base_score + 0.05, 4))

        (stranger,) = offer_learning.evaluate(
            [_candidate(key="dutch-tax-law", title="Dutch tax law",
                        positive_signals=["belastingdienst", "box three"])],
            learned, now=OFFER_NOW)
        self.assertEqual(stranger.learning["accept_bonus"], 0.0)
        self.assertGreater(twin.score, stranger.score + 0.04)

    def test_the_exploratory_pick_opts_out_of_the_learned_bonus(self):
        accepted = ["binding of isaac", "isaac unlocks", "repentance"]
        learned = self._warm([
            _decision(key="isaac-runs", sha="sha-run-1", days_ago=30, signals=accepted),
            _decision(key="isaac-mods", sha="sha-run-2", days_ago=10, signals=accepted),
        ])
        (explorer,) = offer_learning.evaluate(
            [_candidate(key="isaac-adjacent-explore", exploratory=True,
                        title="Binding of Isaac progression", positive_signals=accepted)],
            learned, now=OFFER_NOW)
        # Rewarding resemblance to what the owner already likes is exactly
        # what an exploration lane must not do.
        self.assertEqual(explorer.learning["accept_bonus"], 0.0)
        self.assertEqual(explorer.score, explorer.base_score)

    def test_a_learned_bonus_can_never_lift_a_candidate_over_the_floor(self):
        thin = _candidate(key="thin-evidence", title="Binding of Isaac progression",
                          durability={"n_convs": 3, "active_months": 2, "recency_days": 180},
                          expected_yield=0.1,
                          similarity_to_existing=[{"key": "nbis-nebius", "sim": 0.5}])
        learned = self._warm([
            _decision(key="isaac-runs", sha="sha-run-1", days_ago=30),
            _decision(key="isaac-mods", sha="sha-run-2", days_ago=10),
        ])
        (item,) = offer_learning.evaluate([thin], learned, now=OFFER_NOW)
        self.assertLess(item.base_score, 0.45)
        self.assertGreater(item.score, 0.45)          # the preference bonus does lift it...
        chosen, skipped = offer_learning.select([item])
        self.assertEqual(chosen, [])                  # ...but the floor reads the base score
        self.assertIn("below floor", skipped[0].reason)

    def test_a_rejection_blocks_the_theme_in_the_other_language_too(self):
        learned = self._warm([
            _decision(key="something-else", sha="sha-run-1", days_ago=30),
            _hebrew_decision(sha="sha-run-2", days_ago=10),
        ])
        (paraphrase,) = offer_learning.evaluate(
            [_candidate(key="working-memory-load", title="זיכרון עבודה",
                        positive_signals=["זיכרון עבודה", "עומס קוגניטיבי"])],
            learned, now=OFFER_NOW)
        self.assertFalse(paraphrase.ok)
        self.assertIn("overlap", paraphrase.reason)

    def test_a_rejection_expires_after_the_block_window(self):
        stale = self._warm([
            _decision(key="something-else", sha="sha-run-1", days_ago=400),
            _decision(decision=offers.REJECTED, sha="sha-run-2", days_ago=181),
        ])
        (item,) = offer_learning.evaluate([_candidate()], stale, now=OFFER_NOW)
        self.assertTrue(item.ok, item.reason)

    def test_the_store_is_authoritative_about_what_is_blocked(self):
        learned = offer_learning.learn([], now=OFFER_NOW)
        (item,) = offer_learning.evaluate(
            [_candidate()], learned, now=OFFER_NOW,
            blocked={"binding-of-isaac-progression"})
        self.assertFalse(item.ok)
        self.assertIn("offers store", item.reason)

    def test_an_accepted_offer_is_not_offered_again_before_the_next_artifact(self):
        learned = self._warm([
            _decision(sha="sha-run-1", days_ago=30),
            _decision(key="something-else", sha="sha-run-2", days_ago=10),
        ])
        (again,) = offer_learning.evaluate([_candidate()], learned, now=OFFER_NOW)
        self.assertFalse(again.ok)
        self.assertIn("already accepted", again.reason)

    def test_the_owners_latest_word_wins_over_an_older_rejection(self):
        learned = self._warm([
            _decision(decision=offers.REJECTED, sha="sha-run-1", days_ago=60),
            _decision(decision=offers.ACCEPTED, sha="sha-run-2", days_ago=10),
        ])
        self.assertEqual(learned.blocked_keys, {})

    def test_a_snooze_holds_the_door_shut_for_a_paraphrase_and_then_opens_it(self):
        supplements = ["magnesium glycinate", "creatine", "l-tyrosine"]
        asleep = self._warm([
            _decision(key="something-else", sha="sha-run-1", days_ago=30),
            _decision(key="supplement-stack", decision=offers.SNOOZED, sha="sha-run-2",
                      days_ago=3, signals=supplements, title="Supplement stack",
                      snoozed_until=_days_ago(-27)),
        ])
        paraphrase = _candidate(key="daily-supplements", title="Supplement stack",
                                positive_signals=supplements)
        (blocked,) = offer_learning.evaluate([paraphrase], asleep, now=OFFER_NOW)
        self.assertFalse(blocked.ok)
        self.assertIn("snoozed until", blocked.reason)
        # ...and a snooze that has run out teaches nothing at all.
        woken = offer_learning.learn(
            [_decision(key="something-else", sha="sha-run-1", days_ago=30),
             _decision(key="supplement-stack", decision=offers.SNOOZED, sha="sha-run-2",
                       days_ago=40, signals=supplements, snoozed_until=_days_ago(10))],
            now=OFFER_NOW)
        (open_again,) = offer_learning.evaluate([paraphrase], woken, now=OFFER_NOW)
        self.assertTrue(open_again.ok, open_again.reason)
        self.assertEqual([t for t in ("magnesium", "creatine", "tyrosine")
                          if t in woken.prototype], [])

    def test_repeatedly_lowering_the_bar_lowers_what_gets_suggested(self):
        learned = self._warm([
            _decision(key="a", sha="sha-run-1", days_ago=30,
                      proposed_min_score=0.78, accepted_min_score=0.72),
            _decision(key="b", sha="sha-run-2", days_ago=10,
                      proposed_min_score=0.80, accepted_min_score=0.70),
        ])
        self.assertEqual((learned.bar_shift, learned.bar_shift_n), (-0.08, 2))
        (item,) = offer_learning.evaluate(
            [_candidate(key="fresh-one", suggested_min_score=0.78)], learned, now=OFFER_NOW)
        self.assertEqual(item.suggested_min_score, 0.70)
        self.assertEqual(item.learning["bar_shift"], -0.08)

    def test_one_edit_is_not_a_habit_and_a_landslide_is_clamped(self):
        once = self._warm([
            _decision(key="a", sha="sha-run-1", days_ago=30,
                      proposed_min_score=0.78, accepted_min_score=0.72),
            _decision(key="b", sha="sha-run-2", days_ago=10),
        ])
        self.assertEqual(once.bar_shift, 0.0)
        landslide = self._warm([
            _decision(key="a", sha="sha-run-1", days_ago=30,
                      proposed_min_score=0.90, accepted_min_score=0.40),
            _decision(key="b", sha="sha-run-2", days_ago=10,
                      proposed_min_score=0.90, accepted_min_score=0.45),
        ])
        self.assertEqual(landslide.bar_shift, -0.10)   # a prior nudges, it does not decide

    def test_evidence_terms_are_corpus_facts_and_learning_never_moves_them(self):
        learned = self._warm([
            _decision(key="isaac-runs", sha="sha-run-1", days_ago=30),
            _decision(key="isaac-mods", sha="sha-run-2", days_ago=10),
        ])
        candidate = _candidate(key="isaac-daily-runs")
        cold = offer_learning.evaluate([candidate], offer_learning.learn([], now=OFFER_NOW),
                                       now=OFFER_NOW)[0]
        warm = offer_learning.evaluate([candidate], learned, now=OFFER_NOW)[0]
        for term in ("evidence_strength", "recurrence", "recency", "novelty", "expected_yield"):
            self.assertEqual(cold.terms[term], warm.terms[term], term)

    def test_a_stored_score_is_read_rather_than_recomputed(self):
        stored = dict(_candidate(), score=0.91, score_terms={"evidence_strength": 0.42})
        (item,) = offer_learning.evaluate([stored], offer_learning.learn([], now=OFFER_NOW),
                                          now=OFFER_NOW)
        self.assertEqual(item.base_score, 0.91)
        self.assertEqual(item.terms["evidence_strength"], 0.42)


class OfferLearningSeamTests(unittest.TestCase):
    """The seam PRs H/J plug into: two methods, one fake, one real."""

    def test_the_in_memory_fake_ranks_a_batch_end_to_end(self):
        source = offer_learning.MemoryOfferDecisionSource(candidates=[
            _candidate(key="binding-of-isaac-progression"),
            _candidate(key="supplement-stack", positive_signals=["creatine", "magnesium"]),
            _candidate(key="one-odd-pick", exploratory=True),
        ])
        source.decide("supplement-stack", offers.REJECTED, decided_at=_days_ago(5),
                      artifact_sha256="sha-run-1",
                      signal_terms=offers.signal_tokens("Supplement stack",
                                                        ["creatine", "magnesium"]))
        source.decide("older-thing", offers.ACCEPTED, decided_at=_days_ago(30),
                      artifact_sha256="sha-run-2",
                      signal_terms=offers.signal_tokens("Older thing", ["nebius"]))
        chosen, skipped = source.rank(now=OFFER_NOW)
        self.assertEqual(sorted(item.key for item in chosen),
                         ["binding-of-isaac-progression", "one-odd-pick"])
        self.assertIn("supplement-stack", [item.key for item in skipped])

    def test_the_store_backed_source_reads_decisions_out_of_offer_events(self):
        conn = db.connect(":memory:")
        db.init(conn)
        self.addCleanup(conn.close)
        candidate = _candidate(key="binding-of-isaac-progression")
        score, terms = offers.score_candidate(candidate, now=OFFER_NOW)
        offers.insert_offer(conn, dict(candidate, score=score, score_terms=terms), now=OFFER_NOW)
        offers.offer(conn, candidate["key"])
        offers.reject(conn, candidate["key"], note="not this", now=OFFER_NOW)

        source = offer_learning.StoreOfferDecisionSource(
            conn, candidates=[_candidate(key="binding-of-isaac-progression"),
                              _candidate(key="nbis-nebius", title="Nebius AI infrastructure",
                                         positive_signals=["nebius", "gpu cloud"])],
            now=OFFER_NOW)
        self.assertEqual([d.decision for d in source.decisions()], [offers.REJECTED])
        chosen, skipped = source.rank(now=OFFER_NOW)
        self.assertEqual([item.key for item in chosen], ["nbis-nebius"])
        self.assertIn("binding-of-isaac-progression", [item.key for item in skipped])


class SilentWebTickRegressionTests(unittest.TestCase):
    """The 2026-08-13..18 outage: web discovery was dead for five days while
    every monitor read healthy. Four independent defects lined up, and each
    one gets a test here.

      1. cdp.find_tab() prefix-matched the origin, so a scratch tab another
         tool parked on <origin>/robots.txt (the sibling `ai` repo's
         corpus_backfill.own_tab does exactly that) outranked the real
         logged-in tab -- /json lists newest first. The provider then drove a
         tab it did not own, and lost its websocket the moment the owner
         closed it.
      2. _poll() treated that dropped socket as "the answer is empty",
         throwing away a Council reply that was still being generated.
      3. The resulting ProviderError said "empty completion" and named no
         cause, so the log could not have explained it even if it had one.
      4. The tick returned exit 0 regardless, so _run_job stamped
         job:web:last_ok and `health` kept calling web healthy.
    """

    # --- 1: never hijack another tool's scratch tab -------------------------

    def test_find_tab_skips_a_scratch_tab_parked_on_the_same_origin(self):
        listed = [
            {"type": "page", "url": "https://chatgpt.com/robots.txt"},   # newest, another tool's
            {"type": "page", "url": "https://chatgpt.com/"},             # the real logged-in tab
        ]
        with mock.patch.object(chatgpt_browser.cdp, "list_tabs", return_value=listed):
            self.assertEqual(chatgpt_browser.cdp.find_chatgpt_tab(9222)["url"], "https://chatgpt.com/")

    def test_only_a_scratch_tab_reads_as_no_tab_at_all(self):
        # "There is a tab" was the old answer, and it was worse than useless:
        # preflight passed and every call then failed deep inside the run.
        with mock.patch.object(
            chatgpt_browser.cdp, "list_tabs", return_value=[{"type": "page", "url": "https://claude.ai/robots.txt"}]
        ):
            self.assertIsNone(chatgpt_browser.cdp.find_claude_tab(9222))

    # --- 2 + 3: a dropped poll is not an empty answer ----------------------

    def test_a_dropped_poll_socket_reconnects_instead_of_losing_the_answer(self):
        # The first poll dies the way Chrome kills a long-lived CDP socket
        # (WinError 10053, measured 166s into a real Council call). The answer
        # is still on chatgpt.com, so the retry reconnects and reads it back.
        dead = FakeChatGPTConnection(
            [chatgpt_handoff("c1")],
            poll_results=[ConnectionAbortedError("[WinError 10053] aborted")],
        )
        fresh = FakeChatGPTConnection([], poll_results=[{"text": '{"a": 7}', "done": True}])
        remaining = [dead, fresh]
        provider = chatgpt_browser.ChatGPTBrowserProvider(
            "auto", port=9222, connect=lambda: remaining.pop(0),
        )
        with mock.patch.object(chatgpt_browser.time, "sleep"):
            self.assertEqual(provider.complete_json("s", "p", self.SCHEMA), {"a": 7})
        self.assertTrue(dead.closed)          # the dead socket was reset, not reused
        self.assertIn("poll", fresh.calls)    # ...and the answer came off the new one

    def test_a_poll_that_never_finishes_says_why_instead_of_just_empty(self):
        conn = FakeChatGPTConnection(
            [chatgpt_handoff("c1")], poll_results=[{"text": "", "done": False}] * 50,
        )
        provider = chatgpt_browser.ChatGPTBrowserProvider("auto", port=9222, connect=lambda: conn)
        clock = iter([0.0, 1.0, 2.0, 999.0])
        with mock.patch.object(chatgpt_browser.time, "sleep"), \
                mock.patch.object(chatgpt_browser.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        message = str(ctx.exception)
        self.assertIn("empty completion", message)
        self.assertIn("still being generated", message)   # the part that was missing
        self.assertIn("c1", message)                      # ...and which conversation

    def test_repeated_read_failures_give_up_with_the_last_error_named(self):
        conns = [FakeChatGPTConnection([chatgpt_handoff("c9")], poll_results=[OSError("HTTP 429")])]
        conns += [FakeChatGPTConnection([], poll_results=[OSError("HTTP 429")]) for _ in range(20)]
        remaining = list(conns)
        provider = chatgpt_browser.ChatGPTBrowserProvider(
            "auto", port=9222, connect=lambda: remaining.pop(0),
        )
        with mock.patch.object(chatgpt_browser.time, "sleep"):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete_json("s", "p", self.SCHEMA)
        message = str(ctx.exception)
        self.assertIn("consecutive failed reads", message)
        self.assertIn("HTTP 429", message)

    # --- 4: a tick that did nothing must not look like a tick that worked ---

    SCHEMA = {"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]}

    def _tick_cfg(self):
        return dataclasses.replace(
            CFG, mission_provider="fake_mission", mission_low_water=1,
            missions_per_tick=2, council_missions_per_generation=2,
        )

    def _conn_with_interest(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.init(conn)
        db.upsert_interest(conn, an_interest(key="alpha", title="alpha", sources=["web_search"]))
        return conn

    def test_a_tick_whose_council_call_failed_is_not_productive_and_says_why(self):
        conn = self._conn_with_interest()
        mp = FakeCouncilProvider(mission_batches=[ProviderError("empty completion from chatgpt.com")])
        with mock.patch.object(providers, "get_provider", return_value=mp):
            result = missions.web_tick(conn, self._tick_cfg(), provider=FakeProvider({}), dry_run=True)
        self.assertTrue(result["preflight_ok"])
        self.assertFalse(result["productive"])
        self.assertEqual(result["generated"], 0)
        self.assertIn("empty completion from chatgpt.com", result["reason"])
        self.assertTrue(result["failures"])

    def test_the_cli_refuses_to_stamp_last_ok_for_a_tick_that_did_nothing(self):
        from discovery.__main__ import WEB_TICK_UNPRODUCTIVE, _run_job, _web_tick_cmd

        conn = self._conn_with_interest()
        cfg = self._tick_cfg()
        mp = FakeCouncilProvider(mission_batches=[ProviderError("empty completion from chatgpt.com")])
        buf = io.StringIO()
        with mock.patch.object(providers, "get_provider", return_value=mp), \
                contextlib.redirect_stdout(buf):
            code = _run_job(conn, "web", lambda: _web_tick_cmd(conn, FakeProvider({}), cfg, True))

        self.assertEqual(code, WEB_TICK_UNPRODUCTIVE)
        self.assertIsNone(db.state_get(conn, "job:web:last_ok"))
        self.assertIsNotNone(db.state_get(conn, "job:web:last_fail"))
        self.assertEqual(db.today_counts(conn).get("run_failed"), 1)
        printed = buf.getvalue()
        self.assertIn("web-tick did nothing", printed)               # it says so
        self.assertIn("empty completion from chatgpt.com", printed)  # ...and why

    def test_a_productive_tick_still_reports_success(self):
        from discovery.__main__ import _run_job, _web_tick_cmd

        conn = self._conn_with_interest()
        cfg = self._tick_cfg()
        mp = FakeCouncilProvider(
            mission_batches=[mission_batch("a1", "a2")],
            search_results={"a1": [], "a2": []},
        )
        with mock.patch.object(providers, "get_provider", return_value=mp), \
                contextlib.redirect_stdout(io.StringIO()):
            code = _run_job(conn, "web", lambda: _web_tick_cmd(conn, FakeProvider({}), cfg, True))
        self.assertEqual(code, 0)
        self.assertIsNotNone(db.state_get(conn, "job:web:last_ok"))

    def test_the_tick_stops_at_its_budget_and_reports_what_it_abandoned(self):
        # The scheduler fires this every 60s under a 30-minute
        # ExecutionTimeLimit: a tick that outruns the limit is killed, and a
        # killed tick reports nothing at all. It has to stop by itself.
        conn = self._conn_with_interest()
        cfg = dataclasses.replace(self._tick_cfg(), web_tick_budget_seconds=0)
        mp = FakeCouncilProvider(mission_batches=[mission_batch("a1", "a2")])
        with mock.patch.object(providers, "get_provider", return_value=mp):
            result = missions.web_tick(conn, cfg, provider=FakeProvider({}), dry_run=True)
        self.assertEqual(result["executed"], 0)
        self.assertEqual(result["abandoned"], result["leased"])
        self.assertTrue(result["leased"])
        self.assertEqual(mp.search_prompts, [])   # no mission was actually run
        self.assertIn("budget ran out", " ".join(result["failures"]))


# Generated from the live inbox, 2026-08-18. The conversation ids are
# renamed conv-NN but the SHARING PATTERN is exactly production's --
# that pattern is what the dedup rule reads, and what this pins.
_LIVE_INBOX_2026_08_18 = [
    ('extraction-shooters-competitive-fps',
     'Extraction shooters, battle royales and competitive FPS practice',
     ['balance patches changing weapon TTK, loot economy or extraction rules', 'measured comparisons of sensitivity, DPI, polling or audio configurations', 'pro-player settings and routine breakdowns with reasoning'],
     ['conv-00', 'conv-01', 'conv-02', 'conv-03']),
    ('competitive-shooter-performance',
     'Competitive shooter mechanics and performance tuning',
     ['aim-training protocols with measured results', 'latency, DPI and sensitivity measurement', 'audio/visual config methodology'],
     ['conv-02', 'conv-03', 'conv-04', 'conv-05']),
    ('game-systems-theorycrafting',
     'Game systems theorycrafting and build math',
     ['datamined formulas, scaling tables or hidden stat derivations', 'build-system teardowns for RPGs and action games', 'patch changes to underlying stat math rather than surface numbers'],
     ['conv-06', 'conv-07', 'conv-08', 'conv-04']),
    ('portfolio-construction-conviction-risk',
     'Concentrated portfolio construction and thesis discipline',
     ['empirical studies of concentration, drawdown behavior and sizing rules', 'scenario and probability-weighting frameworks with worked math', 'research on correlation clustering that breaks apparent diversification'],
     ['conv-09', 'conv-10', 'conv-11', 'conv-12']),
    ('roguelike-souls-run-design',
     'Roguelike, souls-like and run-based game design',
     ['mechanics and stat-math breakdowns', 'patch notes changing build systems', 'unlock routing and completion analysis'],
     ['conv-13', 'conv-14', 'conv-07', 'conv-08']),
    ('handheld-pc-gaming-steamos',
     'Handheld PC gaming, SteamOS and Linux compatibility',
     ['measured FPS/thermal/battery testing', 'Proton and SteamOS release notes', 'compatibility regressions and fixes'],
     ['conv-15', 'conv-16', 'conv-17', 'conv-13']),
    ('concentrated-portfolio-construction',
     'Concentrated portfolio construction and thesis discipline',
     ['empirical work on concentration and drawdown', 'position-sizing and Kelly-style frameworks', 'correlation regime studies'],
     ['conv-18', 'conv-19', 'conv-09', 'conv-12']),
    ('handheld-pc-gaming-linux',
     'Handheld PC gaming, SteamOS and compatibility tuning',
     ['Proton, SteamOS or driver releases changing title compatibility', 'benchmarked per-title settings with frame-time data', 'handheld hardware comparisons with thermal and battery measurements'],
     ['conv-20', 'conv-21', 'conv-15', 'conv-13']),
    ('roguelike-run-progression-design',
     'Roguelikes, roguelites and run-based progression design',
     ['patch notes and balance changes to item pools, unlocks or difficulty tiers', 'designer interviews or post-mortems on run structure and RNG mitigation', 'new roguelite releases with an unusual progression or unlock system'],
     ['conv-22', 'conv-23', 'conv-17', 'conv-14']),
    ('evolutionary-mismatch-prehistory',
     'Evolutionary mismatch, deprivation and human prehistory',
     ['new archaeological dating or material evidence', 'comparative hunter-gatherer cognition studies', 'developmental deprivation case analysis'],
     ['conv-24', 'conv-25', 'conv-26', 'conv-27']),
]

_LIVE_INBOX_SCORES = [0.8988, 0.8230, 0.8205, 0.8147, 0.8079,
                      0.8022, 0.7947, 0.7883, 0.7755, 0.7113]


def _live_offer(entry, score):
    """One live offer as the store holds it, built from the fixture above."""
    key, title, signals, convs = entry
    return {
        "key": key, "kind": "new", "title": title, "description": "",
        "positive_signals": list(signals), "negative_signals": [],
        "suggested_sources": ["web_search"],
        "evidence": [
            # The quote is keyed on the CONVERSATION, as in production: when
            # two offers cite the same conversation they quote the same words,
            # so a merge dedupes those and keeps only what is genuinely new.
            {"date": "2026-08-01", "quote": f"{c} quote", "lang": "en",
             "depth": 0.7, "conversation_id": c}
            for c in convs
        ],
        "source_conversations": list(convs),
        "durability": {"n_convs": 8, "active_months": 4, "recency_days": 5},
        "score": score, "score_terms": {}, "similarity": [],
    }



class LiveInboxDuplicateRegressionTests(unittest.TestCase):
    """The four near-duplicate pairs that were actually live in the owner's
    inbox on 2026-08-18, pinned as fixtures.

    Two of the four are invisible to lexical matching, which is why the inbox
    check that existed (exact key only) let all four in:

      * 'extraction-shooters-competitive-fps' and
        'competitive-shooter-performance' share only 35% of their signal
        tokens -- under the .50 lexical bar -- yet come out of the same
        aim-training and mouse-sensitivity conversations.
      * 'roguelike-souls-run-design' and 'game-systems-theorycrafting' share
        NO key token at all, yet both describe tear-delay stat math, scaling
        curves and build/item interaction, from the same two conversations.

    So the rule these tests pin is evidence overlap, not word overlap: the
    share of the smaller offer's source conversations that both offers cite.
    """

    # The four pairs, loser -> survivor. The survivor of each is the offer
    # standing on the most evidence, ties broken by score -- see
    # duplicate_pairs(): the composite score is unanchored, so it is only ever
    # a tiebreak between offers carrying equal evidence, never a threshold.
    EXPECTED = {
        "competitive-shooter-performance": "extraction-shooters-competitive-fps",
        "roguelike-souls-run-design": "game-systems-theorycrafting",
        "concentrated-portfolio-construction": "portfolio-construction-conviction-risk",
        "handheld-pc-gaming-linux": "handheld-pc-gaming-steamos",
    }

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        for entry, score in zip(_LIVE_INBOX_2026_08_18, _LIVE_INBOX_SCORES):
            row = _live_offer(entry, score)
            offers.insert_offer(self.conn, row, now=OFFER_NOW)
            offers.offer(self.conn, row["key"])
        self.assertEqual(offers.live_offer_count(self.conn), 10)

    def test_exactly_the_four_real_pairs_are_found(self):
        found = {loser: winner for loser, winner, _why in offers.duplicate_pairs(self.conn)}
        self.assertEqual(found, self.EXPECTED)

    def test_the_measured_separation_is_a_clean_gap_with_nothing_in_between(self):
        """The boundary a human can check: every duplicate pair shares half of
        the smaller offer's conversations, every other pair at most a quarter,
        and no pair falls in between. The bar sits at the bottom of the
        duplicate cluster rather than at a number chosen to look principled."""
        rows = offers.list_offers(self.conn, status=offers.OFFERED)
        convs = {r["key"]: offers.evidence_conversations(r) for r in rows}
        duplicates, distinct = [], []
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                share, _n = offers._evidence_overlap(convs[a["key"]], convs[b["key"]])
                pair = {a["key"], b["key"]}
                is_dupe = any(pair == {lo, wi} for lo, wi in self.EXPECTED.items())
                (duplicates if is_dupe else distinct).append(share)
        self.assertEqual(len(duplicates), 4)
        self.assertEqual(len(distinct), 41)
        self.assertEqual(min(duplicates), 0.50)
        self.assertEqual(max(distinct), 0.25)
        self.assertGreaterEqual(min(duplicates),
                                offers.DEFAULT_RULES.evidence_overlap_duplicate)
        self.assertLess(max(distinct), offers.DEFAULT_RULES.evidence_overlap_duplicate)

    def test_the_pair_that_only_looks_duplicate_by_name_is_left_alone(self):
        """'roguelike-souls-run-design' and 'roguelike-run-progression-design'
        share a key stem and read alike, but rest on different conversations --
        one is stat math, the other meta-progression -- and are two real
        interests. A lexical rule would have merged them and cost a suggestion.
        """
        offers.reconcile_duplicates(self.conn, now=OFFER_NOW)
        self.assertEqual(
            offers.get_offer(self.conn, "roguelike-run-progression-design")["status"],
            offers.OFFERED,
        )

    def test_reconciling_leaves_six_distinct_offers_and_no_pair_behind(self):
        summary = offers.reconcile_duplicates(self.conn, now=OFFER_NOW)
        self.assertEqual(summary["merged"], 4)
        self.assertEqual(offers.live_offer_count(self.conn), 6)
        self.assertEqual(offers.duplicate_pairs(self.conn), [])
        self.assertEqual(
            sorted(r["key"] for r in offers.inbox(self.conn)),
            sorted(["extraction-shooters-competitive-fps", "game-systems-theorycrafting",
                    "portfolio-construction-conviction-risk", "handheld-pc-gaming-steamos",
                    "roguelike-run-progression-design", "evolutionary-mismatch-prehistory"]),
        )

    def test_a_merge_keeps_the_losers_evidence_on_the_survivor(self):
        """Both offers were written from real conversations; a merge must cost
        the owner none of those quotes."""
        before = len(offers.get_offer(
            self.conn, "extraction-shooters-competitive-fps")["evidence"])
        loser = offers.get_offer(self.conn, "competitive-shooter-performance")
        offers.reconcile_duplicates(self.conn, now=OFFER_NOW)
        survivor = offers.get_offer(self.conn, "extraction-shooters-competitive-fps")
        # The two conversations it did NOT already have are now on the survivor.
        self.assertEqual(len(survivor["evidence"]), before + 2)
        self.assertEqual(
            offers.evidence_conversations(survivor),
            {"conv-00", "conv-01", "conv-02", "conv-03", "conv-04", "conv-05"},
        )
        self.assertTrue(
            {q["quote"] for q in loser["evidence"]}
            & {q["quote"] for q in survivor["evidence"]}
        )

    def test_the_superseded_offer_reads_as_a_merge_not_a_timeout(self):
        offers.reconcile_duplicates(self.conn, now=OFFER_NOW)
        loser = offers.get_offer(self.conn, "competitive-shooter-performance")
        self.assertEqual(loser["status"], offers.EXPIRED)
        events = offers.offer_events(self.conn, "competitive-shooter-performance")
        (merge,) = [e for e in events if e["action"] == "supersede"]
        self.assertEqual(merge["detail"]["superseded_by"],
                         "extraction-shooters-competitive-fps")
        self.assertIn("same evidence", merge["detail"]["why"])
        # ...and the survivor's own chain records what it absorbed.
        (absorb,) = [e for e in offers.offer_events(
            self.conn, "extraction-shooters-competitive-fps") if e["action"] == "absorb"]
        self.assertEqual(absorb["detail"]["absorbed"], "competitive-shooter-performance")

    def test_a_merge_never_blocklists_the_survivors_own_terms(self):
        """Why a merge expires the loser instead of rejecting it: rejection
        blocklists the theme's signal tokens for 180 days, and the survivor is
        built from those very tokens -- a reject-based merge would suppress the
        survivor and every future offer like it."""
        offers.reconcile_duplicates(self.conn, now=OFFER_NOW)
        blocked = offers.blocked_offer_keys(self.conn, now=OFFER_NOW)
        for key in self.EXPECTED.values():
            self.assertNotIn(key, blocked)
            self.assertNotIn(offers.normalize_key(key), blocked)

    def test_a_decided_offer_is_never_superseded(self):
        offers.accept(self.conn, "competitive-shooter-performance", now=OFFER_NOW)
        summary = offers.reconcile_duplicates(self.conn, now=OFFER_NOW)
        self.assertEqual(
            offers.get_offer(self.conn, "competitive-shooter-performance")["status"],
            offers.ACCEPTED,
        )
        self.assertNotIn("competitive-shooter-performance",
                         [p["superseded"] for p in summary["pairs"]])

    def test_reconciling_twice_changes_nothing_the_second_time(self):
        first = offers.reconcile_duplicates(self.conn, now=OFFER_NOW)
        second = offers.reconcile_duplicates(self.conn, now=OFFER_NOW)
        self.assertEqual(first["merged"], 4)
        self.assertEqual(second["merged"], 0)
        self.assertIn("no near-duplicate pairs", second["reason"])
        self.assertEqual(offers.live_offer_count(self.conn), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
