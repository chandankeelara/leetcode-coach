"""Local test runner: run the learner's Python against test cases in a SUBPROCESS.

    run(code, cases, mode, entry, timeout_s=3) -> [{case_index, ok, got, expected, error,
                                                     stdout, ms}]

The learner's code is never imported into the server process. Each run writes a small
harness plus an input file into a fresh temp dir and starts `python -I harness.py` there
(base interpreter, env stripped to PATH/SYSTEMROOT, stdin closed, one wall timeout for the
whole run). The harness exec's the code once, then runs every case and writes one JSON line
per case to a results file it opened before the learner's code ran. Comparison happens here,
in the server, on plain JSON.

Modes
  function  case {"args": {"nums": [1,1,1], "k": 2}, "expected": 2}
            entry {"class": "Solution", "method": "subarraySum"}
            Solution() per case, method(**args).
  design    case {"ops": ["StockSpanner","next"], "args": [[],[100]], "expected": [null,1]}
            entry {"class": "StockSpanner"}; the first op constructs, the rest are method
            calls; output has null for the constructor.

Case options
  "types":     {"head": "ListNode", "root": "TreeNode", "return": "TreeNode"} -- list <->
               ListNode, level-order list <-> TreeNode, for args and the return value
               (a returned node object is converted even without "return").
  "unordered": true sorts the top-level list before comparing; "deep" sorts every level.
  "inplace":   "nums" -- compare that argument after the call instead of the return value.
  "expected":  optional; without it the case only runs (ok = None).
Floats compare with 1e-5 absolute tolerance; bool never equals int.
"""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

MODES = ("function", "design")
OUT_CAP = 64 * 1024            # learner stdout and each `got`, per run
READ_CAP = 1024 * 1024         # how much of the results file the server reads
FLOAT_TOL = 1e-5
TIMEOUT = "timeout"

