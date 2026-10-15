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
| `tests/test_webapp.py` | web-app helpers: uploads reach the real pipeline (same result as the CLI path), unit conversions, settings passed through, zero exceptions, missing/empty/unreadable/malformed files, invalid conversions, issues vs exceptions, current-run downloads, fingerprints, fixed internal filenames, temp-folder cleanup on success and failure, no writes to the repo, CSV formula neutralisation (`=`, `+`, `-`, `@`, tab, CR) without altering numbers, Markdown escaping, filters, the report's settings line identical for web and CLI configurations (`0%`, `0.00`, `0` for zero tolerances; no decimals rounded away); import profiles: validation of uploaded profiles (valid, malformed, non-UTF-8, oversized, clashing separators, every problem listed), a profile run equal to the CLI pipeline, fingerprint unchanged without a profile and changed by adding/editing/removing one, escaped name and short hash, Markdown download names the profile with unchanged CSV columns; AI explanations (Milestone 8, Stage C): readiness and fixed status messages, the exact request preview (sends nothing), literal display of accepted answers, rejected answers as problem codes and allow-listed field locations only, fixed failure messages, `AIRequestGuard` (one request per finding at a time); investigation checks (Stage D2): display views built from the checks, a failed check carried with its fixed label only, a fixed message when the checks cannot be prepared, hostile text made literal, results computed once per run and recomputed when the fingerprint or the run changes, findings and downloads unchanged, no AI SDK, key or network needed |
| `tests/test_app_ui.py` | Streamlit `AppTest` (run as if bound to localhost): the single shared config (no server address, no telemetry, XSRF on, no other `config.toml` in the repository), the local app running only on `localhost`, `127.0.0.1` or `::1` and otherwise showing only the launch command, every documented local launch command passing `--server.address localhost`, long validation problems wrapping instead of being cut off, initial state (template and example-profile downloads), uploads → results, zero exceptions, sample run, validation shown separately, unit conversions, engine notes, bad uploads, engine failure, invalid settings, stale results cleared on setting/source/upload changes, downloads built from the current run, filters, uploaded text not rendered as Markdown, hostile upload names; import profiles: uploader only in upload mode, example profile valid, valid-profile feedback and provenance above the metrics, invalid profiles listed as plain text with Run disabled, any profile change clears results, hostile profile names escaped everywhere, sample mode ignores profiles; AI explanations (Stage C): off by default with nothing AI-related run, readiness reasons without the SDK or key, previewing and enabling send nothing, an answer shown only after an explicit send and only as literal text, rejected answers and provider failures as codes and fixed messages, findings and downloads unchanged, explanations cleared when inputs change, one request per finding at a time; investigation checks (Stage D2): statuses, details and cited records in the detail panel, findings outside the catalogue, a failed check shown only as "H<n>: Check failed — nothing concluded", fixed messages when a finding's checks or the whole run's checks cannot be prepared, hostile document numbers and upload names kept literal, computed once per run and again after every change, findings and downloads unchanged, working without the AI SDK or a key |
| `tests/test_profiles.py` | profile model and parsers: canonical fields derived from the models; valid profiles, partial mappings, every delimiter, encoding alias, date format and separator combination; malformed JSON, size limit, duplicate keys at every level, unknown keys, unsupported values, separator clashes, invalid mappings (unknown fields, internal fields, empty headers, two fields on one header), every problem collected; strict number and date parsing at parser level (`test_malformed_or_ambiguous_numbers_are_rejected`: 30 malformed number inputs across every rule 10 category; `test_dates_not_in_the_declared_format_are_rejected`, `test_impossible_calendar_dates_are_rejected`); and, at row level through ingestion, one case for each of six rule 10 categories (`test_rule_10_rejections_are_row_level_issues_with_value_and_line`: currency symbol `€5,00`, exponent `1e3`, leading decimal separator `,5`, trailing sign `5-`, bracketed negative `(5)`, grouping without a declared thousands separator `1,000`) with line, canonical field and original value |
| `tests/test_ingestion_profiles.py` | ingestion and pipeline with a profile: the ERP fixture (several delimiters, encodings and date formats in one export) equal to the same data in the built-in format and to hand-computed amounts; whitespace handled as without a profile; header mapping, missing/ambiguous/repeated columns, wrong delimiter and its hint, encodings and byte-order marks, short and long rows, line numbers; row-level rejections with line, field and value of `1,200.5` (other convention), `1.23,4` (misplaced grouping), `abc` (letters), an overlong value (shortened), an ISO date under `DD.MM.YYYY` and an impossible date; an empty profile reads the project data like no profile; no-profile paths never call profile code (ingestion, pipeline, CLI, web helpers, with a positive control); the reformatted sample (`sample_de`) equal to `data/sample` |
| `tests/test_cli_profiles.py` | CLI `--profile`: with `--data-dir` and with per-file paths (each option and `--uom-conversions` overriding only its own file); invalid or unreadable profiles (malformed, missing, unknown key, separator clash, oversized, directory, endless stream) exit 2 with every problem on stderr and no reports; provenance line only when a profile is used, name escaped in Markdown, hash of the original bytes, CSV columns unchanged; bounded profile reads |
| `tests/test_profile_evaluation_equivalence.py` | the reformatted evaluation set (`evaluation_de`) through its profile: declared format, no rows without the profile, identical records and all 36 exceptions, S32–S35 unit-of-measure results, and the same score against the unchanged answer key (TP 36, FP 0, FN 1, S36's missed duplicate, S27's note) |
| `tests/test_explanation.py` | Milestone 8, Stage A (no model, network or extra dependency): evidence packs for one exception of every type (engine evidence mapped to source records, typed values without supplier names or item descriptions, converted records keeping original and compared values, identifying values, rejected or unavailable source records listed as limitations); SHA-256 pack hashing and determinism; packs immutable and inputs unchanged; the answer contract and `validate_explanation`: wrong exception id, evidence references (existence, ranges, document numbers starting with `E`), schema violations, unexpected or missing fields, malformed and oversized output; numbers, identifiers and dates read as whole tokens and checked against the evidence each fact cites (line numbers versus quantities, leading zeros, digits-only identifiers, whole ISO dates, number words); direction and sign of the engine's figures; currencies, units, codes and known supplier names; sufficiency, missing information and ambiguity; every problem reported and nothing partially accepted; boundaries (no new dependency or network) |
| `tests/test_ai.py` | Milestone 8, Stage B, all offline (fakes or a mocked HTTP transport; a fixture blocks network connections): configuration defaults and `OPSRESOLVE_AI_*` overrides; invalid settings, a missing key, a missing or broken SDK and client-construction errors give `not_configured` with nothing sent; the key only from `ANTHROPIC_API_KEY`, passed explicitly; only the documented fields sent and the preview equal to the request body; the response schema uses only supported keywords, with the omitted limits still enforced; supplier ids pseudonymised (every loaded file, engine text, digits-only ids, overlapping ids, structured fields equal to an id kept); the privacy guard sends nothing if a supplier id or a whole-word supplier name remains; every answer validated by the Stage A validator before and after real ids are restored; malformed, refused and empty answers; provider errors mapped to safe codes and unexpected exceptions contained; logs hold no key, prompt, evidence, names or answer, and SDK debug records are dropped only for the requesting thread; import isolation (only `ai.py` refers to the SDK, imported lazily; only `webapp.py` imports `opsresolve.ai`); 11 tests against the real SDK with a mocked transport (accepted answer, status errors, transport failures, refusal, `ANTHROPIC_BASE_URL` ignored, no request in debug logs), skipped without the `ai` extra unless `OPSRESOLVE_REQUIRE_AI_SDK_TESTS=1` (set in CI) makes them fail instead |
| `tests/test_investigation.py` | Milestone 8, Stage D1: the hand-written fixture `tests/fixtures/investigations/` (scenarios A–R; 24 exceptions and 2 rejected rows, derived by hand in its README) checked against `expected_investigations.csv` (38 check results) with `strict=True`, so a check that raises fails the test; the PO-number similarity rules and the same-supplier-and-SKU requirement; rejected purchase-order, invoice and receipt rows, including rows whose PO number or SKU could not be read; receipt not posted (never "contradicted"); unit mix-up (exact factors, a loaded conversion table, one document among several, several documents without a fit, under-billing); units across PO lines (stated, unstated and other units never matched); price filters (supplier, SKU, currency, unit); over-receipts against another line's open quantity; several fits marked not unique; a failing check isolated and recorded by class name only, failures before the checks contained per exception, and indexing failures never contained; invariants on the fixture, `data/sample` and `data/evaluation` (citations resolve, sequential H and R references, nothing changed, deterministic, no failed checks, wording never claims a confirmed cause); the approved five-check catalogue; speed on the evaluation data; purity and the import boundary (only `cli.py` and `webapp.py` import the layer) |
| `tests/test_investigations_csv.py` | Milestone 8, Stage D3, the CLI's `--with-investigations`: without it, the output files and console output are unchanged and the checks are never run; with it, `exceptions.csv` and `validation_issues.csv` are byte-identical and the Markdown report differs only in its timestamp; one row per check matching the fixture expectations, in exception and check order, with resolvable record references and one row for an exception without checks; deterministic output; no wording claiming a confirmed cause; `neutralize_cell` (formula triggers `=`, `+`, `-`, `@`, tab and carriage return prefixed with `'`; plain numbers, ordinary text and non-text values unchanged), identical to the web app's rule; hostile PO numbers, SKUs and document numbers neutralised in `investigations.csv` but written unchanged to `exceptions.csv`; a failed check with its status and fixed label only; a failure before the checks marking only that exception; an indexing failure exiting 3 with only the fixed message, an earlier `investigations.csv` file removed and the other reports complete; a directory named `investigations.csv` and a symlink's target left alone; a stale file that cannot be removed giving only a fixed warning (exit 3 still taking precedence over 1); `--fail-on-exceptions` unchanged; no AI SDK, key or network needed |
| `tests/test_end_to_end.py` | full pipeline on `data/sample`: exact validation issues, exact exception counts per type, hand-computed amounts, clean lines unflagged, rejected-row notes worded "A row of the <file> file …", report rendering, CSV round-trip, CLI exit codes |
| `tests/test_public_demo.py` | the public-demo mode (`demo/streamlit_app.py`): synthetic-data notice and only the bundled sample (no data-source choice); no upload, template or profile widget before or after a run; the documented sample counts; results identical to the local app's sample run and to the engine called directly; no AI toggle, section, readiness check, preview, request or SDK load in any finding's panel even with a key configured, and a crafted `ai_enabled` session state has no effect; a fixed error when the bundled sample is missing; separate state per browser session; formula-protected downloads built from the current run; the local app's uploads, import profile and AI toggle unchanged; the demo entrypoint; the demo running with no server address (as hosted) and bound to localhost; error details hidden from code, with no config file under `demo/`; `requirements.txt` as `-e .[app]` with pins inside `pyproject.toml`'s ranges and no AI extra, and no competing dependency file in `demo/` or the root; no secret files or credential patterns in the repository and secret files git-ignored |

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
- [ ] Public demo, once deployed: run the post-deployment checks in the README ("Deploying the public demo") at the
      public address and record the date, commit and result before linking to it.

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
- S36 remains the known missed duplicate-invoice scenario (a known limitation in `data/evaluation/scenarios.csv`:
  a bill re-sent under a new number on a half-delivered PO, whose records look exactly like the legitimate second
  invoice in S27). The benchmark is synthetic, written by the same team as the rules, and is not evidence of
  accuracy on real supplier data.
- Installation on a fresh user machine (for example by following the README's macOS steps) has not been
  verified; the evidence is CI, which installs the project from scratch on GitHub's Linux runners for every run.
- AI explanations (Milestone 8, Stages A–C): the 11 tests against the real Anthropic SDK are skipped wherever the
  `ai` extra is not installed (as in the local runs recorded in section 6); CI installs it and requires them, so CI
  is the evidence for those tests. No request has been sent to the real API by any test, and the quality of real
  model answers has not been evaluated; the validator checks consistency with the evidence, not truth.
- Investigation checks (Stage D) have been exercised on synthetic data only (`tests/fixtures/investigations/`,
  `data/sample`, `data/evaluation`). Several situations deliberately give "Can't be checked" (records in another or
  unstated unit, a unit mix-up on only some of several documents, possibly rejected rows, no comparable records,
  more invoiced than ordered for "receipt not posted"), and "consistent" cannot see documents missing from the
  export. Their usefulness on real exports is unknown.
- No manual browser walkthrough of the AI-explanation or investigation-check pages is recorded; they are covered
  by Streamlit's `AppTest` only (see the web app limitations in the README for the earlier browser smoke test).

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

## 6. Milestone 8, Stage D (investigation checks) acceptance record

No separate design document with numbered acceptance criteria exists for Stage D. This record lists what was
reviewed and accepted in its three steps (D1 `6e42c3d`, D2 `7abc461`, merged in PR #13; D3 `b42dc4b`, merged in
PR #14), the evidence, and the approved scope decisions and limitations. The design is described in
`docs/architecture.md` ("Investigation checks (Milestone 8, Stage D)").

Accepted behaviour:

- **Deterministic, local checks (D1).** `src/opsresolve/investigation.py` checks candidate explanations from a fixed
  catalogue of five against the loaded records with exact `Decimal` comparisons. It uses no AI and makes no network
  calls, and it never changes an exception, amount, severity, ID, classification or recommended action. Findings
  are candidates for human investigation, never confirmed root causes (CLAUDE.md rule 2 and its exception).
- **Statuses.** Three evidence statuses: "Consistent with the data — not confirmed", "Contradicted by the data"
  and "Can't be checked". Several fitting records are all cited and marked not unique. "Check failed — nothing
  concluded" is an operational state, not evidence: a check that raised an error, recorded by stage and error class
  name only.
- **Evidence references.** Checks are numbered H1, H2, …; cited records R1, R2, … with file, CSV line and document
  number, including records outside the finding's own evidence.
- **Web app (D2).** Each finding's detail panel shows the checks as table cells and `st.text`, never as Markdown.
  A failed check shows only its identifier and the fixed label; when a finding's checks or the whole run's checks
  cannot be prepared, a fixed message is shown and the findings, metrics and downloads are unaffected. The checks
  are computed once per run and recomputed when inputs, settings or the run change.
- **CLI (D3).** `--with-investigations` (off by default) writes `investigations.csv` next to the other reports.
  Every cell is protected against spreadsheet formula injection (text starting with `=`, `+`, `-`, `@`, a tab or a
  carriage return gets a leading `'`; plain numbers such as `-50.00` are unchanged), unlike `exceptions.csv` and
  `validation_issues.csv`, which are written exactly as before. An individual failed check does not change the exit
  status: it is written as a `failed` row with its fixed label, the other checks still run, and stderr gives only a
  count. Only when the checks cannot be prepared at all (an error while indexing the run) does the CLI print a
  fixed message, write the other reports as usual, remove an earlier `investigations.csv` file (for a symlink only
  the link; a directory of that name is left alone; a file that cannot be removed gives a fixed warning) and exit
  3, which takes precedence over the 1 from `--fail-on-exceptions`.
