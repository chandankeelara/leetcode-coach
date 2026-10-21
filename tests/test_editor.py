"""Coach editor tools: draft sync, stub, annotations, coach comments, scratch (coach/editor.py)."""
import re

import pytest
from fastapi.testclient import TestClient

from bridge import server
from coach import db, service
from coach.models import Handout, Pattern, Problem, UiEvent
from coach.web import app as webapp
from tests.test_web import assert_clean

STUB = "class Solution:\n    def pairSum(self, nums: list[int], k: int) -> int:\n        pass\n"


@pytest.fixture
def ed(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_DB", str(tmp_path / "t.db"))
    db.init(str(tmp_path / "t.db"))
    uid = service.create_user("tester", "pw")
    with db.session_scope() as s:
        s.add(Pattern(slug="two-pointers", name="Two Pointers", tier=1))
        s.add(Pattern(slug="monotonic-stack", name="Monotonic Stack", tier=1))
        s.add(Problem(number=5, title="Pair Sum", lc_slug="pair-sum", difficulty="E",
                      pattern_slug="two-pointers", source="bank"))
        s.flush()
        h = Handout(user_id=uid, problem_number=5, slot="drill", target_min=10)
        s.add(h)
        s.flush()
        hid = h.id
    server.set_service(service, user_id=uid)
    yield uid, hid
    server.set_service(None)


def ui_kinds():
    with db.session_scope() as s:
        return [e.kind for e in s.query(UiEvent).order_by(UiEvent.id)]


# ---- draft + read_code -----------------------------------------------------------------------
def test_draft_upsert_and_read_code(ed):
    uid, hid = ed
    assert service.read_code(uid, hid) == {"code": "", "cursor_line": None, "updated_at": None,
                                           "last_submission": None}
    before = len(ui_kinds())
    service.save_draft(uid, hid, "x = 1\n", 1)
    service.save_draft(uid, hid, "x = 1\ny = 2\n", 2)
    r = service.read_code(uid, hid)
    assert r["code"] == "x = 1\ny = 2\n" and r["cursor_line"] == 2 and r["updated_at"]
    assert len(ui_kinds()) == before                     # draft saves emit nothing
    with db.session_scope() as s:
        from coach.models import CodeDraft, Event
        assert s.query(CodeDraft).count() == 1
        assert s.query(Event).count() == 0
    service.push_event(uid, "submission", {"handout_id": hid, "code": "old", "verdict": "TLE"})
    service.push_event(uid, "submission", {"handout_id": hid + 99, "code": "other",
                                           "verdict": "accepted"})
    service.push_event(uid, "submission", {"handout_id": hid, "code": "new",
                                           "verdict": "wrong answer"})
    last = service.read_code(uid, hid)["last_submission"]
    assert last["code"] == "new" and last["verdict"] == "wrong answer" and last["at"]
    with pytest.raises(ValueError):
        service.read_code(uid + 1, hid)


# ---- stub ------------------------------------------------------------------------------------
def test_stub_stored_and_leak_guarded(ed):
    uid, hid = ed
    assert service.set_stub(uid, hid, STUB) == {"ok": True}
    assert service.get_stub(uid, hid) == STUB
    assert ui_kinds()[-1] == "editor"
    with pytest.raises(ValueError, match="leak"):
        service.set_stub(uid, hid, STUB + "        # use a monotonic stack\n")
    with pytest.raises(ValueError, match="leak"):        # identifiers count (camelCase split)
        service.set_stub(uid, hid, "class Solution:\n    def slidingWindowMax(self): pass\n")
    assert service.get_stub(uid, hid) == STUB


def test_unlock_editor_with_stub_sets_both(ed):
    uid, hid = ed
    assert service.unlock_editor(uid, hid, stub=STUB) == {"ok": True}
    assert service.editor_open(uid, hid) and service.get_stub(uid, hid) == STUB
    service.lock_editor(uid, hid)
    r = service.unlock_editor(uid, hid, stub="# two pointers\n" + STUB)
    assert r["ok"] is False and r["error"].startswith("leak")
    assert service.editor_open(uid, hid) is False


# ---- annotations -----------------------------------------------------------------------------
def test_annotate_highlight_resolve_clear(ed):
    uid, hid = ed
    service.save_draft(uid, hid, "a\nb\nc\nd = top\ne\n", 1)
    q = service.annotate(uid, hid, 4, "question", "What is `top` when nothing was pushed?")
    assert q["line"] == 4 and q["anchor"] == "d = top" and ui_kinds()[-1] == "annotations"
    n = service.annotate(uid, hid, 2, "note", "Input is never empty.")
    q2 = service.annotate(uid, hid, 5, "followup", "And after one push?")
    open_ids = [a["id"] for a in service.annotations(uid, hid)]
    assert q["id"] not in open_ids and {n["id"], q2["id"]} <= set(open_ids)   # one open question
    h = service.highlight(uid, hid, [3, 1, 3], "runs on [2, 7]")
    assert h["lines"] == [1, 3] and h["kind"] == "highlight"
    with pytest.raises(ValueError, match="leak"):
        service.annotate(uid, hid, 1, "question", "Why not a heap here?")
    with pytest.raises(ValueError):
        service.annotate(uid, hid, 0, "note", "x")
    with pytest.raises(ValueError):
        service.annotate(uid, hid, 1, "bogus", "x")
    k0 = len(ui_kinds())
    assert service.resolve_annotation(n["id"])["ok"]
    assert len(ui_kinds()) == k0 + 1 and ui_kinds()[-1] == "annotations"
    assert n["id"] not in [a["id"] for a in service.annotations(uid, hid)]
    assert service.clear_annotations(uid, hid) == 2
    assert service.annotations(uid, hid) == []
    assert ui_kinds()[-1] == "annotations"


def test_own_code_words_are_exempt(ed):
    uid, hid = ed
    with pytest.raises(ValueError, match="leak"):
        service.annotate(uid, hid, 1, "question", "What is in `stack` now?")
    service.save_draft(uid, hid, "stack = []\n", 1)       # he named it himself
    assert service.annotate(uid, hid, 1, "question", "What is in `stack` now?")["id"]


def test_reply_pushes_event_and_chat_line(ed):
    uid, hid = ed
    q = service.annotate(uid, hid, 4, "question", "What is `top` here?")
    r = service.reply_annotation(uid, q["id"], "None, it crashes")
    assert r["ok"]
    ev = service._drain(uid)
    assert ev[-1]["kind"] == "annotation_reply"
    assert ev[-1]["payload"] == {"id": q["id"], "handout_id": hid, "line": 4,
                                 "question": "What is `top` here?", "reply": "None, it crashes"}
    m = service.list_messages(uid)[-1]
    assert m["role"] == "user" and m["content"] == "Re line 4: None, it crashes"
    assert service.annotations(uid, hid)[0]["reply"] == "None, it crashes"
    note = service.annotate(uid, hid, 1, "note", "fyi")
    with pytest.raises(ValueError):
        service.reply_annotation(uid, note["id"], "x")


# ---- coach comments --------------------------------------------------------------------------
def test_add_comment_pending_then_applied(ed):
    uid, hid = ed
    r = service.add_comment(uid, hid, 3, "build the answer here")
    assert ui_kinds()[-1] == "code_ops"
    assert service.pending_ops(uid, hid) == [{"id": r["id"], "op": "insert_comment", "line": 3,
                                              "text": "build the answer here"}]
    with pytest.raises(ValueError):
        service.add_comment(uid, hid, 1, "x" * 161)
    with pytest.raises(ValueError):
        service.add_comment(uid, hid, 1, "one\ntwo")
    with pytest.raises(ValueError, match="leak"):
        service.add_comment(uid, hid, 1, "sliding window goes here")
    assert service.mark_op_applied(uid, r["id"]) == {"ok": True, "first": True}
    assert service.mark_op_applied(uid, r["id"]) == {"ok": True, "first": False}
    assert service.pending_ops(uid, hid) == []


# ---- scratch ---------------------------------------------------------------------------------
def test_scratch_stored_and_emitted(ed):
    uid, hid = ed
    r = service.scratch(uid, hid, "aliasing.py", "rows = [[0] * 2] * 2\nrows[0][0] = 1\nprint(rows)")
    assert ui_kinds()[-1] == "scratch"
    sc = service.scratches(uid, hid)
    assert sc[0]["id"] == r["id"] and sc[0]["title"] == "aliasing.py"
    with pytest.raises(ValueError, match="leak"):
        service.scratch(uid, hid, "demo", "# dynamic programming table\nx = 1")


# ---- bridge round trips ----------------------------------------------------------------------
def test_bridge_round_trips(ed):
    uid, hid = ed
    assert server.read_code()["code"] == ""
    service.save_draft(uid, hid, "x = 1\n", 1)
    assert server.read_code()["code"] == "x = 1\n"
    assert server.set_stub(STUB) == {"ok": True}
    assert server.set_stub("# heap\n" + STUB)["error"].startswith("leak")
    a = server.annotate(4, "question", "What is `x` here?")
    assert a["ok"] and a["id"]
    assert server.annotate(1, "question", "binary search?")["error"].startswith("leak")
    assert server.highlight([1, 2], "this runs")["ok"]
    assert server.clear_annotations() == {"ok": True, "cleared": 2}
    assert server.add_comment(2, "section: read input")["ok"]
    assert server.scratch("demo.py", "print(-7 // 2)")["ok"]
    service.lock_editor(uid, hid)
    assert server.unlock_editor(stub=STUB) == {"ok": True}
    assert server.unlock_editor(stub="# greedy\n" + STUB)["error"].startswith("leak")
    assert server.unlock_editor() == {"ok": True}


def test_bridge_tools_need_an_open_problem(ed):
    uid, hid = ed
    with db.session_scope() as s:
        s.get(Handout, hid).status = "logged"
    for r in (server.read_code(), server.annotate(1, "note", "x"), server.scratch("a", "b")):
        assert r == {"error": "no open problem"}


# ---- web: the page endpoints and RULE ZERO with boxes present --------------------------------
@pytest.fixture
def web(ed, monkeypatch):
    from datetime import datetime
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    uid, hid = ed
    with db.session_scope() as s:
        s.get(Handout, hid).handed_at = datetime.now()
    service.set_password("tester", "pw")
    webapp.app.state.service = None
    c = TestClient(webapp.app, follow_redirects=False)
    page = c.get("/login").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert c.post("/login", data={"username": "tester", "password": "pw",
                                  "csrf": tok}).status_code == 303
    t = re.search(r'X-CSRF-Token": "([^"]+)"', c.get("/solve").text).group(1)
    yield c, {"X-CSRF-Token": t}, uid, hid
    webapp.app.state.service = None


def test_web_draft_ops_reply_and_rule_zero(web):
    c, h, uid, hid = web
    assert c.post("/code/draft", data={"code": "a\nb\n", "cursor_line": 2}).status_code == 403
    assert c.post("/code/draft", data={"code": "a\nb\n", "cursor_line": 2},
                  headers=h).json()["ok"]
    assert service.read_code(uid, hid)["code"] == "a\nb\n"
    service.set_stub(uid, hid, STUB)
    q = service.annotate(uid, hid, 2, "question", "What is `b` after one pass?")
    service.highlight(uid, hid, [1], "this runs first")
    op = service.add_comment(uid, hid, 1, "read input")
    service.scratch(uid, hid, "demo.py", "print(1)")
    st = c.get("/code/state").json()
    assert st["stub"] == STUB and st["draft"]["code"] == "a\nb\n"
    assert len(st["annotations"]) == 2 and st["ops"][0]["id"] == op["id"]
    assert st["scratch"][0]["title"] == "demo.py"
    assert c.post(f"/code/ops/{op['id']}/applied", headers=h).json() == {"ok": True,
                                                                          "first": True}
    assert c.get("/code/state").json()["ops"] == []
    r = c.post(f"/code/annotations/{q['id']}/reply", data={"text": "two"}, headers=h)
    assert r.json()["ok"]
    assert service._drain(uid)[-1]["kind"] == "annotation_reply"
    html = c.get("/solve").text
    assert "{# tests panel #}" not in html and 'id="ed-files"' in html
    assert_clean(html)
    assert_clean(c.get("/messages?since=0").text)
    assert c.post(f"/code/annotations/{q['id']}/resolve", headers=h).json()["ok"]
    assert len(c.get("/code/state").json()["annotations"]) == 1
