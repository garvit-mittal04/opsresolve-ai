"""Domain models for OpsResolve.

All monetary amounts and quantities use ``Decimal`` so that discrepancy
calculations are exact. Floats are never used for financial values.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

MONEY_QUANTUM = Decimal("0.01")


def round_money(value: Decimal) -> Decimal:
    """Round a monetary value to cents using commercial (half-up) rounding."""
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="ignore")

    source_row: int = Field(description="Line number in the source CSV file (the header is line 1).")

    @field_validator("currency", check_fields=False)
    @classmethod
    def _currency_code(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("must be a 3-letter ISO 4217 currency code")
        return value


class PurchaseOrderLine(_Record):
    po_number: str = Field(min_length=1)
    line_number: int = Field(ge=1)
    supplier_id: str = Field(min_length=1)
    supplier_name: str = Field(min_length=1)
    sku: str = Field(min_length=1)
    description: str = ""
    quantity_ordered: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    currency: str
    order_date: date

    @property
    def key(self) -> tuple[str, str]:
        return (self.po_number, self.sku)


class InvoiceLine(_Record):
    invoice_number: str = Field(min_length=1)
    invoice_date: date
    supplier_id: str = Field(min_length=1)
    po_number: str = Field(min_length=1)
    sku: str = Field(min_length=1)
    quantity_invoiced: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    currency: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.po_number, self.sku)

    @property
    def line_amount(self) -> Decimal:
        return self.quantity_invoiced * self.unit_price


class GoodsReceiptLine(_Record):
    grn_number: str = Field(min_length=1)
    received_date: date
    supplier_id: str = Field(min_length=1)
    po_number: str = Field(min_length=1)
    sku: str = Field(min_length=1)
    quantity_received: Decimal = Field(ge=0)

    @property
    def key(self) -> tuple[str, str]:
        return (self.po_number, self.sku)


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ExceptionType(str, Enum):
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    SUPPLIER_MISMATCH = "SUPPLIER_MISMATCH"
    MISSING_PURCHASE_ORDER = "MISSING_PURCHASE_ORDER"
    MISSING_GOODS_RECEIPT = "MISSING_GOODS_RECEIPT"
    MISSING_INVOICE = "MISSING_INVOICE"
    POSSIBLE_DUPLICATE_INVOICE = "POSSIBLE_DUPLICATE_INVOICE"


class Evidence(BaseModel):
    """A single fact supporting an exception, traceable to a source row."""

    model_config = ConfigDict(frozen=True)

    source: str  # "purchase_orders" | "invoices" | "goods_received" | "validation"
    reference: str  # e.g. invoice number, GRN number, PO number
    detail: str
    source_row: int | None = None


class OpsException(BaseModel):
    """A detected discrepancy. Every number here is computed deterministically."""

    model_config = ConfigDict(frozen=True)

    exception_id: str
    exception_type: ExceptionType
    severity: Severity
    supplier_id: str
    po_number: str | None
    sku: str | None
    summary: str
    expected: str | None = None
    actual: str | None = None
    quantity_difference: Decimal | None = None
    amount_impact: Decimal = Decimal("0.00")
    currency: str | None = None
    evidence: tuple[Evidence, ...] = ()
    recommended_action: str
    notes: tuple[str, ...] = ()


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    file: str
    row: int | None  # None for file-level issues (e.g. missing column)
    field: str | None
    message: str
    value: str | None = None
