"""Plain-Python helpers behind the local web app (``app.py``).

Nothing here imports Streamlit or implements business rules. The helpers
describe the expected inputs, hand uploaded bytes to the unchanged pipeline,
count what the pipeline returned, and build download files.

Safety properties:

* Uploaded filenames are never used as paths. Bytes are written to fixed names
  inside a fresh temporary directory, which is deleted when the run ends,
  including when the run raises.
* Results live only in memory; nothing is written outside that temporary
  directory.
* CSV downloads neutralise spreadsheet formulas (see ``neutralize_cell``). The
  CLI's report files are not affected.
* An uploaded import profile is parsed from memory and never written to disk.
  Its name comes from the user, so it is shown escaped (see ``profile_caption``).
"""

from __future__ import annotations

import hashlib
import io
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Mapping

import pandas as pd

from opsresolve.config import MatchConfig
from opsresolve.ingestion import GOODS_RECEIVED, INVOICES, PURCHASE_ORDERS, REQUIRED_COLUMNS, UOM_CONVERSIONS
from opsresolve.models import ExceptionType, OpsException, Severity
from opsresolve.pipeline import InvestigationResult, investigate_files
from opsresolve.profiles import ImportProfile, ProfileError, parse_profile
from opsresolve.reporting import PROFILE_HASH_CHARS, exceptions_frame, render_markdown, validation_frame

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"


@dataclass(frozen=True)
class InputSpec:
    key: str  # the ingestion file label, also the fixed internal filename stem
    label: str
    required: bool
    description: str
    optional_columns: tuple[str, ...]
    example_path: Path | None

    @property
    def required_columns(self) -> tuple[str, ...]:
        return REQUIRED_COLUMNS[self.key]

    @property
    def internal_name(self) -> str:
        return f"{self.key}.csv"


INPUTS: tuple[InputSpec, ...] = (
    InputSpec(PURCHASE_ORDERS, "Purchase orders", True,
              "One row per PO line. A SKU may appear only once per PO.",
              ("description", "unit_of_measure"), SAMPLE_DIR / "purchase_orders.csv"),
    InputSpec(INVOICES, "Supplier invoices", True,
              "One row per invoice line, referencing the PO number and SKU it bills.",
              ("unit_of_measure",), SAMPLE_DIR / "invoices.csv"),
    InputSpec(GOODS_RECEIVED, "Goods receipts", True,
              "One row per goods-received (GRN) line, referencing the PO number and SKU.",
              ("unit_of_measure",), SAMPLE_DIR / "goods_received.csv"),
    InputSpec(UOM_CONVERSIONS, "Unit-of-measure conversions", False,
              "Only needed when documents state different units. One row per SKU and unit: "
              "1 uom = units_per_uom base_uom.",
              (), REPO_ROOT / "data" / "evaluation" / "uom_conversions.csv"),
)
INPUTS_BY_KEY = {spec.key: spec for spec in INPUTS}


@dataclass(frozen=True)
class Upload:
    """An uploaded file. ``name`` is for display only and never touches the filesystem."""

    name: str
    data: bytes


