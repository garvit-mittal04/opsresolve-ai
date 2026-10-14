"""Investigation checks: candidate explanations for an exception, checked against the loaded data (Stage D).

This is a separate layer on top of the engine's results. It never changes an exception, an amount, a
classification or a recommended action, and it never states why a discrepancy happened. For some exception
types it lists candidate explanations from a fixed, reviewed catalogue (``CATALOGUE``) and checks each one
deterministically against the records of the loaded files, with exact ``Decimal`` comparisons. Each check
has one of three evidence statuses (``STATUS_LABELS``):

* ``consistent``: the loaded records match what the explanation would produce. Not confirmed: a person
  must verify it, and several explanations can be consistent at once. When more than one record fits, every
  candidate is cited and an observation says the match is not unique.
* ``contradicted``: comparable, complete records exist and none of them fits.
* ``not_checkable``: the records needed are missing, were (or may have been) rejected during validation, are
  in units that cannot be compared, or cannot rule the explanation out; ``missing`` says what would be needed.

A fourth status, ``failed``, is not about the evidence: the check itself raised an error (a defect in this
module). The other checks still run, the failure is recorded in ``Investigation.failures`` (stage and error
class name only, never the error's message or data), and ``strict=True`` re-raises it so tests and
diagnostics cannot miss it. Errors while indexing the run are never contained.

Every statement, observation and next step is fixed text; only values taken from the cited records are
filled in. Every observation cites the records it relies on (``RecordRef``: file, line and document
number), including records outside the exception's own evidence, such as another PO line.

The v1 catalogue has five checks: receipt not posted, unit mix-up, mistyped PO number, document under
another PO line, and price from another order. Matching is strict: same supplier and SKU, exact
quantities and prices, PO numbers equal after normalisation or one character apart, and quantities or
prices of different PO lines compared only in the same unit of measure (or with no unit stated on either).

Pure: reads an ``InvestigationResult`` and returns new frozen objects. No files, no output formatting,
no network, no AI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Callable, Iterable

from opsresolve.detection import fmt_qty
from opsresolve.ingestion import LoadResult
from opsresolve.matching import MatchedLine
from opsresolve.models import ExceptionType, GoodsReceiptLine, InvoiceLine, OpsException, PurchaseOrderLine
from opsresolve.pipeline import InvestigationResult


class CheckStatus(str, Enum):
    CONSISTENT = "consistent"
    CONTRADICTED = "contradicted"
    NOT_CHECKABLE = "not_checkable"
    FAILED = "failed"  # the check raised an error; nothing is concluded


STATUS_LABELS = {
    CheckStatus.CONSISTENT: "Consistent with the data — not confirmed",
    CheckStatus.CONTRADICTED: "Contradicted by the data",
    CheckStatus.NOT_CHECKABLE: "Can't be checked",
    CheckStatus.FAILED: "Check failed — nothing concluded",
}
CHECK_FAILED = "The check could not be completed, so nothing is concluded."
INVESTIGATION_FAILED = "The checks for this exception could not be run, so nothing is concluded."
NOT_UNIQUE = "The match is not unique: {count} candidates fit."


@dataclass(frozen=True)
class RecordRef:
    """A cited record: ``ref`` is local to one investigation (R1, R2, ...)."""

    ref: str
    source: str  # "purchase_orders" | "invoices" | "goods_received" | "uom_conversions"
    source_row: int  # line in the original CSV file (header is line 1)
    reference: str  # document number (PO, invoice or GRN number; the SKU for a conversion row)


@dataclass(frozen=True)
class Observation:
    text: str  # fixed wording with values from the cited records
    refs: tuple[str, ...]  # RecordRef.ref values


@dataclass(frozen=True)
class HypothesisCheck:
    id: str  # H1, H2, ... in catalogue order
    code: str  # catalogue code, e.g. "receipt_not_posted"
    statement: str  # the candidate explanation (fixed text, always "may")
    status: CheckStatus
    observations: tuple[Observation, ...]
    missing: tuple[str, ...]  # what would be needed to decide
    how_to_confirm: str  # fixed next step; describes an action, never a cause

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


@dataclass(frozen=True)
class CheckFailure:
    """An error raised inside this module, for tests and diagnostics. Never shown with its message."""

    stage: str  # the catalogue code of the check, or "subject" if the exception could not be prepared
    error_type: str  # the error's class name only


@dataclass(frozen=True)
class Investigation:
    exception_id: str
    related: tuple[RecordRef, ...]  # every record cited by the checks, in order of first citation
    checks: tuple[HypothesisCheck, ...]  # empty when the catalogue has nothing for this exception
    missing: tuple[str, ...]  # every check's missing evidence, without repeats
    failures: tuple[CheckFailure, ...] = ()  # empty unless a check (or its preparation) raised an error


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class HypothesisSpec:
    code: str
    statement: str
    how_to_confirm: str


RECEIPT_NOT_POSTED = HypothesisSpec(
    "receipt_not_posted",
    "The goods may have arrived without the goods receipt being posted yet.",
    "Ask the receiving location whether the goods arrived and, if so, when the goods receipt will be posted.")
UNIT_MIX_UP = HypothesisSpec(
    "unit_mix_up",
    "Quantities or prices may have been recorded in different units of measure.",
    "Check the unit of measure on the invoice and the delivery note against the PO.")
MISTYPED_PO_NUMBER = HypothesisSpec(
    "mistyped_po_number",
    "The PO number on the document may have been mistyped.",
    "Compare the PO reference on the document with the cited PO and confirm the correct number with the buyer.")
DOCUMENT_UNDER_OTHER_PO = HypothesisSpec(
    "document_under_other_po",
    "The matching document may have been recorded against another PO line.",
    "Check the paperwork of the cited records to see which PO they belong to, and correct the posting if needed.")
PRICE_FROM_OTHER_ORDER = HypothesisSpec(
    "price_from_other_order",
    "The invoice may have used the price of another order for the same supplier and SKU.",
    "Ask the supplier which order or price agreement the invoice used, and whether it applies to this PO.")

CATALOGUE = (RECEIPT_NOT_POSTED, UNIT_MIX_UP, MISTYPED_PO_NUMBER, DOCUMENT_UNDER_OTHER_PO, PRICE_FROM_OTHER_ORDER)


# --------------------------------------------------------------------------- #
# Index of the loaded data (built once per run)
# --------------------------------------------------------------------------- #

Cite = tuple[str, int, str]  # (source, source_row, reference): a record before it gets its R-number


def _po_cite(po: PurchaseOrderLine) -> Cite:
    return ("purchase_orders", po.source_row, po.po_number)


def _invoice_cite(inv: InvoiceLine) -> Cite:
    return ("invoices", inv.source_row, inv.invoice_number)


def _receipt_cite(grn: GoodsReceiptLine) -> Cite:
    return ("goods_received", grn.source_row, grn.grn_number)


class _Index:
    """Lookups over one run's data. Quantities and prices are the engine's (after unit conversion): the
    documents of a matched PO line are in that line's unit; unmatched documents are in their own unit."""

    def __init__(self, result: InvestigationResult):
        self.result = result
        match = result.match
        self.unresolved = set(result.units.unresolved) if result.units is not None else set()
        self.lines = {line.po_line.key: line for line in match.matched}
        self.lines_by_supplier_sku: dict[tuple[str, str], list[MatchedLine]] = {}
        for line in match.matched:
            po = line.po_line
            self.lines_by_supplier_sku.setdefault((po.supplier_id, po.sku), []).append(line)
        self.unmatched_invoices = {inv.source_row: inv for inv in match.unmatched_invoices}
        self.unmatched_receipts = {grn.source_row: grn for grn in match.unmatched_receipts}
        conversions = result.dataset.uom_conversions
        self.conversions = conversions
        self.conversion_rows: dict[str, list] = {}
        for row in (conversions.records if conversions is not None else []):
            self.conversion_rows.setdefault(row.sku, []).append(row)

    def comparable(self, line: MatchedLine) -> bool:
        return line.po_line.key not in self.unresolved

    def same_unit(self, line: MatchedLine, po: PurchaseOrderLine) -> bool:
        """Whether another PO line's quantities and prices can be compared with ``po``'s as they are: its
        units were resolved and it has the same unit of measure (or neither states one)."""
        return self.comparable(line) and line.po_line.unit_of_measure == po.unit_of_measure

    def other_lines(self, supplier_id: str, sku: str, exclude: tuple[str, str] | None) -> list[MatchedLine]:
        return [line for line in self.lines_by_supplier_sku.get((supplier_id, sku), [])
                if line.po_line.key != exclude]

    def receipts_elsewhere(self, po: PurchaseOrderLine) -> tuple[list[GoodsReceiptLine], list[GoodsReceiptLine]]:
        """Receipts for the same supplier and SKU outside this PO line: (comparable in ``po``'s unit, not)."""
        same: list[GoodsReceiptLine] = []
        other: list[GoodsReceiptLine] = []
        for line in self.other_lines(po.supplier_id, po.sku, po.key):
            for grn in line.receipts:
                if grn.supplier_id == po.supplier_id:
                    (same if self.same_unit(line, po) else other).append(grn)
        for grn in self.unmatched_receipts.values():
            if (grn.supplier_id, grn.sku) == (po.supplier_id, po.sku):
                (same if grn.unit_of_measure == po.unit_of_measure else other).append(grn)
        return sorted(same, key=lambda g: g.source_row), sorted(other, key=lambda g: g.source_row)

    def invoices_elsewhere(self, po: PurchaseOrderLine) -> tuple[list[InvoiceLine], list[InvoiceLine]]:
        """Invoices for the same supplier and SKU outside this PO line: (comparable in ``po``'s unit, not)."""
        same: list[InvoiceLine] = []
        other: list[InvoiceLine] = []
        for line in self.other_lines(po.supplier_id, po.sku, po.key):
            for inv in line.invoices:
                if inv.supplier_id == po.supplier_id:
                    (same if self.same_unit(line, po) else other).append(inv)
        for inv in self.unmatched_invoices.values():
            if (inv.supplier_id, inv.sku) == (po.supplier_id, po.sku):
                (same if inv.unit_of_measure == po.unit_of_measure else other).append(inv)
        return sorted(same, key=lambda i: i.source_row), sorted(other, key=lambda i: i.source_row)


