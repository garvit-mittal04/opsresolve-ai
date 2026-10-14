# Investigation-check fixtures (Stage D)

Synthetic data for `opsresolve.investigation`. Every supplier is fictional. This set is separate from the
evaluation dataset (`data/evaluation`) and its answer key, which it does not change or extend.

`expected_investigations.csv` lists, for every check the catalogue runs on these files, the status written
by hand from the scenarios below, and the records each check should cite (`file:line`, header is line 1;
empty when the check cites nothing). Statuses describe the loaded data only: "consistent" is never a
confirmed cause.

| Scenario | Data | Exceptions | Expected checks |
|---|---|---|---|
| A | P-1001 SKU-A: ordered 100, invoiced 100, received 60 | `QUANTITY_MISMATCH` | receipt not posted: consistent (100 is within the 100 ordered); unit mix-up: can't be checked (no conversion row for SKU-A) |
| B | P-1002 SKU-B: ordered 50, invoiced 80, received 50; SKU-B has 1 BOX = 10 EA | `QUANTITY_MISMATCH` | receipt not posted: can't be checked (80 is more than the 50 ordered; an unposted delivery above the order cannot be ruled out); unit mix-up: contradicted (80 and 50 do not differ by 10) |
| C | P-1003 SKU-C: ordered 12 at 24.00, invoiced 144 at 2.00, received 12; SKU-C has 1 BOX = 12 EA | `QUANTITY_MISMATCH`, `PRICE_MISMATCH` | quantity: receipt not posted can't be checked (144 > 12 ordered), unit mix-up consistent (144 = 12 x 12); price: unit mix-up consistent (2.00 x 12 = 24.00), price from another order can't be checked (no other order) |
| D | Invoice INV-D2 cites P-2010, GRN G-D9 cites P-9999; P-2001 exists for the same supplier and SKU | two `MISSING_PURCHASE_ORDER` | INV-D2: mistyped PO consistent (P-2010 and P-2001 differ by two swapped adjacent characters); G-D9: contradicted (P-9999 is not close to P-2001) |
| E | Invoice INV-E1 cites P-3009; P-3003 and P-3008 exist for the same supplier and SKU | `MISSING_PURCHASE_ORDER` | mistyped PO: consistent, both POs cited, not unique (each is one character away) |
| F | P-4001 SKU-F: ordered 20, invoiced 20, no receipt; GRN G-F9 (20 units, same supplier and SKU) cites P-4999 | `MISSING_GOODS_RECEIPT`, `MISSING_PURCHASE_ORDER` | missing receipt: receipt not posted consistent, document under another PO consistent (G-F9); G-F9: mistyped PO contradicted (P-4999 is not close to P-4001) |
| G | P-5001 SKU-G: received 30, no invoice; invoices INV-G8 (P-5999) and INV-G9 (P-5888) each bill 30 for the same supplier and SKU (different prices and dates, so not duplicates) | `MISSING_INVOICE`, two `MISSING_PURCHASE_ORDER` | missing invoice: document under another PO consistent, both invoices cited, not unique; INV-G8 and INV-G9: mistyped PO contradicted |
| H | P-6001 SKU-H: ordered 10, received 15, no invoice; P-6002 (same supplier and SKU) ordered 5, nothing received | `QUANTITY_MISMATCH` (over-receipt), `MISSING_INVOICE` | over-receipt: unit mix-up can't be checked, document under another PO consistent (P-6002 has exactly 5 open); missing invoice: can't be checked (no other invoice for the supplier and SKU) |
| J | P-7001 SKU-J at 9.50, invoiced at 10.00; P-7002 (same supplier and SKU) at 10.00, fully matched | `PRICE_MISMATCH` | unit mix-up can't be checked; price from another order consistent (P-7002) |
| K | P-8001 SKU-K at 4.00, invoiced at 4.40; P-8002 (same supplier and SKU) at 4.20 | `PRICE_MISMATCH` | unit mix-up can't be checked; price from another order contradicted |
| L | P-9001 SKU-L: invoiced 10; its only goods-receipt row (G-L1) has an invalid quantity and is rejected | `MISSING_GOODS_RECEIPT` | receipt not posted and document under another PO: can't be checked (a goods-receipt row was rejected) |
| M | P-1101 SKU-M: ordered 24, invoiced 2, received 24; SKU-M has 1 BOX = 12 EA | `QUANTITY_MISMATCH` (under-billed) | unit mix-up: consistent (2 x 12 = 24) |
| N | P-1201 SKU-N: ordered 40, invoiced 40, no receipt; GRN G-N1 (40 units, same supplier and SKU) cites P-1210 | `MISSING_GOODS_RECEIPT`, `MISSING_PURCHASE_ORDER` | missing receipt: receipt not posted consistent, document under another PO consistent (G-N1); G-N1: mistyped PO consistent (P-1210 and P-1201 differ by two swapped adjacent characters) |
| O | P-1301 SKU-O: ordered 24, received 24; invoices INV-O1 (12) and INV-O2 (144); SKU-O has 1 BOX = 12 EA | `QUANTITY_MISMATCH` | receipt not posted: can't be checked (156 invoiced > 24 ordered); unit mix-up: consistent for INV-O2 alone (144 / 12 = 12, and 12 + 12 = 24), though the totals 156 and 24 do not differ by 12 |
| P | P-1401 SKU-P: ordered 30, invoiced 30; receipts G-P1 (10) and G-P2 (10); SKU-P has 1 BOX = 12 EA | `QUANTITY_MISMATCH` | receipt not posted: consistent (30 is within the 30 ordered); unit mix-up: can't be checked (no total or single document fits, and a mix-up on both receipts cannot be ruled out) |
| Q | Units stated. P-1501 SKU-Q: 100 EA ordered and invoiced, no receipt; P-1502 (100 BOX, received 100 BOX) and P-1503 (100 EA, received 100 EA) for the same supplier and SKU; SKU-Q has 1 BOX = 10 EA | `MISSING_GOODS_RECEIPT` | receipt not posted consistent; document under another PO consistent with G-Q3 (100 EA) only: G-Q2 is 100 BOX and is not compared |
| R | P-1601 SKU-R: ordered 50, invoiced 50 (INV-R1), received 10; a second invoice row for the line (INV-R2) has an invalid quantity and is rejected; SKU-R has 1 BOX = 5 EA | `QUANTITY_MISMATCH` | receipt not posted and unit mix-up: can't be checked (an invoice row of this line was rejected, so its totals may be incomplete; without that rule both would wrongly read consistent) |

Only scenario Q states units of measure (the `unit_of_measure` column is empty elsewhere).

The engine's exceptions for these files, derived by hand: 24 in total, namely the ones named in the table
(A 1, B 1, C 2, D 2, E 1, F 2, G 3, H 2, J 1, K 1, L 1, M 1, N 2, O 1, P 1, Q 1, R 1), plus 2 validation
issues (goods_received line 13 and invoices line 24).
