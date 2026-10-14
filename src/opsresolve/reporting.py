"""Report rendering. Formats results only; performs no business calculations
beyond counting and grouping already-computed exceptions."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import pandas as pd

from opsresolve.detection import fmt_money, fmt_qty
from opsresolve.models import ExceptionType, Severity
from opsresolve.pipeline import InvestigationResult
from opsresolve.profiles import ImportProfile

# Shown after "sha256" to identify the exact profile file a run used.
PROFILE_HASH_CHARS = 12
# Characters with a meaning in Markdown; the profile name comes from a user-supplied file.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~:$^=&])")


def _summary_rows(result: InvestigationResult) -> list[tuple[str, int, int, int]]:
    ds = result.dataset
    loads = [ds.purchase_orders, ds.invoices, ds.goods_received]
    if ds.uom_conversions is not None:
        loads.append(ds.uom_conversions)
    return [(load.file_label, load.rows_read, len(load.records), load.rows_rejected) for load in loads]


def _amounts_by_type(result: InvestigationResult) -> dict[ExceptionType, dict[str, Decimal]]:
    totals: dict[ExceptionType, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for exc in result.exceptions:
        if exc.currency:
            totals[exc.exception_type][exc.currency] += exc.amount_impact
    return totals


def render_markdown(result: InvestigationResult) -> str:
    out: list[str] = []
    add = out.append
    add("# OpsResolve Exception Report")
    add("")
    add(f"Generated: {result.generated_at:%Y-%m-%d %H:%M UTC}  ")
    cfg = result.config
    add(f"Rules: price tolerance {cfg.price_tolerance_pct}% / {cfg.price_tolerance_abs} absolute; "
        f"quantity tolerance {cfg.quantity_tolerance}; duplicate window {cfg.duplicate_window_days} days.")
    add("")
    if result.profile is not None:
        add(_profile_line(result.profile, _MARKDOWN_SPECIAL.sub(r"\\\1", result.profile.name)))
        add("")
    add("> All figures are calculated by deterministic rules from the input files. "
        "Root causes are not asserted; each exception lists the evidence and a recommended next step.")
    add("")

    add("## Input summary")
    add("")
    add("| File | Rows read | Accepted | Rejected |")
    add("|---|---:|---:|---:|")
    for label, read, accepted, rejected in _summary_rows(result):
        add(f"| {label} | {read} | {accepted} | {rejected} |")
    add("")
    add(f"PO lines matched: {len(result.match.matched)}; "
        f"unmatched invoice lines: {len(result.match.unmatched_invoices)}; "
        f"unmatched receipt lines: {len(result.match.unmatched_receipts)}.")
    add("")

    issues = result.dataset.issues
    add(f"## Validation issues ({len(issues)})")
    add("")
    if issues:
        add("Rejected rows are excluded from matching. Fix these first: they can cause or hide exceptions.")
        add("")
        add("| File | Line | Field | Value | Issue |")
        add("|---|---:|---|---|---|")
        for issue in issues:
            add(f"| {issue.file} | {issue.row if issue.row is not None else '-'} | {issue.field or '-'} "
                f"| {_cell(issue.value)} | {_cell(issue.message)} |")
    else:
        add("None. All rows passed validation.")
    add("")

    exceptions = result.exceptions
    add(f"## Exception summary ({len(exceptions)})")
    add("")
    if not exceptions:
        add("No exceptions detected.")
        return "\n".join(out) + "\n"

    by_severity = Counter(e.severity for e in exceptions)
    add("| Severity | Count |")
    add("|---|---:|")
    for sev in Severity:
        add(f"| {sev.value} | {by_severity.get(sev, 0)} |")
    add("")

    by_type = Counter(e.exception_type for e in exceptions)
    amounts = _amounts_by_type(result)
    add("| Exception type | Count | Net amount impact |")
    add("|---|---:|---:|")
    for exc_type in ExceptionType:
        if by_type.get(exc_type):
            money = ", ".join(fmt_money(v, c) for c, v in sorted(amounts[exc_type].items())) or "-"
            add(f"| {exc_type.value} | {by_type[exc_type]} | {money} |")
    add("")
    add("_Amounts can overlap between related exceptions (e.g. a duplicate invoice also inflates the "
        "invoiced quantity), so they should not be added together across types._")
    add("")

    add("## Exceptions")
    add("")
    for exc in exceptions:
        where = " / ".join(x for x in (exc.po_number, exc.sku) if x) or "-"
        add(f"### {exc.exception_id} · {exc.severity.value} · {exc.exception_type.value}")
        add("")
        add(f"**{exc.summary}**")
        add("")
        add(f"- Supplier: {exc.supplier_id}")
        add(f"- PO / SKU: {where}")
        if exc.expected:
            add(f"- Expected: {exc.expected}")
        if exc.actual:
            add(f"- Actual: {exc.actual}")
        if exc.quantity_difference is not None:
            add(f"- Quantity difference: {fmt_qty(exc.quantity_difference)}")
        add(f"- Amount impact: {fmt_money(exc.amount_impact, exc.currency)}")
        add(f"- Recommended action: {exc.recommended_action}")
        add("- Evidence:")
        for ev in exc.evidence:
            line = f" (line {ev.source_row})" if ev.source_row else ""
            add(f"  - `{ev.source}`{line}: {ev.detail}")
        for note in exc.notes:
            add(f"- Note: {note}")
        add("")
    return "\n".join(out) + "\n"


def _profile_line(profile: ImportProfile, shown_name: str) -> str:
    return f"Import profile: {shown_name} (sha256 {profile.sha256[:PROFILE_HASH_CHARS]})"


def _cell(value: str | None) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def exceptions_frame(result: InvestigationResult) -> pd.DataFrame:
    rows = [{
        "exception_id": e.exception_id,
        "severity": e.severity.value,
        "exception_type": e.exception_type.value,
        "supplier_id": e.supplier_id,
        "po_number": e.po_number or "",
        "sku": e.sku or "",
        "summary": e.summary,
        "expected": e.expected or "",
        "actual": e.actual or "",
        "quantity_difference": "" if e.quantity_difference is None else fmt_qty(e.quantity_difference),
        "amount_impact": f"{e.amount_impact:.2f}",
        "currency": e.currency or "",
        "recommended_action": e.recommended_action,
        "evidence": " | ".join(f"{ev.source}:{ev.reference}" for ev in e.evidence),
        "notes": " | ".join(e.notes),
    } for e in result.exceptions]
    return pd.DataFrame(rows, columns=[
        "exception_id", "severity", "exception_type", "supplier_id", "po_number", "sku", "summary",
        "expected", "actual", "quantity_difference", "amount_impact", "currency",
        "recommended_action", "evidence", "notes"])


def validation_frame(result: InvestigationResult) -> pd.DataFrame:
    return pd.DataFrame(
        [i.model_dump() for i in result.dataset.issues],
        columns=["file", "row", "field", "message", "value"],
    )


def render_console(result: InvestigationResult) -> str:
    out = []
    if result.profile is not None:
        out.append(_profile_line(result.profile, result.profile.name))
    for label, read, accepted, rejected in _summary_rows(result):
        out.append(f"{label:<16} rows={read:<4} accepted={accepted:<4} rejected={rejected}")
    out.append(f"validation issues: {len(result.dataset.issues)}")
    out.append(f"exceptions: {len(result.exceptions)}")
    for exc in result.exceptions:
        out.append(f"  {exc.exception_id} {exc.severity.value:<6} {exc.exception_type.value:<27} "
                   f"{fmt_money(exc.amount_impact, exc.currency):>16}  {exc.summary}")
    return "\n".join(out)


def write_reports(result: InvestigationResult, output_dir: str | Path) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": output_dir / "exception_report.md",
        "exceptions_csv": output_dir / "exceptions.csv",
        "validation_csv": output_dir / "validation_issues.csv",
    }
    paths["markdown"].write_text(render_markdown(result), encoding="utf-8")
    exceptions_frame(result).to_csv(paths["exceptions_csv"], index=False)
    validation_frame(result).to_csv(paths["validation_csv"], index=False)
    return paths
