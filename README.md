# OpsResolve AI

[![CI](https://github.com/garvit-mittal04/opsresolve-ai/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/garvit-mittal04/opsresolve-ai/actions/workflows/ci.yml)

AI-powered operations exception investigator for manufacturing and distribution businesses.
OpsResolve finds discrepancies between **purchase orders**, **supplier invoices** and
**goods-received records**, quantifies them, shows the evidence and recommends a corrective action.

> **Status: milestone 1 — deterministic MVP.** All matching and financial calculations are
> rule-based Python using exact decimal arithmetic. No LLM is used. Sample data is synthetic;
> the tool has not been validated on real customer data.

## What it does

1. Loads `purchase_orders.csv`, `invoices.csv` and `goods_received.csv`.
2. Validates every row (required fields, numbers, ISO dates, currency codes, key uniqueness) and
   reports problems by file, line and field. Invalid rows are excluded, never guessed at.
3. Matches invoices and receipts to PO lines by `(po_number, sku)`.
4. Detects: quantity mismatches (billed vs received, over-receipt), price mismatches, currency and
   supplier mismatches, missing PO / goods receipt / invoice, and possible duplicate invoices.
5. Calculates quantity differences and amount impact exactly.
6. Writes a readable Markdown report plus CSV exports, ordered by severity.

See [`docs/architecture.md`](docs/architecture.md) for the rule table and
[`docs/validation-plan.md`](docs/validation-plan.md) for how correctness is tested.

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/garvit-mittal04/opsresolve-ai.git
cd opsresolve-ai
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Run

```bash
# Run on the bundled synthetic data (data/sample) and write reports/
python -m opsresolve

# Your own files
python -m opsresolve --data-dir path/to/folder --output-dir out/
python -m opsresolve --purchase-orders po.csv --invoices inv.csv --goods-received grn.csv

# Exit with status 1 if any exception or validation issue is found (for scheduled jobs)
python -m opsresolve --fail-on-exceptions
```

Outputs (in `reports/` by default, git-ignored):

| File | Contents |
|---|---|
| `exception_report.md` | Input summary, validation issues, exception summary, and each exception with evidence and recommended action |
| `exceptions.csv` | One row per exception |
| `validation_issues.csv` | One row per rejected field/row |

The installed console script `opsresolve` is equivalent to `python -m opsresolve`.

## Configuration

Tolerances and severity thresholds come from `OPSRESOLVE_*` environment variables; defaults are
strict (zero tolerance). See [`.env.example`](.env.example). The variables are read from the
process environment; to use a `.env` file in bash:

```bash
cp .env.example .env
set -a; source .env; set +a
python -m opsresolve
```

No API keys or secrets are needed for milestone 1. Never commit `.env`.

## Input format

| File | Columns |
|---|---|
| `purchase_orders.csv` | po_number, line_number, supplier_id, supplier_name, sku, description (optional), quantity_ordered, unit_price, currency, order_date |
| `invoices.csv` | invoice_number, invoice_date, supplier_id, po_number, sku, quantity_invoiced, unit_price, currency |
| `goods_received.csv` | grn_number, received_date, supplier_id, po_number, sku, quantity_received |

Dates are `YYYY-MM-DD`; numbers use `.` as the decimal separator with no thousands separators;
currency is a 3-letter ISO code.

## Tests

```bash
pytest
```

GitHub Actions runs the same suite on every push and pull request to `main`, on Python 3.10 and 3.14,
then prints the evaluation report to the run summary (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## Evaluation

OpsResolve is measured against a labelled synthetic dataset in [`data/evaluation/`](data/evaluation/):
32 scenarios (34 PO lines, 39 invoice lines, 34 receipt lines) and a hand-computed answer key of
33 expected exceptions covering every exception type.

```bash
python -m opsresolve.evaluation                              # print the report
python -m opsresolve.evaluation --output reports/evaluation.md
pytest tests/test_evaluation.py                              # metric, matching and dataset tests
```

**Dataset.** `scenarios.csv` lists each scenario (ID, category, its PO numbers, description, and whether it
is a *known limitation*). `expected_exceptions.csv` is the answer key: exception type, PO, SKU, expected
amount impact and (where the rules define one) quantity difference, with the arithmetic in `rationale`.
Every PO number belongs to exactly one scenario. Scenarios cover exact matches, partial deliveries and
split billing, price, quantity, supplier and currency mismatches, missing PO / receipt / invoice,
duplicate invoices, and lines with several simultaneous discrepancies. All parties are fictional
(`Synthetic Supplier E01` ... `E11`).

**Ground truth.** The answer key records what a careful reviewer should conclude about each scenario,
not what the engine currently outputs. Three scenarios are deliberately marked `known_limitation`
because the documented rules are expected to get them wrong: a duplicate re-sent outside the 30-day
window (S26), a legitimate second invoice whose goods receipt is not yet posted (S27), and a
unit-of-measure difference that the engine does not convert (S32).

**Definitions.**

- An exception is matched on `(exception_type, po_number, sku)`. Within a key, predictions and
  expectations are paired as multisets (exact-amount pairs first).
- **TP**: a prediction paired with an expected exception. **FP**: an unpaired prediction (including any
  prediction not attributable to a scenario). **FN**: an unpaired expected exception.
- Precision = TP / (TP + FP), recall = TP / (TP + FN), F1 = 2PR / (P + R). Overall figures are micro-averaged;
  the report also breaks them down by exception type and by scenario.
- A zero denominator makes a metric **undefined**, reported as `n/a` (never as 0 or 1). F1 is `n/a` if either
  input is undefined and 0 if both are 0.
- Amount accuracy is reported separately: a TP counts as exact only if its amount impact (and quantity
  difference, where the key gives one) equals the answer key exactly.
- True negatives are not counted. Clean scenarios are instead checked to produce no exceptions.

**Current results** (default rules, measured by `python -m opsresolve.evaluation`):

| TP | FP | FN | Precision | Recall | F1 | Exact amounts | Scenarios fully correct |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 32 | 3 | 1 | 0.914 | 0.970 | 0.941 | 32 / 32 | 29 / 32 |

All misses are the three known-limitation scenarios: one missed duplicate (S26) and three false positives
(a duplicate in S27, and a quantity plus a price mismatch in S32). `tests/test_evaluation.py` pins these
numbers, so any change in detection behaviour fails CI until the change and the documented results are
reviewed together.

**Limitations.** These numbers measure agreement with scenarios written by the same team that wrote
the rules, on a small, clean, synthetic dataset. They are **not** an estimate of accuracy on real
supplier data, which has not been evaluated. The dataset contains no invalid rows (input validation is
covered by the unit tests), uses one currency for prices, and measures only the default configuration.

## Project layout

```
src/opsresolve/   models, config, ingestion, matching, detection, pipeline, reporting, cli, evaluation
data/sample/      synthetic CSVs + README listing every seeded scenario and expected result
data/evaluation/  labelled evaluation dataset: inputs, scenarios.csv, expected_exceptions.csv
tests/            unit and end-to-end tests
docs/             architecture and validation plan
CLAUDE.md         persistent project instructions for contributors and AI assistants
```

## Roadmap (not yet built)

- Larger and more varied evaluation datasets, and evaluation on anonymised real exports
- Units of measure, FX conversion, tax/freight lines, credit notes, returns and PO amendments
- Adapters for real ERP exports
- Persistent exception tracking (status, owner, resolution)
- Optional LLM layer for *narrative only* (e.g. drafting supplier queries from computed evidence) —
  never for calculations
