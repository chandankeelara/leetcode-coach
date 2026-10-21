"""The one API the web UI and the MCP bridge both call. Every function opens its own database
session, commits, and returns plain dicts / the public Handoff. See CORE_API.md.

`today` is an optional keyword everywhere so tests can pin the date; callers normally omit it.
RULE ZERO: only `next_problem()` and `reveal()` touch pattern/slot data, and `reveal()` refuses
until the attempt is logged. `status()` and `report()` are progress talk: the UI/bridge must not
show them in the same screen/message as a problem handoff.
"""
import asyncio
import re
from datetime import date, datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select

from . import clock as clk
from . import picker, rules, state
from .catalog import default_target, pattern_name, pattern_tier
from .db import session_scope
from .models import (Attempt, Burned, ClockEvent, CoachMessage, Config, CoachSession, Event,
                     Handout, Pattern,
                     PatternState, Problem, RetryItem, Skill, SkillEvent, TrapRecall, UiEvent,
                     User, Visual)

_hasher = PasswordHasher()


class RevealBlocked(Exception):
    """reveal() before the attempt is logged."""


def _today(today):
    return today or date.today()


# ---- live page signal ----------------------------------------------------------------------
UI_KINDS = frozenset({"message", "statement", "visual", "editor", "handout", "clock",
                      "presence"})
UI_KEEP_S = 3600            # ui_events rows older than this are pruned on each emit


def emit(user_id, kind, ref_id=None, s=None):
    """Tell open pages something changed: append a ui_events row (user_id None = everyone).
    The web process streams these over SSE. Pass `s` to write inside the caller's transaction,
    so a page never sees the signal before the data. Returns the row id."""
    if kind not in UI_KINDS:
        raise ValueError(f"unknown ui event kind {kind!r}")
    if s is None:
        with session_scope() as s2:
            return emit(user_id, kind, ref_id, s2)
    s.execute(delete(UiEvent).where(
        UiEvent.created_at < datetime.now() - timedelta(seconds=UI_KEEP_S)))
    e = UiEvent(user_id=user_id, kind=kind, ref_id=ref_id)
    s.add(e)
    s.flush()
    return e.id


def ui_events_since(user_id, since_id=0, limit=200):
    """[{"id", "kind", "ref_id"}] of ui_events after since_id for this user (and everyone)."""
    with session_scope() as s:
        rows = s.scalars(select(UiEvent).where(
            UiEvent.id > (since_id or 0),
            (UiEvent.user_id == user_id) | UiEvent.user_id.is_(None))
            .order_by(UiEvent.id).limit(limit))
        return [{"id": e.id, "kind": e.kind, "ref_id": e.ref_id} for e in rows]


def ui_last_id():
    """Newest ui_events id (0 if none): where a freshly opened stream starts."""
    from sqlalchemy import func
    with session_scope() as s:
        return s.scalar(select(func.max(UiEvent.id))) or 0


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
            "accountability": rules.missed_days([a.day for a in atts], today),
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
        skipped = False
        if open_h is not None and open_h.handed_at.date() == today:
            if not force_new:
                return picker.handoff_for(open_h, s.get(Problem, open_h.problem_number))
            open_h.status = "skipped"
            skipped = True
        out = picker.pick_next(s, user_id, today)
        if out is not None or skipped:
            emit(user_id, "handout", out.handout_id if out is not None else open_h.id, s)
        return out


def skip_problem(handout_id):
    """Mark an open handout skipped (it still counts for the 7-day blocklist)."""
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h.status == "open":
            h.status = "skipped"
            emit(h.user_id, "handout", h.id, s)


def reset_timer(user_id, handout_id):
    """Alias of clock(..., "reset", reason="reset"): the coach clock back to 0 from now (wall
    time keeps counting). ValueError unless it is his and open. Also emits `handout`.
    Returns {"ok": True, "handout_id", "started_at"}."""
    clock(user_id, handout_id, "reset", reason="reset")
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        emit(user_id, "handout", h.id, s)
        return {"ok": True, "handout_id": h.id, "started_at": h.handed_at.isoformat()}


