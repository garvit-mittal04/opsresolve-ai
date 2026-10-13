"""CSV ingestion and input validation.

This module only turns files into validated domain records plus a list of
validation issues. It contains no matching or business-rule logic.

Invalid rows are *rejected* (excluded from matching) and reported; they are
never silently repaired or guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

import pandas as pd
from pydantic import BaseModel, ValidationError

from opsresolve.models import GoodsReceiptLine, InvoiceLine, PurchaseOrderLine, ValidationIssue

PURCHASE_ORDERS = "purchase_orders"
INVOICES = "invoices"
GOODS_RECEIVED = "goods_received"

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
}

RecordT = TypeVar("RecordT", bound=BaseModel)

# The header occupies line 1, so data row index 0 is line 2.
_HEADER_OFFSET = 2


@dataclass
class LoadResult(Generic[RecordT]):
    file_label: str
    records: list[RecordT] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    rows_read: int = 0
    # (po_number, sku) keys of rejected rows, used to annotate "missing" exceptions
    rejected_keys: set[tuple[str, str]] = field(default_factory=set)

    @property
    def rows_rejected(self) -> int:
        return self.rows_read - len(self.records)


@dataclass
class Dataset:
    purchase_orders: LoadResult[PurchaseOrderLine]
    invoices: LoadResult[InvoiceLine]
    goods_received: LoadResult[GoodsReceiptLine]

    @property
    def issues(self) -> list[ValidationIssue]:
        return self.purchase_orders.issues + self.invoices.issues + self.goods_received.issues


def _read_raw(path: Path, file_label: str, result: LoadResult) -> pd.DataFrame | None:
    if not path.exists():
        result.issues.append(ValidationIssue(
            file=file_label, row=None, field=None, message=f"File not found: {path}"))
        return None
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


def _validate_rows(
    frame: pd.DataFrame, model: type[RecordT], result: LoadResult[RecordT]
) -> list[tuple[RecordT, dict[str, str]]]:
    valid: list[tuple[RecordT, dict[str, str]]] = []
    for index, row in enumerate(frame.to_dict(orient="records")):
        raw = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if all(v in ("", None) for v in raw.values()):
            continue  # blank line
        result.rows_read += 1
        line = index + _HEADER_OFFSET
        payload = {k: v for k, v in raw.items() if v not in ("", None)}
        payload["source_row"] = line
        try:
            valid.append((model.model_validate(payload), raw))
        except ValidationError as exc:
            for err in exc.errors():
                column = str(err["loc"][0]) if err["loc"] else None
                message = "Missing required value" if err["type"] == "missing" else err["msg"]
                result.issues.append(ValidationIssue(
                    file=result.file_label, row=line, field=column,
                    message=f"{message}; row rejected", value=raw.get(column) if column else None))
            _remember_rejected_key(raw, result)
    return valid


def _remember_rejected_key(raw: dict[str, str], result: LoadResult) -> None:
    po, sku = raw.get("po_number"), raw.get("sku")
    if po and sku:
        result.rejected_keys.add((po, sku))


def _reject(result: LoadResult, record, raw: dict[str, str], field_name: str, message: str) -> None:
    result.issues.append(ValidationIssue(
        file=result.file_label, row=record.source_row, field=field_name,
        message=f"{message}; row rejected", value=raw.get(field_name)))
    _remember_rejected_key(raw, result)


def load_purchase_orders(path: str | Path) -> LoadResult[PurchaseOrderLine]:
    result: LoadResult[PurchaseOrderLine] = LoadResult(PURCHASE_ORDERS)
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


def load_invoices(path: str | Path) -> LoadResult[InvoiceLine]:
    # Repeated invoices are a business exception (duplicate detection), not a
    # validation error, so they are deliberately kept here.
    result: LoadResult[InvoiceLine] = LoadResult(INVOICES)
    frame = _read_raw(Path(path), INVOICES, result)
    if frame is None:
        return result
    result.records = [record for record, _ in _validate_rows(frame, InvoiceLine, result)]
    return result


def load_goods_received(path: str | Path) -> LoadResult[GoodsReceiptLine]:
    result: LoadResult[GoodsReceiptLine] = LoadResult(GOODS_RECEIVED)
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


def load_dataset(po_path: str | Path, invoice_path: str | Path, grn_path: str | Path) -> Dataset:
    return Dataset(
        purchase_orders=load_purchase_orders(po_path),
        invoices=load_invoices(invoice_path),
        goods_received=load_goods_received(grn_path),
    )
