"""The coach's tools, defined ONCE. Every transport builds from this registry:

    website /mcp     remote MCP (Streamable HTTP), Bearer coach token    coach/web/remote.py
    website /tools   plain REST (GET list, POST call), same token         coach/web/remote.py
    bridge.server    local stdio MCP: direct DB (dev) or forwarding to /tools  bridge/server.py

A tool is a plain function `fn(ctx, **args) -> dict`. `ctx` carries the user: `ctx.user_id`
comes from the token (or COACH_USER for the local bridge), NEVER from tool arguments.

RULE ZERO lives here structurally:
  - `hand_out` and `next_problem` return the Handoff fields and nothing else (extra keys are
    stripped and logged), and post the handoff to the chat panel themselves, so the message
    that hands out a problem is built by code, never by the model.
  - every coach text (`say`, statements, drawings, boxes, test labels) is refused while it
    names the open problem's pattern before he has named it.
  - tool descriptions name no patterns, slots, tiers or difficulties.

How Claude learns of website events: `wait_for_event` blocks until the web UI pushes one
(approach / submission / stuck / skip / chat / timer_over / clock_checkpoint / ...) or the
timeout passes. Events drained together are buffered per user and handed out one per call.
"""
import asyncio
import inspect
import json
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Awaitable, Callable

HANDOFF_KEYS = frozenset({"handout_id", "number", "title", "leetcode_url", "paraphrase",
                          "started_at", "target_min", "instruction", "mode"})
# mode is how the coach behaves, never the slot: timed = answer direct questions only,
# coached = full Socratic. Anything else is refused at the boundary.
HANDOFF_MODES = frozenset({"timed", "coached"})
STATEMENT_COMING = "Statement coming."
DEFAULT_REPORT_DATE = "2026-12-15"
PROGRESS_EVERY_S = 30.0       # keep-alive so the client's idle timeout never fires
INSTRUCTIONS = ("LeetCode coaching session tools. Driven by the /coach skill. "
                "Messages to the learner go through `say`; learner actions arrive through "
                "`wait_for_event`.")

# events drained from the DB but not yet handed to the model, per user
_PENDING: dict = {}


def pending(user_id) -> deque:
    return _PENDING.setdefault(user_id, deque())


def reset_pending():
    _PENDING.clear()


@dataclass
class Ctx:
    """Who is calling and how to reach the service. `progress(done, total, message)` is an
    optional async keep-alive (MCP progress notifications); REST passes none."""
    user_id: int
    svc: Any = None
    progress: Callable[[float, float, str], Awaitable[None]] | None = None

    def __post_init__(self):
        if self.svc is None:
            from . import service
            self.svc = service


@dataclass
class Tool:
    name: str
    fn: Callable[..., Any]
    description: str
    is_async: bool = False
    params: list = field(default_factory=list)      # inspect.Parameters after ctx

    async def call(self, ctx, args):
        """Run the tool. Sync tools run in a worker thread so a slow one (judge, run_tests)
        never stalls the event loop that serves the website. A result with `error`, or an
        exception, is filed as friction for the dev team (feedback.record_friction)."""
        import anyio
        try:
            if self.is_async:
                out = await self.fn(ctx, **args)
            else:
                out = await anyio.to_thread.run_sync(lambda: self.fn(ctx, **args))
        except Exception as e:
            await anyio.to_thread.run_sync(lambda: friction(ctx, self.name, f"{type(e).__name__}: {e}"))
            raise
        if isinstance(out, dict) and out.get("error"):
            await anyio.to_thread.run_sync(lambda: friction(ctx, self.name, out["error"]))
        return out


def friction(ctx, name, error):
    """File a failed tool call as friction (never raises; a service without it is skipped)."""
    f = getattr(ctx.svc, "record_friction", None)
    if f is None:
        return None
    try:
        return f(ctx.user_id, name, error)
    except Exception:
        return None


TOOLS: dict[str, Tool] = {}


