"""Evidence packs and the explanation contract for future AI explanations (Milestone 8, Stage A).

Nothing here calls a model, reads files or formats output. Three pieces:

* ``build_evidence_pack`` turns one exception of an ``InvestigationResult`` into an ``EvidencePack``:
  the engine's facts about the exception plus its evidence rows, each with a pack-local reference
  (``E1``, ``E2``, ...) and the typed values of the source record it points to. The result is read,
  never changed.
* ``ExplanationOutput`` is the structured answer a future model must return. It has no amount
  fields: every figure stays the engine's. It must restate the engine's ``direction`` exactly.
* ``validate_explanation`` checks a candidate answer against its pack and returns an
  ``ExplanationCheck``: the explanation when every check passes, otherwise all problems found.
  A failed check rejects the whole explanation.

What validation checks. Structure; the exception id; the direction (``price_below_po`` and so on);
that every cited reference exists and that confirmed facts cite records that exist; and that
sufficiency is consistent with known limitations and unresolved ambiguity. Text is read as whole
tokens (``read_claims``):

* evidence references (``E2``, ``E1-E2``) must exist;
* identifiers (``FES-118204``, ``CIB-07781``) must match exactly; a digits-only token counts as an
  identifier only right after a word such as PO, invoice, GRN or SKU (``PO 4500010005``);
* ISO dates must be dates in the evidence; source lines must name their file (``invoices line 9``)
  and the file-and-line pair must be in the evidence; ``PO line 1`` must be a PO line number;
* numbers, written as digits or as a bounded set of number words and fractions (``seventy-five``,
  ``½``), must be quantities, prices, amounts or counts in the evidence. Line numbers, date parts and
  identifiers never count as numbers. A number written with a sign (``-50.00``, ``+50.00``) must match
  that sign; an unsigned number is matched by size only, so it states no direction;
* currency codes (``EUR``) and unit codes (``BOX``) from short explicit lists must be in the evidence;
* words for over- or under-billing, credits, refunds and reversals must agree with the direction;
  where the direction does not say which (a missing receipt, a duplicate, a supplier mismatch) they
  are rejected;
* supplier names known locally (passed in, never part of the pack) must not appear at all.

For a confirmed fact, numbers, dates, lines, currencies, units and identifiers must come from the
evidence it cites, or from the exception's own deterministic figures (difference, amount impact,
unit-price difference, evidence counts) and its own identifiers (exception id, PO number, SKU,
supplier id), which describe the PO line every fact is about. Other text may use anything in the pack.

These are safeguards against invented or misattributed references, identifiers, dates, figures,
codes and directions. They do not prove that a sentence is true. Known gaps: invented names that are
not in the local supplier list, and other words without digits, are not detected; negations ("not
overcharged") are rejected like the word itself; parentheses are not read as a minus sign. A person
must review every explanation; possible causes are unverified hypotheses by contract.

Further known limitations: numbers are not bound to their role, so a value supported by the evidence
can still be used as the wrong kind of value (a quantity presented as a price); currency and unit codes
written in lowercase are not checked; number words in the engine's own text count as support even when
they are not a numeric fact ("reject the later one" supports "one"); a direction stated in words outside
the explicit vocabulary is not detected; and supplier names are checked only when the local list is
passed to ``validate_explanation``, so invented or unlisted names can pass.

Serialization: all values in a pack are strings. Quantities, prices and other decimals are plain
decimal strings without exponent or trailing zeros (``Decimal("112.00")`` -> ``"112"``); money
amounts computed by the engine (``amount_impact``, an invoice ``line_amount``) have exactly two
decimal places, rounded half-up with ``models.round_money``; dates are ISO ``YYYY-MM-DD``.

Data minimisation: supplier names and item descriptions are never included. Supplier ids and
document numbers are, because the explanation must refer to them; ``EvidencePack.identifying_values``
lists them so that a provider layer can pseudonymise them before anything is transmitted.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Iterable, Literal, Mapping, Union, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from opsresolve.models import (
    ExceptionType,
    GoodsReceiptLine,
    InvoiceLine,
    OpsException,
    PurchaseOrderLine,
    round_money,
)
from opsresolve.pipeline import InvestigationResult

PACK_VERSION = 1

# Fields copied from each source record. Supplier names and descriptions are left out on purpose.
_RECORD_FIELDS = {
    "purchase_orders": ("po_number", "line_number", "supplier_id", "sku", "quantity_ordered", "unit_price",
                        "currency", "order_date", "unit_of_measure"),
    "invoices": ("invoice_number", "invoice_date", "supplier_id", "po_number", "sku", "quantity_invoiced",
                 "unit_price", "currency", "unit_of_measure"),
    "goods_received": ("grn_number", "received_date", "supplier_id", "po_number", "sku", "quantity_received",
                       "unit_of_measure"),
}
# Values the engine compared after converting a record to its PO line's unit of measure.
_CONVERTED_FIELDS = {
    "invoices": ("quantity_invoiced", "unit_price", "unit_of_measure"),
    "goods_received": ("quantity_received", "unit_of_measure"),
}
_DOCUMENT_NUMBER = {"purchase_orders": "po_number", "invoices": "invoice_number", "goods_received": "grn_number"}
_SOURCE_FILES = ("purchase_orders", "invoices", "goods_received")
# A rejected row with a problem in one of these fields cannot be linked to a PO line.
_LINE_KEY_FIELDS = ("po_number", "sku")

# What the engine's figures say about which way the discrepancy goes.
Direction = Literal[
    "price_above_po",  # invoice unit price above the PO price (amount impact > 0)
    "price_below_po",  # invoice unit price below the PO price (amount impact < 0)
    "invoiced_more_than_received",  # quantity difference > 0
    "invoiced_less_than_received",  # quantity difference < 0
    "received_more_than_ordered",  # over-receipt against the PO
    "invoiced_without_receipt",
    "received_without_invoice",
    "possible_duplicate_billing",
    "not_applicable",  # supplier, currency, unit or missing-PO exceptions
    "undetermined",  # the figures do not show a direction (e.g. an impact that rounds to 0.00)
]
DIRECTIONS: tuple[str, ...] = get_args(Direction)
_OVER_BILLING = frozenset({"price_above_po", "invoiced_more_than_received"})
_UNDER_BILLING = frozenset({"price_below_po", "invoiced_less_than_received"})

# Engine notes that state an unresolved ambiguity (see detection._check_duplicates / _link_duplicates).
_AMBIGUITY_MARKERS = ("cannot tell these apart", "cannot be distinguished", "cannot distinguish",
                      "possible duplicate")

Record = Union[PurchaseOrderLine, InvoiceLine, GoodsReceiptLine]


def plain_decimal(value: Decimal) -> str:
    """A decimal as a plain string: no exponent, no trailing zeros, no negative zero."""
    text = format((value + 0).normalize(), "f")  # "+ 0" turns -0 into 0
    return text


def money(value: Decimal) -> str:
    """A money amount rounded half-up to cents, always with two decimal places."""
    return format(round_money(value) + 0, "f")


def _text(value: Any) -> str:
    if isinstance(value, Decimal):
        return plain_decimal(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


# --------------------------------------------------------------------------- #
# Evidence pack
# --------------------------------------------------------------------------- #

class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceItem(_Frozen):
    """One evidence row of the exception, resolved to its source record when that record exists."""

    ref: str  # pack-local reference: E1, E2, ... in the engine's evidence order
    source: str  # file label, e.g. "invoices"
    source_row: int | None  # line in the original CSV file (header is line 1)
    reference: str  # document number as given by the engine's evidence
    record_available: bool
    fields: dict[str, str] = {}  # the record's values as written in the source file (see _RECORD_FIELDS)
    converted_fields: dict[str, str] = {}  # values compared after unit conversion; empty if not converted
    conversion: str | None = None  # the engine's description of that conversion


class ExceptionFacts(_Frozen):
    """The engine's own statement of the exception. Every figure here is computed deterministically."""

    exception_id: str
    exception_type: str
    severity: str
    direction: str  # one of DIRECTIONS
    supplier_id: str
    po_number: str | None
    sku: str | None
    summary: str
    expected: str | None
    actual: str | None
    quantity_difference: str | None
    amount_impact: str
    unit_price_difference: str | None  # PRICE_MISMATCH only: invoice price (as compared) minus PO price
    currency: str | None
    engine_recommended_action: str
    engine_notes: tuple[str, ...]


