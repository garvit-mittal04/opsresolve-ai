"""The evaluation set read through an import profile (milestone 7, acceptance gap G4: criteria 12 and 13).

tests/fixtures/profiles/evaluation_de is data/evaluation rewritten as a German ERP export (see its README).
Read with its profile it must give the same records and exceptions as the original files, and the same
score against the hand-written answer key in data/evaluation, which is read here and never changed.
Expected values are derived by hand from data/evaluation/scenarios.csv and expected_exceptions.csv.
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest

from opsresolve.evaluation import Metrics, compare, evaluate_dataset, load_expected, load_scenarios
from opsresolve.ingestion import load_dataset
from opsresolve.models import ExceptionType as T
from opsresolve.pipeline import investigate_files
from opsresolve.profiles import load_profile

REPO = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO / "data" / "evaluation"
FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
EVAL_DE = FIXTURES / "evaluation_de"
EVAL_DE_PROFILE = FIXTURES / "evaluation_de_profile.json"
FILES = ("purchase_orders", "invoices", "goods_received")
ALL_FILES = FILES + ("uom_conversions",)
ROWS = {"purchase_orders": 38, "invoices": 44, "goods_received": 38, "uom_conversions": 3}
UOM_POS = {"S32": "4600003201", "S33": "4600003301", "S34": "4600003401", "S35": "4600003501"}


def run(folder: Path, profile=None):
    return investigate_files(*[folder / f"{n}.csv" for n in FILES],
                             uom_conversions_path=folder / "uom_conversions.csv", profile=profile)


@pytest.fixture(scope="module")
def pair():
    return run(EVAL_DIR), run(EVAL_DE, load_profile(EVAL_DE_PROFILE))


# ---- the fixture is really in the declared format ---------------------------------------------------

@pytest.mark.parametrize("label", ALL_FILES)
def test_evaluation_de_fixture_is_written_in_the_declared_format(label):
    data = (EVAL_DE / f"{label}.csv").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")  # cp1252, not UTF-8: every header contains a single-byte umlaut
    text = data.decode("cp1252")
    lines = text.split("\n")
    assert lines[-1] == "" and "\r" not in text
    header, rows = lines[0].split(";"), [line.split(";") for line in lines[1:-1]]
    assert any(ch in lines[0] for ch in "äü") and "," not in lines[0]
    assert len(rows) == ROWS[label] and all(len(row) == len(header) for row in rows)
    body = "\n".join(lines[1:])
    assert not re.search(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", body)  # no ISO dates left
    if label != "uom_conversions":
        assert "Mengeneinheit" in header  # the mapped unit_of_measure column
        assert re.search(r";[0-9]{2}\.[0-9]{2}\.2026;", body)  # DD.MM.YYYY
        assert re.search(r";[0-9]+,[0-9]+;", body)  # decimal comma
    if label in ("purchase_orders", "invoices"):  # the only files with values of 1000 or more
        assert re.search(r";[0-9]{1,3}\.[0-9]{3}[;,]", body)  # thousands dot


def test_evaluation_de_fixture_does_not_load_without_its_profile():
    dataset = load_dataset(*[EVAL_DE / f"{n}.csv" for n in FILES], EVAL_DE / "uom_conversions.csv")
    assert [len(getattr(dataset, n).records) for n in ALL_FILES] == [0, 0, 0, 0]


# ---- equivalence with the original files --------------------------------------------------------------

def test_evaluation_de_records_are_identical_to_the_original(pair):
    original, reformatted = pair
    for label in ALL_FILES:
        before, after = getattr(original.dataset, label), getattr(reformatted.dataset, label)
        assert len(before.records) > 0
        # Every field of every record, including source_row and unit_of_measure.
        assert after.records == before.records
        assert (after.rows_read, after.rows_rejected, after.rejected_keys) == (
            before.rows_read, before.rows_rejected, before.rejected_keys)
        assert before.file_format is None and after.file_format.delimiter == ";"  # deliberate difference
    assert original.dataset.issues == reformatted.dataset.issues == []
    assert original.profile is None
    assert reformatted.profile.name == "Reformatted evaluation set, German ERP layout (test fixture)"


def test_evaluation_de_exceptions_are_identical_to_the_original(pair):
    """Criterion 13: exactly the same exceptions as the original files."""
    original, reformatted = pair
    assert len(original.exceptions) == 36
    # IDs, types, severities, exact amounts, quantities, notes and evidence (source, reference, line).
    assert [e.model_dump() for e in reformatted.exceptions] == [e.model_dump() for e in original.exceptions]
    assert reformatted.match == original.match
    assert reformatted.units == original.units


def test_evaluation_de_unit_of_measure_scenarios(pair):
    """Criterion 12: S32-S35 through mapped unit_of_measure columns and a profile-loaded conversion file."""
    original, reformatted = pair
    assert [(r.sku, r.uom, r.base_uom, r.units_per_uom) for r in reformatted.dataset.uom_conversions.records] == [
        ("EV-AE", "BOX", "EA", Decimal("12")), ("EV-AG", "BOX", "EA", Decimal("6")),
        ("EV-AH", "CASE", "EA", Decimal("24"))]
    by_po = {}
    for result, key in ((original, "original"), (reformatted, "reformatted")):
        for scenario, po in UOM_POS.items():
            by_po[key, scenario] = [e.model_dump() for e in result.exceptions if e.po_number == po]
            # Each scenario's documents state their units, read here through the mapped column.
            units = [r.unit_of_measure for name in FILES for r in getattr(result.dataset, name).records
                     if r.po_number == po]
            assert units and all(units), (scenario, units)
    for scenario in UOM_POS:
        assert by_po["reformatted", scenario] == by_po["original", scenario]
    found = {s: [(e["exception_type"], e["amount_impact"]) for e in by_po["reformatted", s]] for s in UOM_POS}
    assert found["S32"] == []  # 10 BOX of 12 at 24.00 = 120 EA at 2.00: no discrepancy
    assert found["S33"] == [(T.UNIT_OF_MEASURE_MISMATCH, Decimal("0"))]  # no CASE conversion for that SKU
    assert found["S34"] == []  # 4 BOX of 6 = 24 EA at 5.00
    assert found["S35"] == [(T.PRICE_MISMATCH, Decimal("24.00"))]  # 240 EA x (2.10 - 2.00)


# ---- score against the unchanged answer key -------------------------------------------------------------

def test_evaluation_de_scores_like_the_baseline(pair):
    """Criterion 13: the profile-loaded copy keeps the established result, including S36 and S27."""
    _, reformatted = pair
    scenarios = load_scenarios(EVAL_DIR / "scenarios.csv")
    expected = load_expected(EVAL_DIR / "expected_exceptions.csv", scenarios)
    scored = compare(expected, reformatted.exceptions, scenarios)
    baseline = evaluate_dataset(EVAL_DIR)

    assert scored.overall == baseline.overall == Metrics(tp=36, fp=0, fn=1)
    assert scored.amounts_correct == baseline.amounts_correct == 36
    assert scored.unattributed == baseline.unattributed == []

    def outline(result):
        return [(s.scenario.scenario_id, s.passed, len(s.matched), len(s.false_positives), len(s.false_negatives))
                for s in result.scenarios]
    assert outline(scored) == outline(baseline)

    by_id = {s.scenario.scenario_id: s for s in scored.scenarios}
    assert {sid for sid, s in by_id.items() if not s.passed} == {"S36"}
    # S36: a real duplicate whose records look exactly like legitimate S27, so it is still missed.
    assert [e.exception_type for e in by_id["S36"].false_negatives] == [T.POSSIBLE_DUPLICATE_INVOICE]
    assert by_id["S36"].false_positives == []
    [s36_quantity] = by_id["S36"].matched
    assert (s36_quantity.predicted.exception_type, s36_quantity.predicted.amount_impact) == (
        T.QUANTITY_MISMATCH, Decimal("160.00"))  # (200 - 100) x 1.60
    # S27: the quantity mismatch only, carrying the note that the records cannot be told apart.
    [s27] = by_id["S27"].matched
    assert (s27.predicted.exception_type, s27.predicted.amount_impact) == (T.QUANTITY_MISMATCH, Decimal("225.00"))
    for pair_ in (s27, s36_quantity):
        assert any("cannot tell these apart" in note for note in pair_.predicted.notes)
