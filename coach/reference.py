"""Running the question: the coach's PRIVATE reference solution per problem, and what it powers.

The reference is the plain definition of the problem, written slow and simple on purpose (brute
force by the problem's own words), never the intended technique. He never sees it. It replaces
hand-computed answers (the coach got two hidden expected outputs wrong on 2026-10-22).

    set_reference(user_id, handout_id, code, mode, entry, options, replace, reason)
        -> {ok, number, verified, checked, passed, mismatches, replaced}
        Stored on problems.reference_*. Checked on the coach cases with `expected` in his
        Tests panel. A checked one (verified True) is only replaced with replace=true + reason.
    resolve_cases(number, cases, mode)          -> (cases, warnings)
        set_tests / judge: fills a missing `expected` from the reference, refuses a case whose
        `expected` disagrees ("your expected 99010 disagrees with the reference 99991").
    trace(user_id, handout_id, input, target, max_steps, watch, allow, reason, code)
        -> {trace_id, target, frames, truncated, result, error}   (traces table)
    trace_to_animation(user_id, handout_id, trace_id | frames, base, map, ms, post)
        -> {kind: "animate", spec, frames, visual_id?}
    playground_state / playground_run           the Problem tab's "Try your own input"
    predict / answer_predict                    "What does this return?" cards in his thread
    gallery                                     one bubble: inputs -> outputs
    card_html(card)                             the card markup (escaped)

RULE ZERO: nothing here shows him code or names a technique. Labels, captions and choices go
through the leak guards; the reference code never leaves the server.
"""
import ast
import json
import threading
import time
from datetime import datetime

from markupsafe import Markup, escape
from sqlalchemy import select

from . import runner
from .db import session_scope
from .models import (Card, CoachMessage, Event, Handout, Problem, Submission, TestCase,
                     TestRun, TestSpec, Trace)

REF_MAX_LINES = 200
REF_TIMEOUT_S = 10           # one run of the small cases; and the total for the large ones
REF_OUT_CAP = 3_000_000      # a reference output may be big (a large hidden case)
LARGE_SIZE = 1000            # list length from which a case runs on its own
INPUT_MAX = 2000             # elements in a playground / predict / gallery input
PLAY_TIMEOUT_S = 5
PLAY_EVERY_S = 1.0
GALLERY_MAX = 8
TRACE_TIMEOUT_S = 5
OPTION_KEYS = ("types", "inplace", "unordered")
TEXT_SHORT = 300


class RateLimited(ValueError):
    pass


def _svc():
    from . import service
    return service


def _handout(s, user_id, handout_id):
    h = s.get(Handout, handout_id) if handout_id is not None else None
    if h is None or h.user_id != user_id:
        raise ValueError(f"no handout {handout_id}")
    return h


def _short(v, n=TEXT_SHORT):
    t = v if isinstance(v, str) else json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return t if len(t) <= n else t[:n] + "..."


def _lit(v):
    """A value the way LeetCode prints it: [1,5,2,4,3], "abc", true, null."""
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def _ref(p):
    if p is None or not p.reference_code:
        return None
    return {"number": p.number, "code": p.reference_code, "mode": p.reference_mode or "function",
            "entry": dict(p.reference_entry or {}), "options": dict(p.reference_options or {}),
            "verified": p.reference_ok,
            "set_at": p.reference_set_at.isoformat() if p.reference_set_at else None}


def reference_of(number):
    """The stored reference of a problem {number, code, mode, entry, options, verified, set_at}
    or None. For the server and the coach only."""
    with session_scope() as s:
        return _ref(s.get(Problem, number))


def _coach_cases(s, h):
    rows = s.scalars(select(TestCase).where(TestCase.handout_id == h.id,
                                            TestCase.source == "coach").order_by(TestCase.id))
    return [dict(r.case or {}) for r in rows]


def _opts_of(case):
    return {k: case[k] for k in OPTION_KEYS if k in case}


def _options(s, p, h):
    """The case options (types, inplace, unordered) for inputs the coach or he types: the
    reference's own, else the first coach case's."""
    o = dict(p.reference_options or {})
    if o:
        return o
    for c in _coach_cases(s, h):
        o = _opts_of(c)
        if o:
            return o
    return {}


def _bare(case):
    return {k: v for k, v in case.items() if k not in ("expected", "label", "note", "id", "source")}


