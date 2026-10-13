import io
import os
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

import opsresolve.webapp as webapp
from opsresolve.config import MatchConfig
from opsresolve.ingestion import REQUIRED_COLUMNS
from opsresolve.models import ExceptionType, Severity
from opsresolve.pipeline import investigate_files
from opsresolve.webapp import (
    INPUTS,
    MissingInputError,
    Upload,
    display_names,
    exceptions_csv,
    filter_exceptions,
    fingerprint,
    neutralize_cell,
    run_reconciliation,
    sample_uploads,
    summarize,
    summary_markdown,
    template_csv,
    to_safe_csv,
    validation_csv,
)

REPO = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO / "data" / "evaluation"

PO_CSV = ("po_number,line_number,supplier_id,supplier_name,sku,quantity_ordered,unit_price,currency,order_date\n"
          "P1,1,S1,Supplier One,SKU-1,10,5.00,USD,2026-08-01\n")
INV_CSV = ("invoice_number,invoice_date,supplier_id,po_number,sku,quantity_invoiced,unit_price,currency\n"
           "I1,2026-08-10,S1,P1,SKU-1,10,5.00,USD\n")
GRN_CSV = ("grn_number,received_date,supplier_id,po_number,sku,quantity_received\n"
           "G1,2026-08-08,S1,P1,SKU-1,10\n")


def uploads(po=PO_CSV, inv=INV_CSV, grn=GRN_CSV, uom=None, names=None):
    names = names or {}
    def up(key, text):
        return None if text is None else Upload(names.get(key, f"{key}.csv"), text.encode("utf-8"))
    return {"purchase_orders": up("purchase_orders", po), "invoices": up("invoices", inv),
            "goods_received": up("goods_received", grn), "uom_conversions": up("uom_conversions", uom)}


def from_dir(directory: Path, with_uom: bool = False):
    result = {spec.key: Upload(spec.internal_name, (directory / spec.internal_name).read_bytes())
              for spec in INPUTS if spec.required}
    result["uom_conversions"] = (Upload("uom_conversions.csv", (directory / "uom_conversions.csv").read_bytes())
                                 if with_uom else None)
    return result


# ---- input specs --------------------------------------------------------------

def test_input_specs_use_engine_schemas():
    assert [s.key for s in INPUTS] == ["purchase_orders", "invoices", "goods_received", "uom_conversions"]
    assert [s.required for s in INPUTS] == [True, True, True, False]
    for spec in INPUTS:
        assert spec.required_columns == REQUIRED_COLUMNS[spec.key]


def test_templates_have_all_columns_and_bundled_examples():
    for spec in INPUTS:
        frame = pd.read_csv(io.BytesIO(template_csv(spec.key)), dtype=str, keep_default_na=False)
        assert list(frame.columns) == list(spec.required_columns) + list(spec.optional_columns)
        assert 1 <= len(frame) <= 3


# ---- valid uploads reach the existing pipeline ---------------------------------

def test_sample_uploads_give_the_same_result_as_the_cli_pipeline():
    via_app = run_reconciliation(sample_uploads(), MatchConfig())
    sample = REPO / "data" / "sample"
    direct = investigate_files(sample / "purchase_orders.csv", sample / "invoices.csv",
                               sample / "goods_received.csv", MatchConfig())
    assert via_app.exceptions == direct.exceptions
    assert via_app.dataset.issues == direct.dataset.issues
    summary = summarize(via_app, display_names(sample_uploads()))
    assert (summary.validation_issue_count, summary.exception_count) == (3, 18)
    assert summary.by_severity == {Severity.HIGH: 8, Severity.MEDIUM: 7, Severity.LOW: 3}


def test_unit_conversions_follow_the_existing_engine():
    with_uom = run_reconciliation(from_dir(EVAL_DIR, with_uom=True), MatchConfig())
    direct = investigate_files(EVAL_DIR / "purchase_orders.csv", EVAL_DIR / "invoices.csv",
                               EVAL_DIR / "goods_received.csv", MatchConfig(),
                               uom_conversions_path=EVAL_DIR / "uom_conversions.csv")
    assert with_uom.exceptions == direct.exceptions
    assert with_uom.units.converted_lines == direct.units.converted_lines
    # Without the optional conversion file the same documents produce unit-of-measure mismatches.
    without = run_reconciliation(from_dir(EVAL_DIR, with_uom=False), MatchConfig())
    unit_lines = {e.po_number for e in without.exceptions if e.exception_type is ExceptionType.UNIT_OF_MEASURE_MISMATCH}
    assert {"4600003201", "4600003301", "4600003401", "4600003501"} <= unit_lines


