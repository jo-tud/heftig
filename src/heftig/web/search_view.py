"""View model of the search page: facet links with counts, timeline, date presets, chips.

Everything is plain links that toggle query parameters, so the page works without JavaScript;
search.js only adds suggestions while typing, the tag filter box and the mobile bottom sheet.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from .. import i18n, senders
from ..datephrases import month_name
from ..i18n import N_, _
from ..search import SearchParams, SearchResult

SOURCE_LABELS = i18n.Labels({
    "scanner": N_("Scanner"), "folder": N_("Folder"), "web": N_("Browser"), "api": N_("API"),
    "email": N_("Email"), "import": N_("Import"),
})  # fmt: skip
STATUS_LABELS = i18n.Labels({
    "queued": N_("waiting"), "processing": N_("in progress"), "done": N_("done"),
    "needs_review": N_("to review"), "failed": N_("error"),
})  # fmt: skip
CHIP_LABELS = i18n.Labels({
    "correspondent": N_("Sender"), "document_type": N_("Type"), "tag": N_("Tag"),
    "date_from": N_("Date from"), "date_to": N_("Date to"), "received_from": N_("Received from"),
    "received_to": N_("Received to"), "source": N_("Source"), "email_from": N_("E-mail from"),
    "status": N_("Status"),
    "filed": N_("Filed"), "filing_section": N_("Section"), "filing_binder": N_("Binder"),
    "cf_key": N_("Field"),
    "cf_min": N_("min."), "cf_max": N_("max."), "session": N_("Scan batch"),
})  # fmt: skip
YES_NO = i18n.Labels({"yes": N_("yes"), "no": N_("no")})
FACET_SHOW = 8  # values shown per group before "more"
Items = list[tuple[str, str]]


def query_items(query_params) -> Items:
    """Search parameters of the current URL (without paging and one-off messages or undo)."""
    return [
        (k, v)
        for k, v in query_params.multi_items()
        if k not in ("page", "msg", "undo", "ai", "ai_note") and v
    ]


def href(items: Items) -> str:
    return "/?" + urlencode(items) if items else "/"


def toggled(items: Items, key: str, value: str) -> str:
    if (key, value) in items:
        return href([kv for kv in items if kv != (key, value)])
    return href([*items, (key, value)])


def with_values(items: Items, **updates: str | None) -> str:
    rest = [(k, v) for k, v in items if k not in updates]
    return href(rest + [(k, v) for k, v in updates.items() if v])


def _values(items: Items, key: str) -> list[str]:
    return [v for k, v in items if k == key]


def _group(items: Items, key: str, counts: list[dict[str, Any]], label_of=None) -> dict[str, Any]:
    selected = _values(items, key)
    out = []
    seen = set()
    for c in counts:
        value = c.get("name", c.get("value"))
        seen.add(value)
        out.append({
            "label": label_of(value) if label_of else value, "count": c["count"],
            "on": value in selected, "href": toggled(items, key, value),
        })  # fmt: skip
    for value in selected:  # selected but no longer in the result (e.g. alias spelling)
        if value not in seen:
            out.insert(0, {"label": label_of(value) if label_of else value, "count": 0,
                           "on": True, "href": toggled(items, key, value)})  # fmt: skip
    out.sort(key=lambda x: (not x["on"],))  # selected first, otherwise by count (stable)
    return {"key": key, "values": out, "more": max(0, len(out) - FACET_SHOW)}


def _year_of(value: str | None) -> str | None:
    m = re.fullmatch(r"(\d{4})(?:-01-01)?", value or "")
    return m.group(1) if m else None


def _timeline(items: Items, months: dict[str, int], today: date) -> dict[str, Any]:
    """Bars per year, or per month when exactly one year is selected (drill-down)."""
    df = next(iter(_values(items, "date_from")), None)
    dt = next(iter(_values(items, "date_to")), None)
    year = _year_of(df)
    one_year = year and dt and re.fullmatch(rf"{year}(?:-12-31)?", dt)
    one_month = df and dt and re.fullmatch(r"\d{4}-\d{2}", df) and df == dt
    if one_month:
        year, one_year = df[:4], True
    bars = []
    if one_year:
        for m in range(1, 13):
            key = f"{year}-{m:02d}"
            bars.append({
                "label": month_name(m)[0], "title": f"{month_name(m)} {year}",
                "count": months.get(key, 0), "on": one_month and df == key,
                "href": with_values(items, date_from=key, date_to=key),
            })  # fmt: skip
        up = {"label": _("All years"), "href": with_values(items, date_from=None, date_to=None)}
    else:
        years: dict[str, int] = {}
        for k, n in months.items():
            years[k[:4]] = years.get(k[:4], 0) + n
        if years:
            lo, hi = int(min(years)), int(max(years))
            span = range(lo, hi + 1) if hi - lo <= 40 else sorted(int(y) for y in years)
            # at most ~6 labels (every 2nd, 5th, 10th ... year) - the bars are narrow
            step = next(n for n in (1, 2, 5, 10, 20, 25, 50, 100) if len(span) / n <= 6)
            for y in span:
                ys = str(y)
                bars.append({
                    "label": ys if y % step == 0 or len(span) <= 6 else "", "title": ys,
                    "count": years.get(ys, 0), "on": year == ys and one_year,
                    "href": with_values(items, date_from=ys, date_to=ys),
                })  # fmt: skip
        up = None
    top = max((b["count"] for b in bars), default=0) or 1
    for b in bars:
        b["h"] = round(b["count"] / top, 3)
    this_year = str(today.year)
    last_year = str(today.year - 1)
    twelve = (today - timedelta(days=365)).isoformat()
    presets = [
        {"label": _("Last 12 months"), "on": df == twelve and not dt,
         "href": with_values(items, date_from=twelve, date_to=None)},
        {"label": _("This year (%(year)s)", year=this_year),
         "on": df == this_year and dt == this_year,
         "href": with_values(items, date_from=this_year, date_to=this_year)},
        {"label": _("Last year (%(year)s)", year=last_year),
         "on": df == last_year and dt == last_year,
         "href": with_values(items, date_from=last_year, date_to=last_year)},
    ]  # fmt: skip
    return {
        "bars": bars,
        "up": up,
        "year": year if one_year else None,
        "presets": presets,
        "active": bool(df or dt),
        "reset": with_values(items, date_from=None, date_to=None),
    }


def _date_label(v: str) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{2})", v)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{month_name(int(m.group(2)))} {m.group(1)}"
    try:
        return i18n.format_date(date.fromisoformat(v))
    except ValueError:
        return v


def chips(
    items: Items,
    session_names: dict[str, str] | None = None,
    email_names: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    out = []
    df = _values(items, "date_from")
    dt = _values(items, "date_to")
    date_pair = len(df) == 1 and df == dt
    for i, (k, v) in enumerate(items):
        if k not in CHIP_LABELS:
            continue
        if date_pair and k in ("date_from", "date_to"):
            if k == "date_to":
                continue
            rest = [kv for kv in items if kv[0] not in ("date_from", "date_to")]
            out.append({"label": f"{_('Date')}: {_date_label(v)}", "href": href(rest)})
            continue
        rest = [kv for j, kv in enumerate(items) if j != i]
        shown = SOURCE_LABELS.get(v, STATUS_LABELS.get(v, YES_NO.get(v, v)))
        if k.startswith(("date_", "received_")):
            shown = _date_label(v)
        if k == "session":
            shown = (session_names or {}).get(v, v)
        if k == "email_from":
            shown = senders.label(email_names or {}, v)
        out.append({"label": f"{CHIP_LABELS[k]}: {shown}", "href": href(rest)})
    return out


def describe(params: SearchParams, items: Items, session_names=None, email_names=None) -> str:
    """Short label of a search, e.g. for saved and recent searches."""
    parts = [params.q.strip()] if params.q.strip() else []
    for c in chips(items, session_names, email_names):
        parts.append(c["label"].split(": ", 1)[-1])
    return " · ".join(parts)[:80] or _("All documents")


def build(
    params: SearchParams,
    result: SearchResult,
    query_params,
    today: date,
    session_names: dict[str, str] | None = None,
    email_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """`email_names`: names for e-mail sender addresses (senders.json)."""
    items = query_items(query_params)
    f = result.facets or {}
    tags_selected = _values(items, "tag")
    tag_mode = "any" if params.tag_mode == "any" else "all"
    view = {
        "qitems": items,
        "chips": chips(items, session_names, email_names),
        "active": any(k in CHIP_LABELS for k, _ in items),
        "groups": [
            (_("Sender"), _group(items, "correspondent", f.get("correspondent", []))),
            (_("Document type"), _group(items, "document_type", f.get("document_type", []))),
        ],
        "tags": _group(items, "tag", f.get("tag", [])),
        "tag_mode": {
            "value": tag_mode,
            "show": len(tags_selected) >= 2,
            "all": with_values(items, tag_mode=None),
            "any": with_values(items, tag_mode="any"),
        },
        "sources": _group(items, "source", f.get("source", []), lambda v: SOURCE_LABELS.get(v, v)),
        "email_senders": _group(
            items,
            "email_from",
            f.get("email_from", []),
            lambda v: senders.label(email_names or {}, v),
        ),  # fmt: skip
        # a date phrase in the text ("seit 2023") is replaced, not combined, by the timeline
        "timeline": _timeline(
            [
                (k, result.q_rest if k == "q" else v)
                for k, v in items
                if not (k == "q" and result.date_phrase and not result.q_rest)
            ]
            if result.date_phrase
            else items,
            f.get("months", {}),
            today,
        ),
        # words + meaning or words only, for this search (shown when it can search by meaning)
        "meaning": {
            "show": result.meaning_available,
            "on": params.meaning,
            "with": with_values(items, meaning=None),
            "without": with_values(items, meaning="0"),
        },
        "undated": f.get("undated", 0),
        "label": describe(params, items, session_names, email_names),
        "query": urlencode(items),
        # search form: a new text keeps the filters (visible as chips), drops paging/literal
        "keep": [(k, v) for k, v in items if k not in ("q", "literal")],
        # sort form keeps everything else
        "keep_sort": [(k, v) for k, v in items if k != "sort"],
        "without_filters": href([("q", params.q)] if params.q else []),
    }
    if result.date_phrase:
        view["literal_href"] = with_values(items, literal="1")
    return view
