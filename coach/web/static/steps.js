/* Steps of a problem: understand -> approach -> code (coach/steps.py).
   The timer shows ONE current step ("Step 1 of 3 · Understand the question 1:20 / 2:00"),
   finished steps as small chips ("✓ Understood 1:40"), and the total beside it (solve.js).
   During step 1 the approach box is hidden; "I understand the question" ends the step on the
   server (/solve/understood) and the page follows the `clock` event. */
(function () {
  "use strict";
  var root = document.getElementById("solve");
  var timer = document.getElementById("timer");
  var box = document.getElementById("t-steps");
  if (!root || !timer || !box) return;
  var post = window.coach.post, toast = window.coach.toast;

  var st = null, run = timer.dataset.run !== "paused", t0 = performance.now();
  try { st = JSON.parse(timer.dataset.steps || "null"); } catch (e) { st = null; }

  function fmt(n) {
    n = Math.max(0, n | 0);
    var m = Math.floor(n / 60), s = n % 60;
    return m + ":" + String(s).padStart(2, "0");
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  var cur = null, curTime = null;
  function render() {
    timer.classList.toggle("has-steps", !!st);
    var was = root.dataset.step;
    root.dataset.step = st && st.current ? st.current : "";
    var title = document.getElementById("think-title");
    if (title && root.dataset.stage === "approach") {
      if (root.dataset.step === "understand") title.textContent = "Question";
      else if (was === "understand") title.textContent = "Approach";
    }
    box.textContent = "";
    if (!st || !st.current) return;
    (st.done || []).forEach(function (d) {
      box.appendChild(el("span", "step-chip", "✓ " + d.label + " " + fmt(d.seconds)));
    });
    cur = el("span", "step-cur");
    cur.appendChild(el("span", "step-n", "Step " + st.index + " of " + st.of));
    cur.appendChild(el("span", "step-sep", "·"));
    cur.appendChild(el("span", "step-t", st.title));
    curTime = el("span", "step-time");
    cur.appendChild(curTime);
    /* a paused clock says so in the step line too (on a phone the total is hidden) */
    var chip = el("span", "step-paused", "Paused");
    chip.hidden = run;
    cur.appendChild(chip);
    box.appendChild(cur);
    tick();
  }
  function tick() {
    if (!st || !st.current || !curTime) return;
    var base = (st.seconds || {})[st.current] || 0;
    var n = base + (run ? Math.floor((performance.now() - t0) / 1000) : 0);
    var budget = ((st.budget || {})[st.current] || 0) * 60;
    curTime.textContent = fmt(n) + " / " + fmt(budget);
    cur.dataset.over = budget && n >= budget ? "1" : "";
    cur.dataset.run = run ? "running" : "paused";
  }
  document.addEventListener("coach:clock", function (ev) {
    var c = ev.detail || {};
    st = c.steps || null;
    run = c.state !== "paused";
    t0 = performance.now();
    render();
  });
  setInterval(tick, 1000);
  render();

  var btn = document.getElementById("understood-btn");
  if (btn) btn.addEventListener("click", function () {
    btn.disabled = true;
    post("/solve/understood", {}).then(function (r) {
      return r.json().catch(function () { return { ok: false }; });
    }).then(function (res) {
      btn.disabled = false;
      if (!res || !res.ok) { toast((res && res.error) || "Not sent. Try again.", "error"); return; }
      if (res.clock) {
        document.dispatchEvent(new CustomEvent("coach:clock", { detail: res.clock }));
      }
      var t = document.getElementById("appr-text");
      if (t) setTimeout(function () { t.focus({ preventScroll: true }); }, 30);
    }).catch(function () { btn.disabled = false; toast("Not sent. Try again.", "error"); });
  });
})();
