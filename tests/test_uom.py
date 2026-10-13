from datetime import date
from decimal import Decimal
from pathlib import Path

from factories import grn, inv, of_type, po

from opsresolve.cli import main
from opsresolve.config import MatchConfig
from opsresolve.detection import detect_exceptions
from opsresolve.ingestion import load_uom_conversions
from opsresolve.matching import match_records
from opsresolve.models import ExceptionType as T
from opsresolve.models import Severity, UomConversion
from opsresolve.pipeline import investigate_files
from opsresolve.uom import ConversionTable, normalize_units

CONV_HEADER = "sku,uom,base_uom,units_per_uom\n"


def conv(sku="SKU-1", uom="BOX", base="EA", size="12", row=2):
    return UomConversion(source_row=row, sku=sku, uom=uom, base_uom=base, units_per_uom=Decimal(size))


def run(pos, invoices, receipts, conversions=(), config=None):
    match = match_records(pos, invoices, receipts)
    units = normalize_units(match, list(conversions))
    return detect_exceptions(units.match, invoices, config or MatchConfig(), None, units.unresolved)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- S32 regression ----------------------------------------------------------

def s32(conversions=(conv(),)):
    return run([po(qty="10", price="24.00", uom="BOX")],
               [inv(qty="120", price="2.00", uom="EA")],
               [grn(qty="10", uom="BOX")], conversions)


def test_s32_box_of_12_invoiced_in_each_has_no_exceptions():
    assert s32() == []


def test_s32_without_conversion_is_one_unit_mismatch_and_no_false_amounts():
    [exc] = s32(conversions=())
    assert exc.exception_type == T.UNIT_OF_MEASURE_MISMATCH
    assert exc.severity == Severity.MEDIUM
    assert exc.amount_impact == Decimal("0")
    assert "no known conversion from EA to BOX" in exc.summary


def test_conversion_is_per_sku_never_assumed_for_another_item():
    # A box of 12 is known for SKU-2 only; SKU-1's box size is unknown.
    exceptions = s32(conversions=(conv(sku="SKU-2"),))
    assert [e.exception_type for e in exceptions] == [T.UNIT_OF_MEASURE_MISMATCH]


def test_different_box_sizes_for_different_skus():
    exceptions = run(
        [po(sku="A", qty="4", price="30.00", uom="BOX"), po(sku="B", qty="1", price="24.00", uom="BOX")],
        [inv(sku="A", qty="24", price="5.00", uom="EA"), inv(sku="B", qty="12", price="2.00", uom="EA")],
        [grn(sku="A", qty="4", uom="BOX"), grn(sku="B", qty="1", uom="BOX")],
        [conv(sku="A", size="6"), conv(sku="B", size="12")])
    assert exceptions == []


# ---- real discrepancies survive conversion ----------------------------------

def test_price_mismatch_found_after_conversion():
    [exc] = run([po(qty="10", price="48.00", uom="CASE")], [inv(qty="240", price="2.10", uom="EA")],
                [grn(qty="10", uom="CASE")], [conv(uom="CASE", size="24")])
    assert exc.exception_type == T.PRICE_MISMATCH
    assert exc.amount_impact == Decimal("24.00")  # (2.10 - 2.00) x 240 EA
    assert "50.40 USD per CASE" in exc.summary
    assert "240 EA x 2.10 converted to CASE (1 CASE = 24 EA)" in exc.evidence[1].detail


def test_quantity_mismatch_found_after_conversion():
    [exc] = run([po(qty="10", price="24.00", uom="BOX")], [inv(qty="120", price="2.00", uom="EA")],
                [grn(qty="9", uom="BOX")], [conv()])
    assert exc.exception_type == T.QUANTITY_MISMATCH
    assert exc.quantity_difference == Decimal("1")  # in BOX, the PO unit
    assert exc.amount_impact == Decimal("24.00")


def test_receipt_converted_too():
    assert run([po(qty="10", price="24.00", uom="BOX")], [inv(qty="10", price="24.00", uom="BOX")],
               [grn(qty="120", uom="EA")], [conv()]) == []


def test_conversion_between_two_non_base_units():
    # 2 CASE of 48 EA = 8 BOX of 12 EA; 96.00 per CASE = 24.00 per BOX.
    assert run([po(qty="8", price="24.00", uom="BOX")], [inv(qty="2", price="96.00", uom="CASE")],
               [grn(qty="8", uom="BOX")], [conv(), conv(uom="CASE", size="48", row=3)]) == []


def test_units_with_different_bases_do_not_convert():
    exceptions = run([po(qty="10", price="24.00", uom="BOX")], [inv(qty="5", price="48.00", uom="KG")],
                     [grn(qty="10", uom="BOX")], [conv(), conv(uom="KG", base="G", size="1000", row=3)])
    assert [e.exception_type for e in exceptions] == [T.UNIT_OF_MEASURE_MISMATCH]


def test_unit_codes_are_case_insensitive():
    assert run([po(qty="10", price="24.00", uom="box")], [inv(qty="120", price="2.00", uom="ea")],
               [grn(qty="10", uom="Box")], [conv()]) == []


