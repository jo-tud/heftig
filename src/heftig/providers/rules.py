"""Deterministic, offline providers.

``rules`` classifies with simple patterns (dates, known correspondents/tags from the existing
taxonomy, number fields, a keyword list for document types). It never calls a network service and
is useful as a baseline without any AI. ``mock`` is the same plus failure injection for tests
(``HEFTIG_MOCK_FAIL=ocr:2,classify``) and a placeholder OCR.
"""

from __future__ import annotations

import json
import re
from datetime import date

from ..textnorm import fold, normalize_name
from .base import ClassifyRequest, ClassifyResponse, ExtractCapabilities, ProviderError

# (German name, English name, German keywords, English keywords). German compounds end with
# their head noun ("Beitragsrechnung" is a "Rechnung", "Vertragsnummer" is not a "Vertrag"), so
# German keywords must end a word; English ones are whole words. The first match wins; the name
# is taken in the language of the installation.
DOC_TYPES = [
    ("Mahnung", "Reminder", ["mahnung", "zahlungserinnerung"],
     ["payment reminder", "overdue notice", "final notice"]),
    ("Kündigung", "Cancellation", ["kuendigung", "kuendigungsbestaetigung"],
     ["cancellation", "notice of termination"]),
    ("Lohnabrechnung", "Payslip", ["lohnabrechnung", "gehaltsabrechnung", "entgeltabrechnung"],
     ["payslip", "pay slip", "pay stub", "earnings statement"]),
    ("Kontoauszug", "Account statement", ["kontoauszug"], ["account statement", "bank statement"]),
    ("Abrechnung", "Statement", ["abrechnung"], ["statement"]),
    ("Rechnung", "Invoice", ["rechnung"], ["invoice", "bill"]),
    ("Versicherungsschein", "Policy", ["versicherungsschein", "police"], ["insurance policy"]),
    ("Bescheid", "Tax assessment", ["bescheid"], ["tax assessment", "notice of assessment"]),
    ("Vertrag", "Contract", ["vertrag", "vertragsbestaetigung"],
     ["contract", "lease agreement", "rental agreement"]),
    ("Kassenbon", "Receipt", ["kassenbon", "kassenbeleg", "quittung"], ["receipt"]),
]  # fmt: skip
DOC_TYPE_RX = [
    (de, en, re.compile("|".join([r"[a-z]*" + w + r"(?![a-z])" for w in de_words]
                                 + [r"(?<![a-z])" + w + r"(?![a-z])" for w in en_words])))
    for de, en, de_words, en_words in DOC_TYPES
]  # fmt: skip

