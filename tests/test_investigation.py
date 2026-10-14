"""Investigation checks (Stage D1): candidate explanations checked against the loaded data, never confirmed.

Expected statuses are written by hand from the scenarios in tests/fixtures/investigations/README.md and the
small cases below; they are never pasted from output.
"""

from __future__ import annotations

import ast
import csv
import re
import time
from decimal import Decimal
from pathlib import Path

import pytest

from factories import grn, inv, po
from opsresolve import investigation
from opsresolve.ingestion import Dataset, LoadResult
from opsresolve.investigation import (
    CATALOGUE,
    STATUS_LABELS,
    CheckStatus,
    investigate_all,
    investigate_exception,
)
from opsresolve.models import ExceptionType as T
from opsresolve.models import UomConversion, ValidationIssue
from opsresolve.pipeline import investigate, investigate_files
from opsresolve.reporting import render_markdown

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "investigations"
SAMPLE = REPO / "data" / "sample"
EVALUATION = REPO / "data" / "evaluation"


def load(folder: Path, uom: bool = False):
    return investigate_files(folder / "purchase_orders.csv", folder / "invoices.csv", folder / "goods_received.csv",
                             uom_conversions_path=folder / "uom_conversions.csv" if uom else None)


@pytest.fixture(scope="module")
def fixture_result():
    return load(FIXTURES, uom=True)


def results():
    return [("fixtures", load(FIXTURES, uom=True)), ("sample", load(SAMPLE)), ("evaluation", load(EVALUATION, True))]


def conv(sku="SKU-1", uom="BOX", base="EA", size="12", row=2):
    return UomConversion(source_row=row, sku=sku, uom=uom, base_uom=base, units_per_uom=Decimal(size))


def run(pos, invoice_records, receipts, conversions=None, **loaded):
    return investigate(Dataset(
        loaded.get("purchase_orders") or LoadResult("purchase_orders", records=pos),
        loaded.get("invoices") or LoadResult("invoices", records=invoice_records),
        loaded.get("goods_received") or LoadResult("goods_received", records=receipts),
        LoadResult("uom_conversions", records=conversions) if conversions is not None else None))


def only(result, exc_type):
    [found] = [e for e in result.exceptions if e.exception_type is exc_type]
    return found


def checks_of(result, exc_type) -> dict[str, investigation.HypothesisCheck]:
    return {c.code: c for c in investigate_exception(result, only(result, exc_type), strict=True).checks}


def cited(found: investigation.Investigation, check: investigation.HypothesisCheck) -> list[str]:
    rows = {r.ref: f"{r.source}:{r.source_row}" for r in found.related}
    return sorted({rows[ref] for o in check.observations for ref in o.refs})


# ---- the hand-written fixture ----------------------------------------------------------------------------------

def test_the_engine_finds_the_hand_derived_exceptions_in_the_fixture(fixture_result):
    counts = {}
    for e in fixture_result.exceptions:
        counts[e.exception_type] = counts.get(e.exception_type, 0) + 1
    assert counts == {T.QUANTITY_MISMATCH: 8, T.PRICE_MISMATCH: 3, T.MISSING_PURCHASE_ORDER: 7,
                      T.MISSING_GOODS_RECEIPT: 4, T.MISSING_INVOICE: 2}
    assert sorted((i.file, i.row, i.field) for i in fixture_result.dataset.issues) == [
        ("goods_received", 13, "quantity_received"), ("invoices", 24, "quantity_invoiced")]


def test_every_check_matches_the_hand_written_expectations(fixture_result):
    expected = {(r["exception_type"], r["po_number"], r["sku"], r["document"], r["check"]):
                (r["status"], sorted(filter(None, r["cited"].split(";"))))
                for r in csv.DictReader((FIXTURES / "expected_investigations.csv").open(encoding="utf-8"))}
    found = investigate_all(fixture_result, strict=True)  # a check that raises fails this test
    produced = {}
    for e in fixture_result.exceptions:
        document = next((ev.reference for ev in e.evidence if ev.source in ("invoices", "goods_received")
                         and e.exception_type in (T.MISSING_PURCHASE_ORDER, T.PRICE_MISMATCH)), "")
        for check in found[e.exception_id].checks:
            produced[(e.exception_type.value, e.po_number, e.sku, document, check.code)] = (
                check.status.value, cited(found[e.exception_id], check))
    assert produced == expected
    assert len(expected) == 38
    assert all(not f.failures for f in found.values())


