"""OpsResolve local web app.

Run from the repository root:

    pip install -e ".[app]"
    streamlit run app.py

This file only lays out the page. Reconciliation is done by the existing
pipeline through ``opsresolve.webapp``; no business rules live here. Text that
comes from uploaded files is shown in tables or escaped, never as raw Markdown.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from opsresolve import webapp
from opsresolve.config import MatchConfig
from opsresolve.detection import fmt_money, fmt_qty
from opsresolve.models import ExceptionType, Severity
from opsresolve.webapp import INPUTS, MissingInputError, Upload, escape_markdown as esc

st.set_page_config(page_title="OpsResolve", layout="wide")

# Fixed styling only; no uploaded content is ever placed inside this HTML.
st.markdown(
    """
    <style>
      .block-container {padding-top: 2rem; max-width: 1200px;}
      .ops-badge {display:inline-block; padding:0.1rem 0.55rem; border-radius:0.25rem;
                  font-size:0.78rem; font-weight:600; letter-spacing:0.03em; border:1px solid;}
      .ops-HIGH {color:#8a1c1c; background:#fbeaea; border-color:#e6b8b8;}
      .ops-MEDIUM {color:#7a4b00; background:#fdf3e1; border-color:#ecd3a3;}
      .ops-LOW {color:#3b4a5a; background:#eef1f4; border-color:#cdd5de;}
    </style>
    """,
    unsafe_allow_html=True,
)

SEVERITY_ORDER = [Severity.HIGH, Severity.MEDIUM, Severity.LOW]


def badge(severity: Severity) -> str:
    return f'<span class="ops-badge ops-{severity.value}">{severity.value}</span>'


def current_config() -> MatchConfig | None:
    """Rule settings: defaults come from OPSRESOLVE_* variables, exactly as for the CLI."""
    try:
        defaults = MatchConfig.from_env()
    except ValidationError as exc:
        st.error(f"Invalid OPSRESOLVE_* environment configuration: {exc.error_count()} problem(s). "
                 "Fix or unset these variables and restart the app.")
        return None
    with st.sidebar:
        st.header("Rule settings")
        st.caption("Defaults are strict. Changing a setting clears previous results.")
        values: dict[str, float | int] = {}
        decimals: dict[str, int] = {}
        for spec in webapp.SETTINGS:
            default = getattr(defaults, spec.field)
            if spec.integer:
                values[spec.field] = st.number_input(spec.label, min_value=0, value=int(default),
                                                     step=int(spec.step), key=f"cfg_{spec.field}")
            else:
                places = decimals[spec.field] = webapp.setting_decimals(spec, default)
                values[spec.field] = st.number_input(spec.label, min_value=0.0, value=float(default),
                                                     step=spec.step, format=f"%.{places}f",
                                                     key=f"cfg_{spec.field}")
    try:
        # Values are rounded to the precision shown, so the engine gets exactly what is displayed.
        return webapp.config_from_inputs(values, decimals)
    except ValidationError as exc:
        st.sidebar.error(f"Invalid rule settings: {exc.error_count()} problem(s).")
        return None


def collect_inputs() -> dict[str, Upload | None]:
    st.subheader("1. Input files")
    source = st.radio("Data source", ["Upload my files", "Use bundled sample data"], horizontal=True,
                      key="source", label_visibility="collapsed")
    if source == "Use bundled sample data":
        st.caption("Synthetic sample from `data/sample/` (read only): 3 validation issues and 18 exceptions "
                   "are seeded deliberately.")
        return webapp.sample_uploads()

    uploads: dict[str, Upload | None] = {}
    # Lay the grid out row by row, so narrow screens stack the files in reading order.
    cells = [cell for start in range(0, len(INPUTS), 2) for cell in st.columns(2)]
    for spec, cell in zip(INPUTS, cells):
        with cell:
            tag = "required" if spec.required else "optional"
            file = st.file_uploader(f"{spec.label} ({tag})", type=["csv"], key=f"upload_{spec.key}")
            uploads[spec.key] = Upload(file.name, file.getvalue()) if file is not None else None
            with st.expander("Expected format"):
                st.caption(spec.description)
                st.markdown("**Required columns:** " + ", ".join(f"`{c}`" for c in spec.required_columns))
                if spec.optional_columns:
                    st.markdown("**Optional columns:** " + ", ".join(f"`{c}`" for c in spec.optional_columns))
                st.caption("Dates as YYYY-MM-DD; numbers with a `.` decimal point and no thousands separators.")
                st.download_button("Download template", webapp.template_csv(spec.key),
                                   file_name=f"{spec.key}_template.csv", mime="text/csv",
                                   key=f"template_{spec.key}")
    return uploads


def show_summary(summary: webapp.RunSummary) -> None:
    {"success": st.success, "warning": st.warning, "error": st.error}[summary.status_level](summary.status_message)
    cols = st.columns(6)
    cols[0].metric("Rows read", sum(f.rows_read for f in summary.files))
    cols[1].metric("Validation issues", summary.validation_issue_count)
    cols[2].metric("Exceptions", summary.exception_count)
    for col, severity in zip(cols[3:], SEVERITY_ORDER):
        col.metric(f"{severity.value.title()} severity", summary.by_severity[severity])

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Rows by file**")
        st.dataframe(pd.DataFrame([
            {"File": f.label, "Uploaded as": f.display_name, "Rows read": f.rows_read,
             "Accepted": f.accepted, "Rejected": f.rejected} for f in summary.files]),
            hide_index=True, width="stretch")
    with right:
        st.markdown("**Exceptions by type**")
        if summary.by_type:
            st.dataframe(pd.DataFrame([{"Exception type": t.value, "Count": n} for t, n in summary.by_type.items()]),
                         hide_index=True, width="stretch")
        else:
            st.caption("No exceptions detected.")


def show_exceptions(result) -> None:
    exceptions = result.exceptions
    if not exceptions:
        st.info("No exceptions were detected by the current rules.")
        return
    if any(e.exception_type is ExceptionType.POSSIBLE_DUPLICATE_INVOICE for e in exceptions):
        st.warning("Possible duplicate invoices are alerts for human review, not confirmed duplicates. "
                   "Compare the documents with the supplier before rejecting an invoice.")

    present_types = [t for t in ExceptionType if any(e.exception_type is t for e in exceptions)]
    f1, f2, f3 = st.columns([1, 2, 2])
    severities = f1.multiselect("Severity", [s.value for s in SEVERITY_ORDER], key="filter_severity")
    types = f2.multiselect("Exception type", [t.value for t in present_types], key="filter_type")
    query = f3.text_input("Search", placeholder="PO, SKU, supplier, invoice or GRN number…", key="filter_query")
    shown = webapp.filter_exceptions(exceptions, {Severity(s) for s in severities},
                                     {ExceptionType(t) for t in types}, query)
    st.caption(f"Showing {len(shown)} of {len(exceptions)} exceptions, ordered by severity and amount.")
    if not shown:
        st.info("No exceptions match these filters.")
        return

    st.dataframe(pd.DataFrame([{
        "ID": e.exception_id, "Severity": e.severity.value, "Type": e.exception_type.value,
        "Supplier": e.supplier_id, "PO": e.po_number or "", "SKU": e.sku or "",
        "Amount impact": fmt_money(e.amount_impact, e.currency), "Summary": e.summary,
    } for e in shown]), hide_index=True, width="stretch")

    by_id = {e.exception_id: e for e in shown}
    chosen = st.selectbox("Inspect a finding", list(by_id), key="detail_id",
                          format_func=lambda i: f"{i} · {by_id[i].exception_type.value}")
    exc = by_id[chosen]
    with st.container(border=True):
        st.markdown(f"{badge(exc.severity)} &nbsp; **{exc.exception_id} · {exc.exception_type.value}**",
                    unsafe_allow_html=True)
        st.markdown(esc(exc.summary))
        details = {
            "Supplier": exc.supplier_id, "PO number": exc.po_number or "–", "SKU": exc.sku or "–",
            "Expected": exc.expected or "–", "Actual": exc.actual or "–",
            "Quantity difference": fmt_qty(exc.quantity_difference) if exc.quantity_difference is not None else "–",
            "Amount impact": fmt_money(exc.amount_impact, exc.currency),
        }
        st.dataframe(pd.DataFrame({"Field": list(details), "Value": list(details.values())}),
                     hide_index=True, width="stretch")
        st.markdown("**Recommended next step**")
        st.markdown(esc(exc.recommended_action))
        st.markdown("**Evidence** (source rows from the uploaded files)")
        st.dataframe(pd.DataFrame([{"File": ev.source, "Line": ev.source_row, "Reference": ev.reference,
                                    "Detail": ev.detail} for ev in exc.evidence]),
                     hide_index=True, width="stretch")
        if exc.notes:
            st.markdown("**Notes from the engine**")
            for note in exc.notes:
                st.markdown("- " + esc(note))


def show_validation(result, names: dict[str, str]) -> None:
    issues = result.dataset.issues
    if not issues:
        st.success("No validation issues: every row passed input validation.")
        return
    st.markdown("Rows listed here were **rejected and excluded from matching**. A rejected row can cause or hide "
                "exceptions, so correct these first and run again. *Line* is the line number in the CSV file "
                "(the header is line 1); a blank line means the whole file was affected.")
    st.dataframe(pd.DataFrame([{
        "File": names.get(i.file, i.file), "Line": i.row, "Field": i.field or "", "Value": i.value or "",
        "Problem": i.message} for i in issues]), hide_index=True, width="stretch")


def show_downloads(result) -> None:
    st.caption("Files are generated from the current run only. In the CSV files, cells that would start a "
               "spreadsheet formula (=, +, -, @) are prefixed with an apostrophe; plain numbers are unchanged.")
    c1, c2, c3 = st.columns(3)
    c1.download_button("Exceptions (CSV)", webapp.exceptions_csv(result), "opsresolve_exceptions.csv",
                       "text/csv", key="dl_exceptions", width="stretch")
    c2.download_button("Validation issues (CSV)", webapp.validation_csv(result), "opsresolve_validation_issues.csv",
                       "text/csv", key="dl_validation", width="stretch")
    c3.download_button("Summary report (Markdown)", webapp.summary_markdown(result), "opsresolve_report.md",
                       "text/markdown", key="dl_report", width="stretch")


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

st.title("OpsResolve")
st.markdown("Three-way match of **purchase orders**, **supplier invoices** and **goods receipts**. Deterministic "
            "rules flag discrepancies with their evidence and a recommended next step.")
st.caption("Findings are reconciliation exceptions for human review, not approvals, payment decisions or fraud "
           "determinations. This tool has not been validated on real supplier data.")

config = current_config()
uploads = collect_inputs()

missing = [spec.label for spec in INPUTS if spec.required and uploads.get(spec.key) is None]
current = webapp.fingerprint(uploads, config) if config is not None else None
stored = st.session_state.get("last_run")
if stored is not None and stored["fingerprint"] != current:
    del st.session_state["last_run"]  # inputs or settings changed: never show results for other data
    stored = None
    st.info("Inputs or settings changed since the last run. Run the reconciliation again to see results.")

st.subheader("2. Run")
if missing:
    st.caption("Waiting for: " + ", ".join(missing) + ".")
if st.button("Run reconciliation", type="primary", disabled=bool(missing) or config is None, key="run_button"):
    with st.spinner("Reconciling…"):
        try:
            result = webapp.run_reconciliation(uploads, config)
        except MissingInputError as exc:
            st.error(str(exc))
        except Exception as exc:  # unexpected engine failure: report it, keep no partial results
            st.error(f"Processing failed unexpectedly ({type(exc).__name__}). No results were produced.")
        else:
            stored = {"fingerprint": current, "result": result}
            st.session_state["last_run"] = stored

st.subheader("3. Results")
if stored is None:
    st.caption("Results appear here after a run.")
else:
    result = stored["result"]
    # The fingerprint matched, so the contents are the ones this result was computed from; take the
    # file names from the current uploads (the same bytes may have been re-uploaded under a new name).
    names = webapp.display_names(uploads)
    show_summary(webapp.summarize(result, names))
    tab_exc, tab_val, tab_dl = st.tabs([
        f"Exceptions ({len(result.exceptions)})", f"Validation issues ({len(result.dataset.issues)})", "Downloads"])
    with tab_exc:
        show_exceptions(result)
    with tab_val:
        show_validation(result, names)
    with tab_dl:
        show_downloads(result)
