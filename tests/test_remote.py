"""The coach's tools served by the website: /mcp (remote MCP) and /tools (REST), Bearer coach
tokens, per-user scoping and presence, registry parity with the stdio bridge, the forwarding
bridge, the /connect page, and the one-attempt-per-handout rule."""
import asyncio
import re
from datetime import date

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bridge import server as bridge
from coach import db, service, tokens, tools
from coach.models import Attempt, CoachMessage, Handout
from coach.web import app as webapp
from tests.remote_helpers import Served, mcp_client


@pytest.fixture(scope="module")
def served():
    with Served(webapp.app) as s:
        yield s


@pytest.fixture
def two(world):
    """User A = the world's tester, user B = a second student. One live token each."""
    a = world
    b = service.create_user("other", "pw-other")
    ta = tokens.create(a, "a-laptop")["token"]
    tb = tokens.create(b, "b-laptop")["token"]
    tools.reset_pending()
    return {"a": a, "b": b, "ta": ta, "tb": tb}


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def _text(result):
    import json
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


async def _session(url, tok, fn, mode="auto"):
    c, http = mcp_client(url, tok, mode=mode)
    async with http:
        async with c:
            return await fn(c)


# ---- tokens --------------------------------------------------------------------------------
def test_tokens_hashed_verify_revoke(world):
    t = tokens.create(world, "laptop")
    assert t["token"].startswith("coach_")
    with db.session_scope() as s:
        from coach.models import CoachToken
        row = s.get(CoachToken, t["id"])
        assert row.token_hash != t["token"] and len(row.token_hash) == 64
    assert tokens.verify(t["token"]) == world
    assert tokens.list_for(world)[0]["last_used_at"] is not None
    assert tokens.verify("coach_nope") is None and tokens.verify("") is None
    other = service.create_user("x", "pw")
    with pytest.raises(ValueError):
        tokens.revoke(other, t["id"])                   # not theirs
    assert tokens.revoke(world, t["id"]) is True
    assert tokens.verify(t["token"]) is None
    assert tokens.revoke(world, t["id"]) is False
    assert tokens.bearer("Bearer abc ") == "abc" and tokens.bearer("Basic abc") is None


def test_cli_coach_token(world, capsys, monkeypatch):
    from coach import cli
    monkeypatch.setattr(db, "init", lambda *a, **k: None)
    cli.main(["coach-token", "tester", "--label", "mine"])
    tok = capsys.readouterr().out.strip()
    assert tokens.verify(tok) == world
    tid = tokens.list_for(world)[0]["id"]
    cli.main(["coach-token", "tester", "--revoke", str(tid)])
    assert "revoked" in capsys.readouterr().out
    assert tokens.verify(tok) is None


# ---- auth: 401 without / bad / revoked token -----------------------------------------------
def test_mcp_and_rest_401(two, served):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}}}
    hdr = {"Accept": "application/json, text/event-stream"}
    revoked = tokens.create(two["a"], "old")
    tokens.revoke(two["a"], revoked["id"])
    for auth in ({}, H("coach_wrong"), H(revoked["token"]), {"Authorization": two["ta"]}):
        r = httpx.post(served.url + "/mcp", json=body, headers={**hdr, **auth})
        assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
        assert httpx.get(served.url + "/tools", headers=auth).status_code == 401
        assert httpx.post(served.url + "/tools/say", json={"text": "x"},
                          headers=auth).status_code == 401
    assert service.list_messages(two["a"]) == []         # nothing ran
    r = httpx.post(served.url + "/mcp", json=body, headers={**hdr, **H(two["ta"])})
    assert r.status_code == 200


def test_mcp_needs_no_cookie_or_csrf(two):
    """TestClient with no login cookie: REST works with the token alone (no CSRF)."""
    c = TestClient(webapp.app)
    r = c.post("/tools/say", json={"text": "hi"}, headers=H(two["ta"]))
    assert r.status_code == 200 and "message_id" in r.json()


