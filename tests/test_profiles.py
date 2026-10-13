"""Stage 1 of import profiles: profile model, JSON loading, validation and strict value parsing.

Nothing here reads CSV files; ingestion does not use profiles yet.
"""

import json
from decimal import Decimal
from itertools import product

import pytest

from opsresolve import profiles
from opsresolve.ingestion import REQUIRED_COLUMNS
from opsresolve.profiles import (
    BUILT_IN_FORMAT,
    DATE_FORMATS,
    DECIMAL_SEPARATORS,
    DELIMITERS,
    ENCODINGS,
    FILE_MODELS,
    THOUSANDS_SEPARATORS,
    ProfileError,
    canonical_fields,
    date_fields,
    load_profile,
    numeric_fields,
    parse_date,
    parse_number,
    parse_profile,
    required_fields,
)


def make(**overrides) -> bytes:
    body = {"profile_version": 1, "name": "Test profile"}
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


def problems_of(data: bytes) -> list[str]:
    with pytest.raises(ProfileError) as err:
        parse_profile(data)
    return err.value.problems


def assert_problem(data: bytes, *fragments: str) -> None:
    problems = problems_of(data)
    assert any(all(f in p for f in fragments) for p in problems), problems


# ---- canonical fields come from the models ------------------------------------------

def test_file_labels_match_ingestion():
    assert tuple(FILE_MODELS) == tuple(REQUIRED_COLUMNS)


@pytest.mark.parametrize("label", list(FILE_MODELS))
def test_required_fields_match_ingestion(label):
    assert required_fields(label) == REQUIRED_COLUMNS[label]


def test_canonical_fields_exclude_internal_fields_and_include_optional_ones():
    assert canonical_fields("purchase_orders")[-2:] == ("order_date", "unit_of_measure")
    assert "description" in canonical_fields("purchase_orders")
    for label in FILE_MODELS:
        assert "source_row" not in canonical_fields(label)
        assert "converted_from" not in canonical_fields(label)


def test_numeric_and_date_fields_are_derived_from_model_types():
    assert numeric_fields("purchase_orders") == {"line_number", "quantity_ordered", "unit_price"}
    assert numeric_fields("invoices") == {"quantity_invoiced", "unit_price"}
    assert numeric_fields("goods_received") == {"quantity_received"}
    assert numeric_fields("uom_conversions") == {"units_per_uom"}
    assert date_fields("purchase_orders") == {"order_date"}
    assert date_fields("invoices") == {"invoice_date"}
    assert date_fields("goods_received") == {"received_date"}
    assert date_fields("uom_conversions") == frozenset()


# ---- valid profiles --------------------------------------------------------------------

EXAMPLE = {
    "profile_version": 1,
    "name": "Example ERP export (EU number format)",
    "defaults": {"delimiter": ";", "encoding": "cp1252", "date_format": "DD.MM.YYYY",
                 "decimal_separator": ",", "thousands_separator": "."},
    "files": {
        "purchase_orders": {"columns": {
            "po_number": "Bestellnummer", "line_number": "Position", "supplier_id": "Lieferant",
            "supplier_name": "Lieferantenname", "sku": "Artikel", "description": "Beschreibung",
            "quantity_ordered": "Menge", "unit_price": "Preis", "currency": "Währung",
            "order_date": "Bestelldatum", "unit_of_measure": "Einheit"}},
        # A comma-delimited file cannot also use ',' for thousands (rule: separators must differ
        # from the delimiter), so this file declares no thousands separator.
        "invoices": {"delimiter": ",", "encoding": "utf-8", "date_format": "YYYY-MM-DD",
                     "decimal_separator": ".", "thousands_separator": "",
                     "columns": {"invoice_number": "Invoice Number", "invoice_date": "Invoice Date",
                                 "supplier_id": "Supplier ID", "po_number": "PO Number", "sku": "Item Code",
                                 "quantity_invoiced": "Qty Billed", "unit_price": "Unit Price",
                                 "currency": "Currency", "unit_of_measure": "UoM"}},
        "goods_received": {"columns": {"grn_number": "Wareneingang", "received_date": "Eingangsdatum",
                                       "supplier_id": "Lieferant", "po_number": "Bestellnummer",
                                       "sku": "Artikel", "quantity_received": "Menge",
                                       "unit_of_measure": "Einheit"}},
    },
}


