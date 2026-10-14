"""UI tests for app.py using Streamlit's AppTest (no browser, no sleeps, no screen sizes)."""

import re
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="install the app extra: pip install -e '.[app]'")
from streamlit.testing.v1 import AppTest  # noqa: E402

import opsresolve.webapp as webapp  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
APP = str(REPO / "app.py")

PO_CSV = ("po_number,line_number,supplier_id,supplier_name,sku,quantity_ordered,unit_price,currency,order_date\n"
          "P1,1,S1,Supplier One,SKU-1,10,5.00,USD,2026-08-01\n")
INV_CSV = ("invoice_number,invoice_date,supplier_id,po_number,sku,quantity_invoiced,unit_price,currency\n"
           "I1,2026-08-10,S1,P1,SKU-1,10,5.00,USD\n")
GRN_CSV = ("grn_number,received_date,supplier_id,po_number,sku,quantity_received\n"
           "G1,2026-08-08,S1,P1,SKU-1,10\n")


def start() -> AppTest:
    return AppTest.from_file(APP, default_timeout=30).run()


def upload(at: AppTest, po=PO_CSV, inv=INV_CSV, grn=GRN_CSV, uom=None, names=None) -> AppTest:
    names = names or {}
    for key, text in (("purchase_orders", po), ("invoices", inv), ("goods_received", grn), ("uom_conversions", uom)):
        if text is not None:
            at.file_uploader(key=f"upload_{key}").upload(names.get(key, f"{key}.csv"), text.encode("latin-1"),
                                                         "text/csv")
    return at.run()


def run(at: AppTest) -> AppTest:
    return at.button(key="run_button").click().run()


def use_sample(at: AppTest) -> AppTest:
    return at.radio(key="source").set_value("Use bundled sample data").run()


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def no_errors(at: AppTest) -> None:
    assert list(at.exception) == [], [e.value for e in at.exception]


# ---- configuration -------------------------------------------------------------

