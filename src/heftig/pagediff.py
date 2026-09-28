"""Visual and textual comparison of two documents (duplicate review, automatic resolution).

Pages are compared as images: both renderings are cropped to their printed content, scaled to
the same size and blurred a little (tolerates small shifts and different rendering), then a
grid of cells marks where they differ. Connected cells become boxes. For every box the side with
more ink is noted - a signature, stamp or handwritten note that only one copy has.

Pages that do not line up at all (a phone photo against a PDF, different layouts) are reported
as "not comparable" instead of drowning in boxes. Text differences come from a word diff.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from . import documents as docs
from .archive import Archive
from .media import render_width
from .models import DocumentMetadata
from .storage import atomic_write_json, read_json
from .textnorm import fold

WIDTH = 800  # rendering width for the comparison
CELL = 12  # grid cell in px (of the normalised page, 600 px wide)
NORM_W = 600
# calibrated on synthetic rescans (skew, shift, JPEG noise must stay "same") against signature,
# stamp and handwritten note (must be found); finer settings flag rescans of the same paper.
# Small changes inside printed text (another amount) are left to the text comparison.
CELL_SHARE = 0.08  # share of changed pixels that marks a cell as different
DIFF_LEVEL = 55  # ink difference (0-255) that counts as changed
BLUR = 2.0
EDGE = 0.025  # boxes entirely within this margin of the page are scanner edges, not content
UNALIGNED_SHARE = 0.35  # more changed cells than this: the pages do not line up
MAX_PAGES = 30
CACHE_VERSION = 2


@dataclass
class PageDiff:
    page: int
    status: str  # "same" | "different" | "unaligned" | "missing"
    boxes_a: list[list[float]] = field(default_factory=list)  # [x0, y0, x1, y1] 0..1
    boxes_b: list[list[float]] = field(default_factory=list)
    more_ink: str | None = None  # "a" | "b": that side has extra marks in the differing areas


def _ink(img: Image.Image) -> Image.Image:
    return ImageChops.invert(img.convert("L"))


def _profile(ink: Image.Image, axis: str) -> list[float]:
    """Ink per column (axis "x") or row (axis "y") of a small copy."""
    w, h = ink.size
    small = ink.resize((w, 1) if axis == "x" else (1, h), Image.BOX)
    return list(small.tobytes())


def _best_shift(pa: list[float], pb: list[float], max_shift: int) -> int:
    """Shift of b against a that lines up their ink profiles best (sum of absolute
    differences) - robust against a few extra marks such as a signature or a stamp."""
    n = len(pa)
    best, best_s = None, 0
    for s in range(-max_shift, max_shift + 1):
        total = 0.0
        for i in range(max(0, s), min(n, n + s)):
            total += abs(pa[i] - pb[i - s])
        total /= max(1, min(n, n + s) - max(0, s))
        if best is None or total < best:
            best, best_s = total, s
    return best_s


def compare_images(img_a: Image.Image, img_b: Image.Image) -> PageDiff:
    ia, ib = _ink(img_a), _ink(img_b)
    ra, rb = ia.width / ia.height, ib.width / ib.height
    if abs(ra - rb) / max(ra, rb) > 0.08:
        return PageDiff(page=0, status="unaligned")
    nh = max(1, round(NORM_W / ra))
    na = ia.resize((NORM_W, nh), Image.LANCZOS)
    nb = ib.resize((NORM_W, nh), Image.LANCZOS)
    if na.getbbox() is None and nb.getbbox() is None:
        return PageDiff(page=0, status="same")
    # line both pages up (scanners shift the paper by a few millimetres)
    dx = _best_shift(_profile(na, "x"), _profile(nb, "x"), NORM_W // 25)
    dy = _best_shift(_profile(na, "y"), _profile(nb, "y"), nh // 25)
    shifted = Image.new("L", nb.size, 0)
    shifted.paste(nb, (dx, dy))
    nb = shifted
    diff = ImageChops.difference(
        na.filter(ImageFilter.GaussianBlur(BLUR)), nb.filter(ImageFilter.GaussianBlur(BLUR))
    ).point(lambda v: 255 if v > DIFF_LEVEL else 0)
    cols, rows = NORM_W // CELL, nh // CELL
    grid = [[False] * cols for _ in range(rows)]
    changed = 0
    px = diff.load()
    for gy in range(rows):
        for gx in range(cols):
            n = 0
            for y in range(gy * CELL, gy * CELL + CELL, 2):
                for x in range(gx * CELL, gx * CELL + CELL, 2):
                    n += px[x, y] > 0
            if n / ((CELL // 2) ** 2) > CELL_SHARE:
                grid[gy][gx] = True
                changed += 1
    if changed == 0:
        return PageDiff(page=0, status="same")
    inked = sum(
        1 for gy in range(rows) for gx in range(cols)
        if na.crop((gx * CELL, gy * CELL, gx * CELL + CELL, gy * CELL + CELL)).getbbox()
    )  # fmt: skip
    if changed / max(1, inked) > UNALIGNED_SHARE:
        return PageDiff(page=0, status="unaligned")
    boxes = [
        b for b in _components(grid)
        if not (
            (b[2] + 1) * CELL <= NORM_W * EDGE or b[0] * CELL >= NORM_W * (1 - EDGE)
            or (b[3] + 1) * CELL <= nh * EDGE or b[1] * CELL >= nh * (1 - EDGE)
        )
    ]  # fmt: skip
    if not boxes:
        return PageDiff(page=0, status="same")  # only isolated specks (dust, noise)
    out = PageDiff(page=0, status="different")
    ink_a = ink_b = 0.0
    for gx0, gy0, gx1, gy1 in boxes:
        box = (gx0 * CELL, gy0 * CELL, (gx1 + 1) * CELL, (gy1 + 1) * CELL)
        ink_a += sum(na.crop(box).tobytes())
        ink_b += sum(nb.crop(box).tobytes())
        out.boxes_a.append([round(box[0] / NORM_W, 4), round(box[1] / nh, 4),
                            round(box[2] / NORM_W, 4), round(box[3] / nh, 4)])  # fmt: skip
        out.boxes_b.append([round((box[0] - dx) / NORM_W, 4), round((box[1] - dy) / nh, 4),
                            round((box[2] - dx) / NORM_W, 4), round((box[3] - dy) / nh, 4)])  # fmt: skip
    if max(ink_a, ink_b) > 0 and abs(ink_a - ink_b) / max(ink_a, ink_b) > 0.25:
        out.more_ink = "a" if ink_a > ink_b else "b"
    return out


def _components(grid: list[list[bool]]) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of connected changed cells (8-neighbourhood, gaps of one cell bridged);
    single isolated cells (noise) are dropped."""
    rows, cols = len(grid), len(grid[0]) if grid else 0
    seen = [[False] * cols for _ in range(rows)]
    out = []
    for y in range(rows):
        for x in range(cols):
            if not grid[y][x] or seen[y][x]:
                continue
            stack = [(x, y)]
            seen[y][x] = True
            x0 = x1 = x
            y0 = y1 = y
            n = 0
            while stack:
                cx, cy = stack.pop()
                n += 1
                x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
                for dy in (-2, -1, 0, 1, 2):
                    for dx in (-2, -1, 0, 1, 2):
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < cols and 0 <= ny < rows and grid[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
            if n >= 2:
                out.append((x0, y0, x1, y1))
    return out


def _render(archive: Archive, meta: DocumentMetadata, page: int) -> Image.Image:
    return render_width(
        archive.paths.resolve(meta.original_relpath), meta.mime_type, page - 1, WIDTH,
        archive.settings.max_image_megapixels,
    ).convert("L")  # fmt: skip


def compare_documents(archive: Archive, a: DocumentMetadata, b: DocumentMetadata) -> dict[str, Any]:
    """Page-by-page comparison, cached next to document a (keyed by both file hashes)."""
    cache = docs.files(archive, a.id).dir / "cache" / f"pagediff-{b.id}.json"
    key = {"v": CACHE_VERSION, "a": a.sha256, "b": b.sha256}
    try:
        data = read_json(cache)
        if data.get("key") == key:
            return data["result"]
    except (OSError, ValueError):
        pass
    pa, pb = a.page_count or 1, b.page_count or 1
    pages = []
    for n in range(1, min(max(pa, pb), MAX_PAGES) + 1):
        if n > pa or n > pb:
            pages.append(PageDiff(page=n, status="missing", more_ink="a" if n <= pa else "b"))
            continue
        try:
            d = compare_images(_render(archive, a, n), _render(archive, b, n))
        except Exception:  # noqa: BLE001 - a broken page must not break the review page
            d = PageDiff(page=n, status="unaligned")
        d.page = n
        pages.append(d)
    result = {
        "pages": [vars(p) for p in pages],
        "identical": all(p.status == "same" for p in pages) and pa == pb,
        "comparable": all(p.status != "unaligned" for p in pages),
    }
    try:
        if docs.cache_writable(archive, a.id):
            atomic_write_json(cache, {"key": key, "result": result})
    except OSError:
        pass
    return result


# --- text --------------------------------------------------------------------------------

_WORD = re.compile(r"\S+")


def text_diff(text_a: str, text_b: str, max_changes: int = 30, context: int = 5) -> dict[str, Any]:
    """Word-level differences: [{"before": ..., "a": ..., "b": ...}], compared case- and
    umlaut-insensitively, shown in the original spelling."""
    wa, wb = _WORD.findall(text_a)[:6000], _WORD.findall(text_b)[:6000]
    fa, fb = [fold(w).strip(".,;:") for w in wa], [fold(w).strip(".,;:") for w in wb]
    sm = difflib.SequenceMatcher(None, fa, fb, autojunk=False)
    changes = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        changes.append({
            "before": " ".join(wa[max(0, i1 - context) : i1]),
            "a": " ".join(wa[i1:i2]), "b": " ".join(wb[j1:j2]),
        })  # fmt: skip
    return {
        "identical": not changes,
        "similarity": round(sm.ratio(), 3),
        "changes": changes[:max_changes],
        "more": max(0, len(changes) - max_changes),
    }
