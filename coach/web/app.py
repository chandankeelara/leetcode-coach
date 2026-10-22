"""Web UI. RULE ZERO by structure: pages that hand out a problem (/, /solve, /log) never render
progress; progress (/dashboard) is unreachable while a handout is open."""
import asyncio
import html
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool

HERE = Path(__file__).parent
COOKIE = "coach_session"
FLASH = "coach_flash"
FLASHES = {"log_first": "Log the current problem first."}
MAX_AGE = 30 * 24 * 3600
REPORT_DATE = "2026-12-15"


def load_secret():
    if os.environ.get("COACH_SECRET"):
        return os.environ["COACH_SECRET"]
    env_file = Path(os.environ.get("COACH_ENV_FILE", ".env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("COACH_SECRET="):
                os.environ["COACH_SECRET"] = line.split("=", 1)[1].strip()
                return os.environ["COACH_SECRET"]
    sec = secrets.token_hex(32)
    with env_file.open("a", encoding="utf-8") as f:
        f.write(f"COACH_SECRET={sec}\n")
    os.environ["COACH_SECRET"] = sec
    return sec


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))

# Token-authenticated ingest for the leetcode.com browser extension (no login cookie).
from .. import viz  # noqa: E402
from . import api_ext  # noqa: E402
app.include_router(api_ext.router)

# The coach's tools for each student's Claude Code: /mcp (remote MCP) and /tools (REST).
# Bearer coach token, no cookie, no CSRF (see remote.py).
from . import remote  # noqa: E402
remote.mount(app)

_FENCE = re.compile(r"```[a-zA-Z0-9]*\n?(.*?)```", re.S)
_TICK = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")


def msgfmt(text):
    """Escape, then a tiny markdown subset: fenced code, inline code, bold."""
    parts, last = [], 0
    for m in _FENCE.finditer(text or ""):
        parts.append(("t", text[last:m.start()]))
        parts.append(("c", m.group(1)))
        last = m.end()
    parts.append(("t", (text or "")[last:]))
    out = []
    for kind, chunk in parts:
        if kind == "c":
            out.append(f"<pre><code>{html.escape(chunk.rstrip())}</code></pre>")
            continue
        t = html.escape(chunk)
        t = _TICK.sub(r"<code>\1</code>", t)
        t = _BOLD.sub(r"<strong>\1</strong>", t)
        out.append(t)
    return Markup("".join(out))


_HEAD = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")


def _inline(line):
    t = html.escape(line)
    t = _TICK.sub(r"<code>\1</code>", t)
    return _BOLD.sub(r"<strong>\1</strong>", t)


def mdblock(text):
    """Escape first, then light markdown: headings, paragraphs, bullets, `code`, **bold**,
    fenced code blocks. Never emits anything but the tags below."""
    out, para, items, code = [], [], [], None
    isviz = False

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            items.clear()

    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if code is not None:
            if line.strip().startswith("```"):
                if isviz:
                    out.append(viz.block_html("\n".join(code)))
                else:
                    out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush()
            code = []
            isviz = line.strip()[3:].strip().lower() == "viz"
            continue
        m = _HEAD.match(line)
        b = _BULLET.match(line)
        if m:
            flush()
            out.append(f"<h4>{_inline(m.group(2))}</h4>")
        elif b:
            if para:
                flush()
            items.append(b.group(1))
        elif not line.strip():
            flush()
        else:
            if items:
                flush()
            para.append(line.strip())
    if code is not None:
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    flush()
    return Markup("".join(out))


def hhmm(iso):
    try:
        return datetime.fromisoformat(str(iso)).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def pname(slug):
    if not slug:
        return ""
    from ..catalog import pattern_name
    return pattern_name(str(slug))


templates.env.filters["msgfmt"] = msgfmt
templates.env.filters["mdblock"] = mdblock
templates.env.filters["pname"] = pname
templates.env.filters["hhmm"] = hhmm


def ser():
    return URLSafeTimedSerializer(load_secret(), salt="coach-session")


def login_ser():
    return URLSafeTimedSerializer(load_secret(), salt="coach-login")


def service():
    if getattr(app.state, "service", None) is None:
        from .svc import Svc
        app.state.service = Svc()
    return app.state.service


