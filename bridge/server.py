"""stdio MCP server: Claude Code (the coach) <-> coach.service (the DB the website writes to).

    python -m bridge.server

Env: COACH_DB (sqlite path, default ./coach.db), COACH_USER (username; default: the first
user, the app is single-user).

How Claude learns of website events: it calls `wait_for_event`, which blocks until the web UI
pushes one (approach / submission / stuck / skip / chat / timer_over) or the timeout passes.

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
                          "started_at", "target_min", "instruction"})
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
    st.setdefault("accountability", _days_missed(st.get("today")))
    return st


def _days_missed(today_iso):
    """{last_practice_day, days_missed}: whole days with no attempt since the last practice
    day (yesterday practised -> 0). Bridge-side because the opening line needs it plainly."""
    try:
        from sqlalchemy import func, select
        from coach.db import session_scope
        from coach.models import Attempt
        with session_scope() as s:
            last = s.scalar(select(func.max(Attempt.day)).where(Attempt.user_id == uid()))
    except Exception:
        return {"last_practice_day": None, "days_missed": None}
    today = date.fromisoformat(today_iso) if today_iso else date.today()
    if last is None:
        return {"last_practice_day": None, "days_missed": None}
    return {"last_practice_day": last.isoformat(),
            "days_missed": max(0, (today - last).days - 1)}


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
    return h


@mcp.tool()
def set_statement(handout_id: int, text: str) -> dict:
    """Write the problem statement, examples and constraints in your own words. Rejected if it names a technique or a difficulty."""
    try:
        svc().set_statement(uid(), handout_id, text)
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True}


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
    timer_over. Call it at the END of every turn while he is working. Claude Code may move it
    to the background after ~2 min; that is fine, the result still arrives."""
    return _jsonable(await _wait(timeout_s, ctx))


@mcp.tool()
def say(text: str) -> dict:
    """Post one coach message to the chat panel. One thing per message."""
    mid = svc().post_message(uid(), "coach", text)
    return {"message_id": mid}


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
    """Record the attempt. actual_min defaults to now minus the handout start time. Returns
    outcome and retry-queue action."""
    if actual_min is None:
        actual_min = _minutes_since_handout(handout_id)
    r = svc().log_attempt(uid(), handout_id=handout_id, actual_min=actual_min, bugs=bugs,
                          optimal=optimal, derived_cold=derived_cold, hints=hints,
                          notes=notes, abandoned=abandoned)
    out = _jsonable(r)
    out["actual_min"] = actual_min
    return out


@mcp.tool()
def reveal(handout_id: int) -> dict:
    """What the problem was testing. Refused until the attempt is logged."""
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


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
