"""xrnmoni server — ingest API + query API + dashboard.

Run:
    pip install -r requirements.txt
    XRNMONI_TOKEN=change-me uvicorn main:app --host 0.0.0.0 --port 8000

Agents and the dashboard all talk to this one process.
"""
import os
import time

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional

import db

TOKEN = os.environ.get("XRNMONI_TOKEN", "change-me")
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
    expected = f"Bearer {TOKEN}"
    if not authorization or authorization != expected:
        raise HTTPException(status_code=401, detail="bad or missing token")


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
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True})