class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
async def _need_login(request, exc):
    return RedirectResponse("/login", status_code=303)


def require(request):
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise NeedLogin()
    try:
        return ser().loads(raw, max_age=MAX_AGE)
    except BadSignature:
        raise NeedLogin()


def check_csrf(request, sess, form):
    tok = request.headers.get("x-csrf-token") or form.get("csrf") or ""
    if not secrets.compare_digest(str(tok), sess["csrf"]):
        raise HTTPException(403, "bad csrf token")


async def authed_form(request):
    sess = require(request)
    form = await request.form()
    check_csrf(request, sess, form)
    return sess, form


def render(request, name, sess=None, **ctx):
    ctx.setdefault("csrf", sess["csrf"] if sess else "")
    ctx.setdefault("authed", sess is not None)
    ctx.setdefault("nav", "neutral")
    if name in ("solve.html", "interstitial.html", "waiting.html"):
        ctx.setdefault("coach_online", bool(
            service().coach_online(sess["uid"] if sess else None)["online"]))
    return templates.TemplateResponse(request, name, ctx)


def go(url):
    return RedirectResponse(url, status_code=303)


def frag(text, ok=True):
    return HTMLResponse(f'<span class="{"ok" if ok else "err"}">{text}</span>')


def this_handout(m, h):
    """RULE ZERO: the solve page chat starts at this handout, so talk about the previous
    problem (its pattern, its reveal) never shows next to a new one."""
    if m.get("handout_id") is not None:
        return m["handout_id"] == h.handout_id
    try:
        return datetime.fromisoformat(str(m["created_at"])) >= h.started_at - timedelta(seconds=2)
    except (KeyError, TypeError, ValueError):
        return True


def elapsed_s(h):
    """Wall seconds since the handoff's start: only the fallback for a service with no clock."""
    return max(0, int((datetime.now() - h.started_at).total_seconds()))


def clock_of(uid, h):
    """The coach's clock for a handoff (service.clock_state), or a running-since-start stand-in
    when the service has none (test fakes)."""
    f = getattr(service(), "clock_state", None)
    c = f(uid, h.handout_id) if f else None
    if c is None:
        e = elapsed_s(h)
        c = {"state": "running", "elapsed_s": e, "wall_s": e, "target_min": h.target_min,
             "phase": None, "anchor_iso": h.started_at.isoformat(), "accum_s": 0,
             "pause_reason": None, "pauses": 0, "resets": 0, "handout_id": h.handout_id}
    return c


# ---- auth ----------------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return render(request, "login.html", csrf=login_ser().dumps("login"), error=None)


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""),
                csrf: str = Form("")):
    try:
        login_ser().loads(csrf, max_age=3600)
    except BadSignature:
        return go("/login")
    uid = service().verify_user(username, password)
    if uid is None:
        return render(request, "login.html", csrf=login_ser().dumps("login"),
                      error="Wrong username or password.", username=username)
    resp = go("/")
    resp.set_cookie(COOKIE, ser().dumps({"uid": uid, "csrf": secrets.token_hex(16)}),
                    max_age=MAX_AGE, httponly=True, samesite="lax")
    return resp


@app.post("/logout")
async def logout(request: Request):
    await authed_form(request)
    resp = go("/login")
    resp.delete_cookie(COOKIE)
    return resp


# ---- interstitial and solve ----------------------------------------------------------------
def needs_plan(uid):
    """A brand-new user has no plan yet: onboarding first. Services without plans never do."""
    f = getattr(service(), "get_plan", None)
    return f is not None and f(uid) is None


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    sess = require(request)
    if service().current_handout(sess["uid"]):
        return go("/solve")
    if needs_plan(sess["uid"]):
        return go("/onboarding")
    return render(request, "interstitial.html", sess)


def _editor_open(uid, h):
    """Has the coach opened the code editor for this handout?"""
    f = getattr(service(), "editor_open", None)
    return bool(h and f and f(uid, h.handout_id))


@app.get("/solve", response_class=HTMLResponse)
async def solve(request: Request):
    sess = require(request)
    h = service().current_handout(sess["uid"])
    if not h:
        return go("/")
    msgs = service().messages(sess["uid"], 0)
    last = msgs[-1]["id"] if msgs else 0
    msgs = [m for m in msgs if this_handout(m, h)]
    c = clock_of(sess["uid"], h)
    return render(request, "solve.html", sess, h=h, elapsed=c["elapsed_s"], clock=c,
                  started=h.started_at.strftime("%H:%M"), msgs=msgs,
                  last=last, editor_open=_editor_open(sess["uid"], h))


