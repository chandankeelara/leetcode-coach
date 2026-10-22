/* One live connection per page: EventSource('/stream'). Every coach-side write lands as an
   event (message | statement | visual | editor | handout | clock | presence), so components refresh
   the moment the coach acts. Usage:
     coachLive.on("statement", function (data) { ... });
     coachLive.on("resync", fn)  -- after (re)connect (hello) and on each fallback poll tick
     coachLive.onState(fn)       -- "open" | "connecting" | "failed"
   Reconnects with backoff. While the stream is failed, a slow 20 s fallback tick fires
   "resync" so the page still catches up.
   Two ways a page used to go quietly deaf (2026-10-22: clock paused on the server, his page
   kept counting):
   * a stream that died without an error (sleep/wake, a dropped connection): the server sends
     a `ping` event every 15 s; nothing at all for WATCHDOG_MS -> reconnect (and resync).
   * the browser's ~6 connections per host: every open tab of the site holds one stream, so
     with a few tabs open the visible page's own requests (/handout/state after a `clock`
     event) queue behind them. A tab hidden for PARK_MS closes its stream; it reconnects and
     resyncs the moment it is shown again. */
(function () {
  "use strict";
  if (window.coachLive) return;
  var subs = {}, stateSubs = [], es = null, state = "connecting";
  var lastId = null, backoff = 1000, retryT = null, fallbackT = null, errors = 0;
  var FALLBACK_MS = 20000, MAX_BACKOFF = 30000, WATCHDOG_MS = 40000, PARK_MS = 10000;
  var lastSeen = Date.now(), parked = false, hiddenAt = document.hidden ? Date.now() : null;
  var KINDS = ["hello", "message", "statement", "visual", "editor", "handout", "clock", "presence",
    /* editor tools */ "annotations", "code_ops", "scratch",
    /* tests panel */ "tests", /* keep-alive, never dispatched */ "ping"];

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
    parked = false;
    lastSeen = Date.now();
    if (es) { try { es.close(); } catch (e) {} es = null; }
    if (!window.EventSource) { setState("failed"); return; }
    var url = "/stream" + (lastId !== null ? "?since=" + encodeURIComponent(lastId) : "");
    es = new EventSource(url, { withCredentials: true });
    KINDS.forEach(function (k) {
      es.addEventListener(k, function (ev) {
        lastSeen = Date.now();
        if (k === "ping") return;
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
  function park() {
    /* hidden tab: give its connection back to the visible one */
    if (!document.hidden || parked) return;
    clearTimeout(retryT);
    if (es) { try { es.close(); } catch (e) {} es = null; }
    parked = true;
    setState("connecting");
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { hiddenAt = Date.now(); return; }
    hiddenAt = null;
    if (parked || state !== "open") window.coachLive.reconnect();
  });
  /* every 5 s: park a tab hidden for PARK_MS (also one opened in the background, which never
     sees a visibilitychange); reconnect an open stream gone silent past the server's pings */
  setInterval(function () {
    if (document.hidden) {
      if (hiddenAt === null) hiddenAt = Date.now();
      if (Date.now() - hiddenAt >= PARK_MS) park();
      return;
    }
    if (parked || !es) return;
    if (Date.now() - lastSeen > WATCHDOG_MS) { errors = 0; window.coachLive.reconnect(); }
  }, 2500);
  window.addEventListener("pagehide", function () {
    if (es) { try { es.close(); } catch (e) {} es = null; }
    parked = true;
  });
  /* back/forward cache: the page comes back with its stream closed */
  window.addEventListener("pageshow", function (ev) { if (ev.persisted) window.coachLive.reconnect(); });
  /* connect after the page's scripts have subscribed */
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", connect);
  else setTimeout(connect, 0);
})();