# ---- set_reference ---------------------------------------------------------------------------
def set_reference(user_id, handout_id, code, mode="function", entry=None, options=None,
                  replace=False, reason=""):
    """Store the reference of the handout's problem and check it on the coach cases in his
    Tests panel that carry `expected`. Returns {ok, number, verified (True | False | None = no
    example to check), checked, passed, mismatches: [{case_index, input, expected, got, error}],
    replaced}. Stored even when it mismatches (verified False): fix it and call again.
    ValueError: over 200 lines, a syntax error, no entry, or a checked one exists and replace is
    not set with a reason."""
    code = (code or "").replace("\r\n", "\n")
    if not code.strip():
        raise ValueError("empty reference")
    if len(code.splitlines()) > REF_MAX_LINES:
        raise ValueError(f"reference: {REF_MAX_LINES} lines max (keep it the plain definition)")
    try:
        compile(code, "reference.py", "exec")
    except SyntaxError as e:
        raise ValueError(f"reference: SyntaxError: {e.msg} (line {e.lineno})")
    if mode not in runner.MODES:
        raise ValueError(f"mode must be one of {runner.MODES}")
    if options is not None and not isinstance(options, dict):
        raise ValueError("options must be an object {types, inplace, unordered}")
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        p = s.get(Problem, h.problem_number)
        sp = s.get(TestSpec, h.id)
        if not entry and sp is not None and sp.mode == mode:
            entry = dict(sp.entry or {})
        if not entry:
            entry = {}
        if mode == "function" and not entry.get("method"):
            raise ValueError("entry needed: {class: \"Solution\", method: \"<name>\"}")
        if mode == "design" and not entry.get("class"):
            raise ValueError("entry needed: {class: \"<ClassName>\"}")
        had = bool(p.reference_code)
        if had and p.reference_ok is True:
            if not replace:
                raise ValueError(f"a checked reference exists for {p.number}; pass replace=true "
                                 "with a reason to change it")
            if not (reason or "").strip():
                raise ValueError("replace needs a reason")
        examples = [c for c in _coach_cases(s, h) if "expected" in c]
        if examples and sp is not None and sp.mode != mode:
            raise ValueError(f"the tests are in {sp.mode} mode, the reference in {mode}")
        opts = dict(options) if options else {}
        if not opts:
            for c in _coach_cases(s, h):
                opts = _opts_of(c)
                if opts:
                    break
        opts = {k: v for k, v in opts.items() if k in OPTION_KEYS}
        number = p.number
    runner.check(mode, entry, examples)
    results = runner.run(code, examples, mode, entry, timeout_s=REF_TIMEOUT_S) if examples else []
    mismatches = []
    for c, r in zip(examples, results):
        if r["ok"] is not True:
            mismatches.append({"case_index": r["case_index"],
                               "input": _short({k: c[k] for k in ("args", "ops") if k in c}),
                               "expected": _short(c.get("expected")),
                               "got": _short(r["got"]), "error": r["error"]})
    verified = None if not examples else not mismatches
    with session_scope() as s:
        p = s.get(Problem, number)
        p.reference_code, p.reference_mode = code, mode
        p.reference_entry, p.reference_options = json.loads(json.dumps(entry)), opts
        p.reference_set_at, p.reference_ok = datetime.now(), verified
        p.reference_reason = (reason or "").strip() or None
        _svc().emit(user_id, "statement", handout_id, s)      # the page shows the playground
    return {"ok": True, "number": number, "verified": verified, "checked": len(examples),
            "passed": len(examples) - len(mismatches), "mismatches": mismatches,
            "replaced": had}


# ---- expected outputs from the reference ------------------------------------------------------
def _size(case):
    n = 0
    for part in (case.get("args"), case.get("ops")):
        vals = part.values() if isinstance(part, dict) else (part or [])
        for x in vals:
            if isinstance(x, list):
                n = max(n, len(x))
    return n


def run_reference(ref, cases, timeout_s=REF_TIMEOUT_S):
    """Run the reference on cases (expected ignored). Small cases share one run; each large
    one runs alone, all large ones sharing `timeout_s`. Returns runner rows, in order."""
    bare = [_bare(c) for c in cases]
    out = [None] * len(bare)
    small = [i for i, c in enumerate(bare) if _size(c) <= LARGE_SIZE]
    large = [i for i, c in enumerate(bare) if _size(c) > LARGE_SIZE]
    if small:
        rows = runner.run(ref["code"], [bare[i] for i in small], ref["mode"], ref["entry"],
                          timeout_s=timeout_s, out_cap=REF_OUT_CAP)
        for i, r in zip(small, rows):
            out[i] = r
    budget = float(timeout_s)
    for i in large:
        if budget < 0.5:
            out[i] = {"case_index": i, "ok": False, "got": None, "error": runner.TIMEOUT,
                      "stdout": "", "ms": None}
            continue
        t0 = time.monotonic()
        out[i] = runner.run(ref["code"], [bare[i]], ref["mode"], ref["entry"],
                            timeout_s=budget, out_cap=REF_OUT_CAP)[0]
        budget -= time.monotonic() - t0
    for i, r in enumerate(out):
        r["case_index"] = i
    return out


