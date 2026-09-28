"""Synthetic German acceptance corpus (all names, numbers and addresses are invented)."""

from __future__ import annotations

from heftig.providers import registry

from .conftest import FakeExtractor, ScriptedClassifier, ingest_bytes, process_all
from .helpers import scan_pdf, text_pdf

DOCS = [
    (
        "telekom_2026_09.pdf",
        "Telekom Deutschland GmbH\nIhre Rechnung für September 2026\nRechnungsdatum: 03.09.2026\n"
        "Kundennummer: 5566778899\nRechnungsbetrag: 39,95 EUR\nTarif MagentaMobil M",
        {
            "title": "Mobilfunkrechnung September 2026",
            "document_date": "2026-09-03",
            "document_date_evidence": "03.09.2026",
            "document_date_confidence": 0.95,
            "correspondent": "Telekom Deutschland GmbH",
            "correspondent_confidence": 0.95,
            "document_type": "Rechnung",
            "document_type_confidence": 0.95,
            "tags": ["Telefon"],
            "summary": "Mobilfunkrechnung der Telekom für September 2026 über 39,95 EUR.",
            "custom_fields": [
                {
                    "key": "Betrag",
                    "type": "monetary",
                    "value": "39,95",
                    "currency": "EUR",
                    "evidence": "Rechnungsbetrag: 39,95 EUR",
                },
            ],
        },
    ),
    (
        "telekom_2026_08.pdf",
        "Telekom Deutschland GmbH\nIhre Rechnung für August 2026\nRechnungsdatum: 04.08.2026\n"
        "Kundennummer: 5566778899\nRechnungsbetrag: 44,95 EUR",
        {
            "title": "Mobilfunkrechnung August 2026",
            "document_date": "2026-08-04",
            "document_date_evidence": "04.08.2026",
            "document_date_confidence": 0.95,
            "correspondent": "Telekom Deutschland GmbH",
            "correspondent_confidence": 0.95,
            "document_type": "Rechnung",
            "document_type_confidence": 0.95,
            "tags": ["Telefon"],
            "custom_fields": [
                {
                    "key": "Betrag",
                    "type": "monetary",
                    "value": "44,95",
                    "currency": "EUR",
                    "evidence": "Rechnungsbetrag: 44,95 EUR",
                },
            ],
        },
    ),
    (
        "vodafone_vertrag.pdf",
        "Vodafone GmbH\nVertragsbestätigung Kabel-Internet\nVertragsnummer: 83729381\n"
        "Datum: 12.03.2025\nVielen Dank für Ihren Auftrag.",
        {
            "title": "Vertragsbestätigung Kabel-Internet",
            "document_date": "2025-03-12",
            "document_date_evidence": "12.03.2025",
            "document_date_confidence": 0.9,
            "correspondent": "Vodafone GmbH",
            "correspondent_confidence": 0.9,
            "document_type": "Vertrag",
            "document_type_confidence": 0.9,
            "tags": ["Internet"],
            "custom_fields": [
                {
                    "key": "Vertragsnummer",
                    "type": "string",
                    "value": "83729381",
                    "evidence": "Vertragsnummer: 83729381",
                },
            ],
        },
    ),
    (
        "vodafone_brief.pdf",
        "Vodafone GmbH\nInformation zu Ihrem Vertrag 8372 9381\nDatum: 01.06.2025\n"
        "Ab Juli ändern sich unsere Servicezeiten.",
        {
            "title": "Information Servicezeiten",
            "document_date": "2025-06-01",
            "document_date_evidence": "01.06.2025",
            "document_date_confidence": 0.9,
            "correspondent": "Vodafone GmbH",
            "correspondent_confidence": 0.9,
            "document_type": "Brief",
            "document_type_confidence": 0.8,
        },
    ),
    (
        "stadtwerke_abschlag.pdf",
        "Stadtwerke Beispielstadt\nAbschlagsplan Strom 2025\nKundennummer 837293812\n"
        "Datum: 15.01.2025\nAbschlag: 85,00 EUR monatlich",
        {
            "title": "Abschlagsplan Strom 2025",
            "document_date": "2025-01-15",
            "document_date_evidence": "15.01.2025",
            "document_date_confidence": 0.9,
            "correspondent": "Stadtwerke Beispielstadt",
            "correspondent_confidence": 0.9,
            "document_type": "Abschlagsplan",
            "document_type_confidence": 0.9,
            "tags": ["Strom"],
            "custom_fields": [
                {
                    "key": "Betrag",
                    "type": "monetary",
                    "value": "85,00",
                    "currency": "EUR",
                    "evidence": "Abschlag: 85,00 EUR",
                },
            ],
        },
    ),
    (
        "allianz_hausrat_2025.pdf",
        "Allianz Versicherungs-AG\nBeitragsrechnung Hausratversicherung\nVersicherungsjahr 2025\n"
        "Datum: 02.01.2025\nBeitrag: 123,40 EUR",
        {
            "title": "Beitragsrechnung Hausrat 2025",
            "document_date": "2025-01-02",
            "document_date_evidence": "02.01.2025",
            "document_date_confidence": 0.95,
            "correspondent": "Allianz Versicherungs-AG",
            "correspondent_confidence": 0.95,
            "document_type": "Versicherungsschein",
            "document_type_confidence": 0.9,
            "tags": ["Versicherung"],
            "custom_fields": [
                {
                    "key": "Betrag",
                    "type": "monetary",
                    "value": "123,40",
                    "currency": "EUR",
                    "evidence": "Beitrag: 123,40 EUR",
                },
            ],
        },
    ),
    (
        "allianz_kfz_2023.pdf",
        "Allianz Versicherungs-AG\nVersicherungsschein Kfz-Versicherung\nDatum: 10.02.2023\n"
        "Gültig bis 31.12.2025",
        {
            "title": "Versicherungsschein Kfz",
            "document_date": "2023-02-10",
            "document_date_evidence": "10.02.2023",
            "document_date_confidence": 0.95,
            "correspondent": "Allianz Versicherungs-AG",
            "correspondent_confidence": 0.95,
            "document_type": "Versicherungsschein",
            "document_type_confidence": 0.9,
            "tags": ["Versicherung", "Auto"],
        },
    ),
    (
        "tickets.pdf",
        "FC Beispielstadt e.V.\nTicketbestätigung Heimspiel\nAuswärtsfahrt zur Allianz Arena, Saison 2025\n"
        "Datum: 20.08.2025\nEine Versicherung für Ihre Tickets ist nicht enthalten.",
        {
            "title": "Ticketbestätigung",
            "document_date": "2025-08-20",
            "document_date_evidence": "20.08.2025",
            "document_date_confidence": 0.9,
            "correspondent": "FC Beispielstadt e.V.",
            "correspondent_confidence": 0.9,
            "document_type": "Ticket",
            "document_type_confidence": 0.9,
            "tags": ["Freizeit"],
        },
    ),
    (
        "finanzamt_bescheid.pdf",
        "Finanzamt Beispielstadt-Süd\nBescheid für 2024 über Einkommensteuer\n"
        "Datum: 14.07.2025\nGrundstück Müllerstraße 5, Flurstück 12/3",
        {
            "title": "Einkommensteuerbescheid 2024",
            "document_date": "2025-07-14",
            "document_date_evidence": "14.07.2025",
            "document_date_confidence": 0.95,
            "correspondent": "Finanzamt Beispielstadt-Süd",
            "correspondent_confidence": 0.95,
            "document_type": "Bescheid",
            "document_type_confidence": 0.95,
            "tags": ["Steuer"],
        },
    ),
]

