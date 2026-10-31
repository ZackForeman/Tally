"""The whole pipeline with a scripted model, so it runs offline and gives a known answer."""

import asyncio
import json
from decimal import Decimal as D

from tally.agents.gemini import ARGON, READER, ROUTER
from tally.engine.models import RateCard, Settings, Setup
from tally.pipeline import Runner
from tally.report import build_report
from tally.store import Store

SETUP = Setup(
    haulier="Kestrel Haulage Ltd",
    settings=Settings(fleet=["KX21 HLG", "KX68 DWV"]),
    rate_cards=[RateCard(customer="Greystone Aggregates", day_rate=D("0.25"), night_rate=D("0.30"), weekend_rate=D("0.35"))],
)


def ticket(net: float) -> dict:
    return {"records": [{
        "ticket_no": "CN-1001", "load_date": "2026-09-01", "load_time": "08:10", "vehicle_reg": "KX21 HLG",
        "driver": "A Driver", "customer": "Greystone Aggregates", "leg": "delivery", "from_site": "Quarry",
        "to_site": "Site A", "material": "20mm aggregate", "recycled": False,
        "gross": 14.5, "tare": 12.5, "gross_tare_unit": "t", "net": net, "net_unit": "t", "miles": 30,
        "returned": None, "full_return": False, "daywork_note": None, "daywork_hours": None, "daywork_chute": False,
        "time_on_site": None, "time_off_site": None, "arrived": None, "unload_start": None, "loads_today": None,
        "internal_account": False, "driver_signed": True, "receiver_signed": True,
        "evidence": [{"field": "net", "quote": str(net), "confidence": "medium"}],
    }]}


SELF_BILL = {
    "invoice_no": "GA-0901", "invoice_date": "2026-09-30", "customer": "Greystone Aggregates",
    "period_start": "2026-09-01", "period_end": "2026-09-30", "net": 150.0, "vat": 30.0, "gross": 180.0,
    "lines": [{"ticket_no": "CN-2002", "job_date": "2026-09-01", "vehicle_reg": "KX21HLG", "driver": None,
               "description": "Haulage 20mm", "kind": "load", "shift": "day", "tonnes": 20, "miles": 30,
               "rate": 0.25, "hours": None, "net_amount": 150.0}],
    "evidence": [],
}

WORK_LOG = b"""date,time,reg,customer,ticket_no,type,net_t,miles
2026-09-01,12:00,KX21 HLG,Greystone Aggregates,CN-2002,load,20,30
"""


class FakeModel:
    def __init__(self):
        self.calls = []

    async def __call__(self, model, prompt, schema, *, files=(), system=None, thinking=None, background=False):
        name = files[0].name if files else ""
        self.calls.append((model, name, thinking, background))
        if model == ROUTER:
            return {"kind": "self_bill" if "bill" in name else "ticket", "reason": "test"}
        if model == READER:
            return SELF_BILL if "bill" in name else ticket(2.6)  # a 0 misread as a 6
        if model == ARGON and "failed these checks" in prompt:
            assert "2.6" in prompt and "2.00" in prompt
            return ticket(2.0)
        if model == ARGON and background:
            return {"patterns": []}
        raise AssertionError(f"unexpected call to {model}")


def test_pipeline_reads_verifies_matches_and_prices(tmp_path):
    model = FakeModel()

    async def main():
        runner = Runner(Store(tmp_path), model, lambda: SETUP)
        run = runner.start([
            ("ticket.jpg", b"\xff\xd8 fake jpeg"),
            ("self-bill.pdf", b"%PDF fake"),
            ("worklog.csv", WORK_LOG),
            ("ticket-again.jpg", b"\xff\xd8 fake jpeg"),  # same bytes: ignored
        ])
        while runner._tasks:
            await asyncio.gather(*list(runner._tasks))
        return runner, run

    runner, run = asyncio.run(main())
    assert run.duplicates == ["ticket-again.jpg"]
    assert {d.name: d.status for d in run.docs} == {"ticket.jpg": "fixed", "self-bill.pdf": "passed", "worklog.csv": "passed"}
    assert [c[:3] for c in model.calls if c[0] == ARGON and not c[3]] == [(ARGON, "ticket.jpg", "high")]

    report = build_report(run, SETUP)
    # Ticket CN-1001 (2.0 t x 30 mi x £0.25 = £15) is missing; CN-2002 is paid in full.
    # The day shift earned £165 against a £200 guarantee: £35 more.
    flags = {f["flag"]: f["amount"] for f in report["findings"]}
    assert flags == {"Missing job": "15.00", "Guarantee not paid": "35.00"}
    assert report["totals"]["owed"] == "50.00"
    assert report["audit_status"] == "done"

    events = [e["type"] for _, e in runner.store.events(run.run_id)]
    assert events[0] == "run" and events[-1] == "end" and "report" in events
    assert json.loads(runner.store.load_run(run.run_id))["status"] == "done"