# ---- a real MCP session over Streamable HTTP -----------------------------------------------
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_mcp_session_end_to_end(two, served, monkeypatch, mode):
    monkeypatch.setattr(tools, "PROGRESS_EVERY_S", 0.3)
    a = two["a"]

    async def go(c):
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == set(tools.TOOLS)
        prof = _text(await c.call_tool("get_profile", {}))
        assert "plan" in prof and "recent_attempts" in prof
        cands = _text(await c.call_tool("find_problems", {"limit": 3}))["candidates"]
        h = _text(await c.call_tool("hand_out", {"number": cands[0]["number"],
                                                 "mode": "timed", "why": "first"}))
        assert set(h) == tools.HANDOFF_KEYS and h["mode"] == "timed"
        said = _text(await c.call_tool("say", {"text": "Ready?"}))
        assert "message_id" in said

        prog = []

        async def on_progress(p, total, msg):
            prog.append((p, total, msg))

        async def push():
            await asyncio.sleep(1.2)
            await anyio.to_thread.run_sync(
                lambda: service.push_event(a, "approach", {"text": "two passes"}))
            await anyio.to_thread.run_sync(
                lambda: service.push_event(a, "chat", {"text": "second"}))

        task = asyncio.create_task(push())
        ev = _text(await c.call_tool("wait_for_event", {"timeout_s": 20},
                                     progress_callback=on_progress))
        await task
        assert ev == {"kind": "approach", "payload": {"text": "two passes"}}
        assert prog and prog[0][1] == 20 and prog[0][2] == "waiting for the learner"
        # buffered: the second drained event comes on the next call, at once
        ev2 = _text(await c.call_tool("wait_for_event", {"timeout_s": 1}))
        assert ev2["kind"] == "chat"
        assert _text(await c.call_tool("wait_for_event", {"timeout_s": 0})) == {"kind": "timeout"}
        return h

    h = asyncio.run(_session(served.url, two["ta"], go, mode))
    msgs = [m["content"] for m in service.list_messages(a)]
    assert msgs[0].startswith(f"{h['number']}. ") and "Ready?" in msgs
    assert service.coach_online(a)["online"] is True


def test_mcp_wait_honours_cancellation(two, served):
    """A cancelled wait stops polling: the event pushed after it is still there for the next."""
    a = two["a"]

    async def go(c):
        with anyio.move_on_after(1.0):
            await c.call_tool("wait_for_event", {"timeout_s": 30})
        await asyncio.sleep(0.5)                  # let the server see the cancellation
        await anyio.to_thread.run_sync(lambda: service.push_event(a, "chat", {"text": "later"}))
        await asyncio.sleep(1.0)                  # a still-running wait would drain it now
        return _text(await c.call_tool("wait_for_event", {"timeout_s": 5}))

    assert asyncio.run(_session(served.url, two["ta"], go)) == {
        "kind": "chat", "payload": {"text": "later"}}


# ---- per-user scoping ----------------------------------------------------------------------
def test_scoping_two_users(two, served):
    a, b = two["a"], two["b"]
    # B has history and an open problem of their own
    nb = service.find_problems(b, limit=2)
    hb = service.hand_out(b, nb[0]["number"], why="b's pick")
    service.log_attempt(b, handout_id=hb.handout_id, bugs=3, hints=0, optimal=True,
                        derived_cold=True)
    hb2 = service.hand_out(b, nb[1]["number"], why="b open")

    async def go(c):
        prof = _text(await c.call_tool("get_profile", {}))
        assert prof["recent_attempts"] == []               # none of B's
        due = _text(await c.call_tool("get_due", {}))
        assert due["open_handout_id"] is None              # B's open one is not A's
        # A's tools only ever touch A: these all name B's handouts
        r = _text(await c.call_tool("reveal", {"handout_id": hb.handout_id}))
        assert "no handout" in r["error"]
        r = _text(await c.call_tool("log_attempt", {"handout_id": hb2.handout_id, "bugs": 0,
                                                     "hints": 0, "optimal": True,
                                                     "derived_cold": True}))
        assert "no handout" in r["error"]
        r = _text(await c.call_tool("set_statement", {"handout_id": hb2.handout_id,
                                                       "text": "Return the count."}))
        assert "no handout" in r["error"]
        # hand_out goes into A's session, never B's
        h = _text(await c.call_tool("hand_out", {"number": nb[1]["number"]}))
        assert "error" in h or set(h) == tools.HANDOFF_KEYS
        await c.call_tool("say", {"text": "Only for A."})
        tl = await c.list_tools()
        for t in tl.tools:                                  # no tool takes a user id
            assert not {"user_id", "uid", "user", "username"} & set(
                (t.input_schema or {}).get("properties", {}))

    asyncio.run(_session(served.url, two["ta"], go))
    with db.session_scope() as s:
        b_handouts = s.scalars(select(Handout.id).where(Handout.user_id == b)).all()
        assert sorted(b_handouts) == sorted([hb.handout_id, hb2.handout_id])
        assert s.get(Handout, hb2.handout_id).status == "open"
        assert s.scalar(select(func.count(Attempt.id)).where(Attempt.user_id == b)) == 1
        b_msgs = s.scalars(select(CoachMessage.content).where(CoachMessage.user_id == b)).all()
    assert "Only for A." not in b_msgs
    assert "Only for A." in [m["content"] for m in service.list_messages(a)]

    # REST: same scoping
    r = httpx.post(served.url + "/tools/reveal", json={"handout_id": hb.handout_id},
                   headers=H(two["ta"])).json()
    assert "no handout" in r["error"]
    r = httpx.post(served.url + "/tools/get_profile", headers=H(two["tb"])).json()
    assert len(r["recent_attempts"]) == 1                   # B sees B's own