def test_example_profile_from_the_design_loads_and_resolves():
    profile = parse_profile(json.dumps(EXAMPLE, ensure_ascii=False).encode("utf-8"))
    assert profile.name == "Example ERP export (EU number format)"
    po = profile.format_for("purchase_orders")
    assert (po.delimiter, po.encoding, po.codec, po.date_format, po.decimal_separator, po.thousands_separator) == (
        ";", "cp1252", "cp1252", "DD.MM.YYYY", ",", ".")
    assert po.source_header("currency") == "Währung"
    inv = profile.format_for("invoices")  # per-file overrides replace the defaults
    assert (inv.delimiter, inv.encoding, inv.date_format, inv.decimal_separator, inv.thousands_separator) == (
        ",", "utf-8", "YYYY-MM-DD", ".", "")
    uom = profile.format_for("uom_conversions")  # no section: defaults and canonical headers
    assert (uom.delimiter, uom.decimal_separator, dict(uom.columns)) == (";", ",", {})
    assert uom.source_header("units_per_uom") == "units_per_uom"


def test_minimal_profile_uses_todays_format_everywhere():
    profile = parse_profile(make())
    for label in FILE_MODELS:
        fmt = profile.format_for(label)
        assert {k: getattr(fmt, k) for k in BUILT_IN_FORMAT} == dict(BUILT_IN_FORMAT)
        assert dict(fmt.columns) == {}


def test_partial_mapping_falls_back_to_canonical_names():
    profile = parse_profile(make(files={"invoices": {"columns": {"invoice_number": "Invoice Number"}}}))
    fmt = profile.format_for("invoices")
    assert fmt.source_header("invoice_number") == "Invoice Number"
    assert fmt.source_header("sku") == "sku"


def test_utf8_byte_order_mark_and_whitespace_in_profile_are_accepted():
    profile = parse_profile(b"\xef\xbb\xbf" + make(name="  Spaced name  "))
    assert profile.name == "Spaced name"


def test_profile_is_immutable_and_fingerprinted():
    data = make()
    profile = parse_profile(data)
    import hashlib
    assert profile.sha256 == hashlib.sha256(data).hexdigest()
    with pytest.raises(TypeError):
        profile.formats["invoices"] = None  # type: ignore[index]
    with pytest.raises(Exception):
        profile.format_for("invoices").delimiter = ";"  # type: ignore[misc]


def test_load_profile_from_file(tmp_path):
    path = tmp_path / "p.json"
    path.write_bytes(make())
    assert load_profile(path).name == "Test profile"


@pytest.mark.parametrize("delimiter", DELIMITERS)
def test_every_supported_delimiter(delimiter):
    decimal, thousands = (".", "") if delimiter != "." else (",", "")
    profile = parse_profile(make(defaults={"delimiter": delimiter, "decimal_separator": decimal,
                                           "thousands_separator": thousands}))
    assert profile.format_for("invoices").delimiter == delimiter


@pytest.mark.parametrize("name, expected", [
    ("utf-8", "utf-8"), ("UTF8", "utf-8"), ("utf_8", "utf-8"),
    ("cp1252", "cp1252"), ("windows-1252", "cp1252"),
    ("iso-8859-1", "iso-8859-1"), ("latin-1", "iso-8859-1"), ("ISO8859-1", "iso-8859-1"),
])
def test_supported_encodings_and_aliases(name, expected):
    assert parse_profile(make(defaults={"encoding": name})).format_for("invoices").encoding == expected