def test_multiple_matching_documents_are_consistent_cited_and_marked_not_unique(fixture_result):
    exc = next(e for e in fixture_result.exceptions
               if e.exception_type is T.MISSING_INVOICE and e.po_number == "P-5001")
    found = investigate_exception(fixture_result, exc)
    [check] = found.checks
    assert check.status is CheckStatus.CONSISTENT and check.status_label == "Consistent with the data — not confirmed"
    texts = [o.text for o in check.observations]
    assert sum("Invoice INV-G" in t for t in texts) == 2
    assert texts[-1] == "The match is not unique: 2 candidates fit."


def test_several_close_po_numbers_are_consistent_cited_and_marked_not_unique(fixture_result):
    exc = next(e for e in fixture_result.exceptions if e.po_number == "P-3009")
    found = investigate_exception(fixture_result, exc)
    [check] = found.checks
    assert check.status is CheckStatus.CONSISTENT
    texts = [o.text for o in check.observations]
    assert texts == [
        "PO P-3003 line 1, for the same supplier and SKU, differs from the referenced PO P-3009 only by one character.",
        "PO P-3008 line 1, for the same supplier and SKU, differs from the referenced PO P-3009 only by one character.",
        "The match is not unique: 2 candidates fit."]
    assert cited(found, check) == ["invoices:7", "purchase_orders:6", "purchase_orders:7"]
    assert check.missing == ()


# ---- the rules, case by case ---------------------------------------------------------------------------------

@pytest.mark.parametrize("referenced, candidate, how", [
    ("P-2010", "P-2001", "two swapped adjacent characters"),
    ("4500010012", "4500010013", "one character"),
    ("po-00451", "PO 451", "letter case, separators or leading zeros"),
    ("P-1000", "P-1000-A", None),  # different length
    ("P-1234", "P-1243", "two swapped adjacent characters"),
    ("P-1234", "P-1432", None),  # two differences, not adjacent
    ("P-1234", "P-1256", None),  # two differences, not a swap
    ("---", "P-1", None),  # nothing left to compare
])
def test_po_numbers_are_close_only_by_the_strict_rules(referenced, candidate, how):
    assert investigation._similarity(referenced, candidate) == how


def test_a_close_po_number_needs_the_same_supplier_and_sku():
    result = run([po(po_number="P-2001", supplier="SUP-1", sku="SKU-1"),
                  po(po_number="P-2002", supplier="SUP-2", sku="SKU-1", row=3),
                  po(po_number="P-2003", supplier="SUP-1", sku="SKU-9", row=4)],
                 [inv(po_number="P-2009", supplier="SUP-3", sku="SKU-1")], [])
    check = checks_of(result, T.MISSING_PURCHASE_ORDER)["mistyped_po_number"]
    assert check.status is CheckStatus.NOT_CHECKABLE  # no PO line for supplier SUP-3 and SKU-1 at all
    assert check.missing == ("No purchase-order line for this supplier and SKU SKU-1 is in the loaded files.",)


def test_rejected_purchase_order_rows_make_a_missing_match_uncheckable():
    rejected = LoadResult("purchase_orders", records=[po(po_number="P-1", supplier="SUP-1")],
                          rejected_keys={("P-777", "SKU-1")},
                          issues=[ValidationIssue(file="purchase_orders", row=3, field="unit_price", message="x")])
    result = run([], [inv(po_number="P-999", supplier="SUP-1")], [], purchase_orders=rejected)
    check = checks_of(result, T.MISSING_PURCHASE_ORDER)["mistyped_po_number"]
    assert check.status is CheckStatus.NOT_CHECKABLE
    assert "rejected during validation" in check.missing[0]


