const $ = (id) => document.getElementById(id);
const DEFAULT_URL = "http://127.0.0.1:8731";
chrome.storage.sync.get({ token: "", url: DEFAULT_URL }, (v) => {
  $("token").value = v.token; $("url").value = v.url || DEFAULT_URL;
});
function save() {
  chrome.storage.sync.set({ token: $("token").value.trim(), url: $("url").value.trim().replace(/\/+$/, "") || DEFAULT_URL });
}
$("token").addEventListener("change", save);
$("url").addEventListener("change", save);
$("test").addEventListener("click", async () => {
  save();
  const url = $("url").value.trim().replace(/\/+$/, "") || DEFAULT_URL;
  $("out").textContent = "Testing...";
  try {
    const r = await fetch(url + "/api/ext/ping?token=" + encodeURIComponent($("token").value.trim()));
    const j = await r.json();
    if (r.status === 401) $("out").textContent = "Bad token.";
    else if (j.ok) $("out").textContent = j.open_problem == null ? "Connected. No open problem." : "Connected. Open problem: " + j.open_problem;
    else $("out").textContent = "Error: " + (j.reason || r.status);
  } catch (e) { $("out").textContent = "Cannot reach " + url; }
});