class EvidencePack(_Frozen):
    """Everything a model may see about one exception, and nothing else."""

    pack_version: int = PACK_VERSION
    exception: ExceptionFacts
    evidence: tuple[EvidenceItem, ...]
    evidence_counts: dict[str, str]  # e.g. {"invoices_rows": "2", "invoices_documents": "2"}
    limitations: tuple[str, ...]  # known gaps in the evidence (missing records, rejected or unreadable input)
    ambiguities: tuple[str, ...]  # unresolved ambiguity stated by the engine (e.g. a possible duplicate)

    @property
    def refs(self) -> dict[str, EvidenceItem]:
        return {item.ref: item for item in self.evidence}

    def identifying_values(self) -> dict[str, tuple[str, ...]]:
        """Supplier ids and document numbers in this pack, for pseudonymisation before transmission."""
        found: dict[str, set[str]] = {"supplier_id": {self.exception.supplier_id}, "po_number": set(),
                                      "invoice_number": set(), "grn_number": set()}
        if self.exception.po_number:
            found["po_number"].add(self.exception.po_number)
        for item in self.evidence:
            for name, values in found.items():
                if name in item.fields:
                    values.add(item.fields[name])
            if item.source in _DOCUMENT_NUMBER:
                found[_DOCUMENT_NUMBER[item.source]].add(item.reference)
        return {name: tuple(sorted(values)) for name, values in found.items()}


