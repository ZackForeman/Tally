"""Hard checks on extracted records (pipeline stage 4, the code half).

A failed check sends the original image and the failed rule back to Argon to
re-read; two failed attempts send the document to the review queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from tally.engine.models import Invoice, Job
from tally.engine.normalize import reg_key

NET_TOLERANCE_T = Decimal("0.02")
MONEY_TOLERANCE = Decimal("0.01")
VAT_RATE = Decimal("0.20")

# Current (AB12CDE), prefix (A123BCD), suffix (ABC123D) and dateless formats.
_UK_REG = re.compile(
    r"^(?:[A-Z]{2}[0-9]{2}[A-Z]{3}"
    r"|[A-Z][0-9]{1,3}[A-Z]{3}"
    r"|[A-Z]{3}[0-9]{1,3}[A-Z]"
    r"|[A-Z]{1,3}[0-9]{1,4}"
    r"|[0-9]{1,4}[A-Z]{1,3})$"
)


@dataclass(frozen=True)
class CheckFailure:
    rule: str
    field: str
    message: str  # written so Argon can act on it in a re-read prompt


def check_job(job: Job) -> list[CheckFailure]:
    failures = []
    if job.gross_kg is not None and job.tare_kg is not None and job.net_t is not None:
        computed = ((job.gross_kg - job.tare_kg) / 1000).quantize(Decimal("0.01"))
        if abs(computed - job.net_t) > NET_TOLERANCE_T:
            failures.append(CheckFailure(
                "net_weight", "net_t",
                f"net {job.net_t} t but gross {job.gross_kg} kg - tare {job.tare_kg} kg = {computed} t",
            ))
    if job.gross_kg is not None and job.tare_kg is not None and job.gross_kg < job.tare_kg:
        failures.append(CheckFailure("gross_above_tare", "gross_kg", f"gross {job.gross_kg} kg is below tare {job.tare_kg} kg"))
    if not is_uk_reg(job.vehicle_reg):
        failures.append(CheckFailure("uk_reg", "vehicle_reg", f"{job.vehicle_reg!r} is not a UK registration"))
    return failures


def check_invoice(invoice: Invoice) -> list[CheckFailure]:
    failures = []
    total = sum((line.net_amount for line in invoice.lines), Decimal("0"))
    if abs(total - invoice.net) > MONEY_TOLERANCE:
        failures.append(CheckFailure("lines_sum", "net", f"lines sum to £{total} but invoice net is £{invoice.net}"))
    if invoice.vat is not None:
        expected = (invoice.net * VAT_RATE).quantize(MONEY_TOLERANCE)
        if abs(expected - invoice.vat) > MONEY_TOLERANCE:
            failures.append(CheckFailure("vat", "vat", f"VAT £{invoice.vat} is not 20% of net £{invoice.net} (£{expected})"))
        if invoice.gross is not None and abs(invoice.net + invoice.vat - invoice.gross) > MONEY_TOLERANCE:
            failures.append(CheckFailure("gross", "gross", f"net £{invoice.net} + VAT £{invoice.vat} != gross £{invoice.gross}"))
    if invoice.period_start and invoice.period_end:
        for line in invoice.lines:
            if line.job_date and not invoice.period_start <= line.job_date <= invoice.period_end:
                failures.append(CheckFailure(
                    "date_in_period", f"lines[{line.line_id}].job_date",
                    f"{line.job_date} is outside the invoice period {invoice.period_start} to {invoice.period_end}",
                ))
    for line in invoice.lines:
        if line.vehicle_reg and not is_uk_reg(line.vehicle_reg):
            failures.append(CheckFailure("uk_reg", f"lines[{line.line_id}].vehicle_reg", f"{line.vehicle_reg!r} is not a UK registration"))
    return failures


def is_uk_reg(reg: str | None) -> bool:
    key = reg_key(reg)
    return bool(key and _UK_REG.match(key))
