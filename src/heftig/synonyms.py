"""Words that mean the same thing in household paperwork.

The search adds these as alternatives of a search word, ranked below documents that contain the
word itself (see docs/search.md, "Other words for the same thing"). Groups are kept small and
unambiguous on purpose: a synonym that is only sometimes right ("Gebühr" for "Beitrag") would
push wrong documents up.

Entries are written folded (umlauts as ae/oe/ue, ß as ss); several words form a phrase. A
search word belongs to a group when it has the same word stem as an entry ("Handys" -> "handy").
"""

from __future__ import annotations

from functools import lru_cache

from .textnorm import tokens

GROUPS: tuple[tuple[str, ...], ...] = (
    # phone
    ("handy", "mobiltelefon", "smartphone", "mobilfunk"),
    ("handyvertrag", "mobilfunkvertrag"),
    ("festnetz", "telefonanschluss"),
    # car
    ("kfz", "auto", "pkw", "kraftfahrzeug", "kraftfahrt"),
    ("kfz steuer", "kraftfahrzeugsteuer"),
    ("tuev", "hauptuntersuchung"),
    ("fuehrerschein", "fahrerlaubnis"),
    # flat, house
    ("nebenkosten", "betriebskosten"),
    ("muell", "abfall"),
    ("vermieter", "hausverwaltung"),
    ("gez", "rundfunkbeitrag", "rundfunkgebuehr"),
    # money
    ("kredit", "darlehen"),
    ("gehalt", "lohn", "entgelt", "bezuege", "verdienst"),
    (
        "gehaltsabrechnung",
        "lohnabrechnung",
        "entgeltabrechnung",
        "verdienstabrechnung",
        "lohnzettel",
    ),
    # health
    ("krankenkasse", "krankenversicherung"),
    ("krankenhaus", "klinik", "klinikum", "spital"),
    ("krankschreibung", "arbeitsunfaehigkeitsbescheinigung", "au bescheinigung"),
    ("arzt", "aerztin", "mediziner"),
    ("zahnarzt", "zahnaerztin", "zahnarztpraxis"),
    ("brille", "sehhilfe"),
    # family
    ("kita", "kindertagesstaette", "kindergarten", "kinderkrippe", "krippe"),
    ("heiratsurkunde", "eheurkunde"),
    # tax, state
    ("finanzamt", "steuerverwaltung"),
    ("steuerbescheid", "steuerfestsetzung"),
    ("personalausweis", "ausweis", "perso"),
    # travel
    ("urlaub", "reise", "pauschalreise"),
    ("zug", "bahn"),
    ("ticket", "fahrkarte", "fahrschein"),
    ("flug", "flugticket", "boardingpass"),
    # things
    ("tv", "fernseher", "fernsehgeraet"),
    ("laptop", "notebook"),
    ("kassenbon", "kassenzettel", "quittung", "kaufbeleg"),
    ("garantie", "gewaehrleistung"),
    # English words people type for German documents
    ("rechnung", "invoice"),
    ("vertrag", "contract"),
    ("versicherung", "insurance"),
    ("steuer", "tax"),
    ("kuendigung", "cancellation"),
    ("kontoauszug", "bank statement"),
)


def _stem(word: str) -> str:
    from .expand import stem

    return stem(word)


@lru_cache(maxsize=1)
def _index() -> dict[tuple[str, ...], list[tuple[str, ...]]]:
    """Stems of an entry -> the other entries of its group (as token tuples)."""
    out: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for group in GROUPS:
        entries = [tuple(tokens(e)) for e in group]
        for e in entries:
            key = tuple(_stem(t) for t in e)
            out.setdefault(key, []).extend(o for o in entries if o != e)
    return out


def alternatives(words: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Other words or phrases (as token tuples) for a search word or phrase (folded tokens)."""
    if not words or not all(w.isalpha() for w in words):
        return []
    found = _index().get(tuple(_stem(w) for w in words), [])
    if not found and len(words) == 1 and words[0].endswith("s") and len(words[0]) > 4:
        found = _index().get((_stem(words[0][:-1]),), [])  # Handys, Pkws, Kfzs
    return [alt for alt in dict.fromkeys(found) if alt != words]
