"""Stage 4: code checks every reading; Argon re-reads the ones that fail.

A failed check sends the original document, the previous reading and the
failed rules back to Argon with high thinking. Two failed re-reads send the
document to the review queue for a person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable

from tally.agents.extract import build_invoice, build_jobs, reading_json, schema_for, system_for
from tally.agents.gemini import ARGON, READER, Asker, FilePart
from tally.engine.checks import CheckFailure
from tally.engine.models import Invoice, Job, Setup

MAX_REREADS = 2

REREAD_PROMPT = """You are checking a reading of this document made by a faster model.
Their reading failed these checks:
{failures}

Their reading:
{reading}

Look at the document again, closely, and return a corrected reading of the whole document.
- Fix misread characters and digits (a 6 read as 0, an E read as F, a dropped decimal point).
- Weights may be labelled with the wrong unit (e.g. "1,780 TON" beside kilogram weights);
  report the unit that matches the other weights and quote the original text.
- If the document itself is inconsistent (the written sum doesn't add up), transcribe it as written
  and mark those fields low confidence. Never make the numbers agree by inventing a value."""


class VerifyStatus(StrEnum):
    PASSED = "passed"  # first reading passed every check
    FIXED = "fixed"  # Argon's re-read passed
    REVIEW = "review"  # still failing: a person confirms or corrects


@dataclass
class Attempt:
    reader: str
    reading: dict[str, Any]
    failures: list[CheckFailure]


@dataclass
class Verified:
    status: VerifyStatus
    jobs: list[Job] = field(default_factory=list)
    invoice: Invoice | None = None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckFailure]:
        return self.attempts[-1].failures if self.attempts else []


Notify = Callable[[str, str], Awaitable[None]]  # (status, message)


def build(doc_kind: str, reading: dict[str, Any], doc_id: str, name: str, final: bool = False):
    if doc_kind == "self_bill":
        invoice, failures = build_invoice(reading, doc_id, name)
        return [], invoice, failures
    jobs, failures = build_jobs(reading, doc_id, name, mark_failures=final)
    return jobs, None, failures


async def verify(
    ask: Asker,
    file: FilePart,
    doc_kind: str,
    reading: dict[str, Any],
    setup: Setup,
    doc_id: str,
    notify: Notify,
) -> Verified:
    jobs, invoice, failures = build(doc_kind, reading, doc_id, file.name)
    attempts = [Attempt(READER, reading, failures)]
    rereads = 0
    while failures and rereads < MAX_REREADS:
        rereads += 1
        await notify("rereading", "; ".join(f.message for f in failures))
        prompt = REREAD_PROMPT.format(
            failures="\n".join(f"- {f.rule} ({f.field}): {f.message}" for f in failures),
            reading=reading_json(reading),
        )
        reading = await ask(
            ARGON, prompt, schema_for(doc_kind), files=[file], system=system_for(setup), thinking="high"
        )
        jobs, invoice, failures = build(doc_kind, reading, doc_id, file.name)
        attempts.append(Attempt(ARGON, reading, failures))

    if not failures:
        status = VerifyStatus.PASSED if rereads == 0 else VerifyStatus.FIXED
        return Verified(status, jobs, invoice, attempts)
    jobs, invoice, _ = build(doc_kind, reading, doc_id, file.name, final=True)
    return Verified(VerifyStatus.REVIEW, jobs, invoice, attempts)