# A rejected row with an issue on one of these fields (None: the row as a whole) may have had any key.
_UNKNOWN_KEY_FIELDS = ("po_number", "sku", None)


def _unknown_key_rows(loaded: LoadResult) -> bool:
    if any(issue.row is None for issue in loaded.issues):
        return True  # a file-level problem: rows may be missing altogether
    return any(issue.row is not None and issue.field in _UNKNOWN_KEY_FIELDS for issue in loaded.issues)


def _incomplete(loaded: LoadResult | None, sku: str) -> bool:
    """Whether rejected input could hide a comparable record for this SKU: a file-level problem, a rejected
    row for this SKU, or a rejected row whose PO number or SKU could not be read."""
    if loaded is None:
        return False
    return any(key_sku == sku for _, key_sku in loaded.rejected_keys) or _unknown_key_rows(loaded)


def _rows_may_be_missing(loaded: LoadResult | None, key: tuple[str, str]) -> bool:
    """Whether rejected input could hide a row of this PO line, so its totals may be incomplete."""
    if loaded is None:
        return False
    return key in loaded.rejected_keys or _unknown_key_rows(loaded)


_ROW_NOUN = {"purchase_orders": "purchase-order", "invoices": "invoice", "goods_received": "goods-receipt"}
_UNITS_UNRESOLVED = ("The quantities on this PO line are in units that could not be converted, so they cannot be "
                     "compared.")