@app.post("/solve")
async def solve_start(request: Request):
    """Next problem. The coach picks, not the page: queue `next_requested` for the coach and
    show the waiting page, which follows the coach's hand_out to /solve. Coach offline: the
    waiting page shows the connect steps and the default pick instead."""
    sess, _ = await authed_form(request)
    uid = sess["uid"]
    if service().current_handout(uid):
        return go("/solve")
    if needs_plan(uid):
        return go("/onboarding")
    if service().coach_online(uid)["online"]:
        service().request_next(uid)
    return go("/next")


@app.get("/next", response_class=HTMLResponse)
async def next_wait(request: Request, none: int = 0):
    sess = require(request)
    if service().current_handout(sess["uid"]):
        return go("/solve")
    online = bool(service().coach_online(sess["uid"])["online"]) and not none
    return render(request, "waiting.html", sess, coach_online=online, none_left=bool(none))


@app.post("/solve/default")
async def solve_default(request: Request):
    """The server's default pick (the old slot picker), for a student whose coach is offline."""
    sess, _ = await authed_form(request)
    if not service().current_handout(sess["uid"]):
        h = service().next_problem(sess["uid"])
        if h is None:
            return go("/next?none=1")
        # The coach only hears the queue: tell it a problem was handed out from the page, so it
        # writes the statement and sets the tests instead of waiting for him.
        service().push_event(sess["uid"], "handout", {
            "handout_id": h.handout_id, "number": h.number, "title": h.title,
            "paraphrase": h.paraphrase, "mode": getattr(h, "mode", None), "source": "web"})
    return go("/solve")


@app.post("/solve/approach", response_class=HTMLResponse)
async def approach(request: Request):
    sess, form = await authed_form(request)
    h = service().current_handout(sess["uid"])
    text = (form.get("approach") or "").strip()
    if not h or not text:
        return frag("Nothing sent.", False)
    service().push_event(sess["uid"], "approach", {"handout_id": h.handout_id, "text": text})
    service().post_message(sess["uid"], "user", text)
    return frag("Sent.")


@app.post("/solve/understood")
async def understood(request: Request):
    """Step 1 ends: he understood the question (coach/steps.py). The server records the
    understand time, starts the approach step and tells the coach (`understood` event)."""
    sess, _ = await authed_form(request)
    h = service().current_handout(sess["uid"])
    f = getattr(service(), "understood", None)
    if not h or f is None:
        return JSONResponse({"ok": False, "error": "No open problem."}, status_code=400)
    try:
        r = await run_in_threadpool(f, sess["uid"], h.handout_id)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    r = dict(r)
    r["clock"] = clock_of(sess["uid"], h)
    return JSONResponse(r)


@app.post("/solve/submit", response_class=HTMLResponse)
async def submit(request: Request):
    sess, form = await authed_form(request)
    h = service().current_handout(sess["uid"])
    code = form.get("code") or ""
    if not h or not code.strip():
        return frag("Nothing to submit.", False)
    found = [{"line": l, "kind": k, "msg": m} for l, k, m in service().lint(code)]
    service().push_event(sess["uid"], "submission", {
        "handout_id": h.handout_id, "code": code, "lint": found, "judged_by": "coach"})
    return templates.TemplateResponse(request, "_lint.html", {"found": found})


@app.post("/solve/chat", response_class=HTMLResponse)
async def chat(request: Request):
    sess, form = await authed_form(request)
    h = service().current_handout(sess["uid"])
    text = (form.get("text") or "").strip()
    if text:
        service().post_message(sess["uid"], "user", text)
        service().push_event(sess["uid"], "chat", {
            "handout_id": h.handout_id if h else None, "text": text})
    return HTMLResponse("")


@app.post("/solve/stuck", response_class=HTMLResponse)
async def stuck(request: Request):
    sess, _ = await authed_form(request)
    h = service().current_handout(sess["uid"])
    service().push_event(sess["uid"], "stuck", {"handout_id": h.handout_id if h else None})
    return frag("Coach told.")