def canonical_json(pack: EvidencePack) -> bytes:
    """The pack as canonical JSON: sorted keys, no insignificant whitespace, UTF-8."""
    return json.dumps(pack.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def pack_sha256(pack: EvidencePack) -> str:
    """SHA-256 (hex) of the canonical JSON. Equal packs give equal hashes; any change in content changes it."""
    return hashlib.sha256(canonical_json(pack)).hexdigest()


def supplier_names(result: InvestigationResult) -> frozenset[str]:
    """Supplier names from the local purchase orders, for ``validate_explanation``. Never put in a pack."""
    return frozenset(po.supplier_name for po in result.dataset.purchase_orders.records)


def _loaded_files(result: InvestigationResult):
    dataset = result.dataset
    return (("purchase_orders", dataset.purchase_orders), ("invoices", dataset.invoices),
            ("goods_received", dataset.goods_received))


def _record_index(result: InvestigationResult) -> dict[tuple[str, int], Record]:
    """Source records by (file label, source row), as read from the input files (before any conversion)."""
    index: dict[tuple[str, int], Record] = {}
    for label, loaded in _loaded_files(result):
        for record in loaded.records:
            index[(label, record.source_row)] = record
    return index


def _converted_index(result: InvestigationResult) -> dict[tuple[str, int], Record]:
    """Records the engine compared after converting them to the PO line's unit of measure."""
    index: dict[tuple[str, int], Record] = {}
    for line in result.match.matched:
        for label, records in (("invoices", line.invoices), ("goods_received", line.receipts)):
            for record in records:
                if record.converted_from:
                    index[(label, record.source_row)] = record
    return index


def _fields(record: Record, names: tuple[str, ...]) -> dict[str, str]:
    values = {name: _text(getattr(record, name)) for name in names if getattr(record, name) is not None}
    if isinstance(record, InvoiceLine) and "quantity_invoiced" in names:
        values["line_amount"] = money(record.line_amount)
    return values


def _direction(exception: OpsException) -> str:
    """The direction stated by the engine's own signed figures; never inferred from a magnitude."""
    kind = exception.exception_type
    if kind is ExceptionType.PRICE_MISMATCH:
        sign = exception.amount_impact.compare(0)
        return {1: "price_above_po", -1: "price_below_po"}.get(int(sign), "undetermined")
    if kind is ExceptionType.QUANTITY_MISMATCH:
        difference = exception.quantity_difference
        sign = 0 if difference is None else int(difference.compare(0))
        if any(evidence.source == "invoices" for evidence in exception.evidence):
            return {1: "invoiced_more_than_received", -1: "invoiced_less_than_received"}.get(sign, "undetermined")
        return "received_more_than_ordered" if sign == 1 else "undetermined"  # receipts against the PO only
    return {ExceptionType.MISSING_GOODS_RECEIPT: "invoiced_without_receipt",
            ExceptionType.MISSING_INVOICE: "received_without_invoice",
            ExceptionType.POSSIBLE_DUPLICATE_INVOICE: "possible_duplicate_billing"}.get(kind, "not_applicable")


def _unit_price_difference(exception: OpsException, items: list[EvidenceItem]) -> str | None:
    """For a price mismatch: the invoice's compared unit price minus the PO price, from the two records."""
    if exception.exception_type is not ExceptionType.PRICE_MISMATCH:
        return None
    orders = [i for i in items if i.source == "purchase_orders" and i.record_available]
    invoices = [i for i in items if i.source == "invoices" and i.record_available]
    if len(orders) != 1 or len(invoices) != 1:
        return None
    invoice_price = invoices[0].converted_fields.get("unit_price", invoices[0].fields["unit_price"])
    return plain_decimal(Decimal(invoice_price) - Decimal(orders[0].fields["unit_price"]))


def _input_limitations(result: InvestigationResult, exception: OpsException, uses_units: bool) -> list[str]:
    """Input that was rejected or unreadable and may belong to this exception.

    A rejected row whose PO number and SKU were read is linked to its PO line. A whole file that could
    not be read, or a rejected row without a usable PO number or SKU, could belong to any PO line, so it
    is a limitation of every exception.
    """
    limitations = []
    key = (exception.po_number, exception.sku)
    for label, loaded in _loaded_files(result):
        if any(issue.row is None for issue in loaded.issues):
            limitations.append(f"The {label} file has file-level validation problems, so some or all of its "
                               f"records are not available.")
        if exception.po_number is not None and exception.sku is not None and key in loaded.rejected_keys:
            limitations.append(f"A {label} row for PO {key[0]} / SKU {key[1]} was rejected during input "
                               f"validation, so it is not part of this evidence.")
        unlinked = sorted({issue.row for issue in loaded.issues
                           if issue.row is not None and issue.field in _LINE_KEY_FIELDS})
        if unlinked:
            lines = ", ".join(f"{label} line {row}" for row in unlinked)
            limitations.append(f"Rejected {label} rows without a usable PO number or SKU ({lines}) could not "
                               f"be linked to a PO line and may belong to this one.")
    conversions = result.dataset.uom_conversions
    if uses_units and conversions is not None and conversions.issues:
        limitations.append("The uom_conversions file has validation problems, so a conversion for this SKU "
                           "may be missing.")
    return limitations


def _ambiguities(exception: OpsException) -> list[str]:
    found = []
    if exception.exception_type is ExceptionType.POSSIBLE_DUPLICATE_INVOICE:
        found.append("This is a possible duplicate: whether it is one is unresolved until a person confirms it.")
    found += [note for note in exception.notes if any(marker in note.lower() for marker in _AMBIGUITY_MARKERS)]
    return found


def build_evidence_pack(result: InvestigationResult, exception: OpsException) -> EvidencePack:
    """Build the evidence pack for one exception of ``result``. Neither argument is changed."""
    records = _record_index(result)
    converted = _converted_index(result)
    items: list[EvidenceItem] = []
    limitations: list[str] = []
    for number, evidence in enumerate(exception.evidence, start=1):
        ref = f"E{number}"
        key = (evidence.source, evidence.source_row)
        record = records.get(key) if evidence.source_row is not None else None
        if record is not None and getattr(record, _DOCUMENT_NUMBER[evidence.source]) != evidence.reference:
            # The loaded record is a different document than the engine's evidence names: never combine them.
            record = None
            limitations.append(f"The record on {evidence.source} line {evidence.source_row} does not have the "
                               f"document number {evidence.reference} given for {ref}, so it is not used.")
        elif record is None:
            where = (f"{evidence.source} line {evidence.source_row}" if evidence.source_row is not None
                     else f"{evidence.source}, no line number")
            limitations.append(f"The source record for {ref} ({where}) is not available.")
        if record is None:
            items.append(EvidenceItem(ref=ref, source=evidence.source, source_row=evidence.source_row,
                                      reference=evidence.reference, record_available=False))
            continue
        compared = converted.get(key)
        items.append(EvidenceItem(
            ref=ref, source=evidence.source, source_row=evidence.source_row, reference=evidence.reference,
            record_available=True, fields=_fields(record, _RECORD_FIELDS[evidence.source]),
            converted_fields=_fields(compared, _CONVERTED_FIELDS[evidence.source]) if compared else {},
            conversion=compared.converted_from if compared else None,
        ))

    uses_units = (exception.exception_type is ExceptionType.UNIT_OF_MEASURE_MISMATCH
                  or any(item.converted_fields for item in items))
    limitations += _input_limitations(result, exception, uses_units)
    counts: dict[str, str] = {}
    for source in _SOURCE_FILES:
        rows = [item for item in items if item.source == source]
        if rows:
            counts[f"{source}_rows"] = str(len(rows))
            counts[f"{source}_documents"] = str(len({item.reference for item in rows}))

    return EvidencePack(
        exception=ExceptionFacts(
            exception_id=exception.exception_id, exception_type=exception.exception_type.value,
            severity=exception.severity.value, direction=_direction(exception), supplier_id=exception.supplier_id,
            po_number=exception.po_number, sku=exception.sku, summary=exception.summary,
            expected=exception.expected, actual=exception.actual,
            quantity_difference=(None if exception.quantity_difference is None
                                 else plain_decimal(exception.quantity_difference)),
            amount_impact=money(exception.amount_impact),
            unit_price_difference=_unit_price_difference(exception, items), currency=exception.currency,
            engine_recommended_action=exception.recommended_action, engine_notes=tuple(exception.notes),
        ),
        evidence=tuple(items),
        evidence_counts=counts,
        limitations=tuple(limitations),
        ambiguities=tuple(_ambiguities(exception)),
    )


# --------------------------------------------------------------------------- #
# Explanation contract
# --------------------------------------------------------------------------- #

MAX_CANDIDATE_CHARS = 20_000
EvidenceRef = Annotated[str, StringConstraints(pattern=r"^E[1-9][0-9]{0,2}$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
Refs = Annotated[tuple[EvidenceRef, ...], Field(max_length=20)]


class _Contract(BaseModel):
    # An instance is validated again whenever it is validated, so one built with model_construct (which
    # skips validation) cannot pass unchecked.
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")


class ConfirmedFact(_Contract):
    statement: ShortText
    evidence_refs: Annotated[tuple[EvidenceRef, ...], Field(min_length=1, max_length=20)]


class PossibleCause(_Contract):
    """A hypothesis, never a finding. ``status`` makes that explicit in the data itself."""

    hypothesis: ShortText
    status: Literal["unverified_hypothesis"]
    supporting_refs: Annotated[tuple[EvidenceRef, ...], Field(min_length=1, max_length=20)]
    how_to_check: ShortText


class InvestigationStep(_Contract):
    step: ShortText
    evidence_refs: Refs = ()


class ExplanationOutput(_Contract):
    """The answer a model must give for one evidence pack. Deliberately has no amount fields."""

    exception_id: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    direction: Direction  # must equal the pack's direction exactly
    explanation: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]
    confirmed_facts: Annotated[tuple[ConfirmedFact, ...], Field(max_length=10)]
    possible_causes: Annotated[tuple[PossibleCause, ...], Field(max_length=5)]
    investigation_steps: Annotated[tuple[InvestigationStep, ...], Field(max_length=8)]
    evidence_refs_used: Refs
    sufficiency: Literal["sufficient", "partial", "insufficient"]
    missing_information: Annotated[tuple[ShortText, ...], Field(max_length=10)]


# --------------------------------------------------------------------------- #
# Reading text as whole tokens
# --------------------------------------------------------------------------- #

_REF = r"E[1-9][0-9]{0,2}"
_FILE_LABELS = "|".join(_SOURCE_FILES)
_NUMBER_END = r"(?![\w-]|[.,]\d)"
_TOKEN = re.compile(
    # Evidence references, alone or as a range: E2, E1-E2, E1 – E3. Not part of an identifier (E08-3501).
    rf"(?P<refs>(?<![\w-]){_REF}(?:\s*[-\u2013]\s*{_REF})*(?![\w-]))"
    # A PO line number: "PO line 1", "PO 4500010005 line 1".
    rf"|(?P<po_line>(?<![\w-])PO\s+(?:(?P<po_line_id>[^\W_]+(?:[-_/.][^\W_]+)*)\s+)?(?i:line)\s+"
    rf"(?P<po_line_no>\d+){_NUMBER_END})"
    # A source line, with or without its file: "invoices line 9", "goods_received.csv row 3", "line 9".
    rf"|(?P<line>(?<![\w-])(?:(?P<line_file>{_FILE_LABELS})(?:\.csv)?\s+(?:file\s+)?)?(?i:lines?|rows?)\s+"
    rf"(?P<line_no>\d+){_NUMBER_END})"
    # An ISO date.
    r"|(?P<date>(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-]))"
    # A number with thousands commas: 6,720.00 (a sign before it is read separately).
    rf"|(?P<grouped>(?<![\w.,])\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?{_NUMBER_END})"
    # Any other run of letters and digits, joined by - _ / or . (FES-118204, ELC-CBL-2.5, 4500010005, 2.5).
    r"|(?P<word>[^\W_]+(?:[-_/.][^\W_]+)*)"
)
_REF_IN_GROUP = re.compile(_REF)
# A digits-only token right after one of these is an identifier: "PO 4500010005", "invoice no. 118204".
_IDENTIFIER_CONTEXT = re.compile(
    r"(?i:\b(?:PO|P\.O\.|purchase\s+order|invoice|GRN|goods\s+receipt|receipt|SKU|item|supplier|exception)"
    r"\s*(?:number|no\.?|#)?\s*:?\s*)$")
_SIGNS = {"-": -1, "\u2212": -1, "+": 1}

# Number words and fractions that are read (bounded on purpose; "two hundred" is read as 2 and 100).
_NUMBER_WORDS = {word: Decimal(value) for value, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_TENS_WORDS = {word: Decimal(10 * value) for value, word in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2)}
_SCALE_WORDS = {"hundred": Decimal(100), "hundreds": Decimal(100), "thousand": Decimal(1000),
                "thousands": Decimal(1000), "dozen": Decimal(12), "dozens": Decimal(12), "half": Decimal("0.5")}

# Codes read as claims only when written in capitals exactly as listed; other capitalised words are prose.
CURRENCY_CODES = frozenset(
    "USD EUR GBP JPY CHF CAD AUD NZD CNY HKD SGD INR KRW SEK NOK DKK PLN CZK HUF RON TRY ZAR MXN BRL ARS CLP "
    "COP AED SAR ILS THB MYR IDR PHP VND".split())
UNIT_CODES = frozenset(
    "EA EACH PC PCS PCE BOX BX CASE CS CTN CARTON PK PACK PKT PAL PALLET ROLL RL REEL SET PR PAIR DZ DOZ BAG "
    "DRUM KG LB LBS TON MT CM MM KM FT YD LTR LT ML GAL SQM HR HRS".split())


@dataclass(frozen=True)
class NumberClaim:
    written: str
    value: Decimal | None  # None if it is not a number that can be read
    signed: bool  # written with an explicit sign; otherwise ``value`` is a magnitude


@dataclass(frozen=True)
class LineClaim:
    written: str
    file: str | None  # "purchase_orders" | "invoices" | "goods_received" | "po_line"; None if not named
    number: int


@dataclass(frozen=True)
class Claims:
    """What a text states, token by token."""

    refs: tuple[str, ...] = ()
    lines: tuple[LineClaim, ...] = ()
    dates: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    numbers: tuple[NumberClaim, ...] = ()
    currencies: tuple[str, ...] = ()
    units: tuple[str, ...] = ()


def _decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _sign(text: str, start: int) -> int:
    """+1 / -1 for a sign directly before ``start`` that is not part of a word or range (SUP-005, 5-6)."""
    if start >= 1 and text[start - 1] in _SIGNS:
        if start == 1 or not (text[start - 2].isalnum() or text[start - 2] in "_.,"):
            return _SIGNS[text[start - 1]]
    return 0


def _number_word(word: str) -> Decimal | None:
    lower = word.lower()
    if lower in _NUMBER_WORDS:
        return _NUMBER_WORDS[lower]
    if lower in _TENS_WORDS:
        return _TENS_WORDS[lower]
    if lower in _SCALE_WORDS:
        return _SCALE_WORDS[lower]
    if len(word) == 1 and unicodedata.numeric(word, None) is not None:  # e.g. ½ ¼ ¾ ⅓
        return Decimal(str(unicodedata.numeric(word)))
    parts = lower.split("-")
    if len(parts) == 2 and parts[0] in _TENS_WORDS and parts[1] in _NUMBER_WORDS and parts[1] != "zero":
        return _TENS_WORDS[parts[0]] + _NUMBER_WORDS[parts[1]]  # seventy-five
    return None


def _is_number_word(word: str) -> bool:
    if _number_word(word) is not None:
        return True
    parts = word.lower().split("-")  # e.g. "twenty-hundred": made of number words, but not readable
    return len(parts) > 1 and all(p in _NUMBER_WORDS or p in _TENS_WORDS or p in _SCALE_WORDS for p in parts)


def read_claims(text: str) -> Claims:
    """Split ``text`` into references, lines, ISO dates, identifiers, numbers, currencies and units.

    Tokens are whole: FES-118204 is one identifier (its digits are not a number), 2026-08-29 is one date
    and "invoices line 9" is one line reference. Words without a digit are ignored unless they are number
    words or listed currency or unit codes.
    """
    refs, lines, dates, identifiers, numbers, currencies, units = [], [], [], [], [], [], []

    def number(written: str, start: int) -> None:
        value, sign = _decimal(written), _sign(text, start)
        if value is not None and sign:
            value = value * sign
        numbers.append(NumberClaim(("-" if sign < 0 else "+" if sign > 0 else "") + written, value, bool(sign)))

    for match in _TOKEN.finditer(text):
        if match.group("refs"):
            refs.extend(_REF_IN_GROUP.findall(match.group("refs")))
        elif match.group("po_line"):
            lines.append(LineClaim(match.group("po_line"), "po_line", int(match.group("po_line_no"))))
            if match.group("po_line_id") and any(ch.isdigit() for ch in match.group("po_line_id")):
                identifiers.append(match.group("po_line_id"))
        elif match.group("line"):
            lines.append(LineClaim(match.group("line"), match.group("line_file"), int(match.group("line_no"))))
        elif match.group("date"):
            dates.append(match.group("date"))
        elif match.group("grouped"):
            number(match.group("grouped"), match.start())
        else:
            word = match.group("word")
            if any(ch.isdigit() for ch in word):
                if any(ch.isalpha() for ch in word) or any(ch in "-_/" for ch in word):
                    identifiers.append(word)
                elif _IDENTIFIER_CONTEXT.search(text, 0, match.start()):
                    identifiers.append(word)  # PO 4500010005, GRN 0050001
                else:
                    number(word, match.start())
            elif _is_number_word(word):
                value = _number_word(word)
                numbers.append(NumberClaim(word, value, False))
            elif word in CURRENCY_CODES:
                currencies.append(word)
            elif word in UNIT_CODES:
                units.append(word)
    return Claims(tuple(refs), tuple(lines), tuple(dates), tuple(identifiers), tuple(numbers),
                  tuple(currencies), tuple(units))


@dataclass(frozen=True)
class Supported:
    """Values a statement may use. ``numbers`` are signed quantities, prices, amounts and counts only."""

    numbers: frozenset[Decimal] = frozenset()
    dates: frozenset[str] = frozenset()
    identifiers: frozenset[str] = frozenset()
    lines: frozenset[tuple[str, int]] = frozenset()  # (file label, source line)
    po_lines: frozenset[int] = frozenset()
    currencies: frozenset[str] = frozenset()
    units: frozenset[str] = frozenset()

    def __or__(self, other: "Supported") -> "Supported":
        return Supported(self.numbers | other.numbers, self.dates | other.dates,
                         self.identifiers | other.identifiers, self.lines | other.lines,
                         self.po_lines | other.po_lines, self.currencies | other.currencies,
                         self.units | other.units)

    @property
    def magnitudes(self) -> frozenset[Decimal]:
        return frozenset(abs(value) for value in self.numbers)


_IDENTIFIER_FIELDS = frozenset({"po_number", "invoice_number", "grn_number", "sku", "supplier_id", "exception_id"})
_NUMBER_FIELDS = frozenset({"quantity_ordered", "quantity_invoiced", "quantity_received", "unit_price",
                            "line_amount", "quantity_difference", "amount_impact", "unit_price_difference"})
_DATE_FIELDS = frozenset({"order_date", "invoice_date", "received_date"})


def _identifier(value: str) -> Supported:
    """An identifier exactly as written, plus its tokens if it has several (a model may write one alone)."""
    claims = read_claims(value)
    parts = {*claims.identifiers, *(c.written for c in claims.numbers), *claims.dates}
    return Supported(identifiers=frozenset({value, *parts}))


def _from_value(name: str, value: str | None) -> Supported:
    """A named field's value, counted only as what it is."""
    if value is None:
        return Supported()
    if name in _IDENTIFIER_FIELDS:
        return _identifier(value)
    if name in _NUMBER_FIELDS:
        number = _decimal(value)
        return Supported(numbers=frozenset({number} if number is not None else ()))
    if name in _DATE_FIELDS:
        return Supported(dates=frozenset({value}))
    if name == "line_number":
        return Supported(po_lines=frozenset({int(value)}))
    if name == "currency":
        return Supported(currencies=frozenset({value}))
    if name == "unit_of_measure":
        return Supported(units=frozenset({value})) | _identifier(value)  # a unit such as M2 reads as an identifier
    return Supported()


def _from_text(text: str | None, identifiers: frozenset[str]) -> Supported:
    """Values stated in engine text. A number written exactly like a known identifier is not a number."""
    if not text:
        return Supported()
    claims = read_claims(text)
    return Supported(
        numbers=frozenset(c.value for c in claims.numbers if c.value is not None and c.written not in identifiers),
        dates=frozenset(claims.dates), identifiers=frozenset(claims.identifiers),
        lines=frozenset((c.file, c.number) for c in claims.lines if c.file in _SOURCE_FILES),
        po_lines=frozenset(c.number for c in claims.lines if c.file == "po_line"),
        currencies=frozenset(claims.currencies), units=frozenset(claims.units))


def _item_supported(item: EvidenceItem) -> Supported:
    supported = _identifier(item.reference)
    if item.source_row is not None:
        supported |= Supported(lines=frozenset({(item.source, item.source_row)}))
    for name, value in (*item.fields.items(), *item.converted_fields.items()):
        supported |= _from_value(name, value)
    return supported | _from_text(item.conversion, supported.identifiers)


def _exception_supported(pack: EvidencePack) -> Supported:
    """What every confirmed fact may use: the exception's own identifiers and deterministic figures."""
    facts = pack.exception
    supported = Supported(numbers=frozenset(Decimal(n) for n in pack.evidence_counts.values()))
    for name in ("exception_id", "supplier_id", "po_number", "sku", "quantity_difference", "amount_impact",
                 "unit_price_difference", "currency"):
        supported |= _from_value(name, getattr(facts, name))
    return supported


def supported_values(pack: EvidencePack) -> Supported:
    """Everything any statement about this pack may use (confirmed facts are narrower; see the module doc)."""
    supported = _exception_supported(pack)
    for item in pack.evidence:
        supported |= _item_supported(item)
    facts = pack.exception
    for text in (facts.summary, facts.expected, facts.actual, facts.engine_recommended_action, *facts.engine_notes,
                 *pack.limitations, *pack.ambiguities):
        supported |= _from_text(text, supported.identifiers)
    return supported


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Problem:
    code: str  # e.g. "schema", "unknown_reference", "unsupported_number"
    location: str  # e.g. "confirmed_facts[0].evidence_refs[1]"
    message: str


@dataclass(frozen=True)
class ExplanationCheck:
    """A checked candidate: ``explanation`` when every check passed, otherwise ``problems``."""

    pack_sha256: str
    explanation: ExplanationOutput | None
    problems: tuple[Problem, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.explanation is not None


# Words that state a billing direction or a remedy for over-billing. Matched whole, case-insensitively.
_OVER_WORDS = re.compile(r"(?i)\b(?:over[- ]?(?:charg|bill|invoic|pa(?:y|id))\w*|(?:charged|billed|invoiced)\s+"
                         r"too\s+much)")
_UNDER_WORDS = re.compile(r"(?i)\b(?:under[- ]?(?:charg|bill|invoic|pa(?:y|id))\w*|(?:charged|billed|invoiced)\s+"
                          r"too\s+little)")
_CREDIT_WORDS = re.compile(r"(?i)\b(?:credit(?:\s+note)?s?|refund\w*|revers\w*|rebate\w*)\b")
_LEGAL_SUFFIX = re.compile(r"(?i)[\s,]+(?:ltd\.?|limited|co\.?|company|inc\.?|llc|gmbh|plc|corp\.?|corporation|ag|"
                           r"s\.?a\.?|b\.?v\.?|n\.?v\.?)$")


def _texts(output: ExplanationOutput):
    """Every free-text field, with the evidence a confirmed fact cites (None: the whole pack applies)."""
    yield "explanation", output.explanation, None
    for i, fact in enumerate(output.confirmed_facts):
        yield f"confirmed_facts[{i}].statement", fact.statement, fact.evidence_refs
    for i, cause in enumerate(output.possible_causes):
        yield f"possible_causes[{i}].hypothesis", cause.hypothesis, None
        yield f"possible_causes[{i}].how_to_check", cause.how_to_check, None
    for i, step in enumerate(output.investigation_steps):
        yield f"investigation_steps[{i}].step", step.step, None
    for i, item in enumerate(output.missing_information):
        yield f"missing_information[{i}]", item, None


def _cited(output: ExplanationOutput):
    for i, fact in enumerate(output.confirmed_facts):
        for j, ref in enumerate(fact.evidence_refs):
            yield f"confirmed_facts[{i}].evidence_refs[{j}]", ref, True
    for i, cause in enumerate(output.possible_causes):
        for j, ref in enumerate(cause.supporting_refs):
            yield f"possible_causes[{i}].supporting_refs[{j}]", ref, False
    for i, step in enumerate(output.investigation_steps):
        for j, ref in enumerate(step.evidence_refs):
            yield f"investigation_steps[{i}].evidence_refs[{j}]", ref, False


def _schema_problems(error: ValidationError) -> list[Problem]:
    problems = []
    for detail in error.errors(include_url=False, include_input=False):
        location = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in detail["loc"])
        problems.append(Problem("schema", location.lstrip(".") or "(root)", detail["msg"]))
    return problems


def _parse(candidate: str | bytes | Mapping[str, Any] | ExplanationOutput,
           ) -> tuple[ExplanationOutput | None, list[Problem]]:
    if isinstance(candidate, (str, bytes)):
        if len(candidate) > MAX_CANDIDATE_CHARS:
            return None, [Problem("too_large", "(root)",
                                  f"Candidate is longer than {MAX_CANDIDATE_CHARS} characters.")]
        parse = ExplanationOutput.model_validate_json
    else:
        parse = ExplanationOutput.model_validate  # instances are validated again (revalidate_instances)
    try:
        return parse(candidate), []
    except ValidationError as error:
        return None, _schema_problems(error)


def _name_patterns(names: Iterable[str]) -> list[re.Pattern[str]]:
    """Whole-phrase, case-insensitive patterns for each name, with and without a legal suffix (Ltd, Co...)."""
    variants = set()
    for name in names:
        name = " ".join(name.split())
        if name:
            variants.add(name)
            short = _LEGAL_SUFFIX.sub("", name)
            if short and short != name:
                variants.add(short)
    return [re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, v.split())) + r"(?!\w)", re.IGNORECASE)
            for v in sorted(variants)]


