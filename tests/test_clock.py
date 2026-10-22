"""The coach's clock: math, the clock_events record, logging, retry targets, checkpoints, the
bridge tools and the web state."""
import asyncio
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from coach import clock as clk
from coach import db, picker, service
from coach.models import Attempt, ClockEvent, Event, Handout, UiEvent
from coach.web import app as webapp
from coach.web.svc import Svc
from tests.test_web import assert_clean

T0 = datetime(2026, 10, 21, 9, 0, 0)


def _h(**kw):
    base = dict(handed_at=T0, clock_state=None, clock_anchor=None, clock_accum_s=None,
                first_started_at=None, phase_label=None, target_min=10, checkpoints_fired=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def opened(world):
    h = service.next_problem(world)
    return world, h.handout_id


def _back(hid, minutes):
    """Pretend the handout (and its clock) started `minutes` ago."""
    with db.session_scope() as s:
        h = s.get(Handout, hid)
        t = datetime.now() - timedelta(minutes=minutes)
        h.handed_at = h.clock_anchor = h.first_started_at = t
        for e in s.query(ClockEvent).filter_by(handout_id=hid):
            e.at = t


def _events(hid):
    with db.session_scope() as s:
        return [(e.action, e.reason, e.value) for e in
                s.query(ClockEvent).filter_by(handout_id=hid).order_by(ClockEvent.id)]


def _ui(uid):
    with db.session_scope() as s:
        return [(e.kind, e.ref_id) for e in s.query(UiEvent).filter_by(user_id=uid)]


# ---- pure math -----------------------------------------------------------------------------
def test_old_rows_are_running_since_handed_at():
    h = _h()
    now = T0 + timedelta(minutes=7)
    assert clk.elapsed_s(h, now) == 420 and clk.wall_s(h, now) == 420
    st = clk.state(h, now)
    assert st["state"] == "running" and st["accum_s"] == 0 and st["pauses"] == 0


def test_pause_banks_resume_continues_reset_zeroes_accum_not_wall():
    h = _h(clock_state="paused", clock_accum_s=300, clock_anchor=T0 + timedelta(minutes=5),
           first_started_at=T0)
    now = T0 + timedelta(minutes=20)
    assert clk.elapsed_s(h, now) == 300                 # frozen while paused
    assert clk.wall_s(h, now) == 1200                   # wall keeps counting
    h.clock_state, h.clock_anchor = "running", T0 + timedelta(minutes=15)
    assert clk.elapsed_s(h, now) == 600                 # 5 banked + 5 since resume
    h.clock_accum_s, h.clock_anchor = 0, now
    assert clk.elapsed_s(h, now + timedelta(seconds=30)) == 30
    assert clk.wall_s(h, now + timedelta(seconds=30)) == 1230


def test_record_and_note():
    evs = [{"action": "start", "at": T0, "reason": "handout"},
           {"action": "pause", "at": T0 + timedelta(minutes=2), "reason": "float precision"},
           {"action": "pause", "at": T0 + timedelta(minutes=3), "reason": "again"},  # no-op
           {"action": "resume", "at": T0 + timedelta(minutes=6), "reason": ""},
           {"action": "reset", "at": T0 + timedelta(minutes=7), "reason": "restart clean"},
           {"action": "pause", "at": T0 + timedelta(minutes=8), "reason": "aside"}]
    rec = clk.record(evs, T0 + timedelta(minutes=9))
    assert [(p["reason"], p["minutes"]) for p in rec["pauses"]] == [("float precision", 4),
                                                                     ("aside", 1)]
    assert [r["reason"] for r in rec["resets"]] == ["restart clean"]
    assert clk.note(rec) == "2 pauses (float precision 4 min; aside 1 min), 1 reset"
    assert clk.note({"pauses": [], "resets": []}) == ""


# ---- service.clock -------------------------------------------------------------------------
def test_handout_starts_the_clock(opened):
    uid, hid = opened
    assert _events(hid)[0][0] == "start"
    st = service.clock_state(uid, hid)
    assert st["state"] == "running" and st["elapsed_s"] < 5 and st["target_min"]


@pytest.mark.parametrize("action,kw", [
    ("start", {}), ("pause", {"reason": "teaching floats"}), ("resume", {}),
    ("reset", {"reason": "restart"}), ("target", {"target_min": 12}),
    ("phase", {"label": "approach"}),
])
def test_every_action_records_and_emits(opened, action, kw):
    uid, hid = opened
    n = len(_events(hid))
    before = len(_ui(uid))
    st = service.clock(uid, hid, action, **kw)
    evs = _events(hid)
    assert len(evs) == n + 1 and evs[-1][0] == action
    assert ("clock", hid) in _ui(uid)[before:]
    assert st["handout_id"] == hid and "elapsed_s" in st and "wall_s" in st


def test_pause_resume_reset_through_service(opened):
    uid, hid = opened
    _back(hid, 10)
    st = service.clock(uid, hid, "pause", reason="float precision")
    assert st["state"] == "paused" and 595 <= st["elapsed_s"] <= 605
    assert st["pause_reason"] == "float precision" and "warning" not in st
    with db.session_scope() as s:                      # 3 min pass while paused
        s.get(Handout, hid).clock_anchor -= timedelta(minutes=3)
    st = service.clock_state(uid, hid)
    assert st["state"] == "paused" and 595 <= st["elapsed_s"] <= 605
    st = service.clock(uid, hid, "resume")
    assert st["state"] == "running" and 595 <= st["elapsed_s"] <= 606
    wall = st["wall_s"]
    st = service.clock(uid, hid, "reset", reason="opened before he began")
    assert st["elapsed_s"] < 2 and st["wall_s"] >= wall and st["resets"] == 1
    with db.session_scope() as s:
        h = s.get(Handout, hid)
        assert h.first_started_at < h.clock_anchor


def test_pause_without_reason_warns(opened):
    uid, hid = opened
    st = service.clock(uid, hid, "pause")
    assert st["warning"] == "say why" and _events(hid)[-1][1] == ""


def test_target_validated_and_set(opened):
    uid, hid = opened
    assert service.clock(uid, hid, "target", target_min=20)["target_min"] == 20
    for bad in (0, 181, None, "x", True):
        with pytest.raises(ValueError):
            service.clock(uid, hid, "target", target_min=bad)
    with pytest.raises(ValueError):
        service.clock(uid, hid, "rewind")


def test_phase_label_leak_guard(opened):
    uid, hid = opened
    n = len(_events(hid))
    with pytest.raises(ValueError, match="leak"):
        service.clock(uid, hid, "phase", label="sliding window")
    with pytest.raises(ValueError, match="leak"):
        service.clock(uid, hid, "phase", label="hard part")
    assert len(_events(hid)) == n
    assert service.clock(uid, hid, "phase", label="debug")["phase"] == "debug"
    assert service.clock(uid, hid, "phase", label="")["phase"] is None


def test_clock_refuses_closed_or_foreign_handout(opened):
    uid, hid = opened
    with pytest.raises(ValueError):
        service.clock(uid + 99, hid, "pause", reason="x")
    service.log_attempt(uid, handout_id=hid, actual_min=3)
    with pytest.raises(ValueError):
        service.clock(uid, hid, "pause", reason="x")


def test_reset_timer_is_a_clock_reset(opened):
    uid, hid = opened
    r = service.reset_timer(uid, hid)
    assert r["ok"] and _events(hid)[-1][:2] == ("reset", "reset")


# ---- logging -------------------------------------------------------------------------------
def test_log_uses_coach_clock_and_stores_wall_and_note(opened):
    uid, hid = opened
    _back(hid, 30)
    now = datetime.now()
    service.clock(uid, hid, "pause", reason="float precision", now=now - timedelta(minutes=10))
    res = service.log_attempt(uid, handout_id=hid, bugs=0)
    assert res["actual_min"] == 20 and res["wall_min"] == 30
    assert res["clock_note"] == "1 pause (float precision 10 min)"
    with db.session_scope() as s:
        a = s.get(Attempt, res["attempt_id"])
        assert (a.actual_min, a.wall_min, a.clock_note) == (20, 30, res["clock_note"])
    # outcome from the coach clock: 20 min vs target; wall time does not count
    with db.session_scope() as s:
        target = s.get(Handout, hid).target_min
    assert res["outcome"] == ("clean" if 20 <= 1.5 * target else "solved")


def test_outcome_uses_coach_minutes_not_wall(opened):
    uid, hid = opened
    with db.session_scope() as s:
        target = s.get(Handout, hid).target_min
    _back(hid, int(target * 3))                          # wall: 3x target
    now = datetime.now()
    service.clock(uid, hid, "pause", reason="long lesson",
                  now=now - timedelta(minutes=int(target * 3) - 2))
    res = service.log_attempt(uid, handout_id=hid, bugs=0)
    assert res["actual_min"] == 2 and res["outcome"] == "clean"
    assert res["wall_min"] >= target * 3 - 1


def test_explicit_actual_min_still_wins(opened):
    uid, hid = opened
    assert service.log_attempt(uid, handout_id=hid, actual_min=7)["actual_min"] == 7


def test_reveal_and_summary_carry_both_times(opened):
    uid, hid = opened
    _back(hid, 15)
    now = datetime.now()
    service.clock(uid, hid, "pause", reason="aside", now=now - timedelta(minutes=5))
    service.clock(uid, hid, "resume", now=now - timedelta(minutes=3))
    service.log_attempt(uid, handout_id=hid)
    ck = service.reveal(hid)["clock"]
    assert ck["coach_min"] == 13 and ck["wall_min"] == 15       # 10 + 3 after resume
    assert [(p["reason"], p["minutes"]) for p in ck["pauses"]] == [("aside", 2)]
    assert ck["resets"] == []
    p = service.session_summary(uid)["problems"][0]
    assert p["clock"]["coach_min"] == 13 and p["attempt"]["wall_min"] == 15


# ---- retry targets -------------------------------------------------------------------------
def test_retry_target_is_80_percent_of_first(world):
    with db.session_scope() as s:
        from coach.models import Problem
        p = s.get(Problem, 1001)                         # difficulty M -> default 15
        assert picker.target_for(s, world, p) == 15
        for target, day in ((20, 1), (30, 5)):           # the FIRST attempt counts
            s.add(Attempt(user_id=world, problem_number=1001, day=datetime(2026, 9, day).date(),
                          target_min=target, actual_min=5, outcome="clean"))
        s.flush()
        assert picker.target_for(s, world, p) == 16
        s.add(Attempt(user_id=world, problem_number=1002, day=datetime(2026, 9, 1).date(),
                      target_min=15, actual_min=5, outcome="clean"))
        s.flush()
        assert picker.target_for(s, world, s.get(Problem, 1002)) == 12


# ---- checkpoints ---------------------------------------------------------------------------
def _set_elapsed(hid, minutes):
    with db.session_scope() as s:
        h = s.get(Handout, hid)
        h.clock_accum_s = 0
        h.clock_anchor = datetime.now() - timedelta(minutes=minutes)


def _cp(uid):
    with db.session_scope() as s:
        return [e.payload for e in s.query(Event).filter_by(user_id=uid, kind="clock_checkpoint")
                .order_by(Event.id)]


def test_checkpoints_fire_once_each(opened):
    uid, hid = opened
    service.clock(uid, hid, "target", target_min=10)
    _set_elapsed(hid, 4)
    assert service._fire_checkpoints(uid) is None
    _set_elapsed(hid, 5.5)
    assert service._fire_checkpoints(uid) and service._fire_checkpoints(uid) is None
    _set_elapsed(hid, 10.2)
    service._fire_checkpoints(uid)
    _set_elapsed(hid, 15.1)
    service._fire_checkpoints(uid)
    service._fire_checkpoints(uid)
    got = _cp(uid)
    assert [c["pct"] for c in got] == [50, 100, 150]
    assert got[0] == {"pct": 50, "elapsed_min": 6, "target_min": 10, "has_approach": False}


def test_checkpoints_not_while_paused_and_rearm(opened):
    uid, hid = opened
    service.clock(uid, hid, "target", target_min=10)
    _set_elapsed(hid, 5.5)
    service.clock(uid, hid, "pause", reason="lesson")      # banks 5.5 min, paused
    with db.session_scope() as s:
        s.get(Handout, hid).clock_accum_s = 20 * 60         # way over, but paused
    assert service._fire_checkpoints(uid) is None
    service.clock(uid, hid, "resume")
    service._fire_checkpoints(uid)                          # 200%: only the highest is pushed
    assert [c["pct"] for c in _cp(uid)] == [150]
    service.clock(uid, hid, "reset", reason="restart")      # re-arms
    service.push_event(uid, "approach", {"handout_id": hid, "text": "scan"})
    _set_elapsed(hid, 5.5)
    service._fire_checkpoints(uid)
    assert _cp(uid)[-1] == {"pct": 50, "elapsed_min": 6, "target_min": 10, "has_approach": True}
    service.clock(uid, hid, "target", target_min=11)        # target change re-arms too
    service._fire_checkpoints(uid)
    assert [c["pct"] for c in _cp(uid)] == [150, 50, 50]


def test_wait_event_delivers_checkpoint(opened):
    uid, hid = opened
    service.clock(uid, hid, "target", target_min=10)
    _set_elapsed(hid, 10.5)
    got = asyncio.run(service.wait_event(uid, timeout_s=2, poll_s=0.05))
    assert [e["kind"] for e in got] == ["clock_checkpoint"] and got[0]["payload"]["pct"] == 100


# ---- bridge --------------------------------------------------------------------------------
@pytest.fixture
def bridge(opened):
    from bridge import server
    uid, hid = opened
    server.set_service(service, user_id=uid)
    yield server, uid, hid
    server.set_service(None)


def test_bridge_clock_tool(bridge):
    server, uid, hid = bridge
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools["clock"].description.strip().startswith(
        "Control his clock: start | pause | resume | reset | target | phase. Say why in chat "
        "when you pause or reset.")
    assert "force=true only if he said in chat he understood" in tools["clock"].description
    r = server.clock("pause", reason="float precision")
    assert r["state"] == "paused" and r["handout_id"] == hid
    assert server.clock("target", target_min=14)["target_min"] == 14
    assert "leak" in server.clock("phase", label="heap work")["error"]
    assert "error" in server.clock("target", target_min=500)


def test_bridge_say_with_clock_action(bridge):
    server, uid, hid = bridge
    r = server.say("Clock stopped. Talking through float precision.", clock_action="pause",
                   clock_reason="float precision")
    assert r["clock"]["state"] == "paused" and r["message_id"]
    assert service.list_messages(uid)[-1]["content"].startswith("Clock stopped.")
    n = len(service.list_messages(uid))
    bad = server.say("Phase.", clock_action="phase", label="greedy")
    assert bad["posted"] is False and "leak" in bad["error"]
    assert len(service.list_messages(uid)) == n
    assert "clock" not in server.say("plain")


def test_bridge_log_attempt_uses_coach_clock(bridge):
    server, uid, hid = bridge
    _back(hid, 12)
    service.clock(uid, hid, "pause", reason="aside", now=datetime.now() - timedelta(minutes=4))
    r = server.log_attempt(hid, bugs=0, hints=0, optimal=True, derived_cold=True)
    assert r["actual_min"] == 8 and r["wall_min"] == 12 and "aside" in r["clock_note"]


# ---- web -----------------------------------------------------------------------------------
@pytest.fixture
def web(world, monkeypatch):
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    webapp.app.state.service = Svc()
    c = TestClient(webapp.app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "tester", "password": "pw-for-tests", "csrf": tok})
    yield c, world
    webapp.app.state.service = None


