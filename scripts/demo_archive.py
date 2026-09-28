"""Build a demo archive with fictional documents (for screenshots and for trying Heftig out).

    uv run python scripts/demo_archive.py /tmp/heftig-demo       # English (default)
    HEFTIG_ARCHIVE_DIR=/tmp/heftig-demo uv run heftig run          # user demo / demo-password-1234

Everything runs offline: letters are drawn as PDFs with a text layer (a few as "photos" that go
through Tesseract), then filed the way a user would - titles, senders, types, tags, amounts,
paper filing - so the archive looks like one that has been in use for a while. A few documents
are left for the inbox: suggestions to review, a possible duplicate, paper to file.

All people, companies, addresses and numbers are invented.
"""

from __future__ import annotations

import argparse
import io
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, timedelta
from pathlib import Path

from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from heftig import auth, jobs, settings_store  # noqa: E402
from heftig import documents as docs  # noqa: E402
from heftig.archive import Archive  # noqa: E402
from heftig.config import Settings  # noqa: E402
from heftig.ingest import ingest_stream  # noqa: E402
from heftig.worker import run_until_idle  # noqa: E402

RECIPIENT = ["Alex Morgan", "14 Linden Street", "Millbrook, OR 97000"]
TODAY = date(2026, 9, 18)
rnd = random.Random(7)


@dataclass
class Sender:
    name: str
    color: str
    lines: list[str]
    shape: str = "circle"  # logo: circle | square | bars


SENDERS = {
    "energy": Sender("Northwind Energy", "#e07b00", ["200 Harbor Road", "Portland, OR 97201"], "circle"),
    "mobile": Sender("Brightline Mobile", "#7a2cc4", ["PO Box 1180", "Seattle, WA 98101"], "bars"),
    "bank": Sender("Harbor Savings Bank", "#0b5394", ["1 Pier Plaza", "Portland, OR 97204"], "square"),
    "health": Sender("Cedar Health Insurance", "#2e8b57", ["55 Grove Avenue", "Salem, OR 97301"], "circle"),
    "landlord": Sender("Riverside Property Management", "#8b4513", ["9 Mill Lane", "Millbrook, OR 97000"], "square"),
    "tax": Sender("City of Millbrook – Revenue Office", "#444444", ["City Hall, 1 Main Street", "Millbrook, OR 97000"], "bars"),
    "car": Sender("Atlas Car Insurance", "#c0392b", ["300 Summit Drive", "Eugene, OR 97401"], "circle"),
    "employer": Sender("Lumen Analytics Ltd.", "#1f7a8c", ["77 Quarry Street", "Portland, OR 97209"], "square"),
    "internet": Sender("Bluewave Internet", "#1e88e5", ["42 Signal Way", "Bend, OR 97701"], "bars"),
    "doctor": Sender("Dr. Priya Nair – Family Practice", "#6d4c41", ["18 Elm Court", "Millbrook, OR 97000"], "circle"),
    "shop": Sender("Summit Electronics", "#37474f", ["Mall at Riverside, Unit 12", "Millbrook, OR 97000"], "square"),
}  # fmt: skip


@dataclass
class Doc:
    key: str
    sender: str
    when: date
    title: str
    doctype: str
    subject: str
    body: list[str]
    tags: list[str] = field(default_factory=list)
    amount: float | None = None
    number: tuple[str, str] | None = None  # (field name, value)
    table: list[tuple[str, str]] = field(default_factory=list)
    pages: int = 1
    summary: str = ""
    photo: bool = False  # delivered as a phone photo / scan
    paper: bool = True
    filed: bool = True
    inbox: str = ""  # "review" | "file" | "duplicate": left open for the inbox


def _money(v: float) -> str:
    return f"${v:,.2f}"


