"""Search quality benchmark: a German household archive and queries with judged results.

All people, companies, addresses and numbers are invented. The documents are what a household
archive holds (bills, contracts, notices, pay slips, certificates), with the texts the way they
arrive: senders' wording that differs from the words people search with ("Betriebskosten" vs.
"Nebenkosten", "Kraftfahrzeugsteuer" vs. "Kfz-Steuer"), inflected forms, compounds, OCR errors in
scans ("Kündiqung", "Kaltrniete") and a few documents that mention many search words in passing
(newsletters, ads, bank statements) and should not come first.

The metadata is what the classification would have produced; scans that could not be
classified have none, so only their (noisy) text can find them.

`heftig.searcheval` computes the metrics; `tests/test_search_quality.py` holds the thresholds and
`scripts/search_bench.py` prints the full report.
"""

from __future__ import annotations

from heftig.providers import registry

from .conftest import ScriptedClassifier, ingest_bytes, process_all
from .helpers import text_pdf


def _d(
    name: str,
    text: str,
    *,
    title: str = "",
    date: str | None = None,
    corr: str | None = None,
    dtype: str | None = None,
    tags: tuple[str, ...] = (),
    summary: str = "",
    fields: tuple[tuple[str, str, str, str], ...] = (),
    pages: tuple[str, ...] = (),
) -> tuple[str, list[str], dict]:
    """A document: the date is printed in the text as the classification's evidence."""
    first = text
    meta: dict = {"title": title, "tags": list(tags), "summary": summary}
    if date:
        y, m, dd = date.split("-")
        shown = f"{dd}.{m}.{y}"
        first = f"{text}\nDatum: {shown}"
        meta.update(
            document_date=date,
            document_date_evidence=shown,
            document_date_confidence=0.95,
        )
    if corr:
        meta.update(correspondent=corr, correspondent_confidence=0.95)
    if dtype:
        meta.update(document_type=dtype, document_type_confidence=0.9)
    if fields:
        meta["custom_fields"] = [
            {"key": k, "type": t, "value": v, "evidence": ev}
            | ({"currency": "EUR"} if t == "monetary" else {})
            for k, t, v, ev in fields
        ]
    return name, [first, *pages], meta


FILLER = (
    "Bitte bewahren Sie dieses Schreiben für Ihre Unterlagen auf. Bei Fragen erreichen Sie uns "
    "montags bis freitags von 8 bis 18 Uhr. Mit freundlichen Grüßen"
)

