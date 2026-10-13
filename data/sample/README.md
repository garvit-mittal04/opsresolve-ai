# Synthetic sample data

Entirely synthetic. Supplier names (Northgate Fasteners, Bramwell Packaging, Calder Industrial
Bearings, Delmore Hydraulics, Fenwick Electrical Supply) are fictional. Currency is USD unless noted.

Each PO below was seeded with a specific scenario. Expected results are what
`tests/test_end_to_end.py` asserts.

| PO | Scenario | Expected result |
|---|---|---|
| 4500010001 | Two lines, fully received and invoiced at PO price | none |
| 4500010002 | Box line received in two deliveries (700 + 500), one invoice for 1200 | none |
| 4500010003 | 400 invoiced, 380 received @ 6.75 | QUANTITY_MISMATCH 20 units, 135.00 |
| 4500010004 | Hose invoiced at 19.95 vs PO 18.40, 250 units | PRICE_MISMATCH 387.50 (fitting line clean) |
| 4500010005 | 60 cable reels invoiced @ 112.00, no receipt | MISSING_GOODS_RECEIPT 6,720.00 |
| 4500010006 | 10,000 washers received @ 0.02, no invoice | MISSING_INVOICE 200.00 (accrual) |
| 4500010007 | Invoice CIB-7781 re-keyed as CIB-07781 two weeks later (300 @ 9.20) | POSSIBLE_DUPLICATE_INVOICE 2,760.00 + QUANTITY_MISMATCH 300 units, 2,760.00 |
| 4500010008 | Same 80 rolls @ 24.50 billed on BPC-55120 and BPC-55187, 9 days apart; 80 received | POSSIBLE_DUPLICATE_INVOICE 1,960.00 + QUANTITY_MISMATCH 80 units, 1,960.00 |
| 4500010009 | 150 ordered, 165 received and invoiced @ 14.60 | QUANTITY_MISMATCH (over-receipt) 15 units, 219.00 |
| 4500010010 | Seal kits invoiced at 0.80 vs PO 0.85 (1000 units); GRN 5000050011 receipts SKU HYD-SEAL-X not on PO | PRICE_MISMATCH −50.00 (LOW); MISSING_PURCHASE_ORDER for the GRN |
| 4500010011 | Received 200; invoice row has quantity `twelve` | Validation issue (invoices line 15); MISSING_INVOICE 230.00 with rejected-row note |
| 4500010012 | PO row missing unit price; invoice (500 @ 2.95) and GRN reference it | Validation issue (purchase_orders line 16); 2 × MISSING_PURCHASE_ORDER (1,475.00 and 0) with rejected-row notes |
| 4500010013 | Invoiced 5000 @ 0.03; GRN row has date `2026-13-09` | Validation issue (goods_received line 18); MISSING_GOODS_RECEIPT 150.00 with rejected-row note |
| 4500010014 | PO in USD, invoice in EUR | CURRENCY_MISMATCH (no amount; no FX conversion) |
| 4500010015 | PO with SUP-001, invoice from SUP-009 | SUPPLIER_MISMATCH 180.00 (HIGH) |
| 4500010016 | Split billing: 2 × 300 invoiced 10 days apart, 2 × 300 received | none (identical invoices but not over-billed) |
| 4500010099 | Invoice DH-INV-30502 (2 pumps @ 845.00) references a PO that does not exist in the file | MISSING_PURCHASE_ORDER 1,690.00 |

Totals: 3 validation issues, 18 exceptions (8 HIGH, 7 MEDIUM, 3 LOW).