class MissingInputError(ValueError):
    """Required files were not provided; the pipeline was not run."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("Missing required file(s): " + ", ".join(missing))


# --------------------------------------------------------------------------- #
# Running the pipeline
# --------------------------------------------------------------------------- #

def run_reconciliation(
    uploads: Mapping[str, Upload | None], config: MatchConfig, profile: ImportProfile | None = None,
) -> InvestigationResult:
    """Run the existing pipeline on uploaded bytes.

    Raises MissingInputError (before touching the filesystem) if a required file
    is absent. Validation problems inside files are not errors here: the pipeline
    reports them as validation issues. ``profile`` (optional, already validated)
    declares the format of every file; without it the built-in format is used.
    """
    missing = [spec.label for spec in INPUTS if spec.required and uploads.get(spec.key) is None]
    if missing:
        raise MissingInputError(missing)

    with tempfile.TemporaryDirectory(prefix="opsresolve-") as tmp:
        folder = Path(tmp)
        paths: dict[str, Path | None] = {}
        for spec in INPUTS:
            upload = uploads.get(spec.key)
            if upload is None:
                paths[spec.key] = None
                continue
            path = folder / spec.internal_name  # fixed name: the upload's own name is never used
            path.write_bytes(upload.data)
            paths[spec.key] = path
        # Without a profile, the pipeline is called exactly as before import profiles existed.
        with_profile = {} if profile is None else {"profile": profile}
        return investigate_files(
            paths[PURCHASE_ORDERS], paths[INVOICES], paths[GOODS_RECEIVED], config,
            uom_conversions_path=paths[UOM_CONVERSIONS], **with_profile,
        )


def sample_uploads() -> dict[str, Upload | None]:
    """The bundled sample data (read only), for a demonstration run."""
    uploads: dict[str, Upload | None] = {}
    for spec in INPUTS:
        path = SAMPLE_DIR / spec.internal_name
        uploads[spec.key] = Upload(f"data/sample/{spec.internal_name}", path.read_bytes()) if path.exists() else None
    return uploads


# --------------------------------------------------------------------------- #
# Rule settings
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SettingSpec:
    field: str  # a MatchConfig field
    label: str
    step: float
    integer: bool = False


SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec("price_tolerance_pct", "Price tolerance (%)", 0.5),
    SettingSpec("price_tolerance_abs", "Price tolerance (absolute)", 0.01),
    SettingSpec("quantity_tolerance", "Quantity tolerance", 1.0),
    SettingSpec("duplicate_window_days", "Duplicate window (days)", 1, integer=True),
    SettingSpec("high_severity_amount", "HIGH severity from amount", 100.0),
    SettingSpec("medium_severity_amount", "MEDIUM severity from amount", 10.0),
)


def setting_decimals(spec: SettingSpec, default) -> int:
    """Decimals shown for a setting: at least 2, or more if the starting value needs them.

    A default from OPSRESOLVE_* such as 0.125 keeps 3 decimals, so the web app uses the
    same value as the CLI instead of rounding it to 0.13.
    """
    if spec.integer:
        return 0
    exponent = Decimal(str(default)).normalize().as_tuple().exponent
    return max(2, -exponent if isinstance(exponent, int) and exponent < 0 else 0)


def normalize_setting(value, decimals: int) -> Decimal:
    """The value as displayed: widget floats such as 0.09999999999999999 become exactly 0.10."""
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def config_from_inputs(values: Mapping[str, float | int], decimals: Mapping[str, int]) -> MatchConfig:
    """Build the engine configuration from widget values at their displayed precision.

    Raises pydantic.ValidationError for values the engine does not accept.
    """
    settings = {}
    for spec in SETTINGS:
        value = values[spec.field]
        settings[spec.field] = int(round(value)) if spec.integer else normalize_setting(value, decimals[spec.field])
    return MatchConfig(**settings)


def fingerprint(
    uploads: Mapping[str, Upload | None], config: MatchConfig, profile_upload: Upload | None = None,
) -> str:
    """Identifies one combination of inputs, settings and import profile; any change gives a new value.

    The profile's bytes count whether or not they are valid. Without a profile the value is
    exactly what it was before import profiles existed.
    """
    digest = hashlib.sha256()
    for spec in INPUTS:
        upload = uploads.get(spec.key)
        digest.update(spec.key.encode())
        digest.update(b"\x00" if upload is None else hashlib.sha256(upload.data).digest())
    digest.update(config.model_dump_json().encode())
    if profile_upload is not None:
        digest.update(b"import_profile")
        digest.update(hashlib.sha256(profile_upload.data).digest())
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Import profiles
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProfileCheck:
    """An uploaded import profile after validation: ``profile`` when valid, otherwise ``problems``."""

    profile: ImportProfile | None
    problems: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.profile is not None


def check_profile(upload: Upload) -> ProfileCheck:
    """Validate an uploaded profile from its bytes, collecting every problem (nothing is written)."""
    try:
        return ProfileCheck(parse_profile(upload.data))
    except ProfileError as exc:
        return ProfileCheck(None, tuple(exc.problems))


def profile_caption(profile: ImportProfile, prefix: str = "Import profile") -> str:
    """'<prefix>: <name> (sha256 <12 hex>)', with the user-supplied name escaped for Markdown."""
    return f"{prefix}: {escape_markdown(profile.name)} (sha256 {profile.sha256[:PROFILE_HASH_CHARS]})"


# --------------------------------------------------------------------------- #
# Summaries (counting pipeline output only)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FileSummary:
    key: str
    label: str
    display_name: str
    rows_read: int
    accepted: int
    rejected: int
    issues: int  # validation issues reported for this file

    @property
    def no_data_rows(self) -> bool:
        """Header only: nothing to load, and nothing was wrong with the file."""
        return self.rows_read == 0 and self.issues == 0


@dataclass(frozen=True)
class RunSummary:
    files: tuple[FileSummary, ...]
    validation_issue_count: int
    exception_count: int
    by_severity: dict[Severity, int]
    by_type: dict[ExceptionType, int]
    status_level: str  # "success" | "warning" | "error"
    status_message: str


def display_names(uploads: Mapping[str, Upload | None]) -> dict[str, str]:
    return {key: upload.name for key, upload in uploads.items() if upload is not None}


def summarize(result: InvestigationResult, names: Mapping[str, str] | None = None) -> RunSummary:
    names = names or {}
    ds = result.dataset
    loads = [ds.purchase_orders, ds.invoices, ds.goods_received]
    if ds.uom_conversions is not None:
        loads.append(ds.uom_conversions)
    issues_per_file = Counter(issue.file for issue in ds.issues)
    files = tuple(
        FileSummary(load.file_label, INPUTS_BY_KEY[load.file_label].label,
                    names.get(load.file_label, load.file_label), load.rows_read, len(load.records),
                    load.rows_rejected, issues_per_file.get(load.file_label, 0))
        for load in loads
    )
    severities = Counter(e.severity for e in result.exceptions)
    types = Counter(e.exception_type for e in result.exceptions)
    issues, exceptions = len(ds.issues), len(result.exceptions)
    # Required files with nothing loaded *because of* validation problems (unreadable file, missing
    # columns, or every row rejected). A header-only file has no issues and is reported separately.
    unloaded = [f.label for f in files[:3] if f.accepted == 0 and f.issues > 0]
    empty = [f.label for f in files if f.no_data_rows]
    empty_note = ""
    if empty:
        empty_note = (f" {', '.join(empty)} contained no data rows (header only), so nothing from "
                      f"{'that file' if len(empty) == 1 else 'those files'} was compared; check that this "
                      f"is expected.")

    if unloaded:
        level = "error"
        message = (f"No valid rows were loaded from: {', '.join(unloaded)}, because of the validation issues "
                   f"below. Fix them and run again; findings from this run are incomplete." + empty_note)
    elif issues:
        level = "warning"
        message = (f"Processing completed with {issues} validation issue(s). Rejected rows were excluded from "
                   f"matching, so correct them before relying on the {exceptions} finding(s) below." + empty_note)
    elif exceptions:
        level = "warning"
        message = f"Processing completed. {exceptions} reconciliation finding(s) need review." + empty_note
    elif empty:
        level = "warning"
        message = "Processing completed with no validation issues and no exceptions." + empty_note
    else:
        level = "success"
        message = ("Processing completed. No validation issues and no exceptions were detected by the current "
                   "rules. This is not an approval: it means the documents agree under these rules.")
    return RunSummary(
        files=files, validation_issue_count=issues, exception_count=exceptions,
        by_severity={s: severities.get(s, 0) for s in Severity},
        by_type={t: types[t] for t in ExceptionType if types.get(t)},
        status_level=level, status_message=message,
    )


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #

def _searchable(exc: OpsException) -> str:
    parts = [exc.exception_id, exc.exception_type.value, exc.severity.value, exc.supplier_id, exc.po_number or "",
             exc.sku or "", exc.summary, exc.expected or "", exc.actual or "", exc.recommended_action,
             *exc.notes, *(f"{e.reference} {e.detail}" for e in exc.evidence)]
    return " ".join(parts).casefold()


def filter_exceptions(
    exceptions: list[OpsException],
    severities: set[Severity] | None = None,
    types: set[ExceptionType] | None = None,
    query: str = "",
) -> list[OpsException]:
    """Filter by severity, type and a case-insensitive text search; order is preserved."""
    needle = query.strip().casefold()
    return [
        e for e in exceptions
        if (not severities or e.severity in severities)
        and (not types or e.exception_type in types)
        and (not needle or needle in _searchable(e))
    ]


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")


def neutralize_cell(value):
    """Make a cell safe to open in a spreadsheet.

    Text starting with a formula trigger gets a leading apostrophe so it is shown
    as text, not evaluated. Plain numbers (e.g. -50.00) and non-text values are
    returned unchanged.
    """
    if not isinstance(value, str) or not value.startswith(_FORMULA_PREFIXES):
        return value
    if _PLAIN_NUMBER.fullmatch(value):
        return value
    return "'" + value


def to_safe_csv(frame: pd.DataFrame) -> bytes:
    safe = frame.map(neutralize_cell)
    buffer = io.StringIO()
    safe.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8")


def exceptions_csv(result: InvestigationResult) -> bytes:
    return to_safe_csv(exceptions_frame(result))


def validation_csv(result: InvestigationResult) -> bytes:
    return to_safe_csv(validation_frame(result))


def summary_markdown(result: InvestigationResult) -> bytes:
    return render_markdown(result).encode("utf-8")


def template_csv(key: str) -> bytes:
    """Header with every supported column, plus up to three example rows from the bundled data."""
    spec = INPUTS_BY_KEY[key]
    columns = list(spec.required_columns) + list(spec.optional_columns)
    rows = pd.DataFrame(columns=columns)
    if spec.example_path is not None and spec.example_path.exists():
        example = pd.read_csv(spec.example_path, dtype=str, keep_default_na=False).head(3)
        rows = example.reindex(columns=columns, fill_value="")
    return to_safe_csv(rows)


# --------------------------------------------------------------------------- #
# Display safety
# --------------------------------------------------------------------------- #

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~:$^=&])")


def escape_markdown(text: str | None) -> str:
    """Show text from uploaded data literally in Markdown (no links, images, HTML or styling)."""
    if not text:
        return ""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text).replace("\n", " ")