DOCS = [
    # --- telephone, internet ------------------------------------------------------------
    _d(
        "telekom_2024_05.pdf",
        "Telekom Deutschland GmbH\nIhre Rechnung für Mai 2024\nMobilfunk Tarif MagentaMobil M\n"
        f"Kundennummer: 5566778899\nRechnungsbetrag: 39,95 EUR\n{FILLER}",
        title="Mobilfunkrechnung Mai 2024",
        date="2024-05-03",
        corr="Telekom Deutschland GmbH",
        dtype="Rechnung",
        tags=("Telefon",),
        summary="Mobilfunkrechnung der Telekom für Mai 2024 über 39,95 EUR.",
        fields=(("Betrag", "monetary", "39,95", "Rechnungsbetrag: 39,95 EUR"),),
    ),
    _d(
        "telekom_2024_06.pdf",
        "Telekom Deutschland GmbH\nIhre Rechnung für Juni 2024\nMobilfunk Tarif MagentaMobil M\n"
        f"Kundennummer: 5566778899\nRechnungsbetrag: 42,10 EUR\n{FILLER}",
        title="Mobilfunkrechnung Juni 2024",
        date="2024-06-04",
        corr="Telekom Deutschland GmbH",
        dtype="Rechnung",
        tags=("Telefon",),
        fields=(("Betrag", "monetary", "42,10", "Rechnungsbetrag: 42,10 EUR"),),
    ),
    _d(
        "telekom_festnetz_2024.pdf",
        "Telekom Deutschland GmbH\nIhre Rechnung für Juni 2024\nFestnetz und Internet "
        "MagentaZuhause L\nKundennummer: 5566778899\nRechnungsbetrag: 49,95 EUR",
        title="Rechnung Festnetz und Internet Juni 2024",
        date="2024-06-04",
        corr="Telekom Deutschland GmbH",
        dtype="Rechnung",
        tags=("Internet",),
    ),
    _d(
        "vodafone_kuendigung.pdf",
        "Vodafone GmbH\nKündigungsbestätigung\nWir bestätigen die Kündigung Ihres "
        "Mobilfunkvertrags zum 30.11.2023.\nVertragsnummer: 83729381\nIhre Rufnummer wird "
        "zum Vertragsende abgeschaltet.",
        title="Kündigungsbestätigung Mobilfunkvertrag",
        date="2023-10-12",
        corr="Vodafone GmbH",
        dtype="Kündigung",
        tags=("Telefon",),
        fields=(("Vertragsnummer", "string", "83729381", "Vertragsnummer: 83729381"),),
    ),
    _d(
        "vodafone_vertrag.pdf",
        "Vodafone GmbH\nVertragsbestätigung Kabel-Internet 250\nVielen Dank für Ihren Auftrag. "
        "Ihr Anschluss wird am 15.03.2025 freigeschaltet.\nMonatlicher Grundpreis 39,99 EUR",
        title="Vertragsbestätigung Kabel-Internet",
        date="2025-03-02",
        corr="Vodafone GmbH",
        dtype="Vertrag",
        tags=("Internet",),
    ),
    _d(
        "handy_kauf.pdf",
        "Elektro-Discount Beispielstadt\nKaufbeleg\n1 x Smartphone Galaxy S24 128 GB schwarz "
        "799,00 EUR\nInkl. 19 % MwSt.\n24 Monate Gewährleistung",
        title="Kaufbeleg Smartphone Galaxy S24",
        date="2024-02-17",
        corr="Elektro-Discount Beispielstadt",
        dtype="Kaufbeleg",
        tags=("Elektronik",),
    ),
    # --- energy, water, waste ---------------------------------------------------------------
    _d(
        "stadtwerke_jahresabrechnung_2023.pdf",
        "Stadtwerke Beispielstadt\nJahresabrechnung 2023 für Ihre Stromlieferung\n"
        "Verbrauch 01.01.2023 bis 31.12.2023: 2.845 kWh\nArbeitspreis 32,1 ct/kWh\n"
        "Abrechnungsbetrag 1.012,40 EUR, abzüglich Abschläge 1.020,00 EUR\nGuthaben 7,60 EUR",
        title="Jahresabrechnung Strom 2023",
        date="2024-01-19",
        corr="Stadtwerke Beispielstadt",
        dtype="Abrechnung",
        tags=("Strom",),
        summary="Jahresabrechnung Strom 2023 mit einem Guthaben von 7,60 EUR.",
    ),
    _d(
        "stadtwerke_abschlag_2024.pdf",
        "Stadtwerke Beispielstadt\nAbschlagsplan Strom 2024\nIhr monatlicher Abschlag ab "
        "Februar 2024: 85,00 EUR\nFälligkeit jeweils zum 15. des Monats",
        title="Abschlagsplan Strom 2024",
        date="2024-01-19",
        corr="Stadtwerke Beispielstadt",
        dtype="Abschlagsplan",
        tags=("Strom",),
    ),
    _d(
        "gas_2023.pdf",
        "EnergieNord AG\nRechnung Erdgas 2023\nGasverbrauch 11.230 kWh\nNachzahlung 212,00 EUR\n"
        "Neuer Abschlag für Ihre Heizung ab März: 140,00 EUR",
        title="Gasrechnung 2023",
        date="2024-02-08",
        corr="EnergieNord AG",
        dtype="Rechnung",
        tags=("Gas",),
    ),
    _d(
        "wasser_2023.pdf",
        "Zweckverband Wasserversorgung Beispielstadt\nGebührenbescheid 2023\nTrinkwasser 96 m³\n"
        "Schmutzwasser und Abwasser 96 m³\nNiederschlagswasser\nGesamt 412,50 EUR",
        title="Gebührenbescheid Wasser 2023",
        date="2024-02-20",
        corr="Zweckverband Wasserversorgung Beispielstadt",
        dtype="Bescheid",
        tags=("Wasser",),
    ),
    _d(
        "muell_2024.pdf",
        "Stadt Beispielstadt – Abfallwirtschaft\nBescheid über Abfallgebühren 2024\n"
        "Restmülltonne 120 l, 14-tägliche Leerung 186,00 EUR\nBiotonne 48,00 EUR",
        title="Bescheid Abfallgebühren 2024",
        date="2024-01-10",
        corr="Stadt Beispielstadt",
        dtype="Bescheid",
        tags=("Wohnung",),
    ),
    # --- flat ---------------------------------------------------------------------------------
    _d(
        "mietvertrag.pdf",
        "Mietvertrag über Wohnraum\nVermieter: Hausverwaltung Sonnenhof GmbH\nMieter: Alex "
        "Beispiel\nWohnung Lindenweg 12, 3. OG links, 3 Zimmer, 78 m²\nKaltmiete 780,00 EUR\n"
        "Vorauszahlung Betriebskosten 180,00 EUR, Heizkosten 60,00 EUR\nKaution: drei Kaltmieten",
        title="Mietvertrag Lindenweg 12",
        date="2021-03-01",
        corr="Hausverwaltung Sonnenhof GmbH",
        dtype="Vertrag",
        tags=("Wohnung",),
    ),
    _d(
        "betriebskosten_2023.pdf",
        "Hausverwaltung Sonnenhof GmbH\nBetriebskostenabrechnung für das Jahr 2023\nObjekt "
        "Lindenweg 12\nGrundsteuer, Wasser, Müllabfuhr, Hausmeister, Gartenpflege, "
        "Treppenhausreinigung\nHeizung und Warmwasser nach Verbrauch\nIhre Vorauszahlungen "
        "2.880,00 EUR\nNachzahlung 143,20 EUR",
        title="Betriebskostenabrechnung 2023",
        date="2024-06-12",
        corr="Hausverwaltung Sonnenhof GmbH",
        dtype="Abrechnung",
        tags=("Wohnung",),
        fields=(("Betrag", "monetary", "143,20", "Nachzahlung 143,20 EUR"),),
    ),
    _d(
        "mieterhoehung_2024.pdf",
        "Hausverwaltung Sonnenhof GmbH\nMieterhöhungsverlangen nach § 558 BGB\nWir bitten um "
        "Zustimmung zur Erhöhung der Kaltmiete von 780,00 EUR auf 820,00 EUR ab 01.09.2024.\n"
        "Begründung: Mietspiegel der Stadt Beispielstadt",
        title="Mieterhöhung ab September 2024",
        date="2024-06-20",
        corr="Hausverwaltung Sonnenhof GmbH",
        dtype="Brief",
        tags=("Wohnung",),
    ),
    # an unclassified scan with OCR errors ("rn" for "m")
    _d(
        "scan_mietbescheinigung.pdf",
        "Hausverwaltunq Sonnenhof\nBescheiniqung\nHiermit bestätiqen wir den Einqang der "
        "Kaltrniete in Höhe von 780,00 EUR für die Wohnunq Lindenweq 12\nfür die Monate "
        "Januar bis Dezernber 2023.",
    ),
    # --- insurance --------------------------------------------------------------------------
    _d(
        "allianz_hausrat_2025.pdf",
        "Allianz Versicherungs-AG\nBeitragsrechnung Hausratversicherung\nVersicherungsjahr "
        "2025\nVersicherungssumme 65.000 EUR\nBeitrag: 123,40 EUR",
        title="Beitragsrechnung Hausrat 2025",
        date="2025-01-02",
        corr="Allianz Versicherungs-AG",
        dtype="Beitragsrechnung",
        tags=("Versicherung",),
    ),
    _d(
        "allianz_haftpflicht.pdf",
        "Allianz Versicherungs-AG\nVersicherungsschein Privathaftpflichtversicherung\n"
        "Versicherungsnummer AS-4471-2290\nDeckungssumme 50 Mio. EUR pauschal\nJahresbeitrag "
        "68,90 EUR",
        title="Versicherungsschein Privathaftpflicht",
        date="2022-04-01",
        corr="Allianz Versicherungs-AG",
        dtype="Versicherungsschein",
        tags=("Versicherung",),
    ),
    _d(
        "huk_kfz_2024.pdf",
        "HUK-COBURG\nBeitragsrechnung Kraftfahrtversicherung 2024\nKfz-Haftpflicht und "
        "Teilkasko für Ihr Fahrzeug VW Golf, amtliches Kennzeichen BS-AB 123\n"
        "Schadenfreiheitsklasse SF 12\nJahresbeitrag 412,00 EUR",
        title="Kfz-Versicherung Beitragsrechnung 2024",
        date="2023-12-01",
        corr="HUK-COBURG",
        dtype="Beitragsrechnung",
        tags=("Auto", "Versicherung"),
    ),
    _d(
        "werbung_hausrat.pdf",
        "Versicherungsmakler Schnell\nJetzt wechseln und sparen!\nIhre neue Hausratversicherung "
        "ab 3,90 EUR im Monat. Auch Haftpflicht, Kfz-Versicherung und Rechtsschutz "
        "günstig bei uns. Rufen Sie an!",
        title="Werbung Hausratversicherung",
        date="2024-03-05",
        corr="Versicherungsmakler Schnell",
        dtype="Werbung",
    ),
    # an unclassified scan with OCR errors ("q" for "g")
    _d(
        "scan_wohngebaeude.pdf",
        "Beispiel Versicherunq AG\nVersicherunqsschein Wohngebäudeversicherunq\nVersichertes "
        "Gebäude: Lindenweq 12\nJahresbeitraq 344,00 EUR\nLeitunqswasser, Sturm, Haqel",
    ),
    # --- car ----------------------------------------------------------------------------------
    _d(
        "kfz_steuer.pdf",
        "Hauptzollamt Beispielstadt\nBescheid über Kraftfahrzeugsteuer\nAmtliches Kennzeichen "
        "BS-AB 123\nDie Steuer wird ab 01.05.2023 auf jährlich 136,00 EUR festgesetzt.",
        title="Kraftfahrzeugsteuerbescheid",
        date="2023-04-18",
        corr="Hauptzollamt Beispielstadt",
        dtype="Bescheid",
        tags=("Auto", "Steuer"),
    ),
    _d(
        "tuev_2024.pdf",
        "TÜV Süd Auto Service\nUntersuchungsbericht Hauptuntersuchung nach § 29 StVZO\n"
        "Fahrzeug VW Golf, Kennzeichen BS-AB 123\nErgebnis: ohne festgestellte Mängel\n"
        "Nächste HU: 05/2026",
        title="Hauptuntersuchung VW Golf",
        date="2024-05-14",
        corr="TÜV Süd",
        dtype="Prüfbericht",
        tags=("Auto",),
    ),
    _d(
        "werkstatt_2024.pdf",
        "Autohaus Meier GmbH\nRechnung Nr. 2024-1187\nInspektion nach Herstellervorgabe, "
        "Ölwechsel, Bremsflüssigkeit\nReifenwechsel Sommerreifen\nSumme 389,70 EUR",
        title="Rechnung Inspektion VW Golf",
        date="2024-04-03",
        corr="Autohaus Meier GmbH",
        dtype="Rechnung",
        tags=("Auto",),
    ),
    # --- health -------------------------------------------------------------------------------
    _d(
        "tk_mitglied.pdf",
        "Techniker Krankenkasse\nMitgliedsbescheinigung\nHiermit bestätigen wir die "
        "Mitgliedschaft in der Kranken- und Pflegeversicherung seit 01.01.2019.",
        title="Mitgliedsbescheinigung",
        date="2024-02-02",
        corr="Techniker Krankenkasse",
        dtype="Bescheinigung",
        tags=("Gesundheit",),
    ),
    _d(
        "tk_beitrag.pdf",
        "Techniker Krankenkasse\nBeitragsbescheid für die freiwillige Krankenversicherung\n"
        "Beitrag ab 01.01.2024 monatlich 231,40 EUR einschließlich Zusatzbeitrag",
        title="Beitragsbescheid freiwillige Krankenversicherung 2024",
        date="2023-12-15",
        corr="Techniker Krankenkasse",
        dtype="Bescheid",
        tags=("Gesundheit", "Versicherung"),
    ),
    _d(
        "zahnarzt_2024.pdf",
        "Zahnarztpraxis Dr. Weber\nRechnung nach GOZ\nProfessionelle Zahnreinigung, "
        "Füllung Zahn 36\nRechnungsbetrag 184,50 EUR",
        title="Rechnung Zahnreinigung",
        date="2024-03-11",
        corr="Zahnarztpraxis Dr. Weber",
        dtype="Rechnung",
        tags=("Gesundheit",),
    ),
    _d(
        "hausarzt_au.pdf",
        "Praxis Dr. med. Lange, Fachärztin für Allgemeinmedizin\nArbeitsunfähigkeitsbescheinigung"
        "\nErstbescheinigung\nArbeitsunfähig seit 04.11.2024 voraussichtlich bis 08.11.2024\n"
        "Ihr behandelnder Arzt",
        title="Arbeitsunfähigkeitsbescheinigung November 2024",
        date="2024-11-04",
        corr="Praxis Dr. med. Lange",
        dtype="Bescheinigung",
        tags=("Gesundheit",),
    ),
    _d(
        "klinikum_brief.pdf",
        "Klinikum Beispielstadt, Klinik für Unfallchirurgie\nEntlassungsbrief\nAufenthalt vom "
        "12.08.2023 bis 15.08.2023\nDiagnose: Distorsion des oberen Sprunggelenks\n"
        "Empfehlung: Physiotherapie",
        title="Entlassungsbrief Unfallchirurgie",
        date="2023-08-15",
        corr="Klinikum Beispielstadt",
        dtype="Arztbrief",
        tags=("Gesundheit",),
    ),
    # --- work, income -------------------------------------------------------------------------
    _d(
        "lohn_2024_03.pdf",
        "Muster Software GmbH\nEntgeltabrechnung März 2024\nBruttobezüge 4.200,00 EUR\n"
        "Lohnsteuer 612,33 EUR, Solidaritätszuschlag 0,00 EUR\nSozialversicherung 863,10 EUR\n"
        "Auszahlungsbetrag 2.724,57 EUR",
        title="Gehaltsabrechnung März 2024",
        date="2024-03-28",
        corr="Muster Software GmbH",
        dtype="Lohnabrechnung",
        tags=("Arbeit",),
    ),
    _d(
        "lohn_2024_04.pdf",
        "Muster Software GmbH\nEntgeltabrechnung April 2024\nBruttobezüge 4.200,00 EUR\n"
        "Lohnsteuer 612,33 EUR\nSozialversicherung 863,10 EUR\nAuszahlungsbetrag 2.724,57 EUR",
        title="Gehaltsabrechnung April 2024",
        date="2024-04-26",
        corr="Muster Software GmbH",
        dtype="Lohnabrechnung",
        tags=("Arbeit",),
    ),
    _d(
        "lohnsteuerbescheinigung_2023.pdf",
        "Muster Software GmbH\nAusdruck der elektronischen Lohnsteuerbescheinigung für 2023\n"
        "Bruttoarbeitslohn 50.400,00 EUR\nEinbehaltene Lohnsteuer 7.347,96 EUR",
        title="Lohnsteuerbescheinigung 2023",
        date="2024-01-31",
        corr="Muster Software GmbH",
        dtype="Bescheinigung",
        tags=("Arbeit", "Steuer"),
    ),
    _d(
        "arbeitsvertrag.pdf",
        "Arbeitsvertrag\nzwischen der Muster Software GmbH und Alex Beispiel\nBeginn des "
        "Arbeitsverhältnisses: 01.04.2022\nTätigkeit: Softwareentwicklerin\nBruttomonatsgehalt "
        "4.000,00 EUR\nProbezeit sechs Monate\nUrlaubsanspruch 30 Arbeitstage",
        title="Arbeitsvertrag Muster Software",
        date="2022-02-15",
        corr="Muster Software GmbH",
        dtype="Vertrag",
        tags=("Arbeit",),
    ),
    _d(
        "arbeitszeugnis.pdf",
        "Zwischenzeugnis\nFrau Alex Beispiel ist seit dem 01.04.2022 als Softwareentwicklerin "
        "in unserem Unternehmen tätig. Sie erledigte ihre Aufgaben stets zu unserer vollsten "
        "Zufriedenheit.",
        title="Zwischenzeugnis Muster Software",
        date="2024-09-30",
        corr="Muster Software GmbH",
        dtype="Zeugnis",
        tags=("Arbeit",),
    ),
    # --- family -------------------------------------------------------------------------------
    _d(
        "schulzeugnis_mia.pdf",
        "Grundschule am Park\nZeugnis Schuljahr 2023/24, 2. Halbjahr\nMia Beispiel, Klasse 3b\n"
        "Deutsch: gut, Mathematik: sehr gut, Sachunterricht: gut\nUnterschrift der "
        "Erziehungsberechtigten des Kindes",
        title="Zeugnis Mia Klasse 3",
        date="2024-07-10",
        corr="Grundschule am Park",
        dtype="Zeugnis",
        tags=("Familie", "Schule"),
    ),
    _d(
        "kita_2024.pdf",
        "Stadt Beispielstadt – Jugendamt\nFestsetzung des Elternbeitrags für die Betreuung in "
        "der Kindertagesstätte\nKind: Paul Beispiel\nMonatlicher Elternbeitrag 212,00 EUR ab "
        "01.08.2024",
        title="Elternbeitrag Kita 2024",
        date="2024-07-01",
        corr="Stadt Beispielstadt",
        dtype="Bescheid",
        tags=("Familie",),
    ),
    _d(
        "kindergeld.pdf",
        "Familienkasse Niedersachsen-Bremen\nBescheid über Kindergeld\nFür Ihre Kinder Mia und "
        "Paul wird Kindergeld in Höhe von jeweils 250,00 EUR monatlich festgesetzt.",
        title="Kindergeldbescheid",
        date="2023-01-20",
        corr="Familienkasse Niedersachsen-Bremen",
        dtype="Bescheid",
        tags=("Familie",),
    ),
    _d(
        "elterngeld.pdf",
        "Elterngeldstelle Beispielstadt\nBewilligungsbescheid\nElterngeld für Paul Beispiel "
        "für den 1. bis 12. Lebensmonat\nBasiselterngeld monatlich 1.430,00 EUR",
        title="Bewilligung Elterngeld",
        date="2021-05-06",
        corr="Elterngeldstelle Beispielstadt",
        dtype="Bescheid",
        tags=("Familie",),
    ),
    _d(
        "geburtsurkunde_mia.pdf",
        "Standesamt Beispielstadt\nGeburtsurkunde\nMia Beispiel\ngeboren am 14.02.2016 in "
        "Beispielstadt",
        title="Geburtsurkunde Mia",
        date="2016-02-20",
        corr="Standesamt Beispielstadt",
        dtype="Urkunde",
        tags=("Familie",),
    ),
    _d(
        "heiratsurkunde.pdf",
        "Standesamt Beispielstadt\nEheurkunde\nDie Ehe wurde am 21.06.2014 geschlossen zwischen "
        "Alex Beispiel und Kim Beispiel",
        title="Eheurkunde",
        date="2014-06-21",
        corr="Standesamt Beispielstadt",
        dtype="Urkunde",
        tags=("Familie",),
    ),
    # --- tax ----------------------------------------------------------------------------------
    _d(
        "est_bescheid_2023.pdf",
        "Finanzamt Beispielstadt\nBescheid für 2023 über Einkommensteuer, Solidaritätszuschlag "
        "und Kirchensteuer\nFestgesetzt werden: Einkommensteuer 6.113,00 EUR\n"
        "Erstattung 1.234,00 EUR\nDer Betrag wird auf Ihr Konto überwiesen.",
        title="Einkommensteuerbescheid 2023",
        date="2024-08-22",
        corr="Finanzamt Beispielstadt",
        dtype="Bescheid",
        tags=("Steuer",),
    ),
    _d(
        "est_bescheid_2022.pdf",
        "Finanzamt Beispielstadt\nBescheid für 2022 über Einkommensteuer und "
        "Solidaritätszuschlag\nFestgesetzt werden: Einkommensteuer 5.870,00 EUR\n"
        "Nachzahlung 310,00 EUR",
        title="Einkommensteuerbescheid 2022",
        date="2023-07-11",
        corr="Finanzamt Beispielstadt",
        dtype="Bescheid",
        tags=("Steuer",),
    ),
    _d(
        "steuererklaerung_2023.pdf",
        "Einkommensteuererklärung 2023\nHauptvordruck ESt 1 A\nSteuernummer 12/345/67890\n"
        "Anlage N: Einkünfte aus nichtselbständiger Arbeit, Muster Software GmbH",
        title="Einkommensteuererklärung 2023",
        date="2024-05-30",
        dtype="Steuererklärung",
        tags=("Steuer",),
        pages=(
            "Anlage N – Werbungskosten\nEntfernungspauschale 22 km an 180 Tagen\nArbeitsmittel "
            "Laptop 1.200,00 EUR\nHäusliches Arbeitszimmer / Homeoffice-Pauschale\n"
            "Steuerberatungskosten, Kontoführung",
            "Anlage Vorsorgeaufwand\nBeiträge zur Krankenversicherung und Pflegeversicherung\n"
            "Beiträge zur Haftpflichtversicherung und Kfz-Haftpflicht\nRentenversicherung",
            "Anlage Kind: Mia und Paul\nKinderbetreuungskosten Kita 2.544,00 EUR\n"
            "Anlage Haushaltsnahe Aufwendungen: Handwerkerleistungen, Schornsteinfeger",
        ),
    ),
    _d(
        "grundsteuer_2024.pdf",
        "Stadt Beispielstadt – Steueramt\nGrundsteuerbescheid 2024\nGrundstück Lindenweg 12, "
        "Flur 3, Flurstück 12/3\nGrundsteuer B, Hebesatz 480 %\nJahresbetrag 96,00 EUR",
        title="Grundsteuerbescheid 2024",
        date="2024-01-15",
        corr="Stadt Beispielstadt",
        dtype="Bescheid",
        tags=("Steuer", "Wohnung"),
    ),
    _d(
        "hundesteuer.pdf",
        "Stadt Beispielstadt – Steueramt\nHundesteuerbescheid\nHund: Bello, Rasse Labrador\n"
        "Jahressteuer 120,00 EUR, fällig zum 01.07.",
        title="Hundesteuerbescheid",
        date="2022-06-01",
        corr="Stadt Beispielstadt",
        dtype="Bescheid",
        tags=("Hund", "Steuer"),
    ),
    _d(
        "rundfunkbeitrag.pdf",
        "ARD ZDF Deutschlandradio Beitragsservice\nZahlungsaufforderung Rundfunkbeitrag\n"
        "Beitragsnummer 123 456 789\nFür das Quartal Oktober bis Dezember 2024: 55,08 EUR",
        title="Rundfunkbeitrag 4. Quartal 2024",
        date="2024-09-15",
        corr="ARD ZDF Deutschlandradio Beitragsservice",
        dtype="Zahlungsaufforderung",
        tags=("Wohnung",),
    ),
    # --- bank, finance ------------------------------------------------------------------------
    _d(
        "kontoauszug_2024_01.pdf",
        "Sparkasse Beispielstadt\nKontoauszug Nr. 1/2024\nIBAN DE12 3456 7890 1234 5678 90\n"
        "02.01. Hausverwaltung Sonnenhof Miete Januar -960,00\n05.01. Telekom Deutschland "
        "Rechnung -39,95\n29.01. Muster Software GmbH Gehalt +2.724,57",
        title="Kontoauszug Januar 2024",
        date="2024-01-31",
        corr="Sparkasse Beispielstadt",
        dtype="Kontoauszug",
        tags=("Bank",),
    ),
    _d(
        "kontoauszug_2024_02.pdf",
        "Sparkasse Beispielstadt\nKontoauszug Nr. 2/2024\nIBAN DE12 3456 7890 1234 5678 90\n"
        "01.02. Hausverwaltung Sonnenhof Miete Februar -960,00\n15.02. Stadtwerke Abschlag "
        "Strom -85,00\n28.02. Muster Software GmbH Gehalt +2.724,57",
        title="Kontoauszug Februar 2024",
        date="2024-02-29",
        corr="Sparkasse Beispielstadt",
        dtype="Kontoauszug",
        tags=("Bank",),
    ),
    _d(
        "darlehen.pdf",
        "Volksbank Beispielstadt eG\nDarlehensvertrag Immobilienfinanzierung\nDarlehensbetrag "
        "250.000,00 EUR, Sollzins gebunden 3,45 % p.a. für 15 Jahre\nSicherheit: Grundschuld "
        "auf dem Grundstück Lindenweg 12",
        title="Darlehensvertrag Baufinanzierung",
        date="2023-09-01",
        corr="Volksbank Beispielstadt eG",
        dtype="Vertrag",
        tags=("Bank", "Wohnung"),
    ),
    _d(
        "depot_2024.pdf",
        "Beispiel Direktbank AG\nDepotauszug zum 31.12.2024\nMSCI World ETF 142 Stück\n"
        "Kurswert 14.310,00 EUR",
        title="Depotauszug 2024",
        date="2024-12-31",
        corr="Beispiel Direktbank AG",
        dtype="Depotauszug",
        tags=("Bank",),
    ),
    _d(
        "renteninfo_2024.pdf",
        "Deutsche Rentenversicherung Bund\nRenteninformation 2024\nVersicherungsnummer "
        "12 150385 B 123\nIhre bisher erreichte Rentenanwartschaft: 1.050,32 EUR monatlich",
        title="Renteninformation 2024",
        date="2024-07-02",
        corr="Deutsche Rentenversicherung Bund",
        dtype="Renteninformation",
        tags=("Altersvorsorge",),
    ),
    _d(
        "riester.pdf",
        "Beispiel Lebensversicherung AG\nAltersvorsorgevertrag (Riester)\nJährliche "
        "Standmitteilung 2023\nZulagen 2022 gutgeschrieben: 475,00 EUR\nBitte denken Sie an "
        "Ihren Zulagenantrag.",
        title="Riester Standmitteilung 2023",
        date="2024-02-12",
        corr="Beispiel Lebensversicherung AG",
        dtype="Mitteilung",
        tags=("Altersvorsorge",),
    ),
    # --- purchases, travel --------------------------------------------------------------------
    _d(
        "waschmaschine.pdf",
        "Amazon EU S.à r.l.\nRechnung\nBosch Waschmaschine Serie 6, 9 kg, WGG244A40\n649,00 EUR\n"
        "Herstellergarantie 2 Jahre",
        title="Rechnung Waschmaschine Bosch",
        date="2023-11-24",
        corr="Amazon",
        dtype="Rechnung",
        tags=("Haushalt",),
    ),
    _d(
        "fernseher.pdf",
        "MediaMarkt Beispielstadt\nKassenbon\nLG OLED Fernseher 55 Zoll 1.299,00 EUR\n"
        "Garantieverlängerung 5 Jahre 149,00 EUR",
        title="Kaufbeleg Fernseher LG OLED",
        date="2024-11-29",
        corr="MediaMarkt Beispielstadt",
        dtype="Kaufbeleg",
        tags=("Elektronik",),
    ),
    _d(
        "bahncard.pdf",
        "Deutsche Bahn AG\nIhre BahnCard 50, 2. Klasse\nGültig vom 01.10.2024 bis 30.09.2025\n"
        "Preis 244,00 EUR. Das Abo verlängert sich automatisch.",
        title="BahnCard 50 2024/25",
        date="2024-09-10",
        corr="Deutsche Bahn AG",
        dtype="Bestätigung",
        tags=("Reise",),
    ),
    _d(
        "bahn_ticket.pdf",
        "Deutsche Bahn AG\nOnline-Ticket Flexpreis\nHamburg Hbf → München Hbf, ICE 783\n"
        "Fahrkarte gültig am 18.10.2024, 2. Klasse, BahnCard 50 Rabatt\nPreis 76,45 EUR",
        title="Fahrkarte Hamburg – München",
        date="2024-10-02",
        corr="Deutsche Bahn AG",
        dtype="Ticket",
        tags=("Reise",),
    ),
    _d(
        "reise_mallorca.pdf",
        "Sonnenreisen GmbH\nReisebestätigung Pauschalreise\nPalma de Mallorca, Hotel Playa Azul, "
        "7 Nächte Halbpension, 2 Erwachsene, 2 Kinder\nReisepreis 3.480,00 EUR",
        title="Reisebestätigung Mallorca",
        date="2024-03-18",
        corr="Sonnenreisen GmbH",
        dtype="Buchungsbestätigung",
        tags=("Reise",),
    ),
    # --- documents, pets, house -----------------------------------------------------------------
    _d(
        "personalausweis.pdf",
        "Stadt Beispielstadt – Bürgeramt\nAntrag auf Ausstellung eines Personalausweises\n"
        "Ausweisnummer L01X00T47\nGebühr 37,00 EUR",
        title="Antrag Personalausweis",
        date="2023-05-09",
        corr="Stadt Beispielstadt",
        dtype="Antrag",
        tags=("Ausweis",),
    ),
    _d(
        "reisepass.pdf",
        "Stadt Beispielstadt – Bürgeramt\nAbholbenachrichtigung Reisepass\nIhr Reisepass liegt "
        "zur Abholung bereit. Bitte bringen Sie Ihr altes Dokument mit.",
        title="Abholung Reisepass",
        date="2023-06-02",
        corr="Stadt Beispielstadt",
        dtype="Brief",
        tags=("Ausweis",),
    ),
    _d(
        "tierarzt.pdf",
        "Tierarztpraxis am Stadtpark\nRechnung\nPatient: Bello (Labrador)\nJahresimpfung SHPPi "
        "und Tollwut, Entwurmung\nGesamtbetrag 98,40 EUR",
        title="Rechnung Tierarzt Bello",
        date="2024-04-17",
        corr="Tierarztpraxis am Stadtpark",
        dtype="Rechnung",
        tags=("Hund",),
    ),
    _d(
        "elektriker.pdf",
        "Elektro Schulz GmbH\nRechnung Nr. 5512\nAustausch Sicherungskasten, Einbau FI-Schalter\n"
        "Arbeitslohn 420,00 EUR (Handwerkerleistung nach § 35a EStG)\nMaterial 310,00 EUR",
        title="Rechnung Elektroarbeiten",
        date="2024-09-05",
        corr="Elektro Schulz GmbH",
        dtype="Rechnung",
        tags=("Wohnung",),
    ),
    _d(
        "schornsteinfeger.pdf",
        "Bezirksschornsteinfegermeister Klaus Russ\nFeuerstättenschau und Abgasmessung\n"
        "Gas-Brennwerttherme, Messergebnis in Ordnung\nGebühr 64,20 EUR",
        title="Feuerstättenschau",
        date="2024-10-08",
        corr="Klaus Russ",
        dtype="Rechnung",
        tags=("Wohnung",),
    ),
    # --- cancellations ------------------------------------------------------------------------
    # an unclassified scan with OCR errors
    _d(
        "scan_fitx.pdf",
        "FitX Deutschland GmbH\nBestätiqunq Ihrer Kündiqunq\nIhre Mitqliedschaft im "
        "Fitnessstudio endet zum 31.12.2024.\nWir bedauern, dass Sie uns verlassen.",
    ),
    _d(
        "zeitung_kuendigung.pdf",
        "Beispielstädter Tageblatt\nIhre Kündigung\nWir bestätigen das Ende Ihres Abonnements "
        "zum 30.06.2024.",
        title="Kündigung Zeitungsabonnement",
        date="2024-05-02",
        corr="Beispielstädter Tageblatt",
        dtype="Kündigung",
    ),
    # --- mentions many things in passing --------------------------------------------------------
    _d(
        "newsletter.pdf",
        "Verbraucherzentrale Newsletter\nTipps für Ihre Steuererklärung: Rechnung vom "
        "Handwerker aufheben! Versicherung prüfen: Brauchen Sie wirklich eine "
        "Hausratversicherung? Handytarif und Stromanbieter wechseln spart Geld. Die Deutsche "
        "Rentenversicherung informiert über die Rente. Bahn frei für Ihre Altersvorsorge: "
        "Kündigung alter Verträge.",
        title="Newsletter Verbraucherzentrale",
        date="2024-04-01",
        corr="Verbraucherzentrale",
        dtype="Newsletter",
    ),
]


