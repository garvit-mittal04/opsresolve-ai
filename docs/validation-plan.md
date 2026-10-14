# OpsResolve AI — Validation Plan

How we establish that OpsResolve is correct, and what has **not** yet been validated.

## 1. What "correct" means

- Every invalid input row is reported with file, line and field, and excluded from matching.
- Every seeded discrepancy in the sample data is detected with the right type, quantity and amount.
- Clean PO lines (including partial deliveries and split billing) produce no exceptions.
- Amounts are exact (`Decimal`, half-up rounding to cents) and reproducible.
- Same input + same config → identical exceptions, IDs and order.
- With an import profile, the same data in another declared layout gives the same records, exceptions
  and score as in the built-in format. Milestone 7 did not change how files are read and reconciled
  without a profile (see section 5, deviation 1, for the comparison performed).

## 2. Automated tests (`pytest`)

| Suite | Covers |
|---|---|
| `tests/test_ingestion.py` | valid load; missing values; invalid number/date/currency; zero/negative quantities; missing column; missing/empty file; blank lines and line numbering; whitespace trimming; duplicate PO line / SKU / supplier conflict; duplicate GRN; duplicate invoices kept for detection |
| `tests/test_matching.py` | partial receipts and split invoices; unmatched records; key is (PO, SKU) not SKU alone |
| `tests/test_detection.py` | each rule's trigger and non-trigger; amount and sign; severity; tolerances (inclusive boundaries); fractional quantities and rounding; currency mismatch skipping price check; missing-record notes for rejected rows; each duplicate rule including false-positive guards (split billing, outside window, different supplier); determinism and ordering |
| `tests/test_config.py` | strict defaults; env overrides; invalid env values rejected |
| `tests/test_evaluation.py` | metric arithmetic and zero denominators; TP/FP/FN matching (multisets, amount pairing, unattributed predictions); answer-key and scenario validation; evaluation dataset integrity (valid inputs, every PO owned by one scenario, all exception types covered, synthetic parties only); measured results pinned; reproducible report; CLI |
| `tests/test_uom.py` | S32 regression; per-SKU conversions (no assumed pack sizes); discrepancies still found after conversion; receipts and non-base units; missing, partial, inexact and cross-base units → `UNIT_OF_MEASURE_MISMATCH`; other rules on unresolved lines; conversion file validation (invalid, ambiguous, duplicate, missing file/column); unchanged results without units; CLI discovery |
| `tests/test_duplicates.py` | S26 (over-ordered duplicate outside the window) and S27 (open PO quantity: noted, not flagged); window not simply widened; notes on missing receipts; conflicting evidence (mixed lines, reformatted numbers with open quantity or different content); incomplete evidence (no PO line, unresolved units); split billing unchanged |
| `tests/test_webapp.py` | web-app helpers: uploads reach the real pipeline (same result as the CLI path), unit conversions, settings passed through, zero exceptions, missing/empty/unreadable/malformed files, invalid conversions, issues vs exceptions, current-run downloads, fingerprints, fixed internal filenames, temp-folder cleanup on success and failure, no writes to the repo, CSV formula neutralisation (`=`, `+`, `-`, `@`, tab, CR) without altering numbers, Markdown escaping, filters; import profiles: validation of uploaded profiles (valid, malformed, non-UTF-8, oversized, clashing separators, every problem listed), a profile run equal to the CLI pipeline, fingerprint unchanged without a profile and changed by adding/editing/removing one, escaped name and short hash, Markdown download names the profile with unchanged CSV columns |
| `tests/test_app_ui.py` | Streamlit `AppTest`: localhost/no-telemetry config, initial state (template and example-profile downloads), uploads → results, zero exceptions, sample run, validation shown separately, unit conversions, engine notes, bad uploads, engine failure, invalid settings, stale results cleared on setting/source/upload changes, downloads built from the current run, filters, uploaded text not rendered as Markdown, hostile upload names; import profiles: uploader only in upload mode, example profile valid, valid-profile feedback and provenance above the metrics, invalid profiles listed as plain text with Run disabled, any profile change clears results, hostile profile names escaped everywhere, sample mode ignores profiles |
| `tests/test_profiles.py` | profile model and parsers: canonical fields derived from the models; valid profiles, partial mappings, every delimiter, encoding alias, date format and separator combination; malformed JSON, size limit, duplicate keys at every level, unknown keys, unsupported values, separator clashes, invalid mappings (unknown fields, internal fields, empty headers, two fields on one header), every problem collected; strict number and date parsing at parser level (`test_malformed_or_ambiguous_numbers_are_rejected`: 30 malformed number inputs across every rule 10 category; `test_dates_not_in_the_declared_format_are_rejected`, `test_impossible_calendar_dates_are_rejected`); and, at row level through ingestion, one case for each of six rule 10 categories (`test_rule_10_rejections_are_row_level_issues_with_value_and_line`: currency symbol `€5,00`, exponent `1e3`, leading decimal separator `,5`, trailing sign `5-`, bracketed negative `(5)`, grouping without a declared thousands separator `1,000`) with line, canonical field and original value |
| `tests/test_ingestion_profiles.py` | ingestion and pipeline with a profile: the ERP fixture (several delimiters, encodings and date formats in one export) equal to the same data in the built-in format and to hand-computed amounts; whitespace handled as without a profile; header mapping, missing/ambiguous/repeated columns, wrong delimiter and its hint, encodings and byte-order marks, short and long rows, line numbers; row-level rejections with line, field and value of `1,200.5` (other convention), `1.23,4` (misplaced grouping), `abc` (letters), an overlong value (shortened), an ISO date under `DD.MM.YYYY` and an impossible date; an empty profile reads the project data like no profile; no-profile paths never call profile code (ingestion, pipeline, CLI, web helpers, with a positive control); the reformatted sample (`sample_de`) equal to `data/sample` |
| `tests/test_cli_profiles.py` | CLI `--profile`: with `--data-dir` and with per-file paths (each option and `--uom-conversions` overriding only its own file); invalid or unreadable profiles (malformed, missing, unknown key, separator clash, oversized, directory, endless stream) exit 2 with every problem on stderr and no reports; provenance line only when a profile is used, name escaped in Markdown, hash of the original bytes, CSV columns unchanged; bounded profile reads |
| `tests/test_profile_evaluation_equivalence.py` | the reformatted evaluation set (`evaluation_de`) through its profile: declared format, no rows without the profile, identical records and all 36 exceptions, S32–S35 unit-of-measure results, and the same score against the unchanged answer key (TP 36, FP 0, FN 1, S36's missed duplicate, S27's note) |
| `tests/test_end_to_end.py` | full pipeline on `data/sample`: exact validation issues, exact exception counts per type, hand-computed amounts, clean lines unflagged, report rendering, CSV round-trip, CLI exit codes |

