"""The agent's config is checked when it starts, not when the server first refuses it."""
import os
import tempfile
import unittest

from agent_helpers import config  # noqa: F401  (puts agent/ on sys.path)

import agent


def write(text):
    path = os.path.join(tempfile.mkdtemp(), "config.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write('server_url = "http://127.0.0.1:9"\ntoken = "' + "t" * 20 + '"\n' + text)
    return path


class ConfigTest(unittest.TestCase):
    def test_a_device_name_the_server_would_refuse_stops_the_agent(self):
        # the server answers 400 to the whole batch for such a name; retried one by one
        # that would drop every event, so refuse to start instead
        for name in ("x" * 65, "tab\\there", "new\\nline"):
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as cm:
                    agent.Config(write(f'device = "{name}"\n'))
                self.assertIn("device", str(cm.exception))

    def test_a_normal_device_name(self):
        self.assertEqual(agent.Config(write('device = "Alex\\u2019s laptop"\n')).device,
                         "Alex\u2019s laptop")
