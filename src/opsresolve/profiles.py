"""Import profiles: explicitly declared CSV formats for real-world exports.

A profile is a JSON file that states, for each input file, the delimiter,
text encoding, date format, decimal and thousands separators, and how source
column headers map onto the canonical fields of the existing models. Nothing
is guessed: every value must match the declared format exactly or it is
rejected.

This module only loads and validates profiles and parses individual values.
It reads no CSV files and contains no business rules. Canonical field names
are taken from the existing models, never written out by hand.

Example::

    {
      "profile_version": 1,
      "name": "Example ERP export",
      "defaults": {"delimiter": ";", "encoding": "cp1252", "date_format": "DD.MM.YYYY",
                   "decimal_separator": ",", "thousands_separator": "."},
      "files": {"invoices": {"columns": {"invoice_number": "Invoice Number"}}}
    }
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel

from opsresolve.models import GoodsReceiptLine, InvoiceLine, PurchaseOrderLine, UomConversion

PROFILE_VERSION = 1
MAX_PROFILE_BYTES = 64 * 1024
MAX_NAME_LENGTH = 100

# The file labels used by ingestion (tests check they match ingestion's labels).
FILE_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType({
    "purchase_orders": PurchaseOrderLine,
    "invoices": InvoiceLine,
    "goods_received": GoodsReceiptLine,
    "uom_conversions": UomConversion,
})

# Model fields filled in by the program, never read from an input file.
INTERNAL_FIELDS = frozenset({"source_row", "converted_from"})

DELIMITERS = (",", ";", "\t", "|")
# Display name -> codec name as reported by codecs.lookup().
ENCODINGS = MappingProxyType({"utf-8": "utf-8", "cp1252": "cp1252", "iso-8859-1": "iso8859-1"})
DECIMAL_SEPARATORS = (".", ",")
THOUSANDS_SEPARATORS = ("", ",", ".", " ", "'")
# Zero-padded day and month, four-digit year; groups are captured as (year, month, day) by name.
DATE_FORMATS = MappingProxyType({
    "YYYY-MM-DD": r"(?P<y>[0-9]{4})-(?P<m>[0-9]{2})-(?P<d>[0-9]{2})",
    "DD.MM.YYYY": r"(?P<d>[0-9]{2})\.(?P<m>[0-9]{2})\.(?P<y>[0-9]{4})",
    "DD/MM/YYYY": r"(?P<d>[0-9]{2})/(?P<m>[0-9]{2})/(?P<y>[0-9]{4})",
    "MM/DD/YYYY": r"(?P<m>[0-9]{2})/(?P<d>[0-9]{2})/(?P<y>[0-9]{4})",
    "YYYYMMDD": r"(?P<y>[0-9]{4})(?P<m>[0-9]{2})(?P<d>[0-9]{2})",
})

FORMAT_KEYS = ("delimiter", "encoding", "date_format", "decimal_separator", "thousands_separator")
_TOP_LEVEL_KEYS = ("profile_version", "name", "defaults", "files")
_FILE_KEYS = FORMAT_KEYS + ("columns",)

# The settings used when a profile does not declare them: exactly today's CSV format.
BUILT_IN_FORMAT = MappingProxyType({
    "delimiter": ",", "encoding": "utf-8", "date_format": "YYYY-MM-DD",
    "decimal_separator": ".", "thousands_separator": "",
})


class ProfileError(ValueError):
    """The profile is not valid. ``problems`` lists every problem found, each as 'location: message'."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("Invalid import profile:\n" + "\n".join(f"  - {p}" for p in self.problems))


# --------------------------------------------------------------------------- #
# Canonical fields (derived from the models)
# --------------------------------------------------------------------------- #

def canonical_fields(label: str) -> tuple[str, ...]:
    """Fields an input file of this type may provide, in model order."""
    return tuple(name for name in FILE_MODELS[label].model_fields if name not in INTERNAL_FIELDS)


def required_fields(label: str) -> tuple[str, ...]:
    fields = FILE_MODELS[label].model_fields
    return tuple(name for name in canonical_fields(label) if fields[name].is_required())


