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
from .models import Handout, Problem, TestCase, TestRun, TestSpec

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
        return {"ok": True, "handout_id": h.id, "count": len(cases)}


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
                "cases": [{"id": r.id, "source": r.source, **(r.case or {})} for r in rows],
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


__all__ = ["set_tests", "add_user_test", "delete_user_test", "tests", "last_run", "run_tests",
           "results_summary", "TESTS_UI_KINDS"]
