"""MMR: one rating per learner, each logged attempt a "match" against the problem's rating.

    expected = 1 / (1 + 10 ** ((problem_rating - mmr) / 400))
    delta    = K * (actual - expected),   K = 40 for his first 20 games, then 20
    start 1200 (Silver)

Problem rating: problems.rating when the coach rated it (rate_problem, fixed once set), else the
default of its label (E 1000 / M 1400 / H 1800). Bands a rating must fall in: E 800-1300,
M 1200-1900, H 1700-2600. No crowd adjustment.

LIVE attempts (with a handout) are scored by the COACH after logging (score_attempt ->
apply_coach): each part 0..1 with one reason, same weights, minus a hints deduction 0..0.3.
The formula below rates only placement replay and attempts logged without a handout.

formula actual (0..1) = sum of weighted parts, minus 0.1 per hint (at most 0.3), floor 0; abandoned = 0:
    understand  0.10  understand step within its budget (live: time up to his button press)
    approach    0.25  his own approach (derived_cold) AND the approach step within budget,
                      linear to 0 at 2x budget
    first_ac    0.30  the FIRST judged submission was Accepted
    quality     0.15  optimal AND no lint hits on the accepted submission
    time        0.20  1.0 at <= target, linear to 0 at 2x target
Fallbacks when the live data is missing (old handouts, placement replay):
    understand  1 if actual_min <= 1.5x target, else 0
    approach    derived_cold x the total-time factor
    first_ac    no judged submission: 1 if bugs == 0, else 0
    quality     no submission: optimal only

Ranks: Bronze < 1200 <= Silver < 1400 <= Gold < 1600 <= Platinum < 1800 <= Diamond.
Progress data: never on /, /solve or /log, never in a handoff.
"""
from datetime import datetime

from sqlalchemy import select

START_MMR = 1200.0
K_NEW, K_SETTLED, NEW_GAMES = 40, 20, 20
DEFAULT_RATING = {"E": 1000, "M": 1400, "H": 1800}
BANDS = {"E": (800, 1300), "M": (1200, 1900), "H": (1700, 2600)}
RANKS = [("Bronze", None), ("Silver", 1200), ("Gold", 1400), ("Platinum", 1600),
         ("Diamond", 1800)]
WEIGHTS = {"understand": 0.10, "approach": 0.25, "first_ac": 0.30, "quality": 0.15,
           "time": 0.20}
HINT_COST, HINT_MAX = 0.1, 0.3
CLEAN_FACTOR = 1.5


class RatingRefused(ValueError):
    """rate_problem broke a rule (outside the band, already rated)."""


# ---- pure math ---------------------------------------------------------------------------
def rank(mmr):
    name = RANKS[0][0]
    for n, lo in RANKS[1:]:
        if mmr >= lo:
            name = n
    return name


def next_rank_at(mmr):
    for n, lo in RANKS[1:]:
        if mmr < lo:
            return {"rank": n, "mmr": lo}
    return None


def expected(mmr, problem_rating):
    return 1.0 / (1.0 + 10 ** ((problem_rating - mmr) / 400.0))


def k_factor(games_before):
    return K_NEW if games_before < NEW_GAMES else K_SETTLED


def _ramp(ratio):
    """1.0 at <= 1, linear to 0 at 2."""
    if ratio <= 1:
        return 1.0
    return max(0.0, 2.0 - ratio)


def problem_rating(problem):
    d = getattr(problem, "difficulty", None) or "M"
    r = getattr(problem, "rating", None)
    return int(r) if r else DEFAULT_RATING.get(d, 1400)