def test_receipt_not_posted_compares_invoiced_with_ordered():
    within = run([po(qty="100")], [inv(qty="100")], [grn(qty="40")])
    over = run([po(qty="100")], [inv(qty="120")], [grn(qty="40")])
    assert checks_of(within, T.QUANTITY_MISMATCH)["receipt_not_posted"].status is CheckStatus.CONSISTENT
    check = checks_of(over, T.QUANTITY_MISMATCH)["receipt_not_posted"]
    # an unposted delivery above the ordered quantity cannot be ruled out, so this is never "contradicted"
    assert check.status is CheckStatus.NOT_CHECKABLE
    assert [o.text for o in check.observations] == ["Invoiced quantity 120 is more than the ordered quantity 100.",
                                                    "Received so far on PO 4500000001 line 1: 40."]
    assert check.missing == ("Whether more than the ordered quantity was delivered: an unposted delivery above the "
                             "ordered quantity cannot be ruled out from these records alone.",)


def test_quantities_in_unconvertible_units_cannot_be_checked():
    result = run([po(uom="BOX")], [inv(uom=None)], [])  # the invoice states no unit: units unresolved
    exc = only(result, T.MISSING_GOODS_RECEIPT)
    assert exc.quantity_difference is None
    checks = {c.code: c for c in investigate_exception(result, exc).checks}
    assert {code: c.status for code, c in checks.items()} == {
        "receipt_not_posted": CheckStatus.NOT_CHECKABLE, "document_under_other_po": CheckStatus.NOT_CHECKABLE}
    assert "could not be converted" in checks["receipt_not_posted"].missing[0]


def test_unit_mix_up_needs_a_loaded_conversion_table():
    without = run([po(qty="12", price="24.00")], [inv(qty="144", price="24.00")], [grn(qty="12")])
    check = checks_of(without, T.QUANTITY_MISMATCH)["unit_mix_up"]
    assert (check.status, check.missing) == (CheckStatus.NOT_CHECKABLE, ("No unit-conversion file was loaded.",))
    with_table = run([po(qty="12", price="24.00")], [inv(qty="144", price="24.00")], [grn(qty="12")], [conv()])
    check = checks_of(with_table, T.QUANTITY_MISMATCH)["unit_mix_up"]
    assert check.status is CheckStatus.CONSISTENT
    assert check.observations[0].text == ("The invoiced quantity 144 and the received quantity 12 differ exactly by "
                                          "a factor of the conversion 1 BOX = 12 EA.")


def test_unit_mix_up_is_exact():
    result = run([po(qty="12", price="24.00")], [inv(qty="145", price="24.00")], [grn(qty="12")], [conv()])
    assert checks_of(result, T.QUANTITY_MISMATCH)["unit_mix_up"].status is CheckStatus.CONTRADICTED


def test_a_price_from_another_order_needs_the_same_supplier_sku_currency_and_unit():
    pos = [po(po_number="P-1", price="9.50"),
           po(po_number="P-2", price="10.00", row=3, currency="EUR"),  # other currency
           po(po_number="P-3", price="10.00", row=4, supplier="SUP-9"),  # other supplier
           po(po_number="P-4", price="10.00", row=5, sku="SKU-9")]  # other SKU
    result = run(pos, [inv(po_number="P-1", price="10.00")], [grn(po_number="P-1")])
    check = checks_of(result, T.PRICE_MISMATCH)["price_from_other_order"]
    assert check.status is CheckStatus.NOT_CHECKABLE  # nothing comparable


def test_several_matching_prices_are_consistent_and_not_unique():
    pos = [po(po_number="P-1", price="9.50"), po(po_number="P-2", price="10.00", row=3),
           po(po_number="P-3", price="10.00", row=4), po(po_number="P-4", price="11.00", row=5)]
    result = run(pos, [inv(po_number="P-1", price="10.00")], [grn(po_number="P-1")])
    found = investigate_exception(result, only(result, T.PRICE_MISMATCH))
    check = {c.code: c for c in found.checks}["price_from_other_order"]
    assert check.status is CheckStatus.CONSISTENT
    assert check.observations[-1].text == "The match is not unique: 2 candidates fit."
    assert cited(found, check) == ["invoices:2", "purchase_orders:3", "purchase_orders:4"]