def tool(fn):
    """Register fn(ctx, ...) under its own name; its docstring is the description."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())[1:]
    TOOLS[fn.__name__] = Tool(fn.__name__, fn, fn.__doc__ or "",
                              asyncio.iscoroutinefunction(fn), params)
    return fn


# ---- helpers -------------------------------------------------------------------------------
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
    print(f"[coach-tools] {msg}", file=sys.stderr, flush=True)


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


def _post_handoff(ctx, raw):
    h = clean_handoff(raw)
    ctx.svc.post_message(ctx.user_id, "coach", render_handoff(h), handout_id=h["handout_id"])
    if not h.get("paraphrase"):
        ctx.svc.post_message(ctx.user_id, "coach", STATEMENT_COMING, handout_id=h["handout_id"])
    return h


def _open_handout(ctx):
    f = getattr(ctx.svc, "open_handout_id", None)
    return f(ctx.user_id) if f else None


def _handout_owner(handout_id):
    """user_id of a handout, None if it does not exist (or there is no database)."""
    try:
        from .db import session_scope
        from .models import Handout
        with session_scope() as s:
            h = s.get(Handout, handout_id)
            return h.user_id if h else None
    except Exception:
        return None


def _days_missed(ctx, today_iso):
    """Fallback for a service whose status() has no `accountability`: {last_practice_day,
    days_missed, missed_days_list} from the attempts table (see coach.rules.missed_days)."""
    from . import rules
    try:
        from sqlalchemy import select
        from .db import session_scope
        from .models import Attempt
        with session_scope() as s:
            days = list(s.scalars(select(Attempt.day).where(Attempt.user_id == ctx.user_id)))
    except Exception:
        return {"last_practice_day": None, "days_missed": None, "missed_days_list": []}
    today = date.fromisoformat(today_iso) if today_iso else date.today()
    return _jsonable(rules.missed_days(days, today))


# ---- tools ---------------------------------------------------------------------------------
@tool
def coach_status(ctx) -> dict:
    """Progress briefing: backlog, days missed, streak, retry queue, skills. Progress talk:
    never in or right before a problem handoff."""
    st = _jsonable(ctx.svc.status(ctx.user_id))
    if not isinstance(st.get("accountability"), dict):
        st["accountability"] = _days_missed(ctx, st.get("today"))
    return st


@tool
def next_problem(ctx, new: bool = False) -> dict:
    """FALLBACK only: the server's default pick. Normally you choose with get_profile, get_due, find_problems and hand_out. Posts the handoff to the chat panel itself and returns the same fields. Returns the still-open one unless new=true. Say nothing else alongside it."""
    try:
        raw = ctx.svc.next_problem(ctx.user_id, force_new=new)
    except TypeError:                       # a service without force_new
        raw = ctx.svc.next_problem(ctx.user_id)
    if raw is None:
        return {"handoff": None, "reason": "nothing eligible right now"}
    return _post_handoff(ctx, raw)


@tool
def get_profile(ctx) -> dict:
    """His plan (phase, focus, target date, when Hards open), skill ledger, per-pattern record (clean, attempts, owned, last attempt, retired), last 20 attempts, backlog, streak, clean rate. code_speed {seconds_per_line (null under 3 samples), samples, recent} for the code budget. For choosing the next problem. Never quote it to him before a handoff."""
    return _jsonable(ctx.svc.get_profile(ctx.user_id))


@tool
def get_due(ctx) -> dict:
    """What is owed now: retry items due (most overdue first), retired patterns due a re-fire, the 7-day blocklist, today's done and target count."""
    return _jsonable(ctx.svc.get_due(ctx.user_id))


@tool
def find_problems(ctx, pattern: str | None = None, tier: int | None = None,
                  untouched: bool | None = None, difficulty: str | None = None,
                  limit: int = 20) -> dict:
    """Candidate problems in bank order, with his attempts, clean count and whether hand_out would refuse each one now (blocked, block_reason). Filters: pattern slug, tier, untouched (never attempted), level letter E/M/H."""
    try:
        rows = ctx.svc.find_problems(ctx.user_id, pattern=pattern, tier=tier,
                                     untouched=untouched, difficulty=difficulty, limit=limit)
    except ValueError as e:
        return {"error": str(e)}
    return {"candidates": _jsonable(rows)}


@tool
def hand_out(ctx, number: int, target_min: int | None = None, mode: str = "coached",
             why: str = "", phase_budget: dict | None = None, budget_reason: str = "") -> dict:
    """Hand out the problem YOU chose. The server checks the hard rules and refuses with {error} (touched in the last 7 days, retired, not allowed yet by the plan, burned this session, another problem open). why: one plain line, kept private until he logs. mode: timed | coached. phase_budget: {understand, approach} minutes, each >= 1 (a code value here is only provisional: set the code budget at unlock_editor); the target is their sum and must fit the server's range for the problem (refused otherwise). Left out: the server default for his rank (get_profile.default_budget). budget_reason: one private line, shown after he logs. Posts the handoff to the chat panel itself and returns its fields. Say nothing else alongside it."""
    try:
        if phase_budget is None and not budget_reason:
            raw = ctx.svc.hand_out(ctx.user_id, number, target_min=target_min, mode=mode,
                                   why=why)
        else:
            raw = ctx.svc.hand_out(ctx.user_id, number, target_min=target_min, mode=mode,
                                   why=why, phase_budget=phase_budget,
                                   budget_reason=budget_reason)
    except ValueError as e:
        return {"error": str(e)}
    return _post_handoff(ctx, raw)


