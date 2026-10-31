"""Stage 3: Flash reads each document into JSON; code turns that into records.

The model transcribes. It reports numbers as printed, with their units, and
quotes the text behind each field. Unit conversion, net-from-gross-and-tare,
and every check happen here in code.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from tally.agents.gemini import READER, Asker, FilePart, nullable, obj
from tally.engine.checks import CheckFailure, check_invoice, check_job
from tally.engine.models import (
    Confidence,
    Daywork,
    Evidence,
    Invoice,
    InvoiceLine,
    Job,
    Kind,
    Leg,
    Setup,
    ShiftKind,
    Signatures,
)

UNIT = {"type": "string", "enum": ["kg", "t", "none"]}
EVIDENCE = {
    "type": "array",
    "items": obj({
        "field": {"type": "string"},
        "quote": {"type": "string"},
        "confidence": {"type": "string", "enum": [c.value for c in Confidence]},
    }),
}

JOB_RECORD = obj({
    "ticket_no": nullable("string"),
    "load_date": nullable("string", format="date", description="YYYY-MM-DD of the loaded weighing, not the tare"),
    "load_time": nullable("string", description="HH:MM, 24-hour"),
    "vehicle_reg": nullable("string"),
    "driver": nullable("string"),
    "customer": nullable("string"),
    "leg": {"type": "string", "enum": ["delivery", "return"]},
    "from_site": nullable("string"),
    "to_site": nullable("string"),
    "material": nullable("string"),
    "recycled": {"type": "boolean"},
    "gross": nullable("number"),
    "tare": nullable("number"),
    "gross_tare_unit": UNIT,
    "net": nullable("number"),
    "net_unit": UNIT,
    "miles": nullable("number"),
    "returned": nullable("number", description="weight in the Returned material box, in net_unit"),
    "full_return": {"type": "boolean"},
    "daywork_note": nullable("string"),
    "daywork_hours": nullable("number"),
    "daywork_chute": {"type": "boolean"},
    "time_on_site": nullable("string", description="YYYY-MM-DD HH:MM"),
    "time_off_site": nullable("string", description="YYYY-MM-DD HH:MM"),
    "arrived": nullable("string", description="YYYY-MM-DD HH:MM arrival at the ship-to site"),
    "unload_start": nullable("string", description="YYYY-MM-DD HH:MM start of unloading"),
    "loads_today": nullable("integer"),
    "internal_account": {"type": "boolean"},
    "driver_signed": {"type": "boolean"},
    "receiver_signed": {"type": "boolean"},
    "evidence": EVIDENCE,
})
TICKET_SCHEMA = obj({"records": {"type": "array", "items": JOB_RECORD}})

LINE_RECORD = obj({
    "ticket_no": nullable("string"),
    "job_date": nullable("string", format="date", description="YYYY-MM-DD"),
    "vehicle_reg": nullable("string"),
    "driver": nullable("string"),
    "description": {"type": "string"},
    "kind": {"type": "string", "enum": [k.value for k in Kind]},
    "shift": {"type": "string", "enum": ["day", "night", "weekend", "unknown"]},
    "tonnes": nullable("number"),
    "miles": nullable("number"),
    "rate": nullable("number", description="price per tonne-mile, if the line shows one"),
    "hours": nullable("number"),
    "net_amount": {"type": "number"},
})
INVOICE_SCHEMA = obj({
    "invoice_no": nullable("string"),
    "invoice_date": nullable("string", format="date", description="YYYY-MM-DD"),
    "customer": nullable("string"),
    "period_start": nullable("string", format="date", description="YYYY-MM-DD"),
    "period_end": nullable("string", format="date", description="YYYY-MM-DD"),
    "net": nullable("number"),
    "vat": nullable("number"),
    "gross": nullable("number"),
    "lines": {"type": "array", "items": LINE_RECORD},
    "evidence": EVIDENCE,
})

SYSTEM = """You transcribe UK haulage paperwork for {haulier}, a haulage subcontractor.
Rules:
- Transcribe what is written. Never correct a registration, weight or number; a later check does that.
- Report numbers exactly as printed, with their unit. Do not do arithmetic, except to read a written sum.
- If a field isn't on the document, use null. Don't guess.
- For every field you fill, add an evidence entry quoting the exact text you read, with confidence
  high (clearly printed), medium (legible but faint or handwritten) or low (a guess between readings).
- customer is the company that hired {haulier} for this job (the self-biller), never {haulier} itself.
  Known customers: {customers}. Use the matching name from that list when one fits."""

TICKET_PROMPT = """Read this {kind} into records: one record per leg (a delivery, or a return of material).
Notes on common layouts:
- Conveyance notes: SENT and ORDERED are running totals for the order. Use NET for this load.
  Key the record on the conveyance note number, not the WCON order reference, which repeats.
