"""Study plans: what the coach picks problems by (coach/picks.py reads them).

One active `plans` row per user. A user with history from before plans existed gets the old
repo's PLAN.md as his plan (`migrate_legacy`, run by `db.init`); a brand-new user has none and
the web app sends him to /onboarding first.
"""
from datetime import date, datetime

from sqlalchemy import select

from . import rules
from .db import session_scope
from .models import Attempt, Plan

# PLAN.md (the old markdown repo), 2026-10-09. Binding for the owner; the backlog start date
# stays in config (backlog_start).
LEGACY_PLAN = {
    "title": "The Plan: maximum preparation",
    "target_date": date(2026, 12, 15),
    "daily_problems": 2,
    "minutes_per_day": 90,
    "focus_patterns": list(rules.TIER1) + list(rules.TIER2),
    "phases": [
        {"name": "A", "start": "2026-10-09", "end": "2026-10-31",
         "rules": "Tier 1 to automatic: all ten Tier-1 patterns at 3 clean solves. Blocked "
                  "practice while a skill is level <= 2; interleaved at 3+. Close heap and "
                  "linked list. One timed OA simulation a week."},
        {"name": "B", "start": "2026-11-01", "end": "2026-11-23",
         "rules": "Tier 2 (monotonic deque, binary search on answer, backtracking, dp-1d, "
                  "toposort, union-find), Hards start here without labels, company-tagged sets. "
                  "Narrate every third problem aloud. One timed OA simulation a week."},
        {"name": "C", "start": "2026-11-24", "end": "2026-12-15",
         "rules": "Simulate and repair: two timed OA simulations a week (2 problems, 70 min, no "
                  "help). Close everything in the retry queue. Always submit something."},
    ],
    "hards_from": date(2026, 11, 1),
    "notes": ("From PLAN.md. 90 min = 60 solving, 20 review, 10 pattern note. Clean = 0 bugs, "
              "<= 1.5x target, optimal, derived cold. Retired patterns get a NEW variant every "
              "7-14 days plus a spoken trap quiz, three times. Session split weeks 1-3: new 65%, "
              "re-fire 15%, timed sim 20%."),
}

FIELDS = ("title", "target_date", "daily_problems", "minutes_per_day", "focus_patterns",
          "phases", "hards_from", "notes")


def _date(v, what):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip())
    except ValueError:
        raise ValueError(f"{what}: a date YYYY-MM-DD, got {v!r}") from None


def _int(v, what, lo, hi):
    if isinstance(v, bool):
        raise ValueError(f"{what}: an integer {lo}..{hi}")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: an integer {lo}..{hi}, got {v!r}") from None
    if not lo <= n <= hi:
        raise ValueError(f"{what}: an integer {lo}..{hi}, got {n}")
    return n


def clean_fields(fields, known_patterns=None):
    """Validate and normalise set_plan keyword fields. ValueError on anything bad."""
    bad = set(fields) - set(FIELDS)
    if bad:
        raise ValueError(f"unknown plan field(s): {', '.join(sorted(bad))}")
    out = {}
    for k, v in fields.items():
        if k == "title":
            v = (v or "").strip()[:200] or "My plan"
        elif k in ("target_date", "hards_from"):
            v = _date(v, k)
        elif k == "daily_problems":
            v = _int(v, k, 1, 20)
        elif k == "minutes_per_day":
            v = _int(v, k, 10, 600)
        elif k == "focus_patterns":
            if isinstance(v, str):
                v = [x for x in (p.strip() for p in v.split(",")) if x]
            v = list(dict.fromkeys(str(x).strip() for x in (v or []) if str(x).strip()))
            if known_patterns is not None:
                unknown = [x for x in v if x not in known_patterns]
                if unknown:
                    raise ValueError(f"focus_patterns: unknown pattern(s) {', '.join(unknown)}")
        elif k == "phases":
            phases = []
            for i, ph in enumerate(v or [], 1):
                if not isinstance(ph, dict):
                    raise ValueError(f"phase {i}: an object {{name, start, end, rules}}")
                start, end = _date(ph.get("start"), f"phase {i} start"), _date(
                    ph.get("end"), f"phase {i} end")
                if start and end and end < start:
                    raise ValueError(f"phase {i}: end before start")
                phases.append({"name": str(ph.get("name") or i).strip()[:40],
                               "start": start.isoformat() if start else None,
                               "end": end.isoformat() if end else None,
                               "rules": str(ph.get("rules") or "").strip()})
            v = phases
        elif k == "notes":
            v = (v or "").strip()
        out[k] = v
    return out


