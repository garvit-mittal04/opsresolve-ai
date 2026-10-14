"""OpsResolve local web app.

Run from the repository root:

    pip install -e ".[app]"
    streamlit run app.py

This file only lays out the page. Reconciliation is done by the existing
pipeline through ``opsresolve.webapp``; no business rules live here. Text that
comes from uploaded files is shown in tables or escaped, never as raw Markdown.

Investigation checks (candidate explanations from a fixed catalogue, checked
locally and deterministically against the loaded records) are computed once per
run and shown in the detail panel as table cells and ``st.text``; a failed check
shows its fixed label only.

Optional AI explanations are off until the user turns them on, and a request is
sent only when the user presses the send button for one finding. Text written by
the model is shown literally (``st.text`` and table cells), the request preview
with ``st.code``, and failures with the fixed messages from ``opsresolve.webapp``.
"""

from __future__ import annotations

from pathlib import Path

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
EXAMPLE_PROFILE = Path(__file__).resolve().parent / "docs" / "examples" / "import-profile-example.json"


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


def ai_sidebar() -> webapp.AIStatus | None:
    """Optional AI explanations: off by default. Returns the readiness when the user has turned them on."""
    with st.sidebar:
        st.header("AI explanations")
        if not st.toggle("Enable AI explanations (optional)", value=False, key="ai_enabled"):
            st.caption("Off. Reconciliation never uses AI; explanations are optional and advisory.")
            return None
        status = webapp.ai_status()
        (st.success if status.ready else st.warning)(esc(status.message))
        if status.ready:
            st.caption(f"A request is given up after {status.config.timeout_seconds:g} seconds and retried "
                       f"{status.config.max_retries} time(s).")
        return status


def show_investigation(run: webapp.InvestigationRun | None, exception_id: str, names: dict[str, str]) -> None:
    """The deterministic investigation checks of one finding. Text from the checks and the uploaded files only in
    table cells and st.text; labels and messages are fixed text. A failed check shows its fixed label only."""
    st.markdown("**Investigation checks** (deterministic; candidate explanations, never confirmed causes)")
    if run is None:
        return
    if run.message is not None:
        st.info(esc(run.message))
        return
    view = run.views.get(exception_id)
    if view is None or view.not_run:
        st.warning(esc(webapp.INVESTIGATION_NOT_RUN))
        return
    if not view.checks:
        st.caption(esc(webapp.INVESTIGATION_NONE))
        return
    st.caption("Consistent with the data — not confirmed: the records match the explanation, but a person must "
               "verify it. Contradicted by the data: comparable records exist and none fits. Can't be checked: the "
               "records needed are missing, rejected, in other units or not conclusive.")
    evidence = [check for check in view.checks if not check.failed]
    if evidence:
        st.dataframe(pd.DataFrame([{"Check": c.id, "Candidate explanation": c.statement, "Status": c.label}
                                   for c in evidence]), hide_index=True, width="stretch")
    for check in view.checks:
        if check.failed:  # an operational failure, not evidence: the fixed label only
            st.warning(esc(f"{check.id}: {webapp.CHECK_FAILED_LABEL}"))
    for check in evidence:
        with st.expander(f"{check.id}: details"):
            st.text(f"{check.statement} — {check.label}")
            if check.observations:
                rows = [{"Observation": text, "Records": refs} for text, refs in check.observations]
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            if check.missing:
                st.dataframe(pd.DataFrame({"Needed to decide": list(check.missing)}), hide_index=True, width="stretch")
            st.caption("How to confirm")
            st.text(check.how_to_confirm)
    if view.records:
        st.caption("Records cited by the checks (line numbers in the uploaded files; the header is line 1)")
        st.dataframe(pd.DataFrame([{"Ref": r.ref, "File": webapp.literal_text(names.get(r.file, r.file)), "Line": r.line,
                                    "Reference": r.reference} for r in view.records]),
                     hide_index=True, width="stretch")


def show_ai_view(view: webapp.AIExplanationView) -> None:
    """One AI result. Model-written text only through st.text and table cells; messages are fixed text."""
    if view.status == "accepted":
        st.info(esc(view.message))
        st.text(view.explanation)
        tables = [
            ("Confirmed facts", [{"Fact": i.text, "Evidence": ", ".join(i.refs)} for i in view.confirmed_facts]),
            ("Possible causes (unverified hypotheses)", [
                {"Hypothesis": i.text, "How to check": i.check, "Evidence": ", ".join(i.refs)}
                for i in view.possible_causes]),
            ("Investigation steps", [{"Step": i.text, "Evidence": ", ".join(i.refs)}
                                     for i in view.investigation_steps]),
            ("Missing information", [{"Missing": item} for item in view.missing_information]),
        ]
        for title, rows in tables:
            if rows:
                st.caption(title)
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.text(view.sufficiency)
    elif view.status == "rejected":
        st.warning(esc(view.message))
        st.dataframe(pd.DataFrame([{"Problem": code, "Field": location} for code, location in view.problems]),
                     hide_index=True, width="stretch")
    else:
        (st.info if view.status == "unavailable" else st.error)(esc(view.message))


