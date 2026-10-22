"""Coach-led problem choice. The coach (Claude, through the bridge) reads the learner's plan and
record with get_profile / get_due / find_problems, then picks with hand_out. The server keeps
the hard rules: hand_out refuses a problem touched in the last 7 days, a problem retired by 3
clean solves, a Hard before the plan allows them, a pattern burned this session, and any pick
while another handout is open.

RULE ZERO: hand_out returns the same 9-key Handoff as next_problem. The coach's `why` is stored
on the handout (pick_reason) and only shown after the attempt is logged.
The old slot picker (coach/picker.py, `service.next_problem`) stays as the FALLBACK: for a
student whose coach is offline ("Use the default pick").
"""
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select

from . import picker, rating, rules, state
from . import steps as stp
from .catalog import pattern_name
from .db import session_scope
from .models import Burned, Handout, Pattern, Problem
from .plans import active_plan, as_dict as plan_dict

DIFF_RANK = {"E": 0, "M": 1, "H": 2}


class PickRefused(ValueError):
    """hand_out broke a hard rule. The message says which."""


def _today(today):
    return today or date.today()


@dataclass
class Ctx:
    today: date
    cfg: dict
    plan: object
    problems: dict
    atts: list
    handouts: list
    retry: list
    pstate: list
    skills: list
    burned: set
    tiers: dict
    names: dict
    attempts_by_problem: dict = field(default_factory=dict)
    clean_by_problem: dict = field(default_factory=dict)
    last_by_problem: dict = field(default_factory=dict)
    touched: dict = field(default_factory=dict)       # problem -> last attempt/handout day
    blocked: set = field(default_factory=set)
    hards_ok: bool = False


def _ctx(s, user_id, today, create_session=False):
    cfg = state.load_config(s)
    sess = state.current_session(s, user_id, today, create=create_session)
    burned = set()
    if sess is not None:
        burned = set(s.scalars(select(Burned.pattern_slug).where(Burned.session_id == sess.id)))
    tiers, names = {}, {}
    for p in s.scalars(select(Pattern)):
        tiers[p.slug], names[p.slug] = p.tier, p.name
    for slug in cfg["tier1"]:
        tiers[slug] = 1
    for slug in cfg["tier2"]:
        tiers[slug] = 2
    c = Ctx(today=today, cfg=cfg, plan=active_plan(s, user_id), problems=state.all_problems(s),
            atts=state.user_attempts(s, user_id), handouts=state.user_handouts(s, user_id),
            retry=state.user_retry_items(s, user_id), pstate=state.user_pattern_state(s, user_id),
            skills=state.user_skills(s, user_id), burned=burned, tiers=tiers, names=names)
    for a in c.atts:
        n = a.problem_number
        c.attempts_by_problem[n] = c.attempts_by_problem.get(n, 0) + 1
        if a.outcome == "clean":
            c.clean_by_problem[n] = c.clean_by_problem.get(n, 0) + 1
        if n not in c.last_by_problem or a.day > c.last_by_problem[n]:
            c.last_by_problem[n] = a.day
        c.touched[n] = max(c.touched.get(n, a.day), a.day)
    for h in c.handouts:
        d = h.handed_at.date()
        c.touched[h.problem_number] = max(c.touched.get(h.problem_number, d), d)
    c.blocked = rules.blocklist(c.atts, c.handouts, today, cfg["block_days"])
    c.hards_ok = picker.hards_allowed(s, user_id, today, cfg)
    return c


def _hards_rule(c):
    if c.plan is not None:
        if c.plan.hards_from is None:
            return "Hards are off in the plan"
        return f"no Hards before {c.plan.hards_from.isoformat()} (plan)"
    return f"no Hards before {c.cfg['hards_from'].isoformat()}"


