"""xrnmoni server — ingest API + query API + dashboard.

Run:
    pip install -r requirements.txt
    XRNMONI_TOKEN=<a long random token> uvicorn main:app --host <this machine's Tailscale IP> --port 8000

The server refuses to start without XRNMONI_TOKEN (or with the example's "change-me").
It speaks plain HTTP, so serve it over Tailscale (which encrypts) or a firewall rule that
admits only Tailscale, as windows/install.ps1 sets up, never on an open network.

Agents and the dashboard all talk to this one process.
"""
import base64
import hashlib
import hmac
import os
import re
import sys
import time

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional

import db

TOKEN = os.environ.get("XRNMONI_TOKEN", "").strip()
if not TOKEN or TOKEN == "change-me":
    sys.exit("xrnmoni: set XRNMONI_TOKEN to a long random value (not empty, not \"change-me\")")
if len(TOKEN) < 16:
    print("xrnmoni: warning: XRNMONI_TOKEN is short; use 16+ random characters", file=sys.stderr)
EXPECTED_AUTH = f"Bearer {TOKEN}".encode()
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="xrnmoni")


class Event(BaseModel):
    type: str = "app"
    app: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    domain: Optional[str] = None
    ts_start: Optional[float] = None
    ts_end: Optional[float] = None
    duration: Optional[float] = None


class Batch(BaseModel):
    device: str
    os: Optional[str] = None
    events: List[Event]


def _auth(authorization: Optional[str]):
    # compare_digest: the time taken doesn't reveal how much of a guess was right
    if not authorization or not hmac.compare_digest(authorization.encode(), EXPECTED_AUTH):
        raise HTTPException(status_code=401, detail="bad or missing token")


def _dashboard_page():
    """The dashboard, with a CSP that runs only its own inline script (by hash) and lets it
    talk only to this server, so even a title that slipped past escaping couldn't run code
    or send the token anywhere."""
    with open(os.path.join(STATIC_DIR, "dashboard.html"), encoding="utf-8") as f:
        html = f.read()
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(m.encode()).digest()).decode() + "'"
        for m in re.findall(r"<script>(.*?)</script>", html, re.S))
    csp = ("default-src 'none'; script-src " + hashes + "; style-src 'unsafe-inline'; "
           "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
           "frame-ancestors 'none'")
    return html, {"Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff",
                  "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}


DASHBOARD_HTML, DASHBOARD_HEADERS = _dashboard_page()


@app.post("/api/events")
def ingest(batch: Batch, authorization: Optional[str] = Header(default=None)):
    _auth(authorization)
    n = db.insert_events(
        batch.device, batch.os, [e.model_dump() for e in batch.events]
    )
    return {"ok": True, "inserted": n}


@app.get("/api/devices")
def devices(authorization: Optional[str] = Header(default=None)):
    _auth(authorization)
    return {"devices": db.list_devices(), "now": time.time()}


@app.get("/api/summary")
def summary(
    device: str,
    since: float = Query(default=None),
    until: float = Query(default=None),
    authorization: Optional[str] = Header(default=None),
):
    _auth(authorization)
    now = time.time()
    since = since if since is not None else now - 86400
    until = until if until is not None else now
    return db.summary(device, since, until)


@app.get("/api/activity")
def activity(
    device: str,
    since: float = Query(default=None),
    until: float = Query(default=None),
    authorization: Optional[str] = Header(default=None),
):
    _auth(authorization)
    now = time.time()
    since = since if since is not None else now - 86400
    until = until if until is not None else now
    return {"events": db.activity(device, since, until)}


@app.get("/")
def dashboard():
    return HTMLResponse(DASHBOARD_HTML, headers=DASHBOARD_HEADERS)


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True})
