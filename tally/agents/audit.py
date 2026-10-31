"""Stage 7: Argon reads the whole set in one context and reports patterns.

Patterns are explanations, not money: a customer that drops Friday loads, a
rate that changed mid-month, a run of unsigned tickets. Nothing here changes
a total.
"""

from __future__ import annotations

import json
from typing import Any

from tally.agents.gemini import ARGON, Asker, obj
from tally.engine.models import InvoiceLine
from tally.engine.money import Reconciliation

SCHEMA = obj({
    "patterns": {
        "type": "array",
        "items": obj({
            "title": {"type": "string"},
            "detail": {"type": "string"},
            "customer": {"type": ["string", "null"]},
            "refs": {"type": "array", "items": {"type": "string"}},
        }),
    }
})

PROMPT = """You are auditing a haulage subcontractor's self-bill invoices against their tickets.
Below is every ticket, every invoice line, every truck-shift and every discrepancy code has already found.
Report patterns that no single row shows. Examples: a customer that keeps dropping loads on one weekday
or one truck; a rate that changed mid-month without notice; unpaid guarantees clustering on one truck or
on night shifts; a run of tickets with no receiver signature from one site.

Rules:
- Each pattern must rest on at least two rows. Put their ids (job ids, line ids, shift keys) in refs.
- Do not restate single discrepancies already listed, and do not calculate or state money amounts;
  the code owns the numbers.
- Say how the haulier could act on it (what to check, what to ask the customer). Two sentences at most.
- If there is no real pattern, return an empty list. Don't pad.

DATA:
{data}"""


def audit_data(rec: Reconciliation) -> dict[str, Any]:
    jobs = {m.item.job.job_id: m.item.job for m in rec.matches.matches}
    return {
        "tickets": [
            {
                "id": j.job_id, "ticket": j.ticket_no, "time": j.load_time.isoformat(timespec="minutes"),
                "weekday": j.load_time.strftime("%a"), "reg": j.vehicle_reg, "customer": j.customer,
                "leg": j.leg, "net_t": str(j.net_t) if j.net_t is not None else None, "miles": str(j.miles) if j.miles is not None else None,
                "material": j.material, "receiver_signed": j.signatures.receiver, "source": j.source,
                "site": j.to_site,
            }
            for j in jobs.values()
        ],
        "invoice_lines": [_line(l) for l in rec.matches.lines],
        "matches": [
            {"item": m.item.item_id, "status": m.status, "line": m.line.line_id if m.line else None}
            for m in rec.matches.matches
        ],
        "shifts": [
            {"shift": f"{s.shift.reg}/{s.shift.date}/{s.shift.kind}", "weekday": s.shift.date.strftime("%a"),
             "customer": s.customer, "earned": str(s.earned), "paid": str(s.paid),
             "top_up_paid": str(s.top_up_paid), "guarantee": str(s.guarantee)}
            for s in rec.shifts
        ],
        "discrepancies": [
            {"flag": f.flag.value, "customer": f.customer, "detail": f.detail, "job": f.job_id,
             "line": f.line_id, "shift": f"{f.shift.reg}/{f.shift.date}/{f.shift.kind}" if f.shift else None}
            for f in rec.findings
        ],
    }


def _line(l: InvoiceLine) -> dict[str, Any]:
    return {
        "id": l.line_id, "invoice": l.invoice_no, "date": str(l.job_date) if l.job_date else None,
        "customer": l.customer, "kind": l.kind, "shift": l.shift, "reg": l.vehicle_reg,
        "ticket": l.ticket_no, "tonnes": str(l.tonnes) if l.tonnes is not None else None,
        "miles": str(l.miles) if l.miles is not None else None,
        "rate": str(l.rate) if l.rate is not None else None, "amount": str(l.net_amount),
        "description": l.description,
    }


async def audit(ask: Asker, rec: Reconciliation) -> list[dict[str, Any]]:
    prompt = PROMPT.format(data=json.dumps(audit_data(rec), separators=(",", ":")))
    out = await ask(ARGON, prompt, SCHEMA, thinking="high", background=True)
    return out["patterns"]
