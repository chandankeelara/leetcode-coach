"""Tests panel endpoints (coach/tests_api.py; the runner is coach/runner.py). Included at the
end of app.py. Every POST needs the CSRF header (window.coach.post sends it).

    GET  /tests          -> {handout_id, mode, entry, cases, last_run}
    POST /tests/run      code=<editor text>           -> {run_id, results, passed, failed, ...}
    POST /tests/add      case=<json of one case>      -> the stored case
    POST /tests/delete   id=<case id> (his own only)  -> {ok}
Errors come back as 400 {"error": "..."}."""
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import app as _app

router = APIRouter()


def _f(name):
    """The web service adapter's method, else coach.service's function."""
    f = getattr(_app.service(), name, None)
    if f is None:
        from .. import service as core
        f = getattr(core, name)
    return f


def _no_store(data, status=200):
    r = JSONResponse(data, status_code=status)
    r.headers["Cache-Control"] = "no-store"
    return r


def _open(uid):
    h = _app.service().current_handout(uid)
    return h.handout_id if h else None


EMPTY = {"handout_id": None, "mode": None, "entry": {}, "cases": [], "last_run": None}


@router.get("/tests")
async def tests_get(request: Request):
    sess = _app.require(request)
    uid = sess["uid"]
    hid = await run_in_threadpool(_open, uid)
    if hid is None:
        return _no_store(EMPTY)
    return _no_store(await run_in_threadpool(_f("tests"), uid, hid))


@router.post("/tests/run")
async def tests_run(request: Request):
    sess, form = await _app.authed_form(request)
    uid = sess["uid"]
    hid = await run_in_threadpool(_open, uid)
    if hid is None:
        return _no_store({"error": "no open problem"}, 400)
    code = form.get("code") or ""
    if not code.strip():
        return _no_store({"error": "nothing to run yet"}, 400)
    try:
        out = await run_in_threadpool(_f("run_tests"), uid, hid, code, "user")
    except ValueError as e:
        return _no_store({"error": str(e)}, 400)
    return _no_store(out)


@router.post("/tests/add")
async def tests_add(request: Request):
    sess, form = await _app.authed_form(request)
    uid = sess["uid"]
    hid = await run_in_threadpool(_open, uid)
    if hid is None:
        return _no_store({"error": "no open problem"}, 400)
    try:
        case = json.loads(form.get("case") or "")
    except ValueError:
        return _no_store({"error": "case is not valid JSON"}, 400)
    try:
        out = await run_in_threadpool(_f("add_user_test"), uid, hid, case)
    except ValueError as e:
        return _no_store({"error": str(e)}, 400)
    return _no_store(out)


@router.post("/tests/delete")
async def tests_delete(request: Request):
    sess, form = await _app.authed_form(request)
    uid = sess["uid"]
    hid = await run_in_threadpool(_open, uid)
    try:
        cid = int(form.get("id") or 0)
        out = await run_in_threadpool(_f("delete_user_test"), uid, hid, cid)
    except ValueError as e:
        return _no_store({"error": str(e)}, 400)
    return _no_store(out)
