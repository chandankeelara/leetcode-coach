/* Running the question, page side: the Problem tab's "Try your own input" (POST /playground runs
   your coach's answer key on what you type, never your code) and the "What does this return?"
   cards in the thread (POST /predict/<id>). */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var post = window.coach && window.coach.post;
  var box = $("play"), form = $("play-form"), fields = $("play-fields"), msg = $("play-msg");
  var out = $("play-out"), val = $("play-val"), draw = $("play-draw"), run = $("play-run");
  var shape = null, busy = false;

  function field(name, label, value) {
    var w = document.createElement("label");
    w.className = "play-field";
    var k = document.createElement("span");
    k.className = "play-name";
    k.textContent = label;
    var i = document.createElement("input");
    i.className = "play-in";
    i.name = name;
    i.value = value || "";
    i.spellcheck = false;
    i.autocomplete = "off";
    i.setAttribute("autocapitalize", "off");
    w.appendChild(k);
    w.appendChild(i);
    return w;
  }

  function build(st) {
    var key = JSON.stringify([st.mode, st.params || []]);
    if (key === shape) return;
    shape = key;
    fields.textContent = "";
    if (st.mode === "design") {
      fields.appendChild(field("ops", "ops", st.ops));
      fields.appendChild(field("args", "args", st.args));
    } else {
      (st.params || []).forEach(function (n) { fields.appendChild(field(n, n, (st.example || {})[n])); });
    }
    out.hidden = true;
  }

  function load() {
    if (!box) return;
    fetch("/playground", { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (st) {
      if (!st || !st.ready) { box.hidden = true; return; }
      build(st);
      box.hidden = false;
    }).catch(function () {});
  }

  function section(label, html) {
    if (!html) return "";
    return '<div class="play-fig"><span class="play-k">' + label + "</span>" + html + "</div>";
  }

  if (form) form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (busy) return;
    var data = {};
    fields.querySelectorAll("input").forEach(function (i) { data[i.name] = i.value; });
    busy = true;
    run.disabled = true;
    msg.textContent = "Running…";
    msg.className = "play-msg";
    post("/playground", data).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      var d = res.d || {};
      if (!res.ok) { msg.textContent = d.error || "Could not run that."; msg.className = "play-msg err"; return; }
      msg.textContent = "";
      out.hidden = false;
      if (d.error) {
        val.textContent = d.error;
        val.className = "play-val err";
      } else {
        val.textContent = d.output_text;
        val.className = "play-val";
      }
      draw.innerHTML = section("Input", d.input_html) + section("Output", d.output_html);
    }).catch(function () {
      msg.textContent = "Could not reach the server.";
      msg.className = "play-msg err";
    }).then(function () {
      setTimeout(function () { busy = false; run.disabled = false; }, 1000);
    });
  });

  /* predict cards: the thread re-renders from server html, so listen on the document */
  document.addEventListener("submit", function (e) {
    var f = e.target.closest ? e.target.closest(".rq-form") : null;
    if (!f) return;
    e.preventDefault();
    var id = f.dataset.card, data = {};
    var sub = e.submitter;
    if (sub && sub.name === "choice") data.choice = sub.value;
    else {
      var a = f.querySelector(".rq-answer");
      data.answer = a ? a.value : "";
      if (!data.answer.trim()) { f.querySelector(".rq-msg").textContent = "Type your answer first."; return; }
    }
    f.querySelectorAll("button, input").forEach(function (b) { b.disabled = true; });
    post("/predict/" + id, data).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      var card = f.closest(".rq");
      if (res.ok && res.d.html && card) { card.outerHTML = res.d.html; return; }
      f.querySelector(".rq-msg").textContent = (res.d && res.d.error) || "Could not check that.";
      f.querySelectorAll("button, input").forEach(function (b) { b.disabled = false; });
    }).catch(function () {
      f.querySelector(".rq-msg").textContent = "Could not reach the server.";
      f.querySelectorAll("button, input").forEach(function (b) { b.disabled = false; });
    });
  });

  /* hint cards: "Show hint" fetches the text only now; "Not now" dismisses (not counted) */
  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest(".hint-show, .hint-no") : null;
    if (!b) return;
    var card = b.closest(".hint-card"), id = b.dataset.card;
    var show = b.classList.contains("hint-show");
    card.querySelectorAll("button").forEach(function (x) { x.disabled = true; });
    post("/hint/" + id + (show ? "/reveal" : "/dismiss"), {}).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      if (res.ok && res.d.html) { card.outerHTML = res.d.html; return; }
      if (!res.ok && card.isConnected) {
        var m = card.querySelector(".rq-msg");
        if (m) m.textContent = (res.d && res.d.error) || "Could not open that.";
        card.querySelectorAll("button").forEach(function (x) { x.disabled = false; });
      }
    }).catch(function () {
      var m = card.querySelector(".rq-msg");
      if (m) m.textContent = "Could not reach the server.";
      card.querySelectorAll("button").forEach(function (x) { x.disabled = false; });
    });
  });

  load();
  if (window.coachLive) {
    window.coachLive.on("statement", load);
    window.coachLive.on("resync", load);
  }
})();
