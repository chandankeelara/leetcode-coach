"""The local test runner (coach/runner.py), the tests-panel service, routes and bridge tools."""
import asyncio
import json
import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from coach import runner, service
from coach.web import app as webapp
from coach.web.svc import Svc

SUM = {"class": "Solution", "method": "subarraySum"}
SUM_OK = '''
class Solution:
    def subarraySum(self, nums: List[int], k: int) -> int:
        seen, run, n = Counter({0: 1}), 0, 0
        for x in nums:
            run += x
            n += seen[run - k]
            seen[run] += 1
        return n
'''
SUM_BAD = '''
class Solution:
    def subarraySum(self, nums: List[int], k: int) -> int:
        return 1
'''
SUM_CASES = [{"args": {"nums": [1, 1, 1], "k": 2}, "expected": 2},
             {"args": {"nums": [1, 2, 3], "k": 3}, "expected": 2}]

SPANNER = '''
class StockSpanner:
    def __init__(self):
        self.st = []

    def next(self, price: int) -> int:
        span = 1
        while self.st and self.st[-1][0] <= price:
            span += self.st.pop()[1]
        self.st.append((price, span))
        return span
'''
SPANNER_BAD = '''
class StockSpanner:
    def __init__(self):
        self.prices = []

    def next(self, price: int) -> int:
        self.prices.append(price)
        return 1
'''
SPAN_CASE = {"ops": ["StockSpanner", "next", "next", "next", "next", "next", "next", "next"],
             "args": [[], [100], [80], [60], [70], [60], [75], [85]],
             "expected": [None, 1, 1, 1, 2, 1, 4, 6]}


# ---- runner ---------------------------------------------------------------------------------
def test_function_mode_pass_and_fail():
    good = runner.run(SUM_OK, SUM_CASES, "function", SUM)
    assert [r["ok"] for r in good] == [True, True]
    assert good[0]["got"] == 2 and good[0]["error"] is None and good[0]["ms"] is not None
    bad = runner.run(SUM_BAD, SUM_CASES, "function", SUM)
    assert [r["ok"] for r in bad] == [False, False]
    assert bad[0]["got"] == 1 and bad[0]["expected"] == 2


def test_case_without_expected_only_runs_and_stdout_is_captured():
    code = SUM_OK.replace("        return n", "        print('n =', n)\n        return n")
    r = runner.run(code, [{"args": {"nums": [1], "k": 1}}], "function", SUM)[0]
    assert r["ok"] is None and r["got"] == 1 and r["stdout"] == "n = 1\n"


def test_design_mode_stock_spanner():
    r = runner.run(SPANNER, [SPAN_CASE], "design", {"class": "StockSpanner"})[0]
    assert r["ok"] is True and r["got"] == SPAN_CASE["expected"]
    bad = runner.run(SPANNER_BAD, [SPAN_CASE], "design", {"class": "StockSpanner"})[0]
    assert bad["ok"] is False and bad["got"][:4] == [None, 1, 1, 1]


def test_listnode_and_treenode_conversion():
    code = '''
class Solution:
    def reverseList(self, head: Optional[ListNode]) -> Optional[ListNode]:
        prev = None
        while head:
            head.next, prev, head = prev, head, head.next
        return prev

    def invertTree(self, root: Optional[TreeNode]) -> Optional[TreeNode]:
        if root:
            root.left, root.right = self.invertTree(root.right), self.invertTree(root.left)
        return root

    def maxDepth(self, root):
        return 0 if not root else 1 + max(self.maxDepth(root.left), self.maxDepth(root.right))
'''
    ll = runner.run(code, [{"args": {"head": [1, 2, 3]}, "types": {"head": "ListNode"},
                            "expected": [3, 2, 1]}], "function", {"method": "reverseList"})
    assert ll[0]["ok"] is True, ll
    tr = runner.run(code, [{"args": {"root": [4, 2, 7, 1, 3, 6, 9]},
                            "types": {"root": "TreeNode", "return": "TreeNode"},
                            "expected": [4, 7, 2, 9, 6, 3, 1]}], "function",
                    {"method": "invertTree"})
    assert tr[0]["ok"] is True, tr
    d = runner.run(code, [{"args": {"root": [3, 9, 20, None, None, 15, 7]},
                           "types": {"root": "TreeNode"}, "expected": 3}], "function",
                   {"method": "maxDepth"})
    assert d[0]["ok"] is True, d