def score(a, steps=None, first_verdict=None, accepted_lint_hits=None, has_submission=False):
    """(actual, breakdown) for an attempt `a` (needs outcome, bugs, hints, optimal,
    derived_cold, actual_min, target_min). steps: {"budget": {...}, "seconds": {...}} or None.
    first_verdict: verdict of the first judged submission or None. accepted_lint_hits: lint hit
    count on the accepted submission (None = no accepted submission seen)."""
    abandoned = a.outcome == "abandoned"
    target = max(1, a.target_min or 1)
    ratio = (a.actual_min or 0) / target
    time_f = _ramp(ratio)
    parts, src = {}, {}
    b = (steps or {}).get("budget") or {}
    secs = (steps or {}).get("seconds") or {}
    if b.get("understand"):
        parts["understand"] = 1.0 if secs.get("understand", 0) <= b["understand"] * 60 else 0.0
        src["understand"] = "step"
    else:
        parts["understand"] = 1.0 if ratio <= CLEAN_FACTOR else 0.0
        src["understand"] = "total time"
    if b.get("approach"):
        f = _ramp(secs.get("approach", 0) / (b["approach"] * 60))
        src["approach"] = "step"
    else:
        f = time_f
        src["approach"] = "total time"
    parts["approach"] = f if a.derived_cold else 0.0
    if first_verdict:
        parts["first_ac"] = 1.0 if first_verdict == "Accepted" else 0.0
        src["first_ac"] = "judge"
    else:
        parts["first_ac"] = 1.0 if (a.bugs or 0) == 0 else 0.0
        src["first_ac"] = "bugs"
    if accepted_lint_hits is not None:
        parts["quality"] = 1.0 if (a.optimal and accepted_lint_hits == 0) else 0.0
        src["quality"] = "lint"
    else:
        parts["quality"] = 1.0 if a.optimal else 0.0
        src["quality"] = "optimal"
    parts["time"] = time_f
    src["time"] = "total time"
    raw = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS)
    penalty = min(HINT_MAX, HINT_COST * (a.hints or 0))
    actual = 0.0 if abandoned else max(0.0, raw - penalty)
    breakdown = {
        "parts": {k: {"weight": WEIGHTS[k], "score": round(parts[k], 3),
                      "points": round(WEIGHTS[k] * parts[k], 3), "from": src[k]}
                  for k in WEIGHTS},
        "raw": round(raw, 3), "hint_penalty": round(penalty, 3), "abandoned": abandoned,
        "actual": round(actual, 3)}
    if steps:
        breakdown["steps"] = {"budget": b, "seconds": secs}
    return actual, breakdown


# ---- DB ----------------------------------------------------------------------------------
def _live_inputs(s, att):
    """steps, first judged verdict, lint hits on the accepted submission, for a live attempt."""
    from . import steps as st
    from .lint import lint
    from .models import Handout, Submission
    if att.handout_id is None:
        return None, None, None
    h = s.get(Handout, att.handout_id)
    steps = None
    if h is not None and st.has_steps(h):
        steps = {"budget": dict(h.phase_budget),
                 "seconds": {k: int((h.phase_elapsed or {}).get(k, 0)) for k in st.PHASES}}
    subs = list(s.scalars(select(Submission).where(Submission.handout_id == att.handout_id)
                          .order_by(Submission.id)))
    judged = [x for x in subs if x.verdict]
    first = judged[0].verdict if judged else None
    acc = next((x for x in judged if x.verdict == "Accepted"), None)
    hits = None
    if acc is not None:
        try:
            hits = len(lint(acc.code or ""))
        except Exception:
            hits = None
    return steps, first, hits


def get_or_create(s, user_id):
    from .models import Rating
    r = s.get(Rating, user_id)
    if r is None:
        r = Rating(user_id=user_id, mmr=START_MMR, games=0, updated_at=datetime.now())
        s.add(r)
        s.flush()
    return r


def apply(s, att, replayed=False):
    """Rate one attempt by the FORMULA (once: an existing rating_events row comes back
    unchanged). Used for placement replay and for attempts logged without a handout (imports);
    a live handout attempt is scored by the coach instead (apply_coach). Returns the RatingEvent."""
    ev = event_for(s, att.id)
    if ev is not None:
        return ev
    if replayed:
        actual, bd = score(att)
        bd["replayed"] = True
    else:
        steps, first, hits = _live_inputs(s, att)
        actual, bd = score(att, steps, first, hits)
    bd["scored_by"] = "formula"
    return _commit(s, att, actual, bd)


def event_for(s, attempt_id):
    from .models import RatingEvent
    return s.scalars(select(RatingEvent).where(RatingEvent.attempt_id == attempt_id)).first()


def _commit(s, att, actual, bd):
    """Expected vs the problem's rating, K, the delta; writes the rating_events row."""
    from .models import Problem, RatingEvent
    r = get_or_create(s, att.user_id)
    pr = problem_rating(s.get(Problem, att.problem_number))
    e = expected(r.mmr, pr)
    k = k_factor(r.games)
    delta = k * (actual - e)
    before = r.mmr
    r.mmr = before + delta
    r.games += 1
    r.updated_at = datetime.now()
    ev = RatingEvent(user_id=att.user_id, attempt_id=att.id, before=round(before, 2),
                     after=round(r.mmr, 2), delta=round(delta, 2), expected=round(e, 4),
                     actual=round(actual, 4), k=k, problem_rating=pr, breakdown=bd,
                     created_at=datetime.now())
    s.add(ev)
    s.flush()
    return ev