def test_presence_per_user(two, served):
    a, b = two["a"], two["b"]
    assert service.coach_online(a)["online"] is False
    assert service.coach_online(b)["online"] is False
    assert httpx.get(served.url + "/tools", headers=H(two["ta"])).status_code == 200
    assert service.coach_online(a)["online"] is True
    assert service.coach_online(b)["online"] is False       # A's coach is not B's
    asyncio.run(service.wait_event(b, timeout_s=0.05, poll_s=0.01))
    assert service.coach_online(b)["online"] is True


def test_web_presence_reads_the_logged_in_user(two):
    from coach.web.svc import Svc
    webapp.app.state.service = Svc()
    try:
        c = TestClient(webapp.app, follow_redirects=False)
        page = c.get("/login").text
        tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"username": "other", "password": "pw-other", "csrf": tok})
        service.coach_seen(two["a"])                         # A's coach is up, B's is not
        assert c.get("/coach/status").json()["online"] is False
        service.coach_seen(two["b"])
        assert c.get("/coach/status").json()["online"] is True
    finally:
        webapp.app.state.service = None


# ---- parity: /mcp, REST and stdio expose the same tools ------------------------------------
def test_registry_parity_mcp_rest_stdio(two, served):
    async def mcp_tools(c):
        return [(t.name, t.description, t.input_schema) for t in (await c.list_tools()).tools]

    remote = asyncio.run(_session(served.url, two["ta"], mcp_tools))
    stdio = [(t.name, t.description, t.input_schema)
             for t in asyncio.run(bridge.mcp.list_tools())]
    rest = [(t["name"], t["description"], t["input_schema"])
            for t in httpx.get(served.url + "/tools", headers=H(two["ta"])).json()["tools"]]
    key = lambda x: x[0]
    assert sorted(remote, key=key) == sorted(stdio, key=key) == sorted(rest, key=key)
    assert {n for n, _, _ in remote} == set(tools.TOOLS) and len(remote) == len(tools.TOOLS)


# ---- REST ----------------------------------------------------------------------------------
def test_rest_errors_and_long_poll(two, served, monkeypatch):
    from coach.web import remote
    u, h = served.url, H(two["ta"])
    assert httpx.post(u + "/tools/nope", json={}, headers=h).status_code == 404
    r = httpx.post(u + "/tools/say", json={}, headers=h)            # text missing
    assert r.status_code == 422 and r.json()["error"] == "bad arguments"
    assert httpx.post(u + "/tools/say", content=b"not json", headers=h).status_code == 422
    assert httpx.post(u + "/tools/find_problems", json={"tier": "x"}, headers=h).status_code == 422
    r = httpx.post(u + "/tools/skip_problem", headers=h)          # tool-level error: 200
    assert r.status_code == 200 and "error" in r.json()
    # long poll: a timeout, then a pushed event
    r = httpx.post(u + "/tools/wait_for_event", json={"timeout_s": 1}, headers=h, timeout=30)
    assert r.json() == {"kind": "timeout"}
    monkeypatch.setattr(remote, "REST_WAIT_MAX_S", 1)               # cap per request
    import time
    t0 = time.time()
    r = httpx.post(u + "/tools/wait_for_event", json={"timeout_s": 600}, headers=h, timeout=30)
    assert r.json() == {"kind": "timeout"} and time.time() - t0 < 10
    monkeypatch.setattr(remote, "REST_WAIT_MAX_S", 55)

    def later():
        import time as _t
        _t.sleep(0.8)
        service.push_event(two["a"], "submission", {"code": "x = 1"})
    import threading
    threading.Thread(target=later).start()
    r = httpx.post(u + "/tools/wait_for_event", json={"timeout_s": 20}, headers=h, timeout=30)
    assert r.json() == {"kind": "submission", "payload": {"code": "x = 1"}}


