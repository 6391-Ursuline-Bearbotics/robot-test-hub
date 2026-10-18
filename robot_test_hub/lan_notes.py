"""Phone notes over the practice network with a PIN (P1.4). Opt-in, notebook only.

A separate listener (hub.json "lan_notes_port") serves just the driver notebook and
its note API to phones on the practice network. Nothing else of the hub is reachable
from it. A phone enters the 6-digit PIN shown on the admin page once; the hub then
sets a session cookie for that phone until the hub restarts (the PIN changes on every
start). Wrong PINs are limited per address. Notes are saved on the phone first and
keep their original event time, exactly like the laptop notebook. Plain HTTP on the
team's own network: the PIN keeps strangers on the same network out, not a network
attacker who can read traffic.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 64 * 1024
FAIL_LIMIT = 5
FAIL_WINDOW_S = 60.0
STATIC = Path(__file__).parent / 'static'
PIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone notes · 6391</title><style>body{font-family:system-ui,sans-serif;background:#101820;color:#e8eef4;max-width:420px;margin:auto;padding:24px}
input{font-size:32px;letter-spacing:.3em;width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #586f82;background:#17232e;color:#fff;text-align:center}
button{font-size:20px;width:100%;margin-top:14px;padding:14px;border-radius:8px;border:1px solid #e6b957;background:#8b6a1f;color:#fff}p{color:#aebfcb}</style></head>
<body><h1>Team 6391 notes</h1><p>Enter the PIN shown on the hub's Transfers (admin) page.</p><form method="post" action="/pin">
<input name="pin" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" autofocus required>
<button>Open notebook</button></form><p>__MESSAGE__</p></body></html>"""