def test_handout_state_carries_clock(web):
    c, uid = web
    h = service.next_problem(uid)
    st = c.get("/handout/state").json()
    assert set(st["clock"]) >= {"state", "elapsed_s", "wall_s", "target_min", "phase",
                                "anchor_iso", "accum_s", "pause_reason", "pauses", "resets"}
    service.clock(uid, h.handout_id, "pause", reason="aside")
    service.clock(uid, h.handout_id, "phase", label="approach")
    service.clock(uid, h.handout_id, "target", target_min=9)
    st = c.get("/handout/state").json()
    assert st["clock"]["state"] == "paused" and st["clock"]["phase"] == "approach"
    assert st["target_min"] == 9 and st["clock"]["pause_reason"] == "aside"
    html = c.get("/solve").text
    assert 'id="t-paused"' in html and 'data-run="paused"' in html and ">approach<" in html
    assert "09:00" in html
    assert_clean(html.replace(h.title, ""))      # the fixture's titles name their pattern


def test_reveal_page_shows_both_times(web):
    c, uid = web
    h = service.next_problem(uid)
    _back(h.handout_id, 15)
    service.clock(uid, h.handout_id, "pause", reason="float precision",
                  now=datetime.now() - timedelta(minutes=5))
    service.log_attempt(uid, handout_id=h.handout_id)
    page = c.get(f"/reveal/{h.handout_id}").text
    assert "10 min on the coach's clock, 15 min wall" in page
    assert "Paused 5 min: float precision" in page
