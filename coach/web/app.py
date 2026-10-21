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
VERDICTS = ["accepted", "wrong answer", "TLE", "runtime error"]


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
from . import api_ext  # noqa: E402
app.include_router(api_ext.router)

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
                out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush()
            code = []
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
    if name in ("solve.html", "interstitial.html"):
        ctx.setdefault("coach_online", bool(service().coach_online()["online"]))
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
    return max(0, int((datetime.now() - h.started_at).total_seconds()))


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
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    sess = require(request)
    if service().current_handout(sess["uid"]):
        return go("/solve")
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
    return render(request, "solve.html", sess, h=h, elapsed=elapsed_s(h),
                  started=h.started_at.strftime("%H:%M"), verdicts=VERDICTS, msgs=msgs,
                  last=last, editor_open=_editor_open(sess["uid"], h))


@app.post("/solve")
async def solve_start(request: Request):
    sess, _ = await authed_form(request)
    if not service().current_handout(sess["uid"]):
        service().next_problem(sess["uid"])
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


@app.post("/solve/submit", response_class=HTMLResponse)
async def submit(request: Request):
    sess, form = await authed_form(request)
    h = service().current_handout(sess["uid"])
    code = form.get("code") or ""
    verdict = form.get("verdict") or ""
    if not h or not code.strip() or verdict not in VERDICTS:
        return frag("Need code and a result.", False)
    found = [{"line": l, "kind": k, "msg": m} for l, k, m in service().lint(code)]
    service().push_event(sess["uid"], "submission", {
        "handout_id": h.handout_id, "code": code, "lint": found, "verdict": verdict})
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
    resp.headers["X-Coach-Online"] = "1" if service().coach_online()["online"] else "0"
    resp.headers["X-Editor-Open"] = "1" if _editor_open(sess["uid"], h) else "0"
    return resp


def handout_state(uid):
    """{open_handout_id, editor_open, started_at, target_min, elapsed_s} of the open problem
    (all None when none is open). The page compares it against what it shows."""
    h = service().current_handout(uid)
    if not h:
        return {"open_handout_id": None, "editor_open": False, "started_at": None,
                "target_min": None, "elapsed_s": None}
    return {"open_handout_id": h.handout_id, "editor_open": _editor_open(uid, h),
            "started_at": h.started_at.isoformat(), "target_min": h.target_min,
            "elapsed_s": elapsed_s(h)}


@app.get("/handout/state")
async def handout_state_route(request: Request):
    sess = require(request)
    resp = JSONResponse(await run_in_threadpool(handout_state, sess["uid"]))
    resp.headers["Cache-Control"] = "no-store"
    return resp


STREAM_POLL_S = 0.25        # how often /stream reads ui_events
STREAM_PING_S = 15.0        # SSE comment keep-alive
STREAM_PRESENCE_S = 2.0     # how often /stream re-checks coach_online (catches going offline)
STREAM_MAX_S = 300.0        # recycle each stream; EventSource reconnects with Last-Event-ID


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
            "started_at": st["started_at"], "online": bool(service().coach_online()["online"]),
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
        yield _sse("hello", hello, cursor)
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
                    now_on = bool((await run_in_threadpool(svc.coach_online))["online"])
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
                now_on = bool((await run_in_threadpool(svc.coach_online))["online"])
                if now_on != online:
                    online = now_on
                    yield _sse("presence", {"ref_id": None, "online": online}, cursor)
                    sent += 1
                    continue
            if now - last_ping >= STREAM_PING_S:
                last_ping = now
                yield ": ping\n\n"
            await asyncio.sleep(STREAM_POLL_S)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/coach/status")
async def coach_status(request: Request):
    require(request)
    c = service().coach_online()
    return JSONResponse({"online": bool(c["online"]), "last_seen": c.get("last_seen")})


@app.get("/help/connect", response_class=HTMLResponse)
async def help_connect(request: Request):
    sess = require(request)
    return render(request, "help_connect.html", sess)


# ---- log and reveal ------------------------------------------------------------------------
@app.get("/log", response_class=HTMLResponse)
async def log_form(request: Request, abandoned: int = 0):
    sess = require(request)
    h = service().current_handout(sess["uid"])
    if not h:
        return go("/")
    return render(request, "log.html", sess, h=h, minutes=max(1, round(elapsed_s(h) / 60)),
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
    st = service().status(sess["uid"])
    rep = service().report(sess["uid"], REPORT_DATE)
    rows = history_rows(sess["uid"])
    heat = heat14(rows, (st.get("streak") or {}).get("last14", ""))
    return render(request, "dashboard.html", sess, nav="full", active="dashboard", st=st,
                  rep=rep, report_date=REPORT_DATE, heat=heat,
                  heat_max=max([c["n"] for c in heat] + [1]))


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


@app.middleware("http")
async def flash_mw(request: Request, call_next):
    key = request.cookies.get(FLASH)
    request.state.flash = FLASHES.get(key) if key else None
    resp = await call_next(request)
    if key and request.method == "GET" and resp.status_code == 200:
        resp.delete_cookie(FLASH)
    return resp
