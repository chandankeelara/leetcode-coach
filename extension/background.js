// Network calls live here (a content script on leetcode.com would be CORS-bound).
async function call(url, opts) {
  try {
    const r = await fetch(url, opts);
    let j = {};
    try { j = await r.json(); } catch (e) {}
    if (r.status === 401) return { ok: false, reason: "bad token" };
    return j;
  } catch (e) {
    return null;
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "submit") {
    call(msg.url + "/api/ext/submission", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(msg.body),
    }).then(sendResponse);
    return true;
  }
  if (msg.type === "ping") {
    call(msg.url + "/api/ext/ping?token=" + encodeURIComponent(msg.token), { method: "GET" }).then(sendResponse);
    return true;
  }
});