def _fields_of_type(label: str, types: tuple[type, ...]) -> frozenset[str]:
    fields = FILE_MODELS[label].model_fields
    return frozenset(name for name in canonical_fields(label) if fields[name].annotation in types)


def numeric_fields(label: str) -> frozenset[str]:
    """Fields parsed with the declared decimal and thousands separators."""
    return _fields_of_type(label, (Decimal, int))


def date_fields(label: str) -> frozenset[str]:
    """Fields parsed with the declared date format."""
    return _fields_of_type(label, (date,))


# --------------------------------------------------------------------------- #
# Resolved formats
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FileFormat:
    """The fully resolved format of one input file."""

    label: str
    delimiter: str
    encoding: str  # display name, a key of ENCODINGS
    date_format: str  # a key of DATE_FORMATS
    decimal_separator: str
    thousands_separator: str
    columns: Mapping[str, str]  # canonical field -> source header, only for fields the profile maps

    def source_header(self, field: str) -> str:
        """The header expected in the file for a canonical field (mapped, or the canonical name)."""
        return self.columns.get(field, field)

    @property
    def codec(self) -> str:
        return ENCODINGS[self.encoding]


@dataclass(frozen=True)
class ImportProfile:
    name: str
    formats: Mapping[str, FileFormat]  # every file label -> its resolved format
    sha256: str  # of the profile file's bytes, for reports and stale-result checks

    def format_for(self, label: str) -> FileFormat:
        return self.formats[label]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

class _Pairs(list):
    """A JSON object kept as its ordered key/value pairs, so duplicate keys are not lost."""


def _reject_constant(name: str):
    raise ValueError(f"{name} is not allowed in a profile")


def load_profile(path: str | Path) -> ImportProfile:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ProfileError([f"profile file: cannot be read ({exc.strerror or exc})"]) from None
    if size > MAX_PROFILE_BYTES:
        raise ProfileError([f"profile file: is {size} bytes; the limit is {MAX_PROFILE_BYTES}"])
    return parse_profile(path.read_bytes())


