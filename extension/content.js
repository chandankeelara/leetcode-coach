// Captures a leetcode.com submission verdict + code and hands it to the local coach app.
// Never reads the problem statement. Selects by text, not by class hashes.
(function () {
  "use strict";
  var VERDICTS = ["Accepted", "Wrong Answer", "Time Limit Exceeded", "Memory Limit Exceeded",
    "Output Limit Exceeded", "Runtime Error", "Compile Error"];
  var DEFAULT_URL = "http://127.0.0.1:8731";
  var armedAt = 0;          // set when Submit is clicked
  var lastSentKey = "";
  var lastSentAt = 0;
  var badge = null;

  function slug() {
    var m = location.pathname.match(/^\/problems\/([^\/]+)/);
    return m ? m[1] : null;
  }

  // ---- page hook for Monaco ----
  function injectHook() {
    try {
      var s = document.createElement("script");
      s.src = chrome.runtime.getURL("page.js");
      s.onload = function () { s.remove(); };
      (document.head || document.documentElement).appendChild(s);
    } catch (e) {}
  }

  function getPageCode() {
    return new Promise(function (resolve) {
      var id = String(Math.random());
      var done = false;
      function on(ev) {
        if (ev.source !== window || !ev.data || ev.data.type !== "coach-code" || ev.data.id !== id) return;
        done = true; window.removeEventListener("message", on);
        resolve({ code: ev.data.code, language: ev.data.language });
      }
      window.addEventListener("message", on);
      window.postMessage({ type: "coach-get-code", id: id }, "*");
      setTimeout(function () {
        if (!done) { window.removeEventListener("message", on); resolve({ code: null, language: null }); }
      }, 800);
    });
  }

  function domCode() {
    var lines = document.querySelectorAll(".view-lines .view-line");
    if (!lines.length) return null;
    var arr = Array.prototype.slice.call(lines).sort(function (a, b) {
      return (parseFloat(a.style.top) || 0) - (parseFloat(b.style.top) || 0);
    });
    // Monaco virtualizes: only visible lines exist, so this is a last resort.
    return arr.map(function (l) { return l.textContent.replace(/ /g, " "); }).join("\n");
  }

  function domLanguage() {
    var names = ["C++", "Java", "Python3", "Python", "C", "C#", "JavaScript", "TypeScript", "PHP",
      "Swift", "Kotlin", "Dart", "Go", "Ruby", "Scala", "Rust", "Racket", "Erlang", "Elixir"];
    var btns = document.querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) {
      var t = (btns[i].textContent || "").trim();
      if (names.indexOf(t) >= 0) return t;
    }
    return null;
  }

  function normLang(modelLang, domLang) {
    if (domLang) return domLang;
    if (modelLang === "python") return "Python3";
    return modelLang || "unknown";
  }

  // ---- verdict detection ----
  function findVerdict() {
    var loc = document.querySelector('[data-e2e-locator="submission-result"]');
    if (loc) {
      var t = (loc.textContent || "").trim();
      for (var i = 0; i < VERDICTS.length; i++) if (t.indexOf(VERDICTS[i]) === 0) return { v: VERDICTS[i], el: loc };
    }
    if (!armedAt) return null;
    // Fallback by text: short elements whose whole text is a verdict.
    var els = document.querySelectorAll("span, div, h3, h4, p");
    for (var j = 0; j < els.length; j++) {
      var e = els[j];
      if (e.children.length > 2) continue;
      var tx = (e.textContent || "").trim();
      if (tx.length > 30) continue;
      if (VERDICTS.indexOf(tx) >= 0) return { v: tx, el: e };
    }
    return null;
  }

  function runtimeText(el) {
    try {
      var root = el;
      for (var i = 0; i < 5 && root.parentElement; i++) root = root.parentElement;
      var txt = root.innerText || "";
      var m = txt.match(/Runtime\s*[\r\n ]*([\d.]+\s*ms)[\s\S]{0,80}?Memory\s*[\r\n ]*([\d.]+\s*MB)/i);
      if (m) return "Runtime " + m[1] + ", Memory " + m[2];
      var m2 = txt.match(/(\d+\s*\/\s*\d+\s*testcases passed)/i);
      return m2 ? m2[1] : "";
    } catch (e) { return ""; }
  }

  var scanTimer = null;
  function scheduleScan() {
    if (scanTimer) return;
    scanTimer = setTimeout(function () { scanTimer = null; scan(); }, 300);
  }

  function scan() {
    var f = findVerdict();
    if (!f) return;
    var now = Date.now();
    var key = slug() + "|" + f.v + "|" + (armedAt || "x");
    if (key === lastSentKey && now - lastSentAt < 30000) return;
    if (!armedAt && now - lastSentAt < 8000) return;   // unarmed result: dedupe hard
    lastSentKey = key; lastSentAt = now; armedAt = 0;
    send(f.v, runtimeText(f.el));
  }

  async function send(verdict, runtime) {
    var cfg = await getCfg();
    if (!cfg.token) { setBadge("grey", "No token set: open the extension popup"); return; }
    var pc = await getPageCode();
    var code = pc.code;
    if (code == null) code = domCode();
    if (code == null) { setBadge("red", "Could not read the code"); return; }
    var body = { token: cfg.token, slug: slug(), language: normLang(pc.language, domLanguage()),
      code: code, verdict: verdict, runtime: runtime || undefined };
    chrome.runtime.sendMessage({ type: "submit", url: cfg.url, body: body }, function (res) {
      if (chrome.runtime.lastError || !res) { setBadge("red", "Coach app unreachable"); return; }
      if (res.ok) setBadge("green", "Sent to coach: " + verdict);
      else if (res.reason === "not the open problem") setBadge("grey", "Not the open problem: not sent");
      else setBadge("red", "Not sent: " + (res.reason || "error"));
    });
  }

  // ---- config + badge ----
  function getCfg() {
    return new Promise(function (resolve) {
      try {
        chrome.storage.sync.get({ token: "", url: DEFAULT_URL }, function (v) {
          resolve({ token: v.token || "", url: (v.url || DEFAULT_URL).replace(/\/+$/, "") });
        });
      } catch (e) { resolve({ token: "", url: DEFAULT_URL }); }
    });
  }

  function setBadge(color, title) {
    if (!badge) {
      badge = document.createElement("div");
      badge.style.cssText = "position:fixed;bottom:12px;left:12px;width:12px;height:12px;border-radius:50%;" +
        "z-index:2147483647;box-shadow:0 0 0 2px rgba(255,255,255,.7);cursor:default;";
      document.documentElement.appendChild(badge);
    }
    badge.style.background = { green: "#22c55e", red: "#ef4444", grey: "#9ca3af" }[color] || "#9ca3af";
    badge.title = "LeetCode Coach: " + title;
  }

  function pageNumber() {
    var m = (document.title || "").match(/^\s*(\d+)\.\s/);
    if (m) return parseInt(m[1], 10);
    var a = document.querySelector('a[href^="/problems/' + slug() + '"]');
    var mm = a && (a.textContent || "").match(/^\s*(\d+)\.\s/);
    return mm ? parseInt(mm[1], 10) : null;
  }

  async function refreshBadge() {
    var cfg = await getCfg();
    if (!cfg.token) { setBadge("grey", "No token set"); return; }
    chrome.runtime.sendMessage({ type: "ping", url: cfg.url, token: cfg.token }, function (res) {
      if (chrome.runtime.lastError || !res) { setBadge("red", "Coach app unreachable"); return; }
      if (!res.ok) { setBadge("red", "Bad token"); return; }
      var open = res.open_problem;
      if (open == null) { setBadge("grey", "No open problem in the coach"); return; }
      var num = pageNumber();
      if (num == null) setBadge("grey", "Open problem " + open + " (page number unknown)");
      else if (num === open) setBadge("green", "Open problem matches this page");
      else setBadge("grey", "Open problem is " + open + ", this is " + num);
    });
  }

  // ---- wiring ----
  document.addEventListener("click", function (ev) {
    var b = ev.target && ev.target.closest && ev.target.closest("button");
    if (!b) return;
    var t = (b.textContent || "").trim();
    var loc = b.getAttribute("data-e2e-locator") || "";
    if (loc === "console-submit-button" || t === "Submit") armedAt = Date.now();
    else if (loc === "console-run-button" || t === "Run") armedAt = 0;
  }, true);

  document.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "'") armedAt = Date.now();   // submit shortcut
  }, true);

  injectHook();
  new MutationObserver(scheduleScan).observe(document.documentElement,
    { childList: true, subtree: true, characterData: true });
  refreshBadge();
  setInterval(refreshBadge, 60000);
})();
