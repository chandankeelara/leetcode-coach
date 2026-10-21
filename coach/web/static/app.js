(function () {
  "use strict";
  var root = document.documentElement;

  var headers = {};
  try { headers = JSON.parse(document.querySelector('meta[name="coach-headers"]').content); } catch (e) {}

  function post(url, data) {
    var body = new URLSearchParams();
    Object.keys(data || {}).forEach(function (k) { body.append(k, data[k]); });
    var h = Object.assign({ "Content-Type": "application/x-www-form-urlencoded" }, headers);
    return fetch(url, { method: "POST", headers: h, body: body, credentials: "same-origin" });
  }

  var X = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  function toast(msg, variant, opts) {
    opts = opts || {};
    var box = document.getElementById("toasts");
    if (!box) return { dismiss: function () {} };
    while (box.children.length >= 3) box.removeChild(box.firstChild);
    var el = document.createElement("div");
    el.className = "toast toast-" + (variant || "info");
    el.setAttribute("role", variant === "error" ? "alert" : "status");
    var m = document.createElement("div");
    m.className = "toast-msg";
    m.textContent = msg;
    var x = document.createElement("button");
    x.className = "toast-x";
    x.type = "button";
    x.setAttribute("aria-label", "Dismiss");
    x.innerHTML = X;
    el.appendChild(m);
    el.appendChild(x);
    box.appendChild(el);
    var gone = false, timer;
    function dismiss() {
      if (gone) return;
      gone = true;
      clearTimeout(timer);
      el.classList.add("out");
      setTimeout(function () { el.remove(); }, 190);
    }
    x.addEventListener("click", dismiss);
    var ttl = opts.ttl != null ? opts.ttl : (variant === "error" ? 0 : 3800);
    if (ttl) timer = setTimeout(dismiss, ttl);
    return { dismiss: dismiss };
  }

  document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      var next = root.dataset.theme === "light" ? "dark" : "light";
      root.dataset.theme = next;
      try { localStorage.setItem("coach:theme", next); } catch (e) {}
    });
  });

  document.querySelectorAll("[data-flash]").forEach(function (f) {
    toast(f.textContent.trim(), f.dataset.variant || "info", { ttl: 6000 });
  });

  window.coach = { post: post, toast: toast, headers: headers };
  window.toast = toast;
})();