def refusal(c, p):
    """Why the server refuses to hand out problem p right now, or None if it may."""
    if p.number in c.blocked:
        return (f"{p.number} was attempted or handed out in the last {c.cfg['block_days']} days "
                f"(last {c.touched.get(p.number)})")
    if c.clean_by_problem.get(p.number, 0) >= c.cfg["clean_to_own"]:
        return f"{p.number} is retired ({c.cfg['clean_to_own']} clean solves)"
    if p.difficulty == "H" and not c.hards_ok:
        return _hards_rule(c)
    if p.pattern_slug and p.pattern_slug in c.burned:
        return f"the pattern of {p.number} is burned this session"
    return None


def _tier(c, slug):
    return c.tiers.get(slug, 3) if slug else None


def _open_handout(s, user_id, today):
    return s.scalars(select(Handout).where(
        Handout.user_id == user_id, Handout.status == "open", Handout.slot != "imported")
        .order_by(Handout.id.desc())).first()


# ---- read tools ------------------------------------------------------------------------------
from .notes import notes_for  # noqa: E402


def get_profile(user_id, today=None):
    """Everything the coach picks by: {today, plan, ledger, patterns, recent_attempts, backlog,
    streak, clean_rate}. Progress data: never shown to the learner before a handoff."""
    today = _today(today)
    with session_scope() as s:
        c = _ctx(s, user_id, today)
        clean, tried = rules.clean_counts(c.atts)
        last_day = {}
        for a in c.atts:
            if a.pattern_slug and (a.pattern_slug not in last_day or a.day > last_day[a.pattern_slug]):
                last_day[a.pattern_slug] = a.day
        level = {k.pattern_slug: k.level for k in c.skills if k.kind == "pattern" and k.pattern_slug}
        ps = {x.pattern_slug: x for x in c.pstate}
        focus = list(c.plan.focus_patterns or []) if c.plan else []
        slugs = list(dict.fromkeys(list(c.cfg["tier1"]) + list(c.cfg["tier2"]) + sorted(c.tiers)))
        patterns = {}
        for slug in slugs:
            st = ps.get(slug)
            patterns[slug] = {
                "name": c.names.get(slug) or pattern_name(slug), "tier": _tier(c, slug),
                "in_focus": slug in focus, "level": level.get(slug),
                "clean": clean[slug], "attempts": tried[slug],
                "owned": clean[slug] >= c.cfg["clean_to_own"],
                "last_attempt_day": last_day[slug].isoformat() if slug in last_day else None,
                "retired_on": st.retired_on.isoformat() if st and st.retired_on else None,
                "refires": list(st.refires or []) if st else [],
            }
        recent = []
        for a in sorted(c.atts, key=lambda a: (a.day, a.id), reverse=True)[:20]:
            p = c.problems.get(a.problem_number)
            recent.append({"day": a.day.isoformat(), "number": a.problem_number,
                           "title": p.title if p else "", "pattern": a.pattern_slug,
                           "outcome": a.outcome, "target_min": a.target_min,
                           "actual_min": a.actual_min, "bugs": a.bugs, "hints": a.hints,
                           "optimal": a.optimal, "derived_cold": a.derived_cold,
                           "notes": a.notes,
                           "problem_rating": rating.problem_rating(p) if p else None})
        ledger = [{"name": k.name, "kind": k.kind, "pattern": k.pattern_slug, "level": k.level,
                   "trend": k.trend, "evidence": k.evidence,
                   "updated_on": k.updated_on.isoformat() if k.updated_on else None}
                  for k in sorted(c.skills, key=lambda k: (k.level, k.name))]
        bl = rules.backlog(c.atts, today, c.cfg["backlog_start"], per_day(c))
        bl.pop("per_day", None)
        rb = rating.block(s, user_id)
        return {"today": today.isoformat(),
                "rating": rb, "mmr": rb["mmr"], "rank": rb["rank"], "games": rb["games"],
                "next_rank_at": rb["next_rank_at"], "recent_deltas": rb["recent_deltas"],
                "default_budget": {d: stp.split(stp.default_total(d, rb["rank"], False))
                                   for d in "EMH"},
                "code_speed": _code_speed(s, user_id),
                "notes": notes_for(s, user_id),
                "plan": plan_dict(c.plan, today) if c.plan else None,
                "ledger": ledger, "patterns": patterns, "recent_attempts": recent,
                "backlog": bl, "streak": rules.streak(c.atts, today),
                "clean_rate": rules.tier_progress(c.atts, today, c.cfg["clean_to_own"])
                .get("clean_rate")}


