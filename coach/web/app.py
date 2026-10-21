"""Web UI. RULE ZERO by structure: pages that hand out a problem (/, /solve, /log) never render
progress; progress (/dashboard) is unreachable while a handout is open."""
import os
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

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
    return templates.TemplateResponse(request, name, ctx)


def go(url):
    return RedirectResponse(url, status_code=303)


def frag(text, ok=True):
    return HTMLResponse(f'<span class="{"ok" if ok else "err"}">{text}</span>')


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
                      error="Wrong username or password.")
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


@app.get("/solve", response_class=HTMLResponse)
async def solve(request: Request):
    sess = require(request)
    h = service().current_handout(sess["uid"])
    if not h:
        return go("/")
    msgs = service().messages(sess["uid"], 0)
    last = msgs[-1]["id"] if msgs else 0
    return render(request, "solve.html", sess, h=h, elapsed=elapsed_s(h),
                  verdicts=VERDICTS, msgs=msgs, last=last)


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


@app.get("/messages", response_class=HTMLResponse)
async def messages(request: Request, since: int = 0):
    sess = require(request)
    msgs = service().messages(sess["uid"], since)
    last = msgs[-1]["id"] if msgs else since
    return templates.TemplateResponse(request, "_messages.html",
                                      {"msgs": msgs, "last": last, "poll": True})


# ---- log and reveal ------------------------------------------------------------------------
@app.get("/log", response_class=HTMLResponse)
async def log_form(request: Request, abandoned: int = 0):
    sess = require(request)
    h = service().current_handout(sess["uid"])
    if not h:
        return go("/")
    return render(request, "log.html", sess, h=h, minutes=max(1, round(elapsed_s(h) / 60)),
                  abandoned=bool(abandoned))


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
    return render(request, "reveal.html", sess, r=r, outcome=r.get("outcome"),
                  retry_action=r.get("retry_action"))


# ---- dashboard -----------------------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    sess = require(request)
    if service().current_handout(sess["uid"]):
        resp = go("/solve")
        resp.set_cookie(FLASH, "log_first", max_age=60, httponly=True, samesite="lax")
        return resp
    st = service().status(sess["uid"])
    rep = service().report(sess["uid"], REPORT_DATE)
    return render(request, "dashboard.html", sess, st=st, rep=rep, report_date=REPORT_DATE)


@app.middleware("http")
async def flash_mw(request: Request, call_next):
    key = request.cookies.get(FLASH)
    request.state.flash = FLASHES.get(key) if key else None
    resp = await call_next(request)
    if key and request.method == "GET" and resp.status_code == 200:
        resp.delete_cookie(FLASH)
    return resp
