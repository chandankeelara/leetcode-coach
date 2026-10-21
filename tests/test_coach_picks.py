"""Coach-led choice: plans, the coach's read tools, hand_out validation, the RULE ZERO guard."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from coach import db, plans, rules, service
from coach.models import ClockEvent, CoachMessage, Handout, Problem

TODAY = date(2026, 10, 21)
HANDOFF_KEYS = {"handout_id", "number", "title", "leetcode_url", "paraphrase", "started_at",
                "target_min", "instruction", "mode"}


def num(slug, d="E"):
    """Problem number of the `world` fixture: 3 per pattern (E, M, H)."""
    i = (rules.TIER1 + rules.TIER2).index(slug)
    return 1000 + i * 10 + "EMH".index(d)


def log(uid, number, day, **kw):
    kw.setdefault("actual_min", 5)
    kw.setdefault("target_min", 10)
    return service.log_attempt(uid, number=number, day=day, **kw)


# ---- plans -----------------------------------------------------------------------------------
def test_plan_create_get_set(world):
    assert service.get_plan(world, today=TODAY) is None
    p = service.set_plan(world, today=TODAY, target_date="2026-12-15", daily_problems=3,
                         minutes_per_day=60, focus_patterns=["heap", "two-pointers"],
                         hards_from=None)
    assert p["daily_problems"] == 3 and p["focus_patterns"] == ["heap", "two-pointers"]
    assert p["hards_from"] is None and p["hards_allowed"] is False and p["days_left"] == 55
    p = service.set_plan(world, today=TODAY, daily_problems=1, phases=[
        {"name": "A", "start": "2026-10-01", "end": "2026-10-31", "rules": "basics"}])
    assert p["daily_problems"] == 1 and p["minutes_per_day"] == 60      # other fields kept
    assert p["current_phase"]["name"] == "A"
    assert service.get_plan(world, today=TODAY)["id"] == p["id"]        # updated, not added
    with pytest.raises(ValueError):
        service.set_plan(world, daily_problems=0)
    with pytest.raises(ValueError):
        service.set_plan(world, focus_patterns=["not-a-pattern"])
    with pytest.raises(ValueError):
        service.set_plan(world, colour="red")


def test_legacy_plan_migrated_for_users_with_history_only(world, tmp_path):
    other = service.create_user("newbie", "pw")
    log(world, num("heap"), TODAY - timedelta(days=1))
    db.init(str(db.get_engine().url.database))            # the migration runs on init
    p = service.get_plan(world, today=TODAY)
    assert p["target_date"] == "2026-12-15" and p["daily_problems"] == 2
    assert p["focus_patterns"] == rules.TIER1 + rules.TIER2
    assert [(x["name"], x["start"], x["end"]) for x in p["phases"]] == [
        ("A", "2026-10-09", "2026-10-31"), ("B", "2026-11-01", "2026-11-23"),
        ("C", "2026-11-24", "2026-12-15")]
    assert p["hards_from"] == "2026-11-01" and p["current_phase"]["name"] == "A"
    assert service.get_plan(other) is None                 # brand new: onboarding
    db.init(str(db.get_engine().url.database))             # idempotent
    with db.session_scope() as s:
        from coach.models import Plan
        assert len(s.scalars(select(Plan)).all()) == 1


# ---- read tools ------------------------------------------------------------------------------
def test_get_profile_shape(world):
    service.set_plan(world, target_date="2026-12-15", focus_patterns=["heap"])
    log(world, num("heap"), TODAY - timedelta(days=2), bugs=2)
    log(world, num("two-pointers"), TODAY - timedelta(days=1))
    pr = service.get_profile(world, today=TODAY)
    assert set(pr) >= {"plan", "ledger", "patterns", "recent_attempts", "backlog", "streak",
                       "clean_rate"}
    assert pr["plan"]["focus_patterns"] == ["heap"]
    heap = pr["patterns"]["heap"]
    assert set(heap) >= {"tier", "clean", "attempts", "owned", "last_attempt_day", "retired_on"}
    assert heap["tier"] == 1 and heap["attempts"] == 1 and heap["clean"] == 0
    assert heap["in_focus"] is True and heap["level"] == 2
    assert pr["patterns"]["two-pointers"]["clean"] == 1
    r0 = pr["recent_attempts"][0]
    assert r0["number"] == num("two-pointers") and set(r0) >= {"outcome", "actual_min", "bugs",
                                                               "hints"}
    assert {k["name"] for k in pr["ledger"]} and set(pr["ledger"][0]) >= {"level", "trend",
                                                                         "evidence"}
    assert isinstance(pr["backlog"]["backlog"], int) and "current" in pr["streak"]


def test_get_due_shape(world):
    log(world, num("heap"), TODAY - timedelta(days=3), bugs=2)        # queued; wait 2 days
    log(world, num("trees-dfs"), TODAY, bugs=3)                       # queued; not due yet
    d = service.get_due(world, today=TODAY)
    assert set(d) >= {"retry_due", "refire_due", "blocklist", "today_done", "today_target"}
    assert [r["problem_number"] for r in d["retry_due"]] == [num("heap")]
    assert d["retry_due"][0]["blocked"] is True            # attempted 3 days ago: 7-day block
    assert [r["problem_number"] for r in d["retry_waiting"]] == [num("trees-dfs")]
    assert set(d["blocklist"]) == {num("heap"), num("trees-dfs")}
    assert d["today_done"] == 1 and d["refire_due"] == []


def test_find_problems_shape_and_filters(world):
    log(world, num("heap"), TODAY - timedelta(days=1))
    rows = service.find_problems(world, pattern="heap", today=TODAY)
    assert [r["number"] for r in rows] == [num("heap", "M"), num("heap", "E"), num("heap", "H")]
    by = {r["number"]: r for r in rows}
    assert set(rows[0]) >= {"number", "title", "difficulty", "pattern", "source", "attempts",
                            "last_attempt_day", "clean_count", "blocked", "block_reason"}
    assert by[num("heap")]["blocked"] and "7 days" in by[num("heap")]["block_reason"]
    assert by[num("heap", "H")]["blocked"] and "Hards" in by[num("heap", "H")]["block_reason"]
    assert not by[num("heap", "M")]["blocked"] and by[num("heap")]["attempts"] == 1
    assert [r["number"] for r in service.find_problems(world, pattern="heap", untouched=False,
                                                       today=TODAY)] == [num("heap")]
    tier2 = service.find_problems(world, tier=2, difficulty="E", limit=50, today=TODAY)
    assert tier2 and all(r["pattern"] in rules.TIER2 and r["difficulty"] == "E" for r in tier2)
    assert len(service.find_problems(world, limit=3, today=TODAY)) == 3


# ---- hand_out --------------------------------------------------------------------------------
def test_hand_out_success_dto_clock_and_private_reason(world):
    before = datetime.now()
    h = service.hand_out(world, num("heap", "M"), target_min=14, mode="timed",
                         why="weakest Tier-1 with data", today=TODAY)
    assert set(h.model_dump()) == HANDOFF_KEYS
    assert h.number == num("heap", "M") and h.target_min == 14 and h.mode == "timed"
    with db.session_scope() as s:
        row = s.get(Handout, h.handout_id)
        assert row.status == "open" and row.clock_state == "running"
        assert row.pick_reason == "weakest Tier-1 with data" and row.mode == "timed"
        assert row.handed_at.time() >= before.time().replace(microsecond=0)
        ev = s.scalars(select(ClockEvent).where(ClockEvent.handout_id == h.handout_id)).all()
        assert [e.action for e in ev] == ["start"]
    assert service.clock_state(world, h.handout_id)["state"] == "running"
    # hidden until logged
    with pytest.raises(service.RevealBlocked):
        service.reveal(h.handout_id)
    summ = service.session_summary(world, today=TODAY)
    assert summ["problems"][0]["reveal"] is None
    assert "weakest" not in str(summ)
    service.log_attempt(world, handout_id=h.handout_id, actual_min=10, day=TODAY)
    r = service.reveal(h.handout_id)
    assert r["why"] == "weakest Tier-1 with data" and r["picked_by"] == "coach"
    assert r["mode"] == "timed" and r["pattern"] == "heap"
    rv = service.session_summary(world, today=TODAY)["problems"][0]["reveal"]
    assert rv["why"] == "weakest Tier-1 with data"


def test_hand_out_default_target_and_retry_slot(world):
    log(world, num("heap"), TODAY - timedelta(days=10), bugs=2, target_min=15)
    h = service.hand_out(world, num("heap"), today=TODAY)
    assert h.target_min == 12 and h.mode == "coached"           # 80% of the first target
    with db.session_scope() as s:
        assert s.get(Handout, h.handout_id).slot == "retry"


def test_hand_out_refuses_recent(world):
    log(world, num("heap"), TODAY - timedelta(days=2))
    with pytest.raises(service.PickRefused, match="last 7 days"):
        service.hand_out(world, num("heap"), today=TODAY)


def test_hand_out_refuses_recent_handout_even_skipped(world):
    h = service.hand_out(world, num("heap"), today=TODAY - timedelta(days=1))
    service.skip_problem(h.handout_id)
    with pytest.raises(service.PickRefused, match="last 7 days"):
        service.hand_out(world, num("heap"), today=TODAY)


def test_hand_out_refuses_retired(world):
    for k in (30, 20, 10):
        log(world, num("heap", "M"), TODAY - timedelta(days=k))
    with pytest.raises(service.PickRefused, match="retired"):
        service.hand_out(world, num("heap", "M"), today=TODAY)


def test_hand_out_refuses_hard_before_plan_allows(world):
    with pytest.raises(service.PickRefused, match="Hards"):          # no plan: config date
        service.hand_out(world, num("heap", "H"), today=TODAY)
    service.set_plan(world, target_date="2026-12-15", hards_from=None)
    with pytest.raises(service.PickRefused, match="off in the plan"):
        service.hand_out(world, num("heap", "H"), today=TODAY)
    service.set_plan(world, hards_from=TODAY + timedelta(days=5))
    with pytest.raises(service.PickRefused, match="no Hards before"):
        service.hand_out(world, num("heap", "H"), today=TODAY)
    service.set_plan(world, hards_from=TODAY)
    assert service.hand_out(world, num("heap", "H"), today=TODAY).number == num("heap", "H")


def test_hand_out_refuses_burned_pattern(world):
    service.burn_pattern(world, "heap", "said it", today=TODAY)
    with pytest.raises(service.PickRefused, match="burned"):
        service.hand_out(world, num("heap"), today=TODAY)
    assert service.hand_out(world, num("two-pointers"), today=TODAY)


def test_hand_out_refuses_while_another_is_open(world):
    service.hand_out(world, num("heap"), today=TODAY)
    with pytest.raises(service.PickRefused, match="still open"):
        service.hand_out(world, num("two-pointers"), today=TODAY)
    service.skip_open(world)
    assert service.hand_out(world, num("two-pointers"), today=TODAY)


def test_hand_out_refuses_bad_input(world):
    with pytest.raises(service.PickRefused, match="not in the problem bank"):
        service.hand_out(world, 99999, today=TODAY)
    with pytest.raises(service.PickRefused, match="mode"):
        service.hand_out(world, num("heap"), mode="warmup", today=TODAY)
    with pytest.raises(service.PickRefused, match="target_min"):
        service.hand_out(world, num("heap"), target_min=0, today=TODAY)


def test_hand_out_emits_handout_ui_event(world):
    last = service.ui_last_id()
    h = service.hand_out(world, num("heap"), today=TODAY)
    assert {"kind": "handout", "ref_id": h.handout_id} in [
        {k: e[k] for k in ("kind", "ref_id")} for e in service.ui_events_since(world, last)]


def test_next_problem_fallback_still_works(world):
    h = service.next_problem(world, today=TODAY)
    assert set(h.model_dump()) == HANDOFF_KEYS
    service.log_attempt(world, handout_id=h.handout_id, actual_min=5, day=TODAY)
    assert service.reveal(h.handout_id)["picked_by"] == "server"


def test_picker_fallback_honours_plan_hards(world):
    service.set_plan(world, target_date="2026-12-15", hards_from=None)
    with db.session_scope() as s:
        from coach import picker
        assert picker.hards_allowed(s, world, TODAY) is False


# ---- RULE ZERO guard -------------------------------------------------------------------------
def _neutral_titles():
    """The fixture titles name the pattern ("sliding-window problem E"), which the guard
    exempts like any title. Give them plain titles."""
    with db.session_scope() as s:
        for p in s.scalars(select(Problem)):
            p.title = f"Problem {p.number}"


@pytest.fixture(autouse=True)
def _plain(request):
    if request.node.name.startswith("test_guard"):
        request.getfixturevalue("world")
        _neutral_titles()


def _coach_msgs(uid):
    return [m["content"] for m in service.list_messages(uid) if m["role"] == "coach"]


def test_guard_refuses_open_pattern_before_he_names_it(world):
    h = service.hand_out(world, num("sliding-window"), today=TODAY)
    with pytest.raises(ValueError, match="names the open problem's pattern"):
        service.post_message(world, "coach", "Think about a sliding window.")
    with pytest.raises(ValueError, match="names the open problem's pattern"):
        service.post_message(world, "coach", "What is in the window now?", h.handout_id)
    assert _coach_msgs(world) == []
    with pytest.raises(ValueError, match="open problem's pattern"):      # synonym via annotate
        service.annotate(world, h.handout_id, 1, "question", "What does the window hold here?")
    with pytest.raises(ValueError):
        service.check_coach_text(world, "slidingWindow is the move")


def test_guard_allows_once_he_named_it(world):
    h = service.hand_out(world, num("sliding-window"), today=TODAY)
    service.push_event(world, "approach", {"handout_id": h.handout_id,
                                           "text": "keep a window and slide it right"})
    service.post_message(world, "coach", "Why a window here and not something else?")
    assert _coach_msgs(world) == ["Why a window here and not something else?"]


def test_guard_allows_after_he_says_it_in_chat(world):
    service.hand_out(world, num("heap"), today=TODAY)
    service.post_message(world, "user", "could a priority queue work?")
    service.post_message(world, "coach", "What does the priority queue hold?")
    assert _coach_msgs(world)[-1] == "What does the priority queue hold?"


def test_guard_allows_after_log_attempt(world):
    h = service.hand_out(world, num("sliding-window"), today=TODAY)
    service.log_attempt(world, handout_id=h.handout_id, actual_min=5, day=TODAY)
    service.post_message(world, "coach", "It was testing a sliding window.")
    assert _coach_msgs(world) == ["It was testing a sliding window."]


def test_guard_ignores_a_different_pattern(world):
    service.hand_out(world, num("sliding-window"), today=TODAY)
    service.post_message(world, "coach", "Would a heap help?")
    assert _coach_msgs(world) == ["Would a heap help?"]


def test_guard_title_words_are_fine(world):
    with db.session_scope() as s:
        s.add(Problem(number=206, title="Reverse Linked List", lc_slug="reverse-linked-list",
                      difficulty="E", pattern_slug="linked-list", source="bank", bank_order=999))
    service.hand_out(world, 206, today=TODAY)
    service.post_message(world, "coach", "Draw the linked list after one step.")
    assert _coach_msgs(world) == ["Draw the linked list after one step."]


def test_guard_on_statement_visual_tests_and_phase(world):
    h = service.hand_out(world, num("sliding-window"), today=TODAY)
    with pytest.raises(ValueError):
        service.set_statement(world, h.handout_id, "Keep a window over `nums`.")
    with pytest.raises(ValueError):
        service.show_visual(world, h.handout_id, "array", {"values": [1, 2],
                                                           "caption": "the window"})
    with pytest.raises(ValueError):
        service.set_tests(world, h.handout_id, [{"args": {"x": 1}, "expected": 1,
                                                 "label": "window of one"}],
                          "function", {"class": "Solution", "method": "f"})
    with pytest.raises(ValueError):
        service.clock(world, h.handout_id, "phase", label="window")
    with db.session_scope() as s:
        assert s.scalars(select(CoachMessage)).all() == []
