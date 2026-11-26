"""The browser bridge: what it takes, from whom, and what it keeps of a URL."""
import contextlib
import http.client
import io
import json
import math
import threading
import time
import unittest

from agent_helpers import config

import agent

ORIGIN = "moz-extension://4f0c2a8e-0000-4000-8000-123456789abc"
SECRET = "bridge-secret-for-tests"
T = time.time() - 3600


class Collect:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class Away:
    """Stands in for the tracker: the idle/locked/sleep periods it knows."""

    def __init__(self, periods=()):
        self.periods = list(periods)

    def away_periods(self, after):
        return [p for p in self.periods if p[1] > after]


class BridgeCase(unittest.TestCase):
    def start(self, tracker=None, **kw):
        cfg = config(bridge_origins=[ORIGIN], bridge_secret=SECRET, **kw)
        self.sender = Collect()
        self.bridge = agent.BrowserBridge(cfg, self.sender, tracker)
        threading.Thread(target=self.bridge.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.bridge.server_close)
        self.addCleanup(self.bridge.shutdown)

    def post(self, body, origin=ORIGIN, secret=SECRET, path="/browser", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.bridge.server_address[1], timeout=5)
        h = {"Content-Type": "application/json"}
        if origin:
            h["Origin"] = origin
        if secret is not None:
            h["X-Xrnmoni-Secret"] = secret
        h.update(headers or {})
        c.request("POST", path, body if isinstance(body, bytes) else json.dumps(body), h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def page(self, **kw):
        return dict({"url": "https://example.com/", "title": "Example", "ts_start": T,
                     "ts_end": T + 20}, **kw)


class ValidationTest(BridgeCase):
    def setUp(self):
        self.start()

    def test_bad_payloads_get_400_and_the_bridge_keeps_going(self):
        bad = [b"not json", b"[1, 2]", b'"text"', b"5", b"null", b"[]", b"{}",
               json.dumps([self.page(), "x"]).encode(),
               json.dumps(self.page(ts_start="abc")).encode(),
               json.dumps(self.page(ts_start=True)).encode(),
               json.dumps(self.page(ts_end=None)).encode(),
               b'{"url":"https://a.example/","ts_start":NaN,"ts_end":1}',
               json.dumps(self.page(ts_end=T - 5)).encode(),           # ends before it starts
               json.dumps(self.page(ts_end=T + 2 * 86400)).encode(),   # longer than a day
               json.dumps(self.page(ts_start=5, ts_end=10)).encode(),  # 1970
               json.dumps(self.page(url=["x"])).encode(),
               json.dumps(self.page(title={"a": 1})).encode(),
               json.dumps(self.page(url="http://[::1")).encode(),
               json.dumps([self.page()] * 101).encode()]
        for body in bad:
            with self.subTest(body=body[:60]):
                self.assertEqual(self.post(body)[0], 400)
        self.assertEqual(self.sender.events, [])
        self.assertEqual(self.post(self.page())[0], 200)
        self.assertEqual(len(self.sender.events), 1)

    def test_a_bad_content_length_is_400(self):
        c = http.client.HTTPConnection("127.0.0.1", self.bridge.server_address[1], timeout=5)
        c.putrequest("POST", "/browser")
        c.putheader("Origin", ORIGIN)
        c.putheader("Content-Length", "lots")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_only_the_browser_path(self):
        self.assertEqual(self.post(self.page(), path="/other")[0], 404)


class AccessTest(BridgeCase):
    def test_only_listed_origins_with_the_secret(self):
        self.start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            other = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
            self.assertEqual(self.post(self.page(), origin=other)[0], 403)
            self.assertEqual(self.post(self.page(), origin=other)[0], 403)
            self.assertEqual(self.post(self.page(), secret=None)[0], 403)
            self.assertEqual(self.post(self.page(), secret="wrong")[0], 403)
            self.assertEqual(self.post(self.page(), origin="https://evil.example")[0], 403)
        self.assertEqual(self.sender.events, [])
        logged = out.getvalue()
        self.assertEqual(logged.count(other), 1)  # the first unknown origin, once
        self.assertIn("bridge_origins", logged)
        self.assertNotIn("evil.example", logged)
        status, _ = self.post(self.page())
        self.assertEqual(status, 200)

    def test_no_secret_configured_means_nothing_is_taken(self):
        cfg = config(bridge_origins=[ORIGIN], bridge_secret="")
        self.sender = Collect()
        self.bridge = agent.BrowserBridge(cfg, self.sender)
        threading.Thread(target=self.bridge.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.bridge.server_close)
        self.addCleanup(self.bridge.shutdown)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.post(self.page(), secret="")[0], 403)

    def test_the_preflight_allows_the_secret_header(self):
        self.start()
        c = http.client.HTTPConnection("127.0.0.1", self.bridge.server_address[1], timeout=5)
        c.request("OPTIONS", "/browser", headers={"Origin": ORIGIN,
                                                  "Access-Control-Request-Method": "POST"})
        r = c.getresponse()
        self.assertEqual(r.status, 204)
        self.assertIn("X-Xrnmoni-Secret", r.getheader("Access-Control-Allow-Headers"))
        c.close()


