"""End-to-end checks against the committed synthetic sample data.

The expected values below were derived by hand from the scenarios documented
in data/sample/README.md, not copied from program output.
"""

from collections import Counter
from decimal import Decimal

import pandas as pd
import pytest

from opsresolve.cli import main
from opsresolve.config import MatchConfig
from opsresolve.models import ExceptionType as T
from opsresolve.pipeline import investigate_files
from opsresolve.reporting import render_markdown, write_reports


@pytest.fixture
def result(sample_dir):
    return investigate_files(sample_dir / "purchase_orders.csv", sample_dir / "invoices.csv",
                             sample_dir / "goods_received.csv", MatchConfig())


def find(result, exc_type, po_number):
    matches = [e for e in result.exceptions if e.exception_type == exc_type and e.po_number == po_number]
    assert len(matches) == 1, f"expected one {exc_type} for {po_number}, got {matches}"
    return matches[0]


def test_sample_validation_issues(result):
    issues = {(i.file, i.row, i.field) for i in result.dataset.issues}
    assert issues == {
        ("purchase_orders", 16, "unit_price"),
        ("invoices", 15, "quantity_invoiced"),
        ("goods_received", 18, "received_date"),
    }


def test_sample_exception_counts(result):
    counts = Counter(e.exception_type for e in result.exceptions)
    assert counts == {
        T.QUANTITY_MISMATCH: 4,
        T.PRICE_MISMATCH: 2,
        T.CURRENCY_MISMATCH: 1,
        T.SUPPLIER_MISMATCH: 1,
        T.MISSING_PURCHASE_ORDER: 4,
        T.MISSING_GOODS_RECEIPT: 2,
        T.MISSING_INVOICE: 2,
        T.POSSIBLE_DUPLICATE_INVOICE: 2,
    }


@pytest.mark.parametrize("exc_type, po_number, amount", [
    (T.MISSING_GOODS_RECEIPT, "4500010005", "6720.00"),   # 60 x 112.00
    (T.MISSING_GOODS_RECEIPT, "4500010013", "150.00"),    # 5000 x 0.03 (GRN row rejected)
    (T.PRICE_MISMATCH, "4500010004", "387.50"),           # 250 x (19.95 - 18.40)
    (T.PRICE_MISMATCH, "4500010010", "-50.00"),           # 1000 x (0.80 - 0.85)
    (T.MISSING_INVOICE, "4500010006", "200.00"),          # 10000 x 0.02
    (T.MISSING_INVOICE, "4500010011", "230.00"),          # 200 x 1.15 (invoice row rejected)
    (T.SUPPLIER_MISMATCH, "4500010015", "180.00"),        # 2000 x 0.09
    (T.CURRENCY_MISMATCH, "4500010014", "0"),
])
def test_sample_amounts(result, exc_type, po_number, amount):
    assert find(result, exc_type, po_number).amount_impact == Decimal(amount)


def test_sample_quantity_mismatches(result):
    qty = {(e.po_number, e.quantity_difference, e.amount_impact)
           for e in result.exceptions if e.exception_type == T.QUANTITY_MISMATCH}
    assert qty == {
        ("4500010003", Decimal("20"), Decimal("135.00")),    # 400 invoiced vs 380 received @ 6.75
        ("4500010007", Decimal("300"), Decimal("2760.00")),  # duplicate invoice doubles billing
        ("4500010008", Decimal("80"), Decimal("1960.00")),   # duplicate invoice doubles billing
        ("4500010009", Decimal("15"), Decimal("219.00")),    # 165 received vs 150 ordered @ 14.60
    }


def test_sample_duplicates(result):
    dups = {(e.po_number, e.amount_impact) for e in result.exceptions
            if e.exception_type == T.POSSIBLE_DUPLICATE_INVOICE}
    assert dups == {("4500010007", Decimal("2760.00")), ("4500010008", Decimal("1960.00"))}


def test_sample_missing_pos(result):
    missing = {(e.po_number, e.sku, e.amount_impact) for e in result.exceptions
               if e.exception_type == T.MISSING_PURCHASE_ORDER}
    assert missing == {
        ("4500010099", "HYD-PUMP-01", Decimal("1690.00")),
        ("4500010012", "BRG-6001", Decimal("1475.00")),   # PO row rejected (missing price)
        ("4500010012", "BRG-6001", Decimal("0")),         # GRN against the same rejected PO row
        ("4500010010", "HYD-SEAL-X", Decimal("0")),       # SKU not on PO
    }


def test_clean_sample_lines_have_no_exceptions(result):
    flagged = {e.po_number for e in result.exceptions}
    for clean_po in ("4500010001", "4500010002", "4500010016"):
        assert clean_po not in flagged


def test_rejected_rows_annotate_related_exceptions(result):
    for exc_type, po_number in [(T.MISSING_INVOICE, "4500010011"), (T.MISSING_GOODS_RECEIPT, "4500010013")]:
        assert any("rejected during input validation" in n for n in find(result, exc_type, po_number).notes)


def test_rejected_row_notes_name_the_file_grammatically(result):
    notes = [n for e in result.exceptions for n in e.notes if "rejected during input validation" in n]
    assert len(notes) == 4  # PO 4500010012 (2 exceptions), 4500010011 and 4500010013
    assert all(n.startswith("A row of the ") for n in notes)
    assert ("A row of the invoices file for PO 4500010011 / SKU FST-ANCH-10 was rejected during input validation. "
            "This exception may be a consequence of that data error; review the validation issues first.") in notes


def test_markdown_report_contents(result):
    text = render_markdown(result)
    assert "# OpsResolve Exception Report" in text
    assert "## Validation issues (3)" in text
    assert "## Exception summary (18)" in text
    assert "Root causes are not asserted" in text
    for exc in result.exceptions:
        assert f"### {exc.exception_id}" in text


def test_written_csv_round_trips(result, tmp_path):
    paths = write_reports(result, tmp_path)
    frame = pd.read_csv(paths["exceptions_csv"], dtype=str)
    assert len(frame) == len(result.exceptions)
    assert list(frame["exception_id"]) == [e.exception_id for e in result.exceptions]
    assert len(pd.read_csv(paths["validation_csv"])) == 3


def test_cli_runs_on_sample_data(sample_dir, tmp_path, capsys):
    code = main(["--data-dir", str(sample_dir), "--output-dir", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "exception_report.md").exists()
    assert "exceptions: 18" in capsys.readouterr().out
    assert main(["--data-dir", str(sample_dir), "--output-dir", str(tmp_path), "--fail-on-exceptions"]) == 1


def test_cli_rejects_invalid_env_config(sample_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("OPSRESOLVE_PRICE_TOLERANCE_PCT", "-1")
    assert main(["--data-dir", str(sample_dir), "--output-dir", str(tmp_path)]) == 2


def test_sample_severity_split(result):
    from opsresolve.models import Severity
    assert Counter(e.severity for e in result.exceptions) == {
        Severity.HIGH: 8, Severity.MEDIUM: 7, Severity.LOW: 3}
