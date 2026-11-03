"""Security checks for the server, the dashboard page and the agent's browser bridge.

    pip install -r server/requirements.txt -r agent/requirements.txt httpx
    python -m unittest discover tests
"""
import http.client
import importlib
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
AGENT = os.path.join(ROOT, "agent")


class ServerTokenTest(unittest.TestCase):
    def start(self, token):
        env = dict(os.environ)
        env.pop("XRNMONI_TOKEN", None)
        if token is not None:
            env["XRNMONI_TOKEN"] = token
        return subprocess.run([sys.executable, "-c", "import main"], cwd=SERVER, env=env,
                              capture_output=True, text=True)

    def test_refuses_to_start_without_a_real_token(self):
        for token in (None, "", "change-me"):
            with self.subTest(token=token):
                r = self.start(token)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("XRNMONI_TOKEN", r.stderr)

    def test_starts_with_a_token(self):
        self.assertEqual(self.start("a-long-random-test-token-123").returncode, 0)


class ServerApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["XRNMONI_TOKEN"] = "a-long-random-test-token-123"
        os.environ["XRNMONI_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
        sys.path.insert(0, SERVER)
        from fastapi.testclient import TestClient
        cls.main = importlib.import_module("main")
        cls.client = TestClient(cls.main.app)

    def test_api_needs_the_exact_token(self):
        self.assertEqual(self.client.get("/api/devices").status_code, 401)
        bad = {"Authorization": "Bearer a-long-random-test-token-12"}
        self.assertEqual(self.client.get("/api/devices", headers=bad).status_code, 401)
        good = {"Authorization": "Bearer a-long-random-test-token-123"}
        self.assertEqual(self.client.get("/api/devices", headers=good).status_code, 200)

    def test_dashboard_csp_allows_only_its_own_script(self):
        r = self.client.get("/")
        csp = r.headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("script-src 'sha256-", csp)
        self.assertNotIn("unsafe-inline' ;", csp.split("script-src")[1].split(";")[0] + " ;")

    def test_dashboard_escapes_data_before_using_innerHTML(self):
        html = self.client.get("/").text
        self.assertIn("const esc = ", html)
        for raw in ("${dev.device}", "${dev.os", "${e.title", "${i.name", "${e.type}</span>"):
            self.assertNotIn(raw, html)


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