@app.post("/solve/abandon")
async def abandon(request: Request):
    sess, _ = await authed_form(request)
    h = service().current_handout(sess["uid"])
    service().push_event(sess["uid"], "abandon", {"handout_id": h.handout_id if h else None})
    return go("/log?abandoned=1")


@app.get("/statement")
async def statement(request: Request):
    """The coach-written statement of the open problem, rendered; polled until it exists."""
    sess = require(request)
    h = service().current_handout(sess["uid"])
    text = getattr(h, "paraphrase", None) if h else None
    return JSONResponse({"handout_id": h.handout_id if h else None,
                         "html": str(mdblock(text)) if text else ""})


@app.get("/visuals")
async def visuals(request: Request, since: int = 0):
    """Coach-drawn illustrations of the open problem with id > since:
    [{id, html, caption, kind}]. Header X-Visual-Count = how many the handout has in all
    (fewer than shown means the coach cleared them)."""
    sess = require(request)
    svc = service()
    h = svc.current_handout(sess["uid"])
    f = getattr(svc, "visuals", None)
    rows = f(sess["uid"], h.handout_id, since) if (h and f) else []
    out = [{"id": v["id"], "html": v["html"], "caption": v.get("caption") or "",
            "kind": v["kind"]} for v in rows]
    resp = JSONResponse(out)
    count = getattr(svc, "count_visuals", None)
    resp.headers["X-Visual-Count"] = str(count(sess["uid"], h.handout_id) if (h and count)
                                         else len(out))
    resp.headers["X-Open-Handout"] = str(h.handout_id) if h else ""
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/messages", response_class=HTMLResponse)
async def messages(request: Request, since: int = 0):
    sess = require(request)
    msgs = service().messages(sess["uid"], since)
    last = msgs[-1]["id"] if msgs else since
    h = service().current_handout(sess["uid"])
    resp = templates.TemplateResponse(request, "_messages.html", {"msgs": msgs, "last": last})
    resp.headers["X-Last-Id"] = str(last)
    resp.headers["X-Open-Handout"] = str(h.handout_id) if h else ""
    resp.headers["X-Coach-Online"] = "1" if service().coach_online(sess["uid"])["online"] else "0"
    resp.headers["X-Editor-Open"] = "1" if _editor_open(sess["uid"], h) else "0"
    return resp


def handout_state(uid):
    """{open_handout_id, editor_open, started_at, target_min, elapsed_s, clock} of the open
    problem (all None when none is open). elapsed_s and target_min are the coach's clock;
    clock = service.clock_state (state running|paused, elapsed_s, wall_s, target_min, phase,
    anchor_iso, accum_s, pause_reason, pauses, resets). The page renders from it."""
    h = service().current_handout(uid)
    if not h:
        return {"open_handout_id": None, "editor_open": False, "started_at": None,
                "target_min": None, "elapsed_s": None, "clock": None}
    c = clock_of(uid, h)
    return {"open_handout_id": h.handout_id, "editor_open": _editor_open(uid, h),
            "started_at": h.started_at.isoformat(), "target_min": c["target_min"],
            "elapsed_s": c["elapsed_s"], "clock": c}


@app.get("/handout/state")
async def handout_state_route(request: Request):
    sess = require(request)
    resp = JSONResponse(await run_in_threadpool(handout_state, sess["uid"]))
    resp.headers["Cache-Control"] = "no-store"
    return resp


STREAM_POLL_S = 0.25        # how often /stream reads ui_events
STREAM_PING_S = 15.0        # keep-alive `ping` event (live.js reconnects after 40 s of silence)
STREAM_PRESENCE_S = 2.0     # how often /stream re-checks coach_online (catches going offline)
STREAM_MAX_S = 300.0        # recycle each stream; EventSource reconnects with Last-Event-ID
STREAM_RETRY_MS = 1000      # EventSource reconnect delay after a recycle


def _sse(kind, data, eid=None):
    head = f"id: {eid}\n" if eid is not None else ""
    return f"{head}event: {kind}\ndata: {json.dumps(data)}\n\n"


def _last_message_id(uid):
    f = getattr(service(), "last_message_id", None)
    if f:
        return f(uid)
    msgs = service().messages(uid, 0)
    return msgs[-1]["id"] if msgs else 0


