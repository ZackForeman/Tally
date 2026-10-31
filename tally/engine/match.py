"""Pair each piece of ticketed work with a self-bill line. No model calls.

Matching runs from the ticket side, because a ticket is proof a job happened.
The first rule that finds exactly one line wins:

1. Issuer's ticket number, exact after normalising.
2. Load date (±1 day) + vehicle reg snapped to the fleet + driver.
3. Load date (±1 day) + customer + tonnes (±0.5 t) -> candidates for Argon.

Each rule runs over every unmatched item before the next rule starts, so a
loose rule never takes a line that a strict rule would have paired elsewhere.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from enum import StrEnum

from tally.engine.models import InvoiceLine, Job, Kind, Leg, Settings
from tally.engine.normalize import SnappedReg, customer_key, driver_key, snap_reg, ticket_key


@dataclass(frozen=True)
class Item:
    """One billable piece of work evidenced by a job: a load, return, daywork or wait."""

    job: Job
    kind: Kind
    reg: SnappedReg

    @property
    def item_id(self) -> str:
        return f"{self.job.job_id}:{self.kind}"


class Status(StrEnum):
    MATCHED = "matched"
    CANDIDATE = "candidate"  # waiting for Argon to adjudicate
    MISSING = "missing"  # missing from invoice


@dataclass
class Match:
    item: Item
    status: Status
    line: InvoiceLine | None = None
    rule: int | None = None
    candidates: list[InvoiceLine] = field(default_factory=list)
    reasoning: str | None = None  # Argon's one sentence, for adjudicated pairs
    line_reg: SnappedReg | None = None

    @property
    def reg_mismatch(self) -> bool:
        """The pair only lines up after snapping a reg, or the two regs disagree."""
        if self.line is None or self.line_reg is None:
            return False
        return self.item.reg.snapped or self.line_reg.snapped or self.item.reg.reg != self.line_reg.reg


@dataclass
class MatchResult:
    matches: list[Match]
    duplicates: list[tuple[InvoiceLine, InvoiceLine]]  # (extra line, line it repeats)
    lines: list[InvoiceLine]

    def by_status(self, status: Status) -> list[Match]:
        return [m for m in self.matches if m.status is status]

    def unmatched_lines(self) -> list[InvoiceLine]:
        """Lines no ticket accounts for (excluding duplicates and open candidates)."""
        used = {m.line.line_id for m in self.matches if m.line}
        used |= {c.line_id for m in self.by_status(Status.CANDIDATE) for c in m.candidates}
        used |= {extra.line_id for extra, _ in self.duplicates}
        return [line for line in self.lines if line.line_id not in used]


def items_for(job: Job, settings: Settings) -> list[Item]:
    reg = snap_reg(job.vehicle_reg, settings.fleet, settings.reg_max_distance)
    kinds = [Kind.RETURN if job.leg is Leg.RETURN else Kind.LOAD] if job.carries_load else []
    if job.leg is Leg.DELIVERY and job.returned_t:
        kinds.append(Kind.RETURN)
    if job.daywork:
        kinds.append(Kind.DAYWORK)
    if job.arrived and job.unload_start and job.unload_start > job.arrived:
        kinds.append(Kind.WAITING)
    return [Item(job, kind, reg) for kind in kinds]


def find_duplicates(lines: list[InvoiceLine]) -> list[tuple[InvoiceLine, InvoiceLine]]:
    """The same ticket on two lines of the same kind: shown as overpaid, for honesty."""
    groups: dict[tuple, list[InvoiceLine]] = defaultdict(list)
    for line in lines:
        key = ticket_key(line.ticket_no)
        if key:
            groups[(customer_key(line.customer), line.kind, key)].append(line)
    return [(extra, group[0]) for group in groups.values() for extra in group[1:]]


def match(jobs: list[Job], lines: list[InvoiceLine], settings: Settings) -> MatchResult:
    items = [item for job in jobs for item in items_for(job, settings)]
    duplicates = find_duplicates(lines)
    extras = {extra.line_id for extra, _ in duplicates}
    available = {
        line.line_id: line
        for line in lines
        if line.line_id not in extras and line.kind is not Kind.GUARANTEE
    }
    line_regs = {
        lid: snap_reg(line.vehicle_reg, settings.fleet, settings.reg_max_distance)
        for lid, line in available.items()
    }

    found: dict[str, Match] = {}
    ambiguous: dict[str, list[InvoiceLine]] = defaultdict(list)

    for rule in (1, 2):
        for item in items:
            if item.item_id in found:
                continue
            hits = _hits(rule, item, available.values(), line_regs, settings)
            if len(hits) == 1:
                line = hits[0]
                del available[line.line_id]
                found[item.item_id] = Match(
                    item, Status.MATCHED, line, rule, line_reg=line_regs[line.line_id]
                )
            elif hits:
                ambiguous[item.item_id].extend(hits)

    matches = []
    for item in items:
        if item.item_id in found:
            matches.append(found[item.item_id])
            continue
        hits = _hits(3, item, available.values(), line_regs, settings)
        candidates = _unique(
            [line for line in ambiguous[item.item_id] if line.line_id in available] + hits
        )
        if candidates:
            matches.append(Match(item, Status.CANDIDATE, candidates=candidates))
        else:
            matches.append(Match(item, Status.MISSING))
    return MatchResult(matches, duplicates, lines)


def adjudicate(
    result: MatchResult, decisions: dict[str, tuple[str | None, str]], settings: Settings
) -> MatchResult:
    """Apply Argon's tie-breaks: item_id -> (chosen line_id or None, one-sentence reasoning)."""
    taken = {m.line.line_id for m in result.matches if m.line}
    matches = []
    for m in result.matches:
        if m.status is not Status.CANDIDATE or m.item.item_id not in decisions:
            matches.append(m)
            continue
        line_id, reasoning = decisions[m.item.item_id]
        if line_id is None:
            matches.append(replace(m, status=Status.MISSING, candidates=[], reasoning=reasoning))
            continue
        chosen = next((c for c in m.candidates if c.line_id == line_id), None)
        if chosen is None:
            raise ValueError(f"{line_id} is not a candidate for {m.item.item_id}")
        if line_id in taken:
            raise ValueError(f"{line_id} is already matched to another item")
        taken.add(line_id)
        matches.append(
            replace(
                m,
                status=Status.MATCHED,
                line=chosen,
                rule=3,
                candidates=[],
                reasoning=reasoning,
                line_reg=snap_reg(chosen.vehicle_reg, settings.fleet, settings.reg_max_distance),
            )
        )
    return MatchResult(matches, result.duplicates, result.lines)


