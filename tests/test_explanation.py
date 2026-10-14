"""Evidence packs, the explanation contract and its validator (Milestone 8, Stage A).

No model, network or extra dependency is involved. Expected values are derived by hand from
data/sample (see its README), data/evaluation (scenarios S33 and S35) and the factories below.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from factories import grn, inv, po
from opsresolve.explanation import (
    MAX_CANDIDATE_CHARS,
    ConfirmedFact,
    EvidencePack,
    ExplanationOutput,
    LineClaim,
    NumberClaim,
    build_evidence_pack,
    canonical_json,
    money,
    pack_sha256,
    plain_decimal,
    read_claims,
    supplier_names,
    supported_values,
    validate_explanation,
)
from opsresolve.ingestion import Dataset, LoadResult
from opsresolve.models import Evidence, ExceptionType as T
from opsresolve.pipeline import investigate, investigate_files

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample"
EVALUATION = REPO / "data" / "evaluation"
SUPPLIER_NAMES = ("Northgate", "Bramwell", "Calder", "Delmore", "Fenwick", "Synthetic Supplier")


def run_folder(folder: Path, uom: bool = False):
    return investigate_files(folder / "purchase_orders.csv", folder / "invoices.csv", folder / "goods_received.csv",
                             uom_conversions_path=folder / "uom_conversions.csv" if uom else None)


@pytest.fixture(scope="module")
def sample():
    return run_folder(SAMPLE)


@pytest.fixture(scope="module")
def evaluation():
    return run_folder(EVALUATION, uom=True)


def find(result, exc_type: T, po_number: str):
    [found] = [e for e in result.exceptions if e.exception_type is exc_type and e.po_number == po_number]
    return found


def run_records(pos, invoices, receipts):
    return investigate(Dataset(LoadResult("purchase_orders", records=pos), LoadResult("invoices", records=invoices),
                               LoadResult("goods_received", records=receipts)))


def codes(check) -> list[tuple[str, str]]:
    return [(p.code, p.location) for p in check.problems]


# ---- evidence packs: one representative exception of every type ------------------------------------

PO, INV, GRN = "purchase_orders", "invoices", "goods_received"

# (exception type, PO, use evaluation data?, [(source, line, document)], quantity_difference, amount_impact)
CASES = [
    (T.QUANTITY_MISMATCH, "4500010003", False,
     [(PO, 6, "4500010003"), (INV, 6, "CIB-7702"), (GRN, 7, "5000050004")], "20", "135.00"),  # 20 x 6.75
    (T.PRICE_MISMATCH, "4500010004", False,
     [(PO, 7, "4500010004"), (INV, 7, "DH-INV-30418")], None, "387.50"),  # 250 x (19.95 - 18.40)
    (T.MISSING_GOODS_RECEIPT, "4500010005", False,
     [(PO, 9, "4500010005"), (INV, 9, "FES-118204")], "60", "6720.00"),  # 60 x 112.00
    (T.MISSING_INVOICE, "4500010006", False,
     [(PO, 10, "4500010006"), (GRN, 10, "5000050006")], "10000", "200.00"),  # 10000 x 0.02 accrual
    (T.POSSIBLE_DUPLICATE_INVOICE, "4500010007", False,
     [(INV, 10, "CIB-7781"), (INV, 21, "CIB-07781")], None, "2760.00"),  # 300 x 9.20
    (T.CURRENCY_MISMATCH, "4500010014", False,
     [(PO, 18, "4500010014"), (INV, 19, "FES-118412")], None, "0.00"),
    (T.SUPPLIER_MISMATCH, "4500010015", False,
     [(PO, 19, "4500010015"), (INV, 20, "RVX-00981")], None, "180.00"),  # 2000 x 0.09
    (T.MISSING_PURCHASE_ORDER, "4500010099", False,
     [(INV, 12, "DH-INV-30502")], None, "1690.00"),  # 2 x 845.00
    (T.UNIT_OF_MEASURE_MISMATCH, "4600003301", True,
     [(PO, 36, "4600003301"), (INV, 41, "E06-3301"), (GRN, 36, "7000003301")], None, "0.00"),  # S33
]


def test_the_cases_cover_every_exception_type():
    assert {case[0] for case in CASES} == set(T)


@pytest.mark.parametrize("exc_type, po_number, use_eval, rows, quantity_difference, amount", CASES,
                         ids=[case[0].value for case in CASES])
def test_pack_for_each_exception_type(sample, evaluation, exc_type, po_number, use_eval, rows,
                                      quantity_difference, amount):
    result = evaluation if use_eval else sample
    exc = find(result, exc_type, po_number)
    pack = build_evidence_pack(result, exc)

    facts = pack.exception
    assert (facts.exception_id, facts.exception_type, facts.po_number) == (exc.exception_id, exc_type.value, po_number)
    assert (facts.quantity_difference, facts.amount_impact) == (quantity_difference, amount)
    assert [(i.ref, i.source, i.source_row, i.reference) for i in pack.evidence] == [
        (f"E{n}", *row) for n, row in enumerate(rows, start=1)]
    assert all(i.record_available for i in pack.evidence)
    for item in pack.evidence:  # each reference points at the record on that line of that file
        number_field = {PO: "po_number", INV: "invoice_number", GRN: "grn_number"}[item.source]
        assert item.fields[number_field] == item.reference


def test_every_engine_evidence_row_maps_to_its_source_record(sample, evaluation):
    for result in (sample, evaluation):
        for exc in result.exceptions:
            pack = build_evidence_pack(result, exc)
            assert [(i.source, i.source_row, i.reference) for i in pack.evidence] == [
                (e.source, e.source_row, e.reference) for e in exc.evidence]
            assert [i.ref for i in pack.evidence] == [f"E{n}" for n in range(1, len(exc.evidence) + 1)]
            assert all(i.record_available for i in pack.evidence)


def test_record_fields_are_typed_values_without_names_or_descriptions(sample):
    exc = find(sample, T.MISSING_GOODS_RECEIPT, "4500010005")
    po_item, invoice_item = build_evidence_pack(sample, exc).evidence
    # purchase_orders.csv line 9 and invoices.csv line 9; no unit of measure is stated in the sample.
    assert po_item.fields == {"po_number": "4500010005", "line_number": "1", "supplier_id": "SUP-005",
                              "sku": "ELC-CBL-2.5", "quantity_ordered": "60", "unit_price": "112",
                              "currency": "USD", "order_date": "2026-08-12"}
    assert invoice_item.fields == {"invoice_number": "FES-118204", "invoice_date": "2026-08-29",
                                   "supplier_id": "SUP-005", "po_number": "4500010005", "sku": "ELC-CBL-2.5",
                                   "quantity_invoiced": "60", "unit_price": "112", "currency": "USD",
                                   "line_amount": "6720.00"}
    assert po_item.converted_fields == invoice_item.converted_fields == {}


def test_no_supplier_names_or_descriptions_are_included(sample, evaluation):
    for result in (sample, evaluation):
        for exc in result.exceptions:
            text = canonical_json(build_evidence_pack(result, exc)).decode()
            assert not any(name in text for name in SUPPLIER_NAMES), exc.exception_id
            assert "description" not in text and "supplier_name" not in text
    assert "Cable 2.5mm2" not in canonical_json(build_evidence_pack(sample, sample.exceptions[0])).decode()


def test_converted_records_keep_original_and_compared_values(evaluation):
    """S35: 240 EA x 2.10 invoiced against a PO of 10 CASE x 48.00, with 1 CASE = 24 EA."""
    exc = find(evaluation, T.PRICE_MISMATCH, "4600003501")
    _, invoice_item = build_evidence_pack(evaluation, exc).evidence
    assert invoice_item.fields["quantity_invoiced"] == "240" and invoice_item.fields["unit_price"] == "2.1"
    assert invoice_item.fields["unit_of_measure"] == "EA" and invoice_item.fields["line_amount"] == "504.00"
    # 240 / 24 = 10 CASE; 2.10 x 24 = 50.40 per CASE; 10 x 50.40 = 504.00
    assert invoice_item.converted_fields == {"quantity_invoiced": "10", "unit_price": "50.4",
                                             "unit_of_measure": "CASE", "line_amount": "504.00"}
    assert "1 CASE = 24 EA" in invoice_item.conversion
    assert build_evidence_pack(evaluation, exc).exception.amount_impact == "24.00"  # 10 x (50.40 - 48.00)


def test_identifying_values_list_supplier_ids_and_document_numbers(sample):
    pack = build_evidence_pack(sample, find(sample, T.POSSIBLE_DUPLICATE_INVOICE, "4500010007"))
    assert pack.identifying_values() == {"supplier_id": ("SUP-003",), "po_number": ("4500010007",),
                                         "invoice_number": ("CIB-07781", "CIB-7781"), "grn_number": ()}


@pytest.mark.parametrize("exc_type, po_number, label, sku", [
    (T.MISSING_INVOICE, "4500010011", "invoices", "FST-ANCH-10"),  # invoices line 15 rejected
    (T.MISSING_GOODS_RECEIPT, "4500010013", "goods_received", None),  # goods_received line 18 rejected
])
def test_rejected_input_rows_are_listed_as_limitations(sample, exc_type, po_number, label, sku):
    pack = build_evidence_pack(sample, find(sample, exc_type, po_number))
    assert len(pack.limitations) == 1
    assert pack.limitations[0].startswith(f"A {label} row for PO {po_number} / SKU {sku or ''}")
    assert "rejected during input validation" in pack.limitations[0]


def test_missing_purchase_order_rows_rejected_from_the_po_file_are_limitations(sample):
    for exc in [e for e in sample.exceptions if e.po_number == "4500010012"]:  # purchase_orders line 16
        assert build_evidence_pack(sample, exc).limitations == (
            "A purchase_orders row for PO 4500010012 / SKU BRG-6001 was rejected during input validation, "
            "so it is not part of this evidence.",)


# ---- missing or unavailable source records ------------------------------------------------------------

def test_unavailable_source_records_are_marked_and_listed(sample):
    exc = find(sample, T.MISSING_GOODS_RECEIPT, "4500010005")
    po_evidence, invoice_evidence = exc.evidence
    altered = exc.model_copy(update={"evidence": (
        po_evidence,
        invoice_evidence.model_copy(update={"source_row": 999}),  # no such line in invoices.csv
        Evidence(source="validation", reference="X", detail="not a record", source_row=None),
    )})
    pack = build_evidence_pack(sample, altered)
    assert [(i.ref, i.record_available, i.fields) for i in pack.evidence][1:] == [("E2", False, {}),
                                                                                 ("E3", False, {})]
    assert pack.evidence[0].record_available
    assert pack.limitations == ("The source record for E2 (invoices line 999) is not available.",
                                "The source record for E3 (validation, no line number) is not available.")


# ---- hashing --------------------------------------------------------------------------------------------

def test_hash_is_sha256_of_sorted_compact_json(sample):
    pack = build_evidence_pack(sample, sample.exceptions[0])
    data = canonical_json(pack)
    assert pack_sha256(pack) == hashlib.sha256(data).hexdigest()
    assert data == json.dumps(json.loads(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def test_same_input_gives_the_same_hash(sample):
    again = run_folder(SAMPLE)  # a second run: new objects and a different generated_at
    for first, second in zip(sample.exceptions, again.exceptions):
        assert pack_sha256(build_evidence_pack(sample, first)) == pack_sha256(build_evidence_pack(again, second))


def test_equal_decimals_written_differently_give_the_same_hash():
    # 100 vs 100.00 ordered, 10 vs 10.00 per unit: 120 invoiced, 100 received -> 20 units, 200.00.
    first = run_records([po(qty="100", price="10")], [inv(qty="120", price="10")], [grn(qty="100")])
    second = run_records([po(qty="100.00", price="10.00")], [inv(qty="120.0", price="10.00")], [grn(qty="100.0")])
    [a], [b] = first.exceptions, second.exceptions
    pack = build_evidence_pack(first, a)
    assert (pack.exception.quantity_difference, pack.exception.amount_impact) == ("20", "200.00")
    assert pack_sha256(pack) == pack_sha256(build_evidence_pack(second, b))


@pytest.mark.parametrize("changed", [
    {"invoice": {"qty": "121"}},  # a different difference and amount
    {"invoice": {"on": date(2026, 8, 11)}},  # only an evidence field changes
    {"invoice": {"row": 3}},  # the same values on another source line
])
def test_a_change_in_the_evidence_changes_the_hash(changed):
    base = run_records([po()], [inv(qty="120")], [grn()])
    other = run_records([po()], [inv(**{"qty": "120", **changed["invoice"]})], [grn()])
    assert pack_sha256(build_evidence_pack(base, base.exceptions[0])) != pack_sha256(
        build_evidence_pack(other, other.exceptions[0]))


def test_serialization_helpers():
    assert [plain_decimal(Decimal(v)) for v in ("112.00", "1E+2", "0.020", "-0", "-0.00")] == [
        "112", "100", "0.02", "0", "0"]
    assert [money(Decimal(v)) for v in ("6720", "2.005", "-50", "0")] == ["6720.00", "2.01", "-50.00", "0.00"]


# ---- inputs are not changed -----------------------------------------------------------------------------

def test_building_a_pack_does_not_change_the_result(sample):
    before = (copy.deepcopy([e.model_dump() for e in sample.exceptions]),
              [r.model_dump() for r in sample.dataset.invoices.records],
              copy.deepcopy(sample.dataset.invoices.rejected_keys), len(sample.match.matched))
    for exc in sample.exceptions:
        build_evidence_pack(sample, exc)
    after = ([e.model_dump() for e in sample.exceptions], [r.model_dump() for r in sample.dataset.invoices.records],
             sample.dataset.invoices.rejected_keys, len(sample.match.matched))
    assert after == before


def test_packs_are_immutable(sample):
    pack = build_evidence_pack(sample, sample.exceptions[0])
    with pytest.raises(ValidationError):
        pack.limitations = ("changed",)
    with pytest.raises(ValidationError):
        pack.evidence[0].source_row = 1


# ---- validation -----------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def receipt_pack(sample) -> EvidencePack:
    """MISSING_GOODS_RECEIPT for PO 4500010005: E1 is the PO line, E2 invoice FES-118204."""
    return build_evidence_pack(sample, find(sample, T.MISSING_GOODS_RECEIPT, "4500010005"))


def good(pack: EvidencePack) -> dict:
    return {
        "exception_id": pack.exception.exception_id,
        "direction": pack.exception.direction,
        "explanation": ("Invoice FES-118204 bills 60 units at 112.00 USD (6,720.00 USD) against PO 4500010005, "
                        "but no goods receipt is recorded for this PO line (E1, E2)."),
        "confirmed_facts": [
            {"statement": "PO 4500010005 orders 60 units at 112.00 USD.", "evidence_refs": ["E1"]},
            {"statement": "Invoice FES-118204 dated 2026-08-29 bills 60 units for 6720 USD.",
             "evidence_refs": ["E2"]},
        ],
        "possible_causes": [{
            "hypothesis": "The goods may have arrived without a goods receipt being posted.",
            "status": "unverified_hypothesis", "supporting_refs": ["E2"],
            "how_to_check": "Ask the receiving location for the delivery note for PO 4500010005.",
        }],
        "investigation_steps": [{"step": "Confirm with the warehouse whether the reels arrived.",
                                 "evidence_refs": ["E1"]}],
        "evidence_refs_used": ["E1", "E2"],
        "sufficiency": "partial",
        "missing_information": ["Delivery status from the receiving location."],
    }


def with_(candidate: dict, path: str, value):
    """A copy of ``candidate`` with one value replaced; path parts are keys or list indexes."""
    result = copy.deepcopy(candidate)
    *parents, last = [int(p) if p.isdigit() else p for p in path.split(".")]
    target = result
    for part in parents:
        target = target[part]
    target[last] = value
    return result


def test_a_valid_explanation_is_accepted(receipt_pack):
    check = validate_explanation(receipt_pack, good(receipt_pack))
    assert check.accepted and check.problems == ()
    assert check.pack_sha256 == pack_sha256(receipt_pack)
    assert check.explanation.confirmed_facts[1].evidence_refs == ("E2",)
    assert check.explanation.possible_causes[0].status == "unverified_hypothesis"


@pytest.mark.parametrize("encode", [json.dumps, lambda c: json.dumps(c).encode("utf-8")])
def test_json_text_is_accepted(receipt_pack, encode):
    assert validate_explanation(receipt_pack, encode(good(receipt_pack))).accepted


def test_wrong_exception_id_is_rejected(receipt_pack):
    check = validate_explanation(receipt_pack, with_(good(receipt_pack), "exception_id", "EXC-9999"))
    assert not check.accepted and check.explanation is None
    assert codes(check) == [("exception_id_mismatch", "exception_id")]


@pytest.mark.parametrize("path, value, expected", [
    # The fact's 112.00 is only in E1, which it no longer cites, so that number is unsupported too.
    ("confirmed_facts.0.evidence_refs", ["E3"], [("unknown_reference", "confirmed_facts[0].evidence_refs[0]"),
                                              ("unsupported_number", "confirmed_facts[0].statement")]),
    ("possible_causes.0.supporting_refs", ["E9"], [("unknown_reference", "possible_causes[0].supporting_refs[0]")]),
    ("investigation_steps.0.evidence_refs", ["E3"],
     [("unknown_reference", "investigation_steps[0].evidence_refs[0]")]),
    ("evidence_refs_used", ["E1", "E2", "E3"], [("unknown_reference", "evidence_refs_used[2]")]),
    ("explanation", "No goods receipt matches the invoice (see E7).", [("unknown_reference", "explanation")]),
    ("evidence_refs_used", ["E1"], [("unlisted_reference", "confirmed_facts[1].evidence_refs[0]"),
                                    ("unlisted_reference", "possible_causes[0].supporting_refs[0]")]),
    ("evidence_refs_used", ["E1", "E2", "E2"], [("duplicate_reference", "evidence_refs_used")]),
])
def test_references_must_exist_and_be_listed(receipt_pack, path, value, expected):
    check = validate_explanation(receipt_pack, with_(good(receipt_pack), path, value))
    assert not check.accepted and codes(check) == expected


def test_identifiers_that_start_with_e_are_not_references(evaluation):
    """Invoice E06-3301 (S33) is a document number, not an evidence reference."""
    pack = build_evidence_pack(evaluation, find(evaluation, T.UNIT_OF_MEASURE_MISMATCH, "4600003301"))
    candidate = {
        "exception_id": pack.exception.exception_id, "direction": pack.exception.direction,
        "explanation": "Invoice E06-3301 is in EA while PO 4600003301 and GRN 7000003301 are in CASE.",
        "confirmed_facts": [{"statement": "Invoice E06-3301 bills 30 EA at 10.00 USD.", "evidence_refs": ["E2"]}],
        "possible_causes": [], "investigation_steps": [], "evidence_refs_used": ["E2"],
        "sufficiency": "partial", "missing_information": ["How many EA make one CASE of EV-AF."],
    }
    assert validate_explanation(pack, candidate).accepted


@pytest.mark.parametrize("path, value, location", [
    ("confirmed_facts.0.evidence_refs", [], "confirmed_facts[0].evidence_refs"),
    ("confirmed_facts.0", {"statement": "PO 4500010005 orders 60 units."}, "confirmed_facts[0].evidence_refs"),
    ("confirmed_facts.0.evidence_refs", ["e1"], "confirmed_facts[0].evidence_refs[0]"),
    ("possible_causes.0.status", "confirmed", "possible_causes[0].status"),
    ("possible_causes.0.supporting_refs", [], "possible_causes[0].supporting_refs"),
    ("sufficiency", "certain", "sufficiency"),
    ("explanation", 6720, "explanation"),
    ("explanation", "   ", "explanation"),
    ("explanation", "x" * 801, "explanation"),
    ("confirmed_facts", [{"statement": "PO 4500010005.", "evidence_refs": ["E1"]}] * 11, "confirmed_facts"),
    ("amount_impact", "6720.00", "amount_impact"),  # no amount fields: the engine owns every figure
    ("confirmed_facts.0.confidence", "high", "confirmed_facts[0].confidence"),
])
def test_schema_violations_and_unexpected_fields_are_rejected(receipt_pack, path, value, location):
    check = validate_explanation(receipt_pack, with_(good(receipt_pack), path, value))
    assert not check.accepted
    assert ("schema", location) in codes(check)
    assert {code for code, _ in codes(check)} == {"schema"}


def test_missing_required_field_is_rejected(receipt_pack):
    candidate = good(receipt_pack)
    del candidate["missing_information"]
    assert codes(validate_explanation(receipt_pack, candidate)) == [("schema", "missing_information")]


@pytest.mark.parametrize("raw", ["not json", "[]", '{"exception_id": "EXC-0001"', "null", b"\xff\xfe"])
def test_malformed_output_is_rejected(receipt_pack, raw):
    check = validate_explanation(receipt_pack, raw)
    assert not check.accepted and check.problems and all(p.code == "schema" for p in check.problems)


def test_oversized_output_is_rejected_before_parsing(receipt_pack):
    check = validate_explanation(receipt_pack, " " * (MAX_CANDIDATE_CHARS + 1))
    assert codes(check) == [("too_large", "(root)")]


# ---- numbers ----------------------------------------------------------------------------------------------

def test_text_is_read_as_whole_tokens():
    claims = read_claims("Invoice FES-118204 for 6,720.00 USD (E2) dated 2026-08-29 on invoices line 9; "
                         "SKU ELC-CBL-2.5, 2.5 and 1,2; PO 4500010005; -50.00.")
    assert claims.refs == ("E2",)
    # Digits inside identifiers are not numbers; PO 4500010005 is an identifier in that context.
    assert claims.identifiers == ("FES-118204", "ELC-CBL-2.5", "4500010005")
    assert claims.dates == ("2026-08-29",)  # a date is one token, not 2026, 08 and 29
    assert claims.lines == (LineClaim("invoices line 9", "invoices", 9),)  # a line names its file
    assert claims.currencies == ("USD",)
    # The sign of -50.00 is kept.
    assert [c.written for c in claims.numbers] == ["6,720.00", "2.5", "1", "2", "-50.00"]
    assert claims.numbers[0] == NumberClaim("6,720.00", Decimal("6720"), False)
    assert claims.numbers[-1] == NumberClaim("-50.00", Decimal("-50"), True)
    assert read_claims("invoice E08-3501").identifiers == ("E08-3501",)
    assert read_claims("1.2.3").numbers == (NumberClaim("1.2.3", None, False),)  # not a number: never guessed


def test_supported_numbers_are_quantities_prices_and_amounts_only(receipt_pack):
    supported = supported_values(receipt_pack)
    # Line numbers (9), date parts (12, 29, 2026) and identifier digits (118204, 005) are not numbers.
    assert {Decimal(v) for v in ("9", "12", "29", "2026", "118204", "5")}.isdisjoint(supported.numbers)
    assert {Decimal("60"), Decimal("112"), Decimal("6720")} <= supported.numbers
    assert {("purchase_orders", 9), ("invoices", 9)} <= supported.lines and supported.po_lines == {1}
    assert {"2026-08-12", "2026-08-29"} <= supported.dates
    assert {"FES-118204", "4500010005", "SUP-005", "ELC-CBL-2.5"} <= supported.identifiers


@pytest.mark.parametrize("written", ["6720", "6,720", "6720.00", "6,720.00", "6720.0"])
def test_equal_numbers_in_any_format_are_accepted(receipt_pack, written):
    candidate = with_(good(receipt_pack), "confirmed_facts.1.statement", f"Invoice FES-118204 totals {written} USD.")
    assert validate_explanation(receipt_pack, candidate).accepted


@pytest.mark.parametrize("path, text, written", [
    ("confirmed_facts.1.statement", "Invoice FES-118204 totals 6,750.00 USD.", "6,750.00"),
    ("possible_causes.0.hypothesis", "A partial delivery of 45 units may not have been posted.", "45"),
    ("possible_causes.0.how_to_check", "Check the receiving log for the last 14 days.", "14"),
    ("investigation_steps.0.step", "Recount the 61 reels in the warehouse.", "61"),
    ("missing_information.0", "The receipt for the 59 reels delivered.", "59"),
])
def test_invented_numbers_are_rejected(receipt_pack, path, text, written):
    assert Decimal(written.replace(",", "")) not in supported_values(receipt_pack).numbers  # truly absent
    check = validate_explanation(receipt_pack, with_(good(receipt_pack), path, text))
    location = path.replace(".0", "[0]").replace(".1", "[1]")
    assert codes(check) == [("unsupported_number", location)]
    assert written in check.problems[0].message


# ---- D: numbers are checked against the evidence a fact cites ------------------------------------------------

@pytest.mark.parametrize("path", ["confirmed_facts.0.statement", "explanation"])
def test_a_line_number_cannot_justify_a_quantity(receipt_pack, path):
    """Line 9 of both files: '9 units' used to pass because 9 appeared as a source line."""
    assert {item.source_row for item in receipt_pack.evidence} == {9}
    check = validate_explanation(receipt_pack, with_(good(receipt_pack), path, "Only 9 units were delivered."))
    assert codes(check) == [("unsupported_number", path.replace(".0", "[0]"))]


@pytest.mark.parametrize("text", ["12 reels were returned.", "2026 reels were ordered.", "118204 units were billed."])
def test_date_and_identifier_digits_cannot_justify_a_quantity(receipt_pack, text):
    """12 and 2026 are parts of the order date 2026-08-12; 118204 is part of invoice FES-118204."""
    assert codes(validate_explanation(receipt_pack, with_(good(receipt_pack), "explanation", text))) == [
        ("unsupported_number", "explanation")]


def test_a_line_number_is_accepted_as_a_line_number(receipt_pack):
    candidate = with_(good(receipt_pack), "confirmed_facts.1.statement",
                      "Invoice FES-118204 is on invoices line 9 and bills 60 units.")
    assert validate_explanation(receipt_pack, candidate).accepted
    wrong = with_(candidate, "confirmed_facts.1.statement", "Invoice FES-118204 is on invoices line 10.")
    assert codes(validate_explanation(receipt_pack, wrong)) == [
        ("unsupported_line_number", "confirmed_facts[1].statement")]


@pytest.fixture(scope="module")
def price_pack(sample) -> EvidencePack:
    """PRICE_MISMATCH for PO 4500010004: E1 PO 250 x 18.40; E2 invoice DH-INV-30418 250 x 19.95 = 4,987.50."""
    return build_evidence_pack(sample, find(sample, T.PRICE_MISMATCH, "4500010004"))


def price_candidate(pack: EvidencePack, statement: str, refs: list[str]) -> dict:
    return {"exception_id": pack.exception.exception_id, "direction": pack.exception.direction,
            "explanation": "See the cited evidence.",
            "confirmed_facts": [{"statement": statement, "evidence_refs": refs}], "possible_causes": [],
            "investigation_steps": [], "evidence_refs_used": ["E1", "E2"], "sufficiency": "partial",
            "missing_information": ["Whether a price change was agreed."]}


def test_a_fact_may_only_use_numbers_and_dates_from_the_evidence_it_cites(price_pack):
    invoice_total = "The invoice totals 4,987.50 USD."  # 250 x 19.95, only on the invoice (E2)
    assert validate_explanation(price_pack, price_candidate(price_pack, invoice_total, ["E2"])).accepted
    assert codes(validate_explanation(price_pack, price_candidate(price_pack, invoice_total, ["E1"]))) == [
        ("unsupported_number", "confirmed_facts[0].statement")]
    invoice_date = "The invoice is dated 2026-08-28."  # invoices.csv line 7
    assert validate_explanation(price_pack, price_candidate(price_pack, invoice_date, ["E2"])).accepted
    assert codes(validate_explanation(price_pack, price_candidate(price_pack, invoice_date, ["E1"]))) == [
        ("unsupported_date", "confirmed_facts[0].statement")]


def test_the_exceptions_own_figures_support_every_fact(price_pack):
    # 387.50 = 250 x (19.95 - 18.40) is the engine's amount; 1.55 is in its recommended action.
    statement = "The difference of 1.55 USD per unit on 250 units is 387.50 USD."
    assert validate_explanation(price_pack, price_candidate(price_pack, statement, ["E1"])).accepted


@pytest.mark.parametrize("written", ["-50.00", "−50.00", "50.00", "50"])
def test_negative_amounts_match_by_magnitude(sample, written):
    """PO 4500010010: invoiced at 0.80 against 0.85 for 1000 units, an impact of -50.00 (signs are not checked)."""
    pack = build_evidence_pack(sample, find(sample, T.PRICE_MISMATCH, "4500010010"))
    assert pack.exception.amount_impact == "-50.00"
    candidate = price_candidate(pack, f"The amount impact is {written} USD.", ["E2"])
    assert validate_explanation(pack, candidate).accepted


# ---- E: identifiers and dates are whole tokens --------------------------------------------------------------

def identifier_pack(number: str, grn_number: str = "GRN-1") -> EvidencePack:
    """QUANTITY_MISMATCH: PO of 100 x 10.00, invoice ``number`` for 120, GRN for 100 -> 20 units, 200.00."""
    result = run_records([po()], [inv(number=number, qty="120")], [grn(number=grn_number)])
    return build_evidence_pack(result, result.exceptions[0])


def identifier_candidate(pack: EvidencePack, statement: str) -> dict:
    return {"exception_id": pack.exception.exception_id, "direction": pack.exception.direction,
            "explanation": "See the cited evidence.",
            "confirmed_facts": [{"statement": statement, "evidence_refs": ["E2"]}], "possible_causes": [],
            "investigation_steps": [], "evidence_refs_used": ["E2"], "sufficiency": "partial",
            "missing_information": ["The receiving count."]}


@pytest.mark.parametrize("in_pack, written", [("CIB-07781", "CIB-7781"), ("CIB-7781", "CIB-07781")])
def test_leading_zeros_in_identifiers_are_meaningful(in_pack, written):
    pack = identifier_pack(in_pack)
    assert validate_explanation(pack, identifier_candidate(pack, f"Invoice {in_pack} bills 120 units.")).accepted
    check = validate_explanation(pack, identifier_candidate(pack, f"Invoice {written} bills 120 units."))
    assert codes(check) == [("unsupported_identifier", "confirmed_facts[0].statement")]


def test_digits_only_identifiers_must_match_exactly():
    pack = identifier_pack("INV-1", grn_number="0050001")
    statement = "GRN {} records 100 units."
    candidate = identifier_candidate(pack, statement.format("0050001"))
    candidate["confirmed_facts"][0]["evidence_refs"] = candidate["evidence_refs_used"] = ["E3"]
    assert validate_explanation(pack, candidate).accepted
    without_zeros = with_(candidate, "confirmed_facts.0.statement", statement.format("50001"))
    assert codes(validate_explanation(pack, without_zeros)) == [
        ("unsupported_identifier", "confirmed_facts[0].statement")]


@pytest.mark.parametrize("text, code", [
    ("Invoice FES-118205 bills 60 units.", "unsupported_identifier"),  # one digit off
    ("Document CN-118204 was issued.", "unsupported_identifier"),  # invented, real digits
    ("The invoice is dated 2026-08-30.", "unsupported_date"),
    ("The invoice is dated 2026-13-29.", "unsupported_date"),  # not a valid date
    ("The PO was raised on 2026-08-29.", "unsupported_date"),  # a real date, but the invoice's (E2), not E1's
])
def test_invented_identifiers_and_dates_are_rejected(receipt_pack, text, code):
    check = validate_explanation(receipt_pack, with_(good(receipt_pack), "confirmed_facts.0.statement", text))
    assert codes(check) == [(code, "confirmed_facts[0].statement")]


def test_dates_are_accepted_only_as_whole_iso_dates(receipt_pack):
    assert validate_explanation(receipt_pack, with_(good(receipt_pack), "explanation",
                                                    "The PO is dated 2026-08-12 and the invoice 2026-08-29.")).accepted
    # Other date formats are not read as dates, and their parts are not supported numbers.
    check = validate_explanation(receipt_pack, with_(good(receipt_pack), "explanation", "Invoiced on 29 August 2026."))
    assert codes(check) == [("unsupported_number", "explanation"), ("unsupported_number", "explanation")]


# ---- C: evidence-reference ranges ----------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["See E1-E2.", "See E1–E2.", "See E1 - E2 and (E1, E2)."])
def test_reference_ranges_are_references(receipt_pack, text):
    assert validate_explanation(receipt_pack, with_(good(receipt_pack), "explanation", text)).accepted


@pytest.mark.parametrize("text", ["See E1-E3.", "See E1–E3."])
def test_every_reference_in_a_range_must_exist(receipt_pack, text):
    assert codes(validate_explanation(receipt_pack, with_(good(receipt_pack), "explanation", text))) == [
        ("unknown_reference", "explanation")]


def test_document_numbers_starting_with_e_are_identifiers_not_references(evaluation):
    """S35: invoice E08-3501 is evidence E2 of the PRICE_MISMATCH for PO 4600003501."""
    pack = build_evidence_pack(evaluation, find(evaluation, T.PRICE_MISMATCH, "4600003501"))
    good_text = price_candidate(pack, "Invoice E08-3501 (E2) bills 240 EA at 2.10 USD.", ["E2"])
    assert validate_explanation(pack, good_text).accepted
    invented = price_candidate(pack, "Invoice E08-3502 bills 240 EA.", ["E2"])
    assert codes(validate_explanation(pack, invented)) == [("unsupported_identifier", "confirmed_facts[0].statement")]


# ---- A: model instances are validated again -------------------------------------------------------------------

def test_a_valid_instance_is_accepted(receipt_pack):
    instance = ExplanationOutput.model_validate(good(receipt_pack))
    check = validate_explanation(receipt_pack, instance)
    assert check.accepted and check.explanation == instance


def test_an_unvalidated_instance_cannot_bypass_the_schema(receipt_pack):
    data = good(receipt_pack)
    data["confirmed_facts"] = (ConfirmedFact.model_construct(statement="An uncited claim.", evidence_refs=()),)
    check = validate_explanation(receipt_pack, ExplanationOutput.model_construct(**data))
    assert codes(check) == [("schema", "confirmed_facts[0].evidence_refs")]


def test_malformed_nested_objects_are_rejected_without_crashing(receipt_pack):
    data = good(receipt_pack)
    data["confirmed_facts"] = [{"statement": "PO 4500010005.", "evidence_refs": [], "extra": 1}, "not an object"]
    data["sufficiency"] = "certain"
    check = validate_explanation(receipt_pack, ExplanationOutput.model_construct(**data))
    assert not check.accepted
    assert set(codes(check)) == {("schema", "confirmed_facts[0].evidence_refs"), ("schema", "confirmed_facts[0].extra"),
                                 ("schema", "confirmed_facts[1]"), ("schema", "sufficiency")}


@pytest.mark.parametrize("candidate", [None, 42, ["a", "list"], object()])
def test_other_input_types_are_rejected_cleanly(receipt_pack, candidate):
    check = validate_explanation(receipt_pack, candidate)
    assert not check.accepted and [p.code for p in check.problems] == ["schema"]


# ---- B: input lost without a PO line key -----------------------------------------------------------------------

def copy_sample(tmp_path: Path, invoices: str) -> Path:
    for name in ("purchase_orders.csv", "goods_received.csv"):
        (tmp_path / name).write_bytes((SAMPLE / name).read_bytes())
    (tmp_path / "invoices.csv").write_text(invoices, encoding="utf-8")
    return tmp_path


def sample_invoice_lines() -> list[str]:
    return (SAMPLE / "invoices.csv").read_text(encoding="utf-8").splitlines()


def test_a_missing_required_column_is_a_limitation(tmp_path):
    lines = sample_invoice_lines()
    drop = lines[0].split(",").index("unit_price")
    without = "\n".join(",".join(c for i, c in enumerate(line.split(",")) if i != drop) for line in lines) + "\n"
    result = run_folder(copy_sample(tmp_path, without))
    assert result.dataset.invoices.records == []  # no invoice could be read
    exc = find(result, T.MISSING_INVOICE, "4500010003")  # 380 received; its invoice (line 6) was not read
    pack = build_evidence_pack(result, exc)
    assert pack.limitations == ("The invoices file has file-level validation problems, so some or all of its "
                                "records are not available.",)
    candidate = {"exception_id": exc.exception_id, "direction": pack.exception.direction,
                 "explanation": "No invoice is recorded for PO 4500010003.",
                 "confirmed_facts": [], "possible_causes": [], "investigation_steps": [],
                 "evidence_refs_used": [], "sufficiency": "sufficient", "missing_information": []}
    assert codes(validate_explanation(pack, candidate)) == [("sufficiency_inconsistent", "sufficiency")]


def test_a_rejected_row_with_a_blank_sku_is_a_limitation(tmp_path):
    # invoices.csv line 6 is CIB-7702 for PO 4500010003 / BRG-6205; blank its SKU.
    lines = sample_invoice_lines()
    assert lines[5].startswith("CIB-7702,")
    lines[5] = lines[5].replace(",BRG-6205,", ",,")
    result = run_folder(copy_sample(tmp_path, "\n".join(lines) + "\n"))
    assert [(i.file, i.row, i.field) for i in result.dataset.issues if i.row == 6] == [("invoices", 6, "sku")]
    exc = find(result, T.MISSING_INVOICE, "4500010003")  # 380 received, the invoice row is gone
    pack = build_evidence_pack(result, exc)
    assert pack.limitations == ("Rejected invoices rows without a usable PO number or SKU (invoices line 6) could "
                                "not be linked to a PO line and may belong to this one.",)
    candidate = {"exception_id": exc.exception_id, "direction": pack.exception.direction,
                 "explanation": "GRN 5000050004 records 380 units.",
                 "confirmed_facts": [], "possible_causes": [], "investigation_steps": [],
                 "evidence_refs_used": [], "sufficiency": "sufficient", "missing_information": []}
    assert codes(validate_explanation(pack, candidate)) == [("sufficiency_inconsistent", "sufficiency")]
    assert validate_explanation(pack, {**candidate, "sufficiency": "insufficient",
                                       "missing_information": ["The rejected row on invoices line 6."]}).accepted


def test_conversion_file_problems_are_a_limitation_only_where_units_matter():
    from opsresolve.models import ValidationIssue
    lines = ([po(uom="BOX")], [inv(uom="EA")], [grn(uom="BOX")])  # EA cannot be compared with BOX
    conversions = LoadResult("uom_conversions", issues=[ValidationIssue(file="uom_conversions", row=2,
                                                                        field="units_per_uom", message="bad")])
    result = investigate(Dataset(LoadResult("purchase_orders", records=lines[0]),
                                 LoadResult("invoices", records=lines[1]),
                                 LoadResult("goods_received", records=lines[2]), conversions))
    [unit] = [e for e in result.exceptions if e.exception_type is T.UNIT_OF_MEASURE_MISMATCH]
    assert build_evidence_pack(result, unit).limitations == (
        "The uom_conversions file has validation problems, so a conversion for this SKU may be missing.",)
    plain = run_records([po()], [inv(qty="120")], [grn()])  # no units involved: no limitation
    assert build_evidence_pack(plain, plain.exceptions[0]).limitations == ()


# ---- sufficiency and missing information --------------------------------------------------------------------

def test_insufficient_or_partial_requires_missing_information(receipt_pack):
    for level in ("partial", "insufficient"):
        candidate = with_(with_(good(receipt_pack), "sufficiency", level), "missing_information", [])
        assert codes(validate_explanation(receipt_pack, candidate)) == [
            ("missing_information_required", "missing_information")]
    insufficient = with_(good(receipt_pack), "sufficiency", "insufficient")
    assert validate_explanation(receipt_pack, insufficient).accepted


def test_sufficient_must_not_list_missing_information(receipt_pack):
    candidate = with_(good(receipt_pack), "sufficiency", "sufficient")
    assert codes(validate_explanation(receipt_pack, candidate)) == [
        ("sufficiency_inconsistent", "missing_information")]
    assert validate_explanation(receipt_pack, with_(candidate, "missing_information", [])).accepted


def rejected_row_candidate(pack: EvidencePack, sufficiency: str, missing: list[str]) -> dict:
    """PO 4500010011: E1 is the PO line, E2 GRN 5000050012; the invoice row (line 15) was rejected."""
    return {
        "exception_id": pack.exception.exception_id, "direction": pack.exception.direction,
        "explanation": "GRN 5000050012 records 200 units for PO 4500010011 and no valid invoice is recorded.",
        "confirmed_facts": [{"statement": "GRN 5000050012 records 200 units received.", "evidence_refs": ["E2"]}],
        "possible_causes": [], "investigation_steps": [], "evidence_refs_used": ["E2"],
        "sufficiency": sufficiency, "missing_information": missing,
    }


def test_known_limitations_rule_out_sufficient(sample):
    pack = build_evidence_pack(sample, find(sample, T.MISSING_INVOICE, "4500010011"))
    assert pack.limitations
    check = validate_explanation(pack, rejected_row_candidate(pack, "sufficient", []))
    assert codes(check) == [("sufficiency_inconsistent", "sufficiency")]
    accepted = rejected_row_candidate(pack, "insufficient", ["The rejected invoice row."])
    assert validate_explanation(pack, accepted).accepted


def test_a_confirmed_fact_cannot_cite_an_unavailable_record(sample):
    exc = find(sample, T.MISSING_GOODS_RECEIPT, "4500010005")
    altered = exc.model_copy(update={"evidence": (exc.evidence[0], exc.evidence[1].model_copy(
        update={"source_row": 999}))})
    pack = build_evidence_pack(sample, altered)
    candidate = good(pack)
    # Without the invoice record its date is no longer in the pack, so the statement avoids it.
    candidate["confirmed_facts"][1]["statement"] = "Invoice FES-118204 bills 60 units for 6720 USD."
    candidate["missing_information"] = ["The invoice record on invoices line 999."]
    check = validate_explanation(pack, candidate)
    assert codes(check) == [("unavailable_reference", "confirmed_facts[1].evidence_refs[0]")]
    # Causes and steps may still point at it (for example, to say it must be found).
    candidate["confirmed_facts"] = candidate["confirmed_facts"][:1]
    assert validate_explanation(pack, candidate).accepted


def test_every_problem_is_reported_and_nothing_is_partially_accepted(receipt_pack):
    candidate = good(receipt_pack)
    candidate["exception_id"] = "EXC-9999"
    candidate["confirmed_facts"][0]["evidence_refs"] = ["E5"]
    candidate["confirmed_facts"][1]["statement"] = "Invoice FES-118204 totals 7,000.00 USD."
    candidate["missing_information"] = []
    check = validate_explanation(receipt_pack, candidate)
    assert check.explanation is None
    assert codes(check) == [("exception_id_mismatch", "exception_id"),
                            ("unknown_reference", "confirmed_facts[0].evidence_refs[0]"),
                            ("unsupported_number", "confirmed_facts[0].statement"),  # 112.00 is only in E1
                            ("unsupported_number", "confirmed_facts[1].statement"),
                            ("missing_information_required", "missing_information")]


def test_validation_does_not_change_its_inputs(receipt_pack):
    candidate = with_(good(receipt_pack), "exception_id", "EXC-9999")
    before, digest = copy.deepcopy(candidate), pack_sha256(receipt_pack)
    validate_explanation(receipt_pack, candidate)
    validate_explanation(receipt_pack, good(receipt_pack))
    assert candidate == before and pack_sha256(receipt_pack) == digest


# ---- final hardening: shared helpers ------------------------------------------------------------------------

def answer(pack: EvidencePack, *, explanation: str = "See the cited evidence.", facts=(), steps=(),
           sufficiency: str = "partial", missing=("Confirmation from the documents.",), direction=None) -> dict:
    """A candidate with the given facts [(statement, refs)] and steps; every cited reference is listed."""
    refs = sorted({r for _, cited in (*facts, *steps) for r in cited}, key=lambda r: int(r[1:]))
    return {"exception_id": pack.exception.exception_id,
            "direction": pack.exception.direction if direction is None else direction,
            "explanation": explanation,
            "confirmed_facts": [{"statement": s, "evidence_refs": list(r)} for s, r in facts],
            "possible_causes": [],
            "investigation_steps": [{"step": s, "evidence_refs": list(r)} for s, r in steps],
            "evidence_refs_used": refs, "sufficiency": sufficiency, "missing_information": list(missing)}


def problems(pack: EvidencePack, candidate: dict, **kwargs) -> list[tuple[str, str]]:
    return codes(validate_explanation(pack, candidate, **kwargs))


FACT = "confirmed_facts[0].statement"


@pytest.fixture(scope="module")
def low_price_pack(sample) -> EvidencePack:
    """PO 4500010010: invoiced at 0.80 against 0.85 for 1000 units, an impact of -50.00 (README)."""
    return build_evidence_pack(sample, find(sample, T.PRICE_MISMATCH, "4500010010"))


@pytest.fixture(scope="module")
def quantity_pack(sample) -> EvidencePack:
    """PO 4500010003: E1 PO 400 x 6.75; E2 invoice CIB-7702 for 400; E3 GRN 5000050004 for 380."""
    return build_evidence_pack(sample, find(sample, T.QUANTITY_MISMATCH, "4500010003"))


def under_invoiced():
    """Invoiced 80, received 100, at 10.00: -20 units, an impact of -200.00 (LOW)."""
    result = run_records([po()], [inv(qty="80")], [grn(qty="100")])
    return build_evidence_pack(result, result.exceptions[0])


# ---- 1. direction and sign ------------------------------------------------------------------------------------

@pytest.mark.parametrize("exc_type, po_number, direction", [
    (T.PRICE_MISMATCH, "4500010004", "price_above_po"),  # 19.95 invoiced vs 18.40 ordered
    (T.PRICE_MISMATCH, "4500010010", "price_below_po"),  # 0.80 vs 0.85
    (T.QUANTITY_MISMATCH, "4500010003", "invoiced_more_than_received"),  # 400 vs 380
    (T.QUANTITY_MISMATCH, "4500010009", "received_more_than_ordered"),  # 165 received vs 150 ordered
    (T.MISSING_GOODS_RECEIPT, "4500010005", "invoiced_without_receipt"),
    (T.MISSING_INVOICE, "4500010006", "received_without_invoice"),
    (T.POSSIBLE_DUPLICATE_INVOICE, "4500010007", "possible_duplicate_billing"),
    (T.CURRENCY_MISMATCH, "4500010014", "not_applicable"),
    (T.SUPPLIER_MISMATCH, "4500010015", "not_applicable"),
    (T.MISSING_PURCHASE_ORDER, "4500010099", "not_applicable"),
])
def test_direction_comes_from_the_engines_signed_figures(sample, exc_type, po_number, direction):
    assert build_evidence_pack(sample, find(sample, exc_type, po_number)).exception.direction == direction


def test_under_invoicing_has_its_own_direction():
    pack = under_invoiced()
    assert (pack.exception.direction, pack.exception.quantity_difference, pack.exception.amount_impact) == (
        "invoiced_less_than_received", "-20", "-200.00")


def test_the_answer_must_restate_the_direction(low_price_pack):
    assert problems(low_price_pack, answer(low_price_pack, direction="price_above_po")) == [
        ("direction_mismatch", "direction")]


def test_an_undercharge_cannot_be_called_an_overcharge(low_price_pack):
    """The reported case: -50.00 (invoice below PO price) described as an overcharge."""
    overcharge = answer(low_price_pack, facts=[("The supplier overcharged 50.00 USD.", ["E2"])])
    assert problems(low_price_pack, overcharge) == [("unsupported_direction", FACT)]
    plus = answer(low_price_pack, facts=[("The amount impact is +50.00 USD.", ["E2"])])
    assert problems(low_price_pack, plus) == [("unsupported_number", FACT)]  # the sign contradicts -50.00
    under = answer(low_price_pack, facts=[("The supplier under-billed by 50.00 USD, -50.00 in total.", ["E2"])])
    assert validate_explanation(low_price_pack, under).accepted


def test_under_billing_cannot_be_called_over_billing():
    """The reported case: -200.00 (20 fewer units invoiced than received) described as over-billing."""
    pack = under_invoiced()
    wrong = answer(pack, facts=[("The supplier over-billed 20 units, 200.00 USD, and a credit note is due.", ["E2"])])
    assert problems(pack, wrong) == [("unsupported_direction", FACT), ("unsupported_direction", FACT)]
    assert problems(pack, answer(pack, facts=[("The impact is +200.00 USD.", ["E2"])])) == [
        ("unsupported_number", FACT)]
    right = answer(pack, facts=[("The supplier under-invoiced 20 units, -200.00 USD at PO price.", ["E2"])])
    assert validate_explanation(pack, right).accepted


def test_over_billing_and_credit_notes_are_accepted_where_the_engine_shows_them(price_pack):
    """PO 4500010004: 250 x (19.95 - 18.40) = 387.50 above PO price."""
    over = answer(price_pack, facts=[("The supplier overcharged 387.50 USD.", ["E2"])],
                  steps=[("Request a credit note for 387.50 USD.", ["E2"])])
    assert validate_explanation(price_pack, over).accepted
    assert problems(price_pack, answer(price_pack, facts=[("The supplier under-billed 387.50 USD.", ["E2"])])) == [
        ("unsupported_direction", FACT)]
    assert problems(price_pack, answer(price_pack, facts=[("The impact is -387.50 USD.", ["E2"])])) == [
        ("unsupported_number", FACT)]


@pytest.mark.parametrize("text", ["Request a credit note for the invoice.", "Reverse invoice FES-118204.",
                                  "Ask for a refund.", "This is not an overpayment."])
def test_credits_reversals_and_billing_words_are_rejected_where_direction_is_not_shown(receipt_pack, text):
    """A missing receipt shows no billing direction, so these are rejected (negations included, conservatively)."""
    assert receipt_pack.exception.direction == "invoiced_without_receipt"
    assert problems(receipt_pack, answer(receipt_pack, steps=[(text, ["E2"])])) == [
        ("unsupported_direction", "investigation_steps[0].step")]


def test_a_sign_on_a_record_value_must_match(receipt_pack):
    assert problems(receipt_pack, answer(receipt_pack, facts=[("The PO orders -60 units.", ["E1"])])) == [
        ("unsupported_number", FACT)]


# ---- 7. negative numbers with thousands separators -----------------------------------------------------------

@pytest.fixture(scope="module")
def big_undercharge() -> EvidencePack:
    """500 units at 8.00 invoiced against 10.00 ordered: an impact of 500 x -2.00 = -1,000.00.

    No record value is 1000, so the sign checks below are not masked by a coincidental quantity.
    """
    result = run_records([po(qty="500", price="10.00")], [inv(qty="500", price="8.00")], [grn(qty="500")])
    [exc] = [e for e in result.exceptions if e.exception_type is T.PRICE_MISMATCH]
    return build_evidence_pack(result, exc)


@pytest.mark.parametrize("written, accepted", [
    ("-1,000.00", True),  # signed, matches -1000.00
    ("−1,000.00", True),  # Unicode minus sign
    ("+1,000.00", False),  # signed, opposite sign
    ("1,000.00", True),  # unsigned: matched by size only, states no direction
    ("(1,000.00)", True),  # parentheses are not read as a sign, so this is unsigned too
    ("-1,000.01", False),  # unsupported magnitude
    ("-100.00", False),
])
def test_signed_numbers_with_thousands_separators(big_undercharge, written, accepted):
    assert big_undercharge.exception.amount_impact == "-1000.00"
    check = validate_explanation(big_undercharge, answer(
        big_undercharge, facts=[(f"The amount impact is {written} USD.", ["E2"])]))
    assert check.accepted is accepted
    if not accepted:
        assert codes(check) == [("unsupported_number", FACT)]


# ---- 2. evidence provenance ------------------------------------------------------------------------------------

def test_a_quantity_from_another_record_is_rejected(quantity_pack):
    """380 is the GRN's quantity (E3); invoice CIB-7702 (E2) is for 400."""
    wrong = answer(quantity_pack, facts=[("Invoice CIB-7702 bills 380 units.", ["E2"])])
    assert problems(quantity_pack, wrong) == [("unsupported_number", FACT)]
    assert validate_explanation(quantity_pack, answer(
        quantity_pack, facts=[("Invoice CIB-7702 bills 400 units.", ["E2"]),
                              ("GRN 5000050004 records 380 units.", ["E3"])])).accepted


