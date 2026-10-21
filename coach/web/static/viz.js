/* Coach visuals: polls /visuals?since=N every 2 s while the page is visible and appends new
   illustrations under the statement. Newest open, older ones folded to their caption. A dot
   on the folded problem rail (and the mobile Problem tab) marks unseen ones. */
(function () {
  "use strict";
  var panel = document.getElementById("viz-panel");
  var list = document.getElementById("viz-items");
  if (!panel || !list) return;
  var countEl = document.getElementById("viz-count");
  var root = document.getElementById("solve") || document.body;
  var rail = document.getElementById("prob-expand");
  var tab = document.getElementById("tab-prob");
  var last = 0, shown = 0, timer = null, busy = false, handout = null;
  var LABEL = { array: "Array", grid: "Grid", list: "List", tree: "Tree", steps: "Step table",
                io: "Examples", html: "Illustration" };

  function dot(host) {
    if (!host) return null;
    var d = host.querySelector(".viz-dot");
    if (!d) {
      d = document.createElement("span");
      d.className = "viz-dot";
      d.hidden = true;
      d.setAttribute("aria-label", "new illustration");
      host.appendChild(d);
    }
    return d;
  }
  var railDot = dot(rail), tabDot = dot(tab);

  function paneVisible() {
    if (root.classList.contains("prob-folded")) return false;
    var p = document.getElementById("pane-prob");
    return !p || p.offsetParent !== null;
  }
  function markUnread(on) {
    if (railDot) railDot.hidden = !on;
    if (tabDot) tabDot.hidden = !on;
  }
  function seen() { if (paneVisible()) markUnread(false); }
  if (window.MutationObserver) {
    var mo = new MutationObserver(seen);
    mo.observe(root, { attributes: true, attributeFilter: ["class", "data-tab"] });
    if (tab) mo.observe(tab, { attributes: true, attributeFilter: ["aria-selected"] });
  }
  window.addEventListener("resize", seen);

  function item(v) {
    var d = document.createElement("details");
    d.className = "viz-item";
    d.dataset.id = v.id;
    var s = document.createElement("summary");
    s.className = "viz-sum";
    var k = document.createElement("span");
    k.className = "viz-kind";
    k.textContent = LABEL[v.kind] || "Illustration";
    var c = document.createElement("span");
    c.className = "viz-sum-text";
    c.textContent = v.caption || "";
    s.appendChild(k);
    s.appendChild(c);
    var body = document.createElement("div");
    body.className = "viz-body";
    body.innerHTML = v.html;          /* rendered and escaped server-side (coach/viz.py) */
    d.appendChild(s);
    d.appendChild(body);
    return d;
  }

  function reset() {
    list.textContent = "";
    last = 0; shown = 0;
    panel.hidden = true;
    markUnread(false);
  }

  function add(rows) {
    if (!rows.length) return;
    var prev = list.querySelectorAll(".viz-item.is-new");
    for (var i = 0; i < prev.length; i++) { prev[i].classList.remove("is-new"); prev[i].open = false; }
    var el = null;
    rows.forEach(function (v) {
      el = item(v);
      list.appendChild(el);
      last = Math.max(last, v.id);
      shown++;
    });
    el.open = true;
    el.classList.add("is-new");
    panel.hidden = false;
    if (countEl) countEl.textContent = shown > 1 ? String(shown) : "";
    if (paneVisible()) {
      try { el.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }); } catch (e) {}
    } else markUnread(true);
  }

  function schedule(ms) {
    clearTimeout(timer);
    if (document.hidden) { timer = null; return; }
    timer = setTimeout(poll, ms);
  }

  function poll() {
    if (busy) return;
    busy = true;
    fetch("/visuals?since=" + last, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (!r.ok || (r.headers.get("content-type") || "").indexOf("json") < 0) throw new Error(r.status);
        var h = r.headers.get("X-Open-Handout") || "";
        var total = parseInt(r.headers.get("X-Visual-Count") || "", 10);
        return r.json().then(function (rows) { return { rows: rows, h: h, total: total }; });
      })
      .then(function (d) {
        busy = false;
        if (handout !== null && d.h !== handout) { reset(); handout = d.h; schedule(0); return; }
        handout = d.h;
        if (!isNaN(d.total) && d.total < shown + d.rows.length) { reset(); schedule(0); return; }
        add(d.rows);
        schedule(2000);
      })
      .catch(function () { busy = false; schedule(6000); });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) schedule(0);
    else { clearTimeout(timer); timer = null; }
  });
  schedule(300);
})();