def test_an_over_receipt_matches_exactly_the_open_quantity_of_another_line():
    pos = [po(po_number="P-1", qty="10"), po(po_number="P-2", qty="5", row=3), po(po_number="P-3", qty="6", row=4)]
    receipts = [grn(po_number="P-1", qty="15"), grn(number="GRN-3", po_number="P-3", qty="2", row=3)]
    result = run(pos, [], receipts)
    over = next(e for e in result.exceptions if e.exception_type is T.QUANTITY_MISMATCH)
    check = {c.code: c for c in investigate_exception(result, over).checks}["document_under_other_po"]
    assert check.status is CheckStatus.CONSISTENT  # P-2 has 5 open; P-3 has 4 open
    assert len(check.observations) == 1 and "PO P-2 line 1" in check.observations[0].text


# ---- units of measure across PO lines ------------------------------------------------------------------------------

def test_receipts_in_another_unit_are_never_matched_and_block_a_contradiction():
    pos = [po(po_number="P-1", uom="EA"), po(po_number="P-2", row=3, uom="BOX")]
    invoices = [inv(po_number="P-1", uom="EA"), inv(number="INV-2", po_number="P-2", row=3, uom="BOX")]
    receipts = [grn(po_number="P-2", uom="BOX")]  # 100 BOX on P-2; P-1 needs 100 EA
    result = run(pos, invoices, receipts, [conv(size="10")])
    found = investigate_exception(result, only(result, T.MISSING_GOODS_RECEIPT), strict=True)
    check = {c.code: c for c in found.checks}["document_under_other_po"]
    assert check.status is CheckStatus.NOT_CHECKABLE
    assert [o.text for o in check.observations] == [
        "1 other goods receipt(s) for the same supplier and SKU are in a different or unstated unit of measure, or "
        "in units that could not be converted, so their quantities are not compared."]
    assert check.missing == ("Quantities of the cited records in the same unit of measure as this PO line.",)
    assert cited(found, check) == ["goods_received:2", "purchase_orders:2"]


def test_an_unmatched_document_without_a_unit_is_not_compared_with_a_line_that_states_one():
    pos = [po(po_number="P-1", uom="EA")]
    result = run(pos, [inv(po_number="P-1", uom="EA")], [grn(po_number="P-9")])  # the GRN states no unit
    check = checks_of(result, T.MISSING_GOODS_RECEIPT)["document_under_other_po"]
    assert check.status is CheckStatus.NOT_CHECKABLE and "unstated unit" in check.observations[0].text


def test_an_open_quantity_in_another_unit_is_not_matched_to_an_over_receipt():
    pos = [po(po_number="P-1", qty="10", uom="EA"), po(po_number="P-2", qty="5", row=3, uom="BOX")]
    result = run(pos, [], [grn(po_number="P-1", qty="15", uom="EA")], [conv(size="10")])
    over = next(e for e in result.exceptions if e.exception_type is T.QUANTITY_MISMATCH)
    check = {c.code: c for c in investigate_exception(result, over, strict=True).checks}["document_under_other_po"]
    assert check.status is CheckStatus.NOT_CHECKABLE  # 5 BOX open on P-2 is not "the excess of 5" EA
    assert "different or unstated unit" in check.observations[0].text


def test_a_price_of_an_order_in_another_unit_is_not_compared():
    pos = [po(po_number="P-1", price="9.50", uom="EA"), po(po_number="P-2", price="10.00", row=3, uom="BOX")]
    result = run(pos, [inv(po_number="P-1", price="10.00", uom="EA")], [grn(po_number="P-1", uom="EA")])
    check = checks_of(result, T.PRICE_MISMATCH)["price_from_other_order"]
    assert check.status is CheckStatus.NOT_CHECKABLE
    assert check.missing == ("Prices of the cited orders in the same unit of measure as this PO line.",)