def test_an_identifier_from_an_uncited_record_is_rejected(quantity_pack):
    wrong = answer(quantity_pack, facts=[("GRN 5000050004 is recorded for this PO line.", ["E1"])])
    assert problems(quantity_pack, wrong) == [("unsupported_identifier", FACT)]
    # The exception's own PO number, SKU and supplier id describe the PO line every fact is about.
    own = answer(quantity_pack, facts=[("Invoice CIB-7702 is for PO 4500010003, SKU BRG-6205, supplier SUP-003.",
                                        ["E2"])])
    assert validate_explanation(quantity_pack, own).accepted


@pytest.mark.parametrize("text, refs, expected", [
    ("GRN 5000050012 is on goods_received line 16.", ["E2"], []),
    ("The PO is on purchase_orders line 15.", ["E1"], []),
    ("GRN 5000050012 is on goods_received line 15.", ["E2"], [("unsupported_line_number", FACT)]),  # PO's line
    ("GRN 5000050012 is on purchase_orders line 16.", ["E2"], [("unsupported_line_number", FACT)]),  # GRN's line
    ("GRN 5000050012 is on line 16.", ["E2"], [("unsupported_line_number", FACT)]),  # no file named
    ("PO 4500010011 line 1 orders 200 units.", ["E1"], []),
    ("PO 4500010011 line 2 orders 200 units.", ["E1"], [("unsupported_line_number", FACT)]),
])
def test_a_line_must_name_its_file_and_belong_to_it(sample, text, refs, expected):
    """PO 4500010011 is purchase_orders.csv line 15 (PO line 1); GRN 5000050012 is goods_received.csv line 16."""
    pack = build_evidence_pack(sample, find(sample, T.MISSING_INVOICE, "4500010011"))
    candidate = answer(pack, facts=[(text, refs)], missing=("The rejected invoice row.",))
    check = validate_explanation(pack, candidate)
    assert codes(check) == expected
    if "on line 16" in text:
        assert "does not name its file" in check.problems[0].message