def test_learner_defined_listnode_is_used():
    code = '''
class ListNode:
    def __init__(self, x):
        self.val = x
        self.next = None
        self.mine = True

class Solution:
    def f(self, head):
        return head.mine
'''
    r = runner.run(code, [{"args": {"head": [1]}, "types": {"head": "ListNode"},
                           "expected": True}], "function", {"method": "f"})
    assert r[0]["ok"] is True, r


def test_comparison_rules():
    assert runner.compare(0.1 + 0.2, 0.3)
    assert not runner.compare(1, True) and not runner.compare(True, 1)
    assert runner.compare([[2, 1], [3]], [[3], [2, 1]], unordered=True)
    assert not runner.compare([[2, 1], [3]], [[3], [1, 2]], unordered=True)
    assert runner.compare([[2, 1], [3]], [[3], [1, 2]], unordered="deep")
    assert not runner.compare([1, 2], [2, 1])


def test_unordered_and_inplace_options():
    code = '''
class Solution:
    def sortColors(self, nums):
        nums.sort()
    def pairs(self):
        return [[2, 1], [0, 3]]
'''
    r = runner.run(code, [{"args": {"nums": [2, 0, 1]}, "inplace": "nums",
                           "expected": [0, 1, 2]}], "function", {"method": "sortColors"})
    assert r[0]["ok"] is True, r
    r = runner.run(code, [{"args": {}, "unordered": True, "expected": [[0, 3], [2, 1]]}],
                   "function", {"method": "pairs"})
    assert r[0]["ok"] is True, r


def test_timeout_on_infinite_loop():
    t = time.perf_counter()
    r = runner.run("while True:\n    pass\n", SUM_CASES, "function", SUM, timeout_s=3)
    took = time.perf_counter() - t
    assert [x["error"] for x in r] == ["timeout", "timeout"]
    assert took < 4.5, took


def test_timeout_keeps_finished_cases():
    code = '''
class Solution:
    def subarraySum(self, nums, k):
        while k == 3:
            pass
        return 2
'''
    r = runner.run(code, SUM_CASES, "function", SUM, timeout_s=2)
    assert r[0]["ok"] is True and r[1]["error"] == "timeout"


def test_syntax_error_and_runtime_error():
    r = runner.run("class Solution:\n    def subarraySum(self, nums, k)\n        return 1\n",
                   SUM_CASES, "function", SUM)
    assert all(x["error"].startswith("SyntaxError") and "line 2" in x["error"] for x in r)
    r = runner.run("class Solution:\n    def subarraySum(self, nums, k):\n"
                   "        return nums[10]\n", SUM_CASES[:1], "function", SUM)
    assert r[0]["ok"] is False and r[0]["error"].startswith("IndexError") \
        and "(line 3)" in r[0]["error"]


