"""Coach requests 3-6: duplicate submission, clock adjust, leak guard with his words, a broken
reference on a hidden case."""
import asyncio
from datetime import datetime, timedelta

import pytest

from coach import clock as clk
from coach import db, editor, service
from coach.models import ClockEvent, Event, Handout, Pattern, Problem, Submission
from tests.test_mmr import back, give
from tests.test_reference import SUM, SUM_CASES, SUM_OK, SUM_REF, _submit


@pytest.fixture
def opened(world):
    return world, service.next_problem(world).handout_id


def _subs(hid):
    with db.session_scope() as s:
        return [(x.id, x.verdict) for x in s.query(Submission).filter_by(handout_id=hid)
                .order_by(Submission.id)]


def _sub_events(uid):
    with db.session_scope() as s:
        return s.query(Event).filter_by(user_id=uid, kind="submission").count()


# ---- #6 duplicate submission ----------------------------------------------------------------
def test_identical_submission_within_10s_is_one(opened):
    uid, hid = opened
    a = service.push_event(uid, "submission", {"handout_id": hid, "code": "x = 1"})
    b = service.push_event(uid, "submission", {"handout_id": hid, "code": "x = 1"})
    assert a == b and _sub_events(uid) == 1 and len(_subs(hid)) == 1
    service.push_event(uid, "submission", {"handout_id": hid, "code": "x = 2"})
    assert _sub_events(uid) == 2 and len(_subs(hid)) == 2


def test_identical_submission_after_10s_is_new(opened):
    uid, hid = opened
    service.push_event(uid, "submission", {"handout_id": hid, "code": "x = 1"})
    with db.session_scope() as s:
        for e in s.query(Event).filter_by(kind="submission"):
            e.created_at -= timedelta(seconds=30)
        for x in s.query(Submission):
            x.created_at -= timedelta(seconds=30)
    service.push_event(uid, "submission", {"handout_id": hid, "code": "x = 1"})
    assert _sub_events(uid) == 2 and len(_subs(hid)) == 2


def _setup_judge(uid, hid):
    service.set_reference(uid, hid, SUM_REF, "function", SUM)
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)


def test_judge_verdict_reaches_the_unjudged_twin_and_summary_has_no_null(opened):
    uid, hid = opened
    _setup_judge(uid, hid)
    old = datetime.now() - timedelta(seconds=60)
    with db.session_scope() as s:                  # the pre-fix shape: two identical rows
        for _ in range(2):
            s.add(Submission(user_id=uid, handout_id=hid, code=SUM_OK, created_at=old))
    assert service.judge(uid, hid)["verdict"] == "Accepted"
    assert [v for _, v in _subs(hid)] == ["Accepted", "Accepted"]
    with db.session_scope() as s:                  # old data: the first row never judged
        s.query(Submission).filter_by(handout_id=hid).order_by(Submission.id).first().verdict = None
    vs = service.session_summary(uid)["problems"][0]["submissions"]["verdicts"]
    assert vs == ["Accepted", "Accepted"]


def test_judge_grades_latest_submission(opened):
    uid, hid = opened
    _setup_judge(uid, hid)
    _submit(uid, hid, "class Solution:\n    def subarraySum(self, nums, k):\n        return 0\n")
    _submit(uid, hid, SUM_OK)
    assert service.judge(uid, hid)["verdict"] == "Accepted"
    assert [v for _, v in _subs(hid)] == [None, "Accepted"]      # a different earlier one stays


def test_page_locks_submit_while_judging():
    js = open("coach/web/static/solve.js", encoding="utf8").read()
    assert "if (submitLock) return;" in js and "lockSubmit(true)" in js
    assert "if (!on) lockSubmit(false)" in js


# ---- #3 clock adjust ------------------------------------------------------------------------
def _events(hid):
    with db.session_scope() as s:
        return [(e.action, e.reason, e.value) for e in
                s.query(ClockEvent).filter_by(handout_id=hid).order_by(ClockEvent.id)]


def _stepped(uid):
    h = give(uid, phase_budget={"understand": 2, "approach": 5, "code": 8})
    service.understood(uid, h.handout_id)
    service.unlock_editor(uid, h.handout_id)
    return h.handout_id


def _bank(hid, understand, approach, code_running_s):
    with db.session_scope() as s:
        h = s.get(Handout, hid)
        h.phase_elapsed = {"understand": understand, "approach": approach, "code": 0}
        h.phase_start_s = understand + approach
        h.clock_accum_s = understand + approach
        h.clock_anchor = datetime.now() - timedelta(seconds=code_running_s)


def test_adjust_takes_time_off_the_current_step_only(world):
    hid = _stepped(world)
    _bank(hid, 60, 100, 400)
    before = service.clock_state(world, hid)["elapsed_s"]
    st = service.clock(world, hid, "adjust", reason="stepped out", minus_s=300)
    assert 555 <= before <= 565 and 255 <= st["elapsed_s"] <= 265     # total -300
    sec = st["steps"]["seconds"]
    assert sec["understand"] == 60 and sec["approach"] == 100
    assert 95 <= sec["code"] <= 105                                  # 400 -> 100
    assert [e for e in _events(hid) if e[0] == "adjust"] == [
        ("adjust", "stepped out", {"minus_s": 300, "requested": 300, "step": "code"})]


