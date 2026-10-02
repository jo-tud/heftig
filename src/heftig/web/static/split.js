"use strict";
/* Split or arrange pages: the pages as tiles in parts - each part becomes a new document.
   Cut between pages (✂), join parts again, move pages (drag with the mouse; on touch screens
   hold a page, then drag; keys: Alt + arrows), turn them, remove them, look at one large.
   The result goes to the server as one line, e.g. "1,2r90|4" (see ui.parse_layout). */
(function () {
  const board = document.getElementById("split-board");
  if (!board) return;
  const form = document.getElementById("split-form");
  const field = document.getElementById("split-layout");
  const summary = document.getElementById("split-summary");
  const apply = document.getElementById("split-apply");
  const reset = document.getElementById("split-reset");
  const blankBtn = document.getElementById("split-blank");
  const live = document.getElementById("split-live");
  const dropNew = board.querySelector(".split-new");
  const doc = board.dataset.doc;
  const blocked = form.dataset.problem === "1";
  const wrap = board.closest(".pv-media");
  // privacy mode: the first tap only reveals the pages (privacy.js) - no action, no large view
  const veiled = () => document.documentElement.classList.contains("privacy") && wrap && !wrap.classList.contains("pv-open");

  const tiles = () => [...board.querySelectorAll(".split-page")];
  const parts = () => [...board.querySelectorAll(".split-part")];
  const partOf = (li) => li.closest(".split-part");
  const listOf = (part) => part.querySelector(".split-pages");
  const pageNo = (li) => li.dataset.page;
  const isRemoved = (li) => li.classList.contains("removed");
  const original = tiles();
  const spin = new Map(original.map((li) => [li, 0])); // degrees shown, for smooth turning
  let initial = "";
  let submitting = false;

  function say(text) {
    live.textContent = "";
    setTimeout(() => { live.textContent = text; }, 30);
  }

  function newPart() {
    const first = board.querySelector(".split-part");
    const sec = first.cloneNode(false);
    const head = first.querySelector(".split-head").cloneNode(true);
    sec.append(head, document.createElement("ol"));
    sec.lastChild.className = "split-pages";
    return sec;
  }

  function layout() {
    return parts()
      .map((p) => [...p.querySelectorAll(".split-page")].filter((li) => !isRemoved(li))
        .map((li) => pageNo(li) + (+li.dataset.turn ? "r" + li.dataset.turn : "")).join(","))
      .filter(Boolean).join("|");
  }

  function paintTile(li) {
    const n = pageNo(li);
    const gone = isRemoved(li);
    li.querySelector(".sp-thumb img").style.setProperty("--turn", `${spin.get(li)}deg`);
    const rm = li.querySelector("[data-act=remove]");
    rm.textContent = gone ? "↩" : "✕";
    rm.title = gone ? t("Put page %(num)s back", { num: n }) : t("Remove page %(num)s", { num: n });
    rm.setAttribute("aria-label", rm.title);
    li.querySelector("[data-act=turn]").disabled = gone;
    const cut = li.querySelector(".sp-cut");
    cut.title = t("New document after page %(num)s", { num: n });
    cut.setAttribute("aria-label", cut.title);
  }

  // after every change: parts renumbered, empty ones gone, the summary and the form field
  function refresh() {
    parts().forEach((p) => { if (!p.querySelector(".split-page")) p.remove(); });
    let docNo = 0;
    parts().forEach((p, i) => {
      const all = [...p.querySelectorAll(".split-page")];
      const kept = all.filter((li) => !isRemoved(li)).length;
      if (kept) docNo += 1;
      p.classList.toggle("empty", !kept);
      p.querySelector("h2").textContent = kept ? t("Document %(num)s", { num: docNo }) : t("Not created");
      p.querySelector(".sp-count").textContent = kept
        ? tn("%(num)d page", "%(num)d pages", kept) : t("all pages removed");
      const join = p.querySelector("[data-act=join]");
      join.hidden = i === 0;
      join.textContent = t("Join with the part above");
      all.forEach((li, k) => {
        li.querySelector(".sp-cut").hidden = k === all.length - 1;
        const where = kept && !isRemoved(li) ? t("document %(num)s", { num: docNo }) : t("removed");
        li.setAttribute("aria-label", `${t("Page %(num)s", { num: pageNo(li) })}, ${where}`);
      });
    });
    tiles().forEach(paintTile);
    const value = layout();
    field.value = value;
    const docs = value ? value.split("|").length : 0;
    const removed = tiles().filter(isRemoved).length;
    const changed = value !== initial;
    reset.disabled = !changed && !removed;
    apply.disabled = blocked || submitting || !changed || !docs;
    apply.textContent = docs > 1 ? t("Split into %(num)s documents", { num: docs }) : t("Save as new document");
    if (!docs) summary.textContent = t("Keep at least one page.");
    else if (!changed) summary.textContent = t("No changes yet.");
    else {
      summary.textContent = [
        tn("%(num)d document", "%(num)d documents", docs),
        removed ? tn("%(num)d page removed", "%(num)d pages removed", removed) : "",
      ].filter(Boolean).join(" · ");
    }
    if (blankBtn) blankBtn.disabled = !tiles().some((li) => li.classList.contains("is-blank") && !isRemoved(li));
    if (dialog.open) paintView();
  }

  // --- the actions ---------------------------------------------------------------------------

  function cutAfter(li) {
    const rest = [];
    for (let x = li.nextElementSibling; x; x = x.nextElementSibling) rest.push(x);
    if (!rest.length) return;
    const p = newPart();
    partOf(li).after(p);
    listOf(p).append(...rest);
    refresh();
    say(t("New document starts with page %(num)s.", { num: pageNo(rest[0]) }));
  }

  function joinUp(part) {
    const prev = part.previousElementSibling;
    if (!prev || !prev.classList.contains("split-part")) return;
    listOf(prev).append(...part.querySelectorAll(".split-page"));
    part.remove();
    refresh();
    say(t("Parts joined."));
  }

  function turn(li) {
    if (isRemoved(li)) return;
    li.dataset.turn = String((+li.dataset.turn + 90) % 360);
    spin.set(li, spin.get(li) + 90);
    refresh();
  }

  function toggleRemove(li) {
    li.classList.toggle("removed");
    refresh();
    say(isRemoved(li) ? t("Page %(num)s removed.", { num: pageNo(li) }) : t("Page %(num)s put back.", { num: pageNo(li) }));
  }

  // one step towards the start (-1) or the end (+1), across parts
  function step(li, dir) {
    const sib = dir < 0 ? li.previousElementSibling : li.nextElementSibling;
    if (sib) {
      if (dir < 0) sib.before(li); else sib.after(li);
    } else {
      const other = dir < 0 ? partOf(li).previousElementSibling : partOf(li).nextElementSibling;
      if (!other || !other.classList.contains("split-part")) return;
      if (dir < 0) listOf(other).append(li); else listOf(other).prepend(li);
    }
    li.focus();
    refresh();
    const pos = [...li.parentElement.children].indexOf(li) + 1;
    say(t("Page %(num)s: position %(pos)s", { num: pageNo(li), pos }));
  }

  function startOver() {
    const first = board.querySelector(".split-part");
    listOf(first).append(...original);
    original.forEach((li) => {
      li.classList.remove("removed");
      li.dataset.turn = li.dataset.turn0;
      spin.set(li, 0);
    });
    refresh();
    say(t("Back to the original order."));
  }

  board.addEventListener("click", (e) => {
    if (performance.now() < dragEnd + 100) { e.preventDefault(); return; } // the click ending a drag
    if (e.defaultPrevented || veiled()) return; // this tap revealed the pages (privacy.js)
    const btn = e.target.closest("button[data-act]");
    const li = e.target.closest(".split-page");
    if (btn) {
      const act = btn.dataset.act;
      if (act === "join") joinUp(btn.closest(".split-part"));
      else if (act === "cut") cutAfter(li);
      else if (act === "turn") turn(li);
      else if (act === "remove") toggleRemove(li);
    } else if (li) {
      openView(li);
    }
  });

  board.addEventListener("keydown", (e) => {
    const li = e.target.closest(".split-page");
    if (!li || e.target !== li || e.ctrlKey || e.metaKey || veiled()) return;
    const k = e.key;
    const dir = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[k];
    if (dir && e.altKey) step(li, dir);
    else if (dir) {
      const all = tiles();
      const next = all[all.indexOf(li) + dir];
      if (next) next.focus();
    } else if (k === "r" || k === "R") turn(li);
    else if (k === "Delete" || k === "Backspace") toggleRemove(li);
    else if (k === "s" || k === "S") cutAfter(li);
    else if (k === "Enter" || k === " ") openView(li);
    else return;
    e.preventDefault();
  });

  reset.addEventListener("click", startOver);
  if (blankBtn) blankBtn.addEventListener("click", () => {
    tiles().forEach((li) => { if (li.classList.contains("is-blank")) li.classList.add("removed"); });
    refresh();
    say(t("Blank pages removed."));
  });

  form.addEventListener("submit", (e) => {
    if (apply.disabled) { e.preventDefault(); return; }
    submitting = true;
    field.value = layout();
    setTimeout(() => { apply.disabled = true; apply.textContent = t("Saving …"); }, 0);
  });
  window.addEventListener("beforeunload", (e) => {
    if (!submitting && layout() !== initial) e.preventDefault();
  });

  // --- dragging (pointer events: mouse, pen and touch) ---------------------------------------
  // Mouse: drag after a few pixels. Touch: hold still briefly (a swipe keeps scrolling the page).

  const HOLD_MS = 350;
  let drag = null;
  let dragEnd = -1e9;

  board.addEventListener("pointerdown", (e) => {
    const li = e.target.closest(".split-page");
    if (!li || e.target.closest("button") || e.button !== 0 || blocked || veiled()) return;
    drag = { li, id: e.pointerId, x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY,
      touch: e.pointerType !== "mouse", active: false, timer: 0, ghost: null };
    if (drag.touch) drag.timer = setTimeout(startDrag, HOLD_MS);
  });

  function startDrag() {
    if (!drag || drag.active) return;
    const { li } = drag;
    const r = li.getBoundingClientRect();
    drag.active = true;
    drag.dx = drag.x - r.left;
    drag.dy = drag.y - r.top;
    const ghost = li.cloneNode(true);
    ghost.removeAttribute("id");
    ghost.className = "split-page split-ghost";
    ghost.style.width = `${r.width}px`;
    document.body.append(ghost);
    drag.ghost = ghost;
    li.classList.add("dragging");
    board.classList.add("is-dragging");
    if (navigator.vibrate) navigator.vibrate(15);
    moveGhost();
    scrollLoop();
  }

  function moveGhost() {
    drag.ghost.style.transform = `translate(${drag.x - drag.dx}px, ${drag.y - drag.dy}px) rotate(2deg)`;
  }

  // put the dragged tile where the pointer is: before or after the nearest tile of that part
  function place() {
    const el = document.elementFromPoint(drag.x, drag.y);
    drag.toNew = !!(el && el.closest(".split-new"));
    dropNew.classList.toggle("over", drag.toNew);
    if (!el || drag.toNew) return;
    const part = el.closest(".split-part");
    if (!part) return;
    let best = null;
    let dist = Infinity;
    for (const x of part.querySelectorAll(".split-page")) {
      if (x === drag.li) continue;
      const r = x.getBoundingClientRect();
      const d = Math.hypot(drag.x - (r.left + r.width / 2), drag.y - (r.top + r.height / 2));
      if (d < dist) { dist = d; best = x; }
    }
    if (!best) {
      if (drag.li.parentElement !== listOf(part)) listOf(part).append(drag.li);
      return;
    }
    const r = best.getBoundingClientRect();
    const sameRow = drag.y >= r.top && drag.y <= r.bottom;
    const before = sameRow ? drag.x < r.left + r.width / 2 : drag.y < r.top;
    if (before && best.previousElementSibling !== drag.li) best.before(drag.li);
    else if (!before && best.nextElementSibling !== drag.li) best.after(drag.li);
  }

  function scrollLoop() {
    if (!drag || !drag.active) return;
    const edge = 70;
    const bar = document.querySelector(".split-bar");
    const bottom = bar ? Math.min(window.innerHeight, bar.getBoundingClientRect().top) : window.innerHeight;
    let dy = 0;
    if (drag.y < edge) dy = -Math.ceil((edge - drag.y) / 6);
    else if (drag.y > bottom - edge) dy = Math.ceil((drag.y - (bottom - edge)) / 6);
    if (dy) { window.scrollBy(0, dy); place(); }
    requestAnimationFrame(scrollLoop);
  }

  window.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    drag.x = e.clientX;
    drag.y = e.clientY;
    if (!drag.active) {
      const moved = Math.hypot(drag.x - drag.x0, drag.y - drag.y0);
      if (drag.touch) { if (moved > 10) cancel(); return; } // a swipe: the page scrolls
      if (moved < 6) return;
      startDrag();
    }
    e.preventDefault();
    moveGhost();
    place();
  }, { passive: false });

  // while dragging on a touch screen the page must not scroll
  board.addEventListener("touchmove", (e) => { if (drag && drag.active) e.preventDefault(); }, { passive: false });
  board.addEventListener("contextmenu", (e) => { if (drag) e.preventDefault(); });

  function cancel() {
    if (!drag) return;
    clearTimeout(drag.timer);
    drag = null;
  }

  function finish() {
    if (!drag) return;
    clearTimeout(drag.timer);
    if (drag.active) {
      const { li } = drag;
      if (drag.toNew) {
        const p = newPart();
        dropNew.before(p);
        listOf(p).append(li);
      }
      drag.ghost.remove();
      li.classList.remove("dragging");
      board.classList.remove("is-dragging");
      dropNew.classList.remove("over");
      dragEnd = performance.now();
      refresh();
      say(t("Page %(num)s moved.", { num: pageNo(li) }));
    }
    drag = null;
  }
  window.addEventListener("pointerup", finish);
  window.addEventListener("pointercancel", () => { if (drag && drag.active) finish(); else cancel(); });

  // --- one page large -------------------------------------------------------------------------

  const dialog = document.getElementById("split-view");
  const viewImg = document.getElementById("sv-img");
  let current = null;

  function openView(li) {
    current = li;
    paintView();
    if (!dialog.open) dialog.showModal();
  }

  function paintView() {
    const li = current;
    const n = pageNo(li);
    const all = tiles();
    const i = all.indexOf(li);
    const part = partOf(li);
    const firstInPart = part.querySelector(".split-page") === li;
    const partIndex = parts().indexOf(part);
    const src = `/documents/${doc}/pages/${n}.webp?w=960${+li.dataset.turn0 ? "&r=" + li.dataset.turn0 : ""}`;
    if (viewImg.getAttribute("src") !== src) viewImg.src = src;
    viewImg.style.setProperty("--turn", `${spin.get(li)}deg`);
    const head = part.querySelector("h2").textContent;
    document.getElementById("sv-title").textContent = `${t("Page %(num)s", { num: n })} · ${isRemoved(li) ? t("removed") : head}`;
    dialog.querySelector("[data-sv=prev]").disabled = i === 0;
    dialog.querySelector("[data-sv=next]").disabled = i === all.length - 1;
    dialog.querySelector("[data-sv=turn]").disabled = isRemoved(li);
    dialog.querySelector("[data-sv=remove]").textContent = isRemoved(li) ? t("Put back") : t("Remove");
    const start = dialog.querySelector("[data-sv=start]");
    start.disabled = firstInPart && partIndex === 0;
    start.textContent = firstInPart && partIndex > 0 ? t("Join with the part above") : t("New document starts here");
  }

  function go(dir) {
    const all = tiles();
    const next = all[all.indexOf(current) + dir];
    if (next) { current = next; paintView(); }
  }

  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) { dialog.close(); return; } // the backdrop
    const b = e.target.closest("button[data-sv]");
    if (!b) return;
    const act = b.dataset.sv;
    if (act === "close") dialog.close();
    else if (act === "prev") go(-1);
    else if (act === "next") go(1);
    else if (act === "turn") turn(current);
    else if (act === "remove") toggleRemove(current);
    else if (act === "start") {
      const part = partOf(current);
      if (part.querySelector(".split-page") === current) joinUp(part);
      else cutAfter(current.previousElementSibling);
    }
  });
  dialog.addEventListener("keydown", (e) => {
    if (e.target.closest("button") && (e.key === "Enter" || e.key === " ")) return;
    if (e.key === "ArrowLeft") go(-1);
    else if (e.key === "ArrowRight") go(1);
    else if (e.key === "r" || e.key === "R") turn(current);
    else return;
    e.preventDefault();
  });
  dialog.addEventListener("close", () => { if (current) current.focus(); });

  refresh();
  initial = layout();
  refresh();
})();
