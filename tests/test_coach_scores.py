"""The coach scores the solve (score_attempt) and sets the code budget at unlock_editor."""
import pytest

from coach import db, rating, scoring, service, tools
from coach.models import Handout, Rating, RatingEvent, Submission
from tests.test_mmr import TODAY, give, handout, num, parts


def logged(uid, **kw):
    h = give(uid, phase_budget={"understand": 2, "approach": 5, "code": 8})
    r = service.log_attempt(uid, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                            derived_cold=True, **kw)
    return h.handout_id, r


# ---- score_attempt ---------------------------------------------------------------------------
def test_score_only_after_log_and_once(world):
    h = give(world)
    with pytest.raises(service.ScoreRefused, match="log the attempt first"):
        service.score_attempt(world, h.handout_id, parts())
    r = service.log_attempt(world, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                            derived_cold=True)
    assert r["score_pending"] is True and "rating" not in r
    rv = service.reveal(h.handout_id)
    assert rv["score_pending"] and rv["rating"] is None
    out = service.score_attempt(world, h.handout_id, parts())
    assert out["scored_by"] == "coach" and out["actual"] == 1.0
    with pytest.raises(service.ScoreRefused, match="already scored"):
        service.score_attempt(world, h.handout_id, parts())
    assert not service.reveal(h.handout_id)["score_pending"]
    with pytest.raises(service.ScoreRefused, match="no handout"):
        service.score_attempt(world + 99, h.handout_id, parts())


def test_mmr_from_coach_parts(world):
    hid, r = logged(world)
    p = parts(understand=1, approach=0.5, first_submit=0, quality=1, time=0.6, deduction=0.1)
    out = service.score_attempt(world, hid, p, summary="idea his, first submit wrong")
    want = 0.10 + 0.25 * 0.5 + 0 + 0.15 + 0.20 * 0.6 - 0.1
    assert abs(out["actual"] - round(want, 3)) < 1e-3
    e = rating.expected(1200, rating.DEFAULT_RATING["M"])
    with db.session_scope() as s:
        ev = s.query(RatingEvent).filter_by(attempt_id=r["attempt_id"]).one()
        assert abs(ev.delta - 40 * (want - e)) < 0.01
        assert abs(s.get(Rating, world).mmr - (1200 + 40 * (want - e))) < 0.01
        bd = ev.breakdown
    assert bd["scored_by"] == "coach" and bd["summary"] == "idea his, first submit wrong"
    assert bd["parts"]["approach"] == {"weight": 0.25, "score": 0.5, "points": 0.125,
                                       "why": "approach reason"}
    assert bd["hints"] == {"deduction": 0.1, "why": "one hint"}
    rv = service.reveal(hid)["rating"]
    assert rv["scored_by"] == "coach" and rv["breakdown"]["parts"]["first_submit"]["score"] == 0


def test_floor_zero(world):
    hid, _ = logged(world)
    out = service.score_attempt(world, hid, parts(0, 0, 0, 0, 0.5, deduction=0.3))
    assert out["actual"] == 0.0


@pytest.mark.parametrize("bad,msg", [
    (lambda p: p.pop("time"), "missing time"),
    (lambda p: p.update(extra={"score": 1, "why": "x"}), "unknown extra"),
    (lambda p: p["quality"].update(score=1.2), "quality.score must be 0-1"),
    (lambda p: p["quality"].update(score=True), "quality.score must be a number"),
    (lambda p: p["hints"].update(deduction=0.5), "hints.deduction must be 0-0.3"),
    (lambda p: p["approach"].update(why=""), "approach.why"),
    (lambda p: p["approach"].update(why="x" * 201), "at most 200"),
    (lambda p: p["approach"].update(why="def f(a):\n    return a"), "no code"),
    (lambda p: p["approach"].update(why="tester had it"), "name no one"),
])
def test_bad_parts_refused_and_nothing_stored(world, bad, msg):
    hid, _ = logged(world)
    p = parts()
    bad(p)
    with pytest.raises(service.ScoreRefused, match=msg):
        service.score_attempt(world, hid, p)
    with db.session_scope() as s:
        assert s.query(RatingEvent).count() == 0 and s.get(Rating, world).games == 0
    service.score_attempt(world, hid, parts())            # still scorable


def test_pending_scores_in_session_summary(world):
    hid, r = logged(world, day=TODAY)
    summ = service.session_summary(world, today=TODAY)
    assert [x["handout_id"] for x in summ["pending_scores"]] == [hid]
    assert summ["pending_scores"][0]["attempt_id"] == r["attempt_id"]
    service.score_attempt(world, hid, parts())
    assert service.session_summary(world, today=TODAY)["pending_scores"] == []