def _hello(uid):
    st = handout_state(uid)
    return {"open_handout_id": st["open_handout_id"], "editor_open": st["editor_open"],
            "started_at": st["started_at"], "online": bool(service().coach_online(uid)["online"]),
            "last_message_id": _last_message_id(uid)}


@app.get("/stream")
async def stream(request: Request, since: int | None = None, max_events: int = 0,
                 max_s: float = 0):
    """Server-sent events: one per ui_events row for this user after `since` (or the
    Last-Event-ID header; default: from now). Starts with `hello` (state to resync from).
    `presence` {"online"} is sent when coach_online() flips. `max_events` / `max_s` end the
    stream early (tests only)."""
    sess = require(request)
    uid = sess["uid"]
    svc = service()
    if since is None:
        try:
            since = int(request.headers.get("last-event-id") or "")
        except ValueError:
            since = None
    if since is None:
        since = await run_in_threadpool(svc.ui_last_id)

    async def gen():
        loop = asyncio.get_running_loop()
        t_start = loop.time()
        cursor, sent = since, 0
        hello = await run_in_threadpool(_hello, uid)
        online = hello["online"]
        # retry: reconnect 1 s after each recycle (the browser's default is ~3 s)
        yield f"retry: {STREAM_RETRY_MS}\n" + _sse("hello", hello, cursor)
        sent += 1
        last_ping = last_presence = loop.time()
        while not (max_events and sent >= max_events):
            if loop.time() - t_start >= (max_s or STREAM_MAX_S):
                break
            if await request.is_disconnected():
                break
            rows = await run_in_threadpool(svc.ui_events_since, uid, cursor)
            for r in rows:
                cursor = r["id"]
                if r["kind"] == "presence":
                    now_on = bool((await run_in_threadpool(svc.coach_online, uid))["online"])
                    last_presence = loop.time()
                    if now_on == online:
                        continue
                    online = now_on
                    yield _sse("presence", {"ref_id": None, "online": online}, cursor)
                else:
                    yield _sse(r["kind"], {"ref_id": r["ref_id"]}, cursor)
                sent += 1
                if max_events and sent >= max_events:
                    return
            now = loop.time()
            if now - last_presence >= STREAM_PRESENCE_S:
                last_presence = now
                now_on = bool((await run_in_threadpool(svc.coach_online, uid))["online"])
                if now_on != online:
                    online = now_on
                    yield _sse("presence", {"ref_id": None, "online": online}, cursor)
                    sent += 1
                    continue
            if now - last_ping >= STREAM_PING_S:
                last_ping = now
                # a named event (no id), not a comment: the page's watchdog only sees events
                yield "event: ping\ndata: {}\n\n"
            await asyncio.sleep(STREAM_POLL_S)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/coach/status")
async def coach_status(request: Request):
    sess = require(request)
    c = service().coach_online(sess["uid"])
    return JSONResponse({"online": bool(c["online"]), "last_seen": c.get("last_seen")})


@app.get("/help/connect", response_class=HTMLResponse)
async def help_connect(request: Request):
    sess = require(request)
    return render(request, "help_connect.html", sess, site=site_url(request))


# ---- connect: coach tokens for the student's Claude Code ------------------------------------
def site_url(request):
    """This site's public base URL: env COACH_PUBLIC_URL, else what the request came in on."""
    env = (os.environ.get("COACH_PUBLIC_URL") or "").strip().rstrip("/")
    return env or str(request.base_url).rstrip("/")


def connect_commands(site, token):
    """The exact commands a student runs, token filled in (or a placeholder)."""
    tok = token or "<your token>"
    return {
        "mcp": f'claude mcp add --transport http coach {site}/mcp '
               f'--header "Authorization: Bearer {tok}"',
        "forward": f"claude mcp add --transport stdio coach -- "
                   f"python -m bridge.server --remote {site} --token {tok}",
    }


def _connect_page(request, sess, new=None, error=None):
    from .. import tokens
    site = site_url(request)
    return render(request, "connect.html", sess, nav="full", site=site, new=new, error=error,
                  tokens=tokens.list_for(sess["uid"]),
                  cmds=connect_commands(site, new["token"] if new else None))


