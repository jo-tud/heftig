"use strict";
/*
 * Search page: suggestions while typing, tag filter box, mobile filter sheet, recent searches.
 * The page works without this script (all filters are plain links and forms).
 */
(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => [...(root || document).querySelectorAll(sel)];
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* storage blocked */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* storage blocked */ } },
  };
  const RECENT = "heftig-recent-searches";

  // ---------------------------------------------------------------- suggestions (combobox)
  const input = $("#q");
  const box = $("#suggest");
  const form = $("#search-form");
  let items = [];
  let active = -1;
  let replaceFrag = "";
  let timer = null;
  let seq = 0;

  function currentParams() {
    const p = new URLSearchParams();
    $$("input[type=hidden]", form).forEach((h) => p.append(h.name, h.value));
    return p;
  }

  function close() {
    box.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  }

  function render(list) {
    items = list;
    active = -1;
    box.innerHTML = "";
    if (!list.length) return close();
    let group = null;
    list.forEach((it, i) => {
      if (it.group !== group) {
        group = it.group;
        const h = document.createElement("div");
        h.className = "sg-group";
        h.setAttribute("role", "presentation");
        h.textContent = group;
        box.appendChild(h);
      }
      const o = document.createElement("div");
      o.className = "sg-item sg-" + it.kind;
      o.id = "sg-" + i;
      o.setAttribute("role", "option");
      o.setAttribute("aria-selected", "false");
      const l = document.createElement("span");
      l.className = "sg-label";
      l.textContent = it.label;
      o.appendChild(l);
      if (it.detail) {
        const d = document.createElement("span");
        d.className = "sg-detail";
        d.textContent = it.detail;
        o.appendChild(d);
      }
      if (it.count) {
        const n = document.createElement("span");
        n.className = "sg-count";
        n.textContent = it.count;
        o.appendChild(n);
      }
      if (it.kind === "number") o.classList.add("pv");
      o.addEventListener("mousedown", (e) => { e.preventDefault(); choose(i); });
      box.appendChild(o);
    });
    box.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function highlight(i) {
    $$(".sg-item", box).forEach((el) => el.setAttribute("aria-selected", "false"));
    active = i;
    if (i < 0) return input.removeAttribute("aria-activedescendant");
    const el = $("#sg-" + i);
    el.setAttribute("aria-selected", "true");
    el.scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", el.id);
  }

  function choose(i) {
    const it = items[i];
    if (!it) return;
    close();
    if (it.href) { window.location.href = it.href; return; }
    if (it.param) {
      // selected value becomes a filter; the typed fragment is removed from the text
      const p = currentParams();
      let q = input.value;
      if (replaceFrag) {
        const at = q.toLowerCase().lastIndexOf(replaceFrag.toLowerCase());
        if (at >= 0) q = q.slice(0, at) + q.slice(at + replaceFrag.length);
      }
      q = q.replace(/\s+/g, " ").trim();
      if (q) p.set("q", q);
      if (!p.getAll(it.param).includes(it.value)) p.append(it.param, it.value);
      window.location.href = "/?" + p.toString();
      return;
    }
    if (it.kind === "recent" || it.kind === "saved") { window.location.href = "/?" + it.query; return; }
    form.requestSubmit ? form.requestSubmit() : form.submit();
  }

  async function fetchSuggestions() {
    const text = input.value.trim();
    const mine = ++seq;
    if (!text) { showRecent(); return; }
    try {
      const r = await fetch("/api/suggest?q=" + encodeURIComponent(text), { headers: { Accept: "application/json" } });
      if (!r.ok || mine !== seq) return;
      const data = await r.json();
      if (mine !== seq) return;
      replaceFrag = data.replace || "";
      render(data.items);
    } catch (e) { /* offline: the plain search still works */ }
  }

  function showRecent() {
    let saved = [];
    try { saved = JSON.parse(($("#saved-data") || {}).textContent || "[]"); } catch (e) { /* ignore */ }
    const list = saved.slice(0, 6).map((s) => ({ kind: "saved", group: t("Saved searches"), label: "☆ " + s.name, query: s.query }));
    store.get(RECENT, []).slice(0, 6).forEach((r) => list.push({ kind: "recent", group: t("Recent searches"), label: r.label, query: r.query }));
    render(list);
  }

  if (input && box) {
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(fetchSuggestions, 120);
    });
    input.addEventListener("focus", () => { if (!input.value.trim()) showRecent(); });
    form.addEventListener("submit", (e) => {
      if (e.submitter && e.submitter.classList.contains("ai-btn")) {
        e.submitter.textContent = t("✦ AI is thinking…");
        e.submitter.setAttribute("aria-busy", "true");
      }
    });
    input.addEventListener("blur", () => setTimeout(close, 150));
    input.addEventListener("keydown", (e) => {
      const ai = $(".ai-btn", form);
      if (ai && e.key === "Enter" && (e.ctrlKey || e.metaKey)) { close(); e.preventDefault(); ai.click(); return; }
      if (box.hidden) {
        if (e.key === "ArrowDown") { fetchSuggestions(); e.preventDefault(); }
        return;
      }
      const n = items.length;
      if (e.key === "ArrowDown") { highlight((active + 1) % n); e.preventDefault(); }
      else if (e.key === "ArrowUp") { highlight(active <= 0 ? n - 1 : active - 1); e.preventDefault(); }
      else if (e.key === "Enter" && active >= 0) { e.preventDefault(); choose(active); }
      else if (e.key === "Escape") { close(); e.preventDefault(); }
    });
  }

  // ---------------------------------------------------------------- recent searches (this device)
  const info = $(".resultinfo[data-recent-query]");
  if (info && info.dataset.recentQuery && !("start" in info.dataset)) {
    const entry = { label: info.dataset.recentLabel, query: info.dataset.recentQuery };
    const list = store.get(RECENT, []).filter((r) => r.query !== entry.query);
    list.unshift(entry);
    store.set(RECENT, list.slice(0, 10));
  }
  const recentBox = $("#recent");
  if (recentBox) {
    const list = store.get(RECENT, []);
    const ul = $("ul", recentBox);
    list.slice(0, 8).forEach((r) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.className = "pill";
      a.href = "/?" + r.query;
      a.textContent = r.label;
      li.appendChild(a);
      ul.appendChild(li);
    });
    recentBox.hidden = list.length === 0;
    const clear = $("#recent-clear");
    if (clear) clear.addEventListener("click", () => { store.del(RECENT); recentBox.hidden = true; });
  }

  // ---------------------------------------------------------------- tag filter box
  const tagFilter = $(".tag-filter");
  if (tagFilter) {
    tagFilter.hidden = false;
    tagFilter.addEventListener("input", () => {
      const t = tagFilter.value.trim().toLowerCase();
      $$(".tag-cloud .tv").forEach((a) => { a.hidden = t && !a.dataset.name.includes(t) && !a.classList.contains("on"); });
    });
  }

  // ---------------------------------------------------------------- sort without extra button
  $$("select[data-autosubmit]").forEach((s) => s.addEventListener("change", () => s.form.submit()));

  // ---------------------------------------------------------------- mobile filter sheet
  const SHEET = "heftig-sheet-open";
  const facets = $("#facets");
  const backdrop = $(".sheet-backdrop");
  function openSheet(focus) {
    document.body.classList.add("sheet-open");
    if (backdrop) backdrop.hidden = false;
    try { sessionStorage.setItem(SHEET, "1"); } catch (e) { /* ignore */ }
    if (focus && facets) facets.focus();
  }
  function closeSheet() {
    document.body.classList.remove("sheet-open");
    if (backdrop) backdrop.hidden = true;
    try { sessionStorage.removeItem(SHEET); } catch (e) { /* ignore */ }
  }
  $$("[data-sheet-open]").forEach((b) => b.addEventListener("click", () => openSheet(true)));
  $$("[data-sheet-close]").forEach((b) => b.addEventListener("click", closeSheet));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && document.body.classList.contains("sheet-open")) closeSheet(); });
  // choosing a filter inside the open sheet reloads the page: keep the sheet open for the next one
  const narrow = window.matchMedia("(max-width: 800px)");
  let reopen = false;
  try { reopen = sessionStorage.getItem(SHEET) === "1"; } catch (e) { /* ignore */ }
  if (reopen && narrow.matches) {
    // same sheet as before the reload: no slide-in, same scroll position
    document.body.classList.add("sheet-instant");
    openSheet(false);
    try { facets.scrollTop = Number(sessionStorage.getItem(SHEET + "-y")) || 0; } catch (e) { /* ignore */ }
    requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.remove("sheet-instant")));
  } else closeSheet();
  if (facets) {
    facets.addEventListener("click", (e) => {
      const a = e.target.closest("a");
      if (a && narrow.matches) {
        try {
          sessionStorage.setItem(SHEET, "1");
          sessionStorage.setItem(SHEET + "-y", String(facets.scrollTop));
        } catch (err) { /* ignore */ }
      }
    });
  }
  // ---------------------------------------------------------------- larger preview on hover
  // desktop only; not in privacy mode (a larger blurred page would show nothing). The popup
  // only appears once the new page has loaded - never with the previous document's page.
  if (window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
    let pop = null, timer = null, seq = 0;
    const hide = () => { clearTimeout(timer); seq++; if (pop) pop.classList.remove("on"); };
    const place = (thumb) => {
      const r = thumb.getBoundingClientRect(), w = pop.offsetWidth || 420;
      const h = Math.min(pop.offsetHeight || w * 1.414, window.innerHeight - 16);
      const left = r.right + 12 + w < window.innerWidth ? r.right + 12 : Math.max(8, r.left - 12 - w);
      const top = Math.min(Math.max(8, r.top - 40), window.innerHeight - h - 8);
      pop.style.left = `${left}px`;
      pop.style.top = `${top}px`;
    };
    document.querySelectorAll(".thumb[data-preview]").forEach((thumb) => {
      thumb.addEventListener("mouseenter", () => {
        if (document.documentElement.classList.contains("privacy")) return;
        hide();
        const mine = seq;
        timer = setTimeout(() => {
          if (!pop) {
            pop = document.createElement("div");
            pop.className = "hover-preview";
            pop.setAttribute("aria-hidden", "true");
            document.body.appendChild(pop);
          }
          const id = thumb.dataset.preview;
          const img = new Image();
          img.alt = "";
          img.sizes = "420px";
          img.srcset = `/documents/${id}/pages/1.webp?w=480 480w, /documents/${id}/pages/1.webp?w=960 960w`;
          img.src = `/documents/${id}/pages/1.webp?w=480`;
          const show = () => {
            if (mine !== seq) return; // the pointer moved on meanwhile
            pop.replaceChildren(img);
            place(thumb);
            pop.classList.add("on");
          };
          if (img.complete && img.naturalWidth) show();
          else img.addEventListener("load", show, { once: true });
        }, 250);
      });
      thumb.addEventListener("mouseleave", hide);
    });
    window.addEventListener("scroll", hide, { passive: true });
  }
})();