def _rejected_for_line(index: _Index, po: PurchaseOrderLine, files: tuple[str, ...]) -> list[str]:
    """The missing-evidence note when rows of these files that may belong to this PO line were rejected."""
    dataset = index.result.dataset
    names = [name for name in files if _rows_may_be_missing(getattr(dataset, name), po.key)]
    if not names:
        return []
    return [f"Some {' or '.join(_ROW_NOUN[name] for name in names)} rows that may belong to this PO line were "
            f"rejected during validation, so its totals may be incomplete."]


# --------------------------------------------------------------------------- #
# The subject of an exception
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Subject:
    kind: str  # "over_billed", "under_billed", "over_receipt", "price", "missing_receipt", "missing_invoice",
    #            "unmatched_invoice", "unmatched_receipt"
    line: MatchedLine | None = None
    invoice: InvoiceLine | None = None
    receipt: GoodsReceiptLine | None = None


def _evidence_row(exception: OpsException, source: str) -> int | None:
    return next((e.source_row for e in exception.evidence if e.source == source), None)


def _subject(exception: OpsException, index: _Index) -> _Subject | None:
    kind = exception.exception_type
    line = index.lines.get((exception.po_number, exception.sku))
    if kind is ExceptionType.QUANTITY_MISMATCH and line is not None:
        if any(e.source == "invoices" for e in exception.evidence):
            return _Subject("over_billed" if line.total_invoiced > line.total_received else "under_billed", line)
        return _Subject("over_receipt", line)
    if kind is ExceptionType.PRICE_MISMATCH and line is not None:
        row = _evidence_row(exception, "invoices")
        invoice = next((inv for inv in line.invoices if inv.source_row == row), None)
        return _Subject("price", line, invoice=invoice) if invoice is not None else None
    if kind is ExceptionType.MISSING_GOODS_RECEIPT and line is not None:
        return _Subject("missing_receipt", line)
    if kind is ExceptionType.MISSING_INVOICE and line is not None:
        return _Subject("missing_invoice", line)
    if kind is ExceptionType.MISSING_PURCHASE_ORDER:
        invoice = index.unmatched_invoices.get(_evidence_row(exception, "invoices"))
        if invoice is not None:
            return _Subject("unmatched_invoice", invoice=invoice)
        receipt = index.unmatched_receipts.get(_evidence_row(exception, "goods_received"))
        if receipt is not None:
            return _Subject("unmatched_receipt", receipt=receipt)
    return None


