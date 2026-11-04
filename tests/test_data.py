import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backtest.engine import DAY, MINUTE, date_ms
from backtest.okx import OKX, Cancelled
from backtest.runner import run

START = date_ms("2026-01-01")


def row(ts, o="100", h="101", l="99", close="100", confirmed="1"):
    return [str(ts), o, h, l, close, "1", "1", "1", confirmed]


class DataTests(unittest.TestCase):
    def test_reverse_pagination_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            client = OKX(directory)
            with patch.object(client, "get", side_effect=[
                [row(START + MINUTE), row(START)],
            ]) as get:
                data = client.candles("TEST-USDT-SWAP", "1m", START, START + 2 * MINUTE)
                self.assertEqual([c.ts for c in data], [START, START + MINUTE])
                self.assertEqual(get.call_args.args[1]["after"], str(START + 2 * MINUTE))
            with patch.object(client, "get", side_effect=AssertionError("cache not used")):
                self.assertEqual(len(client.candles("TEST-USDT-SWAP", "1m", START, START + 2 * MINUTE)), 2)

    def test_multipage_exclusive_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            client = OKX(directory)
            with patch.object(client, "get", side_effect=[
                [row(START + 2 * MINUTE), row(START + MINUTE)], [row(START)],
            ]) as get:
                data = client.candles("TEST", "1m", START, START + 3 * MINUTE)
                self.assertEqual(len(data), 3)
                self.assertEqual(get.call_args_list[1].args[1]["after"], str(START + MINUTE))

    def test_unconfirmed_and_empty_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            client = OKX(directory)
            with patch.object(client, "get", return_value=[row(START, confirmed="0")]):
                self.assertEqual(client.candles("TEST", "1m", START, START + MINUTE), [])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            stop.set()
            with self.assertRaises(Cancelled):
                OKX(directory, stop).candles("TEST", "1m", START, START + MINUTE)

    def test_missing_daily_reported_not_no_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            client = OKX(directory)
            with patch.object(client, "instruments", return_value=[{"instId":"TEST-USDT-SWAP", "listTime":"0"}]), patch.object(client, "candles", return_value=[]), patch("backtest.runner.time.time", return_value=(START + 3 * DAY) / 1000):
                result = run(client, {"start":"2026-01-01", "end":"2026-01-01"})
            self.assertFalse(result["complete"])
            self.assertEqual(len(result["errors"]), 1)
            self.assertEqual(result["coverage"]["checked_days"], 0)

    def test_listing_day_without_midnight_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            client = OKX(directory)
            with patch.object(client, "instruments", return_value=[{"instId":"TEST-USDT-SWAP", "listTime":str(START + MINUTE)}]), patch.object(client, "candles") as candles, patch("backtest.runner.time.time", return_value=(START + 3 * DAY) / 1000):
                result = run(client, {"start":"2026-01-01", "end":"2026-01-01"})
            candles.assert_not_called()
            self.assertEqual(result["coverage"]["excluded_listing_days"], 1)


if __name__ == "__main__":
    unittest.main()
