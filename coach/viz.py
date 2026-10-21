"""Coach visuals: `render(kind, spec) -> html`, shown as a coach message in the thread.

Kinds: array | grid | list | tree | steps | io | html. Every value is escaped (markupsafe);
styling is by class only (viz.css). `texts(kind, spec)` returns every human-readable string in a
spec, for the leak guard in `service.show_visual`. Bad specs raise ValueError.
"""
import itertools
import json
import re
from html.parser import HTMLParser

from markupsafe import Markup, escape

KINDS = ("array", "grid", "list", "tree", "steps", "io", "html")
KIND_LABEL = {"array": "array", "grid": "grid", "list": "list", "tree": "tree",
              "steps": "step table", "io": "examples", "html": "illustration"}

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
def _render_array(spec):
    vals = _list(spec, "values", limit=MAX_ITEMS)
    n = len(vals)
    hl = _index_set(spec, n)
    ptr = _pointers(spec, n)
    lab = _labels(spec, n)
    cols = []
    for i, v in enumerate(vals):
        p = ptr.get(i)
        cols.append(Markup(
            '<div class="{col}">'
            '<span class="viz-ptr">{ptr}</span>'
            '<span class="{cell}">{v}</span>'
            '<span class="viz-idx">{i}</span>'
            '<span class="viz-label">{lab}</span>'
            '</div>').format(
                col=_cls("viz-col", "viz-col-ptr" if p else ""),
                ptr=", ".join(p) if p else "",
                cell=_cls("viz-cell", "viz-hl" if i in hl else "", "viz-null" if v is None else ""),
                v=_txt(v, "value"), i=i, lab=lab.get(i, "")))
    if not cols:
        cols.append(Markup('<span class="viz-empty">[ ]</span>'))
    body = Markup('<div class="viz-scroll"><div class="viz-array{}">{}</div></div>').format(
        " has-ptr" if ptr else "", Markup("").join(cols))
    return _figure("array-fig", body, _caption(spec))


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


def _render_grid(spec):
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
                         "viz-path" if step else ""),
                v=_txt(v, "value"),
                step=Markup('<sup class="viz-step">{}</sup>').format(step) if step else ""))
        trs.append(Markup('<tr><th class="viz-idx">{}</th>{}</tr>').format(
            r, Markup("").join(tds)))
    body = Markup('<div class="viz-scroll"><table class="viz-grid">{}</table></div>').format(
        Markup("").join(trs))
    return _figure("grid-fig", body, _caption(spec))


# ---- svg helpers ---------------------------------------------------------------------------
_svg_ids = itertools.count(1)
MIN_SCALE = 0.7          # below this the drawing stops shrinking and scrolls sideways instead


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
LL_GAP = 30              # arrow run between boxes
LL_CH = 8.2              # value glyph advance (mono 13px, condensed)
LL_PTR_CH = 6.7          # pointer-tag glyph advance (mono 11px)
LL_LOOP = 44             # bezier control depth of the back-arrow below the row
_POS_CAPTION = re.compile(r"^pos\s*=\s*-?\d+$", re.I)