def resolve_cases(number, cases, mode, where="case"):
    """Fill / cross-check `expected` with the problem's reference. Returns (cases, warnings).
    No reference: unchanged. A case without `expected` gets the reference output; one with
    `expected` must agree (ValueError otherwise). The reference too slow on a large case: kept
    unchecked when it has an explicit `expected`, else dropped with a warning. The reference
    raising on an input: ValueError (the input breaks the problem's rules, or the reference is
    wrong)."""
    ref = reference_of(number)
    if ref is None or not cases:
        return cases, []
    if ref["mode"] != mode:
        raise ValueError(f"the reference is in {ref['mode']} mode, the cases in {mode}")
    rows = run_reference(ref, cases)
    out, warnings, agreed = [], [], False
    for i, (c, r) in enumerate(zip(cases, rows)):
        c = dict(c)
        if r["error"] == runner.TIMEOUT:
            if "expected" in c:
                warnings.append(f"{where} {i}: the reference is too slow for a case this large "
                                f"(size {_size(c)}); your expected is kept unchecked")
                out.append(c)
            else:
                warnings.append(f"{where} {i} dropped: the reference is too slow for a case this "
                                f"large (size {_size(c)}); give it an explicit expected")
            continue
        if r["error"]:
            inp = {k: c[k] for k in ("args", "ops") if k in c}
            raise ValueError(
                f"{where} {i}: the reference fails on this input: {r['error']}. Input: "
                f"{_short(inp, 200)}. Fix set_reference to handle this input (e.g. empty / "
                "single element) and resend it with replace=true and a reason, or give this "
                "case an explicit expected")
        if "expected" in c:
            if not runner.compare(r["got"], c["expected"], c.get("unordered", False)):
                raise ValueError(f"{where} {i}: your expected {_short(c['expected'], 120)} "
                                 f"disagrees with the reference {_short(r['got'], 120)} (if the "
                                 "reference is wrong, fix it: set_reference replace=true)")
            agreed = True
        else:
            c["expected"] = r["got"]
        out.append(c)
    if agreed and ref["verified"] is None:
        with session_scope() as s:
            s.get(Problem, number).reference_ok = True
    return out, warnings


# ---- inputs the coach or he gives -------------------------------------------------------------
def _count(v):
    if isinstance(v, list):
        return len(v) + sum(_count(x) for x in v if isinstance(x, (list, dict)))
    if isinstance(v, dict):
        return len(v) + sum(_count(x) for x in v.values() if isinstance(x, (list, dict)))
    if isinstance(v, str):
        return len(v)
    return 1


def _case(ref_mode, inp, opts):
    """An input -> a runner case. Function mode: {name: value} or {"args": {...}, types...}.
    Design: {"ops": [...], "args": [...]}. Input size capped at INPUT_MAX elements."""
    if not isinstance(inp, dict):
        raise ValueError("input must be an object: {name: value} (or {ops, args})")
    if ref_mode == "design":
        if "ops" not in inp:
            raise ValueError("design input is {ops: [...], args: [[...], ...]}")
        case = {"ops": inp.get("ops"), "args": inp.get("args", [])}
    elif isinstance(inp.get("args"), dict):
        case = {"args": inp["args"]}
        case.update({k: inp[k] for k in OPTION_KEYS if k in inp})
    else:
        case = {"args": {k: v for k, v in inp.items() if k not in OPTION_KEYS + ("label",)}}
    for k, v in (opts or {}).items():
        case.setdefault(k, v)
    if _count(case.get("args")) + _count(case.get("ops") or []) > INPUT_MAX:
        raise ValueError(f"input too large: {INPUT_MAX} elements max")
    return json.loads(json.dumps(case))


def _ref_for(s, user_id, handout_id):
    h = _handout(s, user_id, handout_id)
    p = s.get(Problem, h.problem_number)
    ref = _ref(p)
    if ref is None:
        raise ValueError("no reference for this problem yet: set_reference first")
    return h, p, ref


def _input_text(case):
    if "ops" in case:
        return "ops = " + _short(case.get("ops"), 200) + "\nargs = " + _short(case.get("args"), 200)
    return ", ".join(f"{k} = {_short(v, 200)}" for k, v in (case.get("args") or {}).items())


def _out_type(case):
    t = case.get("types") or {}
    return t.get(case.get("inplace") or "return")


def _draw(kind, spec):
    from . import viz
    try:
        return str(viz.render(kind, spec))
    except (ValueError, TypeError, KeyError):
        return ""


def value_html(value, type_name=None):
    """A drawing of an output value: ListNode -> list, TreeNode -> tree, list of lists ->
    grid, flat list of 2+ scalars -> array. "" when there is nothing to draw."""
    if not isinstance(value, list):
        return ""
    if type_name == "ListNode":
        return _draw("list", {"values": value})
    if type_name == "TreeNode":
        return _draw("tree", {"values": value})
    if value and all(isinstance(r, list) for r in value):
        return _draw("grid", {"rows": value})
    if len(value) >= 2 and not any(isinstance(x, (list, dict)) for x in value):
        return _draw("array", {"values": value})
    return ""