_DATE_NUM = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = {
    "januar": 1, "februar": 2, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
    "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7, "october": 10,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
# "5. März 2026", "5 March 2026", "March 5, 2026"
_DATE_WORD = re.compile(r"\b(\d{1,2})\.?\s*([A-Za-zÄÖÜäöü]+)\.?\s+(\d{4})\b")
_DATE_WORD_US = re.compile(r"\b([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})\b")
# (German key, English key, type, pattern with the value in group "v" [and "cur"])
_FIELD_PATTERNS = [
    ("Vertragsnummer", "Contract number", "string", r"(?:Vertrags(?:-?nummer|-?nr\.?)|(?:Contract|Agreement|Policy) (?:number|no\.?))\s*:?\s*(?P<v>[A-Z0-9][A-Z0-9 /-]{3,30}[0-9])"),
    ("Kundennummer", "Customer number", "string", r"(?:Kunden(?:-?nummer|-?nr\.?)|(?:Customer|Account) (?:number|no\.?))\s*:?\s*(?P<v>[A-Z0-9][A-Z0-9 /-]{3,30}[0-9])"),
    ("Rechnungsnummer", "Invoice number", "string", r"(?:Rechnungs(?:-?nummer|-?nr\.?)|Invoice (?:number|no\.?))\s*:?\s*(?P<v>[A-Z0-9][A-Z0-9 /-]{3,30}[0-9])"),
    ("Betrag", "Amount", "monetary_de", r"(?:Gesamtbetrag|Rechnungsbetrag|Betrag|Summe)\s*:?\s*(?P<v>-?\d{1,3}(?:\.\d{3})*,\d{2})\s*(?:€|EUR)"),
    ("Betrag", "Amount", "monetary_en", r"(?:Total due|Amount due|Balance due|Total amount|Total)\s*:?\s*(?P<cur>[$€£])?\s*(?P<v>-?\d{1,3}(?:,\d{3})*\.\d{2})\s*(?P<code>USD|EUR|GBP)?"),
]  # fmt: skip
_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP"}


def find_date(text: str) -> tuple[str | None, str | None]:
    """First plausible date in the text as (ISO date, evidence)."""
    candidates: list[tuple[int, str, str]] = []
    for m in _DATE_NUM.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        candidates.append((m.start(), _iso(y, mo, d), m.group(0)))
    for m in _DATE_WORD.finditer(text):
        mo = _MONTHS.get(fold(m.group(2)))
        if mo:
            candidates.append((m.start(), _iso(int(m.group(3)), mo, int(m.group(1))), m.group(0)))
    for m in _DATE_WORD_US.finditer(text):
        mo = _MONTHS.get(fold(m.group(1)))
        if mo:
            candidates.append((m.start(), _iso(int(m.group(3)), mo, int(m.group(2))), m.group(0)))
    for m in _DATE_ISO.finditer(text):
        candidates.append(
            (m.start(), _iso(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0))
        )
    for _, iso, ev in sorted(candidates):
        if iso and 1900 <= int(iso[:4]) <= date.today().year + 1:
            return iso, ev
    return None, None


def _iso(y: int, m: int, d: int) -> str:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return ""


def _contains_name(folded_text: str, name: str) -> bool:
    n = normalize_name(name)
    return (
        bool(n)
        and re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", folded_text) is not None
    )


class RulesClassifier:
    name = "rules"
    model = "rules"
    target = "local"
    adapter_version = "rules-v1"
    prompt_version = "rules-v1"

    def __init__(self, fail: bool = False):
        self.fail = fail

    def classify(self, request: ClassifyRequest) -> ClassifyResponse:
        if self.fail:
            raise ProviderError("Mock classification failed (HEFTIG_MOCK_FAIL)", transient=True)
        text = request.text
        # match against a whitespace-normalised folded copy
        folded = " ".join(normalize_name(ln) for ln in text.splitlines())
        out: dict = {
            "title": "",
            "document_date": None,
            "document_date_evidence": None,
            "document_date_confidence": 0.0,
            "correspondent": None,
            "correspondent_confidence": 0.0,
            "document_type": None,
            "document_type_confidence": 0.0,
            "tags": [],
            "summary": "",
            "custom_fields": [],
        }
        iso, ev = find_date(text)
        if iso:
            out.update(document_date=iso, document_date_evidence=ev, document_date_confidence=0.7)
        for term in request.taxonomy.get("correspondent", []):
            if any(_contains_name(folded, n) for n in [term["name"], *term.get("aliases", [])]):
                out.update(correspondent=term["name"], correspondent_confidence=0.9)
                break
        english = request.language == "en"
        for de_name, en_name, rx in DOC_TYPE_RX:
            if rx.search(folded):
                out.update(document_type=en_name if english else de_name,
                           document_type_confidence=0.75)  # fmt: skip
                break
        out["tags"] = [
            t["name"]
            for t in request.taxonomy.get("tag", [])
            if any(_contains_name(folded, n) for n in [t["name"], *t.get("aliases", [])])
        ][:5]
        seen: set[str] = set()
        for de_key, en_key, ftype, pattern in _FIELD_PATTERNS:
            key = en_key if english else de_key
            m = re.search(pattern, text, re.IGNORECASE)
            if not m or key in seen:
                continue
            seen.add(key)
            value, currency = m.group("v").strip(), None
            if ftype == "monetary_de":
                value, currency = value.replace(".", "").replace(",", "."), "EUR"
            elif ftype == "monetary_en":
                value = value.replace(",", "")
                currency = _CURRENCY.get(m.group("cur") or "") or (m.group("code") or "").upper()
            out["custom_fields"].append({
                "key": key, "type": "monetary" if ftype.startswith("monetary") else ftype,
                "value": value, "currency": currency or None, "evidence": m.group(0),
            })  # fmt: skip
        first_line = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 3), "")
        parts = [p for p in [out["document_type"], out["correspondent"]] if p]
        out["title"] = " ".join(parts) if parts else first_line[:80]
        if out["document_date"] and parts:
            out["title"] += f" {out['document_date'][:7]}"
        return ClassifyResponse(data=out, raw=json.dumps(out, ensure_ascii=False))


class MockExtractor:
    """Deterministic fake OCR: returns a placeholder line per page, can fail chosen pages."""

    name = "mock"
    model = "mock"
    target = "local"
    adapter_version = "mock-v1"
    capabilities = ExtractCapabilities(images=True)

    def __init__(self, fail_pages: set[int] | None = None):
        self.fail_pages = fail_pages or set()

    def extract_page(
        self, image: bytes, page_number: int, languages: str, media_type: str = "image/png"
    ) -> str:
        if page_number in self.fail_pages:
            raise ProviderError(f"Mock OCR error on page {page_number}")
        return f"MOCK OCR Seite {page_number}"


def parse_mock_fail(spec: str) -> tuple[set[int], bool]:
    pages: set[int] = set()
    classify_fail = False
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        if part == "classify":
            classify_fail = True
        elif part.startswith("ocr:"):
            pages.add(int(part[4:]))
    return pages, classify_fail