# image-only scan: text comes from (fake) OCR, with a word broken across lines
SCAN_NAME = "scan_krankenkasse.pdf"
SCAN_OCR = {
    1: "Gesundheitskasse Beispiel\nMitgliedsbescheinigung für die Kranken-\nversicherung\n"
    "Datum: 05.06.2024",
    2: "Seite 2 – Hinweise zum Datenschutz",
}
SCAN_META = {
    "title": "Mitgliedsbescheinigung",
    "document_date": "2024-06-05",
    "document_date_evidence": "05.06.2024",
    "document_date_confidence": 0.9,
    "correspondent": "Gesundheitskasse Beispiel",
    "correspondent_confidence": 0.9,
    "document_type": "Bescheinigung",
    "document_type_confidence": 0.9,
    "tags": ["Versicherung"],
}


def load_corpus(archive) -> dict[str, str]:
    """Ingest + process the corpus. Returns filename -> document id."""
    by_name = {name: meta for name, _, meta in DOCS}
    by_name[SCAN_NAME] = SCAN_META
    registry.override(
        extractor=FakeExtractor(pages=SCAN_OCR), classifier=ScriptedClassifier(by_filename=by_name)
    )
    ids = {}
    for name, text, _ in DOCS:
        ids[name] = ingest_bytes(archive, text_pdf([text]), name).doc_id
    ids[SCAN_NAME] = ingest_bytes(
        archive, scan_pdf(["(Bild)", "(Bild)"]), SCAN_NAME, source="scanner"
    ).doc_id
    process_all(archive)
    return ids