def test_documents_in_the_same_stated_unit_are_matched(fixture_result):
    exc = next(e for e in fixture_result.exceptions if e.po_number == "P-1501")
    found = investigate_exception(fixture_result, exc, strict=True)
    check = {c.code: c for c in found.checks}["document_under_other_po"]
    assert [o.text for o in check.observations] == [
        "GRN G-Q3, for the same supplier and SKU, received 100 against PO P-1503."]  # G-Q2 (100 BOX) is not cited


# ---- several documents on a line ---------------------------------------------------------------------------------

def test_one_invoice_in_another_unit_is_found_among_several(fixture_result):
    exc = next(e for e in fixture_result.exceptions if e.po_number == "P-1301")
    check = {c.code: c for c in investigate_exception(fixture_result, exc, strict=True).checks}["unit_mix_up"]
    assert check.status is CheckStatus.CONSISTENT
    assert [o.text for o in check.observations] == [
        "Converting the quantity 144 on invoice INV-O2 by the conversion 1 BOX = 12 EA gives 12; with it, the "
        "invoiced quantity would equal the received quantity 24."]


def test_several_documents_without_a_fit_cannot_be_checked(fixture_result):
    exc = next(e for e in fixture_result.exceptions if e.po_number == "P-1401")
    check = {c.code: c for c in investigate_exception(fixture_result, exc, strict=True).checks}["unit_mix_up"]
    assert check.status is CheckStatus.NOT_CHECKABLE
    assert [o.text for o in check.observations] == [
        "No conversion for SKU SKU-P (1 BOX = 12 EA) relates the invoiced quantity 30 and the received quantity "
        "20, in total or for any single document."]
    assert check.missing == ("This PO line has several documents; a unit mix-up on more than one of them cannot be "
                             "ruled out from these records.",)


def test_one_receipt_in_another_unit_is_found_among_several_on_an_over_receipt():
    receipts = [grn(qty="10"), grn(number="GRN-2", qty="24", row=3)]  # 24 is 2 BOX recorded as EA
    result = run([po(qty="12")], [], receipts, [conv(size="12")])
    over = next(e for e in result.exceptions if e.exception_type is T.QUANTITY_MISMATCH)
    check = {c.code: c for c in investigate_exception(result, over, strict=True).checks}["unit_mix_up"]
    assert check.status is CheckStatus.CONSISTENT
    assert [o.text for o in check.observations] == [
        "Converting the quantity 24 on GRN GRN-2 by the conversion 1 BOX = 12 EA gives 2; with it, the received "
        "quantity would equal the ordered quantity 12."]


def test_several_fits_are_consistent_and_marked_not_unique():
    invoices = [inv(qty="24"), inv(number="INV-2", qty="24", row=3)]  # either one could be 2 BOX
    result = run([po(qty="26")], invoices, [grn(qty="26")], [conv(size="12")])
    check = checks_of(result, T.QUANTITY_MISMATCH)["unit_mix_up"]
    assert check.status is CheckStatus.CONSISTENT
    assert len(check.observations) == 3 and check.observations[-1].text == "The match is not unique: 2 candidates fit."


# ---- under-billing and mistyped PO numbers on receipts -------------------------------------------------------------

def test_an_under_billed_line_is_checked_for_a_unit_mix_up(fixture_result):
    exc = next(e for e in fixture_result.exceptions if e.po_number == "P-1101")
    assert exc.quantity_difference == Decimal("-22")
    found = investigate_exception(fixture_result, exc, strict=True)
    assert [c.code for c in found.checks] == ["unit_mix_up"]
    assert [o.text for o in found.checks[0].observations] == [
        "The invoiced quantity 2 and the received quantity 24 differ exactly by a factor of the conversion "
        "1 BOX = 12 EA."]


def test_a_receipt_under_a_mistyped_po_number_is_found_from_both_sides(fixture_result):
    unmatched = next(e for e in fixture_result.exceptions if e.po_number == "P-1210")
    [check] = investigate_exception(fixture_result, unmatched, strict=True).checks
    assert [o.text for o in check.observations] == [
        "PO P-1201 line 1, for the same supplier and SKU, differs from the referenced PO P-1210 only by two swapped "
        "adjacent characters."]
    missing = next(e for e in fixture_result.exceptions if e.po_number == "P-1201")
    other = {c.code: c for c in investigate_exception(fixture_result, missing, strict=True).checks}
    assert [o.text for o in other["document_under_other_po"].observations] == [
        "GRN G-N1, for the same supplier and SKU, received 40 against PO P-1210."]


