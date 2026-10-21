"""stdio MCP server: Claude Code (the coach) <-> coach.service (the DB the website writes to).

    python -m bridge.server

Env: COACH_DB (sqlite path, default ./coach.db), COACH_USER (username; default: the first
user, the app is single-user).

How Claude learns of website events: it calls `wait_for_event`, which blocks until the web UI
pushes one (approach / submission / stuck / skip / chat / timer_over) or the timeout passes.
The server adds `clock_checkpoint` {pct, elapsed_min, target_min, has_approach} when the
coach's clock crosses 50 / 100 / 150 % of target.

RULE ZERO lives here structurally:
  - `next_problem` returns the Handoff fields and nothing else (extra keys are stripped and
    logged), and it posts the handoff to the chat panel itself, so the message that hands out
    a problem is built by code, never by the model.
  - tool descriptions name no patterns, slots, tiers or difficulties.
"""
import asyncio
import json
import os
import sys
from collections import deque
from datetime import date, datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

HANDOFF_KEYS = frozenset({"handout_id", "number", "title", "leetcode_url", "paraphrase",
                          "started_at", "target_min", "instruction", "mode"})
# mode is how the coach behaves, never the slot: timed = answer direct questions only,
# coached = full Socratic. Anything else is refused at the boundary.
HANDOFF_MODES = frozenset({"timed", "coached"})
STATEMENT_COMING = "Statement coming."
DEFAULT_REPORT_DATE = "2026-12-15"
PROGRESS_EVERY_S = 30.0       # keep-alive so the client's idle timeout never fires

# ---- service wiring (tests swap in a fake with set_service) --------------------------------
_svc = None
_uid = None
_pending: deque = deque()     # events drained from the DB but not yet handed to the model


def set_service(svc, user_id=None):
    """Point the bridge at a service object (the real coach.service or a test fake)."""
    global _svc, _uid
    _svc, _uid = svc, user_id
    _pending.clear()


def svc():
    global _svc
    if _svc is None:
        from coach import db, service
        db.init(os.environ.get("COACH_DB") or None)
        _svc = service
    return _svc


def uid():
    global _uid
    if _uid is None:
        name = (os.environ.get("COACH_USER") or "").strip()
        s = svc()
        if name:
            _uid = _user_by_name(name)
        else:
            _uid = s.default_user_id()
        if _uid is None:
            raise RuntimeError("no user: set COACH_USER or run `python -m coach.cli create-user`")
    return _uid


def _user_by_name(name):
    from sqlalchemy import select
    from coach.db import session_scope
    from coach.models import User
    with session_scope() as s:
        u = s.scalars(select(User.id).where(User.username == name)).first()
    if u is None:
        raise RuntimeError(f"COACH_USER={name!r} is not a user in the database")
    return u