def test_learner_code_runs_in_its_own_temp_dir(tmp_path, monkeypatch):
    """os.remove on a relative path only touches the runner's throwaway cwd."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "keep.txt").write_text("server file")
    code = '''
import os
class Solution:
    def subarraySum(self, nums, k):
        here = os.getcwd()
        try:
            os.remove("keep.txt")
            gone = "removed"
        except FileNotFoundError:
            gone = "absent"
        return [here, gone]
'''
    r = runner.run(code, [{"args": {"nums": [], "k": 0}}], "function", SUM)[0]
    assert r["error"] is None, r
    cwd, gone = r["got"]
    assert gone == "absent"
    assert os.path.normcase(os.path.abspath(cwd)) != os.path.normcase(str(tmp_path))
    assert (tmp_path / "keep.txt").read_text() == "server file"
    assert not os.path.exists(cwd)                  # temp dir cleaned up


def test_env_is_stripped_and_stdin_closed(monkeypatch):
    monkeypatch.setenv("COACH_SECRET", "do-not-leak")
    code = '''
import os, sys
class Solution:
    def subarraySum(self, nums, k):
        return [os.environ.get("COACH_SECRET"), sys.stdin.read() if sys.stdin else ""]
'''
    r = runner.run(code, [{"args": {"nums": [], "k": 0}}], "function", SUM)[0]
    assert r["got"] == [None, ""], r


def test_output_is_capped():
    code = "class Solution:\n    def subarraySum(self, nums, k):\n" \
           "        print('x' * 500000)\n        return 0\n"
    r = runner.run(code, [{"args": {"nums": [], "k": 0}, "expected": 0}], "function", SUM)[0]
    assert r["ok"] is True and len(r["stdout"]) <= runner.OUT_CAP + 100


def test_check_rejects_bad_shapes():
    with pytest.raises(ValueError):
        runner.check("nope", SUM, [])
    with pytest.raises(ValueError):
        runner.check("function", {"class": "Solution"}, [])
    with pytest.raises(ValueError):
        runner.check("design", {"class": "X"}, [{"ops": ["X", "a"], "args": [[]]}])


# ---- service ----------------------------------------------------------------------------------
@pytest.fixture
def opened(world):
    h = service.next_problem(world)
    return world, h.handout_id


def _kinds_since(uid, since):
    return [e["kind"] for e in service.ui_events_since(uid, since)]


def test_set_tests_stores_and_emits(opened):
    uid, hid = opened
    before = service.ui_last_id()
    r = service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    assert r == {"ok": True, "handout_id": hid, "count": 2}
    assert "tests" in _kinds_since(uid, before)
    t = service.tests(uid, hid)
    assert t["mode"] == "function" and t["entry"] == SUM and t["last_run"] is None
    assert [c["source"] for c in t["cases"]] == ["coach", "coach"]
    assert t["cases"][0]["args"] == {"nums": [1, 1, 1], "k": 2}
    # replacing keeps his own cases
    service.add_user_test(uid, hid, {"args": {"nums": [5], "k": 5}})
    service.set_tests(uid, hid, SUM_CASES[:1], "function", SUM)
    assert [c["source"] for c in service.tests(uid, hid)["cases"]] == ["coach", "user"]


def test_leak_guard_on_case_labels(opened):
    uid, hid = opened
    bad = [dict(SUM_CASES[0], label="classic sliding window case")]
    with pytest.raises(ValueError, match="leak"):
        service.set_tests(uid, hid, bad, "function", SUM)
    assert service.tests(uid, hid)["cases"] == []
    ok = [dict(SUM_CASES[0], label="all ones")]
    assert service.set_tests(uid, hid, ok, "function", SUM)["count"] == 1


def test_add_user_test_needs_a_spec(opened):
    uid, hid = opened
    with pytest.raises(ValueError):
        service.add_user_test(uid, hid, {"args": {"nums": [1], "k": 1}})


def test_user_run_stores_pushes_and_posts(opened):
    uid, hid = opened
    service.set_tests(uid, hid, SUM_CASES + [{"args": {"nums": [3], "k": 3}, "expected": 9}],
                      "function", SUM)
    service._drain(uid)
    before = service.ui_last_id()
    r = service.run_tests(uid, hid, SUM_OK, "user")
    assert (r["passed"], r["failed"], r["total"]) == (2, 1, 3)
    assert "tests" in _kinds_since(uid, before)
    ev = service._drain(uid)
    assert [e["kind"] for e in ev] == ["run"]
    p = ev[0]["payload"]
    assert p["passed"] == 2 and p["failed"] == 1 and p["handout_id"] == hid
    failing = [x for x in p["results_summary"] if x["ok"] is False]
    assert len(failing) == 1 and failing[0]["got"] == "1" and failing[0]["expected"] == "9"
    msgs = service.list_messages(uid)
    assert msgs[-1]["role"] == "system" and msgs[-1]["content"] == "Ran tests: 2/3 passed."
    last = service.tests(uid, hid)["last_run"]
    assert last["id"] == r["run_id"] and last["passed"] == 2


def test_coach_run_is_invisible(opened):
    uid, hid = opened
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    service._drain(uid)
    n_msgs = len(service.list_messages(uid))
    before = service.ui_last_id()
    r = service.run_tests(uid, hid, SUM_BAD, "coach")
    assert r["failed"] == 2
    assert len(service.list_messages(uid)) == n_msgs
    assert _kinds_since(uid, before) == []
    assert service._drain(uid) == []
    assert service.tests(uid, hid)["last_run"] is None
    assert service.last_run(uid, hid, "coach")["id"] == r["run_id"]
    adhoc = service.run_tests(uid, hid, SUM_OK, "coach",
                              cases=[{"args": {"nums": [0, 0], "k": 0}, "expected": 3}])
    assert adhoc["passed"] == 1 and len(service.tests(uid, hid)["cases"]) == 2
    with pytest.raises(ValueError):
        service.run_tests(uid, hid, SUM_OK, "user", cases=[])


def test_design_run_through_service(opened):
    uid, hid = opened
    service.set_tests(uid, hid, [SPAN_CASE], "design", {"class": "StockSpanner"})
    assert service.run_tests(uid, hid, SPANNER, "user")["passed"] == 1


# ---- routes -----------------------------------------------------------------------------------
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


def test_routes_get_run_add(web):
    c, uid, hid, hdr = web
    assert c.get("/tests").json()["cases"] == []
    service.set_tests(uid, hid, SUM_CASES, "function", SUM)
    t = c.get("/tests").json()
    assert t["handout_id"] == hid and len(t["cases"]) == 2 and t["mode"] == "function"
    assert c.post("/tests/run", data={"code": SUM_OK}).status_code == 403     # no csrf
    r = c.post("/tests/run", data={"code": SUM_OK}, headers=hdr)
    assert r.status_code == 200 and r.json()["passed"] == 2
    assert c.get("/tests").json()["last_run"]["passed"] == 2
    a = c.post("/tests/add", data={"case": json.dumps({"args": {"nums": [2, 2], "k": 4}})},
               headers=hdr)
    assert a.status_code == 200 and a.json()["source"] == "user"
    assert c.post("/tests/add", data={"case": "{nope"}, headers=hdr).status_code == 400
    assert c.post("/tests/add", data={"case": json.dumps({"args": [1]})},
                  headers=hdr).status_code == 400
    d = c.post("/tests/delete", data={"id": a.json()["id"]}, headers=hdr)
    assert d.status_code == 200 and len(c.get("/tests").json()["cases"]) == 2
    assert c.post("/tests/run", data={"code": "  "}, headers=hdr).status_code == 400


def test_tests_route_requires_login(world, monkeypatch):
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    webapp.app.state.service = Svc()
    try:
        c = TestClient(webapp.app, follow_redirects=False)
        assert c.get("/tests").status_code == 303
        assert c.post("/tests/run", data={"code": "x"}).status_code == 303
    finally:
        webapp.app.state.service = None


# ---- bridge tools -----------------------------------------------------------------------------
@pytest.fixture
def bridge(opened):
    from bridge import server
    uid, hid = opened
    server.set_service(service, user_id=uid)
    yield server, uid, hid
    server.set_service(None)


def test_bridge_set_and_run_tests(bridge):
    server, uid, hid = bridge
    assert server.run_tests() == {"error": "no tests set for this problem"} or \
        server.run_tests() == {"error": "no code yet"}
    assert server.set_tests(SUM_CASES, "function", SUM)["ok"] is True
    assert "leak" in server.set_tests([dict(SUM_CASES[0], label="use a heap")], "function",
                                      SUM)["error"]
    assert server.run_tests() == {"error": "no code yet"}
    service.push_event(uid, "submission", {"handout_id": hid, "code": SUM_BAD})
    service._drain(uid)
    n_msgs = len(service.list_messages(uid))
    r = server.run_tests()
    assert (r["passed"], r["failed"]) == (0, 2)
    if hasattr(service, "save_draft"):
        service.save_draft(uid, hid, SUM_OK)
        assert server.run_tests()["passed"] == 2
    probe = server.run_tests(cases=[{"args": {"nums": [1], "k": 1}, "expected": 1}])
    assert probe["total"] == 1
    assert len(service.list_messages(uid)) == n_msgs        # coach runs post nothing
    assert service._drain(uid) == []
