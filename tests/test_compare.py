import unittest
from unittest.mock import Mock, patch

from backtest.compare import rank_rows, run_comparison, summarize
from backtest.engine import DAY, date_ms
from backtest.okx import Cancelled

START = date_ms("2026-01-01")


def result(entry="10", wins=2, losses=1, *, complete=True, ambiguous=0, opened=0):
    return {"config":{"entry_pct":entry, "observation_cutoff":"2026-01-02T00:00+08:00"},
            "summary":{"success":wins, "failure":losses, "ambiguous":ambiguous, "open":opened},
            "coverage":{"eligible_days":wins+losses+ambiguous+opened}, "complete":complete,
            "trades":[{"pnl_usdt":"200"} for _ in range(wins)] + [{"pnl_usdt":"-600"} for _ in range(losses)]}


class CompareTests(unittest.TestCase):
    def test_win_rate_excludes_unknowns_and_no_trades_is_undefined(self):
        row = summarize(result(wins=1, losses=1, ambiguous=10, opened=20))
        self.assertEqual(row["resolved"], 2)
        self.assertEqual(row["total"], 32)
        self.assertEqual(row["win_rate_pct"], "50")
        self.assertEqual(row["realized_pnl_usdt"], "-400")
        self.assertIsNone(summarize(result(wins=0, losses=0))["win_rate_pct"])

    def test_exact_ties_and_incomplete_rows_not_ranked(self):
        ranked = rank_rows([summarize(result("10", wins=1, losses=1)),
                            summarize(result("11", wins=2, losses=2)),
                            summarize(result("12", wins=0, losses=1)),
                            summarize(result("13", wins=5, losses=0, complete=False)),
                            summarize(result("14", wins=0, losses=0))])
        ranks = {r["entry_pct"]:r["rank"] for r in ranked}
        self.assertEqual(ranks, {"10":1,"11":1,"12":3,"13":None,"14":None})

    def test_eleven_groups_share_universe_cutoff_and_fixed_exits(self):
        client = Mock(requests=1, cache_hits=0)
        client.instruments.return_value = [{"instId":"TEST-USDT-SWAP"}]
        def fake_run(shared, config, progress, **kwargs):
            self.assertEqual(shared.instruments(), client.instruments.return_value)
            return result(config["entry_pct"], wins=int(config["entry_pct"])-9, losses=1)
        with patch("backtest.compare.time.time", return_value=(START+DAY)/1000), patch("backtest.compare.run", side_effect=fake_run) as runner:
            actual = run_comparison(client, {"take_profit_pct":"99", "stop_loss_pct":"99"})
        client.instruments.assert_called_once()
        self.assertEqual(runner.call_count, 11)
        self.assertEqual([c.args[1]["entry_pct"] for c in runner.call_args_list], list(map(str,range(10,21))))
        self.assertTrue(all(c.args[1]["take_profit_pct"]=="2" and c.args[1]["stop_loss_pct"]=="6" for c in runner.call_args_list))
        self.assertEqual({c.kwargs["observation_cutoff"] for c in runner.call_args_list}, {START+DAY})
        self.assertEqual(actual["best_entry_pcts"], ["20"])

    def test_incomplete_group_prevents_overall_winner(self):
        client = Mock(requests=1, cache_hits=0)
        with patch("backtest.compare.run", side_effect=lambda shared,config,*a,**kw: result(config["entry_pct"], complete=config["entry_pct"]!="15")):
            actual = run_comparison(client, {})
        self.assertFalse(actual["complete"])
        self.assertEqual(actual["best_entry_pcts"], [])

    def test_cancel_does_not_return_completed_comparison(self):
        client = Mock()
        client.check.side_effect = Cancelled()
        with self.assertRaises(Cancelled):
            run_comparison(client, {})


if __name__ == "__main__":
    unittest.main()