def _code_speed(s, user_id):
    from .scoring import code_speed
    return code_speed(s, user_id)


def per_day(c):
    """Problems a day: the plan's daily_problems, else config per_day."""
    return c.plan.daily_problems if c.plan is not None else c.cfg["per_day"]


def get_due(user_id, today=None):
    """{today, retry_due, retry_waiting, refire_due, blocklist, today_done, today_target,
    open_handout_id}. retry_due: queue items whose ladder wait has passed, most overdue first.
    refire_due: retired patterns 7+ days since the last touch with re-fires left."""
    today = _today(today)
    with session_scope() as s:
        c = _ctx(s, user_id, today)
        retry_due, waiting = [], []
        for r in rules.retry_due(c.retry, today, c.cfg["ladder"]):
            p = c.problems.get(r["problem_number"])
            row = {"problem_number": r["problem_number"], "title": p.title if p else "",
                   "pattern": p.pattern_slug if p else None, "attempts": r["attempts"],
                   "age": r["age"], "wait": r["wait"],
                   "last_on": r["last_on"].isoformat() if r.get("last_on") else None,
                   "reason": r.get("reason", "")}
            if r["due"]:
                why = refusal(c, p) if p else "unknown problem"
                row.update(blocked=why is not None, block_reason=why,
                           overdue_days=(r["age"] or 0) - r["wait"])
                retry_due.append(row)
            else:
                row["due_in_days"] = r["wait"] - (r["age"] or 0) if r["age"] is not None else None
                waiting.append(row)
        retry_due.sort(key=lambda r: -r["overdue_days"])
        refire = []
        for r in rules.refire_due(c.pstate, today, c.cfg["refire_days"]):
            if not r["due"] or r["refires_done"] >= rules.REFIRES_MAX or r["pattern"] in c.burned:
                continue
            fresh = [p for p in c.problems.values() if p.pattern_slug == r["pattern"]
                     and p.number not in c.attempts_by_problem and refusal(c, p) is None]
            refire.append({"pattern": r["pattern"], "age": r["age"],
                           "refires_done": r["refires_done"],
                           "last_touch": r["last_touch"].isoformat(),
                           "new_variants": len(fresh)})
        bl = rules.backlog(c.atts, today, c.cfg["backlog_start"], per_day(c))
        h = _open_handout(s, user_id, today)
        return {"today": today.isoformat(), "retry_due": retry_due, "retry_waiting": waiting,
                "refire_due": refire, "blocklist": sorted(c.blocked),
                "today_done": bl["done_today"], "today_target": bl["target_today"],
                "open_handout_id": h.id if h else None}


def _difficulty(v):
    if v in (None, ""):
        return None
    d = str(v).strip()[:1].upper()
    if d not in DIFF_RANK:
        raise ValueError("difficulty: E, M or H")
    return d


