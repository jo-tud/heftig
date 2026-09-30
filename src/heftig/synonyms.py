"""Words that mean the same thing in household paperwork.

The search adds these as alternatives of a search word, ranked below documents that contain the
word itself (see docs/search.md, "Other words for the same thing"). Groups are kept small and
unambiguous on purpose: a synonym that is only sometimes right ("Gebühr" for "Beitrag") would
push wrong documents up.

Entries are compared folded (Müll = Muell); several words form a phrase ("Kfz-Steuer"). A search
word belongs to a group when it has the same word stem as an entry ("Handys" -> "Handy").
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

from .storage import ArchivePaths, atomic_write_json, read_json
from .textnorm import clean_display_name, tokens

FILENAME = "synonyms.json"  # the archive's own groups, at the archive root
MAX_GROUPS = 500
MAX_WORDS = 12  # per group
_LOCK = threading.Lock()

GROUPS: tuple[tuple[str, ...], ...] = (
    # phone
    ("Handy", "Mobiltelefon", "Smartphone", "Mobilfunk"),
    ("Handyvertrag", "Mobilfunkvertrag"),
    ("Festnetz", "Telefonanschluss"),
    # car
    ("Kfz", "Auto", "Pkw", "Kraftfahrzeug", "Kraftfahrt"),
    ("Kfz-Steuer", "Kraftfahrzeugsteuer"),
    ("TÜV", "Hauptuntersuchung"),
    ("Führerschein", "Fahrerlaubnis"),
    # flat, house
    ("Nebenkosten", "Betriebskosten"),
    ("Müll", "Abfall"),
    ("Vermieter", "Hausverwaltung"),
    ("GEZ", "Rundfunkbeitrag", "Rundfunkgebühr"),
    # money
    ("Kredit", "Darlehen"),
    ("Gehalt", "Lohn", "Entgelt", "Bezüge", "Verdienst"),
    (
        "Gehaltsabrechnung",
        "Lohnabrechnung",
        "Entgeltabrechnung",
        "Verdienstabrechnung",
        "Lohnzettel",
    ),
    # health
    ("Krankenkasse", "Krankenversicherung"),
    ("Krankenhaus", "Klinik", "Klinikum", "Spital"),
    ("Krankschreibung", "Arbeitsunfähigkeitsbescheinigung", "AU-Bescheinigung"),
    ("Arzt", "Ärztin", "Mediziner"),
    ("Zahnarzt", "Zahnärztin", "Zahnarztpraxis"),
    ("Brille", "Sehhilfe"),
    # family
    ("Kita", "Kindertagesstätte", "Kindergarten", "Kinderkrippe", "Krippe"),
    ("Heiratsurkunde", "Eheurkunde"),
    # tax, state
    ("Finanzamt", "Steuerverwaltung"),
    ("Steuerbescheid", "Steuerfestsetzung"),
    ("Personalausweis", "Ausweis", "Perso"),
    # travel
    ("Urlaub", "Reise", "Pauschalreise"),
    ("Zug", "Bahn"),
    ("Ticket", "Fahrkarte", "Fahrschein"),
    ("Flug", "Flugticket", "Boardingpass"),
    # things
    ("TV", "Fernseher", "Fernsehgerät"),
    ("Laptop", "Notebook"),
    ("Kassenbon", "Kassenzettel", "Quittung", "Kaufbeleg"),
    ("Garantie", "Gewährleistung"),
    # English words people type for German documents
    ("Rechnung", "invoice"),
    ("Vertrag", "contract"),
    ("Versicherung", "insurance"),
    ("Steuer", "tax"),
    ("Kündigung", "cancellation"),
    ("Kontoauszug", "bank statement"),
)


def _stem(word: str) -> str:
    from .expand import stem

    return stem(word)


def _build(groups) -> dict[tuple[str, ...], list[tuple[str, ...]]]:
    """Stems of an entry -> the other entries of its group (as token tuples)."""
    out: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for group in groups:
        entries = [tuple(tokens(e)) for e in group]
        entries = [e for e in entries if e]
        for e in entries:
            key = tuple(_stem(t) for t in e)
            out.setdefault(key, []).extend(o for o in entries if o != e)
    return out


@lru_cache(maxsize=1)
def _index() -> dict[tuple[str, ...], list[tuple[str, ...]]]:
    return _build(GROUPS)


_USER: dict[Path, tuple[float, dict]] = {}


def _user_index(root: Path | None) -> dict[tuple[str, ...], list[tuple[str, ...]]]:
    if root is None:
        return {}
    path = root / FILENAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _USER.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    index = _build(load_groups(path))
    _USER[path] = (mtime, index)
    return index


def version(root: Path | None) -> float:
    """Changes when the archive's own groups change (part of the search caches' key)."""
    try:
        return (root / FILENAME).stat().st_mtime if root else 0.0
    except OSError:
        return 0.0


def alternatives(words: tuple[str, ...], root: Path | None = None) -> list[tuple[str, ...]]:
    """Other words or phrases (as token tuples) for a search word or phrase (folded tokens):
    from the built-in groups and the archive's own (`root`: archive directory)."""
    if not words or not all(w.isalpha() for w in words):
        return []
    keys = [tuple(_stem(w) for w in words)]
    if len(words) == 1 and words[0].endswith("s") and len(words[0]) > 4:
        keys.append((_stem(words[0][:-1]),))  # Handys, Pkws, Kfzs
    found: list[tuple[str, ...]] = []
    for index in (_user_index(root), _index()):
        for key in keys:
            found += index.get(key, [])
    return [alt for alt in dict.fromkeys(found) if alt != words]