def build_docs() -> list[Doc]:
    out: list[Doc] = []
    # electricity: quarterly bills
    for i, q in enumerate([date(2024, 1, 12), date(2024, 4, 11), date(2024, 7, 12), date(2024, 10, 10),
                           date(2025, 1, 13), date(2025, 4, 10), date(2025, 7, 11), date(2025, 10, 13),
                           date(2026, 1, 12), date(2026, 4, 13), date(2026, 7, 10)]):  # fmt: skip
        kwh = rnd.randint(610, 980)
        amount = round(kwh * 0.187 + 24.5, 2)
        out.append(Doc(
            f"energy{i}", "energy", q, f"Electricity bill Q{(q.month - 1) // 3 + 1 - 1 or 4} "
            f"{q.year if q.month > 1 else q.year - 1}", "Invoice", "Your electricity bill",
            [f"Thank you for choosing Northwind Energy. This bill covers {kwh} kWh used at "
             "14 Linden Street.", f"The amount of {_money(amount)} will be debited from your "
             "account on file on the 25th."],
            ["Electricity", "Home"], amount, ("Customer number", "NW-448210"),
            [("Energy charge", _money(kwh * 0.187)), ("Basic service charge", _money(24.5)),
             ("Total", _money(amount))],
            summary=f"Quarterly electricity bill for {kwh} kWh, {_money(amount)} by direct debit.",
            paper=False, filed=False,
        ))  # fmt: skip
    # mobile phone: monthly bills (a selection)
    for i, m in enumerate([date(2025, 11, 3), date(2025, 12, 3), date(2026, 1, 5), date(2026, 2, 3),
                           date(2026, 3, 3), date(2026, 4, 3), date(2026, 5, 4), date(2026, 6, 3),
                           date(2026, 7, 3), date(2026, 8, 3), date(2026, 9, 3)]):  # fmt: skip
        amount = 39.99 if i != 4 else 52.39
        out.append(Doc(
            f"mobile{i}", "mobile", m, f"Mobile phone bill {m:%B %Y}", "Invoice",
            f"Your bill for {m:%B %Y}",
            ["Plan: Unlimited Talk & 20 GB", "Roaming: " + ("$12.40 (Canada)" if i == 4 else "none")],
            ["Phone"], amount, ("Customer number", "BL-2290-7713"),
            [("Monthly plan", "$39.99")] + ([("Roaming", "$12.40")] if i == 4 else [])
            + [("Total", _money(amount))],
            summary=f"Monthly phone bill, {_money(amount)}." + (" Includes roaming in Canada." if i == 4 else ""),
            paper=False, filed=False,
        ))  # fmt: skip
    # bank statements
    for i, m in enumerate([date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 30),
                           date(2026, 7, 31), date(2026, 8, 31)]):  # fmt: skip
        rows = [("Salary Lumen Analytics", "+$4,212.00"), ("Rent Riverside Property Mgmt", "-$1,450.00"),
                ("Brightline Mobile", "-$39.99"), ("Grocer's Market", f"-${rnd.randint(60, 190)}.{rnd.randint(10, 99)}"),
                ("Cedar Health Insurance", "-$318.40"), ("Streamly subscription", "-$15.49")]  # fmt: skip
        out.append(Doc(
            f"bank{i}", "bank", m, f"Account statement {m:%B %Y}", "Account statement",
            f"Statement for checking account ending 4471, {m:%B %Y}",
            ["Opening and closing balance and all transactions of the month."],
            ["Bank", "Finance"], None, ("Account", "…4471"), rows, pages=2,
            summary="Monthly statement of the checking account: salary, rent, phone, insurance, groceries.",
            paper=False, filed=False,
        ))  # fmt: skip
    D = Doc  # noqa: N806
    out += [
        D("lease", "landlord", date(2024, 2, 20), "Residential lease 14 Linden Street", "Contract",
          "Residential lease agreement",
          ["This lease is made between Riverside Property Management (landlord) and Alex Morgan "
           "(tenant) for the apartment at 14 Linden Street, 2nd floor.",
           "Term: unlimited, starting March 1, 2024. Notice period: three months.",
           "Monthly rent: $1,390.00, plus utilities as stated in the annual utility statement.",
           "Security deposit: $2,780.00."],
          ["Home", "Contracts"], 1390.0, ("Contract number", "RPM-L-2024-118"), pages=4,
          summary="Lease for the apartment at 14 Linden Street from March 2024, rent $1,390, three months notice.",
          photo=True),
        D("rent-increase", "landlord", date(2025, 11, 14), "Rent increase from January 2026", "Letter",
          "Adjustment of your monthly rent",
          ["In accordance with section 4 of your lease, we adjust the monthly rent from "
           "$1,390.00 to $1,450.00, effective January 1, 2026."],
          ["Home"], 1450.0, ("Contract number", "RPM-L-2024-118"),
          summary="Rent rises from $1,390 to $1,450 per month from January 2026."),
        D("utilities", "landlord", date(2026, 5, 6), "Utility cost statement 2025", "Statement",
          "Annual utility cost statement 2025",
          ["Heating, water, waste collection and building insurance for 2025.",
           "Your advance payments exceed the costs: a refund of $86.20 will be transferred."],
          ["Home"], -86.2, ("Contract number", "RPM-L-2024-118"),
          [("Heating", "$612.30"), ("Water", "$188.10"), ("Waste", "$96.00"), ("Insurance", "$117.40"),
           ("Advance payments", "-$1,100.00"), ("Refund", "$86.20")],
          summary="Utility statement for 2025 with a refund of $86.20.", inbox="review"),
        D("health-policy", "health", date(2024, 12, 2), "Health insurance policy 2025", "Policy",
          "Your policy documents for 2025",
          ["Plan: Cedar Select PPO. Monthly premium: $318.40.", "Deductible: $1,500 per year."],
          ["Health", "Insurance"], 318.4, ("Policy number", "CH-5561-0092"), pages=2,
          summary="Health insurance policy for 2025, Cedar Select PPO, $318.40 per month."),
        D("health-claim", "health", date(2026, 2, 19), "Claim settlement – physiotherapy", "Letter",
          "Settlement of your claim of February 2, 2026",
          ["We reimburse $240.00 of the $300.00 you submitted for six physiotherapy sessions."],
          ["Health", "Insurance"], 240.0, ("Policy number", "CH-5561-0092"),
          summary="Insurance pays $240 of $300 for physiotherapy."),
        D("doctor", "doctor", date(2026, 1, 28), "Invoice physiotherapy prescription", "Invoice",
          "Invoice for services on January 21, 2026",
          ["Consultation and prescription for physiotherapy."],
          ["Health"], 300.0, ("Invoice number", "PN-2026-0147"),
          [("Consultation", "$120.00"), ("Prescription", "$180.00"), ("Total", "$300.00")],
          summary="Doctor's invoice of $300 for consultation and physiotherapy prescription.", photo=True),
        D("car-renewal", "car", date(2025, 8, 30), "Car insurance renewal 2025/26", "Policy",
          "Renewal of your car insurance",
          ["Vehicle: 2019 Subaru Outback. Full coverage, deductible $500.",
           "Annual premium: $1,124.00, payable in two installments."],
          ["Car", "Insurance"], 1124.0, ("Policy number", "ACI-77-310-5"),
          summary="Car insurance renewed for 2025/26, $1,124 per year."),
        D("car-renewal2", "car", date(2026, 8, 28), "Car insurance renewal 2026/27", "Policy",
          "Renewal of your car insurance",
          ["Vehicle: 2019 Subaru Outback. Full coverage, deductible $500.",
           "Annual premium: $1,089.00 (no-claims discount applied)."],
          ["Car", "Insurance"], 1089.0, ("Policy number", "ACI-77-310-5"),
          summary="Car insurance renewed for 2026/27, now $1,089 per year.", filed=False, inbox="file"),
        D("property-tax", "tax", date(2025, 3, 10), "Property tax assessment 2025", "Tax assessment",
          "Notice of property tax assessment 2025",
          ["Assessed value of the parking space at 14 Linden Street: $18,400.",
           "Tax due: $212.00, payable by May 15, 2025."],
          ["Taxes"], 212.0, ("Assessment number", "MB-2025-04471"),
          summary="Property tax for the parking space, $212 due May 15, 2025."),
        D("property-tax2", "tax", date(2026, 3, 9), "Property tax assessment 2026", "Tax assessment",
          "Notice of property tax assessment 2026",
          ["Assessed value of the parking space at 14 Linden Street: $19,100.",
           "Tax due: $220.00, payable by May 15, 2026."],
          ["Taxes"], 220.0, ("Assessment number", "MB-2026-04471"),
          summary="Property tax for the parking space, $220 due May 15, 2026.", photo=True),
        D("contract-job", "employer", date(2023, 9, 1), "Employment contract", "Contract",
          "Employment contract",
          ["Position: Senior Data Analyst, full time, starting October 1, 2023.",
           "Annual salary: $78,000. Notice period: one month."],
          ["Work", "Contracts"], None, None, pages=3,
          summary="Employment contract as Senior Data Analyst from October 2023."),
        D("payslip", "employer", date(2026, 8, 31), "Payslip August 2026", "Payslip",
          "Payslip August 2026", ["Gross salary $6,500.00, net pay $4,212.00."],
          ["Work"], 4212.0, ("Employee number", "LA-1043"),
          [("Gross", "$6,500.00"), ("Taxes", "-$1,612.00"), ("Benefits", "-$676.00"), ("Net pay", "$4,212.00")],
          summary="Payslip for August 2026: net pay $4,212.", paper=False, filed=False),
        D("internet", "internet", date(2026, 6, 2), "Internet contract upgrade", "Contract",
          "Confirmation of your plan change",
          ["New plan: Fiber 500 for $54.00 per month from July 1, 2026. Minimum term 12 months."],
          ["Home", "Contracts"], 54.0, ("Customer number", "BW-88213"),
          summary="Internet upgraded to Fiber 500, $54 per month from July 2026.", inbox="review"),
        D("tv-receipt", "shop", date(2026, 9, 12), "Receipt 55\" TV with 2-year warranty", "Receipt",
          "Receipt", ["55\" OLED TV, model SE-55X9", "Extended warranty 2 years",
                      "Keep this receipt for warranty claims."],
          ["Warranty"], 1298.0, ("Receipt number", "SE-0912-3381"),
          [("TV SE-55X9", "$1,199.00"), ("Warranty 2 years", "$99.00"), ("Total", "$1,298.00")],
          summary="Receipt for a 55-inch TV incl. 2-year warranty, $1,298.", photo=True, filed=False,
          inbox="file"),
    ]  # fmt: skip
    # the same receipt again, e-mailed as PDF: a possible duplicate
    dup = next(d for d in out if d.key == "tv-receipt")
    out.append(Doc(**{**dup.__dict__, "key": "tv-receipt-mail", "photo": False, "paper": False,
                      "inbox": "duplicate"}))  # fmt: skip
    return out


