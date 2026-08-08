#!/usr/bin/env python3
"""Tests for watch.py. Network is always stubbed -- these never hit Yahoo.

watch.py is a shared library (used by the `stocks` collector and
discovery/config.py), not a standalone CLI, so these tests cover only its
public surface: price_change/fetch_chart and load_dotenv.
"""

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


class FetchChartTest(unittest.TestCase):
    def test_http_error_is_wrapped(self):
        import urllib.error

        with mock.patch.object(
            watch.urllib.request,
            "urlopen",
            side_effect=urllib.error.HTTPError("url", 404, "not found", {}, None),
        ):
            with self.assertRaisesRegex(watch.WatchError, "HTTP 404"):
                watch.fetch_chart("X", "1mo", "1d")

    def test_no_results_is_wrapped(self):
        payload = {"chart": {"result": []}}
        with mock.patch.object(watch.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = (
                __import__("json").dumps(payload).encode("utf-8")
            )
            with self.assertRaisesRegex(watch.WatchError, "no data returned"):
                watch.fetch_chart("X", "1mo", "1d")


class LoadDotenvTest(unittest.TestCase):
    def test_missing_file_is_a_noop(self):
        watch.load_dotenv("does-not-exist.env")  # must not raise

    def test_existing_env_wins_over_file(self):
        import os
        import tempfile

        fh = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        fh.write("SOME_TEST_VAR=from_file\n")
        fh.close()
        self.addCleanup(os.unlink, fh.name)

        with mock.patch.dict(os.environ, {"SOME_TEST_VAR": "from_env"}, clear=False):
            watch.load_dotenv(fh.name)
            self.assertEqual(os.environ["SOME_TEST_VAR"], "from_env")


if __name__ == "__main__":
    unittest.main(verbosity=2)
