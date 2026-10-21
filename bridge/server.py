"""Local stdio MCP server for the coach tools. Two modes:

    python -m bridge.server                          dev: direct DB (COACH_DB, COACH_USER)
    python -m bridge.server --remote URL --token T   forwarding: tools from URL/tools over HTTPS
                                                     (env COACH_URL / COACH_TOKEN work too)

Students normally need neither: the website serves the same tools at /mcp (see README).
The forwarding mode is for networks that block MCP over HTTP; it lists tools from
`GET /tools`, forwards each call to `POST /tools/<name>`, and runs `wait_for_event` as a loop
of <= 55 s long polls up to its own timeout, with MCP progress pings in between.

Every tool is defined once in coach/tools.py (descriptions, RULE ZERO boundary, event
buffering). This module only wires transports. The module-level functions (next_problem,
say, wait_for_event, ...) call the registry as the dev-mode user: tests use them directly.
"""
import argparse
import asyncio
import json
import os
import sys

from coach import tools as _tools
from coach.tools import (DEFAULT_REPORT_DATE, HANDOFF_KEYS, HANDOFF_MODES,  # noqa: F401
                         PROGRESS_EVERY_S, STATEMENT_COMING, TOOLS, clean_handoff,
                         render_handoff)

# ---- dev mode: direct DB -------------------------------------------------------------------
_svc = None
_uid = None


def set_service(svc, user_id=None):
    """Point the bridge at a service object (the real coach.service or a test fake)."""
    global _svc, _uid
    _svc, _uid = svc, user_id
    _tools.reset_pending()


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
        _uid = _user_by_name(name) if name else s.default_user_id()
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


def _ctx():
    return _tools.Ctx(uid(), svc())


def _resolve_local(_mcp_ctx):
    return _ctx()


mcp = _tools.build_mcp(_resolve_local)


def _module_fn(t):
    def fn(*a, **kw):
        return t.fn(_ctx(), *a, **kw)   # async tools return their coroutine
    fn.__name__ = t.name
    fn.__doc__ = t.description
    return fn


for _name, _t in TOOLS.items():
    globals()[_name] = _module_fn(_t)


def _days_missed(today_iso):
    return _tools._days_missed(_ctx(), today_iso)


# ---- forwarding mode: stdio MCP -> website REST --------------------------------------------
REST_WAIT_MAX_S = 55


class Remote:
    """Talks to the website's /tools. `client` is an httpx.AsyncClient (tests inject one with
    an ASGI transport); by default one is made for `base_url`."""

    def __init__(self, base_url, token, client=None):
        import httpx
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=REST_WAIT_MAX_S + 20))
        self._tools = None

    async def list_tools(self):
        if self._tools is None:
            r = await self.client.get(f"{self.base}/tools", headers=self.headers)
            if r.status_code == 401:
                raise PermissionError("the site refused the coach token (401): make a new one "
                                      "on its /connect page")
            r.raise_for_status()
            self._tools = r.json()["tools"]
        return self._tools

    async def post(self, name, args, timeout=None):
        kw = {"timeout": timeout} if timeout is not None else {}
        r = await self.client.post(f"{self.base}/tools/{name}", headers=self.headers,
                                   json=args, **kw)
        try:
            body = r.json()
        except ValueError:
            body = {"error": r.text[:500]}
        return r.status_code, body

    async def call(self, name, args, progress=None):
        """(status, body). wait_for_event: repeated long polls until an event or its timeout."""
        if name != "wait_for_event":
            return await self.post(name, args)
        loop = asyncio.get_running_loop()
        total = float(args.get("timeout_s", 1500) or 0)
        deadline = loop.time() + total
        while True:
            left = deadline - loop.time()
            chunk = int(max(0, min(left, REST_WAIT_MAX_S)))
            try:
                status, body = await self.post(name, {**args, "timeout_s": chunk},
                                               timeout=chunk + 20)
            except Exception as e:              # network blip: retry until our deadline
                status, body = 0, {"error": f"{type(e).__name__}: {e}"}
                await asyncio.sleep(min(2.0, max(0.0, deadline - loop.time())))
            if status == 200 and body.get("kind") != "timeout":
                return status, body
            if status not in (0, 200):
                return status, body
            if deadline - loop.time() <= 0:
                return 200, {"kind": "timeout"}
            if progress is not None:
                try:
                    await progress(total - max(0.0, deadline - loop.time()), total,
                                   "waiting for the learner")
                except Exception:
                    pass


def build_forwarding_server(base_url, token, client=None):
    """A low-level MCP Server whose tools are the site's GET /tools and whose calls forward to
    POST /tools/<name>. Results are shaped as MCPServer shapes them (JSON text content, plus
    structured content when the tool has an output schema)."""
    import mcp_types as types
    from mcp.server.lowlevel.server import Server

    remote = Remote(base_url, token, client)

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(name=t["name"], description=t["description"],
                       input_schema=t["input_schema"], output_schema=t.get("output_schema"))
            for t in await remote.list_tools()])

    def _err(text):
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                    is_error=True)

    async def on_call_tool(ctx, params):
        name, args = params.name, dict(params.arguments or {})
        try:
            known = {t["name"]: t for t in await remote.list_tools()}
        except Exception as e:
            return _err(f"coach site unreachable: {type(e).__name__}: {e}")
        if name not in known:
            return _err(f"unknown tool {name!r}")

        async def progress(done, total, message):
            await ctx.session.report_progress(done, total, message)

        try:
            status, body = await remote.call(name, args, progress)
        except Exception as e:
            return _err(f"coach site unreachable: {type(e).__name__}: {e}")
        if status == 401:
            return _err("the site refused the coach token (401): make a new one on /connect")
        if status != 200:
            return _err(f"HTTP {status}: {json.dumps(body)}")
        text = json.dumps(body, indent=2, ensure_ascii=False)
        if known[name].get("output_schema"):
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                        structured_content=body)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    server = Server("coach", instructions=_tools.INSTRUCTIONS, on_list_tools=on_list_tools,
                    on_call_tool=on_call_tool)
    server.remote = remote
    return server


async def _run_forwarding(base_url, token):
    from mcp.server.stdio import stdio_server
    server = build_forwarding_server(base_url, token)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bridge.server")
    ap.add_argument("--remote", nargs="?", const=os.environ.get("COACH_URL") or "",
                    default=None, help="forward to this coach site (default env COACH_URL)")
    ap.add_argument("--token", default=os.environ.get("COACH_TOKEN"))
    args = ap.parse_args(argv)
    if args.remote is None and os.environ.get("COACH_URL") and os.environ.get("COACH_TOKEN"):
        args.remote = os.environ["COACH_URL"]
    if args.remote is not None:
        if not args.remote or not args.token:
            sys.exit("--remote needs a site URL and --token (or env COACH_URL / COACH_TOKEN)")
        import anyio
        anyio.run(_run_forwarding, args.remote, args.token)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
