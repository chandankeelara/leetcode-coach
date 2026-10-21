"""The one API the web UI and the MCP bridge both call. Every function opens its own database
session, commits, and returns plain dicts / the public Handoff. See CORE_API.md.

`today` is an optional keyword everywhere so tests can pin the date; callers normally omit it.
RULE ZERO: only `next_problem()` and `reveal()` touch pattern/slot data, and `reveal()` refuses
until the attempt is logged. `status()` and `report()` are progress talk: the UI/bridge must not
show them in the same screen/message as a problem handoff.
"""
import asyncio
from datetime import date, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select

from . import picker, rules, state
from .catalog import default_target, pattern_name, pattern_tier
from .db import session_scope
from .models import (Attempt, Burned, CoachMessage, CoachSession, Event, Handout, Pattern,
                     PatternState, Problem, RetryItem, Skill, User)

_hasher = PasswordHasher()


class RevealBlocked(Exception):
    """reveal() before the attempt is logged."""


def _today(today):
    return today or date.today()


# ---- users ---------------------------------------------------------------------------------
def create_user(username, password):
    """New user, or set the password of an existing one that has none yet (the migration
    creates the owner without a password). Returns user_id."""
    with session_scope() as s:
        u = s.scalars(select(User).where(User.username == username)).first()
        h = _hasher.hash(password)
        if u is None:
            u = User(username=username, password_hash=h)
            s.add(u)
            s.flush()
        elif u.password_hash == "!":
            u.password_hash = h
        else:
            raise ValueError(f"user {username!r} already exists")
        return u.id


def set_password(username, password):
    with session_scope() as s:
        u = s.scalars(select(User).where(User.username == username)).one()
        u.password_hash = _hasher.hash(password)


def verify_user(username, password):
    """user_id when the password matches, else None."""
    with session_scope() as s:
        u = s.scalars(select(User).where(User.username == username)).first()
        if u is None or u.password_hash == "!":
            return None
        try:
            _hasher.verify(u.password_hash, password)
        except (VerificationError, InvalidHashError):
            return None
        return u.id


def default_user_id():
    """The first user: this app is single-user. None if there is none."""
    with session_scope() as s:
        return s.scalars(select(User.id).order_by(User.id)).first()


# ---- sessions ------------------------------------------------------------------------------
def start_session(user_id, today=None, label=None):
    """Open a session for today (reuses an already-open one). Returns session_id."""
    with session_scope() as s:
        row = state.current_session(s, user_id, _today(today))
        if label:
            row.label = label
        return row.id


def end_session(session_id, log_md=None):
    with session_scope() as s:
        row = s.get(CoachSession, session_id)
        row.ended_at = datetime.now()
        if log_md is not None:
            row.log_md = log_md
        return row.id


# ---- status (progress talk) ----------------------------------------------------------------
def status(user_id, today=None):
    today = _today(today)
    with session_scope() as s:
        cfg = state.load_config(s)
        atts = state.user_attempts(s, user_id)
        handouts = state.user_handouts(s, user_id)
        problems = state.all_problems(s)
        bl = rules.backlog(atts, today, cfg["backlog_start"], cfg["per_day"])
        bl.pop("per_day", None)
        retry = rules.retry_due(state.user_retry_items(s, user_id), today, cfg["ladder"])
        for r in retry:
            p = problems.get(r["problem_number"])
            r["title"] = p.title if p else ""
        skills = [k for k in state.user_skills(s, user_id)]
        row = lambda k: {"name": k.name, "level": k.level, "trend": k.trend, "evidence": k.evidence}
        weakest = sorted(skills, key=lambda k: (k.level, k.name))[:5]
        # same order as status.py: level high -> low, ties by name descending
        strongest = sorted([k for k in skills if k.level >= 4], key=lambda k: (k.level, k.name),
                           reverse=True)
        return {
            "today": today.isoformat(),
            "backlog": bl,
            "streak": rules.streak(atts, today),
            "tiers": rules.tier_progress(atts, today, cfg["clean_to_own"]),
            "retry": retry,
            "refire": rules.refire_due(state.user_pattern_state(s, user_id), today, cfg["refire_days"]),
            "blocklist": sorted(rules.blocklist(atts, handouts, today, cfg["block_days"])),
            "weakest": [row(k) for k in weakest],
            "strongest": [row(k) for k in strongest],
        }


