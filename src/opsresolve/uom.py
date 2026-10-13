"""Unit-of-measure normalisation.

Runs between matching and detection. For each PO line whose documents state a
unit of measure, invoice and receipt lines are converted into the PO line's
unit so the existing rules compare like with like.

Safety rules (a line that breaks any of them is *unresolved*, reported as
UNIT_OF_MEASURE_MISMATCH, and excluded from quantity and price comparison):

* If any document on a PO line states a unit, every document on it must.
* Conversions come only from the per-SKU conversion table; nothing is inferred
  from descriptions and no unit is assumed to have a standard size.
* Two units convert only through the same base unit for that SKU.
* A converted quantity or price must be an exact decimal; nothing is rounded.

Lines where no document states a unit are left untouched, preserving the
behaviour from before unit support existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, Inexact, localcontext

from opsresolve.detection import fmt_qty
from opsresolve.matching import MatchedLine, MatchResult
from opsresolve.models import GoodsReceiptLine, InvoiceLine, PurchaseOrderLine, UomConversion

LineKey = tuple[str, str]


class ConversionTable:
    """Per-SKU conversion factors to a base unit."""

    def __init__(self, conversions: list[UomConversion] | None = None):
        self._to_base: dict[tuple[str, str], tuple[str, Decimal]] = {}
        for c in conversions or []:
            self._to_base[(c.sku, c.uom)] = (c.base_uom, c.units_per_uom)
            # A base unit converts to itself, unless the table defines it explicitly.
            self._to_base.setdefault((c.sku, c.base_uom), (c.base_uom, Decimal(1)))

    def factors(self, sku: str, from_uom: str, to_uom: str) -> tuple[Decimal, Decimal] | None:
        """Base-unit sizes ``(from, to)`` for this SKU, or None if there is no known conversion.

        ``quantity_in_to = quantity_in_from * from / to``. Both units must share a base unit.
        """
        if from_uom == to_uom:
            return Decimal(1), Decimal(1)
        source, target = self._to_base.get((sku, from_uom)), self._to_base.get((sku, to_uom))
        if source is None or target is None or source[0] != target[0]:
            return None
        return source[1], target[1]

    def describe(self, sku: str, uom: str) -> str | None:
        entry = self._to_base.get((sku, uom))
        if entry is None or entry[0] == uom:
            return None
        return f"1 {uom} = {fmt_qty(entry[1])} {entry[0]}"


def _exact_div(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """numerator / denominator if it is an exact decimal, else None."""
    with localcontext() as ctx:
        ctx.prec = 60
        ctx.traps[Inexact] = True
        try:
            return numerator / denominator
        except Inexact:
            return None


def _exact_mul(a: Decimal, b: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 60
        return a * b


@dataclass(frozen=True)
class UnitIssue:
    """Why a PO line's quantities and prices could not be compared."""

    reason: str
    po_uom: str | None
    document_uoms: tuple[str, ...]  # e.g. ("invoice E05-3201: EA", "GRN 7000003201: none stated")


@dataclass
class UnitNormalization:
    match: MatchResult
    unresolved: dict[LineKey, UnitIssue] = field(default_factory=dict)
    converted_lines: set[LineKey] = field(default_factory=set)


def _documents(line: MatchedLine) -> list[tuple[str, str | None]]:
    docs: list[tuple[str, str | None]] = [(f"PO {line.po_line.po_number}", line.po_line.unit_of_measure)]
    docs += [(f"invoice {i.invoice_number}", i.unit_of_measure) for i in line.invoices]
    docs += [(f"GRN {r.grn_number}", r.unit_of_measure) for r in line.receipts]
    return docs


def _rule(table: ConversionTable, sku: str, *uoms: str | None) -> str:
    rules = [r for r in (table.describe(sku, u) for u in uoms if u) if r]
    return "; ".join(dict.fromkeys(rules))


