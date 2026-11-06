import time
from collections import Counter

from .engine import DAY, MINUTE, HoldingDay, Strategy, date_ms, simulate, stamp


def run(client, config, progress=lambda message, **values: None, *, observation_cutoff=None):
    strategy = Strategy.from_config(config)
    start = date_ms(config.get("start", "2026-01-01"))
    end = date_ms(config.get("end", "2026-11-03")) + DAY
    cutoff = observation_cutoff if observation_cutoff is not None else int(time.time() * 1000) // MINUTE * MINUTE
    if end <= start or end > cutoff:
        raise ValueError("日期无效：结束日期必须是不晚于昨天（UTC+8）的完整交易日")
    if (end - start) // DAY > 1096:
        raise ValueError("单次回测最多三年")
    progress("读取 OKX 当前 USDT 永续合约列表")
    universe = client.instruments()
    selected = config.get("symbols", "").strip()
    if selected:
        wanted = {x.strip().upper() for x in selected.split(",") if x.strip()}
        known = {r["instId"] for r in universe}
        if wanted - known:
            raise ValueError("不在当前 OKX USDT 永续列表：" + ", ".join(sorted(wanted - known)))
        universe = [r for r in universe if r["instId"] in wanted]
    if not universe:
        raise RuntimeError("未读取到任何 USDT 永续合约，不能生成有效回测")
    result = {
        "config": {"start": stamp(start), "end_exclusive": stamp(end), "observation_cutoff": stamp(cutoff), **strategy.as_dict()},
        "trades": [], "errors": [], "universe": [r["instId"] for r in universe],
        "coverage": {"expected_days": 0, "checked_days": 0, "eligible_days": 0, "excluded_listing_days": 0},
        "warnings": [
            "仅覆盖本次 OKX 当前合约列表，可能遗漏已下架合约，存在幸存者偏差。",
            "以成交价分钟K线模拟，时间精度为一分钟，非逐笔成交；触发价/成交量/实际成交不保证。",
            f"UTC+8 日开盘价为基准，日内首次严格超过{strategy.entry_pct}%触发；按边界估算买入价，跳空按开盘价。",
            f"相对买入价上涨{strategy.take_profit_pct}%止盈，下跌{strategy.stop_loss_pct}%止损。",
            "每币对每天最多一笔；跨天未退出时，次日仍可独立新开一笔，可存在重叠持仓。",
            "每笔10000 USDT名义本金，允许碎数数量；未计手续费、滑点、资金费、保证金和资金上限。",
            "止盈止损同分钟或开仓分钟低点先后不明时标为顺序不明，不强行判胜负。",
            "开仓区间截至结束日期24点，持仓继续追踪到本次运行时最后完整分钟；未退出标为持仓中。",
        ],
    }
    for i, instrument in enumerate(universe):
        client.check()
        inst = instrument["instId"]
        progress(f"{i + 1}/{len(universe)} · {inst}：筛选日线", completed=i, total=len(universe))
        listing = int(instrument.get("listTime") or 0)
        first = max(start, ((listing + DAY - 1 + 8 * 3_600_000) // DAY) * DAY - 8 * 3_600_000)
        # Explicitly exclude a listing day lacking a midnight baseline.
        result["coverage"]["excluded_listing_days"] += max(0, min(end, first) - start) // DAY
        expected = list(range(first, end, DAY))
        result["coverage"]["expected_days"] += len(expected)
        if not expected:
            continue
        try:
            daily = client.candles(inst, "1D", first, end)  # OKX 1D is UTC+8; 1Dutc is UTC.
        except Exception as exc:
            from .okx import Cancelled
            if isinstance(exc, Cancelled):
                raise
            result["errors"].append({"symbol": inst, "day": None, "error": str(exc)})
            continue
        by_day = {c.ts: c for c in daily}
        tail_loaded = False
        last_complete_day = ((cutoff + 8 * 3_600_000) // DAY) * DAY - 8 * 3_600_000
        def holding_daily(nxt):
            nonlocal tail_loaded
            if nxt >= end and not tail_loaded:
                tail_loaded = True
                if end < last_complete_day:
                    try:
                        by_day.update({c.ts: c for c in client.candles(inst, "1D", end, last_complete_day)})
                    except Exception:
                        client.check()  # Missing optimization data falls back to minute history.
            return by_day.get(nxt)
        for day in expected:
            client.check()
            candle = by_day.get(day)
            if candle is None:
                result["errors"].append({"symbol": inst, "day": stamp(day)[:10], "error": "缺少完整UTC+8日线，不能认定当天无信号"})
                continue
            result["coverage"]["checked_days"] += 1
            if candle.high <= candle.open * strategy.entry_factor:
                continue
            result["coverage"]["eligible_days"] += 1
            progress(f"{i + 1}/{len(universe)} · {inst} · {stamp(day)[:10]}：计算交易（优先复用本地缓存）", completed=i, total=len(universe))
            try:
                entry_candles = client.candles(inst, "1m", day, day + DAY)
                if entry_candles and entry_candles[0].ts == day and entry_candles[0].open != candle.open:
                    raise ValueError("日线和分钟线零点开盘价不一致，拒绝使用错误基准")
                def following():
                    for nxt in range(day + DAY, cutoff, DAY):
                        yield nxt, HoldingDay(holding_daily(nxt),
                            lambda nxt=nxt: client.candles(inst, "1m", nxt, min(nxt + DAY, cutoff)))
                trade = simulate(inst, day, candle.open, entry_candles, following(), cutoff, client.check, strategy=strategy)
                result["trades"].append(trade)
            except Exception as exc:
                from .okx import Cancelled
                if isinstance(exc, Cancelled):
                    raise
                result["trades"].append({"day": stamp(day)[:10], "symbol": inst, "status": "data_error", "note": str(exc), **strategy.as_dict()})
        progress(f"已完成 {inst}", completed=i + 1, total=len(universe), trades=len(result["trades"]))
    result["trades"].sort(key=lambda t: (t["day"], t["symbol"]))
    result["summary"] = dict(Counter(t["status"] for t in result["trades"]))
    result["complete"] = not result["errors"] and not result["summary"].get("data_error")
    result["requests"] = client.requests
    result["cache_hits"] = getattr(client, "cache_hits", 0)
    result["holding_days_skipped"] = sum(t.get("holding_days_skipped", 0) for t in result["trades"])
    return result