def input_html(case):
    if "ops" in case:
        return ""
    from .tests_api import input_html as _ih
    return _ih(case)


def _run_one(ref, case, timeout_s=PLAY_TIMEOUT_S):
    r = runner.run(ref["code"], [_bare(case)], ref["mode"], ref["entry"], timeout_s=timeout_s,
                   out_cap=REF_OUT_CAP)[0]
    return r["got"], r["error"]


# ---- tracer ------------------------------------------------------------------------------------
HIS_TRACE_REFUSED = ("his code is traced only after he asked for help (I'm stuck), a failed run "
                     "or a failed judge; else pass allow=true with a reason")


def his_trace_allowed(s, h):
    for e in s.scalars(select(Event).where(Event.user_id == h.user_id, Event.kind == "stuck")
                       .order_by(Event.id.desc()).limit(200)):
        if (e.payload or {}).get("handout_id") == h.id:
            return True
    for r in s.scalars(select(TestRun).where(TestRun.handout_id == h.id, TestRun.who == "user")):
        if any(x.get("ok") is False for x in (r.results or [])):
            return True
    for sub in s.scalars(select(Submission).where(Submission.handout_id == h.id)):
        if sub.verdict and sub.verdict != "Accepted":
            return True
    return False


def _his_code(s, h):
    from .models import CodeDraft
    d = s.scalars(select(CodeDraft).where(CodeDraft.handout_id == h.id)).first()
    if d is not None and (d.code or "").strip():
        return d.code
    sub = s.scalars(select(Submission).where(Submission.handout_id == h.id)
                    .order_by(Submission.id.desc())).first()
    return sub.code if sub is not None and (sub.code or "").strip() else None


def trace(user_id, handout_id, input, target="reference", max_steps=60, watch=None,
          allow=False, reason="", code=None):
    """Trace one input. target "reference": the stored reference. target "his": his current
    draft (or `code`), only after a `stuck` event, a failed run or a failed judge for this
    handout, or allow=true with a reason. Returns {trace_id, target, frames: [{line, func,
    locals, note}], truncated, result, error}."""
    if target not in ("reference", "his"):
        raise ValueError("target must be reference or his")
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        p = s.get(Problem, h.problem_number)
        ref = _ref(p)
        opts = _options(s, p, h)
        sp = s.get(TestSpec, h.id)
        if target == "his":
            if not his_trace_allowed(s, h) and not (allow and (reason or "").strip()):
                raise ValueError(HIS_TRACE_REFUSED)
            code = code if (code or "").strip() else _his_code(s, h)
            if not code:
                raise ValueError("no code yet")
            if sp is not None:
                mode, entry = sp.mode, dict(sp.entry or {})
            elif ref is not None:
                mode, entry = ref["mode"], ref["entry"]
            else:
                raise ValueError("no tests set for this problem: the call spec is unknown")
        else:
            if ref is None:
                raise ValueError("no reference for this problem yet: set_reference first")
            code, mode, entry = ref["code"], ref["mode"], ref["entry"]
    case = _case(mode, input, opts)
    r = runner.trace(code, case, mode, entry, max_steps=max_steps, watch=watch,
                     timeout_s=TRACE_TIMEOUT_S)
    with session_scope() as s:
        row = Trace(user_id=user_id, handout_id=handout_id, target=target, input=case,
                    frames=r["frames"], result={"result": r["result"], "error": r["error"],
                                                "truncated": r["truncated"]},
                    reason=(reason or "").strip(), created_at=datetime.now())
        s.add(row)
        s.flush()
        tid = row.id
    return {"trace_id": tid, "target": target, "frames": r["frames"],
            "truncated": r["truncated"], "result": r["result"], "error": r["error"]}


def get_trace(user_id, trace_id):
    with session_scope() as s:
        t = s.get(Trace, trace_id)
        if t is None or t.user_id != user_id:
            raise ValueError(f"no trace {trace_id}")
        return {"trace_id": t.id, "handout_id": t.handout_id, "target": t.target,
                "input": t.input, "frames": t.frames or [], **(t.result or {})}


# ---- trace -> animate spec ---------------------------------------------------------------------
ANIM_BASES = ("array", "list", "tree", "grid")


def _is_node(x):
    return isinstance(x, dict) and x.get("$type") in ("ListNode", "TreeNode")


def _plainval(x):
    if _is_node(x):
        if "val" in x:
            return x["val"]
        vals = x.get("values") or []
        return vals[0] if vals else None
    if isinstance(x, (dict, list)):
        return _short(x, 40)
    return x


def _nid(x):
    if not _is_node(x):
        return None
    if "id" in x:
        return x["id"]
    ids = x.get("ids") or []
    return ids[0] if ids else None


