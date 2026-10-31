"""Stage 2: classify each upload with Flash-Lite."""

from __future__ import annotations

from enum import StrEnum

from tally.agents.gemini import ROUTER, Asker, FilePart, obj


class DocKind(StrEnum):
    SELF_BILL = "self_bill"
    TICKET = "ticket"  # weighbridge ticket, conveyance note, delivery note, POD, docket
    WASTE_TRANSFER_NOTE = "waste_transfer_note"
    WORK_LOG = "work_log"
    OTHER = "other"


SCHEMA = obj({
    "kind": {"type": "string", "enum": [k.value for k in DocKind]},
    "reason": {"type": "string"},
})

PROMPT = """You sort paperwork for a UK haulage subcontractor. Classify this file as one of:
- self_bill: an invoice the customer wrote on the haulier's behalf (self-billing), listing jobs and amounts
- ticket: proof of one delivery: weighbridge ticket, conveyance note, delivery note or ePOD, proof of delivery, docket
- waste_transfer_note: a waste transfer note (often handwritten, often a return leg)
- work_log: the driver's or office's own log of jobs
- other: anything else
Give a reason of a few words."""

SPREADSHEETS = (".csv", ".xlsx")


async def route(ask: Asker, file: FilePart) -> tuple[DocKind, str]:
    if file.name.lower().endswith(SPREADSHEETS):
        return DocKind.WORK_LOG, "spreadsheet"
    out = await ask(ROUTER, PROMPT, SCHEMA, files=[file])
    return DocKind(out["kind"]), out["reason"]
