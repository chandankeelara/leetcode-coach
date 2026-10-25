"""Tests panel: the cases of a handout, his own cases, and runs (coach/runner.py executes).

Re-exported by coach/service.py (the "tests panel" block there), so callers use
`service.set_tests(...)` etc.

* set_tests      coach: replace the coach cases + the call spec (mode, entry). Leak guard on
                 every free-text field of a case (label, note). Emits `tests`.
* add_user_test  his own case (expected optional). Emits `tests`.
* tests          {handout_id, mode, entry, cases, last_run} for the panel. last_run is his
                 latest run only; coach runs are never shown.
* run_tests      run code on the cases in a subprocess, store a test_runs row.
                 who="user": emits `tests`, pushes a `run` event for the coach and posts a
                 muted chat line "Ran tests: 2/3 passed". who="coach": stored, nothing else
                 (invisible to him). `cases=` runs those instead of the stored ones (coach
                 probing for the smallest failing input; not stored as cases).
"""
import json
from datetime import datetime

from sqlalchemy import delete, select

from . import runner
from .db import session_scope
from .models import Handout, Problem, Submission, TestCase, TestRun, TestSpec

TESTS_UI_KINDS = frozenset({"tests"})
TEXT_FIELDS = ("label", "note")
CASE_MAX = 20_000            # json chars per case
CASES_MAX = 50               # cases per handout (coach + his)
CODE_MAX = 200_000
SUMMARY_TEXT = 300           # chars of got/expected/error per failing case in the run event


def _svc():
    from . import service
    return service


def _handout(s, user_id, handout_id):
    h = s.get(Handout, handout_id) if handout_id is not None else None
    if h is None or h.user_id != user_id:
        raise ValueError(f"no handout {handout_id}")
    return h


def _clean_case(c, i):
    if not isinstance(c, dict):
        raise ValueError(f"case {i} must be an object")
    c = json.loads(json.dumps(c))         # plain JSON, a private copy
    c.pop("id", None)
    c.pop("source", None)
    if len(json.dumps(c)) > CASE_MAX:
        raise ValueError(f"case {i}: {CASE_MAX} json characters max")
    for k in TEXT_FIELDS:
        if k in c and not isinstance(c[k], str):
            raise ValueError(f"case {i}: {k} must be a string")
    return c


def _guard(s, h, cases):
    title = s.get(Problem, h.problem_number).title
    text = "\n".join(str(c.get(k) or "") for c in cases for k in TEXT_FIELDS)
    bad = _svc().leak_words(text, title, s)
    if bad:
        raise ValueError("leak: " + ", ".join(bad))
    _svc().rule_zero_check(s, h.user_id, text, h)


def _spec(s, handout_id):
    return s.get(TestSpec, handout_id)


def _case_rows(s, user_id, handout_id):
    return list(s.scalars(select(TestCase).where(
        TestCase.user_id == user_id, TestCase.handout_id == handout_id)
        .order_by(TestCase.source, TestCase.id)))


def _ordered(rows):
    """Coach cases first, then his, each oldest first."""
    return sorted(rows, key=lambda r: (r.source != "coach", r.id))


def set_tests(user_id, handout_id, cases, mode="function", entry=None):
    """Replace the coach cases of a handout and its call spec. ValueError on a bad shape or
    ('leak: ...') when a label/note names a technique or a difficulty; nothing is stored then.
    His own cases stay. Returns {"ok", "handout_id", "count"}."""
    entry = entry or {}
    if not isinstance(cases, list):
        raise ValueError("cases must be a list")
    cases = [_clean_case(c, i) for i, c in enumerate(cases)]
    runner.check(mode, entry, cases)
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        _guard(s, h, cases)
        number = h.problem_number
    # expected outputs come from the problem's reference (coach/reference.py): a missing one is
    # filled, a given one must agree (ValueError, nothing stored)
    from . import reference
    missing, given = sum(1 for c in cases if "expected" not in c), len(cases)
    has_ref = reference.reference_of(number) is not None
    cases, warnings = reference.resolve_cases(number, cases, mode)
    filled = missing - (given - len(cases)) if has_ref else 0
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        mine = s.scalars(select(TestCase.id).where(
            TestCase.handout_id == h.id, TestCase.source == "user")).all()
        if len(cases) + len(mine) > CASES_MAX:
            raise ValueError(f"{CASES_MAX} cases max per problem")
        sp = _spec(s, h.id)
        if sp is None:
            sp = TestSpec(handout_id=h.id, user_id=user_id)
            s.add(sp)
        sp.mode, sp.entry = mode, json.loads(json.dumps(entry))
        s.execute(delete(TestCase).where(TestCase.handout_id == h.id,
                                         TestCase.source == "coach"))
        for c in cases:
            s.add(TestCase(user_id=user_id, handout_id=h.id, source="coach", case=c))
        s.flush()
        _svc().emit(user_id, "tests", h.id, s)
        out = {"ok": True, "handout_id": h.id, "count": len(cases)}
        if filled > 0:
            out["expected_from_reference"] = filled
        if warnings:
            out["warnings"] = warnings
        return out


