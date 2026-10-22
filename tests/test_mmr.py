"""Steps of a problem, time budgets, MMR, problem ratings and the coach feedback channel."""
import asyncio
import json
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from coach import db, rating, service, steps, tools
from coach.models import (Attempt, ClockEvent, CoachRequest, Event, Handout, Problem, Rating,
                          RatingEvent, Submission, User)
from coach.web import app as webapp
from coach.web.svc import Svc
from tests.test_web import assert_clean

TODAY = date.today()          # hand_out(today=...) dates the handoff; the clock reads now()


def num(slug, d="M"):
    from coach.rules import TIER1, TIER2
    i = (TIER1 + TIER2).index(slug)
    return 1000 + i * 10 + "EMH".index(d)


def give(uid, slug="heap", d="M", **kw):
    return service.hand_out(uid, num(slug, d), today=TODAY, **kw)


def back(hid, seconds):
    """Pretend the handout's clock (and its current step) started `seconds` earlier."""
    with db.session_scope() as s:
        h = s.get(Handout, hid)
        dt = timedelta(seconds=seconds)
        h.handed_at -= dt
        h.clock_anchor -= dt
        h.first_started_at -= dt
        for e in s.query(ClockEvent).filter_by(handout_id=hid):
            e.at -= dt


def events(uid, kind):
    with db.session_scope() as s:
        return [e.payload for e in s.query(Event).filter_by(user_id=uid, kind=kind)
                .order_by(Event.id)]


def handout(hid):
    with db.session_scope() as s:
        h = s.get(Handout, hid)
        s.expunge(h)
        return h


# ---- budgets: default by rank x retry, clamp -----------------------------------------------
def test_default_budget_by_rank_and_retry():
    assert steps.split(15) == {"understand": 2, "approach": 4, "code": 9}
    assert steps.split(17) == {"understand": 3, "approach": 5, "code": 9}
    assert sum(steps.split(5).values()) == 5 and min(steps.split(5).values()) >= 1
    assert steps.default_total("M", "Gold", False) == 15
    assert steps.default_total("M", "Silver", False) == 17           # 15 x 1.15
    assert steps.default_total("E", "Bronze", False) == 13           # 10 x 1.3
    assert steps.default_total("H", "Diamond", False) == 26          # 35 x 0.75
    assert steps.default_total("M", "Silver", True) == 14            # x 0.8 on a retry
    assert steps.default_total("H", "Platinum", True) == 24          # 35 x .85 x .8


def test_hand_out_default_budget_for_a_new_student(world):
    h = give(world)                                     # Silver (1200): M 15 x 1.15 = 17
    row = handout(h.handout_id)
    assert row.phase_budget == {"understand": 3, "approach": 5, "code": 9}
    assert h.target_min == 17 and row.phase_label == "understand"
    assert set(h.model_dump()) == {"handout_id", "number", "title", "leetcode_url",
                                   "paraphrase", "started_at", "target_min", "instruction",
                                   "mode"}                   # nothing about the budget


def test_hand_out_coach_budget_and_reason(world):
    h = give(world, phase_budget={"understand": 2, "approach": 5, "code": 8},
             budget_reason="level 2 pattern: more approach time")
    row = handout(h.handout_id)
    assert h.target_min == 15 and row.phase_budget["approach"] == 5
    assert row.budget_reason == "level 2 pattern: more approach time"


@pytest.mark.parametrize("d,budget,msg", [
    ("E", {"understand": 1, "approach": 1, "code": 2}, "outside 5-20"),
    ("E", {"understand": 5, "approach": 8, "code": 10}, "outside 5-20"),
    ("M", {"understand": 1, "approach": 2, "code": 4}, "outside 8-35"),
    ("M", {"understand": 0, "approach": 5, "code": 8}, "at least 1"),
    ("M", {"understand": 2, "approach": 5}, "exactly understand"),
    ("M", {"understand": 2, "approach": 5.5, "code": 8}, "whole minutes"),
])
def test_clamp_refusals(world, d, budget, msg):
    with pytest.raises(service.PickRefused, match=msg):
        give(world, "heap", d, phase_budget=budget)
    with db.session_scope() as s:
        assert s.query(Handout).count() == 0                  # nothing written