# ---- 3. digits-only identifiers ----------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["fact", "explanation"])
def test_a_po_number_is_not_a_quantity(receipt_pack, path):
    text = "The invoice bills 4500010005 units."
    if path == "fact":
        assert problems(receipt_pack, answer(receipt_pack, facts=[(text, ["E2"])])) == [("unsupported_number", FACT)]
    else:
        assert problems(receipt_pack, answer(receipt_pack, explanation=text)) == [("unsupported_number", "explanation")]
    assert validate_explanation(receipt_pack, answer(receipt_pack, explanation="PO 4500010005 has no receipt.")).accepted


def test_a_digits_only_sku_is_not_a_quantity():
    """SKU 100: 7 invoiced, 5 received; '100 units' is the SKU, not a quantity."""
    result = run_records([po(sku="100", qty="5")], [inv(sku="100", qty="7")], [grn(sku="100", qty="5")])
    pack = build_evidence_pack(result, result.exceptions[0])
    assert problems(pack, answer(pack, facts=[("The invoice bills 100 units.", ["E2"])])) == [
        ("unsupported_number", FACT)]
    assert problems(pack, answer(pack, explanation="The invoice bills 100 units.")) == [
        ("unsupported_number", "explanation")]
    assert validate_explanation(pack, answer(pack, facts=[("SKU 100 is invoiced for 7 units.", ["E2"])])).accepted


