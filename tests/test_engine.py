import unittest
from decimal import Decimal as D

from backtest.engine import Candle, DAY, MINUTE, date_ms, simulate, stamp

START = date_ms("2026-01-01")


def c(n, o="100", h="101", l="99", close="100"):
    return Candle.parse([str(START + n * MINUTE), o, h, l, close])


def simulate_rows(rows, cutoff=None, following=()):
    return simulate("TEST-USDT-SWAP", START, D("100"), rows, following,
                    cutoff if cutoff is not None else START + len(rows) * MINUTE)


class EngineTests(unittest.TestCase):
    def test_timezone(self):
        self.assertEqual(START, 1767196800000)
        self.assertEqual(stamp(START), "2026-01-01T00:00+08:00")

    def test_strict_breakout_and_take_profit(self):
        r = simulate_rows([c(0, h="110"), c(1, o="106", h="111", l="105", close="110"),
                           c(2, o="111", h="114", l="110", close="113")])
        self.assertEqual(r["entry_time"], stamp(START + MINUTE))
        self.assertEqual(D(r["entry_price"]), D("110"))
        self.assertEqual(r["status"], "success")
        self.assertEqual(D(r["exit_price"]), D("113.3"))
        self.assertEqual(D(r["pnl_usdt"]), D("300"))

    def test_loss(self):
        r = simulate_rows([c(0, h="111", l="105", o="106", close="110"),
                           c(1, o="109", h="110", l="102", close="103")])
        self.assertEqual(r["status"], "failure")
        self.assertEqual(D(r["exit_price"]), D("103.4"))
        self.assertEqual(D(r["pnl_usdt"]), D("-600"))

    def test_both_exits_in_later_minute(self):
        r = simulate_rows([c(0, h="111", l="105", o="106", close="110"),
                           c(1, o="110", h="115", l="100", close="110")])
        self.assertEqual(r["status"], "ambiguous")
        self.assertIsNone(r["exit_time"])
        self.assertIsNone(r["pnl_usdt"])

    def test_entry_candle_stop_may_precede_entry(self):
        r = simulate_rows([c(0, h="111", l="99")])
        self.assertEqual(r["status"], "ambiguous")

    def test_entry_candle_tp_without_stop(self):
        r = simulate_rows([c(0, o="108", h="114", l="105", close="113")])
        self.assertEqual(r["status"], "success")

    def test_gap_entry_uses_open(self):
        r = simulate_rows([c(0, o="112", h="113", l="110", close="112")])
        self.assertEqual(D(r["entry_price"]), D("112"))
        self.assertEqual(r["status"], "open")

    def test_gap_exit_slippage(self):
        r = simulate_rows([c(0, o="108", h="111", l="105", close="110"),
                           c(1, o="100", h="115", l="99", close="110")])
        self.assertEqual(r["status"], "failure")
        self.assertEqual(D(r["exit_price"]), D("100"))

    def test_missing_minute_is_error(self):
        r = simulate_rows([c(1, o="108", h="114", l="105", close="113")])
        self.assertEqual(r["status"], "data_error")
        self.assertIsNone(r["entry_price"])

    def test_partial_history_is_not_open(self):
        r = simulate_rows([c(0, o="108", h="111", l="105", close="110")], START + 2 * MINUTE)
        self.assertEqual(r["status"], "data_error")

    def test_cross_day_holding(self):
        rows = [c(0, o="108", h="111", l="105", close="110")]
        rows += [c(i, o="110", h="111", l="109", close="110") for i in range(1, 1440)]
        following = [(START + DAY, [c(1440, o="110", h="114", l="109", close="113")])]
        r = simulate_rows(rows, START + DAY + MINUTE, following)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["exit_time"], "2026-01-02T00:00+08:00")

    def test_unclosed_trade_is_open(self):
        r = simulate_rows([c(0, o="108", h="111", l="105", close="110")])
        self.assertEqual(r["status"], "open")
        self.assertIsNone(r["exit_price"])

    def test_following_api_error_preserves_known_entry(self):
        rows = [c(0, o="108", h="111", l="105", close="110")]
        rows += [c(i, o="110", h="111", l="109", close="110") for i in range(1, 1440)]
        def failed():
            raise RuntimeError("network unavailable")
            yield
        r = simulate_rows(rows, START + DAY + MINUTE, failed())
        self.assertEqual(r["status"], "data_error")
        self.assertEqual(D(r["entry_price"]), D("110"))
        self.assertIn("network unavailable", r["note"])

    def test_bad_ohlc_rejected(self):
        for row in ([0,"100","90","80","100"], [0,"NaN","110","90","100"], [0,"0","110","0","100"]):
            with self.assertRaises(ValueError):
                Candle.parse(row)


if __name__ == "__main__":
    unittest.main()
