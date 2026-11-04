"""HTTP integration using synthetic candles, never presented as historical results."""
import csv
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from backtest.engine import Candle, DAY, MINUTE, date_ms
from backtest.server import Jobs, make_handler

START = date_ms("2026-01-01")


class SyntheticOKX:
    def __init__(self, cache, stop):
        self.stop = stop
        self.requests = 0

    def check(self):
        pass

    def instruments(self):
        return [{"instId": "SYNTHETIC-USDT-SWAP", "listTime": "0"}]

    def candles(self, inst, bar, start, end):
        self.requests += 1
        if bar == "1D":
            return [Candle.parse([START, "100", "114", "99", "113"])]
        rows = [[START, "100", "101", "99", "100"],
                [START + MINUTE, "108", "111", "105", "110"],
                [START + 2 * MINUTE, "110", "114", "109", "113"]]
        rows += [[START + i * MINUTE, "113", "114", "112", "113"] for i in range(3, 1440)]
        return [Candle.parse(r) for r in rows]


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.jobs = Jobs(self.directory.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.jobs))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def get(self, path):
        with urlopen(self.base + path) as response:
            return response.read()

    def post(self, path, data, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        with urlopen(Request(self.base + path, data=json.dumps(data).encode(), headers=headers)) as response:
            return json.load(response)

    def test_page_health_run_and_exports(self):
        self.assertIn("USDT 永续".encode(), self.get("/"))
        self.assertEqual(json.loads(self.get("/api/health"))["service"], "okx-backtest")
        with patch("backtest.server.OKX", SyntheticOKX), patch("backtest.runner.time.time", return_value=(START + 3 * DAY) / 1000):
            started = self.post("/api/run", {"start": "2026-01-01", "end": "2026-01-01"})
            for _ in range(200):
                state = json.loads(self.get("/api/job"))
                if state["state"] != "running":
                    break
                threading.Event().wait(.01)
        self.assertEqual(state["state"], "done", state)
        result = state["result"]
        self.assertTrue(result["complete"])
        self.assertEqual(result["summary"], {"success": 1})
        self.assertEqual(result["trades"][0]["pnl_usdt"], "300.00")
        exported = list(csv.DictReader(io.StringIO(self.get("/api/export").decode("utf-8-sig"))))
        self.assertEqual(exported[0]["symbol"], "SYNTHETIC-USDT-SWAP")
        self.assertEqual(exported[0]["notional_usdt"], "10000")
        self.assertEqual(json.loads(self.get("/api/export?format=json")), result)
        self.assertTrue((self.jobs.cache / (started["id"] + ".json")).exists())

    def test_bad_input_and_cross_origin_rejected(self):
        with self.assertRaises(HTTPError) as bad:
            self.post("/api/run", {"start": ["bad"]})
        self.assertEqual(bad.exception.code, 400)
        with self.assertRaises(HTTPError) as origin:
            self.post("/api/run", {}, "https://unrelated.example")
        self.assertEqual(origin.exception.code, 403)

    def test_empty_exports_are_not_success(self):
        with self.assertRaises(HTTPError) as absent:
            self.get("/api/export")
        self.assertEqual(absent.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
