"""Service layer: logging, retry rule, reveal, events, users, status smoke test."""
import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from coach import db, service
from coach.models import Attempt, Handout, PatternState, Problem, RetryItem

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


# ---- coach-written statement -------------------------------------------------------------
GOOD = ("Given an array of integers nums and an integer k, return the number of subarrays "
        "whose sum equals k.")


def _handout_for(world, number):
    with db.session_scope() as s:
        if s.get(Problem, number) is None:
            s.add(Problem(number=number, title="Pair Sum", lc_slug="pair-sum", difficulty="E",
                          source="attempt"))
            s.flush()
        h = Handout(user_id=world, problem_number=number, slot="drill", target_min=15)
        s.add(h)
        s.flush()
        return h.id


@pytest.mark.parametrize("bad", ["use a sliding window", "Hard", "a Two-Pointers walk",
                                 "keep a hash-map of counts", "Prefix  Sum", "try DP",
                                 "a min Heap", "BFS it", "binary-search the answer"])
def test_statement_leak_guard_rejects(world, bad):
    hid = _handout_for(world, 7)
    with pytest.raises(ValueError, match="leak"):
        service.set_statement(world, hid, GOOD + " " + bad)
    assert service.get_statement(hid) is None


def test_statement_stored_and_overwritten(world):
    hid = _handout_for(world, 7)
    assert service.set_statement(world, hid, GOOD)["ok"]
    assert service.get_statement(hid) == GOOD
    service.set_statement(world, hid, GOOD + " Constraints: n <= 10.")
    assert service.get_statement(hid).endswith("n <= 10.")
    assert service.next_problem(world, today=TODAY) is not None


def test_statement_reaches_handoff(world):
    h = service.next_problem(world, today=TODAY)
    assert h.paraphrase is None
    service.set_statement(world, h.handout_id, GOOD)
    again = service.next_problem(world, today=TODAY)     # same open handout
    assert again.handout_id == h.handout_id and again.paraphrase == GOOD


def test_statement_title_words_allowed(world):
    with db.session_scope() as s:
        s.add(Problem(number=206, title="Reverse Linked List", lc_slug="reverse-linked-list",
                      difficulty="E", source="attempt"))
    hid = _handout_for(world, 206)
    service.set_statement(world, hid, "Reverse a singly linked list and return its head.")
    with pytest.raises(ValueError, match="stack"):
        service.set_statement(world, hid, "Reverse a linked list, maybe with a stack.")
    other = _handout_for(world, 8)
    with pytest.raises(ValueError, match="linked list"):
        service.set_statement(world, other, "Reverse a linked list.")


# ---- accountability ------------------------------------------------------------------------
def test_missed_days_pure():
    from coach.rules import missed_days
    d = lambda m, dd: date(2026, m, dd)
    r = missed_days([d(10, 1), d(10, 12)], d(10, 18))
    assert r["last_practice_day"] == d(10, 12) and r["days_missed"] == 5
    assert r["missed_days_list"] == [d(10, 13), d(10, 14), d(10, 15), d(10, 16), d(10, 17)]
    assert missed_days([d(10, 17)], d(10, 18))["missed_days_list"] == []
    assert missed_days([d(10, 18)], d(10, 18))["days_missed"] == 0       # practised today only
    assert missed_days([], d(10, 18)) == {"last_practice_day": None, "days_missed": None,
                                          "missed_days_list": []}


def test_status_accountability(world):
    service.log_attempt(world, number=1000, actual_min=5, day=TODAY - timedelta(days=3))
    acc = service.status(world, today=TODAY)["accountability"]
    assert acc["days_missed"] == 2
    assert acc["missed_days_list"] == [TODAY - timedelta(days=2), TODAY - timedelta(days=1)]


# ---- trap quiz after a re-fire -------------------------------------------------------------
NOTE = """# Sliding Window

## The signal
- not a trap -> ignore me

## Where it bites
Written as **cue -> rule**.

- constraint allows negatives  ->  NOT a window. Use prefix sums
  plus a hashmap.
- counting with a `Counter` -> delete keys that hit zero.
  - sub-point joins its parent
- a bullet with no arrow

## Complexity
- O(n) -> not a trap either
"""


def test_parse_traps_sections_and_fallback():
    from coach.rules import parse_traps
    assert parse_traps(NOTE) == [
        {"cue": "constraint allows negatives",
         "trap": "NOT a window. Use prefix sums plus a hashmap."},
        {"cue": "counting with a `Counter`",
         "trap": "delete keys that hit zero. sub-point joins its parent"},
        {"cue": "a bullet with no arrow", "trap": "a bullet with no arrow"},
    ]
    assert parse_traps("## Traps\n* a -> b\n\n### deeper\n* c -> d\n## End\n* e") == [
        {"cue": "a", "trap": "b"}, {"cue": "c", "trap": "d"}]
    assert parse_traps("") == [] and parse_traps(None) == [] and parse_traps("# x\n- y") == []