# ---- forwarding bridge: stdio MCP -> REST --------------------------------------------------
def test_forwarding_bridge_against_asgi_app(two, monkeypatch):
    from mcp.client.client import Client
    monkeypatch.setattr(bridge, "REST_WAIT_MAX_S", 1)          # exercise the poll loop
    a = two["a"]
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app),
                             base_url="http://site", timeout=30)
    srv = bridge.build_forwarding_server("http://site", two["ta"], client=http)

    async def go():
        async with http:
            async with Client(srv) as c:
                tl = (await c.list_tools()).tools
                assert [(t.name, t.input_schema) for t in tl] == [
                    (t["name"], t["input_schema"]) for t in tools.tool_list()]
                prof = _text(await c.call_tool("get_profile", {}))
                assert "plan" in prof
                prog = []

                async def on_progress(p, total, msg):
                    prog.append(p)

                async def push():
                    await asyncio.sleep(2.5)
                    await anyio.to_thread.run_sync(
                        lambda: service.push_event(a, "stuck", {"handout_id": None}))
                task = asyncio.create_task(push())
                ev = _text(await c.call_tool("wait_for_event", {"timeout_s": 15},
                                             progress_callback=on_progress))
                await task
                assert ev == {"kind": "stuck", "payload": {"handout_id": None}}
                assert prog                                   # pings between long polls
                assert _text(await c.call_tool("wait_for_event", {"timeout_s": 1})) == {
                    "kind": "timeout"}
                bad = await c.call_tool("say", {})
                assert bad.is_error and "422" in bad.content[0].text

    asyncio.run(go())
    assert service.coach_online(a)["online"] is True


def test_forwarding_bridge_bad_token(world):
    from mcp.client.client import Client
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app),
                             base_url="http://site")
    srv = bridge.build_forwarding_server("http://site", "coach_bad", client=http)

    async def go():
        async with http:
            async with Client(srv) as c:
                r = await c.call_tool("get_profile", {})
                assert r.is_error and "401" in r.content[0].text
    asyncio.run(go())


def test_bridge_main_requires_token(monkeypatch):
    monkeypatch.delenv("COACH_TOKEN", raising=False)
    monkeypatch.delenv("COACH_URL", raising=False)
    with pytest.raises(SystemExit):
        bridge.main(["--remote", "https://example.test"])


# ---- one attempt per handout ---------------------------------------------------------------
def test_log_attempt_refuses_second_log(world):
    n = service.find_problems(world, limit=1)[0]["number"]
    h = service.hand_out(world, n)
    first = service.log_attempt(world, handout_id=h.handout_id, bugs=3, hints=1,
                                optimal=False, derived_cold=False)
    assert first["retry"] == "added" and "already_logged" not in first
    st_before = service.status(world)["backlog"]
    again = service.log_attempt(world, handout_id=h.handout_id, bugs=0, hints=0,
                                optimal=True, derived_cold=True)
    assert again["already_logged"] is True and again["attempt_id"] == first["attempt_id"]
    assert again["outcome"] == first["outcome"] and again["retry"] == "none"
    with db.session_scope() as s:
        assert s.scalar(select(func.count(Attempt.id)).where(
            Attempt.handout_id == h.handout_id)) == 1
    assert service.status(world)["backlog"] == st_before        # no double count
    # through the tool, as a second coach would call it
    bridge.set_service(service, user_id=world)
    try:
        r = bridge.log_attempt(h.handout_id, bugs=0, hints=0, optimal=True, derived_cold=True)
    finally:
        bridge.set_service(None)
    assert r["already_logged"] is True and r["attempt_id"] == first["attempt_id"]


