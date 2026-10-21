"""Pure functions ported from the old tools/status.py and tools/report.py.

No database, no clock: every function takes rows and `today`. Rows may be dicts or ORM
objects; `_g` reads either. The attempt fields used are: day, outcome, pattern (or
pattern_slug), problem_number, target_min, actual_min, bugs.
"""
import re
from collections import defaultdict
from datetime import date, timedelta

# ---- constants, as in status.py / report.py ----------------------------------------------
DAILY_MIN = 15
TARGET_MIN = 90
BACKLOG_START = date(2026, 10, 9)    # the day he made the rule
PROBLEMS_PER_DAY = 2                 # uncapped, no decay: his decision, 10/09 and 10/18
LADDER = [2, 5, 10, 21]              # days before repeat 1, 2, 3, 4+
CLEAN_TO_OWN = 3
HARDS_FROM = date(2026, 11, 1)
BLOCK_DAYS = 7
REFIRE_DAYS = 7
REFIRES_MAX = 3
CLEAN_TIME_FACTOR = 1.5

TIER1 = [
    "prefix-hashmap", "two-pointers", "sliding-window", "trees-dfs", "trees-bfs",
    "grid-bfs", "binary-search-array", "linked-list", "heap", "intervals",
]
TIER2 = [
    "monotonic-stack", "monotonic-deque", "binary-search-answer",
    "backtracking", "dp-1d", "graph-toposort", "union-find",
]
CORE = TIER1 + TIER2


def _g(row, *names, default=None):
    """Read the first present field from a dict or an object."""
    for n in names:
        if isinstance(row, dict):
            if n in row:
                return row[n]
        elif hasattr(row, n):
            return getattr(row, n)
    return default


def _pattern(r):
    return _g(r, "pattern_slug", "pattern")


def _done(attempts):
    return [r for r in attempts if _g(r, "outcome") != "abandoned"]


# ---- outcome -----------------------------------------------------------------------------
def outcome(bugs, actual_min, target_min, optimal, derived_cold):
    """PLAN.md "what clean means": zero bugs AND <= 1.5x target AND optimal AND derived cold.
    Anything else that was completed is `solved`. `abandoned` is set explicitly by the caller."""
    if bugs == 0 and actual_min <= CLEAN_TIME_FACTOR * target_min and optimal and derived_cold:
        return "clean"
    return "solved"


# ---- today's numbers (status.py quota) ---------------------------------------------------
def _per_day(attempts):
    per_day = defaultdict(int)
    for r in _done(attempts):
        per_day[_g(r, "day")] += 1
    return per_day


def backlog(attempts, today, start=BACKLOG_START, per_day=PROBLEMS_PER_DAY):
    """Backlog in PROBLEMS: `per_day` owed for every calendar day since `start`, every completed
    problem (new or retry) pays one off, abandoned ones do not count. No cap, no decay."""
    pd = _per_day(attempts)
    days = max(0, (today - start).days)
    owed_before = days * per_day
    done_before = sum(v for d, v in pd.items() if start <= d < today)
    owed = max(0, owed_before - done_before)
    done_today = pd.get(today, 0)
    target = per_day + owed
    return {
        "backlog": owed,
        "target_today": target,
        "done_today": done_today,
        "remaining": max(0, target - done_today),
        "days": days,
        "owed_before": owed_before,
        "done_before": done_before,
        "per_day": per_day,
        "start": start.isoformat(),
    }


def streak(attempts, today):
    """Current streak (today counts if done, else count back from yesterday), longest ever, and
    a 14-day bar, oldest -> today."""
    pd = _per_day(attempts)
    days = sorted(_g(r, "day") for r in attempts)
    first = days[0] if days else today
    cur_streak = 0
    d = today if pd.get(today) else today - timedelta(days=1)
    while pd.get(d):
        cur_streak += 1
        d -= timedelta(days=1)
    best = cur = 0
    d = first
    while d <= today:
        cur = cur + 1 if pd.get(d) else 0
        best = max(best, cur)
        d += timedelta(days=1)
    bar = "".join("#" if pd.get(today - timedelta(days=13 - i)) else "." for i in range(14))
    return {"current": cur_streak, "longest": best, "last14": bar}