def test_clamp_ranges():
    for d, lo, hi in (("E", 5, 20), ("M", 8, 35), ("H", 15, 60)):
        steps.check_total(d, lo)
        steps.check_total(d, hi)
        for bad in (lo - 1, hi + 1):
            with pytest.raises(steps.BudgetRefused, match=f"outside {lo}-{hi}"):
                steps.check_total(d, bad)


def test_clamp_target_only_and_mismatch(world):
    with pytest.raises(service.PickRefused, match="outside 8-35"):
        give(world, target_min=40)
    with pytest.raises(service.PickRefused, match="differs"):
        give(world, target_min=16, phase_budget={"understand": 2, "approach": 5, "code": 8})
    h = give(world, target_min=20)
    assert handout(h.handout_id).phase_budget == steps.split(20)


def test_clamp_through_the_tool(world):
    ctx = tools.Ctx(world)
    r = tools.hand_out(ctx, num("heap", "E"), phase_budget={"understand": 9, "approach": 9,
                                                            "code": 9})
    assert "outside 5-20" in r["error"]


# ---- steps: transitions, per-step time with pauses, checkpoints ----------------------------
def test_understood_button_moves_to_approach_and_tells_the_coach(world):
    h = give(world, phase_budget={"understand": 2, "approach": 5, "code": 8})
    back(h.handout_id, 70)
    r = service.understood(world, h.handout_id)
    assert r["phase"] == "approach" and 69 <= r["understand_s"] <= 72 and not r["already"]
    ev = events(world, "understood")
    assert len(ev) == 1 and ev[0]["handout_id"] == h.handout_id
    assert ev[0]["understand_budget_min"] == 2
    st = service.clock_state(world, h.handout_id)["steps"]
    assert st["current"] == "approach" and st["index"] == 2
    assert st["done"][0]["label"] == "Understood"
    again = service.understood(world, h.handout_id)
    assert again["already"] and len(events(world, "understood")) == 1


def test_coach_cannot_end_understand_without_force(world):
    h = give(world)
    with pytest.raises(ValueError, match="force=true only if he said"):
        service.clock(world, h.handout_id, "phase", label="approach")
    with pytest.raises(ValueError, match="force"):
        service.clock(world, h.handout_id, "phase", label="code")
    with pytest.raises(ValueError, match="one of understand"):
        service.clock(world, h.handout_id, "phase", label="debug")
    assert handout(h.handout_id).phase_label == "understand"
    st = service.clock(world, h.handout_id, "phase", label="approach", force=True)
    assert st["steps"]["current"] == "approach"
    # and the tool passes force through; going back needs it too
    ctx = tools.Ctx(world)
    assert "force" in tools.clock(ctx, "phase", label="understand")["error"]


def test_unlock_moves_to_code_lock_moves_back(world):
    h = give(world)
    service.understood(world, h.handout_id)
    service.unlock_editor(world, h.handout_id)
    assert handout(h.handout_id).phase_label == "code"
    service.lock_editor(world, h.handout_id)
    assert handout(h.handout_id).phase_label == "approach"
    # unlocking straight from understand does not skip his step
    h2_id = h.handout_id
    with db.session_scope() as s:
        s.get(Handout, h2_id).phase_label = "understand"
    service.unlock_editor(world, h2_id)
    assert handout(h2_id).phase_label == "understand"


def test_per_step_seconds_exclude_pauses():
    t0 = datetime(2026, 10, 21, 9, 0, 0)
    h = SimpleNamespace(handed_at=t0, clock_state="running", clock_anchor=t0, clock_accum_s=0,
                        first_started_at=t0, target_min=15, phase_label="understand",
                        phase_budget={"understand": 2, "approach": 5, "code": 8},
                        phase_elapsed={}, phase_start_s=0, phase_fired=[])
    steps.move(h, "approach", t0 + timedelta(seconds=100))
    # pause at 160 s for 10 minutes, then resume
    h.clock_accum_s, h.clock_state = 160, "paused"
    h.clock_anchor = t0 + timedelta(seconds=160)
    assert steps.seconds(h, t0 + timedelta(minutes=9))["approach"] == 60
    h.clock_state, h.clock_anchor = "running", t0 + timedelta(seconds=760)
    secs = steps.seconds(h, t0 + timedelta(seconds=790))
    assert secs == {"understand": 100, "approach": 90, "code": 0}