# --------------------------------------------------------------------------- #
# Checks: each returns (status, observations as (text, cites), missing texts)
# --------------------------------------------------------------------------- #

_RawObservation = tuple[str, tuple[Cite, ...]]
_Outcome = tuple[CheckStatus, list[_RawObservation], list[str]]


def _line_cites(line: MatchedLine, invoices: bool = True, receipts: bool = True) -> tuple[Cite, ...]:
    cites = [_po_cite(line.po_line)]
    cites += [_invoice_cite(inv) for inv in line.invoices] if invoices else []
    cites += [_receipt_cite(grn) for grn in line.receipts] if receipts else []
    return tuple(cites)


def _consistent(fits: list[_RawObservation]) -> _Outcome:
    """Consistent, citing every fit; with several fits, an observation says the match is not unique."""
    observations = list(fits)
    if len(fits) > 1:
        observations.append((NOT_UNIQUE.format(count=len(fits)), tuple(c for _, cites in fits for c in cites)))
    return CheckStatus.CONSISTENT, observations, []


def _check_receipt_not_posted(subject: _Subject, index: _Index) -> _Outcome:
    line = subject.line
    po = line.po_line
    if not index.comparable(line):
        return CheckStatus.NOT_CHECKABLE, [], [_UNITS_UNRESOLVED]
    rejected = _rejected_for_line(index, po, ("invoices", "goods_received"))
    if rejected:
        return CheckStatus.NOT_CHECKABLE, [], rejected
    invoiced, ordered, received = line.total_invoiced, po.quantity_ordered, line.total_received
    received_so_far = (f"Received so far on PO {po.po_number} line {po.line_number}: {fmt_qty(received)}.",
                       _line_cites(line, invoices=False))
    if invoiced <= ordered:
        return (CheckStatus.CONSISTENT,
                [(f"Invoiced quantity {fmt_qty(invoiced)} is within the ordered quantity {fmt_qty(ordered)}.",
                  _line_cites(line, receipts=False)), received_so_far],
                ["A goods receipt posted after these files were produced would not be in the loaded files."])
    return (CheckStatus.NOT_CHECKABLE,
            [(f"Invoiced quantity {fmt_qty(invoiced)} is more than the ordered quantity {fmt_qty(ordered)}.",
              _line_cites(line, receipts=False)), received_so_far],
            ["Whether more than the ordered quantity was delivered: an unposted delivery above the ordered "
             "quantity cannot be ruled out from these records alone."])


def _factor_text(row) -> str:
    return f"1 {row.uom} = {fmt_qty(row.units_per_uom)} {row.base_uom}"


