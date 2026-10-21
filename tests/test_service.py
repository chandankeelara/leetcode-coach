"""Service layer: logging, retry rule, reveal, events, users, status smoke test."""
import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from coach import db, service
from coach.models import Attempt, Handout, PatternState, RetryItem

TODAY = date(2026, 10, 21)


def _retry(number):
    with db.session_scope() as s:
        return s.scalars(select(RetryItem).where(RetryItem.problem_number == number)
                         .order_by(RetryItem.id.desc())).first()


def test_e_reveal_before_log_raises_then_works(world):
    h = service.next_problem(world, today=TODAY)
    with pytest.raises(service.RevealBlocked):
        service.reveal(h.handout_id)
    service.log_attempt(world, handout_id=h.handout_id, actual_min=10, bugs=0, day=TODAY)
    r = service.reveal(h.handout_id)
    assert r["number"] == h.number and r["pattern"] and r["slot"]


def test_skipped_handout_cannot_be_revealed(world):
    h = service.next_problem(world, today=TODAY)
    service.skip_problem(h.handout_id)
    with pytest.raises(service.RevealBlocked):
        service.reveal(h.handout_id)


def test_log_attempt_sets_outcome_itself(world):
    h = service.next_problem(world, today=TODAY)           # target is the handout's target
    res = service.log_attempt(world, handout_id=h.handout_id, actual_min=h.target_min,
                              bugs=0, day=TODAY)
    assert res["outcome"] == "clean" and res["retry"] == "none"
    h2 = service.next_problem(world, today=TODAY, force_new=True)
    res = service.log_attempt(world, handout_id=h2.handout_id, actual_min=h2.target_min, bugs=1,
                              day=TODAY)
    assert res["outcome"] == "solved" and res["retry"] == "none"     # one bug alone: no queue


@pytest.mark.parametrize("kwargs", [
    dict(actual_min=100, bugs=0),                      # over 1.5x
    dict(actual_min=5, bugs=2),                        # 2+ bugs
    dict(actual_min=5, bugs=0, hints=1),               # a hint
    dict(actual_min=5, bugs=0, optimal=False),         # suboptimal
    dict(actual_min=5, bugs=0, derived_cold=False),    # not derived cold
    dict(actual_min=0, bugs=0, abandoned=True),        # abandoned
])
def test_retry_added_per_master_context_rule(world, kwargs):
    h = service.next_problem(world, today=TODAY)
    res = service.log_attempt(world, handout_id=h.handout_id, day=TODAY, **kwargs)
    assert res["retry"] == "added"
    item = _retry(h.number)
    assert item.attempts == 1 and item.cleared_on is None


def test_retry_bump_then_clear(world):
    h = service.next_problem(world, today=TODAY)
    service.log_attempt(world, handout_id=h.handout_id, actual_min=5, bugs=3, day=TODAY)
    later = TODAY + timedelta(days=3)
    res = service.log_attempt(world, number=h.number, actual_min=5, bugs=2, day=later)
    assert res["retry"] == "bumped"
    item = _retry(h.number)
    assert item.attempts == 2 and item.last_on == later
    res = service.log_attempt(world, number=h.number, actual_min=5, bugs=0,
                              day=later + timedelta(days=5), target_min=10)
    assert res["retry"] == "cleared" and _retry(h.number).cleared_on is not None


def test_abandoned_never_counts_for_backlog(world):
    h = service.next_problem(world, today=TODAY)
    service.log_attempt(world, handout_id=h.handout_id, actual_min=0, abandoned=True, day=TODAY)
    st = service.status(world, today=TODAY)
    assert st["backlog"]["done_today"] == 0
    h = service.next_problem(world, today=TODAY, force_new=True)
    service.log_attempt(world, handout_id=h.handout_id, actual_min=5, day=TODAY)
    assert service.status(world, today=TODAY)["backlog"]["done_today"] == 1


def test_third_clean_solve_retires_pattern(world):
    # three clean solves of one pattern, logged through the service
    nums = [1000, 1001, 1002]
    for n in nums:
        service.log_attempt(world, number=n, actual_min=5, bugs=0, target_min=10, day=TODAY)
    with db.session_scope() as s:
        ps = s.scalars(select(PatternState).where(
            PatternState.pattern_slug == "prefix-hashmap")).one()
        assert ps.retired_on == TODAY
    st = service.status(world, today=TODAY)
    assert [r["pattern"] for r in st["refire"]] == ["prefix-hashmap"]


def test_burn_and_status_blocklist(world):
    h = service.next_problem(world, today=TODAY)
    st = service.status(world, today=TODAY)
    assert h.number in st["blocklist"]


def test_events_queue_roundtrip(world):
    async def go():
        assert await service.wait_event(world, timeout_s=0.3, poll_s=0.05) == []
        eid = service.push_event(world, "submission", {"problem": 424, "verdict": "AC"})
        got = await service.wait_event(world, timeout_s=2, poll_s=0.05)
        assert [e["id"] for e in got] == [eid]
        assert got[0]["kind"] == "submission" and got[0]["payload"]["problem"] == 424
        # drained: a second wait finds nothing
        assert await service.wait_event(world, timeout_s=0.2, poll_s=0.05) == []

    asyncio.run(go())


def test_wait_event_wakes_on_push(world):
    async def go():
        async def later():
            await asyncio.sleep(0.2)
            service.push_event(world, "chat", {"text": "hi"})
        task = asyncio.create_task(later())
        got = await service.wait_event(world, timeout_s=5, poll_s=0.05)
        await task
        return got
    got = asyncio.run(go())
    assert got and got[0]["payload"] == {"text": "hi"}


def test_messages(world):
    a = service.post_message(world, "user", "my approach is ...")
    b = service.post_message(world, "coach", "why not the alternative?")
    msgs = service.list_messages(world)
    assert [m["id"] for m in msgs] == [a, b] and msgs[1]["role"] == "coach"
    assert service.list_messages(world, since_id=a)[0]["id"] == b


def test_users(world):
    assert service.verify_user("tester", "pw-for-tests") == world
    assert service.verify_user("tester", "wrong") is None
    assert service.verify_user("nobody", "x") is None
    with pytest.raises(ValueError):
        service.create_user("tester", "again")


def test_sessions(world):
    sid = service.start_session(world, today=TODAY)
    assert service.start_session(world, today=TODAY) == sid          # reuses the open one
    service.end_session(sid, "# log")
    assert service.start_session(world, today=TODAY) != sid


def test_report_runs_on_service_data(world):
    assert service.report(world, date(2026, 12, 15), today=TODAY) == {}     # no attempts yet
    h = service.next_problem(world, today=TODAY)
    service.log_attempt(world, handout_id=h.handout_id, actual_min=5, day=TODAY)
    card = service.report(world, "2026-12-15", today=TODAY)
    assert card["throughput"]["completed"] == 1