def _check_text(location: str, text: str, supported: Supported, refs: Mapping[str, EvidenceItem],
                direction: str, names: list[re.Pattern[str]], problems: list[Problem]) -> None:
    claims = read_claims(text)
    for ref in claims.refs:
        if ref not in refs:
            problems.append(Problem("unknown_reference", location, f"{ref} is not in the pack."))
    for line in claims.lines:
        if line.file is None:
            problems.append(Problem("unsupported_line_number", location,
                                    f"'{line.written}' does not name its file (purchase_orders, invoices or "
                                    f"goods_received)."))
        elif (line.number not in supported.po_lines if line.file == "po_line"
              else (line.file, line.number) not in supported.lines):
            problems.append(Problem("unsupported_line_number", location,
                                    f"'{line.written}' is not a line in this evidence."))
    for written in claims.dates:
        if written not in supported.dates:
            problems.append(Problem("unsupported_date", location, f"{written} is not a date in this evidence."))
    for written in claims.identifiers:
        if written not in supported.identifiers:
            problems.append(Problem("unsupported_identifier", location,
                                    f"{written} is not an identifier in this evidence."))
    for claim in claims.numbers:
        if claim.value is None:
            ok = False
        elif claim.signed:
            ok = claim.value in supported.numbers
        else:
            ok = claim.value in supported.magnitudes
        if not ok:
            problems.append(Problem("unsupported_number", location,
                                    f"{claim.written} is not a quantity, price, amount or count in this evidence"
                                    + (" with that sign." if claim.signed else ".")))
    for code in claims.currencies:
        if code not in supported.currencies:
            problems.append(Problem("unsupported_currency", location, f"{code} is not a currency in this evidence."))
    for code in claims.units:
        if code not in supported.units:
            problems.append(Problem("unsupported_unit", location, f"{code} is not a unit in this evidence."))
    for pattern, allowed, kind in ((_OVER_WORDS, _OVER_BILLING, "over-billing"),
                                   (_CREDIT_WORDS, _OVER_BILLING, "a credit or reversal"),
                                   (_UNDER_WORDS, _UNDER_BILLING, "under-billing")):
        match = pattern.search(text)
        if match and direction not in allowed:
            problems.append(Problem("unsupported_direction", location,
                                    f"'{match.group()}' states {kind}, which the engine's direction "
                                    f"({direction}) does not support."))
    for pattern in names:
        match = pattern.search(text)
        if match:
            problems.append(Problem("unsupported_name", location,
                                    "A supplier name appears; names are not part of the evidence."))


