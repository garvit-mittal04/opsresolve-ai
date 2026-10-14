# CLAUDE.md — OpsResolve AI project instructions

Persistent guidance for anyone (human or AI) working in this repository.

## What this project is

OpsResolve AI investigates operations exceptions for manufacturing and distribution
businesses: discrepancies between purchase orders (PO), supplier invoices and goods-received
notes (GRN). It finds the discrepancy, quantifies it, lists the evidence and recommends a
corrective action.

Current state: **Milestone 7 (import profiles)** — a deterministic Python engine (no LLM) with unit-of-measure
normalisation and evidence-based duplicate detection, a labelled synthetic evaluation set, a CLI and
a local Streamlit web app, plus optional, explicitly selected import profiles for other CSV layouts.
See `docs/architecture.md`; Milestone 7's approved deviations and deferred follow-ups are listed in
`docs/validation-plan.md` (section 5).

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
  profiles.py    Import profiles: load/validate JSON, strict number & date parsers   (reads no CSV; no rules)
  ingestion.py   CSV -> validated records + ValidationIssues   (Pandas + Pydantic; no rules; optional profile)
  matching.py    Group invoices/receipts under PO lines by (po_number, sku)   (pure)
  uom.py         Convert documents to the PO line's unit via per-SKU table    (pure)
  detection.py   Exception rules, amounts, severity, recommended actions     (pure)
  pipeline.py    Orchestration: ingestion -> matching -> uom -> detection
  reporting.py   Markdown / CSV / console rendering                         (no rules)
  webapp.py      Helpers for app.py: temp-file runs, summaries, safe downloads (no rules, no Streamlit)
  evaluation.py  Scores the pipeline against data/evaluation's answer key    (P/R/F1)
  cli.py         Command-line entry point
app.py           Streamlit local web app (layout only; run: streamlit run app.py)
data/sample/     Synthetic CSVs + README describing every seeded scenario
data/evaluation/ Labelled evaluation dataset (scenarios.csv, expected_exceptions.csv)
tests/           Pytest suite (unit, end-to-end, evaluation, web-app helpers, Streamlit AppTest UI, import profiles)
tests/fixtures/profiles/  Import-profile fixtures; reformatted copies of the sample and evaluation data (each with a README)
docs/            architecture.md, validation-plan.md; examples/import-profile-example.json
```

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,app]"  # "app" adds Streamlit; without it the UI tests are skipped
pytest                       # run all tests
python -m opsresolve         # run on data/sample, write reports/ (git-ignored)
python -m streamlit run app.py   # local web app on http://localhost:8501
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
- Keep dependencies minimal (pandas, pydantic; pytest for dev; Streamlit only in the optional `app` extra).
- The web app must stay a thin layer: no business rules in `app.py` or `webapp.py`, uploaded text never
  rendered as raw Markdown/HTML, and the server bound to localhost with telemetry off.
- Import profiles: never discover or guess a profile, delimiter, encoding or format; a profile is used only
  when explicitly selected. Without a profile, data ingestion and reconciliation must stay unchanged: the same
  records, validation issues, exceptions and report content as before profiles existed (the no-profile tests
  in `tests/test_ingestion_profiles.py` fail if profile code is reached). This guarantee does not cover
  everything else: Milestone 7's documented deviations, such as the web app's profile uploader and the
  ambiguous `--p` abbreviation, are listed in `docs/validation-plan.md` (section 5). Keep the allowlists
  strict, reject values that do not match the declared format, and keep
  `docs/examples/import-profile-example.json` valid (tested in `tests/test_app_ui.py`, which needs the `app`
  extra; CI installs it). The evaluation command has no profile option. Use `--profile` in full on the command
  line (`--p` is ambiguous between `--profile` and `--purchase-orders`; `--pr` resolves to `--profile`).