def _render_list(spec):
    vals = _list(spec, "values", limit=MAX_ITEMS)
    n = len(vals)
    hl = _index_set(spec, n)
    ptr = _pointers(spec, n)
    cyc = spec.get("cycle_to")
    cyc = _int(cyc, "spec.cycle_to") if cyc is not None else None
    if cyc is not None and not 0 <= cyc < n:
        raise ValueError("spec.cycle_to must be an index into values")
    cap = _caption(spec)
    if _POS_CAPTION.match(cap):
        cap = ""
    if not n:
        body = Markup('<div class="viz-scroll"><span class="viz-end">null</span></div>')
        return _figure("list-fig", body, cap)

    texts = [_short(_txt(v, "value"), 8) for v in vals]
    nw = max(44, max(len(t) for t in texts) * LL_CH + 18)
    pitch = nw + LL_GAP
    pad = 6
    ptr_h = 30 if ptr else 0
    idx_h = 16 if cyc is not None else 0
    top = pad + ptr_h + idx_h
    mid_y = top + LL_H / 2
    bot = top + LL_H
    x0 = pad + (max(0, max(len(", ".join(v)) for v in ptr.values()) * LL_PTR_CH + 12 - nw) / 2
                if ptr else 0)

    def left(i):
        return x0 + i * pitch

    def cx(i):
        return left(i) + nw / 2

    uid = next(_svg_ids)
    m_next, m_loop, m_ptr = f"vz{uid}n", f"vz{uid}c", f"vz{uid}p"
    defs = [_marker(m_next, "viz-ll-head")]
    parts = []
    for i in range(n - 1):
        parts.append(Markup('<path class="viz-ll-next" d="M{:.1f},{:.1f} H{:.1f}" '
                            'marker-end="url(#{})"/>').format(
            left(i) + nw + 3, mid_y, left(i + 1) - 3, m_next))
    w = left(n - 1) + nw + pad
    h = bot + pad
    if cyc is None:
        end = left(n - 1) + nw
        parts.append(Markup('<path class="viz-ll-next" d="M{:.1f},{:.1f} H{:.1f}" '
                            'marker-end="url(#{})"/>').format(end + 3, mid_y, end + LL_GAP - 3,
                                                              m_next))
        parts.append(Markup('<text class="viz-ll-null" x="{:.1f}" y="{:.1f}" '
                            'dominant-baseline="central">null</text>').format(
            end + LL_GAP + 2, mid_y))
        w = end + LL_GAP + 34 + pad
    else:
        defs.append(_marker(m_loop, "viz-ll-loop-head"))
        sy = bot + 1
        if cyc == n - 1:
            c = cx(n - 1)
            d = (f"M{c + 9:.1f},{sy:.1f} C{c + 34:.1f},{sy + LL_LOOP:.1f} "
                 f"{c - 34:.1f},{sy + LL_LOOP:.1f} {c - 9:.1f},{sy + 2:.1f}")
        else:
            a, b = cx(n - 1), cx(cyc)
            d = (f"M{a:.1f},{sy:.1f} C{a:.1f},{sy + LL_LOOP:.1f} "
                 f"{b:.1f},{sy + LL_LOOP:.1f} {b:.1f},{sy + 2:.1f}")
        parts.append(Markup('<path class="viz-back viz-ll-loop" d="{}" marker-end="url(#{})"/>')
                     .format(d, m_loop))
        h = sy + LL_LOOP * 0.75 + 4 + pad
    for i, t in enumerate(texts):
        x = left(i)
        g = _cls("viz-ll-node", "viz-hl" if i in hl else "", "viz-null" if vals[i] is None else "")
        parts.append(Markup(
            '<g class="{g}"><rect class="viz-ll-box" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
            'height="{h}" rx="7"/><text class="viz-ll-val" x="{c:.1f}" y="{m:.1f}" '
            'text-anchor="middle" dominant-baseline="central">{t}</text></g>').format(
                g=g, x=x, y=top, w=nw, h=LL_H, c=cx(i), m=mid_y, t=t))
        if cyc is not None:
            parts.append(Markup('<text class="viz-ll-idx" x="{:.1f}" y="{:.1f}" '
                                'text-anchor="middle">{}</text>').format(cx(i), top - 5, i))
    if ptr:
        defs.append(_marker(m_ptr, "viz-ll-ptr-head"))
        for i, names in sorted(ptr.items()):
            label = ", ".join(names)
            tw = len(label) * LL_PTR_CH + 12
            c = cx(i)
            parts.append(Markup(
                '<g class="viz-ll-ptr"><rect class="viz-ll-tag" x="{x:.1f}" y="{y}" '
                'width="{w:.1f}" height="17" rx="4"/><text class="viz-ll-ptr-name" x="{c:.1f}" '
                'y="{ty}" text-anchor="middle" dominant-baseline="central">{t}</text>'
                '<path class="viz-ll-ptr-line" d="M{c:.1f},{y1} V{y2}" marker-end="url(#{m})"/>'
                '</g>').format(x=c - tw / 2, y=pad, w=tw, c=c, ty=pad + 8.5, t=label,
                               y1=pad + 18, y2=top - idx_h - 2, m=m_ptr))
            w = max(w, c + tw / 2 + pad)
    if cyc is None:
        label = "linked list " + ", ".join(texts) + ", then null"
    else:
        label = ("linked list " + ", ".join(texts) +
                 f"; the last node links back to node {cyc} (value {texts[cyc]})")
    svg = _svg("viz-ll", w, h, label,
               Markup("<defs>{}</defs>{}").format(Markup("").join(defs), Markup("").join(parts)))
    body = Markup('<div class="viz-scroll">{}</div>').format(svg)
    return _figure("list-fig", body, cap)


# ---- tree ----------------------------------------------------------------------------------
NODE_R = 15
GAP_X = 36
GAP_Y = 54
PAD = 20


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
            edges.append(Markup('<line class="viz-tree-edge" x1="{}" y1="{}" x2="{}" y2="{}"/>')
                         .format(px, py, x, y))
        s = _txt(v, "value")
        size = "viz-tree-sm" if len(s) > 3 else ""
        circles.append(Markup(
            '<g class="{g}"><circle class="viz-tree-node" cx="{x}" cy="{y}" r="{r}"/>'
            '<text class="{t}" x="{x}" y="{ty}" text-anchor="middle">{s}</text>'
            '<text class="viz-tree-idx" x="{x}" y="{iy}" text-anchor="middle">{k}</text></g>')
            .format(g=_cls("viz-tree-g", "viz-hl" if k in hl else ""), x=x, y=y, r=NODE_R,
                    t=_cls("viz-tree-val", size), ty=y + 4.5, s=s if len(s) <= 6 else s[:5] + "…",
                    iy=y + NODE_R + 11, k=k))
    svg = Markup('<svg class="viz-tree" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
                 'role="img" aria-label="tree">{e}{c}</svg>').format(
        w=w, h=h, e=Markup("").join(edges), c=Markup("").join(circles))
    return _figure("tree-fig", Markup('<div class="viz-scroll">{}</div>').format(svg),
                   _caption(spec))


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


# ---- public --------------------------------------------------------------------------------
_RENDER = {"array": _render_array, "grid": _render_grid, "list": _render_list,
           "tree": _render_tree, "steps": _render_steps, "io": _render_io,
           "html": _render_html}


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
