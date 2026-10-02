"use strict";
/* Page viewer: server-rendered page images, zoom, page indicator, fullscreen, keyboard. */
(function () {
  const viewer = document.getElementById("viewer");
  if (!viewer) return;
  // page proportions before the images load (the CSP forbids style attributes in the HTML)
  viewer.querySelectorAll(".vpage[data-ar]").forEach((f) => { f.style.aspectRatio = f.dataset.ar; });
  const card = document.getElementById("viewer-card");
  const cur = document.getElementById("vp-cur");
  const zoomLabel = document.getElementById("vp-zoom");
  const STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 4];
  let zoom = 1;

  function setZoom(z, anchor) {
    const old = zoom;
    zoom = Math.min(4, Math.max(0.5, z));
    // keep the point under the cursor/finger (or the centre) in place
    const r = viewer.getBoundingClientRect();
    const ax = anchor ? anchor.x - r.left : r.width / 2;
    const ay = anchor ? anchor.y - r.top : r.height / 2;
    const cx = (viewer.scrollLeft + ax) / old;
    const cy = (viewer.scrollTop + ay) / old;
    viewer.style.setProperty("--zoom", zoom);
    viewer.classList.toggle("zoomed", zoom > 1);
    viewer.scrollLeft = cx * zoom - ax;
    viewer.scrollTop = cy * zoom - ay;
    zoomLabel.textContent = t("%(pct)s%", { pct: Math.round(zoom * 100) });
  }
  const step = (dir) => {
    const next = dir > 0 ? STEPS.find((s) => s > zoom + 0.01) : [...STEPS].reverse().find((s) => s < zoom - 0.01);
    if (next) setZoom(next);
  };

  document.querySelectorAll("[data-zoom]").forEach((b) => {
    b.addEventListener("click", () => {
      const d = b.dataset.zoom;
      if (d === "in") step(1);
      else if (d === "out") step(-1);
      else setZoom(1);
    });
  });

  viewer.addEventListener("dblclick", (e) => setZoom(zoom > 1 ? 1 : 2, { x: e.clientX, y: e.clientY }));
  viewer.addEventListener("wheel", (e) => {
    if (!e.ctrlKey) return; // trackpad pinch / ctrl+wheel
    e.preventDefault();
    setZoom(zoom * (e.deltaY < 0 ? 1.1 : 0.9), { x: e.clientX, y: e.clientY });
  }, { passive: false });

  // two-finger pinch inside the viewer (the page itself keeps its normal zoom)
  let pinch = null;
  viewer.addEventListener("touchstart", (e) => {
    if (e.touches.length === 2) {
      const [a, b] = e.touches;
      pinch = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), z: zoom };
    }
  }, { passive: true });
  viewer.addEventListener("touchmove", (e) => {
    if (!pinch || e.touches.length !== 2) return;
    e.preventDefault();
    const [a, b] = e.touches;
    const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    setZoom(pinch.z * (d / pinch.d), { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 });
  }, { passive: false });
  viewer.addEventListener("touchend", () => { pinch = null; });

  // current page indicator: the last page whose top is above 30 % of the viewer height
  const pages = [...viewer.querySelectorAll(".vpage")];
  let ticking = false;
  function updateCurrent() {
    ticking = false;
    const line = viewer.scrollTop + viewer.clientHeight * 0.3;
    let n = pages.length ? Number(pages[0].dataset.page) : 1;
    for (const p of pages) {
      if (p.offsetTop <= line) n = Number(p.dataset.page);
      else break;
    }
    cur.textContent = n;
  }
  viewer.addEventListener("scroll", () => {
    if (!ticking) { ticking = true; requestAnimationFrame(updateCurrent); }
  }, { passive: true });

  // the next / previous page shown (hidden blank pages are not in the viewer)
  function neighbour(n, dir) {
    const i = pages.findIndex((p) => Number(p.dataset.page) === n);
    const p = pages[i + dir];
    return p ? Number(p.dataset.page) : n;
  }
  function gotoPage(n) {
    const el = document.getElementById(`page-${n}`);
    if (!el) return;
    viewer.scrollTo({ top: el.offsetTop - 8, behavior: "smooth" });
    card.scrollIntoView({ block: "start", behavior: "smooth" });
  }
  document.querySelectorAll("a.goto-page").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    gotoPage(a.getAttribute("href").slice(6));
  }));

  // search hits: jump to the first page with a hit and mark the words (boxes from the server)
  const q = viewer.dataset.q;
  if (q) {
    const hitPages = (viewer.dataset.hitPages || "").split(",").map(Number).filter(Boolean);
    const loaded = new Set();
    const loadHits = async (n) => {
      if (loaded.has(n)) return;
      loaded.add(n);
      const fig = document.getElementById(`page-${n}`);
      try {
        const r = await fetch(`${location.pathname}/pages/${n}/hits?q=${encodeURIComponent(q)}`,
          { headers: { Accept: "application/json" } });
        if (!r.ok) return;
        const data = await r.json();
        data.boxes.forEach(([x0, y0, x1, y1]) => {
          const m = document.createElement("span");
          m.className = "hl";
          m.style.left = `${x0 * 100}%`;
          m.style.top = `${y0 * 100}%`;
          m.style.width = `${(x1 - x0) * 100}%`;
          m.style.height = `${(y1 - y0) * 100}%`;
          fig.appendChild(m);
        });
      } catch (e) { /* no marks - the page jump still works */ }
    };
    if (hitPages.length) {
      const first = document.getElementById(`page-${hitPages[0]}`);
      if (first && hitPages[0] > 1) viewer.scrollTop = first.offsetTop - 8;
      loadHits(hitPages[0]);
      const io = new IntersectionObserver((entries) => entries.forEach((e) => {
        if (e.isIntersecting) loadHits(Number(e.target.dataset.page));
      }), { root: viewer, rootMargin: "200px" });
      hitPages.slice(1).forEach((n) => { const f = document.getElementById(`page-${n}`); if (f) io.observe(f); });
    }
    const toggle = document.querySelector("[data-hl-toggle]");
    if (toggle) toggle.addEventListener("click", () => {
      const off = viewer.classList.toggle("hl-off");
      toggle.textContent = off ? t("Show highlights") : t("Hide highlights");
      toggle.setAttribute("aria-pressed", String(!off));
    });
  }

  // fullscreen (native where available, otherwise a fixed overlay)
  function toggleFullscreen() {
    if (document.fullscreenElement) return document.exitFullscreen();
    if (card.requestFullscreen) {
      card.requestFullscreen().catch(() => card.classList.toggle("viewer-max"));
    } else {
      card.classList.toggle("viewer-max");
    }
  }
  document.querySelector("[data-fullscreen]").addEventListener("click", toggleFullscreen);

  viewer.addEventListener("keydown", (e) => {
    const page = Number(cur.textContent);
    if (e.key === "+" || e.key === "=") { step(1); e.preventDefault(); }
    else if (e.key === "-") { step(-1); e.preventDefault(); }
    else if (e.key === "0") { setZoom(1); e.preventDefault(); }
    else if (e.key === "f" || e.key === "F") { toggleFullscreen(); e.preventDefault(); }
    else if (e.key === "PageDown" || e.key === "n") { gotoPage(neighbour(page, 1)); e.preventDefault(); }
    else if (e.key === "PageUp" || e.key === "p") { gotoPage(neighbour(page, -1)); e.preventDefault(); }
    else if (e.key === "Escape") card.classList.remove("viewer-max");
  });
})();

