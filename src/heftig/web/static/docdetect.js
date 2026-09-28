"use strict";
/*
 * Document outline detection for the Heftig camera (needs OpenCV.js).
 *
 * Line-based approach after Dropbox ("Fast and Accurate Document Detection for Scanning") and
 * Tropin et al. 2021 (arXiv:2106.09987) instead of "largest contour":
 *  1. downscale (short side 256), edge strength from several colour channels (Lab L/a/b, HSV S)
 *     after a small open/close that removes text strokes
 *  2. straight lines with the Hough transform, split into near-horizontal / near-vertical,
 *     non-maximum suppression; the image borders are added as candidate sides so sheets that
 *     fill or leave the frame are still found
 *  3. every combination of 2 horizontal + 2 vertical lines forms a candidate quadrilateral;
 *     implausible ones (tiny, concave, skewed angles) are dropped
 *  4. score = edge support along the sides minus edges just outside, a bonus for area, and for
 *     the best few the colour contrast between inside and outside; optional bonus for being close
 *     to the previously shown outline (stability)
 */
(function (global) {
  const H_TOL = Math.PI / 4;

  function edgeMap(src) {
    const rgb = new cv.Mat();
    cv.cvtColor(src, rgb, cv.COLOR_RGBA2RGB);
    const lab = new cv.Mat();
    const hsv = new cv.Mat();
    cv.cvtColor(rgb, lab, cv.COLOR_RGB2Lab);
    cv.cvtColor(rgb, hsv, cv.COLOR_RGB2HSV);
    const labCh = new cv.MatVector();
    const hsvCh = new cv.MatVector();
    cv.split(lab, labCh);
    cv.split(hsv, hsvCh);
    const chans = [labCh.get(0), labCh.get(1), labCh.get(2), hsvCh.get(1)];
    const w = src.cols;
    const h = src.rows;
    const acc = new Float32Array(w * h);
    const k = cv.getStructuringElement(cv.MORPH_RECT, new cv.Size(3, 3));
    const tmp = new cv.Mat();
    const gx = new cv.Mat();
    const gy = new cv.Mat();
    const mag = new cv.Mat();
    for (const c of chans) {
      cv.morphologyEx(c, tmp, cv.MORPH_OPEN, k);
      cv.morphologyEx(tmp, tmp, cv.MORPH_CLOSE, k);
      cv.GaussianBlur(tmp, tmp, new cv.Size(5, 5), 0);
      cv.Sobel(tmp, gx, cv.CV_32F, 1, 0, 3);
      cv.Sobel(tmp, gy, cv.CV_32F, 0, 1, 3);
      cv.magnitude(gx, gy, mag);
      const d = mag.data32F;
      // never below a fixed floor: colourless channels would otherwise turn noise into "edges"
      const p99 = Math.max(percentile(d, 0.99), 120);
      for (let i = 0; i < d.length; i++) {
        const v = d[i] / p99;
        if (v > acc[i]) acc[i] = v > 1 ? 1 : v;
      }
      c.delete();
    }
    const labData = new Uint8Array(lab.data); // for the contrast score
    [rgb, lab, hsv, labCh, hsvCh, k, tmp, gx, gy, mag].forEach((m) => m.delete());
    return { acc, w, h, labData };
  }

  function percentile(arr, q) {
    const step = Math.max(1, Math.floor(arr.length / 12000));
    const s = [];
    for (let i = 0; i < arr.length; i += step) s.push(arr[i]);
    s.sort((a, b) => a - b);
    return s[Math.min(s.length - 1, Math.floor(q * s.length))];
  }

  function houghLines(acc, w, h) {
    const thr = Math.max(0.25, percentile(acc, 0.9));
    const bin = new Uint8Array(w * h);
    for (let i = 0; i < acc.length; i++) bin[i] = acc[i] > thr ? 255 : 0;
    const m = cv.matFromArray(h, w, cv.CV_8UC1, bin);
    const lines = new cv.Mat();
    cv.HoughLines(m, lines, 1, Math.PI / 180, Math.round(Math.min(w, h) * 0.18), 0, 0, 0, Math.PI);
    const hor = [];
    const ver = [];
    const d = lines.data32F;
    for (let i = 0; i < lines.rows; i++) {
      const rho = d[2 * i];
      const th = d[2 * i + 1];
      const group = Math.abs(th - Math.PI / 2) < H_TOL ? hor : ver;
      if (group.length >= 10) continue;
      const close = group.some(
        (L) =>
          (Math.abs(rho - L.rho) < 10 && Math.abs(th - L.th) < 0.105) ||
          (Math.abs(rho + L.rho) < 10 && Math.abs(Math.abs(th - L.th) - Math.PI) < 0.105),
      );
      if (!close) group.push({ rho, th, kind: "line" });
    }
    m.delete();
    lines.delete();
    // image borders as fallback sides
    hor.push({ rho: 0, th: Math.PI / 2, kind: "border" }, { rho: h - 1, th: Math.PI / 2, kind: "border" });
    ver.push({ rho: 0, th: 0, kind: "border" }, { rho: w - 1, th: 0, kind: "border" });
    return { hor, ver };
  }

  function intersect(a, b) {
    const a1 = Math.cos(a.th), b1 = Math.sin(a.th);
    const a2 = Math.cos(b.th), b2 = Math.sin(b.th);
    const det = a1 * b2 - a2 * b1;
    if (Math.abs(det) < 1e-6) return null;
    return { x: (a.rho * b2 - b.rho * b1) / det, y: (a1 * b.rho - a2 * a.rho) / det };
  }

  function sideSupport(acc, w, h, p, q) {
    const n = 24;
    const dx = q.x - p.x, dy = q.y - p.y;
    const L = Math.hypot(dx, dy) + 1e-6;
    const nx = -dy / L, ny = dx / L;
    let on = 0, out = 0;
    const at = (x, y) => {
      const xi = Math.round(x), yi = Math.round(y);
      return xi < 0 || yi < 0 || xi >= w || yi >= h ? 0 : acc[yi * w + xi];
    };
    for (let i = 0; i < n; i++) {
      const t = 0.05 + (0.9 * i) / (n - 1);
      const x = p.x + dx * t, y = p.y + dy * t;
      on += Math.max(at(x, y), at(x + nx, y + ny), at(x - nx, y - ny));
      out += Math.min(at(x + 4 * nx, y + 4 * ny), at(x - 4 * nx, y - 4 * ny));
    }
    return (on - 0.5 * out) / n;
  }

  function orderCorners(pts) {
    const s = pts.map((p) => p.x + p.y);
    const d = pts.map((p) => p.y - p.x);
    const idx = (arr, fn) => arr.indexOf(fn(...arr));
    return [pts[idx(s, Math.min)], pts[idx(d, Math.min)], pts[idx(s, Math.max)], pts[idx(d, Math.max)]];
  }

  function area(q) {
    let a = 0;
    for (let i = 0; i < 4; i++) {
      const p = q[i], r = q[(i + 1) % 4];
      a += p.x * r.y - r.x * p.y;
    }
    return Math.abs(a) / 2;
  }

  function convexAndAngles(q) {
    let sign = 0;
    for (let i = 0; i < 4; i++) {
      const a = q[(i + 3) % 4], b = q[i], c = q[(i + 1) % 4];
      const v1x = a.x - b.x, v1y = a.y - b.y, v2x = c.x - b.x, v2y = c.y - b.y;
      const cross = v1x * v2y - v1y * v2x;
      const s = Math.sign(cross);
      if (!s || (sign && s !== sign)) return false;
      sign = s;
      const cos = (v1x * v2x + v1y * v2y) / (Math.hypot(v1x, v1y) * Math.hypot(v2x, v2y) + 1e-6);
      const ang = (Math.acos(Math.max(-1, Math.min(1, cos))) * 180) / Math.PI;
      if (ang < 55 || ang > 125) return false;
    }
    return true;
  }

  function contrast(labData, w, h, q) {
    const mask = cv.Mat.zeros(h, w, cv.CV_8UC1);
    const pts = cv.matFromArray(4, 1, cv.CV_32SC2, q.flatMap((p) => [Math.round(p.x), Math.round(p.y)]));
    cv.fillConvexPoly(mask, pts, new cv.Scalar(255));
    const ring = new cv.Mat();
    cv.dilate(mask, ring, cv.getStructuringElement(cv.MORPH_RECT, new cv.Size(15, 15)));
    cv.subtract(ring, mask, ring);
    const mk = mask.data, rg = ring.data;
    const si = [0, 0, 0], so = [0, 0, 0];
    let ni = 0, no = 0;
    for (let i = 0, j = 0; i < mk.length; i++, j += 3) {
      if (mk[i]) { si[0] += labData[j]; si[1] += labData[j + 1]; si[2] += labData[j + 2]; ni++; }
      else if (rg[i]) { so[0] += labData[j]; so[1] += labData[j + 1]; so[2] += labData[j + 2]; no++; }
    }
    [mask, pts, ring].forEach((m) => m.delete());
    if (!no || !ni) return 0.3;
    const dl = si[0] / ni - so[0] / no, da = si[1] / ni - so[1] / no, db = si[2] / ni - so[2] / no;
    return Math.min(1, Math.hypot(dl, da, db) / 60);
  }

  /**
   * Detect the document in a canvas/image.
   * @param source canvas, image or <video> (the current frame is used)
   * @param opts {short: detection resolution (short side px), prev: previous corners in source px}
   * @returns {corners: [tl,tr,br,bl] in source pixels, score} or null. Scores below ~0.85 are
   *   unreliable (typically white paper on a white background) and should not be shown.
   */
  function detect(source, opts) {
    opts = opts || {};
    const short = opts.short || 256;
    const sw = source.videoWidth || source.naturalWidth || source.width;
    const sh = source.videoHeight || source.naturalHeight || source.height;
    if (!sw || !sh) return null;
    const f = short / Math.min(sw, sh);
    const w = Math.round(sw * f), h = Math.round(sh * f);
    const small = document.createElement("canvas");
    small.width = w;
    small.height = h;
    small.getContext("2d").drawImage(source, 0, 0, w, h);
    const src = cv.imread(small);
    let em;
    try {
      em = edgeMap(src);
    } finally {
      src.delete();
    }
    const { acc, labData } = em;
    const { hor, ver } = houghLines(acc, w, h);
    const prev = opts.prev ? opts.prev.map((p) => ({ x: p.x * f, y: p.y * f })) : null;
    const cands = [];
    const minArea = 0.12 * w * h;
    for (let i = 0; i < hor.length; i++) {
      for (let j = i + 1; j < hor.length; j++) {
        for (let k = 0; k < ver.length; k++) {
          for (let l = k + 1; l < ver.length; l++) {
            const nBorder = [hor[i], hor[j], ver[k], ver[l]].filter((L) => L.kind === "border").length;
            if (nBorder > 2) continue;
            const p = [intersect(hor[i], ver[k]), intersect(hor[i], ver[l]), intersect(hor[j], ver[l]), intersect(hor[j], ver[k])];
            if (p.some((x) => !x)) continue;
            if (p.some((x) => x.x < -0.05 * w || x.x > 1.05 * w || x.y < -0.05 * h || x.y > 1.05 * h)) continue;
            const q = orderCorners(p);
            const a = area(q);
            if (a < minArea || !convexAndAngles(q)) continue;
            // sides in order top, right, bottom, left: which source line is each?
            let sup = 0;
            for (let s = 0; s < 4; s++) {
              const p1 = q[s], p2 = q[(s + 1) % 4];
              const onBorder =
                (Math.abs(p1.y) < 1.5 && Math.abs(p2.y) < 1.5) || (Math.abs(p1.y - h + 1) < 1.5 && Math.abs(p2.y - h + 1) < 1.5) ||
                (Math.abs(p1.x) < 1.5 && Math.abs(p2.x) < 1.5) || (Math.abs(p1.x - w + 1) < 1.5 && Math.abs(p2.x - w + 1) < 1.5);
              sup += onBorder ? 0.35 : sideSupport(acc, w, h, p1, p2);
            }
            cands.push({ q, score: sup / 4 + (0.15 * a) / (w * h) - 0.05 * nBorder });
          }
        }
      }
    }
    if (!cands.length) return null;
    cands.sort((a, b) => b.score - a.score);
    let best = null;
    for (const c of cands.slice(0, 8)) {
      let total = c.score + 0.35 * contrast(labData, w, h, c.q);
      if (prev) {
        const dev = Math.max(...c.q.map((p, i) => Math.hypot(p.x - prev[i].x, p.y - prev[i].y)));
        total -= (0.5 * dev) / Math.max(w, h);
      }
      if (!best || total > best.total) best = { total, q: c.q };
    }
    return { corners: best.q.map((p) => ({ x: p.x / f, y: p.y / f })), score: best.total };
  }

  /**
   * True width/height ratio of a rectangle seen in perspective, after Zhang & He ("Whiteboard
   * scanning and image enhancement", 2007). Their focal length estimate from a single quad is too
   * noisy for almost frontal phone shots, so a typical phone focal length (0.7 x image diagonal)
   * is assumed; on the test photos this is within 2 % for flat sheets. Ratios within 6 % of
   * DIN A (1 : sqrt 2) snap to it exactly.
   * @param q [tl, tr, br, bl] in pixels of an image of size w x h
   */
  function aspectRatio(q, w, h) {
    const u0 = w / 2, v0 = h / 2;
    const f2 = (0.7 * Math.hypot(w, h)) ** 2;
    const m1 = [q[0].x, q[0].y, 1], m2 = [q[1].x, q[1].y, 1], m3 = [q[3].x, q[3].y, 1], m4 = [q[2].x, q[2].y, 1];
    const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
    const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    const k2 = dot(cross(m1, m4), m3) / dot(cross(m2, m4), m3);
    const k3 = dot(cross(m1, m4), m2) / dot(cross(m3, m4), m2);
    const n2 = m2.map((v, i) => k2 * v - m1[i]);
    const n3 = m3.map((v, i) => k3 * v - m1[i]);
    const norm = (n) => ((n[0] - u0 * n[2]) ** 2 + (n[1] - v0 * n[2]) ** 2) / f2 + n[2] * n[2];
    let ratio = Math.sqrt(norm(n2) / norm(n3));
    if (!Number.isFinite(ratio) || ratio <= 0) {
      const d = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
      ratio = (d(q[0], q[1]) + d(q[3], q[2])) / (d(q[0], q[3]) + d(q[1], q[2]));
    }
    for (const din of [Math.SQRT1_2, Math.SQRT2]) {
      if (Math.abs(ratio - din) / din < 0.06) return din;
    }
    return ratio;
  }

  global.HeftigDetect = { detect, aspectRatio, MIN_SCORE: 0.85 };
})(window);
