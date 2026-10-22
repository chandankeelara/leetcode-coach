"""Steps of a problem: understand -> approach -> code, each with a minute budget. Pure helpers
over a Handout row (no DB access); service.py does the writes.

* The current step is `phase_label`. Seconds per step come from the coach's clock, so pauses
  are excluded: current step = clock elapsed now - `phase_start_s`, plus what was banked in
  `phase_elapsed` when the step was left before (steps can be re-entered, e.g. lock_editor).
* understand -> approach only by the STUDENT (the "I understand the question" button,
  service.understood). The coach can force it only with clock(phase, force=True).
* approach -> code when the coach calls unlock_editor (lock_editor goes back to approach).
* A reset zeroes every step and re-arms the per-step checkpoints.

Budgets: the coach sets them at hand_out (phase_budget); the server clamps the total by
difficulty and each step to >= 1 min. Default = difficulty base x rank factor (x 0.8 for a
problem he has attempted before), split 15 / 30 / 55 %.
"""
from . import clock as clk

PHASES = ("understand", "approach", "code")
TITLES = {"understand": "Understand the question", "approach": "Approach", "code": "Code"}
DONE = {"understand": "Understood", "approach": "Approach", "code": "Code"}

BASE_MIN = {"E": 10, "M": 15, "H": 35}
TOTAL_RANGE = {"E": (5, 20), "M": (8, 35), "H": (15, 60)}
TIER_FACTOR = {"Bronze": 1.3, "Silver": 1.15, "Gold": 1.0, "Platinum": 0.85, "Diamond": 0.75}
RETRY_FACTOR = 0.8
SPLIT = (0.15, 0.30)                  # understand, approach; code gets the rest
IDLE_S = 180                         # no activity from him this long -> one `idle` event


class BudgetRefused(ValueError):
    """A phase_budget / target outside what the server allows. The message says why."""


def split(total):
    """{understand, approach, code} minutes summing to `total`, each >= 1."""
    total = max(3, int(total))
    u = max(1, round(total * SPLIT[0]))
    a = max(1, round(total * SPLIT[1]))
    c = max(1, total - u - a)
    return {"understand": u, "approach": a, "code": c}


def default_total(difficulty, rank, retry):
    base = BASE_MIN.get(difficulty, 15) * TIER_FACTOR.get(rank, 1.0)
    if retry:
        base *= RETRY_FACTOR
    return max(1, round(base))


def check_total(difficulty, total):
    lo, hi = TOTAL_RANGE.get(difficulty, TOTAL_RANGE["M"])
    if not lo <= total <= hi:
        raise BudgetRefused(f"total target {total} min is outside {lo}-{hi} min for this problem")


def parse_budget(raw):
    """{understand, approach, code} ints >= 1 from a dict (or [u, a, c]). BudgetRefused if not."""
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        raw = dict(zip(PHASES, raw))
    if not isinstance(raw, dict) or set(raw) != set(PHASES):
        raise BudgetRefused("phase_budget needs exactly understand, approach, code (minutes)")
    out = {}
    for k in PHASES:
        v = raw[k]
        if isinstance(v, bool):
            raise BudgetRefused(f"phase_budget.{k} must be whole minutes")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise BudgetRefused(f"phase_budget.{k} must be whole minutes") from None
        if f != int(f):
            raise BudgetRefused(f"phase_budget.{k} must be whole minutes")
        n = int(f)
        if n < 1:
            raise BudgetRefused(f"phase_budget.{k} must be at least 1 min")
        out[k] = n
    return out


# ---- live step state ---------------------------------------------------------------------
def has_steps(h):
    return bool(getattr(h, "phase_budget", None))


def current(h):
    p = getattr(h, "phase_label", None)
    return p if p in PHASES else None


def seconds(h, now=None):
    """{step: seconds on the coach's clock} including the running part of the current step."""
    out = {k: int((h.phase_elapsed or {}).get(k, 0)) for k in PHASES}
    cur = current(h)
    if cur:
        out[cur] += max(0, clk.elapsed_s(h, now) - (h.phase_start_s or 0))
    return out


def move(h, to, now=None):
    """Bank the current step and start `to` from now. Mutates h."""
    banked = seconds(h, now)
    h.phase_elapsed = banked
    h.phase_label = to
    h.phase_start_s = clk.elapsed_s(h, now)
    return banked


def on_reset(h):
    """A clock reset: every step back to 0 (the current one restarts with the clock)."""
    h.phase_elapsed = {k: 0 for k in PHASES}
    h.phase_start_s = 0
    h.phase_fired = []


def due_phase_checkpoint(h, now=None):
    """The current step when it has just passed 100% of its budget (once per step), else None.
    Never for a paused clock."""
    if not has_steps(h) or not clk.running(h):
        return None
    cur = current(h)
    if not cur or cur in (h.phase_fired or []):
        return None
    budget = (h.phase_budget or {}).get(cur)
    if not budget:
        return None
    return cur if seconds(h, now)[cur] >= budget * 60 else None


def state(h, now=None):
    """The step block of the page's clock: None for a handout without steps."""
    if not has_steps(h):
        return None
    cur = current(h)
    secs = seconds(h, now)
    b = h.phase_budget
    return {"current": cur, "index": PHASES.index(cur) + 1 if cur else None, "of": len(PHASES),
            "title": TITLES.get(cur), "budget": {k: b.get(k) for k in PHASES},
            "seconds": secs,
            "done": [{"phase": k, "label": DONE[k], "seconds": secs[k]}
                     for k in PHASES[:PHASES.index(cur)]] if cur else []}