# --- drawing --------------------------------------------------------------------------------


def _logo(c: canvas.Canvas, s: Sender, x: float, y: float) -> None:
    c.setFillColor(colors.HexColor(s.color))
    if s.shape == "circle":
        c.circle(x + 6 * mm, y + 4 * mm, 6 * mm, stroke=0, fill=1)
    elif s.shape == "square":
        c.roundRect(x, y - 2 * mm, 12 * mm, 12 * mm, 2 * mm, stroke=0, fill=1)
    else:
        for i in range(3):
            c.rect(x + i * 4.5 * mm, y - 2 * mm, 3 * mm, (6 + i * 3) * mm, stroke=0, fill=1)


def draw_pdf(d: Doc) -> bytes:
    s = SENDERS[d.sender]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    for page in range(1, d.pages + 1):
        _logo(c, s, 20 * mm, h - 30 * mm)
        c.setFillColor(colors.HexColor(s.color))
        c.setFont("Helvetica-Bold", 16)
        c.drawString(38 * mm, h - 28 * mm, s.name)
        c.setFillColor(colors.HexColor("#555555"))
        c.setFont("Helvetica", 8.5)
        c.drawString(38 * mm, h - 33 * mm, " · ".join(s.lines))
        c.setStrokeColor(colors.HexColor(s.color))
        c.setLineWidth(1.2)
        c.line(20 * mm, h - 38 * mm, w - 20 * mm, h - 38 * mm)
        c.setFillColor(colors.black)
        if page == 1:
            c.setFont("Helvetica", 10.5)
            for i, line in enumerate(RECIPIENT):
                c.drawString(20 * mm, h - 55 * mm - i * 5 * mm, line)
            c.drawRightString(w - 20 * mm, h - 55 * mm, d.when.strftime("%B %-d, %Y"))
            if d.number:
                c.drawRightString(w - 20 * mm, h - 60 * mm, f"{d.number[0]}: {d.number[1]}")
            c.setFont("Helvetica-Bold", 13)
            c.drawString(20 * mm, h - 85 * mm, d.subject)
            c.setFont("Helvetica", 10.5)
            y = h - 97 * mm
            c.drawString(20 * mm, y, "Dear Alex Morgan,")
            y -= 9 * mm
            for para in d.body:
                for line in _wrap(para, 92):
                    c.drawString(20 * mm, y, line)
                    y -= 5.2 * mm
                y -= 3 * mm
            if d.table:
                y -= 2 * mm
                for i, (label, value) in enumerate(d.table):
                    last = i == len(d.table) - 1
                    c.setFont("Helvetica-Bold" if last else "Helvetica", 10.5)
                    if last:
                        c.line(20 * mm, y + 4 * mm, w - 20 * mm, y + 4 * mm)
                    c.drawString(24 * mm, y, label)
                    c.drawRightString(w - 24 * mm, y, value)
                    y -= 6.5 * mm
            y -= 8 * mm
            c.setFont("Helvetica", 10.5)
            c.drawString(20 * mm, y, "Sincerely,")
            c.drawString(20 * mm, y - 6 * mm, s.name)
        else:
            c.setFont("Helvetica", 10.5)
            y = h - 55 * mm
            for n in range(22):
                c.drawString(20 * mm, y, f"{page}.{n + 1}  " + _filler(d, n))
                y -= 6 * mm
        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.HexColor("#777777"))
        c.drawString(20 * mm, 15 * mm, f"{s.name} · {s.lines[0]} · Page {page} of {d.pages}")
        c.showPage()
    c.save()
    return buf.getvalue()


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        if len(cur) + len(wd) + 1 > width:
            lines.append(cur)
            cur = wd
        else:
            cur = f"{cur} {wd}".strip()
    return lines + [cur] if cur else lines


