"""UI tests for app.py using Streamlit's AppTest (no browser, no sleeps, no screen sizes)."""

import re
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="install the app extra: pip install -e '.[app]'")
from streamlit import config as streamlit_config  # noqa: E402
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


@pytest.fixture(autouse=True)
def bound_to_localhost():
    """As launched with `streamlit run app.py --server.address localhost`; restored after each test."""
    before = streamlit_config.get_option("server.address")
    streamlit_config.set_option("server.address", "localhost")
    yield
    streamlit_config.set_option("server.address", before)


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

def test_the_single_streamlit_config_is_shared_and_leaves_binding_to_the_launch_command():
    text = (REPO / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    settings = dict(re.findall(r"^(\w+) = (.+)$", text, re.M))
    assert "address" not in settings and "port" not in settings and "serverAddress" not in settings
    assert settings["gatherUsageStats"] == "false"
    assert settings["enableXsrfProtection"] == "true"
    assert settings["maxUploadSize"] == "20"
    # Streamlit Community Cloud reads only the repository root's config file, so there is no other one.
    configs = [p.relative_to(REPO).as_posix() for p in REPO.rglob("config.toml")
               if ".venv" not in p.parts and "venv" not in p.parts]
    assert configs == [".streamlit/config.toml"]


@pytest.mark.parametrize("address", ["localhost", "127.0.0.1", "::1"])
def test_local_app_runs_when_bound_to_this_computer(address):
    streamlit_config.set_option("server.address", address)
    at = start()
    no_errors(at)
    assert not at.error and at.get("file_uploader")


@pytest.mark.parametrize("address", [None, "", "0.0.0.0", "::", "192.168.1.20"])
def test_local_app_refuses_to_run_unless_bound_to_this_computer(address):
    streamlit_config.set_option("server.address", address)
    at = start()
    no_errors(at)
    assert "runs only when the server listens on this computer alone" in at.error[0].value
    assert at.code[0].value == "streamlit run app.py --server.address localhost"
    assert at.get("file_uploader") == [] and not at.toggle and not at.button and not at.sidebar.number_input


LAUNCH = re.compile(r"streamlit run (app\.py|demo/streamlit_app\.py)\b[^\n`]*")


@pytest.mark.parametrize("document", ["README.md", "CLAUDE.md", "app.py", "demo/streamlit_app.py",
                                      ".streamlit/config.toml"])
def test_documented_local_launch_commands_bind_to_localhost(document):
    commands = [m.group(0) for m in LAUNCH.finditer((REPO / document).read_text(encoding="utf-8"))]
    assert commands, document
    assert all("--server.address localhost" in command for command in commands), commands


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


def test_long_validation_problems_wrap_instead_of_being_cut_off():
    source = (REPO / "app.py").read_text(encoding="utf-8")
    # Streamlit's table wraps cell text only in rows taller than 4rem (64 px).
    assert int(re.search(r"^VALIDATION_ROW_HEIGHT = (\d+)", source, re.M).group(1)) > 64
    assert "column_config=VALIDATION_COLUMNS, row_height=VALIDATION_ROW_HEIGHT" in source
    tab = run(use_sample(start())).tabs[1]  # the validation tab still lists every problem in full
    problems = tab.dataframe[0].value["Problem"].tolist()
    assert any("month value is outside expected range of 1-12; row rejected" in p for p in problems)


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


# ---- optional AI explanations (C2): fakes only, network blocked, no real key -------------------------------------

import socket  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from opsresolve import ai  # noqa: E402  (the tests may use it; app.py itself must not import it)
from opsresolve.models import ExceptionType  # noqa: E402
from opsresolve.pipeline import investigate_files  # noqa: E402

FAKE_KEY = "sk-ant-test-key-never-sent-0123456789"  # fake; the fake SDK below is the only thing that sees it
SAMPLE = REPO / "data" / "sample"


class NetworkBlocked(AssertionError):
    pass


@pytest.fixture
def offline(monkeypatch):
    """No real connection, no real key, no OPSRESOLVE_AI_* settings from the developer's shell."""
    def blocked(*args, **kwargs):
        raise NetworkBlocked("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.delenv(ai.API_KEY_VARIABLE, raising=False)
    for name in ("MODEL", "EFFORT", "TIMEOUT_SECONDS", "MAX_RETRIES", "MAX_TOKENS"):
        monkeypatch.delenv(f"OPSRESOLVE_AI_{name}", raising=False)


def mismatch_id() -> str:
    """The sample's supplier mismatch on PO 4500010015 (SUP-001 on the PO, SUP-009 on invoice RVX-00981)."""
    result = investigate_files(SAMPLE / "purchase_orders.csv", SAMPLE / "invoices.csv", SAMPLE / "goods_received.csv")
    [exc] = [e for e in result.exceptions
             if e.exception_type is ExceptionType.SUPPLIER_MISMATCH and e.po_number == "4500010015"]
    return exc.exception_id


def good_answer(exception_id: str, explanation: str | None = None) -> dict:
    return {
        "exception_id": exception_id, "direction": "not_applicable",
        "explanation": explanation or ("Invoice RVX-00981 is from supplier SUPPLIER-2, but PO 4500010015 was raised "
                                       "with supplier SUPPLIER-1 (E1, E2)."),
        "confirmed_facts": [{"statement": "PO 4500010015 was raised with supplier SUPPLIER-1.",
                             "evidence_refs": ["E1"]}],
        "possible_causes": [{"hypothesis": "The invoice may have been keyed against the wrong supplier.",
                             "status": "unverified_hypothesis", "supporting_refs": ["E2"],
                             "how_to_check": "Compare invoice RVX-00981 with the vendor master."}],
        "investigation_steps": [{"step": "Check the remittance details on invoice RVX-00981.",
                                 "evidence_refs": ["E2"]}],
        "evidence_refs_used": ["E1", "E2"], "sufficiency": "partial",
        "missing_information": ["The vendor master record."],
    }


class FakeSDK:
    """Stands in for the Anthropic SDK: records every client and request, returns ``text`` or raises ``error``."""

    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(APIError):
        status_code = 500

    AuthenticationError = PermissionDeniedError = RateLimitError = APIStatusError

    def __init__(self, text: str = "", error: Exception | None = None, stop_reason: str = "end_turn"):
        self.text, self.error, self.stop_reason = text, error, stop_reason
        self.clients, self.requests = [], []

    def Anthropic(self, **settings):  # noqa: N802 (the SDK's name)
        self.clients.append(settings)
        return SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **body):
        self.requests.append(body)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], stop_reason=self.stop_reason,
                               _request_id="req_secret_id")


