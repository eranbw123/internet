#!/usr/bin/env python3
"""Offline tests for the discovery engine.

Same shape as test_watch.py: stdlib unittest, run with `python test_discovery.py`,
network fully stubbed. Nothing here touches an LLM API, Telegram, or Yahoo --
the pipeline holds an LLMProvider, so a fake object with `complete_json` /
`search_json` is the whole seam.
"""
import dataclasses
import json
import os
import tempfile
import unittest
from unittest import mock

from discovery import (
    config,
    db,
    dedup,
    feedback_listener,
    interests,
    matching,
    models,
    normalize,
    notify,
    pipeline,
    scheduler,
    scoring,
    stats,
)
from discovery.collectors import COLLECTORS, stocks, web, web_search, youtube
from discovery.models import CandidateItem, Interest, ScoreResult
from discovery.providers import PROVIDERS, claude_chat
from discovery.providers.base import LLMProvider, ProviderError, UnsupportedCapability

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


class InterestsFileTests(unittest.TestCase):
    def test_sample_file_loads_with_defaults_applied(self):
        loaded = interests.load_file("interests.json")
        keys = [i.key for i in loaded]
        self.assertEqual(keys, ["narcolepsy-eds", "nbis-nebius", "behavioral-psychology"])
        nbis = loaded[1]
        self.assertIn("stocks", nbis.sources)
        self.assertEqual(nbis.source_config["stocks"]["tickers"][0]["ticker"], "NBIS")

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


def a_video(**kw):
    base = dict(
        video_id="vid1",
        video_title="Some Podcast",
        channel="A Channel",
        published_at="2026-08-01T00:00:00Z",
        video_url="https://www.youtube.com/watch?v=vid1",
    )
    base.update(kw)
    return base


class YoutubeCollectorTests(unittest.TestCase):
    def test_chunk_transcript_slides_with_overlap(self):
        snippets = [FakeSnippet(f"word{i}", i * 10, 10) for i in range(10)]  # 0..100s
        chunks = youtube._chunk_transcript(snippets, window_seconds=30, overlap_seconds=10)
        starts = [c[0] for c in chunks]
        self.assertEqual(starts, [0, 20, 40, 60, 80])
        self.assertIn("word0", chunks[0][2])

    def test_chunk_transcript_empty_input(self):
        self.assertEqual(youtube._chunk_transcript([], 30, 10), [])
        self.assertEqual(youtube._chunk_transcript([FakeSnippet("  ", 0, 5)], 30, 10), [])

    def test_no_channels_or_queries_returns_empty_without_needing_a_key(self):
        interest = an_interest(sources=["youtube"], source_config={"youtube": {}})
        self.assertEqual(youtube.collect(interest, CFG, None), [])

    def test_missing_api_key_raises(self):
        interest = an_interest(
            sources=["youtube"], source_config={"youtube": {"channels": ["UC1"]}}
        )
        with self.assertRaises(youtube.YoutubeCollectorError):
            youtube.collect(interest, CFG, None)

    def test_collect_shapes_one_candidate_per_segment(self):
        cfg = dataclasses.replace(CFG, youtube_api_key="key")
        interest = an_interest(
            sources=["youtube"],
            source_config={
                "youtube": {
                    "channels": ["UC1"],
                    "chunk_seconds": 30,
                    "chunk_overlap_seconds": 10,
                }
            },
        )
        snippets = [FakeSnippet(f"segment about {i} orexin", i * 10, 10) for i in range(6)]
        with mock.patch.object(youtube, "_search_videos", return_value=[a_video()]), \
             mock.patch.object(youtube, "_fetch_transcript", return_value=snippets):
            items = youtube.collect(interest, cfg, None)

        self.assertGreaterEqual(len(items), 2)
        first, second = items[0], items[1]
        self.assertEqual(first.source, "youtube")
        self.assertEqual(first.type, "video_segment")
        self.assertEqual(first.metadata["video_id"], "vid1")
        self.assertEqual(first.metadata["channel"], "A Channel")
        self.assertEqual(first.metadata["start_time"], 0)
        self.assertIn("orexin", first.metadata["transcript"])
        self.assertTrue(first.url.startswith("https://www.youtube.com/watch?v=vid1&t="))

        # Two segments of the same video must not collide on any dedup layer:
        # distinct urls (survives fragment-stripping normalize.canonical_url),
        # distinct titles (mm:ss range), distinct dedup_keys.
        self.assertNotEqual(normalize.canonical_url(first.url), normalize.canonical_url(second.url))
        self.assertNotEqual(first.title, second.title)
        self.assertNotEqual(first.dedup_key, second.dedup_key)

    def test_an_already_chunked_video_is_not_re_fetched(self):
        """A search job re-lists the same uploads every few hours. Without the
        check, each run re-fetches and re-chunks a transcript whose segments
        dedup.py is only going to reject again."""
        conn = db.connect(":memory:")
        db.init(conn)
        self.addCleanup(conn.close)
        cfg = dataclasses.replace(CFG, youtube_api_key="key")
        interest = an_interest(
            sources=["youtube"], source_config={"youtube": {"channels": ["UC1"]}}
        )
        snippets = [FakeSnippet("real content about orexin agonists", 0, 10)]

        with mock.patch.object(youtube, "_search_videos", return_value=[a_video()]), \
             mock.patch.object(youtube, "_fetch_transcript", return_value=snippets) as fetch:
            items = youtube.collect(interest, cfg, None, conn)
            for item in items:
                normalize.normalize(item)
                db.insert_item(conn, item)
            self.assertEqual(fetch.call_count, 1)

            self.assertEqual(youtube.collect(interest, cfg, None, conn), [])
            self.assertEqual(fetch.call_count, 1)

    def test_a_video_with_no_transcript_is_skipped_not_fatal(self):
        interest = an_interest(
            sources=["youtube"], source_config={"youtube": {"channels": ["UC1"]}}
        )
        cfg = dataclasses.replace(CFG, youtube_api_key="key")
        videos = [a_video(video_id="no-transcript"), a_video(video_id="has-transcript")]
        snippets = [FakeSnippet("some real content here", 0, 10)]

        def fake_fetch(video_id, languages):
            return snippets if video_id == "has-transcript" else None

        with mock.patch.object(youtube, "_search_videos", return_value=videos), \
             mock.patch.object(youtube, "_fetch_transcript", side_effect=fake_fetch):
            items = youtube.collect(interest, cfg, None)

        self.assertTrue(items)
        self.assertTrue(all(i.metadata["video_id"] == "has-transcript" for i in items))

    def test_limit_stops_at_video_boundaries_never_mid_video(self):
        """A video is chunked in full or not at all: the seen-prefix skip means
        a half-chunked video would permanently lose its later segments."""
        interest = an_interest(
            sources=["youtube"],
            source_config={"youtube": {"channels": ["UC1"], "limit": 1,
                                       "chunk_seconds": 30, "chunk_overlap_seconds": 10}},
        )
        cfg = dataclasses.replace(CFG, youtube_api_key="key")
        videos = [a_video(video_id="first"), a_video(video_id="second")]
        snippets = [FakeSnippet(f"word{i}", i * 10, 10) for i in range(10)]  # 5 chunks
        fetched = []

        def fake_fetch(video_id, languages):
            fetched.append(video_id)
            return snippets

        with mock.patch.object(youtube, "_search_videos", return_value=videos), \
             mock.patch.object(youtube, "_fetch_transcript", side_effect=fake_fetch):
            items = youtube.collect(interest, cfg, None)

        # The first video overshoots the limit but is emitted whole; the
        # second is never even fetched.
        self.assertEqual(len(items), 5)
        self.assertEqual(fetched, ["first"])
        self.assertTrue(all(i.metadata["video_id"] == "first" for i in items))


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
            {"collected": 2, "duplicate": 0, "filtered": 0, "already_scored": 0,
             "scored": 2, "deferred": 0, "errors": 0, "notified": 0},
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


