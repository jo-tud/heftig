"""Open AI suggestions, grouped by what they propose ("document type: Schreiben" on 29
documents), so that the same decision is taken once for all of them.

Accepting works exactly like the button on the document page (the value is set as the user's
and locked; competing suggestions for the field disappear); dismissing drops the suggestion.
Only documents the user saw in the group are touched, and each one is matched by field and
value again, so a document reclassified meanwhile is left alone.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from . import documents as docs
from .archive import Archive
from .i18n import N_, Labels, _, translate_text

FIELD_LABELS = Labels({
    "correspondent": N_("Sender"),
    "document_type": N_("Document type"),
    "tags": N_("Tags"),
    "document_date": N_("Document date"),
    "title": N_("Title"),
})  # fmt: skip


def _key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _label(value: Any) -> str:
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def groups(archive: Archive) -> list[dict[str, Any]]:
    """Groups of identical suggestions, largest first."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    rows = archive.conn.execute(
        "SELECT id, title, original_filename, metadata_json FROM documents "
        "WHERE status IN ('needs_review','failed') ORDER BY received_at DESC"
    )
    for r in rows:
        meta = json.loads(r["metadata_json"])
        for s in meta.get("suggestions") or []:
            key = (s["field"], _key(s["value"]))
            g = by_key.setdefault(key, {
                "field": s["field"], "field_label": FIELD_LABELS.get(s["field"], s["field"]),
                "value": key[1], "label": _label(s["value"]), "docs": [], "reasons": Counter(),
                "confidences": [],
            })  # fmt: skip
            if any(d["id"] == r["id"] for d in g["docs"]):
                continue
            others = [
                _label(o["value"]) for o in meta["suggestions"]
                if o["field"] == s["field"] and _key(o["value"]) != key[1]
            ]  # fmt: skip
            g["docs"].append({
                "id": r["id"], "title": r["title"] or r["original_filename"],
                "current": _label(meta.get(s["field"])) if meta.get(s["field"]) else "",
                "others": others,
            })  # fmt: skip
            g["reasons"][s.get("reason") or ""] += 1
            if isinstance(s.get("confidence"), int | float):
                g["confidences"].append(float(s["confidence"]))
    out = []
    for g in by_key.values():
        conf = g.pop("confidences")
        g["confidence"] = sum(conf) / len(conf) if conf else None
        g["reason"] = translate_text(g.pop("reasons").most_common(1)[0][0])  # stored in English
        g["count"] = len(g["docs"])
        alt = Counter(o for d in g["docs"] for o in d["others"])
        g["alternatives"] = [
            _("“%(value)s” (%(num)s)", value=v, num=n) for v, n in alt.most_common(3)
        ]
        out.append(g)
    out.sort(key=lambda g: (-g["count"], g["field"], g["label"].casefold()))
    return out


def apply(archive: Archive, field: str, value: str, ids: list[str], accept: bool) -> int:
    """Accept or dismiss the suggestion (field, value as in ``groups``) on these documents."""
    done = 0
    for doc_id in dict.fromkeys(ids):
        try:
            meta = docs.load_meta(archive, doc_id)
        except docs.DocumentNotFound:
            continue
        idx = next(
            (i for i, s in enumerate(meta.suggestions)
             if s.field == field and _key(s.value) == value),
            None,
        )  # fmt: skip
        if idx is None:
            continue  # reclassified or decided meanwhile
        (docs.accept_suggestion if accept else docs.dismiss_suggestion)(archive, doc_id, idx)
        done += 1
    return done


def open_count(archive: Archive) -> tuple[int, int]:
    """(documents with open suggestions, groups with more than one document)."""
    gs = groups(archive)
    return len({d["id"] for g in gs for d in g["docs"]}), sum(1 for g in gs if g["count"] > 1)
