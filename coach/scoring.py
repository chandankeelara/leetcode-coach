"""The coach scores the solve; the coach sets the code budget.

* `score_attempt(user_id, handout_id, parts, summary)`: after log_attempt, ONCE per attempt.
  parts may carry `review` (step 4, after Accepted): a bonus up to 0.10 on top (rating.py).
  The coach gives each part a score 0..1 and one plain reason (rating.coach_score); the
  server computes expected (Elo vs the problem's rating), K and the MMR delta, and stores the
  rating_events row with breakdown.scored_by = "coach". The server does not second-guess it.
  A logged attempt the coach has not scored has no rating event yet (`pending_scores`).
* `unlock_with_budget(...)`: unlock_editor with `code_min` sets phase_budget.code (2-40 min)
  and the total target; `code_reason` is private until logged (shown on reveal).
* `code_speed(s, user_id)`: seconds per line of his recent accepted code, for budgeting.
"""
import statistics

from sqlalchemy import select

from . import rating
from . import steps as stp
from .db import session_scope
from .models import Attempt, Handout, Problem, RatingEvent, Submission, User

CODE_MIN_RANGE = (2, 40)
SPEED_SAMPLES, SPEED_MIN_SAMPLES = 10, 3
REASON_MAX = 200


class ScoreRefused(ValueError):
    """score_attempt / unlock budget broke a rule. The message says which."""


# ---- scoring ---------------------------------------------------------------------------------
def _guard(texts, username):
    """Reasons are shown on reveal (after logging, so pattern names are fine): no code, no
    names."""
    from .feedback import looks_like_code
    for t in texts:
        if looks_like_code(t):
            raise ScoreRefused("reasons are plain words, no code")
        if username and len(username) >= 3 and username.lower() in t.lower():
            raise ScoreRefused("reasons name no one")


def score_attempt(user_id, handout_id, parts, summary=""):
    """Score a logged attempt from the coach's parts. ScoreRefused: not his handout, not
    logged yet, already scored, or a bad part. Returns {attempt_id, scored_by, actual,
    expected, delta, before, after, rank, rank_up, rank_down, next_rank_at}."""
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None or h.user_id != user_id:
            raise ScoreRefused(f"no handout {handout_id}")
        a = s.scalars(select(Attempt).where(Attempt.handout_id == h.id)
                      .order_by(Attempt.id.desc())).first()
        if a is None or h.status != "logged":
            raise ScoreRefused("log the attempt first (log_attempt), then score it")
        u = s.get(User, user_id)
        try:
            ev = rating.apply_coach(s, a, parts, summary)
        except rating.RatingRefused as e:
            raise ScoreRefused(str(e)) from None
        # the reasons as he wrote them (before whitespace is collapsed); raising rolls back
        texts = [str((v or {}).get("why") or "") for v in parts.values()
                 if isinstance(v, dict)] + [str(summary or "")]
        rv = parts.get("review") if isinstance(parts, dict) else None
        if isinstance(rv, dict):
            texts += [str((v or {}).get("why") or "") for v in rv.values() if isinstance(v, dict)]
        _guard(texts, u.username if u else None)
        from .service import emit
        emit(user_id, "rating", h.id, s)         # the open reveal page fills its rating card
        d = rating.event_dict(ev)
        out = {k: d[k] for k in ("attempt_id", "scored_by", "actual", "expected", "delta",
                                 "before", "after", "rank", "rank_up", "rank_down")}
        out["next_rank_at"] = rating.next_rank_at(ev.after)
        return out


def pending_scores(s, user_id):
    """[{handout_id, attempt_id, number, title, day}] logged with a handout, no rating yet."""
    rows = s.execute(
        select(Attempt, Problem.title).join(Problem, Problem.number == Attempt.problem_number)
        .outerjoin(RatingEvent, RatingEvent.attempt_id == Attempt.id)
        .where(Attempt.user_id == user_id, Attempt.handout_id.is_not(None),
               RatingEvent.id.is_(None))
        .order_by(Attempt.id)).all()
    return [{"handout_id": a.handout_id, "attempt_id": a.id, "number": a.problem_number,
             "title": title, "day": a.day.isoformat()} for a, title in rows]


# ---- the code budget, set when the editor opens ------------------------------------------------
def _code_min(v):
    lo, hi = CODE_MIN_RANGE
    if isinstance(v, bool):
        raise ScoreRefused(f"code_min must be whole minutes, {lo}-{hi}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ScoreRefused(f"code_min must be whole minutes, {lo}-{hi}") from None
    if f != int(f):
        raise ScoreRefused(f"code_min must be whole minutes, {lo}-{hi}")
    n = int(f)
    if not lo <= n <= hi:
        raise ScoreRefused(f"code_min {n} is outside {lo}-{hi} min")
    return n


def unlock_with_budget(user_id, handout_id, stub=None, code_min=None, code_reason=""):
    """service.unlock_editor, then (handout with steps) phase_budget.code = code_min and
    target_min = the sum of the steps; code_reason stored privately. code_min is checked
    BEFORE anything opens (ScoreRefused outside 2-40). No code_min: the hand_out budget stays.
    Returns unlock_editor's result plus {"budget", "target_min"} when a budget was set."""
    from .service import emit, unlock_editor
    n = _code_min(code_min) if code_min is not None else None
    reason = " ".join(str(code_reason or "").split())[:REASON_MAX] or None
    r = unlock_editor(user_id, handout_id, stub=stub) if stub else unlock_editor(user_id,
                                                                                handout_id)
    if not r.get("ok") or (n is None and reason is None):
        return r
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if reason is not None:
            h.code_reason = reason
        if n is not None and stp.has_steps(h):
            b = dict(h.phase_budget)
            b["code"] = n
            h.phase_budget = b
            h.target_min = sum(b.values())
            emit(user_id, "clock", h.id, s)
            return {**r, "budget": b, "target_min": h.target_min}
        if n is not None:
            return {**r, "note": "this problem has no steps; code_min was not applied"}
    return r


# ---- his coding speed ------------------------------------------------------------------------
def code_lines(code):
    """Non-blank, non-comment lines."""
    return sum(1 for ln in (code or "").splitlines()
               if ln.strip() and not ln.strip().startswith("#"))


def code_speed(s, user_id):
    """{seconds_per_line, samples, recent: [{number, lines, code_s, budget_s}]} over his last
    <= 10 logged problems with steps and an accepted submission: code-step seconds / accepted
    lines, median. seconds_per_line is None under 3 samples."""
    recent = []
    hs = s.scalars(select(Handout).where(Handout.user_id == user_id,
                                         Handout.status == "logged",
                                         Handout.phase_budget.is_not(None))
                   .order_by(Handout.id.desc()).limit(50))
    for h in hs:
        code_s = int((h.phase_elapsed or {}).get("code", 0))
        if code_s <= 0:
            continue
        acc = s.scalars(select(Submission).where(Submission.handout_id == h.id,
                                                 Submission.verdict == "Accepted")
                        .order_by(Submission.id)).first()
        if acc is None:
            continue
        lines = code_lines(acc.code)
        if not lines:
            continue
        recent.append({"number": h.problem_number, "lines": lines, "code_s": code_s,
                       "budget_s": int((h.phase_budget or {}).get("code", 0)) * 60})
        if len(recent) >= SPEED_SAMPLES:
            break
    spl = None
    if len(recent) >= SPEED_MIN_SAMPLES:
        spl = round(statistics.median(r["code_s"] / r["lines"] for r in recent), 1)
    return {"seconds_per_line": spl, "samples": len(recent), "recent": recent}