# ---- the coach's clock (coach/clock.py has the math) ----------------------------------------
def _materialize_clock(h):
    """Old rows have NULL clock columns: running since handed_at, accum 0."""
    if h.first_started_at is None:
        h.first_started_at = h.handed_at
    if h.clock_anchor is None:
        h.clock_anchor = h.handed_at
    if h.clock_accum_s is None:
        h.clock_accum_s = 0
    if h.clock_state is None:
        h.clock_state = "running"


def _clock_events(s, handout_id):
    return list(s.scalars(select(ClockEvent).where(ClockEvent.handout_id == handout_id)
                          .order_by(ClockEvent.id)))


def clock(user_id, handout_id, action, reason="", target_min=None, label=None, now=None):
    """The coach owns the clock. action: start | pause | resume | reset | target | phase.
      start   running from now (no-op if running; sets first_started_at if unset)
      pause   bank elapsed, freeze. No reason -> stored "" and a "warning": "say why"
      resume  running from now (no-op if running)
      reset   coach clock to 0 from now, running; wall time and first_started_at unchanged;
              handed_at = now (the handoff's start time); checkpoints re-arm.
              No reason -> "warning": "say why"
      target  target_min = 1..180; checkpoints re-arm
      phase   neutral label next to the timer ("approach", "code"); leak-guarded like a
              statement: ValueError("leak: ...") and nothing stored. "" clears it.
    Every action writes a clock_events row and emits `clock`. ValueError unless the handout
    is his and open, or on a bad action/target. Returns clock.state(...) + handout_id
    (+ "warning")."""
    if action not in clk.ACTIONS:
        raise ValueError(f"unknown clock action {action!r}; one of {', '.join(clk.ACTIONS)}")
    reason = (reason or "").strip()
    now = now or datetime.now()
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None or h.user_id != user_id or h.status != "open":
            raise ValueError(f"no open handout {handout_id}")
        _materialize_clock(h)
        value, warning = {}, None
        if action in ("start", "resume"):
            if h.clock_state != "running":
                h.clock_state, h.clock_anchor = "running", now
        elif action == "pause":
            if h.clock_state == "running":
                h.clock_accum_s = clk.elapsed_s(h, now)
                h.clock_state, h.clock_anchor = "paused", now
            if not reason:
                warning = "say why"
        elif action == "reset":
            h.clock_accum_s, h.clock_anchor, h.clock_state = 0, now, "running"
            h.handed_at = now
            h.checkpoints_fired = []
            if not reason:
                warning = "say why"
        elif action == "target":
            if isinstance(target_min, bool):
                raise ValueError("target_min must be 1..180")
            try:
                n = int(target_min)
            except (TypeError, ValueError):
                raise ValueError("target needs target_min, an int 1..180") from None
            if not clk.TARGET_MIN <= n <= clk.TARGET_MAX:
                raise ValueError("target_min must be 1..180")
            h.target_min = n
            h.checkpoints_fired = []
            value = {"target_min": n}
        elif action == "phase":
            text = re.sub(r"\s+", " ", (label or "")).strip()
            if len(text) > 40:
                raise ValueError("phase label: 40 characters max")
            bad = leak_words(text, s.get(Problem, h.problem_number).title, s)
            if bad:
                raise ValueError("leak: " + ", ".join(bad))
            h.phase_label = text or None
            value = {"label": text}
        s.add(ClockEvent(user_id=user_id, handout_id=h.id, action=action, at=now,
                         reason=reason, value=value))
        s.flush()
        emit(user_id, "clock", h.id, s)
        out = clk.state(h, now, _clock_events(s, h.id))
        out["handout_id"] = h.id
        if warning:
            out["warning"] = warning
        return out


def clock_state(user_id, handout_id, now=None):
    """clock.state(...) + handout_id of a handout (any status), or None if it is not his."""
    now = now or datetime.now()
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None or h.user_id != user_id:
            return None
        out = clk.state(h, now, _clock_events(s, h.id))
        out["handout_id"] = h.id
        return out


def clock_record(s, h, end=None):
    """{coach_min, wall_min, pauses: [{reason, minutes, at}], resets: [{reason, at}]} for a
    handout: as stored on its attempt when logged, else live."""
    a = s.scalars(select(Attempt).where(Attempt.handout_id == h.id)
                  .order_by(Attempt.id.desc())).first()
    end = end or (a.created_at if a is not None else datetime.now())
    rec = clk.record(_clock_events(s, h.id), end)
    coach_min = a.actual_min if a is not None else max(1, round(clk.elapsed_s(h, end) / 60))
    if a is not None and a.wall_min is not None:
        wall = a.wall_min
    else:
        wall = round(clk.wall_s(h, end) / 60)
    return {"coach_min": coach_min, "wall_min": wall, **rec}


