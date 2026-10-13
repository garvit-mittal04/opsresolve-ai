from datetime import date
from decimal import Decimal

from factories import grn, inv, of_type, po, run

from opsresolve.config import MatchConfig
from opsresolve.detection import RejectedKeys, normalize_invoice_number, severity_for_amount
from opsresolve.models import ExceptionType as T
from opsresolve.models import Severity


def test_clean_three_way_match_produces_no_exceptions():
    assert run([po()], [inv()], [grn()]) == []


def test_split_billing_and_partial_receipts_are_not_exceptions():
    exceptions = run(
        [po(qty="600", price="5.10")],
        [inv("I1", qty="300", price="5.10", on=date(2026, 9, 18)),
         inv("I2", qty="300", price="5.10", on=date(2026, 9, 28))],
        [grn("G1", qty="300"), grn("G2", qty="300")],
    )
    assert exceptions == []


# ---- quantity ----------------------------------------------------------------

def test_over_billing_vs_receipt_valued_at_po_price():
    [exc] = run([po(qty="400", price="6.75")], [inv(qty="400", price="6.75")], [grn(qty="380")])
    assert exc.exception_type == T.QUANTITY_MISMATCH
    assert exc.quantity_difference == Decimal("20")
    assert exc.amount_impact == Decimal("135.00")
    assert exc.severity == Severity.MEDIUM


def test_under_billing_is_low_severity_negative_impact():
    [exc] = run([po(qty="100")], [inv(qty="90")], [grn(qty="100")])
    assert exc.quantity_difference == Decimal("-10")
    assert exc.amount_impact == Decimal("-100.00")
    assert exc.severity == Severity.LOW


def test_over_receipt_vs_ordered():
    [exc] = run([po(qty="150", price="14.60")], [inv(qty="165", price="14.60")], [grn(qty="165")])
    assert exc.exception_type == T.QUANTITY_MISMATCH
    assert exc.quantity_difference == Decimal("15")
    assert exc.amount_impact == Decimal("219.00")


def test_quantity_tolerance_is_inclusive():
    config = MatchConfig(quantity_tolerance=Decimal("2"))
    within = run([po(qty="200")], [inv(qty="102")], [grn(qty="100")], config)
    assert of_type(within, T.QUANTITY_MISMATCH) == []
    beyond = run([po(qty="200")], [inv(qty="103")], [grn(qty="100")], config)
    assert len(of_type(beyond, T.QUANTITY_MISMATCH)) == 1


def test_fractional_quantities_are_exact():
    [exc] = run([po(qty="10.5", price="3.333")], [inv(qty="10.5", price="3.333")], [grn(qty="10.2")])
    # 0.3 * 3.333 = 0.9999 -> 1.00 (half-up to cents)
    assert exc.quantity_difference == Decimal("0.3")
    assert exc.amount_impact == Decimal("1.00")


# ---- price -------------------------------------------------------------------

def test_price_overcharge_impact_uses_invoiced_quantity():
    [exc] = run([po(qty="250", price="18.40")], [inv(qty="250", price="19.95")], [grn(qty="250")])
    assert exc.exception_type == T.PRICE_MISMATCH
    assert exc.amount_impact == Decimal("387.50")
    assert exc.severity == Severity.MEDIUM


def test_price_undercharge_is_low_and_negative():
    [exc] = run([po(qty="1000", price="0.85")], [inv(qty="1000", price="0.80")], [grn(qty="1000")])
    assert exc.amount_impact == Decimal("-50.00")
    assert exc.severity == Severity.LOW


def test_price_tolerances():
    pct = MatchConfig(price_tolerance_pct=Decimal("1"))  # 1% of 10.00 = 0.10
    assert run([po()], [inv(price="10.10")], [grn()], pct) == []
    assert len(run([po()], [inv(price="10.11")], [grn()], pct)) == 1
    absolute = MatchConfig(price_tolerance_abs=Decimal("0.05"))
    assert run([po()], [inv(price="9.95")], [grn()], absolute) == []


