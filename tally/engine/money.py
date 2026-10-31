"""Every pound Tally claims is calculated here, in plain Python.

The models read documents and explain; this module counts. Positive amounts
are owed to the haulier; negative ones reduce the claim (guarantee offsets) or
show an overpayment (duplicates).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from tally.engine.match import Item, Match, MatchResult, Status
from tally.engine.models import (
    Confidence,
    InvoiceLine,
    Job,
    Kind,
    Leg,
    RateCard,
    Settings,
    ShiftKind,
    Source,
)
from tally.engine.normalize import customer_key, snap_reg
from tally.engine.shifts import Shift, default_shift_kind, shift_of

PENNY = Decimal("0.01")
ZERO = Decimal("0")


def pennies(x: Decimal) -> Decimal:
    return x.quantize(PENNY, rounding=ROUND_HALF_UP)


class Flag(StrEnum):
    MISSING_JOB = "Missing job"
    SHORT_TONNAGE = "Short tonnage"
    SHORT_MILES = "Short miles"
    LOW_RATE = "Low rate"
    SHIFT_RATE_NOT_PAID = "Shift rate not paid"
    RECYCLED_WRONG_RATE = "Recycled wrong rate"
    UNDERPAID = "Paid short"  # line shows no tonnes/miles/rate breakdown
    GUARANTEE_NOT_PAID = "Guarantee not paid"
    GUARANTEE_OFFSET = "Guarantee already covered"
    RETURN_NOT_PAID = "Return not paid"
    FULL_RETURN_PAID_AS_PART = "Full return paid as part"
    DAYWORK_NOT_PAID = "Daywork not paid"
    WAITING_NOT_PAID = "Waiting not paid"
    MISSING_TICKET = "Missing ticket"
    WEAK_EVIDENCE = "Weak evidence"
    DUPLICATE = "Duplicate"
    NEEDS_ADJUDICATION = "Needs adjudication"
    UNPRICED = "Rate unknown"


# Flags that carry money into the claim total. Duplicates are shown, not netted.
CLAIM_FLAGS = frozenset(Flag) - {
    Flag.MISSING_TICKET,
    Flag.WEAK_EVIDENCE,
    Flag.DUPLICATE,
    Flag.NEEDS_ADJUDICATION,
    Flag.UNPRICED,
}

_MISSING_FLAG = {
    Kind.LOAD: Flag.MISSING_JOB,
    Kind.RETURN: Flag.RETURN_NOT_PAID,
    Kind.DAYWORK: Flag.DAYWORK_NOT_PAID,
    Kind.WAITING: Flag.WAITING_NOT_PAID,
}


@dataclass
class Finding:
    flag: Flag
    amount: Decimal
    customer: str
    detail: str
    job_id: str | None = None
    line_id: str | None = None
    shift: Shift | None = None
    claim: bool = True  # ticked "claim" by default; weak evidence starts unticked

    @property
    def finding_id(self) -> str:
        """Stable across re-runs, so a person's approvals survive corrections."""
        shift = f"{self.shift.reg}/{self.shift.date}/{self.shift.kind}" if self.shift else ""
        return "|".join([self.flag.name, self.job_id or "", self.line_id or "", shift])


@dataclass
class ShiftSummary:
    shift: Shift
    customer: str
    guarantee: Decimal
    earned: Decimal  # corrected earnings: loads, returns, waiting, daywork
    paid: Decimal  # everything paid for the shift, including any top-up
    top_up_paid: Decimal
    claim: Decimal  # max(earned, guarantee) - paid
    pending: bool = False  # waiting on adjudication; guarantee not assessed


@dataclass
class Reconciliation:
    matches: MatchResult
    findings: list[Finding]
    shifts: list[ShiftSummary] = field(default_factory=list)

    def owed(self, customer: str | None = None, claimed_only: bool = False) -> Decimal:
        return sum(
            (
                f.amount
                for f in self.findings
                if f.flag in CLAIM_FLAGS
                and (customer is None or customer_key(f.customer) == customer_key(customer))
                and (f.claim or not claimed_only)
            ),
            ZERO,
        )

    def overpaid(self) -> Decimal:
        return -sum((f.amount for f in self.findings if f.flag is Flag.DUPLICATE), ZERO)

    def by_flag(self, flag: Flag) -> list[Finding]:
        return [f for f in self.findings if f.flag is flag]


def site_key(from_site: str | None, to_site: str | None) -> str:
    return f"{customer_key(from_site or '')}|{customer_key(to_site or '')}"


