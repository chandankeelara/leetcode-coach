"""Picker: RULE ZERO DTO, the hard filters, slot order."""
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from coach import db, rules, service
from coach.models import Attempt, Handout, PatternState, Problem, RetryItem
from coach.picker import Handoff

TODAY = date(2026, 10, 21)

ALLOWED = {"handout_id", "number", "title", "leetcode_url", "paraphrase", "started_at",
           "target_min", "instruction"}


def _pick_all(uid, today=TODAY, limit=500):
    """Pick until nothing passes the filters. Every pick is a handout, so it blocks itself."""
    out = []
    for _ in range(limit):
        h = service.next_problem(uid, today=today, force_new=True)
        if h is None:
            return out
        out.append(h)
    raise AssertionError("picker never ran dry")


def _problem(number):
    with db.session_scope() as s:
        return s.get(Problem, number)


def _add_attempt(uid, number, day, outcome="solved"):
    with db.session_scope() as s:
        p = s.get(Problem, number)
        s.add(Attempt(user_id=uid, problem_number=number, pattern_slug=p.pattern_slug, day=day,
                      target_min=15, actual_min=12, bugs=0, outcome=outcome, source="import"))


def test_a_handoff_has_only_the_allowed_fields():
    assert set(Handoff.model_fields) == ALLOWED
    # no extras can sneak in
    with pytest.raises(ValidationError):
        Handoff(handout_id=1, number=1, title="t", leetcode_url="u", paraphrase=None,
                started_at=datetime.now(), target_min=10, pattern="heap")
    for banned in ("pattern", "slot", "tier", "difficulty", "progress"):
        assert banned not in Handoff.model_fields


def test_a_real_handoff_dump_has_no_pattern_text(world):
    h = service.next_problem(world, today=TODAY)
    d = h.model_dump()
    assert set(d) == ALLOWED
    assert d["instruction"] == "Give your approach in plain words before any code."
    assert d["leetcode_url"].startswith("https://leetcode.com/problems/")
    assert d["leetcode_url"].endswith("/")
    assert d["started_at"].date() == TODAY          # stamped by the server


def test_b_recent_attempts_and_handouts_never_picked(world):
    _add_attempt(world, 1000, TODAY - timedelta(days=1))
    _add_attempt(world, 1001, TODAY - timedelta(days=6))
    _add_attempt(world, 1010, TODAY - timedelta(days=7))        # 7 days ago: free again
    with db.session_scope() as s:
        s.add(Handout(user_id=world, problem_number=1011, slot="drill", pattern_slug=None,
                      handed_at=datetime.combine(TODAY - timedelta(days=2), datetime.min.time()),
                      target_min=15, status="skipped"))
    picks = {h.number for h in _pick_all(world)}
    assert not picks & {1000, 1001, 1011}
    assert 1010 in picks


def test_b_a_picked_problem_blocks_itself(world):
    h = service.next_problem(world, today=TODAY)
    again = {x.number for x in _pick_all(world)}
    assert h.number not in again


def test_c_burned_pattern_never_picked(world):
    service.burn_pattern(world, "heap", "leaked in chat", today=TODAY)
    service.burn_pattern(world, "two-pointers", "leaked", today=TODAY)
    picks = _pick_all(world)
    assert picks
    with db.session_scope() as s:
        pats = {s.get(Problem, h.number).pattern_slug for h in picks}
    assert not pats & {"heap", "two-pointers"}


def test_c_burn_is_per_session(world):
    service.burn_pattern(world, "heap", "x", today=TODAY)
    tomorrow = TODAY + timedelta(days=1)
    with db.session_scope() as s:
        pats = {s.get(Problem, h.number).pattern_slug for h in _pick_all(world, tomorrow)}
    assert "heap" in pats


def test_d_no_hard_before_november(world):
    picks = _pick_all(world, today=date(2026, 10, 31))
    assert picks
    with db.session_scope() as s:
        assert all(s.get(Problem, h.number).difficulty != "H" for h in picks)
    later = _pick_all(world, today=date(2026, 11, 1))
    with db.session_scope() as s:
        assert any(s.get(Problem, h.number).difficulty == "H" for h in later)


def test_retired_by_three_clean_solves_not_picked(world):
    for i in range(3):
        _add_attempt(world, 1002, date(2026, 9, 1) + timedelta(days=i), "clean")
    assert 1002 not in {h.number for h in _pick_all(world)}


def test_open_retry_item_only_through_retry_slot(world):
    with db.session_scope() as s:
        s.add(RetryItem(user_id=world, problem_number=1020, added=date(2026, 10, 1),
                        last_on=date(2026, 10, 1), attempts=1))
    _add_attempt(world, 1020, date(2026, 10, 1))
    first = service.next_problem(world, today=TODAY, force_new=True)
    # 1020 is DUE (20 days > 2) so the retry slot may hand it out, and only then
    with db.session_scope() as s:
        slots = {h.problem_number: h.slot for h in s.scalars(select(Handout))}
    for n, slot in slots.items():
        if n == 1020:
            assert slot == "retry"
    assert first is not None


def test_slot_order_cycles_warmup_drill_growth_retry(world):
    # give the picker data: an attempted Tier-1 pattern (drill), a retired one due for re-fire
    _add_attempt(world, 1000, date(2026, 10, 1))                  # prefix-hashmap touched
    _add_attempt(world, 1010, date(2026, 10, 2))                  # two-pointers touched
    _add_attempt(world, 1030, date(2026, 9, 1))                   # trees-dfs? (index 3)
    with db.session_scope() as s:
        s.add(PatternState(user_id=world, pattern_slug="prefix-hashmap",
                           retired_on=date(2026, 10, 5), refires=[]))
        s.add(RetryItem(user_id=world, problem_number=1010, added=date(2026, 10, 2),
                        last_on=date(2026, 10, 2), attempts=1))
    for _ in range(4):
        h = service.next_problem(world, today=TODAY)
        service.log_attempt(world, handout_id=h.handout_id, actual_min=5, bugs=0, day=TODAY)
    with db.session_scope() as s:
        slots = [h.slot for h in s.scalars(select(Handout).order_by(Handout.id))]
    assert slots[0] == "refire"                 # retired pattern, 16 days since last touch
    assert slots[1] == "drill"
    assert slots[2] == "growth"
    assert slots[3] == "retry"


def test_nothing_left_returns_none(world):
    assert _pick_all(world)            # drains the bank
    assert service.next_problem(world, today=TODAY, force_new=True) is None


def test_open_handout_is_returned_again(world):
    a = service.next_problem(world, today=TODAY)
    b = service.next_problem(world, today=TODAY)
    assert (a.handout_id, a.started_at) == (b.handout_id, b.started_at)
    c = service.next_problem(world, today=TODAY, force_new=True)
    assert c.handout_id != a.handout_id