# ---- the coach's score (score_attempt) ----------------------------------------------------
# The coach scores each part 0..1 with one plain reason; same weights as the formula, with
# first_submit in place of first_ac. hints: a deduction 0..0.3.
COACH_PARTS = ("understand", "approach", "first_submit", "quality", "time")
COACH_WEIGHTS = {"understand": 0.10, "approach": 0.25, "first_submit": 0.30, "quality": 0.15,
                 "time": 0.20}
WHY_MAX = 200


def _unit(v, name, hi=1.0):
    if isinstance(v, bool):
        raise RatingRefused(f"{name} must be a number 0-{hi:g}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise RatingRefused(f"{name} must be a number 0-{hi:g}") from None
    if not 0.0 <= f <= hi:
        raise RatingRefused(f"{name} must be 0-{hi:g}, got {f:g}")
    return f


def _why(v, name):
    w = " ".join(str(v or "").split())
    if not w:
        raise RatingRefused(f"{name}.why: one plain line")
    if len(w) > WHY_MAX:
        raise RatingRefused(f"{name}.why: at most {WHY_MAX} characters")
    return w


def coach_score(parts, abandoned=False):
    """(actual, breakdown) from the coach's parts:
    {understand|approach|first_submit|quality|time: {score 0-1, why}, hints: {deduction 0-0.3,
    why}}. actual = weighted sum - deduction, floor 0 (0 when abandoned). RatingRefused on a
    missing / extra part, a value out of range, or a why missing or over 200 characters."""
    if not isinstance(parts, dict):
        raise RatingRefused("parts must be an object")
    want = set(COACH_PARTS) | {"hints"}
    if set(parts) != want:
        miss, extra = sorted(want - set(parts)), sorted(set(parts) - want)
        raise RatingRefused("parts needs exactly " + ", ".join(COACH_PARTS) + ", hints"
                            + (f" (missing {', '.join(miss)})" if miss else "")
                            + (f" (unknown {', '.join(extra)})" if extra else ""))
    out = {}
    for k in COACH_PARTS:
        p = parts[k]
        if not isinstance(p, dict):
            raise RatingRefused(f"{k}: {{score, why}}")
        sc = _unit(p.get("score"), f"{k}.score")
        out[k] = {"weight": COACH_WEIGHTS[k], "score": round(sc, 3),
                  "points": round(COACH_WEIGHTS[k] * sc, 3), "why": _why(p.get("why"), k)}
    hp = parts["hints"]
    if not isinstance(hp, dict):
        raise RatingRefused("hints: {deduction, why}")
    ded = _unit(hp.get("deduction"), "hints.deduction", HINT_MAX)
    raw = sum(v["points"] for v in out.values())
    actual = 0.0 if abandoned else max(0.0, raw - ded)
    return actual, {"parts": out, "hints": {"deduction": round(ded, 3),
                                            "why": _why(hp.get("why"), "hints")},
                    "raw": round(raw, 3), "hint_penalty": round(ded, 3),
                    "abandoned": bool(abandoned), "actual": round(actual, 3),
                    "scored_by": "coach"}


def apply_coach(s, att, parts, summary=""):
    """Rate a logged attempt from the coach's parts. Once: RatingRefused if it already has a
    rating event. Returns the RatingEvent."""
    prev = event_for(s, att.id)
    if prev is not None:
        if (prev.breakdown or {}).get("scored_by") == "auto" and                 not (prev.breakdown or {}).get("reasons"):
            return attach_reasons(s, prev, parts, summary)      # abandoned: reasons only, once
        raise RatingRefused("this attempt is already scored; a score is final")
    actual, bd = coach_score(parts, abandoned=att.outcome == "abandoned")
    summ = " ".join(str(summary or "").split())
    if len(summ) > WHY_MAX:
        raise RatingRefused(f"summary: at most {WHY_MAX} characters")
    bd["summary"] = summ
    return _commit(s, att, actual, bd)


# ---- abandoned: scored at once, 0, no pending (coach/service.py log_attempt) --------------------
def apply_auto_abandoned(s, att):
    """An abandoned attempt scores 0 the moment it is logged (scored_by "auto"); the coach may
    later attach reasons once (attach_reasons) but never changes the score. Returns the event."""
    ev = event_for(s, att.id)
    if ev is not None:
        return ev
    bd = {"parts": {}, "hints": None, "raw": 0.0, "hint_penalty": 0.0, "abandoned": True,
          "actual": 0.0, "scored_by": "auto", "summary": "", "reasons": False}
    return _commit(s, att, 0.0, bd)


def attach_reasons(s, ev, parts, summary=""):
    """The coach's reasons and summary on an auto-scored (abandoned) attempt: the parts are
    validated like any score, then only their text is kept. actual, delta and MMR do not move."""
    _, bd = coach_score(parts, abandoned=True)
    summ = " ".join(str(summary or "").split())
    if len(summ) > WHY_MAX:
        raise RatingRefused(f"summary: at most {WHY_MAX} characters")
    old = dict(ev.breakdown or {})
    old["parts"] = {k: {"weight": v["weight"], "why": v["why"]} for k, v in bd["parts"].items()}
    old["hints"] = {"why": bd["hints"]["why"]}
    old["summary"] = summ
    old["reasons"] = True
    ev.breakdown = old                      # a new dict: SQLAlchemy sees the JSON change
    return ev


def ensure(s, user_id, exclude_attempt_id=None):
    """Placement: the first time a user is rated, replay every attempt he already has (date
    order, then id) from the stored fields, each with breakdown.replayed = True. Idempotent:
    a user with a ratings row is left alone. Returns the Rating row."""
    from .models import Attempt, Rating
    r = s.get(Rating, user_id)
    if r is not None:
        return r
    r = get_or_create(s, user_id)
    atts = s.scalars(select(Attempt).where(Attempt.user_id == user_id)
                     .order_by(Attempt.day, Attempt.id)).all()
    for a in atts:
        if a.id != exclude_attempt_id:
            apply(s, a, replayed=True)
    return r


def migrate(s):
    """db.init: place every user who has attempts and no rating yet."""
    from .models import Attempt, Rating
    have = set(s.scalars(select(Rating.user_id)))
    for uid in sorted(set(s.scalars(select(Attempt.user_id))) - have):
        ensure(s, uid)


def event_dict(ev, s=None):
    if ev is None:
        return None
    before_rank, after_rank = rank(ev.before), rank(ev.after)
    order = [n for n, _ in RANKS]
    return {"attempt_id": ev.attempt_id, "before": round(ev.before), "after": round(ev.after),
            "delta": round(ev.delta, 1), "expected": round(ev.expected, 3),
            "actual": round(ev.actual, 3), "k": ev.k, "problem_rating": ev.problem_rating,
            "breakdown": ev.breakdown, "rank_before": before_rank, "rank": after_rank,
            "rank_up": order.index(after_rank) > order.index(before_rank),
            "rank_down": order.index(after_rank) < order.index(before_rank),
            "replayed": bool((ev.breakdown or {}).get("replayed")),
            "scored_by": (ev.breakdown or {}).get("scored_by") or "formula",
            "summary": (ev.breakdown or {}).get("summary") or None,
            "reasons_attached": bool((ev.breakdown or {}).get("reasons")),
            "at": ev.created_at.isoformat() if ev.created_at else None}


def block(s, user_id, recent=10):
    """{mmr, rank, games, next_rank_at, to_next, recent_deltas} (newest last)."""
    from .models import Attempt, RatingEvent
    r = ensure(s, user_id)
    rows = s.execute(select(RatingEvent, Attempt).join(Attempt, Attempt.id == RatingEvent.attempt_id)
                     .where(RatingEvent.user_id == user_id)
                     .order_by(RatingEvent.id.desc()).limit(recent)).all()
    deltas = [{"delta": round(e.delta, 1), "after": round(e.after), "number": a.problem_number,
               "day": a.day.isoformat(), "replayed": bool((e.breakdown or {}).get("replayed"))}
              for e, a in reversed(rows)]
    nxt = next_rank_at(r.mmr)
    return {"mmr": round(r.mmr), "rank": rank(r.mmr), "games": r.games, "next_rank_at": nxt,
            "to_next": (nxt["mmr"] - round(r.mmr)) if nxt else None, "recent_deltas": deltas}


def check_band(difficulty, value):
    lo, hi = BANDS.get(difficulty or "M", BANDS["M"])
    if isinstance(value, bool):
        raise RatingRefused("rating must be a whole number")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise RatingRefused("rating must be a whole number") from None
    if not lo <= n <= hi:
        raise RatingRefused(f"rating {n} is outside {lo}-{hi} for this problem's level")
    return n
