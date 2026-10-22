"""Running the question, page side (coach/reference.py). Included at the end of app.py. Every
POST needs the CSRF header (window.coach.post sends it). The REFERENCE runs here, never his code.

    GET  /playground            -> {ready, mode, params, example | ops, args}
    POST /playground            one form field per argument (design: ops, args)
                                -> {output, output_text, error, input_html, output_html}
                                   429 {error} more than one run a second, 400 {error} bad input
    POST /predict/{card_id}     answer=<text> or choice=<index> -> {ok, correct, html}
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import app as _app

router = APIRouter()


def _f(name):
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


@router.get("/playground")
async def playground_get(request: Request):
    sess = _app.require(request)
    uid = sess["uid"]
    hid = await run_in_threadpool(_open, uid)
    if hid is None:
        return _no_store({"ready": False})
    try:
        return _no_store(await run_in_threadpool(_f("playground_state"), uid, hid))
    except ValueError:
        return _no_store({"ready": False})


@router.post("/playground")
async def playground_post(request: Request):
    sess, form = await _app.authed_form(request)
    uid = sess["uid"]
    hid = await run_in_threadpool(_open, uid)
    if hid is None:
        return _no_store({"error": "no open problem"}, 400)
    fields = {k: str(v) for k, v in form.items() if k != "csrf"}
    from ..reference import RateLimited
    try:
        out = await run_in_threadpool(_f("playground_run"), uid, hid, fields)
    except RateLimited as e:
        return _no_store({"error": str(e)}, 429)
    except ValueError as e:
        return _no_store({"error": str(e)}, 400)
    return _no_store(out)


@router.post("/predict/{card_id}")
async def predict_post(request: Request, card_id: int):
    sess, form = await _app.authed_form(request)
    try:
        out = await run_in_threadpool(_f("answer_predict"), sess["uid"], card_id,
                                      form.get("answer"), form.get("choice"))
    except ValueError as e:
        return _no_store({"error": str(e)}, 400)
    return _no_store(out)