# ---- 4. number words and counts -----------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Seventy-five reels were delivered.", "½ of the reels arrived.",
                                  "Half of the reels arrived.", "Twenty-hundred reels."])
def test_unsupported_number_words_and_fractions_are_rejected(receipt_pack, text):
    assert problems(receipt_pack, answer(receipt_pack, explanation=text)) == [("unsupported_number", "explanation")]


def test_supported_number_words_and_counts_are_accepted(sample, receipt_pack):
    assert validate_explanation(receipt_pack, answer(
        receipt_pack, explanation="One invoice bills sixty reels and no receipt is recorded.")).accepted
    # PO 4500010007: invoices CIB-7781 and CIB-07781, each for 300 units.
    pack = build_evidence_pack(sample, find(sample, T.POSSIBLE_DUPLICATE_INVOICE, "4500010007"))
    assert pack.evidence_counts == {"invoices_rows": "2", "invoices_documents": "2"}
    for text in ("The two invoices CIB-7781 and CIB-07781 each bill 300 units.",
                 "The 2 invoices CIB-7781 and CIB-07781 each bill 300 units."):
        assert validate_explanation(pack, answer(pack, explanation=text)).accepted
    assert problems(pack, answer(pack, explanation="The three invoices each bill 300 units.")) == [
        ("unsupported_number", "explanation")]


