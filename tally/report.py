"""Turn a run into what the dashboard shows. Every amount comes from engine.money."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from tally.engine.match import Status
from tally.engine.models import Setup
from tally.engine.money import CLAIM_FLAGS, Finding, Flag, Reconciliation
from tally.engine.shifts import Shift
from tally.pipeline import RunState, recompute

ZERO = Decimal("0")


def doc_of(record_id: str | None) -> str | None:
    """Job and line ids start with the id of the document they were read from."""
    return record_id.split("-")[0] if record_id else None


def approved(run: RunState, f: Finding) -> bool:
    return run.approvals.get(f.finding_id, f.claim)


def claimed_findings(run: RunState, rec: Reconciliation, customer: str | None = None) -> list[Finding]:
    return [
        f for f in rec.findings
        if f.flag in CLAIM_FLAGS and approved(run, f) and (customer is None or f.customer == customer)
    ]


def build_report(run: RunState, setup: Setup) -> dict[str, Any]:
    rec = recompute(run, setup)
    jobs = {m.item.job.job_id: m.item.job for m in rec.matches.matches}
    lines = {l.line_id: l for l in rec.matches.lines}
    reasoning = {
        (m.item.job.job_id, m.line.line_id if m.line else None): m.reasoning
        for m in rec.matches.matches if m.reasoning
    }

    findings = []
    for f in rec.findings:
        job, line = jobs.get(f.job_id), lines.get(f.line_id)
        when = job.load_time if job else None
        findings.append({
            "id": f.finding_id,
            "flag": f.flag.value,
            "money": f.flag in CLAIM_FLAGS,
            "amount": str(f.amount),
            "customer": f.customer,
            "detail": f.detail,
            "date": (when.date() if when else f.shift.date if f.shift else None),
            "time": when.strftime("%H:%M") if when else None,
            "shift": _shift(f.shift),
            "claim_default": f.claim,
            "approved": approved(run, f),
            "job": job.model_dump(mode="json") if job else None,
            "line": line.model_dump(mode="json") if line else None,
            "doc_id": doc_of(f.job_id) or doc_of(f.line_id),
            "reasoning": reasoning.get((f.job_id, f.line_id)),
        })
    findings.sort(key=lambda f: (f["customer"], str(f["date"] or ""), f["time"] or ""))

    customers: dict[str, dict[str, Any]] = defaultdict(lambda: {"owed": ZERO, "owed_all": ZERO, "count": 0, "oldest": None})
    for f in rec.findings:
        if f.flag not in CLAIM_FLAGS:
            continue
        c = customers[f.customer]
        c["owed_all"] += f.amount
        c["count"] += 1
        if approved(run, f):
            c["owed"] += f.amount
        day = jobs[f.job_id].load_time.date() if f.job_id in jobs else (f.shift.date if f.shift else None)
        if day and (c["oldest"] is None or day < c["oldest"]):
            c["oldest"] = day
    by_customer = sorted(
        ({"customer": k, **{n: str(v) if isinstance(v, Decimal) else v for n, v in c.items()}} for k, c in customers.items()),
        key=lambda c: str(c["oldest"] or "9999"),
    )

    counts: dict[str, int] = defaultdict(int)
    for f in rec.findings:
        counts[f.flag.value] += 1

    open_matches = [
        {
            "item_id": m.item.item_id,
            "kind": m.item.kind,
            "job": m.item.job.model_dump(mode="json"),
            "candidates": [c.model_dump(mode="json") for c in m.candidates],
        }
        for m in rec.matches.by_status(Status.CANDIDATE)
    ]
    adjudicated = [
        {"item_id": m.item.item_id, "line_id": m.line.line_id if m.line else None, "reasoning": m.reasoning,
         "status": m.status}
        for m in rec.matches.matches if m.reasoning
    ]

    return {
        "run_id": run.run_id,
        "created": run.created,
        "status": run.status,
        "seconds": run.seconds,
        "haulier": setup.haulier,
        "totals": {
            "owed": str(rec.owed(claimed_only=True)),
            "owed_all": str(rec.owed()),
            "overpaid": str(rec.overpaid()),
            "line_errors": sum(1 for f in rec.findings if f.flag in CLAIM_FLAGS and f.flag not in {Flag.GUARANTEE_NOT_PAID, Flag.GUARANTEE_OFFSET}),
            "guarantees": counts.get(Flag.GUARANTEE_NOT_PAID.value, 0),
            "offset": str(sum((f.amount for f in rec.by_flag(Flag.GUARANTEE_OFFSET)), ZERO)),
            "by_flag": dict(counts),
        },
        "by_customer": by_customer,
        "findings": findings,
        "shifts": [
            {**_shift(s.shift), "customer": s.customer, "guarantee": str(s.guarantee), "earned": str(s.earned),
             "paid": str(s.paid), "top_up_paid": str(s.top_up_paid), "claim": str(s.claim), "pending": s.pending}
            for s in rec.shifts
        ],
        "open_matches": open_matches,
        "adjudicated": adjudicated,
        "docs": [_doc(d) for d in run.docs],
        "duplicates": run.duplicates,
        "audit_status": run.audit_status,
        "audit": run.audit,
        "matched": sum(1 for m in rec.matches.matches if m.status is Status.MATCHED),
        "items": len(rec.matches.matches),
        "invoice_lines": len(rec.matches.lines),
    }


def _shift(s: Shift | None) -> dict[str, Any] | None:
    return {"reg": s.reg, "date": s.date, "kind": s.kind} if s else None


def _doc(d) -> dict[str, Any]:
    return {
        "doc_id": d.doc_id,
        "name": d.name,
        "mime": d.mime,
        "kind": d.kind,
        "status": d.status,
        "route_reason": d.route_reason,
        "records": len(d.jobs) or (len(d.invoice.lines) if d.invoice else 0),
        "attempts": [a.model_dump() for a in d.attempts],
        "problems": d.problems,
        "error": d.error,
    }