def test_pause_through_service_freezes_the_step(world):
    h = give(world)
    service.understood(world, h.handout_id)
    back(h.handout_id, 60)
    service.clock(world, h.handout_id, "pause", reason="aside")
    s1 = service.clock_state(world, h.handout_id)["steps"]["seconds"]["approach"]
    back(h.handout_id, 300)                         # time passes while paused
    s2 = service.clock_state(world, h.handout_id)["steps"]["seconds"]["approach"]
    assert s1 == s2 and 55 <= s1 <= 65


def test_reset_zeroes_steps(world):
    h = give(world)
    back(h.handout_id, 90)
    service.understood(world, h.handout_id)
    service.clock(world, h.handout_id, "reset", reason="restart clean")
    st = service.clock_state(world, h.handout_id)["steps"]
    assert st["seconds"]["understand"] == 0 and st["current"] == "approach"


def test_target_change_adjusts_the_code_step(world):
    h = give(world, phase_budget={"understand": 2, "approach": 5, "code": 8})
    service.clock(world, h.handout_id, "target", target_min=18)
    assert handout(h.handout_id).phase_budget == {"understand": 2, "approach": 5, "code": 11}
    with pytest.raises(ValueError, match="no time for the code step"):
        service.clock(world, h.handout_id, "target", target_min=7)


def test_per_step_checkpoint_fires_once(world):
    h = give(world, phase_budget={"understand": 2, "approach": 5, "code": 8})
    assert service._fire_checkpoints(world) is None
    back(h.handout_id, 125)                          # understand past its 2 min
    assert service._fire_checkpoints(world)
    assert service._fire_checkpoints(world) is None  # once
    cps = [p for p in events(world, "clock_checkpoint") if p.get("phase")]
    assert cps == [cps[0]] and cps[0]["phase"] == "understand" and cps[0]["pct"] == 100
    assert cps[0]["phase_budget_min"] == 2 and cps[0]["mode"] == "coached"
    service.understood(world, h.handout_id)
    back(h.handout_id, 400)                          # approach past 5 min
    service._fire_checkpoints(world)
    phases = [p["phase"] for p in events(world, "clock_checkpoint") if p.get("phase")]
    assert phases == ["understand", "approach"]
    # the total checkpoints still fire (50 / 100 / 150 % of 15 min)
    assert any(p.get("pct") == 50 and "phase" not in p for p in events(world, "clock_checkpoint"))


def test_idle_fires_once_and_rearms(world):
    h = give(world)
    assert service._fire_idle(world) is None
    back(h.handout_id, 200)
    eid = service._fire_idle(world)
    assert eid
    ev = events(world, "idle")
    assert len(ev) == 1 and ev[0]["phase"] == "understand" and ev[0]["idle_s"] >= 180
    assert service._fire_idle(world) is None             # once per quiet stretch
    service.push_event(world, "chat", {"handout_id": h.handout_id, "text": "hm"})
    assert service._fire_idle(world) is None             # he just did something
    with db.session_scope() as s:                        # ... 3+ minutes ago
        e = s.query(Event).filter_by(kind="chat").one()
        e.created_at -= timedelta(seconds=200)
    assert service._fire_idle(world)
    assert len(events(world, "idle")) == 2
    service.clock(world, h.handout_id, "pause", reason="aside")
    with db.session_scope() as s:
        s.get(Handout, h.handout_id).idle_mark = None
    assert service._fire_idle(world) is None             # never while paused


def test_idle_counts_a_draft_change(world):
    h = give(world)
    back(h.handout_id, 400)
    service.save_draft(world, h.handout_id, "x = 1", 1)
    assert service._fire_idle(world) is None


# ---- problem ratings ------------------------------------------------------------------------
def test_rate_problem_band_and_once(world):
    n = num("heap", "M")
    with pytest.raises(service.PickRefused, match="outside 1200-1900"):
        service.rate_problem(world, n, 1100, "too easy for M")
    with pytest.raises(service.PickRefused, match="why"):
        service.rate_problem(world, n, 1500, "")
    r = service.rate_problem(world, n, 1500, "two ideas stacked")
    assert r == {"number": n, "rating": 1500, "why": "two ideas stacked"}
    with pytest.raises(service.PickRefused, match="already rated 1500"):
        service.rate_problem(world, n, 1600, "changed my mind")
    with pytest.raises(service.PickRefused, match="outside 800-1300"):
        service.rate_problem(world, num("heap", "E"), 1400, "x")
    with pytest.raises(service.PickRefused, match="outside 1700-2600"):
        service.rate_problem(world, num("heap", "H"), 2700, "x")
    rows = {p["number"]: p for p in service.find_problems(world, pattern="heap")}
    assert rows[n]["rating"] == 1500 and rows[n]["rated"]
    assert rows[num("heap", "H")]["rating"] == 1800 and not rows[num("heap", "H")]["rated"]
    ctx = tools.Ctx(world)
    assert "already rated" in tools.rate_problem(ctx, n, 1400, "again")["error"]