def _filler(d: Doc, n: int) -> str:
    parts = ["The parties agree to the terms set out in this section.",
             "Payments are due on the first day of each month.",
             "Changes to this agreement must be made in writing.",
             "Notices are sent to the addresses stated above."]  # fmt: skip
    return parts[n % len(parts)]


def as_scan(pdf: bytes, stamp: bool = False) -> bytes:
    """All pages as a scanner would deliver them: an image-only PDF, off-white paper, a little
    skew and softness - the text comes from Tesseract."""
    import pypdfium2 as pdfium

    pages = []
    for page in pdfium.PdfDocument(pdf):
        img = page.render(scale=200 / 72).to_pil().convert("RGB")
        img = Image.blend(img, Image.new("RGB", img.size, (244, 240, 230)), 0.10)
        img = img.rotate(rnd.uniform(-0.8, 0.8), fillcolor=(250, 250, 248),
                         resample=Image.Resampling.BICUBIC)  # fmt: skip
        if stamp and not pages:
            _paid_stamp(img)
        pages.append(img.filter(ImageFilter.GaussianBlur(0.5)))
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:], resolution=200)
    return buf.getvalue()


# --- filling the archive --------------------------------------------------------------------


def _paid_stamp(img: Image.Image) -> None:
    """A red "PAID" stamp with a handwritten date: what only the paper copy has."""
    from PIL import ImageDraw, ImageFont

    w, h = img.size
    layer = Image.new("RGBA", (int(w * 0.3), int(h * 0.09)), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    red = (190, 30, 40, 210)
    d.rounded_rectangle([4, 4, layer.width - 5, layer.height - 5], radius=18, outline=red, width=7)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(layer.height * 0.42))
        small = ImageFont.truetype("DejaVuSans-Oblique.ttf", int(layer.height * 0.2))
    except OSError:
        font = small = ImageFont.load_default()
    d.text((layer.width * 0.1, layer.height * 0.12), "PAID", fill=red, font=font)
    d.text((layer.width * 0.1, layer.height * 0.64), "Sep 14 – A.M.", fill=(30, 50, 140, 230),
           font=small)  # fmt: skip
    layer = layer.rotate(-8, expand=True, resample=Image.Resampling.BICUBIC)
    img.paste(layer, (int(w * 0.58), int(h * 0.36)), layer)