def current_phase(plan_row, today):
    """The phase dict whose [start, end] holds today, else None."""
    for ph in plan_row.phases or []:
        s, e = ph.get("start"), ph.get("end")
        if (not s or date.fromisoformat(s) <= today) and (not e or today <= date.fromisoformat(e)):
            return ph
    return None


def as_dict(p, today=None):
    today = today or date.today()
    return {
        "id": p.id, "user_id": p.user_id, "title": p.title,
        "target_date": p.target_date.isoformat() if p.target_date else None,
        "days_left": (p.target_date - today).days if p.target_date else None,
        "daily_problems": p.daily_problems, "minutes_per_day": p.minutes_per_day,
        "focus_patterns": list(p.focus_patterns or []), "phases": list(p.phases or []),
        "current_phase": current_phase(p, today),
        "hards_from": p.hards_from.isoformat() if p.hards_from else None,
        "hards_allowed": bool(p.hards_from and p.hards_from <= today),
        "notes": p.notes or "", "active": bool(p.active),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def active_plan(s, user_id):
    return s.scalars(select(Plan).where(Plan.user_id == user_id, Plan.active.is_(True))
                     .order_by(Plan.id.desc())).first()


def migrate_legacy(s):
    """Every user with attempts but no plan row at all gets LEGACY_PLAN (the PLAN.md values).
    Idempotent. Brand-new users (no history) get nothing: they go through onboarding."""
    from .models import User
    have = set(s.scalars(select(Plan.user_id)))
    with_history = set(s.scalars(select(Attempt.user_id).distinct()))
    made = []
    for uid in s.scalars(select(User.id).order_by(User.id)):
        if uid in with_history and uid not in have:
            now = datetime.now()
            s.add(Plan(user_id=uid, active=True, created_at=now, updated_at=now, **LEGACY_PLAN))
            made.append(uid)
    return made


def pattern_choices():
    """[{slug, name, tier}] for the focus checkboxes: Tier 1, Tier 2, then the rest by name."""
    from .catalog import pattern_name
    from .models import Pattern
    with session_scope() as s:
        rows = {p.slug: (p.name, p.tier) for p in s.scalars(select(Pattern))}
    out = []
    for i, slug in enumerate(list(rules.TIER1) + list(rules.TIER2)):
        name = rows.get(slug, (pattern_name(slug), None))[0]
        out.append({"slug": slug, "name": name, "tier": 1 if i < len(rules.TIER1) else 2})
    rest = sorted((v[0], k) for k, v in rows.items() if k not in rules.CORE)
    out += [{"slug": k, "name": n, "tier": 3} for n, k in rest]
    return out


# ---- service-level API (re-exported by coach.service) ----------------------------------------
def get_plan(user_id, today=None):
    """The user's active plan as a dict (with current_phase, days_left, hards_allowed), or None."""
    with session_scope() as s:
        p = active_plan(s, user_id)
        return as_dict(p, today) if p else None


def set_plan(user_id, today=None, **fields):
    """Create or update the user's active plan with the given fields (see FIELDS). A new plan
    starts from defaults: 2 problems/day, 90 min, no focus, no phases, Hards never.
    ValueError on a bad field. Returns get_plan()."""
    from .models import Pattern
    with session_scope() as s:
        known = set(s.scalars(select(Pattern.slug))) | set(rules.CORE)
        vals = clean_fields(fields, known)
        p = active_plan(s, user_id)
        now = datetime.now()
        if p is None:
            p = Plan(user_id=user_id, active=True, created_at=now, title="My plan",
                     daily_problems=2, minutes_per_day=90, focus_patterns=[], phases=[],
                     notes="")
            s.add(p)
        for k, v in vals.items():
            setattr(p, k, v)
        p.updated_at = now
        s.flush()
        return as_dict(p, today)