@pytest.mark.parametrize("date_format", list(DATE_FORMATS))
def test_every_supported_date_format(date_format):
    assert parse_profile(make(defaults={"date_format": date_format})).format_for("invoices").date_format == date_format


SEPARATOR_COMBINATIONS = [(d, t) for d, t in product(DECIMAL_SEPARATORS, THOUSANDS_SEPARATORS) if d != t]


@pytest.mark.parametrize("decimal, thousands", SEPARATOR_COMBINATIONS)
def test_every_valid_separator_combination(decimal, thousands):
    delimiter = ";" if "," in (decimal, thousands) else ","
    fmt = parse_profile(make(defaults={"delimiter": delimiter, "decimal_separator": decimal,
                                       "thousands_separator": thousands})).format_for("invoices")
    assert (fmt.decimal_separator, fmt.thousands_separator) == (decimal, thousands)


# ---- malformed profiles -----------------------------------------------------------------

@pytest.mark.parametrize("data, fragment", [
    (b"", "not valid JSON"),
    (b"{", "not valid JSON"),
    (b"{'profile_version': 1}", "not valid JSON"),
    (b"[]", "must be a JSON object"),
    (b"\"text\"", "must be a JSON object"),
    (b"\xff\xfe{}", "not UTF-8"),
    (b'{"profile_version": 1, "name": "x", "defaults": {"x": NaN}}', "NaN is not allowed"),
])
def test_malformed_profile_files(data, fragment):
    assert_problem(data, fragment)


def test_oversized_profile_is_rejected(tmp_path):
    big = make(name="x" * 10).replace(b"}", b',"padding":"' + b" " * profiles.MAX_PROFILE_BYTES + b'"}')
    assert_problem(big, "the limit is")
    path = tmp_path / "big.json"
    path.write_bytes(big)
    with pytest.raises(ProfileError, match="the limit is"):
        load_profile(path)


def test_missing_profile_file_is_a_profile_error(tmp_path):
    with pytest.raises(ProfileError, match="cannot be read"):
        load_profile(tmp_path / "missing.json")


@pytest.mark.parametrize("data, location", [
    (b'{"profile_version": 1, "profile_version": 1, "name": "x"}', "profile: duplicate key 'profile_version'"),
    (b'{"profile_version": 1, "name": "x", "defaults": {"delimiter": ",", "delimiter": ";"}}',
     "defaults: duplicate key 'delimiter'"),
    (b'{"profile_version": 1, "name": "x", "files": {"invoices": {}, "invoices": {}}}',
     "files: duplicate key 'invoices'"),
    (b'{"profile_version": 1, "name": "x", "files": {"invoices": {"columns": '
     b'{"sku": "Item", "sku": "Item Code"}}}}', "files.invoices.columns: duplicate key 'sku'"),
])
def test_duplicate_keys_are_rejected_at_every_level(data, location):
    assert location in problems_of(data)


@pytest.mark.parametrize("body, fragments", [
    ({"name": "x"}, ("missing required key 'profile_version'",)),
    ({"profile_version": 1}, ("missing required key 'name'",)),
    ({"profile_version": 2, "name": "x"}, ("profile_version: must be 1",)),
    ({"profile_version": "1", "name": "x"}, ("profile_version: must be 1",)),
    ({"profile_version": True, "name": "x"}, ("profile_version: must be 1",)),
    ({"profile_version": 1, "name": "  "}, ("name: must be a non-empty string",)),
    ({"profile_version": 1, "name": 5}, ("name: must be a non-empty string",)),
    ({"profile_version": 1, "name": "x" * 101}, ("at most 100 characters",)),
    ({"profile_version": 1, "name": "bad\nname"}, ("name: must not contain control characters",)),
    ({"profile_version": 1, "name": "x", "extra": 1}, ("profile: unknown key 'extra'",)),
    ({"profile_version": 1, "name": "x", "defaults": []}, ("defaults: must be a JSON object",)),
    ({"profile_version": 1, "name": "x", "defaults": {"delimeter": ";"}}, ("defaults: unknown key 'delimeter'",)),
    ({"profile_version": 1, "name": "x", "defaults": {"columns": {}}}, ("defaults: unknown key 'columns'",)),
    ({"profile_version": 1, "name": "x", "files": []}, ("files: must be a JSON object",)),
    ({"profile_version": 1, "name": "x", "files": {"payments": {}}}, ("files: unknown key 'payments'",)),
    ({"profile_version": 1, "name": "x", "files": {"invoices": "csv"}}, ("files.invoices: must be a JSON object",)),
    ({"profile_version": 1, "name": "x", "files": {"invoices": {"sep": ";"}}},
     ("files.invoices: unknown key 'sep'",)),
])
def test_malformed_structure(body, fragments):
    assert_problem(json.dumps(body).encode(), *fragments)