def validate_explanation(pack: EvidencePack, candidate: str | bytes | Mapping[str, Any] | ExplanationOutput,
                         *, supplier_names: Iterable[str] = ()) -> ExplanationCheck:
    """Check a candidate explanation (JSON text, a mapping or an instance) against ``pack``. Pure.

    ``supplier_names`` are the locally known supplier names (see ``supplier_names(result)``); they are only
    searched for, never added to the pack. Returns the parsed explanation only if every check passes;
    otherwise every problem found, and the explanation must not be shown. Passing means the answer is
    consistent with the pack, not that it is true.
    """
    digest = pack_sha256(pack)
    output, problems = _parse(candidate)
    if output is None:
        return ExplanationCheck(digest, None, tuple(problems))

    if output.exception_id != pack.exception.exception_id:
        problems.append(Problem("exception_id_mismatch", "exception_id",
                                f"Expected {pack.exception.exception_id!r}, got {output.exception_id!r}."))
    if output.direction != pack.exception.direction:
        problems.append(Problem("direction_mismatch", "direction",
                                f"Expected {pack.exception.direction!r}, got {output.direction!r}."))

    items = pack.refs
    listed = set(output.evidence_refs_used)
    if len(listed) != len(output.evidence_refs_used):
        problems.append(Problem("duplicate_reference", "evidence_refs_used", "A reference is listed more than once."))
    for i, ref in enumerate(output.evidence_refs_used):
        if ref not in items:
            problems.append(Problem("unknown_reference", f"evidence_refs_used[{i}]", f"{ref} is not in the pack."))
    for location, ref, is_fact in _cited(output):
        if ref not in items:
            problems.append(Problem("unknown_reference", location, f"{ref} is not in the pack."))
            continue
        if ref not in listed:
            problems.append(Problem("unlisted_reference", location, f"{ref} is cited but not in evidence_refs_used."))
        if is_fact and not items[ref].record_available:
            problems.append(Problem("unavailable_reference", location,
                                    f"{ref} has no source record, so it cannot support a confirmed fact."))

    everything = supported_values(pack)
    exception_level = _exception_supported(pack)
    names = _name_patterns(supplier_names)
    for location, text, cited in _texts(output):
        if cited is None:
            supported = everything
        else:  # a confirmed fact: only what it cites, plus the exception's own identifiers and figures
            supported = exception_level
            for ref in cited:
                if ref in items:
                    supported |= _item_supported(items[ref])
        _check_text(location, text, supported, items, pack.exception.direction, names, problems)

    if output.sufficiency == "sufficient":
        if output.missing_information:
            problems.append(Problem("sufficiency_inconsistent", "missing_information",
                                    "Sufficiency is 'sufficient' but missing information is listed."))
        if pack.limitations:
            problems.append(Problem("sufficiency_inconsistent", "sufficiency",
                                    "The pack has known limitations, so sufficiency cannot be 'sufficient'."))
        if pack.ambiguities:
            problems.append(Problem("sufficiency_inconsistent", "sufficiency",
                                    "The engine states an unresolved ambiguity, so sufficiency cannot be "
                                    "'sufficient'."))
    elif not output.missing_information:
        problems.append(Problem("missing_information_required", "missing_information",
                                f"Sufficiency is {output.sufficiency!r}, so the missing information must be listed."))

    if problems:
        return ExplanationCheck(digest, None, tuple(problems))
    return ExplanationCheck(digest, output)
