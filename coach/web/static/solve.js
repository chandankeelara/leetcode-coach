(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var root = $("solve");
  if (!root) return;
  var post = window.coach.post, toast = window.coach.toast;

  var hid = root.dataset.hid;
  var K = { code: "coach:code:" + hid, appr: "coach:appr:" + hid, draft: "coach:apprdraft:" + hid };
  var P = { fold: "coach:probfold", split: "coach:split", psplit: "coach:probsplit", tab: "coach:tab:" + hid };
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
  };
  try {
    for (var i = localStorage.length - 1; i >= 0; i--) {
      var key = localStorage.key(i), m = /^coach:(code|appr|apprdraft|tab):(\d+)$/.exec(key || "");
      if (m && m[2] !== hid) localStorage.removeItem(key);
    }
  } catch (e) {}

  var isMac = /Mac|iPhone|iPad/.test(navigator.platform || "");
  if (isMac) document.querySelectorAll("kbd").forEach(function (k) { if (k.textContent === "Ctrl") k.textContent = "⌘"; });
  function modEnter(e) { return e.key === "Enter" && (e.ctrlKey || e.metaKey); }
  var wide = matchMedia("(min-width: 1100px)");
  var reduce = matchMedia("(prefers-reduced-motion: reduce)");

  /* ---------- timer: the coach's clock ----------
     Rendered from {state, elapsed_s at fetch time}: running counts up from there, paused
     freezes. /handout/state is refetched on every `clock` event. */
  var elapsed0 = parseInt(root.dataset.elapsed, 10) || 0;
  var target = (parseInt(root.dataset.target, 10) || 1) * 60;
  var run = root.dataset.run !== "paused";
  var resets = parseInt(root.dataset.resets, 10) || 0;
  var t0 = performance.now();
  var tNow = $("t-now"), tFill = $("t-fill"), timer = $("timer"), tState = $("t-state");
  var tPaused = $("t-paused"), tPhase = $("t-phase"), tTarget = $("t-target");
  var baseTitle = root.dataset.title;
  function fmt(n) {
    var h = Math.floor(n / 3600), mi = Math.floor((n % 3600) / 60), s = n % 60;
    var mm = String(mi).padStart(2, "0"), ss = String(s).padStart(2, "0");
    return h ? h + ":" + mm + ":" + ss : mm + ":" + ss;
  }
  var startedAt = null;   /* ISO from /handout/state; moves on a reset (title only) */
  function applyClock(c) {
    if (!c) return;
    elapsed0 = Math.max(0, c.elapsed_s | 0);
    t0 = performance.now();
    run = c.state !== "paused";
    timer.dataset.run = run ? "running" : "paused";
    tPaused.hidden = run;
    tPaused.title = run ? "" : (c.pause_reason || "");
    tPhase.textContent = c.phase || "";
    if (c.target_min) {
      target = c.target_min * 60;
      tTarget.textContent = String(c.target_min).padStart(2, "0") + ":00";
    }
    if ((c.resets | 0) > resets) toast("Clock reset.", "info");
    resets = c.resets | 0;
    tick();
  }
  function tick() {
    var n = elapsed0 + (run ? Math.floor((performance.now() - t0) / 1000) : 0);
    tNow.textContent = fmt(n);
    tFill.style.transform = "scaleX(" + Math.min(1, n / (target * 1.5)) + ")";
    var st = n >= target * 1.5 ? "way-over" : n >= target ? "over" : "";
    if (timer.dataset.state !== st) {
      timer.dataset.state = st;
      tState.textContent = st === "way-over" ? "well past target" : st === "over" ? "past target" : "";
    }
    document.title = fmt(n) + " · " + baseTitle;
  }
  tick();
  setInterval(tick, 1000);

  /* ---------- panes: tabs, problem fold, split ---------- */
  var ws = $("ws"), ed = $("ed");
  var tabs = root.querySelectorAll(".tab");
  function stage() { return root.dataset.stage; }
  function setTab(name, quiet) {
    root.dataset.tab = name;
    tabs.forEach(function (t) { t.setAttribute("aria-selected", t.dataset.go === name ? "true" : "false"); });
    if (name === "think") { $("tab-unread").hidden = true; toBottom(false); }
    if (name === "code" && cm) setTimeout(function () { cm.refresh(); }, 0);
    store.set(P.tab, name);
  }
  tabs.forEach(function (t) { t.addEventListener("click", function () { setTab(t.dataset.go); }); });
  root.querySelector(".tabs").addEventListener("keydown", function (e) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    var order = ["prob", "think", "code"], i = order.indexOf(root.dataset.tab);
    i = (i + (e.key === "ArrowRight" ? 1 : 2)) % 3;
    setTab(order[i], true);
    $("tab-" + order[i]).focus();
  });

  function setFold(on) {
    root.classList.toggle("prob-folded", on);
    store.set(P.fold, on ? "1" : "0");
    if (cm) setTimeout(function () { cm.refresh(); }, 220);
  }
  $("prob-collapse").addEventListener("click", function () { setFold(true); $("prob-expand").focus(); });
  $("prob-expand").addEventListener("click", function () { setFold(false); $("prob-collapse").focus(); });
  if (store.get(P.fold) === "1") root.classList.add("prob-folded");

  var split = $("split"), think = $("pane-think");
  var MIN_MID = 320, MIN_ED = 380;
  function applySplit(px) {
    var total = think.offsetWidth + split.offsetWidth + ed.offsetWidth;
    if (!total) return;
    var w = Math.max(MIN_MID, Math.min(total - split.offsetWidth - MIN_ED, px));
    ws.style.setProperty("--mid", Math.round(w) + "px");
    return w;
  }
  var savedSplit = parseInt(store.get(P.split), 10);
  if (savedSplit) ws.style.setProperty("--mid", savedSplit + "px");
  split.addEventListener("pointerdown", function (e) {
    if (!wide.matches) return;
    e.preventDefault();
    split.setPointerCapture(e.pointerId);
    var left = think.getBoundingClientRect().left;
    root.classList.add("dragging");
    function move(ev) { applySplit(ev.clientX - left); }
    function up() {
      root.classList.remove("dragging");
      split.removeEventListener("pointermove", move);
      split.removeEventListener("pointerup", up);
      split.removeEventListener("pointercancel", up);
      store.set(P.split, String(Math.round(think.offsetWidth)));
      if (cm) cm.refresh();
    }
    split.addEventListener("pointermove", move);
    split.addEventListener("pointerup", up);
    split.addEventListener("pointercancel", up);
  });
  split.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    var w = applySplit(think.offsetWidth + (e.key === "ArrowRight" ? 32 : -32));
    if (w) store.set(P.split, String(Math.round(w)));
    if (cm) cm.refresh();
  });
  split.addEventListener("dblclick", function () {
    ws.style.removeProperty("--mid");
    store.del(P.split);
    if (cm) cm.refresh();
  });

  var psplit = $("psplit"), probPane = $("pane-prob");
  var MIN_PROB = 240;
  function probMax() {
    var room = ws.clientWidth - 9 - MIN_MID - MIN_ED;
    return Math.max(MIN_PROB, Math.min(Math.floor(window.innerWidth / 2), room));
  }
  function applyProb(px) {
    var w = Math.round(Math.max(MIN_PROB, Math.min(probMax(), px)));
    ws.style.setProperty("--pwu", w + "px");
    psplit.setAttribute("aria-valuenow", String(w));
    fitCols();
    return w;
  }
  function fitCols() {
    var over = ws.scrollWidth - ws.clientWidth;
    if (over > 0 && ws.style.getPropertyValue("--mid")) {
      ws.style.setProperty("--mid", Math.max(MIN_MID, think.offsetWidth - over) + "px");
    }
  }
  psplit.setAttribute("aria-valuemin", String(MIN_PROB));
  var savedProb = parseInt(store.get(P.psplit), 10);
  if (savedProb && wide.matches) applyProb(savedProb);
  psplit.addEventListener("pointerdown", function (e) {
    if (!wide.matches) return;
    e.preventDefault();
    psplit.setPointerCapture(e.pointerId);
    var left = ws.getBoundingClientRect().left;
    root.classList.add("dragging");
    function move(ev) { applyProb(ev.clientX - left); }
    function up() {
      root.classList.remove("dragging");
      psplit.removeEventListener("pointermove", move);
      psplit.removeEventListener("pointerup", up);
      psplit.removeEventListener("pointercancel", up);
      store.set(P.psplit, String(Math.round(probPane.offsetWidth)));
      if (cm) cm.refresh();
    }
    psplit.addEventListener("pointermove", move);
    psplit.addEventListener("pointerup", up);
    psplit.addEventListener("pointercancel", up);
  });
  psplit.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    var w = applyProb(probPane.offsetWidth + (e.key === "ArrowRight" ? 32 : -32));
    store.set(P.psplit, String(w));
    if (cm) cm.refresh();
  });
  psplit.addEventListener("dblclick", function () {
    ws.style.removeProperty("--pwu");
    psplit.removeAttribute("aria-valuenow");
    store.del(P.psplit);
    if (cm) cm.refresh();
  });
  window.addEventListener("resize", function () {
    var cur = parseInt(ws.style.getPropertyValue("--pwu"), 10);
    if (wide.matches && cur) applyProb(cur);
  });

  /* ---------- editor ---------- */
  var ta = $("code");
  var cm = null;
  var saved = store.get(K.code);
  if (saved) ta.value = saved;
  if (window.CodeMirror) {
    cm = CodeMirror.fromTextArea(ta, {
      mode: { name: "python", version: 3 },
      theme: "coach",
      lineNumbers: true,
      indentUnit: 4,
      tabSize: 4,
      indentWithTabs: false,
      matchBrackets: true,
      autoCloseBrackets: true,
      styleActiveLine: true,
      readOnly: "nocursor",
      extraKeys: {
        Tab: function (c) {
          if (c.somethingSelected()) c.indentSelection("add");
          else c.replaceSelection("    ", "end");
        },
        "Shift-Tab": function (c) { c.indentSelection("subtract"); },
        "Ctrl-Enter": function () { submit(); },
        "Cmd-Enter": function () { submit(); },
        "Ctrl-/": "toggleComment",
        "Cmd-/": "toggleComment"
      }
    });
  } else {
    ta.readOnly = true;
    ta.addEventListener("keydown", function (e) {
      if (modEnter(e)) { e.preventDefault(); submit(); }
      else if (e.key === "Tab") {
        e.preventDefault();
        var s = ta.selectionStart;
        ta.setRangeText("    ", s, ta.selectionEnd, "end");
        onCodeChange();
      }
    });
    ta.addEventListener("input", onCodeChange);
  }
  function getCode() { return cm ? cm.getValue() : ta.value; }

  var savedEl = $("ed-saved"), saveT;
  function onCodeChange() {
    clearTimeout(saveT);
    savedEl.textContent = "editing";
    saveT = setTimeout(function () {
      var v = getCode();
      if (v) store.set(K.code, v); else store.del(K.code);
      var d = new Date();
      savedEl.textContent = "draft saved " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    }, 450);
  }
  if (cm) cm.on("change", onCodeChange);
  if (saved) savedEl.textContent = "draft restored";

  function openEditor(first) {
    var was = stage();
    root.dataset.stage = "code";
    fitCols();
    $("think-title").textContent = "Discussion";
    $("ed-lock").hidden = true;
    if (cm) cm.setOption("readOnly", false); else ta.readOnly = false;
    if (first && was !== "code") {
      ed.classList.add("entering");
      setTimeout(function () { ed.classList.remove("entering"); }, reduce.matches ? 0 : 420);
      if (!wide.matches) setTab("code", true);
      setTimeout(function () {
        if (cm) { cm.refresh(); cm.focus(); } else ta.focus();
      }, reduce.matches ? 0 : 60);
    } else if (cm) setTimeout(function () { cm.refresh(); }, 0);
  }

  /* The coach opens the editor (editor_open on the server). Until then, after an approach,
     the page sits in "discuss": chat visible, editor locked. */
  function lockEditor() {
    if (stage() !== "code") return;
    root.dataset.stage = "discuss";
    fitCols();
    $("ed-lock").hidden = false;
    if (cm) cm.setOption("readOnly", "nocursor"); else ta.readOnly = true;
    if (root.dataset.tab === "code") setTab("think", true);
  }
  function enterDiscuss() {
    if (stage() !== "approach") return;
    root.dataset.stage = "discuss";
    $("think-title").textContent = "Discussion";
    fitCols();
  }

  /* ---------- approach ---------- */
  var apprForm = $("appr"), apprText = $("appr-text"), apprSend = $("appr-send");
  var draft = store.get(K.draft);
  if (draft) apprText.value = draft;
  apprText.addEventListener("input", function () { store.set(K.draft, apprText.value); });

  var revising = false;
  if (stage() === "code") openEditor(false);
  else if (stage() === "discuss" || store.get(K.appr)) enterDiscuss();
  else setTimeout(function () { if (wide.matches || root.dataset.tab === "think") apprText.focus({ preventScroll: true }); }, 50);

  function markApproach() {
    var t = (store.get(K.appr) || "").trim();
    var mine = log.querySelectorAll('.msg[data-role="user"]');
    for (var j = 0; j < mine.length; j++) {
      if (mine[j].classList.contains("is-appr")) continue;
      var body = mine[j].querySelector(".msg-body");
      if (j === 0 || (t && body && body.textContent.trim() === t)) {
        mine[j].classList.add("is-appr");
        mine[j].classList.remove("cont");
        var who = mine[j].querySelector(".msg-who");
        if (who) who.textContent = j === 0 ? "Your approach" : "Revised approach";
      }
    }
  }

  apprText.addEventListener("keydown", function (e) {
    if (modEnter(e)) { e.preventDefault(); sendApproach(); }
    else if (e.key === "Escape" && revising) endRevise();
  });
  apprForm.addEventListener("submit", function (e) { e.preventDefault(); sendApproach(); });
  function sendApproach() {
    var text = apprText.value.trim();
    if (!text) { toast("Write the idea first, in plain words.", "warn"); apprText.focus(); return; }
    apprSend.classList.add("busy");
    post("/solve/approach", { approach: text }).then(function (r) {
      return r.text().then(function (t) { return { ok: r.ok && t.indexOf('class="ok"') !== -1 }; });
    }).then(function (res) {
      apprSend.classList.remove("busy");
      if (!res.ok) { toast("Not sent. Is the problem still open?", "error"); return; }
      var first = stage() === "approach";
      store.set(K.appr, text);
      store.del(K.draft);
      apprText.value = "";
      endRevise();
      enterDiscuss();
      expectReply();
      pollSoon();
      toast(first ? "Approach sent. Your coach will open the editor." : "Revised approach sent.", "ok");
    }).catch(function () {
      apprSend.classList.remove("busy");
      toast("Network error. Approach not sent.", "error");
    });
  }
  function endRevise() {
    revising = false;
    root.classList.remove("revising");
  }
  $("appr-cancel").addEventListener("click", function () { endRevise(); input.focus(); });
  $("appr-again").addEventListener("click", function () {
    revising = true;
    root.classList.add("revising");
    apprText.focus();
  });

  /* ---------- submit ---------- */
  var lintOut = $("lint-out"), submitBtn = $("submit"), seg = $("verdict"), logLink = $("log-link");
  var marked = [];
  function clearMarks() {
    if (!cm) return;
    marked.forEach(function (h) { cm.removeLineClass(h, "background", "cm-lint-line"); cm.removeLineClass(h, "gutter", "cm-lint-gutter"); });
    marked = [];
  }
  function markLines() {
    if (!cm) return;
    lintOut.querySelectorAll("[data-line]").forEach(function (b) {
      var ln = parseInt(b.dataset.line, 10) - 1;
      if (ln < 0 || ln >= cm.lineCount()) return;
      var h = cm.addLineClass(ln, "background", "cm-lint-line");
      cm.addLineClass(ln, "gutter", "cm-lint-gutter");
      marked.push(h);
    });
  }
  lintOut.addEventListener("click", function (e) {
    var b = e.target.closest("[data-line]");
    if (!b) return;
    var ln = parseInt(b.dataset.line, 10) - 1;
    if (cm) { cm.focus(); cm.setCursor({ line: Math.max(0, ln), ch: 0 }); cm.scrollIntoView(null, 120); }
  });
  seg.addEventListener("change", function () { seg.classList.remove("invalid"); });
  function submitted() {
    root.classList.add("has-submission");
    submitBtn.classList.remove("btn-primary");
    logLink.classList.add("btn-primary");
  }
  function submit() {
    if (stage() !== "code") { toast("Code opens when your coach says the idea holds.", "warn"); setTab("think", true); return; }
    var code = getCode();
    if (!code.trim()) { toast("Nothing to submit yet.", "warn"); return; }
    var v = seg.querySelector("input:checked");
    if (!v) {
      seg.classList.add("invalid");
      toast("Pick what LeetCode said: Accepted, Wrong answer, TLE or Runtime error.", "warn");
      var f = seg.querySelector("input"); if (f) f.focus();
      return;
    }
    submitBtn.classList.add("busy");
    post("/solve/submit", { code: code, verdict: v.value }).then(function (r) {
      return r.text().then(function (t) { return { ok: r.ok, html: t }; });
    }).then(function (res) {
      submitBtn.classList.remove("busy");
      if (!res.ok || res.html.indexOf('class="err"') !== -1) { toast("Not submitted. Need code and a result.", "error"); return; }
      clearMarks();
      lintOut.innerHTML = res.html;
      markLines();
      submitted();
      var n = lintOut.querySelectorAll("[data-line]").length;
      toast(n ? "Submitted. " + n + " lint finding" + (n === 1 ? "" : "s") + "." : "Submitted. Lint clean.", n ? "warn" : "ok");
      expectReply();
      pollSoon();
    }).catch(function () {
      submitBtn.classList.remove("busy");
      toast("Network error. Not submitted.", "error");
    });
  }
  submitBtn.addEventListener("click", submit);

  /* ---------- stuck / abandon ---------- */
  var stuckBtn = $("stuck");
  stuckBtn.addEventListener("click", function () {
    stuckBtn.classList.add("busy");
    post("/solve/stuck", {}).then(function (r) {
      stuckBtn.classList.remove("busy");
      if (!r.ok) throw new Error();
      toast("Coach told. Hang on.", "ok");
      if (!wide.matches) setTab("think", true);
      expectReply();
      pollSoon();
    }).catch(function () { stuckBtn.classList.remove("busy"); toast("Could not reach the coach.", "error"); });
  });
  var dlg = $("abandon-dlg");
  $("abandon").addEventListener("click", function () {
    if (dlg.showModal) dlg.showModal();
    else if (confirm("Abandon this problem?")) dlg.querySelector("form").submit();
  });
  $("abandon-cancel").addEventListener("click", function () { dlg.close(); });

  /* ---------- chat ---------- */
  var log = $("chat-log"), typing = $("typing"), empty = $("chat-empty");
  var form = $("chat-form"), input = $("chat-text");
  var last = parseInt(root.dataset.last, 10) || 0;
  var waitingSince = null, waitT;
  markApproach();

  function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 80; }
  function toBottom(smooth) { log.scrollTo({ top: log.scrollHeight, behavior: smooth && !reduce.matches ? "smooth" : "auto" }); }
  toBottom(false);

  function expectReply() {
    waitingSince = last;
    typing.hidden = false;
    log.appendChild(typing);
    toBottom(true);
    clearTimeout(waitT);
    waitT = setTimeout(stopTyping, 180000);
  }
  function stopTyping() { waitingSince = null; typing.hidden = true; clearTimeout(waitT); }

  function append(html) {
    var tpl = document.createElement("template");
    tpl.innerHTML = html;
    var nodes = Array.prototype.slice.call(tpl.content.querySelectorAll(".msg"));
    if (!nodes.length) return 0;
    var stick = nearBottom();
    var prev = log.querySelectorAll(".msg");
    var prevRole = prev.length ? prev[prev.length - 1].dataset.role : null;
    var gotCoach = false, added = 0;
    nodes.forEach(function (n) {
      var id = parseInt(n.dataset.id, 10);
      if (log.querySelector('.msg[data-id="' + id + '"]')) return;
      n.classList.toggle("cont", n.dataset.role === prevRole);
      n.classList.add("new");
      prevRole = n.dataset.role;
      log.insertBefore(n, typing);
      added++;
      if (id > last) last = id;
      if (n.dataset.role === "coach") gotCoach = true;
    });
    if (!added) return 0;
    empty.hidden = true;
    root.classList.add("has-msgs");
    markApproach();
    if (gotCoach && waitingSince !== null) stopTyping();
    if (stick || !gotCoach) toBottom(true);
    if (gotCoach && !wide.matches && root.dataset.tab !== "think") $("tab-unread").hidden = false;
    return added;
  }
  if (log.querySelector(".msg")) root.classList.add("has-msgs");

  /* ---------- live: every coach write arrives on the shared stream (static/live.js) ---------- */
  var inflight = false, again = false, closing = false;
  var live = $("live"), liveTxt = $("live-txt");
  function setLive(on) { live.dataset.state = on ? "on" : "off"; liveTxt.textContent = on ? "Live" : "Offline"; }
  function applyEditor(ed) {
    if (ed === "1" && stage() !== "code") { enterDiscuss(); openEditor(true); toast("Editor open.", "ok"); }
    else if (ed === "0" && stage() === "code") lockEditor();
  }
  function pollSoon() { fetchMessages(); }
  /* A `visual` event after a clear: swap each illustration bubble the server now marks
     cleared for its muted line. New illustrations arrive as messages (fetchMessages). */
  function refreshVisuals() {
    if (closing || !log.querySelector('.msg[data-visual]:not([data-cleared])')) return;
    fetch("/messages?since=0", { credentials: "same-origin", headers: { "Accept": "text/html" } }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (t) {
      var tpl = document.createElement("template");
      tpl.innerHTML = t;
      log.querySelectorAll('.msg[data-visual]:not([data-cleared])').forEach(function (el) {
        var fresh = tpl.content.querySelector('.msg[data-id="' + el.dataset.id + '"]');
        if (fresh && fresh.hasAttribute("data-cleared")) {
          fresh.className = el.className.replace(/\bnew\b/, "");
          fresh.setAttribute("data-cleared", "1");
          el.replaceWith(fresh);
        }
      });
    }).catch(function () {});
  }
  function fetchMessages() {
    if (closing) return;
    if (inflight) { again = true; return; }
    inflight = true;
    fetch("/messages?since=" + last, { credentials: "same-origin", headers: { "Accept": "text/html" } }).then(function (r) {
      if (r.redirected && /\/login/.test(r.url)) { location.href = "/login"; return; }
      if (!r.ok) throw new Error(r.status);
      var open = r.headers.get("X-Open-Handout");
      var ed = r.headers.get("X-Editor-Open");
      return r.text().then(function (t) {
        append(t);
        applyEditor(ed);
        if (open !== null && open !== hid) closed(open);
      });
    }).catch(function () {}).then(function () {
      inflight = false;
      if (again) { again = false; fetchMessages(); }
    });
  }
  function fetchState() {
    if (closing) return;
    fetch("/handout/state", { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.redirected && /\/login/.test(r.url)) { location.href = "/login"; return null; }
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function (st) {
      if (!st || closing) return;
      var open = st.open_handout_id === null ? "" : String(st.open_handout_id);
      if (open !== hid) { closed(open); return; }
      applyEditor(st.editor_open ? "1" : "0");
      if (st.clock) applyClock(st.clock);
      else if (st.target_min) target = st.target_min * 60;
      if (st.started_at && st.started_at !== startedAt) {
        startedAt = st.started_at;
        var d = new Date(st.started_at);
        if (!isNaN(d)) timer.title = "Started " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
      }
    }).catch(function () {});
  }
  var stmt = $("stmt-body"), stmtHtml = null;
  function fetchStatement() {
    if (!stmt || closing) return;
    fetch("/statement", { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function (d) {
      if (d.handout_id !== null && String(d.handout_id) !== hid) return;
      if (!d.html || d.html === stmtHtml) return;
      var first = stmt.hasAttribute("data-pending");
      stmtHtml = d.html;
      stmt.innerHTML = d.html;
      stmt.removeAttribute("data-pending");
      if (!first) toast("Statement updated.", "info");
    }).catch(function () {});
  }
  function closed(open) {
    if (closing) return;
    closing = true;
    if (open) { toast("A different problem is open. Reloading.", "warn"); setTimeout(function () { location.href = "/solve"; }, 900); }
    else { toast("This problem was closed.", "info"); setTimeout(function () { location.href = "/reveal/" + hid; }, 900); }
  }
  if (window.coachLive) {
    coachLive.onState(function (st) { setLive(st !== "failed"); });
    coachLive.on("message", fetchMessages);
    coachLive.on("statement", fetchStatement);
    coachLive.on("visual", refreshVisuals);
    coachLive.on("editor", fetchState);
    coachLive.on("handout", fetchState);
    coachLive.on("clock", fetchState);
    coachLive.on("resync", function () {
      fetchMessages(); fetchState();
      if (stmt && stmt.hasAttribute("data-pending")) fetchStatement();
    });
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(168, input.scrollHeight) + "px";
  }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      sendChat();
    } else if (e.key === "Escape") input.blur();
  });
  form.addEventListener("submit", function (e) { e.preventDefault(); sendChat(); });
  function sendChat() {
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    autosize();
    post("/solve/chat", { text: text }).then(function (r) {
      if (!r.ok) throw new Error();
      expectReply();
      pollSoon();
    }).catch(function () {
      input.value = text;
      autosize();
      toast("Message not sent.", "error");
    });
  }

  var savedTab = store.get(P.tab);
  if (savedTab && !wide.matches) setTab(savedTab, true);
  wide.addEventListener("change", function () { if (cm) setTimeout(function () { cm.refresh(); }, 0); });
})();