def _fire_checkpoints(user_id, now=None):
    """Called from wait_event's loop: for his open handout, push ONE `clock_checkpoint` event
    when the coach clock first crosses 50 / 100 / 150 % of target (never while paused). If
    several are crossed at once only the highest is pushed; all are marked fired. A reset or
    a target change re-arms them. Returns the event id or None."""
    now = now or datetime.now()
    with session_scope() as s:
        h = s.scalars(select(Handout).where(
            Handout.user_id == user_id, Handout.status == "open",
            Handout.slot != "imported").order_by(Handout.id.desc())).first()
        if h is None:
            return None
        due = clk.due_checkpoints(h, now)
        if not due:
            return None
        h.checkpoints_fired = sorted(set(h.checkpoints_fired or []) | set(due))
        appr = s.scalars(select(Event).where(
            Event.user_id == user_id, Event.kind == "approach",
            Event.created_at >= clk.first_started(h) - timedelta(seconds=2)))
        has_approach = any((e.payload or {}).get("handout_id") == h.id for e in appr)
        e = Event(user_id=user_id, kind="clock_checkpoint", payload={
            "pct": max(due), "elapsed_min": round(clk.elapsed_s(h, now) / 60),
            "target_min": h.target_min, "has_approach": has_approach})
        s.add(e)
        s.flush()
        return e.id


# ---- the statement (written by the coach, stored on the problem) ----------------------------
# Words that would name a technique or a difficulty. Pattern names/slugs come from the patterns
# table; these are the synonyms. Each is a regex over lowercase text; spaces/hyphens are loose.
_SEP = r"[\s\-_]*"
_LEAK_SYNONYMS = [
    "two pointers?", "sliding window", "prefix sum", "hash map", "heap", "priority queue",
    "bfs", "dfs", "binary search", "monotonic", "deque", "stack", r"backtrack(?:ing)?",
    "dynamic programming", "dp", r"memo(?:ization|ize|ised|ized)?", "topological", "union find",
    "dsu", "trie", "greedy", "linked list", "easy", "medium", "hard",
]


def _term_regex(words_or_regex, is_regex=False):
    if is_regex:
        body = words_or_regex.replace(" ", _SEP)
    else:
        body = _SEP.join(re.escape(w) for w in re.split(r"[\s\-_]+", words_or_regex.strip()) if w)
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?:e?s)?(?![a-z0-9])")


def _leak_terms(s):
    """{display: compiled regex} for every pattern name/slug and synonym."""
    terms = {}
    names = set(rules.CORE)
    for slug, name in s.execute(select(Pattern.slug, Pattern.name)):
        names.update((slug, name))
    # Domain words, not techniques: a statement about meeting rooms has to say "intervals".
    names -= {"intervals", "Intervals"}
    for n in names:
        key = re.sub(r"[\s\-_]+", " ", n.lower()).strip()
        if key:
            terms.setdefault(key, _term_regex(key))
    for syn in _LEAK_SYNONYMS:
        terms.setdefault(syn.replace("?", "").replace("(?:", "").replace(")", "").replace("|", "/"),
                         _term_regex(syn, True))
    return terms


def leak_words(text, title, s):
    """Technique / difficulty words in `text`. A term the problem's own title contains is fine
    (the title is shown anyway: 'Reverse Linked List' may say 'linked list')."""
    low = (text or "").lower()
    low_title = (title or "").lower()
    hits = []
    for display, rx in _leak_terms(s).items():
        m = rx.search(low)
        if m and not rx.search(low_title):
            hits.append(m.group(0).strip())
    return sorted(set(hits))


def set_statement(user_id, handout_id, text):
    """Store the coach-written statement on the handout's problem (overwrites). Raises
    ValueError('leak: ...') naming the offending words and stores nothing when the text names a
    technique or a difficulty. Returns {"ok": True, "handout_id", "number"}."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty statement")
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None or h.user_id != user_id:
            raise ValueError(f"no handout {handout_id}")
        p = s.get(Problem, h.problem_number)
        bad = leak_words(text, p.title, s)
        if bad:
            raise ValueError("leak: " + ", ".join(bad))
        p.paraphrase = text
        emit(user_id, "statement", h.id, s)
        return {"ok": True, "handout_id": h.id, "number": p.number}


def get_statement(handout_id):
    """The stored statement of a handout's problem, or None."""
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None:
            return None
        return s.get(Problem, h.problem_number).paraphrase


