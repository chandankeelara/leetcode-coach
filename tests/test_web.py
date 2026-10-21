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
    """Open a problem the way a student without a coach does: the default pick."""
    t = csrf(c)
    c.post("/solve/default", data={"csrf": t})
    return t


def test_login_required(env):
    c, _ = env
    for path in ["/", "/solve", "/log", "/dashboard", "/history", "/messages", "/reveal/1"]:
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


def assert_neutral_nav(html):
    nav = re.search(r'(?s)<header class="nav">.*?</header>', html).group(0)
    assert "/history" not in nav and "/dashboard" not in nav
    assert not re.search(r"\d", visible_text(nav))


def test_interstitial_neutral(env):
    c, _ = env
    login(c)
    r = c.get("/")
    assert r.status_code == 200 and "Next problem" in r.text
    assert_clean(r.text)
    assert_neutral_nav(r.text)


def test_solve_page_clean_and_flow(env):
    c, fake = env
    login(c)
    t = start(c)
    r = c.get("/")
    assert r.headers["location"] == "/solve"
    html = c.get("/solve").text
    assert "Two Sum" in html and "leetcode.com" in html and 'target="_blank"' in html
    assert_clean(html)
    assert_neutral_nav(html)
    assert "/history" not in html and "/dashboard" not in html
    assert_clean(c.get("/log").text)
    h = {"X-CSRF-Token": t}
    c.post("/solve/approach", data={"approach": "scan once"}, headers=h)
    c.post("/solve/chat", data={"text": "hi"}, headers=h)
    r = c.post("/solve/submit", data={"code": "bad"}, headers=h)
    assert "COST" in r.text
    sub = [p for k, p in fake.events if k == "submission"][0]
    assert "verdict" not in sub and sub["judged_by"] == "coach" and sub["code"] == "bad"
    assert 'id="verdict"' not in html and "LeetCode said" not in html
    assert 'id="judging"' in html
    c.post("/solve/stuck", headers=h)
    kinds = [k for k, _ in fake.events]
    assert kinds == ["handout", "approach", "chat", "submission", "stuck"]
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


def test_history_requires_login(env):
    c, _ = env
    r = c.get("/history")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_history_page_and_blocked_while_open(env):
    c, _ = env
    login(c)
    r = c.get("/history")
    assert r.status_code == 200 and "Two Sum" in r.text and "Longest Substring" in r.text
    r = c.get("/history?outcome=clean")
    assert "Two Sum" in r.text and "Longest Substring" not in r.text
    r = c.get("/history?pattern=sliding-window")
    assert "Longest Substring" in r.text and "Two Sum" not in r.text
    start(c)
    r = c.get("/history")
    assert r.status_code == 303 and r.headers["location"] == "/solve"


def test_messages_reports_open_handout(env):
    c, fake = env
    login(c)
    t = start(c)
    r = c.get("/messages?since=0")
    assert r.headers["x-open-handout"] == "1"
    c.post("/log", data={"csrf": t, "actual_min": "5"})
    assert c.get("/messages?since=0").headers["x-open-handout"] == ""


def test_solve_chat_starts_at_handout(env):
    from datetime import datetime, timedelta
    c, fake = env
    login(c)
    old = (datetime.now() - timedelta(hours=1)).isoformat()
    fake.msgs.append({"id": 1, "role": "coach", "content": "that one was a sliding window",
                      "created_at": old})
    start(c)
    fake.msgs.append({"id": 2, "role": "coach", "content": "go", "created_at": datetime.now().isoformat()})
    html = c.get("/solve").text
    assert "sliding window" not in html and ">go<" in html
    assert 'data-last="2"' in html


