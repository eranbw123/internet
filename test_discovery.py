#!/usr/bin/env python3
"""Offline tests for the discovery engine.

Same shape as test_watch.py: stdlib unittest, run with `python test_discovery.py`,
network fully stubbed. Nothing here touches an LLM API, Telegram, or Yahoo --
the pipeline holds an LLMProvider, so a fake object with `complete_json` /
`search_json` is the whole seam.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from discovery import (
    config,
    db,
    dedup,
    interests,
    matching,
    models,
    normalize,
    notify,
    pipeline,
    scoring,
)
from discovery.collectors import COLLECTORS, stocks, web, web_search
from discovery.models import CandidateItem, Interest, ScoreResult
from discovery.providers.base import LLMProvider, UnsupportedCapability

CFG = config.Config(
    db_path=":memory:",
    interests_path="interests.json",
    provider="fake",
    model="fake-1",
    max_items_per_source=5,
    interval_seconds=60,
    min_match_score=0.25,
    min_text_chars=40,
    telegram_bot_token="",
    telegram_chat_id="",
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
    title to the value every dimension gets back; an Exception value raises."""

    name = "fake"

    def __init__(self, scores=None, search_results=None, model="fake-1"):
        super().__init__(model)
        self.scores = scores or {}
        self.search_results = search_results
        self.prompts = []
        self.search_prompts = []

    def complete_json(self, system, prompt, schema, max_tokens=2000):
        self.prompts.append(prompt)
        for needle, value in self.scores.items():
            if needle in prompt:
                if isinstance(value, Exception):
                    raise value
                return self._payload(value)
        raise AssertionError(f"FakeProvider got an unexpected prompt:\n{prompt}")

    def search_json(self, prompt, max_searches=5, max_tokens=8000):
        self.search_prompts.append(prompt)
        if self.search_results is None:
            raise UnsupportedCapability("fake provider has no search")
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