# ---- visuals (coach-drawn illustrations under the statement) --------------------------------
def open_handout_id(user_id):
    """The user's latest open handout id (any day), or None."""
    with session_scope() as s:
        return s.scalars(select(Handout.id).where(
            Handout.user_id == user_id, Handout.status == "open",
            Handout.slot != "imported").order_by(Handout.id.desc())).first()


def show_visual(user_id, handout_id, kind, spec):
    """Render, leak-check and store one visual for a handout, and post it to the discussion
    thread as a coach message (content = caption or kind label, visual_id = the visual; the
    thread renders the visual itself). ValueError on a bad kind/spec, or ValueError('leak:
    ...') (nothing stored, nothing posted) when any text in the spec names a technique or a
    difficulty. Returns {"id", "html", "message_id"}."""
    from . import viz
    kind = viz.check_kind(kind)
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    with session_scope() as s:
        h = s.get(Handout, handout_id) if handout_id is not None else None
        if h is None or h.user_id != user_id:
            raise ValueError(f"no handout {handout_id}")
        title = s.get(Problem, h.problem_number).title
        bad = leak_words("\n".join(viz.texts(kind, spec)), title, s)
        if bad:
            raise ValueError("leak: " + ", ".join(bad))
        html_out = viz.render(kind, spec)
        cap = viz.caption(spec)
        label = cap or viz.KIND_LABEL[kind]
        if leak_words(label, title, s):
            label = "illustration"
        v = Visual(user_id=user_id, handout_id=h.id, kind=kind, spec=spec, html=html_out,
                   caption=cap)
        s.add(v)
        s.flush()
        m = CoachMessage(user_id=user_id, handout_id=h.id, role="coach", content=label,
                         visual_id=v.id)
        s.add(m)
        s.flush()
        emit(user_id, "visual", v.id, s)
        emit(user_id, "message", m.id, s)
        return {"id": v.id, "html": html_out, "message_id": m.id}


def visuals(user_id, handout_id, since_id=0):
    """[{"id", "kind", "caption", "html", "created_at"}] of a handout, oldest first."""
    with session_scope() as s:
        rows = s.scalars(select(Visual).where(
            Visual.user_id == user_id, Visual.handout_id == handout_id,
            Visual.cleared_at.is_(None), Visual.id > (since_id or 0)).order_by(Visual.id))
        return [{"id": v.id, "kind": v.kind, "caption": v.caption, "html": v.html,
                 "created_at": v.created_at.isoformat()} for v in rows]


def count_visuals(user_id, handout_id):
    from sqlalchemy import func
    with session_scope() as s:
        return s.scalar(select(func.count(Visual.id)).where(
            Visual.user_id == user_id, Visual.handout_id == handout_id,
            Visual.cleared_at.is_(None))) or 0


def clear_visuals(user_id, handout_id):
    """Clear a handout's visuals: each is marked cleared (cleared_at) and its thread bubble
    turns into a muted "Illustration cleared." line. Returns how many."""
    with session_scope() as s:
        rows = s.scalars(select(Visual).where(Visual.user_id == user_id,
                                              Visual.handout_id == handout_id,
                                              Visual.cleared_at.is_(None))).all()
        now = datetime.now()
        for v in rows:
            v.cleared_at = now
        emit(user_id, "visual", handout_id, s)
        return len(rows)


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