def test_mmr_uses_the_problem_rating(world):
    n = num("heap", "M")
    service.rate_problem(world, n, 1900, "hard medium")
    h = service.hand_out(world, n, today=TODAY)
    r = service.log_attempt(world, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                            derived_cold=True, day=TODAY)
    with db.session_scope() as s:
        ev = s.query(RatingEvent).filter_by(attempt_id=r["attempt_id"]).one()
        assert ev.problem_rating == 1900
        assert abs(ev.expected - rating.expected(1200, 1900)) < 1e-4


# ---- MMR math -------------------------------------------------------------------------------
def A(**kw):
    base = dict(outcome="clean", bugs=0, hints=0, optimal=True, derived_cold=True,
                actual_min=10, target_min=15)
    base.update(kw)
    return SimpleNamespace(**base)


def test_expected_and_ranks():
    assert rating.expected(1400, 1400) == 0.5
    assert abs(rating.expected(1200, 1400) - 0.2403) < 1e-3
    assert abs(rating.expected(1200, 1000) - 0.7597) < 1e-3
    assert [rating.rank(x) for x in (1199.9, 1200, 1399, 1400, 1600, 1799, 1800)] == [
        "Bronze", "Silver", "Silver", "Gold", "Platinum", "Platinum", "Diamond"]
    assert rating.next_rank_at(1250) == {"rank": "Gold", "mmr": 1400}
    assert rating.next_rank_at(1900) is None
    assert rating.k_factor(0) == 40 and rating.k_factor(19) == 40 and rating.k_factor(20) == 20


def test_score_parts():
    full = {"budget": {"understand": 2, "approach": 5, "code": 8},
            "seconds": {"understand": 100, "approach": 280, "code": 400}}
    act, bd = rating.score(A(), full, "Accepted", 0)
    assert act == 1.0 and bd["raw"] == 1.0
    # understand over budget: -0.10
    act, _ = rating.score(A(), {**full, "seconds": {**full["seconds"], "understand": 130}},
                          "Accepted", 0)
    assert abs(act - 0.90) < 1e-9
    # approach at 1.5x budget: half of 0.25
    act, bd = rating.score(A(), {**full, "seconds": {**full["seconds"], "approach": 450}},
                           "Accepted", 0)
    assert bd["parts"]["approach"]["score"] == 0.5 and abs(act - 0.875) < 1e-9
    # not his own approach: 0 for approach whatever the time
    act, _ = rating.score(A(derived_cold=False), full, "Accepted", 0)
    assert abs(act - 0.75) < 1e-9
    # first judged submission wrong: -0.30
    act, _ = rating.score(A(), full, "Wrong Answer", 0)
    assert abs(act - 0.70) < 1e-9
    # lint hits on the accepted code, or not optimal: -0.15
    assert abs(rating.score(A(), full, "Accepted", 2)[0] - 0.85) < 1e-9
    assert abs(rating.score(A(optimal=False), full, "Accepted", 0)[0] - 0.85) < 1e-9
    # total time: 1.0 at target, 0.5 at 1.5x, 0 at 2x
    assert rating.score(A(actual_min=15), full, "Accepted", 0)[0] == 1.0
    assert abs(rating.score(A(actual_min=30), full, "Accepted", 0)[0] - 0.80) < 1e-9


def test_score_hints_floor_abandoned_and_fallbacks():
    full = {"budget": {"understand": 2, "approach": 5, "code": 8},
            "seconds": {"understand": 60, "approach": 60, "code": 60}}
    assert abs(rating.score(A(hints=1), full, "Accepted", 0)[0] - 0.9) < 1e-9
    assert abs(rating.score(A(hints=5), full, "Accepted", 0)[0] - 0.7) < 1e-9   # max -0.3
    worst = A(derived_cold=False, optimal=False, actual_min=40, hints=3)
    assert rating.score(worst, None, "Wrong Answer", 3)[0] == 0.0               # floor 0
    assert rating.score(A(outcome="abandoned"), full, "Accepted", 0)[0] == 0.0
    # fallbacks without steps / judge: total time, bugs, optimal
    act, bd = rating.score(A(bugs=1, actual_min=10))
    assert bd["parts"]["first_ac"]["from"] == "bugs" and bd["parts"]["first_ac"]["score"] == 0
    assert bd["parts"]["understand"]["from"] == "total time" and abs(act - 0.70) < 1e-9
    act, _ = rating.score(A(actual_min=25))            # 1.67x: understand 0, time .33
    assert abs(act - (0 + 0.25 * (2 - 25 / 15) + 0.30 + 0.15 + 0.20 * (2 - 25 / 15))) < 1e-9