# ---- rejected rows and the totals of a line ------------------------------------------------------------------------

REJECTED_TOTALS = ("rows that may belong to this PO line were rejected during validation, so its totals may be "
                   "incomplete.")


def test_a_rejected_invoice_row_of_the_line_blocks_conclusions_from_its_totals(fixture_result):
    exc = next(e for e in fixture_result.exceptions if e.po_number == "P-1601")
    checks = {c.code: c for c in investigate_exception(fixture_result, exc, strict=True).checks}
    for code in ("receipt_not_posted", "unit_mix_up"):
        assert checks[code].status is CheckStatus.NOT_CHECKABLE and checks[code].observations == ()
        assert checks[code].missing == ("Some invoice " + REJECTED_TOTALS,)


def rejected_invoices(records, key=("4500000001", "SKU-1"), field="quantity_invoiced"):
    return LoadResult("invoices", records=records, rejected_keys={key} if key else set(),
                      issues=[ValidationIssue(file="invoices", row=9, field=field, message="x")])


def test_a_rejected_invoice_row_with_an_unreadable_key_may_belong_to_any_line():
    invoices = [inv(qty="100")]
    result = run([po(qty="100")], invoices, [grn(qty="40")],
                 invoices=rejected_invoices(invoices, key=None, field="po_number"))
    check = checks_of(result, T.QUANTITY_MISMATCH)["receipt_not_posted"]
    assert (check.status, check.missing) == (CheckStatus.NOT_CHECKABLE, ("Some invoice " + REJECTED_TOTALS,))


def test_a_rejected_invoice_row_of_another_line_does_not_affect_this_line():
    invoices = [inv(qty="100")]
    result = run([po(qty="100")], invoices, [grn(qty="40")],
                 invoices=rejected_invoices(invoices, key=("P-OTHER", "SKU-1")))
    assert checks_of(result, T.QUANTITY_MISMATCH)["receipt_not_posted"].status is CheckStatus.CONSISTENT


def test_a_rejected_invoice_row_blocks_a_match_for_a_missing_receipt():
    invoices = [inv(po_number="P-1", qty="20")]
    result = run([po(po_number="P-1", qty="20")], invoices, [grn(po_number="P-9", qty="20")],
                 invoices=rejected_invoices(invoices, key=("P-1", "SKU-1")))
    check = checks_of(result, T.MISSING_GOODS_RECEIPT)["document_under_other_po"]
    assert (check.status, check.missing) == (CheckStatus.NOT_CHECKABLE, ("Some invoice " + REJECTED_TOTALS,))


def test_a_rejected_receipt_row_for_the_sku_blocks_open_quantity_matches():
    pos = [po(po_number="P-1", qty="10"), po(po_number="P-2", qty="5", row=3)]
    receipts = [grn(po_number="P-1", qty="15")]
    loaded = LoadResult("goods_received", records=receipts, rejected_keys={("P-2", "SKU-1")},
                        issues=[ValidationIssue(file="goods_received", row=9, field="quantity_received", message="x")])
    result = run(pos, [], receipts, goods_received=loaded)
    over = next(e for e in result.exceptions if e.exception_type is T.QUANTITY_MISMATCH)
    check = {c.code: c for c in investigate_exception(result, over, strict=True).checks}["document_under_other_po"]
    assert check.status is CheckStatus.NOT_CHECKABLE  # P-2's open 5 may already have been received
    assert check.missing == ("Some goods-receipt rows for this SKU were rejected during validation, so the open "
                             "quantities of other PO lines may be wrong.",)