def test_unique_index_on_attempt_handout(world, tmp_path):
    from sqlalchemy.exc import IntegrityError
    n = service.find_problems(world, limit=1)[0]["number"]
    h = service.hand_out(world, n)
    service.log_attempt(world, handout_id=h.handout_id)
    with pytest.raises(IntegrityError):                       # the DB itself refuses
        with db.session_scope() as s:
            s.add(Attempt(user_id=world, problem_number=n, day=date.today(),
                          handout_id=h.handout_id, target_min=10, actual_min=5,
                          outcome="clean"))
    with db.session_scope() as s:                             # imported rows: no handout, many
        for _ in range(2):
            s.add(Attempt(user_id=world, problem_number=n, day=date.today(), handout_id=None,
                          target_min=10, actual_min=5, outcome="clean", source="import"))


def test_index_migration_skips_when_duplicates_exist(tmp_path):
    """An old database that already holds a duplicate keeps working (enforced in code)."""
    from sqlalchemy import text
    from coach.models import Problem
    path = tmp_path / "old.db"
    db.init(str(path))
    uid = service.create_user("u", "pw")
    with db.session_scope() as s:
        s.execute(text("DROP INDEX uq_attempts_handout"))       # as before the index existed
        s.add(Problem(number=78, title="Subsets", lc_slug="subsets", difficulty="M",
                      source="bank"))
        s.flush()
        h = Handout(user_id=uid, problem_number=78, slot="drill", target_min=20,
                    status="logged")
        s.add(h)
        s.flush()
        hid = h.id
        for _ in range(2):                                      # the live duplicate
            s.add(Attempt(user_id=uid, problem_number=78, day=date.today(), handout_id=hid,
                          target_min=20, actual_min=10, outcome="clean"))
    db.init(str(path))                                         # must not crash
    assert db.add_attempt_handout_index(db.get_engine()) is False
    r = service.log_attempt(uid, handout_id=hid)
    assert r["already_logged"] is True
    with db.session_scope() as s:
        assert s.scalar(select(func.count(Attempt.id)).where(Attempt.handout_id == hid)) == 2


# ---- /connect ------------------------------------------------------------------------------
def test_connect_page_new_token_once_and_revoke(world):
    from coach.web.svc import Svc
    webapp.app.state.service = Svc()
    try:
        c = TestClient(webapp.app, follow_redirects=False)
        assert c.get("/connect").headers["location"] == "/login"
        page = c.get("/login").text
        tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"username": "tester", "password": "pw-for-tests", "csrf": tok})
        r = c.get("/connect")
        assert r.status_code == 200
        assert "claude mcp add --transport http coach http://testserver/mcp" in r.text
        assert "python -m bridge.server --remote http://testserver" in r.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
        assert c.post("/connect/token", data={"label": "Laptop"}).status_code == 403  # no csrf
        r = c.post("/connect/token", data={"label": "Laptop", "csrf": csrf})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        new = re.search(r"coach_[A-Za-z0-9_\-]{20,}", r.text).group(0)
        assert tokens.verify(new) == world
        import html
        assert f'--header "Authorization: Bearer {new}"' in html.unescape(r.text)
        assert f"--token {new}" in r.text
        again = c.get("/connect").text
        assert new not in again and "Laptop" in again           # shown once only
        tid = tokens.list_for(world)[0]["id"]
        assert c.post(f"/connect/revoke/{tid}", data={"csrf": csrf}).status_code == 303
        assert tokens.verify(new) is None and "revoked" in c.get("/connect").text
        service.set_plan(world, target_date="2026-12-15")
        assert 'href="/connect"' in c.get("/dashboard").text
        assert 'href="/connect"' in c.get("/help/connect").text
        n = service.find_problems(world, limit=1)[0]["number"]
        service.hand_out(world, n)
        assert 'href="/connect"' not in c.get("/solve").text
    finally:
        webapp.app.state.service = None