def log_attempt(user_id, handout_id=None, number=None, actual_min=None, bugs=0, optimal=True,
                derived_cold=True, hints=0, notes="", abandoned=False, target_min=None,
                day=None):
    """Record an attempt. The outcome is computed here (rules.outcome); nobody types "clean".

    actual_min left out: the coach's clock, round(elapsed / 60), at least 1 (0 with no
    handout). The attempt also stores wall_min and clock_note (pauses / resets) from the clock
    record. The outcome uses actual_min (the coach's clock).

    Retry queue (MASTER_CONTEXT): add if over 1.5x target, 2+ bugs, a hint, suboptimal, not
    derived cold, or abandoned; clear on a clean solve; an item already queued gets attempts+1.
    Returns {attempt_id, outcome, retry: added|bumped|cleared|none, problem_number,
    actual_min, wall_min, clock_note}."""
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
        now = datetime.now()
        wall_min = clock_note = None
        if h is not None:
            wall_min = round(clk.wall_s(h, now) / 60)
            clock_note = clk.note(clk.record(_clock_events(s, h.id), now)) or None
            if actual_min is None:
                actual_min = max(1, round(clk.elapsed_s(h, now) / 60))
        if actual_min is None:
            actual_min = 0
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
            outcome=result, notes=notes or "", source="app", wall_min=wall_min,
            clock_note=clock_note)
        s.add(att)
        s.flush()
        if h:
            h.status = "logged"
            emit(user_id, "handout", h.id, s)

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
                "problem_number": number, "actual_min": actual_min, "wall_min": wall_min,
                "clock_note": clock_note}


def _update_pattern_state(s, user_id, slug, handout, day):
    """Retire a pattern at CLEAN_TO_OWN clean solves; log a re-fire when a refire handout is done."""
    if not slug:
        return
    ps = s.scalars(select(PatternState).where(
        PatternState.user_id == user_id, PatternState.pattern_slug == slug)).first()
    if handout is not None and handout.slot == "refire" and ps is not None:
        _append_refire(ps, day)
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


def _append_refire(ps, day):
    """Add `day` to pattern_state.refires once (a re-fire is counted once per day)."""
    iso = day.isoformat()
    cur = list(ps.refires or [])
    if iso not in cur:
        ps.refires = cur + [iso]


def record_refire(user_id, pattern_slug, today=None):
    """Append today's date to the pattern's refires (idempotent per day). `log_attempt` already
    does this for a re-fire handout. ValueError if the pattern has no state (never retired).
    Returns {"pattern", "refires": [...]}."""
    day = _today(today)
    with session_scope() as s:
        ps = s.scalars(select(PatternState).where(
            PatternState.user_id == user_id, PatternState.pattern_slug == pattern_slug)).first()
        if ps is None:
            raise ValueError(f"no pattern state for {pattern_slug!r}; it was never retired")
        _append_refire(ps, day)
        return {"pattern": pattern_slug, "refires": list(ps.refires)}


def _yn(v):
    if isinstance(v, str):
        return v.strip().lower()
    return "yes" if v else "no"


def log_trap_recall(user_id, pattern_slug, trap, cue, recalled, hit_in_code, today=None):
    """One spoken trap-quiz answer. A trap_recall row with the columns of the old
    data/trap_recall.csv (date, pattern, trap, cue, recalled, hit_in_code); booleans are
    stored as "yes"/"no". Returns that row as a dict (plus "id")."""
    day = _today(today)
    with session_scope() as s:
        r = TrapRecall(user_id=user_id, day=day, pattern=pattern_slug or "", trap=trap or "",
                       cue=cue or "", recalled=_yn(recalled), hit_in_code=_yn(hit_in_code))
        s.add(r)
        s.flush()
        # PLAN.md: "three re-fires, 7-14 days apart. A missed trap restarts the count."
        if _yn(recalled) == "no":
            ps = s.scalars(select(PatternState).where(
                PatternState.user_id == user_id, PatternState.pattern_slug == pattern_slug)).first()
            if ps is not None:
                ps.refires = []
        return _trap_row(r)


def _trap_row(r):
    return {"id": r.id, "date": r.day.isoformat(), "pattern": r.pattern, "trap": r.trap,
            "cue": r.cue, "recalled": r.recalled, "hit_in_code": r.hit_in_code}


def trap_quiz(user_id, pattern_slug, s):
    """{pattern, cues: [{cue, trap}]} from the pattern note's traps section ([] if none)."""
    ps = s.scalars(select(PatternState).where(
        PatternState.user_id == user_id, PatternState.pattern_slug == pattern_slug)).first()
    return {"pattern": pattern_slug, "cues": rules.parse_traps(ps.note_md if ps else "")}


