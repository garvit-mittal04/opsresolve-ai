from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from opsresolve.evaluation import (
    UNATTRIBUTED,
    EvaluationDatasetError,
    ExpectedException,
    Metrics,
    Scenario,
    compare,
    evaluate_dataset,
    f1_score,
    fmt_metric,
    load_expected,
    load_scenarios,
    main,
    render_markdown,
    safe_ratio,
)
from opsresolve.ingestion import load_dataset
from opsresolve.models import ExceptionType as T
from opsresolve.models import OpsException, Severity

EVAL_DIR = Path(__file__).resolve().parents[1] / "data" / "evaluation"


def scenario(sid="S1", pos=("P1",), limitation=False):
    return Scenario(scenario_id=sid, category="test", po_numbers=pos, known_limitation=limitation,
                    description="test scenario")


def expected(exc_type=T.PRICE_MISMATCH, po="P1", sku="A", amount="10.00", qty=None, sid="S1"):
    return ExpectedException(scenario_id=sid, exception_type=exc_type, po_number=po, sku=sku,
                             amount_impact=Decimal(amount),
                             quantity_difference=None if qty is None else Decimal(qty))


def predicted(exc_type=T.PRICE_MISMATCH, po="P1", sku="A", amount="10.00", qty=None, n=1):
    return OpsException(exception_id=f"EXC-{n:04d}", exception_type=exc_type, severity=Severity.LOW,
                        supplier_id="SUP", po_number=po, sku=sku, summary="s", recommended_action="a",
                        amount_impact=Decimal(amount),
                        quantity_difference=None if qty is None else Decimal(qty))


# ---- metric arithmetic ------------------------------------------------------

def test_metrics_basic():
    m = Metrics(tp=32, fp=3, fn=1)
    assert m.precision == pytest.approx(32 / 35)
    assert m.recall == pytest.approx(32 / 33)
    assert m.f1 == pytest.approx(64 / 68)  # 2TP / (2TP + FP + FN)
    assert m.support == 33


def test_zero_denominators_are_undefined_not_zero():
    assert safe_ratio(0, 0) is None
    nothing = Metrics()
    assert nothing.precision is None and nothing.recall is None and nothing.f1 is None
    only_fn = Metrics(fn=2)  # nothing predicted
    assert only_fn.precision is None and only_fn.recall == 0.0 and only_fn.f1 is None
    only_fp = Metrics(fp=2)  # nothing expected
    assert only_fp.precision == 0.0 and only_fp.recall is None and only_fp.f1 is None


def test_f1_edge_cases():
    assert f1_score(0.0, 0.0) == 0.0
    assert f1_score(1.0, 1.0) == 1.0
    assert f1_score(None, 1.0) is None
    assert Metrics(tp=0, fp=1, fn=1).f1 == 0.0


def test_metrics_addition_and_formatting():
    assert Metrics(1, 2, 3) + Metrics(4, 5, 6) == Metrics(5, 7, 9)
    assert fmt_metric(None) == "n/a"
    assert fmt_metric(2 / 3) == "0.667"


# ---- comparison -------------------------------------------------------------

def test_perfect_match():
    result = compare([expected()], [predicted()], [scenario()])
    assert result.overall == Metrics(tp=1)
    assert result.amounts_correct == 1
    assert result.scenarios[0].passed


def test_false_positive_and_false_negative():
    result = compare([expected(sku="A")], [predicted(sku="B")], [scenario()])
    assert result.overall == Metrics(fp=1, fn=1)
    assert not result.scenarios[0].passed


def test_type_mismatch_is_one_fp_and_one_fn():
    result = compare([expected(T.PRICE_MISMATCH)], [predicted(T.QUANTITY_MISMATCH)], [scenario()])
    by_type = result.by_type()
    assert by_type[T.PRICE_MISMATCH] == Metrics(fn=1)
    assert by_type[T.QUANTITY_MISMATCH] == Metrics(fp=1)


def test_multiset_counting_within_one_key():
    exp = [expected(amount="5.00"), expected(amount="7.00")]
    result = compare(exp, [predicted(amount="7.00")], [scenario()])
    assert result.overall == Metrics(tp=1, fn=1)
    assert result.amounts_correct == 1  # paired on the exact amount, not arbitrarily
    assert result.false_negatives[0].amount_impact == Decimal("5.00")