# ---- 5. codes, currencies, units and names --------------------------------------------------------------------------

def test_a_changed_currency_is_rejected(receipt_pack):
    assert problems(receipt_pack, answer(receipt_pack, facts=[("Invoice FES-118204 bills 6,720.00 EUR.", ["E2"])])) == [
        ("unsupported_currency", FACT)]
    assert validate_explanation(receipt_pack, answer(
        receipt_pack, facts=[("Invoice FES-118204 bills 6,720.00 USD.", ["E2"])])).accepted


def test_a_unit_not_on_the_cited_record_is_rejected(evaluation):
    """S35: invoice E08-3501 is 240 EA (10 CASE after conversion); BOX appears nowhere."""
    pack = build_evidence_pack(evaluation, find(evaluation, T.PRICE_MISMATCH, "4600003501"))
    assert problems(pack, answer(pack, facts=[("Invoice E08-3501 bills 240 BOX.", ["E2"])])) == [
        ("unsupported_unit", FACT)]
    assert validate_explanation(pack, answer(
        pack, facts=[("Invoice E08-3501 bills 240 EA, compared as 10 CASE.", ["E2"])])).accepted


def test_ordinary_capitalised_words_and_lowercase_prose_are_not_codes(receipt_pack):
    text = "In this case the PO and the invoice are in USD; no GRN or SKU problem is stated."
    assert validate_explanation(receipt_pack, answer(receipt_pack, explanation=text)).accepted


