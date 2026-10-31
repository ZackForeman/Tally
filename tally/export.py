"""Evidence pack: a PDF with the claimed jobs, then a copy of every ticket behind them."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tally.engine.money import Finding
from tally.pipeline import RunState

RASTER = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def evidence_pdf(run: RunState, haulier: str, customer: str, findings: list[Finding], jobs: dict, lines: dict) -> bytes:
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    total = sum((f.amount for f in findings), Decimal("0"))

    story = [
        Paragraph(f"Self-bill discrepancies: {customer}", styles["Title"]),
        Paragraph(f"Prepared by {haulier} on {date.today():%d %B %Y}. Every amount is calculated from the "
                  "tickets and the customer's own self-bill lines; ticket copies follow.", styles["BodyText"]),
        Spacer(1, 6 * mm),
    ]
    rows = [["Date", "Ticket", "Reg", "Invoice", "Issue", "Detail", "Amount"]]
    for f in findings:
        job, line = jobs.get(f.job_id), lines.get(f.line_id)
        rows.append([
            job.load_time.strftime("%d/%m/%y") if job else (f.shift.date.strftime("%d/%m/%y") if f.shift else ""),
            (job.ticket_no if job else None) or "-",
            job.vehicle_reg if job else (f.shift.reg if f.shift else "-"),
            line.invoice_no if line else "-",
            Paragraph(f.flag.value, small),
            Paragraph(f.detail, small),
            f"£{f.amount:,.2f}",
        ])
    rows.append(["", "", "", "", "", Paragraph("<b>Total owed</b>", small), f"£{total:,.2f}"])
    table = Table(rows, colWidths=[16 * mm, 18 * mm, 20 * mm, 20 * mm, 26 * mm, 62 * mm, 20 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(table)

    docs = {d.doc_id: d for d in run.docs}
    attached: list[Path] = []
    for f in findings:
        for record_id in (f.job_id, f.line_id):
            doc = docs.get((record_id or "").split("-")[0])
            if doc and Path(doc.path) not in attached:
                attached.append(Path(doc.path))
                if Path(doc.path).suffix.lower() in RASTER:
                    story += [PageBreak(), Paragraph(f"Evidence: {doc.name}", styles["Heading2"]), _image(Path(doc.path))]

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm,
                      title=f"Evidence pack: {customer}").build(story)

    out = PdfWriter()
    for page in PdfReader(io.BytesIO(buf.getvalue())).pages:
        out.add_page(page)
    for path in attached:  # PDF tickets and self-bills are appended page for page
        if path.suffix.lower() == ".pdf":
            for page in PdfReader(path).pages:
                out.add_page(page)
    result = io.BytesIO()
    out.write(result)
    return result.getvalue()


def _image(path: Path) -> Image:
    w, h = ImageReader(str(path)).getSize()
    scale = min(180 * mm / w, 240 * mm / h)
    return Image(str(path), width=w * scale, height=h * scale)
