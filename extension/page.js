// Runs in the PAGE world (injected by content.js). Content scripts cannot see window.monaco.
(function () {
  if (window.__coachPageHook) return;
  window.__coachPageHook = true;
  window.addEventListener("message", function (ev) {
    if (ev.source !== window || !ev.data || ev.data.type !== "coach-get-code") return;
    var code = null, language = null;
    try {
      var m = window.monaco && window.monaco.editor;
      if (m) {
        var models = m.getModels();
        var eds = m.getEditors ? m.getEditors() : [];
        var model = eds.length && eds[0].getModel ? eds[0].getModel() : models[0];
        if (model) { code = model.getValue(); language = model.getLanguageId ? model.getLanguageId() : null; }
      }
    } catch (e) {}
    window.postMessage({ type: "coach-code", id: ev.data.id, code: code, language: language }, "*");
  });
})();
