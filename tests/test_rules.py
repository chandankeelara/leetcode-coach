"""Golden tests: coach.rules against a frozen copy of attempts.csv. The expected numbers are
what the old tools/status.py and tools/report.py printed for 2026-10-21 on this exact file
(report with --date 2026-12-15)."""
from datetime import date, datetime, timedelta

from coach import rules

TODAY = date(2026, 10, 21)
HORIZON = (date(2026, 12, 15) - TODAY).days


def test_backlog_golden(csv_attempts):
    b = rules.backlog(csv_attempts, TODAY)
    assert (b["backlog"], b["target_today"], b["done_today"], b["remaining"]) == (7, 9, 1, 8)
    assert (b["days"], b["owed_before"], b["done_before"]) == (12, 24, 17)


def test_backlog_ignores_abandoned_and_never_caps():
    rows = [{"day": date(2026, 10, 9), "outcome": "abandoned"},
            {"day": date(2026, 10, 10), "outcome": "solved"}]
    b = rules.backlog(rows, date(2026, 11, 30))
    # 52 days x 2 owed, 1 paid: no cap, no decay
    assert b["backlog"] == 52 * 2 - 1
    assert b["target_today"] == 2 + b["backlog"]


def test_backlog_before_start_is_zero():
    b = rules.backlog([], date(2026, 10, 1))
    assert b["backlog"] == 0 and b["target_today"] == 2


def test_streak_golden(csv_attempts):
    sk = rules.streak(csv_attempts, TODAY)
    assert (sk["current"], sk["longest"]) == (1, 3)
    assert sk["last14"] == "###.#.....#..#"


def test_streak_counts_from_yesterday_when_today_empty(csv_attempts):
    rows = [r for r in csv_attempts if r["day"] != TODAY]
    # nothing on 10/21 and nothing on 10/20 or 10/19: streak is 0
    assert rules.streak(rows, TODAY)["current"] == 0
    # 10/18 is the last practice day; asked on 10/19 the streak is 1
    assert rules.streak(rows, date(2026, 10, 19))["current"] == 1


def test_tier_progress_golden(csv_attempts):
    tp = rules.tier_progress(csv_attempts, TODAY)
    got = {r["pattern"]: (r["clean"], r["status"]) for r in tp["tier1"]}
    assert got == {
        "prefix-hashmap": (3, "owned"), "two-pointers": (1, "2 to go"),
        "sliding-window": (3, "owned"), "trees-dfs": (2, "1 to go"),
        "trees-bfs": (3, "owned"), "grid-bfs": (2, "1 to go"),
        "binary-search-array": (1, "2 to go"), "linked-list": (0, "3 to go"),
        "heap": (1, "2 to go"), "intervals": (1, "2 to go"),
    }
    assert tp["owed"] == 13
    assert f"{tp['clean_rate']:.0%}" == "48%"
    assert tp["attempts_needed"] == 27
    assert tp["ready_1_per_day"] == "2026-11-17" and tp["ready_at_55"] == "2026-11-14"
    assert tp["tier2_open"] == ["monotonic-stack", "monotonic-deque", "binary-search-answer",
                                "backtracking", "dp-1d", "graph-toposort", "union-find"]


def test_not_probed_tag():
    tp = rules.tier_progress([], TODAY)
    assert all(r["status"] == "not probed" for r in tp["tier1"])
    assert tp["owed"] == 30 and tp["clean_rate"] == 0


def test_outcome_definition_of_clean():
    assert rules.outcome(0, 15, 10, True, True) == "clean"          # exactly 1.5x is still clean
    assert rules.outcome(0, 16, 10, True, True) == "solved"         # over 1.5x
    assert rules.outcome(1, 5, 10, True, True) == "solved"          # a bug
    assert rules.outcome(0, 5, 10, False, True) == "solved"         # suboptimal (57's pop(0))
    assert rules.outcome(0, 5, 10, True, False) == "solved"         # coach supplied the algorithm