# ---- the handoff ---------------------------------------------------------------------------
def next_problem(user_id, today=None, force_new=False):
    """The public Handoff for the next problem, or None. If a handout from today is still open
    it is returned again (same handout_id, same start time) unless force_new, which skips it."""
    today = _today(today)
    with session_scope() as s:
        open_h = s.scalars(
            select(Handout).where(Handout.user_id == user_id, Handout.status == "open",
                                  Handout.slot != "imported")
            .order_by(Handout.id.desc())
        ).first()
        if open_h is not None and open_h.handed_at.date() == today:
            if not force_new:
                return picker.handoff_for(open_h, s.get(Problem, open_h.problem_number))
            open_h.status = "skipped"
        return picker.pick_next(s, user_id, today)


def skip_problem(handout_id):
    """Mark an open handout skipped (it still counts for the 7-day blocklist)."""
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h.status == "open":
            h.status = "skipped"


# ---- logging -------------------------------------------------------------------------------
def _retry_reason(actual, target, bugs, hints, optimal, derived_cold, abandoned, notes):
    bits = [f"{actual} min vs {target}", f"{bugs} bug(s)"]
    if hints:
        bits.append(f"{hints} hint(s)")
    if not optimal:
        bits.append("not optimal")
    if not derived_cold:
        bits.append("not derived cold")
    if abandoned:
        bits.append("abandoned")
    text = ", ".join(bits)
    return f"{text}. {notes}".strip() if notes else text


def log_attempt(user_id, handout_id=None, number=None, actual_min=0, bugs=0, optimal=True,
                derived_cold=True, hints=0, notes="", abandoned=False, target_min=None,
                day=None):
    """Record an attempt. The outcome is computed here (rules.outcome); nobody types "clean".

    Retry queue (MASTER_CONTEXT): add if over 1.5x target, 2+ bugs, a hint, suboptimal, not
    derived cold, or abandoned; clear on a clean solve; an item already queued gets attempts+1.
    Returns {attempt_id, outcome, retry: added|bumped|cleared|none, problem_number}."""
    day = day or date.today()
    with session_scope() as s:
        h = None
        if handout_id is not None:
            h = s.get(Handout, handout_id)
            number = h.problem_number
        elif number is not None:
            h = s.scalars(select(Handout).where(
                Handout.user_id == user_id, Handout.problem_number == number,
                Handout.status == "open").order_by(Handout.id.desc())).first()
        if number is None:
            raise ValueError("log_attempt needs a handout_id or a problem number")
        prob = s.get(Problem, number)
        if prob is None:
            raise ValueError(f"unknown problem {number}; add it to the problems table first")
        if target_min is None:
            target_min = h.target_min if h else default_target(prob.difficulty)
        if hints > 0:
            derived_cold = False        # a hint means the coach supplied part of the idea
        if abandoned:
            result = "abandoned"
        else:
            result = rules.outcome(bugs, actual_min, target_min, optimal, derived_cold)
        att = Attempt(
            user_id=user_id, problem_number=number, pattern_slug=prob.pattern_slug, day=day,
            handout_id=h.id if h else None, target_min=target_min, actual_min=actual_min,
            bugs=bugs, hints=hints, optimal=optimal, derived_cold=derived_cold,
            outcome=result, notes=notes or "", source="app")
        s.add(att)
        s.flush()
        if h:
            h.status = "logged"

        # retry queue
        should_queue = (abandoned or actual_min > rules.CLEAN_TIME_FACTOR * target_min
                        or bugs >= 2 or hints > 0 or not optimal or not derived_cold)
        item = s.scalars(select(RetryItem).where(
            RetryItem.user_id == user_id, RetryItem.problem_number == number,
            RetryItem.cleared_on.is_(None))).first()
        reason = _retry_reason(actual_min, target_min, bugs, hints, optimal, derived_cold,
                               abandoned, notes)
        action = "none"
        if result == "clean":
            if item:
                item.cleared_on = day
                action = "cleared"
        elif item:
            item.attempts += 1
            item.last_on = day
            item.reason = reason
            action = "bumped"
        elif should_queue:
            s.add(RetryItem(user_id=user_id, problem_number=number, added=day, last_on=day,
                            reason=reason, attempts=1))
            action = "added"

        _update_pattern_state(s, user_id, prob.pattern_slug, h, day)
        return {"attempt_id": att.id, "outcome": result, "retry": action,
                "problem_number": number}