def _check_unit_mix_up(subject: _Subject, index: _Index) -> _Outcome:
    line = subject.line
    po = line.po_line
    if not index.comparable(line):
        return CheckStatus.NOT_CHECKABLE, [], [_UNITS_UNRESOLVED]
    if index.conversions is None:
        return CheckStatus.NOT_CHECKABLE, [], ["No unit-conversion file was loaded."]
    rows = [row for row in index.conversion_rows.get(po.sku, []) if row.units_per_uom != 1]
    if not rows:
        return (CheckStatus.NOT_CHECKABLE, [],
                [f"The unit-conversion file has no conversion between different units for SKU {po.sku}."])
    # sides: (documents as (label, quantity), their total, the total they are compared with, noun, other side)
    sides: list[tuple[list[tuple[str, Decimal]], Decimal, Decimal, str, str]] = []
    if subject.kind == "price":
        inv = subject.invoice
        first, second = inv.unit_price, po.unit_price
        what = (f"invoice unit price {inv.unit_price}", f"PO unit price {po.unit_price}")
        cites = (_po_cite(po), _invoice_cite(inv))
    else:
        rejected = _rejected_for_line(
            index, po, ("goods_received",) if subject.kind == "over_receipt" else ("invoices", "goods_received"))
        if rejected:
            return CheckStatus.NOT_CHECKABLE, [], rejected
        receipts = [(f"GRN {grn.grn_number}", grn.quantity_received) for grn in line.receipts]
        if subject.kind == "over_receipt":
            first, second = line.total_received, po.quantity_ordered
            what = (f"received quantity {fmt_qty(first)}", f"ordered quantity {fmt_qty(second)}")
            cites = _line_cites(line, invoices=False)
            sides.append((receipts, first, second, "received quantity", what[1]))
        else:
            invoices = [(f"invoice {inv.invoice_number}", inv.quantity_invoiced) for inv in line.invoices]
            first, second = line.total_invoiced, line.total_received
            what = (f"invoiced quantity {fmt_qty(first)}", f"received quantity {fmt_qty(second)}")
            cites = _line_cites(line)
            sides.append((invoices, first, second, "invoiced quantity", what[1]))
            sides.append((receipts, second, first, "received quantity", what[0]))
    fits: list[_RawObservation] = []
    for row in rows:
        size, conversion = row.units_per_uom, ("uom_conversions", row.source_row, row.sku)
        if first == second * size or first * size == second:
            fits.append((f"The {what[0]} and the {what[1]} differ exactly by a factor of the conversion "
                         f"{_factor_text(row)}.", cites + (conversion,)))
        for documents, total, other_total, noun, other in sides:
            if len(documents) < 2:
                continue  # a single document is its whole side, compared above
            for label, quantity in documents:
                corrected = other_total - (total - quantity)  # what this document would need to show
                if corrected > 0 and (quantity == corrected * size or quantity * size == corrected):
                    fits.append((f"Converting the quantity {fmt_qty(quantity)} on {label} by the conversion "
                                 f"{_factor_text(row)} gives {fmt_qty(corrected)}; with it, the {noun} would "
                                 f"equal the {other}.", cites + (conversion,)))
    if fits:
        return _consistent(fits)
    if _incomplete(index.conversions, po.sku):
        return (CheckStatus.NOT_CHECKABLE, [],
                ["Some unit-conversion rows were rejected during validation, so a factor may be missing."])
    factors = ", ".join(_factor_text(row) for row in rows)
    conversion_cites = tuple(("uom_conversions", row.source_row, row.sku) for row in rows)
    if any(len(documents) > 1 for documents, *_ in sides):
        return (CheckStatus.NOT_CHECKABLE,
                [(f"No conversion for SKU {po.sku} ({factors}) relates the {what[0]} and the {what[1]}, in total "
                  f"or for any single document.", cites + conversion_cites)],
                ["This PO line has several documents; a unit mix-up on more than one of them cannot be ruled out "
                 "from these records."])
    return (CheckStatus.CONTRADICTED,
            [(f"No conversion for SKU {po.sku} ({factors}) relates the {what[0]} and the {what[1]}.",
              cites + conversion_cites)], [])


def _normalized_po(number: str) -> str:
    """A PO number compared without case, separators or the leading zeros of its digit groups."""
    compact = re.sub(r"[^0-9a-z]", "", number.casefold())
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", compact)