@app.get("/connect", response_class=HTMLResponse)
async def connect(request: Request):
    sess = require(request)
    return await run_in_threadpool(_connect_page, request, sess)


@app.post("/connect/token", response_class=HTMLResponse)
async def connect_new_token(request: Request):
    """New token: shown on this one response only (it is stored hashed)."""
    from .. import tokens
    sess, form = await authed_form(request)
    label = (form.get("label") or "").strip() or "Claude Code"
    new = await run_in_threadpool(tokens.create, sess["uid"], label)
    resp = await run_in_threadpool(_connect_page, request, sess, new)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/connect/revoke/{token_id}")
async def connect_revoke(request: Request, token_id: int):
    from .. import tokens
    sess, _ = await authed_form(request)
    try:
        await run_in_threadpool(tokens.revoke, sess["uid"], token_id)
    except ValueError:
        raise HTTPException(404, "no such token")
    return go("/connect")


# ---- log and reveal ------------------------------------------------------------------------
@app.get("/log", response_class=HTMLResponse)
async def log_form(request: Request, abandoned: int = 0):
    sess = require(request)
    h = service().current_handout(sess["uid"])
    if not h:
        return go("/")
    minutes = max(1, round(clock_of(sess["uid"], h)["elapsed_s"] / 60))
    return render(request, "log.html", sess, h=h, minutes=minutes,
                  abandoned=bool(abandoned), started=h.started_at.strftime("%H:%M"))


@app.post("/log")
async def log_post(request: Request):
    sess, form = await authed_form(request)
    h = service().current_handout(sess["uid"])
    if not h:
        return go("/")

    def num(k):
        try:
            return max(0, int(form.get(k) or 0))
        except ValueError:
            return 0

    service().log_attempt(
        sess["uid"], h.handout_id, actual_min=num("actual_min"), bugs=num("bugs"),
        optimal=form.get("optimal") == "yes", derived_cold=form.get("derived_cold") == "yes",
        hints=num("hints"), notes=(form.get("notes") or "").strip(),
        abandoned=bool(form.get("abandoned")))
    return go(f"/reveal/{h.handout_id}")


@app.get("/reveal/{handout_id}", response_class=HTMLResponse)
async def reveal(request: Request, handout_id: int):
    sess = require(request)
    try:
        r = service().reveal(handout_id)
    except Exception:
        return go("/log" if service().current_handout(sess["uid"]) else "/")
    return render(request, "reveal.html", sess, nav="full", r=r, outcome=r.get("outcome"),
                  retry_action=r.get("retry_action"), a=r.get("attempt") or {},
                  retry=r.get("retry_state") or {})


# ---- dashboard -----------------------------------------------------------------------------
def blocked_while_open(sess):
    if service().current_handout(sess["uid"]):
        resp = go("/solve")
        resp.set_cookie(FLASH, "log_first", max_age=60, httponly=True, samesite="lax")
        return resp
    return None


def history_rows(uid):
    f = getattr(service(), "history", None)
    if f is None:
        return None
    try:
        return f(uid)
    except Exception:
        return None


def heat14(rows, last14):
    today = date.today()
    days = [today - timedelta(days=13 - i) for i in range(14)]
    if rows is not None:
        counts = {}
        for r in rows:
            counts[r["day"]] = counts.get(r["day"], 0) + 1
        vals = [counts.get(d.isoformat(), 0) for d in days]
    else:
        marks = list(last14) if isinstance(last14, (list, str)) else []
        vals = [1 if (m == "#" or m is True or m == 1) else 0 for m in marks[-14:]]
        vals = [0] * (14 - len(vals)) + vals
    return [{"day": d.isoformat(), "dow": "MTWTFSS"[d.weekday()], "dom": d.day, "n": n,
             "today": d == today} for d, n in zip(days, vals)]


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    sess = require(request)
    blocked = blocked_while_open(sess)
    if blocked:
        return blocked
    if needs_plan(sess["uid"]):
        return go("/onboarding")
    st = service().status(sess["uid"])
    rep = service().report(sess["uid"], REPORT_DATE)
    rows = history_rows(sess["uid"])
    heat = heat14(rows, (st.get("streak") or {}).get("last14", ""))
    rt = rating_of(sess["uid"])
    return render(request, "dashboard.html", sess, nav="full", active="dashboard", st=st,
                  rep=rep, report_date=REPORT_DATE, heat=heat,
                  heat_max=max([c["n"] for c in heat] + [1]), rt=rt, spark=sparkline(rt),
                  admin=is_admin(sess["uid"]))