def test_solve_statement_card_and_poll(env):
    c, fake = env
    login(c)
    start(c)
    html = c.get("/solve").text
    assert "Your coach is writing the statement" in html and 'data-pending="1"' in html
    assert c.get("/statement").json()["html"] == ""
    fake.h.paraphrase = ("## Task\nGiven `nums` and `k`, count <b>subarrays</b> summing to k.\n\n"
                         "- Input: [1,1,1], 2 -> 2\n- Input: [1,2,3], 3 -> 2\n\n"
                         "```python\ndef f(nums: list[int], k: int) -> int:\n```")
    html = c.get("/solve").text
    assert "<h4>Task</h4>" in html and "<code>nums</code>" in html and "<li>" in html
    assert "<pre><code>def f(" in html
    assert "&lt;b&gt;subarrays" in html and "<b>subarrays" not in html
    assert "writing the statement" not in html
    assert html.index('id="stmt"') < html.index('id="appr"')
    assert_clean(html)
    j = c.get("/statement").json()
    assert "<li>" in j["html"] and j["handout_id"] == 1


def test_coach_presence(env):
    c, fake = env
    login(c); start(c)
    r = c.get("/messages")
    assert r.headers["X-Coach-Online"] == "1"
    page = c.get("/solve").text
    assert 'id="coach-banner"' in page and "Coach not connected." in page
    assert re.search(r'id="coach-banner"[^>]*\shidden', page)       # online: hidden
    assert c.get("/coach/status").json() == {"online": True, "last_seen": None}
    fake.online = False
    assert c.get("/messages").headers["X-Coach-Online"] == "0"
    page = c.get("/solve").text
    assert not re.search(r'id="coach-banner"[^>]*\shidden', page)   # offline: shown
    assert "/help/connect" in page and "/leetcode-coach:coach" in page
    assert c.get("/coach/status").json()["online"] is False
    c.get("/") ; assert 'id="coach-banner"' in c.get("/solve").text


def test_help_connect(env):
    c, _ = env
    assert c.get("/help/connect").headers["location"] == "/login"
    login(c)
    r = c.get("/help/connect")
    assert r.status_code == 200 and "claude mcp add --transport http coach" in r.text
    assert "/mcp" in r.text and 'href="/connect"' in r.text
    assert_clean(r.text)


# ---- editor gate: only the coach opens the editor -------------------------------------------
def test_editor_locked_until_coach_unlocks(env):
    c, fake = env
    login(c)
    t = start(c)
    h = {"X-CSRF-Token": t}
    assert 'data-stage="approach"' in c.get("/solve").text
    assert c.get("/messages?since=0").headers["X-Editor-Open"] == "0"
    c.post("/solve/approach", data={"approach": "scan once"}, headers=h)
    html = c.get("/solve").text
    assert 'data-stage="discuss"' in html                       # discussion shows, editor locked
    assert "Code opens when your coach says the idea holds." in html
    assert 'id="ed-lock"' in html and 'id="ed-lock" hidden' not in html
    assert c.get("/messages?since=0").headers["X-Editor-Open"] == "0"
    fake.unlock_editor(1, 1)
    r = c.get("/messages?since=0")
    assert r.headers["X-Editor-Open"] == "1" and "Editor open." in r.text
    html = c.get("/solve").text
    assert 'data-stage="code"' in html and 'id="ed-lock" hidden' in html
    fake.lock_editor(1, 1)
    assert c.get("/messages?since=0").headers["X-Editor-Open"] == "0"
    assert 'data-stage="discuss"' in c.get("/solve").text


def test_web_handout_tells_the_coach(env):
    """Next problem pressed on the page -> the coach's queue gets a `handout` event."""
    c, fake = env
    login(c)
    t = start(c)
    kinds = [k for k, _ in fake.events]
    assert kinds.count("handout") == 1
    payload = dict(fake.events)["handout"]
    assert payload["handout_id"] == fake.h.handout_id and payload["source"] == "web"
    c.post("/solve", data={"csrf": t})  # a second press while one is open hands out nothing new
    assert [k for k, _ in fake.events].count("handout") == 1


def test_mdblock_viz_blocks():
    from coach.web.app import mdblock
    out = str(mdblock('Intro\n```viz\n{"kind":"list","values":[3,2],"cycle_to":1,"caption":"pos = 1"}\n```\n'
                      '```viz\n{"kind":"tree","values":[1,2,3]}\n```\n'))
    assert "viz-list" in out and "viz-back" in out and "<pre>" not in out
    assert "pos = 1" not in out                       # the Input line already says it
    assert out.count("<circle") == 3


