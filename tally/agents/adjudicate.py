"""Stage 6: Argon settles ambiguous ticket-to-line pairs, with one sentence of reasoning."""

from __future__ import annotations

import asyncio
import json

from tally.agents.gemini import ARGON, Asker, obj
from tally.engine.match import Match

PROMPT = """A haulage ticket could match more than one line of the customer's self-bill invoice,
or the only candidate is a loose match. Decide which invoice line pays for this ticket, or "none"
if none of them does. A wrong match hides an unpaid job, so say "none" unless the evidence agrees.
Matching rules of thumb: night loads can be dated a day either side; a reg one character off is
usually a misread; tonnage within 0.5 t is close; the material and sites should agree.

Ticket ({kind}):
{ticket}

Candidate invoice lines:
{lines}

Give one plain sentence of reasoning a haulage office manager would follow."""


async def adjudicate_one(ask: Asker, m: Match) -> tuple[str | None, str]:
    job = m.item.job
    ticket = job.model_dump(
        mode="json",
        include={"ticket_no", "load_time", "vehicle_reg", "driver", "customer", "from_site",
                 "to_site", "material", "net_t", "miles", "leg"},
    )
    lines = [
        c.model_dump(mode="json", exclude={"source_file", "invoice_date", "customer"})
        for c in m.candidates
    ]
    schema = obj({
        "line_id": {"type": "string", "enum": [c.line_id for c in m.candidates] + ["none"]},
        "reasoning": {"type": "string"},
    })
    prompt = PROMPT.format(kind=m.item.kind, ticket=json.dumps(ticket), lines=json.dumps(lines, indent=1))
    out = await ask(ARGON, prompt, schema, thinking="medium")
    choice = out["line_id"]
    return (None if choice == "none" else choice), out["reasoning"]


async def adjudicate_all(ask: Asker, candidates: list[Match]) -> dict[str, tuple[str | None, str]]:
    """Decisions for every candidate. If two tickets claim the same line, neither is
    decided, and both stay in front of a person."""
    results = await asyncio.gather(*(adjudicate_one(ask, m) for m in candidates))
    decisions = {m.item.item_id: r for m, r in zip(candidates, results)}
    chosen: dict[str, list[str]] = {}
    for item_id, (line_id, _) in decisions.items():
        if line_id:
            chosen.setdefault(line_id, []).append(item_id)
    for item_ids in chosen.values():
        if len(item_ids) > 1:
            for item_id in item_ids:
                del decisions[item_id]
    return decisions