def _convert_invoice(inv: InvoiceLine, factors: tuple[Decimal, Decimal], po: PurchaseOrderLine,
                     table: ConversionTable) -> InvoiceLine | None:
    size_from, size_to = factors
    quantity = _exact_div(_exact_mul(inv.quantity_invoiced, size_from), size_to)
    price = _exact_div(_exact_mul(inv.unit_price, size_to), size_from)
    if quantity is None or price is None:
        return None
    return inv.model_copy(update={
        "quantity_invoiced": quantity, "unit_price": price, "unit_of_measure": po.unit_of_measure,
        "converted_from": (f"{fmt_qty(inv.quantity_invoiced)} {inv.unit_of_measure} x {inv.unit_price} "
                           f"converted to {po.unit_of_measure} "
                           f"({_rule(table, po.sku, inv.unit_of_measure, po.unit_of_measure)})"),
    })


def _convert_receipt(grn: GoodsReceiptLine, factors: tuple[Decimal, Decimal], po: PurchaseOrderLine,
                     table: ConversionTable) -> GoodsReceiptLine | None:
    size_from, size_to = factors
    quantity = _exact_div(_exact_mul(grn.quantity_received, size_from), size_to)
    if quantity is None:
        return None
    return grn.model_copy(update={
        "quantity_received": quantity, "unit_of_measure": po.unit_of_measure,
        "converted_from": (f"{fmt_qty(grn.quantity_received)} {grn.unit_of_measure} "
                           f"converted to {po.unit_of_measure} "
                           f"({_rule(table, po.sku, grn.unit_of_measure, po.unit_of_measure)})"),
    })


def _normalize_line(line: MatchedLine, table: ConversionTable) -> tuple[MatchedLine, UnitIssue | None, bool]:
    docs = _documents(line)
    stated = [uom for _, uom in docs if uom is not None]
    if not stated:
        return line, None, False  # no units anywhere: unchanged legacy behaviour

    po = line.po_line
    described = tuple(f"{name}: {uom or 'none stated'}" for name, uom in docs)
    missing = [name for name, uom in docs if uom is None]
    if missing:
        return line, UnitIssue(f"unit of measure not stated on {', '.join(missing)}",
                               po.unit_of_measure, described), False

    if all(uom == po.unit_of_measure for uom in stated):
        return line, None, False  # same unit everywhere: nothing to convert

    def no_conversion(uom: str) -> UnitIssue:
        return UnitIssue(f"no known conversion from {uom} to {po.unit_of_measure} for SKU {po.sku}",
                         po.unit_of_measure, described)

    def inexact(what: str, uom: str) -> UnitIssue:
        return UnitIssue(f"{what} in {uom} has no exact equivalent in {po.unit_of_measure}",
                         po.unit_of_measure, described)

    invoices: list[InvoiceLine] = []
    for inv in line.invoices:
        factors = table.factors(po.sku, inv.unit_of_measure, po.unit_of_measure)
        if factors is None:
            return line, no_conversion(inv.unit_of_measure), False
        converted = inv if inv.unit_of_measure == po.unit_of_measure else _convert_invoice(inv, factors, po, table)
        if converted is None:
            return line, inexact(f"invoice {inv.invoice_number} quantity or price", inv.unit_of_measure), False
        invoices.append(converted)

    receipts: list[GoodsReceiptLine] = []
    for grn in line.receipts:
        factors = table.factors(po.sku, grn.unit_of_measure, po.unit_of_measure)
        if factors is None:
            return line, no_conversion(grn.unit_of_measure), False
        converted = grn if grn.unit_of_measure == po.unit_of_measure else _convert_receipt(grn, factors, po, table)
        if converted is None:
            return line, inexact(f"GRN {grn.grn_number} quantity", grn.unit_of_measure), False
        receipts.append(converted)

    return MatchedLine(po_line=po, invoices=invoices, receipts=receipts), None, True


def normalize_units(match: MatchResult, conversions: list[UomConversion] | None = None) -> UnitNormalization:
    """Convert each PO line's documents into the PO line's unit where it is safe to do so."""
    table = ConversionTable(conversions)
    result = UnitNormalization(match=MatchResult(
        matched=[], unmatched_invoices=match.unmatched_invoices,
        unmatched_receipts=match.unmatched_receipts, known_po_numbers=match.known_po_numbers))
    for line in match.matched:
        normalized, issue, converted = _normalize_line(line, table)
        result.match.matched.append(normalized)
        if issue is not None:
            result.unresolved[line.po_line.key] = issue
        if converted:
            result.converted_lines.add(line.po_line.key)
    return result
