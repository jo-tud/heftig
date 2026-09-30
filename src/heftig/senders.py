"""Names for the e-mail addresses documents came from ("anna@example.org" -> "Anna").

Documents imported from the mailbox keep the sender's address (``source_details.from``). A name
given here (Settings -> E-mail senders) is shown instead of the address - on the document page,
in the list and in the filter - for every document from that address, also the ones imported
before. Stored in the archive (``senders.json`` at its root), so the names travel with backups
and exports; missing file = addresses only.
"""

from __future__ import annotations

import threading

from .storage import ArchivePaths, atomic_write_json, read_json
from .textnorm import clean_display_name

FILENAME = "senders.json"
MAX_NAMES = 1000
MAX_NAME = 80
_LOCK = threading.Lock()


def normalized(address: str) -> str:
    return address.strip().lower()


def clean(names) -> dict[str, str]:
    """Address -> name, addresses lower case, empty and invalid entries left out."""
    out: dict[str, str] = {}
    for address, name in names.items() if isinstance(names, dict) else []:
        if not isinstance(address, str) or not isinstance(name, str):
            continue
        address, name = normalized(address), clean_display_name(name)[:MAX_NAME]
        if "@" in address and len(address) <= 200 and name:
            out[address] = name
        if len(out) >= MAX_NAMES:
            break
    return out


def load(paths: ArchivePaths) -> dict[str, str]:
    try:
        data = read_json(paths.root / FILENAME)
    except (OSError, ValueError):
        return {}
    return clean(data.get("names", {}) if isinstance(data, dict) else {})


def save(paths: ArchivePaths, names: dict[str, str]) -> dict[str, str]:
    names = clean(names)
    with _LOCK:
        if names:
            atomic_write_json(paths.root / FILENAME, {"version": 1, "names": names})
        else:
            (paths.root / FILENAME).unlink(missing_ok=True)
    return names


def merge(paths: ArchivePaths, incoming) -> int:
    """Add the names of an import for addresses that have none yet. Returns how many."""
    current = load(paths)
    added = {a: n for a, n in clean(incoming).items() if a not in current}
    if added:
        save(paths, {**current, **added})
    return len(added)


def label(names: dict[str, str], address: str | None) -> str:
    """The name for an address, else the address itself."""
    if not address:
        return ""
    return names.get(normalized(address)) or address
