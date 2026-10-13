"""Deterministic record matching.

Records are matched on the PO line key ``(po_number, sku)``. A PO line may
have any number of invoice lines and goods receipts (partial deliveries and
split billing are normal). Nothing here is fuzzy or probabilistic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from opsresolve.models import GoodsReceiptLine, InvoiceLine, PurchaseOrderLine


@dataclass
class MatchedLine:
    po_line: PurchaseOrderLine
    invoices: list[InvoiceLine] = field(default_factory=list)
    receipts: list[GoodsReceiptLine] = field(default_factory=list)

    @property
    def total_invoiced(self) -> Decimal:
        return sum((i.quantity_invoiced for i in self.invoices), Decimal("0"))

    @property
    def total_received(self) -> Decimal:
        return sum((r.quantity_received for r in self.receipts), Decimal("0"))


@dataclass
class MatchResult:
    matched: list[MatchedLine]
    unmatched_invoices: list[InvoiceLine]
    unmatched_receipts: list[GoodsReceiptLine]
    known_po_numbers: set[str]


def match_records(
    purchase_orders: list[PurchaseOrderLine],
    invoices: list[InvoiceLine],
    receipts: list[GoodsReceiptLine],
) -> MatchResult:
    """Group invoices and receipts under the PO line they reference.

    Output ordering is deterministic: PO lines in (po_number, line_number)
    order; invoices and receipts in source-file order.
    """
    lines: dict[tuple[str, str], MatchedLine] = {}
    for po in sorted(purchase_orders, key=lambda p: (p.po_number, p.line_number)):
        lines[po.key] = MatchedLine(po_line=po)

    unmatched_invoices: list[InvoiceLine] = []
    for invoice in invoices:
        target = lines.get(invoice.key)
        if target is None:
            unmatched_invoices.append(invoice)
        else:
            target.invoices.append(invoice)

    unmatched_receipts: list[GoodsReceiptLine] = []
    for receipt in receipts:
        target = lines.get(receipt.key)
        if target is None:
            unmatched_receipts.append(receipt)
        else:
            target.receipts.append(receipt)

    return MatchResult(
        matched=list(lines.values()),
        unmatched_invoices=unmatched_invoices,
        unmatched_receipts=unmatched_receipts,
        known_po_numbers={po.po_number for po in purchase_orders},
    )