- **Compatibility.** Without the option, the default CLI outputs are unchanged.

Evidence:

- Tests: `tests/test_investigation.py`, `tests/test_investigations_csv.py`, and the Stage D2 tests in
  `tests/test_webapp.py` and `tests/test_app_ui.py` (section 2).
- Historical results recorded during the acceptance audit after D3 was merged (not re-run for this record):
  - GitHub CI on the merge commit `bd8a7d7` (workflow run 38605255493): 1116 passed on Python 3.10 and 3.14,
    including the 11 real-SDK tests; the evaluation step reported TP 36, FP 0, FN 1, precision 1.000, recall 0.973,
    F1 0.986 and exact amounts 36/36.
  - Local runs on the same file contents: 1105 passed and 11 skipped (the real-SDK tests; the `ai` extra was not
    installed) on Python 3.10, 3.13 and 3.14 with Streamlit 1.65; 1032 passed and 12 skipped on Python 3.11 and 3.12,
    which had no Streamlit (the UI test module was skipped as well).
  - Default CLI outputs compared with the commit before Stage D (`8180a79`) on `data/sample` and on
    `data/evaluation`, with and without `--with-investigations`: `exceptions.csv` and `validation_issues.csv`
    byte-identical, `exception_report.md` different only in its `Generated:` line, console output different only in
    the output paths it prints. The evaluation report was identical except for the dataset path line, which reflected
    the path given on the command line.
  - Hostile identifiers (`=HYPERLINK(...)`, `+SUM(...)`, `-2+3`, `@cmd`) passed through the CLI left no unprotected
    formula cell in `investigations.csv`, while `exceptions.csv` kept the raw values as designed.

Approved scope decisions and limitations:

1. No `investigations.md` export was implemented; investigations are written only to `investigations.csv`.
2. Without `--with-investigations`, an `investigations.csv` left by an earlier run is not removed.
3. A directory named `investigations.csv` in the output directory still makes a successful write fail with an
   error (only the cleanup after an indexing failure leaves such a directory alone).
4. Invisible characters in document numbers are kept as they are in `investigations.csv`; only formula triggers are
   changed.
5. Matching is strict: records in another or unstated unit are excluded, never converted; other currencies are not
   compared; a unit mix-up on only some of several documents gives "Can't be checked"; a rejected row whose PO
   number or SKU could not be read, or a file-level problem, makes the affected checks "Can't be checked".
6. The checks have been evaluated on synthetic data only (section 4). They are not used by the AI explanations, the
   evaluation or the default reports.

## 7. Next validation steps

1. Grow the evaluation dataset (more scenarios per category, generated variations, non-default tolerances) and keep the answer key hand-derived.
2. Property-based tests (e.g. Hypothesis) for the arithmetic: impacts are invariant to row order; clean data never yields exceptions.
3. Pilot on an anonymised real export with a finance user reviewing every exception; record agreed/disagreed outcomes before making any accuracy claims.