@tool
def rate_problem(ctx, number: int, rating: int, why: str) -> dict:
    """Rate a problem's hardness ONCE, the first time you hand it out (before or right after hand_out). Bands by its level letter: E 800-1300, M 1200-1900, H 1700-2600; outside is refused, and a second rating is refused (fixed once set). why: one line. His rating moves against it. Never tell him the number before he logs."""
    f = getattr(ctx.svc, "rate_problem", None)
    if f is None:
        return {"error": "not supported"}
    try:
        return _jsonable(f(ctx.user_id, number, rating, why))
    except ValueError as e:
        return {"error": str(e)}


@tool
def get_rating(ctx) -> dict:
    """His rating: mmr, rank, games, next_rank_at, to_next, recent_deltas (last 10). For setting time budgets. Progress talk: never quote it while a problem is open; after reveal one line with the delta is fine."""
    f = getattr(ctx.svc, "get_rating", None)
    if f is None:
        return {"error": "not supported"}
    return _jsonable(f(ctx.user_id))


@tool
def request_feature(ctx, title: str, why: str, example: str = "", severity: str = "nice",
                    tool: str | None = None) -> dict:
    """Tell the dev team what you were missing. title: short. why: what it would let you do. example: the situation in words, at most 400 characters, NO code and no names. severity: blocked | workaround | nice. tool: the tool it is about, if any. Repeats of the same title are counted, not duplicated. Never mention it in his chat."""
    f = getattr(ctx.svc, "request_feature", None)
    if f is None:
        return {"error": "not supported"}
    try:
        return _jsonable(f(ctx.user_id, title, why=why, example=example, severity=severity,
                           tool=tool))
    except ValueError as e:
        return {"error": str(e)}


@tool
def skip_problem(ctx) -> dict:
    """Skip the open problem (after a leak, or when he skips). It still blocks for 7 days. Then hand out another."""
    f = getattr(ctx.svc, "skip_open", None)
    if f is None:
        return {"error": "not supported"}
    try:
        return _jsonable(f(ctx.user_id))
    except ValueError as e:
        return {"error": str(e)}


@tool
def set_statement(ctx, handout_id: int, text: str) -> dict:
    """Write the problem statement, examples and constraints in your own words. Rejected if it names a technique or a difficulty. It shows in his Problem tab only, never in the chat: explain the question in chat yourself."""
    try:
        ctx.svc.set_statement(ctx.user_id, handout_id, text)
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True}


@tool
def show_visual(ctx, kind: str, spec: dict) -> dict:
    """Draw something into his discussion thread, as your message: array | string | grid | list | tree | graph | intervals | compare | steps | io | html | animate. list takes links/order for relinking; compare = {before:{kind,spec}, after:{kind,spec}, mark_changes}. animate spec: {base: array|string|list|tree|grid|graph|intervals|compare|steps, frames: [{spec: <a full spec of base>, caption}], loop: false, ms: 1200}, 2-40 frames, played in place with a player. Spec lines: skill section "Drawing kinds". To explain the question draw input/output only; how-it-is-built animations only after he logs or when debugging. Captions go through the leak guard."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    try:
        r = ctx.svc.show_visual(ctx.user_id, hid, kind, spec)
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True, "id": r["id"]}


@tool
def unlock_editor(ctx, stub: str | None = None, code_min: int | None = None,
                  code_reason: str = "") -> dict:
    """Open the code editor on his page. Only after the idea holds. Optional stub: LeetCode's starter signature only, loaded if his editor is empty. code_min: the code-step budget in minutes (2-40, refused outside), set from the approach he will code; the total target becomes the sum of the steps. Left out: the hand_out budget stays. code_reason: one private line, shown after he logs."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    if code_min is not None or code_reason:
        f = getattr(ctx.svc, "unlock_with_budget", None)
        if f is None:
            return {"error": "not supported"}
        try:
            r = _jsonable(f(ctx.user_id, hid, stub=stub, code_min=code_min,
                            code_reason=code_reason))
        except ValueError as e:
            return {"error": str(e)}
        return {"error": r["error"]} if not r.get("ok") and r.get("error") else r
    if stub:
        r = ctx.svc.unlock_editor(ctx.user_id, hid, stub=stub)
        return {"error": r["error"]} if not r.get("ok") and r.get("error") else r
    return ctx.svc.unlock_editor(ctx.user_id, hid)


