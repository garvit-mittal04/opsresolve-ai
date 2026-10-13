# OpsResolve AI — Validation Plan

How we establish that OpsResolve is correct, and what has **not** yet been validated.

## 1. What "correct" means for milestone 1

- Every invalid input row is reported with file, line and field, and excluded from matching.
- Every seeded discrepancy in the sample data is detected with the right type, quantity and amount.
- Clean PO lines (including partial deliveries and split billing) produce no exceptions.
- Amounts are exact (`Decimal`, half-up rounding to cents) and reproducible.
- Same input + same config → identical exceptions, IDs and order.

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
| `tests/test_end_to_end.py` | full pipeline on `data/sample`: exact validation issues, exact exception counts per type, hand-computed amounts, clean lines unflagged, report rendering, CSV round-trip, CLI exit codes |

Rule for expected values: they are computed by hand from the scenario description
(`data/sample/README.md`), never copied from program output.

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
- Real ERP exports (SAP, NetSuite, Dynamics, etc.) have not been tested: column names, date formats,
  thousands separators, units of measure, multi-line quoted fields and encodings may differ.
- Performance on large files has not been measured.
- Unit-of-measure conversion is per SKU only; supplier-specific pack sizes and real item-master data are untested.
- Not covered by rules yet: FX conversion, tax/freight/discount lines,
  credit notes, returns, PO amendments, invoice-level header totals.

## 5. Next validation steps

1. Grow the evaluation dataset (more scenarios per category, generated variations, non-default tolerances) and keep the answer key hand-derived.
2. Property-based tests (e.g. Hypothesis) for the arithmetic: impacts are invariant to row order; clean data never yields exceptions.
3. Pilot on an anonymised real export with a finance user reviewing every exception; record agreed/disagreed outcomes before making any accuracy claims.
