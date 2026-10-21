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


def test_list_cycle_and_grid_path():
    out = viz.render("list", SPECS["list"])
    assert "viz-cycle-target" in out and "viz-back" in out
    g = viz.render("grid", SPECS["grid"])
    assert g.count("viz-path") == 2


def test_tree_layout_seven_nodes_has_seven_circles():
    out = viz.render("tree", {"values": [1, 2, 3, 4, 5, 6, 7]})
    assert out.count("<circle") == 7
    assert out.count("<line") == 6
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
    assert m["content"] == "See the problem pane: where does it go wrong?"
    assert m["role"] == "coach" and m["handout_id"] == hid
    service.show_visual(uid, hid, "steps", {"columns": ["i"], "rows": [[0]]})
    assert service.list_messages(uid)[-1]["content"] == "See the problem pane: step table"
    rows = service.visuals(uid, hid)
    assert [v["kind"] for v in rows] == ["array", "steps"]
    assert service.visuals(uid, hid, since_id=rows[0]["id"])[0]["kind"] == "steps"
    assert service.count_visuals(uid, hid) == 2
    assert service.open_handout_id(uid) == hid
    assert service.clear_visuals(uid, hid) == 2 and service.visuals(uid, hid) == []


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


def test_partial_and_assets_exist():
    from pathlib import Path
    web = Path(viz.__file__).parent / "web"
    part = (web / "templates" / "_visuals.html").read_text(encoding="utf-8")
    assert 'id="viz-items"' in part
    js = (web / "static" / "viz.js").read_text(encoding="utf-8")
    assert "/visuals?since=" in js and "visibilitychange" in js
    assert re.search(r"\.viz-hl", (web / "static" / "viz.css").read_text(encoding="utf-8"))