@pytest.mark.parametrize("text", ["Fenwick Electrical Supply issued invoice FES-118204.",
                                  "fenwick  electrical supply issued the invoice.",
                                  "Northgate Fasteners shipped the reels."])  # "Northgate Fasteners Ltd" in the PO file
def test_known_supplier_names_are_rejected(sample, receipt_pack, text):
    names = supplier_names(sample)
    assert "Fenwick Electrical Supply" in names and "Northgate Fasteners Ltd" in names
    assert problems(receipt_pack, answer(receipt_pack, explanation=text), supplier_names=names) == [
        ("unsupported_name", "explanation")]
    assert validate_explanation(receipt_pack, answer(receipt_pack, explanation="The supplier issued the invoice."),
                                supplier_names=names).accepted
    assert not any(name in canonical_json(receipt_pack).decode() for name in names)  # never in the pack


# ---- 6. evidence-record identity ----------------------------------------------------------------------------------

def test_a_record_with_a_different_document_number_is_not_used(sample):
    """The engine's evidence says FES-118204, but invoices.csv line 12 holds DH-INV-30502."""
    exc = find(sample, T.MISSING_GOODS_RECEIPT, "4500010005")
    altered = exc.model_copy(update={"evidence": (exc.evidence[0],
                                                  exc.evidence[1].model_copy(update={"source_row": 12}))})
    pack = build_evidence_pack(sample, altered)
    item = pack.evidence[1]
    assert (item.reference, item.record_available, item.fields) == ("FES-118204", False, {})
    assert pack.limitations == ("The record on invoices line 12 does not have the document number FES-118204 "
                                "given for E2, so it is not used.",)
    assert "DH-INV-30502" not in canonical_json(pack).decode()
    mixed = answer(pack, facts=[("Invoice FES-118204 bills 60 units.", ["E2"])])
    assert problems(pack, mixed) == [("unavailable_reference", "confirmed_facts[0].evidence_refs[0]")]


