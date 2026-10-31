from datetime import datetime
from decimal import Decimal as D

from tally.engine.match import match
from tally.engine.models import Confidence, Daywork, Evidence, Kind, Leg, ShiftKind, Signatures
from tally.engine.money import Flag, reconcile
from tally.tests.factories import CARD, SETTINGS, job, line


def run(jobs, lines):
    return reconcile(match(jobs, lines, SETTINGS), [CARD], SETTINGS)


def amounts(rec, flag):
    return [f.amount for f in rec.by_flag(flag)]


def test_correctly_paid_shift_owes_nothing():
    jobs = [job("2026-09-01T08:00", ticket="1"), job("2026-09-01T12:00", ticket="2")]
    lines = [line("2026-09-01", "150.00", ticket="1"), line("2026-09-01", "150.00", ticket="2")]
    rec = run(jobs, lines)
    assert rec.owed() == 0
    assert rec.findings == []


def test_short_tonnage_miles_and_rate_add_up_to_the_whole_shortfall():
    j = job("2026-09-01T08:00", ticket="1", net="20", miles="30")
    # Paid 18 t x 25 mi x £0.20 = £90; should be 20 x 30 x £0.25 = £150.
    l = line("2026-09-01", "90.00", ticket="1", tonnes="18", miles="25", rate="0.20")
    other = line("2026-09-01", "200.00", reg="KX21HLG", ticket="X", kind=Kind.LOAD)  # keeps shift above guarantee
    rec = run([j], [l, other])
    assert amounts(rec, Flag.SHORT_TONNAGE) == [D("10.00")]  # 2 x 25 x 0.20
    assert amounts(rec, Flag.SHORT_MILES) == [D("20.00")]  # 20 x 5 x 0.20
    assert amounts(rec, Flag.LOW_RATE) == [D("30.00")]  # 20 x 30 x 0.05
    assert rec.owed() == D("60.00")


def test_tonnage_within_tolerance_is_not_claimed():
    j = job("2026-09-01T08:00", ticket="1", net="20.08")
    rec = run([j, job("2026-09-01T12:00", ticket="2")],
              [line("2026-09-01", "150", ticket="1"), line("2026-09-01", "150", ticket="2")])
    assert rec.by_flag(Flag.SHORT_TONNAGE) == []


def test_night_load_paid_at_day_rate_and_night_guarantee():
    # Tuesday 02:00 -> Monday night shift. 20 t x 30 mi: day £150, night £180.
    j = job("2026-09-01T02:00", ticket="1")
    rec = run([j], [line("2026-09-01", "150.00", ticket="1")])
    [shift_rate] = rec.by_flag(Flag.SHIFT_RATE_NOT_PAID)
    assert shift_rate.amount == D("30.00")
    assert shift_rate.shift.kind is ShiftKind.NIGHT and str(shift_rate.shift.date) == "2026-08-31"
    # Night guarantee £350: claim = 350 - 150 paid = 200, of which 30 is the rate.
    assert amounts(rec, Flag.GUARANTEE_NOT_PAID) == [D("170.00")]
    assert rec.owed() == D("200.00")


def test_missing_load_in_a_topped_up_shift_is_worth_less():
    paid = job("2026-09-01T08:00", ticket="1")  # £150
    missing = job("2026-09-01T13:00", ticket="2", net="10")  # 10 x 30 x 0.25 = £75
    lines = [
        line("2026-09-01", "150.00", ticket="1"),
        line("2026-09-01", "50.00", kind=Kind.GUARANTEE, tonnes=None, miles=None, rate=None),
    ]
    rec = run([paid, missing], lines)
    assert amounts(rec, Flag.MISSING_JOB) == [D("75.00")]
    # Corrected earnings £225 beat the £200 guarantee, which was already paid in full.
    assert amounts(rec, Flag.GUARANTEE_OFFSET) == [D("-50.00")]
    assert rec.owed() == D("25.00")


def test_unpaid_day_guarantee():
    rec = run([job("2026-09-01T08:00", ticket="1")], [line("2026-09-01", "150.00", ticket="1")])
    assert amounts(rec, Flag.GUARANTEE_NOT_PAID) == [D("50.00")]


def test_recycled_load_paid_old_flat_rate():
    j = job("2026-09-05T08:00", ticket="1", recycled=True, material="6F5 crushed hardcore")
    rec = run([j], [line("2026-09-05", "60.00", ticket="1", tonnes=None, miles=None, rate=None)])
    assert amounts(rec, Flag.RECYCLED_WRONG_RATE) == [D("7.50")]


def test_full_return_paid_as_part_and_unpaid_part_return():
    full = job("2026-09-01T15:00", ticket="W1", leg=Leg.RETURN, net="19.34", full_return=True)
    part = job("2026-09-01T16:00", ticket="W2", leg=Leg.RETURN, net="3")
    rec = run([full, part], [line("2026-09-01", "55.00", ticket="W1", kind=Kind.RETURN, tonnes=None, miles=None, rate=None)])
    assert amounts(rec, Flag.FULL_RETURN_PAID_AS_PART) == [D("45.00")]
    assert amounts(rec, Flag.RETURN_NOT_PAID) == [D("55.00")]