def add_user_test(user_id, handout_id, case):
    """His own case (expected optional). Needs the coach's spec first (mode/entry). Returns
    the stored case {id, source: "user", ...}."""
    case = _clean_case(case, 0)
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        sp = _spec(s, h.id)
        if sp is None:
            raise ValueError("no tests yet: your coach sets them up")
        runner.check(sp.mode, sp.entry or {}, [case])
        if len(_case_rows(s, user_id, h.id)) >= CASES_MAX:
            raise ValueError(f"{CASES_MAX} cases max per problem")
        row = TestCase(user_id=user_id, handout_id=h.id, source="user", case=case)
        s.add(row)
        s.flush()
        _svc().emit(user_id, "tests", h.id, s)
        return {"id": row.id, "source": "user", **case}


def delete_user_test(user_id, handout_id, case_id):
    """Remove one of HIS cases (coach cases cannot be removed from the page)."""
    with session_scope() as s:
        _handout(s, user_id, handout_id)
        row = s.get(TestCase, case_id)
        if row is None or row.handout_id != handout_id or row.source != "user":
            raise ValueError(f"no case {case_id}")
        s.delete(row)
        _svc().emit(user_id, "tests", handout_id, s)
        return {"ok": True}


def _run_row(r):
    return {"id": r.id, "who": r.who, "at": r.created_at.isoformat(), "results": r.results,
            **runner.summary(r.results or [])}


def _case_out(r):
    c = {"id": r.id, "source": r.source, **(r.case or {})}
    html = input_html(c) if (r.case or {}) else ""
    if html:
        c["input_html"] = html
    return c


def _draw(kind, spec):
    from . import viz
    try:
        return str(viz.render(kind, spec))
    except (ValueError, TypeError, KeyError):
        return ""


def input_html(case):
    """Drawings of the structures in a function-mode case's input, via viz.render (escaped
    there): ListNode arg -> list (cycle_to from an int `pos` arg, -1 -> none), TreeNode arg ->
    tree, list of lists -> grid, flat list of 2+ -> array. Scalars and short arrays: nothing.
    Returns "" when there is nothing to draw."""
    args = case.get("args")
    if not isinstance(args, dict):
        return ""
    types = case.get("types") if isinstance(case.get("types"), dict) else {}
    pos = args.get("pos")
    parts = []
    for name, v in args.items():
        if not isinstance(v, list):
            continue
        kind_t = types.get(name)
        if kind_t == "ListNode":
            spec = {"values": v}
            if isinstance(pos, int) and not isinstance(pos, bool) and pos >= 0:
                spec["cycle_to"] = pos
            parts.append(_draw("list", spec))
        elif kind_t == "TreeNode":
            parts.append(_draw("tree", {"values": v}))
        elif v and all(isinstance(r, list) for r in v):
            parts.append(_draw("grid", {"rows": v}))
        elif len(v) >= 2 and not any(isinstance(x, (list, dict)) for x in v):
            parts.append(_draw("array", {"values": v}))
    return "".join(p for p in parts if p)


def tests(user_id, handout_id):
    """{handout_id, mode, entry, cases: [{id, source, ...case}], last_run: {id, who, at,
    results, passed, failed, ran, total} | None}. last_run = his latest run."""
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        sp = _spec(s, h.id)
        rows = _ordered(_case_rows(s, user_id, h.id))
        last = s.scalars(select(TestRun).where(
            TestRun.handout_id == h.id, TestRun.who == "user")
            .order_by(TestRun.id.desc()).limit(1)).first()
        return {"handout_id": h.id, "mode": sp.mode if sp else None,
                "entry": (sp.entry if sp else None) or {},
                "cases": [_case_out(r) for r in rows],
                "last_run": _run_row(last) if last else None}


def last_run(user_id, handout_id, who="user"):
    """The latest run by `who`, or None."""
    with session_scope() as s:
        _handout(s, user_id, handout_id)
        r = s.scalars(select(TestRun).where(TestRun.handout_id == handout_id,
                                            TestRun.who == who)
                      .order_by(TestRun.id.desc()).limit(1)).first()
        return _run_row(r) if r else None