# ---- ambiguity --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("exc_type, po_number", [
    (T.QUANTITY_MISMATCH, "4500010007"),  # its invoice is also flagged as a possible duplicate
    (T.QUANTITY_MISMATCH, "4500010008"),
    (T.POSSIBLE_DUPLICATE_INVOICE, "4500010008"),
])
def test_unresolved_ambiguity_rules_out_sufficient(sample, exc_type, po_number):
    pack = build_evidence_pack(sample, find(sample, exc_type, po_number))
    assert pack.ambiguities and pack.limitations == ()
    candidate = answer(pack, explanation="The evidence is listed.", sufficiency="sufficient", missing=())
    assert problems(pack, candidate) == [("sufficiency_inconsistent", "sufficiency")]


@pytest.mark.parametrize("exc_type, po_number", [
    (T.QUANTITY_MISMATCH, "4500010003"),  # note: impact valued at PO unit price
    (T.MISSING_INVOICE, "4500010006"),  # note: an accrual estimate, not an overpayment
])
def test_ordinary_notes_are_not_ambiguity(sample, exc_type, po_number):
    exc = find(sample, exc_type, po_number)
    pack = build_evidence_pack(sample, exc)
    assert exc.notes and pack.ambiguities == () and pack.limitations == ()
    candidate = answer(pack, explanation="The evidence is listed.", sufficiency="sufficient", missing=())
    assert validate_explanation(pack, candidate).accepted