Rule for expected values: they are computed by hand from the scenario description
(`data/sample/README.md`), never copied from program output.

Rule 10 coverage, precisely: every rule 10 category (currency symbols, letters, exponents, misplaced
grouping, a leading decimal separator, trailing signs, bracketed negatives, grouping without a declared
thousands separator) has at least one row-level case through ingestion. This includes `twelve` in the
reformatted sample (letters). The remaining parser cases (for example `.5`, `5.`, `1E3`, `NaN`, `Infinity`,
`0x10`, `--5`, `+-5`, non-ASCII digits and misplaced space grouping) are tested at parser level only. Ingestion
sends every number field through the same `parse_number` call, so they are expected, but not tested, to give
the same row-level issues. Blank values never reach the parser; they are reported as missing values.

### Import-profile fixtures (`tests/fixtures/profiles/`)

| Fixture | Contents | Used by |
|---|---|---|
| `erp/` + `erp_profile.json`, `canonical/` | a small export combining several settings (`;` cp1252 `DD.MM.YYYY`; tab UTF-8 with byte-order mark `YYYYMMDD`; `\|` ISO-8859-1 `DD/MM/YYYY`; a BOX conversion) and the same data in the built-in format | ingestion, CLI and web-app profile tests |
| `sample_de/` + `sample_de_profile.json` | `data/sample` rewritten as a German ERP export: `;`, cp1252, German headers with umlauts, `DD.MM.YYYY`, decimal comma, thousands dot; its three invalid rows kept invalid | criterion 2 equivalence tests |
| `evaluation_de/` + `evaluation_de_profile.json` | the four input files of `data/evaluation` in the same German layout, with a mapped `unit_of_measure` column (`Mengeneinheit`) and a profile-loaded conversion file | `tests/test_profile_evaluation_equivalence.py` |

Each reformatted folder has a README describing the row-by-row transformation (nothing added, removed or
reordered, so line numbers match). The answer key is never copied: the evaluation tests read it from
`data/evaluation`.

Sensitivity checks performed during milestone 7 (on temporary copies, not the committed fixtures):
altering one price, date, unit or conversion factor in a reformatted copy breaks the record or exception
comparison; bypassing the strict number parser makes the rule 10 row-level tests fail; inserting a silent
profile-code call into the no-profile path fails the no-profile tests; a regression to an unbounded profile
read fails the bounded-read tests within a second instead of hanging.

Test sensitivity check (performed during milestone 1): deliberately breaking the price-impact
formula and the quantity tolerance comparison caused 16 tests to fail; restoring the code
returned the suite to green.

