"""Step 4 Review: starts on Accepted only, own clock outside the solve, skip, bonus scoring."""
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from coach import db, rating, service, steps, tools
from coach.models import Handout, Problem, RatingEvent
from coach.web import app as webapp
from coach.web.svc import Svc
from tests.test_mmr import back, events, give, handout, parts
from tests.test_runner import SUM, SUM_BAD, SUM_CASES, SUM_OK
from tests.test_web import assert_clean, visible_text

REVIEW = {"complexity": {"score": 1, "why": "linear time, said why"},
          "edge_case": {"score": 0.5, "why": "named negatives, not zero"}}


def _submit(uid, hid, code):
    service.push_event(uid, "submission", {"handout_id": hid, "code": code,
                                           "judged_by": "coach"})


def stepped(uid):
    h = give(uid, phase_budget={"understand": 2, "approach": 5, "code": 8})
    hid = h.handout_id
    service.understood(uid, hid)
    service.unlock_editor(uid, hid)
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    return hid


def accepted(uid, hid):
    _submit(uid, hid, SUM_OK)
    return service.judge(uid, hid)


def log(uid, hid):
    return service.log_attempt(uid, handout_id=hid, bugs=0, hints=0, optimal=True,
                               derived_cold=True)


# ---- start: Accepted only, stepped only ------------------------------------------------------
def test_review_starts_on_accepted_only(world):
    hid = stepped(world)
    _submit(world, hid, SUM_BAD)
    r = service.judge(world, hid)
    assert r["verdict"] == "Wrong Answer" and "review_started" not in r
    assert handout(hid).review_state is None and events(world, "review_started") == []
    r = accepted(world, hid)
    assert r["verdict"] == "Accepted" and r["review_started"] is True
    h = handout(hid)
    assert h.review_state == "open" and h.phase_label == "review" and h.clock_state == "stopped"
    assert events(world, "review_started")[0]["handout_id"] == hid
    st = service.clock_state(world, hid)
    assert st["state"] == "stopped" and st["steps"]["current"] == "review"
    assert st["steps"]["title"] == "Review (optional)" and st["steps"]["index"] == 4
    assert [d["phase"] for d in st["steps"]["done"]] == ["understand", "approach", "code"]
    # a second Accepted does not restart it
    assert "review_started" not in accepted(world, hid)
    assert len(events(world, "review_started")) == 1


def test_unstepped_handout_never_starts_review(world):
    hid = service.next_problem(world).handout_id
    service.set_tests(world, hid, SUM_CASES, "function", SUM)
    r = accepted(world, hid)
    assert r["verdict"] == "Accepted" and "review_started" not in r
    assert handout(hid).review_state is None
    assert service.clock_state(world, hid)["state"] == "running"


# ---- own clock: excluded from the solve time / target ---------------------------------------
def test_review_clock_is_outside_the_solve(world):
    hid = stepped(world)
    back(hid, 300)
    accepted(world, hid)
    solve = handout(hid).clock_accum_s
    code_s = handout(hid).phase_elapsed["code"]
    with db.session_scope() as s:                      # 20 minutes of review talk
        h = s.get(Handout, hid)
        h.review_started_at -= timedelta(minutes=20)
    st = service.clock_state(world, hid)
    assert st["elapsed_s"] == solve and st["steps"]["seconds"]["code"] == code_s
    assert st["steps"]["review"]["seconds"] >= 1200
    assert service._fire_checkpoints(world) is None and service._fire_idle(world) is None
    with pytest.raises(ValueError, match="review"):
        service.clock(world, hid, "resume")
    r = log(world, hid)
    assert r["actual_min"] == max(1, round(solve / 60)) and r["actual_min"] < 20
    h = handout(hid)
    assert h.review_state == "done" and h.review_s >= 1200
    assert h.phase_elapsed["code"] == code_s
    rv = service.reveal(hid)
    assert rv["review"]["state"] == "done" and rv["review"]["seconds"] >= 1200
    assert [s["phase"] for s in rv["steps"]] == ["understand", "approach", "code"]


