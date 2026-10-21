/* Tests panel under the editor: the coach's cases + his own, Run (Ctrl+'), results per case.
   GET /tests -> {handout_id, mode, entry, cases, last_run}; POST /tests/run {code};
   POST /tests/add {case: json}; POST /tests/delete {id}. Refreshes on the live `tests` event.
   Reads the editor via window.coachEditor.getValue(), else the CodeMirror instance. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var tp = $("tp");
  if (!tp) return;
  var post = window.coach && window.coach.post, toast = (window.coach && window.coach.toast) || function () {};
  var OPEN_KEY = "coach:tests:open";
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };
  var isMac = /Mac|iPhone|iPad/.test(navigator.platform || "");
  if (isMac) tp.querySelectorAll("kbd").forEach(function (k) { if (k.textContent === "Ctrl") k.textContent = "⌘"; });

  var el = {
    toggle: $("tp-toggle"), count: $("tp-count"), sum: $("tp-sum"), run: $("tp-run"),
    add: $("tp-add"), body: $("tp-body"), empty: $("tp-empty"), chips: $("tp-chips"),
    detail: $("tp-detail"), form: $("tp-addf"), fields: $("tp-addf-fields"),
    exp: $("tp-add-exp"), err: $("tp-addf-err"), cancel: $("tp-add-cancel")
  };
  var data = { mode: null, entry: {}, cases: [], last_run: null };
  var results = {};          // case id (or "i<index>") -> result row
  var sel = 0, running = false, lastSeq = 0;

  /* ---------- open / closed, remembered ---------- */
  function setOpen(on, remember) {
    tp.dataset.open = on ? "1" : "0";
    el.toggle.setAttribute("aria-expanded", on ? "true" : "false");
    if (remember) store.set(OPEN_KEY, on ? "1" : "0");
  }
  var saved = store.get(OPEN_KEY);
  setOpen(saved === null ? !matchMedia("(max-width: 640px)").matches : saved === "1", false);
  el.toggle.addEventListener("click", function () { setOpen(tp.dataset.open !== "1", true); });

  /* ---------- formatting (LeetCode style) ---------- */
  function fmt(v) {
    if (v === undefined) return "";
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }
  function inputParts(c) {
    if (data.mode === "design") return [["ops", fmt(c.ops)], ["args", fmt(c.args)]];
    var a = c.args || {};
    return Object.keys(a).map(function (k) { return [k, fmt(a[k])]; });
  }
  function inputLine(c) {
    if (data.mode === "design") return fmt(c.ops) + "  " + fmt(c.args);
    return inputParts(c).map(function (p) { return p[0] + " = " + p[1]; }).join(", ");
  }
  function key(c, i) { return c.id != null ? String(c.id) : "i" + i; }
  function resultFor(c, i) { return results[key(c, i)] || null; }
  function state(r) {
    if (!r) return "idle";
    if (r.error) return r.error === "timeout" ? "tle" : "err";
    if (r.ok === true) return "pass";
    if (r.ok === false) return "fail";
    return "ran";
  }
  var LABEL = { pass: "Passed", fail: "Wrong answer", err: "Runtime error", tle: "Time limit", ran: "Ran", idle: "" };
  var BADGE = { pass: "badge-ok", fail: "badge-danger", err: "badge-danger", tle: "badge-warn", ran: "badge-accent" };

  function node(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function kv(k, v, extra) {
    var row = node("div", "tp-kv" + (extra ? " " + extra : ""));
    row.appendChild(node("div", "tp-k", k));
    var val = node("pre", "tp-v");
    val.textContent = v;
    row.appendChild(val);
    return row;
  }
  function ms(v) {
    if (v == null) return "";
    return v < 1 ? "<1 ms" : (v < 100 ? v.toFixed(1) : Math.round(v)) + " ms";
  }

  /* ---------- render ---------- */
  function renderChips() {
    el.chips.textContent = "";
    data.cases.forEach(function (c, i) {
      var r = resultFor(c, i), st = state(r);
      var b = node("button", "tp-chip");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", i === sel ? "true" : "false");
      b.dataset.state = st;
      b.dataset.i = i;
      b.appendChild(node("span", "tp-dot"));
      b.appendChild(node("span", "", "Case " + (i + 1)));
      if (c.source === "user") { var m = node("em", "tp-mine", "yours"); b.appendChild(m); }
      b.title = (LABEL[st] ? LABEL[st] + ": " : "") + inputLine(c);
      el.chips.appendChild(b);
    });
  }
  function renderDetail() {
    el.detail.textContent = "";
    var c = data.cases[sel];
    if (!c) return;
    var r = resultFor(c, sel), st = state(r);
    var head = node("div", "tp-dhead");
    if (st !== "idle") head.appendChild(node("span", "badge " + BADGE[st], LABEL[st]));
    if (c.label) head.appendChild(node("span", "tp-label", c.label));
    if (r && r.ms != null && st !== "tle") head.appendChild(node("span", "tp-ms", ms(r.ms)));
    if (c.source === "user") {
      var del = node("button", "linkish tp-del", "Remove");
      del.type = "button";
      del.dataset.del = c.id;
      head.appendChild(del);
    }
    if (head.childNodes.length) el.detail.appendChild(head);
    if (r && r.error) el.detail.appendChild(kv(st === "tle" ? "Time limit" : "Error", r.error === "timeout" ? "Stopped after the time limit (infinite loop, or too slow)." : r.error, "tp-errv"));
    if (data.mode === "design") {
      el.detail.appendChild(kv("Operations", fmt(c.ops)));
      el.detail.appendChild(kv("Arguments", fmt(c.args)));
    } else {
      var parts = inputParts(c);
      if (!parts.length) el.detail.appendChild(kv("Input", "(none)"));
      parts.forEach(function (p) { el.detail.appendChild(kv(p[0] + " =", p[1])); });
    }
    if (r && (r.error || r.got !== null || st === "ran" || st === "pass" || st === "fail")) {
      if (!(r.error && r.got === null)) el.detail.appendChild(kv("Output", fmt(r.got), "tp-out tp-" + st));
    }
    if ("expected" in c) el.detail.appendChild(kv("Expected", fmt(c.expected), "tp-exp"));
    if (r && r.stdout) el.detail.appendChild(kv("Stdout", r.stdout.replace(/\n$/, ""), "tp-stdout"));
  }
  function renderSummary() {
    var n = data.cases.length;
    el.count.textContent = String(n);
    el.empty.hidden = n > 0;
    el.chips.hidden = el.detail.hidden = n === 0;
    el.run.disabled = n === 0 || running;
    el.add.hidden = !data.mode;
    var rs = data.cases.map(resultFor).filter(Boolean);
    var st = "idle";
    el.sum.textContent = "";
    if (rs.length) {
      var pass = 0, fail = 0, ran = 0, total = 0;
      data.cases.forEach(function (c, i) {
        var s = state(resultFor(c, i));
        if (s === "pass") pass++; else if (s === "ran") ran++; else if (s !== "idle") fail++;
      });
      var checked = pass + fail;
      rs.forEach(function (r) { total += r.ms || 0; });
      st = fail ? "fail" : (pass ? "pass" : "ran");
      var b = node("span", "tp-sum-n", checked ? pass + "/" + checked + " passed" : ran + " ran");
      el.sum.appendChild(b);
      if (total) el.sum.appendChild(node("span", "tp-sum-ms", ms(total)));
    }
    tp.dataset.state = running ? "running" : st;
  }
  function render() {
    if (sel >= data.cases.length) sel = Math.max(0, data.cases.length - 1);
    renderSummary();
    renderChips();
    renderDetail();
  }

  function takeRun(run) {
    results = {};
    if (!run || !run.results) return;
    run.results.forEach(function (r) {
      var k = r.case_id != null ? String(r.case_id) : "i" + r.case_index;
      results[k] = r;
    });
  }
  function firstBad() {
    for (var i = 0; i < data.cases.length; i++) {
      var s = state(resultFor(data.cases[i], i));
      if (s === "fail" || s === "err" || s === "tle") return i;
    }
    return -1;
  }

  /* ---------- load ---------- */
  function load() {
    var seq = ++lastSeq;
    return fetch("/tests", { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.redirected && /\/login/.test(r.url)) { location.href = "/login"; return null; }
      return r.ok ? r.json() : null;
    }).then(function (d) {
      if (!d || seq !== lastSeq) return;
      var hadCases = data.cases.length;
      data = { mode: d.mode, entry: d.entry || {}, cases: d.cases || [], last_run: d.last_run };
      if (!running) takeRun(d.last_run);
      if (!hadCases && data.cases.length && tp.dataset.open !== "1" && store.get(OPEN_KEY) === null) setOpen(true, false);
      render();
    }).catch(function () {});
  }

  /* ---------- run ---------- */
  function code() {
    if (window.coachEditor && typeof window.coachEditor.getValue === "function") return window.coachEditor.getValue();
    var cmEl = document.querySelector("#ed .CodeMirror");
    if (cmEl && cmEl.CodeMirror) return cmEl.CodeMirror.getValue();
    var ta = $("code");
    return ta ? ta.value : "";
  }
  function editorOpen() {
    var s = document.getElementById("solve");
    return !s || s.dataset.stage === "code";
  }
  function run() {
    if (running) return;
    if (!editorOpen()) { toast("Code opens when your coach says the idea holds.", "warn"); return; }
    if (!data.cases.length) { toast("No tests yet.", "warn"); return; }
    var src = code();
    if (!src.trim()) { toast("Nothing to run yet.", "warn"); return; }
    running = true;
    el.run.classList.add("busy");
    el.run.disabled = true;
    tp.dataset.state = "running";
    el.sum.textContent = "Running…";
    setOpen(true, false);
    var t0 = performance.now();
    post("/tests/run", { code: src }).then(function (r) {
      return r.json().catch(function () { return { error: "Run failed (" + r.status + ")." }; });
    }).then(function (d) {
      running = false;
      el.run.classList.remove("busy");
      if (d.error) { toast(d.error, "error"); render(); return; }
      takeRun(d);
      var bad = firstBad();
      if (bad >= 0) sel = bad;
      tp.dataset.lastRunMs = String(Math.round(performance.now() - t0));
      render();
    }).catch(function () {
      running = false;
      el.run.classList.remove("busy");
      toast("Network error. Not run.", "error");
      render();
    });
  }
  el.run.addEventListener("click", run);
  window.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "'" || e.code === "Quote")) {
      e.preventDefault();
      e.stopPropagation();
      run();
    }
  }, true);

  /* ---------- chips / delete ---------- */
  el.chips.addEventListener("click", function (e) {
    var b = e.target.closest(".tp-chip");
    if (!b) return;
    sel = parseInt(b.dataset.i, 10) || 0;
    renderChips();
    renderDetail();
  });
  el.chips.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    var n = data.cases.length;
    if (!n) return;
    sel = (sel + (e.key === "ArrowRight" ? 1 : n - 1)) % n;
    renderChips();
    renderDetail();
    var b = el.chips.querySelector('[data-i="' + sel + '"]');
    if (b) b.focus();
  });
  el.detail.addEventListener("click", function (e) {
    var b = e.target.closest("[data-del]");
    if (!b) return;
    post("/tests/delete", { id: b.dataset.del }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) toast(d.error, "error"); else load();
    }).catch(function () { toast("Network error.", "error"); });
  });

  /* ---------- add a case ---------- */
  function argNames() {
    var c = data.cases.filter(function (x) { return x.source !== "user"; })[0] || data.cases[0];
    return c && c.args && !Array.isArray(c.args) ? Object.keys(c.args) : [];
  }
  function field(name, label, ph) {
    var l = node("label", "tp-field");
    l.appendChild(node("span", "tp-k", label));
    var inp = node("input", "tp-in");
    inp.type = "text";
    inp.name = name;
    inp.spellcheck = false;
    inp.setAttribute("autocapitalize", "off");
    if (ph) inp.placeholder = ph;
    l.appendChild(inp);
    return l;
  }
  function openAdd() {
    el.fields.textContent = "";
    var first = data.cases[0];
    if (data.mode === "design") {
      el.fields.appendChild(field("ops", "Operations", first ? fmt(first.ops) : '["Cls","op"]'));
      el.fields.appendChild(field("args", "Arguments", first ? fmt(first.args) : "[[],[1]]"));
    } else {
      var names = argNames();
      if (!names.length) el.fields.appendChild(node("p", "tp-hint", "This method takes no arguments."));
      names.forEach(function (n) {
        el.fields.appendChild(field("arg:" + n, n + " =", first && first.args ? fmt(first.args[n]) : ""));
      });
    }
    el.exp.value = "";
    el.err.hidden = true;
    el.form.hidden = false;
    el.add.setAttribute("aria-expanded", "true");
    setOpen(true, false);
    var f = el.fields.querySelector("input");
    if (f) f.focus();
  }
  function closeAdd() {
    el.form.hidden = true;
    el.add.setAttribute("aria-expanded", "false");
  }
  function parse(v, what) {
    try { return JSON.parse(v); } catch (e) { throw new Error(what + " is not valid: " + v); }
  }
  el.add.addEventListener("click", function () { if (el.form.hidden) openAdd(); else closeAdd(); });
  el.cancel.addEventListener("click", closeAdd);
  el.form.addEventListener("keydown", function (e) { if (e.key === "Escape") { e.preventDefault(); closeAdd(); } });
  el.form.addEventListener("submit", function (e) {
    e.preventDefault();
    var c = {};
    try {
      if (data.mode === "design") {
        var ops = el.form.elements.ops.value.trim(), args = el.form.elements.args.value.trim();
        if (!ops) throw new Error("Operations are needed.");
        c.ops = parse(ops, "Operations");
        c.args = args ? parse(args, "Arguments") : c.ops.map(function () { return []; });
      } else {
        c.args = {};
        argNames().forEach(function (n) {
          var v = el.form.elements["arg:" + n].value.trim();
          if (!v) throw new Error(n + " is needed.");
          c.args[n] = parse(v, n);
        });
      }
      var ex = el.exp.value.trim();
      if (ex) c.expected = parse(ex, "Expected");
      var tmpl = data.cases[0];
      if (tmpl && tmpl.types) c.types = tmpl.types;
      if (tmpl && tmpl.unordered) c.unordered = tmpl.unordered;
      if (tmpl && tmpl.inplace) c.inplace = tmpl.inplace;
    } catch (err) {
      el.err.textContent = err.message;
      el.err.hidden = false;
      return;
    }
    post("/tests/add", { "case": JSON.stringify(c) }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { el.err.textContent = d.error; el.err.hidden = false; return; }
      closeAdd();
      return load().then(function () { sel = data.cases.length - 1; render(); });
    }).catch(function () { el.err.textContent = "Network error."; el.err.hidden = false; });
  });

  /* ---------- live ---------- */
  if (window.coachLive) {
    window.coachLive.on("tests", function () { load(); });
    window.coachLive.on("resync", function () { load(); });
    window.coachLive.on("handout", function () { load(); });
  }
  render();
  load();
})();