def test_settings_are_passed_to_the_engine():
    overpriced = INV_CSV.replace("10,5.00,USD", "10,5.20,USD")
    strict = run_reconciliation(uploads(inv=overpriced), MatchConfig())
    tolerant = run_reconciliation(uploads(inv=overpriced), MatchConfig(price_tolerance_pct=Decimal("5")))
    assert [e.exception_type for e in strict.exceptions] == [ExceptionType.PRICE_MISMATCH]
    assert tolerant.exceptions == []


# ---- zero exceptions -----------------------------------------------------------

def test_clean_run_has_zero_exceptions_and_a_success_status():
    result = run_reconciliation(uploads(), MatchConfig())
    summary = summarize(result)
    assert result.exceptions == [] and result.dataset.issues == []
    assert summary.status_level == "success"
    assert "not an approval" in summary.status_message
    assert summary.by_type == {} and set(summary.by_severity.values()) == {0}
    frame = pd.read_csv(io.BytesIO(exceptions_csv(result)))
    assert len(frame) == 0 and "exception_id" in frame.columns


# ---- missing and malformed uploads ----------------------------------------------

def test_missing_required_files_raise_before_running(monkeypatch):
    monkeypatch.setattr(webapp, "investigate_files", lambda *a, **k: pytest.fail("pipeline must not run"))
    with pytest.raises(MissingInputError) as err:
        run_reconciliation(uploads(inv=None, grn=None), MatchConfig())
    assert err.value.missing == ["Supplier invoices", "Goods receipts"]


def test_optional_conversion_file_can_be_omitted():
    assert run_reconciliation(uploads(uom=None), MatchConfig()).dataset.uom_conversions is None


@pytest.mark.parametrize("bad, expected_message", [
    ("", "File is empty"),
    ("\xff\xfe binary", "could not be parsed"),
    ("po_number,sku\nP1,SKU-1\n", "Required column is missing"),
])
def test_unreadable_files_become_validation_issues_with_error_status(bad, expected_message):
    data = bad.encode("latin-1")
    files = uploads()
    files["purchase_orders"] = Upload("bad.csv", data)
    result = run_reconciliation(files, MatchConfig())
    assert any(expected_message in i.message for i in result.dataset.issues)
    summary = summarize(result, display_names(files))
    assert summary.status_level == "error"
    assert "Purchase orders" in summary.status_message


def test_malformed_values_are_reported_not_dropped_silently():
    bad = PO_CSV + "P2,1,S1,Supplier One,SKU-2,ten,5.00,USD,2026-13-01\n"
    result = run_reconciliation(uploads(po=bad), MatchConfig())
    fields = {(i.file, i.row, i.field) for i in result.dataset.issues}
    assert fields == {("purchase_orders", 3, "quantity_ordered"), ("purchase_orders", 3, "order_date")}
    summary = summarize(result)
    assert summary.files[0].rejected == 1 and summary.status_level == "warning"


def test_invalid_conversion_data_is_reported():
    uom = "sku,uom,base_uom,units_per_uom\nSKU-1,BOX,EA,12\nSKU-1,BOX,EA,10\n"
    result = run_reconciliation(uploads(uom=uom), MatchConfig())
    assert len(result.dataset.issues) == 2
    assert all(i.file == "uom_conversions" and "ambiguous" in i.message for i in result.dataset.issues)


def test_validation_issues_and_exceptions_stay_separate():
    result = run_reconciliation(sample_uploads(), MatchConfig())
    issues = pd.read_csv(io.BytesIO(validation_csv(result)))
    findings = pd.read_csv(io.BytesIO(exceptions_csv(result)))
    assert list(issues.columns) == ["file", "row", "field", "message", "value"]
    assert len(issues) == 3 and len(findings) == 18
    assert "exception_id" not in issues.columns and "message" not in findings.columns


# ---- downloads are for the current run -------------------------------------------

def test_downloads_reflect_each_run():
    first = run_reconciliation(sample_uploads(), MatchConfig())
    second = run_reconciliation(uploads(), MatchConfig())
    assert len(pd.read_csv(io.BytesIO(exceptions_csv(first)))) == 18
    assert len(pd.read_csv(io.BytesIO(exceptions_csv(second)))) == 0
    assert "Exception summary (18)" in summary_markdown(first).decode()
    assert "No exceptions detected." in summary_markdown(second).decode()