def test_attempt_without_handout_keeps_the_formula(world):
    r = service.log_attempt(world, number=num("heap", "E"), actual_min=5, target_min=10,
                            bugs=0, day=TODAY)
    assert "rating" in r and "score_pending" not in r
    with db.session_scope() as s:
        ev = s.query(RatingEvent).filter_by(attempt_id=r["attempt_id"]).one()
        assert ev.breakdown["scored_by"] == "formula"


def test_score_tool(world):
    hid, _ = logged(world)
    ctx = tools.Ctx(world)
    bad = parts()
    bad.pop("hints")
    assert "missing hints" in tools.score_attempt(ctx, hid, bad)["error"]
    out = tools.score_attempt(ctx, hid, parts(), "clean")
    assert out["scored_by"] == "coach" and "delta" in out
    assert "already scored" in tools.score_attempt(ctx, hid, parts())["error"]
    assert "score_attempt" in {t["name"] for t in tools.tool_list()}


# ---- code budget at unlock -------------------------------------------------------------------
def test_hand_out_needs_only_understand_and_approach(world):
    h = give(world, phase_budget={"understand": 2, "approach": 5})
    b = handout(h.handout_id).phase_budget
    # provisional code = default total (Silver M: 17) - 7
    assert b == {"understand": 2, "approach": 5, "code": 10} and h.target_min == 17


def test_unlock_sets_code_budget_and_reason(world):
    h = give(world, phase_budget={"understand": 2, "approach": 5})
    service.understood(world, h.handout_id)
    r = service.unlock_with_budget(world, h.handout_id, code_min=12,
                                   code_reason="12 lines, two fiddly index updates")
    assert r["ok"] and r["budget"]["code"] == 12 and r["target_min"] == 19
    row = handout(h.handout_id)
    assert row.phase_budget["code"] == 12 and row.target_min == 19
    assert row.editor_open and row.phase_label == "code"
    service.log_attempt(world, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                        derived_cold=True)
    rv = service.reveal(h.handout_id)
    assert rv["code_reason"] == "12 lines, two fiddly index updates"
    assert rv["steps"][2]["budget_min"] == 12


@pytest.mark.parametrize("bad,msg", [(1, "outside 2-40"), (41, "outside 2-40"),
                                     (2.5, "whole minutes"), ("x", "whole minutes")])
def test_unlock_code_min_clamp(world, bad, msg):
    h = give(world)
    service.understood(world, h.handout_id)
    with pytest.raises(service.ScoreRefused, match=msg):
        service.unlock_with_budget(world, h.handout_id, code_min=bad)
    assert not handout(h.handout_id).editor_open                     # nothing opened
    ctx = tools.Ctx(world)
    assert "outside 2-40" in tools.unlock_editor(ctx, code_min=50)["error"]


def test_unlock_without_code_min_keeps_budget(world):
    h = give(world, phase_budget={"understand": 2, "approach": 5, "code": 8})
    service.understood(world, h.handout_id)
    ctx = tools.Ctx(world)
    assert tools.unlock_editor(ctx) == {"ok": True}
    assert handout(h.handout_id).phase_budget["code"] == 8
    r = tools.unlock_editor(ctx, code_min=10, code_reason="short loop")
    assert r["budget"]["code"] == 10 and handout(h.handout_id).code_reason == "short loop"


# ---- code speed ------------------------------------------------------------------------------
def _solved(uid, slug, lines, code_s):
    h = give(uid, slug, phase_budget={"understand": 2, "approach": 5, "code": 8})
    code = "\n".join(f"x{i} = {i}" for i in range(lines)) + "\n\n# note\n"
    with db.session_scope() as s:
        s.add(Submission(user_id=uid, handout_id=h.handout_id, code="wrong",
                         verdict="Wrong Answer"))
        s.add(Submission(user_id=uid, handout_id=h.handout_id, code=code, verdict="Accepted"))
    service.log_attempt(uid, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                        derived_cold=True)
    with db.session_scope() as s:
        s.get(Handout, h.handout_id).phase_elapsed = {"understand": 60, "approach": 60,
                                                       "code": code_s}
    service.score_attempt(uid, h.handout_id, parts())


def test_code_speed_needs_three_samples(world):
    assert service.get_profile(world, today=TODAY)["code_speed"] == {
        "seconds_per_line": None, "samples": 0, "recent": []}
    _solved(world, "heap", 10, 300)                   # 30 s/line
    _solved(world, "two-pointers", 10, 200)           # 20 s/line
    cs = service.get_profile(world, today=TODAY)["code_speed"]
    assert cs["samples"] == 2 and cs["seconds_per_line"] is None
    assert cs["recent"][0] == {"number": num("two-pointers"), "lines": 10, "code_s": 200,
                               "budget_s": 480}
    _solved(world, "sliding-window", 20, 800)         # 40 s/line
    cs = service.get_profile(world, today=TODAY)["code_speed"]
    assert cs["samples"] == 3 and cs["seconds_per_line"] == 30.0
    assert scoring.code_lines("a = 1\n\n  # c\nb = 2\n") == 2
