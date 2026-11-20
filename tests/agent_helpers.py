"""Shared pieces for the agent tests: a fake ingest server and a config object."""
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "agent")
if AGENT not in sys.path:
    sys.path.insert(0, AGENT)


def config(**kw):
    base = dict(server_url="http://127.0.0.1:9", token="t" * 20, device="test-pc",
                poll_interval=2.0, flush_interval=10.0, afk_threshold=60.0, bridge_port=0,
                spool_path=":memory:", spool_max_days=30, spool_max_rows=200000,
                bridge_origins=[], bridge_secret="", keep_url_query=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


class FakeServer:
    """An ingest server on an ephemeral port. `reply(body)` returns (status, payload) for
    each POST; every request is recorded as (path, headers, body)."""

    def __init__(self, reply=None):
        self.requests = []
        self.reply = reply or (lambda body: (200, {"ok": True, "inserted": len(body["events"])}))
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, dict(self.headers), body))
                status, payload = outer.reply(body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    def events(self):
        return [e for _, _, body in self.requests for e in body["events"]]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
