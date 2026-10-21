"""Problem selection. Ported from status.py + MASTER_CONTEXT, per design doc section 4.

Hard filters first (7-day blocklist incl. handouts, burned patterns this session, no Hards
before the config date, no problem retired by 3 clean solves), then the slot order
warm-up -> drill -> growth -> retry. Inside a slot the pool is PROBLEM_BANK order.

RULE ZERO: only `Handoff` leaves this module. Pattern, slot, tier and difficulty stay in the
internal `Pick` and in the handouts table; `service.reveal()` gives them back after logging.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from . import rules
from .catalog import default_target, leetcode_url
from .models import Attempt, Burned, ClockEvent, Handout, Problem
from . import state

INSTRUCTION = "Give your approach in plain words before any code."
SLOT_ORDER = ["warmup", "drill", "growth", "retry"]

# How the coach behaves while the problem is open. Slot-neutral on purpose: two values, each
# shared by several slots, so the mode alone never names the slot, a pattern or a tier.
#   timed   - strength keeper / re-fire: coach silent, answers direct questions only.
#   coached - drill / growth / retry: full Socratic coaching.
MODES = ("timed", "coached")
MODE_BY_SLOT = {"warmup": "timed", "refire": "timed",
                "drill": "coached", "growth": "coached", "retry": "coached"}


def mode_for(slot):
    """Handoff mode for an internal slot. Unknown slots are coached."""
    return MODE_BY_SLOT.get(slot, "coached")


class Handoff(BaseModel):
    """The ONLY thing the picker returns to the outside. No pattern, slot, tier, difficulty or
    progress. A test pins the field set. `mode` is timed|coached (see MODES), never a slot."""
    model_config = ConfigDict(extra="forbid")

    handout_id: int
    number: int
    title: str
    leetcode_url: str
    paraphrase: str | None = None
    started_at: datetime
    target_min: int
    instruction: str = INSTRUCTION
    mode: Literal["timed", "coached"] = "coached"


@dataclass
class Pick:
    """Internal result: the problem plus why it was picked."""
    problem: Problem
    slot: str
    pattern: str | None


def _pool(problems):
    """PROBLEM_BANK order, then the reproduction list. Attempt-only problems are not in the pool."""
    ps = [p for p in problems.values() if p.bank_order is not None]
    return sorted(ps, key=lambda p: p.bank_order)


def choose(s, user_id, today):
    """Pick without writing a handout. Returns Pick or None."""
    cfg = state.load_config(s)
    sess = state.current_session(s, user_id, today)
    problems = state.all_problems(s)
    atts = state.user_attempts(s, user_id)
    handouts = state.user_handouts(s, user_id)
    retry_all = state.user_retry_items(s, user_id)
    skills = state.user_skills(s, user_id)
    pstate = state.user_pattern_state(s, user_id)

    blocked = rules.blocklist(atts, handouts, today, cfg["block_days"])
    burned = set(s.scalars(select(Burned.pattern_slug).where(Burned.session_id == sess.id)))
    clean_by_problem = {}
    attempted = set()
    for a in atts:
        attempted.add(a.problem_number)
        if a.outcome == "clean":
            clean_by_problem[a.problem_number] = clean_by_problem.get(a.problem_number, 0) + 1
    retired = {n for n, c in clean_by_problem.items() if c >= cfg["clean_to_own"]}
    open_retry = {r.problem_number for r in retry_all}
    pool = _pool(problems)

    def ok(p, in_retry_slot=False):
        if p.number in blocked or p.number in retired:
            return False
        if p.pattern_slug in burned:
            return False
        if p.difficulty == "H" and today < cfg["hards_from"]:
            return False
        if p.number in open_retry and not in_retry_slot:
            return False          # queued problems come back only through the retry ladder
        return True

    def first_for(pattern, new_only=False):
        """First OK problem of a pattern in pool order; never-attempted ones come first."""
        cands = [p for p in pool if p.pattern_slug == pattern and ok(p)]
        if new_only:
            cands = [p for p in cands if p.number not in attempted]
        cands.sort(key=lambda p: p.number in attempted)      # stable: keeps bank order
        return cands[0] if cands else None

    clean, tried = rules.clean_counts(atts)
    last_touch = {}
    for a in atts:
        if a.pattern_slug and (a.pattern_slug not in last_touch or a.day > last_touch[a.pattern_slug]):
            last_touch[a.pattern_slug] = a.day

    def warmup():
        # A re-fire first: a retired pattern, 7+ days since last touch -> a NEW variant.
        for r in rules.refire_due(pstate, today, cfg["refire_days"]):
            if r["due"] and r["refires_done"] < rules.REFIRES_MAX and r["pattern"] not in burned:
                p = first_for(r["pattern"], new_only=True)
                if p:
                    return Pick(p, "refire", r["pattern"])
        # Otherwise a level-4+ strength, the one touched longest ago first.
        strengths = [k for k in skills if k.kind == "pattern" and k.pattern_slug and k.level >= 4]
        strengths.sort(key=lambda k: (last_touch.get(k.pattern_slug, date.min), -k.level))
        for k in strengths:
            p = first_for(k.pattern_slug)
            if p:
                return Pick(p, "warmup", k.pattern_slug)
        return None

    def drill():
        # Lowest-level Tier-1 pattern skill that has data and is not yet owned.
        level = {k.pattern_slug: k.level for k in skills if k.kind == "pattern" and k.pattern_slug}
        cands = [t for t in cfg["tier1"] if tried[t] and clean[t] < cfg["clean_to_own"]]
        cands.sort(key=lambda t: (level.get(t, 3), clean[t], cfg["tier1"].index(t)))
        for t in cands:
            p = first_for(t)
            if p:
                return Pick(p, "drill", t)
        return None

    def growth():
        # Untouched patterns: Tier 1 first, then Tier 2.
        for t in list(cfg["tier1"]) + list(cfg["tier2"]):
            if not tried[t]:
                p = first_for(t)
                if p:
                    return Pick(p, "growth", t)
        return None

    def retry():
        due = [r for r in rules.retry_due(retry_all, today, cfg["ladder"]) if r["due"]]
        due.sort(key=lambda r: -(r["age"] - r["wait"]))      # most overdue first
        for r in due:
            p = problems.get(r["problem_number"])
            if p and ok(p, in_retry_slot=True):
                return Pick(p, "retry", p.pattern_slug)
        return None

    slots = {"warmup": warmup, "drill": drill, "growth": growth, "retry": retry}
    n = sum(1 for h in handouts if h.session_id == sess.id and h.status != "skipped")
    for k in range(len(SLOT_ORDER)):
        pick = slots[SLOT_ORDER[(n + k) % len(SLOT_ORDER)]]()
        if pick:
            return pick
    # Last resort: anything in the pool that passes the hard filters.
    for p in pool:
        if ok(p):
            return Pick(p, "growth", p.pattern_slug)
    return None


def pick_next(s, user_id, today):
    """Choose a problem, record a `handouts` row stamped with the SERVER clock, return the
    public Handoff. None when nothing passes the filters."""
    pick = choose(s, user_id, today)
    if pick is None:
        return None
    sess = state.current_session(s, user_id, today)
    at = datetime.combine(today, datetime.now().time())
    h = Handout(
        user_id=user_id, session_id=sess.id, problem_number=pick.problem.number,
        slot=pick.slot, pattern_slug=pick.pattern, handed_at=at,
        target_min=target_for(s, user_id, pick.problem), status="open",
        clock_state="running", clock_anchor=at, clock_accum_s=0, first_started_at=at,
        checkpoints_fired=[],
    )
    s.add(h)
    s.flush()
    # the server starts the coach's clock at handout
    s.add(ClockEvent(user_id=user_id, handout_id=h.id, action="start", at=at, reason="handout",
                     value={"target_min": h.target_min}))
    return handoff_for(h, pick.problem)


RETRY_TARGET_FACTOR = 0.8


def target_for(s, user_id, problem):
    """Target minutes for a new handout. Seen before: 80% of the FIRST attempt's target
    (retries are tighter: 15 -> 12, 20 -> 16). Else the difficulty default."""
    first = s.scalars(select(Attempt.target_min).where(
        Attempt.user_id == user_id, Attempt.problem_number == problem.number)
        .order_by(Attempt.day, Attempt.id).limit(1)).first()
    if first:
        return max(1, round(RETRY_TARGET_FACTOR * first))
    return default_target(problem.difficulty)


def handoff_for(h, problem):
    return Handoff(
        handout_id=h.id, number=problem.number, title=problem.title,
        leetcode_url=leetcode_url(problem.lc_slug), paraphrase=problem.paraphrase,
        started_at=h.handed_at, target_min=h.target_min, mode=mode_for(h.slot),
    )