def address_rank(address, team=6391):
    """Robot network first, then other private LANs; virtual-adapter ranges (WSL/Hyper-V/Docker) last."""
    parts = [int(p) for p in address.split('.')]
    if parts[:3] == [10, team // 100, team % 100]:
        rank = 0
    elif parts[0] == 10:
        rank = 1
    elif parts[:2] == [192, 168]:
        rank = 2
    elif parts[0] == 172 and 16 <= parts[1] <= 31:
        rank = 4
    else:
        rank = 3
    return (rank, parts)


class LanNotes:
    def __init__(self, notebook, *, host='0.0.0.0', port=6392, clock=time.monotonic):
        self.notebook, self.clock = notebook, clock
        self.pin = f'{secrets.randbelow(10**6):06d}'
        self.tokens = set()
        self.failures = {}
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer((host, port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .2}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def urls(self):
        addresses = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                if not info[4][0].startswith('127.'):
                    addresses.add(info[4][0])
        except OSError:
            pass
        return [f'http://{a}:{self.port}/' for a in sorted(addresses, key=address_rank)]

    def _blocked(self, client):
        now = self.clock()
        with self.lock:
            times = [t for t in self.failures.get(client, []) if now - t < FAIL_WINDOW_S]
            self.failures[client] = times
            return len(times) >= FAIL_LIMIT

    def _fail(self, client):
        with self.lock:
            self.failures.setdefault(client, []).append(self.clock())

    def check_pin(self, client, pin):
        if self._blocked(client):
            return None
        if isinstance(pin, str) and secrets.compare_digest(pin.strip(), self.pin):
            token = secrets.token_urlsafe(24)
            with self.lock:
                self.tokens.add(token)
                self.failures.pop(client, None)
            return token
        self._fail(client)
        return None

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'RobotTestHubNotes'
            sys_version = ''

            def log_message(self, *args):
                pass

            def send(self, status, body, content_type='application/json', headers=()):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                for key, value in headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                for part in self.headers.get('Cookie', '').split(';'):
                    name, _, value = part.strip().partition('=')
                    if name == 'hubnotes':
                        with owner.lock:
                            return value in owner.tokens
                return False

            def pin_page(self, message='', status=200):
                self.send(status, PIN_PAGE.replace('__MESSAGE__', message).encode(), 'text/html; charset=utf-8')

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in ('/', '/pin'):
                    if self.authorized():
                        return self.send(303, b'', headers=(('Location', '/notebook'),))
                    return self.pin_page()
                if not self.authorized():
                    if path == '/notebook':
                        return self.send(303, b'', headers=(('Location', '/pin'),))
                    return self.send(401, b'{"error_code":"pin_required"}')
                if path == '/notebook':
                    return self.send(200, (STATIC / 'notebook.html').read_bytes(), 'text/html; charset=utf-8')
                if path in ('/notebook.js', '/notebook-sw.js'):
                    return self.send(200, (STATIC / path[1:]).read_bytes(), 'application/javascript; charset=utf-8')
                if path.startswith('/api/v1/annotations'):
                    return self.annotations_get()
                return self.send(404, b'{}')

            def annotations_get(self):
                from .notebook import NotebookError
                try:
                    url = urlsplit(self.path)
                    query = {k: v[0] for k, v in parse_qs(url.query, max_num_fields=8).items()}
                    if url.path == '/api/v1/annotations':
                        if query.keys() - {'from', 'to', 'include_unknown', 'limit', 'cursor'}:
                            raise ValueError()
                        result = owner.notebook.list(start_ns=query.get('from'), end_ns=query.get('to'),
                                                     include_unknown=query.get('include_unknown', 'true') == 'true',
                                                     limit=int(query.get('limit', '50')), cursor=query.get('cursor'))
                    else:
                        event = url.path[len('/api/v1/annotations/'):]
                        if '/' in event or query.keys() - {'limit', 'after_revision'}:
                            return self.send(404, b'{}')
                        result = owner.notebook.history(event, limit=int(query.get('limit', '50')),
                                                        after_revision=int(query.get('after_revision', '0')))
                    return self.send(200, json.dumps(result, allow_nan=False).encode())
                except NotebookError as exc:
                    return self.send(exc.status, json.dumps({'schema_version': 1, 'error_code': exc.code}).encode())
                except (ValueError, TypeError, OverflowError):
                    return self.send(400, b'{"schema_version":1,"error_code":"invalid_query"}')

            def do_POST(self):
                from .notebook import NotebookError
                path = urlsplit(self.path).path
                client = self.client_address[0]
                length = int(self.headers.get('Content-Length', '0') or 0)
                if not 0 < length <= MAX_BODY:
                    return self.send(400, b'{"error_code":"invalid_request"}')
                body = self.rfile.read(length)
                if path == '/pin':
                    pin = parse_qs(body.decode('utf-8', 'replace')).get('pin', [''])[0]
                    if owner._blocked(client):
                        return self.pin_page('Too many wrong PINs. Wait a minute and try again.', 429)
                    token = owner.check_pin(client, pin)
                    if token is None:
                        return self.pin_page('That PIN is not right. Check the admin page.', 403)
                    return self.send(303, b'', headers=(('Location', '/notebook'),
                                     ('Set-Cookie', f'hubnotes={token}; HttpOnly; SameSite=Strict; Path=/')))
                if not self.authorized():
                    return self.send(401, b'{"error_code":"pin_required"}')
                if (self.headers.get('X-Hub-Request') != '1' or self.headers.get('Content-Type') != 'application/json'
                        or not path.startswith('/api/v1/annotations')):
                    return self.send(403, b'{}')
                try:
                    payload = json.loads(body)
                    if path == '/api/v1/annotations':
                        result = owner.notebook.save(payload)
                    else:
                        prefix, suffix = '/api/v1/annotations/', '/revisions'
                        if not path.startswith(prefix) or not path.endswith(suffix):
                            return self.send(404, b'{}')
                        event = path[len(prefix):-len(suffix)]
                        if (not isinstance(payload, dict) or set(payload) != {'annotation', 'expected_previous_revision'}
                                or payload['annotation'].get('event_id') != event):
                            raise ValueError()
                        result = owner.notebook.save(payload['annotation'],
                                                     expected_previous_revision=payload['expected_previous_revision'])
                    return self.send(200 if result['idempotent'] else 201, json.dumps(result, allow_nan=False).encode())
                except NotebookError as exc:
                    return self.send(exc.status, json.dumps({'schema_version': 1, 'error_code': exc.code, 'error': str(exc)}).encode())
                except (ValueError, TypeError, KeyError, AttributeError):
                    return self.send(400, b'{"schema_version":1,"error_code":"invalid_annotation"}')
        return Handler
