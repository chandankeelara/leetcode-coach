"""coach.lint reproduces the five historical no-op slips, plus the COST / ALIAS checks."""
from coach.lint import lint


def kinds(src):
    return [k for _, k, _ in lint(src)]


# The five no-op slips logged since 2026-09-14 (see the lintcheck docstring).
def test_slip_comparison_instead_of_assignment_grid():
    # 994: `grid[nr][nc] == 2` where `= 2` was meant
    src = "def f(grid, nr, nc):\n    grid[nr][nc] == 2\n"
    assert kinds(src) == ["USELESS"]


def test_slip_comparison_on_string_cell():
    # 200: `grid[r][c] == "0"` instead of assigning "0"
    src = 'def f(grid, r, c):\n    grid[r][c] == "0"\n'
    assert kinds(src) == ["USELESS"]


def test_slip_value_computed_and_discarded():
    # `hmap[k] + 1` instead of `hmap[k] += 1`
    src = "def f(hmap, k):\n    hmap[k] + 1\n"
    assert kinds(src) == ["USELESS"]


def test_slip_bare_attribute_statement():
    # `node.left` instead of `node = node.left`
    src = "def f(node):\n    node.left\n"
    assert kinds(src) == ["USELESS"]


def test_slip_assign_none_from_heapify():
    # 703: `self.heap = heapq.heapify([])`
    src = "import heapq\nclass K:\n    def __init__(self):\n        self.heap = heapq.heapify([])\n"
    hits = lint(src)
    assert [(h[0], h[1]) for h in hits] == [(4, "NONE-ASSIGN")]


def test_none_assign_list_append():
    assert kinds("h = []\nh = h.append(1)\n") == ["NONE-ASSIGN"]
    assert kinds("d = {}\nx = d.setdefault(1, 2)\n") == []      # setdefault returns a value


def test_cost_pop_zero_in_loop():
    src = "def f(q):\n    while q:\n        x = q.pop(0)\n"
    assert kinds(src) == ["COST"]
    assert kinds("def f(q):\n    x = q.pop(0)\n") == []          # outside a loop it is fine


def test_cost_other_linear_calls_in_loop():
    src = ("def f(a):\n    for i in range(3):\n        a.insert(0, i)\n        a.remove(i)\n"
           "        a.index(i)\n        m = max(a)\n")
    assert kinds(src) == ["COST"] * 4


def test_alias_nested_list_repeat():
    assert kinds("g = [[0] * 3] * 3\n") == ["ALIAS"]
    assert kinds("g = [[0] * 3 for _ in range(3)]\n") == []


def test_clean_code_and_syntax_error():
    assert lint("def f(x):\n    return x + 1\n") == []
    hits = lint("def f(:\n")
    assert len(hits) == 1 and hits[0][1] == "SYNTAX"


def test_sorted_by_line():
    src = "a = [[0]] * 2\nb = [].append(1)\n"
    assert [h[0] for h in lint(src)] == sorted(h[0] for h in lint(src))
