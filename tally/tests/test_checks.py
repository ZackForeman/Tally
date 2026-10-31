from datetime import date
from decimal import Decimal as D

from tally.engine.checks import check_invoice, check_job, is_uk_reg
from tally.engine.models import Invoice
from tally.tests.factories import job, line


def test_net_weight_check_catches_a_six_read_as_a_zero():
    # "14.5 - 12.5 = 2.0" read as 2.6 t
    j = job("2026-09-01T08:00", net="2.6", gross_kg=D("14500"), tare_kg=D("12500"))
    [failure] = check_job(j)
    assert failure.rule == "net_weight" and "2.0" in failure.message


def test_mixed_units_fail_the_net_check():
    # "14080 - 12300 = 1,780 TON"
    j = job("2026-09-01T08:00", net="1780", gross_kg=D("14080"), tare_kg=D("12300"))
    assert [f.rule for f in check_job(j)] == ["net_weight"]


def test_uk_reg_formats():
    for reg in ["KX21 HLG", "K21PAK", "S121PAK", "NX66AYF", "ABC123D", "1 AB"]:
        assert is_uk_reg(reg), reg
    for reg in ["DRIVER", "", None, "KX21HLGX"]:
        assert not is_uk_reg(reg), reg


def test_name_in_the_reg_field_fails():
    assert [f.rule for f in check_job(job("2026-09-01T08:00", reg="J SMITH"))] == ["uk_reg"]


def test_invoice_checks():
    lines = [line("2026-09-03", "150.00"), line("2026-10-02", "100.00")]
    inv = Invoice(
        invoice_no="SB-0901", invoice_date=date(2026, 9, 30), customer="Greystone",
        period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
        net=D("260.00"), vat=D("50.00"), gross=D("310.00"), lines=lines,
    )
    assert sorted(f.rule for f in check_invoice(inv)) == ["date_in_period", "lines_sum", "vat"]


def test_extraction_accepts_uk_dates():
    from tally.agents.extract import _date

    assert str(_date("01/09/2026", "d")) == "2026-09-01" == str(_date("2026-09-01", "d"))


def test_time_in_and_out_become_daywork():
    from datetime import date
    from decimal import Decimal as D

    from tally.agents.extract import _daywork

    day = date(2026, 9, 2)
    non_chute = _daywork({"daywork_note": "Waiting time", "time_on_site": "09:10", "time_off_site": "11:45"}, day)
    assert (non_chute.chute, round(non_chute.hours, 4)) == (False, D("2.5833"))
    chute = _daywork({"time_on_site": "2026-09-02 13:00", "time_off_site": "2026-09-02 16:00", "daywork_chute": True}, day)
    assert (chute.chute, chute.hours) == (True, D("3"))
    overnight = _daywork({"time_on_site": "23:30", "time_off_site": "01:00"}, day)
    assert overnight.hours == D("1.5")
    assert _daywork({"time_on_site": "09:00"}, day) is None
    assert _daywork({"daywork_note": "4hrs Daywork (Chute)", "daywork_hours": 4, "daywork_chute": True}, day).hours == D("4")