def rating_of(uid):
    """The rating block with the last 20 deltas (service.rating_history), or None. Progress
    data: dashboard and reveal only, never /, /solve, /log."""
    f = getattr(service(), "rating_history", None)
    if f is None:
        return None
    try:
        return f(uid)
    except Exception:
        return None


def sparkline(rt, w=220, h=44):
    """Bars for the last 20 rating deltas: [{x, y, h, up, title}], baseline mid-height."""
    ds = (rt or {}).get("recent_deltas") or []
    if not ds:
        return None
    top = max([abs(d["delta"]) for d in ds] + [1.0])
    n, gap = len(ds), 2
    bw = max(2.0, (w - gap * (n - 1)) / max(n, 1))
    mid = h / 2
    bars = []
    for i, d in enumerate(ds):
        bh = max(1.5, abs(d["delta"]) / top * (mid - 2))
        up = d["delta"] >= 0
        bars.append({"x": round(i * (bw + gap), 1), "y": round(mid - bh if up else mid, 1),
                     "h": round(bh, 1), "w": round(bw, 1), "up": up,
                     "title": f'{d["day"]} #{d["number"]}: {d["delta"]:+.1f}'})
    return {"w": w, "h": h, "mid": mid, "bars": bars}


def is_admin(uid):
    f = getattr(service(), "is_admin", None)
    try:
        return bool(f and f(uid))
    except Exception:
        return False


# ---- feedback to the dev team (admins only) -------------------------------------------------
FEEDBACK_SEV = {"blocked": "badge-danger", "workaround": "badge-warn", "nice": ""}


@app.get("/feedback", response_class=HTMLResponse)
async def feedback(request: Request, kind: str = "", tool: str = "", since: str = "",
                   status: str = ""):
    sess = require(request)
    if not is_admin(sess["uid"]):
        raise HTTPException(404)
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            since_dt = None
    data = service().list_requests(kind=kind or None, tool=tool or None, since=since_dt,
                                   status=status or None)
    return render(request, "feedback.html", sess, nav="full", active="feedback",
                  rows=data["rows"], tools=data["tools"], f={"kind": kind, "tool": tool,
                                                            "since": since, "status": status},
                  sev=FEEDBACK_SEV, admin=True)


@app.post("/feedback/{request_id}/status")
async def feedback_status(request: Request, request_id: int):
    sess, form = await authed_form(request)
    if not is_admin(sess["uid"]):
        raise HTTPException(404)
    try:
        service().set_request_status(request_id, form.get("status") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    back = form.get("back") or "/feedback"
    return go(back if back.startswith("/feedback") else "/feedback")


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, pattern: str = "", outcome: str = ""):
    sess = require(request)
    blocked = blocked_while_open(sess)
    if blocked:
        return blocked
    rows = history_rows(sess["uid"]) or []
    patterns = sorted({(r["pattern"], r["pattern_name"] or r["pattern"]) for r in rows
                       if r.get("pattern")}, key=lambda p: p[1])
    shown = [r for r in rows if (not pattern or r.get("pattern") == pattern)
             and (not outcome or r.get("outcome") == outcome)]
    totals = {k: sum(1 for r in shown if r.get("outcome") == k)
              for k in ("clean", "solved", "abandoned")}
    return render(request, "history.html", sess, nav="full", active="history", rows=shown,
                  total=len(rows), patterns=patterns, pattern=pattern, outcome=outcome,
                  totals=totals)


# ---- plan and onboarding (progress-side pages: blocked while a problem is open) -------------
def _pattern_groups():
    pts = service().pattern_choices()
    labels = {1: "Core", 2: "Next", 3: "More"}
    return [{"label": labels[t], "patterns": [p for p in pts if p["tier"] == t]}
            for t in (1, 2, 3) if any(p["tier"] == t for p in pts)]


def _hards_mode(plan):
    if not plan or not plan.get("hards_from"):
        return "never"
    return "now" if plan["hards_from"] <= (plan.get("created_at") or "")[:10] else "date"


