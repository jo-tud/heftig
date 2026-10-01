"use strict";
// Document page: every field is saved as soon as it is finished - when the user leaves it or
// picks a value, never while typing. "Saved · Undo" appears next to it; undo restores the
// value, its lock and the AI suggestions of that field. Notes are saved the same way: a new
// note is added when its field is left, and the field then keeps editing that note.
// Links and other buttons on the page wait for a save still running, so nothing typed is
// lost; when the page is left otherwise (back button, closing the tab), what is still open
// is sent along on the way out.
(function () {
  const form = document.getElementById("meta-form");
  if (!form || !window.fetch) return;
  form.classList.add("autosaving");
  const csrf = document.body.dataset.csrf || "";
  let pending = Promise.resolve();
  let busy = 0;
  const failed = new Set(); // fields and note forms whose last save went wrong

  // which saved field an input belongs to
  function fieldOf(el) {
    if (el.closest && el.closest(".tagbox")) return "tags";
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
  form.querySelectorAll("input, select, textarea").forEach((el) => { if (el.name && fieldOf(el)) saved.set(el, valueOf(el)); });
  const changed = (field) => inputsOf(field).some((el) => saved.get(el) !== valueOf(el));
  const changedFields = () => [...new Set([...saved.keys()].filter((el) => saved.get(el) !== valueOf(el)).map(fieldOf))];

  // notes: the field for a new one and the edit field of each note
  const noteForms = [...document.querySelectorAll("form.note-add, .note-edit form")];
  const noteText = (f) => f.elements.text.value;
  const noteId = (f) => (f.elements.note_id ? f.elements.note_id.value : "");
  const noteSaved = new Map(noteForms.map((f) => [f, noteText(f)]));
  // a new note that is still empty is no change; an emptied note is (it is deleted)
  const noteChanged = (f) => noteSaved.get(f) !== noteText(f) && (noteId(f) !== "" || noteText(f).trim() !== "");

  const dirty = (except) => changedFields().length > 0 || noteForms.some((f) => f !== except && noteChanged(f));
  // changes whose last save failed: leaving the page would lose them
  const unsavable = () => changedFields().some((f) => failed.has(f)) || noteForms.some((f) => failed.has(f) && noteChanged(f));

  function status(anchor, html, isError) {
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
  function note(field, html, isError) {
    const first = inputsOf(field)[0];
    return status((first && (first.closest(".field") || first.closest("fieldset"))) || form, html, isError);
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

  function post(url, body, keepalive) {
    return fetch(url, { method: "POST", body, headers: { "X-CSRF-Token": csrf }, credentials: "same-origin", keepalive: !!keepalive })
      .then((r) => r.json().then((data) => ({ status: r.status, data })));
  }
  function send(extra, keepalive) {
    const body = new FormData(form);
    body.set("autosave", "1");
    for (const [k, v] of Object.entries(extra)) body.set(k, v);
    return post(form.action, body, keepalive);
  }

  function save(field) {
    if (!changed(field)) return pending;
    const before = new Map(inputsOf(field).map((el) => [el, saved.get(el)]));
    const now = new Map(inputsOf(field).map((el) => [el, valueOf(el)]));
    busy++;
    note(field, [text(t("Saving …"))]);
    pending = pending.then(() => send({ field })).then(({ data }) => {
      if (!data.ok) {
        failed.add(field);
        note(field, [text(data.error || t("Could not save."))], true);
        return;
      }
      failed.delete(field);
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
    }).catch(() => { failed.add(field); note(field, [text(t("Could not save – check the connection."))], true); })
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
        el.dispatchEvent(new Event("input", { bubbles: true })); // e.g. the tag box shows it
      });
      note(field, [text(t("Undone"))]);
    }).catch(() => note(field, [text(t("Could not undo."))], true))
      .finally(() => { busy--; });
  }

  // a note: added while it is new (the form then edits that note), changed otherwise
  function noteBody(f) {
    const body = new FormData(f);
    body.set("autosave", "1");
    body.set("action", noteId(f) ? "save" : "add");
    return body;
  }
  function noteDone(f, data, value) {
    noteSaved.set(f, value);
    failed.delete(f);
    if (f.classList.contains("note-add")) {
      let id = f.elements.note_id;
      if (!id) {
        id = document.createElement("input");
        id.type = "hidden";
        id.name = "note_id";
        f.appendChild(id);
      }
      id.value = data.note_id || "";
      const button = f.querySelector("button[name=action]");
      button.value = data.note_id ? "save" : "add";
      button.textContent = data.note_id ? t("Done") : t("Add note");
      status(f, [text(data.deleted ? t("Note deleted") : t("Note saved"))]);
      return;
    }
    const article = f.closest("article.note");
    if (data.deleted) {
      article.replaceChildren(status(document.createElement("div"), [text(t("Note deleted"))]));
      return;
    }
    article.querySelector(".note-text").textContent = value.trim();
    status(f, [text(t("Note saved"))]);
  }
  function saveNote(f) {
    if (!noteChanged(f)) return pending;
    busy++;
    status(f, [text(t("Saving …"))]);
    pending = pending.then(() => {
      const value = noteText(f);
      return post(f.getAttribute("action"), noteBody(f)).then(({ data }) => {
        if (!data.ok) { failed.add(f); status(f, [text(data.error || t("Could not save."))], true); return; }
        setRevision(data.revision);
        noteDone(f, data, value);
      });
    }).catch(() => { failed.add(f); status(f, [text(t("Could not save – check the connection."))], true); })
      .finally(() => { busy--; });
    return pending;
  }

  // when a field is finished: leaving it (text, date, text area), picking a value (select),
  // ticking a lock; the date field only on leaving, so half-typed dates are never saved
  form.addEventListener("focusout", (e) => {
    const field = fieldOf(e.target);
    if (!field || e.target.type === "checkbox" || e.target.tagName === "SELECT") return;
    // custom fields: a row counts as finished when the focus leaves the table; tags when it
    // leaves the tag box
    const group = e.target.closest("table.cf, .tagbox");
    if (group && e.relatedTarget && group.contains(e.relatedTarget)) return;
    save(field);
  });
  form.addEventListener("change", (e) => {
    const field = fieldOf(e.target);
    // a hidden input is changed by a script, as a finished step (a tag removed)
    if (field && (e.target.type === "checkbox" || e.target.type === "hidden" || e.target.tagName === "SELECT")) {
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
  // a note is finished when the focus leaves its form - not for its own buttons, which save
  // it themselves
  noteForms.forEach((f) => f.addEventListener("focusout", (e) => {
    if (e.relatedTarget && f.contains(e.relatedTarget)) return;
    saveNote(f);
  }));

  // before anything leaves the page: save what is still open, wait for running saves
  async function flush(except) {
    changedFields().forEach(save);
    noteForms.forEach((f) => { if (f !== except) saveNote(f); });
    await pending;
  }
  let passing = null;
  document.addEventListener("click", async (e) => {
    const a = e.target.closest("a[href]");
    if (!a || a === passing || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
    // a new tab, a download, a place on this page: the page stays
    if (a.target || a.hasAttribute("download") || a.getAttribute("href").startsWith("#")) return;
    if (!busy && !dirty()) return;
    e.preventDefault();
    await flush();
    // the link as if clicked now (so "← Documents" still returns to the list it came from);
    // if a save failed, the browser asks before the page is left
    passing = a;
    try { a.click(); } finally { passing = null; }
  }, true);
  document.addEventListener("submit", async (e) => {
    const f = e.target;
    if (f.dataset.flushed || (!busy && !dirty(f))) return;
    e.preventDefault();
    let submitter = e.submitter;
    const row = submitter && submitter.closest("tr");
    const rowText = row && row.textContent;
    await flush(f);
    let target = f;
    if (!f.isConnected) {
      // the review box was renewed by the save: the same button there. A suggestion is
      // addressed by its position in the list, which the save may have changed - it is found
      // by its row; if the save settled it, there is nothing left to do.
      if (!submitter) return;
      const kind = submitter.value.replace(/_\d+$/, "_");
      const same = row
        ? [...document.querySelectorAll("#review-box tr")].filter((r) => r.textContent === rowText)
          .map((r) => [...r.querySelectorAll(`button[name="${submitter.name}"]`)].find((b) => b.value.startsWith(kind)))[0]
        : [...document.querySelectorAll(`button[name="${submitter.name}"]`)].find((b) => b.value === submitter.value);
      if (!same) return;
      submitter = same;
      target = same.form;
    }
    target.dataset.flushed = "1";
    try { target.requestSubmit(submitter || undefined); } finally { delete target.dataset.flushed; }
  }, true);

  // leaving the page without a link (back button, closing the tab, reload): what is still
  // open goes out in requests that outlive the page
  function sendNow() {
    const fields = changedFields();
    if (fields.length) {
      const before = new Map(saved);
      saved.forEach((v, el) => saved.set(el, valueOf(el)));
      pending = send({ field: "*", fields: fields.join(",") }, true).then(({ data }) => {
        // only reached if the page stays after all
        if (!data.ok) throw new Error(data.error);
        setRevision(data.revision);
        applyReview(data);
        fields.forEach((field) => note(field, [text(t("Saved"))]));
      }).catch(() => {
        before.forEach((v, el) => saved.set(el, v));
        fields.forEach((field) => { failed.add(field); note(field, [text(t("Could not save."))], true); });
      });
    }
    noteForms.filter(noteChanged).forEach((f) => {
      const value = noteText(f);
      const prev = noteSaved.get(f);
      noteSaved.set(f, value);
      post(f.getAttribute("action"), noteBody(f), true).then(({ data }) => {
        if (!data.ok) throw new Error(data.error);
        setRevision(data.revision);
        noteDone(f, data, value);
      }).catch(() => { noteSaved.set(f, prev); failed.add(f); status(f, [text(t("Could not save."))], true); });
    });
  }
  window.addEventListener("beforeunload", (e) => {
    if (!busy && !unsavable() && dirty()) sendNow();
    if (busy || (unsavable() && dirty())) { e.preventDefault(); e.returnValue = ""; }
  });
  // phones often leave a page without "beforeunload"
  window.addEventListener("pagehide", () => { if (!busy && dirty()) sendNow(); });
})();
