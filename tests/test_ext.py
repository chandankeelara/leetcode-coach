"""Browser-extension ingest: token, open-handout rule, router."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from coach import db, ext, service
from coach.models import CoachMessage, Event, Problem
from coach.web import api_ext


def _events(kind="submission"):
    with db.session_scope() as s:
        return [(e.kind, e.payload) for e in s.scalars(select(Event).where(Event.kind == kind))]


def _open(world):
    """Open handout dated now (the open-handout rule compares against the wall-clock date)."""
    h = service.next_problem(world)
    with db.session_scope() as s:
        return h, s.get(Problem, h.number).lc_slug


def _client():
    app = FastAPI()
    app.include_router(api_ext.router)
    return TestClient(app)


def test_token_round_trip(world):
    t = ext.ext_token_get_or_create()
    assert len(t) >= 40 and ext.ext_token_get_or_create() == t
    t2 = ext.ext_token_rotate()
    assert t2 != t and ext.ext_token_get_or_create() == t2
    assert ext.token_ok(t2) and not ext.token_ok(t) and not ext.token_ok("")


def test_wrong_token_401(world):
    ext.ext_token_get_or_create()
    with pytest.raises(PermissionError):
        ext.ingest_submission("nope", "x", "code", "Python3", "Accepted")
    c = _client()
    r = c.post("/api/ext/submission", json={"token": "nope", "slug": "x"})
    assert r.status_code == 401 and _events() == []
    assert c.get("/api/ext/ping", params={"token": "nope"}).status_code == 401


def test_not_open_problem_pushes_nothing(world):
    tok = ext.ext_token_get_or_create()
    h, slug = _open(world)
    with db.session_scope() as s:
        other = s.scalars(select(Problem).where(Problem.number != h.number)).first().lc_slug
    r = ext.ingest_submission(tok, other, "x=1", "Python3", "Accepted")
    assert r["ok"] is False and r["reason"] == "not the open problem"
    assert ext.ingest_submission(tok, "unknown-slug", "x=1", "Python3", "Accepted")["ok"] is False
    assert _events() == []


def test_open_problem_pushes_event_and_message(world):
    tok = ext.ext_token_get_or_create()
    h, slug = _open(world)
    r = ext.ingest_submission(tok, slug, "x = [].sort()\n", "Python3", "Wrong Answer",
                              {"runtime": "5 ms"})
    assert r["ok"] and r["handout_id"] == h.handout_id and r["lint"]
    ev = _events()
    assert len(ev) == 1
    p = ev[0][1]
    assert p["source"] == "leetcode" and p["verdict"] == "Wrong Answer"
    assert p["language"] == "Python3" and p["lint"] and "code" in p
    with db.session_scope() as s:
        msgs = [m.content for m in s.scalars(select(CoachMessage).where(CoachMessage.role == "user"))]
    assert "Submitted on LeetCode: Wrong Answer" in msgs
    # numeric fallback; non-python skips lint
    r2 = ext.ingest_submission(tok, "", "int x;", "C++", "Accepted", {"number": h.number})
    assert r2["ok"] and r2["lint"] == []


def test_router_roundtrip_and_cors(world):
    tok = ext.ext_token_get_or_create()
    h, slug = _open(world)
    c = _client()
    hd = {"Origin": "https://leetcode.com"}
    r = c.get("/api/ext/ping", params={"token": tok}, headers=hd)
    assert r.json() == {"ok": True, "open_problem": h.number}
    assert r.headers["access-control-allow-origin"] == "https://leetcode.com"
    r = c.get("/api/ext/ping", params={"token": tok}, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    pre = c.options("/api/ext/submission", headers=hd)
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "https://leetcode.com"
    r = c.post("/api/ext/submission", headers=hd, json={
        "token": tok, "slug": slug, "language": "Python3", "code": "print(1)",
        "verdict": "Accepted"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(_events()) == 1