def test_a_failing_check_is_isolated_but_recorded_and_never_shown_as_evidence(fixture_result, monkeypatch):
    def broken(subject, index):
        raise RuntimeError("secret detail INV-C1 144")

    monkeypatch.setitem(investigation._CHECKS, "unit_mix_up", broken)
    exc = next(e for e in fixture_result.exceptions
               if e.exception_type is T.QUANTITY_MISMATCH and e.po_number == "P-1003")
    found = investigate_exception(fixture_result, exc)
    checks = {c.code: c for c in found.checks}
    failed = checks["unit_mix_up"]
    assert (failed.status, failed.missing, failed.observations) == (
        CheckStatus.FAILED, (investigation.CHECK_FAILED,), ())
    assert failed.status_label == "Check failed — nothing concluded" != STATUS_LABELS[CheckStatus.NOT_CHECKABLE]
    assert checks["receipt_not_posted"].status is CheckStatus.NOT_CHECKABLE  # the other check still ran
    assert found.failures == (investigation.CheckFailure("unit_mix_up", "RuntimeError"),)
    assert "secret" not in repr(found)  # only the error's class name is kept, never its message
    with pytest.raises(RuntimeError):
        investigate_exception(fixture_result, exc, strict=True)
    with pytest.raises(RuntimeError):
        investigate_all(fixture_result, strict=True)


def test_a_failure_before_the_checks_is_contained_per_exception_and_recorded(fixture_result, monkeypatch):
    real = investigation._subject

    def broken(exception, index):
        if exception.po_number == "P-1003":
            raise KeyError("P-1003")
        return real(exception, index)

    monkeypatch.setattr(investigation, "_subject", broken)
    found = investigate_all(fixture_result)
    for e in fixture_result.exceptions:
        if e.po_number == "P-1003":
            assert found[e.exception_id].checks == () and found[e.exception_id].related == ()
            assert found[e.exception_id].missing == (investigation.INVESTIGATION_FAILED,)
            assert found[e.exception_id].failures == (investigation.CheckFailure("subject", "KeyError"),)
        else:
            assert found[e.exception_id].failures == ()
    with pytest.raises(KeyError):
        investigate_all(fixture_result, strict=True)


def test_a_failure_while_indexing_the_run_is_never_contained(fixture_result, monkeypatch):
    def broken(self, result):
        raise ValueError("index")

    monkeypatch.setattr(investigation._Index, "__init__", broken)
    with pytest.raises(ValueError):
        investigate_all(fixture_result)
    with pytest.raises(ValueError):
        investigate_exception(fixture_result, fixture_result.exceptions[0])


@pytest.mark.parametrize("exc_type", [T.SUPPLIER_MISMATCH, T.CURRENCY_MISMATCH, T.POSSIBLE_DUPLICATE_INVOICE,
                                      T.UNIT_OF_MEASURE_MISMATCH])
def test_exception_types_outside_the_catalogue_have_no_checks(exc_type):
    sample = load(SAMPLE)
    evaluation = load(EVALUATION, True)
    found = [e for r in (sample, evaluation) for e in r.exceptions if e.exception_type is exc_type]
    assert found
    for r in (sample, evaluation):
        for e in r.exceptions:
            if e.exception_type is exc_type:
                assert investigate_exception(r, e).checks == () and investigate_exception(r, e).related == ()


# ---- invariants on every dataset --------------------------------------------------------------------------------

def record_rows(result) -> set[tuple[str, int]]:
    dataset = result.dataset
    rows = {(name, rec.source_row) for name in ("purchase_orders", "invoices", "goods_received")
            for rec in getattr(dataset, name).records}
    if dataset.uom_conversions is not None:
        rows |= {("uom_conversions", rec.source_row) for rec in dataset.uom_conversions.records}
    return rows


@pytest.mark.parametrize("name, result", results())
def test_every_citation_resolves_to_a_loaded_record(name, result):
    rows = record_rows(result)
    for found in investigate_all(result, strict=True).values():
        assert [r.ref for r in found.related] == [f"R{i}" for i in range(1, len(found.related) + 1)]
        refs = {r.ref for r in found.related}
        assert all((r.source, r.source_row) in rows for r in found.related)
        assert [c.id for c in found.checks] == [f"H{i}" for i in range(1, len(found.checks) + 1)]
        for check in found.checks:
            assert all(set(o.refs) <= refs and o.refs for o in check.observations)
            if check.status in (CheckStatus.CONSISTENT, CheckStatus.CONTRADICTED):
                assert check.observations  # an evidence status is always backed by cited records