def test_wrong_amount_is_detected_but_not_exact():
    result = compare([expected(amount="10.00")], [predicted(amount="10.01")], [scenario()])
    assert result.overall == Metrics(tp=1)
    assert result.amounts_correct == 0
    assert not result.scenarios[0].passed


def test_quantity_checked_only_when_answer_key_gives_it():
    assert compare([expected(qty=None)], [predicted(qty="3")], [scenario()]).amounts_correct == 1
    assert compare([expected(qty="2")], [predicted(qty="3")], [scenario()]).amounts_correct == 0


def test_missing_po_or_sku_normalised_to_empty():
    exp = [expected(T.POSSIBLE_DUPLICATE_INVOICE, po="P1", sku="")]
    result = compare(exp, [predicted(T.POSSIBLE_DUPLICATE_INVOICE, po="P1", sku=None)], [scenario()])
    assert result.overall == Metrics(tp=1)


def test_predictions_outside_any_scenario_are_unattributed_false_positives():
    result = compare([], [predicted(po="ELSEWHERE"), predicted(po=None, n=2)], [scenario()])
    assert result.overall == Metrics(fp=2)
    assert len(result.unattributed) == 2
    assert "not attributable" in render_markdown(result)
    assert UNATTRIBUTED  # sentinel exported for callers


def test_clean_scenario_with_no_expectations_passes():
    result = compare([], [], [scenario()])
    assert result.overall == Metrics()
    assert result.scenarios[0].passed
    assert result.overall.f1 is None


# ---- dataset loading and validation -----------------------------------------

def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


SCEN_HEADER = "scenario_id,category,po_numbers,known_limitation,description\n"
EXP_HEADER = "scenario_id,exception_type,po_number,sku,quantity_difference,amount_impact,rationale\n"


def test_scenario_po_list_parsing(tmp_path):
    path = write(tmp_path, "s.csv", SCEN_HEADER + "S1,x,P1; P2,true,desc\n")
    [s] = load_scenarios(path)
    assert s.po_numbers == ("P1", "P2") and s.known_limitation is True


def test_po_shared_by_two_scenarios_rejected(tmp_path):
    path = write(tmp_path, "s.csv", SCEN_HEADER + "S1,x,P1,false,d\nS2,x,P1,false,d\n")
    with pytest.raises(EvaluationDatasetError, match="exactly one scenario"):
        load_scenarios(path)


def test_duplicate_scenario_id_rejected(tmp_path):
    path = write(tmp_path, "s.csv", SCEN_HEADER + "S1,x,P1,false,d\nS1,x,P2,false,d\n")
    with pytest.raises(EvaluationDatasetError, match="Duplicate scenario_id"):
        load_scenarios(path)


@pytest.mark.parametrize("row, message", [
    ("S1,NOT_A_TYPE,P1,A,,1.00,r", "exception_type"),
    ("S9,PRICE_MISMATCH,P1,A,,1.00,r", "unknown scenario"),
    ("S1,PRICE_MISMATCH,P2,A,,1.00,r", "not one of S1"),
    ("S1,PRICE_MISMATCH,P1,A,,abc,r", "amount_impact"),
])
def test_invalid_answer_key_rows_rejected(tmp_path, row, message):
    path = write(tmp_path, "e.csv", EXP_HEADER + row + "\n")
    with pytest.raises(EvaluationDatasetError, match=message):
        load_expected(path, [scenario()])


def test_missing_answer_key_file(tmp_path):
    with pytest.raises(EvaluationDatasetError, match="not found"):
        load_expected(tmp_path / "missing.csv", [scenario()])


# ---- the committed evaluation dataset ----------------------------------------

def test_evaluation_inputs_pass_validation():
    dataset = load_dataset(EVAL_DIR / "purchase_orders.csv", EVAL_DIR / "invoices.csv",
                           EVAL_DIR / "goods_received.csv")
    assert dataset.issues == []


def test_every_document_belongs_to_exactly_one_scenario():
    owned = {po for s in load_scenarios(EVAL_DIR / "scenarios.csv") for po in s.po_numbers}
    for name in ("purchase_orders.csv", "invoices.csv", "goods_received.csv"):
        pos = set(pd.read_csv(EVAL_DIR / name, dtype=str)["po_number"])
        assert pos <= owned, f"{name} has POs outside every scenario: {sorted(pos - owned)}"


