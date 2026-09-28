"""Turning a classifier response into metadata - strictly validated, never overriding the user.

Rules:
- locked fields are never changed
- every value must pass validation; dates and custom field values must be backed by a quote
  that really occurs in the document text, otherwise they are dropped (never invented)
- existing taxonomy terms (incl. aliases) are reused; a new term that looks like a near-duplicate
  of an existing one becomes a review suggestion instead of being created
- low confidence -> suggestion (for names) or "uncertain" status (for the date)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import taxonomy as tax
from .archive import Archive
from .i18n import N_
from .models import CustomField, DocumentMetadata, Suggestion
from .providers.base import ClassifyRequest
from .textnorm import clean_display_name, fold, normalize_name, parse_number
from .titles import normalize_title, title_examples

MAX_TITLE = 200
MAX_SUMMARY = 1200
MAX_TAGS = 8


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    suggested: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)


def build_request(archive: Archive, meta: DocumentMetadata, text: str) -> ClassifyRequest:
    conn = archive.conn
    taxonomy: dict[str, list[dict[str, Any]]] = {}
    for kind in tax.KINDS:
        taxonomy[kind] = [
            {"name": t.name, "aliases": t.aliases} for t in tax.list_terms(conn, kind)
        ]
    keys = [r[0] for r in conn.execute("SELECT DISTINCT key FROM custom_field_values ORDER BY key")]
    limit = archive.settings.classify_max_chars
    truncated = len(text) > limit
    if truncated:
        head = text[: int(limit * 0.8)]
        tail = text[-int(limit * 0.2) :]
        text = head + "\n[…]\n" + tail
    return ClassifyRequest(
        text=text,
        filename=meta.original_filename,
        page_count=meta.page_count,
        taxonomy=taxonomy,
        custom_field_keys=keys,
        truncated=truncated,
        title_examples=title_examples(conn, meta.id),
        paper=meta.paper,
        paper_folder=meta.scan_session.name if meta.scan_session else None,
        language=archive.settings.language,
    )


# stored texts (English; translated when shown)
DATE_UNCERTAIN = N_("Document date uncertain")
DATE_NOT_BACKED = N_("Date not found word for word in the text – please check")


def found_in_text(evidence: str) -> str:
    """The stored explanation of a date backed by a quote."""
    return N_("Found in the text: “%(quote)s”") % {"quote": evidence.strip()[:60]}


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", fold(s)).strip()


def _in_text(evidence: str | None, text_squashed: str) -> bool:
    if not evidence or len(evidence.strip()) < 2:
        return False
    return _squash(evidence) in text_squashed


def _conf(data: dict, key: str) -> float:
    v = data.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(v)))


def _str(data: dict, key: str, limit: int = 200) -> str | None:
    v = data.get(key)
    if not isinstance(v, str):
        return None
    v = clean_display_name(v)[:limit]
    # "–", "-", "n/a" style placeholders carry no name
    return v if normalize_name(v) else None


_MONTHS = {
    "jan": 1, "feb": 2, "mae": 3, "mar": 3, "mrz": 3, "apr": 4, "mai": 5, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dez": 12, "dec": 12,
    "jae": 1,  # Jänner
}  # fmt: skip
_ORD = r"(?:st|nd|rd|th|ter|ten)?"
_MONTH_WORD = r"([A-Za-zÄÖÜäöüé]{3,})"


def _month(word: str) -> int | None:
    return _MONTHS.get(fold(word)[:3])


def _unspace(text: str) -> str:
    """'1 1 J u n i 2 0 1 5' (letter-spaced headers) -> '11 Juni 2015'."""
    return re.sub(r"(?<=\b\w) (?=\w\b)", "", text)


def _dates_in(evidence: str) -> set[date]:
    """Every date an evidence quote can mean (day/month order and century ambiguity included):
    31.12.2025, 31/12/2025, 31-12-25, 2025-12-31, 31. Dezember 2025, December 31st, 2025,
    Dec-31-2025, 31 Dec 2025 ..."""
    out: set[date] = set()

    def add(y: int, m: int, d: int) -> None:
        try:
            out.add(date(y, m, d))
        except ValueError:
            pass

    for text in {evidence, _unspace(evidence)}:
        for m in re.finditer(r"(\d{1,2})\s?[./-]\s?(\d{1,2})\s?[./-]\s?(\d{4}|\d{2})(?!\d)", text):
            a, b, y = int(m[1]), int(m[2]), m[3]
            years = [int(y)] if len(y) == 4 else [2000 + int(y), 1900 + int(y)]
            for yy in years:
                add(yy, b, a)  # day first (German)
                add(yy, a, b)  # month first (US)
        for m in re.finditer(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)", text):
            add(int(m[1]), int(m[2]), int(m[3]))
        # 31. Dezember 2025 / 31st of December, 2025 / 31 Dec 2025
        for m in re.finditer(
            rf"(\d{{1,2}}){_ORD}\.?\s*(?:of\s+)?{_MONTH_WORD}\.?,?\s*(\d{{4}})", text
        ):
            mo = _month(m[2])
            if mo:
                add(int(m[3]), mo, int(m[1]))
        # December 31st, 2025 / Dec-31-2025 / June, 2nd, 2021
        for m in re.finditer(rf"{_MONTH_WORD}\.?[\s,-]+(\d{{1,2}}){_ORD}[\s,.-]+(\d{{4}})", text):
            mo = _month(m[1])
            if mo:
                add(int(m[3]), mo, int(m[2]))
    return out


def _evidence_matches_date(evidence: str, iso: str) -> bool:
    return date.fromisoformat(iso) in _dates_in(evidence)


def apply(
    archive: Archive,
    meta: DocumentMetadata,
    data: dict[str, Any],
    text: str,
    provider: str,
) -> ApplyResult:
    s = archive.settings
    conn = archive.conn
    res = ApplyResult()
    squashed = _squash(text)
    min_conf = s.classify_min_confidence
    # suggestions from earlier runs are replaced by this run's result
    meta.suggestions = []

    # documents that arrived by e-mail come from anyone who knows the address: their AI output
    # never creates new categories (they would reach every later prompt), only suggestions
    from_outside = meta.source == "email"

    def suggest(fld: str, value: Any, reason: str, confidence: float | None = None) -> None:
        meta.suggestions.append(
            Suggestion(field=fld, value=value, reason=reason, confidence=confidence)
        )
        res.suggested.append(fld)

    # title
    title = normalize_title(_str(data, "title", MAX_TITLE))
    if title and not meta.locked("title"):
        meta.title = title
        meta.field_sources["title"] = "ai"
        res.applied.append("title")

    # keep the paper original? (only meaningful for paper; the user's decision wins)
    keep = data.get("keep_original")
    if meta.paper and isinstance(keep, bool) and meta.keep_original_source != "user":
        meta.keep_original = keep
        meta.keep_original_reason = _str(data, "keep_original_reason", 200)
        meta.keep_original_source = "ai"

    # document date
    if not meta.locked("document_date"):
        raw_date = data.get("document_date")
        evidence = data.get("document_date_evidence")
        conf = _conf(data, "document_date_confidence")
        iso = None
        if isinstance(raw_date, str):
            try:
                iso = date.fromisoformat(raw_date.strip()[:10]).isoformat()
            except ValueError:
                res.dropped.append(N_("document_date (invalid format)"))
        if iso and not (1900 <= int(iso[:4]) <= date.today().year + 1):
            res.dropped.append(N_("document_date (implausible year)"))
            iso = None
        if iso:
            backed = (
                isinstance(evidence, str)
                and _in_text(evidence, squashed)
                and (_evidence_matches_date(evidence, iso))
            )
            if not backed:
                suggest(
                    "document_date",
                    iso,
                    DATE_NOT_BACKED,
                    conf,
                )
                meta.document_date_status = "unknown"
                meta.document_date_reason = N_("Suggestion not backed by the text")
            else:
                meta.document_date = iso
                meta.field_sources["document_date"] = "ai"
                meta.document_date_reason = found_in_text(evidence)
                if conf >= min_conf:
                    meta.document_date_status = "ai"
                else:
                    meta.document_date_status = "ai_uncertain"
                    res.review_reasons.append(DATE_UNCERTAIN)
                res.applied.append("document_date")
        elif raw_date is None:
            if meta.document_date is None:
                meta.document_date_status = "none_found"
                meta.document_date_reason = N_("No date found in the document")

    # correspondent / document type
    for fld, kind in (("correspondent", "correspondent"), ("document_type", "document_type")):
        if meta.locked(fld):
            continue
        name = _str(data, fld)
        conf = _conf(data, f"{fld}_confidence")
        if not name:
            continue
        canonical = tax.canonical_name(conn, kind, name)
        if canonical:
            if conf >= min_conf:
                setattr(meta, fld, canonical)
                meta.field_sources[fld] = "ai"
                res.applied.append(fld)
            else:
                suggest(fld, canonical, N_("Uncertain match"), conf)
            continue
        if not plausible_name(name, kind):
            res.dropped.append(f"{fld} {name[:40]}")
            continue
        similar = tax.similar_terms(conn, kind, name)
        if similar:
            suggest(
                fld,
                similar[0],
                N_("AI suggested “%(name)s” – similar to “%(similar)s”")
                % {"name": name, "similar": similar[0]},
                conf,
            )
            suggest(fld, name, N_("New entry (not created automatically)"), conf)
            res.review_reasons.append(
                N_("%(field)s: a similar name already exists") % {"field": _label(fld)}
            )
        elif from_outside:
            suggest(fld, name, N_("New entry from an email – please confirm"), conf)
        elif conf >= min_conf:
            setattr(meta, fld, name)
            meta.field_sources[fld] = "ai"
            res.applied.append(fld)
        else:
            suggest(fld, name, N_("New entry, uncertain match"), conf)

    # tags
    if not meta.locked("tags"):
        raw_tags = data.get("tags")
        tags: list[str] = []
        if isinstance(raw_tags, list):
            for t in raw_tags[:MAX_TAGS]:
                if not isinstance(t, str):
                    continue
                t = clean_display_name(t)[:60]
                if not normalize_name(t):
                    continue
                canonical = tax.canonical_name(conn, "tag", t)
                if canonical:
                    tags.append(canonical)
                    continue
                if not plausible_name(t, "tag"):
                    res.dropped.append(f"tag {t[:40]}")
                    continue
                similar = tax.similar_terms(conn, "tag", t)
                if similar:
                    suggest(
                        "tags",
                        [similar[0]],
                        N_("AI suggested the tag “%(name)s” – similar to “%(similar)s”")
                        % {"name": t, "similar": similar[0]},
                    )
                elif from_outside:
                    suggest("tags", [t], N_("New tag from an email – please confirm"))
                else:
                    tags.append(t)
        removed = {normalize_name(x) for x in meta.tag_overrides.removed}
        final = [t for t in tags if normalize_name(t) not in removed]
        for t in meta.tag_overrides.added:
            if normalize_name(t) not in {normalize_name(x) for x in final}:
                final.append(t)
        meta.tags = list(dict.fromkeys(final))
        meta.field_sources["tags"] = "ai"
        res.applied.append("tags")

    # summary
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip() and not meta.locked("summary"):
        meta.summary = summary.strip()[:MAX_SUMMARY]
        meta.field_sources["summary"] = "ai"
        res.applied.append("summary")

    # custom fields
    if not meta.locked("custom_fields"):
        existing_keys = {normalize_name(k): k for k in _known_keys(conn)}
        new_fields: dict[str, CustomField] = {}
        raw_fields = data.get("custom_fields")
        for item in raw_fields if isinstance(raw_fields, list) else []:
            cf = _validate_custom_field(item, squashed)
            if cf is None:
                res.dropped.append(
                    f"custom_field {str(item.get('key') if isinstance(item, dict) else item)[:40]}"
                )
                continue
            key, value = cf
            key = existing_keys.get(normalize_name(key), key)
            new_fields[key] = value
        if new_fields or meta.custom_fields:
            meta.custom_fields = new_fields
            meta.field_sources["custom_fields"] = "ai"
            res.applied.append("custom_fields")
    return res


# new category names the AI may create: names, not sentences (an instruction smuggled into a
# document would otherwise become a permanent entry that every later prompt contains)
_NAME_RE = re.compile(r"[\w .,&'’()/+@-]+")
_NAME_LIMITS = {"correspondent": (80, 10), "document_type": (50, 5), "tag": (40, 4)}


def plausible_name(name: str, kind: str) -> bool:
    max_len, max_words = _NAME_LIMITS[kind]
    return (
        len(name) <= max_len and len(name.split()) <= max_words and bool(_NAME_RE.fullmatch(name))
    )


def _label(fld: str) -> str:
    """The field's English label (stored in review reasons, translated when shown)."""
    return {"correspondent": N_("Sender"), "document_type": N_("Document type")}.get(fld, fld)