def _seq(v):
    """A local -> (values, ids, cycle_to) for drawing, or None if it is not a sequence."""
    if _is_node(v) and "values" in v:
        return list(v.get("values") or []), list(v.get("ids") or []), v.get("cycle_to")
    if isinstance(v, list):
        return [_plainval(x) for x in v], [_nid(x) for x in v], None
    return None


def _index(pv, ids, n):
    if isinstance(pv, bool):
        return None
    if isinstance(pv, int):
        return pv if 0 <= pv < n else None
    nid = _nid(pv)
    if nid is not None and nid in ids:
        return ids.index(nid)
    return None


def _fmt(v):
    if _is_node(v):
        return "node " + json.dumps(_plainval(v))
    return _short(v, 40)


def _frame_spec(base, f, m):
    loc = f.get("locals") or {}
    var = m.get("values")
    if var not in loc:
        return None
    v = loc[var]
    ptrs = m.get("pointers") or {}
    if base == "grid":
        if not (isinstance(v, list) and all(isinstance(r, list) for r in v)):
            return None
        rows = [[_plainval(x) for x in r] for r in v]
        spec = {"rows": rows}
        cell = m.get("cell")
        if isinstance(cell, list) and len(cell) == 2:
            r, c = loc.get(cell[0]), loc.get(cell[1])
            if isinstance(r, int) and isinstance(c, int) and not isinstance(r, bool) \
                    and 0 <= r < len(rows) and 0 <= c < len(rows[r]):
                spec["highlight"] = [[r, c]]
        return spec
    sq = _seq(v)
    if sq is None:
        return None
    values, ids, cyc = sq
    if base == "tree":
        hl = []
        for _, pv in ptrs.items():
            i = _index(loc.get(pv), ids, len(values))
            if i is not None:
                hl.append(i)
        spec = {"values": values}
        if hl:
            spec["highlight"] = sorted(set(hl))
        return spec
    spec = {"values": values}
    at = {}
    for tag, pv in ptrs.items():
        if pv in loc:
            i = _index(loc[pv], ids, len(values))
            if i is not None:
                at[str(tag)] = i
    if at:
        spec["pointers"] = at
    hl = m.get("highlight")
    if isinstance(hl, str) and hl in loc:
        i = _index(loc[hl], ids, len(values))
        if i is not None:
            spec["highlight"] = [i]
    if base == "list" and cyc is not None:
        spec["cycle_to"] = cyc
    return spec


def _caption(f, m):
    bits = [f"line {f.get('line')}"]
    note = f.get("note") or ""
    if note and m.get("notes", True):
        bits.append(note)
    loc = f.get("locals") or {}
    shown = [f"{n} = {_fmt(loc[n])}" for n in (m.get("show") or []) if n in loc]
    if shown:
        bits.append(", ".join(shown))
    return _short(" · ".join(bits), 200)


def trace_to_animation(user_id, handout_id, trace_id=None, frames=None, base="array", map=None,
                       ms=1200, post=False):
    """Turn trace frames into a ready `animate` spec. map: {values: "<var drawn>", pointers:
    {tag: "<var holding an index or a node>"}, show: ["<var>", ...] (in the caption), lines:
    [n, ...] (keep only these lines), highlight: "<var>", cell: ["<row var>", "<col var>"]
    (grid), keep_all: false (consecutive identical drawings are merged), notes: true}.
    At most 40 frames (evenly sampled, first and last kept). post=true posts it through
    show_visual (leak guard as usual). Returns {kind, spec, frames, visual_id?}."""
    if base not in ANIM_BASES:
        raise ValueError(f"base must be one of {', '.join(ANIM_BASES)}")
    m = dict(map or {})
    if not isinstance(m.get("values"), str):
        raise ValueError("map.values: the name of the variable to draw")
    if trace_id is not None:
        frames = get_trace(user_id, trace_id)["frames"]
    if not isinstance(frames, list) or not frames:
        raise ValueError("give trace_id or frames")
    lines = m.get("lines")
    out = []
    for f in frames:
        if not isinstance(f, dict):
            continue
        if lines and f.get("line") not in lines:
            continue
        spec = _frame_spec(base, f, m)
        if spec is None:
            continue
        if out and not m.get("keep_all") and out[-1]["spec"] == spec:
            out[-1]["caption"] = _caption(f, m)
            continue
        out.append({"spec": spec, "caption": _caption(f, m)})
    if len(out) < 2:
        raise ValueError("fewer than 2 distinct frames: pick another variable or other lines")
    if len(out) > 40:
        step = (len(out) - 1) / 39
        out = [out[round(k * step)] for k in range(40)]
    spec = {"base": base, "frames": out, "ms": ms, "loop": False}
    from . import viz
    viz.render("animate", spec)             # ValueError on a bad spec
    res = {"kind": "animate", "spec": spec, "frames": len(out)}
    if post:
        v = _svc().show_visual(user_id, handout_id, "animate", spec)
        res["visual_id"] = v["id"]
    return res