def _plan_fields(form, full):
    """Form -> set_plan fields. ValueError with a plain message on bad input."""
    out = {"target_date": form.get("target_date") or None,
           "daily_problems": form.get("daily_problems") or 2,
           "minutes_per_day": form.get("minutes_per_day") or 90,
           "focus_patterns": form.getlist("focus")}
    mode = form.get("hards") or "never"
    if mode == "now":
        out["hards_from"] = date.today()
    elif mode == "date":
        if not form.get("hards_from"):
            raise ValueError("Pick the date Hards start, or choose another option.")
        out["hards_from"] = form.get("hards_from")
    else:
        out["hards_from"] = None
    if not out["target_date"]:
        raise ValueError("Pick a target date.")
    if full:
        out["title"] = form.get("title") or ""
        out["notes"] = form.get("notes") or ""
        phases = []
        for n, line in enumerate((form.get("phases") or "").splitlines(), 1):
            if not line.strip():
                continue
            bits = [b.strip() for b in line.split("|", 3)]
            if len(bits) < 3:
                raise ValueError(f"Phase line {n}: name | start | end | rules.")
            bits += [""] * (4 - len(bits))
            phases.append({"name": bits[0], "start": bits[1] or None, "end": bits[2] or None,
                           "rules": bits[3]})
        out["phases"] = phases
    return out


def _plan_page(request, sess, onboarding, plan=None, error=None, saved=False):
    return render(request, "plan.html", sess, nav="neutral" if onboarding else "full",
                  onboarding=onboarding, plan=plan, error=error, saved=saved,
                  pattern_groups=_pattern_groups(), hards_mode=_hards_mode(plan))


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request):
    sess = require(request)
    blocked = blocked_while_open(sess)
    if blocked:
        return blocked
    if not needs_plan(sess["uid"]):
        return go("/plan")
    return _plan_page(request, sess, True)


@app.post("/onboarding")
async def onboarding_post(request: Request):
    sess, form = await authed_form(request)
    blocked = blocked_while_open(sess)
    if blocked:
        return blocked
    try:
        service().set_plan(sess["uid"], **_plan_fields(form, full=False))
    except ValueError as e:
        return _plan_page(request, sess, True, plan=_echo(form), error=str(e))
    return go("/")


def _echo(form):
    return {"target_date": form.get("target_date"), "daily_problems": form.get("daily_problems"),
            "minutes_per_day": form.get("minutes_per_day"),
            "focus_patterns": form.getlist("focus"), "hards_from": form.get("hards_from"),
            "title": form.get("title"), "notes": form.get("notes")}


@app.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request, saved: int = 0):
    sess = require(request)
    blocked = blocked_while_open(sess)
    if blocked:
        return blocked
    plan = service().get_plan(sess["uid"])
    if plan is None:
        return go("/onboarding")
    return _plan_page(request, sess, False, plan=plan, saved=bool(saved))


@app.post("/plan")
async def plan_post(request: Request):
    sess, form = await authed_form(request)
    blocked = blocked_while_open(sess)
    if blocked:
        return blocked
    try:
        service().set_plan(sess["uid"], **_plan_fields(form, full=True))
    except ValueError as e:
        plan = dict(service().get_plan(sess["uid"]) or {}, **{k: v for k, v in _echo(form).items()
                                                            if v is not None})
        return _plan_page(request, sess, False, plan=plan, error=str(e))
    return go("/plan?saved=1")


@app.middleware("http")
async def flash_mw(request: Request, call_next):
    key = request.cookies.get(FLASH)
    request.state.flash = FLASHES.get(key) if key else None
    resp = await call_next(request)
    if key and request.method == "GET" and resp.status_code == 200:
        resp.delete_cookie(FLASH)
    return resp


# ---- editor tools: draft sync, annotations, coach comments, scratch (editor_routes.py) -------
from . import editor_routes  # noqa: E402
app.include_router(editor_routes.router)


# ---- tests panel: cases, his own cases, Run (tests_routes.py) --------------------------------
from . import tests_routes  # noqa: E402
app.include_router(tests_routes.router)


# ---- running the question: playground, predict cards (reference_routes.py) -------------------
from . import reference_routes  # noqa: E402
app.include_router(reference_routes.router)
