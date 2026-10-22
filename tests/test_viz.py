"""Coach visuals: the renderer, the html sanitiser, the leak guard, storage, /visuals."""
import re

import pytest
from fastapi.testclient import TestClient

from coach import db, service, viz
from coach.models import Handout, Pattern, Problem, Visual

SPECS = {
    "array": {"values": [1, "<script>alert(1)</script>", 3], "highlight": [0, [1, 2]],
              "labels": {"1": "<b>x</b>"}, "pointers": {"i": 0, "j": 2}, "caption": "c"},
    "grid": {"rows": [["1", "<script>"], ["0", "1"]], "highlight": [[0, 0]],
             "path": [[0, 0], [1, 1]], "caption": "c"},
    "list": {"values": [1, "<script>", 3], "highlight": [1], "pointers": {"slow": 0},
             "cycle_to": 1, "caption": "c"},
    "tree": {"values": [1, 2, "<script>", None, 4], "highlight": [0], "caption": "c"},
    "steps": {"columns": ["i", "seen"], "rows": [[0, "{}"], [1, "<script>"]],
              "highlight_row": 1, "caption": "c"},
    "io": {"cases": [{"input": "[3,3], 6", "output": "<script>", "why": "same index twice"}],
           "caption": "c"},
    "html": {"html": "<p>hi <script>alert(1)</script></p>", "caption": "c"},
}


@pytest.mark.parametrize("kind", sorted(SPECS))
def test_each_kind_renders_and_escapes(kind):
    out = viz.render(kind, SPECS[kind])
    assert f"viz-{kind}" in out
    assert "<script" not in out
    if kind == "tree":                                     # long node values are cut short
        assert "&lt;scri" in out
    elif kind != "html":
        assert "&lt;script&gt;" in out


def test_array_marks_highlight_pointers_labels():
    out = viz.render("array", SPECS["array"])
    assert out.count("viz-cell viz-hl") == 3              # 0 and the range 1..2
    assert ">i<" in out and ">j<" in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out


def _loop_path(out):
    m = re.search(r'<path class="viz-back viz-ll-loop" d="([^"]+)" marker-end="url\(#([\w-]+)\)"',
                  out)
    assert m, out
    assert f'<marker id="{m.group(2)}"' in out
    return m.group(1)


def test_list_cycle_is_a_drawn_back_arrow():
    out = viz.render("list", {"values": [3, 2, 0, -4], "cycle_to": 1})
    assert out.count("<svg") == 1 and "↩" not in out and "&#8617;" not in out
    d = _loop_path(out)
    assert d.startswith("M") and " C" in d                       # one cubic: a U under the row
    assert out.count('class="viz-ll-node') == 4 and out.count('class="viz-ll-next"') == 3
    assert "viz-cycle-target" not in out and ">null<" not in out
    assert [int(x) for x in re.findall(r'class="viz-ll-idx"[^>]*>(\d+)<', out)] == [0, 1, 2, 3]
    ys = [float(y) for y in re.findall(r'class="viz-ll-box" x="[^"]+" y="([^"]+)"', out)]
    iy = float(re.search(r'class="viz-ll-idx" x="[^"]+" y="([^"]+)"', out).group(1))
    assert iy < ys[0]                                           # indices above, arrow below
    start_y = float(d.split()[0].split(",")[1])
    assert start_y > ys[0] + 30


def test_list_no_cycle_ends_in_null_without_indices():
    out = viz.render("list", {"values": [1, 2, 3]})
    assert out.count('class="viz-ll-next"') == 3 and out.rstrip().count(">null</text>") == 1
    assert "viz-ll-idx" not in out and "viz-back" not in out
    nx = float(re.search(r'class="viz-ll-null" x="([^"]+)"', out).group(1))
    vx = max(float(x) for x in re.findall(r'class="viz-ll-val" x="([^"]+)"', out))
    assert nx > vx                                              # null is last, on the right
    assert "null" in viz.render("list", {"values": []})


def test_list_self_loop():
    out = viz.render("list", {"values": [7], "cycle_to": 0})
    d = _loop_path(out)
    xs = [float(p.split(",")[0].lstrip("MC")) for p in d.split() if "," in p]
    assert xs[0] != xs[-1]                                      # leaves and re-enters the node
    with pytest.raises(ValueError):
        viz.render("list", {"values": [7], "cycle_to": 1})


def test_list_caption_dropped_only_when_it_repeats_pos():
    for cap in ("pos = 1", "pos=-1", "POS = 0"):
        assert "viz-cap" not in viz.render("list", {"values": [1], "caption": cap})
    out = viz.render("list", {"values": [1, 2], "caption": "where do they meet?"})
    assert '<figcaption class="viz-cap">where do they meet?</figcaption>' in out


