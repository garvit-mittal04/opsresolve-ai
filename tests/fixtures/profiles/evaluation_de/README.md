# Reformatted evaluation set (test fixture)

A copy of the four input files of `data/evaluation` written the way a German ERP export might look,
read with `../evaluation_de_profile.json`. `tests/test_profile_evaluation_equivalence.py` checks that it
gives the same records and exceptions as the original and the same score against the hand-written
answer key (milestone 7, acceptance criteria 12 and 13). The answer key itself
(`data/evaluation/expected_exceptions.csv` and `scenarios.csv`) is not copied: the tests read it from
`data/evaluation`, unchanged. The data is the same synthetic data; all supplier names are fictional.

Transformation applied to each file, row by row, with nothing added, removed or reordered (so every line
number is unchanged):

| Aspect | `data/evaluation` | This copy |
|---|---|---|
| Delimiter | `,` | `;` |
| Encoding | UTF-8 | cp1252 (every file has a header with `ä` or `ü`) |
| Headers | canonical field names | German headers, mapped in the profile; `unit_of_measure` is `Mengeneinheit` |
| Dates | `2026-07-01` | `01.07.2026` |
| Numbers | `1000`, `2.50`, `12` | `1.000`, `2,50`, `12` (thousands dot, decimal comma) |
| Line endings | `\n` | `\n` |

Text values (IDs, SKUs, names, descriptions, currencies, units) are copied unchanged, including empty
units of measure. The conversion file (`uom_conversions.csv`) is converted the same way and read with
the same profile, so the unit-of-measure scenarios S32-S35 go through a profile-loaded conversion file.