def test_fingerprint_changes_with_any_input_or_setting():
    base = fingerprint(uploads(), MatchConfig())
    assert fingerprint(uploads(), MatchConfig()) == base
    assert fingerprint(uploads(names={"invoices": "renamed.csv"}), MatchConfig()) == base  # name is display only
    changed = [
        fingerprint(uploads(inv=INV_CSV + "I2,2026-08-11,S1,P1,SKU-1,1,5.00,USD\n"), MatchConfig()),
        fingerprint(uploads(uom="sku,uom,base_uom,units_per_uom\n"), MatchConfig()),
        fingerprint(uploads(grn=None), MatchConfig()),
        fingerprint(uploads(), MatchConfig(quantity_tolerance=Decimal("1"))),
    ]
    assert base not in changed and len(set(changed)) == len(changed)


# ---- filesystem safety -------------------------------------------------------

def test_upload_names_never_become_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def spy(po, inv, grn, config, uom_conversions_path=None):
        seen.update(po=Path(po), inv=Path(inv), grn=Path(grn), uom=Path(uom_conversions_path))
        seen["files"] = sorted(p.name for p in Path(po).parent.iterdir())
        return investigate_files(po, inv, grn, config, uom_conversions_path=uom_conversions_path)

    monkeypatch.setattr(webapp, "investigate_files", spy)
    hostile = {"purchase_orders": "../../app.py", "invoices": "/etc/passwd",
               "goods_received": "..\\..\\README.md", "uom_conversions": "~/.bashrc"}
    run_reconciliation(uploads(uom="sku,uom,base_uom,units_per_uom\n", names=hostile), MatchConfig())
    assert seen["files"] == ["goods_received.csv", "invoices.csv", "purchase_orders.csv", "uom_conversions.csv"]
    assert seen["po"].parent == seen["inv"].parent == seen["grn"].parent == seen["uom"].parent
    assert seen["po"].parent.name.startswith("opsresolve-")
    assert list(tmp_path.iterdir()) == []  # nothing written next to the working directory


def test_temporary_files_are_removed_after_success(monkeypatch):
    folders = []
    real = webapp.investigate_files
    monkeypatch.setattr(webapp, "investigate_files",
                        lambda po, *a, **k: folders.append(Path(po).parent) or real(po, *a, **k))
    run_reconciliation(uploads(), MatchConfig())
    assert folders and not folders[0].exists()


def test_temporary_files_are_removed_when_the_pipeline_fails(monkeypatch):
    folders = []

    def boom(po, *args, **kwargs):
        folders.append(Path(po).parent)
        assert Path(po).exists()
        raise RuntimeError("engine failure")

    monkeypatch.setattr(webapp, "investigate_files", boom)
    with pytest.raises(RuntimeError):
        run_reconciliation(uploads(), MatchConfig())
    assert folders and not folders[0].exists()


def test_running_does_not_write_into_the_repository():
    before = {p for p in REPO.rglob("*") if ".git" not in p.parts and "__pycache__" not in p.parts}
    run_reconciliation(sample_uploads(), MatchConfig())
    after = {p for p in REPO.rglob("*") if ".git" not in p.parts and "__pycache__" not in p.parts}
    assert after == before


# ---- formula neutralisation (web downloads only) ---------------------------------

@pytest.mark.parametrize("value", ["=HYPERLINK(\"http://x\")", "+1+2", "-2+3", "@SUM(A1)", "\tcmd", "\r=1"])
def test_formula_triggers_are_neutralised(value):
    assert neutralize_cell(value) == "'" + value


@pytest.mark.parametrize("value", ["-50.00", "-5", "+12", "0.5", "-.5", "PO-10", "Supplier = Co", "", "abc"])
def test_numbers_and_ordinary_text_are_unchanged(value):
    assert neutralize_cell(value) == value


def test_non_text_values_are_unchanged():
    assert neutralize_cell(5) == 5 and neutralize_cell(None) is None


def test_user_controlled_fields_neutralised_in_exception_csv():
    po = PO_CSV.replace("S1,Supplier One,SKU-1", "=EVIL(),Supplier One,@SKU")
    inv = INV_CSV.replace("I1,2026-08-10,S1,P1,SKU-1,10,5.00", "+INV1,2026-08-10,-EVIL,P1,@SKU,10,4.50")
    grn = GRN_CSV.replace("S1,P1,SKU-1", "=EVIL(),P1,@SKU")
    result = run_reconciliation(uploads(po=po, inv=inv, grn=grn), MatchConfig())
    frame = pd.read_csv(io.BytesIO(exceptions_csv(result)), dtype=str, keep_default_na=False)
    assert len(frame) >= 1
    for column in frame.columns:
        for cell in frame[column]:
            if cell and cell[0] in "=+@":
                pytest.fail(f"unneutralised cell in {column}: {cell!r}")
            if cell.startswith("-"):
                assert webapp._PLAIN_NUMBER.fullmatch(cell), f"{column}: {cell!r}"
    assert "'@SKU" in set(frame["sku"])
    # Legitimate negative amounts are untouched...
    assert all(webapp._PLAIN_NUMBER.fullmatch(c) for c in frame["amount_impact"])
    # ...and the underlying results are not modified.
    assert any(e.sku == "@SKU" for e in result.exceptions)