def test_all_problems_are_reported_together():
    problems = problems_of(make(profile_version=3, defaults={"delimiter": ":", "encoding": "nope"},
                                files={"invoices": {"columns": {"invoice_no": "No."}}}))
    assert len(problems) == 4
    assert str(ProfileError(problems)).count("\n  - ") == 4


@pytest.mark.parametrize("key, value, fragment", [
    ("delimiter", ":", "':' is not supported"),
    ("delimiter", ",,", "',,' is not supported"),
    ("delimiter", 1, "must be a string"),
    ("encoding", "rot13", "'rot13' is not supported"),
    ("encoding", "zlib", "'zlib' is not supported"),
    ("encoding", "utf-16", "'utf-16' is not supported"),
    ("encoding", "ascii", "'ascii' is not supported"),
    ("encoding", "no-such-encoding", "'no-such-encoding' is not supported"),
    ("date_format", "%d.%m.%Y", "'%d.%m.%Y' is not supported"),
    ("date_format", "DD.MM.YY", "'DD.MM.YY' is not supported"),
    ("date_format", "dd.mm.yyyy", "'dd.mm.yyyy' is not supported"),
    ("decimal_separator", "", "'' is not supported"),
    ("decimal_separator", "·", "'·' is not supported"),
    ("thousands_separator", "_", "'_' is not supported"),
    ("thousands_separator", None, "must be a string"),
])
def test_unsupported_format_values(key, value, fragment):
    assert_problem(make(defaults={key: value}), f"defaults.{key}", fragment)


@pytest.mark.parametrize("settings, fragment", [
    ({"decimal_separator": ".", "thousands_separator": "."}, "must differ"),
    ({"decimal_separator": ",", "thousands_separator": ","}, "must differ"),
    ({"delimiter": ",", "decimal_separator": ","}, "must not also be a decimal or thousands separator"),
    ({"delimiter": ",", "thousands_separator": ","}, "must not also be a decimal or thousands separator"),
])
def test_invalid_separator_combinations_in_defaults(settings, fragment):
    assert_problem(make(defaults=settings), "defaults:", fragment)


def test_separator_conflicts_created_by_a_file_override_are_reported_on_that_file():
    data = make(defaults={"delimiter": ";", "decimal_separator": ","},
                files={"invoices": {"delimiter": ","}})
    problems = problems_of(data)
    assert problems == ["files.invoices: the delimiter ',' must not also be a decimal or thousands separator"]


def test_conflicting_defaults_are_reported_once():
    problems = problems_of(make(defaults={"decimal_separator": ".", "thousands_separator": "."}))
    assert problems == ["defaults: decimal_separator and thousands_separator must differ (both '.')"]