def test_daywork_chute_and_non_chute():
    chute = job("2026-09-01T07:00", ticket="1", daywork=Daywork(chute=True, hours=D("4")))
    site = job("2026-09-01T12:00", ticket="2", daywork=Daywork(chute=False, hours=D("3.9166")))
    lines = [line("2026-09-01", "150", ticket="1"), line("2026-09-01", "150", ticket="2")]
    rec = run([chute, site], lines)
    # Chute 4 h x £50; non-chute (3 h 55 - 30 min) x £40 = £136.66
    assert sorted(amounts(rec, Flag.DAYWORK_NOT_PAID)) == [D("136.66"), D("200.00")]


def test_waiting_beyond_free_time():
    j = job("2026-09-01T09:00", ticket="1",
            arrived=datetime(2026, 9, 1, 9, 0), unload_start=datetime(2026, 9, 1, 11, 0))
    rec = run([j], [line("2026-09-01", "250", ticket="1", rate="0.4166667")])
    assert amounts(rec, Flag.WAITING_NOT_PAID) == [D("30.00")]  # (120 - 60 free) min at £30/h


def test_weak_evidence_starts_unticked_but_internal_accounts_are_fine():
    unsigned = job("2026-09-01T08:00", ticket="1", signatures=Signatures(driver=True))
    internal = job("2026-09-02T08:00", ticket="2", signatures=Signatures(), internal_account=True)
    rec = run([unsigned, internal], [])
    [weak] = rec.by_flag(Flag.WEAK_EVIDENCE)
    assert weak.job_id == unsigned.job_id and "receiver" in weak.detail
    missing = {f.job_id: f.claim for f in rec.by_flag(Flag.MISSING_JOB)}
    assert missing == {unsigned.job_id: False, internal.job_id: True}


def test_low_confidence_and_snapped_reg_are_weak():
    j = job("2026-09-01T08:00", reg="NX66AYE",
            evidence=[Evidence(field="net_t", quote="2.6", confidence=Confidence.LOW)])
    [weak] = run([j], []).by_flag(Flag.WEAK_EVIDENCE)
    assert "net_t" in weak.detail and "snapped" in weak.detail


def test_loads_today_counter_prompts_a_ticket_search():
    jobs = [job("2026-09-01T08:00", ticket="1", loads_today=1),
            job("2026-09-01T14:00", ticket="2", loads_today=3)]
    [gap] = run(jobs, []).by_flag(Flag.MISSING_TICKET)
    assert gap.amount == 0 and "3 loads, 2 ticket" in gap.detail


def test_duplicate_shown_as_overpaid_not_netted():
    j = job("2026-09-01T08:00", ticket="5")
    rec = run([j, job("2026-09-01T12:00", ticket="6")],
              [line("2026-09-01", "150", ticket="5"), line("2026-09-01", "150", ticket="5"),
               line("2026-09-01", "150", ticket="6")])
    assert rec.overpaid() == D("150")
    assert rec.owed() == 0


def test_unpriced_wait_is_not_claimed():
    card = CARD.model_copy(update={"waiting_per_hour": None})
    j = job("2026-09-01T09:00", ticket="1",
            arrived=datetime(2026, 9, 1, 9, 0), unload_start=datetime(2026, 9, 1, 12, 0))
    rec = reconcile(match([j], [line("2026-09-01", "250", ticket="1", rate="0.4166667")], SETTINGS), [card], SETTINGS)
    assert [f.flag for f in rec.findings] == [Flag.UNPRICED]


def test_work_log_rows_need_no_signature():
    from tally.engine.models import Source

    rec = run([job("2026-09-01T08:00", ticket="1", source=Source.WORK_LOG, signatures=Signatures())], [])
    assert rec.by_flag(Flag.WEAK_EVIDENCE) == []


def test_ticket_and_log_row_for_the_same_load_merge():
    from tally.engine.merge import merge_jobs
    from tally.engine.models import Source

    photo = job("2026-09-01T08:00", ticket="00 42", miles=None)
    log = job("2026-09-01T08:05", ticket="42", miles="31", source=Source.WORK_LOG)
    [merged] = merge_jobs([log, photo])
    assert merged.job_id == photo.job_id and merged.miles == D("31")


def test_work_log_time_in_and_out_and_merge_with_the_ticket():
    from tally.engine.merge import merge_jobs
    from tally.engine.worklog import parse_work_log

    log = (b"date,time,reg,customer,ticket_no,type,net_t,miles,time_in,time_out,chute\n"
           b"2026-09-02,06:55,KX21 HLG,Greystone Aggregates,CN-10301,load,20,30,,,\n"
           b"2026-09-02,09:10,KX21 HLG,Greystone Aggregates,CN-10301,daywork,,,09:10,11:45,n\n"
           b"2026-09-02,23:30,KX21 HLG,Greystone Aggregates,CN-10302,daywork,,,23:30,01:00,y\n")
    jobs, problems = parse_work_log(log, "log.csv", "W")
    assert problems == []
    assert [(j.carries_load, j.daywork and round(j.daywork.hours, 2)) for j in jobs] == [
        (True, None), (False, D("2.58")), (False, D("1.50"))]

    ticket = job("2026-09-02T06:55", ticket="CN-10301", daywork=Daywork(chute=False, hours=D("2.5833")))
    merged = merge_jobs([ticket] + jobs[:2])
    assert len(merged) == 1 and merged[0].job_id == ticket.job_id
    lines = [line("2026-09-02", "150", ticket="CN-10301"), line("2026-09-02", "100", ticket="X")]
    assert len(amounts(run([ticket] + jobs[:2], lines), Flag.DAYWORK_NOT_PAID)) == 2  # unmerged: double
    assert amounts(run(merged, lines), Flag.DAYWORK_NOT_PAID) == [D("83.33")]  # as the pipeline runs it