def test_streamlit_config_is_local_only_without_telemetry():
    text = (REPO / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert re.search(r'^address = "localhost"$', text, re.M)
    assert re.search(r"^gatherUsageStats = false$", text, re.M)
    assert re.search(r"^maxUploadSize = 20$", text, re.M)


# ---- initial state ---------------------------------------------------------------

def test_initial_page_waits_for_required_files():
    at = start()
    no_errors(at)
    assert at.title[0].value == "OpsResolve"
    assert at.button(key="run_button").disabled
    assert "Purchase orders, Supplier invoices, Goods receipts" in " ".join(c.value for c in at.caption)
    assert not at.metric and not at.tabs
    # One template per input file, plus the example import profile.
    assert [d.key for d in at.download_button] == [
        "template_purchase_orders", "template_invoices", "template_goods_received", "template_uom_conversions",
        "example_profile"]


def test_optional_conversion_file_is_not_required():
    at = upload(start())  # three required files, no conversion file
    assert not at.button(key="run_button").disabled


# ---- running ---------------------------------------------------------------------

def test_uploaded_files_reach_the_pipeline_and_zero_exceptions_render():
    at = run(upload(start()))
    no_errors(at)
    assert metrics(at) == {"Rows read": "3", "Validation issues": "0", "Exceptions": "0",
                           "High severity": "0", "Medium severity": "0", "Low severity": "0"}
    assert "not an approval" in at.success[0].value
    assert [t.label for t in at.tabs] == ["Exceptions (0)", "Validation issues (0)", "Downloads"]
    assert any("No exceptions were detected" in i.value for i in at.info)


def test_sample_run_shows_findings_issues_and_duplicate_caution():
    at = run(use_sample(start()))
    no_errors(at)
    assert metrics(at) == {"Rows read": "62", "Validation issues": "3", "Exceptions": "18",
                           "High severity": "8", "Medium severity": "7", "Low severity": "3"}
    assert [t.label for t in at.tabs] == ["Exceptions (18)", "Validation issues (3)", "Downloads"]
    assert any("not confirmed duplicates" in w.value for w in at.warning)
    assert len(at.selectbox(key="detail_id").options) == 18


def test_validation_issues_are_shown_separately_with_upload_names():
    at = run(use_sample(start()))
    tables = [d.value for d in at.dataframe]
    validation = next(t for t in tables if list(t.columns) == ["File", "Line", "Field", "Value", "Problem"])
    assert len(validation) == 3
    assert set(validation["File"]) == {"data/sample/purchase_orders.csv", "data/sample/invoices.csv",
                                       "data/sample/goods_received.csv"}
    findings = next(t for t in tables if "Summary" in t.columns and "ID" in t.columns)
    assert len(findings) == 18 and "Problem" not in findings.columns


def test_unit_conversions_are_used_when_uploaded():
    po = ("po_number,line_number,supplier_id,supplier_name,sku,quantity_ordered,unit_price,currency,order_date,"
          "unit_of_measure\nP1,1,S1,Supplier One,SKU-1,10,24.00,USD,2026-08-01,BOX\n")
    inv = ("invoice_number,invoice_date,supplier_id,po_number,sku,quantity_invoiced,unit_price,currency,"
           "unit_of_measure\nI1,2026-08-10,S1,P1,SKU-1,120,2.00,USD,EA\n")
    grn = ("grn_number,received_date,supplier_id,po_number,sku,quantity_received,unit_of_measure\n"
           "G1,2026-08-08,S1,P1,SKU-1,10,BOX\n")
    without = run(upload(start(), po=po, inv=inv, grn=grn))
    assert metrics(without)["Exceptions"] == "1"
    assert without.selectbox(key="detail_id").options == ["EXC-0001 · UNIT_OF_MEASURE_MISMATCH"]
    with_uom = run(upload(start(), po=po, inv=inv, grn=grn, uom="sku,uom,base_uom,units_per_uom\nSKU-1,BOX,EA,12\n"))
    no_errors(with_uom)
    assert metrics(with_uom)["Exceptions"] == "0" and metrics(with_uom)["Rows read"] == "4"


def test_detail_panel_keeps_engine_notes():
    at = run(use_sample(start()))
    options = at.selectbox(key="detail_id").options
    qty = next(o for o in options if "QUANTITY_MISMATCH" in o)
    at.selectbox(key="detail_id").set_value(qty).run()
    text = " ".join(m.value for m in at.markdown)
    assert "Notes from the engine" in text and "Recommended next step" in text


# ---- bad input -------------------------------------------------------------------

def test_empty_and_unreadable_uploads_give_actionable_messages():
    at = run(upload(start(), po="", inv="\xff\xfe junk"))
    no_errors(at)
    assert "No valid rows were loaded from: Purchase orders, Supplier invoices" in at.error[0].value
    validation = next(d.value for d in at.dataframe if "Problem" in d.value.columns)
    problems = " ".join(validation["Problem"])
    assert "File is empty" in problems and "could not be parsed" in problems


def test_missing_columns_and_bad_values_are_listed():
    at = run(upload(start(), po="po_number,sku\nP1,SKU-1\n", inv=INV_CSV + "I2,not-a-date,S1,P1,SKU-1,x,5.00,USD\n"))
    validation = next(d.value for d in at.dataframe if "Problem" in d.value.columns)
    assert "Required column is missing" in " ".join(validation["Problem"])
    assert {"invoice_date", "quantity_invoiced"} <= set(validation["Field"])


def test_engine_failure_is_reported_without_results(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(webapp, "run_reconciliation", boom)
    at = run(upload(start()))
    no_errors(at)
    assert "Processing failed unexpectedly (RuntimeError)" in at.error[0].value
    assert "secret internal detail" not in at.error[0].value
    assert not at.metric


def test_invalid_environment_settings_block_running(monkeypatch):
    monkeypatch.setenv("OPSRESOLVE_QUANTITY_TOLERANCE", "abc")
    at = upload(start())
    assert "Invalid OPSRESOLVE_* environment configuration" in at.error[0].value
    assert at.button(key="run_button").disabled


# ---- stale results -----------------------------------------------------------------

def test_changing_a_setting_clears_results():
    at = run(use_sample(start()))
    assert at.metric
    at.number_input(key="cfg_quantity_tolerance").set_value(5.0).run()
    assert not at.metric and not at.tabs
    assert any("Inputs or settings changed" in i.value for i in at.info)


def test_changing_the_data_source_clears_results():
    at = run(use_sample(start()))
    at.radio(key="source").set_value("Upload my files").run()
    assert not at.metric and not at.tabs


def test_replacing_an_upload_clears_results_and_the_next_run_shows_new_data():
    at = run(upload(start()))
    assert metrics(at)["Exceptions"] == "0"
    at.file_uploader(key="upload_invoices").upload("invoices.csv", INV_CSV.replace("10,5.00", "10,5.50").encode(),
                                                    "text/csv")
    at.run()
    assert not at.metric  # old (zero-exception) results are gone before the new run
    at = run(at)
    assert metrics(at)["Exceptions"] == "1"
    assert at.selectbox(key="detail_id").options == ["EXC-0001 · PRICE_MISMATCH"]


def test_downloads_are_built_from_the_current_run(monkeypatch):
    seen = []
    for name in ("exceptions_csv", "validation_csv", "summary_markdown"):
        real = getattr(webapp, name)
        monkeypatch.setattr(webapp, name, lambda result, _real=real, _name=name: seen.append((_name, result))
                            or _real(result))
    at = run(use_sample(start()))
    first = at.session_state["last_run"]["result"]
    at.radio(key="source").set_value("Upload my files").run()
    at = run(upload(at))
    second = at.session_state["last_run"]["result"]
    assert second is not first and len(second.exceptions) == 0
    latest = {name: result for name, result in seen}
    assert set(latest) == {"exceptions_csv", "validation_csv", "summary_markdown"}
    assert all(result is second for result in latest.values())
    assert {d.key for d in at.download_button} >= {"dl_exceptions", "dl_validation", "dl_report"}


# ---- filters and display safety ------------------------------------------------------

def test_filters_narrow_the_table():
    at = run(use_sample(start()))
    at.multiselect(key="filter_severity").set_value(["HIGH"]).run()
    assert any(c.value.startswith("Showing 8 of 18") for c in at.caption)
    at.text_input(key="filter_query").set_value("no such text").run()
    assert any("No exceptions match these filters" in i.value for i in at.info)


def test_uploaded_text_is_not_rendered_as_markdown():
    hostile = "![x](http://tracker.invalid/p.png) **bold** <b>html</b>"
    # Hostile text in the supplier, SKU and invoice numbers. The two invoice numbers differ only in
    # formatting ("p.png" vs "p.0png" normalise alike), so the run produces a possible duplicate
    # (whose summary, action and notes quote invoice numbers) plus quantity and price findings.
    second = hostile.replace("p.png", "p.0png")
    po = PO_CSV.replace("S1,Supplier One,SKU-1", f'"{hostile}",Supplier One,"{hostile}"')
    inv = ("invoice_number,invoice_date,supplier_id,po_number,sku,quantity_invoiced,unit_price,currency\n"
           f'"{hostile}",2026-08-10,"{hostile}",P1,"{hostile}",10,4.00,USD\n'
           f'"{second}",2026-08-12,"{hostile}",P1,"{hostile}",10,4.00,USD\n')
    grn = GRN_CSV.replace("S1,P1,SKU-1", f'"{hostile}",P1,"{hostile}"')
    at = run(upload(start(), po=po, inv=inv, grn=grn))
    no_errors(at)
    options = at.selectbox(key="detail_id").options
    assert any("POSSIBLE_DUPLICATE_INVOICE" in o for o in options) and len(options) >= 3
    for option in options:
        at.selectbox(key="detail_id").set_value(option).run()
        rendered = [m.value for m in at.markdown]
        assert any("tracker" in value for value in rendered), f"hostile text should be displayed in {option}"
        for value in rendered:
            assert "](http" not in value and "<b>html" not in value, (option, value)


def test_hostile_upload_names_are_only_displayed(tmp_path, monkeypatch):
    # Streamlit itself only accepts *.csv names; these still try to escape or overwrite project files.
    import hashlib
    target = REPO / "data" / "sample" / "invoices.csv"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    monkeypatch.chdir(tmp_path)
    names = {"purchase_orders": "../../app.csv", "invoices": "../../data/sample/invoices.csv",
             "goods_received": "/etc/passwd.csv"}
    at = run(upload(start(), names=names))
    no_errors(at)
    files_table = next(d.value for d in at.dataframe if "Uploaded as" in d.value.columns)
    assert list(files_table["Uploaded as"]) == ["../../app.csv", "../../data/sample/invoices.csv", "/etc/passwd.csv"]
    assert list(tmp_path.iterdir()) == []
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    assert not (REPO.parent / "app.csv").exists()


# ---- review fix 1: header-only files ---------------------------------------------------

def test_header_only_files_show_an_accurate_warning():
    at = run(upload(start(), inv=INV_CSV.splitlines()[0] + "\n", grn=GRN_CSV.splitlines()[0] + "\n"))
    no_errors(at)
    assert not at.error
    assert any("contained no data rows (header only)" in w.value for w in at.warning)
    assert metrics(at)["Validation issues"] == "0"
    assert any("No validation issues" in s.value for s in at.success)  # the validation tab


# ---- review fix 2: displayed settings are the settings used --------------------------------

def test_tolerance_reached_with_plus_buttons_is_exactly_the_displayed_value():
    at = start()
    for _ in range(10):  # AppTest's increment accumulates 0.01 as a float, like a drifting widget
        at.number_input(key="cfg_price_tolerance_abs").increment().run()
    assert f"{at.number_input(key='cfg_price_tolerance_abs').value:.2f}" == "0.10"
    at = run(upload(at, inv=INV_CSV.replace("10,5.00,USD", "10,5.10,USD")))
    no_errors(at)
    assert at.session_state["last_run"]["result"].config.price_tolerance_abs == Decimal("0.10")
    assert metrics(at)["Exceptions"] == "0"  # exactly at the inclusive 0.10 boundary


# ---- review fix 3: file names follow the current uploads -------------------------------------

def test_same_contents_under_new_names_keep_results_and_show_new_names():
    at = run(upload(start(), names={"purchase_orders": "po_v1.csv", "invoices": "inv_v1.csv"}))
    first = at.session_state["last_run"]["result"]
    at = upload(at, names={"purchase_orders": "po_v2.csv", "invoices": "inv_v2.csv"})  # same bytes
    no_errors(at)
    assert at.session_state["last_run"]["result"] is first  # results kept: contents are identical
    assert metrics(at)["Exceptions"] == "0"
    files_table = next(d.value for d in at.dataframe if "Uploaded as" in d.value.columns)
    assert list(files_table["Uploaded as"]) == ["po_v2.csv", "inv_v2.csv", "goods_received.csv"]


def test_validation_view_shows_the_current_file_name():
    bad = PO_CSV + "P2,1,S1,Supplier One,SKU-2,ten,5.00,USD,2026-08-01\n"
    at = run(upload(start(), po=bad, names={"purchase_orders": "orders_march.csv"}))
    at = upload(at, po=bad, names={"purchase_orders": "orders_march_final.csv"})
    no_errors(at)
    validation = next(d.value for d in at.dataframe if "Problem" in d.value.columns)
    assert list(validation["File"]) == ["orders_march_final.csv"]
    assert metrics(at)["Validation issues"] == "1"


# ---- import profiles (milestone 7, stage 4b) --------------------------------------------------------
# Expected values are derived by hand from the fixtures in tests/fixtures/profiles/ (see the stage 2 tests).

import hashlib  # noqa: E402
import json  # noqa: E402

from opsresolve.profiles import load_profile  # noqa: E402

PROFILE_FIXTURES = REPO / "tests" / "fixtures" / "profiles"
ERP_PROFILE = PROFILE_FIXTURES / "erp_profile.json"
ERP_SHORT_HASH = hashlib.sha256(ERP_PROFILE.read_bytes()).hexdigest()[:12]
EXAMPLE_PROFILE = REPO / "docs" / "examples" / "import-profile-example.json"
HOSTILE_NAME = "<img src=x onerror=alert(1)> **bold** [link](javascript:x) `code` | # _x_ &amp;"
FILE_KEYS = ("purchase_orders", "invoices", "goods_received", "uom_conversions")


def upload_folder(at: AppTest, folder: Path) -> AppTest:
    for key in FILE_KEYS:
        at.file_uploader(key=f"upload_{key}").upload(f"{key}.csv", (folder / f"{key}.csv").read_bytes(), "text/csv")
    return at.run()


def upload_profile(at: AppTest, data: bytes, name: str = "profile.json") -> AppTest:
    at.file_uploader(key="upload_profile").upload(name, data, "application/json")
    return at.run()


def json_profile(**body) -> bytes:
    return json.dumps({"profile_version": 1, "name": "Test", **body}).encode()


def keys(widgets) -> set:
    return {w.key for w in widgets}


def texts(at: AppTest) -> list[str]:
    """Every piece of text the page renders as Markdown (markdown, captions and alerts)."""
    return [e.value for group in (at.markdown, at.caption, at.success, at.error, at.info, at.warning) for e in group]


def document_order(at: AppTest) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def walk(node):
        for child in getattr(node, "children", {}).values():
            if getattr(child, "type", None) in ("markdown", "metric", "alert"):
                out.append((child.type, str(getattr(child, "value", ""))))
            walk(child)
    walk(at.main)
    return out


def test_profile_uploader_appears_only_in_upload_mode():
    at = start()
    assert "upload_profile" in keys(at.file_uploader)
    assert "What is an import profile?" in [e.proto.label for e in at.expander]
    at = use_sample(at)
    no_errors(at)
    assert "upload_profile" not in keys(at.file_uploader)
    assert "example_profile" not in keys(at.download_button)
    assert not any("import profile" in t.lower() for t in texts(at))


def test_example_profile_offered_for_download_is_valid():
    at = start()
    assert "example_profile" in keys(at.download_button)
    profile = load_profile(EXAMPLE_PROFILE)
    assert profile.name == "Example ERP export (semicolon, decimal comma)"
    assert profile.format_for("purchase_orders").delimiter == ";"
    assert profile.format_for("goods_received").date_format == "YYYY-MM-DD"  # a per-file override
    caption = " ".join(c.value for c in at.caption)
    assert "With a valid import profile, files are read in the formats it declares" in caption


def test_valid_profile_feedback_run_and_provenance_above_the_metrics():
    at = upload_profile(upload_folder(start(), PROFILE_FIXTURES / "erp"), ERP_PROFILE.read_bytes(), "erp.json")
    no_errors(at)
    assert [s.value for s in at.success] == [
        f"Import profile loaded: Fictional ERP export \\(test fixture\\) (sha256 {ERP_SHORT_HASH})"]
    assert not at.button(key="run_button").disabled
    at = run(at)
    no_errors(at)
    # 2 + 2 + 2 + 1 rows; the price (A-100) and quantity (B-200) exceptions of the fixture.
    assert metrics(at) == {"Rows read": "7", "Validation issues": "0", "Exceptions": "2",
                           "High severity": "1", "Medium severity": "1", "Low severity": "0"}
    provenance = f"Import profile: Fictional ERP export \\(test fixture\\) (sha256 {ERP_SHORT_HASH})"
    order = document_order(at)
    assert ("markdown", provenance) in order
    first_metric = next(i for i, (kind, _) in enumerate(order) if kind == "metric")
    assert order.index(("markdown", provenance)) < first_metric
    # The same files without the profile load no rows: the profile was really applied.
    at.file_uploader(key="upload_profile").clear()
    at = run(at.run())
    assert metrics(at)["Rows read"] == "0"
    assert not any(t.startswith("Import profile:") for t in texts(at))


def test_invalid_profile_lists_problems_as_plain_text_and_disables_run():
    data = json.dumps({"profile_version": 1, "name": "x", "<b>bold</b>": 1,
                       "defaults": {"thousands_separator": ","}}).encode()
    at = upload_profile(upload_folder(start(), PROFILE_FIXTURES / "canonical"), data)
    no_errors(at)
    assert at.button(key="run_button").disabled
    assert [e.value for e in at.error] == [
        "The import profile is not valid (2 problem(s)). Fix or remove it to run."]
    problems = list(at.dataframe[0].value["Problem"])
    assert problems == [
        "profile: unknown key '<b>bold</b>' (allowed: profile_version, name, defaults, files)",
        "defaults: the delimiter ',' must not also be a decimal or thousands separator",
    ]
    assert any(c.value.startswith("Waiting for a valid import profile") for c in at.caption)
    assert not at.success
    # Removing the invalid profile enables Run again.
    at.file_uploader(key="upload_profile").clear()
    assert not at.run().button(key="run_button").disabled


@pytest.mark.parametrize("data", [b'{"profile_version": 1,', b"\xff\xfe", b" " * 70_000])
def test_malformed_undecodable_or_oversized_profiles_disable_run(data):
    at = upload_profile(upload_folder(start(), PROFILE_FIXTURES / "canonical"), data)
    no_errors(at)
    assert at.button(key="run_button").disabled
    assert len(at.error) == 1 and len(at.dataframe[0].value) == 1


def test_run_stays_disabled_for_missing_files_even_with_a_valid_profile():
    at = upload_profile(start(), ERP_PROFILE.read_bytes())
    assert at.success and at.button(key="run_button").disabled
    assert any(c.value.startswith("Waiting for: Purchase orders") for c in at.caption)


@pytest.mark.parametrize("change", ["add", "edit", "replace with invalid", "remove"])
def test_any_profile_change_clears_previous_results(change):
    canonical = PROFILE_FIXTURES / "canonical"
    named = json_profile(name="Built-in layout")  # declares nothing else: the built-in format
    at = upload_folder(start(), canonical)
    if change != "add":
        at = upload_profile(at, named)
    at = run(at)
    assert at.metric and at.tabs
    if change == "add":
        at = upload_profile(at, named)
    elif change == "edit":
        at = upload_profile(at, json_profile(name="Built-in layout, edited"))
    elif change == "replace with invalid":
        at = upload_profile(at, b"{")
    else:
        at.file_uploader(key="upload_profile").clear()
        at = at.run()
    no_errors(at)
    assert not at.metric and not at.tabs
    assert any(i.value.startswith("Inputs or settings changed since the last run") for i in at.info)


def test_unchanged_profile_keeps_results():
    at = run(upload_profile(upload_folder(start(), PROFILE_FIXTURES / "canonical"), json_profile()))
    at = upload_profile(at, json_profile(), "renamed.json")  # same bytes, new file name
    assert at.metric and not any(i.value.startswith("Inputs or settings changed") for i in at.info)


def test_hostile_profile_name_is_never_rendered_unescaped():
    data = json_profile(name=HOSTILE_NAME)
    at = run(upload_profile(upload_folder(start(), PROFILE_FIXTURES / "canonical"), data, "<script>.json"))
    no_errors(at)
    escaped = webapp.escape_markdown(HOSTILE_NAME)
    short = hashlib.sha256(data).hexdigest()[:12]
    assert f"Import profile loaded: {escaped} (sha256 {short})" in [s.value for s in at.success]
    assert f"Import profile: {escaped} (sha256 {short})" in [m.value for m in at.markdown]
    rendered = texts(at)
    for raw in ("<img", "**bold**", "[link](", "`code`", "&amp;", "<script>"):
        pattern = re.compile(r"(?<!\\)" + re.escape(raw[0]) + re.escape(raw[1:]))
        assert not any(pattern.search(t) for t in rendered), raw
    # Only the fixed severity badges and page style may use HTML; nothing containing the name does.
    assert not any(m.proto.allow_html and "img" in m.value for m in at.markdown)


def test_sample_mode_ignores_an_uploaded_profile():
    at = upload_profile(start(), ERP_PROFILE.read_bytes())
    assert at.success
    at = run(use_sample(at))
    no_errors(at)
    assert metrics(at)["Exceptions"] == "18" and metrics(at)["Validation issues"] == "3"
    assert not at.success and not any(t.startswith("Import profile") for t in texts(at))
