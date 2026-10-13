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

GitHub Actions runs the same suite on every push and pull request to `main`, on Python 3.10 and 3.14
(see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## Project layout

```
src/opsresolve/   models, config, ingestion, matching, detection, pipeline, reporting, cli
data/sample/      synthetic CSVs + README listing every seeded scenario and expected result
tests/            unit and end-to-end tests
docs/             architecture and validation plan
CLAUDE.md         persistent project instructions for contributors and AI assistants
```

## Roadmap (not yet built)

- Larger generated datasets with an answer key; measured detection precision/recall
- Units of measure, FX conversion, tax/freight lines, credit notes, returns and PO amendments
- Adapters for real ERP exports
- Persistent exception tracking (status, owner, resolution)
- Optional LLM layer for *narrative only* (e.g. drafting supplier queries from computed evidence) —
  never for calculations