@tool
def lock_editor(ctx) -> dict:
    """Lock the code editor again."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    return ctx.svc.lock_editor(ctx.user_id, hid)


@tool
def reset_timer(ctx) -> dict:
    """Restart the clock on the open problem from now."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    try:
        return _jsonable(ctx.svc.reset_timer(ctx.user_id, hid))
    except ValueError as e:
        return {"error": str(e)}


def _clock(ctx, action, reason="", target_min=None, label=None, force=False):
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    kw = {"force": True} if force else {}
    try:
        return _jsonable(ctx.svc.clock(ctx.user_id, hid, action, reason=reason,
                                       target_min=target_min, label=label, **kw))
    except ValueError as e:
        return {"error": str(e)}


@tool
def clock(ctx, action: str, reason: str = "", target_min: int | None = None,
          label: str | None = None, force: bool = False) -> dict:
    """Control his clock: start | pause | resume | reset | target | phase. Say why in chat when you pause or reset. phase label on a problem with steps: understand | approach | code. He ends the understand step himself (his button); force=true only if he said in chat he understood and the button failed. unlock_editor moves approach -> code."""
    return _clock(ctx, action, reason, target_min, label, force)


@tool
def clear_visuals(ctx) -> dict:
    """Clear every drawing of the open problem from his thread (each becomes a muted "Illustration cleared." line)."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    return {"ok": True, "cleared": ctx.svc.clear_visuals(ctx.user_id, hid)}


async def wait(ctx, timeout_s):
    """One wait: a buffered event, else poll the service (chunks of PROGRESS_EVERY_S, a
    progress ping after each empty chunk) until an event or the timeout."""
    buf = pending(ctx.user_id)
    if buf:
        return buf.popleft()
    loop = asyncio.get_running_loop()
    total = max(0.0, float(timeout_s))
    deadline = loop.time() + total
    while True:
        left = deadline - loop.time()
        if left <= 0:
            return {"kind": "timeout"}
        got = await ctx.svc.wait_event(ctx.user_id, min(left, PROGRESS_EVERY_S))
        if isinstance(got, dict):
            got = [got]
        if got:
            for e in got:
                buf.append({"kind": e.get("kind"), "payload": e.get("payload") or {}})
            return buf.popleft()
        if ctx.progress is not None:
            try:
                await ctx.progress(total - max(0.0, deadline - loop.time()), total,
                                   "waiting for the learner")
            except Exception:
                pass


@tool
async def wait_for_event(ctx, timeout_s: int = 1500) -> dict:
    """Block until the learner does something on the website, then return {kind, payload};
    {kind: "timeout"} if nothing came. kind: approach | submission | stuck | skip | chat |
    timer_over | clock_checkpoint | annotation_reply | understood | idle | playground {input,
    output} (he tried an input in the Problem tab) | prediction {id, correct}. Call it at the END of every turn while he is working. Claude Code may move it
    to the background after ~2 min; that is fine, the result still arrives."""
    return _jsonable(await wait(ctx, timeout_s))


@tool
def say(ctx, text: str, clock_action: str | None = None, clock_reason: str = "",
        target_min: int | None = None, label: str | None = None) -> dict:
    """Post one coach message to the chat panel. One thing per message. Optional clock_action
    (as in `clock`) is applied first, so "Clock stopped. <why>." and the pause are one call;
    if the clock change fails nothing is posted."""
    out = {}
    check = getattr(ctx.svc, "check_coach_text", None)
    if check is not None:
        try:
            check(ctx.user_id, text)
        except ValueError as e:
            return {"error": str(e), "posted": False}
    if clock_action:
        c = _clock(ctx, clock_action, clock_reason, target_min, label)
        if "error" in c:
            return {"error": c["error"], "posted": False}
        out["clock"] = c
    try:
        out["message_id"] = ctx.svc.post_message(ctx.user_id, "coach", text)
    except ValueError as e:
        return {"error": str(e), "posted": False, **out}
    return out


def _minutes_since_handout(handout_id):
    from .db import session_scope
    from .models import Handout
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        if h is None:
            raise ValueError(f"no handout {handout_id}")
        return max(1, round((datetime.now() - h.handed_at).total_seconds() / 60))


@tool
def log_attempt(ctx, handout_id: int, bugs: int, hints: int, optimal: bool, derived_cold: bool,
                notes: str = "", abandoned: bool = False, actual_min: int | None = None) -> dict:
    """Record the attempt. actual_min defaults to the minutes on your clock (pauses excluded);
    pass it only if the clock was broken. Returns outcome, retry-queue action, actual_min,
    wall_min, clock_note and score_pending: true (score it next with score_attempt). A handout already logged is not logged again: you get the
    existing attempt with already_logged true."""
    kw = {}
    if actual_min is not None:
        kw["actual_min"] = actual_min
    elif not hasattr(ctx.svc, "clock"):          # an older service: wall time since handout
        kw["actual_min"] = _minutes_since_handout(handout_id)
    try:
        r = ctx.svc.log_attempt(ctx.user_id, handout_id=handout_id, bugs=bugs, optimal=optimal,
                                derived_cold=derived_cold, hints=hints, notes=notes,
                                abandoned=abandoned, **kw)
    except ValueError as e:
        return {"error": str(e)}
    out = _jsonable(r)
    out.setdefault("actual_min", kw.get("actual_min"))
    return out


@tool
def score_attempt(ctx, handout_id: int, parts: dict, summary: str = "") -> dict:
    """Score his solve, ONCE, after log_attempt (and reveal). parts: {understand, approach, first_submit, quality, time: {score 0-1, why}, hints: {deduction 0-0.3, why}}. Weights 0.10 / 0.25 / 0.30 / 0.15 / 0.20; actual = weighted sum - hints deduction, floor 0 (0 if abandoned). Each why: one plain line, at most 200 characters, no code, no names; shown on his reveal page. summary: one line. The server rates it against the problem (expected, K) and moves his MMR. A second call is refused. Returns actual, expected, delta, before, after, rank, rank_up."""
    f = getattr(ctx.svc, "score_attempt", None)
    if f is None:
        return {"error": "not supported"}
    owner = _handout_owner(handout_id)
    if owner is not None and owner != ctx.user_id:
        return {"error": f"no handout {handout_id}"}
    try:
        return _jsonable(f(ctx.user_id, handout_id, parts, summary))
    except ValueError as e:
        return {"error": str(e)}


@tool
def reveal(ctx, handout_id: int) -> dict:
    """What the problem was testing. Refused until the attempt is logged. After a re-fire it
    also carries trap_quiz {pattern, cues: [{cue, trap}]} for the spoken trap quiz.
    score_pending true until you score_attempt; rating then carries your parts and reasons."""
    owner = _handout_owner(handout_id)
    if owner is not None and owner != ctx.user_id:
        return {"error": f"no handout {handout_id}"}
    try:
        return _jsonable(ctx.svc.reveal(handout_id))
    except Exception as e:                   # RevealBlocked and friends
        return {"error": str(e)}


@tool
def burn_pattern(ctx, slug: str, reason: str) -> dict:
    """A pattern was leaked: take it out of play for this session."""
    return {"burned_id": ctx.svc.burn_pattern(ctx.user_id, slug, reason)}


@tool
def lint(ctx, code: str) -> dict:
    """Run the submission checker on Python source. Returns hits [{line, kind, msg}]."""
    from .lint import lint as _lint
    return {"hits": [{"line": ln, "kind": k, "msg": m} for ln, k, m in _lint(code)]}


@tool
def report(ctx, target_date: str = DEFAULT_REPORT_DATE) -> dict:
    """Readiness projection to target_date (YYYY-MM-DD). Progress talk."""
    return _jsonable(ctx.svc.report(ctx.user_id, target_date))


@tool
def log_trap_recall(ctx, pattern_slug: str, trap: str, cue: str, recalled: bool,
                    hit_in_code: bool) -> dict:
    """Record one trap-quiz answer (after reveal of a re-fire). Returns the stored row:
    date, pattern, trap, cue, recalled, hit_in_code."""
    try:
        return _jsonable(ctx.svc.log_trap_recall(ctx.user_id, pattern_slug, trap, cue, recalled,
                                                 hit_in_code))
    except ValueError as e:
        return {"error": str(e)}


@tool
def record_refire(ctx, pattern_slug: str) -> dict:
    """Add today's date to a retired pattern's re-fire dates (once per day)."""
    try:
        return _jsonable(ctx.svc.record_refire(ctx.user_id, pattern_slug))
    except ValueError as e:
        return {"error": str(e)}


@tool
def set_skill(ctx, name: str, level: int, evidence: str, trend: str) -> dict:
    """Update one skill-ledger entry: level 0-5, evidence, trend. Session end only, one call per
    skill the session produced evidence for. Returns was/level."""
    try:
        return _jsonable(ctx.svc.set_skill(ctx.user_id, name, level, evidence, trend))
    except ValueError as e:
        return {"error": str(e)}


@tool
def write_session_log(ctx, markdown: str) -> dict:
    """Store today's session log (markdown, session-log template format). Overwrites."""
    try:
        return _jsonable(ctx.svc.write_session_log(ctx.user_id, markdown))
    except ValueError as e:
        return {"error": str(e)}


