/* Reveal page: the rating card fills in the moment the coach scores the solve.
   The slot (#rv-rating-slot) starts with a "scoring" card when the score is pending. A
   `rating` event for this handout fetches GET /reveal/{id}/rating and swaps the card in place.
   Without a working stream, the same endpoint is polled every 3 s for up to 2 minutes. */
(function () {
  "use strict";
  var slot = document.getElementById("rv-rating-slot");
  if (!slot) return;
  var hid = slot.getAttribute("data-handout");
  var done = slot.getAttribute("data-pending") !== "1";
  var busy = false, pollT = null, pollUntil = 0;

  function swap(html) {
    var tmp = document.createElement("div");
    tmp.innerHTML = html;
    var card = tmp.querySelector(".rv-rating");
    slot.innerHTML = html;
    if (card && slot.firstElementChild) slot.firstElementChild.classList.add("rv-fresh");
    var live = slot.querySelector("[id=rv-rating]");
    if (live && live.scrollIntoView && !isVisible(live)) {
      try { live.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch (e) {}
    }
  }
  function isVisible(el) {
    var r = el.getBoundingClientRect();
    return r.top >= 0 && r.bottom <= (window.innerHeight || document.documentElement.clientHeight);
  }
  function fetchCard() {
    if (busy) return;
    busy = true;
    fetch("/reveal/" + encodeURIComponent(hid) + "/rating", {
      credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" }
    }).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      busy = false;
      if (!d || d.pending) return;
      done = true;
      stopPoll();
      swap(d.html);
    }).catch(function () { busy = false; });
  }
  function stopPoll() { clearInterval(pollT); pollT = null; }
  function startPoll() {
    if (pollT || done) return;
    pollUntil = Date.now() + 120000;
    pollT = setInterval(function () {
      if (done || Date.now() > pollUntil) { stopPoll(); return; }
      fetchCard();
    }, 3000);
  }

  if (window.coachLive) {
    window.coachLive.on("rating", function (d) {
      if (!d || d.ref_id === undefined || String(d.ref_id) === String(hid)) fetchCard();
    });
    /* after a (re)connect: catch a score that landed while the stream was down */
    window.coachLive.on("resync", function () { if (!done) fetchCard(); });
    window.coachLive.onState(function (s) {
      if (s === "open") stopPoll(); else if (!done) startPoll();
    });
  } else if (!done) startPoll();
})();