# (query, {filename: grade}, filenames that must not come before the right ones)
# grade 2: what was searched for; grade 1: also a good answer.
QUERIES: list[tuple[str, dict[str, int], tuple[str, ...]]] = [
    # known documents: sender, type, date
    ("Telekom Rechnung Mai 2024", {"telekom_2024_05.pdf": 2}, ()),
    ("Mobilfunkrechnung", {"telekom_2024_05.pdf": 2, "telekom_2024_06.pdf": 2}, ()),
    ("Allianz Haftpflicht", {"allianz_haftpflicht.pdf": 2}, ()),
    ("Lohnsteuerbescheinigung 2023", {"lohnsteuerbescheinigung_2023.pdf": 2}, ()),
    ("Arbeitsvertrag", {"arbeitsvertrag.pdf": 2}, ()),
    ("Mietvertrag", {"mietvertrag.pdf": 2}, ()),
    ("Kindergeld", {"kindergeld.pdf": 2}, ()),
    ("Elterngeld", {"elterngeld.pdf": 2}, ()),
    ("Grundsteuer", {"grundsteuer_2024.pdf": 2}, ()),
    ("Hundesteuer", {"hundesteuer.pdf": 2}, ()),
    ("Personalausweis", {"personalausweis.pdf": 2}, ()),
    ("Geburtsurkunde Mia", {"geburtsurkunde_mia.pdf": 2}, ()),
    ("Waschmaschine", {"waschmaschine.pdf": 2}, ()),
    ("Garantie Fernseher", {"fernseher.pdf": 2}, ()),
    ("Bahncard", {"bahncard.pdf": 2}, ()),
    ("Schornsteinfeger", {"schornsteinfeger.pdf": 2}, ()),
    ("Gasrechnung", {"gas_2023.pdf": 2}, ()),
    ("Abwasser", {"wasser_2023.pdf": 2}, ()),
    ("Pflegeversicherung", {"tk_mitglied.pdf": 2, "steuererklaerung_2023.pdf": 1}, ()),
    ("Einkommensteuererklärung", {"steuererklaerung_2023.pdf": 2}, ()),
    ("Steuererklärung 2023", {"steuererklaerung_2023.pdf": 2}, ("newsletter.pdf",)),
    ("Steuerbescheid 2023", {"est_bescheid_2023.pdf": 2, "est_bescheid_2022.pdf": 1}, ()),
    ("Gehaltsabrechnung März 2024", {"lohn_2024_03.pdf": 2}, ()),
    ("Kontoauszug Januar 2024", {"kontoauszug_2024_01.pdf": 2}, ()),
    ("Deutsche Bahn Ticket", {"bahn_ticket.pdf": 2, "bahncard.pdf": 1}, ()),
    ("Kfz Versicherung", {"huk_kfz_2024.pdf": 2}, ()),
    ("Krankenkasse", {"tk_mitglied.pdf": 2, "tk_beitrag.pdf": 2}, ()),
    ("Krankenversicherung Beitrag", {"tk_beitrag.pdf": 2}, ()),
    ("Zahnarzt Rechnung", {"zahnarzt_2024.pdf": 2}, ()),
    ("Tierarzt Hund", {"tierarzt.pdf": 2}, ()),
    ("Riester Zulage", {"riester.pdf": 2}, ()),
    ("Haftpflicht Versicherung", {"allianz_haftpflicht.pdf": 2, "huk_kfz_2024.pdf": 1}, ()),
    ("Klinik", {"klinikum_brief.pdf": 2}, ()),
    # words from the text
    ("Nachzahlung Nebenkosten", {"betriebskosten_2023.pdf": 2}, ()),
    ("Erstattung Finanzamt", {"est_bescheid_2023.pdf": 2}, ()),
    ("Sicherungskasten", {"elektriker.pdf": 2}, ()),
    ("Probezeit", {"arbeitsvertrag.pdf": 2}, ()),
    ("Impfung Hund", {"tierarzt.pdf": 2}, ()),
    ("Kennzeichen BS-AB 123", {"kfz_steuer.pdf": 2, "huk_kfz_2024.pdf": 2, "tuev_2024.pdf": 2}, ()),
    # numbers
    ("83729381", {"vodafone_kuendigung.pdf": 2}, ()),
    (
        "5566778899",
        {"telekom_2024_05.pdf": 2, "telekom_2024_06.pdf": 2, "telekom_festnetz_2024.pdf": 2},
        (),
    ),
    ("DE12345678901234567890", {"kontoauszug_2024_01.pdf": 2, "kontoauszug_2024_02.pdf": 2}, ()),
    # inflected forms (plural, umlaut plural, dative)
    ("Mietverträge", {"mietvertrag.pdf": 2}, ()),
    (
        "Kontoauszüge",
        {"kontoauszug_2024_01.pdf": 2, "kontoauszug_2024_02.pdf": 2, "depot_2024.pdf": 1},
        (),
    ),
    ("Zeugnisse", {"arbeitszeugnis.pdf": 2, "schulzeugnis_mia.pdf": 2}, ()),
    ("Urkunden", {"geburtsurkunde_mia.pdf": 2, "heiratsurkunde.pdf": 2}, ()),
    (
        "Kindern",
        {"kindergeld.pdf": 2, "schulzeugnis_mia.pdf": 1, "kita_2024.pdf": 1},
        (),
    ),
    (
        "Ärzte",
        {
            "hausarzt_au.pdf": 2,
            "klinikum_brief.pdf": 2,
            "zahnarzt_2024.pdf": 1,
            "tierarzt.pdf": 1,
        },
        (),
    ),
    (
        "Steuerbescheide",
        {
            "est_bescheid_2023.pdf": 2,
            "est_bescheid_2022.pdf": 2,
            "grundsteuer_2024.pdf": 2,
            "hundesteuer.pdf": 2,
            "kfz_steuer.pdf": 2,
        },
        ("steuererklaerung_2023.pdf", "newsletter.pdf"),
    ),
    (
        "Kündigungen",
        {"vodafone_kuendigung.pdf": 2, "zeitung_kuendigung.pdf": 2, "scan_fitx.pdf": 2},
        (),
    ),
    (
        "Verträge",
        {
            "mietvertrag.pdf": 2,
            "arbeitsvertrag.pdf": 2,
            "darlehen.pdf": 2,
            "vodafone_vertrag.pdf": 2,
            "riester.pdf": 1,
        },
        (),
    ),
    (
        "Versicherungen",
        {
            "allianz_hausrat_2025.pdf": 2,
            "allianz_haftpflicht.pdf": 2,
            "huk_kfz_2024.pdf": 2,
            "tk_beitrag.pdf": 1,
            "scan_wohngebaeude.pdf": 1,
        },
        (),
    ),
    # compounds: part of a longer word, or the query is a compound the text splits
    (
        "Kaltmiete",
        {"mietvertrag.pdf": 2, "mieterhoehung_2024.pdf": 2, "scan_mietbescheinigung.pdf": 2},
        (),
    ),
    ("Hausratversicherung", {"allianz_hausrat_2025.pdf": 2, "werbung_hausrat.pdf": 1}, ()),
    ("Ausweis", {"personalausweis.pdf": 2, "reisepass.pdf": 1}, ()),
    ("Rente", {"renteninfo_2024.pdf": 2, "riester.pdf": 1}, ()),
    (
        "Stromrechnung",
        {"stadtwerke_jahresabrechnung_2023.pdf": 2, "stadtwerke_abschlag_2024.pdf": 1},
        (),
    ),
    ("Arztrechnung", {"zahnarzt_2024.pdf": 2, "tierarzt.pdf": 1}, ()),
    ("Handwerkerrechnung", {"elektriker.pdf": 2}, ("newsletter.pdf",)),
    ("Rentenversicherungsnummer", {"renteninfo_2024.pdf": 2}, ()),
    ("Müllgebühren", {"muell_2024.pdf": 2}, ()),
    ("Kreditvertrag", {"darlehen.pdf": 2}, ()),
    ("Autoversicherung", {"huk_kfz_2024.pdf": 2}, ()),
    ("Zugticket München", {"bahn_ticket.pdf": 2}, ()),
    ("Kranken kasse", {"tk_mitglied.pdf": 2, "tk_beitrag.pdf": 2}, ()),
    # other words for the same thing
    ("Nebenkostenabrechnung", {"betriebskosten_2023.pdf": 2}, ()),
    ("Handy", {"handy_kauf.pdf": 2, "telekom_2024_05.pdf": 1, "telekom_2024_06.pdf": 1}, ()),
    (
        "Handy Rechnung",
        {"telekom_2024_05.pdf": 2, "telekom_2024_06.pdf": 2, "handy_kauf.pdf": 1},
        (),
    ),
    ("Kfz-Steuer", {"kfz_steuer.pdf": 2}, ()),
    ("Auto TÜV", {"tuev_2024.pdf": 2}, ()),
    ("Krankschreibung", {"hausarzt_au.pdf": 2}, ()),
    ("Gehalt", {"lohn_2024_03.pdf": 2, "lohn_2024_04.pdf": 2, "arbeitsvertrag.pdf": 1}, ()),
    ("Lohnabrechnung", {"lohn_2024_03.pdf": 2, "lohn_2024_04.pdf": 2}, ()),
    ("Kindergarten", {"kita_2024.pdf": 2}, ()),
    ("GEZ", {"rundfunkbeitrag.pdf": 2}, ()),
    ("Kredit", {"darlehen.pdf": 2}, ()),
    ("Krankenhaus", {"klinikum_brief.pdf": 2}, ()),
    ("TV", {"fernseher.pdf": 2}, ()),
    ("Urlaub Mallorca", {"reise_mallorca.pdf": 2}, ()),
    ("Heiratsurkunde", {"heiratsurkunde.pdf": 2}, ()),
    # natural phrasing with filler words
    ("Zeugnis von Mia", {"schulzeugnis_mia.pdf": 2}, ()),
    ("die Rechnung vom Zahnarzt", {"zahnarzt_2024.pdf": 2}, ()),
    ("Bescheid über das Kindergeld", {"kindergeld.pdf": 2}, ()),
    # typing errors
    (
        "Rechnugn Telekom",
        {"telekom_2024_05.pdf": 2, "telekom_2024_06.pdf": 2, "telekom_festnetz_2024.pdf": 2},
        (),
    ),
    ("Vodafnoe", {"vodafone_kuendigung.pdf": 2, "vodafone_vertrag.pdf": 2}, ()),
    ("Hausratversicherug", {"allianz_hausrat_2025.pdf": 2, "werbung_hausrat.pdf": 1}, ()),
    ("Scornsteinfeger", {"schornsteinfeger.pdf": 2}, ()),
    ("Wodafone", {"vodafone_kuendigung.pdf": 2, "vodafone_vertrag.pdf": 2}, ()),
    # OCR errors in scans without metadata
    ("Kündigung Fitnessstudio", {"scan_fitx.pdf": 2}, ()),
    ("Wohngebäudeversicherung", {"scan_wohngebaeude.pdf": 2}, ()),
    ("Mietbescheinigung", {"scan_mietbescheinigung.pdf": 2}, ()),
    ("Versicherungsschein", {"allianz_haftpflicht.pdf": 2, "scan_wohngebaeude.pdf": 2}, ()),
    # words in the wrong order or far apart
    ("Versicherung Kfz", {"huk_kfz_2024.pdf": 2}, ()),
    ("Rentenversicherung", {"renteninfo_2024.pdf": 2}, ("newsletter.pdf",)),
]


