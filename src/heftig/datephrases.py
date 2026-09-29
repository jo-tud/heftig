"""Date phrases in the search text, German and English: "März 2025", "letztes Jahr",
"since 2023", "last 3 months", "between January and March 2025", ...

A recognised phrase is removed from the query and becomes a document-date range, so
``Rechnung letztes Jahr`` searches for "Rechnung" among documents dated last year. A bare year
("2025") is deliberately *not* a phrase: it stays a search term with the year boost (see
:mod:`heftig.search`), because it may just as well be part of a number or a title.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from .i18n import N_, _, format_date

MONTHS = {
    "januar": 1, "jan": 1, "jänner": 1, "jaenner": 1, "january": 1,
    "februar": 2, "feb": 2, "february": 2,
    "märz": 3, "maerz": 3, "marz": 3, "mär": 3, "mrz": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "okt": 10, "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "dez": 12, "december": 12, "dec": 12,
}  # fmt: skip
MONTH_NAMES = [
    "", N_("January"), N_("February"), N_("March"), N_("April"), N_("May"), N_("June"),
    N_("July"), N_("August"), N_("September"), N_("October"), N_("November"), N_("December"),
]  # fmt: skip


def month_name(month: int) -> str:
    """The month's name in the current language."""
    return _(MONTH_NAMES[month])


# relative phrases by how far back: this / last / the one before last
_RELATIVE = {
    "jahr": (N_("this year"), N_("last year"), N_("the year before last")),
    "monat": (N_("this month"), N_("last month"), N_("the month before last")),
    "quartal": (N_("this quarter"), N_("last quarter"), N_("the quarter before last")),
    "woche": (N_("this week"), N_("last week"), N_("the week before last")),
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_YEAR = r"((?:19|20)\d\d)"
# by the first three letters (German and English)
_UNIT = {"tag": "d", "woc": "w", "mon": "m", "jah": "y", "day": "d", "wee": "w", "yea": "y"}
# English words -> the German ones the interpretation below works with
_EN = {
    "since": "seit", "until": "bis", "till": "bis", "through": "bis", "before": "vor",
    "after": "nach", "in": "", "from": "", "of": "", "between": "zwischen", "and": "und",
    "to": "bis", "last": "letzte", "previous": "letzte", "past": "letzte", "this": "diese",
    "current": "diese", "year": "jahr", "month": "monat", "quarter": "quartal", "week": "woche",
}  # fmt: skip


def _de(word: str | None) -> str:
    w = (word or "").lower()
    return _EN.get(w, w)


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # "März bis Mai 2025", "von Nov. 2024 bis Feb. 2025", "zwischen Januar und März 2025"
    ("month_span", re.compile(
        rf"\b(?:(?P<pre>von|zwischen|from|between)\s+)?(?P<m1>{_MONTH_RE})\.?(?:\s+(?P<y1>(?:19|20)\d\d))?"
        rf"\s*(?P<con>bis|und|to|until|and|–|-)\s*(?P<m2>{_MONTH_RE})\.?,?\s+(?P<y2>(?:19|20)\d\d)\b",
        re.I)),
    ("month", re.compile(
        rf"\b(?:(im|vom|von|aus|ab|seit|bis|vor|nach|in|from|of|since|until|till|before|after)\s+)?"
        rf"({_MONTH_RE})\.?,?\s+{_YEAR}\b", re.I)),
    ("span", re.compile(
        r"\b(?:(?P<pre>von|zwischen|from|between)\s+)?(?P<y1>(?:19|20)\d\d)\s*"
        r"(?P<con>bis|und|to|until|and|–)\s*"
        r"(?P<y2>(?:19|20)\d\d)\b", re.I)),
    ("relative", re.compile(
        r"\b(?:(?:im|aus|vom|von|in)\s+(?:den\s+)?)?(vorletzte[nmrs]?|letzte[nmrs]?|vorige[nmrs]?|"
        r"vergangene[nmrs]?|diese[nmrs]?|aktuelle[nmrs]?)\s+(jahr|monat|quartal|woche)\b", re.I)),
    ("relative", re.compile(
        r"\b(?:(?:in|from|of)\s+)?(?:the\s+)?(last|previous|this|current)\s+"
        r"(year|month|quarter|week)\b", re.I)),
    ("last_n", re.compile(
        r"\b(?:(?:in\s+)?den\s+|die\s+)?(?:letzten|letzte|vergangenen)\s+(\d{1,3})\s+"
        r"(tag|tage|tagen|woche|wochen|monat|monate|monaten|jahr|jahre|jahren)\b", re.I)),
    ("last_n", re.compile(
        r"\b(?:(?:in|from|of|over)\s+)?(?:the\s+)?(?:last|past|previous)\s+(\d{1,3})\s+"
        r"(days?|weeks?|months?|years?)\b", re.I)),
    ("bound", re.compile(rf"\b(seit|ab|bis|vor|nach|since|until|till|before|after)\s+{_YEAR}\b",
                         re.I)),
    ("year", re.compile(rf"\b(?:im\s+jahre?|jahrgang)\s+{_YEAR}\b", re.I)),
]  # fmt: skip


@dataclass
class DatePhrase:
    text: str  # the phrase as typed
    date_from: str  # ISO, inclusive
    date_to: str  # ISO, inclusive
    label: str  # for the UI, in the language of the moment it was recognised


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _fmt(d: date) -> str:
    return format_date(d)


