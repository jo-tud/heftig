"use strict";
// Document page: every field is saved as soon as it is finished - when the user leaves it or
// picks a value, never while typing. "Saved · Undo" appears next to it; undo restores the
// value, its lock and the AI suggestions of that field. Notes are saved when their field is
// left (and after a pause in typing); a new note then moves into the list above and the field
// is empty for the next one.
// Links and other buttons on the page wait for a save still running, so nothing typed is
// lost. When the page is left otherwise (back button, closing the tab, reload), what is still
// open is sent along on the way out and remembered for the tab: if the page loads again
// before the server has it (a reload is quicker than the save), it waits for it and loads
// once more, or saves it again.
(function () {
  const form = document.getElementById("meta-form");
  if (!form || !window.fetch) return;
  form.classList.add("autosaving");
  const notesBox = document.querySelector("section.notes");
  if (notesBox) notesBox.classList.add("autosaving");
  const csrf = document.body.dataset.csrf || "";
  const docId = form.getAttribute("action").split("/")[2];
  const draftKey = "heftig-drafts:" + docId;
  let pending = Promise.resolve();
  let busy = 0;
  const failed = new Set(); // fields and note forms whose last save went wrong
  const text = (s) => document.createTextNode(s);
  const revision = () => form.querySelector('input[name="revision"]').value;
  const pageRevision = Number(revision());

  // --- metadata fields ------------------------------------------------------------------

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
  function serverValueOf(el) {
    if (el.type === "checkbox") return el.defaultChecked ? "on" : "";
    if (el.tagName === "SELECT") return ([...el.options].find((o) => o.defaultSelected) || el.options[0] || { value: "" }).value;
    return el.defaultValue;
  }
  function setValue(el, v) {
    if (el.type === "checkbox") el.checked = v === "on"; else el.value = v;
    el.dispatchEvent(new Event("input", { bubbles: true })); // e.g. the tag chips show it
  }
  const tracked = [...form.querySelectorAll("input, select, textarea")].filter((el) => el.name && fieldOf(el));
  // the page shows what the server has, not what the browser kept from an earlier visit
  tracked.forEach((el) => { if (valueOf(el) !== serverValueOf(el)) setValue(el, serverValueOf(el)); });
  const confirmed = new Map(tracked.map((el) => [el, valueOf(el)])); // what the server has
  const sent = new Map(confirmed); // what is on its way there (or there)
  const changed = (field) => inputsOf(field).some((el) => sent.get(el) !== valueOf(el));
  const changedFields = () => [...new Set(tracked.filter((el) => sent.get(el) !== valueOf(el)).map(fieldOf))];

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

  // small requests outlive the page, so a save still running when it is left is not lost
  function post(url, body) {
    let size = 0;
    for (const [, v] of body) size += typeof v === "string" ? v.length * 3 + 100 : 64000;
    return fetch(url, { method: "POST", body, headers: { "X-CSRF-Token": csrf }, credentials: "same-origin", keepalive: size < 16000 })
      .then((r) => r.json().then((data) => ({ status: r.status, data })));
  }
  // the form as it is now (the value being saved); the revision when it is sent
  function metaBody(extra) {
    const body = new FormData(form);
    body.set("autosave", "1");
    for (const [k, v] of Object.entries(extra)) body.set(k, v);
    return body;
  }
  function sendMeta(body) {
    body.set("revision", revision());
    return post(form.getAttribute("action"), body);
  }
  function unsend(values) {
    values.forEach((v, el) => { if (sent.get(el) === v) sent.set(el, confirmed.get(el)); });
  }

  function save(field) {
    if (!changed(field)) return pending;
    const before = new Map(inputsOf(field).map((el) => [el, confirmed.get(el)]));
    const now = new Map(inputsOf(field).map((el) => [el, valueOf(el)]));
    const body = metaBody({ field });
    now.forEach((v, el) => sent.set(el, v));
    busy++;
    note(field, [text(t("Saving …"))]);
    pending = pending.then(() => sendMeta(body)).then(({ data }) => {
      if (!data.ok) {
        failed.add(field);
        unsend(now);
        note(field, [text(data.error || t("Could not save."))], true);
        return;
      }
      failed.delete(field);
      setRevision(data.revision);
      applyReview(data);
      now.forEach((v, el) => confirmed.set(el, v));
      if (!data.undo) { note(field, [text(t("Saved"))]); return; }
      const undo = document.createElement("button");
      undo.type = "button";
      undo.className = "linklike";
      undo.textContent = t("Undo");
      undo.addEventListener("click", () => undoChange(field, data.undo, before));
      note(field, [text(data.locked ? t("Saved and locked 🔒") + " · " : t("Saved") + " · "), undo]);
    }).catch(() => { failed.add(field); unsend(now); note(field, [text(t("Could not save – check the connection."))], true); })
      .finally(() => { busy--; });
    return pending;
  }

  function undoChange(field, snap, before) {
    busy++;
    pending = pending.then(() => sendMeta(metaBody({ undo: JSON.stringify(snap) }))).then(({ data }) => {
      if (!data.ok) { note(field, [text(data.error || t("Could not undo."))], true); return; }
      setRevision(data.revision);
      applyReview(data);
      before.forEach((v, el) => {
        confirmed.set(el, v);
        sent.set(el, v);
        setValue(el, v);
      });
      note(field, [text(t("Undone"))]);
    }).catch(() => note(field, [text(t("Could not undo."))], true))
      .finally(() => { busy--; });
  }

  // --- notes ----------------------------------------------------------------------------

  const NOTE_FORMS = "form.note-add, .note-edit form";
  const noteForms = () => (notesBox ? [...notesBox.querySelectorAll(NOTE_FORMS)] : []);
  const isAdd = (f) => f.classList.contains("note-add");
  const noteText = (f) => f.elements.text.value;
  const noteId = (f) => (f.elements.note_id ? f.elements.note_id.value : "");
  const noteSent = new Map();
  const noteConfirmed = new Map();
  const confirmedText = (f) => (noteConfirmed.has(f) ? noteConfirmed.get(f) : f.elements.text.defaultValue);
  const sentText = (f) => (noteSent.has(f) ? noteSent.get(f) : confirmedText(f));
  // a new note that is still empty is no change; an emptied note is (it is deleted)
  const meaningful = (f) => !isAdd(f) || f.dataset.created === "1" || noteText(f).trim() !== "";
  const noteChanged = (f) => sentText(f) !== noteText(f) && meaningful(f);
  const noteUnconfirmed = (f) => confirmedText(f) !== noteText(f) && meaningful(f);
  const lastHtml = new Map(); // the new note as the list shows it
  const timers = new Map();
  noteForms().forEach((f) => { f.elements.text.value = f.elements.text.defaultValue; });

  function randomId() {
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    return [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  }
  // a new note gets its id here, before it is first sent: sent twice, it is still one note
  function ensureId(f) {
    let id = f.elements.note_id;
    if (!id) {
      id = document.createElement("input");
      id.type = "hidden";
      id.name = "note_id";
      f.appendChild(id);
    }
    if (!id.value) id.value = randomId();
    return id.value;
  }
  function noteBody(f) {
    if (isAdd(f)) ensureId(f);
    const body = new FormData(f);
    body.set("autosave", "1");
    body.set("action", isAdd(f) ? "add" : "save");
    return body;
  }

  function saveNote(f) {
    clearTimeout(timers.get(f));
    if (!noteChanged(f)) return pending;
    const value = noteText(f);
    const body = noteBody(f);
    noteSent.set(f, value);
    busy++;
    status(f, [text(t("Saving …"))]);
    const fail = (message) => {
      failed.add(f);
      if (noteSent.get(f) === value) noteSent.set(f, confirmedText(f));
      status(f, [text(message)], true);
    };
    pending = pending.then(() => post(f.getAttribute("action"), body)).then(({ data }) => {
      if (!data.ok) { fail(data.error || t("Could not save.")); return; }
      failed.delete(f);
      noteConfirmed.set(f, value);
      setRevision(data.revision);
      noteDone(f, data, value);
    }).catch(() => fail(t("Could not save – check the connection.")))
      .finally(() => { busy--; });
    return pending;
  }
  function noteDone(f, data, value) {
    if (isAdd(f)) {
      if (data.deleted) {
        f.dataset.created = "";
        f.elements.note_id.value = "";
        status(f, [text(t("Note deleted"))]);
        return;
      }
      f.dataset.created = "1";
      lastHtml.set(f, data.note_html);
      status(f, [text(t("Note saved"))]);
      settle(f);
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
  // a new note, saved and left: into the list above, the field empty for the next one
  function settle(f) {
    if (!isAdd(f) || f.dataset.created !== "1" || f.contains(document.activeElement)) return;
    if (noteText(f) !== confirmedText(f) || !lastHtml.get(f)) return;
    const tpl = document.createElement("template");
    tpl.innerHTML = lastHtml.get(f);
    const article = tpl.content.firstElementChild;
    f.before(article);
    status(article, [text(t("Note saved"))]);
    f.elements.text.value = "";
    noteSent.set(f, "");
    noteConfirmed.set(f, "");
    f.dataset.created = "";
    f.elements.note_id.value = "";
    lastHtml.delete(f);
    const box = f.querySelector(":scope > .autosave-note");
    if (box) box.remove();
  }

  if (notesBox) {
    // finished when the focus leaves the note's form (its own buttons save it themselves)
    notesBox.addEventListener("focusout", (e) => {
      const f = e.target.closest(NOTE_FORMS);
      if (!f || (e.relatedTarget && f.contains(e.relatedTarget))) return;
      saveNote(f).then(() => settle(f));
    });
    // a pause in typing saves too: less is open when the page is left suddenly
    notesBox.addEventListener("input", (e) => {
      const f = e.target.closest(NOTE_FORMS);
      if (!f) return;
      clearTimeout(timers.get(f));
      timers.set(f, setTimeout(() => saveNote(f), 1500));
    });
    // Ctrl+Enter: the note is finished
    notesBox.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.closest(NOTE_FORMS)) {
        e.preventDefault();
        e.target.blur();
      }
    });
  }

  // --- when fields are finished ---------------------------------------------------------

  // leaving a field (text, date, text area), picking a value (select), ticking a lock; the
  // date field only on leaving, so half-typed dates are never saved
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

  // --- before anything leaves the page --------------------------------------------------

  const dirty = (except) => changedFields().length > 0 || noteForms().some((f) => f !== except && noteChanged(f));
  // changes whose last save failed: leaving the page would lose them
  const unsavable = () => changedFields().some((f) => failed.has(f)) || noteForms().some((f) => failed.has(f) && noteChanged(f));

  // save what is still open, wait for running saves
  async function flush(except) {
    changedFields().forEach(save);
    noteForms().forEach((f) => { if (f !== except) saveNote(f); });
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

  // leaving without a link (back button, closing the tab, reload): what the server does not
  // have yet is remembered for this tab, and what was not sent yet goes out now
  function storeDrafts() {
    const meta = {};
    tracked.forEach((el) => { if (valueOf(el) !== confirmed.get(el)) meta[el.name] = valueOf(el); });
    const notes = noteForms().filter(noteUnconfirmed)
      .map((f) => ({ id: isAdd(f) ? ensureId(f) : noteId(f), add: isAdd(f), text: noteText(f) }));
    if (!Object.keys(meta).length && !notes.length) return;
    try {
      sessionStorage.setItem(draftKey, JSON.stringify({ at: Date.now(), rev: pageRevision, meta, notes, tries: 0 }));
    } catch (_) { /* no storage: the requests below still go out */ }
  }
  function sendNow() {
    const fields = changedFields();
    if (fields.length) {
      const now = new Map(tracked.map((el) => [el, valueOf(el)]));
      const body = metaBody({ field: "*", fields: fields.join(",") });
      now.forEach((v, el) => sent.set(el, v));
      sendMeta(body).then(({ data }) => {
        // only reached if the page stays after all
        if (!data.ok) throw new Error(data.error);
        now.forEach((v, el) => confirmed.set(el, v));
        setRevision(data.revision);
        applyReview(data);
        fields.forEach((field) => note(field, [text(t("Saved"))]));
      }).catch(() => {
        unsend(now);
        fields.forEach((field) => { failed.add(field); note(field, [text(t("Could not save."))], true); });
      });
    }
    noteForms().filter(noteChanged).forEach((f) => {
      const value = noteText(f);
      const body = noteBody(f);
      noteSent.set(f, value);
      post(f.getAttribute("action"), body).then(({ data }) => {
        if (!data.ok) throw new Error(data.error);
        noteConfirmed.set(f, value);
        setRevision(data.revision);
        noteDone(f, data, value);
      }).catch(() => {
        if (noteSent.get(f) === value) noteSent.set(f, confirmedText(f));
        failed.add(f);
        status(f, [text(t("Could not save."))], true);
      });
    });
  }
  function leaving() {
    storeDrafts();
    if (dirty()) sendNow();
  }
  window.addEventListener("beforeunload", (e) => {
    leaving();
    if (unsavable()) { e.preventDefault(); e.returnValue = ""; }
  });
  // phones often leave a page without "beforeunload"
  window.addEventListener("pagehide", leaving);

  // the page loaded again right after it was left with something still open (a reload):
  // if the server does not show it yet, wait for its save and load once more - or, if it
  // never arrives, put it back into the fields and save it now
  function checkDrafts() {
    let d = null;
    try { d = JSON.parse(sessionStorage.getItem(draftKey) || "null"); } catch (_) { return; }
    if (!d) return;
    const done = () => { try { sessionStorage.removeItem(draftKey); } catch (_) { /* ignore */ } };
    if (Date.now() - d.at > 60000) { done(); return; }
    const editForm = (id) => noteForms().find((f) => !isAdd(f) && noteId(f) === id);
    const metaMissing = Object.entries(d.meta).some(([name, v]) => {
      const el = tracked.find((x) => x.name === name);
      return el && confirmed.get(el) !== v;
    });
    const notesMissing = d.notes.some((n) => {
      const f = editForm(n.id);
      if (!n.text.trim()) return !!f; // emptied: deleted
      return !f || f.elements.text.defaultValue.trim() !== n.text.trim();
    });
    if (!metaMissing && !notesMissing) { done(); return; }
    const restore = () => {
      done();
      Object.entries(d.meta).forEach(([name, v]) => {
        const el = tracked.find((x) => x.name === name);
        if (el && valueOf(el) !== v) setValue(el, v);
      });
      d.notes.forEach((n) => {
        let f = editForm(n.id);
        if (!f && n.add && n.text.trim()) {
          f = notesBox.querySelector("form.note-add");
          f.elements.text.value = n.text;
          ensureId(f);
          f.elements.note_id.value = n.id;
        } else if (f) {
          f.closest("details").open = true;
          f.elements.text.value = n.text;
        }
      });
      flush().then(() => noteForms().forEach(settle));
    };
    if (d.tries > 0) { restore(); return; }
    d.tries = 1;
    try { sessionStorage.setItem(draftKey, JSON.stringify(d)); } catch (_) { restore(); return; }
    const started = Date.now();
    const poll = () => fetch(`/api/documents/${docId}`, { credentials: "same-origin", cache: "no-store" })
      .then((r) => r.json())
      .then((j) => {
        if (j.metadata && j.metadata.revision > pageRevision) { location.reload(); return; }
        if (Date.now() - started > 5000) restore(); else setTimeout(poll, 300);
      })
      .catch(() => { if (Date.now() - started > 5000) restore(); else setTimeout(poll, 300); });
    poll();
  }
  checkDrafts();
})();