STATEMENT_141 = '## Examples\n\n### Example 1\n**Input:** `head = [3,2,0,-4], pos = 1`\n```viz\n{"kind": "list", "values": [3,2,0,-4], "cycle_to": 1, "caption": "pos = 1"}\n```\n**Output:** `true`\n**Explanation:** the last node (-4) links back to the node at index 1 (value 2).\n\n### Example 2\n**Input:** `head = [1,2], pos = 0`\n```viz\n{"kind": "list", "values": [1,2], "cycle_to": 0, "caption": "pos = 0"}\n```\n**Output:** `true`\n**Explanation:** the last node (2) links back to the first node.\n\n### Example 3\n**Input:** `head = [1], pos = -1`\n```viz\n{"kind": "list", "values": [1], "cycle_to": null, "caption": "pos = -1"}\n```\n**Output:** `false`\n**Explanation:** the single node\'s `next` is `null`, so no node repeats.'


def test_statement_141_renders_three_drawings():
    from coach.web.app import mdblock
    out = str(mdblock(STATEMENT_141))
    assert out.count("<svg") == 3 and "illustration unavailable" not in out
    assert out.count('class="viz-back viz-ll-loop"') == 2 and out.count(">null</text>") == 1
    assert "↩" not in out and "viz-cap" not in out


def test_mdblock_viz_invalid_is_muted_and_escaped():
    from coach.web.app import mdblock
    for blk in ('{"kind":"list","values":[', '{"kind":"nope"}', '[1]'):
        out = str(mdblock("```viz\n" + blk + "\n```"))
        assert "illustration unavailable" in out and "nope" not in out and "<pre>" not in out
    out = str(mdblock('```viz\n{"kind":"array","values":["<script>alert(1)</script>"]}\n```'))
    assert "<script" not in out and "&lt;script&gt;" in out


def test_statement_endpoint_renders_drawings(env):
    c, fake = env
    login(c)
    start(c)
    fake.h.paraphrase = ('Intro.\n**Input:** `head = [1,2]`, `pos = 0`\n'
                         '```viz\n{"kind":"list","values":[1,2],"cycle_to":0}\n```\n')
    j = c.get("/statement").json()
    assert "viz-list" in j["html"] and "viz-back" in j["html"]
    assert "viz-list" in c.get("/solve").text


# ---- coach-led choice: Next problem asks the coach; plan and onboarding ---------------------
def test_next_problem_asks_the_coach_when_online(env):
    c, fake = env
    login(c)
    t = csrf(c)
    r = c.post("/solve", data={"csrf": t})
    assert r.status_code == 303 and r.headers["location"] == "/next"
    assert fake.h is None                                     # the page picked nothing
    assert [k for k, _ in fake.events] == ["next_requested"]
    page = c.get("/next").text
    assert "Your coach is choosing your next problem" in page
    assert re.search(r'id="wait-off"[^>]*hidden', page)
    assert_clean(page)
    assert_neutral_nav(page)
    c.post("/solve", data={"csrf": t})                        # pressing again: one request only
    assert [k for k, _ in fake.events] == ["next_requested"]
    fake.next_problem(1)                                      # the coach hands one out
    assert c.get("/next").headers["location"] == "/solve"


def test_next_problem_offline_offers_connect_steps_and_default_pick(env):
    c, fake = env
    fake.online = False
    login(c)
    t = csrf(c)
    assert c.post("/solve", data={"csrf": t}).headers["location"] == "/next"
    assert fake.events == [] and fake.h is None
    page = c.get("/next").text
    assert "Your coach is not connected." in page and "/leetcode-coach:coach" in page
    assert "Use the default pick" in page and re.search(r'id="wait-on"[^>]*hidden', page)
    assert_clean(page)
    r = c.post("/solve/default", data={"csrf": t})
    assert r.headers["location"] == "/solve" and fake.h is not None
    assert [k for k, _ in fake.events] == ["handout"]


