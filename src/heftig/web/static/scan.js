"use strict";
/*
 * Heftig document camera.
 *
 * Live mode (HTTPS/localhost): camera preview in the page, the paper outline is detected with
 * docdetect.js (OpenCV.js) and a photo is taken automatically once the sheet lies still.
 * Against jitter the outline goes through a "funnel" (after WeScan): it is only shown once several
 * of the recent detections agree, and the drawn outline glides smoothly to the new position.
 * Fallback (plain HTTP, where browsers block live camera access): the phone's camera app opens,
 * the photo is then cropped and perspective-corrected the same way.
 * All pages of one document are combined into a single PDF in the browser and uploaded.
 * The crop editor takes the place of the camera view; the page strip stays below it, so another
 * page can be opened directly (the edits so far are kept). The ⓧ on a page removes it, with undo.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const csrf = document.body.dataset.csrf || "";
  const OUT_LONG_SIDE = 2200; // output page long side in px (~190 dpi for A4)
  const JPEG_Q = 0.88;
  const FUNNEL_SIZE = 8; // recent detections considered
  const FUNNEL_MIN = 3; // ... of which this many must agree before an outline is shown
  const MATCH = 0.04; // corners "agree" within 4 % of the frame diagonal
  const STILL = 0.015; // outline counts as still while it moves less than 1.5 % per detection
  const STILL_MS = 1000; // auto-capture after this long without movement
  const NEW_SHEET = 0.12; // this much movement means a different sheet
  const UNDO_MS = 8000; // a removed page can be brought back this long
  const CORNERS = ["topLeftCorner", "topRightCorner", "bottomRightCorner", "bottomLeftCorner"];

  const state = {
    stream: null,
    pages: [], // {blob, w, h, url, src: Blob, corners, rotation}
    auto: true,
    armed: true,
    history: [], // recent detections (corners or null), newest last
    target: null, // agreed outline in video pixels
    shown: null, // outline as currently drawn (glides towards target)
    stillSince: 0,
    lostSince: 0,
    busy: false,
    editing: null, // {page, canvas, corners, rotation, scale}
    removed: null, // {page, index, timer}: the last removed page, while it can be brought back
  };

  // ------------------------------------------------------------------ OpenCV loading
  function loadOpenCV() {
    return new Promise((resolve, reject) => {
      if (window.cv && window.cv.Mat) return resolve();
      const s = document.createElement("script");
      s.src = document.body.dataset.opencv;
      s.async = true;
      s.onerror = () => reject(new Error("OpenCV could not be loaded"));
      s.onload = async () => {
        try {
          if (window.cv instanceof Promise) window.cv = await window.cv;
          // the WebAssembly runtime may finish before or after onload, so don't rely on
          // onRuntimeInitialized alone (it never fires if initialisation is already done)
          const started = Date.now();
          while (!(window.cv && window.cv.Mat)) {
            if (Date.now() - started > 90000) throw new Error("OpenCV initialization is taking too long");
            await new Promise((r) => setTimeout(r, 50));
          }
          resolve();
        } catch (e) { reject(e); }
      };
      document.head.appendChild(s);
    });
  }

  // ------------------------------------------------------------------ geometry helpers
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

  const maxMove = (a, b) => Math.max(...CORNERS.map((k) => dist(a[k], b[k])));

  function average(list) {
    const o = {};
    for (const k of CORNERS) {
      o[k] = {
        x: list.reduce((s, c) => s + c[k].x, 0) / list.length,
        y: list.reduce((s, c) => s + c[k].y, 0) / list.length,
      };
    }
    return o;
  }

  function fullCorners(w, h) {
    return {
      topLeftCorner: { x: 0, y: 0 }, topRightCorner: { x: w, y: 0 },
      bottomLeftCorner: { x: 0, y: h }, bottomRightCorner: { x: w, y: h },
    };
  }

  /**
   * Detect paper corners on a canvas or video frame. Returns corners in source pixels, or null
   * when nothing trustworthy was found. `prev` (corners) prefers outlines close to it.
   */
  function detect(source, opts) {
    opts = opts || {};
    let r = null;
    try {
      const prev = opts.prev ? CORNERS.map((k) => opts.prev[k]) : null;
      r = window.HeftigDetect.detect(source, { short: opts.short, prev });
    } catch (e) {
      return null;
    }
    if (!r || r.score < window.HeftigDetect.MIN_SCORE) return null;
    const o = {};
    CORNERS.forEach((k, i) => { o[k] = r.corners[i]; });
    return o;
  }

  /** Perspective-correct `source` to a new canvas. */
  function warp(source, c, rotation) {
    // size from the longest sides, proportions corrected for the perspective (a tilted shot would
    // otherwise come out too wide or too tall)
    const ratio = window.HeftigDetect.aspectRatio(CORNERS.map((k) => c[k]), source.width, source.height);
    const long = Math.max(dist(c.topLeftCorner, c.topRightCorner), dist(c.bottomLeftCorner, c.bottomRightCorner),
      dist(c.topLeftCorner, c.bottomLeftCorner), dist(c.topRightCorner, c.bottomRightCorner));
    const w0 = ratio >= 1 ? long : long * ratio;
    const h0 = ratio >= 1 ? long / ratio : long;
    const s = Math.min(OUT_LONG_SIDE / Math.max(w0, h0), 1.3);
    const w = Math.max(1, Math.round(w0 * s));
    const h = Math.max(1, Math.round(h0 * s));
    const src = cv.imread(source);
    const dst = new cv.Mat();
    const from = cv.matFromArray(4, 1, cv.CV_32FC2, [
      c.topLeftCorner.x, c.topLeftCorner.y, c.topRightCorner.x, c.topRightCorner.y,
      c.bottomLeftCorner.x, c.bottomLeftCorner.y, c.bottomRightCorner.x, c.bottomRightCorner.y,
    ]);
    const to = cv.matFromArray(4, 1, cv.CV_32FC2, [0, 0, w, 0, 0, h, w, h]);
    const M = cv.getPerspectiveTransform(from, to);
    cv.warpPerspective(src, dst, M, new cv.Size(w, h), cv.INTER_LINEAR, cv.BORDER_REPLICATE, new cv.Scalar());
    const out = document.createElement("canvas");
    cv.imshow(out, dst);
    [src, dst, from, to, M].forEach((m) => m.delete());
    return rotate(out, rotation || 0);
  }

  function rotate(canvas, deg) {
    deg = ((deg % 360) + 360) % 360;
    if (!deg) return canvas;
    const out = document.createElement("canvas");
    const swap = deg === 90 || deg === 270;
    out.width = swap ? canvas.height : canvas.width;
    out.height = swap ? canvas.width : canvas.height;
    const ctx = out.getContext("2d");
    ctx.translate(out.width / 2, out.height / 2);
    ctx.rotate((deg * Math.PI) / 180);
    ctx.drawImage(canvas, -canvas.width / 2, -canvas.height / 2);
    return out;
  }

  const toBlob = (canvas, q) => new Promise((r) => canvas.toBlob(r, "image/jpeg", q || JPEG_Q));

  async function blobToCanvas(blob) {
    let bmp;
    try {
      bmp = await createImageBitmap(blob, { imageOrientation: "from-image" });
    } catch (e) {
      bmp = await new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = reject;
        img.src = URL.createObjectURL(blob);
      });
    }
    const f = Math.min(1, 4000 / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * f);
    c.height = Math.round(bmp.height * f);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    return c;
  }

  // ------------------------------------------------------------------ pages
  async function addPage(sourceCanvas, corners, rotation) {
    rotation = rotation || 0;
    const out = warp(sourceCanvas, corners, rotation);
    const blob = await toBlob(out);
    const src = await toBlob(sourceCanvas, 0.85);
    state.pages.push({ blob, w: out.width, h: out.height, url: URL.createObjectURL(blob), src, corners, rotation });
    renderPages();
    const list = $("pages");
    list.scrollLeft = list.scrollWidth; // the newest page stays in view
    flash();
  }

  async function updatePage(page) {
    const source = await blobToCanvas(page.src);
    const out = warp(source, page.corners, page.rotation);
    URL.revokeObjectURL(page.url);
    page.blob = await toBlob(out);
    page.w = out.width;
    page.h = out.height;
    page.url = URL.createObjectURL(page.blob);
    renderPages();
  }

  function renderPages() {
    const list = $("pages");
    list.innerHTML = "";
    state.pages.forEach((p, i) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "page-thumb";
      b.setAttribute("aria-label", t("Edit page %(num)s", { num: i + 1 }));
      if (state.editing && state.editing.page === p) {
        b.classList.add("current");
        b.setAttribute("aria-current", "true");
      }
      const img = document.createElement("img");
      img.src = p.url;
      img.alt = "";
      const n = document.createElement("span");
      n.textContent = i + 1;
      b.append(img, n);
      b.addEventListener("click", () => openPage(p));
      const x = document.createElement("button");
      x.type = "button";
      x.className = "page-remove";
      x.textContent = "×";
      x.setAttribute("aria-label", t("Remove page %(num)s", { num: i + 1 }));
      x.addEventListener("click", () => removePage(p));
      li.append(b, x);
      list.appendChild(li);
    });
    $("count").textContent = tn("%(num)d page", "%(num)d pages", state.pages.length);
    $("finish").disabled = state.pages.length === 0;
  }

  // ------------------------------------------------------------------ removing pages (with undo)
  /** The last removed page can no longer be brought back. */
  function forgetRemoved() {
    const r = state.removed;
    if (!r) return;
    clearTimeout(r.timer);
    URL.revokeObjectURL(r.page.url);
    state.removed = null;
    $("undo").hidden = true;
  }

  /** Remove a page; it can be brought back for a few seconds. The sheet is not captured again. */
  function removePage(page) {
    const i = state.pages.indexOf(page);
    if (i < 0) return;
    if (state.editing && state.editing.page === page) closeEditor();
    forgetRemoved();
    state.pages.splice(i, 1);
    state.removed = { page, index: i, timer: setTimeout(forgetRemoved, UNDO_MS) };
    renderPages();
    setHint(t("Page %(num)s removed.", { num: i + 1 }));
    $("undo").hidden = false;
  }

  function undoRemove() {
    const r = state.removed;
    if (!r) return;
    clearTimeout(r.timer);
    state.removed = null;
    $("undo").hidden = true;
    state.pages.splice(Math.min(r.index, state.pages.length), 0, r.page);
    renderPages();
    setHint(t("Page %(num)s is back.", { num: state.pages.indexOf(r.page) + 1 }));
  }

  function flash() {
    const el = $("flash");
    el.classList.remove("on");
    void el.offsetWidth;
    el.classList.add("on");
    if (navigator.vibrate) navigator.vibrate(60);
  }

  // ------------------------------------------------------------------ live camera
  async function startLive() {
    const video = $("video");
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: "environment" }, width: { ideal: 3840 }, height: { ideal: 2160 } },
    });
    video.srcObject = state.stream;
    await video.play();
    document.body.classList.add("live");
    setHint(HINT_START);
    let lastTick = 0;
    const loop = (t) => {
      if (!state.stream) return;
      // detection at most every 100 ms (it takes ~30-150 ms), drawing at display rate
      if (t - lastTick > 100 && !state.busy && !state.editing) {
        lastTick = t;
        tick(t);
      }
      drawOverlay();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  const HINT_START = t("Put the sheet on a steady, preferably dark surface and photograph it from above.");
  const HINT_LOST = t("No sheet detected – a darker surface helps. Tapping the shutter button captures the whole image.");

  function stopLive() {
    if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
    state.stream = null;
  }

  function frameCanvas() {
    const video = $("video");
    const c = document.createElement("canvas");
    c.width = video.videoWidth;
    c.height = video.videoHeight;
    c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
    return c;
  }

  /** WeScan-style funnel: the outline most of the recent detections agree on, or null. */
  function funnel(diag) {
    const found = state.history.filter(Boolean);
    let best = null;
    for (let i = found.length - 1; i >= 0; i--) {
      const group = found.filter((c) => maxMove(c, found[i]) < MATCH * diag);
      if (!best || group.length > best.length) best = group;
    }
    return best && best.length >= FUNNEL_MIN ? average(best) : null;
  }

  function tick(now) {
    const video = $("video");
    if (!video.videoWidth) return;
    const diag = Math.hypot(video.videoWidth, video.videoHeight);
    state.history.push(detect(video, { prev: state.target }));
    if (state.history.length > FUNNEL_SIZE) state.history.shift();
    const target = funnel(diag);
    const move = target && state.target ? maxMove(target, state.target) / diag : Infinity;
    if (!target) {
      state.armed = true; // sheet removed -> ready for the next one
      state.stillSince = 0;
      if (!state.lostSince) state.lostSince = now;
      if (now - state.lostSince > 2500 && !state.pages.length) setHint(HINT_LOST);
    } else {
      if (state.lostSince && !state.pages.length) setHint(HINT_START);
      state.lostSince = 0;
      if (move > NEW_SHEET) state.armed = true; // a different sheet
      if (move > STILL || !state.stillSince) state.stillSince = now;
    }
    state.target = target;
    const still = target && state.stillSince ? (now - state.stillSince) / STILL_MS : 0;
    setProgress(state.auto && state.armed ? Math.min(1, still) : 0);
    if (state.auto && state.armed && still >= 1) capture(target);
  }

  async function capture(corners) {
    state.busy = true;
    state.armed = false;
    state.stillSince = 0;
    try {
      const full = frameCanvas();
      if (corners) {
        // refine on a sharper frame; keep the live outline if the refinement disagrees
        const fine = detect(full, { short: 512, prev: corners });
        const diag = Math.hypot(full.width, full.height);
        if (fine && maxMove(fine, corners) < 0.03 * diag) corners = fine;
      } else {
        corners = detect(full, { short: 512 });
      }
      if (!corners) corners = fullCorners(full.width, full.height);
      await addPage(full, corners);
      setHint(t("Page captured. Put down the next sheet or tap “Done”."));
    } finally {
      state.busy = false;
    }
  }

  function drawOverlay() {
    const video = $("video");
    const canvas = $("overlay");
    const rect = video.getBoundingClientRect();
    const cw = Math.round(rect.width * devicePixelRatio);
    const ch = Math.round(rect.height * devicePixelRatio);
    if (canvas.width !== cw || canvas.height !== ch) {
      canvas.width = cw;
      canvas.height = ch;
    }
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // glide towards the agreed outline instead of jumping
    const t = state.target;
    if (!t) {
      state.shown = null;
      return;
    }
    if (!state.shown) state.shown = JSON.parse(JSON.stringify(t));
    for (const k of CORNERS) {
      state.shown[k].x += (t[k].x - state.shown[k].x) * 0.35;
      state.shown[k].y += (t[k].y - state.shown[k].y) * 0.35;
    }
    const w = video.videoWidth;
    const h = video.videoHeight;
    // video uses object-fit: contain -> compute the drawn area
    const s = Math.min(canvas.width / w, canvas.height / h);
    const ox = (canvas.width - w * s) / 2;
    const oy = (canvas.height - h * s) / 2;
    const P = (p) => [ox + p.x * s, oy + p.y * s];
    const c = state.shown;
    ctx.lineWidth = 4 * devicePixelRatio;
    ctx.lineJoin = "round";
    ctx.strokeStyle = state.armed ? "#ffd400" : "#39d98a";
    ctx.fillStyle = state.armed ? "rgba(255,212,0,.15)" : "rgba(57,217,138,.15)";
    ctx.beginPath();
    CORNERS.forEach((k, i) => (i ? ctx.lineTo(...P(c[k])) : ctx.moveTo(...P(c[k]))));
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }

  function setProgress(f) {
    $("shutter").style.setProperty("--p", f);
  }

  function setHint(t) {
    $("hint").textContent = t;
  }

  // ------------------------------------------------------------------ fallback: photo from camera app
  async function fromFile(file) {
    if (!file) return;
    setHint(t("Analyzing the photo …"));
    const canvas = await blobToCanvas(file);
    const corners = detect(canvas, { short: 512 }) || fullCorners(canvas.width, canvas.height);
    const src = await toBlob(canvas, 0.85);
    // straight into the editor so the user can confirm the outline
    const page = { src, corners, rotation: 0, isNew: true };
    if (state.editing) await commitEditor(state.editing);
    openEditor(page, canvas);
  }

  // ------------------------------------------------------------------ corner editor
  /** A page in the strip was tapped: open it in the editor, keeping the edits of the page open so far. */
  async function openPage(page) {
    const e = state.editing;
    if (e && e.page === page) return;
    if (e) await commitEditor(e);
    if (state.pages.includes(page)) openEditor(page);
  }

  async function openEditor(page, preloaded) {
    const canvas = preloaded || (await blobToCanvas(page.src));
    state.editing = { page, canvas, corners: JSON.parse(JSON.stringify(page.corners)), rotation: page.rotation || 0 };
    $("edit-delete").hidden = !!page.isNew;
    setRotateLabel();
    // in place of the camera view, above the page strip; both must be in their final place
    // before the stage is measured
    document.body.classList.add("editing");
    $("editor").hidden = false;
    renderPages();
    setHint(t("Check the corners and move them if needed."));
    const view = $("edit-canvas");
    const box = $("edit-stage").getBoundingClientRect();
    const pad = 32;
    const s = Math.min((box.width - pad) / canvas.width, (box.height - pad) / canvas.height);
    view.width = Math.max(1, Math.round(canvas.width * s));
    view.height = Math.max(1, Math.round(canvas.height * s));
    state.editing.scale = s;
    drawEditor();
  }

  function drawEditor() {
    const e = state.editing;
    const view = $("edit-canvas");
    const ctx = view.getContext("2d");
    ctx.drawImage(e.canvas, 0, 0, view.width, view.height);
    const c = e.corners;
    const P = (p) => [p.x * e.scale, p.y * e.scale];
    ctx.lineWidth = 3;
    ctx.strokeStyle = "#39d98a";
    ctx.beginPath();
    ctx.moveTo(...P(c.topLeftCorner));
    ctx.lineTo(...P(c.topRightCorner));
    ctx.lineTo(...P(c.bottomRightCorner));
    ctx.lineTo(...P(c.bottomLeftCorner));
    ctx.closePath();
    ctx.stroke();
    for (const k of Object.keys(c)) {
      const [x, y] = P(c[k]);
      ctx.beginPath();
      ctx.arc(x, y, 14, 0, 2 * Math.PI);
      ctx.fillStyle = "rgba(57,217,138,.35)";
      ctx.fill();
      ctx.stroke();
    }
  }

  function setupEditorDrag() {
    const view = $("edit-canvas");
    let dragging = null;
    const pos = (ev) => {
      const r = view.getBoundingClientRect();
      const e = state.editing;
      return {
        x: Math.max(0, Math.min(e.canvas.width, (ev.clientX - r.left) / e.scale)),
        y: Math.max(0, Math.min(e.canvas.height, (ev.clientY - r.top) / e.scale)),
      };
    };
    view.addEventListener("pointerdown", (ev) => {
      const e = state.editing;
      if (!e) return;
      const p = pos(ev);
      let best = null;
      let bestD = 40 / e.scale;
      for (const k of Object.keys(e.corners)) {
        const d = dist(p, e.corners[k]);
        if (d < bestD) { best = k; bestD = d; }
      }
      if (best) {
        dragging = best;
        view.setPointerCapture(ev.pointerId);
      }
    });
    view.addEventListener("pointermove", (ev) => {
      if (!dragging) return;
      state.editing.corners[dragging] = pos(ev);
      drawEditor();
    });
    const end = () => { dragging = null; };
    view.addEventListener("pointerup", end);
    view.addEventListener("pointercancel", end);
  }

  function setRotateLabel() {
    const deg = state.editing.rotation;
    $("edit-rotate").textContent = deg ? t("Rotate (%(deg)s°)", { deg }) : t("Rotate");
  }

  function closeEditor() {
    $("editor").hidden = true;
    document.body.classList.remove("editing");
    state.editing = null;
    renderPages();
  }

  /** Take over the corners and rotation of editor state `e` (a new photo becomes a page). */
  async function commitEditor(e) {
    const changed = JSON.stringify(e.corners) !== JSON.stringify(e.page.corners) || e.rotation !== (e.page.rotation || 0);
    e.page.corners = e.corners;
    e.page.rotation = e.rotation;
    if (e.page.isNew) {
      delete e.page.isNew;
      await addPage(e.canvas, e.corners, e.rotation);
      setHint(t("Page added. Photograph another page or tap “Done”."));
    } else if (changed && state.pages.includes(e.page)) {
      await updatePage(e.page);
    }
  }

  async function applyEditor() {
    const e = state.editing;
    closeEditor();
    await commitEditor(e);
  }

  // ------------------------------------------------------------------ PDF + upload
  function buildPdf(pages) {
    // minimal PDF 1.4 with one JPEG (DCTDecode) per page; A4 width, height by aspect ratio
    const enc = new TextEncoder();
    const chunks = [];
    const offsets = [];
    let size = 0;
    const push = (x) => {
      const b = typeof x === "string" ? enc.encode(x) : x;
      chunks.push(b);
      size += b.length;
    };
    push("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n");
    const n = pages.length;
    const obj = (id, body) => {
      offsets[id] = size;
      push(`${id} 0 obj\n`);
      body();
      push("\nendobj\n");
    };
    // 1 catalog, 2 pages, then per page: page, content, image
    const kids = pages.map((_, i) => `${3 + i * 3} 0 R`).join(" ");
    obj(1, () => push("<< /Type /Catalog /Pages 2 0 R >>"));
    obj(2, () => push(`<< /Type /Pages /Kids [${kids}] /Count ${n} >>`));
    pages.forEach((p, i) => {
      const pid = 3 + i * 3;
      const W = 595.28;
      const H = Math.round((W * p.h) / p.w * 100) / 100;
      const content = `q ${W} 0 0 ${H} 0 0 cm /Im0 Do Q`;
      obj(pid, () => push(
        `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${W} ${H}] ` +
        `/Resources << /XObject << /Im0 ${pid + 2} 0 R >> >> /Contents ${pid + 1} 0 R >>`,
      ));
      obj(pid + 1, () => push(`<< /Length ${content.length} >>\nstream\n${content}\nendstream`));
      obj(pid + 2, () => {
        push(`<< /Type /XObject /Subtype /Image /Width ${p.w} /Height ${p.h} /ColorSpace /DeviceRGB ` +
          `/BitsPerComponent 8 /Filter /DCTDecode /Length ${p.bytes.length} >>\nstream\n`);
        push(p.bytes);
        push("\nendstream");
      });
    });
    const xref = size;
    const total = 3 + n * 3;
    let x = `xref\n0 ${total}\n0000000000 65535 f \n`;
    for (let id = 1; id < total; id++) x += `${String(offsets[id]).padStart(10, "0")} 00000 n \n`;
    push(x);
    push(`trailer\n<< /Size ${total} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
    return new Blob(chunks, { type: "application/pdf" });
  }

  async function finish() {
    if (!state.pages.length) return;
    $("finish").disabled = true;
    setHint(t("Creating and uploading the PDF …"));
    const pages = [];
    for (const p of state.pages) {
      pages.push({ w: p.w, h: p.h, bytes: new Uint8Array(await p.blob.arrayBuffer()) });
    }
    const pdf = buildPdf(pages);
    const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
    const fd = new FormData();
    fd.append("files", pdf, `handy_${stamp}.pdf`);
    fd.append("kind", $("kind-paper").checked ? "paper" : "digital");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/documents");
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) setHint(t("Uploading … %(percent)s %", { percent: Math.round((ev.loaded / ev.total) * 100) }));
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
      const r = data && data.results && data.results[0];
      if (r && (r.status === "created" || r.status === "duplicate")) {
        state.pages.forEach((p) => URL.revokeObjectURL(p.url));
        state.pages = [];
        forgetRemoved(); // must not come back into the next document
        renderPages();
        const a = $("result-link");
        a.href = "/documents/" + r.document_id;
        $("result-text").textContent = r.status === "created"
          ? t("Document archived – text recognition is running in the background.")
          : t("This file is already archived.");
        $("result").hidden = false;
        setHint(t("Ready for the next document."));
      } else {
        $("finish").disabled = false;
        setHint(t("Upload failed: %(message)s", { message: (r && r.message) || (data && data.error && data.error.message) || xhr.status }));
      }
    };
    xhr.onerror = () => {
      $("finish").disabled = false;
      setHint(t("Network error while uploading – please tap again."));
    };
    xhr.send(fd);
  }

  // ------------------------------------------------------------------ wiring
  async function init() {
    setupEditorDrag();
    $("edit-cancel").addEventListener("click", closeEditor);
    $("edit-apply").addEventListener("click", applyEditor);
    $("edit-full").addEventListener("click", () => {
      const e = state.editing;
      e.corners = fullCorners(e.canvas.width, e.canvas.height);
      drawEditor();
    });
    $("edit-rotate").addEventListener("click", () => {
      state.editing.rotation = (state.editing.rotation + 90) % 360;
      setRotateLabel();
    });
    $("edit-delete").addEventListener("click", () => removePage(state.editing.page));
    $("undo").addEventListener("click", undoRemove);
    $("finish").addEventListener("click", finish);
    $("result-close").addEventListener("click", () => { $("result").hidden = true; });
    $("file").addEventListener("change", (ev) => {
      fromFile(ev.target.files[0]);
      ev.target.value = "";
    });
    $("auto").addEventListener("change", (ev) => {
      state.auto = ev.target.checked;
      state.stillSince = 0;
    });
    $("shutter").addEventListener("click", () => {
      if (state.stream) {
        if (!state.busy) capture(state.target);
      } else {
        $("file").click();
      }
    });
    renderPages();

    setHint(t("Loading the scanner …"));
    try {
      await loadOpenCV();
    } catch (e) {
      setHint(t("The image processing could not be loaded. You can still send photos via “Upload”."));
      return;
    }
    const liveOk = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
    if (liveOk) {
      try {
        await startLive();
        return;
      } catch (e) {
        const why = {
          NotAllowedError: t("access not allowed – allow it in the browser settings"),
          NotFoundError: t("no camera found"),
          NotReadableError: t("the camera is being used by another app"),
          OverconstrainedError: t("the camera is not suitable"),
        }[e && e.name] || t("error");
        setHint(t("Camera not available (%(reason)s) – take the photo with the camera app.", { reason: why }));
      }
    } else {
      $("insecure").hidden = false;
      setHint(t("Tap the shutter button: the camera app opens, then the sheet is cropped automatically."));
    }
    document.body.classList.add("photo");
  }

  window.addEventListener("pagehide", stopLive);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopLive();
    else if (document.body.classList.contains("live") && !state.stream) startLive().catch(() => {});
  });
  init();

  // exposed for testing in the browser console
  window.heftigScan = { detect, warp, buildPdf, state, fromFile };
})();