# --- the archive's own groups (synonyms.json) ------------------------------------------------


def load_groups(path: Path) -> list[list[str]]:
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return []
    groups = data.get("groups", []) if isinstance(data, dict) else []
    return clean_groups(g for g in groups if isinstance(g, list))


def clean_groups(groups) -> list[list[str]]:
    """Groups of at least two different words or phrases, as the user wrote them."""
    out: list[list[str]] = []
    seen: set[tuple] = set()
    for group in groups:
        words: dict[str, None] = {}
        for w in group:
            w = clean_display_name(str(w))[:60]
            if w and tokens(w) and all(t.isalpha() for t in tokens(w)):
                words.setdefault(w, None)
        unique = list(dict.fromkeys(words))[:MAX_WORDS]
        key = tuple(sorted(" ".join(tokens(w)) for w in unique))
        if len({" ".join(tokens(w)) for w in unique}) >= 2 and key not in seen:
            seen.add(key)
            out.append(unique)
    return out[:MAX_GROUPS]


def parse_text(text: str) -> list[list[str]]:
    """One group per line, words separated by commas: "Handy, Mobiltelefon, Smartphone"."""
    return clean_groups(line.replace(";", ",").split(",") for line in text.splitlines())


def as_text(groups: list[list[str]]) -> str:
    return "\n".join(", ".join(g) for g in groups)


def load(paths: ArchivePaths) -> list[list[str]]:
    return load_groups(paths.root / FILENAME)


def save(paths: ArchivePaths, groups: list[list[str]]) -> list[list[str]]:
    groups = clean_groups(groups)
    with _LOCK:
        if groups:
            atomic_write_json(paths.root / FILENAME, {"version": 1, "groups": groups})
        else:
            (paths.root / FILENAME).unlink(missing_ok=True)
    return groups


def merge(paths: ArchivePaths, incoming: list) -> int:
    """Add groups from an import that are not there yet. Returns how many were added."""
    current = load(paths)
    before = len(current)
    merged = clean_groups([*current, *(g for g in incoming if isinstance(g, list))])
    save(paths, merged)
    return len(merged) - before


def builtin() -> list[list[str]]:
    return [list(g) for g in GROUPS]