# ---- reveal / burn -------------------------------------------------------------------------
def reveal(handout_id):
    """Pattern + slot of a handout. Only after its attempt is logged. A re-fire handout also
    carries `trap_quiz` (see trap_quiz)."""
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None:
            raise ValueError(f"no handout {handout_id}")
        if h.status != "logged":
            raise RevealBlocked("log the attempt first; the pattern is revealed afterwards")
        p = s.get(Problem, h.problem_number)
        slug = h.pattern_slug or p.pattern_slug
        out = {"handout_id": h.id, "number": p.number, "title": p.title, "slot": h.slot,
               "mode": picker.mode_for(h.slot),
               "pattern": slug, "pattern_name": pattern_name(slug) if slug else None,
               "tier": pattern_tier(slug) if slug else None}
        if h.slot == "refire" and slug:
            out["trap_quiz"] = trap_quiz(h.user_id, slug, s)
        out["clock"] = clock_record(s, h)
        return out


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


SEEN_KEY = "coach_last_seen"
SEEN_EVERY_S = 5.0          # heartbeat write throttle while polling
ONLINE_WINDOW_S = 90        # heartbeat this fresh = coach is connected
MSG_WINDOW_S = 600          # fallback: a coach message this recent = coach is around
CHECKPOINT_EVERY_S = 1.0    # how often wait_event checks the clock checkpoints


def coach_seen(now=None):
    """Heartbeat: record that the coach is polling right now (config.coach_last_seen, UTC iso)."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as s:
        row = s.get(Config, SEEN_KEY)
        prev = _parse_utc(row.value if row else None)
        if row is None:
            s.add(Config(key=SEEN_KEY, value=now.isoformat()))
        else:
            row.value = now.isoformat()
        # presence signal for open pages, at most once per SEEN_EVERY_S
        if prev is None or abs((now - prev).total_seconds()) >= SEEN_EVERY_S * 0.9:
            emit(None, "presence", None, s)


def _parse_utc(iso):
    try:
        ts = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def coach_online(now=None):
    """{online, last_seen (iso|None), reason}. Online when the heartbeat is under 90 s old, or
    (older bridge builds that never heartbeat) a coach message was posted in the last 10 min."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as s:
        row = s.get(Config, SEEN_KEY)
        seen = row.value if row else None
        newest = s.scalars(select(CoachMessage.created_at).where(CoachMessage.role == "coach")
                           .order_by(CoachMessage.id.desc()).limit(1)).first()
    if seen:
        try:
            ts = datetime.fromisoformat(seen)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() <= ONLINE_WINDOW_S:
                return {"online": True, "last_seen": seen, "reason": "heartbeat"}
        except (TypeError, ValueError):
            pass
    if newest is not None:          # message times are naive local
        local_now = now.astimezone().replace(tzinfo=None)
        if (local_now - newest).total_seconds() <= MSG_WINDOW_S:
            return {"online": True, "last_seen": seen, "reason": "recent_message"}
    return {"online": False, "last_seen": seen, "reason": "no_heartbeat" if seen else "never_seen"}


