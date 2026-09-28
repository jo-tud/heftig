"use strict";
/* Duplicate comparison: both documents side by side with shared zoom and scrolling, the
   differing areas marked, and keyboard shortcuts for a quick decision. */
(function () {
  const viewers = [...document.querySelectorAll(".cviewer")];
  if (viewers.length !== 2) return;

  // page proportions and difference boxes (positions come as data: the CSP forbids styles)
  document.querySelectorAll(".compare-pages .vpage[data-ar]").forEach((f) => { f.style.aspectRatio = f.dataset.ar; });
  document.querySelectorAll(".dbox[data-box]").forEach((b) => {
    const [x0, y0, x1, y1] = b.dataset.box.split(",").map(Number);
    b.style.left = `${x0 * 100}%`;
    b.style.top = `${y0 * 100}%`;
    b.style.width = `${(x1 - x0) * 100}%`;
    b.style.height = `${(y1 - y0) * 100}%`;
  });
  const marks = document.getElementById("cz-marks");
  if (marks) marks.addEventListener("change", () => document.body.classList.toggle("no-marks", !marks.checked));

  // shared zoom
  const STEPS = [0.5, 0.75, 1, 1.5, 2, 3];
  let zoom = 1;
  const label = document.getElementById("cz-label");
  function setZoom(z) {
    zoom = z;
    viewers.forEach((v) => { v.style.setProperty("--zoom", zoom); v.classList.toggle("zoomed", zoom > 1); });
    label.textContent = t("%(pct)s%", { pct: Math.round(zoom * 100) });
  }
  document.querySelectorAll("[data-czoom]").forEach((b) => b.addEventListener("click", () => {
    const d = b.dataset.czoom;
    if (d === "fit") return setZoom(1);
    const next = d === "in" ? STEPS.find((s) => s > zoom + 0.01) : [...STEPS].reverse().find((s) => s < zoom - 0.01);
    if (next) setZoom(next);
  }));

  // scroll both together (proportionally: the documents may differ in length)
  let syncing = false;
  viewers.forEach((v, i) => v.addEventListener("scroll", () => {
    if (syncing) return;
    syncing = true;
    const o = viewers[1 - i];
    const fy = v.scrollTop / Math.max(1, v.scrollHeight - v.clientHeight);
    const fx = v.scrollLeft / Math.max(1, v.scrollWidth - v.clientWidth);
    o.scrollTop = fy * (o.scrollHeight - o.clientHeight);
    o.scrollLeft = fx * (o.scrollWidth - o.clientWidth);
    requestAnimationFrame(() => { syncing = false; });
  }, { passive: true }));

  // keyboard: ← keep left, → keep right, B both, N skip
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
    if (zoom > 1 && e.key.startsWith("Arrow") && e.target.closest(".cviewer")) return; // panning
    const el = document.querySelector(`#decide [data-key="${e.key}"], #decide [data-key="${e.key.toLowerCase()}"]`);
    if (!el) return;
    e.preventDefault();
    el.click();
  });
})();
