# CLAUDE.md — OpsResolve AI project instructions

Persistent guidance for anyone (human or AI) working in this repository.

## What this project is

OpsResolve AI investigates operations exceptions for manufacturing and distribution
businesses: discrepancies between purchase orders (PO), supplier invoices and goods-received
notes (GRN). It finds the discrepancy, quantifies it, lists the evidence and recommends a
corrective action.

Current state: **milestone 1** — a deterministic Python core (no LLM). See `docs/architecture.md`.

## Non-negotiable rules

1. **No LLM for financial calculations.** Every quantity, price and amount is computed by
   deterministic code using `decimal.Decimal`. Never use `float` for money or quantities.
   Round money only at the edge of a calculation with `models.round_money` (half-up, cents).
2. **Do not invent root causes.** Exceptions state what differs, by how much, and the
   evidence. Recommended actions describe how to investigate/resolve; they never assert *why*
   a discrepancy happened. Duplicates are always "possible" until a person confirms.
3. **Do not claim customer validation** or real-world accuracy. Sample data is synthetic and
   all supplier names are fictional.
4. **Never silently repair input.** Invalid rows are rejected and reported with file, line and
   field. Do not guess missing values.
5. **Keep layers separate** (see below). Business rules must not read files or format output.
6. **Secrets stay out of Git.** Use `.env` (git-ignored); document new variables in `.env.example`.
7. **Do not commit or push without the repository owner's explicit approval.**

## Layout

```
src/opsresolve/
  models.py      Pydantic domain models (records, exceptions, validation issues)
  config.py      MatchConfig: tolerances & severity thresholds (env: OPSRESOLVE_*)
  ingestion.py   CSV -> validated records + ValidationIssues   (Pandas + Pydantic; no rules)
  matching.py    Group invoices/receipts under PO lines by (po_number, sku)   (pure)
  detection.py   Exception rules, amounts, severity, recommended actions     (pure)
  pipeline.py    Orchestration: ingestion -> matching -> detection
  reporting.py   Markdown / CSV / console rendering                         (no rules)
  evaluation.py  Scores the pipeline against data/evaluation's answer key    (P/R/F1)
  cli.py         Command-line entry point
data/sample/     Synthetic CSVs + README describing every seeded scenario
data/evaluation/ Labelled evaluation dataset (scenarios.csv, expected_exceptions.csv)
tests/           Pytest suite (unit + end-to-end on sample data)
docs/            architecture.md, validation-plan.md
```

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                       # run all tests
python -m opsresolve         # run on data/sample, write reports/ (git-ignored)
```

## Working conventions

- Add or change a rule → add unit tests in `tests/test_detection.py` with hand-computed
  expected amounts, and update `docs/architecture.md` (rule table) if behaviour changes.
- Change sample data → update `data/sample/README.md` and `tests/test_end_to_end.py`.
  Expected values in tests are derived by hand from the scenario, never pasted from output.
- The evaluation answer key (`data/evaluation/expected_exceptions.csv`) is ground truth written by hand.
  Never edit it to match engine output or to raise a score. If detection behaviour changes, update the
  pinned results in `tests/test_evaluation.py` and the README results table in the same change.
- Output ordering and exception IDs must stay deterministic (same input → same report).
- CI (`.github/workflows/ci.yml`) runs `pytest` on Python 3.10 and 3.14 for pushes and PRs to `main`;
  keep it green, and keep its permissions read-only and its actions pinned to commit SHAs.
- Run `pytest` and make it pass before declaring work complete. Do not report tests as passing
  unless they were actually run.
- Keep dependencies minimal (pandas, pydantic; pytest for dev).