def _short(v):
    t = v if isinstance(v, str) else json.dumps(v)
    return t if len(t) <= SUMMARY_TEXT else t[:SUMMARY_TEXT] + "..."


def results_summary(cases, results):
    """Compact per-case lines for the coach: failing cases carry input, got, expected, error."""
    out = []
    for c, r in zip(cases, results):
        row = {"case_index": r["case_index"], "ok": r["ok"], "id": c.get("id"),
               "source": c.get("source")}
        if r["ok"] is not True:
            inp = {k: c[k] for k in ("args", "ops") if k in c}
            row.update(input=_short(inp), got=_short(r["got"]),
                       expected=_short(r["expected"]) if "expected" in c else None,
                       error=_short(r["error"]) if r["error"] else None)
        out.append(row)
    return out


def run_tests(user_id, handout_id, code, who="user", cases=None, timeout_s=None):
    """Run `code` on the handout's cases (or on `cases`, coach only). Stores a test_runs row.
    Returns {"run_id", "results", "passed", "failed", "ran", "total"}. ValueError when no
    spec/cases exist or the code is too long."""
    if who not in ("user", "coach"):
        raise ValueError("who must be user or coach")
    code = (code or "").replace("\r\n", "\n")
    if len(code) > CODE_MAX:
        raise ValueError(f"code: {CODE_MAX} characters max")
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        sp = _spec(s, h.id)
        if sp is None:
            raise ValueError("no tests set for this problem")
        mode, entry = sp.mode, sp.entry or {}
        if cases is not None:
            if who != "coach":
                raise ValueError("only the coach runs ad-hoc cases")
            run_cases = [{"id": None, "source": "adhoc", **_clean_case(c, i)}
                         for i, c in enumerate(cases)]
        else:
            run_cases = [{"id": r.id, "source": r.source, **(r.case or {})}
                         for r in _ordered(_case_rows(s, user_id, h.id))]
    if not run_cases:
        raise ValueError("no test cases")
    bare = [{k: v for k, v in c.items() if k not in ("id", "source")} for c in run_cases]
    kw = {"timeout_s": timeout_s} if timeout_s else {}
    results = runner.run(code, bare, mode, entry, **kw)
    for c, r in zip(run_cases, results):
        r["case_id"], r["source"] = c.get("id"), c.get("source")
    summ = runner.summary(results)
    with session_scope() as s:
        row = TestRun(user_id=user_id, handout_id=handout_id, who=who, code=code,
                      results=results, created_at=datetime.now())
        s.add(row)
        s.flush()
        run_id = row.id
        if who == "user":
            _svc().emit(user_id, "tests", handout_id, s)
    if who == "user":
        checked = summ["passed"] + summ["failed"]
        line = f"Ran tests: {summ['passed']}/{checked} passed" if checked else \
            f"Ran tests: {summ['ran']} case(s), no expected output"
        if checked and summ["ran"]:
            line += f" (+{summ['ran']} without expected)"
        _svc().push_event(user_id, "run", {
            "handout_id": handout_id, "run_id": run_id, "passed": summ["passed"],
            "failed": summ["failed"], "ran": summ["ran"], "total": summ["total"],
            "results_summary": results_summary(run_cases, results)})
        _svc().post_message(user_id, "system", line + ".", handout_id=handout_id)
    return {"run_id": run_id, "results": results, **summ}


# ---- judge: the coach grades what he submitted ------------------------------------------------
JUDGE_CASE_MAX = 3_000_000   # json chars per hidden case (a large case near the constraints)
JUDGE_EXTRA_MAX = 30
JUDGE_TIMEOUT_S = 3          # one wall timeout for the whole run; the runner has no per-case clock
JUDGE_OUT_CAP = 3_000_000    # a large hidden case's output must compare whole, not cut at 64 KB
VERDICTS = ("Accepted", "Wrong Answer", "Time Limit Exceeded", "Runtime Error")


def _expand(v):
    """{"$repeat": [value, n]} -> [value] * n; {"$range": [a, b(, step)]} -> list(range(...)).
    Lets the coach write a 100000-element case in a few characters. Recursive."""
    if isinstance(v, dict) and len(v) == 1:
        (k, a), = v.items()
        if k == "$repeat" and isinstance(a, list) and len(a) == 2 and isinstance(a[1], int):
            if not 0 <= a[1] <= 2_000_000:
                raise ValueError("$repeat: n must be 0..2000000")
            return [_expand(a[0])] * a[1]
        if k == "$range" and isinstance(a, list) and 2 <= len(a) <= 3                 and all(isinstance(x, int) for x in a):
            r = range(*a)
            if len(r) > 2_000_000:
                raise ValueError("$range: 2000000 items max")
            return list(r)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def _size(case):
    n = 0
    for part in (case.get("args"), case.get("ops")):
        vals = part.values() if isinstance(part, dict) else (part or [])
        for x in vals:
            if isinstance(x, list):
                n = max(n, len(x))
    return n


