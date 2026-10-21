"""The MCP bridge: RULE ZERO at the handoff boundary, the blocking wait, say()."""
import asyncio
import json
from datetime import datetime

import pytest

from bridge import server
from bridge.server import HANDOFF_KEYS


class FakeService:
    """Just enough of coach.service. next_problem leaks extra keys on purpose."""

    def __init__(self):
        self.messages = []
        self.events = []

    def next_problem(self, user_id, force_new=False):
        return {"handout_id": 7, "number": 1, "title": "Two Sum",
                "leetcode_url": "https://leetcode.com/problems/two-sum/",
                "paraphrase": "find two that add up", "started_at": datetime(2026, 10, 21, 9, 5),
                "target_min": 20, "instruction": "Give your approach in plain words first.",
                # leaks the service must never let through:
                "pattern": "arrays-hashmap", "slot": "weakness", "tier": 1, "difficulty": "E"}

    def post_message(self, user_id, role, content, handout_id=None):
        self.messages.append({"role": role, "content": content, "handout_id": handout_id})
        return len(self.messages)

    def push_event(self, user_id, kind, payload=None):
        self.events.append({"kind": kind, "payload": payload or {}})

    async def wait_event(self, user_id, timeout_s):
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not self.events:
            if asyncio.get_running_loop().time() >= deadline:
                return []
            await asyncio.sleep(0.01)
        got, self.events = self.events, []
        return got


@pytest.fixture
def fake():
    f = FakeService()
    server.set_service(f, user_id=1)
    yield f
    server.set_service(None)


@pytest.fixture
def real(tmp_path, monkeypatch):
    """The real coach.service on a throwaway sqlite file."""
    from coach import db, service
    monkeypatch.setenv("COACH_DB", str(tmp_path / "t.db"))
    db.init(str(tmp_path / "t.db"))
    uid = service.create_user("tester", "pw")
    server.set_service(service, user_id=uid)
    yield service, uid
    server.set_service(None)


# ---- RULE ZERO -----------------------------------------------------------------------------
def test_next_problem_keys_are_exactly_the_handoff(fake):
    out = server.next_problem()
    assert set(out) == HANDOFF_KEYS
    for leak in ("pattern", "slot", "tier", "difficulty"):
        assert leak not in out


def test_next_problem_posts_only_handoff_text(fake):
    server.next_problem()
    (msg,) = fake.messages
    assert msg["role"] == "coach" and msg["handout_id"] == 7
    text = msg["content"].lower()
    assert "two sum" in text and "09:05" in text and "20 min" in text
    for leak in ("arrays-hashmap", "hashmap", "weakness", "tier", "slot"):
        assert leak not in text


def test_next_problem_none(fake, monkeypatch):
    monkeypatch.setattr(fake, "next_problem", lambda user_id, force_new=False: None)
    assert server.next_problem()["handoff"] is None
    assert fake.messages == []


def test_clean_handoff_rejects_missing_keys():
    with pytest.raises(ValueError):
        server.clean_handoff({"handout_id": 1})


def test_tool_descriptions_do_not_leak():
    tools = asyncio.run(server.mcp.list_tools())
    blob = json.dumps([t.description for t in tools]).lower()
    blob = blob.replace("a technique or a difficulty", "")     # set_statement's own wording
    for word in ("two-pointer", "sliding", "hashmap", "binary search", "heap", "dfs", "bfs",
                 "tier 1", "difficulty"):
        assert word not in blob


# ---- wait_for_event ------------------------------------------------------------------------
def test_wait_returns_pushed_event_fake(fake):
    async def go():
        async def later():
            await asyncio.sleep(0.05)
            fake.push_event(1, "submission", {"code": "x = 1", "verdict": "accepted"})
        asyncio.create_task(later())
        return await server.wait_for_event(timeout_s=5)
    ev = asyncio.run(go())
    assert ev == {"kind": "submission", "payload": {"code": "x = 1", "verdict": "accepted"}}


def test_wait_timeout(fake):
    assert asyncio.run(server.wait_for_event(timeout_s=0)) == {"kind": "timeout"}
    assert asyncio.run(server.wait_for_event(timeout_s=1)) == {"kind": "timeout"}


def test_wait_keeps_extra_drained_events_for_next_call(fake):
    fake.push_event(1, "approach", {"text": "a"})
    fake.push_event(1, "chat", {"text": "b"})
    first = asyncio.run(server.wait_for_event(timeout_s=1))
    second = asyncio.run(server.wait_for_event(timeout_s=1))
    assert [first["kind"], second["kind"]] == ["approach", "chat"]


def test_wait_returns_pushed_event_real_db(real):
    service, uid = real
    service.push_event(uid, "chat", {"text": "ready"})
    assert asyncio.run(server.wait_for_event(timeout_s=5)) == {
        "kind": "chat", "payload": {"text": "ready"}}
    assert asyncio.run(server.wait_for_event(timeout_s=1)) == {"kind": "timeout"}


# ---- say / lint ----------------------------------------------------------------------------
def test_say_stores_coach_message_fake(fake):
    r = server.say("Ready?")
    assert r["message_id"] == 1
    assert fake.messages[0]["role"] == "coach" and fake.messages[0]["content"] == "Ready?"


def test_say_stores_coach_message_real_db(real):
    service, uid = real
    server.say("Ready?")
    msgs = service.list_messages(uid)
    assert [(m["role"], m["content"]) for m in msgs] == [("coach", "Ready?")]


def test_lint_flags_noop():
    hits = server.lint("def f(g):\n    g[0] == 1\n    return g\n")["hits"]
    assert hits and hits[0]["line"] == 2


# ---- set_statement -------------------------------------------------------------------------
def test_set_statement_tool_ok_and_leak_real_db(real):
    service, uid = real
    from coach import db
    from coach.models import Handout, Pattern, Problem
    with db.session_scope() as s:
        s.add(Pattern(slug="two-pointers", name="Two Pointers", tier=1))
        s.add(Problem(number=5, title="Pair Sum", lc_slug="pair-sum", difficulty="E",
                      pattern_slug="two-pointers", source="bank"))
        s.flush()
        h = Handout(user_id=uid, problem_number=5, slot="drill", target_min=10)
        s.add(h)
        s.flush()
        hid = h.id
    r = server.set_statement(hid, "Use two pointers on the sorted array.")
    assert r["error"].startswith("leak:") and "two pointers" in r["error"]
    assert service.get_statement(hid) is None
    assert server.set_statement(hid, "Return indices of two numbers adding to target.") == {"ok": True}
    assert service.get_statement(hid).startswith("Return indices")


def test_set_statement_tool_described_without_leaks():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert "set_statement" in tools
    assert "Rejected if it names a technique or a difficulty" in tools["set_statement"].description