def test_list_highlight_and_pointers():
    out = viz.render("list", {"values": [1, 2, 3, 4], "highlight": [1, 2],
                              "pointers": {"slow": 1, "fast": 3}, "cycle_to": 0})
    assert out.count('class="viz-ll-node viz-hl"') == 2
    assert ">slow<" in out and ">fast<" in out
    assert out.count('class="viz-ll-ptr-line"') == 2
    ids = re.findall(r'<marker id="([\w-]+)"', out)
    assert len(ids) == len(set(ids)) == 3
    again = re.findall(r'<marker id="([\w-]+)"', viz.render("list", {"values": [1]}))
    assert not set(ids) & set(again)                             # unique per drawing


def test_grid_path():
    g = viz.render("grid", SPECS["grid"])
    assert g.count("viz-path") == 2


def test_tree_layout_seven_nodes_has_seven_circles():
    out = viz.render("tree", {"values": [1, 2, 3, 4, 5, 6, 7]})
    assert out.count("<circle") == 7
    assert out.count("<line") == 6
    assert out.count('dominant-baseline="central"') == 7 and "viz-tree-idx" not in out
    lay = viz.tree_layout([1, 2, 3, 4, 5, 6, 7])
    xs = {k: x for k, _, _, x, _, _ in lay}
    assert xs[3] < xs[1] < xs[4] < xs[0] < xs[5] < xs[2] < xs[6]     # in-order


def test_tree_with_gaps_and_depth_limit():
    lay = viz.tree_layout([1, None, 2, None, 3])
    assert [d for _, _, d, _, _, _ in lay] == [0, 1, 2]
    deep = [1] + [x for _ in range(6) for x in (None, 1)]
    with pytest.raises(ValueError):
        viz.render("tree", {"values": deep})


def test_html_kind_strips_handlers_style_and_script():
    src = ('<div class="ok" onclick="x()" style="color:red"><span>a</span>'
           '<script>alert(1)</script><a href="javascript:x">l</a>'
           '<img src=x onerror=alert(1)>'
           '<svg viewBox="0 0 10 10" onload="x()"><circle cx="5" cy="5" r="4" fill="url(#a)" '
           'style="x"/><text x="1" y="2">t</text></svg></div>')
    out = viz.render("html", {"html": src})
    low = out.lower()
    for bad in ("onclick", "style=", "<script", "alert", "href", "<img", "onerror", "onload",
                "url(", "<a"):
        assert bad not in low, bad
    assert 'class="ok"' in out and 'viewBox="0 0 10 10"' in out and 'cx="5"' in out
    assert "<span>a</span>" in out and ">t</text>" in out


def test_bad_kind_and_spec():
    with pytest.raises(ValueError):
        viz.render("pie", {})
    with pytest.raises(ValueError):
        viz.render("array", {"values": "nope"})
    with pytest.raises(ValueError):
        viz.render("grid", {"rows": [[1] * 50]})


# ---- service --------------------------------------------------------------------------------
@pytest.fixture
def hw(tmp_path):
    db.init(str(tmp_path / "v.db"))
    uid = service.create_user("tester", "pw")
    with db.session_scope() as s:
        s.add(Pattern(slug="sliding-window", name="Sliding Window", tier=1))
        s.add(Problem(number=3, title="Longest Substring", lc_slug="longest-substring",
                      difficulty="M", pattern_slug="sliding-window", source="bank"))
        s.flush()
        h = Handout(user_id=uid, problem_number=3, slot="drill", target_min=20)
        s.add(h)
        s.flush()
        hid = h.id
    return uid, hid


def _count_visuals():
    with db.session_scope() as s:
        return len(s.query(Visual).all())


def test_leak_guard_rejects_caption_and_stores_nothing(hw):
    uid, hid = hw
    with pytest.raises(ValueError, match="leak"):
        service.show_visual(uid, hid, "array", {"values": [1, 2], "caption": "the sliding window here"})
    with pytest.raises(ValueError, match="leak"):
        service.show_visual(uid, hid, "array", {"values": [1], "pointers": {"heap": 0}})
    with pytest.raises(ValueError, match="leak"):
        service.show_visual(uid, hid, "io", {"cases": [{"input": "a", "output": "b",
                                                         "why": "classic greedy"}]})
    with pytest.raises(ValueError, match="leak"):
        service.show_visual(uid, hid, "html", {"html": "<p>use <b>two pointers</b></p>"})
    assert _count_visuals() == 0
    assert service.list_messages(uid) == []