def test_log_applies_once_and_k_switches_at_20(world):
    nums = [num(s, d) for s in ("heap", "two-pointers", "sliding-window", "trees-dfs",
                                "trees-bfs", "grid-bfs", "linked-list") for d in "EMH"]
    ks = []
    for i, n in enumerate(nums[:21]):
        r = service.log_attempt(world, number=n, actual_min=5, target_min=10, bugs=0,
                                day=TODAY - timedelta(days=30 - i))
        with db.session_scope() as s:
            ks.append(s.query(RatingEvent).filter_by(attempt_id=r["attempt_id"]).one().k)
    assert ks[:20] == [40] * 20 and ks[20] == 20
    g = service.get_rating(world)
    assert g["games"] == 21 and len(g["recent_deltas"]) == 10


def test_idempotent_per_attempt(world):
    h = give(world)
    r1 = service.log_attempt(world, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                             derived_cold=True)
    r2 = service.log_attempt(world, handout_id=h.handout_id, bugs=3, hints=0, optimal=True,
                             derived_cold=True)
    assert r2["already_logged"]
    with db.session_scope() as s:
        assert s.query(RatingEvent).count() == 1
        a = s.get(Attempt, r1["attempt_id"])
        ev = rating.apply(s, a)                          # a second apply changes nothing
        assert s.query(RatingEvent).count() == 1 and s.get(Rating, world).games == 1
        assert ev.attempt_id == a.id


def test_live_inputs_first_judged_submission_and_lint(world):
    h = give(world, phase_budget={"understand": 2, "approach": 5, "code": 8})
    service.understood(world, h.handout_id)
    service.unlock_editor(world, h.handout_id)
    with db.session_scope() as s:
        s.add(Submission(user_id=world, handout_id=h.handout_id, code="x", verdict="Wrong Answer"))
        s.add(Submission(user_id=world, handout_id=h.handout_id,
                         code="class Solution:\n    def f(self, a):\n        return a\n",
                         verdict="Accepted"))
    r = service.log_attempt(world, handout_id=h.handout_id, bugs=1, hints=0, optimal=True,
                            derived_cold=True)
    rv = service.reveal(h.handout_id)
    parts = rv["rating"]["breakdown"]["parts"]
    assert parts["first_ac"] == {"weight": 0.3, "score": 0.0, "points": 0.0, "from": "judge"}
    assert parts["quality"]["from"] == "lint" and parts["quality"]["score"] == 1.0
    assert parts["understand"]["from"] == "step" and parts["approach"]["from"] == "step"
    assert [x["phase"] for x in rv["steps"]] == ["understand", "approach", "code"]
    assert rv["rating"]["before"] == 1200 and r["rating"]["after"] == rv["rating"]["after"]


def test_abandoned_scores_zero(world):
    h = give(world)
    service.log_attempt(world, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                        derived_cold=True, abandoned=True)
    rt = service.reveal(h.handout_id)["rating"]
    assert rt["actual"] == 0 and rt["delta"] < 0 and rt["breakdown"]["abandoned"]


def test_rank_up_moment(world):
    with db.session_scope() as s:
        s.add(Rating(user_id=world, mmr=1395.0, games=30, updated_at=datetime.now()))
    h = give(world)
    service.log_attempt(world, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                        derived_cold=True)
    rt = service.reveal(h.handout_id)["rating"]
    assert rt["rank_before"] == "Silver" and rt["rank"] == "Gold" and rt["rank_up"]


