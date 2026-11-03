"""A local HTTP server that answers POSTs from a script, for CES client tests."""
from __future__ import annotations

import http.server
import json
import threading


class FakeCes:
    def __init__(self):
        self.script = []          # (status, headers, body) answered in order; then 200 {}; bytes bodies are sent raw
        self.requests = []        # (path, headers, parsed body or None)
        self.methods = []         # the method of each request, in the same order
        self.respond = None       # optional (method, path, headers, body) -> (status, headers, body), used when script is empty
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PATCH(self):
                self.do_POST()

            def do_GET(self):
                self.do_POST()

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                parsed = json.loads(raw) if raw else None
                fake.requests.append((self.path, dict(self.headers), parsed))
                fake.methods.append(self.command)
                if fake.script:
                    status, headers, body = fake.script.pop(0)
                elif fake.respond:
                    status, headers, body = fake.respond(self.command, self.path, dict(self.headers), parsed)
                else:
                    status, headers, body = (200, {}, {})
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

            def log_message(self, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
