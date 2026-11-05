"""Local-only HTTP service. No API keys or third-party Python dependencies."""
import csv
import io
import json
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .okx import OKX, Cancelled
from .runner import run
from .engine import Strategy

ROOT = Path(__file__).resolve().parent
FIELDS = ["day", "symbol", "entry_pct", "take_profit_pct", "stop_loss_pct", "day_open", "threshold", "notional_usdt", "entry_time", "entry_price", "quantity",
          "take_profit", "stop_loss", "exit_time", "exit_price", "status", "pnl_usdt", "ambiguity_time", "note"]


class Jobs:
    def __init__(self, cache):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.current = None

    def start(self, config):
        with self.lock:
            if self.current and self.current["state"] == "running":
                raise ValueError("已有回测运行中，请先取消或等待完成")
            stop = threading.Event()
            job = {"id": uuid.uuid4().hex, "state": "running", "message": "启动中", "result": None, "stop": stop}
            self.current = job

        def work():
            def progress(message, **values):
                with self.lock:
                    job.update(message=message, **values)
            try:
                client = OKX(self.cache / "candles", stop)
                def activity(**values):
                    with self.lock:
                        job.update(**values)
                client.on_activity = activity
                result = run(client, config, progress)
                target = self.cache / (job["id"] + ".json")
                target.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                with self.lock:
                    job.update(state="done", message="回测完成" if result["complete"] else "回测结束，存在数据缺失", result=result)
            except Cancelled:
                with self.lock:
                    job.update(state="cancelled", message="已取消；已下载的完整K线缓存保留，可重新运行")
            except Exception as exc:
                with self.lock:
                    job.update(state="error", message=str(exc))

        threading.Thread(target=work, daemon=True).start()
        return job["id"]

    def snapshot(self):
        with self.lock:
            return {k: v for k, v in self.current.items() if k != "stop"} if self.current else {"state": "idle"}


def make_handler(jobs):
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, content_type="application/json; charset=utf-8", attachment=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            if parsed.path == "/api/health":
                return self.send(200, {"ok": True, "service": "okx-backtest", "network_checked": False})
            if parsed.path == "/api/job":
                return self.send(200, jobs.snapshot())
            if parsed.path == "/api/export":
                state = jobs.snapshot()
                result = state.get("result")
                if not result:
                    return self.send(404, {"error": "没有已完成的回测结果"})
                if parse_qs(parsed.query).get("format") == ["json"]:
                    return self.send(200, result, attachment="okx-backtest.json")
                buf = io.StringIO()
                buf.write("\ufeff")
                writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
                writer.writeheader()
                for trade in result["trades"]:
                    # Neutralize formula injection if external data becomes spreadsheet content.
                    safe = {k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "@", "\t", "\r")) else v)
                            for k, v in trade.items()}
                    writer.writerow(safe)
                return self.send(200, buf.getvalue(), "text/csv; charset=utf-8", "okx-trades.csv")
            return self.send(404, {"error": "Not found"})

        def do_POST(self):
            # Local UI only. Reject cross-origin requests / DNS rebinding.
            host = self.headers.get("Host", "")
            if host.split(":")[0] not in ("localhost", "127.0.0.1"):
                return self.send(403, {"error": "仅支持本地访问"})
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://{host}", f"https://{host}"):
                return self.send(403, {"error": "跨域请求被拒绝"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 <= length <= 16384:
                    raise ValueError("请求过大")
                config = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(config, dict):
                    raise ValueError("请求格式错误")
                if self.path == "/api/run":
                    if any(not isinstance(v, str) for v in config.values()):
                        raise ValueError("参数必须为字符串")
                    Strategy.from_config(config)
                    return self.send(HTTPStatus.ACCEPTED, {"id": jobs.start(config)})
                if self.path == "/api/cancel":
                    with jobs.lock:
                        if jobs.current:
                            jobs.current["stop"].set()
                    return self.send(200, {"ok": True})
                return self.send(404, {"error": "Not found"})
            except (ValueError, TypeError) as exc:
                return self.send(400, {"error": str(exc)})

    return Handler


def serve(port=8000, cache=None):
    cache = cache or ROOT.parent / ".backtest-cache"
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Jobs(cache)))
    print(f"OKX backtest server: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
