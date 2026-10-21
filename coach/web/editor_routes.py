"""Solve-page endpoints for the coach's editor tools (coach/editor.py): draft autosave, the
editor state (stub, draft, annotations, pending comment inserts, scratch tabs), op applied,
annotation resolve / reply. Included at the end of app.py. Every POST needs the CSRF header.
Draft saves never emit a ui_event or a coach event."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import app as _app

router = APIRouter()


def _f(name):
    return getattr(_app.service(), name, None)


def _no_store(data, status=200):
    r = JSONResponse(data, status_code=status)
    r.headers["Cache-Control"] = "no-store"
    return r


def _open(uid):
    h = _app.service().current_handout(uid)
    return h.handout_id if h else None


@router.get("/code/state")
async def code_state(request: Request):
    sess = _app.require(request)
    uid = sess["uid"]
    hid = await run_in_threadpool(_open, uid)
    f = _f("editor_state")
    if hid is None or f is None:
        return _no_store({"handout_id": hid, "stub": None, "draft": None, "annotations": [],
                          "ops": [], "scratch": []})
    return _no_store(await run_in_threadpool(f, uid, hid))


@router.post("/code/draft")
async def code_draft(request: Request):
    sess, form = await _app.authed_form(request)
    uid = sess["uid"]
    hid = await run_in_threadpool(_open, uid)
    f = _f("save_draft")
    if hid is None or f is None:
        return _no_store({"ok": False, "error": "no open problem"}, 409)
    if str(form.get("handout_id") or hid) != str(hid):
        return _no_store({"ok": False, "error": "a different problem is open"}, 409)
    try:
        out = await run_in_threadpool(f, uid, hid, form.get("code") or "",
                                      form.get("cursor_line"))
    except ValueError as e:
        return _no_store({"ok": False, "error": str(e)}, 400)
    return _no_store(out)


@router.post("/code/ops/{op_id}/applied")
async def code_op_applied(request: Request, op_id: int):
    sess, _ = await _app.authed_form(request)
    f = _f("mark_op_applied")
    if f is None:
        return _no_store({"ok": False, "first": False}, 404)
    return _no_store(await run_in_threadpool(f, sess["uid"], op_id))


@router.post("/code/annotations/{ann_id}/resolve")
async def annotation_resolve(request: Request, ann_id: int):
    sess, _ = await _app.authed_form(request)
    f = _f("resolve_annotation")
    if f is None:
        return _no_store({"ok": False}, 404)
    return _no_store(await run_in_threadpool(f, ann_id, sess["uid"]))


@router.post("/code/annotations/{ann_id}/reply")
async def annotation_reply(request: Request, ann_id: int):
    sess, form = await _app.authed_form(request)
    f = _f("reply_annotation")
    if f is None:
        return _no_store({"ok": False}, 404)
    try:
        out = await run_in_threadpool(f, sess["uid"], ann_id, form.get("text") or "")
    except ValueError as e:
        return _no_store({"ok": False, "error": str(e)}, 400)
    return _no_store(out)
