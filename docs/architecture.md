# OpsResolve AI — Architecture

## Scope (as of Milestone 7)

A deterministic three-way-match exception investigator:

1. Load purchase order, invoice and goods-received CSV files, plus optional per-SKU unit conversions,
   in the built-in format or in a layout declared by an explicitly selected import profile.
2. Validate input; report missing/invalid fields with file, line and field.
3. Match records with deterministic business rules.
4. Normalise units of measure where documents state them (exact, per-SKU conversions only).
5. Detect quantity, price, currency, supplier and unit-of-measure mismatches, missing records and
   possible duplicate invoices.
6. Calculate discrepancies exactly.
7. Produce a readable exception report (Markdown + CSV + console summary) from the CLI or the local
   Streamlit web app, and score the engine against a labelled synthetic dataset (`evaluation.py`).

No LLM is used anywhere.

## Data flow

```
 CSV files ──► ingestion.py ──► validated records ──► matching.py ──► MatchResult
               (Pandas reads                         (group by          │
                raw strings;                          po_number+sku)    ▼
                Pydantic validates)                                  uom.py
                     │                                (convert to the PO line's unit with
                     │                                 per-SKU conversions; unresolved lines)
                     │                                                  │
                     │                                                  ▼
                     │                                            detection.py
                     │                                            (rules, amounts,
                     └──► ValidationIssues + rejected keys ──────► severity, actions)
                                                                        │
                                                                        ▼
                                              reporting.py ◄── InvestigationResult (pipeline.py)
                                              (Markdown, CSV, console)
```