# ---- tier bars ---------------------------------------------------------------------------
def clean_counts(attempts):
    clean = defaultdict(int)
    tried = defaultdict(int)
    for r in attempts:
        p = _pattern(r)
        tried[p] += 1
        if _g(r, "outcome") == "clean":
            clean[p] += 1
    return clean, tried


def tier_progress(attempts, today=None, clean_to_own=CLEAN_TO_OWN):
    """Per Tier-1 and Tier-2 pattern: clean count, still needed, owned / N to go / not probed.
    Plus owed, clean rate and the status.py ready-date estimates (when `today` is given)."""
    clean, tried = clean_counts(attempts)

    def row(p, tier):
        need = max(0, clean_to_own - clean[p])
        status = "owned" if need == 0 else (f"{need} to go" if tried[p] else "not probed")
        return {"pattern": p, "tier": tier, "clean": clean[p], "needed": need,
                "tried": tried[p], "status": status}

    t1 = [row(p, 1) for p in TIER1]
    t2 = [row(p, 2) for p in TIER2]
    owed = sum(r["needed"] for r in t1)
    done = _done(attempts)
    cleans = sum(1 for r in attempts if _g(r, "outcome") == "clean")
    rate = cleans / len(done) if done else 0
    out = {"tier1": t1, "tier2": t2, "owed": owed, "clean_rate": rate,
           "tier2_open": [r["pattern"] for r in t2 if r["clean"] < clean_to_own]}
    if rate > 0:
        need_att = round(owed / rate)
        out["attempts_needed"] = need_att
        if today:
            out["ready_1_per_day"] = (today + timedelta(days=need_att)).isoformat()
            out["ready_at_55"] = (today + timedelta(days=round(owed / 0.55))).isoformat()
    return out


# ---- retry queue and re-fire -------------------------------------------------------------
def retry_due(retry_items, today, ladder=LADDER):
    """Each open item: age since the last attempt, the wait for its attempt count, a due flag.
    Order is the order given. Cleared items are skipped."""
    out = []
    for it in retry_items:
        if _g(it, "cleared_on"):
            continue
        n = _g(it, "attempts", default=1) or 1
        last = _g(it, "last_on", "added")
        wait = ladder[min(max(n, 1) - 1, len(ladder) - 1)]
        if last is None:
            out.append({"problem_number": _g(it, "problem_number"), "attempts": n,
                        "age": None, "wait": wait, "due": False, "last_on": None})
            continue
        age = (today - last).days
        out.append({"problem_number": _g(it, "problem_number"), "attempts": n, "age": age,
                    "wait": wait, "due": age >= wait, "last_on": last,
                    "reason": _g(it, "reason", default="")})
    return out


def refire_due(pattern_state, today, days=REFIRE_DAYS):
    """Retired patterns: age since the latest of retirement / re-fire, DUE at `days` or more."""
    out = []
    for ps in pattern_state:
        retired = _g(ps, "retired_on")
        if not retired:
            continue
        last = retired
        refires = [date.fromisoformat(x) if isinstance(x, str) else x
                   for x in (_g(ps, "refires") or [])]
        if refires:
            last = max(last, max(refires))
        age = (today - last).days
        out.append({"pattern": _g(ps, "pattern_slug"), "age": age, "due": age >= days,
                    "refires_done": len(refires), "last_touch": last})
    return sorted(out, key=lambda r: r["pattern"])


# ---- 7-day blocklist ---------------------------------------------------------------------
def blocklist(attempts, handouts, today, days=BLOCK_DAYS):
    """Problem numbers touched (attempted OR handed out) within the last `days` days.

    status.py derives this from session-log tables instead. Same rule (a day `d` blocks while
    (today - d) < 7); the SOURCE differs, see migrate/verify.py for the known differences."""
    out = set()
    for r in attempts:
        d = _g(r, "day")
        if d is not None and (today - d).days < days:
            out.add(int(_g(r, "problem_number", "problem")))
    for h in handouts:
        at = _g(h, "handed_at")
        d = at.date() if hasattr(at, "date") else at
        if d is not None and (today - d).days < days:
            out.add(int(_g(h, "problem_number")))
    return out