@tool
def session_summary(ctx) -> dict:
    """Today's handouts, attempts, reveals, ledger, queue and trap-quiz changes, for writing the
    session log. pending_scores: logged attempts you have not scored yet: score_attempt each. Progress talk: session end only, never while a problem is open."""
    return _jsonable(ctx.svc.session_summary(ctx.user_id))


# ---- editor tools: his code, boxes under lines, comments, scratch tabs --------------------
def _on_open(ctx, fn):
    """Run fn(hid) on the open handout; ValueError -> {"error"}."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    try:
        return _jsonable(fn(hid))
    except ValueError as e:
        return {"error": str(e)}


@tool
def read_code(ctx) -> dict:
    """His editor text as autosaved (code, cursor_line, updated_at) and his last submission {code, verdict, at} or null (verdict is only set for LeetCode-extension submissions; use `judge`)."""
    return _on_open(ctx, lambda hid: ctx.svc.read_code(ctx.user_id, hid))


@tool
def set_stub(ctx, code: str) -> dict:
    """Store starter code (signature only); his editor loads it when empty. Leak-guarded."""
    return _on_open(ctx, lambda hid: ctx.svc.set_stub(ctx.user_id, hid, code))


@tool
def annotate(ctx, line: int, kind: str, text: str) -> dict:
    """Box under line N (1-based) of his code. kind: question | followup | note. Questions get a reply input; a new question closes the old one. Leak-guarded."""
    return _on_open(ctx, lambda hid: {"ok": True, "id": ctx.svc.annotate(
        ctx.user_id, hid, line, kind, text)["id"]})


@tool
def highlight(ctx, lines: list[int], why: str = "") -> dict:
    """Shade lines (1-based) of his code, with why as a small chip. Leak-guarded."""
    return _on_open(ctx, lambda hid: {"ok": True, "id": ctx.svc.highlight(
        ctx.user_id, hid, lines, why)["id"]})


@tool
def clear_annotations(ctx) -> dict:
    """Remove every box and shading from his code."""
    return _on_open(ctx, lambda hid: {"ok": True,
                                      "cleared": ctx.svc.clear_annotations(ctx.user_id, hid)})


@tool
def add_comment(ctx, line: int, text: str) -> dict:
    """Insert "# coach: <text>" above line N of his code (one line, 160 chars max). He can delete it. Leak-guarded."""
    return _on_open(ctx, lambda hid: ctx.svc.add_comment(ctx.user_id, hid, line, text))


@tool
def scratch(ctx, title: str, code: str) -> dict:
    """Open a read-only tab beside his solution with a small demo. Never his solution. Leak-guarded."""
    return _on_open(ctx, lambda hid: ctx.svc.scratch(ctx.user_id, hid, title, code))


# ---- tests panel: set cases, run his current draft (coach/tests_api.py) --------------------
def _current_code(ctx, hid):
    """His current draft (service.read_code), else his last submission's code, else None."""
    f = getattr(ctx.svc, "read_code", None)
    if f is not None:
        try:
            r = f(ctx.user_id, hid) or {}
        except ValueError:
            r = {}
        if (r.get("code") or "").strip():
            return r["code"]
        sub = r.get("last_submission") or {}
        if (sub.get("code") or "").strip():
            return sub["code"]
    try:
        from sqlalchemy import select
        from .db import session_scope
        from .models import Event
        with session_scope() as db:
            for e in db.scalars(select(Event).where(Event.user_id == ctx.user_id,
                                                    Event.kind == "submission")
                                .order_by(Event.id.desc()).limit(200)):
                p = e.payload or {}
                if p.get("handout_id") == hid and (p.get("code") or "").strip():
                    return p["code"]
    except Exception:
        pass
    return None