`cli.py`, `evaluation.py` and the web app (`webapp.py` / `app.py`) all call `pipeline.investigate_files`.
`cli.py` and the web app may pass an `ImportProfile` (see [Import profiles](#import-profiles));
`evaluation.py` never does.

| Layer | Responsibility | Must not |
|---|---|---|
| `profiles` | Load and validate import profiles; parse single number and date values strictly in a declared format | read CSV files or apply business rules |
| `ingestion` | Read CSV as strings, normalise headers (or apply a profile's format), validate each row via Pydantic, enforce key uniqueness | apply business rules |
| `matching` | Attach invoice and receipt lines to the PO line they reference | judge whether anything is wrong |
| `uom` | Convert invoice and receipt lines to the PO line's unit using the per-SKU conversion table; mark lines that cannot be converted exactly | guess pack sizes or round |
| `detection` | Apply rules, compute differences and amounts, assign severity, write evidence and actions | read files or format reports |
| `reporting` | Render results | compute business values |
| `pipeline` / `cli` | Wire layers together, load config | contain rules |

## Data model

All quantities and money are `Decimal`. In the built-in format dates must be ISO `YYYY-MM-DD` (no
ambiguous formats). An import profile may declare another layout from its allowlist (including
`DD/MM/YYYY` and `MM/DD/YYYY`, which only the declaration tells apart); such values are parsed in that
layout and converted to ISO text, which the models then validate as before.

| File | Required columns | Key |
|---|---|---|
| `purchase_orders.csv` | po_number, line_number, supplier_id, supplier_name, sku, quantity_ordered (>0), unit_price (>=0), currency, order_date (`description` optional) | (po_number, line_number) and (po_number, sku) unique |
| `invoices.csv` | invoice_number, invoice_date, supplier_id, po_number, sku, quantity_invoiced (>0), unit_price (>=0), currency | none enforced — repeats are a *business* exception |
| `goods_received.csv` | grn_number, received_date, supplier_id, po_number, sku, quantity_received (>=0) | (grn_number, po_number, sku) unique |
| `uom_conversions.csv` (optional) | sku, uom, base_uom, units_per_uom (>0) | (sku, uom) unique and unambiguous |

All three document files accept an optional `unit_of_measure` column.

### Validation behaviour

- Missing file, empty file, unparseable file or missing required column → file-level issue, no rows loaded from that file.
- Invalid row → every failing field reported (file, line, field, value, message); row rejected.
- Duplicate PO line / SKU on a PO / conflicting supplier on a PO / duplicate GRN line → later row rejected, first kept.
- Line numbers are physical CSV line numbers (header = line 1). Blank lines are skipped without shifting numbering.
  This holds with and without a profile; as before, a quoted value spanning several lines shifts the numbering.
- The `(po_number, sku)` of every rejected row is remembered. If an exception reports a record as "missing" and a
  row for that key was rejected, the exception carries a note telling the reviewer to fix the validation issue first.

## Matching

Match key: **(po_number, sku)**. A PO line can have any number of invoices and receipts
(partial deliveries and split billing are normal). Totals are compared at PO-line level.
Invoices/receipts whose key has no PO line are "unmatched".

## Detection rules

Tolerances are inclusive (difference equal to the tolerance is *not* an exception). Defaults are zero.

| Exception | Condition | Quantity difference | Amount impact | Severity |
|---|---|---|---|---|
| `QUANTITY_MISMATCH` (billing) | invoices and receipts exist, \|Σinvoiced − Σreceived\| > qty tolerance | Σinvoiced − Σreceived | difference × PO unit price | by amount if over-billed; LOW if under-billed |
| `QUANTITY_MISMATCH` (over-receipt) | Σreceived − ordered > qty tolerance | Σreceived − ordered | excess × PO unit price | by amount |
| `PRICE_MISMATCH` | per invoice line, same currency, \|inv price − PO price\| > max(abs tol, PO price × pct tol) | – | (inv price − PO price) × invoiced qty | by amount if overcharged; LOW if under |
| `CURRENCY_MISMATCH` | invoice currency ≠ PO currency (price check skipped) | – | 0 (no FX conversion) | MEDIUM |
| `UNIT_OF_MEASURE_MISMATCH` | units stated on the PO line cannot be safely converted (see below); quantity and price checks skipped | – | 0 | MEDIUM |
| `SUPPLIER_MISMATCH` | invoice supplier ≠ PO supplier | – | invoice line amount | HIGH |
| `SUPPLIER_MISMATCH` | GRN supplier ≠ PO supplier | – | 0 | MEDIUM |
| `MISSING_GOODS_RECEIPT` | PO line invoiced, nothing received | Σinvoiced | Σ invoice line amounts | by amount |
| `MISSING_INVOICE` | PO line received, nothing invoiced | Σreceived | Σreceived × PO price (accrual, not a loss) | LOW |
| `MISSING_PURCHASE_ORDER` | invoice line with no PO line | – | invoice line amount | by amount, min MEDIUM |
| `MISSING_PURCHASE_ORDER` | receipt line with no PO line | received qty | 0 (receipts carry no price) | MEDIUM |
| `POSSIBLE_DUPLICATE_INVOICE` | same invoice number repeats an identical line | qty × (n−1) | line amount × (n−1) | by amount, min MEDIUM |
| `POSSIBLE_DUPLICATE_INVOICE` | two invoice numbers from one supplier where (a) numbers are equal after normalisation, or (b) line content is identical **and** either combined billing exceeds the **ordered** qty (any date gap), or the PO line is missing / its units unresolved and the invoices are within the window | – | later invoice total | by amount, min MEDIUM |

Severity by amount (absolute value): ≥ 1,000.00 → HIGH; ≥ 100.00 → MEDIUM; else LOW (configurable).

Design notes:

- **Billing quantity impact uses the PO price** so that a price variance on the same line is not counted twice; it is reported separately.
- **Content-identical invoices alone are not flagged.** Rule (b) classifies the PO line's supply evidence
  (`detection.Supply`): billing over the ordered quantity is strong evidence (the PO cannot support both
  invoices, so the date gap does not matter); billing over received but within ordered is ambiguous, because
  receipts can be posted late, so it is **not** claimed as a duplicate and instead adds a note to the line's
  `QUANTITY_MISMATCH` / `MISSING_GOODS_RECEIPT` (within the window); billing covered by receipts is split
  billing. With no PO line or unresolved units the ordered quantity is unknown and the windowed rule applies.
  Rule (a) (re-keyed invoice number such as `CIB-7781` vs `CIB-07781`) is flagged regardless.
- Each later invoice is reported once, against its earliest match.
- A duplicate invoice also inflates the billed quantity, so the same money can appear in a duplicate exception and a quantity exception. Such exceptions are cross-referenced with a note, and the report warns that amounts must not be summed across types.
- Exceptions are sorted by severity, then |amount| descending, then type/PO/SKU, and numbered `EXC-0001…` — the same input always yields the same report.
- A PO line with no invoices and no receipts is open, not an exception.

## Evaluation

`evaluation.py` runs the unchanged pipeline with default `MatchConfig` on `data/evaluation/` and
compares its exceptions with the hand-written answer key on `(exception_type, po_number, sku)`.
It reports TP/FP/FN, precision, recall and F1 overall, by type and by scenario, plus amount accuracy.
It depends on the pipeline but nothing in the pipeline depends on it. Methodology: README "Evaluation".

## Import profiles

An import profile (`profiles.py`) is a JSON file declaring, per input file, the delimiter, encoding,
date format, decimal and thousands separators, and a mapping from canonical fields to source headers.
Usage and the user-facing rules are in the README ("Import profiles"); the example is
`docs/examples/import-profile-example.json`.

```
--profile PATH / web upload ──► profiles.load_profile / parse_profile ──► ImportProfile (name, formats, sha256)
                                                                              │ format_for(label) → FileFormat
CSV files ──► ingestion (file_format=None: unchanged code path) ─────────────┤
          └─► ingestion (FileFormat: decode, read, map headers, parse values) ┘──► Dataset.profile
                                                                                    InvestigationResult.profile
```

- **Explicit only.** A profile is used only when passed (`cli --profile`, the web upload); nothing is
  discovered. `evaluation.py` has no profile option.
- **Profile validation** (`parse_profile`, before any CSV is read) collects every problem: JSON object
  only, UTF-8 (byte-order mark allowed; the `encoding` setting applies to the CSV files only), at most
  65,536 bytes (64 KiB), inclusive (`load_profile` checks the file size and reads at most one byte more,
  so devices and pipes cannot exhaust memory; `parse_profile` checks the length again, which is the check
  the web upload goes through), no duplicate or unknown keys, `profile_version` 1, a 1–100 character name,
  allowlisted values, distinct separators that differ from the delimiter, mappings only onto canonical
  model fields (never `source_row` or `converted_from`), no two fields reading the same header.
- **No profile: unchanged.** Every loader takes `file_format=None` by default and then runs the
  pre-profile statements unchanged. `tests/test_ingestion_profiles.py` replaces every profile-only
  function with one that fails and runs the CLI, the pipeline and the web helpers without a profile.
- **Reading with a profile** (`ingestion._read_with_format`): decode with the declared codec (a UTF-8
  byte-order mark is only allowed with `utf-8`; undecodable bytes are a file-level issue giving the byte
  position), read with the declared delimiter and `header=None` (so repeated headers stay visible and a
  row with more cells than the header is a parse error), match headers case-insensitively after trimming,
  and reject missing mapped columns, ambiguous columns and repeated headers as file-level issues. A
  single-column header containing another allowed delimiter adds one hint (profile mode only).
- **Values** (`_parse_declared_values`): number and date fields are parsed by `profiles.parse_number` /
  `parse_date` into canonical text before the unchanged models validate the row. For dates this is two
  steps: `parse_date` checks the declared source layout (`does not match the declared date format …`)
  and calendar validity (`is not a valid calendar date …`) and returns ISO `YYYY-MM-DD`; the model's
  `date` field then validates that ISO value exactly as for built-in input. A rejected value is a
  row-level issue with the line, canonical field, original value and a message naming the declared
  format; the row's other problems are reported too. Messages shorten values to 40 characters and issue
  values to 200 characters (profile mode only).
- **Provenance.** `load_dataset` stores the profile on `Dataset.profile`, exposed as
  `InvestigationResult.profile`, so a report can only name the profile the data was actually read with.
  `reporting` adds `Import profile: <name> (sha256 <12 hex>)` to the console output and the Markdown
  report (name Markdown-escaped) only when a profile is set; CSV columns are unchanged.
- **CLI.** An invalid or unreadable profile prints every problem to stderr and exits 2 before any CSV
  is read; no reports are written. Because argparse accepts unambiguous prefixes, adding `--profile` made
  `--p` ambiguous with `--purchase-orders` (an argparse error, exit 2), and `--pr` now resolves to
  `--profile`. The profile applies to all four files, including an automatically found
  `uom_conversions.csv`.
- **Web app.** `webapp.check_profile` validates uploaded bytes in memory; the profile's bytes (valid or
  not) are part of the stale-result fingerprint, and the fingerprint without a profile is unchanged.

### Approved deviations from the Milestone 7 design

- Milestone 7 did not change how files are read and reconciled without a profile. Compared against
  `origin/main` at the start of the milestone (commit `581c597`) on `data/sample` and `data/evaluation`,
  runs without a profile gave byte-identical CLI CSV files and evaluation report, a Markdown report that
  differed only in its generated timestamp, and console output that differed only in the output paths it
  prints (a record of that comparison, not a guarantee for later versions). Profile-independent
  additions: the web upload mode shows the profile uploader and example download (so an existing UI test
  assertion counting four download buttons became an exact list of five keys), and `--p` is now ambiguous.
- Equivalence (acceptance criterion 2) uses the amended rule: messages must match except where the raw
  value differs or where design rules 10–11 require a format-specific rejection message.
- A missing mapped column names the source header in the message and the canonical field in the issue's
  `field`, not both in the message.
- The wrong-delimiter hint is given only in profile mode.
- File names differ from the design's plan (e.g. `tests/test_cli_profiles.py`, a separate
  `tests/test_profile_evaluation_equivalence.py`, fixture READMEs, the example profile delivered with the
  web app).

### Deferred follow-ups

- Design rule 6 says reports should state which encoding was used; decision D4 kept provenance to the
  profile name and hash, so reports do not show the encoding.
- Design rule 10 says rejection messages should include the source header; they currently give the
  canonical field and the declared format only.
- Out of scope (no-profile path, unchanged): a row with one more cell than the header shifts values one
  column to the left (pandas treats the extra cell as an index), and a CSV column named `converted_from`
  is read into that internal field.

## Units of measure

`uom.py` runs between matching and detection (`pipeline.investigate`). Documents may carry an optional
`unit_of_measure`; conversions come only from an optional per-SKU table (`uom_conversions.csv`:
`sku, uom, base_uom, units_per_uom`).

- No unit on any document of a PO line → the line is left untouched (pre-UoM behaviour).
- Otherwise every invoice and receipt line is converted into the PO line's unit, as
  `quantity × size(from) / size(to)` and `price × size(to) / size(from)`, where sizes are in the SKU's base unit.
  The converted record keeps a `converted_from` description that the evidence shows.
- The line is *unresolved* (→ `UNIT_OF_MEASURE_MISMATCH`) if a document states no unit, no conversion links
  the units for that SKU through one base unit, or a result is not an exact decimal. Unresolved lines skip
  quantity and price rules; a missing invoice there is not valued, and for duplicate detection their supply
  counts as unverified.
- Ingestion rejects invalid conversion rows (non-positive size, bad unit code, a base unit with size ≠ 1)
  and treats conflicting rows for one `(sku, uom)` as ambiguous, rejecting all of them.

## Local web app

`app.py` (Streamlit) is a presentation layer over the same pipeline as the CLI:

```
browser ──► app.py (layout, widgets) ──► webapp.py (plain Python) ──► pipeline.investigate_files
                                          │  uploads → fixed names in a TemporaryDirectory (deleted after the run)
                                          │  summaries: counts of result.exceptions / dataset.issues
                                          │  downloads: reporting.exceptions_frame / validation_frame / render_markdown
                                          └  CSV formula neutralisation (downloads only)
```

- `webapp.py` imports no Streamlit and contains no business rules, so it is unit-tested directly
  (`tests/test_webapp.py`); `app.py` is tested with Streamlit's `AppTest` (`tests/test_app_ui.py`).
- Each stored result carries a fingerprint of the uploaded bytes, the rule settings and any uploaded
  import profile; any change removes the stored result before the page renders.
- An optional import-profile upload (upload mode only) is validated in memory; an invalid profile shows
  its problems in a table and disables Run. Results read with a profile show its name and short hash.
- Text from uploaded files is shown in tables or Markdown-escaped, never rendered as Markdown/HTML.
- `.streamlit/config.toml` binds the server to `localhost`, disables usage statistics and limits uploads.

## Configuration

`MatchConfig` (Pydantic, validated) reads `OPSRESOLVE_*` environment variables — see `.env.example`.
Invalid configuration aborts the CLI with exit code 2 (checked before an import profile is loaded).

## Outputs

`reports/exception_report.md`, `reports/exceptions.csv`, `reports/validation_issues.csv`, plus a console summary.
With an import profile, the console summary and the Markdown report also name the profile and its hash.
`--fail-on-exceptions` makes the CLI exit 1 when anything is found (for scheduled/CI use).
An invalid import profile makes the CLI exit 2 without writing reports.

## Where an LLM may fit later (not built)

Only on top of the deterministic results: drafting supplier emails or narrative summaries from
already-computed exceptions, or classifying free-text notes. An LLM must never compute, alter or
round an amount, and any generated text must cite the deterministic evidence.
