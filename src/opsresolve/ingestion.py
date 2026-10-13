"""CSV ingestion and input validation.

This module only turns files into validated domain records plus a list of
validation issues. It contains no matching or business-rule logic.

Invalid rows are *rejected* (excluded from matching) and reported; they are
never silently repaired or guessed at.

Every loader takes an optional ``FileFormat`` from an import profile. Without
one (the default) files are read exactly as before: comma-delimited UTF-8 with
canonical column names. With one, the declared encoding, delimiter, column
headers, number format and date format are applied strictly; nothing is
detected or guessed.
"""

from __future__ import annotations

import codecs
import io
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable, Generic, TypeVar

import pandas as pd
from pydantic import BaseModel, ValidationError

from opsresolve.models import GoodsReceiptLine, InvoiceLine, PurchaseOrderLine, UomConversion, ValidationIssue
from opsresolve.profiles import (
    DELIMITERS,
    FileFormat,
    ImportProfile,
    canonical_fields,
    date_fields,
    numeric_fields,
    parse_date,
    parse_number,
    preview,
    required_fields,
)

PURCHASE_ORDERS = "purchase_orders"
INVOICES = "invoices"
GOODS_RECEIVED = "goods_received"
UOM_CONVERSIONS = "uom_conversions"

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    PURCHASE_ORDERS: (
        "po_number", "line_number", "supplier_id", "supplier_name", "sku",
        "quantity_ordered", "unit_price", "currency", "order_date",
    ),
    INVOICES: (
        "invoice_number", "invoice_date", "supplier_id", "po_number", "sku",
        "quantity_invoiced", "unit_price", "currency",
    ),
    GOODS_RECEIVED: (
        "grn_number", "received_date", "supplier_id", "po_number", "sku", "quantity_received",
    ),
    UOM_CONVERSIONS: ("sku", "uom", "base_uom", "units_per_uom"),
}

RecordT = TypeVar("RecordT", bound=BaseModel)

# The header occupies line 1, so data row index 0 is line 2.
_HEADER_OFFSET = 2

# With an import profile, longer cell values are shortened in validation issues.
MAX_ISSUE_VALUE_CHARS = 200


@dataclass
class LoadResult(Generic[RecordT]):
    file_label: str
    records: list[RecordT] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    rows_read: int = 0
    # (po_number, sku) keys of rejected rows, used to annotate "missing" exceptions
    rejected_keys: set[tuple[str, str]] = field(default_factory=set)
    # The import profile's format for this file; None means the built-in format (no profile).
    file_format: FileFormat | None = None

    @property
    def rows_rejected(self) -> int:
        return self.rows_read - len(self.records)


@dataclass
class Dataset:
    purchase_orders: LoadResult[PurchaseOrderLine]
    invoices: LoadResult[InvoiceLine]
    goods_received: LoadResult[GoodsReceiptLine]
    # None when no conversion file was supplied (the default, pre-UoM behaviour).
    uom_conversions: LoadResult[UomConversion] | None = None

    @property
    def issues(self) -> list[ValidationIssue]:
        issues = self.purchase_orders.issues + self.invoices.issues + self.goods_received.issues
        if self.uom_conversions is not None:
            issues += self.uom_conversions.issues
        return issues


def _read_raw(path: Path, file_label: str, result: LoadResult) -> pd.DataFrame | None:
    if not path.exists():
        result.issues.append(ValidationIssue(
            file=file_label, row=None, field=None, message=f"File not found: {path}"))
        return None
    if result.file_format is not None:
        return _read_with_format(path, result.file_format, result)
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False, skip_blank_lines=False)
    except pd.errors.EmptyDataError:
        result.issues.append(ValidationIssue(
            file=file_label, row=None, field=None, message="File is empty (no header row)"))
        return None
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        result.issues.append(ValidationIssue(
            file=file_label, row=None, field=None, message=f"File could not be parsed: {exc}"))
        return None

    frame.columns = [str(c).strip().lower() for c in frame.columns]
    missing = [c for c in REQUIRED_COLUMNS[file_label] if c not in frame.columns]
    if missing:
        for column in missing:
            result.issues.append(ValidationIssue(
                file=file_label, row=None, field=column,
                message="Required column is missing; no rows from this file were loaded"))
        return None
    return frame


def _file_issue(result: LoadResult, message: str, field_name: str | None = None) -> None:
    result.issues.append(ValidationIssue(file=result.file_label, row=None, field=field_name, message=message))