def find_problems(user_id, pattern=None, tier=None, untouched=None, difficulty=None, limit=20,
                  today=None):
    """Candidate problems, bank order (then E -> M -> H), blocked ones last:
    [{number, title, difficulty, pattern, tier, source, bank_order, attempts, last_attempt_day,
      clean_count, in_retry, blocked, block_reason}]. untouched=True: never attempted;
    False: attempted before. blocked = hand_out would refuse it now (block_reason says why)."""
    today = _today(today)
    d = _difficulty(difficulty)
    try:
        limit = max(1, min(200, int(limit or 20)))
    except (TypeError, ValueError):
        limit = 20
    if tier is not None and tier != "":
        tier = int(tier)
    else:
        tier = None
    with session_scope() as s:
        c = _ctx(s, user_id, today)
        open_retry = {r.problem_number for r in c.retry}
        rows = []
        for p in c.problems.values():
            if pattern and p.pattern_slug != pattern:
                continue
            if tier is not None and _tier(c, p.pattern_slug) != tier:
                continue
            if d and p.difficulty != d:
                continue
            n_att = c.attempts_by_problem.get(p.number, 0)
            if untouched is True and n_att:
                continue
            if untouched is False and not n_att:
                continue
            why = refusal(c, p)
            last = c.last_by_problem.get(p.number)
            rows.append({"number": p.number, "title": p.title, "difficulty": p.difficulty,
                         "pattern": p.pattern_slug, "tier": _tier(c, p.pattern_slug),
                         "source": p.source, "bank_order": p.bank_order, "attempts": n_att,
                         "last_attempt_day": last.isoformat() if last else None,
                         "clean_count": c.clean_by_problem.get(p.number, 0),
                         "in_retry": p.number in open_retry,
                         "rating": rating.problem_rating(p), "rated": p.rating is not None,
                         "blocked": why is not None, "block_reason": why})
        rows.sort(key=lambda r: (r["blocked"], r["bank_order"] is None,
                                 r["bank_order"] if r["bank_order"] is not None else 0,
                                 DIFF_RANK.get(r["difficulty"], 3), r["number"]))
        return rows[:limit]


# ---- the coach's pick ------------------------------------------------------------------------
def _infer_slot(c, p):
    """Internal slot of a coach pick, for reveal and the session log (never shown before)."""
    slug = p.pattern_slug
    if p.number in {r.problem_number for r in c.retry}:
        return "retry"
    if slug and any(x.pattern_slug == slug and x.retired_on for x in c.pstate):
        return "refire"
    if slug and not any(a.pattern_slug == slug for a in c.atts):
        return "growth"
    lvl = next((k.level for k in c.skills if k.kind == "pattern" and k.pattern_slug == slug), None)
    if lvl is not None and lvl >= 4:
        return "warmup"
    return "drill"


def hand_out(user_id, number, target_min=None, mode="coached", why="", today=None,
             phase_budget=None, budget_reason=""):
    """The coach's pick. Validates the hard rules (PickRefused with the reason, nothing written),
    then writes the handout stamped now by the server clock, starts the clock, stores `why` as
    pick_reason (private until logged), emits `handout`. Returns the public Handoff (9 keys).

    Time budget (coach/steps.py): every coach handout has steps understand -> approach -> code.
      phase_budget {understand, approach[, code]} minutes: target = their sum (target_min, if
        also given, must equal it). Each step >= 1 min. code left out: provisional = the
        default total (or target_min) minus the two, kept inside the range; the coach sets
        the real one at unlock_editor(code_min).
      only target_min: split 15 / 30 / 55 %.
      neither: the default, difficulty base (E10 / M15 / H35) x his rank factor (Bronze 1.3,
        Silver 1.15, Gold 1.0, Platinum 0.85, Diamond 0.75), x 0.8 if he attempted it before.
    The total must lie in E 5-20 / M 8-35 / H 15-60 min, else PickRefused. budget_reason is
    private (shown on reveal)."""
    from .service import emit
    today = _today(today)
    if mode not in picker.MODES:
        raise PickRefused(f"mode must be one of {', '.join(picker.MODES)}")
    if target_min is not None:
        if isinstance(target_min, bool):
            raise PickRefused("target_min must be 1..180")
        try:
            target_min = int(target_min)
        except (TypeError, ValueError):
            raise PickRefused("target_min must be 1..180") from None
        if not 1 <= target_min <= 180:
            raise PickRefused("target_min must be 1..180")
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise PickRefused(f"not a problem number: {number!r}") from None
    with session_scope() as s:
        h_open = _open_handout(s, user_id, today)
        if h_open is not None and h_open.handed_at.date() == today:
            raise PickRefused(f"handout {h_open.id} ({h_open.problem_number}) is still open; "
                              "log it or skip it first")
        c = _ctx(s, user_id, today, create_session=True)
        p = c.problems.get(number)
        if p is None:
            raise PickRefused(f"{number} is not in the problem bank")
        why_not = refusal(c, p)
        if why_not:
            raise PickRefused(why_not)
        budget = _budget(s, c, user_id, p, target_min, phase_budget)
        h = picker.create_handout(s, user_id, today, p, _infer_slot(c, p), p.pattern_slug,
                                  target_min=sum(budget.values()), mode=mode,
                                  pick_reason=(why or "").strip(), phase_budget=budget,
                                  budget_reason=(budget_reason or "").strip() or None)
        emit(user_id, "handout", h.id, s)
        return picker.handoff_for(h, p)