def test_user_controlled_fields_neutralised_in_validation_csv():
    po = PO_CSV + "=CMD(),1,S1,Supplier One,SKU-2,-1,5.00,USD,2026-08-01\n"
    result = run_reconciliation(uploads(po=po), MatchConfig())
    frame = pd.read_csv(io.BytesIO(validation_csv(result)), dtype=str, keep_default_na=False)
    assert "-1" in set(frame["value"])  # a plain negative number stays a number
    assert all(not v.startswith(("=", "+", "@")) for v in frame["value"])


def test_cli_report_files_are_not_neutralised(tmp_path):
    from opsresolve.reporting import write_reports
    po = PO_CSV.replace("SKU-1", "@SKU")
    inv = INV_CSV.replace("SKU-1", "@SKU").replace("10,5.00", "10,4.50")
    result = run_reconciliation(uploads(po=po, inv=inv, grn=GRN_CSV.replace("SKU-1", "@SKU")), MatchConfig())
    paths = write_reports(result, tmp_path)
    assert "@SKU" in set(pd.read_csv(paths["exceptions_csv"], dtype=str)["sku"])


def test_to_safe_csv_does_not_modify_its_input():
    frame = pd.DataFrame({"a": ["=1", "-2"]})
    to_safe_csv(frame)
    assert list(frame["a"]) == ["=1", "-2"]


# ---- filtering ----------------------------------------------------------------

def test_filters_by_severity_type_and_text():
    exceptions = run_reconciliation(sample_uploads(), MatchConfig()).exceptions
    assert filter_exceptions(exceptions) == exceptions
    high = filter_exceptions(exceptions, severities={Severity.HIGH})
    assert len(high) == 8 and all(e.severity is Severity.HIGH for e in high)
    dups = filter_exceptions(exceptions, types={ExceptionType.POSSIBLE_DUPLICATE_INVOICE})
    assert len(dups) == 2
    assert [e.exception_id for e in filter_exceptions(exceptions, query="cib-07781")] == [
        e.exception_id for e in exceptions if "CIB-07781" in e.summary + "".join(ev.reference for ev in e.evidence)]
    assert filter_exceptions(exceptions, severities={Severity.LOW}, types={ExceptionType.PRICE_MISMATCH},
                             query="hyd-seal") and filter_exceptions(exceptions, query="no such text") == []


# ---- display safety -------------------------------------------------------------

def test_escape_markdown_neutralises_links_images_html_and_styling():
    from opsresolve.webapp import escape_markdown
    import re
    escaped = escape_markdown("![x](http://t/p.png) [a](b) **b** <img src=x> :red[x] $x$ `c`")
    # Every Markdown/HTML control character must be preceded by a backslash (shown literally).
    unescaped = re.findall(r"(?<!\\)[\[\]()*<>:$`!]", escaped)
    assert unescaped == []
    assert escaped.startswith(r"\!\[x\]\(http\:")
    assert escape_markdown(None) == "" and escape_markdown("") == ""
    assert escape_markdown("line1\nline2") == "line1 line2"
    assert escape_markdown("PO 4500010005") == "PO 4500010005"  # ordinary text stays readable


# ---- review fix 1: header-only files vs rejected rows ------------------------------

PO_HEADER = PO_CSV.splitlines()[0] + "\n"
INV_HEADER = INV_CSV.splitlines()[0] + "\n"
GRN_HEADER = GRN_CSV.splitlines()[0] + "\n"


def test_header_only_files_are_reported_as_empty_not_as_validation_errors():
    result = run_reconciliation(uploads(inv=INV_HEADER, grn=GRN_HEADER), MatchConfig())
    summary = summarize(result)
    assert result.dataset.issues == [] and summary.validation_issue_count == 0
    assert summary.status_level == "warning"
    assert "Supplier invoices, Goods receipts contained no data rows (header only)" in summary.status_message
    # The old message told the user to fix "the validation issues below" when there were none.
    assert "validation issues below" not in summary.status_message and "Fix" not in summary.status_message
    assert "no validation issues" in summary.status_message
    by_key = {f.key: f for f in summary.files}
    assert by_key["invoices"].no_data_rows and by_key["goods_received"].no_data_rows
    assert not by_key["purchase_orders"].no_data_rows


