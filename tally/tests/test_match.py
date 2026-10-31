from datetime import datetime

from tally.engine.match import Status, adjudicate, match
from tally.engine.models import Kind, Leg, ShiftKind
from tally.engine.normalize import snap_reg, ticket_key
from tally.engine.shifts import shift_of
from tally.tests.factories import FLEET, SETTINGS, job, line


def test_ticket_key_strips_spaces_dashes_and_leading_zeros():
    assert ticket_key("00 1234-5") == "12345"
    assert ticket_key(" 0 ") is None


def test_snap_reg_fixes_one_character_misread():
    snapped = snap_reg("NX66AYE", FLEET)
    assert (snapped.reg, snapped.snapped) == ("NX66AYF", True)
    assert snap_reg("kx21 hlg", FLEET).snapped is False


def test_snap_reg_leaves_ambiguous_reads_alone():
    fleet = ["K21PAK", "B21PAK"]
    assert snap_reg("X21PAK", fleet).snapped is False
    assert snap_reg("S121PAK", ["K21PAK", "S121PAK"]).reg == "S121PAK"


def test_night_shift_belongs_to_the_date_it_starts():
    # Tuesday 01:30 counts towards Monday's night shift.
    assert shift_of(datetime(2026, 9, 1, 1, 30)) == (datetime(2026, 8, 31).date(), ShiftKind.NIGHT)
    assert shift_of(datetime(2026, 9, 1, 18, 0))[1] is ShiftKind.NIGHT
    assert shift_of(datetime(2026, 9, 1, 4, 0))[1] is ShiftKind.DAY
    # Saturday, and Monday 02:00 (Sunday's night shift), are weekend shifts.
    assert shift_of(datetime(2026, 9, 5, 10, 0))[1] is ShiftKind.WEEKEND
    assert shift_of(datetime(2026, 9, 7, 2, 0))[1] is ShiftKind.WEEKEND


def test_rule_1_ticket_number_wins_even_with_a_wrong_date():
    j = job("2026-09-01T09:00", ticket="00-42")
    l = line("2026-09-20", "150", ticket="42")
    [m] = match([j], [l], SETTINGS).matches
    assert (m.status, m.rule, m.line) == (Status.MATCHED, 1, l)


def test_rule_2_snaps_reg_and_flags_mismatch():
    j = job("2026-09-01T23:50", reg="NX66AYE")  # read AYE, fleet truck is AYF
    l = line("2026-09-02", "150", reg="NX66 AYF")  # crosses midnight: ±1 day
    [m] = match([j], [l], SETTINGS).matches
    assert (m.status, m.rule) == (Status.MATCHED, 2)
    assert m.reg_mismatch


def test_rule_2_separates_same_day_loads_by_tonnage():
    a = job("2026-09-01T08:00", net="20")
    b = job("2026-09-01T12:00", net="14")
    la = line("2026-09-01", "150", tonnes="20")
    lb = line("2026-09-01", "105", tonnes="14")
    result = match([a, b], [lb, la], SETTINGS)
    assert {m.item.job.job_id: m.line.line_id for m in result.matches} == {
        a.job_id: la.line_id,
        b.job_id: lb.line_id,
    }


def test_rule_3_makes_candidates_for_argon():
    j = job("2026-09-01T09:00", reg="LN70TMP", net="18.2")
    l = line("2026-09-01", "150", reg=None, tonnes="18.0")
    result = match([j], [l], SETTINGS)
    [m] = result.matches
    assert m.status is Status.CANDIDATE and m.candidates == [l]
    assert result.unmatched_lines() == []

    decided = adjudicate(result, {m.item.item_id: (l.line_id, "Same customer, day and weight.")}, SETTINGS)
    [m] = decided.matches
    assert (m.status, m.rule, m.reasoning) == (Status.MATCHED, 3, "Same customer, day and weight.")

    rejected = adjudicate(result, {m.item.item_id: (None, "Different site.")}, SETTINGS)
    assert rejected.matches[0].status is Status.MISSING


def test_ticket_with_no_line_is_missing():
    [m] = match([job("2026-09-01T09:00", ticket="99")], [], SETTINGS).matches
    assert m.status is Status.MISSING


def test_same_ticket_on_two_lines_is_a_duplicate():
    j = job("2026-09-01T09:00", ticket="77")
    first, again = line("2026-09-01", "150", ticket="77"), line("2026-09-01", "150", ticket="0077")
    result = match([j], [first, again], SETTINGS)
    assert result.matches[0].line == first
    assert result.duplicates == [(again, first)]


def test_return_leg_matches_only_return_lines():
    ret = job("2026-09-01T15:00", ticket="W1", leg=Leg.RETURN, net="3")
    load_line = line("2026-09-01", "150", ticket="W1")
    [m] = match([ret], [load_line], SETTINGS).matches
    assert m.item.kind is Kind.RETURN and m.status is Status.MISSING
