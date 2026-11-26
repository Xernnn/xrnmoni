#!/usr/bin/env python3
"""Package the Linux agent as dist/xrnmoni-linux-<version>.tar.gz: what linux/install.sh needs on a
Linux machine with no checkout (the agent, its installer and unit, the extension to load unpacked,
the README and the licence), under one folder, xrnmoni-linux-<version>/.

Unpack it where it can stay (the service runs from there) and run linux/install.sh in it. The bytes
are the same on every run of the same files (sorted names, fixed times and owners).

    python scripts/bundle-linux.py [--version X.Y.Z] [--out DIR]
"""
import argparse
import gzip
import io
import json
import os
import re
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = [
    "LICENSE",
    "README.md",
    "agent/agent.py",
    "agent/platform_linux.py",
    "agent/requirements.txt",
    "agent/config.example.toml",
    "linux/install.sh",
    "linux/xrnmoni-agent.service",
]
EXECUTABLE = {"linux/install.sh"}
VERSION = re.compile(r"[0-9][0-9A-Za-z.+-]{0,40}")


def extension_files():
    ext = os.path.join(ROOT, "extension")
    for folder, dirs, names in os.walk(ext):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            if not name.startswith("."):
                yield os.path.relpath(os.path.join(folder, name), ROOT).replace(os.sep, "/")


def manifest_version():
    with open(os.path.join(ROOT, "extension", "manifest.json"), encoding="utf-8") as f:
        return json.load(f)["version"]


def build(out, version=None):
    version = version or manifest_version()
    if not VERSION.fullmatch(version):
        raise SystemExit(f"not a version: {version!r}")
    top = f"xrnmoni-linux-{version}"
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, top + ".tar.gz")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted([*FILES, *extension_files()]):
            with open(os.path.join(ROOT, name), "rb") as f:
                data = f.read()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mtime = 315532800  # 1980-01-01, as the extension's zip
            info.mode = 0o755 if name in EXECUTABLE else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    with open(path, "wb") as f, gzip.GzipFile(filename="", fileobj=f, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", help="default: the extension's manifest version")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    args = ap.parse_args()
    print(build(args.out, args.version))


if __name__ == "__main__":
    main()
