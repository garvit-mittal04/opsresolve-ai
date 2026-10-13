"""Evaluation of exception detection against a labelled synthetic dataset.

Runs the unchanged reconciliation pipeline (default ``MatchConfig``) over an
evaluation dataset and compares its exceptions with a hand-written answer key.

Definitions
-----------
An exception is identified by its *match key*: ``(exception_type, po_number, sku)``.
Within one key, predicted and expected exceptions are compared as multisets:

* true positive (TP)  - a predicted exception paired with an expected one of the same key
* false positive (FP) - a predicted exception left unpaired
* false negative (FN) - an expected exception left unpaired

precision = TP / (TP + FP), recall = TP / (TP + FN), F1 = 2PR / (P + R).
A ratio whose denominator is zero is *undefined* and reported as ``None``
("n/a"), never silently as 0 or 1. F1 is ``None`` if either input is undefined
and 0.0 when precision and recall are both 0.

Detection is scored separately from calculation: a TP whose amount impact (or,
where the key gives one, quantity difference) differs from the answer key still
counts as detected, but not as an exact amount.

Usage: ``python -m opsresolve.evaluation [--dataset-dir DIR] [--output FILE]``
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from opsresolve.config import MatchConfig
from opsresolve.detection import fmt_money, fmt_qty
from opsresolve.models import ExceptionType, OpsException, ValidationIssue
from opsresolve.pipeline import investigate_files

DEFAULT_DATASET_DIR = Path("data/evaluation")
UNATTRIBUTED = "UNATTRIBUTED"

MatchKey = tuple[ExceptionType, str, str]


class EvaluationDatasetError(ValueError):
    """The evaluation dataset or answer key is malformed or inconsistent."""


# --------------------------------------------------------------------------- #
# Dataset models
# --------------------------------------------------------------------------- #

class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    scenario_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    po_numbers: tuple[str, ...] = Field(min_length=1)
    known_limitation: bool
    description: str = Field(min_length=1)

    @field_validator("po_numbers", mode="before")
    @classmethod
    def _split(cls, value):
        if isinstance(value, str):
            return tuple(p.strip() for p in value.split(";") if p.strip())
        return value


class ExpectedException(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    scenario_id: str = Field(min_length=1)
    exception_type: ExceptionType
    po_number: str = ""
    sku: str = ""
    quantity_difference: Decimal | None = None
    amount_impact: Decimal
    rationale: str = ""

    @property
    def key(self) -> MatchKey:
        return (self.exception_type, self.po_number, self.sku)


def predicted_key(exc: OpsException) -> MatchKey:
    return (exc.exception_type, exc.po_number or "", exc.sku or "")


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise EvaluationDatasetError(f"File not found: {path}")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [{k.strip(): v.strip() for k, v in row.items()} for row in frame.to_dict(orient="records")]


def _parse(model, rows: list[dict[str, str]], path: Path) -> list:
    parsed = []
    for line, row in enumerate(rows, start=2):
        payload = {k: v for k, v in row.items() if v != ""}
        try:
            parsed.append(model.model_validate(payload))
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            raise EvaluationDatasetError(f"{path.name} line {line}: {problems}") from None
    return parsed


def load_scenarios(path: str | Path) -> list[Scenario]:
    path = Path(path)
    scenarios: list[Scenario] = _parse(Scenario, _read_rows(path), path)
    seen_ids: set[str] = set()
    owner: dict[str, str] = {}
    for scenario in scenarios:
        if scenario.scenario_id in seen_ids:
            raise EvaluationDatasetError(f"Duplicate scenario_id {scenario.scenario_id}")
        seen_ids.add(scenario.scenario_id)
        for po in scenario.po_numbers:
            if po in owner:
                raise EvaluationDatasetError(
                    f"PO {po} is listed in both {owner[po]} and {scenario.scenario_id}; "
                    f"each PO must belong to exactly one scenario")
            owner[po] = scenario.scenario_id
    return scenarios


def load_expected(path: str | Path, scenarios: list[Scenario]) -> list[ExpectedException]:
    path = Path(path)
    expected: list[ExpectedException] = _parse(ExpectedException, _read_rows(path), path)
    by_id = {s.scenario_id: s for s in scenarios}
    for row in expected:
        scenario = by_id.get(row.scenario_id)
        if scenario is None:
            raise EvaluationDatasetError(f"Expected exception refers to unknown scenario {row.scenario_id}")
        if row.po_number not in scenario.po_numbers:
            raise EvaluationDatasetError(
                f"Expected exception for PO {row.po_number!r} is not one of {row.scenario_id}'s POs")
    return expected


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def safe_ratio(numerator: int, denominator: int) -> float | None:
    """numerator / denominator, or None when the denominator is zero (undefined)."""
    return None if denominator == 0 else numerator / denominator


def f1_score(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


@dataclass(frozen=True)
class Metrics:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def __add__(self, other: "Metrics") -> "Metrics":
        return Metrics(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)

    @property
    def precision(self) -> float | None:
        return safe_ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return safe_ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self) -> float | None:
        return f1_score(self.precision, self.recall)

    @property
    def support(self) -> int:
        """Number of expected exceptions."""
        return self.tp + self.fn


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MatchedPair:
    expected: ExpectedException
    predicted: OpsException

    @property
    def amount_correct(self) -> bool:
        if self.predicted.amount_impact != self.expected.amount_impact:
            return False
        if self.expected.quantity_difference is not None:
            return self.predicted.quantity_difference == self.expected.quantity_difference
        return True


@dataclass
class ScenarioResult:
    scenario: Scenario
    matched: list[MatchedPair] = field(default_factory=list)
    false_positives: list[OpsException] = field(default_factory=list)
    false_negatives: list[ExpectedException] = field(default_factory=list)

    @property
    def amounts_correct(self) -> int:
        return sum(p.amount_correct for p in self.matched)

    @property
    def passed(self) -> bool:
        return (not self.false_positives and not self.false_negatives
                and self.amounts_correct == len(self.matched))


@dataclass
class EvaluationResult:
    scenarios: list[ScenarioResult]
    unattributed: list[OpsException]
    validation_issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def matched(self) -> list[MatchedPair]:
        return [p for s in self.scenarios for p in s.matched]

    @property
    def false_positives(self) -> list[OpsException]:
        return [e for s in self.scenarios for e in s.false_positives] + self.unattributed

    @property
    def false_negatives(self) -> list[ExpectedException]:
        return [e for s in self.scenarios for e in s.false_negatives]

    def by_type(self) -> dict[ExceptionType, Metrics]:
        counts = {t: Metrics() for t in ExceptionType}
        for pair in self.matched:
            counts[pair.expected.exception_type] += Metrics(tp=1)
        for exc in self.false_positives:
            counts[exc.exception_type] += Metrics(fp=1)
        for exp in self.false_negatives:
            counts[exp.exception_type] += Metrics(fn=1)
        return counts

    @property
    def overall(self) -> Metrics:
        return Metrics(len(self.matched), len(self.false_positives), len(self.false_negatives))

    @property
    def amounts_correct(self) -> int:
        return sum(p.amount_correct for p in self.matched)


def _pair_within_key(expected: list[ExpectedException], predicted: list[OpsException]):
    """Pair exact-amount matches first, then the remainder in a stable order."""
    remaining_pred = sorted(predicted, key=lambda e: (e.amount_impact, e.exception_id))
    remaining_exp = sorted(expected, key=lambda e: (e.amount_impact, e.rationale))
    pairs: list[MatchedPair] = []

    for exp in list(remaining_exp):
        for pred in remaining_pred:
            if MatchedPair(exp, pred).amount_correct:
                pairs.append(MatchedPair(exp, pred))
                remaining_exp.remove(exp)
                remaining_pred.remove(pred)
                break

    while remaining_exp and remaining_pred:
        pairs.append(MatchedPair(remaining_exp.pop(0), remaining_pred.pop(0)))
    return pairs, remaining_pred, remaining_exp


def compare(
    expected: list[ExpectedException],
    predicted: list[OpsException],
    scenarios: list[Scenario],
) -> EvaluationResult:
    """Score predicted exceptions against the answer key, scenario by scenario."""
    owner = {po: s.scenario_id for s in scenarios for po in s.po_numbers}
    results = {s.scenario_id: ScenarioResult(s) for s in scenarios}

    exp_by_key: dict[tuple[str, MatchKey], list[ExpectedException]] = defaultdict(list)
    for exp in expected:
        exp_by_key[(exp.scenario_id, exp.key)].append(exp)

    pred_by_key: dict[tuple[str, MatchKey], list[OpsException]] = defaultdict(list)
    unattributed: list[OpsException] = []
    for exc in predicted:
        scenario_id = owner.get(exc.po_number or "")
        if scenario_id is None:
            unattributed.append(exc)
        else:
            pred_by_key[(scenario_id, predicted_key(exc))].append(exc)

    for scenario_key in sorted(set(exp_by_key) | set(pred_by_key), key=lambda k: (k[0], k[1][0].value, k[1][1:])):
        pairs, fps, fns = _pair_within_key(exp_by_key.get(scenario_key, []), pred_by_key.get(scenario_key, []))
        result = results[scenario_key[0]]
        result.matched += pairs
        result.false_positives += fps
        result.false_negatives += fns

    return EvaluationResult(
        scenarios=[results[s.scenario_id] for s in scenarios],
        unattributed=sorted(unattributed, key=lambda e: e.exception_id),
    )


def evaluate_dataset(dataset_dir: str | Path = DEFAULT_DATASET_DIR) -> EvaluationResult:
    """Run the pipeline on an evaluation dataset with default (strict) rules and score it.

    ``MatchConfig()`` is used deliberately instead of environment overrides so
    results are reproducible regardless of the caller's environment.
    """
    dataset_dir = Path(dataset_dir)
    scenarios = load_scenarios(dataset_dir / "scenarios.csv")
    expected = load_expected(dataset_dir / "expected_exceptions.csv", scenarios)
    run = investigate_files(
        dataset_dir / "purchase_orders.csv",
        dataset_dir / "invoices.csv",
        dataset_dir / "goods_received.csv",
        MatchConfig(),
    )
    result = compare(expected, run.exceptions, scenarios)
    result.validation_issues = run.dataset.issues
    return result


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def fmt_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _describe_predicted(exc: OpsException) -> str:
    where = " / ".join(x for x in (exc.po_number, exc.sku) if x) or "-"
    return f"{exc.exception_type.value} {where} ({fmt_money(exc.amount_impact, exc.currency)})"


def _describe_expected(exp: ExpectedException) -> str:
    where = " / ".join(x for x in (exp.po_number, exp.sku) if x) or "-"
    return f"{exp.exception_type.value} {where} ({fmt_money(exp.amount_impact, None)})"


def render_markdown(result: EvaluationResult, dataset_dir: str | Path = DEFAULT_DATASET_DIR) -> str:
    """Deterministic report: the same dataset always renders byte-identical output."""
    overall = result.overall
    passed = sum(s.passed for s in result.scenarios)
    out: list[str] = []
    add = out.append
    add("# OpsResolve Evaluation Report")
    add("")
    add(f"Dataset: `{Path(dataset_dir).as_posix()}` (synthetic). Rules: default `MatchConfig` (zero tolerances).")
    add("")
    if result.validation_issues:
        add(f"**Warning: {len(result.validation_issues)} validation issue(s) in the evaluation dataset; "
            f"results are not reliable until they are fixed.**")
        add("")
    add("## Overall")
    add("")
    add("| Metric | Value |")
    add("|---|---:|")
    add(f"| Expected exceptions | {overall.support} |")
    add(f"| Predicted exceptions | {overall.tp + overall.fp} |")
    add(f"| True positives | {overall.tp} |")
    add(f"| False positives | {overall.fp} |")
    add(f"| False negatives | {overall.fn} |")
    add(f"| Precision | {fmt_metric(overall.precision)} |")
    add(f"| Recall | {fmt_metric(overall.recall)} |")
    add(f"| F1 | {fmt_metric(overall.f1)} |")
    add(f"| Exact amounts (of true positives) | {result.amounts_correct} / {overall.tp} |")
    add(f"| Scenarios fully correct | {passed} / {len(result.scenarios)} |")
    add("")
    add("A true positive is a predicted exception matching an expected one on "
        "(exception type, PO number, SKU); unmatched predictions are false positives and unmatched "
        "expectations are false negatives. `n/a` means the metric is undefined (zero denominator).")
    add("")

    add("## By exception type")
    add("")
    add("| Exception type | Support | TP | FP | FN | Precision | Recall | F1 |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for exc_type, m in result.by_type().items():
        add(f"| {exc_type.value} | {m.support} | {m.tp} | {m.fp} | {m.fn} | "
            f"{fmt_metric(m.precision)} | {fmt_metric(m.recall)} | {fmt_metric(m.f1)} |")
    add("")

    add("## By scenario")
    add("")
    add("| Scenario | Category | Known limitation | Expected | TP | FP | FN | Amounts exact | Result |")
    add("|---|---|---|---:|---:|---:|---:|---:|---|")
    for s in result.scenarios:
        expected_count = len(s.matched) + len(s.false_negatives)
        add(f"| {s.scenario.scenario_id} | {s.scenario.category} | "
            f"{'yes' if s.scenario.known_limitation else 'no'} | {expected_count} | {len(s.matched)} | "
            f"{len(s.false_positives)} | {len(s.false_negatives)} | {s.amounts_correct}/{len(s.matched)} | "
            f"{'pass' if s.passed else 'FAIL'} |")
    add("")

    failures = [s for s in result.scenarios if not s.passed]
    add("## Discrepancies")
    add("")
    if not failures and not result.unattributed:
        add("None: every scenario matched its expected outcome.")
    for s in failures:
        add(f"### {s.scenario.scenario_id}: {s.scenario.description}")
        add("")
        for exc in s.false_positives:
            add(f"- False positive: {_describe_predicted(exc)}")
        for exp in s.false_negatives:
            add(f"- False negative: {_describe_expected(exp)}")
        for pair in s.matched:
            if not pair.amount_correct:
                qty = pair.predicted.quantity_difference
                add(f"- Amount differs: {_describe_expected(pair.expected)} expected; engine gave "
                    f"{fmt_money(pair.predicted.amount_impact, None)}"
                    + (f", quantity {fmt_qty(qty)}" if qty is not None else ""))
        add("")
    if result.unattributed:
        add("### Predictions not attributable to any scenario")
        add("")
        for exc in result.unattributed:
            add(f"- False positive: {_describe_predicted(exc)}")
        add("")
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m opsresolve.evaluation",
        description="Score exception detection against the labelled synthetic evaluation dataset.")
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR,
                        help="Directory with the three input CSVs, scenarios.csv and expected_exceptions.csv "
                             "(default: data/evaluation).")
    parser.add_argument("--output", type=Path, help="Also write the Markdown report to this file.")
    args = parser.parse_args(argv)

    try:
        result = evaluate_dataset(args.dataset_dir)
    except EvaluationDatasetError as exc:
        print(f"Invalid evaluation dataset: {exc}", file=sys.stderr)
        return 2

    report = render_markdown(result, args.dataset_dir)
    print(report, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    # Metrics are reported, not gated: a dataset that fails validation is the only error.
    return 2 if result.validation_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