HARNESS = r'''
import io, json, math, sys, threading, time, traceback

OUT_CAP = %(out_cap)d
_out = open("results.jsonl", "w", encoding="utf-8")


def emit(d):
    _out.write(json.dumps(d) + "\n")
    _out.flush()


class Capped(io.TextIOBase):
    def __init__(self):
        self.buf, self.n, self.cut = [], 0, False

    def writable(self):
        return True

    def write(self, s):
        s = str(s)
        room = OUT_CAP - self.n
        if room <= 0:
            self.cut = True
            return len(s)
        if len(s) > room:
            self.cut = True
        self.buf.append(s[:room])
        self.n += min(len(s), room)
        return len(s)

    def take(self):
        t = "".join(self.buf)
        self.buf = []
        return t + ("\n[output truncated]" if self.cut and t else "")


class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next


class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right


PRELUDE = """
from typing import *
from collections import *
from heapq import *
from bisect import *
from itertools import *
from functools import *
import math, heapq, bisect, itertools, functools, collections, string, re, random, operator
from math import inf
"""


def _new(cls, val):
    try:
        return cls(val)
    except TypeError:
        n = cls.__new__(cls)
        n.val = val
        return n


def to_list_node(cls, xs):
    head = cur = None
    for v in xs or []:
        n = _new(cls, v)
        n.next = None
        if head is None:
            head = cur = n
        else:
            cur.next = n
            cur = n
    return head


def to_tree_node(cls, xs):
    if not xs or xs[0] is None:
        return None
    def mk(v):
        n = _new(cls, v)
        n.left = n.right = None
        return n
    root = mk(xs[0])
    q, i = [root], 1
    qi = 0
    while qi < len(q) and i < len(xs):
        node = q[qi]
        qi += 1
        if i < len(xs) and xs[i] is not None:
            node.left = mk(xs[i])
            q.append(node.left)
        i += 1
        if i < len(xs) and xs[i] is not None:
            node.right = mk(xs[i])
            q.append(node.right)
        i += 1
    return root


def from_list_node(n):
    out, seen = [], 0
    while n is not None:
        out.append(plain(getattr(n, "val", None)))
        n = getattr(n, "next", None)
        seen += 1
        if seen > 100000:
            out.append("...cycle?")
            break
    return out


def from_tree_node(root):
    if root is None:
        return []
    out, q, qi = [], [root], 0
    while qi < len(q):
        n = q[qi]
        qi += 1
        if n is None:
            out.append(None)
            continue
        out.append(plain(getattr(n, "val", None)))
        q.append(getattr(n, "left", None))
        q.append(getattr(n, "right", None))
        if len(q) > 200000:
            break
    while out and out[-1] is None:
        out.pop()
    return out


def is_tree(o):
    return hasattr(o, "val") and (hasattr(o, "left") or hasattr(o, "right"))


def is_list(o):
    return hasattr(o, "val") and hasattr(o, "next")


def plain(o, depth=0):
    if o is None or isinstance(o, (bool, int, str)):
        return o
    if isinstance(o, float):
        if math.isnan(o):
            return "nan"
        if math.isinf(o):
            return "inf" if o > 0 else "-inf"
        return o
    if depth > 50:
        return repr(o)[:200]
    if isinstance(o, (list, tuple)):
        return [plain(x, depth + 1) for x in o]
    if isinstance(o, (set, frozenset)):
        xs = [plain(x, depth + 1) for x in o]
        try:
            return sorted(xs)
        except TypeError:
            return xs
    if isinstance(o, dict):
        return {str(k): plain(v, depth + 1) for k, v in o.items()}
    if is_list(o):
        return from_list_node(o)
    if is_tree(o):
        return from_tree_node(o)
    return repr(o)[:500]


def convert_in(ns, kind, v):
    if kind == "ListNode":
        return to_list_node(ns.get("ListNode", ListNode), v)
    if kind == "TreeNode":
        return to_tree_node(ns.get("TreeNode", TreeNode), v)
    return v


def convert_out(kind, v):
    if kind == "ListNode":
        return from_list_node(v)
    if kind == "TreeNode":
        return from_tree_node(v)
    return plain(v)


def where(tb):
    line = None
    for fr in traceback.extract_tb(tb):
        if fr.filename == "solution.py":
            line = fr.lineno
    return line


def err_text(e):
    line = where(e.__traceback__)
    msg = type(e).__name__ + (": " + str(e) if str(e) else "")
    return msg + (" (line %%d)" %% line if line else "")


def capped_got(v):
    s = json.dumps(v)
    if len(s) > OUT_CAP:
        return s[:OUT_CAP] + "...[truncated]"
    return v


def run_function(ns, entry, i, case, cap):
    cls = ns.get(entry.get("class") or "Solution")
    if cls is None:
        raise NameError("no class %%s" %% (entry.get("class") or "Solution"))
    types = case.get("types") or {}
    args = {k: convert_in(ns, types.get(k), v) for k, v in (case.get("args") or {}).items()}
    obj = cls()
    fn = getattr(obj, entry.get("method") or "", None)
    if fn is None:
        raise AttributeError("Solution has no method %%s" %% entry.get("method"))
    # LeetCode convention (141, 142): `pos` only describes the test list. Link the tail of the
    # ListNode argument to node `pos` (-1 = no cycle) and do not pass `pos`, unless the
    # learner's method really takes a `pos` parameter.
    if "pos" in args and "pos" not in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
        pos = args.pop("pos")
        lists = [k for k, t in types.items() if t == "ListNode" and k in args]
        if lists and isinstance(pos, int) and pos >= 0:
            head = args[lists[0]]
            nodes = []
            while head is not None and len(nodes) <= 100000:
                nodes.append(head)
                head = head.next
            if pos < len(nodes):
                nodes[-1].next = nodes[pos]
    ret = fn(**args)
    inplace = case.get("inplace")
    if inplace:
        return convert_out(types.get(inplace), args.get(inplace))
    return convert_out(types.get("return"), ret)


def run_design(ns, entry, i, case, cap):
    ops, argl = case.get("ops") or [], case.get("args") or []
    cname = entry.get("class") or (ops[0] if ops else "")
    cls = ns.get(cname)
    if cls is None:
        raise NameError("no class %%s" %% cname)
    out, obj = [], None
    for j, op in enumerate(ops):
        a = argl[j] if j < len(argl) else []
        if j == 0:
            obj = cls(*a)
            out.append(None)
            continue
        fn = getattr(obj, op, None)
        if fn is None:
            raise AttributeError("%%s has no method %%s" %% (cname, op))
        try:
            out.append(plain(fn(*a)))
        except Exception as e:
            e.partial = out
            e.op_index = j
            raise
    return out


def main():
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    code, mode, entry, cases = spec["code"], spec["mode"], spec["entry"], spec["cases"]
    cap = Capped()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = cap
    ns = {"__name__": "__main__", "ListNode": ListNode, "TreeNode": TreeNode}
    try:
        exec(compile(PRELUDE, "<prelude>", "exec"), ns)
    except Exception:
        pass
    try:
        compiled = compile(code, "solution.py", "exec")
    except SyntaxError as e:
        emit({"fatal": "%%s: %%s (line %%s)" %% (type(e).__name__, e.msg, e.lineno),
              "stdout": cap.take()})
        return
    try:
        t0 = time.perf_counter()
        exec(compiled, ns)
    except BaseException as e:
        emit({"fatal": err_text(e), "stdout": cap.take()})
        return
    runner = run_design if mode == "design" else run_function
    for i, case in enumerate(cases):
        t0 = time.perf_counter()
        got, error = None, None
        try:
            got = runner(ns, entry, i, case, cap)
        except RecursionError as e:
            error = err_text(e)
        except BaseException as e:
            error = err_text(e)
            if getattr(e, "op_index", None) is not None:
                error = "op %%d: %%s" %% (e.op_index, error)
                got = e.partial
        ms = round((time.perf_counter() - t0) * 1000, 2)
        emit({"i": i, "got": capped_got(got), "error": error, "stdout": cap.take(), "ms": ms})


def boot():
    sys.setrecursionlimit(20000)
    threading.stack_size(64 * 1024 * 1024)
    t = threading.Thread(target=main)
    t.start()
    t.join()
    _out.close()


boot()
'''