- Delivery notes (ePOD): the tare may be dated days earlier; load_date is the loaded weighing.
  arrived is arrival at the ship-to site; unload_start is when unloading began.
  "Loads Today: N" goes in loads_today.
- Waste transfer notes are usually return legs (leg "return"). A note such as "FULL RETURN" sets full_return.
- Daywork notes such as "4hrs Daywork (Chute)": put the note in daywork_note, hours in daywork_hours,
  daywork_chute true when the note says chute.
- Time on site, often a "Waiting time" line with Time in / Time out: put them in time_on_site and
  time_off_site and the line's wording in daywork_note. This is not the ePOD arrived/unload_start.
  If there is a Chute tickbox, daywork_chute is true only when the box is visibly ticked.
- load_time is when the load was weighed out; if the only time printed is the job start, use that.
- Recycled material (recycled asphalt, 6F5 crushed hardcore, "Recycled Out") sets recycled true.
- Internal transfers between the customer's own sites set internal_account true.
- driver_signed / receiver_signed: true only if that signature box has a signature in it."""

INVOICE_PROMPT = """Read this self-bill invoice. One entry in lines per job line, in order.
kind: load (haulage or recycled aggregate), return, daywork, waiting, or guarantee (a shift top-up).
shift: day, night or weekend when the line says so, otherwise unknown.
Copy each line's net amount exactly as printed."""


async def extract(ask: Asker, file: FilePart, doc_kind: str, setup: Setup) -> dict[str, Any]:
    system = system_for(setup)
    if doc_kind == "self_bill":
        return await ask(READER, INVOICE_PROMPT, INVOICE_SCHEMA, files=[file], system=system)
    prompt = TICKET_PROMPT.format(kind=doc_kind.replace("_", " "))
    return await ask(READER, prompt, TICKET_SCHEMA, files=[file], system=system)


def schema_for(doc_kind: str) -> dict[str, Any]:
    return INVOICE_SCHEMA if doc_kind == "self_bill" else TICKET_SCHEMA


def system_for(setup: Setup) -> str:
    return SYSTEM.format(haulier=setup.haulier, customers=", ".join(setup.customers) or "none yet")


# --- reading -> records -----------------------------------------------------


def build_jobs(
    data: dict[str, Any], doc_id: str, name: str, mark_failures: bool = False
) -> tuple[list[Job], list[CheckFailure]]:
    """Records from a ticket reading, and every check it fails.

    With ``mark_failures`` (after the last re-read), fields that still fail get
    a low-confidence evidence entry, so the job is flagged as weak evidence.
    """
    jobs, failures = [], []
    for i, r in enumerate(data.get("records", [])):
        where = f"records[{i}]"
        try:
            job = _job(r, f"{doc_id}-{i + 1}", name)
        except _Unreadable as e:
            failures.append(CheckFailure("required", f"{where}.{e.field}", str(e)))
            continue
        found = check_job(job)
        failures += [CheckFailure(f.rule, f"{where}.{f.field}", f.message) for f in found]
        if mark_failures and found:
            extra = [Evidence(field=f.field, quote=f.message, confidence=Confidence.LOW) for f in found]
            job = job.model_copy(update={"evidence": job.evidence + extra})
        jobs.append(job)
    return jobs, failures


def build_invoice(data: dict[str, Any], doc_id: str, name: str) -> tuple[Invoice | None, list[CheckFailure]]:
    try:
        invoice = _invoice(data, doc_id, name)
    except _Unreadable as e:
        return None, [CheckFailure("required", e.field, str(e))]
    return invoice, check_invoice(invoice)