def _update_pattern_state(s, user_id, slug, handout, day):
    """Retire a pattern at CLEAN_TO_OWN clean solves; log a re-fire when a refire handout is done."""
    if not slug:
        return
    ps = s.scalars(select(PatternState).where(
        PatternState.user_id == user_id, PatternState.pattern_slug == slug)).first()
    if handout is not None and handout.slot == "refire" and ps is not None:
        ps.refires = list(ps.refires or []) + [day.isoformat()]
        return
    if ps is not None and ps.retired_on:
        return
    cleans = s.scalars(select(Attempt).where(
        Attempt.user_id == user_id, Attempt.pattern_slug == slug, Attempt.outcome == "clean")).all()
    if len(cleans) >= rules.CLEAN_TO_OWN:
        if ps is None:
            s.add(PatternState(user_id=user_id, pattern_slug=slug, retired_on=day, refires=[]))
        else:
            ps.retired_on = day


# ---- reveal / burn -------------------------------------------------------------------------
def reveal(handout_id):
    """Pattern + slot of a handout. Only after its attempt is logged."""
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None:
            raise ValueError(f"no handout {handout_id}")
        if h.status != "logged":
            raise RevealBlocked("log the attempt first; the pattern is revealed afterwards")
        p = s.get(Problem, h.problem_number)
        slug = h.pattern_slug or p.pattern_slug
        return {"handout_id": h.id, "number": p.number, "title": p.title, "slot": h.slot,
                "pattern": slug, "pattern_name": pattern_name(slug) if slug else None,
                "tier": pattern_tier(slug) if slug else None}


def burn_pattern(user_id, slug, reason="", today=None):
    """RULE ZERO leak: the pattern is out of play for this session."""
    with session_scope() as s:
        sess = state.current_session(s, user_id, _today(today))
        b = Burned(session_id=sess.id, pattern_slug=slug, reason=reason)
        s.add(b)
        s.flush()
        return b.id


# ---- events and chat -----------------------------------------------------------------------
def push_event(user_id, kind, payload=None):
    """Web UI -> queue. Returns the event id."""
    with session_scope() as s:
        e = Event(user_id=user_id, kind=kind, payload=payload or {})
        s.add(e)
        s.flush()
        return e.id


def _drain(user_id):
    with session_scope() as s:
        rows = list(s.scalars(select(Event).where(
            Event.user_id == user_id, Event.consumed.is_(False)).order_by(Event.id)))
        for e in rows:
            e.consumed = True
        return [{"id": e.id, "kind": e.kind, "payload": e.payload,
                 "created_at": e.created_at.isoformat()} for e in rows]


async def wait_event(user_id, timeout_s=25.0, poll_s=0.25):
    """Long-poll: return (and mark consumed) every pending event as soon as there is one, or
    [] when `timeout_s` passes with none."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        got = _drain(user_id)
        if got:
            return got
        if loop.time() >= deadline:
            return []
        await asyncio.sleep(poll_s)


def post_message(user_id, role, content, handout_id=None):
    """Chat panel message. role: user | coach | system. Returns the message id."""
    with session_scope() as s:
        m = CoachMessage(user_id=user_id, handout_id=handout_id, role=role, content=content)
        s.add(m)
        s.flush()
        return m.id


def list_messages(user_id, since_id=0, limit=200):
    with session_scope() as s:
        rows = s.scalars(select(CoachMessage).where(
            CoachMessage.user_id == user_id, CoachMessage.id > since_id)
            .order_by(CoachMessage.id).limit(limit))
        return [{"id": m.id, "role": m.role, "content": m.content, "handout_id": m.handout_id,
                 "created_at": m.created_at.isoformat()} for m in rows]


# ---- report --------------------------------------------------------------------------------
def report(user_id, target_date=None, today=None):
    """report.py as a dict. `target_date` is the horizon end (default: 8 weeks out)."""
    today = _today(today)
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    horizon = (target_date - today).days if target_date else 56
    with session_scope() as s:
        atts = state.user_attempts(s, user_id)
        return rules.report_card(atts, today, horizon)