# ---- placement ------------------------------------------------------------------------------
def _seed_history(path):
    db.init(str(path))
    uid = service.create_user("old", "pw")
    from coach.models import Pattern
    with db.session_scope() as s:
        s.add(Pattern(slug="heap", name="Heap", tier=1))
        for n, d in ((1, "E"), (2, "M"), (3, "H")):
            s.add(Problem(number=n, title=f"p{n}", lc_slug=f"p{n}", difficulty=d,
                          pattern_slug="heap", source="bank"))
        s.flush()
        rows = [(3, "solved", 2, 1, 40, 35), (1, "clean", 0, 0, 8, 10), (2, "clean", 0, 0, 12, 15),
                (2, "solved", 1, 0, 25, 15), (1, "abandoned", 0, 0, 20, 10)]
        for i, (n, oc, b, hi, act, tgt) in enumerate(rows):
            s.add(Attempt(user_id=uid, problem_number=n, pattern_slug="heap",
                          day=date(2026, 9, 1) + timedelta(days=i), target_min=tgt,
                          actual_min=act, bugs=b, hints=hi, optimal=True,
                          derived_cold=hi == 0, outcome=oc, source="import"))
        s.query(Rating).delete()
        s.query(RatingEvent).delete()
    return uid


def test_placement_replay_deterministic(tmp_path):
    uid = _seed_history(tmp_path / "a.db")
    db.init(str(tmp_path / "a.db"))                    # the migration places him
    with db.session_scope() as s:
        evs = [(e.before, e.after, e.k, e.breakdown.get("replayed"))
               for e in s.query(RatingEvent).order_by(RatingEvent.id)]
        mmr = s.get(Rating, uid).mmr
    assert len(evs) == 5 and all(r for *_, r in evs) and evs[0][0] == 1200
    db.init(str(tmp_path / "a.db"))                    # idempotent
    with db.session_scope() as s:
        assert s.query(RatingEvent).count() == 5 and s.get(Rating, uid).mmr == mmr
    uid2 = _seed_history(tmp_path / "b.db")
    db.init(str(tmp_path / "b.db"))
    with db.session_scope() as s:
        assert s.get(Rating, uid2).mmr == mmr          # same history, same MMR
        # abandoned (last) is a 0 score
        last = s.query(RatingEvent).order_by(RatingEvent.id.desc()).first()
        assert last.actual == 0


def test_profile_and_rating_tool(world):
    p = service.get_profile(world, today=TODAY)
    assert p["mmr"] == 1200 and p["rank"] == "Silver" and p["games"] == 0
    assert p["next_rank_at"] == {"rank": "Gold", "mmr": 1400} and p["recent_deltas"] == []
    assert p["default_budget"]["M"] == {"understand": 3, "approach": 5, "code": 9}
    r = tools.get_rating(tools.Ctx(world))
    assert r["rank"] == "Silver" and r["to_next"] == 200


# ---- feedback channel -----------------------------------------------------------------------
def test_request_feature_dedupe(world):
    a = service.request_feature(world, "Draw a two-row table", why="trace",
                                severity="workaround", tool="show_visual")
    b = service.request_feature(world, "  draw a TWO-row table! ", why="again",
                                severity="blocked", tool="show_visual")
    c = service.request_feature(world, "Draw a two-row table", why="x", tool="scratch")
    assert a["new"] and not b["new"] and b["id"] == a["id"] and b["count"] == 2
    assert c["new"] and c["id"] != a["id"]
    rows = service.list_requests()["rows"]
    assert rows[0]["id"] == a["id"] and rows[0]["severity"] == "blocked"   # raised, ranked first


def test_request_feature_refuses_code_and_cuts_example(world):
    with pytest.raises(ValueError, match="no code"):
        service.request_feature(world, "t", example="```python\nx = 1\n```")
    with pytest.raises(ValueError, match="no code"):
        service.request_feature(world, "t", example="def f(a):\n    return a[0]\n")
    with pytest.raises(ValueError, match="severity"):
        service.request_feature(world, "t", severity="urgent")
    r = service.request_feature(world, "long", example="words " * 200)
    with db.session_scope() as s:
        assert len(s.get(CoachRequest, r["id"]).example) == 400
    ctx = tools.Ctx(world)
    assert "no code" in tools.request_feature(ctx, "t", "w", example="```x```")["error"]


