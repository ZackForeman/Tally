"""Stage 8: Argon drafts the dispute email; code writes every number in it.

Argon returns the subject and the words around the table. The table of jobs
and the total are rendered here from approved findings only. If the prose
mentions a pound figure the code didn't produce, the prose is replaced with a
plain template, so a made-up number can't reach the customer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from tally.agents.gemini import ARGON, Asker, obj
from tally.engine.money import Finding, Flag

SCHEMA = obj({
    "subject": {"type": "string"},
    "opening": {"type": "string"},
    "closing": {"type": "string"},
})

PROMPT = """Draft a short, firm, polite email from {haulier} to {customer}'s accounts team, disputing
self-bill invoices. A table of the jobs, each with its ticket evidence, goes between your opening and
closing; refer to it as "the table below" and to the sum as "the total below".
Do not write any amounts, dates or ticket numbers yourself; the table carries them.
Opening: say which invoices are affected and what kinds of error were found ({kinds}).
Closing: ask for a corrected self-bill or a credit within 14 days, note the ticket copies are attached,
and sign off from {haulier}. Plain British English, no exclamation marks.
Put the greeting, each paragraph and the sign-off on their own lines, separated by blank lines."""

_POUNDS = re.compile(r"£\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


@dataclass
class Draft:
    customer: str
    subject: str
    body: str
    total: Decimal
    used_template: bool  # Argon's prose was rejected or unavailable


def table(findings: list[Finding], jobs: dict, lines: dict) -> str:
    rows = [("Date", "Ticket", "Reg", "Invoice", "Issue", "Detail", "Amount")]
    for f in findings:
        job, line = jobs.get(f.job_id), lines.get(f.line_id)
        when = job.load_time.strftime("%d/%m/%Y") if job else (f.shift.date.strftime("%d/%m/%Y") if f.shift else "")
        rows.append((
            when,
            (job.ticket_no if job else None) or "-",
            job.vehicle_reg if job else (f.shift.reg if f.shift else "-"),
            line.invoice_no if line else "-",
            f.flag.value,
            f.detail,
            f"£{f.amount:,.2f}",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    fmt = lambda r: "  ".join(c.ljust(w) if i < 6 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths)))
    return "\n".join([fmt(rows[0]), "  ".join("-" * w for w in widths)] + [fmt(r) for r in rows[1:]])


async def draft(
    ask: Asker | None, haulier: str, customer: str, findings: list[Finding], jobs: dict, lines: dict
) -> Draft:
    total = sum((f.amount for f in findings), Decimal("0"))
    kinds = ", ".join(sorted({f.flag.value.lower() for f in findings if f.flag is not Flag.GUARANTEE_OFFSET}))
    prose, used_template = None, True
    if ask is not None:
        try:
            prose = await ask(ARGON, PROMPT.format(haulier=haulier, customer=customer, kinds=kinds), SCHEMA, thinking="medium")
        except Exception:
            prose = None
        if prose and not _invented_amounts(prose, findings, total):
            used_template = False
        else:
            prose = None
    prose = prose or {
        "subject": f"Self-bill discrepancies: {haulier}",
        "opening": f"Dear {customer} accounts team,\n\nWe have checked your recent self-bill invoices against our delivery tickets and found the discrepancies set out in the table below ({kinds}).",
        "closing": f"Please send a corrected self-bill or a credit for the total below within 14 days. Copies of the tickets are attached.\n\nKind regards,\n{haulier}",
    }
    body = "\n\n".join([
        prose["opening"].strip(),
        table(findings, jobs, lines),
        f"Total owed: £{total:,.2f}",
        prose["closing"].strip(),
    ])
    return Draft(customer, prose["subject"].strip(), body, total, used_template)


def _invented_amounts(prose: dict, findings: list[Finding], total: Decimal) -> bool:
    allowed = {f.amount for f in findings} | {total}
    text = " ".join(prose.values())
    return any(Decimal(m.replace(",", "")) not in allowed for m in _POUNDS.findall(text))
