# OpsResolve AI

[![CI](https://github.com/garvit-mittal04/opsresolve-ai/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/garvit-mittal04/opsresolve-ai/actions/workflows/ci.yml)

AI-powered operations exception investigator for manufacturing and distribution businesses.
OpsResolve finds discrepancies between **purchase orders**, **supplier invoices** and
**goods-received records**, quantifies them, shows the evidence and recommends a corrective action.

> **Status: deterministic engine with a CLI and a local web app.** All matching and financial
> calculations are rule-based Python using exact decimal arithmetic; no LLM is used for them. An optional,
> separately installed module can ask an AI model to explain one exception from its evidence (advisory
> only, see [AI explanations](#ai-explanations-optional)); it is not yet used by the CLI or the web app.
> Sample data is synthetic; the tool has not been validated on real customer or supplier data.

## What it does

1. Loads `purchase_orders.csv`, `invoices.csv` and `goods_received.csv`, plus an optional per-SKU
   `uom_conversions.csv`. Files in other layouts (another delimiter, encoding, date or number format, or
   column names) can be read with an explicitly selected [import profile](#import-profiles).
2. Validates every row (required fields, numbers, ISO dates, currency codes, unit codes, key uniqueness)
   and reports problems by file, line and field. Invalid rows are excluded, never guessed at.
3. Matches invoices and receipts to PO lines by `(po_number, sku)`.
4. Normalises units of measure: where documents state units, quantities and prices are converted to the
   PO line's unit using only the per-SKU conversions supplied, and only when the result is exact
   (see [Units of measure](#units-of-measure-optional)).
5. Detects: quantity mismatches (billed vs received, over-receipt), price mismatches, currency,
   supplier and unit-of-measure mismatches, missing PO / goods receipt / invoice, and possible
   duplicate invoices (judged against the PO's ordered and received quantities).
6. Calculates quantity differences and amount impact exactly.
7. Writes a readable Markdown report plus CSV exports, ordered by severity, from the CLI or from the
   [local web app](#local-web-app), which also lets you filter and inspect findings in a browser.

See [`docs/architecture.md`](docs/architecture.md) for the rule table and
[`docs/validation-plan.md`](docs/validation-plan.md) for how correctness is tested.

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/garvit-mittal04/opsresolve-ai.git
cd opsresolve-ai
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,app]"        # engine + tests + web app (Streamlit)
```

The `app` extra adds Streamlit for the web app and its UI tests. `pip install -e ".[dev]"` installs only the
engine, CLI and test tools; with it, `pytest` skips the Streamlit UI tests (reported as skipped in its summary).
The optional `ai` extra (`pip install -e ".[dev,app,ai]"`) adds the Anthropic SDK for
[AI explanations](#ai-explanations-optional); without it, the tests against the real SDK are skipped.

## Run

```bash
# Run on the bundled synthetic data (data/sample) and write reports/
python -m opsresolve

# Your own files
python -m opsresolve --data-dir path/to/folder --output-dir out/
python -m opsresolve --purchase-orders po.csv --invoices inv.csv --goods-received grn.csv

# Exit with status 1 if any exception or validation issue is found (for scheduled jobs)
python -m opsresolve --fail-on-exceptions

# Files exported in another layout, read with an import profile (see "Import profiles")
python -m opsresolve --data-dir path/to/export --profile docs/examples/import-profile-example.json
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
streamlit run app.py               # or: python -m streamlit run app.py
```

Streamlit reads `.streamlit/config.toml` from next to `app.py` (checked with Streamlit 1.50 and 1.65), so its
localhost-only and no-telemetry settings apply wherever you launch from; command-line flags or `STREAMLIT_*`
environment variables that you set yourself still override them.
It opens `http://localhost:8501` in your browser; stop it with `Ctrl+C` in the terminal.

### Performing a reconciliation

1. **Input files.** Upload the three required CSVs (purchase orders, invoices, goods receipts) and,
   if your documents state units of measure, the optional unit-conversion file. Each uploader has an
   *Expected format* panel listing the required and optional columns (see [Input format](#input-format))
   and a template download with example rows. To try it out, choose **Use bundled sample data**.
   If your files use another layout, also upload an **Import profile (optional, .json)** below the four
   file uploaders (see [Import profiles](#import-profiles)). A valid profile is confirmed with its name and
   a short hash; an invalid one lists its problems in a table and the Run button stays disabled until you
   fix or remove it. The *What is an import profile?* panel offers the example profile as a download.
   Profiles are not used with the bundled sample data.
2. **Rule settings** (sidebar). Tolerances, duplicate window and severity thresholds. Defaults are strict,
   or come from `OPSRESOLVE_*` environment variables as for the CLI.
3. **Run reconciliation.** The button is enabled once all required files are present (and any uploaded
   import profile is valid).

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

Changing any upload (including adding, changing or removing an import profile) or setting clears the
previous results, so what you see always matches the current inputs. When a profile was used, the results
start with `Import profile: <name> (sha256 <first 12 hex characters>)`.

### Data handling

Uploaded files are written under fixed names (never their uploaded names) to a temporary folder that is
deleted as soon as the run finishes, including when it fails. Results are kept only in memory for your
browser session; there is no upload history. Streamlit itself holds uploaded files in memory until you
remove them or close the tab. Uploads are limited to 20 MB and `.csv` files (`.json` for the import
profile, which is read in memory and never written to disk; the profile limit is 65,536 bytes (64 KiB),
inclusive, and a larger profile is rejected with the problem listed in the app).

### Web app limitations

- Same engine limitations as the CLI (see [Evaluation](#evaluation) and the roadmap below); **the app
  has not been validated on real supplier data**, and its findings need human review.
- Single user, local only: no login, sharing, saved runs or status tracking of exceptions.
- Files must use the column names above unless an import profile maps them; there is no automatic
  detection of layouts.
- Verified versions (on Linux): the full test suite passes with Streamlit 1.65 on Python 3.10, 3.13 and 3.14.
  With Streamlit 1.50, the declared minimum, the app was only smoke-tested in a browser (upload, run,
  CSV download) on Python 3.13 during Milestone 6, when 17 UI tests failed there because Streamlit 1.50's
  test tool (`AppTest`) has a smaller API (no file-upload or download-button support, and dropdowns can
  only be set by value). The import-profile UI has not been tested with Streamlit 1.50. CI runs on Linux only.

## Configuration

Tolerances and severity thresholds come from `OPSRESOLVE_*` environment variables; defaults are
strict (zero tolerance). See [`.env.example`](.env.example). The variables are read from the
process environment; to use a `.env` file in bash:

```bash
cp .env.example .env
set -a; source .env; set +a
python -m opsresolve
```

No API keys or secrets are needed for reconciliation. Only the optional AI explanations need
`ANTHROPIC_API_KEY` (see below). Never commit `.env`.

## AI explanations (optional)

`opsresolve.ai.explain_exception(result, exception)` asks an Anthropic model to explain one exception in
plain language: what differs, which evidence shows it, unverified possible causes and how to check them,
and what information is missing. It needs `pip install -e ".[ai]"` and `ANTHROPIC_API_KEY`; without
either it returns `not_configured` and sends nothing. Model, effort, timeout, retries and output size come
from `OPSRESOLVE_AI_*` variables (see [`.env.example`](.env.example)).

- **Advisory only.** The model never calculates or changes an amount or an exception; every figure stays
  the engine's. Each answer is checked by a deterministic validator (citations, identifiers, dates,
  numbers, signs, direction, codes, supplier names, sufficiency) and is returned only if it passes;
  otherwise the result is `rejected` with the problems found. Passing does not make it true: a person
  must review every explanation.
- **What is sent.** One exception's evidence: the engine's facts and text, and for each evidence row its
  document number, PO line, SKU, quantities, prices, currency, dates, units and line amounts. Supplier
  ids are replaced by pseudonyms and supplier names and item descriptions are never sent, but this is
  still business data and is not anonymous. If any known supplier id or name is still in the request,
  nothing is sent. `opsresolve.ai.preview_request(...)` shows the exact request body;
  `TRANSMITTED_FIELDS` and the module docstring list every field. Requests go only to
  `https://api.anthropic.com` (`ANTHROPIC_BASE_URL` is ignored).
- **Failures** (missing configuration, authentication, rate limits, timeouts, network, refusals,
  malformed answers, unexpected provider errors, the privacy check) are returned as results with a code;
  logs hold no key, prompt, evidence, names or answers. Automated tests never call the API.

Not yet built: using this from the web app (with a preview and an explicit send action) and keeping any
history of explanations.

## Input format

| File | Columns |
|---|---|
| `purchase_orders.csv` | po_number, line_number, supplier_id, supplier_name, sku, description (optional), quantity_ordered, unit_price, currency, order_date |
| `invoices.csv` | invoice_number, invoice_date, supplier_id, po_number, sku, quantity_invoiced, unit_price, currency |
| `goods_received.csv` | grn_number, received_date, supplier_id, po_number, sku, quantity_received |

This is the built-in format: comma-delimited UTF-8 with these column names. Dates are `YYYY-MM-DD`;
numbers use `.` as the decimal separator with no thousands separators; currency is a 3-letter ISO code.
Other layouts need an [import profile](#import-profiles).

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

## Import profiles

An import profile is a small JSON file that declares how your exports are written, so that files with
another delimiter, encoding, date or number format, or column names can be read **without editing them**.
Nothing is guessed: a profile is used only when you select it (`--profile` in the CLI, or the upload in the
web app), it is never discovered automatically, and every value must match the declared format exactly or
its row is rejected and reported.

Milestone 7 did not change how files are read and reconciled without a profile (see
[Input format](#input-format)). This was checked by comparing runs without a profile against `origin/main`
at the start of the milestone (commit `581c597`) on `data/sample` and on `data/evaluation` with its
conversion file: the CLI's CSV files and the evaluation report were byte-identical, the Markdown report
differed only in its generated timestamp, and console output only in the output paths it prints. This
describes that comparison, not a promise that future versions keep these outputs byte-identical; the tests
check that runs without a profile never reach profile code. What did change without a profile: the web
app's upload mode shows the optional profile uploader and example download, and the CLI abbreviation `--p`
is no longer accepted (see below).

### Example

[`docs/examples/import-profile-example.json`](docs/examples/import-profile-example.json) (abridged):

```json
{
  "profile_version": 1,
  "name": "Example ERP export (semicolon, decimal comma)",
  "defaults": {"delimiter": ";", "encoding": "cp1252", "date_format": "DD.MM.YYYY",
               "decimal_separator": ",", "thousands_separator": "."},
  "files": {
    "invoices": {"columns": {"invoice_number": "Invoice Number", "invoice_date": "Invoice Date",
                             "quantity_invoiced": "Invoiced Qty", "unit_price": "Unit Price"}},
    "goods_received": {"date_format": "YYYY-MM-DD", "columns": {"grn_number": "GRN Number"}}
  }
}
```

- `defaults` apply to every file; a section under `files` (`purchase_orders`, `invoices`, `goods_received`,
  `uom_conversions`) overrides them for that file and maps its columns.
- `columns` maps a canonical field name (left) to the header in your file (right). Headers are matched
  ignoring case and surrounding spaces. Fields you do not map are read from their canonical column name.
- `profile_version` must be `1`; `name` (1–100 characters) appears in reports.
- The profile file itself must be UTF-8 JSON (a byte-order mark is allowed) of at most 65,536 bytes
  (64 KiB), inclusive, whatever `encoding` it declares; the `encoding` setting applies to the CSV files only.

| Setting | Allowed values |
|---|---|
| `delimiter` | `,` `;` tab (`"\t"`) `\|` |
| `encoding` | `utf-8` (a UTF-8 byte-order mark is accepted), `cp1252`, `iso-8859-1`; aliases such as `windows-1252` or `latin-1` are accepted |
| `date_format` | `YYYY-MM-DD`, `DD.MM.YYYY`, `DD/MM/YYYY`, `MM/DD/YYYY`, `YYYYMMDD` (zero-padded day and month, four-digit year) |
| `decimal_separator` | `.` `,` |
| `thousands_separator` | none (`""`), `,` `.` space `'` |

The decimal and thousands separators must differ, and neither may equal the delimiter (an unquoted `1,234`
in a comma-delimited file would shift columns).

### Using a profile

```bash
# All four files from one folder (uom_conversions.csv is picked up from the folder as usual)
python -m opsresolve --data-dir exports/acme --profile profiles/acme.json

# Individual files; each option overrides only its own file, --data-dir supplies the rest
python -m opsresolve --purchase-orders po.csv --invoices inv.csv --goods-received grn.csv \
                     --uom-conversions uom.csv --profile profiles/acme.json

# Check a profile before using it: prints its name and short hash, or fails listing every problem
python -c "from opsresolve.profiles import load_profile; p = load_profile('profiles/acme.json'); print(p.name, p.sha256[:12])"
```

- One profile applies to **all** input files, including a `uom_conversions.csv` found automatically in
  `--data-dir`; that file must then be written in the profile's `uom_conversions` format.
- An invalid or unreadable profile (malformed or non-UTF-8 JSON, over the limit of 65,536 bytes (64 KiB),
  inclusive, duplicate or unknown keys, unsupported values, clashing separators, unknown fields or
  `source_row`/`converted_from` as targets, two fields reading the same column) is reported on stderr with
  **every** problem listed, the CLI exits with status 2 and no reports are written.
- **Spell the option out.** The CLI accepts unambiguous abbreviations of long options. Since `--profile`
  was added, `--p` is ambiguous between `--profile` and `--purchase-orders`, so it is rejected with an error
  (exit status 2; before, it meant `--purchase-orders`), while `--pr` resolves to `--profile`. Use
  `--profile` and `--purchase-orders` in full.
- The evaluation command (`python -m opsresolve.evaluation`) has no profile option.

### What the profile changes when reading

- **Encoding:** the declared encoding is used, never guessed. Two things are rejected as file-level
  errors: a file starting with a UTF-8 byte-order mark under another declared encoding, and a file
  containing bytes that are invalid in the declared encoding (this message gives the byte position).
  Invalid bytes means invalid UTF-8 under `utf-8`, or one of the five bytes `cp1252` leaves undefined
  (`0x81`, `0x8D`, `0x8F`, `0x90`, `0x9D`); `iso-8859-1` accepts every byte. A file whose bytes are valid
  in the declared encoding but which was written in another one **cannot be detected**: UTF-8 `Müller`
  declared as `cp1252` reads as `MÃ¼ller`, and a `cp1252` `€` declared as `iso-8859-1` becomes an
  invisible control character. Check that names with accents read correctly.
- **Columns:** a mapped column that is missing (required or optional) is a file-level error and no rows
  are loaded from that file. The message names the header from the profile (e.g.
  `Column 'Item' (mapped by the import profile) is missing`) and the issue's field is the canonical name
  (`sku`). A file containing both a mapped header and the field's canonical name, or containing a header
  the profile reads more than once, is ambiguous and also rejected. Other columns (including repeated
  ones the profile does not read) are ignored.
- **Delimiter:** a row with more cells than the header rejects the file. If the header is read as a single
  column that contains another allowed delimiter, one extra hint points at the declared delimiter. This
  hint exists only with a profile; runs without a profile are unchanged.
- **Numbers** (quantities, prices, line numbers, conversion factors) must match the declared format exactly,
  e.g. `1.234,50` with decimal `,` and thousands `.`; after the first group, digit groups must have
  exactly three digits. Rejected:
  currency symbols, letters, exponents (`1e3`), misplaced grouping (`1.23,4`), a bare decimal separator
  (`,5`), trailing signs (`5-`), bracketed negatives (`(5)`), and grouping when no thousands separator is
  declared. A profile is therefore stricter than the built-in format, which accepts `1e3`. The models' own
  checks (for example quantity > 0) still apply after conversion.
- **Dates** are handled in two steps. First the value must match the declared source layout exactly (no
  other layout is tried; e.g. `1.3.2026` fails `DD.MM.YYYY`) and be a real calendar date (`31.02.2026` is
  rejected); these give distinct messages. The value is then converted to an ISO `YYYY-MM-DD` date, which
  the models validate exactly as they validate dates in the built-in format.
- **Text** (IDs, SKUs, names, currency, units) is passed through unchanged; leading and trailing spaces
  are trimmed as without a profile.
- **Units of measure:** a document's `unit_of_measure` column can be mapped like any other column (e.g.
  `"unit_of_measure": "UoM"`), and the conversion file is read in the profile's `uom_conversions` format,
  including its decimal and thousands separators for `units_per_uom`. The conversion rules themselves are
  unchanged (see [Units of measure](#units-of-measure-optional)).
- Each rejected value is a row-level validation issue with the CSV line, the canonical field, the original
  value and a message naming the declared format. In messages, values longer than 40 characters are
  shortened; the issue's value column keeps up to 200 characters.

### Provenance

When a profile is used, the console output starts with, and the Markdown report (after the rules line)
contains, `Import profile: <name> (sha256 <first 12 hex characters of the profile file>)`. The hash is of
the profile file's exact bytes, so it identifies which profile a run used. The CSV exports are unchanged.
The profile's name is Markdown-escaped in the Markdown report and in the web app; the console prints it
as written.

### Known gaps (recorded, not yet built)

- Reports name the profile and its hash but not the encoding used for each file (deferred under design
  decision D4).
- Messages for rejected numbers and dates name the canonical field and the declared format, but not the
  mapped header from your file.
- Profiles have been tested only on synthetic, reformatted copies of the sample and evaluation data, not on
  real ERP exports. The fixtures, the test inventory and the milestone's approved deviations are listed in
  [`docs/validation-plan.md`](docs/validation-plan.md).

## Tests

```bash
pytest
```

Install with `.[dev,app]` (see [Setup](#setup)) so the Streamlit UI tests run instead of being skipped.
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

The evaluation command reads the dataset in the built-in format and has no import-profile option. A test
(`tests/test_profile_evaluation_equivalence.py`) reads a reformatted copy of the dataset through an import
profile and checks that it gives the same records, exceptions and score.

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
src/opsresolve/   models, config, profiles (import profiles), ingestion, matching, uom, detection,
                  pipeline, reporting, cli, evaluation, webapp (helpers for the web app),
                  explanation (evidence packs and answer validation), ai (optional AI provider)
app.py            Streamlit web app (layout only; see "Local web app")
.streamlit/       local-only Streamlit settings (localhost, no telemetry)
data/sample/      synthetic CSVs + README listing every seeded scenario and expected result
data/evaluation/  labelled evaluation dataset: inputs, scenarios.csv, expected_exceptions.csv
tests/            unit and end-to-end tests; tests/fixtures/profiles/ holds import-profile fixtures
docs/             architecture and validation plan; docs/examples/ holds the example import profile
CLAUDE.md         persistent project instructions for contributors and AI assistants
```

## Roadmap (not yet built)

- Larger and more varied evaluation datasets, and evaluation on anonymised real exports
- FX conversion, tax/freight lines, credit notes, returns and PO amendments; supplier-specific pack sizes
- Validation of import profiles on real ERP exports, and ready-made profiles for common systems
- Persistent exception tracking (status, owner, resolution)
- AI explanations in the web app, with a preview of what is sent and an explicit send action; later,
  reviewer notes, resolution status and history (AI stays narrative only, never calculations)