def _similarity(referenced: str, candidate: str) -> str | None:
    """How two PO numbers are close, or None. Strict: equal after normalisation, one substituted character,
    or two swapped adjacent characters, at the same length."""
    a, b = _normalized_po(referenced), _normalized_po(candidate)
    if not a or not b:
        return None
    if a == b:
        return "letter case, separators or leading zeros"
    if len(a) != len(b):
        return None
    differences = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(differences) == 1:
        return "one character"
    if len(differences) == 2 and differences[1] == differences[0] + 1 \
            and a[differences[0]] == b[differences[1]] and a[differences[1]] == b[differences[0]]:
        return "two swapped adjacent characters"
    return None


def _check_mistyped_po_number(subject: _Subject, index: _Index) -> _Outcome:
    document = subject.invoice or subject.receipt
    cite = _invoice_cite(document) if subject.invoice is not None else _receipt_cite(document)
    comparable = [line for line in index.other_lines(document.supplier_id, document.sku, exclude=None)
                  if line.po_line.po_number != document.po_number]
    candidates = [(line, how) for line in comparable
                  if (how := _similarity(document.po_number, line.po_line.po_number)) is not None]
    if candidates:
        return _consistent([(f"PO {line.po_line.po_number} line {line.po_line.line_number}, for the same supplier "
                             f"and SKU, differs from the referenced PO {document.po_number} only by {how}.",
                             (cite, _po_cite(line.po_line))) for line, how in candidates])
    if _incomplete(index.result.dataset.purchase_orders, document.sku):
        return (CheckStatus.NOT_CHECKABLE, [],
                ["Some purchase-order rows were rejected during validation, so a matching PO may be missing."])
    if not comparable:
        return (CheckStatus.NOT_CHECKABLE, [],
                [f"No purchase-order line for this supplier and SKU {document.sku} is in the loaded files."])
    return (CheckStatus.CONTRADICTED,
            [(f"{len(comparable)} PO line(s) for the same supplier and SKU are in the loaded files; none has a "
              f"number close to the referenced PO {document.po_number}.",
              (cite, *(_po_cite(line.po_line) for line in comparable)))], [])


def _check_document_under_other_po(subject: _Subject, index: _Index) -> _Outcome:
    line = subject.line
    po = line.po_line
    if not index.comparable(line):
        return CheckStatus.NOT_CHECKABLE, [], [_UNITS_UNRESOLVED]
    dataset = index.result.dataset
    if subject.kind == "missing_receipt":
        rejected = _rejected_for_line(index, po, ("invoices", "goods_received"))
        quantity, noun, loaded = line.total_invoiced, "goods receipt", dataset.goods_received
        comparable, uncomparable = index.receipts_elsewhere(po)
        fits = [(f"GRN {grn.grn_number}, for the same supplier and SKU, received {fmt_qty(quantity)} against PO "
                 f"{grn.po_number}.", (_po_cite(po), _receipt_cite(grn)))
                for grn in comparable if grn.quantity_received == quantity]
        cites = tuple(_receipt_cite(grn) for grn in comparable)
        uncomparable_cites = tuple(_receipt_cite(grn) for grn in uncomparable)
    elif subject.kind == "missing_invoice":
        rejected = _rejected_for_line(index, po, ("invoices", "goods_received"))
        quantity, noun, loaded = line.total_received, "invoice", dataset.invoices
        comparable, uncomparable = index.invoices_elsewhere(po)
        fits = [(f"Invoice {inv.invoice_number}, for the same supplier and SKU, bills {fmt_qty(quantity)} against "
                 f"PO {inv.po_number}.", (_po_cite(po), _invoice_cite(inv)))
                for inv in comparable if inv.quantity_invoiced == quantity]
        cites = tuple(_invoice_cite(inv) for inv in comparable)
        uncomparable_cites = tuple(_invoice_cite(inv) for inv in uncomparable)
    else:  # over_receipt: the excess could be the open quantity of another PO line
        rejected = _rejected_for_line(index, po, ("goods_received",))
        if not rejected and _incomplete(dataset.goods_received, po.sku):
            rejected = ["Some goods-receipt rows for this SKU were rejected during validation, so the open "
                        "quantities of other PO lines may be wrong."]
        quantity, noun, loaded = line.total_received - po.quantity_ordered, "PO line", dataset.purchase_orders
        others = index.other_lines(po.supplier_id, po.sku, po.key)
        comparable = [other for other in others if index.same_unit(other, po)]
        uncomparable = [other for other in others if not index.same_unit(other, po)]
        fits = [(f"PO {other.po_line.po_number} line {other.po_line.line_number}, for the same supplier and SKU, "
                 f"has exactly the excess of {fmt_qty(quantity)} still open (ordered "
                 f"{fmt_qty(other.po_line.quantity_ordered)}, received {fmt_qty(other.total_received)}).",
                 _line_cites(line, invoices=False) + _line_cites(other, invoices=False))
                for other in comparable if other.po_line.quantity_ordered - other.total_received == quantity]
        cites = tuple(_po_cite(other.po_line) for other in comparable)
        uncomparable_cites = tuple(_po_cite(other.po_line) for other in uncomparable)
    if rejected:
        return CheckStatus.NOT_CHECKABLE, [], rejected
    if fits:
        return _consistent(fits)
    if _incomplete(loaded, po.sku):
        return (CheckStatus.NOT_CHECKABLE, [],
                [f"Some {_ROW_NOUN[loaded.file_label]} rows were rejected during validation, so a matching "
                 f"{noun} may be missing."])
    if uncomparable_cites:
        return (CheckStatus.NOT_CHECKABLE,
                [(f"{len(uncomparable_cites)} other {noun}(s) for the same supplier and SKU are in a different or "
                  f"unstated unit of measure, or in units that could not be converted, so their quantities are not "
                  f"compared.", (_po_cite(po), *uncomparable_cites))],
                ["Quantities of the cited records in the same unit of measure as this PO line."])
    if not comparable:
        return (CheckStatus.NOT_CHECKABLE, [],
                [f"No other {noun} for this supplier and SKU {po.sku} is in the loaded files."])
    return (CheckStatus.CONTRADICTED,
            [(f"{len(comparable)} other {noun}(s) for the same supplier and SKU are in the loaded files; none "
              f"matches the quantity {fmt_qty(quantity)}.", (_po_cite(po), *cites))], [])


