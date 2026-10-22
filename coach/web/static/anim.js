/* Animated coach visuals (viz.py kind "animate"): a stack of server-rendered frames, one shown.
   Same-shape array/list frames tween: pointer tags glide by name, cells cross-fade in place. */
(function () {
  "use strict";
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  var SPEEDS = [0.5, 1, 2];
  var EASE = "cubic-bezier(.16, 1, .3, 1)";
  var seen = typeof IntersectionObserver === "function" ? new IntersectionObserver(onSeen, { threshold: 0.6 }) : null;

  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  function init(fig) {
    if (fig.classList.contains("is-ready")) return;
    var st = {
      fig: fig,
      frames: fig.querySelectorAll(".viz-anim-stage > .viz-frame"),
      caps: fig.querySelectorAll(".viz-anim-caps > .viz-anim-cap"),
      scrub: fig.querySelector(".viz-anim-scrub"),
      count: fig.querySelector(".viz-anim-count b"),
      play: fig.querySelector(".viz-anim-play"),
      speedBtn: fig.querySelector(".viz-anim-speed"),
      ms: clamp(parseInt(fig.dataset.ms, 10) || 1200, 300, 6000),
      loop: fig.dataset.loop === "1",
      tween: fig.dataset.tween === "1",
      i: 0, speed: 1, timer: null, playing: false, touched: false
    };
    if (st.frames.length < 2 || !st.scrub) return;
    fig._anim = st;
    fig.classList.add("is-ready");
    sync(st);
    fig.addEventListener("click", function (e) {
      var b = e.target.closest(".viz-anim-btn");
      if (!b || !fig.contains(b)) return;
      st.touched = true;
      var act = b.dataset.act;
      if (act === "play") toggle(st);
      else if (act === "prev") { pause(st); go(st, st.i - 1); }
      else if (act === "next") { pause(st); go(st, st.i + 1); }
      else if (act === "speed") setSpeed(st);
    });
    st.scrub.addEventListener("input", function () {
      st.touched = true;
      pause(st);
      go(st, parseInt(st.scrub.value, 10) || 0, true);
    });
    fig.addEventListener("keydown", function (e) {
      if (e.target !== fig || e.altKey || e.ctrlKey || e.metaKey) return;
      var k = e.key;
      if (k === " " || k === "Spacebar") toggle(st);
      else if (k === "ArrowLeft") { pause(st); go(st, st.i - 1); }
      else if (k === "ArrowRight") { pause(st); go(st, st.i + 1); }
      else if (k === "Home") { pause(st); go(st, 0); }
      else if (k === "End") { pause(st); go(st, st.frames.length - 1); }
      else return;
      st.touched = true;
      e.preventDefault();
    });
    if (seen && !reduce.matches) seen.observe(fig);
  }

  function onSeen(entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      var st = en.target._anim;
      seen.unobserve(en.target);
      if (!st || st.touched || reduce.matches) return;
      st.timer = setTimeout(function () { if (!st.touched) play(st, true); }, 500);
    });
  }

  function sync(st) {
    var n = st.frames.length;
    st.scrub.value = st.i;
    st.fig.style.setProperty("--p", n > 1 ? (st.i / (n - 1) * 100).toFixed(2) + "%" : "0%");
    if (st.count) st.count.textContent = st.i + 1;
    st.fig.classList.toggle("is-playing", st.playing);
    st.fig.classList.toggle("at-end", st.i === n - 1);
    st.play.setAttribute("aria-label", st.playing ? "Pause" : st.i === n - 1 ? "Replay" : "Play");
  }

  function settle(st) {
    if (st.fig.getAnimations) st.fig.getAnimations({ subtree: true }).forEach(function (a) { a.finish(); });
    st.fig.querySelectorAll(".is-out").forEach(function (el) { el.classList.remove("is-out"); });
    st.fig.querySelectorAll(".is-moved").forEach(function (el) { el.classList.remove("is-moved"); });
  }

  function names(el) { return (el.getAttribute("data-k") || "").split(/\s+/).filter(Boolean); }

  function center(el) {
    var r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  function svgScale(el) {
    var svg = el.ownerSVGElement;
    if (!svg || !svg.viewBox || !svg.viewBox.baseVal || !svg.viewBox.baseVal.width) return 1;
    return svg.getBoundingClientRect().width / svg.viewBox.baseVal.width || 1;
  }

  /* Tags of the next frame, each paired with where the same name sits in the current one. */
  function pairTags(A, B) {
    var from = Array.prototype.slice.call(A.querySelectorAll("[data-k]"));
    var out = [];
    B.querySelectorAll("[data-k]").forEach(function (t) {
      var want = names(t), src = null;
      for (var j = 0; j < from.length && !src; j++) {
        var have = names(from[j]);
        for (var q = 0; q < want.length; q++) if (have.indexOf(want[q]) >= 0) { src = from[j]; break; }
      }
      if (!src) return;
      var a = center(src), b = center(t);
      var s = t.ownerSVGElement ? svgScale(t) : 1;
      var dx = (a.x - b.x) / s, dy = (a.y - b.y) / s;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
      out.push({ el: t, src: src, dx: dx, dy: dy });
    });
    return out;
  }

  function go(st, to, instant) {
    var n = st.frames.length;
    to = clamp(to, 0, n - 1);
    settle(st);
    if (to === st.i) { sync(st); return; }
    var A = st.frames[st.i], B = st.frames[to];
    var ca = st.caps[st.i], cb = st.caps[to];
    var motion = !instant && !reduce.matches && typeof A.animate === "function";
    var sa = A.querySelector(".viz-scroll"), sb = B.querySelector(".viz-scroll");
    if (sa && sb) sb.scrollLeft = sa.scrollLeft;
    var moves = motion && st.tween && Math.abs(to - st.i) === 1 ? pairTags(A, B) : [];

    B.classList.add("is-on"); B.removeAttribute("aria-hidden");
    A.classList.remove("is-on"); A.setAttribute("aria-hidden", "true");
    if (cb) { cb.classList.add("is-on"); cb.removeAttribute("aria-hidden"); }
    if (ca) { ca.classList.remove("is-on"); ca.setAttribute("aria-hidden", "true"); }
    st.i = to;
    sync(st);
    if (!motion) return;

    var pace = st.ms / st.speed;
    var fade = clamp(pace * 0.35, 120, 360);
    var move = clamp(pace * 0.5, 180, 620);
    A.classList.add("is-out");
    var out = A.animate([{ opacity: 1 }, { opacity: 0 }], { duration: fade, easing: "ease-out" });
    out.onfinish = function () { A.classList.remove("is-out"); };
    moves.forEach(function (m) {
      m.src.classList.add("is-moved");
      m.el.animate([{ transform: "translate(" + m.dx + "px," + m.dy + "px)" }, { transform: "translate(0,0)" }],
        { duration: move, easing: EASE });
    });
    if (cb) cb.animate([{ opacity: 0, transform: "translateY(3px)" }, { opacity: 1, transform: "none" }],
      { duration: clamp(fade, 120, 220), easing: "ease-out" });
  }

  function schedule(st) {
    clearTimeout(st.timer);
    st.timer = setTimeout(function () {
      if (!st.playing) return;
      if (!st.fig.isConnected) { pause(st); return; }
      var n = st.frames.length;
      if (st.i < n - 1) go(st, st.i + 1);
      else if (st.loop) go(st, 0);
      if (st.i === n - 1 && !st.loop) { pause(st); return; }
      schedule(st);
    }, st.ms / st.speed);
  }

  function play(st, auto) {
    clearTimeout(st.timer);
    if (st.i === st.frames.length - 1) go(st, 0, true);
    st.playing = true;
    st.auto = !!auto;
    sync(st);
    schedule(st);
  }
  function pause(st) {
    clearTimeout(st.timer);
    st.timer = null;
    if (!st.playing) return;
    st.playing = false;
    sync(st);
  }
  function toggle(st) { if (st.playing) pause(st); else play(st); }

  function setSpeed(st) {
    var k = (SPEEDS.indexOf(st.speed) + 1) % SPEEDS.length;
    st.speed = SPEEDS[k];
    var txt = st.speed + "x";
    st.speedBtn.textContent = txt;
    st.speedBtn.setAttribute("aria-label", "Speed " + txt);
    if (st.playing) schedule(st);
  }

  function scan(root) {
    (root || document).querySelectorAll(".viz-anim:not(.is-ready)").forEach(init);
  }
  scan();
  if (typeof MutationObserver === "function") {
    new MutationObserver(function (recs) {
      for (var r = 0; r < recs.length; r++) {
        var added = recs[r].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var nd = added[j];
          if (nd.nodeType !== 1) continue;
          if (nd.matches(".viz-anim")) init(nd);
          else if (nd.querySelector(".viz-anim")) scan(nd);
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
  }
  reduce.addEventListener && reduce.addEventListener("change", function () {
    if (!reduce.matches) return;
    document.querySelectorAll(".viz-anim.is-ready").forEach(function (f) { if (f._anim) { settle(f._anim); pause(f._anim); } });
  });
  window.vizAnim = { scan: scan };
})();
