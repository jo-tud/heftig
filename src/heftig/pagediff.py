"""Visual and textual comparison of two documents (duplicate review, automatic resolution).

Pages are compared as images: both renderings are cropped to their printed content, scaled to
the same size and blurred a little (tolerates small shifts and different rendering), then a
grid of cells marks where they differ. Connected cells become boxes. For every box the side with
more ink is noted - a signature, stamp or handwritten note that only one copy has.

Pages that do not line up at all (a phone photo against a PDF, different layouts) are reported
as "not comparable" instead of drowning in boxes. Text differences come from a word diff.

Pages with a text layer on both sides (born-digital PDFs) are compared by what they say: the
words that differ are marked where they are on the page ("31.12.2021" instead of "30.09.2021"),
and the image comparison only looks at what is not text - a signature, a stamp, a note, another
logo. Two renderings of the same words never differ pixel for pixel (fonts, anti-aliasing,
lines moved by a point), so comparing text areas as images marks noise and misses what matters.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

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
CACHE_VERSION = 3
TEXT_PAGE_MIN_SIMILARITY = 0.5  # below: another layout, compare the pages as images


@dataclass
class PageDiff:
    page: int
    status: str  # "same" | "different" | "unaligned" | "missing"
    boxes_a: list[list[float]] = field(default_factory=list)  # [x0, y0, x1, y1] 0..1
    boxes_b: list[list[float]] = field(default_factory=list)
    more_ink: str | None = None  # "a" | "b": that side has extra marks in the differing areas
    # words that differ (text layer on both sides), [x0, y0, x1, y1] 0..1
    words_a: list[list[float]] = field(default_factory=list)
    words_b: list[list[float]] = field(default_factory=list)


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


def compare_images(img_a: Image.Image, img_b: Image.Image, whole_pages: bool = True) -> PageDiff:
    """whole_pages=False: pages with their text painted over (see compare_text_pages) - little
    ink is left, so a signature is not "a page that does not line up"."""
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
    if whole_pages and changed / max(1, inked) > UNALIGNED_SHARE:
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


def _word_key(text: str) -> str:
    return fold(text).strip(".,;:()[]\"'“”„")


def _changed_boxes(words: list, changed: set[int]) -> list[list[float]]:
    """Boxes around the changed words; neighbours on the same line become one box."""
    out: list[list[float]] = []
    last = None
    for i in sorted(changed):
        _, x0, y0, x1, y1 = words[i]
        if out and last == i - 1:
            b = out[-1]
            overlap = min(b[3], y1) - max(b[1], y0)
            if overlap > 0.5 * min(b[3] - b[1], y1 - y0):
                out[-1] = [min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)]
                last = i
                continue
        out.append([x0, y0, x1, y1])
        last = i
    pad = 0.003
    return [[round(max(0, b[0] - pad), 4), round(max(0, b[1] - pad), 4),
             round(min(1, b[2] + pad), 4), round(min(1, b[3] + pad), 4)] for b in out]  # fmt: skip


def _without_words(img: Image.Image, words: list) -> Image.Image:
    """The page with its text painted over in the paper colour: what is left is not text."""
    out = img.convert("L").copy()
    w, h = out.size
    draw = ImageDraw.Draw(out)
    for _, x0, y0, x1, y1 in words:
        draw.rectangle((x0 * w - 2, y0 * h - 2, x1 * w + 2, y1 * h + 2), fill=255)
    return out


def _explained_by_shift(
    a: Image.Image, b: Image.Image, box_a: list[float], box_b: list[float], max_shift: int = 8
) -> bool:
    """The area looks the same on both sides once moved by a few pixels."""
    ia = _ink(a).filter(ImageFilter.GaussianBlur(1))
    ib = _ink(b).filter(ImageFilter.GaussianBlur(1))
    ax0, ay0 = round(box_a[0] * a.width), round(box_a[1] * a.height)
    ax1, ay1 = round(box_a[2] * a.width), round(box_a[3] * a.height)
    bx0, by0 = round(box_b[0] * b.width), round(box_b[1] * b.height)
    crop_a = ia.crop((ax0, ay0, ax1, ay1))
    n = max(1, crop_a.width * crop_a.height)
    for dy in range(-max_shift, max_shift + 1, 2):
        for dx in range(-max_shift, max_shift + 1, 2):
            crop_b = ib.crop(
                (bx0 + dx, by0 + dy, bx0 + dx + crop_a.width, by0 + dy + crop_a.height)
            )
            diff = ImageChops.difference(crop_a, crop_b).point(
                lambda v: 255 if v > DIFF_LEVEL else 0
            )
            if sum(1 for v in diff.tobytes() if v) / n < 0.01:
                return True
    return False


def compare_text_pages(
    img_a: Image.Image, img_b: Image.Image, words_a: list, words_b: list
) -> PageDiff:
    """Two pages with a text layer: the differing words, plus image differences outside the
    text (signature, stamp, note)."""
    ka, kb = [_word_key(w[0]) for w in words_a], [_word_key(w[0]) for w in words_b]
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    if sm.ratio() < TEXT_PAGE_MIN_SIMILARITY:
        return compare_images(img_a, img_b)
    changed_a: set[int] = set()
    changed_b: set[int] = set()
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            changed_a.update(i for i in range(i1, i2) if ka[i])
            changed_b.update(j for j in range(j1, j2) if kb[j])
    bare_a, bare_b = _without_words(img_a, words_a), _without_words(img_b, words_b)
    visual = compare_images(bare_a, bare_b, whole_pages=False)
    # a logo or a frame a few points further down in one of the PDFs is not a difference
    kept = [
        i for i, (ba, bb) in enumerate(zip(visual.boxes_a, visual.boxes_b, strict=True))
        if not _explained_by_shift(bare_a, bare_b, ba, bb)
    ]  # fmt: skip
    visual.boxes_a = [visual.boxes_a[i] for i in kept]
    visual.boxes_b = [visual.boxes_b[i] for i in kept]
    if not kept:
        visual.status, visual.more_ink = "same", None
    out = PageDiff(
        page=0, status=visual.status, boxes_a=visual.boxes_a, boxes_b=visual.boxes_b,
        more_ink=visual.more_ink, words_a=_changed_boxes(words_a, changed_a),
        words_b=_changed_boxes(words_b, changed_b),
    )  # fmt: skip
    if out.words_a or out.words_b:
        out.status = "different"
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


def _text_layer(archive: Archive, meta: DocumentMetadata, page: int) -> list | None:
    """Words of the page's text layer (born-digital PDF), None for scans and photos."""
    if meta.mime_type != "application/pdf":
        return None
    from .wordboxes import pdf_words

    return pdf_words(archive.paths.resolve(meta.original_relpath), page - 1)


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
            wa, wb = _text_layer(archive, a, n), _text_layer(archive, b, n)
            if wa and wb:
                d = compare_text_pages(_render(archive, a, n), _render(archive, b, n), wa, wb)
            else:
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
