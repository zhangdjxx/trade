"""Public OKX API with verified TLS, pacing, bounded retries and disk caching."""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
        key = hashlib.sha256(f"v1:{inst}:{bar}:{start}:{end}".encode()).hexdigest()
        file = self.cache / (key + ".json")
        if file.exists():
            rows = json.loads(file.read_text())
            return [Candle.parse(r) for r in rows]
        rows = {}
        cursor = end
        while cursor > start:
            self.check()
            batch = self.get("/api/v5/market/history-candles", {
                "instId": inst, "bar": bar, "after": str(cursor), "limit": "300",
            })
            if not batch:
                break
            oldest = min(int(r[0]) for r in batch)
            if oldest >= cursor:
                raise RuntimeError("OKX 分页游标未前进")
            for row in batch:
                ts = int(row[0])
                if start <= ts < end and len(row) >= 9 and row[8] == "1":
                    Candle.parse(row)
                    rows[ts] = row
            cursor = oldest
        result = [rows[t] for t in sorted(rows)]
        # Do not persist missing/partial history as a valid cache hit.
        interval = 60_000 if bar == "1m" else 86_400_000
        if len(result) == (end - start) // interval and all(
            int(r[0]) == start + i * interval for i, r in enumerate(result)
        ):
            temp = file.with_suffix(".tmp")
            temp.write_text(json.dumps(result))
            temp.replace(file)
        return [Candle.parse(r) for r in result]