def show_ai_explanation(result, exc, status: webapp.AIStatus, run_key: str) -> None:
    """The optional AI section of the detail panel. Nothing is sent unless the send button is pressed."""
    st.markdown("**AI explanation** (optional, advisory)")
    if not status.ready:
        st.caption(esc(status.message))
        return
    st.caption("Sending shares this finding's evidence with Anthropic: supplier ids are replaced by pseudonyms and "
               "supplier names and item descriptions are left out, but document numbers, SKUs, quantities, prices, "
               "amounts and dates are sent. The explanation is advisory and changes nothing above.")
    if st.checkbox("Show the exact request that would be sent", key=f"ai_preview_{exc.exception_id}"):
        preview = webapp.preview_ai_request(result, exc, status.config)
        if preview.body is None:
            st.warning(esc(preview.message))
        else:
            st.code(preview.body, language="json")
    views = st.session_state.setdefault("ai_views", {})
    guard = st.session_state.setdefault("ai_guard", webapp.AIRequestGuard())
    key = (run_key, exc.exception_id)
    guard.cancel_waiting(keep=key)  # a send not started for another finding is dropped, never sent later
    if st.button("Send to Anthropic for an explanation", key=f"ai_send_{exc.exception_id}",
                 disabled=guard.is_pending(key)) and guard.mark_pending(key):
        st.rerun()  # show the button disabled while the request runs, so it cannot be pressed again
    if guard.is_running(key):  # being sent by an earlier run of this page; never a second request
        st.info(esc(webapp.AI_REQUEST_IN_PROGRESS))
    elif guard.is_pending(key):
        with st.spinner("Waiting for the AI explanation…"):
            view = guard.run(key, lambda: webapp.request_ai_explanation(result, exc, status.config))
            if view is not None:
                views[key] = view
        st.rerun()  # enable the button again and show the answer
    if key in views:
        show_ai_view(views[key])


def collect_inputs() -> tuple[dict[str, Upload | None], Upload | None]:
    """The data files and the optional import profile (never used with the bundled sample)."""
    st.subheader("1. Input files")
    source = st.radio("Data source", ["Upload my files", "Use bundled sample data"], horizontal=True,
                      key="source", label_visibility="collapsed")
    if source == "Use bundled sample data":
        st.caption("Synthetic sample from `data/sample/` (read only): 3 validation issues and 18 exceptions "
                   "are seeded deliberately.")
        return webapp.sample_uploads(), None

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
    return uploads, collect_profile()


def collect_profile() -> Upload | None:
    file = st.file_uploader("Import profile (optional, .json)", type=["json"], key="upload_profile")
    st.caption("With a valid import profile, files are read in the formats it declares; the "
               "'Expected format' notes describe the built-in format.")
    with st.expander("What is an import profile?"):
        st.markdown("An import profile is a small JSON file that declares how your exports are written: the "
                    "delimiter, text encoding, date format, decimal and thousands separators, and which of your "
                    "column headers holds each field. Nothing is guessed: values that do not match the declared "
                    "format are rejected and listed as validation issues.")
        st.download_button("Download example profile", EXAMPLE_PROFILE.read_bytes(),
                           file_name="import-profile-example.json", mime="application/json",
                           key="example_profile")
    return Upload(file.name, file.getvalue()) if file is not None else None


def show_profile_check(check: webapp.ProfileCheck) -> None:
    if check.valid:
        st.success(webapp.profile_caption(check.profile, "Import profile loaded"))
        return
    st.error(f"The import profile is not valid ({len(check.problems)} problem(s)). Fix or remove it to run.")
    st.dataframe(pd.DataFrame({"Problem": list(check.problems)}), hide_index=True, width="stretch")


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


def show_exceptions(result, ai_status: webapp.AIStatus | None = None, run_key: str = "",
                    investigations: webapp.InvestigationRun | None = None,
                    names: dict[str, str] | None = None) -> None:
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
        show_investigation(investigations, exc.exception_id, names or {})
        if ai_status is not None:
            show_ai_explanation(result, exc, ai_status, run_key)


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
ai_status = ai_sidebar()
uploads, profile_upload = collect_inputs()
profile_check = webapp.check_profile(profile_upload) if profile_upload is not None else None
if profile_check is not None:
    show_profile_check(profile_check)
profile_invalid = profile_check is not None and not profile_check.valid

missing = [spec.label for spec in INPUTS if spec.required and uploads.get(spec.key) is None]
current = webapp.fingerprint(uploads, config, profile_upload) if config is not None else None
stored = st.session_state.get("last_run")
if stored is not None and stored["fingerprint"] != current:
    del st.session_state["last_run"]  # inputs or settings changed: never show results for other data
    st.session_state.pop("ai_views", None)  # and no AI explanation of them either
    stored = None
    st.info("Inputs or settings changed since the last run. Run the reconciliation again to see results.")

st.subheader("2. Run")
if missing:
    st.caption("Waiting for: " + ", ".join(missing) + ".")
if profile_invalid:
    st.caption("Waiting for a valid import profile (or remove it to use the built-in format).")
if st.button("Run reconciliation", type="primary", disabled=bool(missing) or config is None or profile_invalid,
             key="run_button"):
    with st.spinner("Reconciling…"):
        try:
            profile = profile_check.profile if profile_check is not None else None
            result = webapp.run_reconciliation(uploads, config, profile)
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
    if result.profile is not None:  # the profile this result was actually read with
        st.markdown(webapp.profile_caption(result.profile))
    show_summary(webapp.summarize(result, names))
    tab_exc, tab_val, tab_dl = st.tabs([
        f"Exceptions ({len(result.exceptions)})", f"Validation issues ({len(result.dataset.issues)})", "Downloads"])
    with tab_exc:
        # Computed once per run and kept with it; a new run or changed inputs or settings replace them.
        show_exceptions(result, ai_status, stored["fingerprint"], webapp.cached_investigations(stored), names)
    with tab_val:
        show_validation(result, names)
    with tab_dl:
        show_downloads(result)