def _check_price_from_other_order(subject: _Subject, index: _Index) -> _Outcome:
    line, invoice = subject.line, subject.invoice
    po = line.po_line
    same_currency = [other for other in index.other_lines(po.supplier_id, po.sku, po.key)
                     if other.po_line.currency == invoice.currency]
    comparable = [other for other in same_currency if other.po_line.unit_of_measure == po.unit_of_measure]
    uncomparable = [other for other in same_currency if other.po_line.unit_of_measure != po.unit_of_measure]
    fits = [(f"PO {other.po_line.po_number} line {other.po_line.line_number}, for the same supplier and SKU, has "
             f"unit price {other.po_line.unit_price} {other.po_line.currency}, the price on invoice "
             f"{invoice.invoice_number}.", (_invoice_cite(invoice), _po_cite(other.po_line)))
            for other in comparable if other.po_line.unit_price == invoice.unit_price]
    if fits:
        return _consistent(fits)
    if _incomplete(index.result.dataset.purchase_orders, po.sku):
        return (CheckStatus.NOT_CHECKABLE, [],
                ["Some purchase-order rows were rejected during validation, so another order may be missing."])
    if uncomparable:
        return (CheckStatus.NOT_CHECKABLE,
                [(f"{len(uncomparable)} other PO line(s) for the same supplier and SKU in {invoice.currency} are "
                  f"priced in a different or unstated unit of measure, so their prices are not compared.",
                  (_invoice_cite(invoice), *(_po_cite(other.po_line) for other in uncomparable)))],
                ["Prices of the cited orders in the same unit of measure as this PO line."])
    if not comparable:
        return (CheckStatus.NOT_CHECKABLE, [],
                [f"No other order for this supplier and SKU {po.sku} in {invoice.currency} is in the loaded "
                 f"files."])
    return (CheckStatus.CONTRADICTED,
            [(f"{len(comparable)} other PO line(s) for the same supplier and SKU are in the loaded files; none has "
              f"unit price {invoice.unit_price}.",
              (_invoice_cite(invoice), *(_po_cite(other.po_line) for other in comparable)))], [])


