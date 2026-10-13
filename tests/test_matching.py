from factories import grn, inv, po

from opsresolve.matching import match_records


def test_partial_receipts_and_split_invoices_group_under_one_po_line():
    result = match_records(
        [po(qty="100")],
        [inv("I1", qty="60"), inv("I2", qty="40")],
        [grn("G1", qty="70"), grn("G2", qty="30")],
    )
    [line] = result.matched
    assert line.total_invoiced == 100
    assert line.total_received == 100
    assert result.unmatched_invoices == [] and result.unmatched_receipts == []


def test_records_without_po_line_are_unmatched():
    result = match_records([po(sku="A")], [inv(sku="B")], [grn(po_number="OTHER", sku="A")])
    assert len(result.unmatched_invoices) == 1
    assert len(result.unmatched_receipts) == 1
    assert result.known_po_numbers == {"4500000001"}


def test_matching_is_by_po_and_sku_not_sku_alone():
    result = match_records([po(po_number="P1", sku="A"), po(po_number="P2", sku="A")],
                           [inv(po_number="P2", sku="A")], [])
    p1, p2 = result.matched
    assert p1.invoices == [] and len(p2.invoices) == 1
