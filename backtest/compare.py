"""Compare 11 entry thresholds using a frozen universe, cutoff and shared candle cache."""
import time
from decimal import Decimal
from fractions import Fraction

from .engine import MINUTE
from .runner import run

THRESHOLDS = tuple(str(i) for i in range(10, 21))


def summarize(result):
    counts = result["summary"]
    wins, losses = counts.get("success", 0), counts.get("failure", 0)
    resolved = wins + losses
    return {
        "entry_pct": result["config"]["entry_pct"],
        "take_profit_pct": "2", "stop_loss_pct": "6",
        "success": wins, "failure": losses, "resolved": resolved,
        "total": result["coverage"]["eligible_days"],
        "ambiguous": counts.get("ambiguous", 0), "open": counts.get("open", 0),
        "data_error": counts.get("data_error", 0),
        "complete": result["complete"],
        "win_rate_pct": str(Decimal(wins) * 100 / resolved) if resolved else None,
        "realized_pnl_usdt": str(sum((Decimal(t.get("pnl_usdt") or "0") for t in result["trades"]), Decimal(0))),
    }


def rank_rows(rows):
    # Compare exact fractions, including ties, before rounding for display.
    ordered = sorted(rows, key=lambda r: (
        not r["complete"], not bool(r["resolved"]),
        -Fraction(r["success"], r["resolved"]) if r["resolved"] else Fraction(0),
        Decimal(r["entry_pct"])))
    previous, rank, position = None, None, 0
    for row in ordered:
        row["rank"] = None
        if row["complete"] and row["resolved"]:
            position += 1
            ratio = Fraction(row["success"], row["resolved"])
            if ratio != previous:
                rank = position
            row["rank"] = rank
            previous = ratio
    return ordered


class SharedUniverse:
    def __init__(self, client, universe):
        self.client = client
        self.universe = universe

    def instruments(self):
        return self.universe

    def __getattr__(self, key):
        return getattr(self.client, key)


def run_comparison(client, config, progress=lambda message, **values: None):
    cutoff = int(time.time() * 1000) // MINUTE * MINUTE
    progress("参数对比：读取一次合约列表，后续11组共享行情缓存", completed=0, total=11)
    frozen = SharedUniverse(client, client.instruments())
    runs, rows = [], []
    for i, entry_pct in enumerate(THRESHOLDS):
        client.check()
        def group_progress(message, **values):
            symbols_done, symbols_total = values.get("completed", 0), values.get("total", 1)
            progress(f"第{i+1}/11组 · 买入涨幅{entry_pct}% · {message}",
                     completed=i + symbols_done / max(1, symbols_total), total=11)
        result = run(frozen, {**config, "entry_pct":entry_pct, "take_profit_pct":"2", "stop_loss_pct":"6"},
                     group_progress, observation_cutoff=cutoff)
        runs.append(result)
        rows.append(summarize(result))
        progress(f"已完成{i+1}/11组 · 买入涨幅{entry_pct}%", completed=i+1, total=11,
                 comparison_rows=[dict(r) for r in rows])
    ranked = rank_rows(rows)
    complete = all(r["complete"] for r in ranked)
    best = [r["entry_pct"] for r in ranked if r["rank"] == 1] if complete else []
    return {
        "mode":"compare", "config": {**runs[0]["config"], "entry_start_pct":"10", "entry_end_pct":"20", "entry_step_pct":"1"},
        "rows": ranked, "runs": runs, "best_entry_pcts": best, "complete":complete,
        "requests":client.requests, "cache_hits":getattr(client, "cache_hits", 0),
        "win_rate_definition":"成功/(成功+失败)；不含顺序不明、未平仓和数据异常。无已判定交易时不计算胜率。",
        "warnings":["所有组使用相同币对列表、日期和持仓观察截止分钟；共用行情缓存，缺少的区间仍需补下载。",
                    "比较的是已平仓且可判定交易的历史胜率；同时比较样本数和未决交易，不能视为未来收益保证。",
                    "任一组存在数据缺失时不宣布整体胜率最高组；不完整组仅展示已知样本胜率。"],
    }
