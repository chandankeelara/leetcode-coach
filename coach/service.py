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
from sqlalchemy import select

from . import picker, rules, state
from .catalog import default_target, pattern_name, pattern_tier
from .db import session_scope
from .models import (Attempt, Burned, CoachMessage, Config, CoachSession, Event, Handout, Pattern,
                     PatternState, Problem, RetryItem, Skill, SkillEvent, TrapRecall, User,
                     Visual)

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
    """Render, leak-check and store one visual for a handout, and post a coach chat line
    pointing at it. ValueError on a bad kind/spec, or ValueError('leak: ...') (nothing stored,
    nothing posted) when any text in the spec names a technique or a difficulty.
    Returns {"id", "html"}."""
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
        s.add(CoachMessage(user_id=user_id, handout_id=h.id, role="coach",
                           content=f"See the problem pane: {label}"))
        s.flush()
        return {"id": v.id, "html": html_out}


def visuals(user_id, handout_id, since_id=0):
    """[{"id", "kind", "caption", "html", "created_at"}] of a handout, oldest first."""
    with session_scope() as s:
        rows = s.scalars(select(Visual).where(
            Visual.user_id == user_id, Visual.handout_id == handout_id,
            Visual.id > (since_id or 0)).order_by(Visual.id))
        return [{"id": v.id, "kind": v.kind, "caption": v.caption, "html": v.html,
                 "created_at": v.created_at.isoformat()} for v in rows]


def count_visuals(user_id, handout_id):
    from sqlalchemy import func
    with session_scope() as s:
        return s.scalar(select(func.count(Visual.id)).where(
            Visual.user_id == user_id, Visual.handout_id == handout_id)) or 0


def clear_visuals(user_id, handout_id):
    """Delete a handout's visuals. Returns how many."""
    with session_scope() as s:
        rows = s.scalars(select(Visual).where(Visual.user_id == user_id,
                                              Visual.handout_id == handout_id)).all()
        for v in rows:
            s.delete(v)
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


def coach_seen(now=None):
    """Heartbeat: record that the coach is polling right now (config.coach_last_seen, UTC iso)."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as s:
        row = s.get(Config, SEEN_KEY)
        if row is None:
            s.add(Config(key=SEEN_KEY, value=now.isoformat()))
        else:
            row.value = now.isoformat()


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
    [] when `timeout_s` passes with none. Each pass beats the coach heartbeat (<= once / 5 s)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    beat = None
    while True:
        if beat is None or loop.time() - beat >= SEEN_EVERY_S:
            beat = loop.time()
            coach_seen()
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


# ---- editor gate: the coach opens the code editor, the learner cannot --------------------
def _set_editor(user_id, handout_id, on, message=None):
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None or h.user_id != user_id:
            return {"ok": False, "error": "no such handout"}
        h.editor_open = on
    if message:
        post_message(user_id, "coach", message, handout_id=handout_id)
    return {"ok": True}


def unlock_editor(user_id, handout_id):
    """Open the code editor on his page. Posts the coach message "Editor open."."""
    return _set_editor(user_id, handout_id, True, "Editor open.")


def lock_editor(user_id, handout_id):
    return _set_editor(user_id, handout_id, False)


def editor_open(user_id, handout_id):
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        return bool(h is not None and h.user_id == user_id and h.editor_open)


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
            "notes": a.notes}


def session_summary(user_id, today=None):
    """Everything today produced, so the coach writes the log without guessing. Progress
    talk: session end only. Pattern and slot appear only for LOGGED handouts (RULE ZERO).
    -> {"today", "session_id", "problems": [{handout_id, number, title, handed_at,
        target_min, status, mode, attempt: {...} | None, reveal: {...} | None}],
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
                "attempt": _attempt_row(a) if a else None, "reveal": rv})
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