def test_dataset_covers_required_scenario_types():
    scenarios = load_scenarios(EVAL_DIR / "scenarios.csv")
    categories = {s.category for s in scenarios}
    assert {"exact_match", "price_mismatch", "quantity_mismatch", "supplier_mismatch",
            "currency_mismatch", "missing_record", "duplicate_invoice", "partial_delivery",
            "multiple_discrepancies"} <= categories
    expected_types = {e.exception_type for e in load_expected(EVAL_DIR / "expected_exceptions.csv", scenarios)}
    assert expected_types == set(T)  # every existing exception category is exercised


def test_dataset_contains_only_synthetic_parties():
    pos = pd.read_csv(EVAL_DIR / "purchase_orders.csv", dtype=str)
    assert all(name.startswith("Synthetic Supplier ") for name in pos["supplier_name"])
    for name in ("purchase_orders.csv", "invoices.csv", "goods_received.csv"):
        suppliers = pd.read_csv(EVAL_DIR / name, dtype=str)["supplier_id"]
        assert all(s.startswith("SUP-E") for s in suppliers)


# ---- measured results on the committed dataset --------------------------------

@pytest.fixture(scope="module")
def evaluation():
    return evaluate_dataset(EVAL_DIR)


def test_measured_overall_results(evaluation):
    assert evaluation.validation_issues == []
    assert evaluation.overall == Metrics(tp=34, fp=1, fn=1)
    assert evaluation.amounts_correct == 34


def test_only_known_limitation_scenarios_fail(evaluation):
    failing = {s.scenario.scenario_id for s in evaluation.scenarios if not s.passed}
    flagged = {s.scenario.scenario_id for s in evaluation.scenarios if s.scenario.known_limitation}
    assert failing == flagged == {"S26", "S27"}


def test_known_limitation_details(evaluation):
    by_id = {s.scenario.scenario_id: s for s in evaluation.scenarios}
    assert [e.exception_type for e in by_id["S26"].false_negatives] == [T.POSSIBLE_DUPLICATE_INVOICE]
    assert [e.exception_type for e in by_id["S27"].false_positives] == [T.POSSIBLE_DUPLICATE_INVOICE]


def test_unit_of_measure_scenarios(evaluation):
    by_id = {s.scenario.scenario_id: s for s in evaluation.scenarios}
    assert all(by_id[sid].passed for sid in ("S32", "S33", "S34", "S35"))
    assert by_id["S32"].matched == [] and by_id["S34"].matched == []  # conversion removes false positives
    [unit] = by_id["S33"].matched
    assert unit.predicted.exception_type == T.UNIT_OF_MEASURE_MISMATCH
    [price] = by_id["S35"].matched
    assert price.predicted.exception_type == T.PRICE_MISMATCH and price.amount_correct


def test_measured_results_by_type(evaluation):
    by_type = evaluation.by_type()
    assert by_type[T.POSSIBLE_DUPLICATE_INVOICE] == Metrics(tp=3, fp=1, fn=1)
    assert by_type[T.QUANTITY_MISMATCH] == Metrics(tp=12, fp=0, fn=0)
    assert by_type[T.PRICE_MISMATCH] == Metrics(tp=7, fp=0, fn=0)
    assert by_type[T.UNIT_OF_MEASURE_MISMATCH] == Metrics(tp=1, fp=0, fn=0)
    for exc_type in (T.CURRENCY_MISMATCH, T.SUPPLIER_MISMATCH, T.MISSING_PURCHASE_ORDER,
                     T.MISSING_GOODS_RECEIPT, T.MISSING_INVOICE):
        assert by_type[exc_type].fp == 0 and by_type[exc_type].fn == 0


def test_report_is_reproducible(evaluation):
    again = evaluate_dataset(EVAL_DIR)
    assert render_markdown(evaluation, "data/evaluation") == render_markdown(again, "data/evaluation")


def test_cli_prints_and_writes_report(tmp_path, capsys):
    out = tmp_path / "eval.md"
    assert main(["--dataset-dir", str(EVAL_DIR), "--output", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "| F1 | 0.971 |" in printed
    assert out.read_text(encoding="utf-8") == printed


def test_cli_rejects_invalid_dataset(tmp_path, capsys):
    write(tmp_path, "scenarios.csv", SCEN_HEADER + "S1,x,P1,false,d\nS1,x,P2,false,d\n")
    assert main(["--dataset-dir", str(tmp_path)]) == 2
    assert "Invalid evaluation dataset" in capsys.readouterr().err