def _refire_world(uid, note=NOTE):
    service.log_attempt(uid, number=1020, actual_min=5, day=date(2026, 10, 1))
    with db.session_scope() as s:
        s.add(PatternState(user_id=uid, pattern_slug="prefix-hashmap",
                           retired_on=date(2026, 10, 5), refires=[], note_md=note))


def test_reveal_of_refire_carries_trap_quiz(world):
    _refire_world(world)
    h = service.next_problem(world, today=TODAY)
    assert h.mode == "timed"
    service.log_attempt(world, handout_id=h.handout_id, actual_min=5, day=TODAY)
    r = service.reveal(h.handout_id)
    assert r["slot"] == "refire" and r["mode"] == "timed"
    assert r["trap_quiz"]["pattern"] == "prefix-hashmap"
    assert [c["cue"] for c in r["trap_quiz"]["cues"]][:2] == ["constraint allows negatives",
                                                              "counting with a `Counter`"]
    # log_attempt already counted the re-fire; record_refire is idempotent per day
    assert service.record_refire(world, "prefix-hashmap", today=TODAY)["refires"] == [
        TODAY.isoformat()]
    later = TODAY + timedelta(days=8)
    assert service.record_refire(world, "prefix-hashmap", today=later)["refires"] == [
        TODAY.isoformat(), later.isoformat()]
    with pytest.raises(ValueError):
        service.record_refire(world, "heap", today=TODAY)


def test_refire_without_traps_section_gives_empty_cues(world):
    _refire_world(world, note="# no traps here\n- just a line")
    h = service.next_problem(world, today=TODAY)
    service.log_attempt(world, handout_id=h.handout_id, actual_min=5, day=TODAY)
    assert service.reveal(h.handout_id)["trap_quiz"] == {"pattern": "prefix-hashmap", "cues": []}


def test_reveal_of_coached_problem_has_no_trap_quiz(world):
    h = service.next_problem(world, today=TODAY)          # no retired pattern: not a re-fire
    service.log_attempt(world, handout_id=h.handout_id, actual_min=5, day=TODAY)
    r = service.reveal(h.handout_id)
    assert "trap_quiz" not in r and r["slot"] != "refire"


def test_log_trap_recall_matches_csv_columns(world):
    r = service.log_trap_recall(world, "sliding-window", "delete zero keys",
                                "Counter decrement", True, False, today=TODAY)
    csv_cols = "date,pattern,trap,cue,recalled,hit_in_code".split(",")
    assert [k for k in r if k != "id"] == csv_cols
    assert (r["date"], r["recalled"], r["hit_in_code"]) == (TODAY.isoformat(), "yes", "no")


# ---- ledger --------------------------------------------------------------------------------
def test_set_skill_upserts_and_logs_event(world):
    from coach.models import Skill, SkillEvent
    r = service.set_skill(world, "two-pointers", 3, "17 min, 0 bugs", "up", today=TODAY)
    assert (r["created"], r["was"], r["level"]) == (False, 4, 3)       # matched by slug
    r2 = service.set_skill(world, "Edge-case narration", 1, "missed empty input", "flat",
                           today=TODAY)
    assert r2["created"] and r2["was"] is None
    with db.session_scope() as s:
        k = s.get(Skill, r2["skill_id"])
        assert (k.kind, k.pattern_slug, k.updated_on) == ("meta", None, TODAY)
        evs = s.scalars(select(SkillEvent).order_by(SkillEvent.id)).all()
        assert [(e.level, e.day) for e in evs] == [(3, TODAY), (1, TODAY)]
    new_pat = service.set_skill(world, "union-find", 1, "first look", "", today=TODAY)
    with db.session_scope() as s:
        assert s.get(Skill, new_pat["skill_id"]).kind == "pattern"
    for bad in (-1, 6, 2.5, True):
        with pytest.raises(ValueError):
            service.set_skill(world, "two-pointers", bad, "", "", today=TODAY)


# ---- session log and summary ---------------------------------------------------------------
def test_write_session_log_creates_and_overwrites(world):
    from coach.models import CoachSession
    sid = service.write_session_log(world, "# Session 2026-10-21\n", today=TODAY)["session_id"]
    service.end_session(sid)
    assert service.write_session_log(world, "# v2", today=TODAY)["session_id"] == sid
    with db.session_scope() as s:
        assert s.get(CoachSession, sid).log_md == "# v2"
    with pytest.raises(ValueError):
        service.write_session_log(world, "", today=TODAY)