## 3. Manual review checklist (per release)

- [ ] Run `python -m opsresolve` and read `reports/exception_report.md` end to end.
- [ ] For three random exceptions, recompute the amount by hand from the source CSV lines cited in the evidence.
- [ ] Confirm no recommended action asserts a root cause.
- [ ] Confirm "possible duplicate" wording and that duplicate/quantity exceptions are cross-referenced.
- [ ] Confirm `.env` and `reports/` are not tracked by Git.

## 4. Not yet validated (be explicit)

- **No real customer data has been processed and no customer has validated the output.**
  All data is synthetic; supplier names are fictional.
- Precision/recall are measured only on the synthetic evaluation dataset (`data/evaluation`, see README
  "Evaluation"): F1 0.986 overall and 0.889 for duplicate detection on 36 scenarios, where S36 is a
  duplicate the records cannot distinguish from a legitimate invoice. Accuracy on real
  invoice populations is unknown.
- Default tolerances and severity thresholds are placeholders, not tuned to any business.
- Real ERP exports (SAP, NetSuite, Dynamics, etc.) have not been tested. Import profiles can declare other
  column names, delimiters, encodings, date and number formats, but they have only been tested on
  synthetic reformatted copies of the project data. Multi-line quoted fields still shift line numbers.
- Encodings: bytes that are invalid in the declared encoding are rejected (invalid UTF-8, or one of the five
  bytes `cp1252` leaves undefined; `iso-8859-1` accepts every byte), but a file whose bytes are valid in the
  declared encoding while it was written in another one (e.g. UTF-8 declared as `cp1252`, or `cp1252`
  declared as `iso-8859-1`) is read without error and with wrong characters; this cannot be detected.
- Performance on large files has not been measured.
- Unit-of-measure conversion is per SKU only; supplier-specific pack sizes and real item-master data are untested.
- Not covered by rules yet: FX conversion, tax/freight/discount lines,
  credit notes, returns, PO amendments, invoice-level header totals.

## 5. Milestone 7 (import profiles) acceptance record

Each of the fourteen acceptance criteria of the import-profile design has evidence (named tests, or for
the no-profile and file-scope criteria recorded output and diff comparisons; listed in the milestone's
acceptance audit). Some are met only with the approved deviations below, so not every original design
requirement is met as written.

Approved deviations:

1. Milestone 7 did not change how files are read and reconciled without a profile. Evidence: runs without
   a profile were compared against `origin/main` at the start of the milestone (commit `581c597`) on
   `data/sample` and on `data/evaluation` with its conversion file, after Stages 1–4, G1 and G4 and again
   during the documentation pass (G2, G3 and the criterion 4 tests changed tests only): CLI CSV files and
   the evaluation report were byte-identical, the Markdown report
   differed only in its generated timestamp, and console output only in the output paths it prints. This is
   a record of that comparison, not a promise for later versions; the automated tests check that runs
   without a profile never reach profile code, not byte identity with `581c597`. Profile-independent
   additions: the web upload mode shows the profile uploader and example download, so one existing UI test
   assertion was changed (it counted four download buttons and now lists the five expected keys), and the
   CLI abbreviation `--p` is now ambiguous between `--profile` and `--purchase-orders`.
2. Equivalence of the reformatted sample (criterion 2) uses the amended rule: messages must match except
   where the original raw value differs, or where the declared-format rules (design rules 10–11) require a
   format-specific rejection message (e.g. `twelve` in a number field).
3. A missing mapped column puts the source header in the message and the canonical field in the issue's
   `field`, rather than both names in the message.
4. The wrong-delimiter hint is given only in profile mode; runs without a profile are unchanged.
5. Delivered file names differ from the design's file plan (`tests/test_cli_profiles.py`, a separate
   `tests/test_profile_evaluation_equivalence.py`, fixture READMEs, the example profile delivered with the
   web app).

Deferred follow-ups:

6. Reports do not state the encoding used (design rule 6); provenance is the profile name and hash
   (decision D4).
7. Number and date rejection messages do not include the mapped source header (design rule 10).
8. Out of scope, unchanged: without a profile, a row with one more cell than the header shifts values one
   column left, and a CSV column named `converted_from` is read into that internal field.

## 6. Next validation steps

1. Grow the evaluation dataset (more scenarios per category, generated variations, non-default tolerances) and keep the answer key hand-derived.
2. Property-based tests (e.g. Hypothesis) for the arithmetic: impacts are invariant to row order; clean data never yields exceptions.
3. Pilot on an anonymised real export with a finance user reviewing every exception; record agreed/disagreed outcomes before making any accuracy claims.