@pytest.fixture
def ready(offline, monkeypatch):
    """AI explanations ready: the SDK counts as installed, a fake key is set, and the fake SDK is used."""
    sdk = FakeSDK(json.dumps(good_answer(mismatch_id())))
    monkeypatch.setattr(ai, "sdk_available", lambda: True)
    monkeypatch.setattr(ai, "_import_sdk", lambda: sdk)
    monkeypatch.setenv(ai.API_KEY_VARIABLE, FAKE_KEY)
    return sdk


def sample_with_ai(enabled: bool = True) -> AppTest:
    at = run(use_sample(start()))
    if enabled:
        at = at.toggle(key="ai_enabled").set_value(True).run()
    return at.selectbox(key="detail_id").set_value(
        next(o for o in at.selectbox(key="detail_id").options if o.startswith(mismatch_id()))).run()


def send(at: AppTest) -> AppTest:
    return at.button(key=f"ai_send_{mismatch_id()}").click().run()


def everything_shown(at: AppTest) -> str:
    groups = (at.markdown, at.caption, at.success, at.error, at.info, at.warning, at.text, at.code)
    shown = [e.value for group in groups for e in group]
    shown += [d.value.to_csv() for d in at.dataframe]
    return "\n".join(shown)


def ai_buttons(at: AppTest) -> list[str]:
    return [b.key for b in at.button if b.key and b.key.startswith("ai_")]