# ---- missing, inexact or ambiguous unit data --------------------------------

def test_unit_stated_on_some_documents_only():
    [exc] = run([po(uom="BOX")], [inv()], [grn(uom="BOX")])
    assert exc.exception_type == T.UNIT_OF_MEASURE_MISMATCH
    assert "not stated on invoice INV-1" in exc.summary


def test_inexact_quantity_is_not_rounded():
    # 100 EA is 8.333... boxes of 12: refuse to compare rather than round.
    [exc] = run([po(qty="10", price="24.00", uom="BOX")], [inv(qty="10", price="24.00", uom="BOX")],
                [grn(qty="100", uom="EA")], [conv()])
    assert exc.exception_type == T.UNIT_OF_MEASURE_MISMATCH
    assert "no exact equivalent" in exc.summary


def test_inexact_price_is_not_rounded():
    # 10.00 per BOX of 3 is 3.333... per EA.
    [exc] = run([po(qty="30", price="3.33", uom="EA")], [inv(qty="10", price="10.00", uom="BOX")],
                [grn(qty="30", uom="EA")], [conv(size="3")])
    assert exc.exception_type == T.UNIT_OF_MEASURE_MISMATCH


def test_unresolved_line_skips_quantity_and_price_but_keeps_other_rules():
    exceptions = run([po(qty="10", price="24.00", uom="BOX", supplier="S1")],
                     [inv(qty="999", price="1.00", uom="EA", supplier="S9", currency="EUR")], [])
    types = sorted(e.exception_type.value for e in exceptions)
    assert types == ["CURRENCY_MISMATCH", "MISSING_GOODS_RECEIPT", "SUPPLIER_MISMATCH",
                     "UNIT_OF_MEASURE_MISMATCH"]
    [missing] = of_type(exceptions, T.MISSING_GOODS_RECEIPT)
    assert missing.quantity_difference is None  # quantities in unknown units are not summed
    assert missing.amount_impact == Decimal("999.00")  # invoice value does not depend on units


def test_unresolved_missing_invoice_is_reported_but_not_valued():
    exceptions = run([po(qty="10", price="24.00", uom="BOX")], [], [grn(qty="120", uom="EA")])
    [missing] = of_type(exceptions, T.MISSING_INVOICE)
    assert missing.amount_impact == Decimal("0") and missing.quantity_difference is None
    assert any("Not valued" in n for n in missing.notes)


def test_unresolved_units_count_as_unverified_supply_for_duplicates():
    invoices = [inv("A-1", qty="120", price="2.00", uom="EA", on=date(2026, 8, 1)),
                inv("B-2", qty="120", price="2.00", uom="EA", on=date(2026, 8, 5))]
    exceptions = run([po(qty="20", price="24.00", uom="BOX")], invoices, [grn(qty="20", uom="BOX")])
    assert len(of_type(exceptions, T.POSSIBLE_DUPLICATE_INVOICE)) == 1
    assert len(of_type(exceptions, T.UNIT_OF_MEASURE_MISMATCH)) == 1


def test_conversion_table_lookup():
    table = ConversionTable([conv(), conv(uom="CASE", size="48", row=3)])
    assert table.factors("SKU-1", "EA", "BOX") == (Decimal(1), Decimal(12))
    assert table.factors("SKU-1", "CASE", "BOX") == (Decimal(48), Decimal(12))
    assert table.factors("SKU-1", "PALLET", "BOX") is None
    assert table.factors("OTHER", "EA", "BOX") is None
    assert table.factors("ANY", "EA", "EA") == (Decimal(1), Decimal(1))


# ---- unchanged behaviour without units -------------------------------------

def test_lines_without_units_ignore_the_conversion_table():
    pos, invoices, receipts = [po(qty="100")], [inv(qty="120")], [grn(qty="100")]
    assert run(pos, invoices, receipts, [conv()]) == run(pos, invoices, receipts)
    assert len(run(pos, invoices, receipts)) == 1  # the normal quantity mismatch


def test_sample_data_unchanged_with_and_without_conversion_file(sample_dir, tmp_path):
    empty = write(tmp_path, "uom_conversions.csv", CONV_HEADER + "SKU-X,BOX,EA,12\n")
    args = (sample_dir / "purchase_orders.csv", sample_dir / "invoices.csv", sample_dir / "goods_received.csv")
    without = investigate_files(*args)
    with_table = investigate_files(*args, uom_conversions_path=empty)
    assert with_table.exceptions == without.exceptions
    assert without.units.converted_lines == set() and without.units.unresolved == {}


# ---- loading the conversion file -------------------------------------------

def test_load_valid_conversions(tmp_path):
    path = write(tmp_path, "c.csv", CONV_HEADER + "SKU-1, box ,ea,12\nSKU-1,CASE,EA,48\n")
    result = load_uom_conversions(path)
    assert result.issues == []
    assert [(c.uom, c.base_uom, c.units_per_uom) for c in result.records] == [
        ("BOX", "EA", Decimal("12")), ("CASE", "EA", Decimal("48"))]


