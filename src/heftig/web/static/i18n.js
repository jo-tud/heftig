"use strict";
// Texts of the browser scripts in the interface language: t("Saved") and
// tn("%(num)d page", "%(num)d pages", n). The translations come with the page
// (<script type="application/json" id="i18n-messages">, see heftig/i18n.py).
(function () {
  let messages = null;
  function load() {
    if (messages === null) {
      const el = document.getElementById("i18n-messages");
      try { messages = el ? JSON.parse(el.textContent) : {}; } catch (_) { messages = {}; }
    }
    return messages;
  }
  function fill(s, vars) {
    return vars ? s.replace(/%\((\w+)\)[sd]/g, (m, k) => (k in vars ? String(vars[k]) : m)) : s;
  }
  window.t = function (text, vars) {
    const m = load();
    const s = Object.prototype.hasOwnProperty.call(m, text) ? m[text] : text;
    return fill(typeof s === "string" ? s : s[0], vars);
  };
  window.tn = function (one, many, n, vars) {
    const m = load();
    const forms = Array.isArray(m[one]) ? m[one] : [one, many];
    return fill(forms[n === 1 ? 0 : 1], Object.assign({ num: n }, vars));
  };
})();
