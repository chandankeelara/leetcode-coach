"""Catch the no-op slip automatically. Ported verbatim from the old tools/lintcheck.py checker.

    from coach.lint import lint
    lint(src) -> [(line, kind, msg), ...]      # sorted by line; [] means clean

Four checks. The first two cover every no-op slip logged since 2026-09-14:

  USELESS   a statement that computes a value and throws it away
            `grid[r][c] == "0"`, `hmap[k] + 1`, `node.left`
  NONE-ASSIGN  assigning the return of a method that mutates and returns None
            `h = lst.append(i)`, `self.heap = heapq.heapify([])`

  COST      an operation inside a loop that is O(n) on its own, quietly making the loop
            quadratic. `lst.pop(0)`, `lst.insert(0, x)`, `lst.remove(x)`, `lst.index(x)`,
            and single-argument `sorted/min/max/sum`. Added 2026-10-12 after 57, where
            `pop(0)` turned an O(n) solution into O(n^2) (0.08s at 10k, 5.39s at 80k).
            Heuristic: it cannot see types, so `dict.pop(0)` would be a false positive.

  ALIAS     `[[x] * n] * m` -- m references to ONE row. Added 2026-10-12 after 1091.

A syntax error comes back as one (line, "SYNTAX", msg) hit.

The first two exist because inspection by the author failed five times in a row. A slip is a
correct intention with a failed execution; the fix is a detector that is not the person
who wrote the line. Run it AFTER submitting -- the error signal is the point, so it is
not an autocorrect.
"""
import ast

# Methods that mutate in place and return None. Assigning their result is always a bug.
NONE_RETURNING = {
    # list
    "append", "extend", "insert", "remove", "clear", "sort", "reverse",
    # set
    "add", "discard", "difference_update", "intersection_update", "update",
    # dict
    "setdefault_NOT",  # placeholder: setdefault DOES return a value, never flag it
    # deque
    "appendleft", "extendleft", "rotate",
    # heapq module functions
    "heapify", "heappush",
}
NONE_RETURNING.discard("setdefault_NOT")

# Expression statements that are always pointless on their own.
USELESS_NODES = (ast.Compare, ast.BinOp, ast.UnaryOp, ast.Name,
                 ast.Subscript, ast.Attribute, ast.BoolOp)


# O(n) operations that are cheap to write and expensive inside a loop.
LINEAR_METHODS = {"remove": "scans the list", "index": "scans the list"}
LINEAR_BUILTINS = {"sorted", "min", "max", "sum"}


class Check(ast.NodeVisitor):
    def __init__(self):
        self.hits = []
        self.loop_depth = 0

    def visit_For(self, node):
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    visit_While = visit_For
    visit_AsyncFor = visit_For

    def visit_Call(self, node):
        if self.loop_depth:
            f = node.func
            if isinstance(f, ast.Attribute):
                first = node.args[0] if node.args else None
                is_zero = isinstance(first, ast.Constant) and first.value == 0
                if f.attr == "pop" and is_zero:
                    self.hits.append((node.lineno, "COST",
                        "`.pop(0)` in a loop shifts every remaining element: O(n) each, "
                        "O(n^2) overall. Use an index, or collections.deque.popleft()"))
                elif f.attr == "insert" and is_zero:
                    self.hits.append((node.lineno, "COST",
                        "`.insert(0, x)` in a loop shifts every element: O(n) each. "
                        "Append and reverse once, or use deque.appendleft()"))
                elif f.attr in LINEAR_METHODS:
                    self.hits.append((node.lineno, "COST",
                        f"`.{f.attr}()` {LINEAR_METHODS[f.attr]} on a list: O(n) inside a loop"))
            elif (isinstance(f, ast.Name) and f.id in LINEAR_BUILTINS
                  and len(node.args) == 1 and not node.keywords):
                self.hits.append((node.lineno, "COST",
                    f"`{f.id}()` over a collection is O(n); inside a loop that is O(n) per step"))
        self.generic_visit(node)

    def visit_BinOp(self, node):
        # [[x] * n] * m  -- every row is the SAME list object. Added 2026-10-12 after 1091,
        # where writing visited[1][1] showed up in row 0 too.
        if isinstance(node.op, ast.Mult) and isinstance(node.left, ast.List):
            inner = node.left.elts
            if len(inner) == 1 and (isinstance(inner[0], ast.List) or (
                    isinstance(inner[0], ast.BinOp) and isinstance(inner[0].op, ast.Mult)
                    and isinstance(inner[0].left, ast.List))):
                self.hits.append((node.lineno, "ALIAS",
                    "`[[...] * n] * m` repeats ONE row object m times, so writing to any row "
                    "writes to all of them. Use `[[...] * n for _ in range(m)]`"))
        self.generic_visit(node)

    def visit_Expr(self, node):
        v = node.value
        if isinstance(v, USELESS_NODES):
            kind = "comparison" if isinstance(v, ast.Compare) else "expression"
            self.hits.append((node.lineno, "USELESS",
                              f"this {kind} is computed and discarded"
                              + (" -- did you mean `=`?" if isinstance(v, ast.Compare) else "")))
        self.generic_visit(node)

    def _call_name(self, call):
        f = call.func
        if isinstance(f, ast.Attribute):
            return f.attr
        return None

    def visit_Assign(self, node):
        self._check_assign(node.value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self._check_assign(node.value, node.lineno)
        self.generic_visit(node)

    def _check_assign(self, value, lineno):
        if not isinstance(value, ast.Call):
            return
        name = self._call_name(value)
        if name in NONE_RETURNING:
            self.hits.append((lineno, "NONE-ASSIGN",
                              f"`.{name}()` mutates in place and returns None, "
                              f"so this assigns None"))


def lint(src):
    """Return [(line, kind, msg)] sorted by line. [] means clean."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(e.lineno or 0, "SYNTAX", e.msg)]
    c = Check()
    c.visit(tree)
    return sorted(c.hits)