def _read_with_format(path: Path, fmt: FileFormat, result: LoadResult) -> pd.DataFrame | None:
    """Read a file in the format declared by an import profile.

    Returns a frame whose columns are the canonical field names (other columns are
    kept under positional names so blank-line detection sees every cell), or None
    after reporting a file-level issue.
    """
    not_loaded = "; no rows from this file were loaded"
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8) and fmt.encoding != "utf-8":
        _file_issue(result, "File starts with a UTF-8 byte-order mark, but the import profile declares "
                            f"encoding {fmt.encoding!r}{not_loaded}")
        return None
    try:
        text = data.decode("utf-8-sig" if fmt.encoding == "utf-8" else fmt.codec)
    except UnicodeDecodeError as exc:
        # utf-8-sig counts from after the byte-order mark; report the position in the file.
        offset = exc.start + (len(codecs.BOM_UTF8) if data.startswith(codecs.BOM_UTF8) else 0)
        _file_issue(result, f"File is not valid {fmt.encoding} text (byte {offset} cannot be decoded); "
                            f"check the encoding declared in the import profile{not_loaded}")
        return None
    try:
        # header=None: the header row is read as data, so repeated headers stay visible
        # and a row with more cells than the header is a parse error, never an index column.
        table = pd.read_csv(io.StringIO(text), sep=fmt.delimiter, header=None, dtype=str,
                            keep_default_na=False, skip_blank_lines=False)
    except pd.errors.EmptyDataError:
        _file_issue(result, "File has no header row (it is empty or its first line is blank)")
        return None
    except pd.errors.ParserError as exc:
        _file_issue(result, f"File could not be parsed with delimiter {fmt.delimiter!r}: {exc}")
        return None

    headers = [str(h).strip() for h in table.iloc[0]]
    positions: dict[str, list[int]] = {}
    for index, header in enumerate(headers):
        positions.setdefault(header.casefold(), []).append(index)

    required = required_fields(result.file_label)
    selected: dict[int, str] = {}  # column position -> canonical field
    ok = True
    for name in canonical_fields(result.file_label):
        header = fmt.source_header(name)
        mapped = name in fmt.columns
        found = positions.get(header.casefold(), [])
        problem = None
        if not found:
            if mapped:
                problem = f"Column {preview(header)} (mapped by the import profile) is missing"
            elif name in required:
                problem = "Required column is missing"
        elif mapped and header.casefold() != name.casefold() and name.casefold() in positions:
            problem = (f"Both the mapped column {preview(header)} and a column named {name!r} are present; "
                       "the column to read is ambiguous")
        elif len(found) > 1:
            problem = f"Column {preview(header)} appears {len(found)} times; the column to read is ambiguous"
        else:
            selected[found[0]] = name
        if problem is not None:
            ok = False
            _file_issue(result, problem + not_loaded, name)
    if not ok:
        _hint_wrong_delimiter(headers, fmt, result)
        return None

    frame = table.iloc[1:].reset_index(drop=True)
    frame.columns = [selected.get(index, f"#{index + 1}") for index in range(len(headers))]
    return frame


def _hint_wrong_delimiter(headers: list[str], fmt: FileFormat, result: LoadResult) -> None:
    """Point at the delimiter when the header was read as one column containing another delimiter."""
    if len(headers) != 1:
        return
    others = [d for d in DELIMITERS if d != fmt.delimiter and d in headers[0]]
    if others:
        _file_issue(result, f"The header was read as a single column using the declared delimiter "
                            f"{fmt.delimiter!r}, but it contains {', '.join(repr(d) for d in others)}; "
                            "check the delimiter declared in the import profile")


def _issue_value(result: LoadResult, value: str | None) -> str | None:
    """The value shown in an issue; shortened only for profile imports (no-profile output is unchanged)."""
    if result.file_format is None or value is None or len(value) <= MAX_ISSUE_VALUE_CHARS:
        return value
    return f"{value[:MAX_ISSUE_VALUE_CHARS]}... [{len(value)} characters; shortened]"


def _value_parsers(result: LoadResult) -> dict[str, Callable[[str], str]]:
    """Canonical field -> parser converting the declared number/date format to canonical text."""
    fmt, label = result.file_format, result.file_label
    numbers, dates = numeric_fields(label), date_fields(label)
    parsers: dict[str, Callable[[str], str]] = {}
    for name in canonical_fields(label):
        if name in numbers:
            parsers[name] = partial(parse_number, decimal_separator=fmt.decimal_separator,
                                    thousands_separator=fmt.thousands_separator)
        elif name in dates:
            parsers[name] = partial(parse_date, date_format=fmt.date_format)
    return parsers