def test_show_visual_stores_and_posts(hw):
    uid, hid = hw
    r = service.show_visual(uid, hid, "array", {"values": [1, 2, 3], "highlight": [1],
                                                "caption": "where does it go wrong?"})
    assert r["id"] and "viz-array" in r["html"]
    (m,) = service.list_messages(uid)
    assert m["content"] == "where does it go wrong?" and m["id"] == r["message_id"]
    assert m["role"] == "coach" and m["handout_id"] == hid
    assert m["visual"]["id"] == r["id"] and m["visual"]["html"] == r["html"]
    assert m["visual"]["cleared"] is False
    service.show_visual(uid, hid, "steps", {"columns": ["i"], "rows": [[0]]})
    assert service.list_messages(uid)[-1]["content"] == "step table"
    rows = service.visuals(uid, hid)
    assert [v["kind"] for v in rows] == ["array", "steps"]
    assert service.visuals(uid, hid, since_id=rows[0]["id"])[0]["kind"] == "steps"
    assert service.count_visuals(uid, hid) == 2
    assert service.open_handout_id(uid) == hid
    assert service.clear_visuals(uid, hid) == 2 and service.visuals(uid, hid) == []
    assert service.count_visuals(uid, hid) == 0 and service.clear_visuals(uid, hid) == 0
    msgs = service.list_messages(uid)
    assert len(msgs) == 2 and all(m["visual"]["cleared"] and m["visual"]["html"] == ""
                                  for m in msgs)


def test_show_visual_wrong_user_or_handout(hw):
    uid, hid = hw
    with pytest.raises(ValueError):
        service.show_visual(uid + 99, hid, "array", {"values": [1]})
    with pytest.raises(ValueError):
        service.show_visual(uid, 9999, "array", {"values": [1]})


def test_bridge_tools(hw):
    from bridge import server
    uid, hid = hw
    server.set_service(service, user_id=uid)
    try:
        assert server.show_visual("array", {"values": [1], "caption": "sliding window"})["error"] \
            .startswith("leak:")
        r = server.show_visual("tree", {"values": [1, 2, 3]})
        assert r["ok"] and r["id"]
        assert server.clear_visuals() == {"ok": True, "cleared": 1}
    finally:
        server.set_service(None)


# ---- web route ------------------------------------------------------------------------------
def test_visuals_route(monkeypatch):
    from coach.web import app as webapp
    from tests.fake_service import FakeService
    from tests.test_web import login, start
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    fake = FakeService()
    store = [{"id": 1, "kind": "io", "caption": "try this", "html": "<figure>1</figure>"},
             {"id": 2, "kind": "array", "caption": "", "html": "<figure>2</figure>"}]
    calls = []

    def visuals(uid, handout_id, since_id=0):
        calls.append(handout_id)
        return [v for v in store if v["id"] > since_id]
    fake.visuals = visuals
    fake.count_visuals = lambda uid, handout_id: len(store)
    webapp.app.state.service = fake
    try:
        c = TestClient(webapp.app, follow_redirects=False)
        assert c.get("/visuals").status_code == 303            # login required
        login(c)
        assert c.get("/visuals").json() == []                  # no open handout
        start(c)
        r = c.get("/visuals?since=0")
        assert [v["id"] for v in r.json()] == [1, 2]
        assert set(r.json()[0]) == {"id", "html", "caption", "kind"}
        assert r.headers["X-Visual-Count"] == "2"
        assert [v["id"] for v in c.get("/visuals?since=1").json()] == [2]
        assert calls and all(h == calls[0] for h in calls)
    finally:
        webapp.app.state.service = None


def test_no_pane_panel_left_and_styles_kept():
    from pathlib import Path
    web = Path(viz.__file__).parent / "web"
    assert not (web / "templates" / "_visuals.html").exists()
    assert not (web / "static" / "viz.js").exists()
    solve = (web / "templates" / "solve.html").read_text(encoding="utf-8")
    assert "viz-panel" not in solve and "viz.js" not in solve and "/static/viz.css" in solve
    css = (web / "static" / "viz.css").read_text(encoding="utf-8")
    assert re.search(r"\.viz-hl", css) and "viz-dot" not in css
    js = (web / "static" / "solve.js").read_text(encoding="utf-8")
    assert 'coachLive.on("visual", refreshVisuals)' in js