# ---- playground (Problem tab) -----------------------------------------------------------------
_LAST_PLAY = {}
_PLAY_LOCK = threading.Lock()


def _params(ref):
    """Argument names of the reference's entry method, from its source (never executed)."""
    try:
        tree = ast.parse(ref["code"])
    except SyntaxError:
        return []
    cls = ref["entry"].get("class") or "Solution"
    meth = ref["entry"].get("method")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == meth:
                    names = [a.arg for a in fn.args.posonlyargs + fn.args.args]
                    return [n for n in names[1:] if n != "pos"]
    return []


def playground_state(user_id, handout_id):
    """{ready, mode, params: [names], example: {name: text}, ops, args} for the Problem tab.
    ready False until the problem has a reference (or when a name would leak)."""
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        p = s.get(Problem, h.problem_number)
        ref = _ref(p)
        if ref is None:
            return {"ready": False}
        cases = _coach_cases(s, h)
        title = p.title
        svc = _svc()
        first = next((c for c in cases if ("ops" in c) == (ref["mode"] == "design")), None)
        if ref["mode"] == "design":
            ex_ops = _lit(first["ops"]) if first else "[]"
            ex_args = _lit(first.get("args", [])) if first else "[]"
            if svc.leak_words(ex_ops, title, s):
                return {"ready": False}
            return {"ready": True, "mode": "design", "params": [], "example": {},
                    "ops": ex_ops, "args": ex_args}
        params = _params(ref)
        if not params and first:
            params = [k for k in (first.get("args") or {}) if k != "pos"]
        if not params or svc.leak_words(" ".join(params), title, s):
            return {"ready": False}
        args = (first or {}).get("args") or {}
        example = {n: _lit(args[n]) if n in args else ""
                   for n in params}
        if "pos" in args and (_options(s, p, h).get("types") or {}):
            example["pos"] = _lit(args["pos"])
            params = params + ["pos"]
        return {"ready": True, "mode": "function", "params": params, "example": example}


def literal(text, name="value"):
    """JSON / LeetCode literal -> value: [1,2,3], "abc", 5, true, null. Also Python's True,
    False, None and single quotes."""
    t = (text or "").strip()
    if not t:
        raise ValueError(f"{name}: empty")
    try:
        return json.loads(t)
    except ValueError:
        pass
    try:
        v = ast.literal_eval(t)
        return json.loads(json.dumps(v))
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        raise ValueError(f"{name}: write it like [1,2,3], \"abc\" or 5")


def playground_run(user_id, handout_id, fields, now=None):
    """Run the REFERENCE (never his code) on what he typed. fields: {name: text} (function) or
    {ops: text, args: text} (design). One run per second per user, INPUT_MAX elements. Pushes
    `playground` {handout_id, input, output, error} to the coach (no chat line). Returns
    {output, output_text, error, input_html, output_html}."""
    now = time.monotonic() if now is None else now
    with _PLAY_LOCK:
        last = _LAST_PLAY.get(user_id)
        if last is not None and now - last < PLAY_EVERY_S:
            raise RateLimited("one run per second")
        _LAST_PLAY[user_id] = now
    if not isinstance(fields, dict):
        raise ValueError("bad input")
    with session_scope() as s:
        h, p, ref = _ref_for(s, user_id, handout_id)
        opts = _options(s, p, h)
    if ref["mode"] == "design":
        inp = {"ops": literal(fields.get("ops"), "ops"), "args": literal(fields.get("args"), "args")}
    else:
        state = playground_state(user_id, handout_id)
        inp = {n: literal(fields.get(n), n) for n in state.get("params", [])}
    case = _case(ref["mode"], inp, opts)
    try:
        runner.check(ref["mode"], ref["entry"], [case])
    except ValueError as e:
        raise ValueError(str(e).replace("case 0: ", ""))
    got, err = _run_one(ref, case)
    shown = case["args"] if ref["mode"] != "design" else {"ops": case["ops"], "args": case["args"]}
    _svc().push_event(user_id, "playground", {"handout_id": handout_id, "input": shown,
                                              "output": got, "error": err})
    if err:
        msg = ("Too slow on an input this size." if err == runner.TIMEOUT else
               "No output: this input breaks the problem's rules.")
        return {"output": None, "output_text": "", "error": msg, "input_html": input_html(case),
                "output_html": ""}
    return {"output": got, "output_text": _lit(got), "error": None,
            "input_html": input_html(case), "output_html": value_html(got, _out_type(case))}


# ---- cards: predict, gallery ------------------------------------------------------------------
def _post_card(s, user_id, h, kind, data, content):
    c = Card(user_id=user_id, handout_id=h.id, kind=kind, data=data, created_at=datetime.now())
    s.add(c)
    s.flush()
    m = CoachMessage(user_id=user_id, handout_id=h.id, role="coach", content=content, card_id=c.id)
    s.add(m)
    s.flush()
    _svc().emit(user_id, "message", m.id, s)
    return c.id, m.id