def test_all_rows_rejected_is_still_an_error_pointing_at_validation_issues():
    bad = INV_HEADER + "I1,not-a-date,S1,P1,SKU-1,10,5.00,USD\n"
    summary = summarize(run_reconciliation(uploads(inv=bad), MatchConfig()))
    assert summary.status_level == "error"
    assert "No valid rows were loaded from: Supplier invoices, because of the validation issues below" in (
        summary.status_message)
    assert "header only" not in summary.status_message
    assert {f.key: f.issues for f in summary.files}["invoices"] == 1


def test_missing_columns_are_an_error_not_an_empty_file():
    summary = summarize(run_reconciliation(uploads(grn="grn_number,sku\n"), MatchConfig()))
    assert summary.status_level == "error" and "Goods receipts" in summary.status_message
    assert "header only" not in summary.status_message


def test_rejected_and_empty_files_are_both_described():
    bad = INV_HEADER + "I1,not-a-date,S1,P1,SKU-1,10,5.00,USD\n"
    summary = summarize(run_reconciliation(uploads(inv=bad, grn=GRN_HEADER), MatchConfig()))
    assert summary.status_level == "error"
    assert "loaded from: Supplier invoices, because" in summary.status_message
    assert "Goods receipts contained no data rows" in summary.status_message


def test_header_only_conversion_file_is_noted_with_findings():
    overpriced = INV_CSV.replace("10,5.00,USD", "10,5.20,USD")
    summary = summarize(run_reconciliation(uploads(inv=overpriced, uom="sku,uom,base_uom,units_per_uom\n"),
                                           MatchConfig()))
    assert summary.status_message.startswith("Processing completed. 1 reconciliation finding(s) need review.")
    assert "Unit-of-measure conversions contained no data rows" in summary.status_message


# ---- review fix 2: settings at their displayed precision ----------------------------

def default_inputs(**overrides):
    defaults = MatchConfig()
    values = {spec.field: (int(getattr(defaults, spec.field)) if spec.integer else float(getattr(defaults, spec.field)))
              for spec in webapp.SETTINGS}
    values.update(overrides)
    decimals = {s.field: webapp.setting_decimals(s, getattr(defaults, s.field)) for s in webapp.SETTINGS}
    return values, decimals


def test_widget_float_drift_is_normalised_to_the_displayed_value():
    drifted = 0.0
    for _ in range(10):
        drifted += 0.01  # how a widget's +/- buttons can accumulate the step
    assert drifted != 0.1 and f"{drifted:.2f}" == "0.10"
    config = webapp.config_from_inputs(*default_inputs(price_tolerance_abs=drifted))
    assert config.price_tolerance_abs == Decimal("0.10") and str(config.price_tolerance_abs) == "0.10"


def test_tolerance_boundary_is_inclusive_after_normalisation():
    drifted = 0.0
    for _ in range(10):  # not sum(): since Python 3.12 sum() compensates and returns exactly 0.1
        drifted += 0.01
    assert drifted == 0.09999999999999999
    config = webapp.config_from_inputs(*default_inputs(price_tolerance_abs=drifted))
    at_boundary = run_reconciliation(uploads(inv=INV_CSV.replace("10,5.00,USD", "10,5.10,USD")), config)
    assert at_boundary.exceptions == []  # exactly 0.10 over the PO price: within an inclusive 0.10 tolerance
    beyond = run_reconciliation(uploads(inv=INV_CSV.replace("10,5.00,USD", "10,5.11,USD")), config)
    assert [e.exception_type for e in beyond.exceptions] == [ExceptionType.PRICE_MISMATCH]


def test_settings_keep_more_decimals_when_the_default_needs_them():
    env = MatchConfig.from_env({"OPSRESOLVE_PRICE_TOLERANCE_ABS": "0.125"})
    spec = next(s for s in webapp.SETTINGS if s.field == "price_tolerance_abs")
    assert webapp.setting_decimals(spec, env.price_tolerance_abs) == 3
    assert webapp.normalize_setting(float(env.price_tolerance_abs), 3) == Decimal("0.125")


def test_integer_and_invalid_settings():
    values, decimals = default_inputs(duplicate_window_days=45)
    assert webapp.config_from_inputs(values, decimals).duplicate_window_days == 45
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        webapp.config_from_inputs(*default_inputs(quantity_tolerance=-1.0))


def test_settings_cover_every_engine_setting():
    assert {s.field for s in webapp.SETTINGS} == set(MatchConfig.model_fields)