# ---- off by default ------------------------------------------------------------------------------------------------

def test_ai_is_off_by_default_and_nothing_ai_runs(offline, monkeypatch):
    monkeypatch.setattr(webapp, "ai_status", lambda *a, **k: pytest.fail("AI readiness checked while off"))
    monkeypatch.setattr(webapp, "request_ai_explanation", lambda *a, **k: pytest.fail("AI request while off"))
    at = sample_with_ai(enabled=False)
    no_errors(at)
    assert at.toggle(key="ai_enabled").value is False
    assert ai_buttons(at) == [] and not at.checkbox and not at.code
    assert "AI explanation" not in " ".join(m.value for m in at.markdown)
    assert metrics(at)["Exceptions"] == "18"


# ---- not ready -----------------------------------------------------------------------------------------------------

def test_without_the_sdk_the_reason_is_shown_and_nothing_can_be_sent(offline, monkeypatch):
    monkeypatch.setattr(ai, "sdk_available", lambda: False)
    monkeypatch.setenv(ai.API_KEY_VARIABLE, FAKE_KEY)
    at = sample_with_ai()
    no_errors(at)
    assert webapp.escape_markdown(webapp.AI_STATUS_MESSAGES["sdk_missing"]) in [w.value for w in at.warning]
    assert ai_buttons(at) == [] and not at.code


def test_without_a_key_the_reason_is_shown_and_nothing_can_be_sent(offline, monkeypatch):
    monkeypatch.setattr(ai, "sdk_available", lambda: True)
    at = sample_with_ai()
    no_errors(at)
    assert webapp.escape_markdown(webapp.AI_STATUS_MESSAGES["key_missing"]) in [w.value for w in at.warning]
    assert ai_buttons(at) == [] and not at.code


def test_invalid_settings_are_reported_without_their_values(offline, monkeypatch):
    monkeypatch.setattr(ai, "sdk_available", lambda: True)
    monkeypatch.setenv(ai.API_KEY_VARIABLE, FAKE_KEY)
    monkeypatch.setenv("OPSRESOLVE_AI_TIMEOUT_SECONDS", "secret-looking-value")
    at = sample_with_ai()
    no_errors(at)
    warning = " ".join(w.value for w in at.warning)
    assert "invalid" in warning and "secret-looking-value" not in everything_shown(at)
    assert ai_buttons(at) == [] and FAKE_KEY not in everything_shown(at)


# ---- ready: preview and explicit send ------------------------------------------------------------------------------

def test_loading_enabling_and_previewing_send_nothing(ready):
    at = sample_with_ai()
    no_errors(at)
    assert ai_buttons(at) == [f"ai_send_{mismatch_id()}"]
    assert any("after 120 seconds and retried 0 time(s)" in s.value for s in at.caption)
    at = at.checkbox(key=f"ai_preview_{mismatch_id()}").check().run()
    no_errors(at)
    [code] = at.code
    result = at.session_state["last_run"]["result"]
    exc = next(e for e in result.exceptions if e.exception_id == mismatch_id())
    assert code.value == webapp.preview_ai_request(result, exc, webapp.interactive_ai_config()).body
    assert code.language == "json" and "SUP-001" not in code.value and FAKE_KEY not in code.value
    assert ready.clients == [] and ready.requests == []  # no client, no request


