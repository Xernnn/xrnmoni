#!/usr/bin/env python3
"""Package extension/ as dist/xrnmoni-extension-<version>.zip and an identical .xpi.

The zip is for Chromium-based browsers and the Chrome Web Store; the .xpi is the
same file under the name Firefox and Zen install. It is unsigned: Zen (or Firefox
Developer Edition / Nightly / ESR) installs it with xpinstall.signatures.required
set to false in about:config. The bytes are the same on every run (sorted names,
fixed timestamps), so a rebuild of an unchanged folder is an unchanged file.

    python scripts/package-extension.py [--out DIR]
"""
import argparse
import json
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")


def files():
    for folder, dirs, names in os.walk(EXT):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            if not name.startswith("."):
                path = os.path.join(folder, name)
                yield path, os.path.relpath(path, EXT).replace(os.sep, "/")


def build(out):
    with open(os.path.join(EXT, "manifest.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]
    os.makedirs(out, exist_ok=True)
    zip_path = os.path.join(out, f"xrnmoni-extension-{version}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in sorted(files(), key=lambda f: f[1]):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as f:
                z.writestr(info, f.read())
    xpi_path = zip_path[:-4] + ".xpi"
    with open(zip_path, "rb") as src, open(xpi_path, "wb") as dst:
        dst.write(src.read())
    return zip_path, xpi_path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    for path in build(ap.parse_args().out):
        print(path)


if __name__ == "__main__":
    main()