@tool
def set_tests(ctx, cases: list, mode: str = "function", entry: dict | None = None) -> dict:
    """Put test cases in his Tests panel (replaces yours; his stay). mode function: case {args: {name: value}, expected}, entry {class: "Solution", method}. mode design: case {ops, args, expected}, entry {class}. Optional per case: types {arg|"return": "ListNode"|"TreeNode"}, unordered (true|"deep"), inplace (arg name), label. Labels go through the leak guard."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    try:
        return _jsonable(ctx.svc.set_tests(ctx.user_id, hid, cases, mode, entry or {}))
    except ValueError as e:
        return {"error": str(e)}


@tool
def run_tests(ctx, cases: list | None = None) -> dict:
    """Run his CURRENT draft on the panel's cases, or on `cases` (same format, not stored). For you only: he never sees it. Returns passed, failed and per-case got/expected/error."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    code = _current_code(ctx, hid)
    if code is None:
        return {"error": "no code yet"}
    try:
        r = ctx.svc.run_tests(ctx.user_id, hid, code, "coach", cases=cases)
    except ValueError as e:
        return {"error": str(e)}
    return _jsonable({k: r[k] for k in ("passed", "failed", "ran", "total", "results")})


@tool
def judge(ctx, extra_cases: list | None = None) -> dict:
    """Grade his latest SUBMISSION the way LeetCode would. Runs the submitted code on the panel's cases plus `extra_cases` (yours, hidden: same format as set_tests cases, with `expected`; he never sees them). Case values may use {"$repeat": [value, n]} and {"$range": [a, b]} so a near-max case is a few characters. Returns {verdict: "Accepted"|"Wrong Answer"|"Time Limit Exceeded"|"Runtime Error", first_failure: {input, got, expected, error, size, hidden} | null, passed, total, slowest_ms}. A timeout (3 s for the whole run) is Time Limit Exceeded. The verdict is stored: every non-Accepted one counts as a bug."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    try:
        return _jsonable(ctx.svc.judge(ctx.user_id, hid, extra_cases))
    except ValueError as e:
        return {"error": str(e)}


# ---- running the question: reference, tracer, cards, learner notes (coach/reference.py) -----
def _svc_call(ctx, name, *a, **kw):
    f = getattr(ctx.svc, name, None)
    if f is None:
        return {"error": "not supported"}
    try:
        return _jsonable(f(ctx.user_id, *a, **kw))
    except ValueError as e:
        return {"error": str(e)}


def _on_open_call(ctx, name, *a, **kw):
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    return _svc_call(ctx, name, hid, *a, **kw)


@tool
def set_reference(ctx, code: str, mode: str = "function", entry: dict | None = None,
                  options: dict | None = None, replace: bool = False, reason: str = "") -> dict:
    """Store YOUR private reference solution for the open problem, right after set_statement. Write the PLAIN DEFINITION: slow, simple, by the problem's own words (brute force), never the intended technique. He never sees it. Max 200 lines. mode function: entry {class: "Solution", method}; design: entry {class}. options: case options for inputs {types: {arg|"return": "ListNode"|"TreeNode"}, inplace, unordered} (default: your first test case's). Checked on the cases with expected in his Tests panel: returns verified (true | false | null = nothing to check), mismatches [{case_index, input, expected, got, error}]: fix them. A verified reference is replaced only with replace=true and a reason; a failed or unchecked one may be resent freely. It then fills expected outputs (set_tests, judge), runs the playground, predict, gallery and trace."""
    return _on_open_call(ctx, "set_reference", code, mode=mode, entry=entry or None,
                         options=options, replace=replace, reason=reason)


@tool
def trace(ctx, input: dict, target: str = "reference", max_steps: int = 60,
          watch: list[str] | None = None, allow: bool = False, reason: str = "") -> dict:
    """Run one input line by line and get the state at each step: {trace_id, frames: [{line, func, locals, note}], truncated, result, error}. input: {name: value} (design: {ops, args}). target reference: your reference (to explain what the QUESTION asks). target his: his current draft, only after he pressed I'm stuck, a run failed or a judge failed (else allow=true with a reason). Locals: a linked list is {$type: ListNode, values, ids, cycle_to}, a tree {$type: TreeNode, values, ids}, a node inside a list {$type, val, id}; same id = same node. watch: only these names. max_steps <= 300. Turn it into a drawing with trace_to_animation."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    code = _current_code(ctx, hid) if target == "his" else None
    return _svc_call(ctx, "trace", hid, input, target=target, max_steps=max_steps,
                     watch=watch, allow=allow, reason=reason, code=code)


