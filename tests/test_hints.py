"""Hint cards: hidden hint, reveal counts, dismiss does not, one unrevealed at a time."""
import re

import pytest
from fastapi.testclient import TestClient

from coach import service, tools
from coach.models import Card
from coach.web import app as webapp
from coach.web.svc import Svc

SECRET = "mirror the second half onto the first"


@pytest.fixture
def opened(world):
    h = service.next_problem(world)
    return world, h.handout_id


@pytest.fixture
def web(opened, monkeypatch):
    uid, hid = opened
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    webapp.app.state.service = Svc()
    c = TestClient(webapp.app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    assert c.post("/login", data={"username": "tester", "password": "pw-for-tests",
                                  "csrf": tok}).status_code == 303
    csrf = re.search(r'X-CSRF-Token": "([^"]+)"', c.get("/solve").text).group(1)
    yield c, uid, hid, {"X-CSRF-Token": csrf}
    webapp.app.state.service = None


def _thread(uid):
    return service.list_messages(uid)[-1]


def test_card_posts_without_the_hint_text(opened):
    uid, hid = opened
    o = service.offer_hint(uid, hid, SECRET, "loop but no return")
    m = _thread(uid)
    assert m["content"] == "Need a hint?" and m["card"]["kind"] == "hint"
    html = m["card"]["html"]
    assert "Need a hint?" in html and "loop but no return" in html and "Show hint" in html
    assert SECRET not in html and "mirror" not in html
    assert o["hints_revealed"] == 0 and o["level"] == 1


def test_reveal_returns_text_pushes_event_and_counts(web):
    c, uid, hid, hdr = web
    o = service.offer_hint(uid, hid, SECRET, "edge case failing", level=2)
    service._drain(uid)
    assert c.post(f"/hint/{o['id']}/reveal").status_code == 403           # csrf
    r = c.post(f"/hint/{o['id']}/reveal", headers=hdr)
    d = r.json()
    assert r.status_code == 200 and d["hint"] == SECRET and SECRET in d["html"]
    assert d["hints_revealed"] == 1 and d["level"] == 2
    ev = service._drain(uid)
    assert [e["kind"] for e in ev] == ["hint_revealed"]
    assert ev[0]["payload"] == {"id": o["id"], "handout_id": hid, "stage": "edge case failing",
                                "level": 2, "hints_revealed": 1}
    c.post(f"/hint/{o['id']}/reveal", headers=hdr)                        # again: not counted twice
    assert service._drain(uid) == []
    sm = service.session_summary(uid)
    assert [p["hints_revealed"] for p in sm["problems"] if p["handout_id"] == hid] == [1]
    assert SECRET in c.get("/messages?since=0").text                      # revealed stays revealed


def test_dismiss_is_not_counted(web):
    c, uid, hid, hdr = web
    o = service.offer_hint(uid, hid, SECRET, "no loop yet")
    service._drain(uid)
    r = c.post(f"/hint/{o['id']}/dismiss", headers=hdr)
    assert r.status_code == 200 and "dismissed" in r.json()["html"]
    ev = service._drain(uid)
    assert [e["kind"] for e in ev] == ["hint_dismissed"]
    assert service.session_summary(uid)["problems"][0]["hints_revealed"] == 0
    assert c.post(f"/hint/{o['id']}/reveal", headers=hdr).status_code == 400
    assert SECRET not in c.get("/messages?since=0").text


def test_one_unrevealed_at_a_time(opened):
    uid, hid = opened
    a = service.offer_hint(uid, hid, "first nudge", "no loop yet")
    b = service.offer_hint(uid, hid, "second nudge", "loop but no return")
    assert b["replaced"] == a["id"]
    from coach.db import session_scope
    with session_scope() as s:
        cards = [c for c in s.query(Card).filter_by(kind="hint")]
        assert [c.id for c in cards] == [b["id"]]
    assert sum(1 for m in service.list_messages(uid) if m["card"]) == 1
    service.reveal_hint(uid, b["id"])
    n = service.offer_hint(uid, hid, "third nudge", "edge case failing", level=2)   # kept: revealed
    assert n["replaced"] is None
    assert sum(1 for m in service.list_messages(uid) if m["card"]) == 2
    assert n["hints_revealed"] == 1


def test_leak_guard_and_limits(opened):
    uid, hid = opened
    with pytest.raises(ValueError, match="leak"):
        service.offer_hint(uid, hid, "use a heap here", "no loop yet")
    with pytest.raises(ValueError, match="leak"):
        service.offer_hint(uid, hid, "fine", "binary search stage")
    with pytest.raises(ValueError, match="400"):
        service.offer_hint(uid, hid, "x" * 401, "no loop yet")
    with pytest.raises(ValueError, match="3 lines"):
        service.offer_hint(uid, hid, "try:\n```\na\nb\nc\nd\n```", "no loop yet")
    with pytest.raises(ValueError, match="level"):
        service.offer_hint(uid, hid, "ok", "no loop yet", level=4)
    assert not [m for m in service.list_messages(uid) if m["card"]]


def test_registry_and_event_docs():
    t = tools.TOOLS["offer_hint"]
    assert [p.name for p in t.params] == ["hint", "stage", "level"]
    assert "hint_revealed" in tools.TOOLS["wait_for_event"].description
    assert "hint_dismissed" in tools.TOOLS["wait_for_event"].description
    assert "Need a hint?" in t.description


def test_rule_zero_words_on_solve_with_a_hint_card(web):
    c, uid, hid, hdr = web
    service.offer_hint(uid, hid, SECRET, "loop but no return")
    blob = (c.get("/solve").text + c.get("/messages?since=0").text
            + c.get("/static/play.js").text + c.get("/static/play.css").text).lower()
    assert SECRET not in blob
    for w in ("two pointer", "sliding window", "heap", "binary search", "dummy node"):
        assert w not in blob