def _hits(rule, item, lines, line_regs, settings) -> list[InvoiceLine]:
    job = item.job
    cust = customer_key(job.customer)
    same = [l for l in lines if l.kind is item.kind and customer_key(l.customer) == cust]

    if rule == 1:
        key = ticket_key(job.ticket_no)
        return [l for l in same if key and ticket_key(l.ticket_no) == key]

    in_window = [l for l in same if _near_date(job, l, settings)]
    if rule == 2:
        driver = driver_key(job.driver)
        hits = [
            l
            for l in in_window
            if item.reg.reg
            and line_regs[l.line_id].reg == item.reg.reg
            and (driver is None or l.driver is None or driver_key(l.driver) == driver)
        ]
        if len(hits) > 1 and item.kind is Kind.LOAD:
            # A truck does several loads a day; tonnage separates them.
            close = [l for l in hits if _close_tonnes(job, l, settings)]
            if len(close) == 1:
                return close
        return hits

    if item.kind is Kind.LOAD:
        return [l for l in in_window if _close_tonnes(job, l, settings)]
    return in_window


def _near_date(job: Job, line: InvoiceLine, settings: Settings) -> bool:
    if line.job_date is None:
        return False
    return abs((line.job_date - job.load_time.date()).days) <= settings.date_window_days


def _close_tonnes(job: Job, line: InvoiceLine, settings: Settings) -> bool:
    if job.net_t is None or line.tonnes is None:
        return False
    return abs(line.tonnes - job.net_t) <= settings.candidate_tonnage_t


def _unique(lines: list[InvoiceLine]) -> list[InvoiceLine]:
    seen: dict[str, InvoiceLine] = {}
    for line in lines:
        seen.setdefault(line.line_id, line)
    return list(seen.values())
