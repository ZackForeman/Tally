"""A small fictional document set for a live smoke test of the whole pipeline.

    uv run python -m tally.demo.smoke              # write the files to data/smoke
    uv run python -m tally.demo.smoke --run        # ...then run them through Gemini and check the answer

Kestrel Haulage, Greystone Aggregates, September 2026. Two self-bills, five
conveyance notes and a work log. Each case below has a hand-worked answer:

1 Sep, KX21 HLG (self-bill GA-SB-0901)
  CN-10231, CN-10232  work log only (no ticket photo), paid correctly       -> nothing
  CN-10233            prints gross 14,500 - tare 12,500 but a handwritten
                      net of 2.6 t; paid 2.00 t. The net-weight check fails,
                      Argon re-reads and keeps 2.6 (it's what's written), so
                      the note goes to review and the claim starts unticked  -> £4.50 short tonnage, unticked
  CN-10234            not on the self-bill: 19.92 t x 30 mi x £0.25          -> £149.40 missing job

2 Sep, KX68 DWV (self-bill GA-SB-0902): waiting time, Time in to Time out,
paid as daywork: non-chute (hours - 30 min) x £40, chute hours x £50
  CN-10301            2 h 35, no chute, not on the self-bill                 -> £83.33 daywork not paid
  CN-10302            3 h 00, CHUTE ticked, paid £100 at the non-chute rate  -> £50.00 daywork short
  CN-10303            1 h 15, no chute, paid £30.00                          -> nothing

The work log lists every job above, including the waiting times. Its rows share
ticket numbers with the notes, so they merge and add nothing: the log must not
double any claim. Both shifts earn well over the £200 day guarantee.
Expected: £282.73 owed on ticked lines, £287.23 if every line were claimed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

INK = (30, 40, 160)  # handwriting

# (flag, ticket) -> (amount, ticked by default)
EXPECTED = {
    ("Short tonnage", "CN-10233"): ("4.50", False),
    ("Missing job", "CN-10234"): ("149.40", True),
    ("Daywork not paid", "CN-10301"): ("83.33", True),
    ("Daywork not paid", "CN-10302"): ("50.00", True),
}
EXPECTED_OWED, EXPECTED_OWED_ALL = "282.73", "287.23"


@dataclass
class Waiting:
    time_in: str
    time_out: str
    chute: bool


@dataclass
class Note:
    no: str
    date: str
    reg: str
    driver: str
    job_start: str
    weighed: str
    gross: str
    tare: str
    net: str  # kg, or tonnes when handwritten
    material: str
    to_site: str
    handwritten_net: bool = False
    waiting: Waiting | None = None


NOTES = [
    Note("CN-10233", "01/09/2026", "KX21 HLG", "A. Driver", "12:40", "13:05", "14,500", "12,500", "2.6",
         "Type 1 sub-base", "Site B", handwritten_net=True),
    Note("CN-10234", "01/09/2026", "KX21 HLG", "A. Driver", "15:10", "15:40", "32,180", "12,260", "19,920",
         "20mm aggregate", "Site B"),
    Note("CN-10301", "02/09/2026", "KX68 DWV", "B. Driver", "06:30", "06:55", "32,000", "12,000", "20,000",
         "20mm aggregate", "Site C", waiting=Waiting("09:10", "11:45", chute=False)),
    Note("CN-10302", "02/09/2026", "KX68 DWV", "B. Driver", "11:50", "12:20", "31,600", "12,000", "19,600",
         "Concrete sand", "Site D", waiting=Waiting("13:00", "16:00", chute=True)),
    Note("CN-10303", "02/09/2026", "KX68 DWV", "B. Driver", "16:30", "16:50", "30,400", "12,000", "18,400",
         "20mm aggregate", "Site C", waiting=Waiting("17:20", "18:35", chute=False)),
]

# invoice no -> lines of (ticket, date, reg, description, tonnes, miles, rate, net £)
SELF_BILLS = {
    "GA-SB-0901": [
        ("CN-10231", "01/09/2026", "KX21 HLG", "20mm aggregate, Quarry -> Site A", "20.00", "30", "0.25", "150.00"),
        ("CN-10232", "01/09/2026", "KX21 HLG", "20mm aggregate, Quarry -> Site A", "19.40", "30", "0.25", "145.50"),
        ("CN-10233", "01/09/2026", "KX21 HLG", "Type 1 sub-base, Quarry -> Site B", "2.00", "30", "0.25", "15.00"),
    ],
    "GA-SB-0902": [
        ("CN-10301", "02/09/2026", "KX68 DWV", "20mm aggregate, Quarry -> Site C", "20.00", "30", "0.25", "150.00"),
        ("CN-10302", "02/09/2026", "KX68 DWV", "Concrete sand, Quarry -> Site D", "19.60", "30", "0.25", "147.00"),
        ("CN-10302", "02/09/2026", "KX68 DWV", "Daywork 2.50 hrs @ £40/hr", "", "", "", "100.00"),
        ("CN-10303", "02/09/2026", "KX68 DWV", "20mm aggregate, Quarry -> Site C", "18.40", "30", "0.25", "138.00"),
        ("CN-10303", "02/09/2026", "KX68 DWV", "Daywork 0.75 hrs @ £40/hr", "", "", "", "30.00"),
    ],
}

# The driver's log records every job too. Rows that share a ticket number with a
# conveyance note merge into it, so nothing is counted twice; the log adds no money.
WORK_LOG = """date,time,reg,driver,customer,ticket_no,type,from_site,to_site,material,net_t,miles,time_in,time_out,chute
2026-09-01,07:30,KX21 HLG,A Driver,Greystone Aggregates,CN-10231,load,Greystone Quarry,Site A,20mm aggregate,20.00,30,,,
2026-09-01,10:15,KX21 HLG,A Driver,Greystone Aggregates,CN-10232,load,Greystone Quarry,Site A,20mm aggregate,19.40,30,,,
2026-09-01,13:05,KX21 HLG,A Driver,Greystone Aggregates,CN-10233,load,Greystone Quarry,Site B,Type 1 sub-base,2.00,30,,,
2026-09-01,15:40,KX21 HLG,A Driver,Greystone Aggregates,CN-10234,load,Greystone Quarry,Site B,20mm aggregate,19.92,30,,,
2026-09-02,06:55,KX68 DWV,B Driver,Greystone Aggregates,CN-10301,load,Greystone Quarry,Site C,20mm aggregate,20.00,30,,,
2026-09-02,09:10,KX68 DWV,B Driver,Greystone Aggregates,CN-10301,daywork,,Site C,,,,09:10,11:45,n
2026-09-02,12:20,KX68 DWV,B Driver,Greystone Aggregates,CN-10302,load,Greystone Quarry,Site D,Concrete sand,19.60,30,,,
2026-09-02,13:00,KX68 DWV,B Driver,Greystone Aggregates,CN-10302,daywork,,Site D,,,,13:00,16:00,y
2026-09-02,16:50,KX68 DWV,B Driver,Greystone Aggregates,CN-10303,load,Greystone Quarry,Site C,20mm aggregate,18.40,30,,,
2026-09-02,17:20,KX68 DWV,B Driver,Greystone Aggregates,CN-10303,daywork,,Site C,,,,17:20,18:35,n
"""


def font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("DejaVuSansMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def conveyance_note(path: Path, n: Note) -> None:
    img = Image.new("RGB", (900, 720), "white")
    d = ImageDraw.Draw(img)
    f, big, hand = font(22), font(28), font(30)
    d.text((30, 20), "GREYSTONE AGGREGATES - CONVEYANCE NOTE", font=big, fill="black")
    rows = [
        f"Note No: {n.no}",
        f"Date: {n.date}   Job start: {n.job_start}   Weighed out: {n.weighed}",
        "Haulier: Kestrel Haulage Ltd",
        f"Vehicle: {n.reg}   Driver: {n.driver}",
        f"Material: {n.material}",
        f"From: Greystone Quarry   To: {n.to_site}   MILEAGE: 30",
        f"GROSS: {n.gross} kg",
        f"TARE:  {n.tare} kg",
    ]
    for i, r in enumerate(rows):
        d.text((30, 80 + i * 38), r, font=f, fill="black")
    y = 80 + len(rows) * 38
    if n.handwritten_net:
        d.text((30, y), "NET:", font=f, fill="black")
        d.text((110, y - 6), f"{n.net} t", font=font(34), fill=INK)
    else:
        d.text((30, y), f"NET:   {n.net} kg", font=f, fill="black")

    y += 58
    d.rectangle((20, y - 10, 880, y + 70), outline="black", width=2)
    d.text((30, y), "WAITING TIME", font=f, fill="black")
    d.text((30, y + 36), "Time in:", font=f, fill="black")
    d.text((300, y + 36), "Time out:", font=f, fill="black")
    d.text((640, y + 36), "Chute", font=f, fill="black")
    d.rectangle((725, y + 36, 753, y + 64), outline="black", width=2)
    if n.waiting:
        d.text((140, y + 30), n.waiting.time_in, font=hand, fill=INK)
        d.text((425, y + 30), n.waiting.time_out, font=hand, fill=INK)
        if n.waiting.chute:  # a pen tick
            d.line((729, y + 50, 738, y + 61), fill=INK, width=4)
            d.line((738, y + 61, 760, y + 26), fill=INK, width=4)

    # Signatures must look handwritten: a typed name is fairly read as unsigned.
    d.text((30, 640), "Driver sig:", font=f, fill="black")
    d.text((180, 632), n.driver.replace(" ", ""), font=hand, fill=INK)
    d.text((440, 640), "Received by:", font=f, fill="black")
    d.text((610, 632), "P. Jones", font=hand, fill=INK)
    img.save(path)


def self_bill(path: Path, invoice_no: str, lines: list[tuple]) -> None:
    net = sum(Decimal(line[-1]) for line in lines)
    vat = (net * Decimal("0.20")).quantize(Decimal("0.01"))
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 280 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, y, "GREYSTONE AGGREGATES LTD - SELF-BILLING INVOICE")
    c.setFont("Helvetica", 10)
    for text in ["Supplier: Kestrel Haulage Ltd", f"Invoice No: {invoice_no}    Invoice date: 30/09/2026",
                 "Period: 01/09/2026 to 30/09/2026"]:
        y -= 7 * mm
        c.drawString(20 * mm, y, text)
    y -= 12 * mm
    cols = [20, 42, 64, 86, 140, 156, 168, 182]
    c.setFont("Helvetica-Bold", 8)
    for x, h in zip(cols, ["Ticket", "Date", "Vehicle", "Description", "Tonnes", "Miles", "Rate/t-mi", "Net £"]):
        c.drawString(x * mm, y, h)
    c.setFont("Helvetica", 8)
    for line in lines:
        y -= 6 * mm
        for x, v in zip(cols, line):
            c.drawString(x * mm, y, v)
    y -= 12 * mm
    c.setFont("Helvetica", 10)
    for text in [f"Net: £{net:,.2f}", f"VAT @ 20%: £{vat:,.2f}", f"Total: £{net + vat:,.2f}"]:
        c.drawString(150 * mm, y, text)
        y -= 6 * mm
    c.save()


def write(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():  # the set changed shape over time; don't upload stale files
        old.unlink()
    files = []
    for invoice_no, lines in SELF_BILLS.items():
        files.append(out / f"self-bill-{invoice_no}.pdf")
        self_bill(files[-1], invoice_no, lines)
    for n in NOTES:
        files.append(out / f"ticket-{n.no}.png")
        conveyance_note(files[-1], n)
    files.append(out / "worklog.csv")
    files[-1].write_text(WORK_LOG)
    return files


def run_and_check(files: list[Path]) -> bool:
    """Send the set through the live pipeline with the demo set-up, and compare to the answer."""
    from tally.agents.gemini import Gemini
    from tally.config import DEMO_SETUP
    from tally.engine.models import Setup
    from tally.pipeline import Runner
    from tally.report import build_report
    from tally.store import Store

    setup = Setup.model_validate_json(DEMO_SETUP.read_text())

    async def go():
        runner = Runner(Store(), Gemini(), lambda: setup)
        run = runner.start([(p.name, p.read_bytes()) for p in files])
        print(f"run {run.run_id}: {len(run.docs)} documents, waiting for Gemini…")
        while runner._tasks:
            await asyncio.gather(*list(runner._tasks))
        return run

    run = asyncio.run(go())
    report = build_report(run, setup)

    print(f"\ndone in {run.seconds} s")
    for d in report["docs"]:
        print(f"  {d['name']:<26} {d['status']}")

    got = {
        (f["flag"], (f["job"] or {}).get("ticket_no")): (f["amount"], f["approved"])
        for f in report["findings"] if f["money"]
    }
    ok = True
    print("\nclaims")
    for key in sorted(EXPECTED.keys() | got.keys(), key=lambda k: str(k[1])):
        want, have = EXPECTED.get(key), got.get(key)
        good = want == have
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {key[1]:<9} {key[0]:<18} expected {_fmt(want):<18} got {_fmt(have)}")
    for name, want, have in [("owed (ticked)", EXPECTED_OWED, report["totals"]["owed"]),
                             ("owed (all)", EXPECTED_OWED_ALL, report["totals"]["owed_all"])]:
        good = Decimal(want) == Decimal(have)
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name:<28} expected £{want:<16} got £{have}")
    print(f"\n{'PASSED' if ok else 'FAILED'}: open http://localhost:8000/#run={run.run_id} to look at it")
    return ok


def _fmt(v) -> str:
    return "-" if v is None else f"£{v[0]} {'ticked' if v[1] else 'unticked'}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", nargs="?", default="data/smoke", type=Path)
    parser.add_argument("--run", action="store_true", help="run the set through Gemini and check the answer")
    args = parser.parse_args()
    files = write(args.out)
    print(f"wrote {len(files)} files to {args.out}")
    if args.run and not run_and_check(files):
        sys.exit(1)


if __name__ == "__main__":
    main()
