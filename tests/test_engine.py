import unittest
from decimal import Decimal as D

from backtest.engine import Candle, DAY, MINUTE, HoldingDay, Strategy, date_ms, simulate, stamp
from unittest.mock import Mock

START = date_ms("2026-01-01")


def c(n, o="100", h="101", l="99", close="100"):
    return Candle.parse([str(START + n * MINUTE), o, h, l, close])


def simulate_rows(rows, cutoff=None, following=()):
    return simulate("TEST-USDT-SWAP", START, D("100"), rows, following,
                    cutoff if cutoff is not None else START + len(rows) * MINUTE)


class EngineTests(unittest.TestCase):
    def holding_entry(self):
        return [c(0, o="108", h="111", l="105", close="110")] + [
            c(i, o="110", h="111", l="109", close="110") for i in range(1, 1440)]

    def test_quiet_day_matches_full_minutes_without_download(self):
        entry = self.holding_entry()
        quiet_minutes = [c(i, o="110", h="112", l="108", close="110") for i in range(1440, 2880)]
        exit_minutes = [c(2880, o="110", h="114", l="109", close="113")]
        plain = simulate_rows(entry, START + 2 * DAY + MINUTE,
                              [(START + DAY, quiet_minutes), (START + 2 * DAY, exit_minutes)])
        loader = Mock(side_effect=AssertionError("Quiet day should not download minutes"))
        fast = simulate_rows(entry, START + 2 * DAY + MINUTE,
                             [(START + DAY, HoldingDay(c(1440, o="110", h="112", l="108", close="110"), loader)),
                              (START + 2 * DAY, exit_minutes)])
        loader.assert_not_called()
        self.assertEqual(fast["holding_days_skipped"], 1)
        self.assertEqual({k:v for k,v in plain.items() if k != "holding_days_skipped"},
                         {k:v for k,v in fast.items() if k != "holding_days_skipped"})

    def test_tighter_custom_exit_requires_minutes(self):
        for strategy, daily, minute in (
            (Strategy("10", "1", "6"), c(1440, o="110", h="112", l="109", close="111"),
             c(1440, o="110", h="112", l="109", close="111")),
            (Strategy("10", "3", "1"), c(1440, o="110", h="111", l="108", close="109"),
             c(1440, o="110", h="111", l="108", close="109")),
            (Strategy("10", "3", "6"), c(1440, o="110", h="113.3", l="109", close="113"),
             c(1440, o="110", h="113.3", l="109", close="113")),
        ):
            # Entry day must not touch either tighter exit.
            entry = [c(0, o="109.5", h="110.5", l="109.4", close="110")]
            entry += [c(i, o="110", h="110.5", l="109.4", close="110") for i in range(1,1440)]
            loader = Mock(return_value=[minute])
            r = simulate("TEST", START, D("100"), entry, [(START + DAY, HoldingDay(daily, loader))],
                         START + 2 * DAY, strategy=strategy)
            loader.assert_called_once()
            self.assertIn(r["status"], ("success", "failure"))
            self.assertEqual(r["holding_days_skipped"], 0)

    def test_partial_day_cannot_use_daily_shortcut(self):
        loader = Mock(return_value=[])
        r = simulate_rows(self.holding_entry(), START + DAY + MINUTE,
                          [(START + DAY, HoldingDay(c(1440, o="110", h="111", l="109", close="110"), loader))])
        loader.assert_called_once()
        self.assertEqual(r["status"], "data_error")

    def test_custom_entry_and_profit_percent(self):
        rows = [c(0, h="104"), c(1, o="104", h="106", l="103", close="105"),
                c(2, o="105", h="108", l="104", close="107")]
        strategy = Strategy("5", "2.5", "4")
        r = simulate("TEST", START, D("100"), rows, [], START + 3 * MINUTE, strategy=strategy)
        self.assertEqual(D(r["entry_price"]), D("105"))
        self.assertEqual(D(r["take_profit"]), D("107.625"))
        self.assertEqual(D(r["stop_loss"]), D("100.8"))
        self.assertEqual(r["status"], "success")
        self.assertEqual(D(r["pnl_usdt"]), D("250"))
        self.assertEqual(r["entry_pct"], "5")

    def test_custom_stop_percent(self):
        rows = [c(0, o="109", h="111", l="109", close="110"),
                c(1, o="110", h="111", l="107", close="108")]
        r = simulate("TEST", START, D("100"), rows, [], START + 2 * MINUTE,
                     strategy=Strategy("10", "4", "2.5"))
        self.assertEqual(r["status"], "failure")
        self.assertEqual(D(r["exit_price"]), D("107.25"))
        self.assertEqual(D(r["pnl_usdt"]), D("-250"))

    def test_invalid_strategy_parameters(self):
        for key, value in (("entry_pct", "0"), ("take_profit_pct", "-1"),
                           ("stop_loss_pct", "100"), ("entry_pct", "NaN"),
                           ("entry_pct", "Infinity"), ("take_profit_pct", "abc"),
                           ("entry_pct", "0.0000001")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Strategy.from_config({key: value})

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
