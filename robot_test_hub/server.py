"""Loopback-only foreground demo host. No robot is contacted."""
from __future__ import annotations

import argparse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import sqlite3
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from .config import Config, ConfigError
from .demo import DemoSource
from .notebook import MAX_BODY, Notebook, NotebookError
from .queue_api import status_view, transfer_page
from . import run_api
from .runs import install_schema as install_run_schema
from .service import HubService
from .storage import OwnershipError, SchemaError


def create_http_server(service: HubService, source: DemoSource, port: int) -> ThreadingHTTPServer:
    page = (Path(__file__).parent / "static/index.html").read_bytes()
    notebook_page = (Path(__file__).parent / "static/notebook.html").read_bytes()
    notebook = Notebook(service.root / "catalog.sqlite3")
    with closing(sqlite3.connect(service.root / 'catalog.sqlite3',timeout=2)) as db:
        db.execute('BEGIN IMMEDIATE')
        with db:
            install_run_schema(db)

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
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'self' 'unsafe-inline'; worker-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed_host(self):
            bound_port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{bound_port}", f"localhost:{bound_port}")

        def reject_unread(self, status, body):
            # Bounded drain gives ordinary rejected POSTs a reliable HTTP error
            # instead of a TCP reset on platforms that discard unread data.
            try:
                length=int(self.headers.get('Content-Length','0'))
                if 0 < length <= 65536:
                    self.rfile.read(length)
            except (ValueError,OSError):
                pass
            return self.send(status,body)

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, b'{}')
            if self.path == "/":
                return self.send(200, page, "text/html; charset=utf-8")
            if self.path == "/notebook":
                return self.send(200, notebook_page, "text/html; charset=utf-8")
            if self.path in ('/notebook.js','/notebook-sw.js'):
                return self.send(200,(Path(__file__).parent/'static'/self.path[1:]).read_bytes(),'application/javascript; charset=utf-8')
            if self.path == '/runs':
                return self.send(200,(Path(__file__).parent/'static/runs.html').read_bytes(),'text/html; charset=utf-8')
            if urlsplit(self.path).path.startswith(('/api/v1/runs','/api/v1/time/')):
                try:
                    url=urlsplit(self.path)
                    query=parse_qs(url.query,keep_blank_values=True,max_num_fields=8)
                    if any(len(v)!=1 for v in query.values()):
                        raise ValueError('Repeated query field')
                    result=run_api.get(service.root,url.path,{k:v[0] for k,v in query.items()},notebook)
                    return self.send(200,json.dumps(result,allow_nan=False).encode())
                except (ValueError,TypeError,OverflowError) as exc:
                    return self.send(400,json.dumps({'schema_version':1,'error_code':'invalid_time_query','error':str(exc)}).encode())
                except KeyError:
                    return self.send(404,b'{"error_code":"run_not_found"}')
                except sqlite3.Error:
                    return self.send(503,b'{"error_code":"catalog_unavailable"}')
            if urlsplit(self.path).path.startswith("/api/v1/annotations"):
                return self.notebook_get()
            if urlsplit(self.path).path == "/api/v1/transfers":
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=2)
                    if query.keys() - {'cursor', 'limit'} or any(len(v)!=1 for v in query.values()):
                        raise ValueError('Invalid queue query')
                    result=transfer_page(service.snapshot(),limit=int(query.get('limit',['50'])[0]),cursor=query.get('cursor',[None])[0])
                    return self.send(200,json.dumps(result,allow_nan=False).encode())
                except (ValueError,TypeError,OverflowError):
                    return self.send(400,b'{"schema_version":1,"error_code":"invalid_query"}')
            if self.path in ("/api/status", "/api/v1/status"):
                result = service.snapshot()
                result["source_type"] = 'synthetic_demo' if isinstance(source, DemoSource) else getattr(source,'source_type','unconfigured')
                result["demo"] = source.snapshot() if isinstance(source, DemoSource) else None
                if self.path == "/api/v1/status":
                    result = status_view(result)
                return self.send(200, json.dumps(result, allow_nan=False).encode())
            if self.path == "/api/diagnostics":
                result = service.diagnostics.snapshot()
                result["workers"] = service.snapshot()["workers"]
                snapshot = service.snapshot()
                result['transfer_limits'] = {key:snapshot.get(key) for key in ('outstanding_bytes','max_outstanding_bytes','outstanding_byte_limit','source_cancellation_guarantee')}
                return self.send(200, json.dumps(result, allow_nan=False).encode())
            self.send(404, b'{}')

        def notebook_get(self):
            try:
                url = urlsplit(self.path)
                query = parse_qs(url.query, keep_blank_values=True, max_num_fields=8)
                if any(len(values) != 1 for values in query.values()):
                    raise ValueError("Duplicate query field")
                values = {key: items[0] for key, items in query.items()}
                if url.path == "/api/v1/annotations":
                    if values.keys() - {"from", "to", "include_unknown", "limit", "cursor"}:
                        raise ValueError("Unknown query field")
                    unknown = values.get("include_unknown", "true")
                    if unknown not in ("true", "false"):
                        raise ValueError("Invalid include_unknown")
                    result = notebook.list(start_ns=values.get("from"), end_ns=values.get("to"),
                        include_unknown=unknown == "true", limit=int(values.get("limit", "50")), cursor=values.get("cursor"))
                else:
                    prefix = "/api/v1/annotations/"
                    if not url.path.startswith(prefix) or "/" in url.path[len(prefix):]:
                        return self.send(404, b'{}')
                    if values.keys() - {"limit", "after_revision"}:
                        raise ValueError("Unknown query field")
                    result = notebook.history(url.path[len(prefix):], limit=int(values.get("limit", "50")),
                        after_revision=int(values.get("after_revision", "0")))
                return self.send(200, json.dumps(result, allow_nan=False).encode())
            except NotebookError as exc:
                self.send(exc.status, json.dumps({"schema_version": 1, "error_code": exc.code, "error": str(exc)}).encode())
            except (ValueError, TypeError, OverflowError):
                self.send(400, b'{"schema_version":1,"error_code":"invalid_query","error":"Check notebook search parameters"}')
            except sqlite3.Error:
                service.diagnostics.record("notebook_read_failed", "Cannot read hub notebook; check local storage")
                self.send(503, b'{"schema_version":1,"error_code":"notebook_storage_failed","error":"Notebook storage unavailable"}')

        def notebook_post(self):
            try:
                if service.stop.is_set() or service.closed:
                    return self.reject_unread(503, b'{"schema_version":1,"error_code":"service_stopping","error":"Hub is stopping; retry after restart"}')
                length = int(self.headers.get("Content-Length", "0"))
                # Drain a modest rejected body before replying so Windows does
                # not replace the JSON error with a reset for unread TCP data.
                if MAX_BODY < length <= MAX_BODY * 2:
                    self.rfile.read(length)
                if not 0 < length <= MAX_BODY:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if self.path == "/api/v1/annotations":
                    result = notebook.save(payload)
                else:
                    prefix, suffix = "/api/v1/annotations/", "/revisions"
                    if not self.path.startswith(prefix) or not self.path.endswith(suffix):
                        return self.send(404, b'{}')
                    event_id = self.path[len(prefix):-len(suffix)]
                    if (not isinstance(payload, dict) or set(payload) != {"annotation", "expected_previous_revision"}
                            or not isinstance(payload["annotation"], dict) or payload["annotation"].get("event_id") != event_id
                            or payload["annotation"].get('revision') == 1):
                        raise ValueError("Invalid revision envelope")
                    result = notebook.save(payload["annotation"], expected_previous_revision=payload["expected_previous_revision"])
                self.send(200 if result["idempotent"] else 201, json.dumps(result, allow_nan=False).encode())
            except NotebookError as exc:
                self.send(exc.status, json.dumps({"schema_version": 1, "error_code": exc.code, "error": str(exc)}).encode())
            except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
                self.send(400, b'{"schema_version":1,"error_code":"invalid_annotation","error":"Check annotation fields, JSON, and request size"}')
            except sqlite3.Error:
                service.diagnostics.record("notebook_write_failed", "Cannot save hub note; retry with the same event/revision after checking storage")
                self.send(503, b'{"schema_version":1,"error_code":"notebook_storage_failed","error":"Note was not confirmed saved; retry the same event/revision"}')

        def do_POST(self):
            origin = self.headers.get("Origin")
            if (not self.allowed_host() or self.headers.get("X-Hub-Request") != "1"
                    or (origin and (urlsplit(origin).netloc != self.headers.get("Host") or urlsplit(origin).scheme != "http"))
                    or self.headers.get("Content-Type") != "application/json"):
                return self.reject_unread(403, b'{}')
            if self.path.startswith("/api/v1/annotations"):
                return self.notebook_post()
            if self.path.startswith('/api/v1/collector/') or self.path.startswith('/api/v1/transfers/'):
                return self.queue_post()
            if self.path != "/api/control":
                return self.reject_unread(404, b'{}')
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
                    if not isinstance(source, DemoSource):
                        return self.send(404,b'{"error_code":"demo_only"}')
                    source.configure(action, payload.get("value"))
                self.send(200, b'{"ok":true}')
            except (ValueError, KeyError, TypeError):
                self.send(400, b'{"error_code":"invalid_control","error":"Check action, value, and JSON request size"}')
            except sqlite3.Error:
                service.diagnostics.record("settings_write_failed", "Cannot save operator preference; check catalog/local storage")
                self.send(503, b'{"error_code":"settings_write_failed","error":"Preference was not saved; check local storage"}')

        def queue_post(self):
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 2048:
                    raise ValueError('Invalid request size')
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):
                    raise ValueError('Expected JSON object')
                if self.path in ('/api/v1/collector/pause','/api/v1/collector/resume'):
                    if payload:
                        raise ValueError('Unexpected fields')
                    service.set_paused(self.path.endswith('/pause'))
                    return self.send(200,b'{"schema_version":1,"state":"saved"}')
                parts=self.path.split('/')
                if len(parts)!=6 or parts[:4]!=['','api','v1','transfers']:
                    return self.send(404,b'{}')
                identity,action=parts[4:]
                if action=='retry' and not payload:
                    service.retry_errors(identity)
                elif action=='priority' and not payload.keys()-{'priority','urgent'}:
                    service.set_priority(identity,payload['priority'],payload.get('urgent',False))
                else:
                    raise ValueError('Invalid transfer action')
                self.send(202,b'{"schema_version":1,"state":"pending"}')
            except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
                self.send(400,b'{"schema_version":1,"error_code":"invalid_control","error":"Check transfer identity and action fields"}')
            except sqlite3.Error:
                self.send(503,b'{"schema_version":1,"error_code":"settings_write_failed","error":"Preference was not saved"}')

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