def load(archive) -> dict[str, str]:
    """Ingest + process the benchmark archive. Returns filename -> document id."""
    # uploaded under neutral names, as from a scanner: the file name must not give it away
    upload = {name: f"scan_{i:04d}.pdf" for i, (name, _, _) in enumerate(DOCS, 1)}
    by_upload = {upload[name]: meta for name, _, meta in DOCS}
    registry.override(classifier=ScriptedClassifier(by_filename=by_upload))
    ids = {
        name: ingest_bytes(archive, text_pdf(pages), upload[name]).doc_id for name, pages, _ in DOCS
    }
    process_all(archive)
    return ids


# Held out: written after the search changes, without looking at the results, to check that
# the rules are not fitted to QUERIES. Some need knowledge no word rule has (Wertpapiere for an
# ETF statement) and are expected to fail.
HELDOUT: list[tuple[str, dict[str, int], tuple[str, ...]]] = [
    ("Mobilfunkvertrag kündigen", {"vodafone_kuendigung.pdf": 2}, ()),
    ("Wasserrechnung", {"wasser_2023.pdf": 2}, ()),
    ("Stromabschlag", {"stadtwerke_abschlag_2024.pdf": 2}, ()),
    ("Abschläge Strom", {"stadtwerke_abschlag_2024.pdf": 2}, ()),
    ("Mieterhöhung", {"mieterhoehung_2024.pdf": 2}, ()),
    ("Bello", {"tierarzt.pdf": 2, "hundesteuer.pdf": 2}, ()),
    ("Zahnreinigung", {"zahnarzt_2024.pdf": 2}, ()),
    ("Physiotherapie", {"klinikum_brief.pdf": 2}, ()),
    ("Unfall", {"klinikum_brief.pdf": 2}, ()),
    ("Lohnsteuer 2023", {"lohnsteuerbescheinigung_2023.pdf": 2}, ()),
    ("Kirchensteuer", {"est_bescheid_2023.pdf": 2}, ()),
    (
        "Solidaritätszuschlag",
        {"est_bescheid_2023.pdf": 2, "est_bescheid_2022.pdf": 2, "lohn_2024_03.pdf": 1},
        (),
    ),
    ("Einkommensteuerbescheide", {"est_bescheid_2023.pdf": 2, "est_bescheid_2022.pdf": 2}, ()),
    ("Steuerbescheid Finanzamt 2022", {"est_bescheid_2022.pdf": 2}, ()),
    ("Gehaltsabrechnungen 2024", {"lohn_2024_03.pdf": 2, "lohn_2024_04.pdf": 2}, ()),
    ("Kontoauszug Februar", {"kontoauszug_2024_02.pdf": 2}, ()),
    ("Sparkasse", {"kontoauszug_2024_01.pdf": 2, "kontoauszug_2024_02.pdf": 2}, ()),
    ("Darlehen Zins", {"darlehen.pdf": 2}, ()),
    ("ETF", {"depot_2024.pdf": 2}, ()),
    ("Wertpapiere", {"depot_2024.pdf": 2}, ()),
    ("Altersvorsorge", {"riester.pdf": 2, "renteninfo_2024.pdf": 1}, ()),
    ("Reisepass abholen", {"reisepass.pdf": 2}, ()),
    ("Kindergeld Paul", {"kindergeld.pdf": 2}, ()),
    ("Elternbeitrag", {"kita_2024.pdf": 2}, ()),
    ("Fahrkarte München", {"bahn_ticket.pdf": 2}, ()),
    ("Hotel Mallorca", {"reise_mallorca.pdf": 2}, ()),
    ("Fernsehgerät", {"fernseher.pdf": 2}, ()),
    ("OLED", {"fernseher.pdf": 2}, ()),
    ("Bosch", {"waschmaschine.pdf": 2}, ()),
    ("Garantie", {"waschmaschine.pdf": 2, "fernseher.pdf": 2}, ()),
    ("Elektriker", {"elektriker.pdf": 2}, ()),
    ("FI-Schalter", {"elektriker.pdf": 2}, ()),
    ("Brennwerttherme", {"schornsteinfeger.pdf": 2}, ()),
    ("Abgasmessung 2024", {"schornsteinfeger.pdf": 2}, ()),
    ("Wohnung Lindenweg", {"mietvertrag.pdf": 2}, ()),
    ("Grundschuld", {"darlehen.pdf": 2}, ()),
    ("Kaution", {"mietvertrag.pdf": 2}, ()),
    ("Heizkosten", {"mietvertrag.pdf": 2, "betriebskosten_2023.pdf": 1, "gas_2023.pdf": 1}, ()),
    ("Versicherungsnummer", {"renteninfo_2024.pdf": 2, "allianz_haftpflicht.pdf": 2}, ()),
    (
        "Kundennummer 5566778899",
        {"telekom_2024_05.pdf": 2, "telekom_2024_06.pdf": 2, "telekom_festnetz_2024.pdf": 2},
        (),
    ),
    ("Beitragsnummer", {"rundfunkbeitrag.pdf": 2}, ()),
    ("Zeugnis Mathematik", {"schulzeugnis_mia.pdf": 2}, ()),
    ("arbeitsunfähig", {"hausarzt_au.pdf": 2}, ()),
    ("Krankenversicherung Beitrag 2024", {"tk_beitrag.pdf": 2}, ()),
    ("Autohaus", {"werkstatt_2024.pdf": 2}, ()),
    ("Ölwechsel", {"werkstatt_2024.pdf": 2}, ()),
    ("Reifen", {"werkstatt_2024.pdf": 2}, ()),
    ("Kfz-Haftpflicht", {"huk_kfz_2024.pdf": 2}, ()),
    ("Teilkasko", {"huk_kfz_2024.pdf": 2}, ()),
    ("Golf", {"tuev_2024.pdf": 2, "werkstatt_2024.pdf": 2, "huk_kfz_2024.pdf": 2}, ()),
]
