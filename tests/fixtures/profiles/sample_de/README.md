# Reformatted sample (test fixture)

A copy of `data/sample` written the way a German ERP export might look, read with
`../sample_de_profile.json`. `tests/test_ingestion_profiles.py` checks that it gives the same
records, exceptions and validation outcomes as the original (milestone 7, acceptance criterion 2).
The data is the same synthetic data; all supplier names are fictional.

Transformation applied to each file of `data/sample`, row by row, with nothing added, removed or
reordered (so every line number is unchanged):

| Aspect | `data/sample` | This copy |
|---|---|---|
| Delimiter | `,` | `;` |
| Encoding | UTF-8 | cp1252 (headers contain `ä` and `ü`) |
| Headers | canonical field names | German headers, mapped in the profile |
| Dates | `2026-08-03` | `03.08.2026` (day and month groups moved, not validated) |
| Numbers | `5000`, `0.12` | `5.000`, `0,12` (thousands dot, decimal comma) |
| Line endings | `\n` | `\n` |

Text values (IDs, SKUs, names, descriptions, currencies) are copied unchanged. Values that are not
well-formed numbers or ISO dates are copied unchanged, so the three seeded invalid rows stay invalid:
the empty `Stückpreis` (purchase orders, line 16), `twelve` (invoices, line 15), and `2026-13-09`,
which becomes `09.13.2026` (goods received, line 18).