def _known_keys(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT key FROM custom_field_values")]


def _validate_custom_field(item: Any, squashed: str) -> tuple[str, CustomField] | None:
    if not isinstance(item, dict):
        return None
    key = item.get("key")
    ftype = item.get("type")
    value = item.get("value")
    evidence = item.get("evidence")
    if (
        not isinstance(key, str)
        or not key.strip()
        or ftype not in ("string", "number", "monetary", "date")
    ):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    if not _in_text(evidence, squashed):
        return None
    key = clean_display_name(key)[:60]
    value = value.strip()[:200]
    currency = item.get("currency")
    if ftype in ("number", "monetary"):
        num = _parse_number(value)
        # the value itself must be the number the quoted evidence shows (a sum or a
        # reading error would otherwise pass as "checked against the text")
        if num is None or not any(abs(abs(num) - abs(n)) < 0.005 for n in _numbers_in(evidence)):
            return None
        if ftype == "monetary":
            if not (isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency or "")):
                currency = "EUR" if "€" in str(evidence) or "eur" in fold(str(evidence)) else None
            if currency is None:
                return None
            return key, CustomField(type="monetary", value=num, currency=currency)
        return key, CustomField(type="number", value=num)
    if ftype == "date":
        try:
            d = date.fromisoformat(value[:10])
        except ValueError:
            return None
        if d not in _dates_in(str(evidence)):
            return None
        return key, CustomField(type="date", value=d.isoformat())
    # a string value must itself be in the text (e.g. a contract number)
    if not _in_text(value, squashed) and not _in_text(
        re.sub(r"[\s./-]", "", value), re.sub(r"[\s./-]", "", squashed)
    ):
        return None
    return key, CustomField(type="string", value=value)


def _parse_number(value: str) -> float | None:
    return parse_number(value)


_NUMBER_RE = re.compile(
    r"\d{1,3}(?:,\d{3})+\.\d+|\d{1,3}(?:[.\u00a0 ]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?"
)


def _numbers_in(evidence: Any) -> list[float]:
    out = []
    for m in _NUMBER_RE.findall(str(evidence or "")):
        n = parse_number(m)
        if n is not None:
            out.append(n)
    return out