@tool
def trace_to_animation(ctx, trace_id: int | None = None, frames: list | None = None,
                       base: str = "array", map: dict | None = None, ms: int = 1200,
                       post: bool = False) -> dict:
    """Map a trace onto a drawing: returns {kind: "animate", spec, frames} ready for show_visual("animate", spec), or posts it with post=true (leak guard as usual). base: array | list | tree | grid. map: {values: "<variable drawn>", pointers: {tag: "<variable holding an index or a node>"}, show: ["<variable shown in the caption>"], lines: [line numbers to keep], highlight: "<variable>", cell: ["<row var>", "<col var>"] (grid), keep_all: false (same drawing twice in a row is merged)}. At most 40 frames, evenly sampled."""
    hid = _open_handout(ctx)
    if hid is None:
        return {"error": "no open problem"}
    return _svc_call(ctx, "trace_to_animation", hid, trace_id=trace_id, frames=frames,
                     base=base, map=map or {}, ms=ms, post=post)


@tool
def predict(ctx, input: dict, choices: list | None = None) -> dict:
    """Post a "What does this return?" card in his thread with the input (drawn when it is a list, tree or grid). choices: none (he types the answer) or 2-4 values, exactly one of them the reference output. His answer is checked against your reference; you get a `prediction` event {id, correct, answer, output}. Returns {ok, id, output} (the output is for you)."""
    return _on_open_call(ctx, "predict", input, choices=choices)