def test_retry_due_ladder():
    today = date(2026, 10, 21)
    items = [
        {"problem_number": 1, "last_on": today - timedelta(days=2), "attempts": 1},   # wait 2
        {"problem_number": 2, "last_on": today - timedelta(days=4), "attempts": 2},   # wait 5
        {"problem_number": 3, "last_on": today - timedelta(days=9), "attempts": 3},   # wait 10
        {"problem_number": 4, "last_on": today - timedelta(days=20), "attempts": 9},  # wait 21
        {"problem_number": 5, "last_on": today, "attempts": 1, "cleared_on": today},  # skipped
    ]
    out = rules.retry_due(items, today)
    assert [(r["problem_number"], r["wait"], r["due"]) for r in out] == [
        (1, 2, True), (2, 5, False), (3, 10, False), (4, 21, False)]


def test_retry_due_golden_queue():
    items = [
        {"problem_number": 862, "last_on": date(2026, 9, 14), "attempts": 1},
        {"problem_number": 200, "last_on": date(2026, 9, 26), "attempts": 2},
        {"problem_number": 1760, "last_on": date(2026, 9, 22), "attempts": 1},
        {"problem_number": 57, "last_on": date(2026, 10, 18), "attempts": 2},
        {"problem_number": 424, "last_on": date(2026, 10, 21), "attempts": 1},
    ]
    out = rules.retry_due(items, TODAY)
    assert [(r["problem_number"], r["due"]) for r in out] == [
        (862, True), (200, True), (1760, True), (57, False), (424, False)]


def test_refire_due():
    ps = [{"pattern_slug": "sliding-window", "retired_on": date(2026, 10, 12), "refires": []},
          {"pattern_slug": "trees-bfs", "retired_on": date(2026, 10, 1),
           "refires": ["2026-10-18"]},
          {"pattern_slug": "heap", "retired_on": None, "refires": []}]
    out = {r["pattern"]: r for r in rules.refire_due(ps, TODAY)}
    assert out["sliding-window"]["due"] and out["sliding-window"]["age"] == 9
    assert not out["trees-bfs"]["due"] and out["trees-bfs"]["refires_done"] == 1
    assert "heap" not in out


def test_blocklist_attempts_and_handouts(csv_attempts):
    handouts = [{"problem_number": 124, "handed_at": datetime(2026, 10, 18, 17, 16)},
                {"problem_number": 999, "handed_at": datetime(2026, 10, 10, 9, 0)}]   # 11 days
    got = rules.blocklist(csv_attempts, handouts, TODAY)
    assert got == {57, 124, 236, 424, 523, 994, 1482}
    # day 7 is free again: a problem touched exactly 7 days ago is not blocked
    assert 5 not in rules.blocklist(
        [{"problem_number": 5, "day": TODAY - timedelta(days=7)}], [], TODAY)
    assert 5 in rules.blocklist(
        [{"problem_number": 5, "day": TODAY - timedelta(days=6)}], [], TODAY)


def test_report_golden(csv_attempts):
    card = rules.report_card(csv_attempts, TODAY, HORIZON)
    t = card["throughput"]
    assert (t["elapsed"], t["active"], t["missed"]) == (42, 16, 26)
    assert (t["completed"], t["abandoned"]) == (44, 4)
    assert f"{t['rate_calendar']:.2f}" == "1.05" and f"{t['rate_active']:.2f}" == "2.75"
    blocks = card["quality"]["blocks"]
    assert len(blocks) == 11
    assert (blocks[0]["span"], blocks[0]["clean"]) == ("09-10-09-15", 2)
    assert f"{blocks[0]['bugs_per_solve']:.2f}" == "1.00"
    assert f"{card['quality']['first6_bugs']:.2f}" == "1.50"
    assert f"{card['quality']['last6_bugs']:.2f}" == "0.83"
    cov = card["coverage"]
    assert (len(cov["owned"]), len(cov["started"]), len(cov["untouched"])) == (3, 10, 4)
    p = card["projection"]
    assert p["tier1_owed"] == 13 and f"{p['clean_ratio']:.0%}" == "44%"
    assert p["tier1_attempts"] == 30 and p["problems_to_attempt"] == 69
    assert p["ready_around"] == "2026-12-25"
    assert p["tier1_ready"] == {"1/day": "2026-11-20", "2/day": "2026-11-05", "3/day": "2026-10-31"}
    assert f"{p['need_rate']:.2f}" == "1.25"