def _guard(s, user_id, h, texts):
    svc = _svc()
    text = "\n".join(t for t in texts if t)
    if not text.strip():
        return
    bad = svc.leak_words(text, s.get(Problem, h.problem_number).title, s, h)
    if bad:
        raise ValueError("leak: " + ", ".join(bad))
    svc.rule_zero_check(s, user_id, text, h)


def _choice_value(c):
    if isinstance(c, str):
        try:
            return literal(c)
        except ValueError:
            return c
    return c


def predict(user_id, handout_id, input, choices=None):
    """Post "What does this return?" with the input (and its drawing). choices: None (free
    text) or 2-4 values, exactly one equal to the reference output. Returns {ok, id, output,
    message_id}; the output is for you, the card hides it until he answers."""
    with session_scope() as s:
        h, p, ref = _ref_for(s, user_id, handout_id)
        opts = _options(s, p, h)
    case = _case(ref["mode"], input, opts)
    runner.check(ref["mode"], ref["entry"], [case])
    got, err = _run_one(ref, case)
    if err:
        raise ValueError(f"the reference fails on this input: {err}")
    shown = None
    if choices is not None:
        if not isinstance(choices, list) or not 2 <= len(choices) <= 4:
            raise ValueError("choices: 2 to 4 values")
        vals = [_choice_value(c) for c in choices]
        hits = [i for i, v in enumerate(vals) if runner.compare(v, got, case.get("unordered", False))]
        if not hits:
            raise ValueError(f"none of the choices is the reference output {_short(got, 120)}")
        if len(hits) > 1:
            raise ValueError("two choices equal the reference output")
        shown = [c if isinstance(c, str) else _lit(c) for c in choices]
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        _guard(s, user_id, h, shown or [])
        data = {"case": case, "output": got, "choices": shown, "input_text": _input_text(case)}
        cid, mid = _post_card(s, user_id, h, "predict", data, "What does this return?")
    return {"ok": True, "id": cid, "output": got, "message_id": mid}


def _correct(answer, output, unordered=False):
    t = (answer or "").strip()
    try:
        v = literal(t)
    except ValueError:
        v = t
    if runner.compare(v, output, unordered):
        return True
    return isinstance(output, str) and t.strip("\"'") == output


def answer_predict(user_id, card_id, answer=None, choice=None):
    """His answer to a predict card (free text, or the index of a choice). Checked against the
    reference output; pushes `prediction` {id, handout_id, correct, answer, output}. Returns
    {ok, correct, html}. ValueError: no such card, already answered, empty answer."""
    with session_scope() as s:
        c = s.get(Card, card_id)
        if c is None or c.user_id != user_id or c.kind != "predict":
            raise ValueError(f"no card {card_id}")
        if c.answered_at is not None:
            raise ValueError("already answered")
        d = dict(c.data or {})
        if choice is not None and str(choice).strip() != "":
            try:
                k = int(choice)
            except (TypeError, ValueError):
                raise ValueError("bad choice")
            ch = d.get("choices") or []
            if not 0 <= k < len(ch):
                raise ValueError("bad choice")
            answer = ch[k]
        answer = (answer or "").strip()
        if not answer:
            raise ValueError("type an answer first")
        if len(answer) > 2000:
            raise ValueError("answer: 2000 characters max")
        ok = _correct(answer, d.get("output"), (d.get("case") or {}).get("unordered", False))
        c.answer, c.correct, c.answered_at = answer, ok, datetime.now()
        s.flush()
        html = card_html(c)
        hid = c.handout_id
        out = d.get("output")
    _svc().push_event(user_id, "prediction", {"id": card_id, "handout_id": hid, "correct": ok,
                                              "answer": answer, "output": out})
    return {"ok": True, "correct": ok, "html": str(html)}


def gallery(user_id, handout_id, inputs, caption=""):
    """Run the reference on each input and post ONE bubble: input (drawn when structural) ->
    output. inputs: 1-8, each {name: value} or {args: {...}, label} (design: {ops, args,
    label}). Labels and caption are leak-guarded. Returns {ok, id, rows: [{label, input, output,
    error}], message_id}."""
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= GALLERY_MAX:
        raise ValueError(f"inputs: 1 to {GALLERY_MAX}")
    caption = (caption or "").strip()
    with session_scope() as s:
        h, p, ref = _ref_for(s, user_id, handout_id)
        opts = _options(s, p, h)
        labels = []
        for i, inp in enumerate(inputs):
            lab = inp.get("label") if isinstance(inp, dict) else None
            if lab is not None and not isinstance(lab, str):
                raise ValueError(f"input {i}: label must be a string")
            labels.append((lab or "").strip())
        _guard(s, user_id, h, labels + [caption])
    cases = [_case(ref["mode"], inp, opts) for inp in inputs]
    runner.check(ref["mode"], ref["entry"], cases)
    rows = runner.run(ref["code"], [_bare(c) for c in cases], ref["mode"], ref["entry"],
                      timeout_s=REF_TIMEOUT_S, out_cap=REF_OUT_CAP)
    data_rows, out_rows = [], []
    for lab, c, r in zip(labels, cases, rows):
        data_rows.append({"label": lab, "case": c, "input_text": _input_text(c),
                          "output": r["got"], "error": bool(r["error"])})
        out_rows.append({"label": lab, "input": c.get("args", {"ops": c.get("ops"),
                                                                "args": c.get("args")}),
                         "output": r["got"], "error": r["error"]})
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        cid, mid = _post_card(s, user_id, h, "gallery", {"rows": data_rows, "caption": caption},
                              caption or "Edge cases")
    return {"ok": True, "id": cid, "rows": out_rows, "message_id": mid}


