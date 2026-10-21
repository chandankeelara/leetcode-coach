"""Routes for the leetcode.com browser extension. Token auth, no login cookie.
Mount with:  app.include_router(api_ext.router)   (from .api_ext import router)"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import ext

ALLOW_ORIGIN = "https://leetcode.com"
router = APIRouter(prefix="/api/ext")


def _cors(resp, request):
    # Only leetcode.com gets CORS headers; other origins get none, so the browser blocks them.
    if request.headers.get("origin") == ALLOW_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = ALLOW_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "600"
        resp.headers["Vary"] = "Origin"
    return resp


def _json(request, data, status=200):
    return _cors(JSONResponse(data, status_code=status), request)


@router.options("/submission")
@router.options("/ping")
async def preflight(request: Request):
    return _cors(Response(status_code=204), request)


@router.post("/submission")
async def submission(request: Request):
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        return _json(request, {"ok": False, "reason": "bad json"}, 400)
    try:
        res = ext.ingest_submission(
            body.get("token"), body.get("slug"), body.get("code"), body.get("language"),
            body.get("verdict"), {"number": body.get("number"), "runtime": body.get("runtime")})
    except PermissionError:
        return _json(request, {"ok": False, "reason": "bad token"}, 401)
    return _json(request, res)


@router.get("/ping")
async def ping(request: Request, token: str = ""):
    try:
        n = ext.open_problem_number(token)
    except PermissionError:
        return _json(request, {"ok": False, "reason": "bad token"}, 401)
    return _json(request, {"ok": True, "open_problem": n})