class SchedulerTests(unittest.TestCase):
    def _calls(self):
        calls = []

        def record(conn, provider, cfg, sources=None, dry_run=False):
            calls.append(sources)
            return {}

        return calls, record

    def test_first_tick_runs_every_collector_job(self):
        calls, record = self._calls()
        with mock.patch.object(scheduler, "run_once", side_effect=record), \
             mock.patch.object(scheduler, "send_digest", return_value=0), \
             mock.patch.object(scheduler, "_seconds_until_digest", return_value=10_000):
            scheduler.run_forever(None, None, CFG, dry_run=True, cycles=1)
        expected = [tuple(sources) for sources, _field in scheduler.JOBS.values()]
        self.assertEqual(sorted(map(tuple, calls)), sorted(expected))

    def test_digest_fires_only_once_its_due(self):
        with mock.patch.object(scheduler, "run_once", return_value={}), \
             mock.patch.object(scheduler, "send_digest", return_value=3) as send_digest, \
             mock.patch.object(scheduler, "_seconds_until_digest", return_value=0):
            scheduler.run_forever(None, None, CFG, dry_run=True, cycles=1)
        send_digest.assert_called_once()

    def test_a_job_not_yet_due_is_skipped_on_the_next_tick(self):
        cfg = dataclasses.replace(
            CFG, interval_stocks_seconds=10_000, interval_web_seconds=10_000,
            interval_youtube_seconds=10_000,
        )
        calls, record = self._calls()
        with mock.patch.object(scheduler, "run_once", side_effect=record), \
             mock.patch.object(scheduler, "send_digest", return_value=0), \
             mock.patch.object(scheduler, "_seconds_until_digest", return_value=10_000), \
             mock.patch.object(scheduler, "TICK_SECONDS", 0):
            scheduler.run_forever(None, None, cfg, dry_run=True, cycles=2)
        # Every job ran on the first tick only -- the second tick found nothing due.
        self.assertEqual(len(calls), len(scheduler.JOBS))


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
            feedback_listener._handle_callback(self.conn, "tok", self._callback(f"fb:fire:{self.score_id}"))
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
            feedback_listener._handle_callback(self.conn, "tok", self._callback("not-a-real-payload"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 0)
        api_call.assert_called_once_with("tok", "answerCallbackQuery", {"callback_query_id": "cb1"})

    def test_a_score_id_that_no_longer_exists_is_acked_without_a_crash(self):
        with mock.patch.object(feedback_listener, "api_call") as api_call:
            feedback_listener._handle_callback(self.conn, "tok", self._callback("fb:up:999999"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], 0)
        api_call.assert_called_once()


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


class SchemaContractTests(unittest.TestCase):
    def test_every_structured_output_schema_forbids_extra_properties(self):
        """The live structured-outputs API requires additionalProperties: false
        on every object -- a schema without it 400s on the first real call."""
        from discovery.collectors import stocks as stocks_module, web as web_module

        for schema in (scoring.SCORE_SCHEMA, web_module.QUERY_SCHEMA,
                       stocks_module.EXPLAIN_SCHEMA):
            self.assertIs(schema.get("additionalProperties"), False, schema)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