def test_an_accepted_explanation_is_shown_literally_after_an_explicit_send(ready):
    text = ("Invoice RVX-00981 is from supplier SUPPLIER-2 <script>alert(SUPPLIER-1)</script> **bold** "
            "[click](javascript:steal) (E1, E2).")
    ready.text = json.dumps(good_answer(mismatch_id(), text))
    at = send(sample_with_ai())
    no_errors(at)
    assert len(ready.requests) == 1
    [client] = ready.clients
    assert (client["timeout"], client["max_retries"]) == (120.0, 0)  # the interactive policy reached the SDK
    shown_literally = [t.value for t in at.text]
    expected = text.replace("SUPPLIER-2", "SUP-009").replace("SUPPLIER-1", "SUP-001")
    assert expected in shown_literally  # pseudonyms restored; markup kept as plain text
    assert webapp.escape_markdown(webapp.AI_ACCEPTED_LABEL) in [i.value for i in at.info]
    markdown = " ".join(m.value for group in (at.markdown, at.caption, at.info, at.warning, at.error, at.success)
                        for m in group)
    assert "<script>" not in markdown and "**bold**" not in markdown and "](javascript" not in markdown
    tables = {tuple(d.value.columns): d.value for d in at.dataframe}
    assert tables[("Fact", "Evidence")].iloc[0].tolist() == ["PO 4500010015 was raised with supplier SUP-001.", "E1"]
    assert tables[("Hypothesis", "How to check", "Evidence")].iloc[0, 0].startswith("The invoice may have")
    assert webapp.AI_SUFFICIENCY["partial"] in shown_literally
    assert "req_secret_id" not in everything_shown(at) and FAKE_KEY not in everything_shown(at)
    assert not any(m.proto.allow_html and "SUP-" in m.value for m in at.markdown)


def test_reruns_show_the_stored_explanation_without_sending_again(ready):
    at = send(sample_with_ai())
    at = at.run()
    at = at.checkbox(key=f"ai_preview_{mismatch_id()}").check().run()
    no_errors(at)
    assert len(ready.requests) == 1 and any("SUP-009" in t.value for t in at.text)


def test_a_rejected_answer_shows_codes_and_locations_only(ready):
    bad = good_answer(mismatch_id(), "Northgate Fasteners Ltd billed 2,500 units of SKU-X999 (E1, E2).")
    bad["invented_field"] = "<img src=x onerror=steal>"
    ready.text = json.dumps(bad)
    before = sample_with_ai()
    shown_before = everything_shown(before)  # the engine's own evidence already names the supplier
    at = send(before)
    no_errors(at)
    assert webapp.escape_markdown(webapp.AI_REJECTED_MESSAGE) in [w.value for w in at.warning]
    problems = next(d.value for d in at.dataframe if list(d.value.columns) == ["Problem", "Field"])
    assert ("schema", webapp.UNEXPECTED_LOCATION) in list(problems.itertuples(index=False, name=None))
    shown = everything_shown(at)
    for leaked in ("Northgate", "2,500", "SKU-X999", "onerror", "invented_field", "in this evidence"):
        assert shown.count(leaked) == shown_before.count(leaked), leaked  # nothing of the answer is added
    assert all("Northgate" not in t.value for t in at.text)


@pytest.mark.parametrize("make_error, code", [
    (lambda: FakeSDK.APITimeoutError("secret raw timeout detail"), "timeout"),
    (lambda: FakeSDK.APIConnectionError("secret raw connection detail"), "connection"),
    (lambda: FakeSDK.APIStatusError("secret raw status detail"), "authentication"),  # the fake maps 4xx/5xx alike
    (lambda: RuntimeError("secret raw unexpected detail"), "provider_error"),
])
def test_provider_failures_show_fixed_messages(ready, make_error, code):
    ready.error = make_error()
    at = send(sample_with_ai())
    no_errors(at)
    assert webapp.escape_markdown(webapp.AI_ERROR_MESSAGES[code]) in [e.value for e in at.error]
    shown = everything_shown(at)
    assert "secret raw" not in shown and FAKE_KEY not in shown and "req_secret_id" not in shown


@pytest.mark.parametrize("text, stop_reason, status", [("not json", "end_turn", "rejected"),
                                                       ("", "end_turn", "malformed"),
                                                       ("{", "max_tokens", "malformed"),
                                                       ("", "refusal", "refused")])
def test_malformed_cut_off_and_refused_answers_are_handled(ready, text, stop_reason, status):
    ready.text, ready.stop_reason = text, stop_reason
    at = send(sample_with_ai())
    no_errors(at)
    if status == "rejected":
        assert webapp.escape_markdown(webapp.AI_REJECTED_MESSAGE) in [w.value for w in at.warning]
    else:
        assert webapp.escape_markdown(webapp.AI_ERROR_MESSAGES[status]) in [e.value for e in at.error]
    assert "not json" not in everything_shown(at)