@tool
def gallery(ctx, inputs: list, caption: str = "") -> dict:
    """Run your reference on 1-8 inputs and post ONE bubble: input -> output, drawn when structural. Each input {name: value} or {args: {...}, label: "..."}. Labels and caption are leak-guarded. For edge cases when he asks "what about...?". Returns {ok, id, rows: [{label, input, output, error}]}."""
    return _on_open_call(ctx, "gallery", inputs, caption=caption)


@tool
def note_learner(ctx, kind: str, note: str) -> dict:
    """Remember how he learns: kind explanation | motivation | pace | other; note at most 300 characters, plain words, no code, no names. Add one when an explanation clearly landed or clearly failed. Returns the stored note."""
    return _svc_call(ctx, "add_note", kind, note)


@tool
def learner_notes(ctx, retire_id: int | None = None) -> dict:
    """His active learner notes {notes: [{id, kind, note, created_at}]}. Read them before explaining. retire_id: mark that note no longer true (kept for history), then list."""
    if retire_id is not None:
        r = _svc_call(ctx, "retire_note", retire_id)
        if "error" in r:
            return r
    r = _svc_call(ctx, "list_notes")
    return r if isinstance(r, dict) else {"notes": r}


# ---- building an MCP server from the registry ----------------------------------------------
def mcp_function(t, resolve):
    """An MCPServer-registrable function for tool `t`: the tool's own signature minus ctx,
    plus an injected MCP Context. `resolve(mcp_ctx) -> Ctx` decides who is calling."""
    from mcp.server.mcpserver import Context

    async def fn(**kw):
        mctx = kw.pop("mcp_ctx", None)
        ctx = resolve(mctx)
        if ctx.progress is None and mctx is not None:
            async def progress(done, total, message, _m=mctx):
                await _m.report_progress(done, total, message)
            ctx.progress = progress
        return await t.call(ctx, kw)

    ctx_param = inspect.Parameter("mcp_ctx", inspect.Parameter.KEYWORD_ONLY, default=None,
                                  annotation=Context)
    fn.__name__ = t.name
    fn.__doc__ = t.description
    fn.__signature__ = inspect.Signature(t.params + [ctx_param], return_annotation=dict)
    fn.__annotations__ = {**{p.name: p.annotation for p in t.params}, "mcp_ctx": Context,
                          "return": dict}
    return fn


def build_mcp(resolve, name="coach"):
    """An MCPServer exposing every registry tool; `resolve(mcp_ctx) -> Ctx` per call."""
    from mcp.server.mcpserver import MCPServer
    server = MCPServer(name, instructions=INSTRUCTIONS)
    for t in TOOLS.values():
        server.add_tool(mcp_function(t, resolve), name=t.name, description=t.description)
    return server


_SCHEMA_SERVER = None


def _schema_server():
    global _SCHEMA_SERVER
    if _SCHEMA_SERVER is None:
        def nobody(_):
            raise PermissionError("schema-only server")
        _SCHEMA_SERVER = build_mcp(nobody)
    return _SCHEMA_SERVER


def tool_list():
    """[{name, description, input_schema, output_schema}] exactly as the MCP transports list
    them (MCPServer derives the schemas from the registry signatures)."""
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters,
             "output_schema": t.output_schema}
            for t in _schema_server()._tool_manager.list_tools()]


def validate_args(name, args):
    """Validate and coerce `args` for tool `name` with the same pydantic model MCP uses.
    KeyError: unknown tool. pydantic.ValidationError: bad arguments. Returns kwargs."""
    if name not in TOOLS:
        raise KeyError(name)
    if not isinstance(args, dict):
        from pydantic import TypeAdapter
        TypeAdapter(dict).validate_python(args)          # raises ValidationError
    mt = _schema_server()._tool_manager.get_tool(name)
    model = mt.fn_metadata.arg_model.model_validate(args)
    return {p.name: getattr(model, p.name) for p in TOOLS[name].params}
