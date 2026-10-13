"""Record factories and a detection runner shared by the tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from opsresolve.config import MatchConfig
from opsresolve.detection import RejectedKeys, detect_exceptions
from opsresolve.matching import match_records
from opsresolve.models import ExceptionType, GoodsReceiptLine, InvoiceLine, OpsException, PurchaseOrderLine


def po(po_number="4500000001", sku="SKU-1", qty="100", price="10.00", supplier="SUP-1",
       currency="USD", line=1, row=2, uom=None) -> PurchaseOrderLine:
    return PurchaseOrderLine(
        source_row=row, po_number=po_number, line_number=line, supplier_id=supplier,
        supplier_name="Test Supplier", sku=sku, quantity_ordered=Decimal(qty),
        unit_price=Decimal(price), currency=currency, order_date=date(2026, 8, 1), unit_of_measure=uom)


def inv(number="INV-1", sku="SKU-1", qty="100", price="10.00", po_number="4500000001",
        supplier="SUP-1", currency="USD", on=date(2026, 8, 10), row=2, uom=None) -> InvoiceLine:
    return InvoiceLine(
        source_row=row, invoice_number=number, invoice_date=on, supplier_id=supplier,
        po_number=po_number, sku=sku, quantity_invoiced=Decimal(qty), unit_price=Decimal(price),
        currency=currency, unit_of_measure=uom)


def grn(number="GRN-1", sku="SKU-1", qty="100", po_number="4500000001", supplier="SUP-1",
        on=date(2026, 8, 8), row=2, uom=None) -> GoodsReceiptLine:
    return GoodsReceiptLine(
        source_row=row, grn_number=number, received_date=on, supplier_id=supplier,
        po_number=po_number, sku=sku, quantity_received=Decimal(qty), unit_of_measure=uom)


def run(pos, invoices, receipts, config: MatchConfig | None = None,
        rejected: RejectedKeys | None = None) -> list[OpsException]:
    match = match_records(pos, invoices, receipts)
    return detect_exceptions(match, invoices, config or MatchConfig(), rejected)


def of_type(exceptions: list[OpsException], exc_type: ExceptionType) -> list[OpsException]:
    return [e for e in exceptions if e.exception_type == exc_type]