# ---- report card (report.py) -------------------------------------------------------------
def throughput(rows, today):
    first = min(_g(r, "day") for r in rows)
    elapsed = (today - first).days + 1
    active = len({_g(r, "day") for r in rows})
    done = _done(rows)
    bailed = [r for r in rows if _g(r, "outcome") == "abandoned"]
    return {
        "first": first.isoformat(), "elapsed": elapsed, "active": active,
        "missed": elapsed - active, "consistency": active / elapsed,
        "completed": len(done), "abandoned": len(bailed),
        "rate_calendar": len(done) / elapsed, "rate_active": len(done) / active,
    }


def quality(rows):
    """Blocks of 4 completed solves, oldest -> newest, and first-6 versus last-6 bugs."""
    done = _done(rows)
    blocks = []
    for i in range(0, len(done), 4):
        chunk = done[i:i + 4]
        if not chunk:
            continue
        d0, d1 = _g(chunk[0], "day").isoformat(), _g(chunk[-1], "day").isoformat()
        blocks.append({
            "span": f"{d0[5:]}-{d1[5:]}", "solves": len(chunk),
            "bugs_per_solve": sum(_g(c, "bugs") for c in chunk) / len(chunk),
            "time_vs_target": sum(_g(c, "actual_min") / _g(c, "target_min") for c in chunk) / len(chunk),
            "clean": sum(1 for c in chunk if _g(c, "outcome") == "clean"),
        })
    out = {"blocks": blocks}
    if done:
        recent, older = done[-6:], done[:6]
        out["first6_bugs"] = sum(_g(r, "bugs") for r in older) / len(older)
        out["last6_bugs"] = sum(_g(r, "bugs") for r in recent) / len(recent)
    return out


def coverage(rows, clean_to_own=CLEAN_TO_OWN):
    clean, touched = clean_counts(rows)
    owned = [p for p in CORE if clean[p] >= clean_to_own]
    started = [p for p in CORE if touched[p] and p not in owned]
    dark = [p for p in CORE if not touched[p]]
    return {
        "total": len(CORE), "owned": owned, "started": started, "untouched": dark,
        "counts": {p: {"clean": clean[p], "attempts": touched[p]} for p in CORE},
    }


def projection(rows, today, horizon_days, clean_to_own=CLEAN_TO_OWN):
    """report.py projection. NB: here the clean ratio is clean / ALL rows (abandoned included),
    unlike status.py which divides by completed rows. Both are kept as in the originals."""
    tp = throughput(rows, today)
    cov = coverage(rows, clean_to_own)
    clean, _ = clean_counts(rows)
    owed = sum(max(0, clean_to_own - clean[p]) for p in cov["started"])
    owed += len(cov["untouched"]) * clean_to_own
    clean_ratio = sum(1 for r in rows if _g(r, "outcome") == "clean") / len(rows)
    total_problems = round(owed / max(clean_ratio, 0.35))
    cal_rate = tp["rate_calendar"]
    days_at_pace = total_problems / cal_rate

    t1_owed = sum(max(0, clean_to_own - clean[p]) for p in TIER1)
    t1_att = round(t1_owed / max(clean_ratio, 0.35))
    t1_dates = {f"{n}/day": (today + timedelta(days=round(t1_att / n))).isoformat() for n in (1, 2, 3)}
    need_rate = total_problems / horizon_days if horizon_days else float("inf")
    per_active = tp["rate_active"]
    week = {}
    for label, dpw in (("5 days/week", 5), ("6 days/week", 6), ("7 days/week", 7)):
        rate = per_active * dpw / 7
        week[label] = {"rate": rate, "weeks": total_problems / (rate * 7)}
    return {
        "tier1_owed": t1_owed, "clean_ratio": clean_ratio, "tier1_attempts": t1_att,
        "tier1_ready": t1_dates,
        "owed": owed, "problems_to_attempt": total_problems, "cal_rate": cal_rate,
        "days_at_pace": days_at_pace, "weeks_at_pace": days_at_pace / 7,
        "ready_around": (today + timedelta(days=int(days_at_pace))).isoformat(),
        "horizon_days": horizon_days, "need_rate": need_rate, "need_rate_delta": need_rate - cal_rate,
        "per_active": per_active, "week": week, "consistency": tp["consistency"],
    }