class InterestsFileTests(unittest.TestCase):
    def test_sample_file_loads_with_defaults_applied(self):
        loaded = interests.load_file("interests.json")
        keys = [i.key for i in loaded]
        self.assertEqual(keys, ["narcolepsy-eds", "nbis-nebius", "behavioral-psychology"])
        nbis = loaded[1]
        self.assertIn("stocks", nbis.sources)
        self.assertEqual(nbis.source_config["stocks"]["tickers"], ["NBIS"])

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

    def _load(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "i.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            return interests.load_file(path)


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
        rows = [{"verdict": "down", "title": "Sleep hygiene tips", "note": "listicle"}]
        item, matches = self._matches(an_interest(id=1))
        prompt = scoring._prompt(item, matches, rows)
        self.assertIn("good stuff", prompt)
        self.assertIn("bad stuff", prompt)
        self.assertIn("rejected: Sleep hygiene tips", prompt)
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


class WebCollectorTests(unittest.TestCase):
    @staticmethod
    def _provider(queries, **kw):
        """FakeProvider.complete_json is keyed by scoring prompts, so query
        generation needs its own stub -- same override pattern as the
        hallucinated-schema test above."""
        provider = FakeProvider(**kw)
        provider.complete_json = lambda *a, **kw: {"queries": queries}
        return provider

    def test_generates_queries_and_records_provenance(self):
        provider = self._provider(
            ["orexin agonist trial"],
            search_results=[{"title": "T", "url": "https://e.com/x", "summary": "S"}],
        )
        (item,) = web.collect(an_interest(), CFG, provider)
        self.assertEqual((item.source, item.type, item.url), ("web", "article", "https://e.com/x"))
        self.assertEqual(item.metadata["query"], "orexin agonist trial")
        self.assertIn("orexin agonist trial", provider.search_prompts[0])

    def test_dedups_urls_across_queries_and_respects_the_limit(self):
        provider = self._provider(
            ["q1", "q2", "q3"],
            search_results=[
                {"title": "A", "url": "https://e.com/a"},
                {"title": "A dup", "url": "https://e.com/a"},
            ],
        )
        interest = an_interest(source_config={"web": {"limit": 1}})
        items = web.collect(interest, CFG, provider)
        self.assertEqual(len(items), 1)

    def test_falls_back_to_the_interest_title_with_no_usable_queries(self):
        provider = self._provider([], search_results=[])
        items = web.collect(an_interest(title="Narcolepsy"), CFG, provider)
        self.assertEqual(items, [])
        self.assertIn("Narcolepsy", provider.search_prompts[0])

    def test_a_provider_without_search_raises_unsupported(self):
        provider = self._provider(["q1"])
        with self.assertRaises(UnsupportedCapability):
            web.collect(an_interest(), CFG, provider)


class StocksCollectorTests(unittest.TestCase):
    def _change(self, pct):
        from datetime import datetime, timezone

        return {
            "ticker": "NBIS",
            "schedule": "daily",
            "label": "1d",
            "currency": "USD",
            "then_price": 100.0,
            "then_at": datetime(2026, 8, 6, tzinfo=timezone.utc),
            "now_price": 100.0 + pct,
            "now_at": datetime(2026, 8, 7, tzinfo=timezone.utc),
            "delta": pct,
            "pct": pct,
        }

    def test_dedup_key_is_per_day_so_moves_do_not_collapse(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"], "min_change_pct": 1}},
        )
        with mock.patch.object(stocks.watch, "price_change", return_value=self._change(5.0)):
            (item,) = stocks.collect(interest, CFG, None)
        self.assertEqual(item.key(), "NBIS:daily:2026-08-07")
        self.assertEqual(item.type, "price_move")
        self.assertAlmostEqual(item.metadata["pct"], 5.0)

    def test_move_below_threshold_is_dropped(self):
        interest = an_interest(
            sources=["stocks"],
            source_config={"stocks": {"tickers": ["NBIS"], "min_change_pct": 4}},
        )
        with mock.patch.object(stocks.watch, "price_change", return_value=self._change(0.5)):
            self.assertEqual(stocks.collect(interest, CFG, None), [])


class NotifyTests(unittest.TestCase):
    def test_dry_run_prints_and_never_calls_the_network(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertTrue(notify.send(CFG, "hello", dry_run=True))
        urlopen.assert_not_called()

    def test_message_shows_the_score_out_of_100_with_reason_and_url(self):
        text = notify.format_message(an_interest(), an_item(), 0.91, "Real data.", "Has numbers.")
        self.assertIn("[91]", text)
        self.assertIn("Real data.", text)
        self.assertIn("Has numbers.", text)
        self.assertIn("https://e.com/a", text)

    def test_low_confidence_is_flagged_in_the_header(self):
        text = notify.format_message(an_interest(), an_item(), 0.91, "r", confidence=0.3)
        self.assertIn("low confidence", text.splitlines()[0])
        text = notify.format_message(an_interest(), an_item(), 0.91, "r", confidence=0.8)
        self.assertNotIn("low confidence", text)


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
        return lambda interest, cfg, provider: [
            an_item(source="fake", url="https://e.com/good", title="Good"),
            an_item(source="fake", url="https://e.com/meh", title="Meh"),
        ]

    def _run(self, provider, collector=None):
        with mock.patch.dict(COLLECTORS, {"fake": collector or self._collector()}):
            return pipeline.run_once(self.conn, provider, CFG, dry_run=True)

    def test_full_cycle_scores_everything_and_notifies_only_above_the_bar(self):
        provider = FakeProvider({"Good": 0.9, "Meh": 0.2})
        summary = self._run(provider)
        self.assertEqual(
            summary,
            {"collected": 2, "duplicate": 0, "filtered": 0, "already_scored": 0,
             "scored": 2, "errors": 0, "notified": 1},
        )

    def test_a_second_cycle_re_collects_but_never_re_scores_or_re_notifies(self):
        provider = FakeProvider({"Good": 0.9, "Meh": 0.2})
        self._run(provider)
        summary = self._run(provider)
        self.assertEqual(summary["duplicate"], 2)
        self.assertEqual((summary["scored"], summary["notified"]), (0, 0))
        self.assertEqual(len(provider.prompts), 2)  # only the first cycle paid

    def test_a_failing_collector_does_not_abort_the_cycle(self):
        def boom(interest, cfg, provider):
            raise RuntimeError("network down")

        summary = self._run(FakeProvider(), collector=boom)
        self.assertEqual(summary["collected"], 0)
        self.assertEqual(summary["errors"], 0)

    def test_a_failing_score_skips_only_that_item(self):
        provider = FakeProvider({"Good": RuntimeError("bad json"), "Meh": 0.95})
        summary = self._run(provider)
        self.assertEqual((summary["errors"], summary["scored"], summary["notified"]), (1, 1, 1))

    def test_an_item_left_unscored_by_a_dead_cycle_is_picked_up_next_time(self):
        provider = FakeProvider({"Good": RuntimeError("api down"), "Meh": 0.1})
        self._run(provider)
        unscored = self.conn.execute(
            "SELECT title FROM candidate_items WHERE prefilter_ok = 1 AND id NOT IN"
            " (SELECT item_id FROM scores)"
        ).fetchall()
        self.assertEqual([r["title"] for r in unscored], ["Good"])

        # Next cycle: collection dedups both, but the backlog still gets scored.
        summary = self._run(FakeProvider({"Good": 0.9, "Meh": 0.1}))
        self.assertEqual((summary["duplicate"], summary["scored"], summary["notified"]), (2, 1, 1))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