def test_invalid_conversion_rows_rejected(tmp_path):
    path = write(tmp_path, "c.csv", CONV_HEADER
                 + "SKU-1,BOX,EA,0\n"        # non-positive
                 + "SKU-2,BOX,EA,-6\n"       # negative
                 + "SKU-3,BOX,EA,twelve\n"   # not a number
                 + "SKU-4,B OX,EA,12\n"      # invalid unit code
                 + "SKU-5,EA,EA,12\n"        # base unit with factor != 1
                 + "SKU-6,BOX,,12\n")        # missing base unit
    result = load_uom_conversions(path)
    assert result.records == []
    assert sorted(i.row for i in result.issues) == [2, 3, 4, 5, 6, 7]


def test_conflicting_conversions_are_ambiguous_and_all_rejected(tmp_path):
    path = write(tmp_path, "c.csv", CONV_HEADER + "SKU-1,BOX,EA,12\nSKU-1,BOX,EA,10\nSKU-2,BOX,EA,6\n")
    result = load_uom_conversions(path)
    assert [c.sku for c in result.records] == ["SKU-2"]
    assert [i.row for i in result.issues] == [2, 3]
    assert all("ambiguous" in i.message for i in result.issues)


def test_conflicting_base_units_are_ambiguous(tmp_path):
    path = write(tmp_path, "c.csv", CONV_HEADER + "SKU-1,BOX,EA,12\nSKU-1,BOX,KG,3\n")
    assert load_uom_conversions(path).records == []


def test_exact_duplicate_conversion_keeps_first(tmp_path):
    path = write(tmp_path, "c.csv", CONV_HEADER + "SKU-1,BOX,EA,12\nSKU-1,BOX,EA,12.0\n")
    result = load_uom_conversions(path)
    assert len(result.records) == 1 and result.records[0].source_row == 2
    assert [i.row for i in result.issues] == [3]


def test_conversion_file_problems_are_file_level_issues(tmp_path):
    assert "not found" in load_uom_conversions(tmp_path / "missing.csv").issues[0].message
    bad = write(tmp_path, "c.csv", "sku,uom,units_per_uom\nSKU-1,BOX,12\n")
    assert load_uom_conversions(bad).issues[0].field == "base_uom"


def test_ambiguous_conversion_file_end_to_end(tmp_path):
    files = {
        "purchase_orders.csv": "po_number,line_number,supplier_id,supplier_name,sku,quantity_ordered,unit_price,"
                               "currency,order_date,unit_of_measure\nP1,1,S1,Sup,SKU-1,10,24.00,USD,2026-08-01,BOX\n",
        "invoices.csv": "invoice_number,invoice_date,supplier_id,po_number,sku,quantity_invoiced,unit_price,currency,"
                        "unit_of_measure\nI1,2026-08-10,S1,P1,SKU-1,120,2.00,USD,EA\n",
        "goods_received.csv": "grn_number,received_date,supplier_id,po_number,sku,quantity_received,unit_of_measure\n"
                              "G1,2026-08-08,S1,P1,SKU-1,10,BOX\n",
        "uom_conversions.csv": CONV_HEADER + "SKU-1,BOX,EA,12\nSKU-1,BOX,EA,10\n",
    }
    for name, text in files.items():
        write(tmp_path, name, text)
    result = investigate_files(*(tmp_path / n for n in list(files)[:3]),
                               uom_conversions_path=tmp_path / "uom_conversions.csv")
    assert len(result.dataset.issues) == 2  # both conflicting rows reported
    assert [e.exception_type for e in result.exceptions] == [T.UNIT_OF_MEASURE_MISMATCH]


def test_invalid_unit_code_on_a_document_rejects_the_row(tmp_path):
    path = write(tmp_path, "grn.csv", "grn_number,received_date,supplier_id,po_number,sku,quantity_received,"
                 "unit_of_measure\nG1,2026-08-01,S1,P1,SKU,5,BOX OF 12\n")
    from opsresolve.ingestion import load_goods_received
    result = load_goods_received(path)
    assert result.records == [] and result.issues[0].field == "unit_of_measure"


def test_cli_picks_up_conversion_file_from_data_dir(tmp_path, capsys, sample_dir):
    for name in ("purchase_orders.csv", "invoices.csv", "goods_received.csv"):
        write(tmp_path, name, (sample_dir / name).read_text(encoding="utf-8"))
    write(tmp_path, "uom_conversions.csv", CONV_HEADER + "SKU-X,BOX,EA,12\n")
    assert main(["--data-dir", str(tmp_path), "--output-dir", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "uom_conversions  rows=1" in out
    assert "exceptions: 18" in out  # sample results unchanged


def test_cli_explicit_missing_conversion_file_is_reported(tmp_path, capsys, sample_dir):
    main(["--data-dir", str(sample_dir), "--output-dir", str(tmp_path), "--uom-conversions",
          str(tmp_path / "nope.csv")])
    assert "validation issues: 4" in capsys.readouterr().out
