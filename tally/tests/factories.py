"""Small builders for hand-checked test cases. All data is fictional."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D
from itertools import count

from tally.engine.models import InvoiceLine, Job, Kind, RateCard, Settings, Signatures

FLEET = ["KX21 HLG", "KX19 BRT", "LN70 TMP", "KX68 DWV", "NX66 AYF"]
SETTINGS = Settings(fleet=FLEET)
GREYSTONE = "Greystone Aggregates Ltd"

CARD = RateCard(
    customer=GREYSTONE,
    day_rate=D("0.25"),
    night_rate=D("0.30"),
    weekend_rate=D("0.35"),
    recycled_per_load=D("67.50"),
    chute_per_hour=D("50"),
    non_chute_per_hour=D("40"),
    part_return=D("55"),
    full_return=D("100"),
    waiting_per_hour=D("30"),
    waiting_free_minutes=60,
)

_ids = count(1)


def job(when: str, *, ticket: str | None = None, reg: str = "KX21 HLG", net: str | None = "20",
        miles: str | None = "30", customer: str = GREYSTONE, **kw) -> Job:
    n = next(_ids)
    kw.setdefault("signatures", Signatures(driver=True, receiver=True))
    return Job(
        job_id=f"J{n}",
        ticket_no=ticket,
        load_time=datetime.fromisoformat(when),
        vehicle_reg=reg,
        customer=customer,
        net_t=D(net) if net is not None else None,
        miles=D(miles) if miles is not None else None,
        **kw,
    )


def line(day: str, amount: str, *, ticket: str | None = None, reg: str | None = "KX21HLG",
         tonnes: str | None = "20", miles: str | None = "30", rate: str | None = "0.25",
         kind: Kind = Kind.LOAD, customer: str = GREYSTONE, **kw) -> InvoiceLine:
    n = next(_ids)
    return InvoiceLine(
        line_id=f"L{n}",
        invoice_no="SB-0901",
        invoice_date=date(2026, 9, 30),
        customer=customer,
        kind=kind,
        job_date=date.fromisoformat(day),
        ticket_no=ticket,
        vehicle_reg=reg,
        tonnes=D(tonnes) if tonnes is not None else None,
        miles=D(miles) if miles is not None else None,
        rate=D(rate) if rate is not None else None,
        net_amount=D(amount),
        **kw,
    )
