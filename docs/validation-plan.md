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
- Precision/recall of duplicate detection on real invoice populations is unknown.
- Default tolerances and severity thresholds are placeholders, not tuned to any business.
- Real ERP exports (SAP, NetSuite, Dynamics, etc.) have not been tested: column names, date formats,
  thousands separators, units of measure, multi-line quoted fields and encodings may differ.
- Performance on large files has not been measured.
- Not covered by rules yet: units-of-measure conversion, FX conversion, tax/freight/discount lines,
  credit notes, returns, PO amendments, invoice-level header totals.

## 5. Next validation steps

1. Build a larger generated dataset with a known answer key (thousands of lines, randomised scenarios) and measure detection precision/recall.
2. Property-based tests (e.g. Hypothesis) for the arithmetic: impacts are invariant to row order; clean data never yields exceptions.
3. Pilot on an anonymised real export with a finance user reviewing every exception; record agreed/disagreed outcomes before making any accuracy claims.