class _Unreadable(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _job(r: dict[str, Any], job_id: str, name: str) -> Job:
    for f in ("load_date", "vehicle_reg", "customer"):
        if not r.get(f):
            raise _Unreadable(f, f"could not read {f}")
    day = _date(r["load_date"], "load_date")
    clock = _clock(r.get("load_time"))
    evidence = [Evidence.model_validate(e) for e in r.get("evidence", [])]
    if clock is None:
        evidence.append(Evidence(field="load_time", quote="(no time printed; assumed midday)", confidence=Confidence.LOW))

    gross_kg = _to_kg(r.get("gross"), r.get("gross_tare_unit"))
    tare_kg = _to_kg(r.get("tare"), r.get("gross_tare_unit"))
    net_t = _to_t(r.get("net"), r.get("net_unit"))
    if net_t is None and gross_kg is not None and tare_kg is not None:
        net_t = (gross_kg - tare_kg) / 1000

    daywork = _daywork(r, day)

    return Job(
        job_id=job_id,
        ticket_no=r.get("ticket_no"),
        load_time=datetime.combine(day, clock or time(12, 0)),
        vehicle_reg=r["vehicle_reg"],
        driver=r.get("driver"),
        customer=r["customer"],
        leg=Leg(r.get("leg") or "delivery"),
        from_site=r.get("from_site"),
        to_site=r.get("to_site"),
        material=r.get("material"),
        recycled=bool(r.get("recycled")),
        gross_kg=gross_kg,
        tare_kg=tare_kg,
        net_t=net_t,
        miles=_num(r.get("miles")),
        returned_t=_to_t(r.get("returned"), r.get("net_unit")),
        full_return=bool(r.get("full_return")),
        daywork=daywork,
        arrived=_moment(r.get("arrived"), day),
        unload_start=_moment(r.get("unload_start"), day),
        loads_today=r.get("loads_today"),
        internal_account=bool(r.get("internal_account")),
        signatures=Signatures(driver=bool(r.get("driver_signed")), receiver=bool(r.get("receiver_signed"))),
        source_file=name,
        evidence=evidence,
    )


def _daywork(r: dict[str, Any], day: date) -> Daywork | None:
    """Time on site is paid as daywork: a written note, or Time in / Time out.

    Chute work is paid on the hours written, or the whole time on site; the
    non-chute 30-minute deduction is applied by the pay rules, not here.
    """
    chute = bool(r.get("daywork_chute"))
    written = _num(r.get("daywork_hours"))
    on, off = _moment(r.get("time_on_site"), day), _moment(r.get("time_off_site"), day)
    on_site = None
    if on and off:
        if off < on:  # crossed midnight
            off += timedelta(days=1)
        on_site = Decimal((off - on).total_seconds()) / 3600
    hours = (written if chute and written is not None else on_site) or written
    if hours is None or not (r.get("daywork_note") or on_site is not None):
        return None
    return Daywork(chute=chute, hours=hours)


def _invoice(d: dict[str, Any], doc_id: str, name: str) -> Invoice:
    for f in ("invoice_no", "invoice_date", "customer", "net"):
        if d.get(f) is None:
            raise _Unreadable(f, f"could not read {f}")
    inv_date = _date(d["invoice_date"], "invoice_date")
    lines = []
    for i, l in enumerate(d.get("lines", [])):
        shift = l.get("shift")
        lines.append(InvoiceLine(
            line_id=f"{doc_id}-L{i + 1}",
            invoice_no=d["invoice_no"],
            invoice_date=inv_date,
            customer=d["customer"],
            kind=Kind(l.get("kind") or "load"),
            job_date=_date(l["job_date"], f"lines[{i}].job_date") if l.get("job_date") else None,
            shift=ShiftKind(shift) if shift in {"day", "night", "weekend"} else None,
            ticket_no=l.get("ticket_no"),
            vehicle_reg=l.get("vehicle_reg"),
            driver=l.get("driver"),
            description=l.get("description") or "",
            tonnes=_num(l.get("tonnes")),
            miles=_num(l.get("miles")),
            rate=_num(l.get("rate")),
            hours=_num(l.get("hours")),
            net_amount=_num(l["net_amount"]),
            source_file=name,
        ))
    return Invoice(
        invoice_no=d["invoice_no"],
        invoice_date=inv_date,
        customer=d["customer"],
        period_start=_date(d["period_start"], "period_start") if d.get("period_start") else None,
        period_end=_date(d["period_end"], "period_end") if d.get("period_end") else None,
        net=_num(d["net"]),
        vat=_num(d.get("vat")),
        gross=_num(d.get("gross")),
        lines=lines,
        source_file=name,
    )


def _num(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _to_kg(v: Any, unit: str | None) -> Decimal | None:
    n = _num(v)
    if n is None:
        return None
    return n * 1000 if _unit(n, unit) == "t" else n


def _to_t(v: Any, unit: str | None) -> Decimal | None:
    n = _num(v)
    if n is None:
        return None
    return n / 1000 if _unit(n, unit) == "kg" else n


def _unit(n: Decimal, unit: str | None) -> str:
    """Unlabelled weights: no single load is over 100 t, so a bigger number is kg."""
    if unit in {"kg", "t"}:
        return unit
    return "kg" if n > 100 else "t"


def _date(v: str, field: str) -> date:
    for parse in (lambda x: date.fromisoformat(x[:10]),
                  lambda x: datetime.strptime(x.strip(), "%d/%m/%Y").date(),
                  lambda x: datetime.strptime(x.strip(), "%d/%m/%y").date()):
        try:
            return parse(v)
        except ValueError:
            pass
    raise _Unreadable(field, f"{field} {v!r} is not a date")


def _clock(v: str | None) -> time | None:
    if not v:
        return None
    try:
        return time.fromisoformat(v.strip()[:5])
    except ValueError:
        return None


def _moment(v: str | None, day: date | None = None) -> datetime | None:
    """A date-time, or a bare HH:MM on ``day``."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.strip())
    except ValueError:
        pass
    clock = _clock(v)
    return datetime.combine(day, clock) if clock and day else None


def reading_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, default=str)