def judge(user_id, handout_id, extra_cases=None):
    """Run his latest SUBMITTED code on the panel's cases (coach + his) plus `extra_cases`
    (hidden: same format as set_tests cases, with `expected`; never stored as cases, never
    shown to him). Case values may use {"$repeat": [v, n]} and {"$range": [a, b]}.
    Returns {verdict, first_failure: {input, got, expected, error, size, hidden} | None, passed,
    total, slowest_ms, submission_id}. Verdict is the first failing case in order: a timeout is
    Time Limit Exceeded, an exception or crash Runtime Error, a wrong value Wrong Answer. The
    verdict is stored on the submission so session_summary can count it. Accepted on a handout
    with steps starts step 4 Review (service.start_review) and adds review_started: True. ValueError when there
    is no submission, no spec or no cases."""
    extra = []
    if extra_cases is not None:
        if not isinstance(extra_cases, list) or len(extra_cases) > JUDGE_EXTRA_MAX:
            raise ValueError(f"extra_cases: a list of at most {JUDGE_EXTRA_MAX}")
        for i, c in enumerate(extra_cases):
            if not isinstance(c, dict):
                raise ValueError(f"extra case {i} must be an object")
            c = _expand(json.loads(json.dumps(c)))
            c.pop("id", None)
            c.pop("source", None)
            if len(json.dumps(c)) > JUDGE_CASE_MAX:
                raise ValueError(f"extra case {i}: too large")
            extra.append(c)
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        sub = s.scalars(select(Submission).where(Submission.handout_id == h.id)
                        .order_by(Submission.id.desc()).limit(1)).first()
        if sub is None:
            raise ValueError("no submission yet")
        sp = _spec(s, h.id)
        if sp is None:
            raise ValueError("no tests set for this problem")
        mode, entry = sp.mode, sp.entry or {}
        sub_id, code = sub.id, sub.code
        panel = [dict(r.case or {}) for r in _ordered(_case_rows(s, user_id, h.id))]
    warnings = []
    if extra:
        from . import reference
        with session_scope() as s:
            number = _handout(s, user_id, handout_id).problem_number
        extra, warnings = reference.resolve_cases(number, extra, mode, where="extra case")
    cases = [dict(c, _hidden=False) for c in panel] + [dict(c, _hidden=True) for c in extra]
    if not cases:
        raise ValueError("no test cases")
    hidden = [c.pop("_hidden") for c in cases]
    runner.check(mode, entry, cases)
    results = runner.run(code, cases, mode, entry, timeout_s=JUDGE_TIMEOUT_S,
                         out_cap=JUDGE_OUT_CAP)
    verdict, first = "Accepted", None
    for i, (c, r) in enumerate(zip(cases, results)):
        if r["ok"] is not False:
            continue
        if r["error"] == runner.TIMEOUT:
            verdict = "Time Limit Exceeded"
        elif r["error"]:
            verdict = "Runtime Error"
        else:
            verdict = "Wrong Answer"
        inp = {k: c[k] for k in ("args", "ops") if k in c}
        first = {"input": _short(inp), "got": _short(r["got"]),
                 "expected": _short(r["expected"]) if "expected" in c else None,
                 "error": _short(r["error"]) if r["error"] else None,
                 "size": _size(c), "hidden": hidden[i]}
        break
    passed = sum(1 for r in results if r["ok"] is not False and not r["error"])
    ms = [r["ms"] for r in results if r.get("ms") is not None]
    out = {"verdict": verdict, "first_failure": first, "passed": passed,
           "total": len(results), "slowest_ms": max(ms) if ms else None,
           "submission_id": sub_id}
    if warnings:
        out["warnings"] = warnings
    with session_scope() as s:
        row = s.get(Submission, sub_id)
        row.verdict, row.judged = verdict, out
        s.add(TestRun(user_id=user_id, handout_id=handout_id, who="coach", code=code,
                      results=results, created_at=datetime.now()))
    if verdict == "Accepted":
        from .service import start_review      # step 4 Review (coach/steps.py); stepped only
        if start_review(user_id, handout_id):
            out["review_started"] = True
    return out


__all__ = ["set_tests", "add_user_test", "delete_user_test", "tests", "last_run", "run_tests",
           "judge", "results_summary", "TESTS_UI_KINDS"]
