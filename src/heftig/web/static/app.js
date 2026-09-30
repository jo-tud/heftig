"use strict";
(function () {
  const csrf = document.body.dataset.csrf || "";

  // Local time for <time datetime="…Z">
  const fmt = new Intl.DateTimeFormat(document.documentElement.lang || "en", { dateStyle: "medium", timeStyle: "short" });
  document.querySelectorAll("time[datetime]").forEach((el) => {
    const d = new Date(el.getAttribute("datetime"));
    if (!isNaN(d)) { el.textContent = fmt.format(d); el.title = el.getAttribute("datetime"); }
  });

  // "/" focuses the search field
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
      const q = document.getElementById("q");
      if (q) { e.preventDefault(); q.focus(); q.select(); }
    }
  });

  // "← Dokumente" returns to the result list the user came from (with its filters and scroll)
  document.querySelectorAll("a[data-back]").forEach((a) => a.addEventListener("click", (e) => {
    try {
      const ref = new URL(document.referrer);
      if (ref.origin === location.origin && ref.pathname === "/" && history.length > 1) {
        e.preventDefault();
        history.back();
      }
    } catch (_) { /* no referrer: follow the link */ }
  }));

  // Confirmation for a single button (e.g. a bulk action inside a larger form)
  document.querySelectorAll("button[data-confirm-click]").forEach((b) => {
    b.addEventListener("click", (e) => { if (!window.confirm(b.dataset.confirmClick)) e.preventDefault(); });
  });

  // Confirmation for destructive forms
  document.querySelectorAll("form[data-confirm]").forEach((f) => {
    f.addEventListener("submit", (e) => { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  // Upload with progress (falls back to the plain form without JS)
  const form = document.getElementById("upload-form");
  if (form) {
    const input = document.getElementById("files");
    const zone = document.getElementById("dropzone");
    const list = document.getElementById("upload-list");
    const labels = { created: t("newly archived"), duplicate: t("already there (duplicate)"), rejected: t("rejected") };

    const uploadOne = (file) => {
      const li = document.createElement("li");
      li.innerHTML = '<strong></strong> <progress max="1" value="0"></progress> <span class="state"></span>';
      li.querySelector(".state").textContent = t("loading …");
      li.querySelector("strong").textContent = file.name;
      list.prepend(li);
      const fd = new FormData();
      fd.append("files", file, file.name);
      fd.append("kind", form.querySelector("input[name=kind]:checked").value);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/documents");
      xhr.setRequestHeader("X-CSRF-Token", csrf);
      xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) li.querySelector("progress").value = ev.loaded / ev.total; };
      xhr.onload = () => {
        const state = li.querySelector(".state");
        li.querySelector("progress").remove();
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
        if (data && data.results) {
          const r = data.results[0];
          li.className = "res-" + r.status;
          state.textContent = "– " + (labels[r.status] || r.status) + (r.message ? ": " + r.message : "");
          if (r.document_id) {
            const a = document.createElement("a");
            a.href = "/documents/" + r.document_id; a.textContent = " " + t("Open document");
            li.appendChild(a);
          }
        } else {
          li.className = "res-rejected";
          state.textContent = "– " + t("Error: %(message)s", { message: (data && data.error && data.error.message) || xhr.status });
        }
      };
      xhr.onerror = () => { li.className = "res-rejected"; li.querySelector(".state").textContent = "– " + t("Network error"); };
      xhr.send(fd);
    };
    const handle = (files) => Array.from(files).forEach(uploadOne);
    input.addEventListener("change", () => { handle(input.files); input.value = ""; });
    ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
    zone.addEventListener("drop", (e) => handle(e.dataTransfer.files));
    form.addEventListener("submit", (e) => { e.preventDefault(); handle(input.files); });
  }

  // Inbox: live progress - counts update in place; the page is never reloaded under the user
  const working = document.getElementById("working");
  if (working) {
    const tick = async () => {
      try {
        const r = await fetch("/inbox/progress", { headers: { Accept: "application/json" } });
        if (!r.ok) return;
        const data = await r.json();
        document.querySelectorAll("[data-count]").forEach((el) => { el.textContent = data.counts[el.dataset.count]; });
        const busy = (data.counts.queued || 0) + (data.counts.processing || 0);
        working.hidden = busy === 0;
        const list = document.getElementById("active-jobs");
        if (list) {
          list.querySelectorAll("li[data-job]").forEach((li) => {
            const j = data.jobs.find((x) => String(x.id) === li.dataset.job);
            if (!j) { li.classList.add("finished"); li.querySelector("progress").value = 1; return; }
            li.querySelector("progress").value = j.progress;
          });
        }
        setTimeout(tick, busy ? 3000 : 15000);
      } catch (_) { setTimeout(tick, 10000); }
    };
    setTimeout(tick, 3000);
  }
  // Search by meaning: the number of prepared documents updates in place while the worker
  // embeds them (settings pages)
  const meaning = document.querySelector("[data-meaning-progress]");
  if (meaning) {
    const show = (name, on) => meaning.querySelectorAll(`[data-meaning="${name}"]`).forEach((el) => { el.hidden = !on; });
    const tick = async () => {
      try {
        const r = await fetch("/settings/search/progress", { headers: { Accept: "application/json" } });
        if (!r.ok) return;
        const s = await r.json();
        meaning.querySelectorAll("[data-meaning=done]").forEach((el) => { el.textContent = s.done; });
        meaning.querySelectorAll("[data-meaning=total]").forEach((el) => { el.textContent = s.total; });
        show("waiting", !s.downloaded);
        show("counts", s.downloaded);
        if (s.active && (!s.downloaded || s.done < s.total)) setTimeout(tick, 5000);
      } catch (_) { setTimeout(tick, 15000); }
    };
    setTimeout(tick, 5000);
  }
  // Text areas that grow with their text (the summary): the whole text visible, up to a limit
  document.querySelectorAll("textarea.autogrow").forEach((el) => {
    const fit = () => {
      if (!el.offsetParent) return; // hidden (a closed section): keep its rows until shown
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight + 2, window.innerHeight * 0.5)}px`;
    };
    el.addEventListener("input", fit);
    el.addEventListener("focus", fit);
    fit();
  });
  // "Mehr" menus close on a click elsewhere or Escape
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.menu[open]").forEach((m) => { if (!m.contains(e.target)) m.open = false; });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll("details.menu[open]").forEach((m) => { m.open = false; });
  });
  // page viewers: hold the mouse button and drag to move the pages; a click without movement
  // stays a click (reveal, double-click zoom). Touch keeps the browser's own scrolling.
  document.querySelectorAll(".viewer").forEach((v) => {
    let start = null;
    v.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "mouse" || e.button !== 0 || e.target.closest("a, button, input")) return;
      start = { x: e.clientX, y: e.clientY, left: v.scrollLeft, top: v.scrollTop, moved: false, id: e.pointerId };
    });
    v.addEventListener("pointermove", (e) => {
      if (!start || e.pointerId !== start.id) return;
      const dx = e.clientX - start.x, dy = e.clientY - start.y;
      if (!start.moved && Math.hypot(dx, dy) < 5) return;
      if (!start.moved) { start.moved = true; v.classList.add("dragging"); v.setPointerCapture(e.pointerId); }
      v.scrollLeft = start.left - dx;
      v.scrollTop = start.top - dy;
    });
    const end = () => {
      if (start && start.moved) {
        v.classList.remove("dragging");
        // the click that ends a drag must not reveal the page (privacy mode checks this)
        v.dataset.dragEnd = String(performance.now());
      }
      start = null;
    };
    v.addEventListener("pointerup", end);
    v.addEventListener("pointercancel", end);
    v.addEventListener("dragstart", (e) => e.preventDefault());
  });
  // list filter while typing (e.g. categories): rows carry data-name
  document.querySelectorAll("input[data-filter]").forEach((input) => {
    const list = document.querySelector(input.dataset.filter);
    const none = document.getElementById("cat-none");
    if (!list) return;
    const apply = () => {
      const q = input.value.trim().toLowerCase();
      let shown = 0;
      list.querySelectorAll("li[data-name]").forEach((li) => {
        const hit = !q || li.dataset.name.includes(q);
        li.hidden = !hit;
        shown += hit;
      });
      if (none) none.hidden = shown > 0;
    };
    input.addEventListener("input", apply);
  });
  document.querySelectorAll("select[data-autosubmit]").forEach((sel) => {
    sel.addEventListener("change", () => sel.form.requestSubmit ? sel.form.requestSubmit() : sel.form.submit());
  });
})();