@pytest.mark.parametrize("name, result", results())
def test_investigating_changes_nothing_and_is_deterministic(name, result):
    before = ([e.model_dump() for e in result.exceptions],
              [[r.model_dump() for r in getattr(result.dataset, f).records]
               for f in ("purchase_orders", "invoices", "goods_received")],
              render_markdown(result), [line.po_line.model_dump() for line in result.match.matched])
    first = investigate_all(result, strict=True)
    second = investigate_all(result, strict=True)
    after = ([e.model_dump() for e in result.exceptions],
             [[r.model_dump() for r in getattr(result.dataset, f).records]
              for f in ("purchase_orders", "invoices", "goods_received")],
             render_markdown(result), [line.po_line.model_dump() for line in result.match.matched])
    assert after == before and first == second
    assert list(first) == [e.exception_id for e in result.exceptions]
    for e in result.exceptions:
        assert investigate_exception(result, e) == first[e.exception_id]


@pytest.mark.parametrize("name, result", results())
def test_no_check_fails_on_any_dataset(name, result):
    found = investigate_all(result)  # not strict: a contained failure would still be visible here
    assert [f.failures for f in found.values() if f.failures] == []
    assert all(c.status is not CheckStatus.FAILED for f in found.values() for c in f.checks)


_FORBIDDEN = re.compile(r"(?i)\b(?:caused by|because|root cause|the cause|definitely|certainly|proves?|"
                        r"(?<!not )confirmed)\b")


@pytest.mark.parametrize("name, result", results())
def test_wording_never_claims_a_cause(name, result):
    fixed = list(investigation.statements())
    shown = [text for found in investigate_all(result, strict=True).values() for check in found.checks
             for text in (check.statement, check.how_to_confirm, check.status_label, *check.missing,
                          *(o.text for o in check.observations))]
    assert [text for text in fixed + shown if _FORBIDDEN.search(text)] == []
    assert all(" may " in spec.statement for spec in CATALOGUE)
    assert STATUS_LABELS[CheckStatus.CONSISTENT].endswith("not confirmed")


def test_the_catalogue_is_the_approved_five_checks():
    assert [spec.code for spec in CATALOGUE] == ["receipt_not_posted", "unit_mix_up", "mistyped_po_number",
                                                  "document_under_other_po", "price_from_other_order"]
    assert set(investigation._CHECKS) == {spec.code for spec in CATALOGUE}
    assert {spec for specs in investigation.APPLICABLE.values() for spec in specs} == set(CATALOGUE)


def test_investigating_the_evaluation_dataset_is_fast():
    result = load(EVALUATION, True)
    started = time.perf_counter()
    investigate_all(result)
    assert time.perf_counter() - started < 2.0  # target well under 1 s; the margin avoids slow-runner flakes


# ---- layering ----------------------------------------------------------------------------------------------------

def imported_modules(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = ".".join(["opsresolve", *([module] if module else [])])
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def test_the_investigation_layer_is_pure_and_used_only_by_the_web_helpers():
    imported = imported_modules(REPO / "src" / "opsresolve" / "investigation.py")
    for forbidden in ("opsresolve.ai", "opsresolve.explanation", "opsresolve.webapp", "opsresolve.reporting",
                      "opsresolve.cli", "anthropic", "streamlit", "pandas", "logging"):
        assert not any(name == forbidden or name.startswith(forbidden + ".") for name in imported), forbidden
    users = [p.name for p in [*(REPO / "src" / "opsresolve").glob("*.py"), REPO / "app.py"]
             if p.name != "investigation.py" and "opsresolve.investigation" in imported_modules(p)]
    # D2: only the web helpers use it (app.py goes through them); the CLI (D3) comes later. The AI layer, the
    # engine and the reports never see it.
    assert users == ["webapp.py"]