def test_adjust_floors_at_zero_and_spares_earlier_steps(world):
    hid = _stepped(world)
    _bank(hid, 60, 100, 30)
    st = service.clock(world, hid, "adjust", reason="away", minus_s=10_000)
    sec = st["steps"]["seconds"]
    assert sec["understand"] == 60 and sec["approach"] == 100 and sec["code"] == 0
    assert 159 <= st["elapsed_s"] <= 162
    v = [e for e in _events(hid) if e[0] == "adjust"][0][2]
    assert 29 <= v["minus_s"] <= 32 and v["requested"] == 10_000


def test_adjust_without_steps_and_while_paused(opened):
    uid, hid = opened
    back(hid, 600)
    st = service.clock(uid, hid, "adjust", reason="phone call", minus_s=120)
    assert 475 <= st["elapsed_s"] <= 485
    service.clock(uid, hid, "pause", reason="aside")
    st = service.clock(uid, hid, "adjust", reason="more", minus_s=100_000)
    assert st["elapsed_s"] == 0 and st["state"] == "paused"


@pytest.mark.parametrize("kw", [{"minus_s": 0, "reason": "x"}, {"minus_s": -5, "reason": "x"},
                                {"minus_s": 5.5, "reason": "x"}, {"minus_s": None, "reason": "x"},
                                {"minus_s": 60, "reason": ""}])
def test_adjust_validates(opened, kw):
    uid, hid = opened
    with pytest.raises(ValueError):
        service.clock(uid, hid, "adjust", **kw)
    assert not [e for e in _events(hid) if e[0] == "adjust"]


def test_adjust_tool_event_and_reveal_note(opened):
    from bridge import server
    uid, hid = opened
    server.set_service(service, user_id=uid)
    try:
        tool = {t.name: t for t in asyncio.run(server.mcp.list_tools())}["clock"]
        assert "minus_s" in tool.description and "adjust" in tool.description
        back(hid, 600)
        r = server.clock("adjust", reason="stepped out without pausing", minus_s=300)
        assert "error" not in r and 295 <= r["elapsed_s"] <= 305
    finally:
        server.set_service(None)
    service.log_attempt(uid, handout_id=hid, bugs=0, hints=0, optimal=True, derived_cold=True)
    ck = service.reveal(hid)["clock"]
    assert [(a["minutes"], a["reason"]) for a in ck["adjusts"]] == \
        [(5, "stepped out without pausing")]
    assert "5 min away: stepped out without pausing" in clk.note(ck)


# ---- #4 leak guard allows his words ---------------------------------------------------------
@pytest.fixture
def hw(tmp_path):
    db.init(str(tmp_path / "v.db"))
    uid = service.create_user("tester", "pw")
    with db.session_scope() as s:
        s.add(Pattern(slug="sliding-window", name="Sliding Window", tier=1))
        s.add(Problem(number=3, title="Longest Substring", lc_slug="longest-substring",
                      difficulty="M", pattern_slug="sliding-window", source="bank"))
        s.flush()
        h = Handout(user_id=uid, problem_number=3, slot="drill", target_min=20)
        s.add(h)
        s.flush()
        hid = h.id
    return uid, hid


def test_leak_words_allowed_once_he_wrote_them(hw):
    uid, hid = hw
    spec = {"values": [1, 2], "caption": "a greedy sweep"}
    with pytest.raises(ValueError, match="leak"):
        service.show_visual(uid, hid, "array", spec)
    editor.save_draft(uid, hid, "def f(a):\n    # greedy\n    return a\n")
    assert service.show_visual(uid, hid, "array", spec)["id"]
    with db.session_scope() as s:                         # the general guard itself
        h = s.get(Handout, hid)
        assert service.leak_words("greedy", "T", s) == ["greedy"]
        assert service.leak_words("greedy", "T", s, h) == []
        assert "trie" in service.leak_words("greedy trie", "T", s, h)    # only HIS words


def test_chat_word_counts_and_statement_stays_strict(hw):
    uid, hid = hw
    service.post_message(uid, "user", "maybe greedy works?", handout_id=hid)
    with db.session_scope() as s:
        assert service.leak_words("greedy", "T", s, s.get(Handout, hid)) == []
    with pytest.raises(ValueError, match="leak"):
        service.set_statement(uid, hid, "Pick the greedy choice each time.")


# ---- #5 reference fails on a hidden case ----------------------------------------------------
def test_broken_reference_on_hidden_case_is_actionable_and_no_verdict(opened):
    uid, hid = opened
    _setup_judge(uid, hid)
    broken = SUM_REF.replace("count = 0", "count = 0\n        nums[1]")  # IndexError on 1 element
    service.set_reference(uid, hid, broken, "function", SUM, replace=True, reason="test")
    _submit(uid, hid, SUM_OK)
    with pytest.raises(ValueError) as e:
        service.judge(uid, hid, [{"args": {"nums": [7], "k": 7}}])
    m = str(e.value)
    assert "extra case 0: the reference fails on this input" in m and "IndexError" in m
    assert "nums" in m and "[7]" in m
    assert "set_reference" in m and "replace=true" in m and "explicit expected" in m
    assert [v for _, v in _subs(hid)] == [None]                   # his code never ran / no verdict
