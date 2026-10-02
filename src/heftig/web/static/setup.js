"use strict";
// Setup and connection pages: show the fields of the chosen AI, list an endpoint's models,
// fill in the mail server of common providers.
(function () {
  const csrf = document.body.dataset.csrf || "";

  // first page: the chosen language right away
  const account = document.getElementById("account-form");
  if (account) {
    account.elements.language.addEventListener("change", () => {
      const u = new URL(location.href);
      u.searchParams.set("lang", account.elements.language.value);
      const token = account.elements.token;
      if (token && token.value) u.searchParams.set("token", token.value);
      location.replace(u.toString());
    });
  }

  const ai = document.getElementById("ai-form");
  if (ai) {
    const mode = () => (ai.querySelector("input[name=mode]:checked") || {}).value || "offline";
    const details = ai.querySelector(".ai-details");
    function show() {
      const m = mode();
      details.hidden = m === "offline";
      ai.querySelectorAll("[data-for]").forEach((el) => {
        el.hidden = !el.dataset.for.split(" ").includes(m);
      });
    }
    ai.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", () => {
      const model = ai.elements.model;
      const defaults = { anthropic: "claude-sonnet-5", openai: "gpt-5-mini", local: "" };
      if (model && (!model.value || Object.values(defaults).includes(model.value))) model.value = defaults[mode()] || "";
      show();
    }));
    show();

    const btn = document.getElementById("load-models");
    const status = document.getElementById("models-status");
    const list = document.getElementById("model-list");
    if (btn) btn.addEventListener("click", async () => {
      status.textContent = t("Loading …");
      const body = new URLSearchParams({
        csrf_token: csrf, mode: mode(),
        api_key: ai.elements.api_key.value, base_url: ai.elements.base_url.value,
        headers: ai.elements.headers ? ai.elements.headers.value : "",
        clear_headers: ai.elements.clear_headers && ai.elements.clear_headers.checked ? "1" : "",
      });
      try {
        const r = await fetch("/settings/ai/models", { method: "POST", body, headers: { "X-CSRF-Token": csrf } });
        const data = await r.json();
        list.replaceChildren(...(data.models || []).map((m) => Object.assign(document.createElement("option"), { value: m })));
        status.textContent = data.error || tn("%(num)d model – choose one in the field above.", "%(num)d models – choose one in the field above.", (data.models || []).length);
      } catch (_) {
        status.textContent = t("Could not load the list.");
      }
    });
  }

  const mail = document.getElementById("mail-form");
  if (mail) {
    let presets = {};
    try { presets = JSON.parse(document.getElementById("imap-presets").textContent); } catch (_) { /* none */ }
    const user = mail.elements.imap_user;
    function fill() {
      const domain = (user.value.split("@")[1] || "").trim().toLowerCase();
      const p = presets[domain];
      document.getElementById("app-password").hidden = !(p && p.app_password);
      document.getElementById("bridge").hidden = !(p && p.unsupported);
      if (p && p.host && (!mail.elements.imap_host.value || mail.elements.imap_host.dataset.auto)) {
        mail.elements.imap_host.value = p.host;
        mail.elements.imap_host.dataset.auto = "1";
        mail.elements.imap_port.value = p.port || 993;
      }
    }
    user.addEventListener("change", fill);
    user.addEventListener("blur", fill);
    mail.elements.imap_host.addEventListener("input", () => { delete mail.elements.imap_host.dataset.auto; });
    fill();
  }
})();
