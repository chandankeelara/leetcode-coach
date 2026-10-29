"""The coach's clock: pure helpers over a Handout row and its clock_events. No DB access.

The coach owns the clock (start / pause / resume / reset / target / phase, see
service.clock). The time shown on the page and logged is `elapsed_s`. The server also keeps
`wall_s`, plain wall time since the first start, which no action changes.

Old rows have NULL clock columns: treated as running since handed_at, accum 0,
first_started_at = handed_at.
"""
from datetime import datetime

ACTIONS = ("start", "pause", "resume", "reset", "target", "phase", "adjust")
CHECKPOINTS = (50, 100, 150)        # % of target; one synthetic event each per handout
TARGET_MIN, TARGET_MAX = 1, 180
# clock_state "stopped": the solve is over (Accepted -> the review step, coach/steps.py). The
# coach's clock is frozen for good; no clock action restarts it.
STOPPED = "stopped"


def running(h):
    return (h.clock_state or "running") == "running"


def stopped(h):
    return h.clock_state == STOPPED


def anchor(h):
    return h.clock_anchor or h.handed_at


def first_started(h):
    return h.first_started_at or h.handed_at


def elapsed_s(h, now=None):
    """Seconds on the coach's clock: banked + the running segment."""
    now = now or datetime.now()
    acc = h.clock_accum_s or 0
    if running(h):
        acc += max(0.0, (now - anchor(h)).total_seconds())
    return max(0, int(acc))


def wall_s(h, now=None):
    """Plain wall seconds since the clock first started. Pauses and resets do not touch it."""
    now = now or datetime.now()
    return max(0, int((now - first_started(h)).total_seconds()))


def _get(e, k):
    return e.get(k) if isinstance(e, dict) else getattr(e, k)


def record(events, end=None):
    """Replay clock_events (oldest first) -> {"pauses": [{reason, minutes, at}],
    "resets": [{reason, at}]}. A pause counts only when the clock was running; it lasts until
    the next resume / start / reset (or `end`, default now, if still paused)."""
    end = end or datetime.now()
    pauses, resets, adjusts = [], [], []
    run, open_p = True, None
    for e in events:
        act, at, why = _get(e, "action"), _get(e, "at"), _get(e, "reason") or ""
        if act == "pause" and run:
            run, open_p = False, {"reason": why, "at": at}
        elif act in ("resume", "start", "reset") and not run:
            pauses.append(_closed(open_p, at))
            run, open_p = True, None
        if act == "adjust":
            v = _get(e, "value") or {}
            adjusts.append({"reason": why, "minutes": round((v.get("minus_s") or 0) / 60),
                            "minus_s": v.get("minus_s") or 0, "step": v.get("step"),
                            "at": at.isoformat() if at else None})
        if act == "reset":
            resets.append({"reason": why, "at": at.isoformat() if at else None})
    if open_p is not None:
        pauses.append(_closed(open_p, end))
    return {"pauses": pauses, "resets": resets, "adjusts": adjusts}


def _closed(p, until):
    secs = max(0.0, (until - p["at"]).total_seconds())
    return {"reason": p["reason"], "minutes": round(secs / 60), "at": p["at"].isoformat()}


def note(rec):
    """'2 pauses (float precision 4 min; aside 1 min), 1 reset' or '' when nothing happened."""
    bits = []
    ps, rs, ads = rec["pauses"], rec["resets"], rec.get("adjusts") or []
    if ps:
        inner = "; ".join(f"{p['reason'] or 'no reason'} {p['minutes']} min" for p in ps)
        bits.append(f"{len(ps)} pause{'s' if len(ps) != 1 else ''} ({inner})")
    if rs:
        bits.append(f"{len(rs)} reset{'s' if len(rs) != 1 else ''}")
    if ads:
        inner = "; ".join(f"−{a['minutes']} min away: {a['reason']}" for a in ads)
        bits.append(f"{len(ads)} adjust{'s' if len(ads) != 1 else ''} ({inner})")
    return ", ".join(bits)


def state(h, now=None, events=()):
    """The clock as the page and the bridge see it."""
    now = now or datetime.now()
    rec = record(events, now)
    a = anchor(h)
    return {
        "state": "running" if running(h) else (STOPPED if stopped(h) else "paused"),
        "elapsed_s": elapsed_s(h, now),
        "wall_s": wall_s(h, now),
        "target_min": h.target_min,
        "phase": h.phase_label or None,
        "anchor_iso": a.isoformat() if a else None,
        "accum_s": h.clock_accum_s or 0,
        "pause_reason": (_last_pause_reason(events) if not running(h) and not stopped(h)
                         else None),
        "pauses": len(rec["pauses"]),
        "resets": len(rec["resets"]),
    }


def _last_pause_reason(events):
    for e in reversed(list(events)):
        if _get(e, "action") == "pause":
            return _get(e, "reason") or ""
    return ""


def due_checkpoints(h, now=None):
    """Thresholds (of CHECKPOINTS) crossed and not yet fired. Never for a paused clock."""
    if not running(h) or not h.target_min:
        return []
    pct = 100.0 * elapsed_s(h, now) / (h.target_min * 60)
    fired = set(h.checkpoints_fired or [])
    return [c for c in CHECKPOINTS if pct >= c and c not in fired]