def _parse_declared_values(
    payload: dict[str, object], raw: dict[str, str], parsers: dict[str, Callable[[str], str]],
    model: type[BaseModel], result: LoadResult, line: int,
) -> bool:
    """Convert numbers and dates in place. On failure, report every problem in the row and return False."""
    failed: dict[str, str] = {}
    for name, parse in parsers.items():
        if name in payload:
            try:
                payload[name] = parse(payload[name])
            except ValueError as exc:
                failed[name] = str(exc)
    if not failed:
        return True

    problems = dict(failed)
    for name in failed:
        del payload[name]
    try:
        model.model_validate(payload)  # to report the row's other problems too
    except ValidationError as exc:
        for err in exc.errors():
            column = str(err["loc"][0]) if err["loc"] else None
            if column in failed:
                continue  # already reported as a format problem; it is "missing" only because it was removed
            problems.setdefault(column, "Missing required value" if err["type"] == "missing" else err["msg"])
    order = {name: index for index, name in enumerate(canonical_fields(result.file_label))}
    for column in sorted(problems, key=lambda c: (c is None, order.get(c, len(order)), c or "")):
        result.issues.append(ValidationIssue(
            file=result.file_label, row=line, field=column, message=f"{problems[column]}; row rejected",
            value=_issue_value(result, raw.get(column)) if column else None))
    return False


def _validate_rows(
    frame: pd.DataFrame, model: type[RecordT], result: LoadResult[RecordT]
) -> list[tuple[RecordT, dict[str, str]]]:
    valid: list[tuple[RecordT, dict[str, str]]] = []
    parsers = _value_parsers(result) if result.file_format is not None else None
    for index, row in enumerate(frame.to_dict(orient="records")):
        raw = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if all(v in ("", None) for v in raw.values()):
            continue  # blank line
        result.rows_read += 1
        line = index + _HEADER_OFFSET
        payload = {k: v for k, v in raw.items() if v not in ("", None)}
        payload["source_row"] = line
        if parsers is not None and not _parse_declared_values(payload, raw, parsers, model, result, line):
            _remember_rejected_key(raw, result)
            continue
        try:
            valid.append((model.model_validate(payload), raw))
        except ValidationError as exc:
            for err in exc.errors():
                column = str(err["loc"][0]) if err["loc"] else None
                message = "Missing required value" if err["type"] == "missing" else err["msg"]
                result.issues.append(ValidationIssue(
                    file=result.file_label, row=line, field=column,
                    message=f"{message}; row rejected",
                    value=_issue_value(result, raw.get(column)) if column else None))
            _remember_rejected_key(raw, result)
    return valid


def _remember_rejected_key(raw: dict[str, str], result: LoadResult) -> None:
    po, sku = raw.get("po_number"), raw.get("sku")
    if po and sku:
        result.rejected_keys.add((po, sku))


def _reject(result: LoadResult, record, raw: dict[str, str], field_name: str, message: str) -> None:
    result.issues.append(ValidationIssue(
        file=result.file_label, row=record.source_row, field=field_name,
        message=f"{message}; row rejected", value=_issue_value(result, raw.get(field_name))))
    _remember_rejected_key(raw, result)


def load_purchase_orders(path: str | Path, file_format: FileFormat | None = None) -> LoadResult[PurchaseOrderLine]:
    result: LoadResult[PurchaseOrderLine] = LoadResult(PURCHASE_ORDERS, file_format=file_format)
    frame = _read_raw(Path(path), PURCHASE_ORDERS, result)
    if frame is None:
        return result

    seen_lines: dict[tuple[str, int], int] = {}
    seen_skus: dict[tuple[str, str], int] = {}
    po_supplier: dict[str, str] = {}
    for record, raw in _validate_rows(frame, PurchaseOrderLine, result):
        line_key = (record.po_number, record.line_number)
        if line_key in seen_lines:
            _reject(result, record, raw, "line_number",
                    f"Duplicate PO line {record.po_number}/{record.line_number} "
                    f"(first seen on line {seen_lines[line_key]})")
            continue
        if record.key in seen_skus:
            _reject(result, record, raw, "sku",
                    f"SKU {record.sku} appears more than once on PO {record.po_number} "
                    f"(first seen on line {seen_skus[record.key]}); matching would be ambiguous")
            continue
        expected_supplier = po_supplier.setdefault(record.po_number, record.supplier_id)
        if record.supplier_id != expected_supplier:
            _reject(result, record, raw, "supplier_id",
                    f"PO {record.po_number} has conflicting supplier ids "
                    f"({expected_supplier} vs {record.supplier_id})")
            continue
        seen_lines[line_key] = record.source_row
        seen_skus[record.key] = record.source_row
        result.records.append(record)
    return result