_CHECKS: dict[str, Callable[[_Subject, _Index], _Outcome]] = {
    RECEIPT_NOT_POSTED.code: _check_receipt_not_posted,
    UNIT_MIX_UP.code: _check_unit_mix_up,
    MISTYPED_PO_NUMBER.code: _check_mistyped_po_number,
    DOCUMENT_UNDER_OTHER_PO.code: _check_document_under_other_po,
    PRICE_FROM_OTHER_ORDER.code: _check_price_from_other_order,
}

# Which catalogue entries apply to which kind of exception, in catalogue order.
APPLICABLE: dict[str, tuple[HypothesisSpec, ...]] = {
    "over_billed": (RECEIPT_NOT_POSTED, UNIT_MIX_UP),
    "under_billed": (UNIT_MIX_UP,),
    "over_receipt": (UNIT_MIX_UP, DOCUMENT_UNDER_OTHER_PO),
    "price": (UNIT_MIX_UP, PRICE_FROM_OTHER_ORDER),
    "missing_receipt": (RECEIPT_NOT_POSTED, DOCUMENT_UNDER_OTHER_PO),
    "missing_invoice": (DOCUMENT_UNDER_OTHER_PO,),
    "unmatched_invoice": (MISTYPED_PO_NUMBER,),
    "unmatched_receipt": (MISTYPED_PO_NUMBER,),
}


# --------------------------------------------------------------------------- #
# Public interface
# --------------------------------------------------------------------------- #

def _run(spec: HypothesisSpec, subject: _Subject, index: _Index,
         strict: bool) -> tuple[_Outcome, CheckFailure | None]:
    try:
        return _CHECKS[spec.code](subject, index), None
    except Exception as error:  # a defect in this module: contained per check, recorded, never concluded from
        if strict:
            raise
        return (CheckStatus.FAILED, [], [CHECK_FAILED]), CheckFailure(spec.code, type(error).__name__)


def _investigate(index: _Index, exception: OpsException, strict: bool) -> Investigation:
    try:
        subject = _subject(exception, index)
    except Exception as error:  # as in _run: recorded for this exception only
        if strict:
            raise
        return Investigation(exception.exception_id, (), (), (INVESTIGATION_FAILED,),
                             (CheckFailure("subject", type(error).__name__),))
    specs = APPLICABLE.get(subject.kind, ()) if subject is not None else ()
    numbers: dict[Cite, str] = {}

    def ref(cite: Cite) -> str:
        if cite not in numbers:
            numbers[cite] = f"R{len(numbers) + 1}"
        return numbers[cite]

    checks, failures = [], []
    for position, spec in enumerate(specs, start=1):
        (status, raw, missing), failure = _run(spec, subject, index, strict)
        if failure is not None:
            failures.append(failure)
        observations = tuple(Observation(text, tuple(dict.fromkeys(ref(c) for c in cites))) for text, cites in raw)
        checks.append(HypothesisCheck(f"H{position}", spec.code, spec.statement, status, observations,
                                      tuple(missing), spec.how_to_confirm))
    related = tuple(RecordRef(number, *cite) for cite, number in numbers.items())
    all_missing = tuple(dict.fromkeys(text for check in checks for text in check.missing))
    return Investigation(exception.exception_id, related, tuple(checks), all_missing, tuple(failures))


def investigate_exception(result: InvestigationResult, exception: OpsException, *,
                          strict: bool = False) -> Investigation:
    """The investigation checks for one exception of ``result``. Neither argument is changed.

    An error inside a check gives that check the ``failed`` status and a ``CheckFailure``; ``strict=True``
    re-raises it instead (tests and diagnostics). Errors while indexing the run always propagate."""
    return _investigate(_Index(result), exception, strict)


def investigate_all(result: InvestigationResult, *, strict: bool = False) -> dict[str, Investigation]:
    """The investigation checks for every exception of ``result``, keyed by exception id (the data is indexed
    once). Neither the result nor its exceptions are changed. ``strict`` as in ``investigate_exception``."""
    index = _Index(result)
    return {exception.exception_id: _investigate(index, exception, strict) for exception in result.exceptions}


def statements() -> Iterable[str]:
    """Every fixed sentence the catalogue can show (statements, next steps, labels), for review and tests."""
    for spec in CATALOGUE:
        yield spec.statement
        yield spec.how_to_confirm
    yield from STATUS_LABELS.values()
    yield CHECK_FAILED
    yield INVESTIGATION_FAILED