def test_default_pick_with_nothing_left(env, monkeypatch):
    c, fake = env
    login(c)
    monkeypatch.setattr(fake, "next_problem", lambda uid: None)
    r = c.post("/solve/default", data={"csrf": csrf(c)})
    assert r.headers["location"] == "/next?none=1"
    assert "Nothing to hand out right now." in c.get("/next?none=1").text


def test_new_user_without_plan_goes_to_onboarding(env):
    c, fake = env
    fake.plan = None
    login(c)
    assert c.get("/").headers["location"] == "/onboarding"
    assert c.get("/dashboard").headers["location"] == "/onboarding"
    assert c.get("/plan").headers["location"] == "/onboarding"
    page = c.get("/onboarding").text
    assert "Set up your plan" in page and "Focus areas" in page and "Two Pointers" in page
    assert "When to allow Hards" in page
    tok = re.search(r'X-CSRF-Token": "([^"]+)"', page).group(1)
    assert c.post("/solve", data={"csrf": tok}).headers["location"] == "/onboarding"
    r = c.post("/onboarding", data={"csrf": tok, "target_date": "2026-12-15",
                                    "daily_problems": "3", "minutes_per_day": "60",
                                    "focus": ["heap", "two-pointers"], "hards": "date",
                                    "hards_from": "2026-11-15"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert fake.plan["daily_problems"] == "3" or fake.plan["daily_problems"] == 3
    assert fake.plan["focus_patterns"] == ["heap", "two-pointers"]
    assert fake.plan["hards_from"] == "2026-11-15"
    assert c.get("/").status_code == 200
    assert c.get("/onboarding").headers["location"] == "/plan"


def test_onboarding_errors_rerender(env):
    c, fake = env
    fake.plan = None
    login(c)
    tok = re.search(r'X-CSRF-Token": "([^"]+)"', c.get("/onboarding").text).group(1)
    r = c.post("/onboarding", data={"csrf": tok, "target_date": "", "hards": "never"})
    assert r.status_code == 200 and "Pick a target date." in r.text and fake.plan is None
    r = c.post("/onboarding", data={"csrf": tok, "target_date": "2026-12-15", "hards": "date"})
    assert "Pick the date Hards start" in r.text


def test_plan_page_view_edit_and_blocked_while_open(env):
    c, fake = env
    login(c)
    assert 'href="/plan"' in c.get("/dashboard").text
    page = c.get("/plan").text
    assert "Your plan" in page and "Phases" in page
    tok = re.search(r'X-CSRF-Token": "([^"]+)"', page).group(1)
    r = c.post("/plan", data={"csrf": tok, "title": "Loop prep", "target_date": "2027-01-10",
                              "daily_problems": "2", "minutes_per_day": "90", "hards": "never",
                              "phases": "A | 2026-10-21 | 2026-11-30 | core first\n",
                              "notes": "n"})
    assert r.headers["location"] == "/plan?saved=1"
    assert fake.plan["title"] == "Loop prep" and fake.plan["hards_from"] is None
    assert fake.plan["phases"] == [{"name": "A", "start": "2026-10-21", "end": "2026-11-30",
                                    "rules": "core first"}]
    start(c)
    assert c.get("/plan").headers["location"] == "/solve"
    assert 'href="/plan"' not in c.get("/solve").text


def test_reveal_shows_the_coach_reason(env, monkeypatch):
    c, fake = env
    login(c)
    start(c)
    c.post("/log", data={"csrf": csrf_from(c), "actual_min": "5"})
    base = fake.reveal
    monkeypatch.setattr(fake, "reveal", lambda hid: dict(base(hid), why="retry due: 2 bugs"))
    assert "retry due: 2 bugs" in c.get("/reveal/1").text


def csrf_from(c):
    return re.search(r'X-CSRF-Token": "([^"]+)"', c.get("/log").text).group(1)
