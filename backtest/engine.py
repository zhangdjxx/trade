"""Conservative OHLC simulation. All calculations use Decimal, timestamps are UTC ms."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

TZ = timezone(timedelta(hours=8))
MINUTE = 60_000
DAY = 86_400_000


@dataclass(frozen=True)
class Strategy:
    entry_pct: Decimal = Decimal("10")
    take_profit_pct: Decimal = Decimal("3")
    stop_loss_pct: Decimal = Decimal("6")

    def __post_init__(self):
        for name, label, upper in (("entry_pct", "买入涨幅", None),
                                   ("take_profit_pct", "止盈比例", None),
                                   ("stop_loss_pct", "止损比例", Decimal("100"))):
            try:
                value = Decimal(str(getattr(self, name)))
            except (InvalidOperation, ValueError):
                raise ValueError(f"{label}必须是有效数字") from None
            if not value.is_finite() or value <= 0 or (upper is not None and value >= upper):
                raise ValueError(f"{label}必须大于0" + ("且小于100%" if upper else ""))
            if value > Decimal("1000000") or value.as_tuple().exponent < -6:
                raise ValueError(f"{label}最多支持6位小数，且不能超过1000000%")
            object.__setattr__(self, name, value)

    @classmethod
    def from_config(cls, config):
        return cls(**{key: config.get(key, str(default)) for key, default in
                     (("entry_pct", 10), ("take_profit_pct", 3), ("stop_loss_pct", 6))})

    def as_dict(self):
        return {key: str(getattr(self, key)) for key in ("entry_pct", "take_profit_pct", "stop_loss_pct")}

    @property
    def entry_factor(self):
        return 1 + self.entry_pct / 100

    @property
    def profit_factor(self):
        return 1 + self.take_profit_pct / 100

    @property
    def stop_factor(self):
        return 1 - self.stop_loss_pct / 100


def date_ms(value):
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ).timestamp() * 1000)


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).isoformat(timespec="minutes")


@dataclass(frozen=True)
class Candle:
    ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @classmethod
    def parse(cls, row):
        ts = int(row[0])
        o, h, l, c = map(Decimal, row[1:5])
        if not all(x.is_finite() and x > 0 for x in (o, h, l, c)):
            raise ValueError("Invalid non-positive/non-finite OHLC")
        if not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError("Invalid OHLC ordering")
        return cls(ts, o, h, l, c)


@dataclass(frozen=True)
class HoldingDay:
    """A full daily candle can prove that neither exit was touched."""
    daily: Optional[Candle]
    load_minutes: Callable


def simulate(inst, day, baseline, entry_candles, following_days, cutoff, check=lambda: None, *, strategy=None):
    """following_days lazily yields (day_start, candles). Never invent intrabar order."""
    strategy = strategy or Strategy()
    threshold = baseline * strategy.entry_factor
    trade = {
        "day": stamp(day)[:10], "symbol": inst, "day_open": str(baseline),
        "threshold": str(threshold), "entry_time": None, "entry_price": None,
        "exit_time": None, "exit_price": None, "status": "data_error",
        "pnl_usdt": None, "note": "", "notional_usdt": "10000", "holding_days_skipped": 0, **strategy.as_dict(),
    }
    entry = None

    def scan(start, candles, end):
        nonlocal entry
        expected = start
        for c in candles:
            check()
            if c.ts >= end:
                break
            if c.ts != expected:
                trade["note"] = f"分钟数据缺口：{stamp(expected)}；不能确定首次触发或退出"
                return True
            expected += MINUTE
            entering = entry is None
            if entering:
                if c.high <= threshold:
                    continue
                entry = max(c.open, threshold)
                trade.update(entry_time=stamp(c.ts), entry_price=str(entry),
                             quantity=str(Decimal("10000") / entry),
                             take_profit=str(entry * strategy.profit_factor),
                             stop_loss=str(entry * strategy.stop_factor))
            tp, sl = entry * strategy.profit_factor, entry * strategy.stop_factor
            # In later candles the opening tick occurs before either extreme.
            if not entering and (c.open >= tp or c.open <= sl):
                win = c.open >= tp
                return finish(c, c.open, "success" if win else "failure", "跳空按开盘价退出")
            hit_tp, hit_sl = c.high >= tp, c.low <= sl
            # With a below-threshold open, the low can precede or follow the entry.
            uncertain_entry_stop = entering and c.open <= threshold and hit_sl
            if (hit_tp and hit_sl) or uncertain_entry_stop:
                trade.update(status="ambiguous", note="分钟内先后顺序不明；不能判定止盈/止损，也不能确定退出时间和价格",
                             ambiguity_time=stamp(c.ts))
                return True
            if hit_tp or hit_sl:
                return finish(c, tp if hit_tp else sl, "success" if hit_tp else "failure", "")
        if expected < end:
            trade["note"] = f"分钟数据缺口：{stamp(expected)}；不能确定首次触发或退出"
            return True
        return False

    def finish(c, price, status, note):
        trade.update(status=status, exit_time=stamp(c.ts), exit_price=str(price),
                     pnl_usdt=str(Decimal("10000") * (price / entry - 1)), note=note)
        return True

    if scan(day, entry_candles, min(day + DAY, cutoff)):
        return trade
    if entry is None:
        trade["note"] = "日线触发，但完整分钟线未触发；数据不一致"
        return trade
    try:
        for next_day, candles in following_days:
            check()
            if isinstance(candles, HoldingDay):
                daily = candles.daily
                if (daily is not None and daily.ts == next_day and next_day + DAY <= cutoff
                        and daily.high < entry * strategy.profit_factor
                        and daily.low > entry * strategy.stop_factor):
                    trade["holding_days_skipped"] += 1
                    continue
                candles = candles.load_minutes()
            if scan(next_day, candles, min(next_day + DAY, cutoff)):
                return trade
    except Exception as exc:
        check()  # Propagate cancellation rather than report it as missing data.
        trade["note"] = f"持仓跟踪数据不可用：{exc}"
        return trade
    trade.update(status="open", note=f"截至 {stamp(cutoff)} 未触发退出；不计为失败")
    return trade
