"""CI only: the hub as an agent on a Windows PC reaches it, behind a front that publishes it
under a path prefix and strips the prefix (as Tailscale Funnel or a reverse proxy does).

A request outside the prefix gets 404, so an agent that drops the prefix fails the checks.
With --upstream the rest goes to a real hub (server/xrnmoni.py); without, this answers as a
hub that takes any batch carrying one of the --token values. Every request becomes one JSON
line in --log: method, path, status, and for a batch its device, event types and what the
hub said of them (inserted, duplicates, rejected).

    python hub_proxy.py --port 5241 --prefix /xrnmoni --log requests.jsonl
                        (--upstream http://127.0.0.1:5240 | --token TOKEN [--token ...])
"""
import argparse
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORWARD = ("Authorization", "Content-Type", "If-Match", "X-Xrn-Client")


class Handler(BaseHTTPRequestHandler):
    opts = None
    lock = threading.Lock()

    def answer(self):
        o = self.opts
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        entry = {"method": self.command, "path": self.path}
        status, out, ctype = 404, b"not under the prefix", "text/plain"
        if self.path == o.prefix or self.path.startswith(o.prefix + "/"):
            path = self.path[len(o.prefix):] or "/"
            if self.command == "POST" and path.split("?")[0] == "/api/events":
                try:
                    batch = json.loads(body)
                    entry["device"] = batch.get("device")
                    entry["types"] = [e.get("type") for e in batch.get("events", [])]
                except (ValueError, AttributeError):
                    entry["bad_body"] = True
            if o.upstream:
                status, out, ctype = self.forward(path, body)
            elif path == "/healthz":
                status, out, ctype = 200, b'{"ok":true}', "application/json"
            elif "types" in entry:
                ok = self.headers.get("Authorization") in [f"Bearer {t}" for t in o.token]
                status = 200 if ok else 401
                out = json.dumps({"ok": True, "inserted": len(entry["types"]), "duplicates": 0,
                                  "rejected": []} if ok else {"error": "bad token"}).encode()
                ctype = "application/json"
            else:
                status, out = 404, b"no such path"
        entry["status"] = status
        if "types" in entry and status == 200:
            try:
                said = json.loads(out)
                for k in ("inserted", "duplicates"):
                    entry[k] = said.get(k)
                entry["rejected"] = said.get("rejected") or []
            except ValueError:
                pass
        with self.lock, open(o.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def forward(self, path, body):
        req = urllib.request.Request(self.opts.upstream + path, method=self.command,
                                     data=body if self.command in ("POST", "PUT") else None)
        for h in FORWARD:
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "text/plain")
        except OSError as e:
            return 502, str(e).encode(), "text/plain"

    do_GET = do_POST = do_PUT = do_DELETE = answer

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--prefix", default="/xrnmoni")
    ap.add_argument("--log", required=True)
    ap.add_argument("--upstream")
    ap.add_argument("--token", action="append", default=[])
    Handler.opts = ap.parse_args()
    Handler.opts.prefix = "/" + Handler.opts.prefix.strip("/")
    ThreadingHTTPServer(("127.0.0.1", Handler.opts.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
