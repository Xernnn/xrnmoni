"""Tests for xrnmoni.py: ingest, dedupe, storage, retention, logins, and starting with no console.

    python3 -m unittest server/test_xrnmoni.py     (from the repo root; needs bcrypt)
"""
import base64
import gzip
import http.client
import importlib.util
import io
import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAM = os.path.join(HERE, "xrnmoni.py")
spec = importlib.util.spec_from_file_location("xrnmoni_server", PROGRAM)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

RIGHT = "right-password"
GUEST = "guest-password"
T0 = 1780000000.0  # a Thursday in 2026, before the tests' "now"


def ev(kind, start, end=None, **fields):
    """An event as an agent sends it, with a fresh id unless told otherwise."""
    e = {"id": uuid.uuid4().hex, "type": kind, "ts_start": start}
    if end is not None:
        e["ts_end"] = end
        e["duration"] = end - start
    e.update(fields)
    return e


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class HubTest(unittest.TestCase):
    legacy = ""
    viewers = ()

    def setUp(self):
        import bcrypt

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # last: on Windows a folder with an open database can't go
        self.data = self.tmp.name
        self.users = os.path.join(self.data, "users")
        with open(self.users, "w") as f:
            for name, pw in (("alex", RIGHT), ("guest", GUEST)):
                f.write(f"{name}:" + bcrypt.hashpw(pw.encode(), bcrypt.gensalt(4)).decode() + "\n")
        for name, value in (("FAIL_DELAY_S", 0),):
            patch = unittest.mock.patch.object(srv, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.app = srv.App(self.data, self.users, viewers=self.viewers, legacy_token=self.legacy)
        self.token = self.app.tokens.add("laptop")
        self.pc_token = self.app.tokens.add("pc")
        handler = type("H", (srv.Handler,), {"app": self.app})
        self.httpd = srv.Server(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.store.db.close()

    def call(self, method, path, body=None, headers=None, auth=("alex", RIGHT), raw=False):
        data = body if raw else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, method=method, data=data)
        if isinstance(auth, tuple):
            req.add_header("Authorization", basic(*auth))
        elif auth:
            req.add_header("Authorization", auth)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers

    def post(self, events, token=None, device="laptop", os_name="linux", headers=None):
        body = {"device": device, "os": os_name, "events": events}
        status, raw, _ = self.call("POST", "/api/events", body, headers, auth="Bearer " + (token or self.token))
        return status, (json.loads(raw) if raw.startswith(b"{") else raw)

    def get(self, path, **kw):
        status, raw, headers = self.call("GET", path, **kw)
        self.assertEqual(200, status, raw)
        return json.loads(raw)

    def summary(self, device="laptop", since=T0 - 86400, until=T0 + 86400):
        return self.get(f"/api/summary?device={device}&since={since}&until={until}")

    def rows(self, sql="SELECT type, device, uid FROM events ORDER BY id"):
        with self.app.store.lock:
            return self.app.store.db.execute(sql).fetchall()


class IngestTest(HubTest):
    def test_a_batch_sent_twice_is_counted_once(self):
        # Nothing told one post from another, so a batch the agent sent again (its answer lost on
        # the way back) was stored twice: 120 s for 60 s of work.
        batch = [ev("app", T0, T0 + 60, app="code", title="main.py"), ev("idle", T0 + 60, T0 + 90)]
        self.assertEqual((200, {"ok": True, "inserted": 2, "duplicates": 0, "rejected": []}), self.post(batch))
        self.assertEqual((200, {"ok": True, "inserted": 0, "duplicates": 2, "rejected": []}), self.post(batch))
        self.assertEqual(2, len(self.rows()))
        self.assertEqual(60, self.summary()["totals"]["app_seconds"])

    def test_the_same_id_twice_in_one_batch_is_one_event(self):
        e = ev("app", T0, T0 + 60, app="code")
        self.assertEqual(1, self.post([e, dict(e)])[1]["inserted"])

    def test_an_id_with_dashes_or_capitals_is_the_same_id(self):
        u = uuid.uuid4()
        self.post([ev("app", T0, T0 + 60, app="code", id=str(u).upper())])
        self.assertEqual(1, self.post([ev("app", T0, T0 + 60, app="code", id=u.hex)])[1]["duplicates"])

    def test_events_without_an_id_are_still_taken(self):
        # An agent from before ids. Sent twice, the same interval is two rows, but its time counts once.
        old = {"type": "app", "app": "code", "ts_start": T0, "ts_end": T0 + 60, "duration": 60}
        self.assertEqual(1, self.post([old])[1]["inserted"])
        self.assertEqual(1, self.post([old])[1]["inserted"])
        self.assertEqual(2, len(self.rows()))
        self.assertEqual(60, self.summary()["totals"]["app_seconds"])

    def test_one_bad_event_is_named_and_the_rest_are_kept(self):
        # One event the server didn't like refused the whole batch, and the agent sent the same
        # batch again forever: nothing it recorded afterwards ever arrived.
        bad_id = uuid.uuid4().hex
        batch = [ev("app", T0, T0 + 10, app="a"),
                 ev("app", "abc", app="b", id=bad_id),
                 "not an event",
                 ev("Bad Type!", T0),
                 ev("app", T0 + 10, T0 + 5),
                 ev("idle", T0, T0 + 8 * 86400),
                 ev("app", T0, T0 + 10, app=7),
                 ev("app", time.time() + 3 * 86400),
                 ev("app", 100.0),
                 ev("app", T0, duration=-1),
                 ev("app", T0, id="not-hex"),
                 ev("app", T0, T0 + 10, app="c", title=True),
                 {"type": "app", "ts_end": T0},
                 ev("app", T0 + 20, T0 + 30, app="d")]
        status, answer = self.post(batch)
        self.assertEqual(200, status)
        self.assertEqual(2, answer["inserted"])
        self.assertEqual(list(range(1, 13)), [r["index"] for r in answer["rejected"]])
        self.assertEqual(bad_id, answer["rejected"][0]["id"])
        self.assertEqual("ts_start must be a number", answer["rejected"][0]["error"])
        self.assertEqual(["a", "d"], [r[0] for r in self.rows("SELECT app FROM events ORDER BY id")])

    def test_a_malformed_batch_is_refused_whole(self):
        for body in ([1, 2], {"device": "laptop"}, {"device": "laptop", "events": {"a": 1}},
                     {"device": "laptop", "events": [ev("app", T0)] * (srv.MAX_EVENTS + 1)}):
            status, _, _ = self.call("POST", "/api/events", body, auth="Bearer " + self.token)
            self.assertEqual(400, status, str(body)[:60])
        status, _, _ = self.call("POST", "/api/events", b"{not json", auth="Bearer " + self.token, raw=True)
        self.assertEqual(400, status)
        self.assertEqual([], self.rows())

    def test_an_empty_batch_says_the_device_is_there(self):
        self.assertEqual(200, self.post([])[0])
        self.assertEqual(["laptop"], [d["device"] for d in self.get("/api/devices")["devices"]])

    def test_a_device_token_posts_as_its_own_device(self):
        # A token taken from one machine can't write another's day.
        self.post([ev("app", T0, T0 + 60, app="code")], device="pc")
        self.post([ev("app", T0, T0 + 60, app="game")], token=self.pc_token, device="laptop")
        self.assertEqual([("laptop", "code"), ("pc", "game")],
                         self.rows("SELECT device, app FROM events ORDER BY device"))

    def test_an_agent_without_a_right_token_posts_nothing(self):
        for auth in (None, "Bearer nope", "Bearer " + self.token[:-1] + "x", basic("alex", RIGHT),
                     "Bearer xrnmoni_00000000_" + "a" * 43):
            status, _, headers = self.call("POST", "/api/events", {"device": "x", "events": [ev("app", T0)]},
                                           auth=auth)
            self.assertEqual(401, status, auth)
        self.assertEqual([], self.rows())

    def test_a_revoked_token_is_refused_at_once(self):
        self.assertEqual(200, self.post([])[0])
        self.assertTrue(self.app.tokens.revoke(self.token[8:16]))
        self.assertEqual(401, self.post([])[0])
        self.assertEqual(200, self.post([], token=self.pc_token)[0])

    def test_a_device_token_reads_nothing(self):
        for path in ("/api/devices", "/api/summary?device=laptop", "/api/activity"):
            self.assertEqual(401, self.call("GET", path, auth="Bearer " + self.token)[0], path)

    def test_the_first_agents_type_names_are_stored_as_the_new_ones(self):
        self.post([ev("browser", T0, T0 + 60, url="https://Example.com/a?b=1", title="A"),
                   ev("afk", T0 + 60, T0 + 120), ev("boot", T0 - 600)])
        self.assertEqual([("web", "example.com"), ("idle", None), ("boot", None)],
                         self.rows("SELECT type, domain FROM events ORDER BY id"))

    def test_a_type_this_server_doesnt_know_is_kept_as_it_is(self):
        self.post([ev("screensaver", T0, T0 + 300)])
        self.assertEqual([("screensaver",)], self.rows("SELECT type FROM events"))

    def test_long_text_is_cut_not_refused(self):
        self.post([ev("app", T0, T0 + 1, app="a" * 500, title="t" * 5000)])
        self.assertEqual([(200, 1000)], self.rows("SELECT length(app), length(title) FROM events"))

    def test_an_oversized_body_is_refused(self):
        with unittest.mock.patch.object(srv, "MAX_BODY", 2000):
            status, _ = self.post([ev("app", T0, T0 + 1, title="x" * 3000)])
        self.assertEqual(413, status)

    def test_a_gzip_body_is_read_and_a_gzip_bomb_is_not(self):
        body = gzip.compress(json.dumps({"device": "laptop", "events": [ev("app", T0, T0 + 1, app="z")]}).encode())
        status, _, _ = self.call("POST", "/api/events", body, {"Content-Encoding": "gzip"},
                                 auth="Bearer " + self.token, raw=True)
        self.assertEqual(200, status)
        bomb = gzip.compress(b'{"device": "laptop", "events": [], "pad": "' + b"0" * (srv.MAX_BODY + 10) + b'"}')
        self.assertLess(len(bomb), srv.MAX_BODY)
        status, _, _ = self.call("POST", "/api/events", bomb, {"Content-Encoding": "gzip"},
                                 auth="Bearer " + self.token, raw=True)
        self.assertEqual(413, status)
        self.assertEqual(1, len(self.rows()))

    def cut_upload(self, reset):
        """A post whose body stops half way, then the connection closes; with [reset], hard."""
        body = json.dumps({"device": "laptop", "events": [ev("app", T0, T0 + 1, title="a" * 4000)]}).encode()
        head = (b"POST /api/events HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + self.token.encode() +
                b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n")
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
                s.sendall(head + body[:len(body) // 2])
                if reset:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    answer = b""
                else:
                    s.shutdown(socket.SHUT_WR)
                    answer = b"".join(iter(lambda: s.recv(4096), b""))
            for _ in range(300):  # the handler finishes on its own thread
                if any(word in log.getvalue() for word in ("dropped", "Traceback", '" 400 ')):
                    break
                time.sleep(0.01)
            time.sleep(0.1)
        return answer, log.getvalue()

    def test_an_upload_cut_short_is_answered_as_such_and_stores_nothing(self):
        answer, log = self.cut_upload(reset=False)
        self.assertRegex(answer, rb"^HTTP/1\.[01] 400 ")
        self.assertIn(b"the upload stopped short", answer)
        self.assertNotIn("Traceback", log)
        self.assertEqual([], self.rows())

    @unittest.skipIf(sys.platform == "win32", "a reset reaches the handler differently on Windows")
    def test_a_caller_that_hangs_up_is_one_line_in_the_log_not_a_traceback(self):
        _, log = self.cut_upload(reset=True)
        self.assertNotIn("Traceback", log)
        self.assertIn("connection dropped", log)


class LegacyTokenTest(HubTest):
    legacy = "the-old-shared-token-123"

    def test_the_old_shared_token_still_posts_under_the_name_sent(self):
        status, answer = self.post([ev("app", T0, T0 + 60, app="code")], token=self.legacy, device="kids-pc")
        self.assertEqual((200, 1), (status, answer["inserted"]))
        self.assertEqual([("kids-pc",)], self.rows("SELECT device FROM events"))
        status, _ = self.post([ev("app", T0, T0 + 60)], token=self.legacy, device="")
        self.assertEqual(400, status)

    def test_the_old_shared_token_reads_nothing(self):
        self.assertEqual(401, self.call("GET", "/api/devices", auth="Bearer " + self.legacy)[0])

    def test_the_examples_placeholder_token_posts_nothing(self):
        other = os.path.join(self.data, "other")
        os.makedirs(other)
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            app = srv.App(other, self.users, legacy_token="change-me")
        self.addCleanup(app.store.db.close)
        self.assertEqual((False, None), app.tokens.device_for("Bearer change-me"))
        self.assertIn("change-me", log.getvalue())


class StorageTest(HubTest):
    def test_the_database_is_in_wal_mode_with_the_indexes_reads_use(self):
        self.assertEqual("wal", self.rows("PRAGMA journal_mode")[0][0])
        indexes = {r[0]: r[1] for r in self.rows("SELECT name, sql FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_events_device_ts", indexes)
        self.assertIn("UNIQUE", indexes["idx_events_uid"])
        self.assertNotIn("idx_events_type", indexes)

    def test_old_events_go_after_the_retention_days(self):
        now = time.time()
        self.post([ev("app", now - 401 * 86400, now - 401 * 86400 + 60, app="old"),
                   ev("app", now - 399 * 86400, now - 399 * 86400 + 60, app="kept")], token=self.token)
        self.post([ev("app", now - 500 * 86400, now - 500 * 86400 + 60, app="old")], token=self.pc_token)
        self.assertEqual(2, self.app.prune(now))
        self.assertEqual([("kept",)], self.rows("SELECT app FROM events"))
        self.app.retain_days = 0  # keeps everything
        self.assertEqual(0, self.app.prune(now + 1000 * 86400))
        self.assertEqual(1, len(self.rows()))

    def test_an_old_database_is_brought_up_to_date(self):
        # The first server's xrnmoni.db, copied into the data folder, keeps its events.
        folder = os.path.join(self.data, "old")
        os.makedirs(folder)
        db = sqlite3.connect(os.path.join(folder, "xrnmoni.db"))
        db.executescript("""
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT NOT NULL, type TEXT NOT NULL,
                app TEXT, title TEXT, url TEXT, domain TEXT, ts_start REAL NOT NULL, ts_end REAL, duration REAL,
                created_at REAL NOT NULL);
            CREATE INDEX idx_events_device_ts ON events(device, ts_start);
            CREATE INDEX idx_events_type ON events(type);
            CREATE TABLE devices (device TEXT PRIMARY KEY, os TEXT, first_seen REAL, last_seen REAL);""")
        db.executemany("INSERT INTO events (device, type, app, domain, ts_start, ts_end, duration, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       [("pc", "app", "code", None, T0, T0 + 600, 600, T0), ("pc", "browser", None, "a.com", T0, T0 + 60, 60, T0),
                        ("pc", "afk", None, None, T0 + 600, T0 + 900, 300, T0), ("pc", "boot", None, None, T0 - 60, None, None, T0)])
        db.execute("INSERT INTO devices VALUES ('pc', 'windows', ?, ?)", (T0, T0))
        db.commit()
        db.close()
        store = srv.Store(os.path.join(folder, "xrnmoni.db"))
        self.addCleanup(store.db.close)
        with store.lock:
            types = [r[0] for r in store.db.execute("SELECT type FROM events ORDER BY id")]
            index = store.db.execute("SELECT name FROM sqlite_master WHERE name='idx_events_type'").fetchall()
        self.assertEqual(["app", "web", "idle", "boot"], types)
        self.assertEqual([], index)
        self.assertEqual(4, len(store.rows("pc", T0 - 3600, T0 + 3600)))
        self.assertEqual(600, store.spans["pc"])
        store.insert("pc", None, [srv.clean_event(ev("app", T0 + 900, T0 + 960, app="x"), time.time())], time.time())
        self.assertEqual(5, len(store.rows("pc", T0 - 3600, T0 + 3600)))

    def test_an_event_that_began_the_day_before_is_found(self):
        self.post([ev("sleep", T0 - 5 * 3600, T0 + 3600), ev("app", T0 + 3600, T0 + 3700, app="code")])
        self.assertEqual(["sleep", "app"], [r[0] for r in self.app.store.rows("laptop", T0, T0 + 86400)])


class SummaryTest(HubTest):
    def test_a_summary_counts_only_its_own_part_of_an_event(self):
        # Intervals were put in the day they began: one running past midnight counted whole there.
        self.post([ev("app", T0 - 1800, T0 + 1800, app="code")])
        totals = self.summary(since=T0, until=T0 + 86400)["totals"]
        self.assertEqual(1800, totals["app_seconds"])
        self.assertEqual(T0, totals["first_ts"])

    def test_time_away_from_the_device_isnt_counted(self):
        # A page left open, or an app the agent still saw in front, while nobody was there.
        self.post([ev("app", T0, T0 + 600, app="zen"),
                   ev("web", T0, T0 + 600, domain="news.example", title="News"),
                   ev("idle", T0 + 300, T0 + 600),
                   ev("app", T0 + 600, T0 + 900, app="code"),
                   ev("locked", T0 + 700, T0 + 800)])
        s = self.summary()
        self.assertEqual([{"name": "zen", "seconds": 300}, {"name": "code", "seconds": 200}], s["apps"])
        self.assertEqual([{"name": "news.example", "seconds": 300}], s["domains"])
        self.assertEqual(300, s["totals"]["afk_seconds"])

    def test_reads_are_checked(self):
        for path in ("/api/summary?device=laptop&since=10&until=5", "/api/summary?device=laptop&since=a",
                     "/api/summary?device=laptop&since=0&until=" + str(63 * 86400), "/api/summary",
                     "/api/activity?since=nan&until=5", "/api/activity?limit=x"):
            self.assertEqual(400, self.call("GET", path)[0], path)

    def test_activity_lists_the_events_newest_first(self):
        self.post([ev("app", T0, T0 + 60, app="a"), ev("app", T0 + 60, T0 + 120, app="b")])
        self.post([ev("app", T0 + 30, T0 + 90, app="c")], token=self.pc_token)
        got = self.get(f"/api/activity?since={T0 - 1}&until={T0 + 3600}")["events"]
        self.assertEqual(["b", "c", "a"], [e["app"] for e in got])
        got = self.get(f"/api/activity?since={T0 - 1}&until={T0 + 3600}&device=pc")["events"]
        self.assertEqual(["c"], [e["app"] for e in got])

    def test_devices_say_when_they_were_last_heard_from(self):
        before = time.time()
        self.post([], os_name="linux")
        self.post([], token=self.pc_token, os_name="windows")
        got = self.get("/api/devices")
        self.assertEqual([("laptop", "linux"), ("pc", "windows")], [(d["device"], d["os"]) for d in got["devices"]])
        self.assertGreaterEqual(got["devices"][0]["last_seen"], before)


class LoginTest(HubTest):
    def test_health_answers_without_a_login_with_and_without_the_prefix(self):
        for path in ("/health", "/xrnmoni/health"):
            self.assertEqual((200, b"ok"), self.call("GET", path, auth=None)[:2], path)
        for path in ("/healthz", "/xrnmoni/healthz"):
            self.assertEqual((200, {"ok": True}), (lambda r: (r[0], json.loads(r[1])))(self.call("GET", path, auth=None)))

    def test_every_read_needs_a_login(self):
        for path in ("/api/devices", "/api/summary?device=laptop", "/api/activity", "/xrnmoni/api/devices"):
            for auth in (None, ("alex", "nope"), ("nobody", RIGHT)):
                status, _, headers = self.call("GET", path, auth=auth)
                self.assertEqual(401, status, (path, auth))
                self.assertIn("Basic", headers["WWW-Authenticate"])

    def test_the_prefixed_and_the_bare_path_reach_the_same_place(self):
        self.post([ev("app", T0, T0 + 60, app="code")])
        status, _, _ = self.call("POST", "/xrnmoni/api/events", {"device": "laptop", "events": [ev("app", T0 + 60, T0 + 70)]},
                                 auth="Bearer " + self.token)
        self.assertEqual(200, status)
        self.assertEqual(self.get("/api/devices")["devices"], self.get("/xrnmoni/api/devices")["devices"])
        self.assertEqual(404, self.call("GET", "/xrnmonix/api/devices")[0])

    def test_only_the_owner_sees_the_data_unless_named(self):
        # The users file can be the one every service on the server shares, with other people in it.
        self.assertEqual(200, self.call("GET", "/api/devices")[0])
        self.assertEqual(403, self.call("GET", "/api/devices", auth=("guest", GUEST))[0])

    def test_docs_and_openapi_are_not_served(self):
        # FastAPI served its own /docs and /openapi.json, every route listed, to anyone.
        for path in ("/docs", "/openapi.json", "/redoc", "/xrnmoni/docs"):
            self.assertEqual(404, self.call("GET", path, auth=None)[0], path)

    def test_a_page_on_another_site_cant_post(self):
        for elsewhere in ({"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
                          {"Origin": "https://evil.example"}):
            self.assertEqual(403, self.post([ev("app", T0, T0 + 1)], headers=elsewhere)[0])
        self.assertEqual([], self.rows())
        for ours in ({"Sec-Fetch-Site": "same-origin"}, {"Origin": f"http://127.0.0.1:{self.port}"},
                     {"Origin": "https://Hub.example", "X-Forwarded-Host": "hub.example:443"}, {}):
            self.assertEqual(200, self.post([], headers=ours)[0], ours)

    def test_an_unparseable_request_gets_an_answer(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(b"GARBAGE\r\n\r\n")
            self.assertIn(b"400", s.recv(200))

    def test_the_log_shows_a_callers_control_characters_escaped(self):
        forged = b'2026-11-06_01:09:14.23919 198.51.100.66 "POST /api/events HTTP/1.1" 200 -'
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            for raw in (b"GET /a\x1b[2Jb HTTP/1.1\r\nX-Forwarded-For: 203.0.113.70\x1b]0;hi\x07\r\n\r\n",
                        b"GET /a\r" + forged + b" HTTP/1.1\r\n\r\n"):
                with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
                    s.sendall(raw)
                    while s.recv(4096):
                        pass
        text = log.getvalue()
        self.assertIn("203.0.113.70\\x1b]0;hi\\x07", text)
        self.assertIn("/a\\x1b[2Jb", text)
        self.assertIn("/a\\x0d2026-11-06", text)
        self.assertEqual([], [c for c in text if c != "\n" and (ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0)])

    def test_no_answer_names_the_python_version(self):
        for method in ("GET", "PATCH"):  # PATCH: http.server's own 501 page
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request(method, "/health")
            res = conn.getresponse()
            res.read()
            conn.close()
            self.assertNotIn("Python", res.getheader("Server"), method)

    def test_head_is_a_get_without_the_body(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(b"HEAD /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            answer = b"".join(iter(lambda: s.recv(4096), b""))
        self.assertRegex(answer, rb"^HTTP/1\.[01] 200 ")
        self.assertIn(b"Content-Length: 2", answer)
        self.assertTrue(answer.endswith(b"\r\n\r\n"), answer[-40:])

    def test_every_answer_carries_the_security_headers(self):
        _, _, headers = self.call("GET", "/api/devices")
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertEqual("DENY", headers["X-Frame-Options"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def guess(self, forwarded, n=0, user="alex"):
        return self.call("GET", "/api/devices", auth=(user, f"guess{n}"), headers={"X-Forwarded-For": forwarded})[0]

    def test_a_made_up_forwarded_hop_is_not_a_new_address(self):
        for i in range(10):
            self.assertEqual(401, self.guess(f"198.51.100.{i}, 203.0.113.20", i))
        self.assertEqual(429, self.guess("198.51.100.200, 203.0.113.20", 10))
        self.assertEqual(401, self.guess("203.0.113.20, 203.0.113.21"))

    def test_an_ipv6_guesser_is_its_64_not_each_address_in_it(self):
        for i in range(10):
            self.assertEqual(401, self.guess(f"2001:db8:1:2::{i + 1:x}", i))
        self.assertEqual(429, self.guess("2001:db8:1:2:aaaa::1", 10))
        self.assertEqual(401, self.guess("2001:db8:1:3::1"))
        self.assertEqual(401, self.guess("::ffff:203.0.113.40"))
        self.assertIn("203.0.113.40", self.app.login.failures)

    def test_a_name_that_isnt_there_costs_a_check_like_one_that_is(self):
        import bcrypt

        def checks(user):
            with unittest.mock.patch.object(bcrypt, "checkpw", wraps=bcrypt.checkpw) as checkpw:
                self.assertIsNone(self.app.login.check_basic(basic(user, "nope")))
            return checkpw.call_count
        self.assertEqual(1, checks("alex"))
        self.assertEqual(1, checks("nobody"))

    def timed(self, addr, auth):
        start = time.monotonic()
        status = self.call("GET", "/api/devices", auth=auth, headers={"X-Forwarded-For": addr})[0]
        return status, time.monotonic() - start

    def test_a_request_with_no_password_isnt_held(self):
        with unittest.mock.patch.object(srv, "FAIL_DELAY_S", 0.5):
            got, took = self.timed("203.0.113.60", None)
            self.assertEqual(401, got)
            self.assertLess(took, 0.4)
            got, took = self.timed("203.0.113.60", ("alex", "nope"))
            self.assertEqual(401, got)
            self.assertGreaterEqual(took, 0.5)

    def test_wrong_passwords_lock_the_address_out(self):
        with unittest.mock.patch.object(srv, "FAIL_LIMIT", 3):
            for wrong in ("nope", "nope2", "nope3"):
                self.assertEqual(401, self.call("GET", "/api/devices", auth=("alex", wrong))[0])
            status, _, headers = self.call("GET", "/api/devices")
            self.assertEqual(429, status)
            self.assertEqual(str(srv.FAIL_WINDOW_S), headers["Retry-After"])
            self.assertEqual(429, self.post([])[0])  # and its agent, until the window passes

    def test_guesses_sent_all_at_once_are_counted_before_they_are_checked(self):
        real, checked, answers = self.app.login.check_basic, [], []

        def slow(header):
            checked.append(header)
            time.sleep(0.3)
            return real(header)

        self.httpd.request_queue_size = 64
        with unittest.mock.patch.object(srv, "FAIL_LIMIT", 3), \
                unittest.mock.patch.object(self.app.login, "check_basic", slow):
            burst = [threading.Thread(target=lambda n=n: answers.append(
                self.call("GET", "/api/devices", auth=("alex", f"guess{n}"))[0])) for n in range(12)]
            for t in burst:
                t.start()
            for t in burst:
                t.join()
            self.assertEqual(3, len(checked))
            self.assertEqual([401] * 3 + [429] * 9, sorted(answers))

    def test_a_revoked_token_sent_again_and_again_counts_once(self):
        # An agent keeps posting with the token it has, every few seconds. Counted each time, it
        # locked its machine's address out, the owner's dashboard on it too.
        old = self.token
        self.app.tokens.revoke(old[8:16])
        with unittest.mock.patch.object(srv, "FAIL_LIMIT", 3):
            for _ in range(10):
                self.assertEqual(401, self.post([], token=old)[0])
            self.assertEqual(200, self.call("GET", "/api/devices")[0])

    def test_a_right_login_clears_only_its_own_names_mistakes(self):
        addr = "203.0.113.80"
        for n in range(srv.FAIL_LIMIT - 1):
            self.assertEqual(401, self.guess(addr, n))
        self.assertEqual(403, self.call("GET", "/api/devices", auth=("guest", GUEST), headers={"X-Forwarded-For": addr})[0])
        self.assertEqual(401, self.guess(addr, 100))
        self.assertEqual(429, self.guess(addr, 101))

    def test_a_connection_that_goes_quiet_is_let_go(self):
        with unittest.mock.patch.object(self.httpd.RequestHandlerClass, "timeout", 0.5):
            with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
                s.sendall(b"POST /api/events HTTP/1.1\r\nContent-Length: 100\r\n\r\n{")
                start = time.monotonic()
                while s.recv(4096):
                    pass
                self.assertLess(time.monotonic() - start, 5)


class SessionTest(HubTest):
    """The dashboard signs in once with the password and then carries the xrn_session cookie."""

    def sign_in(self, user="alex", password=RIGHT, headers=None):
        status, body, h = self.call("POST", "/api/session", auth=(user, password),
                                    headers={"X-Xrn-Client": "xrnmoni", **(headers or {})})
        cookie = (h.get("Set-Cookie") or "").split(";")[0]
        return status, cookie, h

    def read_with(self, cookie, client=True):
        headers = {"Cookie": cookie, **({"X-Xrn-Client": "xrnmoni"} if client else {})}
        return self.call("GET", "/api/devices", auth=None, headers=headers)

    def test_a_session_is_made_only_from_the_password(self):
        status, cookie, h = self.sign_in()
        self.assertEqual(200, status)
        self.assertTrue(cookie.startswith("xrn_session="))
        for part in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", f"Max-Age={srv.SESSION_S}"):
            self.assertIn(part, h["Set-Cookie"])
        self.assertEqual(200, self.read_with(cookie)[0])
        # Not from a cookie (a session would never end), not without the page's header.
        status, _, h = self.call("POST", "/api/session", auth=None, headers={"Cookie": cookie, "X-Xrn-Client": "x"})
        self.assertEqual((401, None), (status, h.get("Set-Cookie")))
        self.assertEqual(400, self.call("POST", "/api/session")[0])
        self.assertEqual(401, self.sign_in(password="nope")[0])

    def test_the_cookie_counts_only_with_the_pages_header(self):
        # A form on another site can make the browser send the cookie, but not the header.
        _, cookie, _ = self.sign_in()
        status, _, h = self.read_with(cookie, client=False)
        self.assertEqual(401, status)
        self.assertIn("Basic", h["WWW-Authenticate"])

    def test_the_pages_own_fetch_sees_a_plain_401(self):
        status, _, h = self.call("GET", "/api/devices", auth=None, headers={"X-Xrn-Client": "xrnmoni"})
        self.assertEqual((401, None), (status, h.get("WWW-Authenticate")))

    def test_signing_out_ends_the_cookie_for_good(self):
        _, cookie, _ = self.sign_in()
        status, _, h = self.call("DELETE", "/api/session", auth=None, headers={"Cookie": cookie})
        self.assertEqual(200, status)
        self.assertIn("Max-Age=0", h["Set-Cookie"])
        self.assertEqual(401, self.read_with(cookie)[0])  # a copy of it, kept elsewhere
        with open(os.path.join(self.data, "signed-out.json")) as f:
            self.assertEqual(1, len(json.load(f)))
        self.assertEqual(200, self.read_with(self.sign_in()[1])[0])

    def test_a_sign_out_on_another_server_sharing_the_users_file_counts_here(self):
        _, cookie, _ = self.sign_in()
        other = srv.Login(self.users)  # another xrn service reading the same users file and key
        self.assertEqual("alex", other.check_session(cookie))
        other.end_session(cookie)
        self.assertEqual(401, self.read_with(cookie)[0])
        made_there = other.make_session("alex")
        self.assertEqual(200, self.read_with(f"xrn_session={made_there}")[0])

    def test_a_new_password_ends_every_session(self):
        import bcrypt

        _, cookie, _ = self.sign_in()
        with open(self.users) as f:
            lines = f.read().splitlines()
        lines[0] = "alex:" + bcrypt.hashpw(b"changed-password", bcrypt.gensalt(4)).decode()
        with open(self.users, "w") as f:
            f.write("\n".join(lines) + "\n")
        self.assertEqual(401, self.read_with(cookie)[0])

    def test_a_cookie_made_up_or_run_out_signs_no_one_in(self):
        _, cookie, _ = self.sign_in()
        who, expires, mac = cookie.partition("=")[2].split(".")
        for bad in (f"{who}.{expires}.{'0' * 64}", f"{who}.{int(expires) + 1}.{mac}", "xrn_session=", "garbage",
                    f"{base64.urlsafe_b64encode(b'guest').decode().rstrip('=')}.{expires}.{mac}"):
            self.assertEqual(401, self.read_with(bad if bad.startswith("xrn") else f"xrn_session={bad}")[0], bad)
        body = f"{who}.{int(time.time()) - 1}"
        old = f"{body}.{self.app.login.session_mac(body, self.app.login.password_tag('alex'))}"
        self.assertEqual(401, self.read_with(f"xrn_session={old}")[0])

    def test_someone_who_cant_see_the_dashboard_gets_no_session(self):
        status, cookie, _ = self.sign_in("guest", GUEST)
        self.assertEqual((403, ""), (status, cookie))
        guest = srv.Login(self.users).make_session("guest")  # from another service on the same address
        self.assertEqual(403, self.read_with(f"xrn_session={guest}")[0])

    def test_a_sign_in_from_another_sites_page_is_refused(self):
        self.assertEqual(403, self.sign_in(headers={"Sec-Fetch-Site": "cross-site"})[0])

    def test_the_cookie_is_secure_but_where_the_browser_would_drop_it(self):
        # A hub on a LAN or tailnet address speaks plain HTTP, and a browser keeps no Secure cookie
        # from that: the sign-in never stuck. Through a proxy on the same machine it is Secure.
        def secure(addr):
            return srv.Handler.secure_cookie(type("H", (), {"client_address": (addr, 1)})())
        self.assertEqual([True, True, False, False],
                         [secure(a) for a in ("127.0.0.1", "::1", "192.168.1.20", "100.64.0.7")])


class ViewerTest(HubTest):
    viewers = ("guest",)

    def test_someone_named_with_viewer_sees_the_dashboard(self):
        self.assertEqual(200, self.call("GET", "/api/devices", auth=("guest", GUEST))[0])


class DayTest(HubTest):
    """What the dashboard draws a day from."""

    def day(self, since=T0, until=T0 + 86400, extra=""):
        return {d["device"]: d for d in self.get(f"/api/day?since={since}&until={until}{extra}")["devices"]}

    def test_each_device_has_its_day(self):
        self.post([ev("boot", T0 + 3600), ev("app", T0 + 3600, T0 + 7200, app="code"),
                   ev("web", T0 + 3600, T0 + 3700, domain="docs.example", title="Docs")])
        self.post([ev("app", T0 + 7200, T0 + 9000, app="game.exe")], token=self.pc_token, os_name="windows")
        days = self.day()
        self.assertEqual({"laptop", "pc"}, set(days))
        laptop = days["laptop"]
        self.assertEqual(3600, laptop["active"])
        self.assertEqual([["code", 3600, None]], laptop["apps"])
        self.assertEqual([["docs.example", 100, None]], laptop["sites"])
        self.assertEqual([[T0 + 3600, "boot"]], laptop["marks"])
        self.assertEqual([[T0 + 3600, T0 + 3700, "docs.example", None]], laptop["web"])
        self.assertEqual("windows", days["pc"]["os"])
        self.assertEqual(["pc"], [d["device"] for d in self.get(f"/api/day?since={T0}&until={T0 + 86400}&device=pc")["devices"]])

    def test_heartbeat_chunks_are_one_stretch_on_the_timeline(self):
        chunks = [ev("app", T0 + i * 30, T0 + (i + 1) * 30, app="code", title=f"file{i % 3}") for i in range(20)]
        chunks += [ev("idle", T0 + 600, T0 + 900), ev("app", T0 + 902, T0 + 960, app="zen")]
        self.post(chunks)
        segments = self.day()["laptop"]["segments"]
        self.assertEqual([[T0, T0 + 600, "app", "code", None], [T0 + 600, T0 + 900, "idle", "", None],
                          [T0 + 902, T0 + 960, "app", "zen", None]], segments)

    def test_page_time_while_away_isnt_on_the_page_track(self):
        self.post([ev("app", T0, T0 + 600, app="zen"), ev("web", T0, T0 + 600, domain="news.example"),
                   ev("locked", T0 + 300, T0 + 600)])
        laptop = self.day()["laptop"]
        self.assertEqual([[T0, T0 + 300, "news.example", None]], laptop["web"])
        self.assertEqual({"app": 300, "web": 300, "locked": 300}, laptop["totals"])
        self.assertEqual(300, laptop["active"])

    def test_first_and_last_are_when_someone_was_there(self):
        # A night asleep made the day start at midnight.
        self.post([ev("sleep", T0 - 3600, T0 + 8 * 3600), ev("app", T0 + 8 * 3600, T0 + 9 * 3600, app="code"),
                   ev("idle", T0 + 9 * 3600, T0 + 10 * 3600)])
        laptop = self.day()["laptop"]
        self.assertEqual((T0 + 8 * 3600, T0 + 9 * 3600), (laptop["first"], laptop["last"]))
        self.assertEqual(8 * 3600, laptop["totals"]["sleep"])

    def test_a_day_is_the_range_the_viewer_sends(self):
        # The viewer's own midnights: an evening running past one is split between the two days.
        self.post([ev("app", T0 - 1800, T0 + 1800, app="code")])
        self.assertEqual(1800, self.day(T0 - 86400, T0)["laptop"]["active"])
        after = self.day(T0, T0 + 86400)["laptop"]
        self.assertEqual((1800, T0), (after["active"], after["segments"][0][0]))

    def test_a_type_this_server_doesnt_know_is_shown_as_itself(self):
        self.post([ev("screensaver", T0, T0 + 300), ev("app", T0 + 300, T0 + 400, app="code")])
        laptop = self.day()["laptop"]
        self.assertEqual(300, laptop["totals"]["screensaver"])
        self.assertEqual(100, laptop["active"])

    def test_the_figures_can_come_without_the_timeline(self):
        self.post([ev("app", T0, T0 + 60, app="code")])
        laptop = self.day(extra="&timeline=0")["laptop"]
        self.assertNotIn("segments", laptop)
        self.assertEqual(60, laptop["active"])

    def test_the_week_is_each_day_by_device(self):
        for d in range(3):
            self.post([ev("app", T0 + d * 86400 + 3600, T0 + d * 86400 + 3600 + 600 * (d + 1), app="code")])
        self.post([ev("app", T0 + 86400 + 100, T0 + 86400 + 200, app="game.exe")], token=self.pc_token)
        bounds = ",".join(str(T0 + i * 86400) for i in range(4))
        days = self.get(f"/api/days?bounds={bounds}")["days"]
        self.assertEqual([600, 1200, 1800], [d["devices"]["laptop"]["active"] for d in days])
        self.assertEqual([{}, {"active": 100}, {}], [{k: v for k, v in d["devices"].get("pc", {}).items() if k == "active"} for d in days])
        self.assertEqual([["code", 600, None]], days[0]["devices"]["laptop"]["apps"])
        for bad in ("", "1", "a,b", f"{T0},{T0}", f"{T0 + 10},{T0}", f"{T0},{T0 + 63 * 86400}",
                    ",".join(str(T0 + i) for i in range(srv.MAX_DAYS + 2))):
            self.assertEqual(400, self.call("GET", f"/api/days?bounds={bad}")[0], bad)

    def test_a_row_opens_to_its_titles_or_pages(self):
        self.post([ev("app", T0, T0 + 600, app="code", title="a.py"), ev("app", T0 + 600, T0 + 700, app="code", title="b.py"),
                   ev("app", T0 + 700, T0 + 800, app="code", title="a.py"),
                   ev("web", T0, T0 + 120, domain="docs.example", title="Intro", url="https://docs.example/intro"),
                   ev("web", T0 + 120, T0 + 150, domain="docs.example", title="Intro", url="https://docs.example/intro?x=2")])
        self.post([ev("app", T0, T0 + 50, app="code", title="c.py")], token=self.pc_token)
        span = f"since={T0}&until={T0 + 86400}"
        got = self.get(f"/api/detail?{span}&app=code&device=laptop")
        self.assertEqual((800, [["a.py", "", 700], ["b.py", "", 100]]), (got["total"], got["rows"]))
        self.assertEqual(850, self.get(f"/api/detail?{span}&app=code")["total"])
        got = self.get(f"/api/detail?{span}&site=docs.example")
        self.assertEqual([["Intro", "https://docs.example/intro", 150]], got["rows"])
        self.assertEqual(400, self.call("GET", f"/api/detail?{span}")[0])

    def test_now_is_what_each_device_last_said(self):
        now = time.time()
        self.post([ev("app", now - 120, now - 60, app="zen"), ev("web", now - 120, now - 60, domain="news.example", title="News"),
                   ev("app", now - 60, now - 5, app="code", title="main.py")])
        self.post([ev("app", now - 900, now - 600, app="game.exe"), ev("idle", now - 600, now - 30)], token=self.pc_token)
        got = {d["device"]: d["now"] for d in self.get("/api/devices")["devices"]}
        # The browser's last page stays its newest page long after another app is in front.
        self.assertEqual(("app", "code", "main.py", None), (got["laptop"]["type"], got["laptop"]["app"], got["laptop"]["title"],
                                                             got["laptop"].get("domain")))
        self.assertEqual("idle", got["pc"]["type"])
        self.post([ev("app", now - 5, now, app="zen"), ev("web", now - 5, now, domain="mail.example", title="Inbox")])
        got = {d["device"]: d["now"] for d in self.get("/api/devices")["devices"]}
        self.assertEqual(("zen", "mail.example", "Inbox"), (got["laptop"]["app"], got["laptop"]["domain"], got["laptop"]["page"]))

    def test_a_long_poll_answers_when_an_agent_posts(self):
        seq = self.get("/api/devices")["seq"]
        self.assertEqual(seq, self.get(f"/api/changes?seq={seq}&wait=0.2")["seq"])
        got = []
        waiter = threading.Thread(target=lambda: got.append(self.get(f"/api/changes?seq={seq}&wait=20")))
        start = time.monotonic()
        waiter.start()
        time.sleep(0.3)
        self.post([])
        waiter.join(10)
        self.assertLess(time.monotonic() - start, 5)
        self.assertNotEqual(seq, got[0]["seq"])
        self.assertEqual(got[0]["seq"], self.get(f"/api/changes?seq={seq}&wait=20")["seq"])  # already moved: at once
        self.assertEqual(400, self.call("GET", "/api/changes?seq=x")[0])

    def test_a_long_poll_whose_caller_left_ends(self):
        # A proxy closes this side when the page's request times out; the thread mustn't wait on.
        seq = self.get("/api/devices")["seq"]
        real, ended = self.app.changes.wait, []
        with unittest.mock.patch.object(self.app.changes, "wait",
                                        lambda *a: ended.append(real(*a)) or ended[-1]):
            with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
                s.sendall(f"GET /api/changes?seq={seq}&wait=30 HTTP/1.1\r\nAuthorization: {basic('alex', RIGHT)}\r\n\r\n".encode())
                time.sleep(0.3)
            for _ in range(50):
                if ended:
                    break
                time.sleep(0.1)
        self.assertEqual([None], ended)  # well before its 30 s, and unanswered


class CategoryTest(HubTest):
    def put_rules(self, rules, match):
        headers = {"If-Match": match} if match else {}
        return self.call("PUT", "/api/categories", {"rules": rules}, headers)

    def test_rules_need_the_version_they_replace(self):
        status, body, headers = self.call("GET", "/api/categories")
        self.assertEqual((200, {"version": 0, "rules": []}, '"0"'), (status, json.loads(body), headers["ETag"]))
        rule = [{"kind": "app", "pattern": "code", "category": "Work"}]
        self.assertEqual(428, self.put_rules(rule, None)[0])
        status, _, headers = self.put_rules(rule, '"0"')
        self.assertEqual((200, '"1"'), (status, headers["ETag"]))
        status, _, headers = self.put_rules([], '"0"')
        self.assertEqual((412, '"1"'), (status, headers["ETag"]))
        self.assertEqual(200, self.put_rules(rule * 2, "*")[0])
        self.assertEqual(304, self.call("GET", "/api/categories", headers={"If-None-Match": '"2"'})[0])

    def test_bad_rules_are_refused(self):
        for rules in ("x", [{"kind": "window", "pattern": "a", "category": "b"}], [{"kind": "app", "pattern": "", "category": "b"}],
                      [{"kind": "app", "pattern": "a", "category": "x" * 41}], [{"kind": "app", "pattern": "a", "category": "\x00"}],
                      [{"kind": "app", "pattern": "a", "category": "b"}] * (srv.MAX_RULES + 1)):
            self.assertEqual(400, self.put_rules(rules, "*")[0], str(rules)[:60])
        self.assertEqual({"version": 0, "rules": []}, self.get("/api/categories"))

    def test_rules_put_apps_and_sites_into_categories(self):
        self.put_rules([{"kind": "site", "pattern": "youtube.com", "category": "Video"},
                        {"kind": "app", "pattern": "*.exe", "category": "Windows"},
                        {"kind": "app", "pattern": "CODE", "category": "Work"},
                        {"kind": "app", "pattern": "zen", "category": "Browsing"}], "*")
        self.post([ev("app", T0, T0 + 100, app="code"), ev("app", T0 + 100, T0 + 400, app="zen"),
                   ev("web", T0 + 100, T0 + 300, domain="m.youtube.com"), ev("web", T0 + 300, T0 + 400, domain="notyoutube.com"),
                   ev("app", T0 + 400, T0 + 450, app="game.exe"), ev("app", T0 + 450, T0 + 460, app="other")])
        laptop = self.get(f"/api/day?since={T0}&until={T0 + 86400}")["devices"][0]
        # A page's category says more than the browser's.
        self.assertEqual([["Video", 200], ["Browsing", 100], ["Work", 100], ["Windows", 50], ["", 10]], laptop["categories"])
        self.assertEqual("Video", laptop["sites"][0][2])
        self.assertEqual([[200, "Video"]], [[s[1] - s[0], s[3]] for s in laptop["web"] if s[2] == "m.youtube.com"])
        got = self.get(f"/api/detail?since={T0}&until={T0 + 86400}&category=Video")
        self.assertEqual([["m.youtube.com", "site", 200]], got["rows"])
        got = self.get(f"/api/detail?since={T0}&until={T0 + 86400}&category=")
        self.assertEqual([["other", "app", 10]], got["rows"])

    def test_damaged_rules_are_never_answered_as_none(self):
        # Read as "no rules", the page would offer to save its empty list over them.
        self.put_rules([{"kind": "app", "pattern": "code", "category": "Work"}], "*")
        path = os.path.join(self.data, "categories.json")
        with open(path, "w") as f:
            f.write('{"version": 1, "rules": [{"ki')
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            self.assertEqual(503, self.call("GET", "/api/categories")[0])
            self.assertEqual(503, self.put_rules([], "*")[0])
            self.post([ev("app", T0, T0 + 60, app="code")])
            self.assertEqual(200, self.call("GET", f"/api/day?since={T0}&until={T0 + 86400}")[0])
        self.assertIn("can't be read", log.getvalue())
        with open(path) as f:
            self.assertEqual('{"version": 1, "rules": [{"ki', f.read())


class ExportTest(HubTest):
    def test_the_range_comes_out_as_csv_in_the_viewers_zone(self):
        self.post([ev("app", T0, T0 + 60, app="code", title="=HYPERLINK(\"http://evil.example\")"),
                   ev("web", T0 + 60, T0 + 90, url="https://a.example/x", title="+1"), ev("boot", T0 - 10)])
        self.post([ev("app", T0, T0 + 30, app="game.exe")], token=self.pc_token)
        status, body, headers = self.call("GET", f"/api/export?since={T0 - 60}&until={T0 + 3600}&tz=Australia/Sydney")
        self.assertEqual(200, status)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn('attachment; filename="xrnmoni-', headers["Content-Disposition"])
        rows = list(__import__("csv").reader(io.StringIO(body.decode())))
        self.assertEqual(["device", "type", "start", "end", "seconds", "app", "title", "domain", "url", "id"], rows[0])
        self.assertEqual(5, len(rows))
        code = next(r for r in rows if r[5] == "code")
        self.assertEqual(("laptop", "app", "60.0"), (code[0], code[1], code[4]))
        if code[2].endswith("+10:00") or code[2].endswith("+11:00"):  # Sydney's zone is known here
            self.assertEqual("2026-05-29T06:26:40+10:00", code[2])
        self.assertTrue(code[6].startswith("'="), code[6])  # a spreadsheet won't run it
        self.assertEqual("'+1", next(r for r in rows if r[1] == "web")[6])
        status, body, _ = self.call("GET", f"/api/export?since={T0 - 60}&until={T0 + 3600}&device=pc")
        self.assertEqual(2, len(body.decode().strip().splitlines()))
        self.assertEqual(400, self.call("GET", f"/api/export?since=0&until={401 * 86400}")[0])

    def test_a_long_export_comes_in_pages(self):
        with unittest.mock.patch.object(srv, "EXPORT_PAGE", 3):
            self.post([ev("app", T0 + i, T0 + i + 1, app=f"a{i}") for i in range(10)])
            _, body, _ = self.call("GET", f"/api/export?since={T0 - 1}&until={T0 + 3600}")
        apps = [r.split(",")[5] for r in body.decode().strip().splitlines()[1:]]
        self.assertEqual([f"a{i}" for i in range(10)], apps)


class WebTest(HubTest):
    def page(self, path="/"):
        status, body, headers = self.call("GET", path, auth=None)
        self.assertEqual(200, status, path)
        return body.decode(), headers

    def test_the_page_works_under_any_prefix(self):
        # The page asked for /api/... from the root: behind a proxy publishing it under /xrnmoni
        # those went to whatever else the proxy serves at the root.
        for path in ("/", "/xrnmoni", "/xrnmoni/"):
            html, _ = self.page(path)
            self.assertIn('<base href="/xrnmoni/">', html)
        self.app.prefix = "/moni"
        self.app.pages.clear()
        self.assertIn('<base href="/moni/">', self.page("/moni/")[0])
        self.app.prefix = ""
        self.app.pages.clear()
        self.assertIn('<base href="/">', self.page("/")[0])

    def test_every_path_the_page_uses_is_relative(self):
        web = os.path.join(HERE, "web")
        for name in os.listdir(web):
            with open(os.path.join(web, name), encoding="utf-8") as f:
                text = f.read().replace('<base href="/xrnmoni/">', "")
            for absolute in ('"/api', "'/api", "`/api", 'fetch("/', 'href="/', 'src="/', "url(/"):
                self.assertNotIn(absolute, text, name)
        self.assertEqual(200, self.call("GET", "/xrnmoni/app.js", auth=None)[0])

    def test_the_page_runs_only_its_own_files(self):
        html, headers = self.page()
        csp = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self';", csp)
        self.assertIn("style-src 'self';", csp)
        self.assertNotIn("unsafe", csp)
        self.assertNotIn("<script>", html)
        self.assertNotIn("style=", html)

    def test_text_from_agents_never_goes_in_as_html(self):
        # Window and page titles are chosen by whatever was open; the first dashboard's innerHTML
        # let a page titled with markup run script in it.
        with open(os.path.join(HERE, "web", "app.js"), encoding="utf-8") as f:
            script = f.read()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, script)

    def test_the_page_is_revalidated_not_kept(self):
        _, headers = self.page("/app.js")
        self.assertEqual("no-cache", headers["Cache-Control"])
        status, _, _ = self.call("GET", "/app.js", auth=None, headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(304, status)

    def test_nothing_outside_the_page_is_served(self):
        for path in ("/../xrnmoni.py", "/%2e%2e/xrnmoni.py", "/xrnmoni.py", "/.hidden.js", "/test/x.js",
                     "/users", "/xrnmoni.db", "/tokens.json", "/app.js.map"):
            self.assertEqual(404, self.call("GET", path, auth=None)[0], path)


class LogFileTest(HubTest):
    """With no console the log is a file; nothing that goes wrong with it may stop the hub."""

    def logging_to_a_file(self):
        path = os.path.join(self.data, "xrnmoni.log")
        logfile = srv.LogFile(path)
        self.addCleanup(lambda: logfile.f.close())
        patch = unittest.mock.patch.object(sys, "stderr", logfile)
        patch.start()
        self.addCleanup(patch.stop)
        return path, logfile

    def test_a_log_that_cant_be_renamed_keeps_the_hub_answering(self):
        # On Windows the log can't be renamed while another program holds it open (an editor,
        # Get-Content -Wait). The file had been closed already, so every later line, and every
        # later request with it, failed with "I/O operation on closed file".
        path, _ = self.logging_to_a_file()
        real_replace = os.replace

        def replace(src, dst):
            if src == path:
                raise PermissionError(13, "The process cannot access the file", src)
            return real_replace(src, dst)
        with unittest.mock.patch.object(srv, "LOG_MAX", 200):
            with unittest.mock.patch.object(srv.os, "replace", replace):
                for _ in range(5):
                    self.assertEqual(200, self.call("GET", "/healthz", auth=None)[0])
            self.assertEqual(200, self.call("GET", "/healthz", auth=None)[0])   # it rotates once it can
        with open(path + ".1", encoding="utf-8") as f:
            self.assertEqual(6, f.read().count('"GET /healthz HTTP/1.1" 200'))

    def test_a_full_disk_doesnt_stop_the_hub(self):
        _, logfile = self.logging_to_a_file()
        with unittest.mock.patch.object(logfile.f, "write", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(200, self.call("GET", "/healthz", auth=None)[0])


class ProcessTest(unittest.TestCase):
    """The program as it's run: under pythonw on Windows there is no console at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def wait_for_health(self, proc, port):
        for _ in range(100):
            if proc.poll() is not None:
                self.fail(f"it stopped with {proc.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    return r.read()
            except OSError:
                time.sleep(0.1)
        self.fail("it never answered")

    def run_server(self, starter, **popen):
        data = os.path.join(self.tmp.name, "data")
        port = self.free_port()
        proc = subprocess.Popen([sys.executable, "-c", starter, PROGRAM, "--port", str(port), "--data", data],
                                stdin=subprocess.DEVNULL, **popen)
        try:
            self.assertEqual(b"ok", self.wait_for_health(proc, port))
            # A request is logged too: the first line written used to end it.
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5).read()
            self.assertIsNone(proc.poll())
        finally:
            proc.terminate()
            proc.wait(10)
        with open(os.path.join(data, "xrnmoni.log"), encoding="utf-8") as f:
            return f.read()

    def test_it_starts_and_logs_to_a_file_with_no_console(self):
        # As pythonw starts it: sys.stdout and sys.stderr are None. The FastAPI server died on its
        # first log line ("Unable to configure formatter 'default'"), so the PC's never came up.
        starter = ("import runpy, sys; sys.stdout = sys.stderr = None; "
                   "path = sys.argv[1]; sys.argv = sys.argv[1:]; runpy.run_path(path, run_name='__main__')")
        log = self.run_server(starter, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertIn("xrnmoni on http://127.0.0.1", log)
        self.assertIn('"GET /healthz HTTP/1.1" 200', log)

    @unittest.skipIf(sys.platform == "win32", "file descriptors are closed differently there")
    def test_it_starts_with_its_output_closed(self):
        starter = "import runpy, sys; path = sys.argv[1]; sys.argv = sys.argv[1:]; runpy.run_path(path, run_name='__main__')"

        def close_output():
            os.close(1)
            os.close(2)
        log = self.run_server(starter, preexec_fn=close_output)
        self.assertIn("xrnmoni on http://127.0.0.1", log)

    def cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, PROGRAM, *args, "--data", os.path.join(self.tmp.name, "data")],
                              input=stdin, capture_output=True, text=True, timeout=60)

    def test_users_and_tokens_are_made_from_the_command_line(self):
        import bcrypt

        r = self.cli("user", "add", "alex", stdin="a-long-password\n")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(0, self.cli("user", "add", "sam", stdin="another-password\n").returncode)
        self.assertEqual(0, self.cli("user", "add", "alex", stdin="a-new-password\n").returncode)
        with open(os.path.join(self.tmp.name, "data", "users")) as f:
            lines = f.read().splitlines()
        self.assertEqual(["alex", "sam"], [line.partition(":")[0] for line in lines])
        self.assertTrue(bcrypt.checkpw(b"a-new-password", lines[0].partition(":")[2].encode()))
        self.assertEqual("alex  (owner)\nsam\n", self.cli("user", "list").stdout)
        self.assertNotEqual(0, self.cli("user", "add", "short", stdin="abc\n").returncode)
        self.assertNotEqual(0, self.cli("user", "add", "../x", stdin="a-long-password\n").returncode)
        self.assertEqual(0, self.cli("user", "remove", "sam").returncode)
        self.assertEqual("alex  (owner)\n", self.cli("user", "list").stdout)

        r = self.cli("token", "add", "laptop")
        self.assertEqual(0, r.returncode, r.stderr)
        token = r.stdout.strip()
        self.assertRegex(token, srv.TOKEN.pattern)
        listed = self.cli("token", "list").stdout
        self.assertIn(token[8:16], listed)
        self.assertIn("laptop", listed)
        with open(os.path.join(self.tmp.name, "data", "tokens.json")) as f:
            self.assertNotIn(token[17:], f.read())
        self.assertEqual(0, self.cli("token", "revoke", token[8:16]).returncode)
        self.assertNotIn("laptop", self.cli("token", "list").stdout)


if __name__ == "__main__":
    unittest.main()
