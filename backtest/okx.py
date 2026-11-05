"""Public OKX API with verified TLS, pacing, bounded retries and disk caching."""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import sqlite3
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

from .engine import Candle


class Cancelled(Exception):
    pass


class OKX:
    def __init__(self, cache, stop=None):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.stop = stop or threading.Event()
        self.last = 0
        self.requests = 0
        self.cache_hits = 0
        self.on_activity = None
        self.memory = OrderedDict()
        self.database = self.cache / "history.sqlite3"

    def activity(self, action):
        if self.on_activity:
            self.on_activity(network_requests=self.requests, cache_hits=self.cache_hits, activity=action)

    def check(self):
        if self.stop.is_set():
            raise Cancelled("用户取消")

    def get(self, path, params):
        url = "https://www.okx.com" + path + "?" + urllib.parse.urlencode(params)
        for attempt in range(5):
            self.check()
            delay = max(0, .15 - (time.monotonic() - self.last))
            if self.stop.wait(delay):
                self.check()
            try:
                self.last = time.monotonic()
                self.requests += 1
                self.activity(f"请求 OKX · {params.get('instId', '合约列表')} · {params.get('bar', '')}")
                req = urllib.request.Request(url, headers={"User-Agent": "OKXBacktest/1.0"})
                with urllib.request.urlopen(req, timeout=25) as response:
                    body = json.load(response)
                if body.get("code") == "50011":
                    raise urllib.error.HTTPError(url, 429, "rate limited", {}, None)
                if body.get("code") != "0":
                    raise RuntimeError(f"OKX 返回错误 {body.get('code')}: {body.get('msg')}")
                if not isinstance(body.get("data"), list):
                    raise RuntimeError("OKX 数据格式错误")
                return body["data"]
            except (urllib.error.URLError, TimeoutError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"OKX HTTP {exc.code}；检查网络许可及地区限制") from exc
                if attempt == 4:
                    raise RuntimeError(f"OKX 连接失败：{type(exc).__name__}；检查网络许可及地区限制") from exc
                if self.stop.wait(2 ** attempt):
                    self.check()

    def instruments(self):
        rows = self.get("/api/v5/public/instruments", {"instType": "SWAP"})
        return [r for r in rows if r.get("settleCcy") == "USDT" and r.get("ctType") == "linear"
                and r.get("instId", "").endswith("-USDT-SWAP")]

    def candles(self, inst, bar, start, end):
        self.check()
        if bar not in ("1m", "1D"):
            raise ValueError("不支持的K线周期")
        interval = 60_000 if bar == "1m" else 86_400_000
        cache_key = (inst, bar, start, end)
        if cache_key in self.memory:
            self.cache_hits += 1
            self.memory.move_to_end(cache_key)
            self.activity(f"内存缓存 · {inst} · {bar}")
            return self.memory[cache_key]
        key = hashlib.sha256(f"v1:{inst}:{bar}:{start}:{end}".encode()).hexdigest()
        file = self.cache / (key + ".json")
        if file.exists():
            rows = json.loads(file.read_text())
            parsed = [Candle.parse(r) for r in rows]
            if len(parsed) != (end - start) // interval or any(c.ts != start + i * interval for i, c in enumerate(parsed)):
                raise ValueError("旧行情缓存不完整，无法当作完整数据使用")
            self.save_rows(inst, bar, rows)
            self.cache_hits += 1
            self.activity(f"复用旧行情缓存 · {inst} · {bar}")
            return self.remember(cache_key, parsed)
        rows = {}
        if self.database.exists():
            with closing(sqlite3.connect(self.database)) as db:
                for ts, raw in db.execute("SELECT ts, data FROM candles WHERE inst=? AND bar=? AND ts>=? AND ts<? ORDER BY ts", (inst, bar, start, end)):
                    value = json.loads(raw)
                    Candle.parse(value)
                    if (ts - start) % interval == 0:
                        rows[ts] = value
        if rows:
            self.cache_hits += 1
            self.activity(f"复用 {len(rows)} 根已下载K线 · {inst} · {bar}")
        # Find missing spans; never treat partial history as complete.
        gaps = []
        expected = start
        for ts in sorted(rows):
            if ts > expected:
                gaps.append((expected, ts))
            expected = ts + interval
        if expected < end:
            gaps.append((expected, end))
        for gap_start, gap_end in gaps:
            cursor = gap_end
            while cursor > gap_start:
                self.check()
                batch = self.get("/api/v5/market/history-candles", {
                    "instId": inst, "bar": bar, "after": str(cursor), "limit": "300",
                })
                if not batch:
                    break
                oldest = min(int(r[0]) for r in batch)
                if oldest >= cursor:
                    raise RuntimeError("OKX 分页游标未前进")
                confirmed = []
                for row in batch:
                    ts = int(row[0])
                    if gap_start <= ts < gap_end and len(row) >= 9 and row[8] == "1":
                        Candle.parse(row)
                        if (ts - start) % interval:
                            raise RuntimeError("OKX K线时间戳未按周期对齐")
                        rows[ts] = row
                        confirmed.append(row)
                # Save each page before fetching another, so cancellation can resume.
                self.save_rows(inst, bar, confirmed)
                cursor = oldest
        result = [Candle.parse(rows[t]) for t in sorted(rows)]
        if len(result) == (end - start) // interval and all(c.ts == start + i * interval for i, c in enumerate(result)):
            return self.remember(cache_key, result)
        return result

    def save_rows(self, inst, bar, rows):
        if not rows:
            return
        with closing(sqlite3.connect(self.database)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS candles (inst TEXT, bar TEXT, ts INTEGER, data TEXT, PRIMARY KEY(inst,bar,ts))")
                db.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?)", (
                    (inst, bar, int(row[0]), json.dumps(row)) for row in rows
                ))

    def remember(self, key, rows):
        self.memory[key] = rows
        self.memory.move_to_end(key)
        while len(self.memory) > 24:
            self.memory.popitem(last=False)
        return rows
