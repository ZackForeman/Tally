"""Record types shared by the agents and the engine.

The models read documents into these records; everything that involves money
works on them in plain Python. Amounts are always ``Decimal``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Evidence(BaseModel):
    """The exact text a field was read from, and how sure the reader was."""

    field: str
    quote: str
    confidence: Confidence = Confidence.HIGH


class Signatures(BaseModel):
    driver: bool = False
    receiver: bool = False


class Kind(StrEnum):
    """What a piece of billable work is. Shared by tickets and invoice lines."""

    LOAD = "load"  # haulage or recycled aggregate
    RETURN = "return"
    DAYWORK = "daywork"
    WAITING = "waiting"
    GUARANTEE = "guarantee"  # shift guarantee top-up; invoice side only


class Leg(StrEnum):
    DELIVERY = "delivery"
    RETURN = "return"


class Source(StrEnum):
    TICKET = "ticket"  # a ticket, delivery note or WTN
    WORK_LOG = "work_log"  # the haulier's own log; no signatures to check


class ShiftKind(StrEnum):
    DAY = "day"
    NIGHT = "night"
    WEEKEND = "weekend"


class Daywork(BaseModel):
    chute: bool
    hours: Decimal  # chute: hours written on the ticket; non-chute: hours on site


class Job(BaseModel):
    """One ticket, waste transfer note or work-log row: proof a job happened."""

    job_id: str
    source: Source = Source.TICKET
    ticket_no: str | None = None  # issuer's number: conveyance note, delivery note, SRC ticket
    load_time: datetime  # when the load happened; drives matching and shift
    vehicle_reg: str
    driver: str | None = None
    customer: str
    leg: Leg = Leg.DELIVERY
    carries_load: bool = True  # False for a log row that records only waiting or daywork
    from_site: str | None = None
    to_site: str | None = None
    material: str | None = None
    recycled: bool = False  # paid a fixed rate per load, whatever the tonnage
    gross_kg: Decimal | None = None
    tare_kg: Decimal | None = None
    net_t: Decimal | None = None
    miles: Decimal | None = None
    returned_t: Decimal | None = None  # "Returned material" box on a delivery ticket
    full_return: bool = False  # the whole load came back
    daywork: Daywork | None = None
    arrived: datetime | None = None  # ship-to arrival, for waiting time
    unload_start: datetime | None = None
    loads_today: int | None = None  # customer's own "Loads Today" counter
    internal_account: bool = False  # internal transfers are never signed
    signatures: Signatures = Field(default_factory=Signatures)
    source_file: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class InvoiceLine(BaseModel):
    line_id: str
    invoice_no: str
    invoice_date: date
    customer: str
    kind: Kind = Kind.LOAD
    job_date: date | None = None
    shift: ShiftKind | None = None  # when the self-bill says so
    ticket_no: str | None = None
    vehicle_reg: str | None = None
    driver: str | None = None
    description: str = ""
    tonnes: Decimal | None = None
    miles: Decimal | None = None
    rate: Decimal | None = None  # per tonne-mile for haulage lines
    hours: Decimal | None = None
    net_amount: Decimal
    source_file: str | None = None


class Invoice(BaseModel):
    invoice_no: str
    invoice_date: date
    customer: str
    period_start: date | None = None
    period_end: date | None = None
    net: Decimal
    vat: Decimal | None = None
    gross: Decimal | None = None
    lines: list[InvoiceLine] = Field(default_factory=list)
    source_file: str | None = None


class RateCard(BaseModel):
    """One per customer, entered once at set-up. ``None`` means not known yet."""

    customer: str
    day_rate: Decimal  # £ per tonne-mile
    night_rate: Decimal
    weekend_rate: Decimal
    recycled_per_load: Decimal | None = None
    chute_per_hour: Decimal | None = None  # Conway: £50
    non_chute_per_hour: Decimal | None = None  # Conway: £40
    non_chute_unpaid_hours: Decimal = Decimal("0.5")
    part_return: Decimal | None = None  # Conway: £55
    full_return: Decimal | None = None  # Conway: £100
    waiting_per_hour: Decimal | None = None
    waiting_free_minutes: int = 0
    guarantee_day: Decimal = Decimal("200")
    guarantee_night: Decimal = Decimal("350")  # night or weekend

    def tonne_mile_rate(self, shift: ShiftKind) -> Decimal:
        return {
            ShiftKind.DAY: self.day_rate,
            ShiftKind.NIGHT: self.night_rate,
            ShiftKind.WEEKEND: self.weekend_rate,
        }[shift]

    def guarantee(self, shift: ShiftKind) -> Decimal:
        return self.guarantee_day if shift is ShiftKind.DAY else self.guarantee_night


class Settings(BaseModel):
    """Tolerances are settings, so a customer who rounds tonnage doesn't flood the list."""

    fleet: list[str] = Field(default_factory=list)
    short_tonnage_t: Decimal = Decimal("0.1")
    candidate_tonnage_t: Decimal = Decimal("0.5")
    date_window_days: int = 1
    reg_max_distance: int = 1
    # Miles between sites, keyed "from|to" (see engine.money.site_key), for
    # tickets that print no mileage (SRC).
    site_miles: dict[str, Decimal] = Field(default_factory=dict)


class Setup(BaseModel):
    """Entered once: who the haulier is, their trucks, and each customer's rates."""

    haulier: str
    settings: Settings
    rate_cards: list[RateCard]

    @property
    def customers(self) -> list[str]:
        return [c.customer for c in self.rate_cards]