def load_invoices(path: str | Path, file_format: FileFormat | None = None) -> LoadResult[InvoiceLine]:
    # Repeated invoices are a business exception (duplicate detection), not a
    # validation error, so they are deliberately kept here.
    result: LoadResult[InvoiceLine] = LoadResult(INVOICES, file_format=file_format)
    frame = _read_raw(Path(path), INVOICES, result)
    if frame is None:
        return result
    result.records = [record for record, _ in _validate_rows(frame, InvoiceLine, result)]
    return result


def load_goods_received(path: str | Path, file_format: FileFormat | None = None) -> LoadResult[GoodsReceiptLine]:
    result: LoadResult[GoodsReceiptLine] = LoadResult(GOODS_RECEIVED, file_format=file_format)
    frame = _read_raw(Path(path), GOODS_RECEIVED, result)
    if frame is None:
        return result

    seen: dict[tuple[str, str, str], int] = {}
    for record, raw in _validate_rows(frame, GoodsReceiptLine, result):
        key = (record.grn_number, record.po_number, record.sku)
        if key in seen:
            _reject(result, record, raw, "grn_number",
                    f"GRN {record.grn_number} records SKU {record.sku} for PO {record.po_number} "
                    f"more than once (first seen on line {seen[key]})")
            continue
        seen[key] = record.source_row
        result.records.append(record)
    return result


def load_uom_conversions(path: str | Path, file_format: FileFormat | None = None) -> LoadResult[UomConversion]:
    """Load per-SKU unit conversions.

    Ambiguity is never resolved by picking a row: if a (sku, uom) pair is defined
    more than once with a different base unit or factor, every row for that pair
    is rejected, so the unit is treated as unconvertible. Exact repeats are
    rejected as duplicates with the first row kept.
    """
    result: LoadResult[UomConversion] = LoadResult(UOM_CONVERSIONS, file_format=file_format)
    frame = _read_raw(Path(path), UOM_CONVERSIONS, result)
    if frame is None:
        return result

    by_pair: dict[tuple[str, str], list[tuple[UomConversion, dict[str, str]]]] = {}
    for record, raw in _validate_rows(frame, UomConversion, result):
        if record.uom == record.base_uom and record.units_per_uom != 1:
            _reject(result, record, raw, "units_per_uom",
                    f"{record.uom} is its own base unit, so units_per_uom must be 1")
            continue
        by_pair.setdefault((record.sku, record.uom), []).append((record, raw))

    for (sku, uom), rows in by_pair.items():
        first = rows[0][0]
        if any((r.base_uom, r.units_per_uom) != (first.base_uom, first.units_per_uom) for r, _ in rows):
            for record, raw in rows:
                _reject(result, record, raw, "units_per_uom",
                        f"Conflicting conversions for SKU {sku} unit {uom}; conversion is ambiguous")
            continue
        for record, raw in rows[1:]:
            _reject(result, record, raw, "uom",
                    f"Duplicate conversion for SKU {sku} unit {uom} (first seen on line {first.source_row})")
        result.records.append(first)
    result.records.sort(key=lambda r: r.source_row)
    return result


def load_dataset(
    po_path: str | Path,
    invoice_path: str | Path,
    grn_path: str | Path,
    uom_conversions_path: str | Path | None = None,
    profile: ImportProfile | None = None,
) -> Dataset:
    """Load all input files; ``profile`` (optional) declares their format, otherwise the built-in one is used."""
    def fmt(label: str) -> FileFormat | None:
        return None if profile is None else profile.format_for(label)

    return Dataset(
        purchase_orders=load_purchase_orders(po_path, fmt(PURCHASE_ORDERS)),
        invoices=load_invoices(invoice_path, fmt(INVOICES)),
        goods_received=load_goods_received(grn_path, fmt(GOODS_RECEIVED)),
        uom_conversions=None if uom_conversions_path is None
        else load_uom_conversions(uom_conversions_path, fmt(UOM_CONVERSIONS)),
    )
