"""Live page signal: every coach-side write appends a ui_events row, and /stream sends them
as server-sent events."""
import json
import re
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from coach import db, service
from coach.models import Handout, UiEvent
from coach.web import app as webapp
from coach.web.svc import Svc


def _rows():
    with db.session_scope() as s:
        return [(e.user_id, e.kind, e.ref_id) for e in s.query(UiEvent).order_by(UiEvent.id)]


def _new_kinds(before):
    return [r for r in _rows()[before:]]


@pytest.fixture
def opened(world):
    h = service.next_problem(world)
    assert h is not None
    return world, h.handout_id


WRITES = {
    "post_message": (lambda u, h: service.post_message(u, "coach", "hi", handout_id=h),
                     "message"),
    "set_statement": (lambda u, h: service.set_statement(u, h, "Return the count of items."),
                      "statement"),
    "show_visual": (lambda u, h: service.show_visual(u, h, "array", {"values": [1, 2]}),
                    "visual"),
    "clear_visuals": (lambda u, h: service.clear_visuals(u, h), "visual"),
    "unlock_editor": (lambda u, h: service.unlock_editor(u, h), "editor"),
    "lock_editor": (lambda u, h: service.lock_editor(u, h), "editor"),
    "next_problem_force_new": (lambda u, h: service.next_problem(u, force_new=True), "handout"),
    "skip_problem": (lambda u, h: service.skip_problem(h), "handout"),
    "log_attempt": (lambda u, h: service.log_attempt(u, handout_id=h, actual_min=5), "handout"),
    "reset_timer": (lambda u, h: service.reset_timer(u, h), "handout"),
}


@pytest.mark.parametrize("name", sorted(WRITES))
def test_each_write_emits(opened, name):
    uid, hid = opened
    fn, kind = WRITES[name]
    before = len(_rows())
    fn(uid, hid)
    got = _new_kinds(before)
    assert any(u == uid and k == kind for u, k, _ in got), (name, got)


def test_new_handout_emits_handout(world):
    before = len(_rows())
    h = service.next_problem(world)
    assert (world, "handout", h.handout_id) in _new_kinds(before)
    before = len(_rows())
    service.next_problem(world)                 # same open one again: nothing changed
    assert _new_kinds(before) == []


def test_coach_seen_presence(world):
    from datetime import timezone
    now = datetime.now(timezone.utc)
    before = len(_rows())
    service.coach_seen(world, now)
    assert _new_kinds(before) == [(world, "presence", None)]
    service.coach_seen(world, now + timedelta(seconds=1))    # inside the 5 s throttle
    assert len(_new_kinds(before)) == 1
    service.coach_seen(world, now + timedelta(seconds=6))
    assert len(_new_kinds(before)) == 2


def test_reset_timer_moves_handed_at(opened):
    uid, hid = opened
    with db.session_scope() as s:
        s.get(Handout, hid).handed_at = datetime.now() - timedelta(minutes=40)
    r = service.reset_timer(uid, hid)
    with db.session_scope() as s:
        at = s.get(Handout, hid).handed_at
    assert (datetime.now() - at).total_seconds() < 5 and r["handout_id"] == hid
    service.log_attempt(uid, handout_id=hid, actual_min=3)
    with pytest.raises(ValueError):
        service.reset_timer(uid, hid)


def test_ui_events_pruned_after_an_hour(world):
    service.emit(world, "message")
    with db.session_scope() as s:
        for e in s.query(UiEvent):
            e.created_at = datetime.now() - timedelta(hours=2)
    last = service.ui_last_id()
    service.emit(world, "message")
    rows = service.ui_events_since(world, 0)
    assert len(rows) == 1 and rows[0]["id"] > last        # ids never reused


def test_ui_events_since_scoped_to_user(world):
    other = service.create_user("other", "pw")
    service.emit(other, "message")
    service.emit(None, "presence")
    mine = service.emit(world, "statement", 7)
    kinds = [(r["kind"], r["ref_id"]) for r in service.ui_events_since(world, 0)]
    assert kinds == [("presence", None), ("statement", 7)]
    assert service.ui_events_since(world, mine) == []


def test_emit_rejects_unknown_kind(world):
    with pytest.raises(ValueError):
        service.emit(world, "nope")


# ---- web: /stream and /handout/state ---------------------------------------------------------
@pytest.fixture
def web(world, monkeypatch):
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    webapp.app.state.service = Svc()
    c = TestClient(webapp.app, follow_redirects=False)
    page = c.get("/login").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert c.post("/login", data={"username": "tester", "password": "pw-for-tests",
                                  "csrf": tok}).status_code == 303
    yield c, world
    webapp.app.state.service = None


def _events(text):
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        ev = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[6:])
            elif line.startswith("id: "):
                ev["id"] = int(line[4:])
        if "event" in ev:
            out.append(ev)
    return out


