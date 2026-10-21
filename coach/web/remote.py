"""The coach's tools served BY THE WEBSITE, so a student needs no local Python.

    /mcp           remote MCP, Streamable HTTP (claude mcp add --transport http ...)
    GET  /tools    REST: [{name, description, input_schema, output_schema}]
    POST /tools/X  REST: JSON body = arguments -> JSON result (where MCP is blocked)

Both build from coach/tools.py and authenticate the same way: `Authorization: Bearer <coach
token>` (coach/tokens.py). No login cookie, no CSRF: the token is the credential and the user.
Every authenticated request beats that user's coach heartbeat (service.coach_seen).

REST errors: 401 no/bad/revoked token, 404 unknown tool, 422 bad arguments; a tool-level
{error} is a normal 200 result. `wait_for_event` over REST is a long poll capped at
REST_WAIT_MAX_S per request ({kind: "timeout"} -> call again), so proxies never cut it.
"""
import contextlib
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import service, tokens, tools

REST_WAIT_MAX_S = 55
USER_KEY = "coach.user_id"
router = APIRouter()


def _unauthorized():
    return JSONResponse({"error": "missing, unknown or revoked coach token"}, status_code=401,
                        headers={"WWW-Authenticate": 'Bearer realm="coach"'})


def authenticate(headers):
    """user_id for the request's Bearer token, else None. Beats that user's heartbeat."""
    raw = tokens.bearer(headers.get("authorization"))
    uid = tokens.verify(raw) if raw else None
    if uid is not None:
        service.coach_seen(uid)
    return uid


# ---- REST ----------------------------------------------------------------------------------
async def _user(request):
    return await run_in_threadpool(authenticate, request.headers)


@router.get("/tools")
async def rest_list(request: Request):
    if await _user(request) is None:
        return _unauthorized()
    return JSONResponse({"tools": tools.tool_list()}, headers={"Cache-Control": "no-store"})


@router.post("/tools/{name}")
async def rest_call(name: str, request: Request):
    uid = await _user(request)
    if uid is None:
        return _unauthorized()
    if name not in tools.TOOLS:
        return JSONResponse({"error": f"unknown tool {name!r}"}, status_code=404)
    raw = await request.body()
    try:
        args = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return JSONResponse({"error": "body must be a JSON object of arguments"},
                            status_code=422)
    from pydantic import ValidationError
    try:
        kw = tools.validate_args(name, args)
    except ValidationError as e:
        return JSONResponse({"error": "bad arguments",
                             "detail": json.loads(e.json(include_url=False))}, status_code=422)
    if name == "wait_for_event":
        kw["timeout_s"] = max(0, min(int(kw.get("timeout_s") or 0), REST_WAIT_MAX_S))
    try:
        out = await tools.TOOLS[name].call(tools.Ctx(uid), kw)
    except Exception as e:                       # a crash, not a refusal: still JSON
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


# ---- MCP (Streamable HTTP) -----------------------------------------------------------------
def _resolve(mctx):
    """The caller of an MCP tool call: the user the ASGI layer authenticated for this POST."""
    req = getattr(getattr(mctx, "request_context", None), "request", None) if mctx else None
    uid = req.scope.get(USER_KEY) if req is not None else None
    if uid is None:
        raise PermissionError("unauthenticated")
    return tools.Ctx(uid)


class McpEndpoint:
    """ASGI app at /mcp: Bearer auth, then the SDK's Streamable HTTP session manager.
    The authenticated user rides in the ASGI scope (tools read it back in _resolve) and as
    scope["user"], so the SDK binds each MCP session to the user that opened it."""

    def __init__(self):
        self.manager = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        from starlette.datastructures import Headers
        uid = await run_in_threadpool(authenticate, Headers(scope=scope))
        if uid is None:
            await _unauthorized()(scope, receive, send)
            return
        if self.manager is None:
            await JSONResponse({"error": "coach MCP not started (app lifespan not running)"},
                               status_code=503)(scope, receive, send)
            return
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        from mcp.server.auth.provider import AccessToken
        scope = dict(scope)
        scope[USER_KEY] = uid
        scope["user"] = AuthenticatedUser(AccessToken(
            token=f"user-{uid}", client_id=f"coach-user-{uid}", scopes=[], subject=str(uid)))
        await self.manager.handle_request(scope, receive, send)


endpoint = McpEndpoint()


@contextlib.asynccontextmanager
async def lifespan(app):
    """Run a fresh MCP session manager for the app's lifetime (one per lifespan: the SDK's
    manager can only run once)."""
    from mcp.server.transport_security import TransportSecuritySettings
    server = tools.build_mcp(_resolve)
    # Bearer tokens are not ambient credentials, so DNS-rebinding protection (a localhost
    # Host allow-list) adds nothing and would refuse the real site's hostname.
    server.streamable_http_app(transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False))
    mgr = server.session_manager
    async with mgr.run():
        endpoint.manager = mgr
        try:
            yield
        finally:
            endpoint.manager = None


def mount(app):
    """Add /mcp and /tools to the FastAPI app and chain the MCP lifespan onto its own."""
    from starlette.routing import Route
    app.include_router(router)
    app.router.routes.append(Route("/mcp", endpoint=endpoint))
    app.router.routes.append(Route("/mcp/", endpoint=endpoint))
    prev = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def chained(a):
        async with prev(a) as state:
            async with lifespan(a):
                yield state

    app.router.lifespan_context = chained
