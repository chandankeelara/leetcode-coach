"""Running the question: the private reference, expected outputs from it, the tracer,
trace_to_animation, the playground, predict cards, the gallery and learner notes."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from coach import db, notes, reference, runner, service, tools, viz
from coach.models import Card, Handout, Trace, User
from coach.web import app as webapp
from coach.web.svc import Svc

SUM = {"class": "Solution", "method": "subarraySum"}
# the plain definition: every subarray, counted
SUM_REF = '''
class Solution:
    def subarraySum(self, nums, k):
        count = 0
        for i in range(len(nums)):
            for j in range(i, len(nums)):
                if sum(nums[i:j + 1]) == k:
                    count += 1
        return count
'''
SUM_OK = '''
class Solution:
    def subarraySum(self, nums, k):
        seen, run, n = {0: 1}, 0, 0
        for x in nums:
            run += x
            n += seen.get(run - k, 0)
            seen[run] = seen.get(run, 0) + 1
        return n
'''
SUM_BAD = "class Solution:\n    def subarraySum(self, nums, k):\n        return 1\n"
SUM_CASES = [{"args": {"nums": [1, 1, 1], "k": 2}, "expected": 2},
             {"args": {"nums": [1, 2, 3], "k": 3}, "expected": 2}]

REORDER = {"class": "Solution", "method": "reorderList"}
REORDER_REF = '''
class Solution:
    def reorderList(self, head):
        nodes = []
        cur = head
        while cur:
            nodes.append(cur)
            cur = cur.next
        order = []
        i, j = 0, len(nodes) - 1
        while i <= j:
            order.append(nodes[i])
            if i != j:
                order.append(nodes[j])
            i += 1
            j -= 1
        for k in range(len(order) - 1):
            order[k].next = order[k + 1]
        if order:
            order[-1].next = None
'''
LIST_OPTS = {"types": {"head": "ListNode"}, "inplace": "head"}
REORDER_CASES = [{"args": {"head": [1, 2, 3, 4]}, "expected": [1, 4, 2, 3], **LIST_OPTS},
                 {"args": {"head": [1, 2, 3, 4, 5]}, "expected": [1, 5, 2, 4, 3], **LIST_OPTS}]


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    reference._LAST_PLAY.clear()
    yield
    reference._LAST_PLAY.clear()


@pytest.fixture
def opened(world):
    h = service.next_problem(world)
    return world, h.handout_id


# ---- set_reference ------------------------------------------------------------------------------
def test_reference_stored_validated_and_replace_rules(opened):
    uid, hid = opened
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    bad = service.set_reference(uid, hid, SUM_BAD, "function", SUM)
    assert bad["verified"] is False and bad["checked"] == 2 and bad["passed"] == 0
    assert [m["case_index"] for m in bad["mismatches"]] == [0, 1]
    assert bad["mismatches"][0]["got"] == "1" and bad["mismatches"][0]["expected"] == "2"
    # a failed one is replaced freely; the entry comes from the tests spec
    good = service.set_reference(uid, hid, SUM_REF)
    assert good["verified"] is True and good["mismatches"] == [] and good["replaced"] is True
    with pytest.raises(ValueError, match="replace=true"):
        service.set_reference(uid, hid, SUM_OK)
    with pytest.raises(ValueError, match="reason"):
        service.set_reference(uid, hid, SUM_OK, replace=True)
    r = service.set_reference(uid, hid, SUM_OK, replace=True, reason="faster for big cases")
    assert r["verified"] is True
    with db.session_scope() as s:
        h = s.get(Handout, hid)
    ref = reference.reference_of(h.problem_number)
    assert ref["code"] == SUM_OK and ref["entry"] == SUM and ref["mode"] == "function"
    assert ref["set_at"]


def test_reference_shape_refusals(opened):
    uid, hid = opened
    with pytest.raises(ValueError, match="entry needed"):
        service.set_reference(uid, hid, SUM_REF)               # no spec, no entry
    with pytest.raises(ValueError, match="200 lines"):
        service.set_reference(uid, hid, "x = 1\n" * 201, "function", SUM)
    with pytest.raises(ValueError, match="SyntaxError"):
        service.set_reference(uid, hid, "class Solution:\n    def f(self:\n", "function", SUM)
    r = service.set_reference(uid, hid, SUM_REF, "function", SUM)
    assert r["verified"] is None and r["checked"] == 0        # nothing to check yet
    # unchecked: may be resent without replace
    assert service.set_reference(uid, hid, SUM_REF, "function", SUM)["ok"] is True


def test_reference_emits_and_never_reaches_his_page_data(opened):
    uid, hid = opened
    before = service.ui_last_id()
    service.set_reference(uid, hid, SUM_REF, "function", SUM)
    assert "statement" in [e["kind"] for e in service.ui_events_since(uid, before)]
    assert "subarraySum(self" not in json.dumps(service.tests(uid, hid))


# ---- expected outputs from the reference --------------------------------------------------------
def test_set_tests_computes_expected_from_reference(opened):
    uid, hid = opened
    service.set_reference(uid, hid, SUM_REF, "function", SUM)
    r = service.set_tests(uid, hid, [{"args": {"nums": [1, 1, 1], "k": 2}},
                                     {"args": {"nums": [1, -1, 0], "k": 0}}], "function", SUM)
    assert r["count"] == 2 and r["expected_from_reference"] == 2
    exp = [c["expected"] for c in service.tests(uid, hid)["cases"]]
    assert exp == [2, 3]


def test_set_tests_refuses_a_disagreeing_expected(opened):
    uid, hid = opened
    service.set_reference(uid, hid, SUM_REF, "function", SUM)
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    with pytest.raises(ValueError, match=r"your expected 99010 disagrees with the reference 2"):
        service.set_tests(uid, hid, [{"args": {"nums": [1, 1, 1], "k": 2}, "expected": 99010}],
                          "function", SUM)
    assert len(service.tests(uid, hid)["cases"]) == 2          # nothing replaced
    with pytest.raises(ValueError, match="fails on this input"):
        service.set_tests(uid, hid, [{"args": {"nums": None, "k": 2}}], "function", SUM)


def test_unchecked_reference_becomes_verified_when_an_example_agrees(opened):
    uid, hid = opened
    service.set_reference(uid, hid, SUM_REF, "function", SUM)
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    with db.session_scope() as s:
        n = s.get(Handout, hid).problem_number
    assert reference.reference_of(n)["verified"] is True


def _submit(uid, hid, code):
    service.push_event(uid, "submission", {"handout_id": hid, "code": code, "judged_by": "coach"})


def test_judge_extra_cases_get_expected_from_reference(opened):
    uid, hid = opened
    service.set_reference(uid, hid, SUM_REF, "function", SUM)
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    _submit(uid, hid, SUM_OK)
    extra = [{"args": {"nums": [0, 0, 0], "k": 0}}, {"args": {"nums": [-1, -1, 1], "k": 0}}]
    assert service.judge(uid, hid, extra)["verdict"] == "Accepted"
    _submit(uid, hid, SUM_BAD)
    j = service.judge(uid, hid, [{"args": {"nums": [0, 0, 0], "k": 0}}])
    assert j["verdict"] == "Wrong Answer"
    with pytest.raises(ValueError, match="disagrees with the reference"):
        service.judge(uid, hid, [{"args": {"nums": [0, 0, 0], "k": 0}, "expected": 5}])


def test_slow_reference_on_a_large_case_warns(opened, monkeypatch):
    uid, hid = opened
    monkeypatch.setattr(reference, "REF_TIMEOUT_S", 2)
    slow = SUM_REF.replace("count = 0", "count = 0\n        if len(nums) > 1000:\n"
                                        "            while True:\n                pass")
    service.set_reference(uid, hid, slow, "function", SUM)
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    _submit(uid, hid, SUM_OK)
    big = {"args": {"nums": {"$repeat": [0, 5000]}, "k": 1}}
    j = service.judge(uid, hid, [big, {"args": {"nums": [2], "k": 2}}])
    assert j["verdict"] == "Accepted" and j["total"] == 3       # the big case was dropped
    assert any("dropped" in w and "too slow" in w for w in j["warnings"])
    j = service.judge(uid, hid, [dict(big, expected=0)])
    assert j["total"] == 3 and any("unchecked" in w for w in j["warnings"])


# ---- tracer -------------------------------------------------------------------------------------
def test_tracer_frames_lines_locals_and_lists():
    t = runner.trace(REORDER_REF, {"args": {"head": [1, 2, 3, 4]}, **LIST_OPTS}, "function",
                     REORDER, max_steps=200)
    assert t["error"] is None and t["result"] == [1, 4, 2, 3] and not t["truncated"]
    f0 = t["frames"][0]
    assert f0["line"] == 4 and f0["note"] == "call reorderList" and f0["func"] == "reorderList"
    assert f0["locals"]["head"] == {"$type": "ListNode", "values": [1, 2, 3, 4],
                                    "ids": [1, 2, 3, 4], "cycle_to": None}
    lines = {f["line"] for f in t["frames"]}
    assert {5, 6, 7, 8, 11, 17} <= lines
    later = [f for f in t["frames"] if "order" in f["locals"]][-1]["locals"]
    assert [x["val"] for x in later["nodes"]] == [1, 2, 3, 4]
    assert [x["id"] for x in later["order"]] == [1, 4, 2, 3]       # node identity kept
    assert t["frames"][-1]["note"].startswith("return")


def test_tracer_is_cycle_safe_capped_and_watches():
    code = "class Solution:\n    def f(self, head):\n        n = 0\n        while n < 500:\n            n += 1\n        return n\n"
    t = runner.trace(code, {"args": {"head": [1, 2, 3], "pos": 1},
                            "types": {"head": "ListNode"}}, "function", {"method": "f"},
                     max_steps=10, watch=["n"])
    assert t["truncated"] is True and len(t["frames"]) == 10 and t["result"] == 500
    assert all(set(f["locals"]) <= {"n"} for f in t["frames"])
    full = runner.trace(code, {"args": {"head": [1, 2, 3], "pos": 1},
                               "types": {"head": "ListNode"}}, "function", {"method": "f"},
                        max_steps=3)
    assert full["frames"][0]["locals"]["head"]["cycle_to"] == 1
    big = runner.trace("class Solution:\n    def f(self, xs):\n        y = xs\n        return 0\n",
                       {"args": {"xs": list(range(500))}}, "function", {"method": "f"})
    ys = big["frames"][-1]["locals"]["y"]
    assert len(ys) == 51 and ys[-1] == "...+450"
    with pytest.raises(ValueError):
        runner.trace(code, {"args": {}}, "function", {"method": "f"}, watch="n")


def test_trace_service_stores_and_gates_his_code(opened):
    uid, hid = opened
    service.set_reference(uid, hid, REORDER_REF, "function", REORDER, options=LIST_OPTS)
    t = service.trace(uid, hid, {"head": [1, 2, 3, 4]})
    assert t["trace_id"] and t["result"] == [1, 4, 2, 3]
    with db.session_scope() as s:
        row = s.get(Trace, t["trace_id"])
        assert row.target == "reference" and row.input["types"] == {"head": "ListNode"}
    service.save_draft(uid, hid, REORDER_REF.replace("order[-1].next = None", "pass"))
    with pytest.raises(ValueError, match="only after he asked"):
        service.trace(uid, hid, {"head": [1, 2]}, target="his")
    with pytest.raises(ValueError, match="only after he asked"):
        service.trace(uid, hid, {"head": [1, 2]}, target="his", allow=True)   # no reason
    ok = service.trace(uid, hid, {"head": [1, 2]}, target="his", allow=True,
                       reason="he asked in chat")
    assert ok["target"] == "his"
    service.push_event(uid, "stuck", {"handout_id": hid})
    assert service.trace(uid, hid, {"head": [1, 2, 3]}, target="his")["frames"]


def test_his_trace_allowed_after_a_failed_run(opened):
    uid, hid = opened
    service.set_reference(uid, hid, SUM_REF, "function", SUM)
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    service.save_draft(uid, hid, SUM_BAD)
    with pytest.raises(ValueError):
        service.trace(uid, hid, {"nums": [1], "k": 1}, target="his")
    service.run_tests(uid, hid, SUM_BAD, "user")
    assert service.trace(uid, hid, {"nums": [1], "k": 1}, target="his")["result"] == 1


def test_trace_to_animation_makes_a_valid_animate_spec(opened):
    uid, hid = opened
    service.set_reference(uid, hid, REORDER_REF, "function", REORDER, options=LIST_OPTS)
    t = service.trace(uid, hid, {"head": [1, 2, 3, 4]}, max_steps=200)
    a = service.trace_to_animation(uid, hid, trace_id=t["trace_id"], base="array",
                                   map={"values": "order", "show": ["i", "j"]})
    assert a["kind"] == "animate" and 2 <= a["frames"] <= 40
    assert a["spec"]["frames"][-1]["spec"]["values"] == [1, 4, 2, 3]
    viz.render("animate", a["spec"])
    arr = service.trace_to_animation(uid, hid, frames=t["frames"], base="array",
                                     map={"values": "nodes", "pointers": {"i": "i", "j": "j"}})
    ptr = [f["spec"].get("pointers") for f in arr["spec"]["frames"]]
    assert {"i": 0, "j": 3} in ptr
    lst = service.trace_to_animation(uid, hid, trace_id=t["trace_id"], base="list",
                                     map={"values": "head", "pointers": {"cur": "cur"}},
                                     post=True)
    assert lst["visual_id"]
    assert lst["spec"]["frames"][-1]["spec"]["values"] == [1, 4, 2, 3]
    assert any(f["spec"].get("pointers", {}).get("cur") == 2 for f in lst["spec"]["frames"])
    msgs = service.list_messages(uid)
    assert msgs[-1]["visual"]["kind"] == "animate"
    with pytest.raises(ValueError):
        service.trace_to_animation(uid, hid, trace_id=t["trace_id"], map={"values": "nope"})
    with pytest.raises(ValueError):
        service.trace_to_animation(uid, hid, trace_id=t["trace_id"], base="io",
                                   map={"values": "head"})


# ---- predict and gallery ----------------------------------------------------------------------
def test_predict_card_flow(opened):
    uid, hid = opened
    service.set_reference(uid, hid, REORDER_REF, "function", REORDER, options=LIST_OPTS)
    with pytest.raises(ValueError, match="none of the choices"):
        service.predict(uid, hid, {"head": [1, 2, 3, 4]}, choices=["[1,2,3,4]", "[4,3,2,1]"])
    with pytest.raises(ValueError, match="2 to 4"):
        service.predict(uid, hid, {"head": [1, 2, 3, 4]}, choices=["[1,4,2,3]"])
    service._drain(uid)
    p = service.predict(uid, hid, {"head": [1, 2, 3, 4]},
                        choices=["[1,2,3,4]", [1, 4, 2, 3], "[4,1,3,2]"])
    assert p["output"] == [1, 4, 2, 3]
    m = service.list_messages(uid)[-1]
    assert m["content"] == "What does this return?" and m["card"]["kind"] == "predict"
    assert "1,4,2,3" in m["card"]["html"]           # it is one of the choices...
    free = service.predict(uid, hid, {"head": [1, 2, 3, 4, 5]})
    html = service.list_messages(uid)[-1]["card"]["html"]
    assert "1,5,2,4,3" not in html and "rq-answer" in html      # ...but never the output itself
    wrong = service.answer_predict(uid, p["id"], choice="0")
    assert wrong["correct"] is False and "✗" in wrong["html"] and "[1,4,2,3]" in wrong["html"]
    with pytest.raises(ValueError, match="already answered"):
        service.answer_predict(uid, p["id"], choice="1")
    right = service.answer_predict(uid, free["id"], answer=" [1, 5, 2, 4, 3] ")
    assert right["correct"] is True and "✓" in right["html"]
    ev = [e for e in service._drain(uid) if e["kind"] == "prediction"]
    assert [(e["payload"]["id"], e["payload"]["correct"]) for e in ev] == \
        [(p["id"], False), (free["id"], True)]


def test_gallery_bubble(opened):
    uid, hid = opened
    service.set_reference(uid, hid, REORDER_REF, "function", REORDER, options=LIST_OPTS)
    n = len(service.list_messages(uid))
    g = service.gallery(uid, hid, [{"args": {"head": []}, "label": "empty"},
                                   {"head": [7]}, {"head": [1, 2]}], caption="Small lists")
    assert [r["output"] for r in g["rows"]] == [[], [7], [1, 2]]
    msgs = service.list_messages(uid)
    assert len(msgs) == n + 1 and msgs[-1]["card"]["kind"] == "gallery"
    assert msgs[-1]["content"] == "Small lists" and "empty" in msgs[-1]["card"]["html"]
    with pytest.raises(ValueError, match="leak"):
        service.gallery(uid, hid, [{"args": {"head": [1]}, "label": "use a heap"}])
    with pytest.raises(ValueError, match="1 to 8"):
        service.gallery(uid, hid, [])


def test_cards_need_a_reference(opened):
    uid, hid = opened
    with pytest.raises(ValueError, match="set_reference first"):
        service.predict(uid, hid, {"nums": [1], "k": 1})
    with pytest.raises(ValueError, match="set_reference first"):
        service.gallery(uid, hid, [{"nums": [1], "k": 1}])


# ---- playground route ---------------------------------------------------------------------------
@pytest.fixture
def web(opened, monkeypatch):
    uid, hid = opened
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    webapp.app.state.service = Svc()
    c = TestClient(webapp.app, follow_redirects=False)
    page = c.get("/login").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert c.post("/login", data={"username": "tester", "password": "pw-for-tests",
                                  "csrf": tok}).status_code == 303
    csrf = re.search(r'X-CSRF-Token": "([^"]+)"', c.get("/solve").text).group(1)
    yield c, uid, hid, {"X-CSRF-Token": csrf}
    webapp.app.state.service = None


def test_playground_runs_the_reference_only(web):
    c, uid, hid, hdr = web
    assert c.get("/playground").json() == {"ready": False}
    service.set_tests(uid, hid, REORDER_CASES, "function", REORDER)
    service.set_reference(uid, hid, REORDER_REF, "function", REORDER)
    st = c.get("/playground").json()
    assert st["ready"] is True and st["params"] == ["head"] and st["example"] == {"head": "[1,2,3,4]"}
    service.save_draft(uid, hid, "class Solution:\n    def reorderList(self, head):\n        head.val = 99\n")
    service._drain(uid)
    n = len(service.list_messages(uid))
    assert c.post("/playground", data={"head": "[1,2,3,4,5]"}).status_code == 403   # csrf
    r = c.post("/playground", data={"head": "[1,2,3,4,5]"}, headers=hdr)
    assert r.status_code == 200
    d = r.json()
    assert d["output"] == [1, 5, 2, 4, 3] and d["error"] is None
    assert "viz-list" in d["input_html"] and "viz-list" in d["output_html"]
    assert c.post("/playground", data={"head": "[1]"}, headers=hdr).status_code == 429
    ev = service._drain(uid)
    assert [e["kind"] for e in ev] == ["playground"]
    assert ev[0]["payload"]["input"] == {"head": [1, 2, 3, 4, 5]}
    assert ev[0]["payload"]["output"] == [1, 5, 2, 4, 3]
    assert len(service.list_messages(uid)) == n                       # no chat line
    reference._LAST_PLAY.clear()
    big = c.post("/playground", data={"head": json.dumps(list(range(2001)))}, headers=hdr)
    assert big.status_code == 400 and "too large" in big.json()["error"]
    reference._LAST_PLAY.clear()
    bad = c.post("/playground", data={"head": "[1,2"}, headers=hdr)
    assert bad.status_code == 400 and "like [1,2,3]" in bad.json()["error"]


def test_solve_page_has_the_playground_and_no_code_or_pattern_words(web):
    c, uid, hid, hdr = web
    service.set_tests(uid, hid, REORDER_CASES, "function", REORDER)
    service.set_reference(uid, hid, REORDER_REF, "function", REORDER)
    page = c.get("/solve").text
    assert 'id="play"' in page and "/static/play.js" in page
    assert "order[k].next" not in page and "nodes.append" not in page
    js = c.get("/static/play.js").text
    blob = (page + js + c.get("/playground").text).lower()
    for w in ("two pointer", "sliding window", "dummy node", "fast and slow", "heap",
              "binary search", "reverse the second half"):
        assert w not in blob
    assert "nodes.append" not in c.get("/playground").text


def test_predict_route(web):
    c, uid, hid, hdr = web
    service.set_reference(uid, hid, REORDER_REF, "function", REORDER, options=LIST_OPTS)
    p = service.predict(uid, hid, {"head": [1, 2, 3]})
    assert c.post(f"/predict/{p['id']}", data={"answer": "[1,3,2]"}).status_code == 403
    r = c.post(f"/predict/{p['id']}", data={"answer": "[1,3,2]"}, headers=hdr)
    assert r.status_code == 200 and r.json()["correct"] is True
    assert c.post(f"/predict/{p['id']}", data={"answer": "x"}, headers=hdr).status_code == 400
    assert c.post("/predict/99999", data={"answer": "x"}, headers=hdr).status_code == 400
    thread = c.get("/messages?since=0").text
    assert 'class="rq rq-predict"' in thread and 'data-state="right"' in thread


# ---- learner notes ------------------------------------------------------------------------------
def test_learner_notes_crud_and_profile(world):
    uid = world
    n = service.add_note(uid, "explanation", "Plain before/after stills landed.")
    assert n["kind"] == "explanation" and n["active"] is True
    service.add_note(uid, "pace", "Fast once the idea holds.")
    assert [x["note"] for x in service.list_notes(uid)][0] == "Plain before/after stills landed."
    for kind, text in (("mood", "x"), ("pace", ""), ("pace", "x" * 301),
                       ("other", "def f(x):\n    return x\n"), ("other", "tester likes stills")):
        with pytest.raises(ValueError):
            service.add_note(uid, kind, text)
    service.retire_note(uid, n["id"])
    assert [x["note"] for x in service.list_notes(uid)] == ["Fast once the idea holds."]
    assert len(service.list_notes(uid, active_only=False)) == 2
    prof = service.get_profile(uid)
    assert [x["note"] for x in prof["notes"]] == ["Fast once the idea holds."]
    with pytest.raises(ValueError):
        service.retire_note(uid, 99999)


def test_owner_note_is_seeded_once(world):
    service.create_user("chandan", "pw-x")
    with db.session_scope() as s:
        assert notes.seed(s) is True
    with db.session_scope() as s:
        assert notes.seed(s) is False
        cid = s.query(User).filter_by(username="chandan").one().id
    got = service.list_notes(cid)
    assert len(got) == 1 and got[0]["kind"] == "explanation" and "143" in got[0]["note"]


# ---- tools --------------------------------------------------------------------------------------
@pytest.fixture
def bridge(opened):
    from bridge import server
    uid, hid = opened
    server.set_service(service, user_id=uid)
    yield server, uid, hid
    server.set_service(None)


def test_tools_registered_and_working_through_the_bridge(bridge):
    server, uid, hid = bridge
    for name in ("set_reference", "trace", "trace_to_animation", "predict", "gallery",
                 "note_learner", "learner_notes"):
        assert name in tools.TOOLS
    assert "set_reference first" in server.predict({"head": [1]})["error"]
    r = server.set_reference(REORDER_REF, "function", REORDER, LIST_OPTS)
    assert r["ok"] is True and r["verified"] is None
    t = server.trace({"head": [1, 2, 3, 4]}, max_steps=200)
    a = server.trace_to_animation(trace_id=t["trace_id"], base="list",
                                  map={"values": "head"}, post=True)
    assert a["visual_id"] and a["kind"] == "animate"
    assert "only after he asked" in server.trace({"head": [1]}, target="his")["error"] or \
        server.trace({"head": [1]}, target="his")["error"] == "no code yet"
    assert server.predict({"head": [1, 2]})["output"] == [1, 2]
    assert server.gallery([{"head": []}])["rows"][0]["output"] == []
    assert server.note_learner("pace", "Quick on easy lists.")["id"]
    assert server.learner_notes()["notes"][0]["note"] == "Quick on easy lists."
    nid = server.learner_notes()["notes"][0]["id"]
    assert server.learner_notes(retire_id=nid)["notes"] == []


SPANNER_REF = '''
class StockSpanner:
    def __init__(self):
        self.prices = []

    def next(self, price):
        self.prices.append(price)
        span = 0
        for p in reversed(self.prices):
            if p > price:
                break
            span += 1
        return span
'''


def test_design_reference_playground_and_expected(opened):
    uid, hid = opened
    service.set_reference(uid, hid, SPANNER_REF, "design", {"class": "StockSpanner"})
    r = service.set_tests(uid, hid, [{"ops": ["StockSpanner", "next", "next", "next"],
                                      "args": [[], [100], [80], [90]]}],
                          "design", {"class": "StockSpanner"})
    assert r["expected_from_reference"] == 1
    assert service.tests(uid, hid)["cases"][0]["expected"] == [None, 1, 1, 2]
    st = service.playground_state(uid, hid)
    assert st["ready"] and st["mode"] == "design" and st["ops"].startswith('["StockSpanner"')
    out = service.playground_run(uid, hid, {"ops": '["StockSpanner","next","next"]',
                                            "args": "[[],[5],[6]]"})
    assert out["output"] == [None, 1, 2] and out["output_text"] == "[null,1,2]"
