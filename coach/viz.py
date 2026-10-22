"""Coach visuals: `render(kind, spec) -> html`, shown as a coach message in the thread.

Kinds: array | grid | list | tree | steps | io | html | animate | graph | intervals | compare |
string. Every value is escaped (markupsafe);
styling is by class only (viz.css). `texts(kind, spec)` returns every human-readable string in a
spec, for the leak guard in `service.show_visual`. Bad specs raise ValueError.
"""
import itertools
import json
import re
from html.parser import HTMLParser

from markupsafe import Markup, escape

KINDS = ("array", "grid", "list", "tree", "steps", "io", "html", "animate", "graph", "intervals",
         "compare", "string")
KIND_LABEL = {"array": "array", "grid": "grid", "list": "list", "tree": "tree",
              "steps": "step table", "io": "examples", "html": "illustration",
              "animate": "animation", "graph": "graph", "intervals": "intervals",
              "compare": "before and after", "string": "string"}

MAX_ITEMS = 200          # array / list values
MAX_GRID = 40            # rows and columns
MAX_STEP_ROWS = 60
MAX_STEP_COLS = 12
MAX_CASES = 12
MAX_TEXT = 600
MAX_HTML = 20000
MAX_TREE_LEVELS = 6      # root + 5


# ---- helpers -------------------------------------------------------------------------------
def _fmt(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return str(v)


def _txt(v, what="text"):
    s = _fmt(v)
    if len(s) > MAX_TEXT:
        raise ValueError(f"{what} longer than {MAX_TEXT} chars")
    return s


def _dict(spec):
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    return spec


def _list(spec, key, required=True, limit=None):
    v = spec.get(key)
    if v is None:
        if required:
            raise ValueError(f"spec.{key} is required")
        return []
    if not isinstance(v, list):
        raise ValueError(f"spec.{key} must be a list")
    if limit is not None and len(v) > limit:
        raise ValueError(f"spec.{key}: at most {limit} items")
    return v


def _int(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
        raise ValueError(f"{what} must be an integer")
    return int(v)


def _index_set(spec, n, key="highlight"):
    """[i, j, ...] and/or [[start, end], ...] (inclusive) -> set of indices in range."""
    out = set()
    for item in _list(spec, key, required=False):
        if isinstance(item, list):
            if len(item) != 2:
                raise ValueError(f"spec.{key}: a range is [start, end]")
            a, b = _int(item[0], key), _int(item[1], key)
            if a > b:
                a, b = b, a
            out.update(i for i in range(a, b + 1) if 0 <= i < n)
        else:
            i = _int(item, key)
            if 0 <= i < n:
                out.add(i)
    return out


def _pointers(spec, n):
    """{"i": 3, "j": 5} -> {index: ["i", ...]} (names escaped later)."""
    p = spec.get("pointers") or {}
    if not isinstance(p, dict):
        raise ValueError("spec.pointers must be an object {name: index}")
    at = {}
    for name, idx in p.items():
        if idx is None:
            continue
        i = _int(idx, f"pointer {name!r}")
        if 0 <= i < n:
            at.setdefault(i, []).append(_txt(name, "pointer name"))
    return at


def _labels(spec, n):
    lab = spec.get("labels") or {}
    if not isinstance(lab, dict):
        raise ValueError("spec.labels must be an object {index: text}")
    out = {}
    for k, v in lab.items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            raise ValueError("spec.labels keys must be indices")
        if 0 <= i < n:
            out[i] = _txt(v, "label")
    return out


def _caption(spec):
    c = spec.get("caption")
    return _txt(c, "caption").strip() if c not in (None, "") else ""


def _figure(kind, body, caption):
    cap = Markup('<figcaption class="viz-cap">{}</figcaption>').format(caption) if caption else ""
    return str(Markup('<figure class="viz viz-{}">{}{}</figure>').format(kind, Markup(body), cap))


def _cls(*names):
    return " ".join(n for n in names if n)


# ---- array ---------------------------------------------------------------------------------
def _render_cells(vals, spec, fig, chg=None, chars=False):
    """One row of boxed cells (array, string). chg = indices outlined as changed (compare)."""
    n = len(vals)
    hl = _index_set(spec, n)
    dim = _index_set(spec, n, "dim")
    ptr = _pointers(spec, n)
    lab = _labels(spec, n)
    chg = chg or set()
    cols = []
    for i, v in enumerate(vals):
        p = ptr.get(i)
        sp = chars and v == " "
        cols.append(Markup(
            '<div class="{col}" data-i="{i}">'
            '<span class="viz-ptr"{key}>{ptr}</span>'
            '<span class="{cell}">{v}</span>'
            '<span class="viz-idx">{i}</span>'
            '<span class="viz-label">{lab}</span>'
            '</div>').format(
                col=_cls("viz-col", "viz-col-ptr" if p else "", "viz-col-dim" if i in dim else ""),
                key=Markup(' data-k="{}"').format(" ".join(p)) if p else "",
                ptr=", ".join(p) if p else "",
                cell=_cls("viz-cell", "viz-hl" if i in hl and i not in dim else "",
                          "viz-dim" if i in dim else "", "viz-null" if v is None else "",
                          "viz-sp" if sp else "", "viz-chg" if i in chg else ""),
                v="·" if sp else _txt(v, "value"), i=i, lab=lab.get(i, "")))
    if not cols:
        cols.append(Markup('<span class="viz-empty">{}</span>').format('""' if chars else "[ ]"))
    body = Markup('<div class="viz-scroll"><div class="viz-array{}{}">{}</div></div>').format(
        " viz-str" if chars else "", " has-ptr" if ptr else "", Markup("").join(cols))
    return _figure(fig, body, _caption(spec))


def _render_array(spec, chg=None):
    return _render_cells(_list(spec, "values", limit=MAX_ITEMS), spec, "array-fig", chg)


# ---- string --------------------------------------------------------------------------------
MAX_STR = 200


def _str_text(spec):
    t = spec.get("text")
    if not isinstance(t, str):
        raise ValueError("spec.text must be a string")
    if len(t) > MAX_STR:
        raise ValueError(f"spec.text longer than {MAX_STR} chars")
    return t


def _render_string(spec, chg=None):
    return _render_cells(list(_str_text(spec)), spec, "string-fig", chg, chars=True)


# ---- grid ----------------------------------------------------------------------------------
def _cells(spec, key, R, C):
    out = []
    for item in _list(spec, key, required=False):
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(f"spec.{key}: each cell is [row, col]")
        r, c = _int(item[0], key), _int(item[1], key)
        if 0 <= r < R and 0 <= c < C:
            out.append((r, c))
    return out


def _render_grid(spec, chg=None):
    chg = chg or set()
    rows = _list(spec, "rows", limit=MAX_GRID)
    for r in rows:
        if not isinstance(r, list):
            raise ValueError("spec.rows must be a list of lists")
        if len(r) > MAX_GRID:
            raise ValueError(f"spec.rows: at most {MAX_GRID} columns")
    R = len(rows)
    C = max((len(r) for r in rows), default=0)
    hl = set(_cells(spec, "highlight", R, C))
    path = {}
    for k, rc in enumerate(_cells(spec, "path", R, C)):
        path.setdefault(rc, k + 1)
    trs = []
    head = Markup("").join(Markup('<th class="viz-idx">{}</th>').format(c) for c in range(C))
    trs.append(Markup('<tr><th class="viz-corner"></th>{}</tr>').format(head))
    for r, row in enumerate(rows):
        tds = []
        for c in range(C):
            v = row[c] if c < len(row) else ""
            step = path.get((r, c))
            tds.append(Markup('<td class="{cls}">{v}{step}</td>').format(
                cls=_cls("viz-cell", "viz-hl" if (r, c) in hl else "",
                         "viz-path" if step else "", "viz-chg" if (r, c) in chg else ""),
                v=_txt(v, "value"),
                step=Markup('<sup class="viz-step">{}</sup>').format(step) if step else ""))
        trs.append(Markup('<tr><th class="viz-idx">{}</th>{}</tr>').format(
            r, Markup("").join(tds)))
    body = Markup('<div class="viz-scroll"><table class="viz-grid">{}</table></div>').format(
        Markup("").join(trs))
    return _figure("grid-fig", body, _caption(spec))


# ---- svg helpers ---------------------------------------------------------------------------
_svg_ids = itertools.count(1)
MIN_SCALE = 0.62         # below this the drawing stops shrinking and scrolls sideways instead


def _marker(mid, cls):
    return Markup('<marker id="{}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="9" '
                  'markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
                  '<path class="{}" d="M0.5,1 L9,5 L0.5,9 z"/></marker>').format(mid, cls)


def _svg(cls, w, h, label, inner):
    w, h = round(w), round(h)
    return Markup('<svg class="viz-svg {cls}" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
                  'data-minw="{m}" role="img" aria-label="{label}">{inner}</svg>').format(
        cls=cls, w=w, h=h, m=round(w * MIN_SCALE), label=label, inner=inner)


def _short(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


# ---- linked list ---------------------------------------------------------------------------
LL_H = 34                # node box height
LL_GAP = 26              # arrow run between boxes
LL_CH = 8.2              # value glyph advance (mono 13px, condensed)
LL_PTR_CH = 6.7          # pointer-tag glyph advance (mono 11px)
LL_LOOP = 44             # bezier control depth of the back-arrow below the row
LL_ROW_GAP = 58          # vertical run between rows (order given as rows)
LL_NULL_W = 34           # room for the "null" mark
LL_TAG_H = 30
LL_IDX_H = 16
LL_PAD = 6
MAX_LL_ROWS = 4
_POS_CAPTION = re.compile(r"^pos\s*=\s*-?\d+$", re.I)


def _ll_links(spec, n):
    """{from: to|None}. links replace the default chain; cycle_to keeps the old meaning."""
    cyc = spec.get("cycle_to")
    cyc = _int(cyc, "spec.cycle_to") if cyc is not None else None
    if cyc is not None and not 0 <= cyc < n:
        raise ValueError("spec.cycle_to must be an index into values")
    raw = spec.get("links")
    if raw is None:
        nxt = {i: i + 1 for i in range(n - 1)}
        if n:
            nxt[n - 1] = cyc
        return nxt, cyc is not None
    if cyc is not None:
        raise ValueError("spec: give links or cycle_to, not both")
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise ValueError(f"spec.links must be a list of [from, to|null] (at most {MAX_ITEMS})")
    nxt = {}
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("spec.links: each link is [from, to|null]")
        a = _int(item[0], "spec.links from")
        b = None if item[1] is None else _int(item[1], "spec.links to")
        if not 0 <= a < n or (b is not None and not 0 <= b < n):
            raise ValueError("spec.links: index out of range")
        if a in nxt:
            raise ValueError(f"spec.links: node {a} has two next links")
        nxt[a] = b
    return nxt, False


def _ll_rows(spec, n):
    """spec.order -> [[index|None, ...], ...] (rows of display slots) and per-row offsets."""
    o = spec.get("order")
    if o is None:
        rows = [list(range(n))]
    else:
        if not isinstance(o, list):
            raise ValueError("spec.order must be a list of indices or a list of rows")
        if o and all(isinstance(x, list) for x in o):
            rows = o
        elif not any(isinstance(x, list) for x in o):
            rows = [o]
        else:
            raise ValueError("spec.order must be a list of indices or a list of rows")
        if len(rows) > MAX_LL_ROWS:
            raise ValueError(f"spec.order: at most {MAX_LL_ROWS} rows")
        seen, out = set(), []
        for row in rows:
            r = []
            for x in row:
                if x is None:
                    r.append(None)
                    continue
                i = _int(x, "spec.order")
                if not 0 <= i < n or i in seen:
                    raise ValueError("spec.order: every node index exactly once")
                seen.add(i)
                r.append(i)
            out.append(r)
        if len(seen) != n:
            raise ValueError("spec.order must place every node")
        rows = out
    off = spec.get("offset")
    if off is None:
        offs = [0.0] * len(rows)
    else:
        if not isinstance(off, list) or len(off) > len(rows):
            raise ValueError("spec.offset: one number per row")
        offs = []
        for x in off:
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= MAX_ITEMS:
                raise ValueError("spec.offset: slots to shift each row right, 0 or more")
            offs.append(float(x))
        offs += [0.0] * (len(rows) - len(offs))
    return rows, offs


def _ll_model(spec):
    vals = _list(spec, "values", limit=MAX_ITEMS)
    n = len(vals)
    m = {"n": n, "vals": vals, "cap": _caption(spec)}
    if _POS_CAPTION.match(m["cap"]):
        m["cap"] = ""
    m["hl"] = _index_set(spec, n)
    m["ptr"] = _pointers(spec, n)
    m["nxt"], m["legacy"] = _ll_links(spec, n)
    rows, offs = _ll_rows(spec, n)
    ind = spec.get("indices", False)
    if not isinstance(ind, bool):
        raise ValueError("spec.indices must be true or false")
    m["idx"] = m["legacy"] or ind
    if not n:
        return m
    m["texts"] = [_short(_txt(v, "value"), 8) for v in vals]
    m["nw"] = max(40, max(len(t) for t in m["texts"]) * LL_CH + 18)
    pos = {}
    for r, row in enumerate(rows):
        for c, i in enumerate(row):
            if i is not None:
                pos[i] = (r, offs[r] + c)
    R = len(rows)
    m["pos"], m["R"] = pos, R
    m["max_slot"] = max(s for _, s in pos.values())

    def row_slots(r):
        return [s for rr, s in pos.values() if rr == r]
    plans, arcs = [], {}
    for a in sorted(m["nxt"]):
        b = m["nxt"][a]
        ra, sa = pos[a]
        p = {"a": a, "b": b}
        if b is None:
            others = [s for s in row_slots(ra) if s != sa]
            p["type"] = "null"
            p["side"] = ("right" if not others or max(others) < sa else
                         "left" if min(others) > sa else "mid")
        elif a == b:
            p["type"] = "self"
        else:
            rb, sb = pos[b]
            both = m["nxt"].get(b) == a
            if ra != rb:
                p["type"], p["pair"] = "cross", both
            elif abs(abs(sb - sa) - 1) < 1e-6 and not (m["legacy"] and a == n - 1):
                p["type"], p["pair"] = "adj", both
            else:
                p["type"] = "arc"
                arcs.setdefault(ra, []).append(p)
        plans.append(p)
    for r, lst in arcs.items():           # nested arcs never cross; interleaved ones swap sides
        placed = {"below": [], "above": []}
        ends = {"below": set(), "above": set()}
        for p in sorted(lst, key=lambda q: (min(pos[q["a"]][1], pos[q["b"]][1]),
                                            -abs(pos[q["a"]][1] - pos[q["b"]][1]))):
            lo, hi = sorted((pos[p["a"]][1], pos[p["b"]][1]))
            p["span"] = hi - lo
            if R > 1:
                p["side"] = "above" if r == 0 else "below"
            elif m["legacy"]:
                p["side"] = "below"
            else:
                def clash(side):       # crossing spans, or two arcs meeting at one node
                    return sum(1 for (l2, h2) in placed[side]
                               if (lo < l2 < hi < h2) or (l2 < lo < h2 < hi) or (l2, h2) == (lo, hi))                         + len({lo, hi} & ends[side])
                p["side"] = "below" if clash("below") <= clash("above") else "above"
            placed[p["side"]].append((lo, hi))
            ends[p["side"]].update((lo, hi))
            p["depth"] = LL_LOOP if m["legacy"] else min(36 + 8 * p["span"], 76)
    for p in plans:                       # a null mark mid-row goes where no arc passes
        if p["type"] == "null" and p["side"] == "mid":
            r, s0 = pos[p["a"]]
            cover = {"below": 0, "above": 0}
            for q in arcs.get(r, []):
                lo, hi = sorted((pos[q["a"]][1], pos[q["b"]][1]))
                if lo <= s0 <= hi:
                    cover[q["side"]] += 1
            p["side"] = "above" if cover["below"] > cover["above"] and R == 1 else "below"
    last = R - 1
    above = below = 0.0
    left_null = right_null = below_null = False
    for p in plans:
        r = pos[p["a"]][0]
        if p["type"] == "arc":
            reach = p["depth"] * 0.75 + 6
            if p["side"] == "above" and r == 0:
                above = max(above, reach)
            elif p["side"] == "below" and r == last:
                below = max(below, reach)
        elif p["type"] == "self" and r == last:
            below = max(below, LL_LOOP * 0.75 + 4)
        elif p["type"] == "null":
            left_null |= p["side"] == "left"
            right_null |= p["side"] == "right"
            if p["side"] == "below" and r == last:
                below_null = True
            if p["side"] == "above" and r == 0:
                above = max(above, 34)
    if below_null:
        below = max(below, 34)
    tag_top = any(pos[i][0] == 0 for i in m["ptr"])
    tag_bot = any(pos[i][0] == last and last > 0 for i in m["ptr"])
    m["plans"] = plans
    m["need"] = {"nw": m["nw"], "above": above, "below": below, "left_null": left_null,
                 "right_null": right_null, "tag_top": tag_top, "tag_bot": tag_bot,
                 "idx": m["idx"], "tag_w": max(
                     [len(", ".join(v)) * LL_PTR_CH + 12 for v in m["ptr"].values()] + [0])}
    return m


def _ll_metrics(models):
    """One frame of reference for every model (animate frames, or a single still)."""
    need = [mm["need"] for mm in models if mm["n"]]
    if not need:
        return {}
    g = {k: max(x[k] for x in need) for k in ("nw", "above", "below", "tag_w")}
    for k in ("left_null", "right_null", "tag_top", "tag_bot", "idx"):
        g[k] = any(x[k] for x in need)
    nw = g["nw"]
    lead = max(0.0, (g["tag_w"] - nw) / 2, LL_GAP + LL_NULL_W if g["left_null"] else 0)
    g["x0"] = LL_PAD + lead
    g["top"] = (LL_PAD + (LL_TAG_H if g["tag_top"] else 0) + g["above"] +
                (LL_IDX_H if g["idx"] else 0))
    return g


def _cubic(x0, y0, x1, y1):
    """A straight segment as a cubic, so every link path morphs into every other."""
    return (f"M{x0:.1f},{y0:.1f} C{x0 + (x1 - x0) / 3:.1f},{y0 + (y1 - y0) / 3:.1f} "
            f"{x0 + (x1 - x0) * 2 / 3:.1f},{y0 + (y1 - y0) * 2 / 3:.1f} {x1:.1f},{y1:.1f}")


def _box_exit(dx, dy, hw, hh):
    tx = hw / abs(dx) if dx else float("inf")
    ty = hh / abs(dy) if dy else float("inf")
    return min(tx, ty)


def _ll_draw(m, g, chg_links=(), chg_nodes=(), min_w=0, min_h=0):
    """-> (figure html, natural width, natural height)."""
    n = m["n"]
    if not n:
        body = Markup('<div class="viz-scroll"><span class="viz-end">null</span></div>')
        return _figure("list-fig", body, m["cap"]), 0, 0
    nw, pos, R = g["nw"], m["pos"], m["R"]
    pitch = nw + LL_GAP
    idx_h = LL_IDX_H if g["idx"] else 0

    def left(i):
        return g["x0"] + pos[i][1] * pitch

    def cx(i):
        return left(i) + nw / 2

    def ty(i):
        return g["top"] + pos[i][0] * (LL_H + LL_ROW_GAP)

    def cy(i):
        return ty(i) + LL_H / 2
    bot = g["top"] + (R - 1) * (LL_H + LL_ROW_GAP) + LL_H
    uid = next(_svg_ids)
    mk = {"next": f"vz{uid}n", "loop": f"vz{uid}c", "chg": f"vz{uid}g", "ptr": f"vz{uid}p"}
    used = set()
    links, nulls = [], []
    right = 0.0
    bottom = bot
    for p in m["plans"]:
        a, b, t = p["a"], p["b"], p["type"]
        chg = a in chg_links
        cls, mid = "viz-ll-next", "next"
        if t == "null":
            y = cy(a)
            if p["side"] == "right":
                x = left(a) + nw
                d = _cubic(x + 3, y, x + LL_GAP - 3, y)
                tx, anchor, tyy = x + LL_GAP + 2, "start", y
                right = max(right, x + LL_GAP + LL_NULL_W)
            elif p["side"] == "left":
                x = left(a)
                d = _cubic(x - 3, y, x - LL_GAP + 3, y)
                tx, anchor, tyy = x - LL_GAP - 2, "end", y
            elif p["side"] == "above":
                x, y0 = cx(a), ty(a)
                d = _cubic(x, y0 - 3, x, y0 - 17)
                tx, anchor, tyy = x, "middle", y0 - 27
            else:
                x, y0 = cx(a), ty(a) + LL_H
                d = _cubic(x, y0 + 3, x, y0 + 17)
                tx, anchor, tyy = x, "middle", y0 + 26
                bottom = max(bottom, y0 + 34)
            nulls.append(Markup('<text class="viz-ll-null" x="{:.1f}" y="{:.1f}" '
                                'dominant-baseline="central"{} data-k="null-{}">null</text>').format(
                tx, tyy, Markup(' text-anchor="{}"').format(anchor) if anchor != "start" else "",
                a))
        elif t == "self":
            c, sy = cx(a), ty(a) + LL_H + 1
            d = (f"M{c + 9:.1f},{sy:.1f} C{c + 34:.1f},{sy + LL_LOOP:.1f} "
                 f"{c - 34:.1f},{sy + LL_LOOP:.1f} {c - 9:.1f},{sy + 2:.1f}")
            cls, mid = ("viz-back viz-ll-loop", "loop") if m["legacy"] else ("viz-ll-arc", "next")
            bottom = max(bottom, sy + LL_LOOP * 0.75 + 4)
        elif t == "adj":
            fwd = pos[b][1] > pos[a][1]
            dy = (-5 if fwd else 5) if p["pair"] else 0
            y = cy(a) + dy
            if fwd:
                d = _cubic(left(a) + nw + 3, y, left(b) - 3, y)
            else:
                d = _cubic(left(a) - 3, y, left(b) + nw + 3, y)
        elif t == "cross":
            ax, ay, bx, by = cx(a), cy(a), cx(b), cy(b)
            dx, dy = bx - ax, by - ay
            ln = (dx * dx + dy * dy) ** 0.5
            ux, uy = dx / ln, dy / ln
            if p["pair"]:
                ax, ay, bx, by = ax - uy * 5, ay + ux * 5, bx - uy * 5, by + ux * 5
            ea = _box_exit(ux, uy, nw / 2, LL_H / 2) + 2
            eb = _box_exit(ux, uy, nw / 2, LL_H / 2) + 3
            d = _cubic(ax + ux * ea, ay + uy * ea, bx - ux * eb, by - uy * eb)
        else:                                               # arc over / under its row
            a_x, b_x = cx(a), cx(b)
            sgn = 1 if b_x > a_x else -1
            if not m["legacy"]:
                a_x, b_x = a_x + 5 * sgn, b_x - 5 * sgn
            dep = p["depth"]
            if p["side"] == "below":
                sy = ty(a) + LL_H + 1
                d = (f"M{a_x:.1f},{sy:.1f} C{a_x:.1f},{sy + dep:.1f} "
                     f"{b_x:.1f},{sy + dep:.1f} {b_x:.1f},{sy + 2:.1f}")
                bottom = max(bottom, sy + dep * 0.75 + 4)
            else:
                sy = ty(a) - 1
                d = (f"M{a_x:.1f},{sy:.1f} C{a_x:.1f},{sy - dep:.1f} "
                     f"{b_x:.1f},{sy - dep:.1f} {b_x:.1f},{sy - 2:.1f}")
            cls, mid = ("viz-back viz-ll-loop", "loop") if m["legacy"] else ("viz-ll-arc", "next")
        if chg:
            cls, mid = cls + " viz-ll-chg", "chg"
        used.add(mid)
        links.append(Markup('<path class="{}" d="{}" marker-end="url(#{})" data-k="link-{}"/>')
                     .format(cls, d, mk[mid], a))
    nodes = []
    for i, t in enumerate(m["texts"]):
        x, y = left(i), ty(i)
        gc = _cls("viz-ll-node", "viz-hl" if i in m["hl"] else "",
                  "viz-null" if m["vals"][i] is None else "", "viz-chg" if i in chg_nodes else "")
        nodes.append(Markup(
            '<g class="{g}" data-i="{i}" data-x="{x:.1f}" data-y="{y:.1f}"><rect class="viz-ll-box" '
            'x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h}" rx="7"/><text class="viz-ll-val" '
            'x="{c:.1f}" y="{m:.1f}" text-anchor="middle" dominant-baseline="central">{t}</text></g>')
            .format(g=gc, i=i, x=x, y=y, w=nw, h=LL_H, c=cx(i), m=cy(i), t=t))
        if m["idx"]:
            nodes.append(Markup('<text class="viz-ll-idx" x="{:.1f}" y="{:.1f}" '
                                'text-anchor="middle">{}</text>').format(cx(i), y - 5, i))
        right = max(right, x + nw)
    tags = []
    if m["ptr"]:
        used.add("ptr")
        tag_bot_y = bot + max(g["below"], 8) + 4
        for i, names in sorted(m["ptr"].items()):
            label = ", ".join(names)
            tw = len(label) * LL_PTR_CH + 12
            c = cx(i)
            r = pos[i][0]
            if r == R - 1 and R > 1:
                y = tag_bot_y
                y1, y2 = y - 1, bot + 3
                bottom = max(bottom, y + 17)
            elif r == 0:
                y = LL_PAD
                y1, y2 = y + 18, ty(i) - idx_h - 2
            else:
                y = ty(i) - idx_h - 28
                y1, y2 = y + 18, ty(i) - idx_h - 2
            tags.append(Markup(
                '<g class="viz-ll-ptr" data-k="{k}"><rect class="viz-ll-tag" x="{x:.1f}" y="{y:.1f}" '
                'width="{w:.1f}" height="17" rx="4"/><text class="viz-ll-ptr-name" x="{c:.1f}" '
                'y="{ty:.1f}" text-anchor="middle" dominant-baseline="central">{t}</text>'
                '<path class="viz-ll-ptr-line" d="M{c:.1f},{y1:.1f} V{y2:.1f}" marker-end="url(#{m})"/>'
                '</g>').format(k=" ".join(names), x=c - tw / 2, y=y, w=tw, c=c, ty=y + 8.5, t=label,
                               y1=y1, y2=y2, m=mk["ptr"]))
            right = max(right, c + tw / 2)
    heads = {"next": "viz-ll-head", "loop": "viz-ll-loop-head", "chg": "viz-ll-chg-head",
             "ptr": "viz-ll-ptr-head"}
    defs = Markup("").join(_marker(mk[k], heads[k]) for k in ("next", "loop", "chg", "ptr")
                           if k in used)
    w = max(right + LL_PAD, min_w)
    h = max(bottom + LL_PAD, min_h)
    svg = _svg("viz-ll", w, h, _ll_label(m),
               Markup("<defs>{}</defs>{}{}{}{}").format(defs, Markup("").join(links),
                                                        Markup("").join(nulls),
                                                        Markup("").join(nodes),
                                                        Markup("").join(tags)))
    body = Markup('<div class="viz-scroll">{}</div>').format(svg)
    return _figure("list-fig", body, m["cap"]), round(w), round(h)


def _ll_label(m):
    t = m["texts"]
    nxt = m["nxt"]
    plain = all(nxt.get(i) == i + 1 for i in range(m["n"] - 1)) and m["R"] == 1 and \
        all(m["pos"][i][1] == i for i in range(m["n"]))
    if plain and nxt.get(m["n"] - 1) is None and m["n"] - 1 in nxt:
        return "linked list " + ", ".join(t) + ", then null"
    if plain and m["legacy"]:
        c = nxt[m["n"] - 1]
        return ("linked list " + ", ".join(t) +
                f"; the last node links back to node {c} (value {t[c]})")
    bits = [f"{t[a]} to {'null' if b is None else t[b]}" for a, b in sorted(nxt.items())]
    return "linked list nodes " + ", ".join(t) + "; next links: " + ", ".join(bits)


def _ll_changes(prev, cur):
    """Links of cur whose target differs from prev (by source node)."""
    return {a for a, b in cur["nxt"].items() if a not in prev["nxt"] or prev["nxt"][a] != b}


def _render_list(spec, chg_links=(), chg_nodes=()):
    m = _ll_model(spec)
    return _ll_draw(m, _ll_metrics([m]), chg_links, chg_nodes)[0]


# ---- tree ----------------------------------------------------------------------------------
NODE_R = 16
GAP_X = 38
GAP_Y = 52
PAD = 6


def tree_layout(values):
    """LeetCode level-order (null = no node; children listed only for real nodes) ->
    [(index_in_values, value, depth, x, y, parent_index|None)] with in-order x positions."""
    if not values or values[0] is None:
        return []
    nodes = {0: {"v": values[0], "d": 0, "l": None, "r": None, "p": None}}
    queue = [0]
    i = 1
    while queue and i < len(values):
        cur = queue.pop(0)
        for side in ("l", "r"):
            if i >= len(values):
                break
            if values[i] is not None:
                d = nodes[cur]["d"] + 1
                if d >= MAX_TREE_LEVELS:
                    raise ValueError(f"tree deeper than {MAX_TREE_LEVELS} levels")
                nodes[i] = {"v": values[i], "d": d, "l": None, "r": None, "p": cur}
                nodes[cur][side] = i
                queue.append(i)
            i += 1
    order = []

    def walk(k):                       # iterative in-order
        stack, cur = [], k
        while stack or cur is not None:
            while cur is not None:
                stack.append(cur)
                cur = nodes[cur]["l"]
            cur = stack.pop()
            order.append(cur)
            cur = nodes[cur]["r"]
    walk(0)
    rank = {k: j for j, k in enumerate(order)}
    out = []
    for k in sorted(nodes):
        nd = nodes[k]
        out.append((k, nd["v"], nd["d"], PAD + NODE_R + rank[k] * GAP_X,
                    PAD + NODE_R + nd["d"] * GAP_Y, nd["p"]))
    return out


def _render_tree(spec):
    vals = _list(spec, "values", limit=2 ** MAX_TREE_LEVELS * 2)
    hl = _index_set(spec, len(vals))
    lay = tree_layout(vals)
    if not lay:
        body = Markup('<span class="viz-empty">empty tree</span>')
        return _figure("tree-fig", body, _caption(spec))
    pos = {k: (x, y) for k, _, _, x, y, _ in lay}
    w = max(x for x, _ in pos.values()) + NODE_R + PAD
    h = max(y for _, y in pos.values()) + NODE_R + PAD
    edges, circles = [], []
    for k, v, d, x, y, p in lay:
        if p is not None:
            px, py = pos[p]
            dx, dy = x - px, y - py
            ln = (dx * dx + dy * dy) ** 0.5
            ux, uy = dx / ln, dy / ln
            edges.append(Markup('<line class="viz-tree-edge" x1="{:.1f}" y1="{:.1f}" '
                                'x2="{:.1f}" y2="{:.1f}"/>').format(
                px + ux * NODE_R, py + uy * NODE_R, x - ux * NODE_R, y - uy * NODE_R))
        s = _txt(v, "value")
        size = "viz-tree-sm" if len(s) > 3 else ""
        circles.append(Markup(
            '<g class="{g}"><circle class="viz-tree-node" cx="{x}" cy="{y}" r="{r}"/>'
            '<text class="{t}" x="{x}" y="{y}" text-anchor="middle" '
            'dominant-baseline="central">{s}</text></g>')
            .format(g=_cls("viz-tree-g", "viz-hl" if k in hl else ""), x=x, y=y, r=NODE_R,
                    t=_cls("viz-tree-val", size), s=_short(s, 6)))
    label = "binary tree " + ", ".join(_fmt(v) for v in vals)
    svg = _svg("viz-tree", w, h, label, Markup("").join(edges + circles))
    return _figure("tree-fig", Markup('<div class="viz-scroll">{}</div>').format(svg),
                   _caption(spec))


# ---- graph ---------------------------------------------------------------------------------
G_R = 17
G_GX = 74                # layer / grid pitch
G_GY = 74
G_PAD = 8
MAX_G_NODES = 40
MAX_G_EDGES = 80
G_LAYOUTS = ("auto", "grid", "circle", "layered")


def _gkey(v, what):
    if v is None or isinstance(v, (bool, list, dict)):
        raise ValueError(f"{what} must be a number or text")
    return _txt(v, what)


def _graph_model(spec):
    ids, labels = [], {}
    for item in _list(spec, "nodes", limit=MAX_G_NODES):
        if isinstance(item, dict):
            if "id" not in item:
                raise ValueError("spec.nodes: each node is an id or {id, label}")
            k, lab = _gkey(item["id"], "node id"), item.get("label")
        else:
            k, lab = _gkey(item, "node id"), None
        if k in labels:
            raise ValueError(f"spec.nodes: duplicate node {k!r}")
        ids.append(k)
        labels[k] = _txt(lab, "node label") if lab not in (None, "") else k
    directed = spec.get("directed", False)
    if not isinstance(directed, bool):
        raise ValueError("spec.directed must be true or false")
    edges, seen = [], set()
    for e in _list(spec, "edges", required=False, limit=MAX_G_EDGES):
        if not isinstance(e, list) or len(e) not in (2, 3):
            raise ValueError("spec.edges: each edge is [u, v] or [u, v, weight]")
        u, v = _gkey(e[0], "edge end"), _gkey(e[1], "edge end")
        if u not in labels or v not in labels:
            raise ValueError(f"spec.edges: [{u}, {v}] names a node not in spec.nodes")
        key = (u, v) if directed else tuple(sorted((u, v)))
        if key in seen:
            continue
        seen.add(key)
        w = e[2] if len(e) == 3 else None
        edges.append((u, v, _txt(w, "edge weight") if w not in (None, "") else None))
    layout = spec.get("layout", "auto")
    if layout not in G_LAYOUTS:
        raise ValueError(f"spec.layout must be one of {', '.join(G_LAYOUTS)}")
    hl = spec.get("highlight") or {}
    if isinstance(hl, list):
        hl = {"nodes": hl}
    if not isinstance(hl, dict):
        raise ValueError("spec.highlight must be {nodes: [id], edges: [[u, v]]}")
    hn = {_gkey(x, "highlight node") for x in _list(hl, "nodes", required=False)}
    he = set()
    for e in _list(hl, "edges", required=False):
        if not isinstance(e, list) or len(e) < 2:
            raise ValueError("spec.highlight.edges: each edge is [u, v]")
        u, v = _gkey(e[0], "highlight edge"), _gkey(e[1], "highlight edge")
        he.add((u, v) if directed else tuple(sorted((u, v))))
    return {"ids": ids, "labels": labels, "edges": edges, "directed": directed,
            "layout": layout, "hn": hn, "he": he, "cap": _caption(spec)}


def _back_edges(ids, edges):
    out = {k: [] for k in ids}
    for u, v, _ in edges:
        if u != v:
            out[u].append(v)
    color, back = dict.fromkeys(ids, 0), set()
    for s in ids:
        if color[s]:
            continue
        color[s] = 1
        stack = [(s, iter(out[s]))]
        while stack:
            u, it = stack[-1]
            for v in it:
                if color[v] == 0:
                    color[v] = 1
                    stack.append((v, iter(out[v])))
                    break
                if color[v] == 1:
                    back.add((u, v))
            else:
                color[u] = 2
                stack.pop()
    return back


def _is_forest(ids, edges):
    parent = {k: k for k in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for u, v, _ in edges:
        a, b = find(u), find(v)
        if a == b:
            return False
        parent[a] = b
    return True


def _layered(ids, edges, directed):
    rank = {k: j for j, k in enumerate(ids)}
    if directed:
        back = _back_edges(ids, edges)
        dag = [(u, v) for u, v, _ in edges if u != v and (u, v) not in back]
        indeg = dict.fromkeys(ids, 0)
        out = {k: [] for k in ids}
        for u, v in dag:
            out[u].append(v)
            indeg[v] += 1
        layer = dict.fromkeys(ids, 0)
        ready = [k for k in ids if not indeg[k]]
        while ready:
            u = min(ready, key=rank.get)
            ready.remove(u)
            for v in out[u]:
                layer[v] = max(layer[v], layer[u] + 1)
                indeg[v] -= 1
                if not indeg[v]:
                    ready.append(v)
        nb = dag
    else:
        adj = {k: [] for k in ids}
        for u, v, _ in edges:
            if u != v:
                adj[u].append(v)
                adj[v].append(u)
        layer = {}
        for s in ids:
            if s in layer:
                continue
            layer[s] = 0
            q = [s]
            while q:
                u = q.pop(0)
                for v in adj[u]:
                    if v not in layer:
                        layer[v] = layer[u] + 1
                        q.append(v)
        nb = [(u, v) if layer[u] <= layer[v] else (v, u) for u, v, _ in edges if u != v]
    L = max(layer.values()) + 1
    rows = [[k for k in ids if layer[k] == d] for d in range(L)]
    up = {k: [] for k in ids}
    down = {k: [] for k in ids}
    for u, v in nb:
        down[u].append(v)
        up[v].append(u)
    for sweep in range(3):                       # barycentre ordering, down / up / down
        rng = range(1, L) if sweep != 1 else range(L - 2, -1, -1)
        for d in rng:
            ref = rows[d - 1] if sweep != 1 else rows[d + 1]
            at = {k: j for j, k in enumerate(ref)}
            nbrs = up if sweep != 1 else down

            def bary(k, j):
                ps = [at[p] for p in nbrs[k] if p in at]
                return (sum(ps) / len(ps) if ps else j, j)
            rows[d] = [k for j, k in sorted(enumerate(rows[d]), key=lambda t: bary(t[1], t[0]))]
    pos = {}
    for d, row in enumerate(rows):
        for j, k in enumerate(row):
            pos[k] = ((j - (len(row) - 1) / 2) * G_GX, d * G_GY)
    return pos


def graph_layout(m):
    """{id: (x, y)}, deterministic for the same nodes, edges and layout."""
    import math
    ids, edges = m["ids"], m["edges"]
    n = len(ids)
    lay = m["layout"]
    if lay == "auto":
        if not edges:
            lay = "grid"
        elif m["directed"] and not _back_edges(ids, edges) and all(u != v for u, v, _ in edges):
            lay = "layered"
        elif not m["directed"] and _is_forest(ids, edges):
            lay = "layered"
        else:
            lay = "circle" if n <= 12 else "grid"
    if lay == "layered":
        return _layered(ids, edges, m["directed"])
    if lay == "circle" and n > 2:
        R = max(58.0, n * G_GX / (2 * math.pi))
        return {k: (R * math.cos(-math.pi / 2 + 2 * math.pi * j / n),
                    R * math.sin(-math.pi / 2 + 2 * math.pi * j / n)) for j, k in enumerate(ids)}
    cols = max(1, math.ceil(math.sqrt(n))) if lay == "grid" else n
    return {k: ((j % cols) * G_GX, (j // cols) * G_GY) for j, k in enumerate(ids)}


def _render_graph(spec, frame=None):
    """frame = {pos, loops} shared by animate frames, so nodes never move between them."""
    m = _graph_model(spec)
    if not m["ids"]:
        return _figure("graph-fig", Markup('<span class="viz-empty">empty graph</span>'), m["cap"])
    allpos = frame["pos"] if frame else graph_layout(m)
    loops = frame["loops"] if frame else any(u == v for u, v, _ in m["edges"])
    minx = min(x for x, _ in allpos.values())
    miny = min(y for _, y in allpos.values())
    ox, oy = G_PAD + G_R + 6 - minx, G_PAD + G_R + 6 + (26 if loops else 0) - miny
    P = {k: (allpos[k][0] + ox, allpos[k][1] + oy) for k in m["ids"]}
    eset = {(u, v) for u, v, _ in m["edges"]}
    uid = next(_svg_ids)
    m_e, m_h = f"vz{uid}e", f"vz{uid}h"
    paths, weights = [], []
    maxx = max(x for x, _ in allpos.values()) + ox + G_R + 6
    maxy = max(y for _, y in allpos.values()) + oy + G_R + 6
    for u, v, w in m["edges"]:
        hi = ((u, v) if m["directed"] else tuple(sorted((u, v)))) in m["he"]
        (x1, y1), (x2, y2) = P[u], P[v]
        tip = 2 if m["directed"] else 0
        if u == v:
            d = (f"M{x1 - 7:.1f},{y1 - G_R + 1:.1f} C{x1 - 26:.1f},{y1 - G_R - 34:.1f} "
                 f"{x1 + 26:.1f},{y1 - G_R - 34:.1f} {x1 + 7:.1f},{y1 - G_R + 1 - tip:.1f}")
            mx, my = x1, y1 - G_R - 30
        else:
            dx, dy = x2 - x1, y2 - y1
            ln = (dx * dx + dy * dy) ** 0.5 or 1
            ux, uy = dx / ln, dy / ln
            bend = 0.0
            if m["directed"] and (v, u) in eset:
                bend = 12.0
            else:
                for k, (px, py) in P.items():
                    if k in (u, v):
                        continue
                    t = ((px - x1) * ux + (py - y1) * uy)
                    if 0 < t < ln and abs((px - x1) * uy - (py - y1) * ux) < G_R + 4:
                        side = (px - x1) * -uy + (py - y1) * ux
                        bend = -24.0 if side > 0 else 24.0
                        break
            if bend:
                qx, qy = (x1 + x2) / 2 - uy * bend * 2, (y1 + y2) / 2 + ux * bend * 2
                ax, ay = qx - x1, qy - y1
                la = (ax * ax + ay * ay) ** 0.5
                bx, by = qx - x2, qy - y2
                lb = (bx * bx + by * by) ** 0.5
                s1 = (x1 + ax / la * G_R, y1 + ay / la * G_R)
                s2 = (x2 + bx / lb * (G_R + tip), y2 + by / lb * (G_R + tip))
                c1 = (s1[0] + (qx - s1[0]) * 2 / 3, s1[1] + (qy - s1[1]) * 2 / 3)
                c2 = (s2[0] + (qx - s2[0]) * 2 / 3, s2[1] + (qy - s2[1]) * 2 / 3)
                d = (f"M{s1[0]:.1f},{s1[1]:.1f} C{c1[0]:.1f},{c1[1]:.1f} "
                     f"{c2[0]:.1f},{c2[1]:.1f} {s2[0]:.1f},{s2[1]:.1f}")
                mx, my = (x1 + x2) / 2 - uy * bend, (y1 + y2) / 2 + ux * bend
            else:
                d = _cubic(x1 + ux * G_R, y1 + uy * G_R, x2 - ux * (G_R + tip),
                           y2 - uy * (G_R + tip))
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        mark = Markup(' marker-end="url(#{})"').format(m_h if hi else m_e) if m["directed"] else ""
        paths.append(Markup('<path class="{}" d="{}"{}/>').format(
            _cls("viz-g-edge", "viz-hl" if hi else ""), d, mark))
        if w is not None:
            tw = len(_short(w, 8)) * 6.4 + 8
            weights.append(Markup(
                '<g class="{}"><rect class="viz-g-wbox" x="{:.1f}" y="{:.1f}" width="{:.1f}" '
                'height="15" rx="4"/><text class="viz-g-w" x="{:.1f}" y="{:.1f}" '
                'text-anchor="middle" dominant-baseline="central">{}</text></g>').format(
                _cls("viz-g-wg", "viz-hl" if hi else ""), mx - tw / 2, my - 7.5, tw, mx, my,
                _short(w, 8)))
            maxx = max(maxx, mx + tw / 2)
    circles = []
    for k in m["ids"]:
        x, y = P[k]
        s = m["labels"][k]
        circles.append(Markup(
            '<g class="{g}" data-i="{k}"><circle class="viz-g-node" cx="{x:.1f}" cy="{y:.1f}" r="{r}"/>'
            '<text class="{t}" x="{x:.1f}" y="{y:.1f}" text-anchor="middle" '
            'dominant-baseline="central">{s}</text></g>').format(
                g=_cls("viz-g-g", "viz-hl" if k in m["hn"] else ""), k=k, x=x, y=y, r=G_R,
                t=_cls("viz-g-val", "viz-g-sm" if len(s) > 3 else ""), s=_short(s, 6)))
    defs = ""
    if m["directed"]:
        defs = Markup("<defs>{}{}</defs>").format(_marker(m_e, "viz-g-head"),
                                                  _marker(m_h, "viz-g-head-hl"))
    lab = ("directed graph" if m["directed"] else "graph") + ", nodes " + \
        ", ".join(m["labels"][k] for k in m["ids"])
    if m["edges"]:
        sep = " to " if m["directed"] else " and "
        lab += "; edges " + ", ".join(f"{m['labels'][u]}{sep}{m['labels'][v]}" +
                                     (f" ({w})" if w is not None else "")
                                     for u, v, w in m["edges"][:40])
    svg = _svg("viz-g", maxx + G_PAD, maxy + G_PAD, lab,
               Markup("{}{}{}{}").format(defs, Markup("").join(paths), Markup("").join(weights),
                                         Markup("").join(circles)))
    return _figure("graph-fig", Markup('<div class="viz-scroll">{}</div>').format(svg), m["cap"])


def _texts_graph(spec):
    out = [_fmt(spec.get("caption"))] if spec.get("caption") not in (None, "") else []
    for item in spec.get("nodes") or []:
        if isinstance(item, dict):
            out += [_fmt(item.get("id")), _fmt(item.get("label"))]
        else:
            out.append(_fmt(item))
    for e in spec.get("edges") or []:
        if isinstance(e, list) and len(e) == 3:
            out.append(_fmt(e[2]))
    return [t for t in out if isinstance(t, str)]


# ---- intervals -----------------------------------------------------------------------------
IV_H = 20
IV_GAP = 6
IV_W = 360               # target drawing width of the axis
MAX_IV = 60


def _num(v, what):
    import math
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{what} must be a number")
    return v


def _pairs(spec, key, required):
    out = []
    for it in _list(spec, key, required=required, limit=MAX_IV):
        if not isinstance(it, list) or len(it) != 2:
            raise ValueError(f"spec.{key}: each interval is [start, end]")
        s, e = _num(it[0], f"spec.{key}"), _num(it[1], f"spec.{key}")
        out.append((min(s, e), max(s, e)))
    return out


def _iv_axis(spec, ivs, merged):
    ax = spec.get("axis")
    if ax is not None:
        if not isinstance(ax, dict) or "min" not in ax or "max" not in ax:
            raise ValueError("spec.axis must be {min, max}")
        lo, hi = _num(ax["min"], "spec.axis.min"), _num(ax["max"], "spec.axis.max")
        if lo >= hi:
            raise ValueError("spec.axis: min must be below max")
        return lo, hi
    vals = [x for p in ivs + merged for x in p]
    if not vals:
        return 0, 10
    lo, hi = min(vals), max(vals)
    return (lo, hi) if hi > lo else (lo - 1, hi + 1)


def _nice(x):
    import math
    p = 10 ** math.floor(math.log10(x))
    for k in (1, 2, 5, 10):
        if k * p >= x:
            return k * p
    return 10 * p


def _numfmt(v):
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def iv_rows(ivs):
    """Interval index -> row: first row whose last bar ends before this one starts."""
    ends, row = [], {}
    for i in sorted(range(len(ivs)), key=lambda i: (ivs[i][0], ivs[i][1], i)):
        s, e = ivs[i]
        for r, end in enumerate(ends):
            if end < s:
                ends[r], row[i] = e, r
                break
        else:
            row[i] = len(ends)
            ends.append(e)
    return row


def _render_intervals(spec):
    import math
    ivs = _pairs(spec, "intervals", True)
    merged = _pairs(spec, "merged", False)
    n = len(ivs)
    hl = _index_set(spec, n)
    lab = spec.get("labels")
    if isinstance(lab, list):
        lab = {str(i): v for i, v in enumerate(lab)}
    labels = _labels({"labels": lab}, n)
    lo, hi = _iv_axis(spec, ivs, merged)
    span = hi - lo
    px = min(64.0, max(3.0, IV_W / span))
    step = _nice(span / 8)
    padl = 14
    X = lambda v: padl + (min(max(v, lo), hi) - lo) * px
    rows = iv_rows(ivs)
    nrows = max(rows.values(), default=-1) + 1
    top = 6
    y_m = top + nrows * (IV_H + IV_GAP) + (6 if merged else 0)
    y_axis = y_m + (IV_H + IV_GAP if merged else 0) + 4
    w = X(hi) + padl
    grid, bars, ticks = [], [], []
    t = math.ceil(lo / step - 1e-9) * step
    while t <= hi + 1e-9:
        x = X(t)
        grid.append(Markup('<path class="viz-iv-grid" d="M{0:.1f},{1:.1f} V{2:.1f}"/>').format(
            x, top - 2, y_axis))
        ticks.append(Markup('<path class="viz-iv-tick" d="M{0:.1f},{1:.1f} v5"/>'
                            '<text class="viz-iv-num" x="{0:.1f}" y="{2:.1f}" text-anchor="middle">'
                            '{3}</text>').format(x, y_axis, y_axis + 17, _numfmt(round(t, 9))))
        t += step

    def bar(cls, s, e, y, text, i=None):
        x1, x2 = X(s), X(e)
        bw = max(4.0, x2 - x1)
        if bw == 4.0:
            x1 -= 2
        out = Markup('<rect class="{}" x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{}" rx="4"/>') \
            .format(cls, x1, y, bw, IV_H)
        if text and len(text) * 6.4 + 8 <= bw:
            out += Markup('<text class="viz-iv-txt" x="{:.1f}" y="{:.1f}" text-anchor="middle" '
                          'dominant-baseline="central">{}</text>').format(x1 + bw / 2, y + IV_H / 2,
                                                                         text)
        return Markup('<g class="viz-iv-g{}"{}>{}</g>').format(
            " viz-hl" if i is not None and i in hl else "",
            Markup(' data-i="{}"').format(i) if i is not None else "", out)
    for i, (s, e) in enumerate(ivs):
        bars.append(bar("viz-iv-bar", s, e, top + rows[i] * (IV_H + IV_GAP),
                        labels.get(i) or f"{_numfmt(s)},{_numfmt(e)}", i))
    for s, e in merged:
        bars.append(bar("viz-iv-merged", s, e, y_m, f"{_numfmt(s)},{_numfmt(e)}"))
    axis = Markup('<path class="viz-iv-axis" d="M{:.1f},{:.1f} H{:.1f}"/>').format(
        X(lo) - 4, y_axis, X(hi) + 4)
    label = "intervals " + ", ".join(f"[{_numfmt(s)}, {_numfmt(e)}]" for s, e in ivs)
    if merged:
        label += "; result " + ", ".join(f"[{_numfmt(s)}, {_numfmt(e)}]" for s, e in merged)
    svg = _svg("viz-iv", w, y_axis + 24, label,
               Markup("").join(grid) + axis + Markup("").join(ticks) + Markup("").join(bars))
    return _figure("intervals-fig", Markup('<div class="viz-scroll">{}</div>').format(svg),
                   _caption(spec))


# ---- compare: before -> after --------------------------------------------------------------
CMP_KINDS = ("array", "grid", "list", "tree", "steps", "io", "graph", "intervals", "string",
             "html")


def _cmp_side(spec, key):
    side = spec.get(key)
    if not isinstance(side, dict) or not isinstance(side.get("spec"), dict):
        raise ValueError(f"spec.{key} must be {{kind, spec}}")
    k = side.get("kind")
    if k not in CMP_KINDS:
        raise ValueError(f"spec.{key}.kind must be one of {', '.join(CMP_KINDS)}")
    return k, side["spec"]


def _render_compare(spec):
    bk, bs = _cmp_side(spec, "before")
    ak, as_ = _cmp_side(spec, "after")
    mark = spec.get("mark_changes", True)
    if not isinstance(mark, bool):
        raise ValueError("spec.mark_changes must be true or false")
    try:
        before = _RENDER[bk](bs)
    except ValueError as e:
        raise ValueError(f"before: {e}") from None
    try:
        if mark and bk == ak and bk in ("array", "string"):
            get = (lambda s: list(_str_text(s))) if bk == "string" else \
                (lambda s: _list(s, "values", limit=MAX_ITEMS))
            bv, av = get(bs), get(as_)
            chg = {i for i, v in enumerate(av) if i >= len(bv) or _fmt(bv[i]) != _fmt(v)}
            after = _RENDER[ak](as_, chg)
        elif mark and bk == ak == "grid":
            br, ar = _list(bs, "rows"), _list(as_, "rows", limit=MAX_GRID)
            chg = {(r, c) for r, row in enumerate(ar) if isinstance(row, list)
                   for c, v in enumerate(row)
                   if r >= len(br) or not isinstance(br[r], list) or c >= len(br[r])
                   or _fmt(br[r][c]) != _fmt(v)}
            after = _render_grid(as_, chg)
        elif mark and bk == ak == "list":
            mb, ma = _ll_model(bs), _ll_model(as_)
            nodes = set()
            if ma["n"]:
                nodes = {i for i in range(ma["n"]) if i >= mb["n"] or
                         _fmt(mb["vals"][i]) != _fmt(ma["vals"][i]) or
                         mb["pos"].get(i) != ma["pos"][i]}
            after = _ll_draw(ma, _ll_metrics([ma]), _ll_changes(mb, ma), nodes)[0]
        else:
            after = _RENDER[ak](as_)
    except ValueError as e:
        raise ValueError(f"after: {e}") from None
    arrow = Markup('<svg class="viz-cmp-arrow" viewBox="0 0 24 24" width="22" height="22" '
                   'aria-hidden="true" focusable="false"><path d="M4 12h14M13 6l6 6-6 6"/></svg>')
    body = Markup(
        '<div class="viz-cmp-body">'
        '<div class="viz-cmp-side"><span class="viz-cmp-tag">before</span>{}</div>{}'
        '<div class="viz-cmp-side"><span class="viz-cmp-tag">after</span>{}</div></div>').format(
            Markup(before), arrow, Markup(after))
    return _figure("compare", body, _caption(spec))


def _texts_compare(spec):
    out = [_fmt(spec.get("caption"))] if spec.get("caption") not in (None, "") else []
    for key in ("before", "after"):
        k, s = _cmp_side(spec, key)
        out += texts(k, s)
    return out


# ---- steps ---------------------------------------------------------------------------------
def _render_steps(spec):
    cols = _list(spec, "columns", limit=MAX_STEP_COLS)
    rows = _list(spec, "rows", limit=MAX_STEP_ROWS)
    for r in rows:
        if not isinstance(r, list):
            raise ValueError("spec.rows must be a list of lists")
        if len(r) > MAX_STEP_COLS:
            raise ValueError(f"spec.rows: at most {MAX_STEP_COLS} cells")
    hr = spec.get("highlight_row")
    hr = _int(hr, "spec.highlight_row") if hr is not None else None
    width = max([len(cols)] + [len(r) for r in rows])
    head = Markup("").join(Markup("<th>{}</th>").format(_txt(c, "column"))
                           for c in cols + [""] * (width - len(cols)))
    trs = []
    for i, r in enumerate(rows):
        tds = Markup("").join(Markup("<td>{}</td>").format(_txt(v, "cell"))
                              for v in list(r) + [""] * (width - len(r)))
        trs.append(Markup('<tr class="{}">{}</tr>').format("viz-hl" if i == hr else "", tds))
    body = Markup('<div class="viz-scroll"><table class="viz-steps"><thead><tr>{}</tr></thead>'
                  '<tbody>{}</tbody></table></div>').format(head, Markup("").join(trs))
    return _figure("steps-fig", body, _caption(spec))


# ---- io ------------------------------------------------------------------------------------
def _render_io(spec):
    cases = _list(spec, "cases", limit=MAX_CASES)
    items = []
    for c in cases:
        if not isinstance(c, dict):
            raise ValueError("spec.cases: each case is {input, output, why}")
        why = c.get("why")
        items.append(Markup(
            '<div class="viz-case"><span class="viz-k">in</span><code class="viz-v">{i}</code>'
            '<span class="viz-k">out</span><code class="viz-v viz-out">{o}</code>{w}</div>')
            .format(i=_txt(c.get("input", ""), "input"), o=_txt(c.get("output", ""), "output"),
                    w=Markup('<p class="viz-why">{}</p>').format(_txt(why, "why"))
                    if why not in (None, "") else ""))
    body = Markup('<div class="viz-io">{}</div>').format(Markup("").join(items))
    return _figure("io-fig", body, _caption(spec))


# ---- html (sanitised escape hatch) ---------------------------------------------------------
HTML_TAGS = {"div", "span", "p", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
             "code", "pre", "strong", "b", "em", "h4", "br"}
SVG_TAGS = {"svg", "g", "rect", "circle", "line", "path", "text", "polyline", "polygon"}
VOID = {"br"}
SVG_ATTRS = {"x", "y", "width", "height", "r", "cx", "cy", "x1", "y1", "x2", "y2", "d", "points",
             "fill", "stroke", "stroke-width", "font-size", "text-anchor", "viewbox", "transform"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript",
                "textarea", "title", "head"}
_SAFE_VAL = re.compile(r"^[#a-zA-Z0-9.,%\s\-]*$")
_SAFE_TRANSFORM = re.compile(r"^[a-zA-Z0-9.,%\s\-()]*$")
_SAFE_CLASS = re.compile(r"^[a-zA-Z0-9_\-\s]*$")


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.text = []
        self.stack = []          # open allowed tags
        self.drop = 0            # depth inside a dropped-content element

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop += 1
            return
        if self.drop or (tag not in HTML_TAGS and tag not in SVG_TAGS):
            return
        keep = []
        for k, v in attrs:
            k = (k or "").lower()
            v = v or ""
            if k == "class" and _SAFE_CLASS.match(v):
                keep.append(("class", v))
            elif tag in SVG_TAGS and k in SVG_ATTRS:
                rx = _SAFE_TRANSFORM if k == "transform" else (
                    re.compile(r"^[a-zA-Z0-9.,\s\-]*$") if k == "d" else _SAFE_VAL)
                if rx.match(v):
                    keep.append(("viewBox" if k == "viewbox" else k, v))
        a = "".join(str(Markup(' {}="{}"').format(k, v)) for k, v in keep)
        if tag in VOID:
            self.out.append(f"<{tag}{a}>")
            return
        self.out.append(f"<{tag}{a}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT or self.drop:
            return
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1] == tag:
            self.stack.pop()
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop or tag not in self.stack:
            return
        while self.stack:                 # close anything left open inside it
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if self.drop:
            return
        self.out.append(str(escape(data)))
        self.text.append(data)

    def close(self):
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")


def sanitize(src):
    """(safe_html, visible_text)."""
    p = _Sanitizer()
    p.feed(src or "")
    p.close()
    return "".join(p.out), " ".join(t.strip() for t in p.text if t.strip())


def _render_html(spec):
    src = spec.get("html")
    if not isinstance(src, str) or not src.strip():
        raise ValueError("spec.html is required")
    if len(src) > MAX_HTML:
        raise ValueError(f"spec.html longer than {MAX_HTML} chars")
    safe, _ = sanitize(src)
    return _figure("html-fig", Markup('<div class="viz-scroll viz-raw">{}</div>').format(
        Markup(safe)), _caption(spec))


# ---- animate: frames of one base kind, played in place ------------------------------------
ANIM_BASES = ("array", "list", "tree", "grid", "steps", "string", "graph", "intervals",
              "compare")
MIN_FRAMES, MAX_FRAMES = 2, 40
ANIM_MS = (300, 6000)
MAX_ANIM_HTML = 400000

_ICON = {
    "prev": '<path d="M5 4v12M16 4 8 10l8 6z"/>',
    "next": '<path d="M15 4v12M4 4l8 6-8 6z"/>',
    "play": '<path class="viz-i-play" d="M6 4l11 6-11 6z"/>'
            '<path class="viz-i-pause" d="M6 4h3v12H6zM11 4h3v12h-3z"/>',
}


def _icon(name):
    return Markup('<svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true" '
                  'focusable="false">{}</svg>').format(Markup(_ICON[name]))


def _anim_frames(spec):
    base = spec.get("base")
    if base not in ANIM_BASES:
        raise ValueError(f"spec.base must be one of {', '.join(ANIM_BASES)}")
    frames = _list(spec, "frames", limit=MAX_FRAMES)
    if len(frames) < MIN_FRAMES:
        raise ValueError(f"spec.frames: at least {MIN_FRAMES} frames")
    out = []
    for n, f in enumerate(frames, 1):
        if not isinstance(f, dict) or not isinstance(f.get("spec"), dict):
            raise ValueError(f"frame {n}: each frame is {{spec, caption}}")
        inner = dict(f["spec"])
        cap = f.get("caption")
        cap = _txt(cap, f"frame {n} caption").strip() if cap not in (None, "") else ""
        inner_cap = inner.pop("caption", None)
        if not cap and inner_cap not in (None, ""):
            cap = _txt(inner_cap, f"frame {n} caption").strip()
        out.append((inner, cap))
    return base, out


def _anim_options(spec):
    ms = spec.get("ms", 1200)
    ms = _int(ms, "spec.ms")
    if not ANIM_MS[0] <= ms <= ANIM_MS[1]:
        raise ValueError(f"spec.ms must be {ANIM_MS[0]}..{ANIM_MS[1]}")
    loop = spec.get("loop", False)
    if not isinstance(loop, bool):
        raise ValueError("spec.loop must be true or false")
    return ms, loop


_SVG_HEAD = re.compile(r'(<svg class="viz-svg [^"]*") width="\d+" height="\d+" '
                       r'viewBox="0 0 \d+ \d+" data-minw="\d+"')


def _svg_size(html):
    m = re.search(r'<svg class="viz-svg [^"]*" width="(\d+)" height="(\d+)"', html)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _fit_svgs(pics):
    """Every frame's drawing on one canvas size, so the scale never jumps between frames."""
    if not all(len(_SVG_HEAD.findall(p)) == 1 for p in pics):
        return pics
    sizes = [_svg_size(p) for p in pics]
    W, H = max(w for w, _ in sizes), max(h for _, h in sizes)
    tail = f' width="{W}" height="{H}" viewBox="0 0 {W} {H}" data-minw="{round(W * MIN_SCALE)}"'
    return [_SVG_HEAD.sub(lambda mt: mt.group(1) + tail, p, count=1) for p in pics]


def _same_shape(base, frames):
    """Frames can tween (tags glide, nodes move, links morph) when every frame draws the same
    values: array / string cells stay put, list nodes keep their identity by index."""
    key = {"array": "values", "list": "values", "string": "text"}.get(base)
    if not key:
        return False
    k = lambda f: json.dumps(f.get(key), sort_keys=True, default=str)
    first = k(frames[0][0])
    return all(k(f) == first for f, _ in frames[1:])


def _each(frames, fn):
    out = []
    for k, (inner, _) in enumerate(frames):
        try:
            out.append(fn(k, inner))
        except ValueError as e:
            raise ValueError(f"frame {k + 1}: {e}") from None
    return out


def _anim_pics(base, frames):
    """Frame drawings, sharing one frame of reference where the base needs one."""
    if base == "list":
        models = _each(frames, lambda k, f: _ll_model(f))
        g = _ll_metrics(models)
        chg = [set()] + [_ll_changes(models[k - 1], models[k]) for k in range(1, len(models))]
        dims = [_ll_draw(m, g, chg[k])[1:] for k, m in enumerate(models)]
        W, H = max(d[0] for d in dims), max(d[1] for d in dims)
        return [_ll_draw(m, g, chg[k], (), W, H)[0] for k, m in enumerate(models)]
    if base == "graph":
        models = _each(frames, lambda k, f: _graph_model(f))
        union = dict(models[0], ids=[], edges=[], labels={})
        seen = set()
        for m in models:
            for k in m["ids"]:
                if k not in union["labels"]:
                    union["ids"].append(k)
                    union["labels"][k] = m["labels"][k]
            for e in m["edges"]:
                key = e[:2] if union["directed"] else tuple(sorted(e[:2]))
                if key not in seen:
                    seen.add(key)
                    union["edges"].append(e)
        frame = {"pos": graph_layout(union) if union["ids"] else {},
                 "loops": any(u == v for u, v, _ in union["edges"])}
        return _fit_svgs(_each(frames, lambda k, f: _render_graph(f, frame)))
    if base == "intervals":
        free = [f for f, _ in frames if f.get("axis") is None]
        vals = [x for f in free for key, req in (("intervals", True), ("merged", False))
                for p in _pairs(f, key, req) for x in p]
        if vals:
            lo, hi = min(vals), max(vals)
            ax = {"min": lo, "max": hi} if hi > lo else {"min": lo - 1, "max": hi + 1}
            frames = [(f if f.get("axis") is not None else dict(f, axis=ax), c) for f, c in frames]
        return _fit_svgs(_each(frames, lambda k, f: _render_intervals(f)))
    pics = _each(frames, lambda k, f: _RENDER[base](f))
    return _fit_svgs(pics) if base == "tree" else pics


def _render_animate(spec):
    base, frames = _anim_frames(spec)
    ms, loop = _anim_options(spec)
    n = len(frames)
    tween = _same_shape(base, frames)
    tags = any(f.get("pointers") for f, _ in frames)      # keep the tag row in every frame
    pics, caps = [], []
    drawn = _anim_pics(base, frames)
    for k, (inner, cap) in enumerate(frames):
        pic = drawn[k]
        on = " is-on" if k == 0 else ""
        hid = "" if k == 0 else ' aria-hidden="true"'
        pics.append(Markup('<div class="viz-frame{}" data-f="{}"{}>{}</div>').format(
            on, k, Markup(hid), Markup(pic)))
        caps.append(Markup('<p class="viz-anim-cap{}" data-f="{}"{}>{}</p>').format(
            on, k, Markup(hid), cap))
    if base in ("array", "string") and tags:
        pics = [Markup(re.sub(r'class="viz-array( viz-str)?"',
                              lambda mt: mt.group(0)[:-1] + ' has-ptr"', str(p))) for p in pics]
    bar = Markup(
        '<div class="viz-anim-bar">'
        '<button type="button" class="viz-anim-btn" data-act="prev" aria-label="Step back" '
        'title="Step back (Left)">{prev}</button>'
        '<button type="button" class="viz-anim-btn viz-anim-play" data-act="play" '
        'aria-label="Play" title="Play / pause (Space)">{play}</button>'
        '<button type="button" class="viz-anim-btn" data-act="next" aria-label="Step forward" '
        'title="Step forward (Right)">{next}</button>'
        '<input type="range" class="viz-anim-scrub" min="0" max="{last}" step="1" value="0" '
        'aria-label="Frame">'
        '<span class="viz-anim-count" aria-hidden="true"><b>1</b>/{n}</span>'
        '<button type="button" class="viz-anim-btn viz-anim-speed" data-act="speed" '
        'aria-label="Speed 1x" title="Speed">1x</button>'
        '</div>').format(prev=_icon("prev"), play=_icon("play"), next=_icon("next"),
                         last=n - 1, n=n)
    cap = _caption(spec)
    label = f"animation, {n} steps" + (f": {cap}" if cap else "")
    html = Markup(
        '<figure class="viz viz-anim viz-anim-{base}" tabindex="0" role="group" '
        'aria-roledescription="animation" aria-label="{label}" data-ms="{ms}" '
        'data-loop="{loop}" data-n="{n}" data-tween="{tw}">'
        '<div class="viz-anim-stage">{pics}</div>'
        '<div class="viz-anim-caps" aria-live="polite">{caps}</div>{bar}{fc}</figure>').format(
            base=base, label=label, ms=ms, loop="1" if loop else "0", n=n,
            tw="1" if tween else "0", pics=Markup("").join(pics), caps=Markup("").join(caps),
            bar=bar, fc=Markup('<figcaption class="viz-cap">{}</figcaption>').format(cap)
            if cap else "")
    if len(html) > MAX_ANIM_HTML:
        raise ValueError("animation too large: fewer frames or smaller inputs")
    return str(html)


def _texts_animate(spec):
    base, frames = _anim_frames(spec)
    out = []
    for inner, cap in frames:
        out.append(cap)
        out += texts(base, inner)
    if spec.get("caption") not in (None, ""):
        out.append(_fmt(spec["caption"]))
    return out


# ---- public --------------------------------------------------------------------------------
_RENDER = {"array": _render_array, "grid": _render_grid, "list": _render_list,
           "tree": _render_tree, "steps": _render_steps, "io": _render_io,
           "html": _render_html, "animate": _render_animate, "graph": _render_graph,
           "intervals": _render_intervals, "compare": _render_compare, "string": _render_string}


def check_kind(kind):
    k = (kind or "").strip().lower()
    if k not in _RENDER:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    return k


def render(kind, spec):
    """HTML for one visual. ValueError on an unknown kind or a bad spec."""
    return _RENDER[check_kind(kind)](_dict(spec))


def texts(kind, spec):
    """Every human-readable string in the spec (for the leak guard): all string leaves and
    the names of pointers/labels; for `html`, the visible text content instead of the markup."""
    kind = check_kind(kind)
    spec = _dict(spec)
    special = {"animate": _texts_animate, "graph": _texts_graph, "compare": _texts_compare}
    if kind in special:
        return [t for t in special[kind](spec) if t and not t.strip().lstrip("-").isdigit()]
    out = []

    def walk(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                out.append(str(k))
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    for key, v in spec.items():
        if kind == "html" and key == "html":
            out.append(sanitize(v if isinstance(v, str) else "")[1])
        else:
            walk(v)
    return [t for t in out if t and not t.strip().lstrip("-").isdigit()]


def caption(spec):
    try:
        return _caption(_dict(spec))
    except ValueError:
        return ""


# ---- statement-embedded blocks: ```viz {json} ``` ------------------------------------------
def parse_block(raw):
    """JSON text of a ```viz block -> (kind, spec), validated by a trial render. ValueError."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as e:
        raise ValueError(f"invalid JSON ({e})")
    if not isinstance(data, dict):
        raise ValueError("block must be a JSON object")
    kind = check_kind(data.get("kind") if isinstance(data.get("kind"), str) else "")
    spec = {k: v for k, v in data.items() if k != "kind"}
    render(kind, spec)
    return kind, spec


_VIZ_FENCE = re.compile(r"^[ \t]*```[ \t]*viz[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*$",
                        re.S | re.M | re.I)


def split_blocks(text):
    """(text_without_viz_blocks, [raw_json, ...]) for a statement."""
    raws = [m.group(1) for m in _VIZ_FENCE.finditer(text or "")]
    return _VIZ_FENCE.sub("", text or ""), raws


def block_html(raw):
    """Rendered figure for one block, or a muted line if it does not parse. Never raw HTML."""
    try:
        kind, spec = parse_block(raw)
        return render(kind, spec)
    except (ValueError, TypeError, RecursionError):
        return '<p class="viz-unavailable">illustration unavailable</p>'