def test_failed_tool_calls_become_friction(world):
    ctx = tools.Ctx(world)
    t = tools.TOOLS["show_visual"]                     # no open problem -> {error}
    asyncio.run(t.call(ctx, {"kind": "array", "spec": {"values": [1]}}))
    asyncio.run(t.call(ctx, {"kind": "array", "spec": {"values": [2]}}))
    with db.session_scope() as s:
        rows = s.query(CoachRequest).filter_by(kind="friction").all()
        assert len(rows) == 1 and rows[0].tool == "show_visual" and rows[0].count == 2
        assert "no open problem" in rows[0].why

    def boom(ctx):
        raise RuntimeError("kaput")
    tools.TOOLS["_boom"] = tools.Tool("_boom", boom, "")
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(tools.TOOLS["_boom"].call(ctx, {}))
    finally:
        tools.TOOLS.pop("_boom")
    with db.session_scope() as s:
        assert s.query(CoachRequest).filter_by(tool="_boom").one().why == "RuntimeError: kaput"


def test_owner_is_admin_by_migration(tmp_path):
    db.init(str(tmp_path / "c.db"))
    service.create_user("chandan", "pw")
    service.create_user("someone", "pw")
    db.init(str(tmp_path / "c.db"))
    with db.session_scope() as s:
        flags = {u.username: u.is_admin for u in s.query(User)}
    assert flags == {"chandan": True, "someone": None}


# ---- web ------------------------------------------------------------------------------------
@pytest.fixture
def web(world, monkeypatch):
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    service.set_plan(world, today=TODAY, daily_problems=2, focus_patterns=["heap"])
    webapp.app.state.service = Svc()
    c = TestClient(webapp.app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "tester", "password": "pw-for-tests", "csrf": tok})
    yield c, world
    webapp.app.state.service = None


def _csrf(c, path):
    return re.search(r'X-CSRF-Token": "([^"]+)"', c.get(path).text).group(1)