def parse_profile(data: bytes) -> ImportProfile:
    """Parse and validate a profile. Raises ProfileError listing every problem found."""
    if len(data) > MAX_PROFILE_BYTES:
        raise ProfileError([f"profile file: is {len(data)} bytes; the limit is {MAX_PROFILE_BYTES}"])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProfileError([f"profile file: is not UTF-8 text (byte {exc.start})"]) from None
    try:
        raw = json.loads(text, object_pairs_hook=_Pairs, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ProfileError([f"profile file: is not valid JSON ({exc.msg} at line {exc.lineno}, "
                            f"column {exc.colno})"]) from None
    except ValueError as exc:
        raise ProfileError([f"profile file: {exc}"]) from None

    problems: list[str] = []
    root = _to_object(raw, "profile", problems)
    if root is None:
        raise ProfileError(problems or ["profile: must be a JSON object"])

    _check_keys(root, _TOP_LEVEL_KEYS, "profile", problems)
    name = _check_name(root, problems)
    _check_version(root, problems)

    defaults_raw = _to_object(root.get("defaults", _Pairs()), "defaults", problems) or {}
    _check_keys(defaults_raw, FORMAT_KEYS, "defaults", problems)
    defaults = _check_format_settings(defaults_raw, "defaults", FORMAT_KEYS, problems)
    base = {**BUILT_IN_FORMAT, **defaults}
    _check_separators(base, "defaults", problems)

    files_raw = _to_object(root.get("files", _Pairs()), "files", problems) or {}
    _check_keys(files_raw, tuple(FILE_MODELS), "files", problems)

    formats: dict[str, FileFormat] = {}
    for label in FILE_MODELS:
        where = f"files.{label}"
        section = _to_object(files_raw[label], where, problems) if label in files_raw else {}
        section = section or {}
        _check_keys(section, _FILE_KEYS, where, problems)
        settings = {**base, **_check_format_settings(section, where, FORMAT_KEYS, problems)}
        if label in files_raw:
            _check_separators(settings, where, problems)
        columns = _check_columns(label, section.get("columns"), f"{where}.columns", problems)
        formats[label] = FileFormat(label=label, columns=MappingProxyType(columns), **settings)

    if problems:
        raise ProfileError(problems)
    return ImportProfile(name=name, formats=MappingProxyType(formats), sha256=hashlib.sha256(data).hexdigest())


def _to_object(value: Any, where: str, problems: list[str]) -> dict[str, Any] | None:
    """Convert a parsed JSON object to a dict, reporting duplicate keys (at any depth)."""
    if not isinstance(value, _Pairs):
        problems.append(f"{where}: must be a JSON object")
        return None
    result: dict[str, Any] = {}
    for key, item in value:
        if key in result:
            problems.append(f"{where}: duplicate key {key!r}")
            continue
        result[key] = item
    return result


def _check_keys(obj: Mapping[str, Any], allowed: tuple[str, ...], where: str, problems: list[str]) -> None:
    for key in obj:
        if key not in allowed:
            problems.append(f"{where}: unknown key {key!r} (allowed: {', '.join(allowed)})")


def _check_name(root: Mapping[str, Any], problems: list[str]) -> str:
    if "name" not in root:
        problems.append("profile: missing required key 'name'")
        return ""
    name = root["name"]
    if not isinstance(name, str) or not name.strip():
        problems.append("name: must be a non-empty string")
        return ""
    name = name.strip()
    if len(name) > MAX_NAME_LENGTH:
        problems.append(f"name: must be at most {MAX_NAME_LENGTH} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        problems.append("name: must not contain control characters")
    return name


def _check_version(root: Mapping[str, Any], problems: list[str]) -> None:
    if "profile_version" not in root:
        problems.append("profile: missing required key 'profile_version'")
        return
    version = root["profile_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != PROFILE_VERSION:
        problems.append(f"profile_version: must be {PROFILE_VERSION}")


def _describe(values) -> str:
    return ", ".join(repr(v) for v in values)


def _check_format_settings(section: Mapping[str, Any], where: str, keys: tuple[str, ...],
                           problems: list[str]) -> dict[str, str]:
    settings: dict[str, str] = {}
    for key in keys:
        if key not in section:
            continue
        value = section[key]
        path = f"{where}.{key}"
        if not isinstance(value, str):
            problems.append(f"{path}: must be a string")
            continue
        if key == "delimiter":
            if value not in DELIMITERS:
                problems.append(f"{path}: {value!r} is not supported (use one of {_describe(DELIMITERS)})")
                continue
        elif key == "encoding":
            canonical = _encoding_name(value)
            if canonical is None:
                problems.append(f"{path}: {value!r} is not supported (use one of {_describe(ENCODINGS)})")
                continue
            value = canonical
        elif key == "date_format":
            if value not in DATE_FORMATS:
                problems.append(f"{path}: {value!r} is not supported (use one of {_describe(DATE_FORMATS)})")
                continue
        elif key == "decimal_separator":
            if value not in DECIMAL_SEPARATORS:
                problems.append(f"{path}: {value!r} is not supported (use one of {_describe(DECIMAL_SEPARATORS)})")
                continue
        elif key == "thousands_separator":
            if value not in THOUSANDS_SEPARATORS:
                problems.append(f"{path}: {value!r} is not supported "
                                f"(use one of {_describe(THOUSANDS_SEPARATORS)}; '' means none)")
                continue
        settings[key] = value
    return settings


def _encoding_name(value: str) -> str | None:
    """The supported display name for an encoding name or alias, or None."""
    try:
        codec = codecs.lookup(value).name
    except LookupError:
        return None
    for display, name in ENCODINGS.items():
        if codec == name:
            return display
    return None


def _check_separators(settings: Mapping[str, str], where: str, problems: list[str]) -> None:
    decimal, thousands, delimiter = (settings["decimal_separator"], settings["thousands_separator"],
                                     settings["delimiter"])
    if decimal == thousands:
        problems.append(f"{where}: decimal_separator and thousands_separator must differ (both {decimal!r})")
    if delimiter in (decimal, thousands):
        problems.append(f"{where}: the delimiter {delimiter!r} must not also be a decimal or thousands separator")


def _check_columns(label: str, value: Any, where: str, problems: list[str]) -> dict[str, str]:
    if value is None:
        return {}
    mapping = _to_object(value, where, problems)
    if mapping is None:
        return {}
    allowed = canonical_fields(label)
    columns: dict[str, str] = {}
    for field, header in mapping.items():
        if field in INTERNAL_FIELDS:
            problems.append(f"{where}: {field!r} is set by the program and cannot be mapped")
            continue
        if field not in allowed:
            problems.append(f"{where}: {field!r} is not a field of {label} (allowed: {', '.join(allowed)})")
            continue
        if not isinstance(header, str) or not header.strip():
            problems.append(f"{where}.{field}: must be a non-empty column header")
            continue
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in header):
            problems.append(f"{where}.{field}: must not contain control characters")
            continue
        columns[field] = header.strip()

    # Every canonical field reads one column: its mapped header, or its own name when unmapped.
    # Two fields reading the same column (compared case-insensitively) would be ambiguous.
    seen: dict[str, str] = {}
    for field in allowed:
        header = columns.get(field, field)
        key = header.casefold()
        if key in seen:
            problems.append(f"{where}: {seen[key]!r} and {field!r} would both read the column {header!r}")
        else:
            seen[key] = field
    return columns


# --------------------------------------------------------------------------- #
# Strict value parsing
# --------------------------------------------------------------------------- #

MAX_PREVIEW_CHARS = 40


def preview(text: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    """A quoted value for an error message, shortened so a huge cell cannot flood the report."""
    if len(text) <= limit:
        return repr(text)
    return f"{text[:limit]!r}... ({len(text)} characters)"


def _number_pattern(decimal_separator: str, thousands_separator: str) -> re.Pattern[str]:
    if thousands_separator:
        grouped = rf"[0-9]{{1,3}}(?:{re.escape(thousands_separator)}[0-9]{{3}})+"
        integer = rf"(?:{grouped}|[0-9]+)"
    else:
        integer = r"[0-9]+"
    return re.compile(rf"(?P<sign>[+-]?)(?P<int>{integer})(?:{re.escape(decimal_separator)}(?P<frac>[0-9]+))?")


def parse_number(text: str, decimal_separator: str = ".", thousands_separator: str = "") -> str:
    """Convert a number written in the declared format to canonical form (e.g. '-1234.50').

    Accepts an optional leading sign, ASCII digits, correctly placed thousands separators
    (groups of exactly three) and an optional decimal part. Everything else is rejected:
    currency symbols, letters, exponents, misplaced separators, a bare decimal separator,
    trailing signs and bracketed negatives. Raises ValueError with an actionable message.
    """
    if decimal_separator not in DECIMAL_SEPARATORS or thousands_separator not in THOUSANDS_SEPARATORS \
            or decimal_separator == thousands_separator:
        raise ValueError("invalid number format: decimal and thousands separators must be supported and differ")
    value = text.strip()
    match = _number_pattern(decimal_separator, thousands_separator).fullmatch(value)
    if match is None:
        thousands = repr(thousands_separator) if thousands_separator else "none"
        raise ValueError(f"{preview(text)} is not a number in the declared format "
                         f"(decimal separator {decimal_separator!r}, thousands separator {thousands})")
    digits = match["int"].replace(thousands_separator, "") if thousands_separator else match["int"]
    return f"{match['sign']}{digits}" + (f".{match['frac']}" if match["frac"] is not None else "")


def parse_date(text: str, date_format: str = "YYYY-MM-DD") -> str:
    """Convert a date written in the declared format to ISO form (YYYY-MM-DD).

    Requires a zero-padded day and month and a four-digit year, and a real calendar
    date. No other layout is tried. Raises ValueError with an actionable message.
    """
    if date_format not in DATE_FORMATS:
        raise ValueError(f"unsupported date format {date_format!r}")
    value = text.strip()
    match = re.fullmatch(DATE_FORMATS[date_format], value)
    if match is None:
        raise ValueError(f"{preview(text)} does not match the declared date format {date_format}")
    try:
        return date(int(match["y"]), int(match["m"]), int(match["d"])).isoformat()
    except ValueError:
        raise ValueError(f"{preview(text)} is not a valid calendar date (format {date_format})") from None
