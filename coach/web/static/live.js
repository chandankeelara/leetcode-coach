/* One live connection per page: EventSource('/stream'). Every coach-side write lands as an
   event (message | statement | visual | editor | handout | presence), so components refresh
   the moment the coach acts. Usage:
     coachLive.on("statement", function (data) { ... });
     coachLive.on("resync", fn)  -- after (re)connect (hello) and on each fallback poll tick
     coachLive.onState(fn)       -- "open" | "connecting" | "failed"
   Reconnects with backoff. While the stream is failed, a slow 20 s fallback tick fires
   "resync" so the page still catches up. */
(function () {
  "use strict";
  if (window.coachLive) return;
  var subs = {}, stateSubs = [], es = null, state = "connecting";
  var lastId = null, backoff = 1000, retryT = null, fallbackT = null, errors = 0;
  var FALLBACK_MS = 20000, MAX_BACKOFF = 30000;
  var KINDS = ["hello", "message", "statement", "visual", "editor", "handout", "presence"];

  function dispatch(kind, data) {
    (subs[kind] || []).slice().forEach(function (fn) {
      try { fn(data || {}); } catch (e) { if (window.console) console.error(e); }
    });
  }
  function setState(s) {
    if (s === state) return;
    state = s;
    stateSubs.slice().forEach(function (fn) { try { fn(s); } catch (e) {} });
    if (s === "failed") startFallback(); else stopFallback();
  }
  function startFallback() {
    if (fallbackT) return;
    fallbackT = setInterval(function () { if (!document.hidden) dispatch("resync", {}); }, FALLBACK_MS);
  }
  function stopFallback() { clearInterval(fallbackT); fallbackT = null; }

  function connect() {
    clearTimeout(retryT);
    if (es) { try { es.close(); } catch (e) {} es = null; }
    if (!window.EventSource) { setState("failed"); return; }
    var url = "/stream" + (lastId !== null ? "?since=" + encodeURIComponent(lastId) : "");
    es = new EventSource(url, { withCredentials: true });
    KINDS.forEach(function (k) {
      es.addEventListener(k, function (ev) {
        if (ev.lastEventId) lastId = ev.lastEventId;
        var data = {};
        try { data = JSON.parse(ev.data || "{}"); } catch (e) {}
        if (k === "hello") {
          errors = 0; backoff = 1000;
          setState("open");
          dispatch("hello", data);
          dispatch("resync", data);
          if (typeof data.online === "boolean") dispatch("presence", { online: data.online });
          return;
        }
        dispatch(k, data);
      });
    });
    es.onerror = function () {
      /* Browser auto-retry gets one chance (readyState CONNECTING); after that, or once it
         gives up (CLOSED, e.g. a 303 to /login), we reconnect ourselves with backoff. */
      errors++;
      if (es && es.readyState === 2 || errors > 1) {
        try { es.close(); } catch (e) {}
        es = null;
        setState(errors >= 3 ? "failed" : "connecting");
        retryT = setTimeout(connect, backoff);
        backoff = Math.min(MAX_BACKOFF, backoff * 2);
        if (errors >= 3) probeLogin();
      } else setState("connecting");
    };
  }
  function probeLogin() {
    fetch("/handout/state", { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.redirected && /\/login/.test(r.url)) location.href = "/login";
    }).catch(function () {});
  }

  window.coachLive = {
    on: function (kind, fn) { (subs[kind] = subs[kind] || []).push(fn); },
    onState: function (fn) { stateSubs.push(fn); fn(state); },
    state: function () { return state; },
    reconnect: function () { backoff = 1000; connect(); }
  };
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && state !== "open") window.coachLive.reconnect();
  });
  window.addEventListener("pagehide", function () { if (es) { try { es.close(); } catch (e) {} } });
  /* connect after the page's scripts have subscribed */
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", connect);
  else setTimeout(connect, 0);
})();
