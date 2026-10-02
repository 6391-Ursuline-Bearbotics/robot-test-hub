"""Loopback-only demo host. Not a production service or a robot dashboard."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .collector import Collector
from .demo import DemoSource


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=6391)
    parser.add_argument("--data-dir", type=Path, default=Path("data/demo"))
    parser.add_argument("--idle-delay", type=float, default=3)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    source = DemoSource()
    collector = Collector(args.data_dir, source, idle_delay=args.idle_delay)
    lock = threading.Lock()
    stop = threading.Event()
    page = (Path(__file__).parent / "static/index.html").read_bytes()

    def worker():
        while not stop.is_set():
            with lock:
                collector.tick()
            stop.wait(0.01)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status: int, body: bytes, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{args.port}", f"localhost:{args.port}")

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, b'{}')
            if self.path == "/":
                return self.send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/status":
                with lock:
                    result = collector.snapshot()
                result["demo"] = source.snapshot()
                return self.send(200, json.dumps(result, allow_nan=False).encode())
            self.send(404, b'{}')

        def do_POST(self):
            # JSON + custom header + strict Host/Origin, with no CORS allowance.
            origin = self.headers.get("Origin")
            if (not self.allowed_host() or self.headers.get("X-Hub-Request") != "1"
                    or (origin and urlsplit(origin).netloc != self.headers.get("Host"))
                    or self.headers.get("Content-Type") != "application/json"):
                return self.send(403, b'{}')
            if self.path != "/api/control":
                return self.send(404, b'{}')
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                action = payload["action"]
                if action == "paused":
                    if type(payload.get("value")) is not bool:
                        raise ValueError("Expected a boolean")
                    with lock:
                        collector.set_paused(payload["value"])
                elif action == "retry":
                    with lock:
                        collector.retry_errors()
                else:
                    # Separate source lock allows Enable to interrupt a pending read.
                    source.configure(action, payload.get("value"))
                self.send(200, b'{"ok":true}')
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400, json.dumps({"error": str(exc)}).encode())

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"DEMO ONLY — synthetic bytes, no robot connection: http://127.0.0.1:{args.port}", flush=True)
    print(f"Checkpoints: {args.data_dir.resolve()}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join()
        httpd.server_close()
        collector.close()


if __name__ == "__main__":
    main()
