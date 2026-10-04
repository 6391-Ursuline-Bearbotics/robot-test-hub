"""Loopback-only foreground demo host. No robot is contacted."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import sqlite3
import sys
import threading
from urllib.parse import urlsplit

from .config import Config, ConfigError
from .demo import DemoSource
from .service import HubService
from .storage import OwnershipError, SchemaError


def create_http_server(service: HubService, source: DemoSource, port: int) -> ThreadingHTTPServer:
    page = (Path(__file__).parent / "static/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

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
            bound_port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{bound_port}", f"localhost:{bound_port}")

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, b'{}')
            if self.path == "/":
                return self.send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/status":
                result = service.snapshot()
                result["demo"] = source.snapshot()
                return self.send(200, json.dumps(result, allow_nan=False).encode())
            if self.path == "/api/diagnostics":
                result = service.diagnostics.snapshot()
                result["workers"] = service.snapshot()["workers"]
                return self.send(200, json.dumps(result, allow_nan=False).encode())
            self.send(404, b'{}')

        def do_POST(self):
            origin = self.headers.get("Origin")
            if (not self.allowed_host() or self.headers.get("X-Hub-Request") != "1"
                    or (origin and (urlsplit(origin).netloc != self.headers.get("Host") or urlsplit(origin).scheme != "http"))
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
                    service.set_paused(payload.get("value"))
                elif action == "retry":
                    service.retry_errors()
                    return self.send(202, b'{"ok":true,"state":"pending"}')
                else:
                    source.configure(action, payload.get("value"))
                self.send(200, b'{"ok":true}')
            except (ValueError, KeyError, TypeError):
                self.send(400, b'{"error_code":"invalid_control","error":"Check action, value, and JSON request size"}')
            except sqlite3.Error:
                service.diagnostics.record("settings_write_failed", "Cannot save operator preference; check catalog/local storage")
                self.send(503, b'{"error_code":"settings_write_failed","error":"Preference was not saved; check local storage"}')

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Versioned JSON configuration; CLI options override its values")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")
    parser.add_argument("--idle-delay", type=float)
    args = parser.parse_args()
    try:
        config = Config.load(args.config, port=args.port, data_dir=args.data_dir, idle_delay=args.idle_delay)
    except ConfigError as exc:
        parser.error(str(exc))
    service = None
    httpd = None
    shutdown = threading.Event()
    old_handlers = {}
    try:
        source = DemoSource()
        service = HubService(config, source)
        httpd = create_http_server(service, source, config.port)
        httpd.timeout = 0.2
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                number = getattr(signal, name)
                old_handlers[number] = signal.signal(number, lambda *_: shutdown.set())
        service.start()
        print(f"DEMO ONLY — synthetic bytes, no robot connection: http://127.0.0.1:{config.port}", flush=True)
        print(f"Checkpoints: {service.root}", flush=True)
        while not shutdown.is_set():
            httpd.handle_request()
        return 0
    except (OwnershipError, SchemaError) as exc:
        print(f"Hub startup failed: {exc}", file=sys.stderr, flush=True)
        return 2
    except OSError:
        print("Hub startup failed: cannot bind loopback port or open local storage; check --port and --data-dir", file=sys.stderr, flush=True)
        return 2
    finally:
        if service is not None and not service.close():
            print("Shutdown pending: outstanding adapter I/O; ownership retained. See diagnostics.", file=sys.stderr, flush=True)
        if httpd is not None:
            httpd.server_close()
        for number, handler in old_handlers.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