# ---- card markup -------------------------------------------------------------------------------
def _code(text):
    return Markup('<code class="rq-code">{}</code>').format(text)


def _drawing(html):
    return Markup('<div class="rq-draw">{}</div>').format(Markup(html)) if html else ""


def card_html(c):
    d = c.data or {}
    if c.kind == "hint":
        from . import hints
        return hints.card_html(c)
    if c.kind == "gallery":
        trs = []
        for r in d.get("rows") or []:
            case = r.get("case") or {}
            lab = Markup('<span class="rq-label">{}</span>').format(r["label"]) if r.get("label") else ""
            if r.get("error"):
                out = Markup('<span class="rq-err">not a valid input</span>')
            else:
                out = Markup("{}{}").format(_code(_lit(r.get("output"))),
                                            _drawing(value_html(r.get("output"), _out_type(case))))
            trs.append(Markup('<tr><td class="rq-in">{}{}{}</td><td class="rq-arrow" '
                              'aria-hidden="true">&rarr;</td><td class="rq-out">{}</td></tr>')
                       .format(lab, _code(r.get("input_text") or ""), _drawing(input_html(case)),
                               out))
        cap = Markup('<p class="rq-cap">{}</p>').format(d["caption"]) if d.get("caption") else ""
        return Markup('<div class="rq rq-gallery" data-card="{}">{}<table class="rq-table">'
                      '<thead><tr><th>Input</th><th></th><th>Output</th></tr></thead>'
                      '<tbody>{}</tbody></table></div>').format(c.id, cap, Markup("").join(trs))
    case = d.get("case") or {}
    head = Markup('<p class="rq-q">What does this return?</p><div class="rq-input">{}{}</div>') \
        .format(_code(d.get("input_text") or ""), _drawing(input_html(case)))
    if c.answered_at is None:
        ch = d.get("choices")
        if ch:
            body = Markup('<div class="rq-choices">{}</div>').format(Markup("").join(
                Markup('<button class="rq-choice" type="submit" name="choice" value="{}">{}'
                       '</button>').format(i, _code(t)) for i, t in enumerate(ch)))
        else:
            body = Markup('<div class="rq-free"><label class="sr-only" for="rq-a-{id}">Your '
                          'answer</label><input class="rq-answer" id="rq-a-{id}" name="answer" '
                          'autocomplete="off" spellcheck="false" placeholder="Your answer, like '
                          '[1,2,3]"><button class="btn btn-primary btn-sm rq-check" '
                          'type="submit">Check</button></div>').format(id=c.id)
        return Markup('<div class="rq rq-predict" data-card="{id}" data-state="open">{head}'
                      '<form class="rq-form" data-card="{id}" autocomplete="off">{body}'
                      '<p class="rq-msg" role="status" aria-live="polite"></p></form></div>') \
            .format(id=c.id, head=head, body=body)
    ok = bool(c.correct)
    out = d.get("output")
    res = Markup('<p class="rq-res {cls}"><span class="rq-mark" aria-hidden="true">{mark}</span>'
                 '{word}</p><dl class="rq-dl"><dt>Your answer</dt><dd>{ans}</dd><dt>Output</dt>'
                 '<dd>{out}</dd></dl>{draw}').format(
        cls="rq-right" if ok else "rq-wrong", mark="✓" if ok else "✗",
        word="Right" if ok else "Not this time", ans=_code(c.answer or ""),
        out=_code(_lit(out)),
        draw=_drawing(value_html(out, _out_type(case))))
    return Markup('<div class="rq rq-predict" data-card="{}" data-state="{}">{}{}</div>').format(
        c.id, "right" if ok else "wrong", head, res)


def card_for_message(s, card_id):
    c = s.get(Card, card_id) if card_id is not None else None
    if c is None:
        return None
    return {"id": c.id, "kind": c.kind, "html": str(card_html(c)),
            "answered": c.answered_at is not None, "correct": c.correct}