@pytest.mark.parametrize("columns, fragment", [
    ({"invoice_no": "Invoice Number"}, "'invoice_no' is not a field of invoices"),
    ({"quantity_ordered": "Qty"}, "'quantity_ordered' is not a field of invoices"),
    ({"source_row": "Line"}, "'source_row' is set by the program and cannot be mapped"),
    ({"converted_from": "Original"}, "'converted_from' is set by the program and cannot be mapped"),
    ({"sku": ""}, "files.invoices.columns.sku: must be a non-empty column header"),
    ({"sku": "   "}, "files.invoices.columns.sku: must be a non-empty column header"),
    ({"sku": 7}, "files.invoices.columns.sku: must be a non-empty column header"),
    ({"sku": "Item\tCode"}, "files.invoices.columns.sku: must not contain control characters"),
    ({"sku": "Item", "po_number": "item"}, "'po_number' and 'sku' would both read the column 'Item'"),
    ({"sku": "po_number"}, "would both read the column 'po_number'"),
])
def test_invalid_column_mappings(columns, fragment):
    assert_problem(make(files={"invoices": {"columns": columns}}), fragment)


def test_columns_must_be_an_object():
    assert_problem(make(files={"invoices": {"columns": ["sku"]}}), "files.invoices.columns: must be a JSON object")


def test_allowed_fields_are_listed_in_the_error():
    problems = problems_of(make(files={"goods_received": {"columns": {"grn": "GRN"}}}))
    assert problems == ["files.goods_received.columns: 'grn' is not a field of goods_received "
                        "(allowed: grn_number, received_date, supplier_id, po_number, sku, "
                        "quantity_received, unit_of_measure)"]


# ---- strict number parsing ------------------------------------------------------------------

@pytest.mark.parametrize("text, decimal, thousands, expected", [
    ("1234.50", ".", "", "1234.50"),
    ("1234", ".", "", "1234"),
    ("-5", ".", "", "-5"),
    ("+5.0", ".", "", "+5.0"),
    ("007", ".", "", "007"),
    ("1,234.50", ".", ",", "1234.50"),
    ("1,234,567", ".", ",", "1234567"),
    ("1234.5", ".", ",", "1234.5"),  # grouping is optional when declared
    ("1.234,50", ",", ".", "1234.50"),
    ("1234,5", ",", ".", "1234.5"),
    ("12,5", ",", "", "12.5"),
    ("1 234,50", ",", " ", "1234.50"),
    ("1'234.50", ".", "'", "1234.50"),
    ("  42  ", ".", "", "42"),
])
def test_numbers_in_declared_formats(text, decimal, thousands, expected):
    assert parse_number(text, decimal, thousands) == expected
    Decimal(expected)  # canonical form is accepted by Decimal (and so by the existing models)


@pytest.mark.parametrize("decimal, thousands", SEPARATOR_COMBINATIONS)
def test_every_separator_combination_round_trips(decimal, thousands):
    written = "1" + (thousands + "234" if thousands else "234") + decimal + "56"
    assert Decimal(parse_number(written, decimal, thousands)) == Decimal("1234.56")


@pytest.mark.parametrize("text, decimal, thousands", [
    ("$5.00", ".", ""),            # currency symbol
    ("5.00 EUR", ".", ""),
    ("€5,00", ",", "."),
    ("1e3", ".", ""),              # exponent
    ("1E3", ".", ""),
    ("ten", ".", ""),
    ("", ".", ""),
    ("   ", ".", ""),
    (".5", ".", ""),               # bare decimal separator
    ("5.", ".", ""),
    (",5", ",", "."),
    ("1,000", ".", ""),            # grouping without a declared thousands separator
    ("1.000,00", ".", ","),        # the other convention
    ("1,234.50", ",", "."),
    ("1.23.4", ",", "."),          # misplaced groups
    ("12,34", ".", ","),
    ("1,2345", ".", ","),
    (",123", ".", ","),
    ("1,234,", ".", ","),
    ("1.2.3", ".", ""),
    ("5-", ".", ""),               # trailing sign
    ("(5)", ".", ""),              # bracketed negative
    ("--5", ".", ""),
    ("+-5", ".", ""),
    ("1 234", ".", ""),            # space only allowed when it is the declared separator
    ("1  234", ",", " "),
    ("١٢٣", ".", ""),              # non-ASCII digits
    ("NaN", ".", ""),
    ("Infinity", ".", ""),
    ("0x10", ".", ""),
])
def test_malformed_or_ambiguous_numbers_are_rejected(text, decimal, thousands):
    with pytest.raises(ValueError, match="is not a number in the declared format"):
        parse_number(text, decimal, thousands)


