from decimal import Decimal
from pathlib import Path

from opsresolve.ingestion import load_goods_received, load_invoices, load_purchase_orders

PO_HEADER = "po_number,line_number,supplier_id,supplier_name,sku,description,quantity_ordered,unit_price,currency,order_date\n"
INV_HEADER = "invoice_number,invoice_date,supplier_id,po_number,sku,quantity_invoiced,unit_price,currency\n"
GRN_HEADER = "grn_number,received_date,supplier_id,po_number,sku,quantity_received\n"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_purchase_orders_load_with_exact_decimals(tmp_path):
    path = write(tmp_path, "po.csv", PO_HEADER + "PO1,1,S1,Supplier,SKU,Desc,10,0.10,usd,2026-08-01\n")
    result = load_purchase_orders(path)
    assert result.issues == []
    record = result.records[0]
    assert record.unit_price == Decimal("0.10")
    assert record.currency == "USD"
    assert record.source_row == 2


def test_missing_required_value_rejects_row_and_reports_line(tmp_path):
    path = write(tmp_path, "po.csv", PO_HEADER
                 + "PO1,1,S1,Supplier,SKU-A,Desc,10,1.00,USD,2026-08-01\n"
                 + "PO1,2,S1,Supplier,SKU-B,Desc,10,,USD,2026-08-01\n")
    result = load_purchase_orders(path)
    assert len(result.records) == 1
    assert result.rows_rejected == 1
    [issue] = result.issues
    assert (issue.file, issue.row, issue.field) == ("purchase_orders", 3, "unit_price")
    assert "Missing required value" in issue.message
    assert ("PO1", "SKU-B") in result.rejected_keys


def test_invalid_number_date_and_currency_are_each_reported(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER
                 + "I1,2026-02-30,S1,PO1,SKU,abc,1.00,DOLLARS\n")
    result = load_invoices(path)
    assert result.records == []
    fields = sorted(i.field for i in result.issues)
    assert fields == ["currency", "invoice_date", "quantity_invoiced"]


def test_negative_and_zero_quantities_rejected(tmp_path):
    path = write(tmp_path, "inv.csv", INV_HEADER
                 + "I1,2026-08-01,S1,PO1,SKU,-5,1.00,USD\n"
                 + "I2,2026-08-01,S1,PO1,SKU,0,1.00,USD\n")
    result = load_invoices(path)
    assert result.records == []
    assert [i.row for i in result.issues] == [2, 3]


def test_zero_received_quantity_is_allowed(tmp_path):
    path = write(tmp_path, "grn.csv", GRN_HEADER + "G1,2026-08-01,S1,PO1,SKU,0\n")
    assert load_goods_received(path).records[0].quantity_received == Decimal("0")


def test_missing_column_is_a_file_level_issue(tmp_path):
    path = write(tmp_path, "grn.csv", "grn_number,received_date,po_number,sku,quantity_received\n"
                 "G1,2026-08-01,PO1,SKU,5\n")
    result = load_goods_received(path)
    assert result.records == []
    [issue] = result.issues
    assert issue.row is None and issue.field == "supplier_id"


def test_missing_file_and_empty_file(tmp_path):
    assert "not found" in load_invoices(tmp_path / "nope.csv").issues[0].message
    empty = write(tmp_path, "empty.csv", "")
    assert "empty" in load_invoices(empty).issues[0].message


def test_blank_lines_skipped_without_shifting_line_numbers(tmp_path):
    path = write(tmp_path, "grn.csv", GRN_HEADER + "G1,2026-08-01,S1,PO1,SKU,5\n\nG2,bad-date,S1,PO1,SKU,5\n")
    result = load_goods_received(path)
    assert result.rows_read == 2
    assert result.issues[0].row == 4


def test_headers_and_values_are_trimmed(tmp_path):
    path = write(tmp_path, "grn.csv", " GRN_Number , received_date,supplier_id,po_number,sku,quantity_received\n"
                 " G1 ,2026-08-01, S1 ,PO1,SKU, 5 \n")
    record = load_goods_received(path).records[0]
    assert (record.grn_number, record.supplier_id, record.quantity_received) == ("G1", "S1", Decimal("5"))


def test_duplicate_po_line_and_duplicate_sku_rejected(tmp_path):
    path = write(tmp_path, "po.csv", PO_HEADER
                 + "PO1,1,S1,Sup,SKU-A,,10,1.00,USD,2026-08-01\n"
                 + "PO1,1,S1,Sup,SKU-B,,10,1.00,USD,2026-08-01\n"   # duplicate line number
                 + "PO1,2,S1,Sup,SKU-A,,10,1.00,USD,2026-08-01\n"   # duplicate SKU on same PO
                 + "PO1,3,S2,Sup,SKU-C,,10,1.00,USD,2026-08-01\n")  # conflicting supplier
    result = load_purchase_orders(path)
    assert [r.sku for r in result.records] == ["SKU-A"]
    assert [i.field for i in result.issues] == ["line_number", "sku", "supplier_id"]


def test_duplicate_grn_line_rejected_but_duplicate_invoices_kept(tmp_path):
    grn = write(tmp_path, "grn.csv", GRN_HEADER + "G1,2026-08-01,S1,PO1,SKU,5\nG1,2026-08-01,S1,PO1,SKU,5\n")
    assert len(load_goods_received(grn).records) == 1
    inv = write(tmp_path, "inv.csv", INV_HEADER + "I1,2026-08-01,S1,PO1,SKU,5,1.00,USD\n" * 2)
    loaded = load_invoices(inv)
    assert len(loaded.records) == 2 and loaded.issues == []
