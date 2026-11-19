"""The agent's outbox: events are kept on disk until the server has taken them."""
import os
import tempfile
import time
import unittest

from agent_helpers import FakeServer, config

import agent


def ev(title, ts=None):
    ts = time.time() if ts is None else ts
    return {"type": "app", "app": "editor", "title": title, "ts_start": ts - 5, "ts_end": ts,
            "duration": 5.0}


class SpoolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state", "spool.sqlite")

    def test_events_survive_a_restart(self):
        spool = agent.Spool(self.path)
        spool.add([ev("one"), ev("two")])
        spool.close()
        again = agent.Spool(self.path)
        self.addCleanup(again.close)
        self.assertEqual([e["title"] for _, e in again.peek(10)], ["one", "two"])

    def test_the_spool_is_capped_oldest_first(self):
        spool = agent.Spool(self.path, max_rows=3, max_days=30)
        self.addCleanup(spool.close)
        now = time.time()
        spool.add([ev("too old", now - 31 * 86400)] + [ev(f"e{i}", now) for i in range(5)])
        dropped = spool.trim(now)
        self.assertEqual(dropped, 3)
        self.assertEqual([e["title"] for _, e in spool.peek(10)], ["e2", "e3", "e4"])

    def test_default_path_follows_the_platform(self):
        self.assertEqual(agent.default_spool_path("linux", {"XDG_STATE_HOME": "/s"}, "/h"),
                         os.path.join("/s", "xrnmoni", "spool.sqlite"))
        self.assertEqual(agent.default_spool_path("linux", {}, "/h"),
                         os.path.join("/h", ".local", "state", "xrnmoni", "spool.sqlite"))
        self.assertEqual(agent.default_spool_path("win32", {"LOCALAPPDATA": r"C:\L"}, r"C:\h"),
                         os.path.join(r"C:\L", "xrnmoni", "spool.sqlite"))


class SenderTest(unittest.TestCase):
    def setUp(self):
        self.spool = agent.Spool(os.path.join(tempfile.mkdtemp(), "spool.sqlite"))
        self.addCleanup(self.spool.close)

    def sender(self, server, **kw):
        return agent.Sender(config(server_url=kw.pop("url", server.url if server else
                                                      "http://127.0.0.1:9"), **kw), self.spool)

    def test_events_are_deleted_only_after_the_server_took_them(self):
        status = [503]
        server = FakeServer(lambda body: (status[0], {"ok": status[0] == 200}))
        self.addCleanup(server.close)
        s = self.sender(server)
        s.put(ev("kept"))
        self.assertEqual(s.send_once(), "retry")
        self.assertEqual(len(self.spool), 1)
        status[0] = 200
        self.assertEqual(s.send_once(), "sent")
        self.assertEqual(len(self.spool), 0)
        self.assertEqual([e["title"] for e in server.events()], ["kept", "kept"])

    def test_one_bad_event_does_not_stall_the_rest(self):
        def reply(body):
            if any(e["title"] == "bad" for e in body["events"]):
                return 422, {"detail": "bad event"}
            return 200, {"ok": True}
        server = FakeServer(reply)
        self.addCleanup(server.close)
        s = self.sender(server)
        for t in ("a", "bad", "b"):
            s.put(ev(t))
        self.assertEqual(s.send_once(), "sent")
        self.assertEqual(len(self.spool), 0)
        accepted = [r[2]["events"] for r in server.requests if r[2]["events"][0]["title"] != "bad"]
        self.assertEqual([e["title"] for batch in accepted[-2:] for e in batch], ["a", "b"])
        s.put(ev("later"))
        self.assertEqual(s.send_once(), "sent")
        self.assertEqual(server.events()[-1]["title"], "later")

    def test_events_the_server_rejects_inside_a_200_are_dropped(self):
        server = FakeServer(lambda body: (200, {"ok": True, "inserted": 1, "rejected": [
            {"index": 1, "id": None, "error": "ts_start missing"}]}))
        self.addCleanup(server.close)
        s = self.sender(server)
        s.put(ev("a"))
        s.put(ev("b"))
        self.assertEqual(s.send_once(), "sent")
        self.assertEqual(len(self.spool), 0)

    def test_auth_and_server_errors_keep_events(self):
        for status in (401, 403, 404, 429, 500, 502):
            with self.subTest(status=status):
                server = FakeServer(lambda body, status=status: (status, {"detail": "no"}))
                self.addCleanup(server.close)
                s = self.sender(server)
                s.put(ev("kept"))
                self.assertEqual(s.send_once(), "retry")
                self.assertGreaterEqual(len(self.spool), 1)

    def test_a_2xx_that_is_not_the_server_keeps_events(self):
        server = FakeServer(lambda body: (200, b"<html>captive portal</html>"))
        self.addCleanup(server.close)
        s = self.sender(server)
        s.put(ev("kept"))
        self.assertEqual(s.send_once(), "retry")
        self.assertEqual(len(self.spool), 1)

    def test_network_errors_keep_events_and_back_off(self):
        s = self.sender(None, url="http://127.0.0.1:9", flush_interval=10.0)
        s.put(ev("kept"))
        self.assertEqual(s.send_once(), "retry")
        self.assertEqual(len(self.spool), 1)
        delays = [s.next_delay(n) for n in range(1, 8)]
        self.assertEqual(delays[0], 10.0)
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[-1], 300.0)

    def test_a_server_url_with_a_path_prefix_keeps_its_prefix(self):
        server = FakeServer()
        self.addCleanup(server.close)
        s = self.sender(server, url=server.url + "/xrnmoni/")
        s.put(ev("a"))
        self.assertEqual(s.send_once(), "sent")
        path, headers, body = server.requests[0]
        self.assertEqual(path, "/xrnmoni/api/events")
        self.assertEqual(headers["Authorization"], "Bearer " + "t" * 20)
        self.assertEqual((body["device"], body["os"]), ("test-pc", agent.OS_NAME))


if __name__ == "__main__":
    unittest.main()