def build(target: Path, language: str) -> None:
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"{target} is not empty")
    s = Settings(_env_file=None, archive_dir=target, ocr_provider="tesseract", ocr_languages="eng",
                 classify_provider="rules", auto_resolve_identical=False,
                 consume_dir=target / "consume")  # fmt: skip
    a = Archive(s)
    settings_store.save(a.conn, a.base_settings, {"language": language})  # as on the setup page
    auth.create_user(a.conn, "demo", "demo-password-1234")
    (target / "consume").mkdir(exist_ok=True)
    all_docs = build_docs()  # once: the values are random
    made: dict[str, str] = {}
    for d in sorted(all_docs, key=lambda d: d.when):
        pdf = draw_pdf(d)
        data = as_scan(pdf, stamp=d.key == "tv-receipt") if d.photo else pdf
        source = "scanner" if d.photo else ("email" if d.key.endswith("mail") else "web")
        r = ingest_stream(a, io.BytesIO(data), f"{d.key}.pdf", source, paper=d.paper or d.photo)
        made[d.key] = r.doc_id
    run_until_idle(a)
    for d in all_docs:
        doc_id = made[d.key]
        fields = {"title": d.title, "correspondent": SENDERS[d.sender].name, "document_type": d.doctype,
                  "tags": d.tags, "document_date": d.when.isoformat(), "summary": d.summary}  # fmt: skip
        custom = {}
        if d.amount is not None:
            custom["Amount"] = {"type": "monetary", "value": d.amount, "currency": "USD"}
        if d.number:
            custom[d.number[0]] = {"type": "string", "value": d.number[1]}
        if custom:
            fields["custom_fields"] = custom
        if d.inbox == "review":
            # left as AI suggestions for the inbox instead of set values
            keep = {k: v for k, v in fields.items() if k not in ("document_type", "tags")}
            docs.update_fields(a, doc_id, keep, lock_changed=False)
            _suggest(a, doc_id, [("document_type", d.doctype, 0.72), ("tags", d.tags, 0.66)])
            continue
        docs.update_fields(a, doc_id, fields)
        if (d.paper or d.photo) and d.filed and not d.inbox:
            docs.mark_filed(a, doc_id)
    _backdate(a, made, all_docs)
    jobs.requeue_expired(a.conn)
    from heftig.duplicates import check_document

    check_document(a, made["tv-receipt-mail"])
    a.close()
    print(f"Demo archive in {target} - user 'demo', password 'demo-password-1234'")


