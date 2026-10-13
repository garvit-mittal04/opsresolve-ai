"""Exception detection rules.

Every rule here is deterministic: given the same inputs and configuration it
produces the same exceptions with the same amounts. No LLM or statistical
model is involved in any calculation.

Rules state *what* differs and *by how much*. They do not assert a root
cause; recommended actions describe how to investigate and resolve.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from opsresolve.config import MatchConfig
from opsresolve.matching import MatchedLine, MatchResult
from opsresolve.models import (
    Evidence,
    ExceptionType,
    GoodsReceiptLine,
    InvoiceLine,
    OpsException,
    Severity,
    round_money,
)

ZERO = Decimal("0")

_REJECTED_NOTE = (
    "A {file} row for PO {po} / SKU {sku} was rejected during input validation. "
    "This exception may be a consequence of that data error; review the validation issues first."
)


def fmt_qty(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def fmt_money(value: Decimal, currency: str | None) -> str:
    amount = f"{round_money(value):,.2f}"
    return f"{amount} {currency}" if currency else amount


@dataclass
class RejectedKeys:
    """(po_number, sku) keys of rows rejected by validation, per source file."""

    purchase_orders: set[tuple[str, str]] = field(default_factory=set)
    invoices: set[tuple[str, str]] = field(default_factory=set)
    goods_received: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class _Draft:
    """An exception before its stable id is assigned."""

    exception_type: ExceptionType
    severity: Severity
    supplier_id: str
    po_number: str | None
    sku: str | None
    summary: str
    recommended_action: str
    expected: str | None = None
    actual: str | None = None
    quantity_difference: Decimal | None = None
    amount_impact: Decimal = ZERO
    currency: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def severity_for_amount(amount: Decimal, config: MatchConfig, minimum: Severity = Severity.LOW) -> Severity:
    magnitude = abs(amount)
    if magnitude >= config.high_severity_amount:
        level = Severity.HIGH
    elif magnitude >= config.medium_severity_amount:
        level = Severity.MEDIUM
    else:
        level = Severity.LOW
    order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH]
    return max(level, minimum, key=order.index)


def _invoice_evidence(inv: InvoiceLine) -> Evidence:
    return Evidence(
        source="invoices", reference=inv.invoice_number, source_row=inv.source_row,
        detail=(f"Invoice {inv.invoice_number} dated {inv.invoice_date}: "
                f"{fmt_qty(inv.quantity_invoiced)} x {inv.unit_price} {inv.currency} "
                f"= {fmt_money(inv.line_amount, inv.currency)}"),
    )


def _receipt_evidence(grn: GoodsReceiptLine) -> Evidence:
    return Evidence(
        source="goods_received", reference=grn.grn_number, source_row=grn.source_row,
        detail=f"GRN {grn.grn_number} dated {grn.received_date}: {fmt_qty(grn.quantity_received)} received",
    )


def _po_evidence(line: MatchedLine) -> Evidence:
    po = line.po_line
    return Evidence(
        source="purchase_orders", reference=po.po_number, source_row=po.source_row,
        detail=(f"PO {po.po_number} line {po.line_number} ({po.supplier_name}): "
                f"{fmt_qty(po.quantity_ordered)} x {po.unit_price} {po.currency} ordered on {po.order_date}"),
    )


# --------------------------------------------------------------------------- #
# PO-line level rules
# --------------------------------------------------------------------------- #

def _check_missing_documents(line: MatchedLine, config: MatchConfig, rejected: RejectedKeys) -> list[_Draft]:
    po = line.po_line
    drafts: list[_Draft] = []

    if line.invoices and not line.receipts:
        invoiced_value = sum((i.line_amount for i in line.invoices), ZERO)
        draft = _Draft(
            exception_type=ExceptionType.MISSING_GOODS_RECEIPT,
            severity=severity_for_amount(invoiced_value, config),
            supplier_id=po.supplier_id, po_number=po.po_number, sku=po.sku,
            summary=(f"{fmt_qty(line.total_invoiced)} units invoiced for PO {po.po_number} / {po.sku} "
                     f"but no goods receipt is recorded."),
            expected="Goods receipt for invoiced quantity",
            actual="No goods receipt found",
            quantity_difference=line.total_invoiced,
            amount_impact=round_money(invoiced_value), currency=po.currency,
            evidence=[_po_evidence(line), *map(_invoice_evidence, line.invoices)],
            recommended_action=("Hold payment for the invoiced amount. Confirm with the receiving location "
                                "whether the goods arrived; if they did, post the goods receipt, otherwise "
                                "query the supplier on the delivery status."),
        )
        if po.key in rejected.goods_received:
            draft.notes.append(_REJECTED_NOTE.format(file="goods_received", po=po.po_number, sku=po.sku))
        drafts.append(draft)

    if line.receipts and not line.invoices:
        received_value = line.total_received * po.unit_price
        draft = _Draft(
            exception_type=ExceptionType.MISSING_INVOICE,
            severity=Severity.LOW,
            supplier_id=po.supplier_id, po_number=po.po_number, sku=po.sku,
            summary=(f"{fmt_qty(line.total_received)} units received for PO {po.po_number} / {po.sku} "
                     f"but no invoice is recorded."),
            expected="Supplier invoice for received quantity",
            actual="No invoice found",
            quantity_difference=line.total_received,
            amount_impact=round_money(received_value), currency=po.currency,
            evidence=[_po_evidence(line), *map(_receipt_evidence, line.receipts)],
            recommended_action=("No payment risk. Accrue the received value at PO price and follow up with "
                                "the supplier if the invoice is overdue under the agreed terms."),
            notes=["Amount shown is the received value at PO unit price (an accrual estimate), not an overpayment."],
        )
        if po.key in rejected.invoices:
            draft.notes.append(_REJECTED_NOTE.format(file="invoices", po=po.po_number, sku=po.sku))
        drafts.append(draft)

    return drafts


def _check_quantities(line: MatchedLine, config: MatchConfig) -> list[_Draft]:
    po = line.po_line
    drafts: list[_Draft] = []

    if line.invoices and line.receipts:
        invoiced, received = line.total_invoiced, line.total_received
        difference = invoiced - received
        if abs(difference) > config.quantity_tolerance:
            impact = round_money(difference * po.unit_price)
            over_billed = difference > 0
            drafts.append(_Draft(
                exception_type=ExceptionType.QUANTITY_MISMATCH,
                severity=severity_for_amount(impact, config) if over_billed else Severity.LOW,
                supplier_id=po.supplier_id, po_number=po.po_number, sku=po.sku,
                summary=(f"Invoiced quantity ({fmt_qty(invoiced)}) "
                         f"{'exceeds' if over_billed else 'is less than'} received quantity "
                         f"({fmt_qty(received)}) for PO {po.po_number} / {po.sku}."),
                expected=f"Invoiced quantity = received quantity ({fmt_qty(received)})",
                actual=f"Invoiced quantity {fmt_qty(invoiced)}",
                quantity_difference=difference,
                amount_impact=impact, currency=po.currency,
                evidence=[_po_evidence(line), *map(_invoice_evidence, line.invoices),
                          *map(_receipt_evidence, line.receipts)],
                recommended_action=(
                    f"Confirm the physical receipt count with the warehouse. If {fmt_qty(received)} units is "
                    f"correct, request a credit note for {fmt_qty(difference)} units "
                    f"({fmt_money(impact, po.currency)} at PO price) before releasing payment."
                    if over_billed else
                    f"Supplier has invoiced {fmt_qty(-difference)} fewer units than received. Check whether a "
                    f"further invoice is expected before closing the PO line."),
                notes=(["Impact is valued at PO unit price; any price variance is reported separately."]
                       if over_billed else []),
            ))

    received = line.total_received
    over_receipt = received - po.quantity_ordered
    if line.receipts and over_receipt > config.quantity_tolerance:
        impact = round_money(over_receipt * po.unit_price)
        drafts.append(_Draft(
            exception_type=ExceptionType.QUANTITY_MISMATCH,
            severity=severity_for_amount(impact, config),
            supplier_id=po.supplier_id, po_number=po.po_number, sku=po.sku,
            summary=(f"Received quantity ({fmt_qty(received)}) exceeds ordered quantity "
                     f"({fmt_qty(po.quantity_ordered)}) for PO {po.po_number} / {po.sku}."),
            expected=f"Received quantity <= ordered quantity ({fmt_qty(po.quantity_ordered)})",
            actual=f"Received quantity {fmt_qty(received)}",
            quantity_difference=over_receipt,
            amount_impact=impact, currency=po.currency,
            evidence=[_po_evidence(line), *map(_receipt_evidence, line.receipts)],
            recommended_action=(f"Verify the receipt count. If the over-delivery of {fmt_qty(over_receipt)} "
                                f"units is confirmed, either return the excess or raise a PO amendment "
                                f"before any invoice for it is paid."),
        ))
    return drafts


def _check_invoice_lines(line: MatchedLine, config: MatchConfig) -> list[_Draft]:
    po = line.po_line
    drafts: list[_Draft] = []
    for inv in line.invoices:
        if inv.supplier_id != po.supplier_id:
            drafts.append(_Draft(
                exception_type=ExceptionType.SUPPLIER_MISMATCH,
                severity=Severity.HIGH,
                supplier_id=inv.supplier_id, po_number=po.po_number, sku=po.sku,
                summary=(f"Invoice {inv.invoice_number} is from supplier {inv.supplier_id} but PO "
                         f"{po.po_number} was raised with supplier {po.supplier_id}."),
                expected=f"Supplier {po.supplier_id}", actual=f"Supplier {inv.supplier_id}",
                amount_impact=round_money(inv.line_amount), currency=inv.currency,
                evidence=[_po_evidence(line), _invoice_evidence(inv)],
                recommended_action=("Do not pay. Verify the invoice's supplier and remittance details against "
                                    "the vendor master and the PO before any further processing."),
            ))

        if inv.currency != po.currency:
            drafts.append(_Draft(
                exception_type=ExceptionType.CURRENCY_MISMATCH,
                severity=Severity.MEDIUM,
                supplier_id=inv.supplier_id, po_number=po.po_number, sku=po.sku,
                summary=(f"Invoice {inv.invoice_number} is in {inv.currency} but PO {po.po_number} "
                         f"is in {po.currency}; price could not be compared."),
                expected=f"Currency {po.currency}", actual=f"Currency {inv.currency}",
                amount_impact=ZERO, currency=inv.currency,
                evidence=[_po_evidence(line), _invoice_evidence(inv)],
                recommended_action="Request a corrected invoice in the PO currency, or confirm an agreed rate.",
                notes=["No currency conversion is performed, so no amount impact is calculated."],
            ))
            continue  # prices in different currencies are not comparable

        price_difference = inv.unit_price - po.unit_price
        allowed = max(config.price_tolerance_abs, po.unit_price * config.price_tolerance_pct / Decimal(100))
        if abs(price_difference) > allowed:
            impact = round_money(price_difference * inv.quantity_invoiced)
            overcharged = price_difference > 0
            drafts.append(_Draft(
                exception_type=ExceptionType.PRICE_MISMATCH,
                severity=severity_for_amount(impact, config) if overcharged else Severity.LOW,
                supplier_id=inv.supplier_id, po_number=po.po_number, sku=po.sku,
                summary=(f"Invoice {inv.invoice_number} unit price {inv.unit_price} {inv.currency} "
                         f"{'is above' if overcharged else 'is below'} PO price {po.unit_price} "
                         f"{po.currency} for {po.sku}."),
                expected=f"Unit price {po.unit_price} {po.currency}",
                actual=f"Unit price {inv.unit_price} {inv.currency}",
                quantity_difference=None,
                amount_impact=impact, currency=inv.currency,
                evidence=[_po_evidence(line), _invoice_evidence(inv)],
                recommended_action=(
                    f"Check for an approved price change or contract amendment. If none exists, request a "
                    f"credit note for {fmt_money(impact, inv.currency)} "
                    f"({fmt_qty(inv.quantity_invoiced)} x {price_difference} {inv.currency})."
                    if overcharged else
                    "Invoice price is below PO price. Confirm with the supplier before payment so the "
                    "difference is not re-billed later."),
                ))
    return drafts


def _check_receipt_suppliers(line: MatchedLine) -> list[_Draft]:
    po = line.po_line
    return [
        _Draft(
            exception_type=ExceptionType.SUPPLIER_MISMATCH,
            severity=Severity.MEDIUM,
            supplier_id=grn.supplier_id, po_number=po.po_number, sku=po.sku,
            summary=(f"GRN {grn.grn_number} records supplier {grn.supplier_id} but PO "
                     f"{po.po_number} was raised with supplier {po.supplier_id}."),
            expected=f"Supplier {po.supplier_id}", actual=f"Supplier {grn.supplier_id}",
            currency=po.currency,
            evidence=[_po_evidence(line), _receipt_evidence(grn)],
            recommended_action="Confirm which supplier delivered the goods and correct the goods receipt record.",
        )
        for grn in line.receipts
        if grn.supplier_id != po.supplier_id
    ]


# --------------------------------------------------------------------------- #
# Unmatched records
# --------------------------------------------------------------------------- #

def _check_unmatched(match: MatchResult, config: MatchConfig, rejected: RejectedKeys) -> list[_Draft]:
    drafts: list[_Draft] = []
    for inv in match.unmatched_invoices:
        po_exists = inv.po_number in match.known_po_numbers
        draft = _Draft(
            exception_type=ExceptionType.MISSING_PURCHASE_ORDER,
            severity=severity_for_amount(inv.line_amount, config, minimum=Severity.MEDIUM),
            supplier_id=inv.supplier_id, po_number=inv.po_number, sku=inv.sku,
            summary=(f"Invoice {inv.invoice_number} references "
                     + (f"PO {inv.po_number}, which has no line for SKU {inv.sku}."
                        if po_exists else f"PO {inv.po_number}, which was not found in the valid purchase order data.")),
            expected="A matching purchase order line", actual="No matching PO line",
            amount_impact=round_money(inv.line_amount), currency=inv.currency,
            evidence=[_invoice_evidence(inv)],
            recommended_action=("Do not pay. Ask the requester or buyer to confirm whether this purchase was "
                                "authorised; obtain a valid PO reference from the supplier or reject the invoice."),
        )
        if inv.key in rejected.purchase_orders:
            draft.notes.append(_REJECTED_NOTE.format(file="purchase_orders", po=inv.po_number, sku=inv.sku))
        drafts.append(draft)

    for grn in match.unmatched_receipts:
        po_exists = grn.po_number in match.known_po_numbers
        draft = _Draft(
            exception_type=ExceptionType.MISSING_PURCHASE_ORDER,
            severity=Severity.MEDIUM,
            supplier_id=grn.supplier_id, po_number=grn.po_number, sku=grn.sku,
            summary=(f"GRN {grn.grn_number} references "
                     + (f"PO {grn.po_number}, which has no line for SKU {grn.sku}."
                        if po_exists else f"PO {grn.po_number}, which was not found in the valid purchase order data.")),
            expected="A matching purchase order line", actual="No matching PO line",
            quantity_difference=grn.quantity_received,
            evidence=[_receipt_evidence(grn)],
            recommended_action=("Confirm the PO reference on the delivery paperwork and correct the goods "
                                "receipt, or raise a retrospective PO if the delivery was authorised."),
            notes=["Receipts carry no price, so no amount impact is calculated."],
        )
        if grn.key in rejected.purchase_orders:
            draft.notes.append(_REJECTED_NOTE.format(file="purchase_orders", po=grn.po_number, sku=grn.sku))
        drafts.append(draft)
    return drafts


# --------------------------------------------------------------------------- #
# Duplicate invoices
# --------------------------------------------------------------------------- #

def normalize_invoice_number(number: str) -> str:
    """Canonical form used to spot re-keyed invoice numbers.

    Uppercases, drops punctuation/whitespace and strips leading zeros from
    each digit run: ``inv-00456`` and ``INV 456`` both become ``INV456``.
    """
    tokens = re.findall(r"[A-Za-z]+|\d+", number)
    return "".join(t.upper() if t.isalpha() else (t.lstrip("0") or "0") for t in tokens)


@dataclass
class _InvoiceHeader:
    supplier_id: str
    invoice_number: str
    lines: list[InvoiceLine]

    @property
    def invoice_date(self):
        return min(line.invoice_date for line in self.lines)

    @property
    def currency(self) -> str:
        return self.lines[0].currency

    @property
    def total(self) -> Decimal:
        return sum((line.line_amount for line in self.lines), ZERO)

    @property
    def signature(self) -> Counter:
        return Counter((l.po_number, l.sku, l.quantity_invoiced, l.unit_price, l.currency) for l in self.lines)

    @property
    def first_row(self) -> int:
        return min(line.source_row for line in self.lines)


def _billing_exceeds_supply(match: MatchResult):
    """Return a predicate: has a PO line been invoiced beyond what was received or ordered?

    Invoice lines with no PO line are treated as exceeding supply.
    """
    lines = {m.po_line.key: m for m in match.matched}

    def exceeds(key: tuple[str, str]) -> bool:
        line = lines.get(key)
        if line is None:
            return True
        return line.total_invoiced > line.total_received or line.total_invoiced > line.po_line.quantity_ordered

    return exceeds


def _check_duplicates(invoices: list[InvoiceLine], match: MatchResult, config: MatchConfig) -> list[_Draft]:
    drafts: list[_Draft] = []
    exceeds_supply = _billing_exceeds_supply(match)
    groups: dict[tuple[str, str], list[InvoiceLine]] = defaultdict(list)
    for inv in invoices:
        groups[(inv.supplier_id, inv.invoice_number)].append(inv)

    # (a) Identical line repeated inside the same invoice number.
    for (supplier_id, number), lines in groups.items():
        by_content: dict[tuple, list[InvoiceLine]] = defaultdict(list)
        for line in lines:
            by_content[(line.po_number, line.sku, line.quantity_invoiced, line.unit_price, line.currency)].append(line)
        for repeats in by_content.values():
            if len(repeats) < 2:
                continue
            first = repeats[0]
            impact = round_money(first.line_amount * (len(repeats) - 1))
            drafts.append(_Draft(
                exception_type=ExceptionType.POSSIBLE_DUPLICATE_INVOICE,
                severity=severity_for_amount(impact, config, minimum=Severity.MEDIUM),
                supplier_id=supplier_id, po_number=first.po_number, sku=first.sku,
                summary=(f"Invoice {number} contains the same line (PO {first.po_number} / {first.sku}, "
                         f"{fmt_qty(first.quantity_invoiced)} x {first.unit_price}) {len(repeats)} times."),
                expected="Each invoice line recorded once", actual=f"Line recorded {len(repeats)} times",
                quantity_difference=first.quantity_invoiced * (len(repeats) - 1),
                amount_impact=impact, currency=first.currency,
                evidence=[_invoice_evidence(r) for r in repeats],
                recommended_action=("Check the invoice image/original. If the line appears once on the source "
                                    "document, remove the duplicate entry before payment."),
                notes=["Duplicates are flagged as *possible*; a person must confirm against the source document."],
            ))

    # (b) Different invoice numbers from the same supplier that look like the same bill.
    headers = sorted(
        (_InvoiceHeader(s, n, lines) for (s, n), lines in groups.items()),
        key=lambda h: (h.supplier_id, h.invoice_date, h.first_row),
    )
    by_supplier: dict[str, list[_InvoiceHeader]] = defaultdict(list)
    for header in headers:
        by_supplier[header.supplier_id].append(header)

    for supplier_id, supplier_headers in by_supplier.items():
        for index, later in enumerate(supplier_headers):
            for earlier in supplier_headers[:index]:
                reasons = []
                if normalize_invoice_number(earlier.invoice_number) == normalize_invoice_number(later.invoice_number):
                    reasons.append(f"invoice numbers '{earlier.invoice_number}' and '{later.invoice_number}' "
                                   f"differ only in formatting")
                days_apart = abs((later.invoice_date - earlier.invoice_date).days)
                # Identical content alone can be legitimate split billing, so it only counts when
                # the combined billing exceeds what was received or ordered.
                if (earlier.signature == later.signature and days_apart <= config.duplicate_window_days
                        and any(exceeds_supply(l.key) for l in later.lines)):
                    reasons.append(f"identical line items, quantities and prices, dated {days_apart} day(s) apart")
                if not reasons:
                    continue
                impact = round_money(later.total)
                po_numbers = sorted({l.po_number for l in later.lines})
                skus = sorted({l.sku for l in later.lines})
                drafts.append(_Draft(
                    exception_type=ExceptionType.POSSIBLE_DUPLICATE_INVOICE,
                    severity=severity_for_amount(impact, config, minimum=Severity.MEDIUM),
                    supplier_id=supplier_id,
                    po_number=po_numbers[0] if len(po_numbers) == 1 else None,
                    sku=skus[0] if len(skus) == 1 else None,
                    summary=(f"Invoice {later.invoice_number} may duplicate invoice {earlier.invoice_number} "
                             f"from supplier {supplier_id}: " + "; ".join(reasons) + "."),
                    expected="Each supplier bill paid once",
                    actual=f"Two invoices totalling {fmt_money(earlier.total, earlier.currency)} and "
                           f"{fmt_money(later.total, later.currency)}",
                    amount_impact=impact, currency=later.currency,
                    evidence=[_invoice_evidence(l) for l in earlier.lines + later.lines],
                    recommended_action=(f"Hold invoice {later.invoice_number}. Compare both documents with the "
                                        f"supplier statement; if they are the same bill, reject the later one."),
                    notes=["Duplicates are flagged as *possible*; a person must confirm against the source document."],
                ))
                break  # report each later invoice once, against its earliest match
    return drafts


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


def _link_duplicates(drafts: list[_Draft], duplicate_drafts: list[_Draft]) -> None:
    """Note on other exceptions when an invoice they rely on is a possible duplicate."""
    flagged = {(d.supplier_id, e.reference) for d in duplicate_drafts for e in d.evidence}
    for draft in drafts:
        involved = sorted({e.reference for e in draft.evidence
                           if e.source == "invoices" and (draft.supplier_id, e.reference) in flagged})
        if involved:
            draft.notes.append(f"Invoice(s) {', '.join(involved)} are also flagged as possible duplicates; "
                               f"resolve that exception first, as it may explain this one.")


def detect_exceptions(
    match: MatchResult,
    all_invoices: list[InvoiceLine],
    config: MatchConfig | None = None,
    rejected: RejectedKeys | None = None,
) -> list[OpsException]:
    """Run every rule and return exceptions in a stable, prioritised order."""
    config = config or MatchConfig()
    rejected = rejected or RejectedKeys()

    drafts: list[_Draft] = []
    for line in match.matched:
        drafts += _check_missing_documents(line, config, rejected)
        drafts += _check_quantities(line, config)
        drafts += _check_invoice_lines(line, config)
        drafts += _check_receipt_suppliers(line)
    drafts += _check_unmatched(match, config, rejected)
    duplicate_drafts = _check_duplicates(all_invoices, match, config)
    _link_duplicates(drafts, duplicate_drafts)
    drafts += duplicate_drafts

    drafts.sort(key=lambda d: (
        _SEVERITY_ORDER[d.severity], -abs(d.amount_impact), d.exception_type.value,
        d.po_number or "", d.sku or "", d.summary,
    ))
    return [
        OpsException(
            exception_id=f"EXC-{index:04d}",
            exception_type=d.exception_type, severity=d.severity,
            supplier_id=d.supplier_id, po_number=d.po_number, sku=d.sku,
            summary=d.summary, expected=d.expected, actual=d.actual,
            quantity_difference=d.quantity_difference, amount_impact=d.amount_impact,
            currency=d.currency, evidence=tuple(d.evidence),
            recommended_action=d.recommended_action, notes=tuple(d.notes),
        )
        for index, d in enumerate(drafts, start=1)
    ]