/* While the document is being processed: wait for the result, then show it (reload) - unless
   the user is typing in a field, then offer the reload. */
(function () {
  const busy = document.getElementById("doc-busy");
  if (!busy) return;
  const started = Date.now();
  function later() {
    const age = Date.now() - started;
    if (age < 30 * 60000) setTimeout(poll, age < 60000 ? 2000 : 10000);
  }
  function done() {
    const el = document.activeElement;
    if (!el || !el.matches("input, textarea, select")) { location.reload(); return; }
    busy.textContent = `${t("Done.")} `;
    const a = document.createElement("a");
    a.href = location.href;
    a.textContent = t("Show the result");
    busy.append(a);
  }
  function poll() {
    fetch(`/api/documents/${busy.dataset.doc}`, { credentials: "same-origin", cache: "no-store" })
      .then((r) => (r.ok ? r.json() : r.status === 404 ? "gone" : null))
      .then((j) => {
        if (j === "gone") { // deleted or split meanwhile: nothing more to wait for
          busy.textContent = t("This document is no longer in the archive (deleted or split).");
          return;
        }
        const st = j && j.metadata && j.metadata.status;
        if (st && st !== "queued" && st !== "processing") done(); else later();
      })
      .catch(later);
  }
  setTimeout(poll, 1500);
})();
