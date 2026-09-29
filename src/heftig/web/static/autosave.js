"use strict";
// Document page: every field is saved as soon as it is finished - when the user leaves it or
// picks a value, never while typing. "Saved · Undo" appears next to it; undo restores the
// value, its lock and the AI suggestions of that field. Links and other buttons on the page
// wait for a save still running, so nothing typed is lost.
(function () {
  const form = document.getElementById("meta-form");
  if (!form || !window.fetch) return;
  form.classList.add("autosaving");
  const csrf = document.body.dataset.csrf || "";
  let pending = Promise.resolve();
  let busy = 0;

  // which saved field an input belongs to
  function fieldOf(el) {
    const n = el.name || "";
    if (n.startsWith("cf_")) return "custom_fields";
    if (n.startsWith("lock_")) return "lock:" + n.slice(5);
    return ["title", "document_date", "correspondent", "document_type", "tags", "summary"].includes(n) ? n : null;
  }
  function inputsOf(field) {
    if (field === "custom_fields") return [...form.querySelectorAll("[name^=cf_]")];
    return [...form.querySelectorAll(`[name="${field.startsWith("lock:") ? "lock_" + field.slice(5) : field}"]`)];
  }
  const valueOf = (el) => (el.type === "checkbox" ? (el.checked ? "on" : "") : el.value);
  const saved = new Map(); // element -> value last saved
  form.querySelectorAll("input, select, textarea").forEach((el) => { if (fieldOf(el)) saved.set(el, valueOf(el)); });
  const changed = (field) => inputsOf(field).some((el) => saved.get(el) !== valueOf(el));
  const dirty = () => [...saved.keys()].some((el) => saved.get(el) !== valueOf(el));

  function note(field, html, isError) {
    const first = inputsOf(field)[0];
    const anchor = (first && (first.closest(".field") || first.closest("fieldset"))) || form;
    let box = anchor.querySelector(":scope > .autosave-note");
    if (!box) {
      box = document.createElement("p");
      box.className = "autosave-note small";
      box.setAttribute("role", "status");
      anchor.appendChild(box);
    }
    box.classList.toggle("error-text", !!isError);
    box.replaceChildren(...html);
    return box;
  }
  const text = (s) => document.createTextNode(s);

  // the review box, the review bar and the status badge as the server has them now (a
  // saved field can settle a suggestion or the whole review)
  function applyReview(data) {
    if (data.review_html === undefined) return;
    const put = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
    put("review-box", data.review_html);
    put("rb-count", data.review_count_html);
    put("rb-note", data.review_note_html);
    put("doc-status", data.status_html);
    put("date-hint", data.date_hint_html);
  }

  // every form on the page sends the revision it was made for: keep them all current
  function setRevision(rev) {
    document.querySelectorAll('input[name="revision"]').forEach((i) => { i.value = rev; });
  }

  function send(extra) {
    const body = new FormData(form);
    body.set("autosave", "1");
    for (const [k, v] of Object.entries(extra)) body.set(k, v);
    return fetch(form.action, { method: "POST", body, headers: { "X-CSRF-Token": csrf }, credentials: "same-origin" })
      .then((r) => r.json().then((data) => ({ status: r.status, data })));
  }

  function save(field) {
    if (!changed(field)) return pending;
    const before = new Map(inputsOf(field).map((el) => [el, saved.get(el)]));
    const now = new Map(inputsOf(field).map((el) => [el, valueOf(el)]));
    busy++;
    note(field, [text(t("Saving …"))]);
    pending = pending.then(() => send({ field })).then(({ data }) => {
      if (!data.ok) {
        note(field, [text(data.error || t("Could not save."))], true);
        return;
      }
      setRevision(data.revision);
      applyReview(data);
      now.forEach((v, el) => saved.set(el, v));
      if (!data.undo) { note(field, [text(t("Saved"))]); return; }
      const undo = document.createElement("button");
      undo.type = "button";
      undo.className = "linklike";
      undo.textContent = t("Undo");
      undo.addEventListener("click", () => undoChange(field, data.undo, before));
      note(field, [text(data.locked ? t("Saved and locked 🔒") + " · " : t("Saved") + " · "), undo]);
    }).catch(() => note(field, [text(t("Could not save – check the connection."))], true))
      .finally(() => { busy--; });
    return pending;
  }

  function undoChange(field, snap, before) {
    busy++;
    pending = pending.then(() => send({ undo: JSON.stringify(snap) })).then(({ data }) => {
      if (!data.ok) { note(field, [text(data.error || t("Could not undo."))], true); return; }
      setRevision(data.revision);
      applyReview(data);
      before.forEach((v, el) => {
        if (el.type === "checkbox") el.checked = v === "on"; else el.value = v;
        saved.set(el, v);
      });
      note(field, [text(t("Undone"))]);
    }).catch(() => note(field, [text(t("Could not undo."))], true))
      .finally(() => { busy--; });
  }

  // when a field is finished: leaving it (text, date, text area), picking a value (select),
  // ticking a lock; the date field only on leaving, so half-typed dates are never saved
  form.addEventListener("focusout", (e) => {
    const field = fieldOf(e.target);
    if (!field || e.target.type === "checkbox" || e.target.tagName === "SELECT") return;
    // custom fields: a row counts as finished when the focus leaves the table
    if (field === "custom_fields" && e.relatedTarget && e.relatedTarget.closest("table.cf")) return;
    save(field);
  });
  form.addEventListener("change", (e) => {
    const field = fieldOf(e.target);
    if (field && (e.target.type === "checkbox" || e.target.tagName === "SELECT")) {
      if (field === "custom_fields" && e.target.closest("table.cf").contains(document.activeElement)) return;
      save(field);
    }
  });
  // Enter in a single-line field: finished (instead of submitting the whole form)
  form.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT" && fieldOf(e.target)) {
      e.preventDefault();
      e.target.blur();
    }
  });

  // before anything leaves the page: save what is still open, wait for running saves
  async function flush() {
    const el = document.activeElement;
    if (el && form.contains(el) && fieldOf(el)) save(fieldOf(el));
    for (const field of new Set([...saved.keys()].map(fieldOf))) if (changed(field)) save(field);
    await pending;
  }
  document.addEventListener("click", async (e) => {
    const a = e.target.closest("a[href]");
    if (!a || a.target || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey) return;
    if (!busy && !dirty()) return;
    e.preventDefault();
    await flush();
    location.href = a.href;
  }, true);
  document.addEventListener("submit", async (e) => {
    const f = e.target;
    if (f.dataset.flushed || (!busy && !dirty())) return;
    e.preventDefault();
    await flush();
    f.dataset.flushed = "1";
    f.requestSubmit(e.submitter || undefined);
  }, true);
  window.addEventListener("beforeunload", (e) => {
    if (busy || dirty()) { e.preventDefault(); e.returnValue = ""; }
  });
})();