class UrlTest(BridgeCase):
    URL = "https://user:pw@Mail.Example.com/inbox/42?q=secret&token=abc#msg-9"

    def test_query_fragment_and_password_are_dropped_by_default(self):
        self.start()
        self.assertEqual(self.post(self.page(url=self.URL))[0], 200)
        e = self.sender.events[0]
        self.assertEqual(e["url"], "https://Mail.Example.com/inbox/42")
        self.assertEqual(e["domain"], "mail.example.com")

    def test_keep_url_query_keeps_them_but_not_the_password(self):
        self.start(keep_url_query=True)
        self.assertEqual(self.post(self.page(url=self.URL))[0], 200)
        self.assertEqual(self.sender.events[0]["url"],
                         "https://Mail.Example.com/inbox/42?q=secret&token=abc#msg-9")


class PrivateTabTest(BridgeCase):
    def test_a_private_tab_keeps_its_time_but_not_its_address_or_title(self):
        self.start()
        page = self.page(url="https://mail.example.com/inbox/42", title="Inbox (3)", private=True)
        self.assertEqual(self.post(page)[0], 200)
        e = self.sender.events[0]
        self.assertEqual((e["url"], e["domain"], e["title"]), (None, None, "Private"))
        self.assertEqual((e["ts_start"], e["ts_end"]), (T, T + 20))

    def test_private_must_be_true_to_count(self):
        self.start()
        self.assertEqual(self.post(self.page(private="no"))[0], 200)
        self.assertEqual(self.sender.events[0]["domain"], "example.com")


class GatingTest(BridgeCase):
    def spans(self):
        return [(e["ts_start"] - T, e["ts_end"] - T) for e in self.sender.events]

    def test_page_time_while_idle_or_locked_is_cut_out(self):
        self.start(tracker=Away([(T + 10, T + 20), (T + 40, math.inf)]))
        self.assertEqual(self.post(self.page(ts_end=T + 50, id="ab" * 16))[0], 200)
        self.assertEqual(self.spans(), [(0, 10), (20, 40)])
        ids = [e["id"] for e in self.sender.events]
        self.assertEqual(len(set(ids)), 2)
        self.assertNotIn("ab" * 16, ids)  # pieces get their own (stable) ids
        status, body = self.post(self.page(ts_start=T + 41, ts_end=T + 49))
        self.assertEqual((status, json.loads(body)["accepted"]), (200, 0))

    def test_long_page_intervals_are_sent_as_minutes(self):
        self.start()
        self.assertEqual(self.post(self.page(ts_end=T + 150))[0], 200)
        self.assertEqual(self.spans(), [(0, 60), (60, 120), (120, 150)])

    def test_the_tracker_reports_its_away_periods(self):
        from test_tracker import Clock, FakeBackend
        clock, backend, events = Clock(T), FakeBackend(), []
        tracker = agent.Tracker(config(), events.extend, backend, now=clock)
        tracker.tick()
        clock.t += 100
        backend.idle = 70.0
        tracker.tick()                       # idle since T + 30
        self.assertEqual(tracker.away_periods(T), [(T + 30, math.inf)])
        clock.t += 10
        backend.idle = 0.0
        tracker.tick()                       # back at T + 110
        self.assertEqual(tracker.away_periods(T), [(T + 30, T + 110)])
        self.assertEqual(tracker.away_periods(T + 120), [])


if __name__ == "__main__":
    unittest.main()


class RefusedBodyTest(unittest.TestCase):
    """A refused post's body is read before the answer goes out. Closing a socket with unread
    data makes Windows reset the connection, and the sender can lose the answer: CI on
    windows-latest saw ConnectionAbortedError instead of the 413 for an oversized body."""

    def handle(self, raw):
        class Kept(io.BytesIO):
            def close(self):  # the handler closes its files; keep them readable for the checks
                pass

        class Conn:
            def __init__(self):
                self.inp, self.out = Kept(raw), Kept()

            def makefile(self, mode, *a, **k):
                return self.inp if "r" in mode else self.out

            def sendall(self, data):
                self.out.write(data)

            def settimeout(self, t):
                pass

        class Server:
            origins = {ORIGIN}
            secret = SECRET

            def log_once(self, *a):
                pass

        conn = Conn()
        agent._BridgeHandler(conn, ("127.0.0.1", 1), Server())
        return conn

    def request(self, origin, body):
        return (f"POST /browser HTTP/1.0\r\nOrigin: {origin}\r\nX-Xrnmoni-Secret: {SECRET}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body

    def test_an_oversized_body_is_read_before_the_413(self):
        raw = self.request(ORIGIN, b'{"title":"' + b"x" * (agent.MAX_BODY + 1024) + b'"}')
        conn = self.handle(raw)
        self.assertTrue(conn.out.getvalue().startswith(b"HTTP/1.0 413"))
        self.assertEqual(conn.inp.tell(), len(raw))

    def test_a_refused_origins_body_is_read_before_the_403(self):
        raw = self.request("https://evil.example", b'{"url":"https://example.com"}')
        conn = self.handle(raw)
        self.assertTrue(conn.out.getvalue().startswith(b"HTTP/1.0 403"))
        self.assertEqual(conn.inp.tell(), len(raw))
