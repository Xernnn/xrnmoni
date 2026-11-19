"""The extension folder loads in Chromium and in Firefox/Zen, and packages to a zip and an
unsigned .xpi."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

from agent_helpers import ROOT

EXT = os.path.join(ROOT, "extension")
PACKAGE = os.path.join(ROOT, "scripts", "package-extension.py")


def manifest():
    with open(os.path.join(EXT, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


class ManifestTest(unittest.TestCase):
    def test_one_manifest_for_both_browsers(self):
        m = manifest()
        self.assertEqual(m["manifest_version"], 3)
        bg = m["background"]
        self.assertEqual(bg["service_worker"], "background.js")   # Chromium
        self.assertEqual(bg["scripts"], ["background.js"])         # Firefox / Zen event page
        gecko = m["browser_specific_settings"]["gecko"]
        self.assertRegex(gecko["id"], r"^xrnmoni@")                # a fixed add-on id
        self.assertGreaterEqual(int(gecko["strict_min_version"].split(".")[0]), 128)
        self.assertEqual(gecko["data_collection_permissions"], {"required": ["none"]})
        self.assertNotIn("update_url", m)

    def test_every_file_the_manifest_names_exists(self):
        m = manifest()
        names = [m["background"]["service_worker"], *m["background"]["scripts"],
                 m["options_ui"]["page"], *m["icons"].values()]
        for name in names:
            self.assertTrue(os.path.isfile(os.path.join(EXT, name)), name)
        with open(os.path.join(EXT, m["options_ui"]["page"]), encoding="utf-8") as f:
            for src in re.findall(r'<script src="([^"]+)"', f.read()):
                self.assertTrue(os.path.isfile(os.path.join(EXT, src)), src)

    def test_permissions_stay_small(self):
        m = manifest()
        self.assertEqual(sorted(m["permissions"]), ["alarms", "idle", "storage", "tabs"])
        self.assertEqual(m["host_permissions"], ["http://127.0.0.1/*"])


class PackageTest(unittest.TestCase):
    def package(self, out):
        r = subprocess.run([sys.executable, PACKAGE, "--out", out], capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_it_builds_a_zip_and_an_xpi_with_the_manifest_at_the_root(self):
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.package(a)
        self.package(b)
        version = manifest()["version"]
        zip_path = os.path.join(a, f"xrnmoni-extension-{version}.zip")
        xpi_path = os.path.join(a, f"xrnmoni-extension-{version}.xpi")
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn("background.js", names)
            self.assertIn("icons/icon128.png", names)
            self.assertFalse([n for n in names if n.startswith(".") or "/." in n])
            self.assertEqual(json.loads(z.read("manifest.json")), manifest())
        with open(zip_path, "rb") as f1, open(xpi_path, "rb") as f2:
            self.assertEqual(f1.read(), f2.read())
        with open(zip_path, "rb") as f1, open(os.path.join(b, os.path.basename(zip_path)),
                                              "rb") as f2:
            self.assertEqual(f1.read(), f2.read())  # the same bytes on every run


if __name__ == "__main__":
    unittest.main()
