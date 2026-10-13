"""Duplicate-invoice rules: supply evidence decides what identical invoices mean."""

from datetime import date, timedelta
from decimal import Decimal

from factories import grn, inv, of_type, po, run

from opsresolve.config import MatchConfig
from opsresolve.detection import detect_exceptions
from opsresolve.matching import match_records
from opsresolve.models import ExceptionType as T
from opsresolve.uom import normalize_units

DUP = T.POSSIBLE_DUPLICATE_INVOICE


def ambiguity_notes(exceptions):
    return [n for e in exceptions for n in e.notes if "cannot tell these apart" in n]


# ---- S26: resubmitted after the window -------------------------------------

def s26(gap_days=60, ordered="10"):
    first = date(2026, 6, 1)
    invoices = [inv("E01-2601", qty="10", price="30.00", on=first),
                inv("E01-2688", qty="10", price="30.00", on=first + timedelta(days=gap_days))]
    return run([po(qty=ordered, price="30.00")], invoices, [grn(qty="10")])


def test_s26_duplicate_outside_window_flagged_when_po_cannot_support_both():
    exceptions = s26()
    [dup] = of_type(exceptions, DUP)
    assert dup.amount_impact == Decimal("300.00")
    assert "outside the 30-day window, but combined billing exceeds the ordered quantity" in dup.summary
    assert len(of_type(exceptions, T.QUANTITY_MISMATCH)) == 1


def test_s26_wording_unchanged_inside_window():
    [dup] = of_type(s26(gap_days=10), DUP)
    assert dup.summary.endswith("identical line items, quantities and prices, dated 10 day(s) apart.")


def test_window_is_not_simply_widened():
    # Same 60-day gap, but the PO was for 20, so a second identical invoice can be legitimate.
    exceptions = s26(ordered="20")
    assert of_type(exceptions, DUP) == []
    assert ambiguity_notes(exceptions) == []  # outside the window: no note either


def test_window_setting_still_applies_to_open_po_notes():
    exceptions = s26(gap_days=10, ordered="20")
    assert of_type(exceptions, DUP) == [] and len(ambiguity_notes(exceptions)) == 1
    narrow = MatchConfig(duplicate_window_days=5)
    invoices = [inv("A", on=date(2026, 6, 1)), inv("B", on=date(2026, 6, 11))]
    exceptions = run([po(qty="200")], invoices, [grn(qty="100")], narrow)
    assert ambiguity_notes(exceptions) == []


# ---- S27: legitimate second invoice, receipt not yet posted ------------------

def s27():
    invoices = [inv("E10-2701", qty="100", price="2.25", on=date(2026, 8, 1)),
                inv("E10-2711", qty="100", price="2.25", on=date(2026, 8, 11))]
    return run([po(qty="200", price="2.25")], invoices, [grn(qty="100")])


def test_s27_open_po_quantity_is_not_claimed_as_duplicate():
    exceptions = s27()
    assert of_type(exceptions, DUP) == []
    [qty] = of_type(exceptions, T.QUANTITY_MISMATCH)
    assert qty.amount_impact == Decimal("225.00")  # payment is still held on the unreceived 100
    [note] = ambiguity_notes(exceptions)
    assert "Invoice E10-2711 has the same line items as invoice E10-2701 (10 day(s) apart)" in note
    assert "confirm the receipt before paying" in note


def test_second_receipt_posted_clears_everything():
    invoices = [inv("A", qty="100", on=date(2026, 8, 1)), inv("B", qty="100", on=date(2026, 8, 11))]
    assert run([po(qty="200")], invoices, [grn("G1", qty="100"), grn("G2", qty="100")]) == []


def test_open_po_with_no_receipt_at_all_notes_the_missing_receipt():
    invoices = [inv("A", qty="100", on=date(2026, 8, 1)), inv("B", qty="100", on=date(2026, 8, 3))]
    exceptions = run([po(qty="200")], invoices, [])
    assert of_type(exceptions, DUP) == []
    [missing] = of_type(exceptions, T.MISSING_GOODS_RECEIPT)
    assert len(ambiguity_notes([missing])) == 1


# ---- conflicting evidence -----------------------------------------------------