def _interpreter():
    """The base interpreter: a Windows venv python.exe is a launcher that starts a second
    process, which a kill would leave running."""
    base = getattr(sys, "_base_executable", None)
    if base and os.path.isfile(base):
        return base
    return sys.executable


def _env():
    keep = {}
    for k in ("PATH", "SYSTEMROOT", "SystemRoot"):
        if os.environ.get(k):
            keep[k] = os.environ[k]
    keep["PYTHONIOENCODING"] = "utf-8"
    return keep


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        proc.kill()
    except Exception:
        pass


def _limits():                       # POSIX only: memory and process caps
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    except Exception:
        pass


def check(mode, entry, cases):
    """Validate the shape; raises ValueError."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not isinstance(entry, dict):
        raise ValueError("entry must be an object")
    if mode == "function" and not entry.get("method"):
        raise ValueError("function mode needs entry.method")
    if mode == "design" and not entry.get("class"):
        raise ValueError("design mode needs entry.class")
    if not isinstance(cases, list):
        raise ValueError("cases must be a list")
    for i, c in enumerate(cases):
        if not isinstance(c, dict):
            raise ValueError(f"case {i} must be an object")
        if mode == "function" and not isinstance(c.get("args", {}), dict):
            raise ValueError(f"case {i}: args must be an object of name -> value")
        if mode == "design":
            ops, args = c.get("ops"), c.get("args", [])
            if not isinstance(ops, list) or not ops:
                raise ValueError(f"case {i}: ops must be a non-empty list")
            if not isinstance(args, list) or len(args) != len(ops):
                raise ValueError(f"case {i}: args must be a list as long as ops")
        if "types" in c and not isinstance(c["types"], dict):
            raise ValueError(f"case {i}: types must be an object")


# ---- comparison ------------------------------------------------------------------------------
def _canon(x):
    return json.dumps(x, sort_keys=True, default=str)


def _sorted(x, deep):
    if isinstance(x, list):
        xs = [_sorted(v, deep) for v in x] if deep else list(x)
        return sorted(xs, key=_canon)
    return x


def _deep_sort(x):
    if isinstance(x, list):
        return sorted((_deep_sort(v) for v in x), key=_canon)
    return x


def equal(a, b):
    """JSON equality with float tolerance; bool is not a number here."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            return a == b
        return math.isclose(a, b, rel_tol=0.0, abs_tol=FLOAT_TOL)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    return a == b