def test_session_summary_hides_open_handouts_pattern(world):
    a = service.next_problem(world, today=TODAY)
    service.log_attempt(world, handout_id=a.handout_id, actual_min=30, bugs=2, hints=1,
                        notes="slow", day=TODAY)
    b = service.next_problem(world, today=TODAY)             # still open
    k = service.set_skill(world, "two-pointers", 3, "e", "up", today=TODAY)
    summ = service.session_summary(world, today=TODAY)
    p1, p2 = summ["problems"]
    assert p1["handout_id"] == a.handout_id and p1["attempt"]["bugs"] == 2
    assert p1["reveal"]["pattern"] and p1["reveal"]["slot"]
    assert p2["handout_id"] == b.handout_id and p2["status"] == "open"
    assert p2["reveal"] is None and p2["attempt"] is None
    assert [r["problem_number"] for r in summ["retry"]["added"]] == [a.number]
    assert summ["skill_changes"] == [{"name": k["name"], "level": 3, "evidence": "e"}]
    assert service.session_summary(world, today=TODAY + timedelta(days=1))["problems"] == []


# ---- additive migration --------------------------------------------------------------------
def test_init_adds_missing_columns_to_old_db(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE skills (id INTEGER PRIMARY KEY, user_id INTEGER, name VARCHAR(128),"
                " kind VARCHAR(8), pattern_slug VARCHAR(64), level INTEGER, trend VARCHAR(64),"
                " evidence TEXT)")
    con.execute("INSERT INTO skills VALUES (1, 1, 'x', 'meta', NULL, 2, '', '')")
    con.commit()
    con.close()
    db.init(str(path))
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(skills)")}
    con.close()
    assert "updated_on" in cols
    assert db.add_missing_columns(db.get_engine()) == []          # idempotent
    from coach.models import Skill
    with db.session_scope() as s:
        assert s.get(Skill, 1).updated_on is None and s.get(Skill, 1).level == 2


def test_missed_trap_restarts_refire_count(world):
    """PLAN.md: a missed trap restarts the three re-fires."""
    from coach import service
    from coach.db import session_scope
    from coach.models import PatternState
    from datetime import date
    uid = world
    with session_scope() as s:
        s.add(PatternState(user_id=uid, pattern_slug="sliding-window",
                           retired_on=date(2026, 10, 1), refires=["2026-10-10"]))
    service.log_trap_recall(uid, "sliding-window", "stale max", "window shrink", True, False)
    with session_scope() as s:
        assert s.scalars(select(PatternState).where(PatternState.user_id == uid)).first().refires == ["2026-10-10"]
    service.log_trap_recall(uid, "sliding-window", "stale max", "window shrink", False, True)
    with session_scope() as s:
        assert s.scalars(select(PatternState).where(PatternState.user_id == uid)).first().refires == []


def test_wait_event_heartbeat(world):
    assert service.coach_online()["last_seen"] is None
    asyncio.run(service.wait_event(world, timeout_s=0.1, poll_s=0.02))
    c = service.coach_online()
    assert c["last_seen"] and c["online"] and c["reason"] == "heartbeat"


def test_coach_online_by_last_seen(world):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    service.coach_seen(now - timedelta(seconds=60))
    assert service.coach_online(now)["online"] is True
    service.coach_seen(now - timedelta(seconds=120))
    r = service.coach_online(now)
    assert r["online"] is False and r["last_seen"]


def test_coach_online_by_recent_message(world):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert service.coach_online(now)["online"] is False
    service.post_message(world, "user", "hi")
    assert service.coach_online(now)["online"] is False       # only coach messages count
    service.post_message(world, "coach", "hello")
    r = service.coach_online(now)
    assert r["online"] is True and r["reason"] == "recent_message"
    assert service.coach_online(now + timedelta(minutes=11))["online"] is False


def test_editor_gate(world):
    h = service.next_problem(world, today=TODAY)
    assert service.editor_open(world, h.handout_id) is False       # starts locked
    assert service.unlock_editor(world, h.handout_id) == {"ok": True}
    assert service.editor_open(world, h.handout_id) is True
    msgs = service.list_messages(world)
    assert msgs[-1]["content"] == "Editor open." and msgs[-1]["role"] == "coach"
    assert msgs[-1]["handout_id"] == h.handout_id
    assert service.lock_editor(world, h.handout_id) == {"ok": True}
    assert service.editor_open(world, h.handout_id) is False
    assert service.unlock_editor(world, 99999)["ok"] is False