def test_one_line_over_ordered_outweighs_a_supported_line():
    pos = [po(sku="A", qty="10"), po(sku="B", qty="20")]
    receipts = [grn(sku="A", qty="10"), grn(sku="B", qty="20")]
    invoices = [inv("X-1", sku="A", qty="10", on=date(2026, 8, 1)), inv("X-1", sku="B", qty="10", on=date(2026, 8, 1)),
                inv("Y-9", sku="A", qty="10", on=date(2026, 8, 20)), inv("Y-9", sku="B", qty="10", on=date(2026, 8, 20))]
    [dup] = of_type(run(pos, invoices, receipts), DUP)
    assert "Y-9 may duplicate invoice X-1" in dup.summary


def test_reformatted_number_still_flagged_even_with_open_po_quantity():
    invoices = [inv("INV-0042", qty="100", on=date(2026, 8, 1)), inv("INV 42", qty="100", on=date(2026, 8, 3))]
    exceptions = run([po(qty="200")], invoices, [grn(qty="100")])
    [dup] = of_type(exceptions, DUP)
    assert "differ only in formatting" in dup.summary
    assert ambiguity_notes(exceptions) == []  # the stronger evidence wins; no contradictory note


def test_no_contradictory_note_when_another_earlier_invoice_flags_it():
    # All three are identical and the PO has open quantity. B-1 is ambiguous against A-7 (note kept).
    # B-01 is ambiguous against A-7 too, but is then flagged against B-1 (reformatted number), so its
    # ambiguity note must be dropped rather than contradict the duplicate exception.
    invoices = [inv("A-7", qty="100", on=date(2026, 8, 1)), inv("B-1", qty="100", on=date(2026, 8, 2)),
                inv("B-01", qty="100", on=date(2026, 8, 3))]
    exceptions = run([po(qty="300")], invoices, [grn(qty="100")])
    [dup] = of_type(exceptions, DUP)
    assert "B-01 may duplicate invoice B-1" in dup.summary
    notes = ambiguity_notes(exceptions)
    assert len(notes) == 1 and "Invoice B-1 has the same line items as invoice A-7" in notes[0]


def test_reformatted_number_with_different_content_is_still_a_possible_duplicate():
    invoices = [inv("INV-0042", qty="100", on=date(2026, 8, 1)), inv("INV42", qty="60", on=date(2026, 8, 3))]
    [dup] = of_type(run([po(qty="200")], invoices, [grn(qty="160")]), DUP)
    assert "identical line items" not in dup.summary


def test_different_content_is_never_a_content_duplicate():
    invoices = [inv("A", qty="100", on=date(2026, 8, 1)), inv("B", qty="100", price="10.01", on=date(2026, 8, 2))]
    assert of_type(run([po(qty="100")], invoices, [grn(qty="100")]), DUP) == []


# ---- incomplete evidence ------------------------------------------------------

def test_no_po_line_keeps_windowed_rule():
    inside = [inv("A", po_number="NOPE", on=date(2026, 8, 1)), inv("B", po_number="NOPE", on=date(2026, 8, 20))]
    assert len(of_type(run([], inside, []), DUP)) == 1
    outside = [inv("A", po_number="NOPE", on=date(2026, 6, 1)), inv("B", po_number="NOPE", on=date(2026, 8, 20))]
    assert of_type(run([], outside, []), DUP) == []


def test_unresolved_units_keep_windowed_rule():
    pos = [po(qty="20", price="24.00", uom="BOX")]
    invoices = [inv("A", qty="120", price="2.00", uom="EA", on=date(2026, 8, 1)),
                inv("B", qty="120", price="2.00", uom="EA", on=date(2026, 8, 5))]
    match = match_records(pos, invoices, [grn(qty="20", uom="BOX")])
    units = normalize_units(match, [])
    exceptions = detect_exceptions(units.match, invoices, MatchConfig(), None, units.unresolved)
    assert len(of_type(exceptions, DUP)) == 1


def test_split_billing_unchanged():
    invoices = [inv("A", qty="100", price="3.00", on=date(2026, 7, 14)),
                inv("B", qty="100", price="3.00", on=date(2026, 7, 21))]
    assert run([po(qty="200", price="3.00")], invoices, [grn("G1", qty="100"), grn("G2", qty="100")]) == []
