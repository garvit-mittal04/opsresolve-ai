"""The opt-in investigations.csv output of the CLI (Stage D3).

Expected rows are derived from the hand-written fixture expectations in tests/fixtures/investigations/ and from
the small cases below; never pasted from output.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from opsresolve import investigation, reporting
from opsresolve.cli import EXIT_INVESTIGATIONS_UNAVAILABLE, INVESTIGATIONS_UNAVAILABLE, STALE_NOT_REMOVED, main
from opsresolve.pipeline import investigate_files
from opsresolve.reporting import INVESTIGATION_COLUMNS, neutralize_cell

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "investigations"
SAMPLE = REPO / "data" / "sample"
DEFAULT_FILES = ("exception_report.md", "exceptions.csv", "validation_issues.csv")


def run_cli(capsys, *args):
    code = main([str(a) for a in args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def without_timestamp(path: Path) -> str:
    return re.sub(r"^Generated: .*$", "", path.read_text(encoding="utf-8"), flags=re.M)


def write_inputs(folder: Path, po: str, inv: str, grn: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "purchase_orders.csv").write_text(
        "po_number,line_number,supplier_id,supplier_name,sku,quantity_ordered,unit_price,currency,order_date\n" + po,
        encoding="utf-8")
    (folder / "invoices.csv").write_text(
        "invoice_number,invoice_date,supplier_id,po_number,sku,quantity_invoiced,unit_price,currency\n" + inv,
        encoding="utf-8")
    (folder / "goods_received.csv").write_text(
        "grn_number,received_date,supplier_id,po_number,sku,quantity_received\n" + grn, encoding="utf-8")
    return folder


# ---- off by default ------------------------------------------------------------------------------------------------

def test_without_the_option_nothing_changes(tmp_path, capsys, monkeypatch):
    plain, flagged = tmp_path / "plain", tmp_path / "flagged"
    code_flagged, out_flagged, _ = run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", flagged,
                                           "--with-investigations")
    monkeypatch.setattr(investigation, "investigate_all", lambda *a, **k: pytest.fail("investigations computed"))
    code, out, err = run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", plain)
    assert (code, err) == (0, "") and code_flagged == 0
    assert sorted(p.name for p in plain.iterdir()) == sorted(DEFAULT_FILES)
    assert "investigation" not in out
    for name in ("exceptions.csv", "validation_issues.csv"):  # byte-identical with or without the option
        assert (plain / name).read_bytes() == (flagged / name).read_bytes()
    assert without_timestamp(plain / "exception_report.md") == without_timestamp(flagged / "exception_report.md")
    assert out_flagged.replace(str(flagged), "") == out.replace(str(plain), "") + (
        f"wrote investigations_csv: {flagged / 'investigations.csv'}\n").replace(str(flagged), "")


def test_the_default_csv_files_keep_their_existing_bytes_on_the_sample(tmp_path, capsys):
    run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", tmp_path, "--with-investigations")
    result = investigate_files(SAMPLE / "purchase_orders.csv", SAMPLE / "invoices.csv", SAMPLE / "goods_received.csv")
    reference = reporting.write_reports(result, tmp_path / "reference")
    assert (tmp_path / "exceptions.csv").read_bytes() == reference["exceptions_csv"].read_bytes()
    assert (tmp_path / "validation_issues.csv").read_bytes() == reference["validation_csv"].read_bytes()


# ---- contents ------------------------------------------------------------------------------------------------------

def test_one_row_per_check_matching_the_hand_written_fixture(tmp_path, capsys):
    code, out, err = run_cli(capsys, "--data-dir", FIXTURES, "--output-dir", tmp_path, "--with-investigations")
    assert code == 0 and err == "" and "wrote investigations_csv" in out
    found = rows(tmp_path / "investigations.csv")
    assert list(found[0]) == INVESTIGATION_COLUMNS
    expected = {(r["exception_type"], r["po_number"], r["sku"], r["check"]): r["status"]
                for r in csv.DictReader((FIXTURES / "expected_investigations.csv").open(encoding="utf-8"))}
    assert len(found) == len(expected) == 38  # every fixture exception has catalogue checks
    produced = {}
    for row in found:
        key = (row["exception_type"], row["po_number"], row["sku"], row["check"])
        produced.setdefault(key, set()).add(row["status"])
    # The expectations key some checks by document as well; here every key's statuses must agree with them.
    for key, status in expected.items():
        assert status in produced[key], key
    labels = {row["status"]: row["status_label"] for row in found}
    assert labels == {"consistent": "Consistent with the data — not confirmed",
                      "contradicted": "Contradicted by the data", "not_checkable": "Can't be checked"}


def test_rows_follow_the_exception_and_check_order_and_cite_resolvable_records(tmp_path, capsys):
    run_cli(capsys, "--data-dir", FIXTURES, "--output-dir", tmp_path, "--with-investigations")
    result = investigate_files(*(FIXTURES / f"{n}.csv" for n in ("purchase_orders", "invoices", "goods_received")),
                               uom_conversions_path=FIXTURES / "uom_conversions.csv")
    strict = investigation.investigate_all(result, strict=True)
    expected = [(e.exception_id, c.id) for e in result.exceptions for c in strict[e.exception_id].checks]
    found = rows(tmp_path / "investigations.csv")
    assert [(r["exception_id"], r["check_id"]) for r in found] == expected
    for row in found:
        for ref in re.findall(r"\[([^\]]+)\]", row["observations"]):  # every cited R-number is listed in records
            for r in ref.split(", "):
                assert re.search(rf"(^| \| ){r}=(purchase_orders|invoices|goods_received|uom_conversions):\d+:",
                                 row["records"]), (row["exception_id"], r)


def test_exceptions_without_checks_get_one_row_and_the_file_is_deterministic(tmp_path, capsys):
    run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", tmp_path / "a", "--with-investigations")
    run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", tmp_path / "b", "--with-investigations")
    first = (tmp_path / "a" / "investigations.csv").read_bytes()
    assert first == (tmp_path / "b" / "investigations.csv").read_bytes()
    found = rows(tmp_path / "a" / "investigations.csv")
    duplicates = [r for r in found if r["exception_type"] == "POSSIBLE_DUPLICATE_INVOICE"]
    assert duplicates and all(r["check_id"] == "" and r["status"] == "" and
                              r["status_label"] == reporting.NO_CHECKS_LABEL for r in duplicates)
    assert len({r["exception_id"] for r in found}) == 18  # every sample exception appears


def test_wording_never_claims_a_confirmed_cause(tmp_path, capsys):
    for folder in (SAMPLE, FIXTURES):
        out = tmp_path / folder.name
        run_cli(capsys, "--data-dir", folder, "--output-dir", out, "--with-investigations")
        text = (out / "investigations.csv").read_text(encoding="utf-8")
        assert not re.search(r"(?i)\b(?:caused by|root cause|definitely|proves?|(?<!not )confirmed)\b", text)


# ---- spreadsheet formula injection ---------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["=HYPERLINK(\"http://x.invalid\",\"y\")", "+SUM(A1)", "-2+3", "@cmd",
                                   "\tTAB", "\rCR", "=1", "-", "+", "@"])
def test_formula_triggers_are_neutralised(value):
    assert neutralize_cell(value) == "'" + value


@pytest.mark.parametrize("value", ["-50.00", "+3", "12", ".5", "0", "", "P-1001", "R1=invoices:2:INV-1", "Can't"])
def test_numbers_and_ordinary_text_are_unchanged(value):
    assert neutralize_cell(value) == value


@pytest.mark.parametrize("value", [None, 3, 2.5])
def test_non_text_values_are_unchanged(value):
    assert neutralize_cell(value) == value


def test_the_rule_is_the_same_as_the_web_app_downloads():
    webapp = pytest.importorskip("opsresolve.webapp")
    values = ["=1", "+x", "-x", "@x", "\tx", "\rx", "-50.00", "+3", ".5", "text", "", None, 7, "=", "'=x"]
    assert [neutralize_cell(v) for v in values] == [webapp.neutralize_cell(v) for v in values]


def test_dangerous_identifiers_are_neutralised_in_investigations_but_not_in_the_existing_csv(tmp_path, capsys):
    danger_po = '=HYPERLINK("http://x.invalid","click")'
    danger_sku = "+SUM(A1:A9)"
    danger_ref = "@cmd"
    po = '"' + danger_po.replace('"', '""') + '"'  # CSV quoting: inner quotes doubled
    folder = write_inputs(
        tmp_path / "in",
        f"{po},1,S1,Supplier One,{danger_sku},10,5.00,USD,2026-08-01\n"
        "P-2,1,S1,Supplier One,SKU-2,10,5.00,USD,2026-08-01\n",
        f"I1,2026-08-10,S1,{po},{danger_sku},10,5.00,USD\n"
        f"{danger_ref},2026-08-11,S1,P-9,SKU-2,4,5.00,USD\n",
        "G2,2026-08-08,S1,P-2,SKU-2,10\n")
    code, _, err = run_cli(capsys, "--data-dir", folder, "--output-dir", tmp_path / "out", "--with-investigations")
    assert code == 0 and err == ""
    found = rows(tmp_path / "out" / "investigations.csv")
    po_cells = {r["po_number"] for r in found}
    assert "'" + danger_po in po_cells and danger_po not in po_cells
    assert {"'" + danger_sku} <= {r["sku"] for r in found} and danger_sku not in {r["sku"] for r in found}
    cells = [value for r in found for value in r.values()]
    assert not any(value.startswith(("=", "+", "-", "@", "\t", "\r")) and not re.fullmatch(r"[+-]?[\d.]+", value)
                   for value in cells)
    assert any(danger_ref in value for value in cells)  # the invoice number is cited inside the records cell
    # Intentional difference: the existing CLI CSV files are written exactly as before, without neutralising.
    exceptions = rows(tmp_path / "out" / "exceptions.csv")
    assert danger_po in {r["po_number"] for r in exceptions}


# ---- failures ------------------------------------------------------------------------------------------------------

def test_a_failed_check_shows_its_status_and_fixed_label_only(tmp_path, capsys, monkeypatch):
    def broken(subject, index):
        raise RuntimeError("secret internal detail SUP-A01")

    monkeypatch.setitem(investigation._CHECKS, "unit_mix_up", broken)
    code, _, err = run_cli(capsys, "--data-dir", FIXTURES, "--output-dir", tmp_path, "--with-investigations")
    assert code == 0
    found = rows(tmp_path / "investigations.csv")
    failed = [r for r in found if r["check"] == "unit_mix_up"]
    assert failed and all((r["status"], r["status_label"], r["observations"], r["needed_to_decide"],
                           r["how_to_confirm"], r["records"]) ==
                          ("failed", "Check failed — nothing concluded", "", "", "", "") for r in failed)
    assert any(r["status"] == "consistent" for r in found)  # the other checks still ran
    text = (tmp_path / "investigations.csv").read_text(encoding="utf-8") + err
    assert "secret" not in text and "RuntimeError" not in text and "Traceback" not in text
    assert err.startswith(f"{len(failed)} investigation check(s) could not be completed")


def test_a_failure_before_the_checks_marks_only_that_exception(tmp_path, capsys, monkeypatch):
    real = investigation._subject

    def broken(exception, index):
        if exception.po_number == "P-1003":
            raise KeyError("secret")
        return real(exception, index)

    monkeypatch.setattr(investigation, "_subject", broken)
    code, _, err = run_cli(capsys, "--data-dir", FIXTURES, "--output-dir", tmp_path, "--with-investigations")
    assert code == 0 and "KeyError" not in err and "secret" not in err
    not_run = [r for r in rows(tmp_path / "investigations.csv") if r["po_number"] == "P-1003"]
    assert len(not_run) == 2 and all(
        (r["status"], r["status_label"], r["check_id"]) == ("failed", reporting.NOT_RUN_LABEL, "") for r in not_run)


def test_an_indexing_failure_exits_3_and_leaves_the_other_reports_complete(tmp_path, capsys, monkeypatch):
    stale = tmp_path / "investigations.csv"
    stale.write_text("from an earlier run\n", encoding="utf-8")

    def broken(self, result):
        raise ValueError("secret internal detail")

    monkeypatch.setattr(investigation._Index, "__init__", broken)
    code, out, err = run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", tmp_path, "--with-investigations",
                             "--fail-on-exceptions")
    assert code == EXIT_INVESTIGATIONS_UNAVAILABLE == 3
    assert err.strip() == INVESTIGATIONS_UNAVAILABLE and "secret" not in out + err
    assert not stale.exists()  # never a previous run's file next to this run's reports
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(DEFAULT_FILES)
    reference = reporting.write_reports(
        investigate_files(SAMPLE / "purchase_orders.csv", SAMPLE / "invoices.csv", SAMPLE / "goods_received.csv"),
        tmp_path / "reference")
    assert (tmp_path / "exceptions.csv").read_bytes() == reference["exceptions_csv"].read_bytes()



def test_an_indexing_failure_leaves_a_directory_of_that_name_and_symlink_targets_alone(tmp_path, capsys,
                                                                                         monkeypatch):
    def broken(self, result):
        raise ValueError("secret internal detail")

    monkeypatch.setattr(investigation._Index, "__init__", broken)
    out = tmp_path / "out"
    folder = out / "investigations.csv"  # a directory, not a file this program wrote
    folder.mkdir(parents=True)
    (folder / "keep.txt").write_text("unrelated\n", encoding="utf-8")
    code, _, err = run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", out, "--with-investigations")
    assert code == EXIT_INVESTIGATIONS_UNAVAILABLE and err.strip() == INVESTIGATIONS_UNAVAILABLE
    assert "secret" not in err and "Traceback" not in err and "IsADirectoryError" not in err
    assert (folder / "keep.txt").read_text(encoding="utf-8") == "unrelated\n"
    assert {p.name for p in out.iterdir()} == {*DEFAULT_FILES, "investigations.csv"}

    target = tmp_path / "elsewhere.csv"
    target.write_text("someone else's file\n", encoding="utf-8")
    folder.joinpath("keep.txt").unlink()
    folder.rmdir()
    folder.symlink_to(target)  # a symlink: only the link is removed
    code, _, err = run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", out, "--with-investigations")
    assert code == 3 and err.strip() == INVESTIGATIONS_UNAVAILABLE
    assert not folder.is_symlink() and not folder.exists()
    assert target.read_text(encoding="utf-8") == "someone else's file\n"


def test_a_stale_file_that_cannot_be_removed_gives_only_a_fixed_warning(tmp_path, capsys, monkeypatch):
    def broken(self, result):
        raise ValueError("secret internal detail")

    real_unlink = Path.unlink

    def refuse(self, *args, **kwargs):
        if self.name == "investigations.csv":
            raise PermissionError(13, "secret permission detail", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(investigation._Index, "__init__", broken)
    monkeypatch.setattr(Path, "unlink", refuse)
    stale = tmp_path / "investigations.csv"
    stale.write_text("from an earlier run\n", encoding="utf-8")
    code, out, err = run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", tmp_path, "--with-investigations",
                             "--fail-on-exceptions")
    assert code == EXIT_INVESTIGATIONS_UNAVAILABLE == 3  # 3 still takes precedence over 1
    assert err.splitlines() == [INVESTIGATIONS_UNAVAILABLE, STALE_NOT_REMOVED]
    for detail in ("secret", "PermissionError", "Errno", "Traceback", str(stale)):
        assert detail not in err and detail not in out
    assert stale.read_text(encoding="utf-8") == "from an earlier run\n"  # left in place, and the user is warned
    assert "investigations_csv" not in out
    for name in DEFAULT_FILES:  # the normal reports are still written
        assert (tmp_path / name).exists()

def test_fail_on_exceptions_is_unchanged_with_the_option(tmp_path, capsys):
    assert run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", tmp_path, "--with-investigations",
                   "--fail-on-exceptions")[0] == 1


def test_the_option_needs_no_ai_sdk_key_or_network(tmp_path, capsys, monkeypatch):
    import socket

    from opsresolve import ai

    def blocked(*args, **kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.delenv(ai.API_KEY_VARIABLE, raising=False)
    monkeypatch.setattr(ai, "explain_exception", lambda *a, **k: pytest.fail("AI used"))
    assert run_cli(capsys, "--data-dir", SAMPLE, "--output-dir", tmp_path, "--with-investigations")[0] == 0
    assert (tmp_path / "investigations.csv").exists()