def _jsonable(obj):
    """Plain JSON types only (dates -> iso strings, pydantic -> dict)."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.loads(json.dumps(obj, default=_default))


def _default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _log(msg):
    print(f"[coach-bridge] {msg}", file=sys.stderr, flush=True)


# ---- RULE ZERO boundary --------------------------------------------------------------------
def clean_handoff(raw):
    """Exactly HANDOFF_KEYS. Anything else is stripped (and logged: it means a leak upstream)."""
    d = _jsonable(raw)
    extra = set(d) - HANDOFF_KEYS
    if extra:
        _log(f"RULE ZERO: stripped non-handoff keys {sorted(extra)}")
    missing = HANDOFF_KEYS - set(d)
    if missing:
        raise ValueError(f"handoff missing keys {sorted(missing)}")
    if d["mode"] not in HANDOFF_MODES:
        raise ValueError(f"handoff mode must be one of {sorted(HANDOFF_MODES)}")
    out = {k: d[k] for k in HANDOFF_KEYS}
    assert set(out) == HANDOFF_KEYS
    return out


def render_handoff(h):
    """The chat-panel text of a handoff. Built from the handoff fields only."""
    started = str(h["started_at"])
    hhmm = started[11:16] if len(started) >= 16 else started
    lines = [f"{h['number']}. {h['title']}", h["leetcode_url"]]
    if h.get("paraphrase"):
        lines += ["", h["paraphrase"]]
    lines += ["", f"Start {hhmm}. Target {h['target_min']} min.", h["instruction"]]
    return "\n".join(lines)


# ---- server --------------------------------------------------------------------------------
mcp = MCPServer(
    "coach",
    instructions=("LeetCode coaching session tools. Driven by the /coach skill. "
                  "Messages to the learner go through `say`; learner actions arrive through "
                  "`wait_for_event`."),
)


@mcp.tool()
def coach_status() -> dict:
    """Progress briefing: backlog, days missed, streak, retry queue, skills. Progress talk:
    never in or right before a problem handoff."""
    st = _jsonable(svc().status(uid()))
    if not isinstance(st.get("accountability"), dict):
        st["accountability"] = _days_missed(st.get("today"))
    return st


def _days_missed(today_iso):
    """Fallback for a service whose status() has no `accountability`: {last_practice_day,
    days_missed, missed_days_list} from the attempts table (see coach.rules.missed_days)."""
    from coach import rules
    try:
        from sqlalchemy import select
        from coach.db import session_scope
        from coach.models import Attempt
        with session_scope() as s:
            days = list(s.scalars(select(Attempt.day).where(Attempt.user_id == uid())))
    except Exception:
        return {"last_practice_day": None, "days_missed": None, "missed_days_list": []}
    today = date.fromisoformat(today_iso) if today_iso else date.today()
    return _jsonable(rules.missed_days(days, today))


@mcp.tool()
def next_problem(new: bool = False) -> dict:
    """Hand out the next problem. Posts the handoff to the chat panel itself and returns the
    same fields. Returns the still-open one unless new=true. Say nothing else alongside it."""
    s = svc()
    try:
        raw = s.next_problem(uid(), force_new=new)
    except TypeError:                       # a service without force_new
        raw = s.next_problem(uid())
    if raw is None:
        return {"handoff": None, "reason": "nothing eligible right now"}
    h = clean_handoff(raw)
    s.post_message(uid(), "coach", render_handoff(h), handout_id=h["handout_id"])
    if not h.get("paraphrase"):
        s.post_message(uid(), "coach", STATEMENT_COMING, handout_id=h["handout_id"])
    return h


@mcp.tool()
def set_statement(handout_id: int, text: str) -> dict:
    """Write the problem statement, examples and constraints in your own words. Rejected if it names a technique or a difficulty. On success it is also posted to the chat panel."""
    try:
        svc().set_statement(uid(), handout_id, text)
    except ValueError as e:
        return {"error": str(e)}
    svc().post_message(uid(), "coach", text.strip(), handout_id=handout_id)
    return {"ok": True}


def _open_handout():
    s = svc()
    f = getattr(s, "open_handout_id", None)
    return f(uid()) if f else None


@mcp.tool()
def show_visual(kind: str, spec: dict) -> dict:
    """Draw something into his discussion thread, as your message: array | grid | list | tree | steps | io | html. Captions go through the leak guard."""
    hid = _open_handout()
    if hid is None:
        return {"error": "no open problem"}
    try:
        r = svc().show_visual(uid(), hid, kind, spec)
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True, "id": r["id"]}


@mcp.tool()
def unlock_editor() -> dict:
    """Open the code editor on his page. Only after the idea holds."""
    hid = _open_handout()
    if hid is None:
        return {"error": "no open problem"}
    return svc().unlock_editor(uid(), hid)


@mcp.tool()
def lock_editor() -> dict:
    """Lock the code editor again."""
    hid = _open_handout()
    if hid is None:
        return {"error": "no open problem"}
    return svc().lock_editor(uid(), hid)


@mcp.tool()
def reset_timer() -> dict:
    """Restart the clock on the open problem from now."""
    hid = _open_handout()
    if hid is None:
        return {"error": "no open problem"}
    try:
        return _jsonable(svc().reset_timer(uid(), hid))
    except ValueError as e:
        return {"error": str(e)}


def _clock(action, reason="", target_min=None, label=None):
    hid = _open_handout()
    if hid is None:
        return {"error": "no open problem"}
    try:
        return _jsonable(svc().clock(uid(), hid, action, reason=reason, target_min=target_min,
                                     label=label))
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def clock(action: str, reason: str = "", target_min: int | None = None,
          label: str | None = None) -> dict:
    """Control his clock: start | pause | resume | reset | target | phase. Say why in chat when you pause or reset."""
    return _clock(action, reason, target_min, label)


@mcp.tool()
def clear_visuals() -> dict:
    """Clear every drawing of the open problem from his thread (each becomes a muted "Illustration cleared." line)."""
    hid = _open_handout()
    if hid is None:
        return {"error": "no open problem"}
    return {"ok": True, "cleared": svc().clear_visuals(uid(), hid)}


async def _wait(timeout_s, ctx=None):
    if _pending:
        return _pending.popleft()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout_s))
    while True:
        left = deadline - loop.time()
        if left <= 0:
            return {"kind": "timeout"}
        got = await svc().wait_event(uid(), min(left, PROGRESS_EVERY_S))
        if isinstance(got, dict):
            got = [got]
        if got:
            for e in got:
                _pending.append({"kind": e.get("kind"), "payload": e.get("payload") or {}})
            return _pending.popleft()
        if ctx is not None:
            try:
                await ctx.report_progress(float(timeout_s) - max(0.0, deadline - loop.time()),
                                          float(timeout_s), "waiting for the learner")
            except Exception:
                pass


@mcp.tool()
async def wait_for_event(timeout_s: int = 1500, ctx: Context = None) -> dict:
    """Block until the learner does something on the website, then return {kind, payload};
    {kind: "timeout"} if nothing came. kind: approach | submission | stuck | skip | chat |
    timer_over | clock_checkpoint. Call it at the END of every turn while he is working. Claude Code may move it
    to the background after ~2 min; that is fine, the result still arrives."""
    return _jsonable(await _wait(timeout_s, ctx))


@mcp.tool()
def say(text: str, clock_action: str | None = None, clock_reason: str = "",
        target_min: int | None = None, label: str | None = None) -> dict:
    """Post one coach message to the chat panel. One thing per message. Optional clock_action
    (as in `clock`) is applied first, so "Clock stopped. <why>." and the pause are one call;
    if the clock change fails nothing is posted."""
    out = {}
    if clock_action:
        c = _clock(clock_action, clock_reason, target_min, label)
        if "error" in c:
            return {"error": c["error"], "posted": False}
        out["clock"] = c
    out["message_id"] = svc().post_message(uid(), "coach", text)
    return out


def _minutes_since_handout(handout_id):
    from coach.db import session_scope
    from coach.models import Handout
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None:
            raise ValueError(f"no handout {handout_id}")
        return max(1, round((datetime.now() - h.handed_at).total_seconds() / 60))


@mcp.tool()
def log_attempt(handout_id: int, bugs: int, hints: int, optimal: bool, derived_cold: bool,
                notes: str = "", abandoned: bool = False, actual_min: int | None = None) -> dict:
    """Record the attempt. actual_min defaults to the minutes on your clock (pauses excluded);
    pass it only if the clock was broken. Returns outcome, retry-queue action, actual_min,
    wall_min and clock_note."""
    kw = {}
    if actual_min is not None:
        kw["actual_min"] = actual_min
    elif not hasattr(svc(), "clock"):          # an older service: wall time since handout
        kw["actual_min"] = _minutes_since_handout(handout_id)
    r = svc().log_attempt(uid(), handout_id=handout_id, bugs=bugs, optimal=optimal,
                          derived_cold=derived_cold, hints=hints, notes=notes,
                          abandoned=abandoned, **kw)
    out = _jsonable(r)
    out.setdefault("actual_min", kw.get("actual_min"))
    return out


@mcp.tool()
def reveal(handout_id: int) -> dict:
    """What the problem was testing. Refused until the attempt is logged. After a re-fire it
    also carries trap_quiz {pattern, cues: [{cue, trap}]} for the spoken trap quiz."""
    try:
        return _jsonable(svc().reveal(handout_id))
    except Exception as e:                   # RevealBlocked and friends
        return {"error": str(e)}


@mcp.tool()
def burn_pattern(slug: str, reason: str) -> dict:
    """A pattern was leaked: take it out of play for this session."""
    return {"burned_id": svc().burn_pattern(uid(), slug, reason)}


@mcp.tool()
def lint(code: str) -> dict:
    """Run the submission checker on Python source. Returns hits [{line, kind, msg}]."""
    from coach.lint import lint as _lint
    return {"hits": [{"line": ln, "kind": k, "msg": m} for ln, k, m in _lint(code)]}


@mcp.tool()
def report(target_date: str = DEFAULT_REPORT_DATE) -> dict:
    """Readiness projection to target_date (YYYY-MM-DD). Progress talk."""
    return _jsonable(svc().report(uid(), target_date))


@mcp.tool()
def log_trap_recall(pattern_slug: str, trap: str, cue: str, recalled: bool,
                    hit_in_code: bool) -> dict:
    """Record one trap-quiz answer (after reveal of a re-fire). Returns the stored row:
    date, pattern, trap, cue, recalled, hit_in_code."""
    try:
        return _jsonable(svc().log_trap_recall(uid(), pattern_slug, trap, cue, recalled,
                                               hit_in_code))
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def record_refire(pattern_slug: str) -> dict:
    """Add today's date to a retired pattern's re-fire dates (once per day)."""
    try:
        return _jsonable(svc().record_refire(uid(), pattern_slug))
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def set_skill(name: str, level: int, evidence: str, trend: str) -> dict:
    """Update one skill-ledger entry: level 0-5, evidence, trend. Session end only, one call per
    skill the session produced evidence for. Returns was/level."""
    try:
        return _jsonable(svc().set_skill(uid(), name, level, evidence, trend))
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def write_session_log(markdown: str) -> dict:
    """Store today's session log (markdown, session-log template format). Overwrites."""
    try:
        return _jsonable(svc().write_session_log(uid(), markdown))
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def session_summary() -> dict:
    """Today's handouts, attempts, reveals, ledger, queue and trap-quiz changes, for writing the
    session log. Progress talk: session end only, never while a problem is open."""
    return _jsonable(svc().session_summary(uid()))


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