def test_currency_mismatch_skips_price_comparison():
    exceptions = run([po(currency="USD")], [inv(currency="EUR", price="9.00")], [grn()])
    assert [e.exception_type for e in exceptions] == [T.CURRENCY_MISMATCH]
    assert exceptions[0].amount_impact == Decimal("0")


def test_supplier_mismatch_on_invoice_is_high():
    [exc] = run([po(supplier="SUP-1")], [inv(supplier="SUP-9")], [grn()])
    assert exc.exception_type == T.SUPPLIER_MISMATCH
    assert exc.severity == Severity.HIGH
    assert exc.amount_impact == Decimal("1000.00")


def test_supplier_mismatch_on_receipt():
    [exc] = run([po(supplier="SUP-1")], [inv()], [grn(supplier="SUP-2")])
    assert exc.exception_type == T.SUPPLIER_MISMATCH and exc.severity == Severity.MEDIUM


# ---- missing records ---------------------------------------------------------

def test_invoice_without_receipt():
    [exc] = run([po(qty="60", price="112.00")], [inv(qty="60", price="112.00")], [])
    assert exc.exception_type == T.MISSING_GOODS_RECEIPT
    assert exc.amount_impact == Decimal("6720.00")
    assert exc.severity == Severity.HIGH


def test_receipt_without_invoice_is_low_accrual():
    [exc] = run([po(qty="10000", price="0.02")], [], [grn(qty="10000")])
    assert exc.exception_type == T.MISSING_INVOICE
    assert exc.amount_impact == Decimal("200.00")
    assert exc.severity == Severity.LOW


def test_po_with_no_activity_is_not_an_exception():
    assert run([po()], [], []) == []


def test_invoice_for_unknown_po_and_unknown_sku():
    exceptions = run([po(sku="A")], [inv(sku="B", qty="2", price="845.00"),
                                     inv("I2", po_number="NOPE", qty="1", price="5.00")], [])
    missing = of_type(exceptions, T.MISSING_PURCHASE_ORDER)
    assert len(missing) == 2
    by_sku = {e.sku: e for e in missing}
    assert "has no line for SKU B" in by_sku["B"].summary
    assert by_sku["B"].amount_impact == Decimal("1690.00") and by_sku["B"].severity == Severity.HIGH
    assert by_sku["SKU-1"].severity == Severity.MEDIUM  # minimum severity for no-PO invoices


def test_receipt_for_unknown_po():
    [exc] = run([], [], [grn(po_number="X")])
    assert exc.exception_type == T.MISSING_PURCHASE_ORDER
    assert exc.amount_impact == Decimal("0")


def test_missing_record_notes_rejected_validation_rows():
    rejected = RejectedKeys(purchase_orders={("4500000001", "SKU-1")})
    [exc] = run([], [inv()], [], rejected=rejected)
    assert any("rejected during input validation" in n for n in exc.notes)


# ---- duplicates --------------------------------------------------------------

def test_reformatted_invoice_number_flagged_as_possible_duplicate():
    exceptions = run([po(qty="300", price="9.20")],
                     [inv("CIB-7781", qty="300", price="9.20", on=date(2026, 9, 2)),
                      inv("CIB-07781", qty="300", price="9.20", on=date(2026, 9, 16))],
                     [grn(qty="300")])
    [dup] = of_type(exceptions, T.POSSIBLE_DUPLICATE_INVOICE)
    assert "CIB-07781 may duplicate invoice CIB-7781" in dup.summary
    assert dup.amount_impact == Decimal("2760.00")
    [qty] = of_type(exceptions, T.QUANTITY_MISMATCH)
    assert any("possible duplicates" in n for n in qty.notes)


