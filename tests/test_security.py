"""Security checks for the agent's browser bridge. The server's are in server/test_xrnmoni.py.

    pip install -r agent/requirements.txt
    python -m unittest discover tests
"""
import http.client
import importlib
import os
import queue
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "agent")


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, AGENT)
        agent = importlib.import_module("agent")

        class Cfg:
            bridge_port = 0

        class Sender:
            def __init__(self):
                self.q = queue.Queue()

            def put(self, item):
                self.q.put(item)

        cls.sender = Sender()
        cls.bridge = agent.BrowserBridge(Cfg(), cls.sender)
        cls.port = cls.bridge.server_address[1]
        threading.Thread(target=cls.bridge.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.shutdown()

    def post(self, origin, body=b'{"url":"https://example.com","title":"t"}'):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        c.request("POST", "/browser", body, headers)
        r = c.getresponse()
        r.read()
        return r.status, r.getheader("Access-Control-Allow-Origin")

    def test_websites_cannot_post(self):
        for origin in ("https://evil.example", "null", "http://127.0.0.1:8000", None):
            with self.subTest(origin=origin):
                self.assertEqual(self.post(origin)[0], 403)
        self.assertTrue(self.sender.q.empty())

    def test_websites_get_no_preflight(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("OPTIONS", "/browser", headers={"Origin": "https://evil.example",
                                                  "Access-Control-Request-Method": "POST"})
        self.assertEqual(c.getresponse().status, 403)

    def test_the_extension_can_post(self):
        origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
        status, allow = self.post(origin)
        self.assertEqual((status, allow), (200, origin))
        self.assertEqual(self.sender.q.get(timeout=2)["url"], "https://example.com")

    def test_oversized_bodies_are_refused(self):
        big = b'{"title":"' + b"x" * (300 * 1024) + b'"}'
        self.assertEqual(self.post("chrome-extension://abc", big)[0], 413)


if __name__ == "__main__":
    unittest.main()
