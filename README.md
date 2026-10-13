# OpsResolve AI

[![CI](https://github.com/garvit-mittal04/opsresolve-ai/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/garvit-mittal04/opsresolve-ai/actions/workflows/ci.yml)

AI-powered operations exception investigator for manufacturing and distribution businesses.
OpsResolve finds discrepancies between **purchase orders**, **supplier invoices** and
**goods-received records**, quantifies them, shows the evidence and recommends a corrective action.

> **Status: deterministic engine with a CLI and a local web app.** All matching and financial
> calculations are rule-based Python using exact decimal arithmetic. No LLM is used. Sample data is
> synthetic; the tool has not been validated on real customer or supplier data.

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

## Local web app

A single-page Streamlit app for running a reconciliation from your browser, reviewing the findings and
exporting them. It runs **only on your computer**: it listens on `localhost`, sends no usage statistics,
and uses no database, cloud service or AI model. It calls the same pipeline as the CLI, so results are
identical for the same files and settings.

### Install and launch (macOS)

```bash
cd opsresolve-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[app]"            # adds Streamlit; use ".[dev,app]" to also run the tests
streamlit run app.py
```

Run the last command from the repository folder so Streamlit picks up `.streamlit/config.toml`.
It opens `http://localhost:8501` in your browser; stop it with `Ctrl+C` in the terminal.

### Performing a reconciliation

1. **Input files.** Upload the three required CSVs (purchase orders, invoices, goods receipts) and,
   if your documents state units of measure, the optional unit-conversion file. Each uploader has an
   *Expected format* panel listing the required and optional columns (see [Input format](#input-format))
   and a template download with example rows. To try it out, choose **Use bundled sample data**.
2. **Rule settings** (sidebar). Tolerances, duplicate window and severity thresholds. Defaults are strict,
   or come from `OPSRESOLVE_*` environment variables as for the CLI.
3. **Run reconciliation.** The button is enabled once all required files are present.

### Reviewing and exporting findings

- **Summary:** rows read per file (accepted and rejected), the number of validation issues and
  exceptions, counts by severity and type, and a processing status. "No exceptions" means the documents
  agree under the current rules; it is not an approval or payment decision.
- **Exceptions:** filter by severity and type, search by PO, SKU, supplier, invoice or GRN number, and
  inspect one finding: expected vs actual, amount impact, recommended next step, the evidence rows
  (with CSV line numbers) and every note from the engine. Possible duplicates are alerts for human
  review, not confirmed duplicates.
- **Validation issues:** shown separately, with the uploaded file name, line, field, value and problem.
  These rows were rejected and excluded from matching; correct them before relying on the findings.
- **Downloads:** exceptions CSV, validation-issues CSV and the Markdown summary report, generated from
  the current run. In the CSV downloads, cells that would start a spreadsheet formula (`=`, `+`, `-`, `@`,
  tab, carriage return) are prefixed with `'` so spreadsheets show them as text; plain numbers such as
  `-50.00` are unchanged. (The CLI's report files are not altered.)

Changing any upload or setting clears the previous results, so what you see always matches the
current inputs.

### Data handling

Uploaded files are written under fixed names (never their uploaded names) to a temporary folder that is
deleted as soon as the run finishes, including when it fails. Results are kept only in memory for your
browser session; there is no upload history. Streamlit itself holds uploaded files in memory until you
remove them or close the tab. Uploads are limited to 20 MB and `.csv` files.

### Web app limitations

- Same engine limitations as the CLI (see [Evaluation](#evaluation) and the roadmap below); **the app
  has not been validated on real supplier data**, and its findings need human review.
- Single user, local only: no login, sharing, saved runs or status tracking of exceptions.
- Files must use the column names above; there is no column mapping for ERP exports.
- Tested with Streamlit 1.50 and 1.65 on Linux (Python 3.10 and 3.14); please confirm on your Mac.

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

### Units of measure (optional)

Each of the three files may have a `unit_of_measure` column (a code such as `EA`, `BOX`, `CASE`, `KG`;
case-insensitive). Conversions are supplied per SKU in an optional `uom_conversions.csv`, picked up from
`--data-dir` or given with `--uom-conversions`:

| Column | Meaning |
|---|---|
| `sku`, `uom`, `base_uom`, `units_per_uom` | one `uom` of this SKU equals `units_per_uom` of `base_uom` (e.g. `EV-AE,BOX,EA,12`) |

Rules (see `src/opsresolve/uom.py`):

- If no document on a PO line states a unit, nothing changes: quantities and prices are compared as before.
- If any document on a PO line states a unit, invoice and receipt quantities and prices are converted into the
  PO line's unit, but only when every document states a unit, a conversion for **that SKU** links the
  two units through the same base unit, and the result is an exact decimal. No pack size is assumed or
  read from descriptions, and nothing is rounded.
- Otherwise the line gets a `UNIT_OF_MEASURE_MISMATCH` exception (MEDIUM, no amount) and its quantity and
  price checks are skipped, just as prices in different currencies are not compared. Other checks still run;
  a missing invoice on such a line is reported but not valued.
- Conversion rows that conflict for the same SKU and unit are ambiguous: all of them are rejected as
  validation issues, so that unit is treated as unconvertible. Exact repeats keep the first row.
- Evidence on converted lines shows the original figures, e.g. `240 EA x 2.10 converted to CASE (1 CASE = 24 EA)`.

## Tests

```bash
pytest
```

GitHub Actions runs the same suite on every push and pull request to `main`, on Python 3.10 and 3.14,
then prints the evaluation report to the run summary (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## Evaluation

OpsResolve is measured against a labelled synthetic dataset in [`data/evaluation/`](data/evaluation/):
36 scenarios (38 PO lines, 44 invoice lines, 38 receipt lines, 3 per-SKU unit conversions) and a
hand-computed answer key of 37 expected exceptions covering every exception type.

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
duplicate invoices, units of measure, and lines with several simultaneous discrepancies. All parties are fictional
(`Synthetic Supplier E01` ... `E11`).

**Ground truth.** The answer key records what a careful reviewer should conclude about each scenario,
not what the engine currently outputs. Scenarios are marked `known_limitation` when the documented rules
are expected to get them wrong; one remains: S36, a real duplicate on a half-received PO whose records
are indistinguishable from the legitimate second invoice in S27 (see "Duplicate invoices" below).
Earlier known limitations were resolved without changing their expected outcomes: S32 (boxes of 12
invoiced in units) by unit-of-measure support, which added explicit units and a conversion row to its
documents; S26 (a duplicate re-sent after 60 days) and S27 (a legitimate second invoice whose receipt
is not yet posted) by the supply-evidence duplicate rule.

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
| 36 | 0 | 1 | 1.000 | 0.973 | 0.986 | 36 / 36 | 35 / 36 |

The one miss is the known-limitation scenario S36. History, each measured on a fixed dataset:

| Change | Dataset | Before (TP / FP / FN, F1) | After (TP / FP / FN, F1) |
|---|---|---|---|
| Unit-of-measure support | 35 scenarios | 33 / 8 / 2, 0.868 | 34 / 1 / 1, 0.971 |
| Duplicate supply evidence | 35 scenarios (S1–S35) | 34 / 1 / 1, 0.971 | 35 / 0 / 0, 1.000 |
| Duplicate supply evidence | 36 scenarios (adds S36) | 36 / 1 / 1, 0.973 | 36 / 0 / 1, 0.986 |

The perfect score on S1–S35 does not mean duplicate detection is solved: S36 was added to show the
pattern the new rule deliberately leaves unclaimed. `tests/test_evaluation.py` pins these
numbers, so any change in detection behaviour fails CI until the change and the documented results are
reviewed together.

**Duplicate invoices.** Rule (b) compares invoices from one supplier with identical line items and decides
what they mean from the PO line, because the ordered quantity is fixed on the PO while receipts can be
posted late:

| Evidence on the PO line | Outcome |
|---|---|
| billed more than **ordered** | `POSSIBLE_DUPLICATE_INVOICE`, whatever the date gap (the PO cannot support both) |
| billed more than **received**, within ordered | not claimed as a duplicate (could be an unposted receipt); a note naming the identical invoice is added to the line's quantity-mismatch or missing-receipt exception, which already holds payment. Only within the duplicate window |
| covered by receipts | nothing (split billing) |
| no PO line, or units unresolved | `POSSIBLE_DUPLICATE_INVOICE` within the duplicate window (unchanged) |

Reformatted invoice numbers (rule a) and lines repeated within one invoice are flagged as before.
Remaining limitations: a duplicate on a PO with open quantity (S36) is not claimed as one, only noted;
identical invoices outside the window within the ordered quantity are not linked at all; a PO that is
genuinely over-delivered and billed twice for identical amounts will be flagged as a possible duplicate.

**Limitations.** These numbers measure agreement with scenarios written by the same team that wrote
the rules, on a small, clean, synthetic dataset. They are **not** an estimate of accuracy on real
supplier data, which has not been evaluated. The dataset contains no invalid rows (input validation is
covered by the unit tests), uses one currency for prices, and measures only the default configuration.

## Project layout

```
src/opsresolve/   models, config, ingestion, matching, uom, detection, pipeline, reporting, cli,
                  evaluation, webapp (helpers for the web app)
app.py            Streamlit web app (layout only; see "Local web app")
.streamlit/       local-only Streamlit settings (localhost, no telemetry)
data/sample/      synthetic CSVs + README listing every seeded scenario and expected result
data/evaluation/  labelled evaluation dataset: inputs, scenarios.csv, expected_exceptions.csv
tests/            unit and end-to-end tests
docs/             architecture and validation plan
CLAUDE.md         persistent project instructions for contributors and AI assistants
```

## Roadmap (not yet built)

- Larger and more varied evaluation datasets, and evaluation on anonymised real exports
- FX conversion, tax/freight lines, credit notes, returns and PO amendments; supplier-specific pack sizes
- Adapters for real ERP exports
- Persistent exception tracking (status, owner, resolution)
- Optional LLM layer for *narrative only* (e.g. drafting supplier queries from computed evidence) —
  never for calculations