def _budget(s, c, user_id, p, target_min, phase_budget):
    """The handout's {understand, approach, code} minutes, or PickRefused."""
    d = p.difficulty or "M"
    try:
        if phase_budget is not None:
            b = stp.parse_budget(phase_budget)
            if "code" not in b:           # provisional: the coach sets it at unlock_editor
                if target_min is not None:
                    dflt = target_min
                else:
                    rk = rating.rank(rating.ensure(s, user_id).mmr)
                    dflt = stp.default_total(d, rk, p.number in c.attempts_by_problem)
                lo, hi = stp.TOTAL_RANGE.get(d, stp.TOTAL_RANGE["M"])
                code = max(1, dflt - b["understand"] - b["approach"])
                total = min(hi, max(lo, b["understand"] + b["approach"] + code))
                b["code"] = max(1, total - b["understand"] - b["approach"])
            total = sum(b.values())
            if target_min is not None and target_min != total:
                raise stp.BudgetRefused(f"target_min {target_min} differs from the phase_budget "
                                        f"sum {total}; leave target_min out")
        elif target_min is not None:
            total = target_min
            b = stp.split(total)
        else:
            rk = rating.rank(rating.ensure(s, user_id).mmr)
            total = stp.default_total(d, rk, p.number in c.attempts_by_problem)
            lo, hi = stp.TOTAL_RANGE.get(d, stp.TOTAL_RANGE["M"])
            total = min(hi, max(lo, total))
            b = stp.split(total)
        stp.check_total(d, sum(b.values()))
    except stp.BudgetRefused as e:
        raise PickRefused(str(e)) from None
    return b


def rate_problem(user_id, number, rating_value, why=""):
    """The coach rates a problem's hardness ONCE (first hand-out): inside the band of its
    level (E 800-1300, M 1200-1900, H 1700-2600), else PickRefused; a second rating is refused
    (fixed once set, no crowd adjustment). Returns {number, rating, why}. Never tell him."""
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise PickRefused(f"not a problem number: {number!r}") from None
    why = (why or "").strip()
    if not why:
        raise PickRefused("why: one line on what makes it this hard")
    from datetime import datetime
    with session_scope() as s:
        p = s.get(Problem, number)
        if p is None:
            raise PickRefused(f"{number} is not in the problem bank")
        if p.rating is not None:
            raise PickRefused(f"{number} is already rated {p.rating}; ratings are fixed once set")
        try:
            n = rating.check_band(p.difficulty, rating_value)
        except rating.RatingRefused as e:
            raise PickRefused(str(e)) from None
        p.rating, p.rating_reason, p.rated_at = n, why[:300], datetime.now()
        return {"number": p.number, "rating": n, "why": p.rating_reason}


def skip_open(user_id):
    """Skip the open handout (it still blocks for 7 days). {"ok", "handout_id"} or ValueError."""
    from .service import emit
    with session_scope() as s:
        h = _open_handout(s, user_id, None)
        if h is None:
            raise ValueError("no open problem")
        h.status = "skipped"
        emit(user_id, "handout", h.id, s)
        return {"ok": True, "handout_id": h.id}
