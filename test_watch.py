#!/usr/bin/env python3
"""Tests for watch.py. Network is always stubbed -- these never hit Yahoo."""

import json
import os
import tempfile
import unittest
from unittest import mock

import watch


def chart_payload(closes, timestamps=None, live=None, gmtoffset=-14400):
    """Build a Yahoo-shaped chart payload from a list of closes."""
    timestamps = timestamps or [1786000000 + i * 86400 for i in range(len(closes))]
    meta = {"currency": "USD", "gmtoffset": gmtoffset}
    if live is not None:
        meta["regularMarketPrice"] = live
        meta["regularMarketTime"] = timestamps[-1]
    return {
        "meta": meta,
        "timestamp": timestamps,
        "indicators": {"quote": [{"close": closes}]},
    }


class LoadWatchlistTest(unittest.TestCase):
    def _write(self, obj):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(obj, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_bare_string_inherits_defaults(self):
        path = self._write(
            {"default_schedule": "weekly", "default_min_change_pct": 2, "tickers": ["nbis"]}
        )
        (entry,) = watch.load_watchlist(path)
        self.assertEqual(entry["ticker"], "NBIS")  # upper-cased
        self.assertEqual(entry["schedule"], "weekly")
        self.assertEqual(entry["min_change_pct"], 2.0)

    def test_object_overrides_defaults(self):
        path = self._write(
            {
                "default_schedule": "daily",
                "tickers": [{"ticker": "MSFT", "schedule": "hourly", "min_change_pct": 5}],
            }
        )
        (entry,) = watch.load_watchlist(path)
        self.assertEqual(entry["schedule"], "hourly")
        self.assertEqual(entry["min_change_pct"], 5.0)

    def test_unknown_schedule_is_rejected(self):
        path = self._write({"tickers": [{"ticker": "X", "schedule": "monthly"}]})
        with self.assertRaisesRegex(watch.WatchError, "unknown schedule"):
            watch.load_watchlist(path)

    def test_missing_file_names_the_example(self):
        with self.assertRaisesRegex(watch.WatchError, "watchlist.example.json"):
            watch.load_watchlist("does-not-exist.json")

    def test_empty_watchlist_is_rejected(self):
        path = self._write({"tickers": []})
        with self.assertRaisesRegex(watch.WatchError, "no tickers"):
            watch.load_watchlist(path)


class PriceChangeTest(unittest.TestCase):
    def test_daily_compares_against_previous_bar(self):
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload([100.0, 110.0])):
            c = watch.price_change("X", "daily")
        self.assertAlmostEqual(c["then_price"], 100.0)
        self.assertAlmostEqual(c["now_price"], 110.0)
        self.assertAlmostEqual(c["pct"], 10.0)

    def test_weekly_compares_five_bars_back(self):
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]  # 6 bars, lookback 5
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload(closes)):
            c = watch.price_change("X", "weekly")
        self.assertAlmostEqual(c["then_price"], 100.0)
        self.assertAlmostEqual(c["pct"], 5.0)

    def test_null_bars_are_skipped_not_counted(self):
        # Yahoo pads with nulls; lookback must count real bars only. With the
        # nulls dropped this is [100, 200], so daily = +100%.
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload([100.0, None, 200.0])):
            c = watch.price_change("X", "daily")
        self.assertAlmostEqual(c["pct"], 100.0)

    def test_live_quote_overrides_last_bar(self):
        payload = chart_payload([100.0, 110.0], live=115.0)
        with mock.patch.object(watch, "fetch_chart", return_value=payload):
            c = watch.price_change("X", "daily")
        self.assertAlmostEqual(c["now_price"], 115.0)
        self.assertAlmostEqual(c["pct"], 15.0)

    def test_not_enough_bars_raises(self):
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload([100.0])):
            with self.assertRaisesRegex(watch.WatchError, "usable bars"):
                watch.price_change("X", "daily")

    def test_negative_change(self):
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload([200.0, 150.0])):
            c = watch.price_change("X", "daily")
        self.assertAlmostEqual(c["pct"], -25.0)
        self.assertIn("-25.00%", watch.format_line(c))

    def test_format_line_is_ascii_only(self):
        # Guards against re-introducing arrows/emoji: this string is printed
        # to a Windows console that may be on a non-UTF-8 codepage.
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload([100.0, 110.0])):
            line = watch.format_line(watch.price_change("X", "daily"))
        line.encode("ascii")  # raises UnicodeEncodeError if not ASCII


class RunTest(unittest.TestCase):
    def setUp(self):
        self.pushes = []
        p = mock.patch.object(
            watch, "ntfy_notify", side_effect=lambda *a, **k: self.pushes.append((a, k)) or True
        )
        p.start()
        self.addCleanup(p.stop)

    def _entries(self, ticker="X", threshold=0.0):
        return [{"ticker": ticker, "schedule": "daily", "min_change_pct": threshold}]

    def test_pushes_when_threshold_met(self):
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload([100.0, 110.0])):
            rc = watch.run(self._entries(threshold=5.0))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("+10.00%", self.pushes[0][0][0])  # title

    def test_silent_when_below_threshold(self):
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload([100.0, 101.0])):
            rc = watch.run(self._entries(threshold=5.0))
        self.assertEqual(rc, 0)
        self.assertEqual(self.pushes, [])

    def test_threshold_is_symmetric(self):
        # A -10% move must alert on a 5% threshold just like +10% does.
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload([100.0, 90.0])):
            watch.run(self._entries(threshold=5.0))
        self.assertEqual(len(self.pushes), 1)

    def test_one_bad_ticker_does_not_abort_the_rest(self):
        def fake_fetch(ticker, *a, **k):
            if ticker == "BAD":
                raise watch.WatchError("BAD: no data returned (bad symbol?)")
            return chart_payload([100.0, 110.0])

        entries = self._entries("BAD") + self._entries("GOOD")
        with mock.patch.object(watch, "fetch_chart", side_effect=fake_fetch):
            rc = watch.run(entries)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("GOOD", self.pushes[0][0][1])  # body
        self.assertIn("Failed", self.pushes[0][0][1])

    def test_dry_run_does_not_push(self):
        with mock.patch.object(watch, "fetch_chart", return_value=chart_payload([100.0, 110.0])):
            watch.run(self._entries(), dry_run=True)
        self.assertEqual(self.pushes, [])

    def test_title_names_biggest_mover(self):
        def fake_fetch(ticker, *a, **k):
            return chart_payload([100.0, 101.0] if ticker == "SMALL" else [100.0, 150.0])

        entries = self._entries("SMALL") + self._entries("BIG")
        with mock.patch.object(watch, "fetch_chart", side_effect=fake_fetch):
            watch.run(entries)
        title = self.pushes[0][0][0]
        self.assertIn("2 movers", title)
        self.assertIn("BIG", title)


class NtfyTest(unittest.TestCase):
    def test_no_topic_means_no_request(self):
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": ""}, clear=False):
            with mock.patch.object(watch.urllib.request, "urlopen") as urlopen:
                self.assertFalse(watch.ntfy_notify("t", "m"))
        urlopen.assert_not_called()

    def test_push_failure_is_swallowed(self):
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": "topic"}, clear=False):
            with mock.patch.object(watch.urllib.request, "urlopen", side_effect=OSError("down")):
                self.assertFalse(watch.ntfy_notify("t", "m"))  # returns, does not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
