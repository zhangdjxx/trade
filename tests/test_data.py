import json
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backtest.engine import Candle, DAY, MINUTE, date_ms, stamp
from backtest.okx import OKX, Cancelled
from backtest.runner import run

START = date_ms("2026-01-01")


def row(ts, o="100", h="101", l="99", close="100", confirmed="1"):
    return [str(ts), o, h, l, close, "1", "1", "1", confirmed]


class DataTests(unittest.TestCase):
    def test_lower_entry_setting_downloads_newly_eligible_minutes(self):
        from unittest.mock import Mock
        def data(inst, bar, start, end):
            if bar == "1D":
                return [Candle.parse(row(START, "100", "107", "99", "106"))]
            return [Candle.parse(row(START)),
                    Candle.parse(row(START + MINUTE, "104.9", "106", "104.5", "105")),
                    Candle.parse(row(START + 2 * MINUTE, "105", "107", "104", "106"))]
        client = Mock(requests=0)
        client.instruments.return_value = [{"instId":"TEST-USDT-SWAP", "listTime":"0"}]
        client.candles.side_effect = data
        with patch("backtest.runner.time.time", return_value=(START + DAY) / 1000):
            config = {"start":"2026-01-01", "end":"2026-01-01", "take_profit_pct":"1"}
            old = run(client, config)
            self.assertEqual(old["trades"], [])
            self.assertEqual(client.candles.call_count, 1)
            client.candles.reset_mock()
            new = run(client, {**config, "entry_pct":"5"})
        self.assertEqual(new["summary"], {"success":1})
        self.assertTrue(any(call.args[1] == "1m" for call in client.candles.call_args_list))

    def test_long_hold_downloads_only_entry_and_possible_exit_days(self):
        class Client:
            requests = 0
            minute_days = []
            def check(self):
                pass
            def instruments(self):
                return [{"instId":"TEST-USDT-SWAP", "listTime":"0"}]
            def candles(self, inst, bar, start, end):
                self.requests += 1
                if bar == "1D":
                    return [Candle.parse(row(START, "100", "111", "99", "110"))] + [
                        Candle.parse(row(START + i * DAY, "110", "114" if i == 60 else "112", "108", "110"))
                        for i in range(1, 61)]
                self.minute_days.append(start)
                if start == START:
                    return [Candle.parse(row(START))] + [
                        Candle.parse(row(START + i * MINUTE, "109.5", "111", "109", "110"))
                        for i in range(1, 1440)]
                return [Candle.parse(row(start, "110", "114", "109", "113"))]
        client = Client()
        with patch("backtest.runner.time.time", return_value=(START + 61 * DAY) / 1000):
            result = run(client, {"start":"2026-01-01", "end":stamp(START + 60 * DAY)[:10], "entry_pct":"10.5"})
        self.assertTrue(result["complete"])
        self.assertEqual(result["summary"], {"success":1})
        self.assertEqual(client.minute_days, [START, START + 60 * DAY])
        self.assertEqual(result["holding_days_skipped"], 59)

    def test_reuse_overlap_and_fetch_only_missing_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            client = OKX(directory)
            with patch.object(client, "get", return_value=[row(START + MINUTE), row(START)]):
                client.candles("TEST", "1m", START, START + 2 * MINUTE)
            client = OKX(directory)
            with patch.object(client, "get", return_value=[row(START + 2 * MINUTE)]) as get:
                result = client.candles("TEST", "1m", START, START + 3 * MINUTE)
            self.assertEqual(len(result), 3)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(get.call_args.args[1]["after"], str(START + 3 * MINUTE))
            self.assertEqual(client.cache_hits, 1)
            client = OKX(directory)
            with patch.object(client, "get", side_effect=AssertionError("Should reuse SQL cache")):
                self.assertEqual(len(client.candles("TEST", "1m", START + MINUTE, START + 3 * MINUTE)), 2)

    def test_cancelled_download_resumes_saved_page(self):
        with tempfile.TemporaryDirectory() as directory:
            client = OKX(directory)
            with patch.object(client, "get", side_effect=[[row(START + MINUTE)], Cancelled()]):
                with self.assertRaises(Cancelled):
                    client.candles("TEST", "1m", START, START + 2 * MINUTE)
            client = OKX(directory)
            with patch.object(client, "get", return_value=[row(START)]) as get:
                result = client.candles("TEST", "1m", START, START + 2 * MINUTE)
            self.assertEqual(len(result), 2)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(get.call_args.args[1]["after"], str(START + MINUTE))

    def test_legacy_json_is_preserved_and_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            key = hashlib.sha256(f"v1:TEST:1m:{START}:{START + 2 * MINUTE}".encode()).hexdigest()
            file = Path(directory) / (key + ".json")
            content = json.dumps([row(START), row(START + MINUTE)])
            file.write_text(content)
            with patch.object(OKX, "get", side_effect=AssertionError("No network needed")):
                self.assertEqual(len(OKX(directory).candles("TEST", "1m", START, START + 2 * MINUTE)), 2)
                self.assertEqual(len(OKX(directory).candles("TEST", "1m", START + MINUTE, START + 2 * MINUTE)), 1)
            self.assertEqual(file.read_text(), content)

    def test_cache_does_not_mix_instruments_or_bars(self):
        with tempfile.TemporaryDirectory() as directory:
            client = OKX(directory)
            with patch.object(client, "get", return_value=[row(START)]):
                client.candles("TEST", "1m", START, START + MINUTE)
            with patch.object(client, "get", return_value=[row(START)]) as get:
                client.candles("OTHER", "1m", START, START + MINUTE)
                client.candles("TEST", "1D", START, START + DAY)
            self.assertEqual(get.call_count, 2)

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