def test_identical_content_judged_by_supply_evidence():
    invoices = [inv("A-1", qty="80", price="24.50", on=date(2026, 9, 3)),
                inv("B-2", qty="80", price="24.50", on=date(2026, 9, 12))]
    over_ordered = run([po(qty="80", price="24.50")], invoices, [grn(qty="80")])
    assert len(of_type(over_ordered, T.POSSIBLE_DUPLICATE_INVOICE)) == 1
    # Over received but within ordered: could be an unposted receipt, so not claimed as a duplicate.
    open_on_po = run([po(qty="160", price="24.50")], invoices, [grn(qty="80")])
    assert of_type(open_on_po, T.POSSIBLE_DUPLICATE_INVOICE) == []
    [qty] = of_type(open_on_po, T.QUANTITY_MISMATCH)
    assert any("same line items as invoice A-1" in n for n in qty.notes)
    fully_received = run([po(qty="160", price="24.50")], invoices, [grn(qty="160")])
    assert of_type(fully_received, T.POSSIBLE_DUPLICATE_INVOICE) == []


def test_identical_content_outside_window_not_flagged():
    invoices = [inv("A-1", on=date(2026, 1, 1)), inv("B-2", on=date(2026, 3, 1))]
    exceptions = run([po(qty="200")], invoices, [grn(qty="100")])
    assert of_type(exceptions, T.POSSIBLE_DUPLICATE_INVOICE) == []


def test_same_invoice_number_different_supplier_is_not_duplicate():
    exceptions = run([po(supplier="S1", sku="A"), po(po_number="P2", supplier="S2", sku="A")],
                     [inv("100", supplier="S1", sku="A"), inv("100", supplier="S2", po_number="P2", sku="A")],
                     [grn(supplier="S1", sku="A"), grn(supplier="S2", po_number="P2", sku="A")])
    assert exceptions == []


def test_repeated_line_within_one_invoice():
    exceptions = run([po(qty="200")], [inv("I1", row=2), inv("I1", row=3)], [grn(qty="100")])
    [dup] = of_type(exceptions, T.POSSIBLE_DUPLICATE_INVOICE)
    assert dup.amount_impact == Decimal("1000.00")
    assert "2 times" in dup.summary


def test_three_copies_reported_once_per_later_invoice():
    invoices = [inv("X-1", on=date(2026, 9, 1)), inv("X-01", on=date(2026, 9, 2)), inv("X-001", on=date(2026, 9, 3))]
    dups = of_type(run([po(qty="300")], invoices, [grn(qty="100")]), T.POSSIBLE_DUPLICATE_INVOICE)
    assert sorted(d.summary.split()[1] for d in dups) == ["X-001", "X-01"]


def test_normalize_invoice_number():
    assert normalize_invoice_number("inv-00456") == normalize_invoice_number("INV 456") == "INV456"
    assert normalize_invoice_number("000") == "0"
    assert normalize_invoice_number("A-10") != normalize_invoice_number("A-100")


# ---- severity, ordering, determinism ----------------------------------------

def test_severity_thresholds():
    cfg = MatchConfig()
    assert severity_for_amount(Decimal("999.99"), cfg) == Severity.MEDIUM
    assert severity_for_amount(Decimal("1000.00"), cfg) == Severity.HIGH
    assert severity_for_amount(Decimal("-1500"), cfg) == Severity.HIGH
    assert severity_for_amount(Decimal("99.99"), cfg) == Severity.LOW
    assert severity_for_amount(Decimal("1"), cfg, minimum=Severity.MEDIUM) == Severity.MEDIUM


def test_output_is_deterministic_and_ordered():
    args = ([po(sku="A", qty="10"), po(sku="B", price="50.00")],
            [inv(sku="A", qty="10", price="11.00"), inv(sku="B", qty="100", price="50.00")],
            [grn(sku="A", qty="10")])
    first, second = run(*args), run(*args)
    assert first == second
    assert [e.exception_id for e in first] == [f"EXC-{i:04d}" for i in range(1, len(first) + 1)]
    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    assert [order[e.severity] for e in first] == sorted(order[e.severity] for e in first)