def test_solve_page_button_flow(web):
    c, uid = web
    h = service.hand_out(uid, num("heap"), phase_budget={"understand": 2, "approach": 5,
                                                         "code": 8})
    service.rate_problem(uid, num("heap"), 1550, "fixture")
    html = c.get("/solve").text
    assert 'data-step="understand"' in html and 'id="understood-btn"' in html
    assert "I understand the question" in html and "has-steps" in html
    assert "1550" not in html
    assert_clean(html.replace(h.title, ""))
    tok = _csrf(c, "/solve")
    r = c.post("/solve/understood", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200 and r.json()["phase"] == "approach"
    assert r.json()["clock"]["steps"]["current"] == "approach"
    assert events(uid, "understood")
    st = c.get("/handout/state").json()["clock"]["steps"]
    assert st["current"] == "approach" and st["done"][0]["label"] == "Understood"
    assert 'data-step="approach"' in c.get("/solve").text
    assert c.post("/solve/understood").status_code == 403          # csrf
    log = c.get("/log").text
    assert_clean(log.replace(h.title, ""))
    assert "1550" not in log


def test_reveal_and_dashboard_show_rating(web):
    c, uid = web
    h = service.hand_out(uid, num("heap"), phase_budget={"understand": 2, "approach": 5,
                                                         "code": 8},
                         budget_reason="more approach time at level 2")
    service.understood(uid, h.handout_id)
    service.log_attempt(uid, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                        derived_cold=True)
    page = c.get(f"/reveal/{h.handout_id}").text
    assert 'id="rv-rating"' in page and "1200" in page and "First submit accepted" in page
    assert "Expected" in page and "Understand the question" in page
    assert "more approach time at level 2" in page
    dash = c.get("/dashboard").text
    assert 'id="rank-card"' in dash and "rank-silver" in dash and "Next rank" in dash
    assert "<svg class=\"spark\"" in dash
    assert "/feedback" not in dash                                   # not an admin


def test_feedback_page_admin_only(web):
    c, uid = web
    service.request_feature(uid, "Draw a two-row table", why="w", severity="blocked")
    assert c.get("/feedback").status_code == 404
    with db.session_scope() as s:
        s.get(User, uid).is_admin = True
    page = c.get("/feedback").text
    assert "Draw a two-row table" in page and "blocked" in page
    assert "Feedback" in c.get("/dashboard").text
    rid = service.list_requests()["rows"][0]["id"]
    tok = _csrf(c, "/dashboard")
    r = c.post(f"/feedback/{rid}/status", data={"status": "planned", "csrf": tok})
    assert r.status_code == 303 and service.list_requests()["rows"][0]["status"] == "planned"
    assert "Draw" not in c.get("/feedback?status=done").text.split("Requests")[1]
    assert c.post(f"/feedback/{rid}/status", data={"status": "nope", "csrf": tok}).status_code == 400


# ---- a paused clock on the page (2026-10-22: paused on the server, his page kept counting) -
def _paused_stepped(uid):
    h = service.hand_out(uid, num("heap"), phase_budget={"understand": 2, "approach": 5,
                                                         "code": 8})
    service.understood(uid, h.handout_id)
    back(h.handout_id, 90)
    service.clock(uid, h.handout_id, "pause", reason="he asked for a break")
    return h


def test_solve_renders_a_paused_clock_paused_in_both_places(web):
    c, uid = web
    h = _paused_stepped(uid)
    html = c.get("/solve").text
    timer = re.search(r'<div class="clock[^"]*" id="timer"[^>]*>', html).group(0)
    root = re.search(r'<section class="solve" id="solve"[^>]*>', html).group(0)
    assert 'data-run="paused"' in timer and 'data-run="paused"' in root
    steps_json = re.search(r"data-steps='([^']*)'", timer).group(1)
    import html as _html
    st = json.loads(_html.unescape(steps_json))
    assert st["current"] == "approach" and 85 <= st["seconds"]["approach"] <= 95
    paused_chip = re.search(r'<span class="clock-paused" id="t-paused"[^>]*>', html).group(0)
    assert " hidden" not in paused_chip
    # running again: both say running, the chip is hidden
    service.clock(uid, h.handout_id, "resume", reason="back")
    html = c.get("/solve").text
    assert html.count('data-run="running"') == 2
    assert re.search(r'<span class="clock-paused" id="t-paused"[^>]*\bhidden', html)


def test_handout_state_paused_stepped_clock_is_frozen(web):
    c, uid = web
    h = _paused_stepped(uid)
    st1 = c.get("/handout/state").json()["clock"]
    assert st1["state"] == "paused" and st1["pause_reason"] == "he asked for a break"
    assert st1["steps"]["current"] == "approach"
    back(h.handout_id, 600)                          # ten minutes pass while paused
    st2 = c.get("/handout/state").json()["clock"]
    assert st2["state"] == "paused"
    assert st2["elapsed_s"] == st1["elapsed_s"]
    assert st2["steps"]["seconds"] == st1["steps"]["seconds"]
    service.clock(uid, h.handout_id, "resume", reason="back")
    assert c.get("/handout/state").json()["clock"]["state"] == "running"
    service.clock(uid, h.handout_id, "pause", reason="again")
    service.clock(uid, h.handout_id, "reset", reason="from scratch")
    st3 = c.get("/handout/state").json()["clock"]
    assert st3["state"] == "running" and st3["elapsed_s"] < 5
    assert st3["steps"]["seconds"]["approach"] < 5


def test_stream_pings_are_events_and_retry_is_short(web, monkeypatch):
    c, uid = web
    monkeypatch.setattr(webapp, "STREAM_PING_S", 0.05)
    monkeypatch.setattr(webapp, "STREAM_POLL_S", 0.02)
    body = c.get("/stream?max_s=0.4").text.replace("\r\n", "\n")
    assert body.startswith(f"retry: {webapp.STREAM_RETRY_MS}\n")
    assert "event: ping\ndata: {}\n\n" in body and not re.search(r"^: ping", body, re.M)
    for block in body.split("\n\n"):              # pings carry no id: Last-Event-ID stays put
        if "event: ping" in block:
            assert "id:" not in block


def test_live_js_has_watchdog_and_parks_hidden_tabs():
    from pathlib import Path
    js = (Path(webapp.__file__).parent / "static" / "live.js").read_text(encoding="utf-8")
    assert '"ping"' in js and "WATCHDOG_MS" in js and "PARK_MS" in js
    assert "pageshow" in js and "persisted" in js
    steps_js = (Path(webapp.__file__).parent / "static" / "steps.js").read_text(
        encoding="utf-8")
    assert "step-paused" in steps_js


def test_ui_events_pruned_inside_a_caller_transaction(world):
    from coach.models import UiEvent
    for _ in range(5):
        service.emit(world, "clock", 1)
    with db.session_scope() as s:
        for e in s.query(UiEvent):
            e.created_at = datetime.now() - timedelta(hours=2)
    with db.session_scope() as s:                  # the path every coach write takes
        service.emit(world, "clock", 1, s)
    with db.session_scope() as s:
        assert s.query(UiEvent).count() == 1