def compare(got, expected, unordered=False):
    if unordered == "deep":
        return equal(_deep_sort(got), _deep_sort(expected))
    if unordered:
        return equal(_sorted(got, False), _sorted(expected, False))
    return equal(got, expected)


# ---- the run ---------------------------------------------------------------------------------
def run(code, cases, mode, entry, timeout_s=3):
    """Run `code` against `cases` in a subprocess. One result per case, in order."""
    check(mode, entry, cases)
    if not cases:
        return []
    tmp = tempfile.mkdtemp(prefix="coach-run-")
    results = [None] * len(cases)
    fatal = None
    timed_out = False
    try:
        with open(os.path.join(tmp, "harness.py"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS % {"out_cap": OUT_CAP})
        with open(os.path.join(tmp, "input.json"), "w", encoding="utf-8") as fh:
            json.dump({"code": code or "", "mode": mode, "entry": entry, "cases": cases}, fh)
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["preexec_fn"] = _limits
        with open(os.path.join(tmp, "stderr.txt"), "wb") as err:
            proc = subprocess.Popen([_interpreter(), "-I", "-X", "utf8", "harness.py", "input.json"],
                                    cwd=tmp, env=_env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=err, **kw)
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        rc = proc.returncode
        raw = ""
        path = os.path.join(tmp, "results.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw = fh.read(READ_CAP)
        for line in raw.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if "fatal" in d:
                fatal = d
            elif isinstance(d.get("i"), int) and 0 <= d["i"] < len(cases):
                results[d["i"]] = d
        crash = None
        if not timed_out and fatal is None and any(r is None for r in results):
            with open(os.path.join(tmp, "stderr.txt"), encoding="utf-8", errors="replace") as fh:
                tail = fh.read(OUT_CAP).strip().splitlines()
            crash = f"crashed (exit {rc})" + (": " + tail[-1] if tail else "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out = []
    for i, case in enumerate(cases):
        r = results[i]
        exp = case.get("expected")
        has_exp = "expected" in case
        row = {"case_index": i, "ok": False, "got": None, "expected": exp, "error": None,
               "stdout": "", "ms": None}
        if fatal is not None:
            row["error"] = fatal["fatal"]
            row["stdout"] = fatal.get("stdout", "") if i == 0 else ""
        elif r is None:
            row["error"] = TIMEOUT if timed_out else (crash or "no result")
            row["ms"] = round(timeout_s * 1000) if timed_out else None
        else:
            row.update(got=r.get("got"), error=r.get("error"), stdout=r.get("stdout") or "",
                       ms=r.get("ms"))
            if row["error"]:
                row["ok"] = False
            elif has_exp:
                row["ok"] = compare(row["got"], exp, case.get("unordered", False))
            else:
                row["ok"] = None
        out.append(row)
    return out


def summary(results):
    """{passed, failed, ran, total}: ran = cases with no expected value."""
    passed = sum(1 for r in results if r.get("ok") is True)
    failed = sum(1 for r in results if r.get("ok") is False)
    return {"passed": passed, "failed": failed,
            "ran": sum(1 for r in results if r.get("ok") is None), "total": len(results)}