async def wait_event(user_id, timeout_s=25.0, poll_s=0.25):
    """Long-poll: return (and mark consumed) every pending event as soon as there is one, or
    [] when `timeout_s` passes with none. Each pass beats the coach heartbeat (<= once / 5 s)
    and checks the clock checkpoints (<= once / s; see _fire_checkpoints)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    beat = checked = None
    while True:
        if beat is None or loop.time() - beat >= SEEN_EVERY_S:
            beat = loop.time()
            coach_seen()
        if checked is None or loop.time() - checked >= CHECKPOINT_EVERY_S:
            checked = loop.time()
            _fire_checkpoints(user_id)
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
        emit(user_id, "message", m.id, s)
        return m.id


# ---- editor gate: the coach opens the code editor, the learner cannot --------------------
def _set_editor(user_id, handout_id, on, message=None):
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None or h.user_id != user_id:
            return {"ok": False, "error": "no such handout"}
        h.editor_open = on
        emit(user_id, "editor", h.id, s)
    if message:
        post_message(user_id, "coach", message, handout_id=handout_id)
    return {"ok": True}


def unlock_editor(user_id, handout_id, stub=None):
    """Open the code editor on his page. Posts the coach message "Editor open.". With `stub`,
    first stores it as the starter code (set_stub: leak-guarded; on a leak nothing opens and
    {"ok": False, "error": "leak: ..."} comes back)."""
    if stub is not None and str(stub).strip():
        try:
            set_stub(user_id, handout_id, stub)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    return _set_editor(user_id, handout_id, True, "Editor open.")


def lock_editor(user_id, handout_id):
    return _set_editor(user_id, handout_id, False)


def editor_open(user_id, handout_id):
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        return bool(h is not None and h.user_id == user_id and h.editor_open)


def list_messages(user_id, since_id=0, limit=200):
    with session_scope() as s:
        rows = list(s.scalars(select(CoachMessage).where(
            CoachMessage.user_id == user_id, CoachMessage.id > since_id)
            .order_by(CoachMessage.id).limit(limit)))
        vids = {m.visual_id for m in rows if m.visual_id is not None}
        vis = {v.id: v for v in s.scalars(select(Visual).where(Visual.id.in_(vids)))} \
            if vids else {}

        def visual(m):
            v = vis.get(m.visual_id)
            if v is None:
                return None
            return {"id": v.id, "kind": v.kind, "caption": v.caption,
                    "html": "" if v.cleared_at else v.html, "cleared": v.cleared_at is not None}
        return [{"id": m.id, "role": m.role, "content": m.content, "handout_id": m.handout_id,
                 "created_at": m.created_at.isoformat(), "visual": visual(m)} for m in rows]


def last_message_id(user_id):
    """Newest chat message id of the user (0 if none)."""
    from sqlalchemy import func
    with session_scope() as s:
        return s.scalar(select(func.max(CoachMessage.id)).where(
            CoachMessage.user_id == user_id)) or 0


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


# ---- the ledger (written by the coach at session end) --------------------------------------
def set_skill(user_id, name, level, evidence="", trend="", today=None):
    """Upsert a skill by name (exact, then case-insensitive, then pattern slug) and insert a
    skill_events row dated today. A new skill is kind "pattern" when `name` is a pattern's
    slug or name, else "meta". ValueError unless 0 <= level <= 5.
    Returns {"skill_id", "name", "was", "level", "trend", "evidence", "created"}."""
    day = _today(today)
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 5:
        raise ValueError(f"level must be an int 0-5, got {level!r}")
    name = (name or "").strip()
    if not name:
        raise ValueError("empty skill name")
    with session_scope() as s:
        skills = state.user_skills(s, user_id)
        k = (next((x for x in skills if x.name == name), None)
             or next((x for x in skills if x.name.lower() == name.lower()), None)
             or next((x for x in skills if x.pattern_slug == name.lower()), None))
        created = k is None
        was = None if created else k.level
        if created:
            pat = s.scalars(select(Pattern).where(
                (Pattern.slug == name.lower()) | (Pattern.name == name))).first()
            k = Skill(user_id=user_id, name=pat.name if pat else name,
                      kind="pattern" if pat else "meta",
                      pattern_slug=pat.slug if pat else None, level=level)
            s.add(k)
        k.level = level
        k.evidence = evidence or ""
        k.trend = trend or ""
        k.updated_on = day
        s.flush()
        s.add(SkillEvent(skill_id=k.id, day=day, level=level, evidence=evidence or ""))
        return {"skill_id": k.id, "name": k.name, "was": was, "level": level,
                "trend": k.trend, "evidence": k.evidence, "created": created}


# ---- the session log -----------------------------------------------------------------------
def _latest_session(s, user_id, day):
    return s.scalars(select(CoachSession).where(
        CoachSession.user_id == user_id, CoachSession.day == day)
        .order_by(CoachSession.id.desc())).first()


def write_session_log(user_id, markdown, today=None):
    """Store the session log (markdown, templates/session_log.md format) in sessions.log_md
    for today's latest session (ended or not), creating the row if there is none. Overwrites.
    Returns {"session_id", "chars"}."""
    day = _today(today)
    markdown = (markdown or "").strip()
    if not markdown:
        raise ValueError("empty session log")
    with session_scope() as s:
        row = _latest_session(s, user_id, day) or state.current_session(s, user_id, day)
        row.log_md = markdown
        return {"session_id": row.id, "chars": len(markdown)}


def _attempt_row(a):
    return {"attempt_id": a.id, "number": a.problem_number, "target_min": a.target_min,
            "actual_min": a.actual_min, "bugs": a.bugs, "hints": a.hints,
            "optimal": a.optimal, "derived_cold": a.derived_cold, "outcome": a.outcome,
            "notes": a.notes, "wall_min": a.wall_min, "clock_note": a.clock_note}


def session_summary(user_id, today=None):
    """Everything today produced, so the coach writes the log without guessing. Progress
    talk: session end only. Pattern and slot appear only for LOGGED handouts (RULE ZERO).
    -> {"today", "session_id", "problems": [{handout_id, number, title, handed_at,
        target_min, status, mode, attempt: {...} | None, reveal: {...} | None,
        clock: {coach_min, wall_min, pauses: [{reason, minutes, at}], resets: [{reason, at}]}}],
        "attempts_without_handout": [...], "skill_changes": [{name, level, evidence}],
        "retry": {"added", "cleared", "bumped"}, "trap_recall": [...]}"""
    day = _today(today)
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    with session_scope() as s:
        sess = _latest_session(s, user_id, day)
        handouts = list(s.scalars(select(Handout).where(
            Handout.user_id == user_id, Handout.slot != "imported",
            Handout.handed_at >= start, Handout.handed_at < end).order_by(Handout.id)))
        atts = list(s.scalars(select(Attempt).where(
            Attempt.user_id == user_id, Attempt.day == day).order_by(Attempt.id)))
        by_handout = {a.handout_id: a for a in atts if a.handout_id is not None}
        problems = []
        for h in handouts:
            p = s.get(Problem, h.problem_number)
            a = by_handout.get(h.id)
            rv = None
            if h.status == "logged":
                slug = h.pattern_slug or p.pattern_slug
                rv = {"slot": h.slot, "pattern": slug,
                      "pattern_name": pattern_name(slug) if slug else None,
                      "tier": pattern_tier(slug) if slug else None}
            problems.append({
                "handout_id": h.id, "number": p.number, "title": p.title,
                "handed_at": h.handed_at.isoformat(), "target_min": h.target_min,
                "status": h.status, "mode": picker.mode_for(h.slot),
                "attempt": _attempt_row(a) if a else None, "reveal": rv,
                "clock": clock_record(s, h)})
        linked = {h.id for h in handouts}
        loose = [_attempt_row(a) for a in atts if a.handout_id not in linked]
        events = s.execute(
            select(SkillEvent, Skill).join(Skill, SkillEvent.skill_id == Skill.id)
            .where(Skill.user_id == user_id, SkillEvent.day == day)
            .order_by(SkillEvent.id)).all()
        skill_changes = [{"name": k.name, "level": e.level, "evidence": e.evidence}
                         for e, k in events]
        retry = {"added": [], "cleared": [], "bumped": []}
        for r in state.user_retry_items(s, user_id, open_only=False):
            p = s.get(Problem, r.problem_number)
            row = {"problem_number": r.problem_number, "title": p.title if p else "",
                   "attempts": r.attempts, "reason": r.reason}
            if r.cleared_on == day:
                retry["cleared"].append(row)
            elif r.added == day:
                retry["added"].append(row)
            elif r.last_on == day:
                retry["bumped"].append(row)
        traps = [_trap_row(r) for r in s.scalars(select(TrapRecall).where(
            TrapRecall.user_id == user_id, TrapRecall.day == day).order_by(TrapRecall.id))]
        return {"today": day.isoformat(), "session_id": sess.id if sess else None,
                "problems": problems, "attempts_without_handout": loose,
                "skill_changes": skill_changes, "retry": retry, "trap_recall": traps}


# ---- editor tools: draft sync, stub, annotations, coach comments, scratch (coach/editor.py) ---
from .editor import (EDITOR_UI_KINDS, add_comment, annotate, annotations,  # noqa: E402
                     clear_annotations, editor_state, get_stub, highlight, mark_op_applied,
                     pending_ops, read_code, reply_annotation, resolve_annotation, save_draft,
                     scratch, scratches, set_stub)

UI_KINDS = UI_KINDS | EDITOR_UI_KINDS       # emit() reads the module global at call time


# ---- tests panel: cases, his own cases, runs (coach/tests_api.py, coach/runner.py) --------
from .tests_api import (TESTS_UI_KINDS, add_user_test, delete_user_test,  # noqa: E402
                        last_run, run_tests, set_tests, tests)

UI_KINDS = UI_KINDS | TESTS_UI_KINDS        # emit() reads the module global at call time