def _shift_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _usable(kind: str, m: re.Match[str]) -> bool:
    # "2021 und 2023" means two years, not the span 2021-2023 - only "zwischen ... und" does
    if kind in ("span", "month_span") and _de(m.group("con")) == "und":
        return _de(m.group("pre")) == "zwischen"
    return True


def _interpret(kind: str, m: re.Match[str], today: date) -> tuple[date, date, str] | None:
    if kind == "month_span":
        m1, m2 = MONTHS[m.group("m1").lower()], MONTHS[m.group("m2").lower()]
        y2 = int(m.group("y2"))
        y1 = int(m.group("y1")) if m.group("y1") else (y2 if m1 <= m2 else y2 - 1)
        lo, hi = date(y1, m1, 1), _month_end(y2, m2)
        if y1 == y2:
            return lo, hi, f"{month_name(m1)}–{month_name(m2)} {y2}"
        return lo, hi, f"{month_name(m1)} {y1} – {month_name(m2)} {y2}"
    if kind == "month":
        prep = _de(m.group(1))
        month = MONTHS[m.group(2).lower()]
        y = int(m.group(3))
        lo, hi = date(y, month, 1), _month_end(y, month)
        name = f"{month_name(month)} {y}"
        if prep in ("ab", "seit"):
            return lo, today, _("since %(date)s", date=name)
        if prep == "bis":
            return date(1900, 1, 1), hi, _("until %(date)s", date=name)
        if prep == "vor":
            return date(1900, 1, 1), lo - timedelta(days=1), _("before %(date)s", date=name)
        if prep == "nach":
            return hi + timedelta(days=1), date(2100, 12, 31), _("after %(date)s", date=name)
        return lo, hi, name
    if kind == "span":
        a, b = sorted((int(m.group("y1")), int(m.group("y2"))))
        return date(a, 1, 1), date(b, 12, 31), f"{a}–{b}"
    if kind == "relative":
        which = _de(m.group(1))
        unit = _de(m.group(2))
        back = 2 if which.startswith("vorletzt") else 0 if which[:4] in ("dies", "aktu") else 1
        label = _(_RELATIVE[unit][back])
        if unit == "jahr":
            y = today.year - back
            return date(y, 1, 1), date(y, 12, 31), f"{label} ({y})"
        if unit == "monat":
            first = _shift_months(today.replace(day=1), -back)
            return (
                first,
                _month_end(first.year, first.month),
                (f"{label} ({month_name(first.month)} {first.year})"),
            )
        if unit == "quartal":
            q0 = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
            first = _shift_months(q0, -3 * back)
            last = _shift_months(first, 2)
            n = (first.month - 1) // 3 + 1
            return first, _month_end(last.year, last.month), f"{label} (Q{n} {first.year})"
        monday = today - timedelta(days=today.weekday()) - timedelta(weeks=back)
        sunday = monday + timedelta(days=6)
        return monday, sunday, f"{label} ({_fmt(monday)}–{_fmt(sunday)})"
    if kind == "last_n":
        n = int(m.group(1))
        unit = _UNIT[m.group(2).lower()[:3]]
        if not 1 <= n <= 100:
            return None
        if unit == "d":
            lo = today - timedelta(days=n)
        elif unit == "w":
            lo = today - timedelta(weeks=n)
        elif unit == "m":
            lo = _shift_months(today, -n)
        else:
            lo = _shift_months(today, -12 * n)
        return lo, today, f"{m.group(0).strip()} ({_fmt(lo)}–{_fmt(today)})"
    if kind == "bound":
        prep = _de(m.group(1))
        y = int(m.group(2))
        if prep in ("seit", "ab"):
            return date(y, 1, 1), today, _("since %(date)s", date=y)
        if prep == "bis":
            return date(1900, 1, 1), date(y, 12, 31), _("until %(date)s", date=y)
        if prep == "vor":
            return date(1900, 1, 1), date(y - 1, 12, 31), _("before %(date)s", date=y)
        return date(y + 1, 1, 1), date(2100, 12, 31), _("after %(date)s", date=y)
    if kind == "year":
        y = int(m.group(1))
        return date(y, 1, 1), date(y, 12, 31), str(y)
    return None


def extract(q: str, today: date | None = None) -> tuple[str, DatePhrase | None]:
    """Find the first date phrase in ``q``; return the rest of the query and the phrase."""
    today = today or date.today()
    best: tuple[int, str, re.Match[str]] | None = None
    for kind, rx in _PATTERNS:
        for m in rx.finditer(q):
            # skip matches inside quotes ("…") - the user wants literal text there
            if q[: m.start()].count('"') % 2 or not _usable(kind, m):
                continue
            # the earliest phrase; at the same place the longer one ("März bis Mai 2025")
            if best is None or (m.start(), -len(m.group(0))) < (best[0], -len(best[2].group(0))):
                best = (m.start(), kind, m)
            break
    if best is None:
        return q, None
    _start, kind, m = best
    res = _interpret(kind, m, today)
    if res is None:
        return q, None
    lo, hi = res[0], res[1]
    if lo > hi:
        return q, None
    rest = re.sub(r"\s+", " ", (q[: m.start()] + " " + q[m.end() :])).strip()
    return rest, DatePhrase(m.group(0).strip(), lo.isoformat(), hi.isoformat(), res[2])