# ---- skip ------------------------------------------------------------------------------------
def test_skip_review_service(world):
    hid = stepped(world)
    with pytest.raises(ValueError, match="no review"):
        service.skip_review(world, hid)
    accepted(world, hid)
    service._drain(world)
    r = service.skip_review(world, hid)
    assert r["ok"] and not r["already"]
    ev = service._drain(world)
    assert [e["kind"] for e in ev] == ["review_skipped"]
    assert ev[0]["payload"]["handout_id"] == hid
    assert service.skip_review(world, hid)["already"] is True
    assert service._drain(world) == []
    assert service.clock_state(world, hid)["steps"]["review"]["state"] == "skipped"
    log(world, hid)
    assert handout(hid).review_state == "skipped"                   # log keeps the skip
    with pytest.raises(service.ScoreRefused, match="skipped"):
        service.score_attempt(world, hid, dict(parts(), review=REVIEW))
    out = service.score_attempt(world, hid, parts())                # no penalty
    assert out["actual"] == 1.0
    assert service.reveal(hid)["review"]["state"] == "skipped"


@pytest.fixture
def web(world, monkeypatch):
    hid = stepped(world)
    with db.session_scope() as s:                  # a neutral title: the page check is RULE ZERO
        s.get(Problem, handout(hid).problem_number).title = "Count the runs"
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    webapp.app.state.service = Svc()
    c = TestClient(webapp.app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    assert c.post("/login", data={"username": "tester", "password": "pw-for-tests",
                                  "csrf": tok}).status_code == 303
    csrf = re.search(r'X-CSRF-Token": "([^"]+)"', c.get("/solve").text).group(1)
    yield c, world, hid, {"X-CSRF-Token": csrf}
    webapp.app.state.service = None


def test_skip_endpoint_login_csrf(web):
    c, uid, hid, hdr = web
    accepted(uid, hid)
    service._drain(uid)
    assert c.post("/solve/review/skip").status_code == 403                # csrf
    anon = TestClient(webapp.app, follow_redirects=False)
    assert anon.post("/solve/review/skip", headers=hdr).status_code in (303, 401, 403)
    r = c.post("/solve/review/skip", headers=hdr)
    d = r.json()
    assert r.status_code == 200 and d["ok"] and d["clock"]["steps"]["review"]["state"] == "skipped"
    assert [e["kind"] for e in service._drain(uid)] == ["review_skipped"]


def test_skip_endpoint_refused_outside_review(web):
    c, uid, hid, hdr = web
    r = c.post("/solve/review/skip", headers=hdr)
    assert r.status_code == 400 and not r.json()["ok"]


def test_solve_page_in_review_step_rule_zero(web):
    c, uid, hid, hdr = web
    accepted(uid, hid)
    page = c.get("/solve").text
    assert 'data-run="stopped"' in page and '"current": "review"' in page
    assert_clean(page)
    js = c.get("/static/steps.js").text
    assert "Skip review" in js and "/solve/review/skip" in js
    for w in ("bonus", "mmr", "rating"):        # no progress talk on the page in review
        assert w not in visible_text(page)


# ---- bonus math --------------------------------------------------------------------------------
def test_review_bonus_math_and_cap():
    full = {k: {"score": 1, "why": "ok"} for k in rating.REVIEW_WEIGHTS}
    assert rating.review_bonus(full)["bonus"] == 0.10
    b = rating.review_bonus(REVIEW)
    assert b["bonus"] == round(0.03 + 0.015, 3) and set(b["items"]) == {"complexity", "edge_case"}
    assert b["items"]["edge_case"] == {"weight": 0.03, "score": 0.5, "points": 0.015,
                                       "why": "named negatives, not zero"}
    old = rating.REVIEW_CAP
    try:
        rating.REVIEW_CAP = 0.05
        assert rating.review_bonus(full)["bonus"] == 0.05
    finally:
        rating.REVIEW_CAP = old
    actual, bd = rating.coach_score(dict(parts(), review=full))
    assert actual == pytest.approx(1.10) and bd["solve_actual"] == 1.0 and bd["actual"] == 1.1
    actual, bd = rating.coach_score(parts())                        # old flow unchanged
    assert actual == 1.0 and "review" not in bd and "solve_actual" not in bd


@pytest.mark.parametrize("rv,msg", [
    ({}, "leave review out"), ({"style": {"score": 1, "why": "x"}}, "unknown style"),
    ({"complexity": {"score": 1.5, "why": "x"}}, "0-1"),
    ({"complexity": {"score": 1, "why": ""}}, "why"), ({"complexity": 1}, "score, why")])
def test_review_part_refused(rv, msg):
    with pytest.raises(rating.RatingRefused, match=msg):
        rating.coach_score(dict(parts(), review=rv))


def test_review_refused_when_abandoned():
    with pytest.raises(rating.RatingRefused, match="abandoned"):
        rating.coach_score(dict(parts(), review=REVIEW), abandoned=True)


def test_scored_review_moves_mmr_with_actual_over_one(world):
    hid = stepped(world)
    accepted(world, hid)
    r = log(world, hid)
    full = {k: {"score": 1, "why": f"{k} answered"} for k in rating.REVIEW_WEIGHTS}
    out = service.score_attempt(world, hid, dict(parts(), review=full))
    e = rating.expected(1200, rating.DEFAULT_RATING["M"])
    assert out["actual"] == pytest.approx(1.10)
    assert out["delta"] == pytest.approx(40 * (1.10 - e), abs=0.06)
    with db.session_scope() as s:
        ev = s.query(RatingEvent).filter_by(attempt_id=r["attempt_id"]).one()
        assert ev.actual == pytest.approx(1.10) and ev.expected <= 1
        assert ev.breakdown["review"]["bonus"] == 0.10
    rv = service.reveal(hid)
    assert rv["rating"]["review"]["items"]["follow_up"]["why"] == "follow_up answered"


def test_review_needs_an_accepted_submission(world):
    h = give(world, phase_budget={"understand": 2, "approach": 5, "code": 8})
    log(world, h.handout_id)
    with pytest.raises(service.ScoreRefused, match="Accepted"):
        service.score_attempt(world, h.handout_id, dict(parts(), review=REVIEW))
    assert service.reveal(h.handout_id)["review"] is None


def test_review_whys_are_guarded(world):
    hid = stepped(world)
    accepted(world, hid)
    log(world, hid)
    bad = {"complexity": {"score": 1, "why": "tester got it right"}}
    with pytest.raises(service.ScoreRefused, match="name no one"):
        service.score_attempt(world, hid, dict(parts(), review=bad))


# ---- reveal page ---------------------------------------------------------------------------------
def test_reveal_shows_review_bonus_or_skipped(web):
    c, uid, hid, hdr = web
    accepted(uid, hid)
    log(uid, hid)
    service.score_attempt(uid, hid, dict(parts(), review=REVIEW))
    page = c.get(f"/reveal/{hid}").text
    assert 'id="rv-review"' in page and "Review bonus" in page
    assert "Complexity and why" in page and "linear time, said why" in page
    assert "named negatives, not zero" in page and "+0.045" in page
    assert "Follow-up twist" not in page                            # unasked = absent
    assert "not part of the solve time" in page
    card = c.get(f"/reveal/{hid}/rating").json()["html"]
    assert "Review bonus" in card


def test_reveal_skipped_and_not_offered(world, monkeypatch):
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    webapp.app.state.service = Svc()
    try:
        c = TestClient(webapp.app, follow_redirects=False)
        tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
        c.post("/login", data={"username": "tester", "password": "pw-for-tests", "csrf": tok})
        hid = stepped(world)
        accepted(world, hid)
        service.skip_review(world, hid)
        log(world, hid)
        service.score_attempt(world, hid, parts())
        page = c.get(f"/reveal/{hid}").text
        assert "Review skipped." in page and "Review bonus" not in page
        h2 = give(world, slug="two-pointers", phase_budget={"understand": 2, "approach": 5,
                                                            "code": 8})
        log(world, h2.handout_id)
        service.score_attempt(world, h2.handout_id, parts())
        page = c.get(f"/reveal/{h2.handout_id}").text
        assert 'id="rv-review"' not in page and "Review skipped" not in page
        assert 'id="rv-review-time"' not in page
    finally:
        webapp.app.state.service = None


# ---- tool docs and registry ------------------------------------------------------------------------
def test_tool_docs_mention_review():
    assert "review_started" in tools.TOOLS["judge"].description
    assert "review" in tools.TOOLS["score_attempt"].description
    assert "0.10" in tools.TOOLS["score_attempt"].description
    w = tools.TOOLS["wait_for_event"].description
    assert "review_started" in w and "review_skipped" in w
    assert [p.name for p in tools.TOOLS["score_attempt"].params] == ["handout_id", "parts",
                                                                     "summary"]


def test_skill_drops_the_all_pass_edge_case_question():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1] / "bridge" / "skills" / "coach"
            / "SKILL.md").read_text(encoding="utf-8")
    assert "All pass. Submit when you're ready." in text
    assert "ask for the edge case he thinks" not in text
    assert "edge case he thinks is weakest" not in text
    sec = text.split("### Review — after Accepted", 1)[1].split("\n## ", 1)[0]
    assert len(sec.strip().splitlines()) <= 20
