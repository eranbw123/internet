#!/usr/bin/env python3
"""Offline tests for the discovery engine.

Same shape as test_watch.py: stdlib unittest, run with `python test_discovery.py`,
network fully stubbed. Nothing here touches an LLM API, Telegram, or Yahoo --
the pipeline holds an LLMProvider, so a fake object with `complete_json` /
`search_json` is the whole seam.
"""
import dataclasses
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from discovery import (
    config,
    db,
    dedup,
    feedback_listener,
    health,
    interest_state,
    interests,
    matching,
    models,
    normalize,
    notify,
    personal_state,
    pipeline,
    providers,
    scoring,
    stats,
    teach,
)
from discovery.personal_state import PersonalState, PersonalStateError
from discovery.collectors import COLLECTORS, stocks, web_search, youtube
from discovery.models import CandidateItem, Interest, ScoreResult
from discovery.providers import PROVIDERS, claude_chat
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
        run.assert_called_once_with(["cmd", "/d", "/c", "chrome.cmd"], check=False, timeout=0)
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
        self.assertIn("max_candidate_videos", youtube_cfg)
        self.assertIn("max_transcript_fetches", youtube_cfg)

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
        path = self._write(self._artifact(contract_version=2))
        with self.assertRaises(PersonalStateError) as ctx:
            personal_state.load(path)
        message = str(ctx.exception)
        self.assertIn("2", message)
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

    def test_six_tasks_share_the_prefix_and_carry_the_right_app_args(self):
        names = [t.name for t in install_tasks.build_tasks(CFG)]
        self.assertEqual(len(names), 6)
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

    def test_rendered_xml_uses_the_d_flag_and_run_cmd(self):
        task = install_tasks.build_tasks(CFG)[0]
        xml = install_tasks.render_xml(task)
        self.assertIn("/d /c", xml)
        self.assertIn("run.cmd", xml)
        self.assertIn("StartWhenAvailable>true<", xml)
        self.assertIn("InteractiveToken", xml)
        self.assertIn("IgnoreNew", xml)

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
        # /create's exit code alone to mean the task actually exists.
        self.assertEqual(len(calls), 12)
        creates = [a for a in calls if "/create" in a]
        queries = [a for a in calls if "/query" in a]
        self.assertEqual(len(creates), 6)
        self.assertEqual(len(queries), 6)
        for args in creates:
            self.assertEqual(args[0], "schtasks")
            self.assertIn("/xml", args)
        for args in queries:
            self.assertEqual(args, ["schtasks", "/query", "/tn", args[-1]])

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

    def test_soak_task_is_not_one_of_the_six_build_tasks(self):
        # SOAK_TASK must stay out of _TASK_SPECS/build_tasks -- otherwise
        # --install would create it (seven tasks) and recreate/reschedule it
        # on every reinstall.
        names = [t.name for t in install_tasks.build_tasks(CFG)]
        self.assertEqual(len(names), 6)
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

    def test_build_x_entry_not_implemented_regardless_of_provider_ok(self):
        # x has no sampler at all in this harness version -- the status must
        # say so plainly and identically whether or not a live provider is
        # reachable (repair: it used to say PENDING unconditionally, implying
        # a live session alone would resolve it).
        for provider_ok, provider_why in ((False, "CLAUDE_ORG_ID is not set"), (True, "")):
            with self.subTest(provider_ok=provider_ok):
                entry = exp_connectors.build_x_entry(["ai-agents-dev-tools"], provider_ok, provider_why)
                self.assertEqual(entry["availability"]["status"], "NOT_IMPLEMENTED")
                self.assertEqual(entry["sample"]["status"], "NOT_IMPLEMENTED")
                self.assertIsNone(entry["metrics"]["marginal_unique_rate"])
                self.assertEqual(entry["metrics"]["marginal_unique_rate_status"], "NOT_IMPLEMENTED")
                self.assertIsNone(entry["command_to_complete"])
                self.assertTrue(entry["follow_up"])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