# ---- the thread: a visual is a coach message -------------------------------------------------
@pytest.fixture
def thread(hw, monkeypatch):
    import re as _re
    from coach.web import app as webapp
    from coach.web.svc import Svc
    from coach.models import User
    uid, hid = hw
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    with db.session_scope() as s:                  # the thread shows today's open handout
        s.get(Handout, hid).handed_at = __import__("datetime").datetime.now()
    webapp.app.state.service = Svc()
    c = TestClient(webapp.app, follow_redirects=False)
    tok = _re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "tester", "password": "pw", "csrf": tok})
    yield c, uid, hid
    webapp.app.state.service = None


def test_visual_renders_as_coach_bubble_in_thread(thread):
    c, uid, hid = thread
    service.post_message(uid, "coach", "before", handout_id=hid)
    r = service.show_visual(uid, hid, "array", {"values": [5, 6], "caption": "look at index 1"})
    service.post_message(uid, "coach", "after", handout_id=hid)
    html = c.get("/messages?since=0").text
    bubble = re.search(r'<div class="msg coach[^"]*has-visual[^"]*" data-id="(\d+)"[^>]*'
                       r'data-visual="(\d+)"', html)
    assert bubble and int(bubble.group(2)) == r["id"] and int(bubble.group(1)) == r["message_id"]
    assert html.index("before") < html.index("viz-array") < html.index("after")
    assert "msg-viz-scroll" in html and "look at index 1" in html
    assert "See the problem pane" not in html
    page = c.get("/solve").text
    assert "viz-array" in page and "viz-panel" not in page


def test_cleared_visual_becomes_muted_line(thread):
    c, uid, hid = thread
    service.show_visual(uid, hid, "io", {"cases": [{"input": "1", "output": "2"}]})
    service.clear_visuals(uid, hid)
    html = c.get("/messages?since=0").text
    assert 'data-cleared="1"' in html and "Illustration cleared." in html
    assert "viz-io" not in html


# ---- animate --------------------------------------------------------------------------------
SPAN = [100, 80, 60, 70, 60, 75, 85]
SPAN_RUNS = [(0, 0), (1, 1), (2, 2), (2, 3), (4, 4), (2, 5), (1, 6)]


def _span_anim():
    return {"base": "array", "ms": 900, "frames": [
        {"spec": {"values": SPAN, "highlight": [[a, b]], "pointers": {"day": b}},
         "caption": f"day {b + 1}: {SPAN[b]} → span {b - a + 1}"} for a, b in SPAN_RUNS]}


def test_animate_renders_every_frame_one_visible_with_player():
    out = viz.render("animate", _span_anim())
    assert out.count('class="viz-frame') == 7
    assert out.count('class="viz-frame is-on"') == 1 and out.count('aria-hidden="true"') >= 12
    assert out.count("<p class=\"viz-anim-cap") == 7 and "day 6: 75 → span 4" in out
    for bit in ('data-act="play"', 'data-act="prev"', 'data-act="next"', 'data-act="speed"',
                'type="range" class="viz-anim-scrub" min="0" max="6"', 'tabindex="0"',
                'data-ms="900"', 'data-loop="0"', 'data-tween="1"'):
        assert bit in out, bit
    assert out.count('data-k="day"') == 7                         # one stable key per frame
    assert re.findall(r'class="viz-col[^"]*" data-i="(\d+)"', out)[:7] == [str(i) for i in range(7)]


def test_animate_list_keys_and_same_frame_size():
    spec = {"base": "list", "loop": True, "frames": [
        {"spec": {"values": [3, 2, 0, -4], "cycle_to": 1, "pointers": {"at": k} if k else {}},
         "caption": f"step {k}"} for k in (0, 1, 2, 3, 1)]}
    out = viz.render("animate", spec)
    assert out.count('class="viz-ll-ptr" data-k="at"') == 4 and 'data-loop="1"' in out
    sizes = set(re.findall(r'<svg class="viz-svg viz-ll" width="(\d+)" height="(\d+)"', out))
    assert len(sizes) == 1                                         # nothing jumps between frames
    assert len(re.findall(r'class="viz-ll-node[^"]*" data-i="3"', out)) == 5


def test_animate_other_bases_cross_fade():
    out = viz.render("animate", {"base": "tree", "frames": [
        {"spec": {"values": [1, 2, 3], "highlight": [0]}}, {"spec": {"values": [1, 2, 3, 4]}}]})
    assert 'data-tween="0"' in out and out.count("viz-tree-fig") == 2
    out = viz.render("animate", {"base": "steps", "frames": [
        {"spec": {"columns": ["day"], "rows": [[1]], "caption": "inner"}},
        {"spec": {"columns": ["day"], "rows": [[1], [2]]}, "caption": "outer"}]})
    assert '<p class="viz-anim-cap is-on" data-f="0">inner</p>' in out and "outer" in out
    assert "viz-cap" not in out.split("viz-anim-caps")[0]          # inner caption moved out