def test_stream_requires_login(env_logged_out):
    r = env_logged_out.get("/stream")
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.fixture
def env_logged_out(world, monkeypatch):
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    webapp.app.state.service = Svc()
    yield TestClient(webapp.app, follow_redirects=False)
    webapp.app.state.service = None


def test_stream_hello_then_live_message(web):
    c, uid = web
    h = service.next_problem(uid)
    service.unlock_editor(uid, h.handout_id)
    posted = {}

    def later():
        time.sleep(0.6)
        posted["id"] = service.post_message(uid, "coach", "ping from the coach")

    t = threading.Thread(target=later)
    t.start()
    with c.stream("GET", "/stream?max_events=2&max_s=10") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    t.join()
    evs = _events(body)
    assert [e["event"] for e in evs] == ["hello", "message"]
    hello = evs[0]["data"]
    assert hello["open_handout_id"] == h.handout_id and hello["editor_open"] is True
    assert hello["last_message_id"] == service.last_message_id(uid) - 1
    assert isinstance(hello["online"], bool)
    assert evs[1]["data"] == {"ref_id": posted["id"]} and evs[1]["id"] > evs[0]["id"]


def test_stream_replays_since_and_last_event_id(web):
    c, uid = web
    since = service.ui_last_id()
    m = service.post_message(uid, "coach", "one")
    service.emit(uid, "statement", 3)
    body = c.get(f"/stream?since={since}&max_events=3&max_s=5").text
    evs = _events(body)
    assert [e["event"] for e in evs] == ["hello", "message", "statement"]
    assert evs[0]["id"] == since and evs[1]["data"]["ref_id"] == m
    # reconnect with Last-Event-ID: only what came after
    body = c.get("/stream?max_events=2&max_s=5",
                 headers={"Last-Event-ID": str(evs[1]["id"])}).text
    assert [e["event"] for e in _events(body)] == ["hello", "statement"]


def test_stream_presence_flip(web):
    from datetime import timezone
    c, uid = web
    since = service.ui_last_id()
    service.coach_seen(uid, datetime.now(timezone.utc))      # offline -> online
    service.coach_seen(uid, datetime.now(timezone.utc) + timedelta(seconds=6))   # no flip
    body = c.get(f"/stream?since={since}&max_events=2&max_s=1").text
    evs = _events(body)
    assert evs[0]["event"] == "hello"
    # hello already says online (heartbeat is fresh), so neither presence row is a flip
    assert evs[0]["data"]["online"] is True
    assert [e["event"] for e in evs[1:]] == []


def test_handout_state(web):
    c, uid = web
    assert c.get("/handout/state").json() == {
        "open_handout_id": None, "editor_open": False, "started_at": None, "target_min": None,
        "elapsed_s": None, "clock": None}
    h = service.next_problem(uid)
    st = c.get("/handout/state").json()
    assert st["open_handout_id"] == h.handout_id and st["editor_open"] is False
    assert st["target_min"] == h.target_min and st["elapsed_s"] >= 0
    service.unlock_editor(uid, h.handout_id)
    with db.session_scope() as s:
        row = s.get(Handout, h.handout_id)
        row.handed_at = row.clock_anchor = datetime.now() - timedelta(minutes=30)
    assert c.get("/handout/state").json()["elapsed_s"] >= 1799
    service.reset_timer(uid, h.handout_id)
    st2 = c.get("/handout/state").json()
    assert st2["editor_open"] is True and st2["elapsed_s"] < 5
    assert st2["started_at"] != st["started_at"]


def test_solve_page_loads_live_js(web):
    c, uid = web
    service.next_problem(uid)
    html = c.get("/solve").text
    assert "/static/live.js" in html
    js = c.get("/static/live.js").text
    assert "EventSource" in js and "/stream" in js


def test_no_fast_polling_left():
    from pathlib import Path
    static = Path(webapp.__file__).parent / "static"
    solve = (static / "solve.js").read_text(encoding="utf-8")
    banner = (Path(webapp.__file__).parent / "templates" / "_coach_banner.html").read_text(
        encoding="utf-8")
    for src in (solve, banner):
        assert not re.search(r"setTimeout\(\s*(poll|tick|pollStatement)\b", src)
    assert "window.fetch =" not in banner


def test_bridge_reset_timer_tool(opened):
    import asyncio
    from bridge import server
    uid, hid = opened
    server.set_service(service, user_id=uid)
    try:
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        assert tools["reset_timer"].description.strip() == \
            "Restart the clock on the open problem from now."
        with db.session_scope() as s:
            s.get(Handout, hid).handed_at = datetime.now() - timedelta(minutes=20)
        before = len(_rows())
        r = server.reset_timer()
        assert r["ok"] is True and r["handout_id"] == hid
        assert (uid, "handout", hid) in _new_kinds(before)
        service.log_attempt(uid, handout_id=hid, actual_min=1)
        assert server.reset_timer() == {"error": "no open problem"}
    finally:
        server.set_service(None)