def test_number_error_message_states_the_declared_format():
    with pytest.raises(ValueError) as err:
        parse_number("1.000,5", ".", ",")
    assert str(err.value) == ("'1.000,5' is not a number in the declared format "
                              "(decimal separator '.', thousands separator ',')")
    with pytest.raises(ValueError, match="thousands separator none"):
        parse_number("1,000", ".", "")


@pytest.mark.parametrize("decimal, thousands", [(".", "."), (";", ""), (".", "_")])
def test_parse_number_rejects_an_invalid_declared_format(decimal, thousands):
    with pytest.raises(ValueError, match="invalid number format"):
        parse_number("1", decimal, thousands)


# ---- strict date parsing ----------------------------------------------------------------------

@pytest.mark.parametrize("text, date_format", [
    ("2026-03-01", "YYYY-MM-DD"),
    ("01.03.2026", "DD.MM.YYYY"),
    ("01/03/2026", "DD/MM/YYYY"),
    ("03/01/2026", "MM/DD/YYYY"),
    ("20260301", "YYYYMMDD"),
    (" 01.03.2026 ", "DD.MM.YYYY"),
])
def test_dates_in_every_declared_format(text, date_format):
    assert parse_date(text, date_format) == "2026-03-01"


def test_day_and_month_order_follows_the_declaration_only():
    assert parse_date("04/05/2026", "DD/MM/YYYY") == "2026-05-04"
    assert parse_date("04/05/2026", "MM/DD/YYYY") == "2026-04-05"


@pytest.mark.parametrize("text, date_format", [
    ("2026-03-01", "DD.MM.YYYY"),   # another layout is never tried
    ("01.03.2026", "YYYY-MM-DD"),
    ("1.3.2026", "DD.MM.YYYY"),     # not zero-padded
    ("01.03.26", "DD.MM.YYYY"),     # two-digit year
    ("01-03-2026", "DD.MM.YYYY"),
    ("2026/03/01", "YYYY-MM-DD"),
    ("2026-03-01T00:00", "YYYY-MM-DD"),
    ("2026031", "YYYYMMDD"),
    ("", "YYYY-MM-DD"),
    ("٢٠٢٦-٠٣-٠١", "YYYY-MM-DD"),  # non-ASCII digits
])
def test_dates_not_in_the_declared_format_are_rejected(text, date_format):
    with pytest.raises(ValueError, match="does not match the declared date format"):
        parse_date(text, date_format)


@pytest.mark.parametrize("text, date_format", [
    ("31.02.2026", "DD.MM.YYYY"),
    ("29.02.2025", "DD.MM.YYYY"),
    ("00.01.2026", "DD.MM.YYYY"),
    ("01.13.2026", "DD.MM.YYYY"),
    ("13/01/2026", "MM/DD/YYYY"),
    ("2026-00-10", "YYYY-MM-DD"),
])
def test_impossible_calendar_dates_are_rejected(text, date_format):
    with pytest.raises(ValueError, match="is not a valid calendar date"):
        parse_date(text, date_format)


def test_leap_day_is_accepted():
    assert parse_date("29.02.2028", "DD.MM.YYYY") == "2028-02-29"


def test_parse_date_rejects_an_unsupported_format():
    with pytest.raises(ValueError, match="unsupported date format"):
        parse_date("2026-03-01", "%Y-%m-%d")


# ---- encodings ----------------------------------------------------------------------------------

def test_encoding_allowlist_is_text_codecs_only():
    import codecs
    for display, codec in ENCODINGS.items():
        assert codecs.lookup(display).name == codec
        assert "é".encode(codec).decode(codec) == "é"