@pytest.mark.parametrize("spec,msg", [
    ({"base": "pie", "frames": []}, "base"),
    ({"base": "animate", "frames": []}, "base"),
    ({"base": "array", "frames": [{"spec": {"values": [1]}}]}, "at least"),
    ({"base": "array", "frames": [{"spec": {"values": [1]}}] * 41}, "at most 40"),
    ({"base": "array", "frames": [{"spec": {"values": [1]}}, {"spec": {"values": "x"}}]},
     "frame 2"),
    ({"base": "grid", "frames": [{"spec": {"rows": [[1]]}}, {"spec": {"rows": [[1] * 50]}}]},
     "frame 2"),
    ({"base": "array", "frames": [{"spec": {"values": [1]}}, "nope"]}, "frame 2"),
    ({"base": "array", "ms": 50, "frames": [{"spec": {"values": [1]}}] * 2}, "ms"),
    ({"base": "array", "loop": "yes", "frames": [{"spec": {"values": [1]}}] * 2}, "loop"),
])
def test_animate_validation(spec, msg):
    with pytest.raises(ValueError, match=msg):
        viz.render("animate", spec)


def test_animate_escapes_and_texts():
    spec = {"base": "array", "frames": [
        {"spec": {"values": ["<script>"], "pointers": {"<b>": 0}}, "caption": "<i>x</i>"},
        {"spec": {"values": ["<script>"]}, "caption": "y"}]}
    out = viz.render("animate", spec)
    assert "<script" not in out and "<i>" not in out and "&lt;i&gt;x" in out
    t = viz.texts("animate", spec)
    assert "<i>x</i>" in t and "y" in t and "<b>" in t and "spec" not in t and "array" not in t


def test_animate_leak_guard_on_frame_caption(hw):
    uid, hid = hw
    spec = {"base": "array", "frames": [
        {"spec": {"values": [1, 2, 3], "highlight": [[0, 1]]}, "caption": "start"},
        {"spec": {"values": [1, 2, 3], "highlight": [[1, 2]]}, "caption": "the window slides"}]}
    with pytest.raises(ValueError, match="leak"):
        service.show_visual(uid, hid, "animate", spec)
    spec["frames"][1]["caption"] = "next"
    spec["frames"][1]["spec"]["pointers"] = {"heap": 1}
    with pytest.raises(ValueError, match="leak"):
        service.show_visual(uid, hid, "animate", spec)
    assert _count_visuals() == 0 and service.list_messages(uid) == []
    del spec["frames"][1]["spec"]["pointers"]
    r = service.show_visual(uid, hid, "animate", spec)
    assert "viz-anim" in r["html"]
    assert service.list_messages(uid)[-1]["content"] == "animation"


def test_animate_rule_zero_on_frame_caption(hw):
    uid, hid = hw
    with db.session_scope() as s:
        s.add(Pattern(slug="linked-list", name="Linked List", tier=1))
        s.get(Handout, hid).pattern_slug = "linked-list"
    spec = {"base": "list", "frames": [{"spec": {"values": [1, 2]}, "caption": "a linked list"},
                                       {"spec": {"values": [1, 2]}, "caption": "b"}]}
    with pytest.raises(ValueError, match="leak"):
        service.show_visual(uid, hid, "animate", spec)


def test_animate_in_statement_block():
    import json as _json
    from coach.web.app import mdblock
    block = "Walk it:\n\n```viz\n" + _json.dumps({"kind": "animate", **_span_anim()}) + "\n```\n"
    out = str(mdblock(block))
    assert "viz-anim" in out and out.count('class="viz-frame') == 7
    assert "illustration unavailable" in str(mdblock(
        '```viz\n{"kind": "animate", "base": "array", "frames": []}\n```'))


def test_animate_bubble_in_thread_and_assets(thread):
    c, uid, hid = thread
    service.show_visual(uid, hid, "animate", _span_anim())
    html = c.get("/messages?since=0").text
    assert "has-visual" in html and "viz-anim" in html and "viz-anim-bar" in html
    page = c.get("/solve").text
    assert page.index("/static/solve.js") < page.index("/static/anim.js")
    from pathlib import Path
    web = Path(viz.__file__).parent / "web" / "static"
    css = (web / "viz.css").read_text(encoding="utf-8")
    assert re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.viz-anim", css)
    js = (web / "anim.js").read_text(encoding="utf-8")
    assert "MutationObserver" in js and "IntersectionObserver" in js and "prefers-reduced-motion" in js