def report_card(rows, today, horizon_days):
    """Everything report.py prints, as one dict."""
    rows = sorted(rows, key=lambda r: _g(r, "day"))
    # No completed problem yet -> no pace -> nothing to project (report.py would divide by 0).
    if not any(_g(r, "outcome") != "abandoned" for r in rows):
        return {}
    return {
        "today": today.isoformat(), "horizon_days": horizon_days,
        "throughput": throughput(rows, today), "quality": quality(rows),
        "coverage": coverage(rows), "projection": projection(rows, today, horizon_days),
    }


# ---- accountability ------------------------------------------------------------------------
def missed_days(practice_days, today):
    """{last_practice_day, days_missed, missed_days_list}: the calendar days strictly between
    the last practice day before `today` and `today`. Practised yesterday -> 0 and []. Today
    itself is never counted (the session is today). No practice at all -> None, None, []."""
    days = [d for d in practice_days if d is not None and d < today]
    if not days:
        if any(d == today for d in practice_days if d is not None):
            return {"last_practice_day": today, "days_missed": 0, "missed_days_list": []}
        return {"last_practice_day": None, "days_missed": None, "missed_days_list": []}
    last = max(days)
    gap = [last + timedelta(days=i) for i in range(1, (today - last).days)]
    return {"last_practice_day": last, "days_missed": len(gap), "missed_days_list": gap}


# ---- pattern-note traps --------------------------------------------------------------------
# A traps section is a heading that STARTS with one of these ("## Traps", "## Where it bites",
# "### Common pitfalls"); a heading that merely mentions traps ("no traps here") is not one.
_TRAP_HEADING = re.compile(r"^(?:common |known |the )?(?:traps?|pitfalls?|gotchas?|where it bites)(?![a-z])")
_ARROWS = ("->", "→", "=>")


def _heading(line):
    s = line.lstrip()
    if not s.startswith("#"):
        return None
    level = len(s) - len(s.lstrip("#"))
    return level, s[level:].strip().lower()


def _bullet(line):
    """(indent, text) for a markdown list item, else None."""
    s = line.lstrip(" \t")
    indent = len(line) - len(s)
    for mark in ("- ", "* ", "+ "):
        if s.startswith(mark):
            return indent, s[len(mark):]
    head = s.split(" ", 1)
    if len(head) == 2 and head[0][:-1].isdigit() and head[0][-1:] in ".)":
        return indent, head[1]
    return None


def parse_traps(note_md):
    """Traps from a pattern note: every top-level bullet under a heading that names traps
    ("## Traps", "## Where it bites", pitfalls, gotchas), until the next heading of the same or
    a higher level. Continuation lines and sub-bullets join their bullet. A bullet written
    `cue -> rule` splits into {"cue", "trap"}; without an arrow both are the whole text.
    Returns [] when the note has no such section."""
    out, cur, base, in_sec, sec_level, gap = [], None, None, False, 0, False

    def flush():
        if cur is not None:
            text = " ".join(" ".join(cur).split())
            if text:
                for a in _ARROWS:
                    if a in text:
                        cue, trap = (x.strip() for x in text.split(a, 1))
                        out.append({"cue": cue, "trap": trap})
                        return
                out.append({"cue": text, "trap": text})

    for line in (note_md or "").splitlines():
        h = _heading(line)
        if h is not None:
            if in_sec and h[0] <= sec_level:
                flush()
                cur, in_sec = None, False
            if not in_sec and _TRAP_HEADING.match(h[1]):
                in_sec, sec_level, base, cur = True, h[0], None, None
            continue
        if not in_sec:
            continue
        b = _bullet(line)
        if not line.strip():
            gap = True
            continue
        if b is not None and (base is None or b[0] <= base):
            flush()
            base, cur = b[0], [b[1]]
        elif cur is not None:
            if b is None and gap and not line[:1].isspace():
                flush()                  # a plain paragraph after the list ends the bullet
                cur = None
            else:
                cur.append(b[1] if b is not None else line.strip())
        gap = False
    if in_sec:
        flush()
    return out