def test_unit_price_difference_is_taken_from_the_two_records(sample, evaluation, price_pack, low_price_pack):
    assert price_pack.exception.unit_price_difference == "1.55"  # 19.95 - 18.40
    assert low_price_pack.exception.unit_price_difference == "-0.05"  # 0.80 - 0.85
    s35 = build_evidence_pack(evaluation, find(evaluation, T.PRICE_MISMATCH, "4600003501"))
    assert s35.exception.unit_price_difference == "2.4"  # 2.10 x 24 = 50.40 per CASE, less 48.00


# ---- boundaries ---------------------------------------------------------------------------------------------

def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
    return names


def test_explanation_module_needs_no_new_dependency_or_network():
    imported = imported_modules(REPO / "src" / "opsresolve" / "explanation.py")
    allowed_roots = {"__future__", "hashlib", "json", "re", "dataclasses", "datetime", "decimal", "typing",
                     "unicodedata", "pydantic", "opsresolve"}  # standard library and existing deps
    assert {name.split(".")[0] for name in imported} <= allowed_roots


def test_no_existing_module_uses_the_explanation_module():
    # The optional AI provider (ai.py, Stage B) is its only user; the engine, CLI, reports and app never use it.
    users = [path.name for path in [*(REPO / "src" / "opsresolve").glob("*.py"), REPO / "app.py"]
             if path.name != "explanation.py" and "opsresolve.explanation" in imported_modules(path)]
    assert users == ["ai.py"]