def test_the_privacy_guard_blocks_preview_and_send(ready, monkeypatch):
    monkeypatch.setattr(ai, "pseudonymise", lambda pack, known: (pack, {}))  # a failed replacement
    at = sample_with_ai().checkbox(key=f"ai_preview_{mismatch_id()}").check().run()
    no_errors(at)
    message = webapp.escape_markdown(webapp.AI_ERROR_MESSAGES["privacy_guard"])
    assert message in [w.value for w in at.warning] and not at.code
    at = send(at)
    assert message in [e.value for e in at.error]
    assert ready.requests == [] and "SUP-001" not in " ".join(e.value for e in at.error)


def test_explicit_timeout_settings_reach_the_sdk(ready, monkeypatch):
    monkeypatch.setenv("OPSRESOLVE_AI_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("OPSRESOLVE_AI_MAX_RETRIES", "2")
    at = send(sample_with_ai())
    no_errors(at)
    assert [(c["timeout"], c["max_retries"]) for c in ready.clients] == [(45.0, 2)]


# ---- the engine's results and downloads are not changed -----------------------------------------------------------

def test_explaining_changes_no_result_or_download(ready):
    at = sample_with_ai()
    result = at.session_state["last_run"]["result"]
    before = ([e.model_dump() for e in result.exceptions], webapp.exceptions_csv(result),
              webapp.validation_csv(result), webapp.summary_markdown(result))
    downloads_before = [d.key for d in at.download_button]
    at = send(at)
    no_errors(at)
    result_after = at.session_state["last_run"]["result"]
    after = ([e.model_dump() for e in result_after.exceptions], webapp.exceptions_csv(result_after),
             webapp.validation_csv(result_after), webapp.summary_markdown(result_after))
    assert after == before and [d.key for d in at.download_button] == downloads_before
    assert metrics(at)["Exceptions"] == "18"


def test_changing_inputs_clears_stored_explanations(ready):
    at = send(sample_with_ai())
    assert at.session_state["ai_views"]
    at = at.number_input(key="cfg_quantity_tolerance").set_value(5.0).run()
    assert "ai_views" not in at.session_state and not at.tabs


def test_app_uses_html_only_for_its_fixed_styling():
    source = (REPO / "app.py").read_text(encoding="utf-8")
    assert source.count("unsafe_allow_html=True") == 2  # the page style and the severity badge
    assert "opsresolve.ai" not in source and "import ai" not in source


# ---- C2 follow-up L1: one request per finding at a time ----------------------------------------------------------

import threading  # noqa: E402


def request_key(at: AppTest) -> tuple:
    return (at.session_state["last_run"]["fingerprint"], mismatch_id())


def test_one_click_sends_once_and_a_later_click_can_send_again(ready):
    at = send(sample_with_ai())
    no_errors(at)
    assert len(ready.requests) == 1
    assert at.button(key=f"ai_send_{mismatch_id()}").disabled is False  # enabled again after the answer
    assert not at.session_state["ai_guard"].is_pending(request_key(at))
    at = send(at)
    no_errors(at)
    assert len(ready.requests) == 2


def test_no_second_request_while_one_is_in_progress(ready):
    at = sample_with_ai()
    guard, key = at.session_state["ai_guard"], request_key(at)
    started, release = threading.Event(), threading.Event()
    worker = threading.Thread(target=guard.run, args=(key, lambda: started.set() or release.wait(10)))
    worker.start()
    try:
        assert started.wait(5)
        # The button was drawn enabled before the request started: a click that arrives now (the race) ...
        at = send(at)
        no_errors(at)
        assert ready.requests == [] and ready.clients == []  # ... starts nothing
        assert webapp.escape_markdown(webapp.AI_REQUEST_IN_PROGRESS) in [i.value for i in at.info]
        assert at.button(key=f"ai_send_{mismatch_id()}").disabled is True  # and the button is now disabled
    finally:
        release.set()
        worker.join(5)
    at = at.run()
    assert at.button(key=f"ai_send_{mismatch_id()}").disabled is False
    at = send(at)
    assert len(ready.requests) == 1  # after the first one ends, a send works


def test_a_send_not_started_is_dropped_when_another_finding_is_shown(ready):
    at = sample_with_ai()
    guard, key = at.session_state["ai_guard"], request_key(at)
    assert guard.mark_pending(key)  # as if the page stopped right after the click
    other = next(o for o in at.selectbox(key="detail_id").options if not o.startswith(mismatch_id()))
    at = at.selectbox(key="detail_id").set_value(other).run()
    assert not guard.is_pending(key)
    at = at.selectbox(key="detail_id").set_value(
        next(o for o in at.selectbox(key="detail_id").options if o.startswith(mismatch_id()))).run()
    no_errors(at)
    assert ready.requests == []  # never sent later without a new click


# ---- Stage D2: investigation checks in the detail panel --------------------------------------------------------------

import pandas as pd  # noqa: E402

from opsresolve import investigation  # noqa: E402


def select(at: AppTest, exception_id: str) -> AppTest:
    option = next(o for o in at.selectbox(key="detail_id").options if o.startswith(exception_id + " "))
    return at.selectbox(key="detail_id").set_value(option).run()


def investigation_tables(at: AppTest) -> dict[str, pd.DataFrame]:
    tables = {}
    for frame in (d.value for d in at.dataframe):
        columns = tuple(frame.columns)
        if columns in (("Check", "Candidate explanation", "Status"), ("Observation", "Records"),
                       ("Needed to decide",), ("Ref", "File", "Line", "Reference")):
            tables.setdefault(columns[0], pd.DataFrame())
            tables[columns[0]] = pd.concat([tables[columns[0]], frame], ignore_index=True)
    return tables


def sample_finding_with_checks() -> str:
    """The sample's first missing goods receipt: receipt not posted is consistent, a GRN elsewhere can't be checked."""
    result = investigate_files(SAMPLE / "purchase_orders.csv", SAMPLE / "invoices.csv", SAMPLE / "goods_received.csv")
    return next(e.exception_id for e in result.exceptions if e.exception_type is ExceptionType.MISSING_GOODS_RECEIPT)


def test_the_detail_panel_shows_the_checks_with_their_evidence(offline):
    at = select(run(use_sample(start())), sample_finding_with_checks())
    no_errors(at)
    assert "**Investigation checks** (deterministic; candidate explanations, never confirmed causes)" in [
        m.value for m in at.markdown]
    tables = investigation_tables(at)
    assert list(tables["Check"]["Status"]) == ["Consistent with the data — not confirmed", "Can't be checked"]
    assert list(tables["Check"]["Check"]) == ["H1", "H2"]
    assert [e.label for e in at.expander if e.label.startswith("H")] == ["H1: details", "H2: details"]
    assert list(tables["Ref"]["Ref"]) == ["R1", "R2"]
    assert set(tables["Ref"]["File"]) == {"data/sample/purchase_orders.csv", "data/sample/invoices.csv"}
    assert any("not confirmed" in t.value for t in at.text)
    assert not at.warning or all(webapp.CHECK_FAILED_LABEL not in w.value for w in at.warning)


def test_findings_outside_the_catalogue_say_so(offline):
    result = investigate_files(SAMPLE / "purchase_orders.csv", SAMPLE / "invoices.csv", SAMPLE / "goods_received.csv")
    duplicate = next(e.exception_id for e in result.exceptions
                     if e.exception_type is ExceptionType.POSSIBLE_DUPLICATE_INVOICE)
    at = select(run(use_sample(start())), duplicate)
    assert webapp.escape_markdown(webapp.INVESTIGATION_NONE) in [c.value for c in at.caption]
    assert "Check" not in investigation_tables(at)


def test_investigations_are_computed_once_per_run_and_after_every_change(offline, monkeypatch):
    calls = []
    real = webapp.investigate_run
    monkeypatch.setattr(webapp, "investigate_run", lambda result, fp: calls.append(fp) or real(result, fp))
    at = run(use_sample(start()))
    assert len(calls) == 1
    at = select(at, sample_finding_with_checks())
    at = at.multiselect(key="filter_severity").set_value(["HIGH"]).run()
    at = at.multiselect(key="filter_severity").set_value([]).run()
    assert len(calls) == 1  # browsing findings and filters reuses the run's results
    at = at.number_input(key="cfg_quantity_tolerance").set_value(5.0).run()
    assert not at.tabs and "investigations" not in at.session_state.get("last_run", {})
    at = run(at)
    assert len(calls) == 2 and calls[1] != calls[0]  # new settings: a new fingerprint, computed again
    at = run(at)
    assert len(calls) == 3  # a new run with the same inputs is never served the previous run's checks
    assert at.session_state["last_run"]["investigations"].result is at.session_state["last_run"]["result"]


def test_a_failed_check_shows_only_its_fixed_label(offline, monkeypatch):
    def broken(subject, index):
        raise RuntimeError("secret internal detail RVX-00981")

    monkeypatch.setitem(investigation._CHECKS, "document_under_other_po", broken)
    at = select(run(use_sample(start())), sample_finding_with_checks())
    no_errors(at)
    assert "H2: Check failed — nothing concluded" in [w.value.replace("\\", "") for w in at.warning]
    tables = investigation_tables(at)
    assert list(tables["Check"]["Status"]) == ["Consistent with the data — not confirmed"]  # evidence only
    assert [e.label for e in at.expander if e.label.startswith("H")] == ["H1: details"]
    shown = everything_shown(at)
    assert "RuntimeError" not in shown and "secret internal detail" not in shown
    assert investigation.CHECK_FAILED not in shown and "Traceback" not in shown
    assert metrics(at)["Exceptions"] == "18"


def test_a_failure_before_the_checks_shows_a_fixed_message(offline, monkeypatch):
    monkeypatch.setattr(investigation, "_subject", lambda exception, index: (_ for _ in ()).throw(KeyError("x")))
    at = select(run(use_sample(start())), sample_finding_with_checks())
    no_errors(at)
    assert webapp.escape_markdown(webapp.INVESTIGATION_NOT_RUN) in [w.value for w in at.warning]
    assert "KeyError" not in everything_shown(at)


def test_an_indexing_failure_is_contained_to_the_section(offline, monkeypatch):
    def broken(self, result):
        raise ValueError("secret internal detail")

    monkeypatch.setattr(investigation._Index, "__init__", broken)
    at = select(run(use_sample(start())), sample_finding_with_checks())
    no_errors(at)
    assert webapp.escape_markdown(webapp.INVESTIGATION_UNAVAILABLE) in [i.value for i in at.info]
    assert metrics(at)["Exceptions"] == "18" and len(at.selectbox(key="detail_id").options) == 18
    assert {d.key for d in at.download_button} >= {"dl_exceptions", "dl_validation", "dl_report"}
    shown = everything_shown(at)
    assert "secret" not in shown and "ValueError" not in shown


def test_hostile_text_in_the_checks_is_shown_only_as_literal_text(offline):
    hostile = "![x](http://tracker.invalid/p.png) **bold** <b>html</b>"
    po = PO_CSV + "P2,1,S1,Supplier One,SKU-2,10,5.00,USD,2026-08-01\n"
    inv = INV_CSV + "I2,2026-08-10,S1,P2,SKU-2,10,5.00,USD\n"
    grn = GRN_CSV + f'"{hostile}",2026-08-08,S1,"{hostile}",SKU-2,10\n'
    at = run(upload(start(), po=po, inv=inv, grn=grn))
    no_errors(at)
    for option in at.selectbox(key="detail_id").options:
        at = at.selectbox(key="detail_id").set_value(option).run()
        for value in (m.value for group in (at.markdown, at.caption, at.info, at.warning, at.error, at.success)
                      for m in group):
            assert "](http" not in value and "<b>html" not in value, (option, value)
        assert not any(m.proto.allow_html and "tracker" in m.value for m in at.markdown)
        assert [e.label for e in at.expander if "tracker" in e.label] == []
    missing_receipt = next(o for o in at.selectbox(key="detail_id").options if "MISSING_GOODS_RECEIPT" in o)
    at = at.selectbox(key="detail_id").set_value(missing_receipt).run()
    tables = investigation_tables(at)
    assert any(hostile in text for text in tables["Observation"]["Observation"])  # displayed as written, in a cell
    assert hostile in set(tables["Ref"]["Reference"])


def test_upload_names_in_the_cited_records_are_shown_without_invisible_characters(offline):
    po = PO_CSV + "P2,1,S1,Supplier One,SKU-2,10,5.00,USD,2026-08-01\n"
    inv = INV_CSV + "I2,2026-08-10,S1,P2,SKU-2,10,5.00,USD\n"
    name = "inv\u202eoices\rcopy.csv"  # a right-to-left override and a carriage return in the upload name
    at = run(upload(start(), po=po, inv=inv, names={"invoices": name}))
    no_errors(at)
    missing_receipt = next(o for o in at.selectbox(key="detail_id").options if "MISSING_GOODS_RECEIPT" in o)
    at = at.selectbox(key="detail_id").set_value(missing_receipt).run()
    files = set(investigation_tables(at)["Ref"]["File"])
    assert "inv oices copy.csv" in files and not any("\u202e" in f or "\r" in f for f in files)

def test_viewing_the_checks_changes_no_finding_or_download(offline):
    at = run(use_sample(start()))
    result = at.session_state["last_run"]["result"]
    fresh = investigate_files(SAMPLE / "purchase_orders.csv", SAMPLE / "invoices.csv", SAMPLE / "goods_received.csv")

    def downloads(r):
        return webapp.exceptions_csv(r), webapp.validation_csv(r), webapp.summary_markdown(r)

    before = downloads(result)  # this run's three downloads, before any finding's checks are shown
    for option in at.selectbox(key="detail_id").options:  # every finding's checks rendered
        at = at.selectbox(key="detail_id").set_value(option).run()
        no_errors(at)
    assert at.session_state["last_run"]["result"] is result and downloads(result) == before
    # The findings and the CSV downloads also match a fresh engine run (its report has another timestamp).
    assert [e.model_dump() for e in result.exceptions] == [e.model_dump() for e in fresh.exceptions]
    assert downloads(result)[:2] == downloads(fresh)[:2]
    assert [d.key for d in at.download_button][-3:] == ["dl_exceptions", "dl_validation", "dl_report"]
    assert metrics(at) == {"Rows read": "62", "Validation issues": "3", "Exceptions": "18",
                           "High severity": "8", "Medium severity": "7", "Low severity": "3"}


def test_checks_work_without_the_ai_sdk_or_a_key_and_with_ai_off(offline, monkeypatch):
    monkeypatch.setattr(ai, "sdk_available", lambda: False)
    monkeypatch.setattr(ai, "explain_exception", lambda *a, **k: pytest.fail("AI used"))
    at = select(run(use_sample(start())), sample_finding_with_checks())
    no_errors(at)
    assert at.toggle(key="ai_enabled").value is False and ai_buttons(at) == []
    assert "Check" in investigation_tables(at)
    at = at.toggle(key="ai_enabled").set_value(True).run()  # AI on but not ready: checks unaffected
    no_errors(at)
    assert webapp.escape_markdown(webapp.AI_STATUS_MESSAGES["sdk_missing"]) in [w.value for w in at.warning]
    assert ai_buttons(at) == [] and "Check" in investigation_tables(at)


def test_the_app_reaches_the_checks_only_through_the_web_helpers():
    source = (REPO / "app.py").read_text(encoding="utf-8")
    assert "opsresolve.investigation" not in source and "import investigation" not in source
    assert source.count("unsafe_allow_html=True") == 2
