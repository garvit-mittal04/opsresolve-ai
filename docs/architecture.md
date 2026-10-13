# OpsResolve AI — Architecture

## Goals of milestone 1

A working, deterministic three-way-match exception investigator:

1. Load purchase order, invoice and goods-received CSV files.
2. Validate input; report missing/invalid fields with file, line and field.
3. Match records with deterministic business rules.
4. Detect quantity mismatches, price mismatches, missing records and possible duplicate invoices.
5. Calculate discrepancies exactly.
6. Produce a readable exception report (Markdown + CSV + console summary).

No LLM is used anywhere in milestone 1.

## Data flow

```
 CSV files ──► ingestion.py ──► validated records ──► matching.py ──► MatchResult
               (Pandas reads                         (group by          │
                raw strings;                          po_number+sku)    ▼
                Pydantic validates)                               detection.py
                     │                                            (rules, amounts,
                     └──► ValidationIssues + rejected keys ──────► severity, actions)
                                                                        │
                                                                        ▼
                                              reporting.py ◄── InvestigationResult (pipeline.py)
                                              (Markdown, CSV, console)
```

| Layer | Responsibility | Must not |
|---|---|---|
| `ingestion` | Read CSV as strings, normalise headers, validate each row via Pydantic, enforce key uniqueness | apply business rules |
| `matching` | Attach invoice and receipt lines to the PO line they reference | judge whether anything is wrong |
| `detection` | Apply rules, compute differences and amounts, assign severity, write evidence and actions | read files or format reports |
| `reporting` | Render results | compute business values |
| `pipeline` / `cli` | Wire layers together, load config | contain rules |

## Data model

All quantities and money are `Decimal`. Dates must be ISO `YYYY-MM-DD` (no ambiguous formats).

| File | Required columns | Key |
|---|---|---|
| `purchase_orders.csv` | po_number, line_number, supplier_id, supplier_name, sku, quantity_ordered (>0), unit_price (>=0), currency, order_date (`description` optional) | (po_number, line_number) and (po_number, sku) unique |
| `invoices.csv` | invoice_number, invoice_date, supplier_id, po_number, sku, quantity_invoiced (>0), unit_price (>=0), currency | none enforced — repeats are a *business* exception |
| `goods_received.csv` | grn_number, received_date, supplier_id, po_number, sku, quantity_received (>=0) | (grn_number, po_number, sku) unique |

### Validation behaviour

- Missing file, empty file, unparseable file or missing required column → file-level issue, no rows loaded from that file.
- Invalid row → every failing field reported (file, line, field, value, message); row rejected.
- Duplicate PO line / SKU on a PO / conflicting supplier on a PO / duplicate GRN line → later row rejected, first kept.
- Line numbers are physical CSV line numbers (header = line 1). Blank lines are skipped without shifting numbering.
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
| `SUPPLIER_MISMATCH` | invoice supplier ≠ PO supplier | – | invoice line amount | HIGH |
| `SUPPLIER_MISMATCH` | GRN supplier ≠ PO supplier | – | 0 | MEDIUM |
| `MISSING_GOODS_RECEIPT` | PO line invoiced, nothing received | Σinvoiced | Σ invoice line amounts | by amount |
| `MISSING_INVOICE` | PO line received, nothing invoiced | Σreceived | Σreceived × PO price (accrual, not a loss) | LOW |
| `MISSING_PURCHASE_ORDER` | invoice line with no PO line | – | invoice line amount | by amount, min MEDIUM |
| `MISSING_PURCHASE_ORDER` | receipt line with no PO line | received qty | 0 (receipts carry no price) | MEDIUM |
| `POSSIBLE_DUPLICATE_INVOICE` | same invoice number repeats an identical line | qty × (n−1) | line amount × (n−1) | by amount, min MEDIUM |
| `POSSIBLE_DUPLICATE_INVOICE` | two invoice numbers from one supplier where (a) numbers are equal after normalisation, or (b) line content is identical, dated within the window, **and** combined billing exceeds received or ordered qty | – | later invoice total | by amount, min MEDIUM |

Severity by amount (absolute value): ≥ 1,000.00 → HIGH; ≥ 100.00 → MEDIUM; else LOW (configurable).

Design notes:

- **Billing quantity impact uses the PO price** so that a price variance on the same line is not counted twice; it is reported separately.
- **Content-identical invoices alone are not flagged** — split billing (two identical invoices for two identical deliveries) is legitimate. Rule (b) requires evidence of over-billing. Rule (a) (re-keyed invoice number such as `CIB-7781` vs `CIB-07781`) is flagged regardless.
- Each later invoice is reported once, against its earliest match.
- A duplicate invoice also inflates the billed quantity, so the same money can appear in a duplicate exception and a quantity exception. Such exceptions are cross-referenced with a note, and the report warns that amounts must not be summed across types.
- Exceptions are sorted by severity, then |amount| descending, then type/PO/SKU, and numbered `EXC-0001…` — the same input always yields the same report.
- A PO line with no invoices and no receipts is open, not an exception.

## Configuration

`MatchConfig` (Pydantic, validated) reads `OPSRESOLVE_*` environment variables — see `.env.example`.
Invalid configuration aborts the CLI with exit code 2.

## Outputs

`reports/exception_report.md`, `reports/exceptions.csv`, `reports/validation_issues.csv`, plus a console summary.
`--fail-on-exceptions` makes the CLI exit 1 when anything is found (for scheduled/CI use).

## Where an LLM may fit later (not built)

Only on top of the deterministic results: drafting supplier emails or narrative summaries from
already-computed exceptions, or classifying free-text notes. An LLM must never compute, alter or
round an amount, and any generated text must cite the deterministic evidence.
