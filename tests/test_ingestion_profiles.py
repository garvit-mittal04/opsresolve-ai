"""Ingestion and pipeline with an import profile (milestone 7, stage 2).

Expected values are derived by hand from the fixture files in tests/fixtures/profiles/.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from opsresolve.ingestion import (
    MAX_ISSUE_VALUE_CHARS,
    load_dataset,
    load_goods_received,
    load_invoices,
    load_purchase_orders,
    load_uom_conversions,
)
from opsresolve.models import ExceptionType
from opsresolve.pipeline import investigate_files
from opsresolve.profiles import MAX_PREVIEW_CHARS, load_profile, parse_profile

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
REPO = Path(__file__).resolve().parents[1]
FILES = ("purchase_orders", "invoices", "goods_received")

PO_HEADER = ("po_number,line_number,supplier_id,supplier_name,sku,quantity_ordered,"
             "unit_price,currency,order_date\n")
INV_HEADER = "invoice_number,invoice_date,supplier_id,po_number,sku,quantity_invoiced,unit_price,currency\n"


def profile(defaults=None, **files):
    body = {"profile_version": 1, "name": "test", "defaults": defaults or {}, "files": files}
    return parse_profile(json.dumps(body).encode())


def write(tmp_path: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def issues_of(result):
    return [(i.row, i.field, i.message) for i in result.issues]


# ---- the fixture export: every setting combined -------------------------------------------------

@pytest.fixture(scope="module")
def erp():
    return load_profile(FIXTURES / "erp_profile.json")


def run_erp(erp):
    return investigate_files(*[FIXTURES / "erp" / f"{n}.csv" for n in FILES],
                             uom_conversions_path=FIXTURES / "erp" / "uom_conversions.csv", profile=erp)


def test_fixture_export_loads_every_file_without_issues(erp):
    dataset = run_erp(erp).dataset
    assert dataset.issues == []
    assert [len(r.records) for r in (dataset.purchase_orders, dataset.invoices,
                                     dataset.goods_received, dataset.uom_conversions)] == [2, 2, 2, 1]


def test_fixture_values_are_parsed_in_each_files_declared_format(erp):
    dataset = run_erp(erp).dataset
    po = dataset.purchase_orders.records
    # cp1252, ';', DD.MM.YYYY, '1.200' and '1.234,56', mapped German headers (one non-ASCII)
    assert (po[0].quantity_ordered, po[0].unit_price, po[0].order_date) == (Decimal("1200"), Decimal("12.50"),
                                                                            date(2026, 2, 3))
    assert po[1].unit_price == Decimal("1234.56")
    assert po[0].supplier_name == "Brückner Metallwaren GmbH"
    assert po[1].description == "Schweißdraht"
    assert po[0].currency == "EUR" and po[0].source_row == 2 and po[1].source_row == 3
    # UTF-8 with byte-order mark, tab, YYYYMMDD, '1,200' and '1,234.56'
    inv = dataset.invoices.records
    assert inv[0].invoice_number == "INV-501"  # the byte-order mark is not part of the first header
    assert (inv[0].quantity_invoiced, inv[0].unit_price, inv[0].invoice_date) == (Decimal("1200"),
                                                                                  Decimal("12.75"),
                                                                                  date(2026, 2, 10))
    assert inv[1].unit_price == Decimal("1234.56")
    # ISO-8859-1, '|', DD/MM/YYYY
    grn = dataset.goods_received.records
    assert (grn[0].quantity_received, grn[0].unit_of_measure, grn[0].received_date) == (Decimal("100"), "BOX",
                                                                                        date(2026, 2, 7))
    assert dataset.uom_conversions.records[0].units_per_uom == Decimal("12")


def test_fixture_export_reconciles_to_hand_computed_exceptions(erp):
    result = run_erp(erp)
    found = {(e.exception_type, e.sku): e for e in result.exceptions}
    assert set(found) == {(ExceptionType.PRICE_MISMATCH, "A-100"), (ExceptionType.QUANTITY_MISMATCH, "B-200")}
    # A-100: (12.75 - 12.50) x 1200 invoiced; 100 BOX x 12 = 1200 EA received, so no quantity exception.
    assert found[(ExceptionType.PRICE_MISMATCH, "A-100")].amount_impact == Decimal("300.00")
    # B-200: 10 invoiced - 8 received = 2, x 1234.56
    assert found[(ExceptionType.QUANTITY_MISMATCH, "B-200")].quantity_difference == Decimal("2")
    assert found[(ExceptionType.QUANTITY_MISMATCH, "B-200")].amount_impact == Decimal("2469.12")


def test_fixture_export_matches_the_same_data_in_the_built_in_format(erp):
    profiled = run_erp(erp)
    canonical = investigate_files(*[FIXTURES / "canonical" / f"{n}.csv" for n in FILES],
                                  uom_conversions_path=FIXTURES / "canonical" / "uom_conversions.csv")
    assert [e.model_dump() for e in profiled.exceptions] == [e.model_dump() for e in canonical.exceptions]
    for name in FILES + ("uom_conversions",):
        assert getattr(profiled.dataset, name).records == getattr(canonical.dataset, name).records


def test_load_dataset_passes_each_file_its_own_format(erp):
    dataset = load_dataset(*[FIXTURES / "erp" / f"{n}.csv" for n in FILES], profile=erp)
    assert dataset.invoices.file_format.delimiter == "\t"
    assert dataset.goods_received.file_format.delimiter == "|"
    assert dataset.purchase_orders.file_format.delimiter == ";"
    assert dataset.uom_conversions is None


# ---- no profile: unchanged ----------------------------------------------------------------------

def test_without_a_profile_no_format_is_recorded():
    dataset = load_dataset(*[FIXTURES / "canonical" / f"{n}.csv" for n in FILES])
    assert dataset.purchase_orders.file_format is None and dataset.invoices.file_format is None


def test_without_a_profile_a_semicolon_file_gets_no_delimiter_hint(tmp_path):
    path = write(tmp_path, "po.csv", PO_HEADER.replace(",", ";"))
    result = load_purchase_orders(path)
    assert {i.message for i in result.issues} == {
        "Required column is missing; no rows from this file were loaded"}


def test_without_a_profile_long_values_are_reported_in_full(tmp_path):
    long = "9" * 300 + "x"
    path = write(tmp_path, "po.csv", PO_HEADER + f"PO-1,1,S1,Supplier,SKU-1,{long},1.00,EUR,2026-03-01\n")
    [issue] = load_purchase_orders(path).issues
    assert issue.value == long


@pytest.mark.parametrize("folder, uom", [("data/sample", None),
                                         ("data/evaluation", "data/evaluation/uom_conversions.csv")])
def test_an_empty_profile_reads_the_project_data_like_no_profile(folder, uom):
    """A profile that declares nothing means the built-in format: same records, exceptions, rejected rows."""
    paths = [REPO / folder / f"{n}.csv" for n in FILES]
    uom_path = REPO / uom if uom else None
    plain = investigate_files(*paths, uom_conversions_path=uom_path)
    profiled = investigate_files(*paths, uom_conversions_path=uom_path, profile=profile())
    assert [e.model_dump() for e in profiled.exceptions] == [e.model_dump() for e in plain.exceptions]
    for name in FILES:
        assert getattr(profiled.dataset, name).records == getattr(plain.dataset, name).records
    # Only the wording of format errors may differ (strict parsing explains the declared format).
    assert ([(i.file, i.row, i.field, i.value) for i in profiled.dataset.issues]
            == [(i.file, i.row, i.field, i.value) for i in plain.dataset.issues])


# ---- whitespace: same as ingestion without a profile ----------------------------------------------

def test_whitespace_around_headers_and_values_is_handled_like_no_profile(tmp_path):
    text = (" PO_Number ,line_number ,supplier_id,supplier_name,sku,quantity_ordered,unit_price,currency,order_date\n"
            "  PO-1 , 1 ,\tS1\t, Supplier A ,SKU-1, 5 , 12.50 , eur ,  2026-03-01 \n")
    path = write(tmp_path, "po.csv", text)
    plain = load_purchase_orders(path)
    profiled = load_purchase_orders(path, profile().format_for("purchase_orders"))
    assert plain.issues == [] and profiled.issues == []
    assert profiled.records == plain.records
    record = profiled.records[0]
    assert (record.po_number, record.supplier_id, record.supplier_name) == ("PO-1", "S1", "Supplier A")
    assert (record.quantity_ordered, record.unit_price, record.currency) == (Decimal("5"), Decimal("12.50"), "EUR")


def test_whitespace_only_values_are_missing_with_and_without_a_profile(tmp_path):
    path = write(tmp_path, "po.csv", PO_HEADER + "PO-1,1,S1,Supplier,SKU-1,   ,1.00,EUR,2026-03-01\n")
    expected = [(2, "quantity_ordered", "Missing required value; row rejected")]
    assert issues_of(load_purchase_orders(path)) == expected
    assert issues_of(load_purchase_orders(path, profile().format_for("purchase_orders"))) == expected


def test_a_space_thousands_separator_inside_a_number_is_not_trimmed_away(tmp_path):
    fmt = profile({"delimiter": ";", "decimal_separator": ",", "thousands_separator": " "}).format_for(
        "purchase_orders")
    path = write(tmp_path, "po.csv", PO_HEADER.replace(",", ";")
                 + "PO-1;1;S1;Supplier;SKU-1; 1 200 ;1 234,50;EUR;2026-03-01\n"
                 + "PO-1;2;S1;Supplier;SKU-2;1  200;1,00;EUR;2026-03-01\n")
    result = load_purchase_orders(path, fmt)
    assert (result.records[0].quantity_ordered, result.records[0].unit_price) == (Decimal("1200"),
                                                                                  Decimal("1234.50"))
    assert [(i.row, i.field) for i in result.issues] == [(3, "quantity_ordered")]


# ---- column mapping -------------------------------------------------------------------------------

def test_headers_are_matched_case_insensitively_and_unmapped_fields_use_canonical_names(tmp_path):
    fmt = profile(invoices={"columns": {"invoice_number": "Invoice No", "sku": "Item"}}).format_for("invoices")
    path = write(tmp_path, "inv.csv", "INVOICE NO,invoice_date,supplier_id,po_number,item,quantity_invoiced,"
                                      "unit_price,currency\nINV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR\n")
    result = load_invoices(path, fmt)
    assert result.issues == []
    assert (result.records[0].invoice_number, result.records[0].sku) == ("INV-1", "SKU-1")


def test_missing_mapped_required_column_is_a_file_level_issue(tmp_path):
    fmt = profile(invoices={"columns": {"sku": "Item"}}).format_for("invoices")
    path = write(tmp_path, "inv.csv", INV_HEADER + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR\n")
    result = load_invoices(path, fmt)
    assert result.records == [] and result.rows_read == 0
    # The canonical 'sku' column is present but is never used instead of the declared 'Item'.
    assert [(i.row, i.field, i.message) for i in result.issues] == [
        (None, "sku", "Column 'Item' (mapped by the import profile) is missing; no rows from this file were loaded")]


def test_missing_mapped_optional_column_is_a_file_level_issue(tmp_path):
    fmt = profile(invoices={"columns": {"unit_of_measure": "UoM"}}).format_for("invoices")
    path = write(tmp_path, "inv.csv", INV_HEADER + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR\n")
    result = load_invoices(path, fmt)
    assert result.records == []
    assert issues_of(result) == [(None, "unit_of_measure", "Column 'UoM' (mapped by the import profile) is "
                                                           "missing; no rows from this file were loaded")]


def test_unmapped_optional_column_may_be_absent(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR\n")
    result = load_invoices(path, profile().format_for("invoices"))
    assert result.issues == [] and result.records[0].unit_of_measure is None


def test_missing_unmapped_required_column_uses_the_existing_message(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER.replace(",currency", "") + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00\n")
    assert issues_of(load_invoices(path, profile().format_for("invoices"))) == [
        (None, "currency", "Required column is missing; no rows from this file were loaded")]


def test_mapped_header_and_canonical_name_both_present_is_ambiguous(tmp_path):
    fmt = profile(invoices={"columns": {"sku": "Item"}}).format_for("invoices")
    path = write(tmp_path, "inv.csv", INV_HEADER.rstrip("\n") + ",Item\nINV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR,X\n")
    result = load_invoices(path, fmt)
    assert result.records == []
    assert issues_of(result) == [(None, "sku", "Both the mapped column 'Item' and a column named 'sku' are "
                                               "present; the column to read is ambiguous; no rows from this "
                                               "file were loaded")]


def test_a_header_repeated_case_insensitively_is_ambiguous(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER.rstrip("\n") + ",SKU\nINV-1,2026-03-01,S1,PO-1,A,2,3.00,EUR,B\n")
    result = load_invoices(path, profile().format_for("invoices"))
    assert result.records == []
    assert issues_of(result) == [(None, "sku", "Column 'sku' appears 2 times; the column to read is ambiguous; "
                                               "no rows from this file were loaded")]


def test_unrelated_and_internal_named_columns_are_ignored(tmp_path):
    # converted_from and source_row are set by the program, never read from a file.
    path = write(tmp_path, "inv.csv", INV_HEADER.rstrip("\n") + ",Notes,converted_from,source_row,\n"
                                      "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR,hello,99 BOX,77,x\n")
    result = load_invoices(path, profile().format_for("invoices"))
    assert result.issues == []
    assert result.records[0].converted_from is None and result.records[0].source_row == 2


def test_a_row_with_only_unrelated_values_is_rejected_not_skipped(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER.rstrip("\n") + ",Notes\n,,,,,,,,stray\n")
    result = load_invoices(path, profile().format_for("invoices"))
    assert result.rows_read == 1 and result.records == []
    assert {(i.row, i.message) for i in result.issues} == {(2, "Missing required value; row rejected")}



# ---- rows with fewer or more cells than the header ----------------------------------------------

def test_profile_row_with_fewer_cells_reports_the_missing_required_fields_only(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER
                 + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00\n"   # line 2: currency cell absent
                 + "INV-2,2026-03-02,S2\n"                     # line 3: stops after supplier_id
                 + "INV-3,2026-03-03,S3,PO-3,SKU-3,4,5.00,USD\n")
    result = load_invoices(path, profile().format_for("invoices"))
    assert issues_of(result) == [
        (2, "currency", "Missing required value; row rejected"),
        (3, "po_number", "Missing required value; row rejected"),
        (3, "sku", "Missing required value; row rejected"),
        (3, "quantity_invoiced", "Missing required value; row rejected"),
        (3, "unit_price", "Missing required value; row rejected"),
        (3, "currency", "Missing required value; row rejected"),
    ]
    assert all(i.value == "" for i in result.issues)
    # The cells that are present stay in their own columns: the rejected key is (PO-1, SKU-1).
    assert result.rejected_keys == {("PO-1", "SKU-1")}
    [record] = result.records
    assert (record.source_row, record.invoice_number, record.po_number, record.sku, record.quantity_invoiced,
            record.unit_price, record.currency) == (4, "INV-3", "PO-3", "SKU-3", Decimal("4"), Decimal("5.00"), "USD")


def test_profile_row_omitting_a_trailing_optional_field_loads_with_it_unset(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER.rstrip("\n") + ",unit_of_measure\n"
                 + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR\n"
                 + "INV-2,2026-03-01,S1,PO-1,SKU-2,7,8.00,EUR,box\n")
    result = load_invoices(path, profile().format_for("invoices"))
    assert result.issues == []
    first, second = result.records
    assert (first.sku, first.quantity_invoiced, first.unit_price, first.currency, first.unit_of_measure) == (
        "SKU-1", Decimal("2"), Decimal("3.00"), "EUR", None)
    assert (second.sku, second.unit_of_measure) == ("SKU-2", "BOX")


def test_profile_short_row_with_reordered_mapped_columns_keeps_each_value_in_its_field(tmp_path):
    fmt = profile({"delimiter": ";", "decimal_separator": ",", "thousands_separator": "."},
                  invoices={"columns": {"sku": "Item", "po_number": "Order", "unit_of_measure": "Unit"}}
                  ).format_for("invoices")
    header = "Item;Order;invoice_number;invoice_date;supplier_id;quantity_invoiced;unit_price;currency;Unit\n"
    path = write(tmp_path, "inv.csv", header
                 + "SKU-9;PO-9;INV-9;2026-03-01;S9;1.500;2,25;EUR\n"   # trailing optional Unit absent
                 + "SKU-8;PO-8;INV-8;2026-03-01;S8;3\n")                # price and currency absent
    result = load_invoices(path, fmt)
    [record] = result.records
    assert (record.sku, record.po_number, record.invoice_number, record.supplier_id, record.quantity_invoiced,
            record.unit_price, record.unit_of_measure) == ("SKU-9", "PO-9", "INV-9", "S9", Decimal("1500"),
                                                           Decimal("2.25"), None)
    assert [(i.row, i.field) for i in result.issues] == [(3, "unit_price"), (3, "currency")]
    assert result.rejected_keys == {("PO-8", "SKU-8")}


def test_profile_row_with_more_cells_than_the_header_rejects_the_file(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR\n"
                                      + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR,extra\n")
    result = load_invoices(path, profile().format_for("invoices"))
    assert result.records == [] and result.rows_read == 0
    assert issues_of(result) == [(None, None, "File could not be parsed with delimiter ',': Error tokenizing "
                                              "data. C error: Expected 8 fields in line 3, saw 9\n")]


def test_no_profile_row_with_fewer_cells_is_unchanged(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00\n")
    result = load_invoices(path)
    assert issues_of(result) == [(2, "currency", "Missing required value; row rejected")]
    assert result.rejected_keys == {("PO-1", "SKU-1")}


def test_no_profile_row_with_more_cells_shifts_values_known_bug(tmp_path):
    """Characterises a KNOWN BUG in the no-profile path, deliberately left unfixed in milestone 7.

    pandas treats the extra first cell as an index, so every value moves one column left
    (invoice_number gets the date, currency gets 'extra'). Import profiles reject such a file
    instead (see the test above). Fixing the no-profile path is a separate, approved change;
    when it is fixed, this test must be updated with it.
    """
    path = write(tmp_path, "inv.csv", INV_HEADER + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR,extra\n")
    result = load_invoices(path)
    assert [(i.row, i.field, i.value) for i in result.issues] == [
        (2, "invoice_date", "S1"), (2, "unit_price", "EUR"), (2, "currency", "extra")]
    assert result.rejected_keys == {("SKU-1", "2")}  # po_number and sku read the wrong cells

# ---- delimiter and encoding -----------------------------------------------------------------------

def test_wrong_delimiter_reports_missing_columns_and_a_hint(tmp_path):
    fmt = profile({"delimiter": ";"}).format_for("invoices")
    path = write(tmp_path, "inv.csv", INV_HEADER + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR\n")
    result = load_invoices(path, fmt)
    assert result.records == []
    assert len(result.issues) == 9  # eight required columns + the hint
    assert result.issues[-1].field is None
    assert result.issues[-1].message == (
        "The header was read as a single column using the declared delimiter ';', but it contains ','; "
        "check the delimiter declared in the import profile")


def test_wrong_encoding_is_a_file_level_issue(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER + "INV-1,2026-03-01,Müller,PO-1,SKU-1,2,3.00,EUR\n", "cp1252")
    result = load_invoices(path, profile().format_for("invoices"))  # declares utf-8
    assert result.records == []
    offset = len(INV_HEADER) + len("INV-1,2026-03-01,M")  # the cp1252 byte for 'ü'
    assert issues_of(result) == [(None, None, f"File is not valid utf-8 text (byte {offset} cannot "
                                              "be decoded); check the encoding declared in the import profile; "
                                              "no rows from this file were loaded")]


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_decode_error_reports_the_byte_position_in_the_file(tmp_path, bom):
    data = bom + INV_HEADER.encode() + b"INV-1,2026-03-01,M\xfcller,PO-1,SKU-1,2,3.00,EUR\n"
    path = tmp_path / "inv.csv"
    path.write_bytes(data)
    [issue] = load_invoices(path, profile().format_for("invoices")).issues
    position = data.index(b"\xfc")  # the actual offset of the invalid byte, counting any byte-order mark
    assert position == len(bom) + len(INV_HEADER) + len("INV-1,2026-03-01,M")
    assert issue.message.startswith(f"File is not valid utf-8 text (byte {position} cannot be decoded)")

def test_byte_undefined_in_cp1252_is_rejected(tmp_path):
    path = tmp_path / "inv.csv"
    path.write_bytes(INV_HEADER.encode() + b"INV-1,2026-03-01,S\x81,PO-1,SKU-1,2,3.00,EUR\n")
    [issue] = load_invoices(path, profile({"encoding": "cp1252"}).format_for("invoices")).issues
    assert issue.message.startswith("File is not valid cp1252 text")


def test_byte_order_mark_with_a_non_utf8_encoding_is_a_file_level_issue(tmp_path):
    path = tmp_path / "inv.csv"
    path.write_bytes(b"\xef\xbb\xbf" + (INV_HEADER + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR\n").encode())
    result = load_invoices(path, profile({"encoding": "cp1252"}).format_for("invoices"))
    assert issues_of(result) == [(None, None, "File starts with a UTF-8 byte-order mark, but the import profile "
                                              "declares encoding 'cp1252'; no rows from this file were loaded")]


def test_same_text_decodes_differently_only_by_declaration(tmp_path):
    path = write(tmp_path, "po.csv", PO_HEADER + "PO-1,1,S1,Café Ü,SKU-1,1,1.00,EUR,2026-03-01\n", "iso-8859-1")
    result = load_purchase_orders(path, profile({"encoding": "iso-8859-1"}).format_for("purchase_orders"))
    assert result.records[0].supplier_name == "Café Ü"


def test_windows_line_endings_are_read_like_unix_ones(tmp_path):
    rows = INV_HEADER + "INV-1,2026-03-01,S1,PO-1,SKU-1,2,3.00,EUR\n\nINV-2,2026-03-01,S1,PO-1,SKU-2,2,3.00,EUR\n"
    unix = load_invoices(write(tmp_path, "unix.csv", rows), profile().format_for("invoices"))
    windows = load_invoices(write(tmp_path, "win.csv", rows.replace("\n", "\r\n")), profile().format_for("invoices"))
    assert windows.issues == [] and windows.records == unix.records
    assert [r.source_row for r in windows.records] == [2, 4]

@pytest.mark.parametrize("text, message", [
    ("", "File has no header row (it is empty or its first line is blank)"),
    ("\n" + INV_HEADER, "File has no header row (it is empty or its first line is blank)"),
])
def test_empty_file_or_blank_first_line(tmp_path, text, message):
    path = write(tmp_path, "inv.csv", text)
    assert issues_of(load_invoices(path, profile().format_for("invoices"))) == [(None, None, message)]


def test_missing_file_is_reported_as_before(tmp_path):
    result = load_invoices(tmp_path / "nope.csv", profile().format_for("invoices"))
    assert result.issues[0].message == f"File not found: {tmp_path / 'nope.csv'}"


# ---- malformed values and row numbers -------------------------------------------------------------

EU = {"delimiter": ";", "date_format": "DD.MM.YYYY", "decimal_separator": ",", "thousands_separator": "."}


def eu_po(tmp_path, *rows):
    return write(tmp_path, "po.csv", PO_HEADER.replace(",", ";") + "".join(r + "\n" for r in rows))


def test_malformed_values_are_rejected_with_line_field_and_declared_format(tmp_path):
    path = eu_po(tmp_path,
                 "PO-1;1;S1;Supplier;SKU-1;1.200;12,50;EUR;03.02.2026",   # line 2: valid
                 "",                                                     # line 3: blank
                 "PO-1;2;S1;Supplier;SKU-2;1,200.5;12,50;EUR;03.02.2026",  # line 4: other convention
                 "PO-1;3;S1;Supplier;SKU-3;5;12,50;EUR;2026-02-03",       # line 5: ISO date, not declared
                 "PO-1;4;S1;Supplier;SKU-4;5;12,50;EUR;30.02.2026",       # line 6: impossible date
                 "PO-1;5;S1;Supplier;SKU-5;5;1.23,4;EUR;03.02.2026")      # line 7: misplaced grouping
    result = load_purchase_orders(path, profile(EU).format_for("purchase_orders"))
    assert [r.sku for r in result.records] == ["SKU-1"]
    assert result.rows_read == 5
    assert issues_of(result) == [
        (4, "quantity_ordered", "'1,200.5' is not a number in the declared format (decimal separator ',', "
                                "thousands separator '.'); row rejected"),
        (5, "order_date", "'2026-02-03' does not match the declared date format DD.MM.YYYY; row rejected"),
        (6, "order_date", "'30.02.2026' is not a valid calendar date (format DD.MM.YYYY); row rejected"),
        (7, "unit_price", "'1.23,4' is not a number in the declared format (decimal separator ',', "
                          "thousands separator '.'); row rejected"),
    ]
    assert [i.value for i in result.issues] == ["1,200.5", "2026-02-03", "30.02.2026", "1.23,4"]
    assert ("PO-1", "SKU-2") in result.rejected_keys and ("PO-1", "SKU-5") in result.rejected_keys


def test_every_problem_in_a_row_is_reported_once_in_field_order(tmp_path):
    path = eu_po(tmp_path, "PO-1;1;S1;Supplier;SKU-1;abc;12,50;EURO;2026-02-03")
    result = load_purchase_orders(path, profile(EU).format_for("purchase_orders"))
    assert [(i.row, i.field) for i in result.issues] == [
        (2, "quantity_ordered"), (2, "currency"), (2, "order_date")]
    assert result.issues[1].message == "Value error, must be a 3-letter ISO 4217 currency code; row rejected"


def test_model_rules_still_apply_after_parsing(tmp_path):
    path = eu_po(tmp_path, "PO-1;1;S1;Supplier;SKU-1;-1.000;12,50;EUR;03.02.2026")
    [issue] = load_purchase_orders(path, profile(EU).format_for("purchase_orders")).issues
    assert (issue.row, issue.field, issue.value) == (2, "quantity_ordered", "-1.000")
    assert issue.message == "Input should be greater than 0; row rejected"


def test_line_number_with_thousands_grouping_and_decimal_part(tmp_path):
    path = eu_po(tmp_path, "PO-1;1.001;S1;Supplier;SKU-1;1;1;EUR;03.02.2026",
                 "PO-1;2,5;S1;Supplier;SKU-2;1;1;EUR;03.02.2026")
    result = load_purchase_orders(path, profile(EU).format_for("purchase_orders"))
    assert result.records[0].line_number == 1001
    assert [(i.row, i.field) for i in result.issues] == [(3, "line_number")]


def test_duplicate_checks_still_apply_with_a_profile(tmp_path):
    path = eu_po(tmp_path, "PO-1;1;S1;Supplier;SKU-1;1;1;EUR;03.02.2026",
                 "PO-1;1;S1;Supplier;SKU-2;1;1;EUR;03.02.2026")
    [issue] = load_purchase_orders(path, profile(EU).format_for("purchase_orders")).issues
    assert (issue.row, issue.field) == (3, "line_number")
    assert issue.message.startswith("Duplicate PO line PO-1/1 (first seen on line 2)")


def test_uom_conversion_factor_in_the_declared_format(tmp_path):
    path = write(tmp_path, "uom.csv", "sku;uom;base_uom;units_per_uom\nA;BOX;EA;1.000\nB;PAL;EA;2,5\n")
    result = load_uom_conversions(path, profile(EU).format_for("uom_conversions"))
    assert [r.units_per_uom for r in result.records] == [Decimal("1000"), Decimal("2.5")]


def test_goods_received_in_the_declared_format(tmp_path):
    path = write(tmp_path, "grn.csv", "grn_number|received_date|supplier_id|po_number|sku|quantity_received\n"
                                      "G-1|12/31/2026|S1|PO-1|SKU-1|0.5\n")
    fmt = profile({"delimiter": "|", "date_format": "MM/DD/YYYY"}).format_for("goods_received")
    record = load_goods_received(path, fmt).records[0]
    assert (record.received_date, record.quantity_received) == (date(2026, 12, 31), Decimal("0.5"))


# ---- very long values -----------------------------------------------------------------------------

def test_a_very_long_malformed_number_gives_a_bounded_message_and_value(tmp_path):
    long = "1" * 100_000 + "x"
    path = eu_po(tmp_path, f"PO-1;1;S1;Supplier;SKU-1;{long};1;EUR;03.02.2026")
    [issue] = load_purchase_orders(path, profile(EU).format_for("purchase_orders")).issues
    assert issue.message == (f"{'1' * MAX_PREVIEW_CHARS!r}... (100001 characters) is not a number in the declared "
                             "format (decimal separator ',', thousands separator '.'); row rejected")
    assert issue.value == "1" * MAX_ISSUE_VALUE_CHARS + "... [100001 characters; shortened]"


def test_a_very_long_value_failing_a_model_rule_is_shortened(tmp_path):
    long = "E" * 5000
    path = eu_po(tmp_path, f"PO-1;1;S1;Supplier;SKU-1;1;1;{long};03.02.2026")
    [issue] = load_purchase_orders(path, profile(EU).format_for("purchase_orders")).issues
    assert issue.field == "currency" and len(issue.message) < 100
    assert issue.value == "E" * MAX_ISSUE_VALUE_CHARS + "... [5000 characters; shortened]"


def test_a_very_long_date_gives_a_bounded_message(tmp_path):
    path = eu_po(tmp_path, f"PO-1;1;S1;Supplier;SKU-1;1;1;EUR;{'03.02.2026' * 1000}")
    [issue] = load_purchase_orders(path, profile(EU).format_for("purchase_orders")).issues
    assert issue.field == "order_date" and len(issue.message) < 200 and "(10000 characters)" in issue.message


# ---- no profile: profile code is never reached (milestone 7, acceptance gap G1) ---------------------
# Each profile-only function is replaced, under the name its caller actually uses, by one that fails the
# test. Ordinary no-profile parsing (pandas.read_csv, the models' own Decimal and date parsing) and
# helpers the no-profile path legitimately calls (ingestion._issue_value) are deliberately left alone.

import opsresolve.cli as cli_module  # noqa: E402
import opsresolve.ingestion as ingestion_module  # noqa: E402
import opsresolve.profiles as profiles_module  # noqa: E402
import opsresolve.webapp as webapp_module  # noqa: E402
from opsresolve.config import MatchConfig  # noqa: E402

PROFILE_ONLY = {
    profiles_module: ("load_profile", "parse_profile", "parse_number", "parse_date"),
    ingestion_module: ("parse_number", "parse_date", "preview", "canonical_fields", "required_fields",
                       "numeric_fields", "date_fields", "_read_with_format", "_hint_wrong_delimiter",
                       "_value_parsers", "_parse_declared_values", "_file_issue"),
    cli_module: ("load_profile",),
    webapp_module: ("parse_profile", "check_profile"),
}
EVAL_DIR = REPO / "data" / "evaluation"
SAMPLE_DIR = REPO / "data" / "sample"


class ProfileCodeCalled(AssertionError):
    pass


def forbid_profile_code(monkeypatch) -> list[str]:
    """Make every profile-only function fail if called; returns the list of attempted calls."""
    calls: list[str] = []
    for module, names in PROFILE_ONLY.items():
        for name in names:
            assert callable(getattr(module, name)), f"{module.__name__}.{name} is not a function"
            qualified = f"{module.__name__}.{name}"

            def refuse(*args, _name=qualified, **kwargs):
                calls.append(_name)
                raise ProfileCodeCalled(f"profile-only code called without a profile: {_name}")
            monkeypatch.setattr(module, name, refuse)
    return calls


def run_folder(folder: Path, with_uom: bool):
    paths = [folder / f"{n}.csv" for n in FILES]
    return investigate_files(*paths, uom_conversions_path=folder / "uom_conversions.csv" if with_uom else None)


def outcome(result) -> tuple:
    dataset = result.dataset
    loads = [dataset.purchase_orders, dataset.invoices, dataset.goods_received, dataset.uom_conversions]
    return ([e.model_dump() for e in result.exceptions], [i.model_dump() for i in dataset.issues],
            [None if load is None else (load.records, load.rows_read, load.rejected_keys, load.file_format)
             for load in loads], result.profile)


@pytest.mark.parametrize("folder, with_uom", [(SAMPLE_DIR, False), (EVAL_DIR, True)], ids=["sample", "evaluation"])
def test_no_profile_ingestion_and_pipeline_never_reach_profile_code(monkeypatch, folder, with_uom):
    expected = outcome(run_folder(folder, with_uom))
    expected_dataset = load_dataset(*[folder / f"{n}.csv" for n in FILES],
                                    folder / "uom_conversions.csv" if with_uom else None)
    calls = forbid_profile_code(monkeypatch)
    assert outcome(run_folder(folder, with_uom)) == expected
    dataset = load_dataset(*[folder / f"{n}.csv" for n in FILES], folder / "uom_conversions.csv" if with_uom else None)
    assert [i.model_dump() for i in dataset.issues] == [i.model_dump() for i in expected_dataset.issues]
    assert dataset.profile is None and (dataset.uom_conversions is not None) == with_uom
    assert calls == []


def _cli_outputs(capsys, out_dir: Path, *args) -> tuple:
    code = cli_module.main([*map(str, args), "--output-dir", str(out_dir)])
    console = capsys.readouterr().out.replace(str(out_dir), "<out>")
    markdown = [line for line in (out_dir / "exception_report.md").read_text(encoding="utf-8").splitlines()
                if not line.startswith("Generated:")]
    csvs = [(out_dir / name).read_bytes() for name in ("exceptions.csv", "validation_issues.csv")]
    return code, console, markdown, csvs


@pytest.mark.parametrize("folder", [SAMPLE_DIR, EVAL_DIR], ids=["sample", "evaluation"])
def test_cli_without_profile_never_loads_or_parses_a_profile(monkeypatch, capsys, tmp_path, folder):
    # The evaluation folder's uom_conversions.csv is found automatically, as before.
    expected = _cli_outputs(capsys, tmp_path / "before", "--data-dir", folder)
    calls = forbid_profile_code(monkeypatch)
    assert _cli_outputs(capsys, tmp_path / "after", "--data-dir", folder) == expected
    assert folder != EVAL_DIR or "uom_conversions  rows=3    accepted=3    rejected=0" in expected[1].splitlines()
    assert "Import profile" not in expected[1]
    assert calls == []


@pytest.mark.parametrize("folder, with_uom", [(SAMPLE_DIR, False), (EVAL_DIR, True)], ids=["sample", "evaluation"])
def test_web_app_without_profile_never_reaches_profile_code(monkeypatch, folder, with_uom):
    uploads = {spec.key: webapp_module.Upload(spec.internal_name, (folder / spec.internal_name).read_bytes())
               for spec in webapp_module.INPUTS if spec.required or with_uom}
    uploads.setdefault("uom_conversions", None)
    expected = outcome(webapp_module.run_reconciliation(uploads, MatchConfig()))
    expected_fingerprint = webapp_module.fingerprint(uploads, MatchConfig())
    calls = forbid_profile_code(monkeypatch)
    assert outcome(webapp_module.run_reconciliation(uploads, MatchConfig())) == expected
    assert not with_uom or expected[2][3] is not None  # the conversion upload was loaded
    assert webapp_module.fingerprint(uploads, MatchConfig()) == expected_fingerprint
    assert webapp_module.fingerprint(uploads, MatchConfig(), None) == expected_fingerprint
    assert calls == []


def test_the_forbidding_patches_are_wired_to_the_names_actually_called(monkeypatch, tmp_path, capsys):
    """Positive control: with a profile, the same patches are hit, so the tests above are not vacuous."""
    profile = load_profile(FIXTURES / "erp_profile.json")
    calls = forbid_profile_code(monkeypatch)
    with pytest.raises(ProfileCodeCalled):
        load_invoices(FIXTURES / "erp" / "invoices.csv", profile.format_for("invoices"))
    with pytest.raises(ProfileCodeCalled):
        cli_module.main(["--data-dir", str(FIXTURES / "erp"), "--profile", str(FIXTURES / "erp_profile.json"),
                         "--output-dir", str(tmp_path)])
    with pytest.raises(ProfileCodeCalled):
        webapp_module.check_profile(webapp_module.Upload("p.json", b"{}"))
    assert calls == ["opsresolve.ingestion._read_with_format", "opsresolve.cli.load_profile",
                     "opsresolve.webapp.check_profile"]
    assert not (tmp_path / "exception_report.md").exists()


# ---- reformatted data/sample (milestone 7, acceptance gap G3: criterion 2) ---------------------------
# tests/fixtures/profiles/sample_de is data/sample rewritten as a German ERP export (see its README).
# Read with its profile it must give the same records, exceptions and validation outcomes as the original.
# Deliberate differences, because only the source format differs: LoadResult.file_format and the
# dataset's profile (None for the original), and the messages listed in test_sample_de_validation_outcomes.

import re  # noqa: E402

SAMPLE_DE = FIXTURES / "sample_de"
SAMPLE_DE_PROFILE = FIXTURES / "sample_de_profile.json"
SAMPLE_ROWS = {"purchase_orders": 19, "invoices": 22, "goods_received": 21}


def sample_pair():
    original = investigate_files(*[SAMPLE_DIR / f"{n}.csv" for n in FILES])
    reformatted = investigate_files(*[SAMPLE_DE / f"{n}.csv" for n in FILES], profile=load_profile(SAMPLE_DE_PROFILE))
    return original, reformatted


@pytest.mark.parametrize("label", FILES)
def test_sample_de_fixture_is_written_in_the_declared_format(label):
    data = (SAMPLE_DE / f"{label}.csv").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")  # cp1252, not UTF-8: the headers contain single-byte umlauts
    lines = data.decode("cp1252").split("\n")
    assert lines[-1] == "" and "\r" not in data.decode("cp1252")
    header, rows = lines[0].split(";"), [line.split(";") for line in lines[1:-1]]
    assert any(ch in lines[0] for ch in "äü") and "," not in lines[0]
    assert len(rows) == SAMPLE_ROWS[label] and all(len(row) == len(header) for row in rows)
    text = "\n".join(lines[1:])
    assert re.search(r";[0-9]{2}\.[0-9]{2}\.2026(;|\n)", text)  # DD.MM.YYYY
    assert ";5.000" in text  # thousands dot
    assert not re.search(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text)  # no ISO dates left
    if label != "goods_received":
        assert re.search(r";[0-9]+,[0-9]{2};", text)  # decimal comma


def test_sample_de_fixture_does_not_load_without_its_profile():
    dataset = load_dataset(*[SAMPLE_DE / f"{n}.csv" for n in FILES])
    assert [len(getattr(dataset, n).records) for n in FILES] == [0, 0, 0]


def test_sample_de_records_are_identical_to_the_original():
    original, reformatted = sample_pair()
    for label in FILES:
        before, after = getattr(original.dataset, label), getattr(reformatted.dataset, label)
        assert len(before.records) > 0
        # Every field of every accepted record, including source_row (the CSV line number).
        assert after.records == before.records
        assert (after.rows_read, after.rows_rejected, after.rejected_keys) == (
            before.rows_read, before.rows_rejected, before.rejected_keys)
        assert after.file_label == before.file_label == label
        assert before.file_format is None and after.file_format.delimiter == ";"  # deliberate difference
    assert original.dataset.uom_conversions is reformatted.dataset.uom_conversions is None
    assert original.profile is None
    assert reformatted.profile.name == "Reformatted sample, German ERP layout (test fixture)"


def test_sample_de_exceptions_are_identical_to_the_original():
    original, reformatted = sample_pair()
    assert len(original.exceptions) == 18  # the seeded sample scenarios (data/sample/README.md)
    # IDs, types, severities, exact amounts, quantities, notes and evidence (source, reference, line).
    assert [e.model_dump() for e in reformatted.exceptions] == [e.model_dump() for e in original.exceptions]
    assert reformatted.match == original.match


def test_sample_de_validation_outcomes_match_the_original():
    original, reformatted = sample_pair()
    before, after = original.dataset.issues, reformatted.dataset.issues
    # The same rejected rows, at the same file, line and field, in the same order.
    assert [(i.file, i.row, i.field) for i in after] == [(i.file, i.row, i.field) for i in before] == [
        ("purchase_orders", 16, "unit_price"), ("invoices", 15, "quantity_invoiced"),
        ("goods_received", 18, "received_date")]
    # Messages and values must be identical except for these two, written out in full:
    different = {
        # The raw value differs (the malformed date rewritten in the declared format): allowed by criterion 2.
        ("goods_received", 18, "received_date"):
            ("09.13.2026", "'09.13.2026' is not a valid calendar date (format DD.MM.YYYY); row rejected"),
        # The raw value is the same, but design rule 10 rejects letters in a number field with a message naming
        # the declared format. Allowed by criterion 2 as amended: messages must match except where the raw value
        # differs or where the declared-format rules (design rules 10-11) require a format-specific message.
        ("invoices", 15, "quantity_invoiced"):
            ("twelve", "'twelve' is not a number in the declared format (decimal separator ',', "
                       "thousands separator '.'); row rejected"),
    }
    for old, new in zip(before, after):
        key = (old.file, old.row, old.field)
        if key in different:
            assert (new.value, new.message) == different[key]
        else:
            assert (new.value, new.message) == (old.value, old.message)
    assert before[1].value == after[1].value == "twelve"
