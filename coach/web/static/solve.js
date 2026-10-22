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
    /* the steps of a problem render in steps.js */
    document.dispatchEvent(new CustomEvent("coach:clock", { detail: c }));
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
    queueServerSave();
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
      if (body && /^Re line \d+:/.test(body.textContent.trim())) continue;   /* a code-box reply */
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
  var lintOut = $("lint-out"), submitBtn = $("submit"), judging = $("judging"), logLink = $("log-link");
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
  function setJudging(on) { if (judging) judging.hidden = !on; }
  function submitted() {
    root.classList.add("has-submission");
    submitBtn.classList.remove("btn-primary");
    logLink.classList.add("btn-primary");
  }
  function submit() {
    if (stage() !== "code") { toast("Code opens when your coach says the idea holds.", "warn"); setTab("think", true); return; }
    var code = getCode();
    if (!code.trim()) { toast("Nothing to submit yet.", "warn"); return; }
    submitBtn.classList.add("busy");
    post("/solve/submit", { code: code }).then(function (r) {
      return r.text().then(function (t) { return { ok: r.ok, html: t }; });
    }).then(function (res) {
      submitBtn.classList.remove("busy");
      if (!res.ok || res.html.indexOf('class="err"') !== -1) { toast("Not submitted. Need code.", "error"); return; }
      clearMarks();
      lintOut.innerHTML = res.html;
      markLines();
      submitted();
      setJudging(true);
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
    if (gotCoach) setJudging(false);
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

  /* ---------- editor tools: server draft, stub, coach boxes, coach comments, scratch ----------
     The draft is autosaved to the server (POST /code/draft, 800 ms debounce) so the coach can
     read it. /code/state carries the stub, the draft, open annotations, pending comment
     inserts and scratch tabs; it is refetched on `editor`, `annotations`, `code_ops`,
     `scratch` and resync. */
  var SAVE_MS = 800, srvT = null, srvPending = false, lastSent = null, edReady = false;
  function cursorLine() { return cm ? cm.getCursor().line + 1 : 1; }
  function queueServerSave() {
    if (!edReady || closing) return;
    srvPending = true;
    clearTimeout(srvT);
    srvT = setTimeout(function () { sendDraft(false); }, SAVE_MS);
  }
  function sendDraft(keep) {
    clearTimeout(srvT);
    if (!srvPending || closing) return;
    srvPending = false;
    var body = { code: getCode(), cursor_line: cursorLine(), handout_id: hid };
    var key = body.code + "\u0000" + body.cursor_line;
    if (key === lastSent) return;
    lastSent = key;
    if (keep) {
      var p = new URLSearchParams();
      Object.keys(body).forEach(function (k) { p.append(k, body[k]); });
      try {
        fetch("/code/draft", { method: "POST", keepalive: true, credentials: "same-origin", body: p,
          headers: Object.assign({ "Content-Type": "application/x-www-form-urlencoded" }, window.coach.headers || {}) });
      } catch (e) {}
      return;
    }
    post("/code/draft", body).then(function (r) { if (!r.ok) lastSent = null; }).catch(function () { lastSent = null; });
  }
  if (cm) cm.on("cursorActivity", queueServerSave);
  window.addEventListener("pagehide", function () { sendDraft(true); });

  function setCode(v) {
    if (cm) { cm.setValue(v); boxes = {}; } else ta.value = v;
  }
  function esc(t) {
    return String(t == null ? "" : t).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function rich(t) { return esc(t).replace(/`([^`\n]+)`/g, "<code>$1</code>"); }

  /* coach comment lines: an overlay for the text, a line class for the row */
  var COACH_RE = /^\s*# coach:/;
  if (cm) {
    cm.addOverlay({ token: function (stream) {
      if (stream.sol() && stream.match(/^\s*# coach:.*$/)) return "coach-note";
      stream.skipToEnd();
      return null;
    } });
    cm.on("renderLine", function (c, line, elt) { if (COACH_RE.test(line.text)) elt.className += " cm-coach-line"; });
  }

  /* annotations: id -> {kind, widget, bookmark, lines: [handles]} */
  var boxes = {};
  function findLine(n0, anchor) {
    var n = cm.lineCount(), ln = Math.min(Math.max(n0 - 1, 0), n - 1);
    if (anchor == null || !String(anchor).trim() || cm.getLine(ln) === anchor) return ln;
    for (var d = 1; d < n; d++) {
      if (ln - d >= 0 && cm.getLine(ln - d) === anchor) return ln - d;
      if (ln + d < n && cm.getLine(ln + d) === anchor) return ln + d;
    }
    return ln;
  }
  function dropBox(id) {
    var b = boxes[id];
    if (!b) return;
    if (b.widget) b.widget.clear();
    if (b.bookmark) b.bookmark.clear();
    (b.lines || []).forEach(function (h) { cm.removeLineClass(h, "background", "cm-hl-line"); cm.removeLineClass(h, "gutter", "cm-hl-gutter"); });
    delete boxes[id];
  }
  function resolveBox(id) {
    dropBox(id);
    post("/code/annotations/" + id + "/resolve", {}).catch(function () {});
  }
  function boxEl(a) {
    var el = document.createElement("div");
    el.className = "ann ann-" + a.kind;
    el.dataset.id = a.id;
    var label = { question: "Coach asks", followup: "Follow-up", note: "Coach note" }[a.kind] || "Coach";
    var html = '<div class="ann-head"><span class="ann-kind">' + label + '</span>' +
      '<button class="ann-x" type="button" aria-label="Dismiss" title="Dismiss">×</button></div>' +
      '<div class="ann-text">' + rich(a.text) + '</div>';
    if (a.kind === "question" || a.kind === "followup") {
      html += a.reply
        ? '<div class="ann-answer"><span>You:</span> ' + rich(a.reply) + '</div>'
        : '<form class="ann-reply" autocomplete="off"><input type="text" class="ann-in" maxlength="2000" placeholder="Your answer" aria-label="Your answer"><button class="btn btn-sm ann-send" type="submit">Send</button></form>';
    }
    el.innerHTML = html;
    el.querySelector(".ann-x").addEventListener("click", function () { resolveBox(a.id); });
    var f = el.querySelector(".ann-reply");
    if (f) {
      var inp = f.querySelector("input");
      inp.addEventListener("keydown", function (e) { e.stopPropagation(); if (e.key === "Escape") { inp.blur(); if (cm) cm.focus(); } });
      f.addEventListener("submit", function (e) {
        e.preventDefault();
        var t = inp.value.trim();
        if (!t) { inp.focus(); return; }
        f.classList.add("busy");
        post("/code/annotations/" + a.id + "/reply", { text: t }).then(function (r) {
          if (!r.ok) throw new Error(r.status);
          var ans = document.createElement("div");
          ans.className = "ann-answer";
          ans.innerHTML = "<span>You:</span> " + rich(t);
          f.replaceWith(ans);
          if (boxes[a.id]) { boxes[a.id].reply = t; if (boxes[a.id].widget) boxes[a.id].widget.changed(); }
          toast("Sent to your coach.", "ok");
          expectReply();
          pollSoon();
        }).catch(function () { f.classList.remove("busy"); toast("Not sent.", "error"); });
      });
    }
    return el;
  }
  function placeBox(a, live) {
    var ln;
    if (a.kind === "highlight") {
      var first = (a.lines && a.lines.length ? a.lines[0] : a.line);
      var off = findLine(first, a.anchor) - (first - 1), handles = [];
      (a.lines || [a.line]).forEach(function (n) {
        var l = n - 1 + off;
        if (l < 0 || l >= cm.lineCount()) return;
        handles.push(cm.addLineClass(l, "background", "cm-hl-line"));
        cm.addLineClass(l, "gutter", "cm-hl-gutter");
      });
      var bm = null;
      ln = Math.max(0, Math.min(cm.lineCount() - 1, first - 1 + off));
      if (a.text) {
        var chip = document.createElement("span");
        chip.className = "hl-chip";
        chip.innerHTML = rich(a.text) + '<button type="button" class="hl-x" aria-label="Dismiss" title="Dismiss">×</button>';
        chip.querySelector(".hl-x").addEventListener("mousedown", function (e) { e.preventDefault(); resolveBox(a.id); });
        bm = cm.setBookmark({ line: ln, ch: cm.getLine(ln).length }, { widget: chip, insertLeft: true, handleMouseEvents: true });
      }
      boxes[a.id] = { kind: a.kind, lines: handles, bookmark: bm };
    } else {
      ln = findLine(a.line, a.anchor);
      var w = cm.addLineWidget(ln, boxEl(a), { coverGutter: false, noHScroll: true });
      boxes[a.id] = { kind: a.kind, widget: w, reply: a.reply || null };
    }
    if (live) cm.scrollIntoView({ line: ln, ch: 0 }, 90);
  }
  function renderAnnotations(list, live) {
    if (!cm) return;
    var want = {};
    list.forEach(function (a) { want[a.id] = a; });
    Object.keys(boxes).forEach(function (id) {
      var a = want[id], b = boxes[id];
      /* gone, or a reply typed in another tab: drop (and rebuild) that box */
      if (!a || (b.widget && (a.reply || null) !== b.reply)) dropBox(id);
    });
    cm.operation(function () {
      list.forEach(function (a) { if (!boxes[a.id]) placeBox(a, live); });
    });
  }

  /* coach comments: claim each pending op on the server first, so it lands once */
  var claimed = {};
  function applyOps(ops) {
    if (!cm) return;
    ops.forEach(function (o) {
      if (claimed[o.id]) return;
      claimed[o.id] = 1;
      post("/code/ops/" + o.id + "/applied", {}).then(function (r) { return r.json(); }).then(function (res) {
        if (!res || !res.first) return;
        var n = cm.lineCount(), ln = Math.max(0, o.line - 1), text = "# coach: " + o.text;
        if (ln >= n) {
          var last = cm.getLine(n - 1);
          cm.replaceRange((last.length ? "\n" : "") + text, { line: n - 1, ch: last.length }, null, "+coach");
          return;
        }
        var probe = ln;
        while (probe < n - 1 && !cm.getLine(probe).trim()) probe++;
        var indent = (cm.getLine(probe).match(/^[ \t]*/) || [""])[0];
        cm.replaceRange(indent + text + "\n", { line: ln, ch: 0 }, null, "+coach");
      }).catch(function () { delete claimed[o.id]; });
    });
  }

  /* scratch tabs: read-only demos from the coach beside solution.py */
  var files = $("ed-files"), scratchBox = $("ed-scratch"), scratchCm = null, scratchById = {}, curFile = "main";
  function renderScratch(list, live) {
    list.forEach(function (sc) {
      scratchById[sc.id] = sc;
      if (files.querySelector('[data-file="' + sc.id + '"]')) return;
      var b = document.createElement("button");
      b.type = "button";
      b.className = "ed-file ed-file-scratch" + (live ? " fresh" : "");
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", "false");
      b.dataset.file = String(sc.id);
      b.textContent = sc.title;
      b.title = sc.title + " (read-only)";
      files.appendChild(b);
      if (live) toast("Coach opened a tab: " + sc.title, "info");
    });
  }
  function selectFile(id) {
    curFile = String(id);
    files.querySelectorAll(".ed-file").forEach(function (b) {
      var on = b.dataset.file === curFile;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
      if (on) b.classList.remove("fresh");
    });
    if (curFile === "main") { scratchBox.hidden = true; if (cm) setTimeout(function () { cm.refresh(); }, 0); return; }
    var sc = scratchById[curFile];
    if (!sc) return;
    scratchBox.hidden = false;
    if (!scratchCm && window.CodeMirror) {
      scratchCm = CodeMirror($("ed-scratch-cm"), { value: "", mode: { name: "python", version: 3 }, theme: "coach",
        lineNumbers: true, readOnly: true, indentUnit: 4, tabSize: 4 });
    }
    if (scratchCm) { scratchCm.setValue(sc.code); setTimeout(function () { scratchCm.refresh(); }, 0); }
    else $("ed-scratch-cm").textContent = sc.code;
  }
  files.addEventListener("click", function (e) {
    var b = e.target.closest(".ed-file");
    if (b) selectFile(b.dataset.file);
  });
  $("scratch-copy").addEventListener("click", function () {
    var sc = scratchById[curFile];
    if (!sc) return;
    var done = function () { toast("Copied.", "ok"); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(sc.code).then(done, function () { toast("Copy failed.", "error"); });
  });

  var stateBusy = false, stateAgain = false, stateLoaded = false;
  function loadCodeState() {
    if (closing) return;
    if (stateBusy) { stateAgain = true; return; }
    stateBusy = true;
    var live = stateLoaded;
    fetch("/code/state", { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (st) {
      if (!st || String(st.handout_id) !== hid || closing) return;
      if (!getCode().trim()) {
        var src = st.draft && st.draft.code ? st.draft.code : (st.stub || "");
        if (src) {
          setCode(src);
          var cl = st.draft && st.draft.code ? st.draft.cursor_line : null;
          if (cm && cl) cm.setCursor({ line: cl - 1, ch: 0 });
          if (!(st.draft && st.draft.code) && live) toast("Starter code loaded.", "info");
        }
      }
      var srv = st.draft ? st.draft.code : "";
      if (!edReady) { edReady = true; if (getCode() !== srv) queueServerSave(); }
      renderAnnotations(st.annotations || [], live);
      applyOps(st.ops || []);
      renderScratch(st.scratch || [], live);
      stateLoaded = true;
    }).catch(function () {}).then(function () {
      stateBusy = false;
      if (!edReady) edReady = true;
      if (stateAgain) { stateAgain = false; loadCodeState(); }
    });
  }
  loadCodeState();
  if (window.coachLive) {
    ["editor", "annotations", "code_ops", "scratch", "resync"].forEach(function (k) { coachLive.on(k, loadCodeState); });
  }

  /* the tests panel reads the editor through this */
  window.coachEditor = { getValue: getCode, cm: cm };

  var savedTab = store.get(P.tab);
  if (savedTab && !wide.matches) setTab(savedTab, true);
  wide.addEventListener("change", function () { if (cm) setTimeout(function () { cm.refresh(); }, 0); });
})();