def _backdate(a: Archive, made: dict[str, str], all_docs: list[Doc]) -> None:
    """Arrival a few days after the letter's date, as if the archive had been in use for years
    (only for the demo: normally the arrival time is fixed)."""
    from datetime import datetime, time

    for d in all_docs:
        when = datetime.combine(d.when + timedelta(days=rnd.randint(1, 4)),
                                time(rnd.randint(8, 19), rnd.randint(0, 59)), UTC)  # fmt: skip
        stamp = min(when, datetime(2026, 9, 28, 18, 0, tzinfo=UTC)).isoformat()
        stamp = stamp.replace("+00:00", "Z")
        with docs.write_tx(a.conn):
            meta = docs.load_meta(a, made[d.key])
            meta.received_at = stamp
            for ev in meta.ingest_events:
                ev.at = stamp
            if meta.filed_at:
                meta.filed_at = stamp
                meta.filing_section = stamp[:7]
            docs.persist(a, meta)
            a.conn.execute("UPDATE documents SET received_at=? WHERE id=?", (stamp, meta.id))


def _suggest(a: Archive, doc_id: str, items: list[tuple[str, object, float]]) -> None:
    from heftig.models import Suggestion

    with docs.write_tx(a.conn):
        meta = docs.load_meta(a, doc_id)
        meta.suggestions = [
            Suggestion(field=f, value=v, confidence=c, reason="Suggested by the AI")
            for f, v, c in items
        ]
        meta.status = "needs_review"
        docs.persist(a, meta)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("target", type=Path)
    p.add_argument("--language", default="en", choices=["en", "de"])
    args = p.parse_args()
    build(args.target.expanduser().resolve(), args.language)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
