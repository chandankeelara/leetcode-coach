(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var root = $("solve");
  if (!root) return;
  var post = window.coach.post, toast = window.coach.toast;

  var hid = root.dataset.hid;
  var K = { code: "coach:code:" + hid, appr: "coach:appr:" + hid, draft: "coach:apprdraft:" + hid };
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
  };
  try {
    for (var i = localStorage.length - 1; i >= 0; i--) {
      var key = localStorage.key(i), m = /^coach:(code|appr|apprdraft):(\d+)$/.exec(key || "");
      if (m && m[2] !== hid) localStorage.removeItem(key);
    }
  } catch (e) {}

  var isMac = /Mac|iPhone|iPad/.test(navigator.platform || "");
  if (isMac) document.querySelectorAll("kbd").forEach(function (k) { if (k.textContent === "Ctrl") k.textContent = "⌘"; });
  function modEnter(e) { return e.key === "Enter" && (e.ctrlKey || e.metaKey); }

  /* ---------- timer ---------- */
  var elapsed0 = parseInt(root.dataset.elapsed, 10) || 0;
  var target = (parseInt(root.dataset.target, 10) || 1) * 60;
  var t0 = performance.now();
  var tNow = $("t-now"), tFill = $("t-fill"), timer = $("timer"), tState = $("t-state");
  var baseTitle = root.dataset.title;
  function fmt(n) {
    var h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = n % 60;
    var mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
    return h ? h + ":" + mm + ":" + ss : mm + ":" + ss;
  }
  function tick() {
    var n = elapsed0 + Math.floor((performance.now() - t0) / 1000);
    tNow.textContent = fmt(n);
    tFill.style.width = Math.min(100, (n / (target * 1.5)) * 100) + "%";
    var st = n >= target * 1.5 ? "way-over" : n >= target ? "over" : "";
    if (timer.dataset.state !== st) {
      timer.dataset.state = st;
      tState.textContent = st === "way-over" ? "well past target" : st === "over" ? "past target" : "";
    }
    document.title = fmt(n) + " · " + baseTitle;
  }
  tick();
  setInterval(tick, 1000);

  /* ---------- editor ---------- */
  var ta = $("code");
  var ed = $("ed");
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
      viewportMargin: Infinity,
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

  function unlock(focus) {
    ed.classList.remove("locked");
    $("ed-lock").hidden = true;
    if (cm) { cm.setOption("readOnly", false); cm.refresh(); if (focus) cm.focus(); }
    else { ta.readOnly = false; if (focus) ta.focus(); }
  }

  /* ---------- approach ---------- */
  var apprForm = $("appr-form"), apprText = $("appr-text"), apprSend = $("appr-send");
  var apprSent = $("appr-sent"), apprCard = $("appr");
  var draft = store.get(K.draft);
  if (draft) apprText.value = draft;
  apprText.addEventListener("input", function () { store.set(K.draft, apprText.value); });
  function showSent(text) {
    $("appr-sent-text").textContent = text;
    apprForm.hidden = true;
    apprSent.hidden = false;
    apprCard.classList.add("done");
    $("appr-no").innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>';
  }
  var sentBefore = store.get(K.appr);
  if (sentBefore) { showSent(sentBefore); unlock(false); }
  else setTimeout(function () { if (!apprText.value) apprText.focus({ preventScroll: true }); }, 50);

  apprText.addEventListener("keydown", function (e) {
    if (modEnter(e)) { e.preventDefault(); apprForm.requestSubmit ? apprForm.requestSubmit() : sendApproach(e); }
  });
  apprForm.addEventListener("submit", sendApproach);
  function sendApproach(e) {
    if (e) e.preventDefault();
    var text = apprText.value.trim();
    if (!text) { toast("Write the idea first, in plain words.", "warn"); apprText.focus(); return; }
    apprSend.classList.add("busy");
    post("/solve/approach", { approach: text }).then(function (r) {
      return r.text().then(function (t) { return { ok: r.ok && t.indexOf('class="ok"') !== -1 }; });
    }).then(function (res) {
      apprSend.classList.remove("busy");
      if (!res.ok) { toast("Not sent. Is the problem still open?", "error"); return; }
      store.set(K.appr, text);
      store.del(K.draft);
      apprText.value = "";
      var first = ed.classList.contains("locked");
      showSent(text);
      unlock(first);
      expectReply();
      pollSoon();
      toast(first ? "Approach sent. Editor open." : "Sent.", "ok");
    }).catch(function () {
      apprSend.classList.remove("busy");
      toast("Network error. Approach not sent.", "error");
    });
  }
  $("appr-again").addEventListener("click", function () {
    apprSent.hidden = true;
    apprForm.hidden = false;
    apprText.focus();
  });

  /* ---------- submit ---------- */
  var lintOut = $("lint-out"), submitBtn = $("submit"), seg = $("verdict");
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
  function submit() {
    if (ed.classList.contains("locked")) { toast("Send your approach first.", "warn"); apprText.focus(); return; }
    var code = getCode();
    if (!code.trim()) { toast("Nothing to submit yet.", "warn"); return; }
    var v = seg.querySelector("input:checked");
    if (!v) {
      seg.classList.add("invalid");
      toast("Pick what LeetCode said: Accepted, Wrong answer, TLE or Runtime error.", "warn");
      var first = seg.querySelector("input"); if (first) first.focus();
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

  function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 80; }
  function toBottom(smooth) { log.scrollTo({ top: log.scrollHeight, behavior: smooth ? "smooth" : "auto" }); }
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
    var gotCoach = false;
    nodes.forEach(function (n) {
      var id = parseInt(n.dataset.id, 10);
      if (log.querySelector('.msg[data-id="' + id + '"]')) return;
      n.classList.toggle("cont", n.dataset.role === prevRole);
      n.classList.add("new");
      prevRole = n.dataset.role;
      log.insertBefore(n, typing);
      if (id > last) last = id;
      if (n.dataset.role === "coach") gotCoach = true;
    });
    empty.hidden = true;
    if (gotCoach && waitingSince !== null) stopTyping();
    if (stick || !gotCoach) toBottom(true);
    if (gotCoach) noteUnread();
    return nodes.length;
  }

  var pollT, inflight = false, fails = 0;
  var live = $("live"), liveTxt = $("live-txt");
  function setLive(on) { live.dataset.state = on ? "on" : "off"; liveTxt.textContent = on ? "Live" : "Offline"; }
  function schedule(ms) { clearTimeout(pollT); pollT = setTimeout(poll, ms); }
  function pollSoon() { schedule(150); }
  function poll() {
    if (inflight) { schedule(500); return; }
    inflight = true;
    fetch("/messages?since=" + last, { credentials: "same-origin", headers: { "Accept": "text/html" } }).then(function (r) {
      if (r.redirected && /\/login/.test(r.url)) { location.href = "/login"; return; }
      if (!r.ok) throw new Error(r.status);
      var open = r.headers.get("X-Open-Handout");
      return r.text().then(function (t) {
        inflight = false;
        fails = 0;
        setLive(true);
        append(t);
        if (open !== null && open !== hid) { closed(open); return; }
        schedule(document.hidden ? 6000 : 2000);
      });
    }).catch(function () {
      inflight = false;
      fails++;
      if (fails >= 2) setLive(false);
      schedule(Math.min(15000, 2000 * fails));
    });
  }
  var closing = false;
  function closed(open) {
    if (closing) return;
    closing = true;
    if (open) { toast("A different problem is open. Reloading.", "warn"); setTimeout(function () { location.href = "/solve"; }, 900); }
    else { toast("This problem was closed.", "info"); setTimeout(function () { location.href = "/reveal/" + hid; }, 900); }
  }
  schedule(2000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) pollSoon(); });

  function autosize() {
    input.style.height = "24px";
    input.style.height = Math.min(160, input.scrollHeight) + "px";
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

  /* ---------- phone: jump to chat ---------- */
  var jump = $("chat-jump"), unread = $("chat-unread"), pane = $("chat-pane");
  var chatVisible = true;
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (es) {
      chatVisible = es[0].isIntersecting;
      jump.hidden = chatVisible;
      if (chatVisible) unread.hidden = true;
    }, { threshold: 0.15 }).observe(pane);
  }
  function noteUnread() { if (!chatVisible) unread.hidden = false; }
  jump.addEventListener("click", function () {
    pane.scrollIntoView({ behavior: "smooth", block: "end" });
    setTimeout(function () { input.focus({ preventScroll: true }); }, 400);
  });
})();
