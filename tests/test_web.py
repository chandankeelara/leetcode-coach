import re

import pytest
from fastapi.testclient import TestClient

from coach.rules import TIER1, TIER2
from coach.web import app as webapp
from tests.fake_service import FakeService

FORBIDDEN = [p for p in TIER1 + TIER2] + [p.replace("-", " ") for p in TIER1 + TIER2] + [
    "slot", "tier", "backlog", "streak", "easy", "medium", "hard"]


def visible_text(html):
    html = re.sub(r"(?s)<(script|style).*?</\1>", "", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return html.lower()


def assert_clean(html):
    text = visible_text(html)
    for w in FORBIDDEN:
        assert not re.search(rf"\b{re.escape(w.lower())}\b", text), w


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    fake = FakeService()
    webapp.app.state.service = fake
    c = TestClient(webapp.app, follow_redirects=False)
    yield c, fake
    webapp.app.state.service = None


def login(c):
    page = c.get("/login").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/login", data={"username": "me", "password": "pw", "csrf": tok})
    assert r.status_code == 303
    return c


def csrf(c):
    return re.search(r'X-CSRF-Token": "([^"]+)"', c.get("/").text).group(1)


def start(c):
    t = csrf(c)
    c.post("/solve", data={"csrf": t})
    return t


def test_login_required(env):
    c, _ = env
    for path in ["/", "/solve", "/log", "/dashboard", "/messages", "/reveal/1"]:
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert c.post("/solve", data={}).headers["location"] == "/login"


def test_bad_login(env):
    c, _ = env
    page = c.get("/login").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/login", data={"username": "me", "password": "no", "csrf": tok})
    assert r.status_code == 200 and "Wrong" in r.text


def test_csrf_enforced(env):
    c, _ = env
    login(c)
    assert c.post("/solve", data={"csrf": "nope"}).status_code == 403


def test_interstitial_neutral(env):
    c, _ = env
    login(c)
    r = c.get("/")
    assert r.status_code == 200 and "Next problem" in r.text
    assert_clean(r.text)


def test_solve_page_clean_and_flow(env):
    c, fake = env
    login(c)
    t = start(c)
    r = c.get("/")
    assert r.headers["location"] == "/solve"
    html = c.get("/solve").text
    assert "Two Sum" in html and "leetcode.com" in html and 'target="_blank"' in html
    assert_clean(html)
    h = {"X-CSRF-Token": t}
    c.post("/solve/approach", data={"approach": "scan once"}, headers=h)
    c.post("/solve/chat", data={"text": "hi"}, headers=h)
    r = c.post("/solve/submit", data={"code": "bad", "verdict": "accepted"}, headers=h)
    assert "COST" in r.text
    c.post("/solve/stuck", headers=h)
    kinds = [k for k, _ in fake.events]
    assert kinds == ["approach", "chat", "submission", "stuck"]
    assert [m["role"] for m in fake.msgs] == ["user", "user"]
    fake.post_message(1, "coach", "what is the cost?")
    assert "what is the cost?" in c.get("/messages?since=0").text


def test_dashboard_blocked_while_open(env):
    c, _ = env
    login(c)
    r = c.get("/dashboard")
    assert r.status_code == 200 and "Dashboard" in r.text
    start(c)
    r = c.get("/dashboard")
    assert r.status_code == 303 and r.headers["location"] == "/solve"
    assert "Log the current problem first" in c.get("/solve").text


def test_log_reveal_flow(env):
    c, fake = env
    login(c)
    t = start(c)
    assert c.get("/reveal/1").headers["location"] == "/log"
    assert "Log attempt" in c.get("/log").text
    r = c.post("/log", data={"csrf": t, "actual_min": "12", "bugs": "0", "hints": "0",
                             "optimal": "yes", "derived_cold": "no", "notes": "n"})
    assert r.headers["location"] == "/reveal/1"
    assert fake.logged[1]["derived_cold"] is False and fake.logged[1]["optimal"] is True
    page = c.get("/reveal/1").text
    assert "prefix-hashmap" in page and 'href="/"' in page and "/dashboard" in page
    assert 'href="/solve"' not in page
    assert c.get("/").text.count("Next problem") == 1