def miles_for(job: Job, line: InvoiceLine | None, settings: Settings) -> Decimal | None:
    """Ticket miles, else the self-bill's miles, else the site-distance table."""
    if job.miles is not None:
        return job.miles
    if line is not None and line.miles is not None:
        return line.miles
    return settings.site_miles.get(site_key(job.from_site, job.to_site))


def expected_amount(
    item: Item, card: RateCard, settings: Settings, line: InvoiceLine | None = None
) -> Decimal | None:
    """What the pay rules say this piece of work is worth; ``None`` if a rate or quantity is unknown."""
    job = item.job
    _, shift = shift_of(job.load_time)
    match item.kind:
        case Kind.LOAD if job.recycled:
            return card.recycled_per_load
        case Kind.LOAD:
            miles = miles_for(job, line, settings)
            if job.net_t is None or miles is None:
                return None
            return job.net_t * miles * card.tonne_mile_rate(shift)
        case Kind.RETURN:
            return card.full_return if job.full_return else card.part_return
        case Kind.DAYWORK:
            dw = job.daywork
            if dw.chute:
                return None if card.chute_per_hour is None else dw.hours * card.chute_per_hour
            if card.non_chute_per_hour is None:
                return None
            return max(ZERO, dw.hours - card.non_chute_unpaid_hours) * card.non_chute_per_hour
        case Kind.WAITING:
            if card.waiting_per_hour is None:
                return None
            minutes = int((job.unload_start - job.arrived).total_seconds() // 60)
            billable = max(0, minutes - card.waiting_free_minutes)
            return Decimal(billable) / 60 * card.waiting_per_hour
    raise ValueError(item.kind)


def line_shortfalls(
    item: Item, line: InvoiceLine, card: RateCard, settings: Settings
) -> list[tuple[Flag, Decimal, str]]:
    """Why a matched line is short, and by how much. Overpayments are not clawed back."""
    job = item.job
    paid = line.net_amount
    _, shift = shift_of(job.load_time)

    if item.kind is Kind.LOAD and not job.recycled and None not in (line.tonnes, line.miles, line.rate):
        return _haulage_shortfalls(job, line, card, settings, shift)

    expected = expected_amount(item, card, settings, line)
    if expected is None or expected <= paid:
        return []
    owed = expected - paid
    if item.kind is Kind.LOAD and job.recycled:
        return [(Flag.RECYCLED_WRONG_RATE, owed, f"recycled load paid £{paid:.2f}, fixed rate £{expected}")]
    if item.kind is Kind.RETURN and job.full_return and card.part_return is not None and paid <= card.part_return:
        return [(Flag.FULL_RETURN_PAID_AS_PART, owed, f"full return paid £{paid:.2f}, full rate £{expected}")]
    if item.kind is Kind.DAYWORK:
        return [(Flag.DAYWORK_NOT_PAID, owed, f"{_daywork_detail(job.daywork, card)}; paid £{paid:.2f}")]
    flag = {
        Kind.DAYWORK: Flag.DAYWORK_NOT_PAID,
        Kind.WAITING: Flag.WAITING_NOT_PAID,
    }.get(item.kind, Flag.UNDERPAID)
    return [(flag, owed, f"paid £{paid:.2f}, pay rules give £{pennies(expected)}")]


def _haulage_shortfalls(job, line, card, settings, shift) -> list[tuple[Flag, Decimal, str]]:
    """Correct tonnes, then miles, then rate, so the parts add up to the whole shortfall."""
    t_inv, m_inv, r_inv = line.tonnes, line.miles, line.rate
    t = job.net_t if job.net_t is not None and job.net_t - t_inv > settings.short_tonnage_t else t_inv
    m = job.miles if job.miles is not None and job.miles > m_inv else m_inv
    r_card = card.tonne_mile_rate(shift)
    r = max(r_card, r_inv)

    out = []
    if (d := (t - t_inv) * m_inv * r_inv) > 0:
        out.append((Flag.SHORT_TONNAGE, d, f"paid {t_inv} t, ticket net {t} t"))
    if (d := t * (m - m_inv) * r_inv) > 0:
        out.append((Flag.SHORT_MILES, d, f"paid {m_inv} miles, ticket {m} miles"))
    if (d := t * m * (r - r_inv)) > 0:
        if shift is not ShiftKind.DAY and r_inv <= card.day_rate:
            out.append((Flag.SHIFT_RATE_NOT_PAID, d, f"{shift} load paid at the day rate £{r_inv}/t-mile, {shift} rate £{r_card}"))
        else:
            out.append((Flag.LOW_RATE, d, f"paid £{r_inv}/t-mile, rate card £{r_card}"))
    return out


def weak_evidence_reasons(job: Job, reg_mismatch: bool) -> list[str]:
    reasons = []
    if job.source is Source.TICKET and not job.signatures.receiver and not job.internal_account:
        reasons.append("no receiver signature")
    low = sorted({e.field for e in job.evidence if e.confidence is Confidence.LOW})
    if low:
        reasons.append("low-confidence read of " + ", ".join(low))
    if reg_mismatch:
        reasons.append("reg snapped to the fleet list")
    return reasons


def reconcile(
    result: MatchResult, cards: list[RateCard], settings: Settings
) -> Reconciliation:
    card_for = {customer_key(c.customer): c for c in cards}
    findings: list[Finding] = []
    per_shift: dict[Shift, _ShiftBook] = defaultdict(_ShiftBook)

    # Evidence warnings first, so money findings for a weak job start unticked.
    jobs: dict[str, Job] = {}
    mismatched: set[str] = set()
    for m in result.matches:
        jobs[m.item.job.job_id] = m.item.job
        if m.reg_mismatch or m.item.reg.snapped:
            mismatched.add(m.item.job.job_id)
    weak_jobs: set[str] = set()
    for job_id, job in jobs.items():
        if reasons := weak_evidence_reasons(job, job_id in mismatched):
            weak_jobs.add(job_id)
            findings.append(Finding(Flag.WEAK_EVIDENCE, ZERO, job.customer, "; ".join(reasons), job_id, claim=False))

    for m in result.matches:
        item, job = m.item, m.item.job
        shift = Shift(item.reg.reg or "", *shift_of(job.load_time))
        book = per_shift[shift]
        book.customers[customer_key(job.customer)] += 1
        card = card_for.get(customer_key(job.customer))
        claim = job.job_id not in weak_jobs

        if m.status is Status.CANDIDATE:
            book.pending = True
            ids = ", ".join(c.line_id for c in m.candidates)
            findings.append(Finding(Flag.NEEDS_ADJUDICATION, ZERO, job.customer, f"{item.kind} could be {ids}", job.job_id, shift=shift, claim=False))
            continue
        if card is None:
            findings.append(Finding(Flag.UNPRICED, ZERO, job.customer, "no rate card for this customer", job.job_id, shift=shift, claim=False))
            continue

        if m.status is Status.MISSING:
            expected = expected_amount(item, card, settings)
            if expected is None:
                book.unpriced += 1
                findings.append(Finding(Flag.UNPRICED, ZERO, job.customer, f"{item.kind} missing from invoice; rate or quantity unknown", job.job_id, shift=shift, claim=False))
                continue
            amount = pennies(expected)
            book.earned += amount
            if amount > 0:
                f = Finding(_MISSING_FLAG[item.kind], amount, job.customer, _missing_detail(item, card), job.job_id, shift=shift, claim=claim)
                findings.append(f)
                book.line_claims += amount
            continue

        line = m.line
        book.earned += line.net_amount
        book.paid += line.net_amount
        for flag, owed, detail in line_shortfalls(item, line, card, settings):
            amount = pennies(owed)
            findings.append(Finding(flag, amount, job.customer, detail, job.job_id, line.line_id, shift, claim))
            book.earned += amount
            book.line_claims += amount

    unmatched = result.unmatched_lines()
    for line in unmatched:
        if line.kind is Kind.GUARANTEE:
            continue
        if (shift := _line_shift(line, settings)) is not None:
            book = per_shift[shift]
            book.customers[customer_key(line.customer)] += 1
            book.earned += line.net_amount
            book.paid += line.net_amount
    for line in (l for l in result.lines if l.kind is Kind.GUARANTEE):
        if (shift := _top_up_shift(line, per_shift, card_for, settings)) is not None:
            per_shift[shift].top_up += line.net_amount

    for extra, original in result.duplicates:
        findings.append(Finding(Flag.DUPLICATE, -extra.net_amount, extra.customer, f"ticket {extra.ticket_no} also billed on {original.line_id}", line_id=extra.line_id, claim=False))

    shifts = []
    for shift, book in sorted(per_shift.items()):
        if not book.customers:
            continue
        customer = book.customers.most_common(1)[0][0]
        card = card_for.get(customer)
        if card is None:
            continue
        summary = book.summarise(shift, card)
        shifts.append(summary)
        if summary.pending:
            continue
        component = summary.claim - book.line_claims
        if component > 0:
            detail = f"{shift.kind} shift earned £{summary.earned} against a £{summary.guarantee} guarantee; top-up paid £{book.top_up}"
            claim = True
            if book.unpriced:
                detail += f"; may be lower, {book.unpriced} item(s) unpriced"
                claim = False
            findings.append(Finding(Flag.GUARANTEE_NOT_PAID, component, card.customer, detail, shift=shift, claim=claim))
        elif component < 0:
            detail = f"guarantee already paid for this {shift.kind} shift covers part of the gap; claim reduced so it isn't counted twice"
            findings.append(Finding(Flag.GUARANTEE_OFFSET, component, card.customer, detail, shift=shift))

    findings.extend(_missing_tickets(result, settings))
    return Reconciliation(result, findings, shifts)


@dataclass
class _ShiftBook:
    customers: Counter = field(default_factory=Counter)
    earned: Decimal = ZERO
    paid: Decimal = ZERO
    top_up: Decimal = ZERO
    line_claims: Decimal = ZERO
    unpriced: int = 0
    pending: bool = False

    def summarise(self, shift: Shift, card: RateCard) -> ShiftSummary:
        guarantee = card.guarantee(shift.kind)
        paid = self.paid + self.top_up
        claim = pennies(max(self.earned, guarantee) - paid)
        return ShiftSummary(shift, card.customer, guarantee, self.earned, paid, self.top_up, claim, self.pending)


def _line_shift(line: InvoiceLine, settings: Settings) -> Shift | None:
    reg = snap_reg(line.vehicle_reg, settings.fleet, settings.reg_max_distance).reg
    if reg is None or line.job_date is None:
        return None
    return Shift(reg, line.job_date, line.shift or default_shift_kind(line.job_date))


def _top_up_shift(line, per_shift, card_for, settings) -> Shift | None:
    """Place a top-up on its shift. Without a stated shift, pick the one that was furthest short."""
    if line.shift is not None:
        return _line_shift(line, settings)
    reg = snap_reg(line.vehicle_reg, settings.fleet, settings.reg_max_distance).reg
    same_day = [s for s in per_shift if s.reg == reg and s.date == line.job_date]
    if not same_day:
        return None

    def shortfall(s: Shift) -> Decimal:
        card = card_for.get(customer_key(line.customer))
        book = per_shift[s]
        return (card.guarantee(s.kind) if card else ZERO) - book.paid - book.top_up

    return max(same_day, key=shortfall)


def _missing_detail(item: Item, card: RateCard) -> str:
    job = item.job
    ref = f"ticket {job.ticket_no}" if job.ticket_no else job.source_file or job.job_id
    what = _daywork_detail(job.daywork, card) if item.kind is Kind.DAYWORK else str(item.kind)
    return f"{what} on {job.load_time:%d %b %H:%M}, {ref}: no invoice line"


def _daywork_detail(dw, card: RateCard) -> str:
    """Show the sum, so a person can check it: chute 3.00 h x £50, or (2.58 h - 0.5 h) x £40."""
    if dw.chute:
        return f"chute daywork {dw.hours:.2f} h × £{card.chute_per_hour}"
    return f"non-chute daywork ({dw.hours:.2f} h − {card.non_chute_unpaid_hours} h) × £{card.non_chute_per_hour}"


def _missing_tickets(result: MatchResult, settings: Settings) -> list[Finding]:
    """The customer's own "Loads Today" counter says more loads than we hold tickets for."""
    groups: dict[tuple, list[Job]] = defaultdict(list)
    for m in result.matches:
        job = m.item.job
        if m.item.kind is Kind.LOAD and job.leg is Leg.DELIVERY:
            groups[(customer_key(job.customer), m.item.reg.reg, job.load_time.date())].append(job)
    out = []
    for (_, reg, day), jobs in groups.items():
        counted = max((j.loads_today or 0 for j in jobs), default=0)
        held = len({j.job_id for j in jobs})
        if counted > held:
            out.append(Finding(
                Flag.MISSING_TICKET, ZERO, jobs[0].customer,
                f"{reg} on {day:%d %b}: customer's counter says {counted} loads, {held} ticket(s) held; find the rest",
                claim=False,
            ))
    return out
