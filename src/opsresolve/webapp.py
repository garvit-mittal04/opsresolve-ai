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
* Optional AI explanations (``opsresolve.ai``) are prepared here for display.
  Text the model wrote is returned as literal text (carriage returns and
  invisible format characters replaced by spaces), not as Markdown: the page
  must show it with ``st.text`` or in table cells, never with ``st.markdown`` or
  ``unsafe_allow_html``, and must show the request preview with ``st.code``.
  Rejected answers expose problem codes and allow-listed field locations only
  (never ``Problem.message``, which can quote the answer); failures carry a
  known error code and a fixed message, never an error's own text. Nothing
  here sends a request unless ``request_ai_explanation`` is called.
* Investigation checks (``opsresolve.investigation``) are local and deterministic;
  they are computed once per run (``cached_investigations``) and prepared here
  as literal text for tables and ``st.text``. A failed check is shown with its
  fixed label only, and an error while preparing the checks gives a fixed
  message; error types and messages are never passed to the page.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import threading
import typing
import unicodedata
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable, Hashable, Mapping, TypeVar

import pandas as pd
from pydantic import ValidationError

from opsresolve import ai, investigation
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


# --------------------------------------------------------------------------- #
# Optional AI explanations (helpers only; the page does not use them yet)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AIStatus:
    """Whether an explanation can be requested. ``message`` is fixed text; ``config`` is set only when ready."""

    ready: bool
    code: str  # "ready", "sdk_missing", "key_missing" or "invalid_settings"
    message: str
    config: ai.AIConfig | None = None


AI_STATUS_MESSAGES = {
    "ready": "AI explanations are available. A request is sent only when you ask for one.",
    "sdk_missing": "AI explanations need the optional Anthropic SDK (this project's ai extra).",
    "key_missing": "AI explanations need the ANTHROPIC_API_KEY environment variable; set it and restart the app.",
}

# Fixed text for every error code of opsresolve.ai; an error's own message is never shown.
AI_ERROR_MESSAGES = {
    "not_configured": "AI explanations are not configured (Anthropic SDK, ANTHROPIC_API_KEY or OPSRESOLVE_AI_* "
                      "settings). Nothing was sent.",
    "privacy_guard": "Supplier details could not be removed from the evidence safely, so nothing was sent.",
    "authentication": "The API key was rejected or lacks permission.",
    "rate_limited": "The API rate limit was reached. Try again later.",
    "timeout": "The request timed out. No explanation is shown.",
    "connection": "The API could not be reached. No explanation is shown.",
    "provider_error": "The AI service returned an error. No explanation is shown.",
    "refused": "The model declined to explain this exception.",
    "malformed": "The AI answer was incomplete or unreadable, so it is not shown.",
}
AI_UNEXPECTED_MESSAGE = "The AI explanation could not be prepared because of an unexpected error. Nothing is shown."
AI_ACCEPTED_LABEL = ("AI-generated explanation (advisory). It was checked against this exception's evidence, which "
                     "does not make it true: review it against the source documents before acting. The figures and "
                     "decisions above are the engine's and are unchanged.")
AI_REJECTED_MESSAGE = "The AI answer did not pass the checks against the evidence, so it is not shown."
AI_REQUEST_IN_PROGRESS = "A request for this finding is already in progress; wait for it to finish."
AI_SUFFICIENCY = {"sufficient": "The model judged the evidence sufficient.",
                  "partial": "The model judged the evidence partial.",
                  "insufficient": "The model judged the evidence insufficient."}


# Interactive defaults for the web page: a person waits for the answer, so a request is given up after two
# minutes and not retried (the user can ask again). They apply only where the variable is unset or blank, exactly
# as AIConfig.from_env treats it; an explicit value always wins. Other callers keep AIConfig's own defaults.
INTERACTIVE_AI_DEFAULTS = {"OPSRESOLVE_AI_TIMEOUT_SECONDS": "120", "OPSRESOLVE_AI_MAX_RETRIES": "0"}


def interactive_ai_config(environ: Mapping[str, str] | None = None) -> ai.AIConfig:
    """The settings for a request made from the web page: ``AIConfig.from_env`` with ``INTERACTIVE_AI_DEFAULTS``
    filled in for unset variables. Validated like any other setting (raises ``ValidationError``)."""
    environ = os.environ if environ is None else environ
    merged = dict(environ)
    for name, value in INTERACTIVE_AI_DEFAULTS.items():
        if not merged.get(name, "").strip():
            merged[name] = value
    return ai.AIConfig.from_env(merged)


def ai_status(environ: Mapping[str, str] | None = None) -> AIStatus:
    """Readiness for AI explanations, without importing the SDK or sending anything. Never shows the key.
    When ready, ``config`` holds the interactive settings (see ``interactive_ai_config``)."""
    if not ai.sdk_available():
        return AIStatus(False, "sdk_missing", AI_STATUS_MESSAGES["sdk_missing"])
    if not ai.api_key_configured(environ):
        return AIStatus(False, "key_missing", AI_STATUS_MESSAGES["key_missing"])
    try:
        config = interactive_ai_config(environ)
    except ValidationError as exc:  # the count only: a value could be sensitive
        return AIStatus(False, "invalid_settings",
                        f"The OPSRESOLVE_AI_* settings are invalid ({exc.error_count()} problem(s)); fix them and "
                        f"restart the app.")
    return AIStatus(True, "ready", AI_STATUS_MESSAGES["ready"], config)


@dataclass(frozen=True)
class AIPreview:
    """The exact request body (JSON) that would be sent, or a fixed message. Show ``body`` as literal code text."""

    body: str | None
    error_code: str | None = None
    message: str | None = None


def _error_code(code: str | None) -> str:
    """A known error code (one with a fixed message), or "unexpected"; any other value is never passed on."""
    return code if isinstance(code, str) and code in AI_ERROR_MESSAGES else "unexpected"


def preview_ai_request(result: InvestigationResult, exception: OpsException, config: ai.AIConfig) -> AIPreview:
    """What ``request_ai_explanation`` would send for this exception. Sends nothing."""
    try:
        return AIPreview(ai.preview_request(result, exception, config))
    except ai.ExplanationError as exc:
        code = _error_code(exc.code)
        return AIPreview(None, code, AI_ERROR_MESSAGES.get(code, AI_UNEXPECTED_MESSAGE))
    except Exception:  # never show an unexpected error's text
        return AIPreview(None, "unexpected", AI_UNEXPECTED_MESSAGE)


def literal_text(text: str) -> str:
    """Model-written text made safe to show literally: carriage returns and invisible format characters
    (Unicode category Cf, e.g. bidi controls that reorder what is displayed) become spaces. It is not escaped:
    the page shows it with ``st.text`` or in a table cell, where nothing is interpreted as Markdown or HTML."""
    return "".join(" " if ch == "\r" or unicodedata.category(ch) == "Cf" else ch for ch in text)


@dataclass(frozen=True)
class AIItem:
    """One line of an accepted explanation (literal text) with the evidence references it cites."""

    text: str
    refs: tuple[str, ...] = ()
    check: str = ""  # how to check a possible cause (literal text)


@dataclass(frozen=True)
class AIExplanationView:
    """What the page may show for one request. Model-written strings are literal text (see ``literal_text``):
    show them with ``st.text`` or in table cells, never with ``st.markdown`` or ``unsafe_allow_html``.

    ``status`` is "accepted", "rejected", "unavailable" or "error". Only "accepted" carries content; "rejected"
    carries (code, location) pairs with allow-listed locations; the others carry a known ``error_code`` and a
    fixed ``message``.
    """

    status: str
    message: str
    explanation: str = ""
    confirmed_facts: tuple[AIItem, ...] = ()
    possible_causes: tuple[AIItem, ...] = ()
    investigation_steps: tuple[AIItem, ...] = ()
    sufficiency: str = ""
    missing_information: tuple[str, ...] = ()
    problems: tuple[tuple[str, str], ...] = ()
    error_code: str | None = None


def _refs(refs) -> tuple[str, ...]:
    return tuple(literal_text(ref) for ref in refs)  # already checked to be E1, E2, ...


def _schema_paths(model, prefix: str = "") -> set[str]:
    """Every field path of the answer contract, with "[]" where a list index goes (derived from the model)."""
    paths = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        paths.add(path)
        if typing.get_origin(field.annotation) is tuple:  # a list in the answer: items are path[i]
            paths.add(f"{path}[]")
            item = typing.get_args(field.annotation)[0]
            if isinstance(item, type) and hasattr(item, "model_fields"):
                paths |= _schema_paths(item, f"{path}[]")
    return paths


# A validator location is a field path such as "confirmed_facts[0].evidence_refs[1]". A schema error can name a
# field the model invented, so only paths of the answer contract (and "(root)") are shown; anything else is
# shown as a placeholder, never as written.
_ALLOWED_LOCATIONS = frozenset(_schema_paths(ai.ExplanationOutput)) | {"(root)"}
_PATH_SYNTAX = re.compile(r"[a-z_]+(\[\d{1,3}\])?(\.[a-z_]+(\[\d{1,3}\])?)*")
UNEXPECTED_LOCATION = "(unexpected field)"


def _location(location: str) -> str:
    if location == "(root)":
        return location
    if not isinstance(location, str) or not _PATH_SYNTAX.fullmatch(location):
        return UNEXPECTED_LOCATION
    return location if re.sub(r"\[\d+\]", "[]", location) in _ALLOWED_LOCATIONS else UNEXPECTED_LOCATION


def explanation_view(outcome: ai.ExplanationResult) -> AIExplanationView:
    """The display form of an ``ExplanationResult``: literal content, codes and fixed messages only."""
    if outcome.status == "accepted" and outcome.explanation is not None:
        answer = outcome.explanation
        return AIExplanationView(
            "accepted", AI_ACCEPTED_LABEL, explanation=literal_text(answer.explanation),
            confirmed_facts=tuple(AIItem(literal_text(f.statement), _refs(f.evidence_refs))
                                  for f in answer.confirmed_facts),
            possible_causes=tuple(AIItem(literal_text(c.hypothesis), _refs(c.supporting_refs),
                                         literal_text(c.how_to_check)) for c in answer.possible_causes),
            investigation_steps=tuple(AIItem(literal_text(s.step), _refs(s.evidence_refs))
                                      for s in answer.investigation_steps),
            sufficiency=AI_SUFFICIENCY.get(answer.sufficiency, ""),
            missing_information=tuple(literal_text(item) for item in answer.missing_information))
    if outcome.status == "rejected":  # Problem.message can quote the answer: codes and locations only
        return AIExplanationView("rejected", AI_REJECTED_MESSAGE, problems=tuple(
            (literal_text(p.code), _location(p.location)) for p in outcome.problems))
    status = "unavailable" if outcome.status == "not_configured" else "error"
    code = _error_code(outcome.error_code)
    return AIExplanationView(status, AI_ERROR_MESSAGES.get(code, AI_UNEXPECTED_MESSAGE), error_code=code)


def request_ai_explanation(result: InvestigationResult, exception: OpsException, config: ai.AIConfig, *,
                           provider: ai.ExplanationProvider | None = None,
                           environ: Mapping[str, str] | None = None) -> AIExplanationView:
    """Ask for an explanation of one exception and return what may be shown. Sends one request.

    ``provider`` defaults to the Anthropic provider (tests pass a fake). The engine's result is not changed.
    """
    try:
        outcome = ai.explain_exception(result, exception, provider, config=config, environ=environ)
    except Exception:  # never show an unexpected error's text
        return AIExplanationView("error", AI_UNEXPECTED_MESSAGE, error_code="unexpected")
    return explanation_view(outcome)


_T = TypeVar("_T")


class AIRequestGuard:
    """At most one AI request per key (a run and a finding) at a time. Thread-safe; one per browser session.

    A send is first marked *pending* (so the page can show the button disabled), then started with ``run``,
    which refuses a key that is already running. Pending and running marks are cleared when the request
    ends, however it ends (answer, rejection, timeout or exception).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: set[Hashable] = set()
        self._running: set[Hashable] = set()

    def is_pending(self, key: Hashable) -> bool:
        """Whether a request for ``key`` is waiting to start or running."""
        with self._lock:
            return key in self._pending or key in self._running

    def is_running(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._running

    def mark_pending(self, key: Hashable) -> bool:
        """Mark a send for ``key``; False (and nothing changes) if one is already pending or running."""
        with self._lock:
            if key in self._pending or key in self._running:
                return False
            self._pending.add(key)
            return True

    def cancel_waiting(self, keep: Hashable) -> None:
        """Drop sends that have not started for any key but ``keep`` (the finding now shown), so a request
        is never sent later for a finding the user has left. Running requests are not affected."""
        with self._lock:
            self._pending = {key for key in self._pending if key == keep or key in self._running}

    def run(self, key: Hashable, request: Callable[[], _T]) -> _T | None:
        """Run ``request`` for ``key`` unless one is already running (then None and nothing is sent)."""
        with self._lock:
            if key in self._running:
                return None
            self._running.add(key)
        try:
            return request()
        finally:
            with self._lock:
                self._running.discard(key)
                self._pending.discard(key)


# --------------------------------------------------------------------------- #
# Investigation checks (deterministic, local; display helpers only)
# --------------------------------------------------------------------------- #

INVESTIGATION_UNAVAILABLE = ("The investigation checks could not be prepared for this run, so none are shown. The "
                             "findings, amounts and downloads are not affected.")
INVESTIGATION_NOT_RUN = "The checks for this finding could not be run — nothing concluded."
INVESTIGATION_NONE = "No investigation checks apply to this type of finding."
CHECK_FAILED_LABEL = investigation.STATUS_LABELS[investigation.CheckStatus.FAILED]


@dataclass(frozen=True)
class CheckView:
    """One check as the page may show it. Text is literal (see ``literal_text``): show it with ``st.text`` or in
    table cells. A failed check carries its fixed label and statement only, never details of the failure."""

    id: str
    statement: str
    status: str  # a CheckStatus value
    label: str  # the fixed status label
    failed: bool
    observations: tuple[tuple[str, str], ...] = ()  # (text, cited references such as "R1, R2")
    missing: tuple[str, ...] = ()
    how_to_confirm: str = ""


@dataclass(frozen=True)
class RecordView:
    ref: str
    file: str
    line: int
    reference: str


@dataclass(frozen=True)
class InvestigationView:
    """The checks of one finding. ``not_run``: the checks could not be prepared (only a fixed message is shown)."""

    exception_id: str
    checks: tuple[CheckView, ...] = ()
    records: tuple[RecordView, ...] = ()
    not_run: bool = False


@dataclass(frozen=True)
class InvestigationRun:
    """The checks of every finding of one run, computed once. ``message`` is set (fixed text) when they could
    not be prepared; ``failed_checks`` counts contained failures, for diagnostics (no error details)."""

    fingerprint: str
    result: InvestigationResult = dataclass_field(repr=False, compare=False)
    views: Mapping[str, InvestigationView] = dataclass_field(default_factory=dict)
    message: str | None = None
    failed_checks: int = 0


def investigation_view(found: investigation.Investigation) -> InvestigationView:
    """The display form of one ``Investigation``. ``failures`` (error class names) are never carried over."""
    if any(failure.stage == "subject" for failure in found.failures):
        return InvestigationView(found.exception_id, not_run=True)
    checks = []
    for check in found.checks:
        if check.status is investigation.CheckStatus.FAILED:
            checks.append(CheckView(check.id, literal_text(check.statement), check.status.value, CHECK_FAILED_LABEL,
                                    True))
            continue
        checks.append(CheckView(
            check.id, literal_text(check.statement), check.status.value, check.status_label, False,
            tuple((literal_text(o.text), ", ".join(o.refs)) for o in check.observations),
            tuple(literal_text(text) for text in check.missing), literal_text(check.how_to_confirm)))
    records = tuple(RecordView(r.ref, r.source, r.source_row, literal_text(r.reference)) for r in found.related)
    return InvestigationView(found.exception_id, tuple(checks), records)


def investigate_run(result: InvestigationResult, run_fingerprint: str) -> InvestigationRun:
    """Run the investigation checks for every finding of ``result`` (local and deterministic; no AI, no network).
    Any error while preparing them gives a fixed message, never the error's text; the result is not changed."""
    try:
        found = investigation.investigate_all(result)
        views = {exception_id: investigation_view(item) for exception_id, item in found.items()}
    except Exception:  # never show an unexpected error's text; the findings themselves are unaffected
        return InvestigationRun(run_fingerprint, result, {}, INVESTIGATION_UNAVAILABLE)
    failed = sum(1 for item in found.values() for f in item.failures)
    return InvestigationRun(run_fingerprint, result, views, None, failed)


def cached_investigations(stored: dict) -> InvestigationRun:
    """The investigation checks of a stored run (``{"fingerprint", "result"}``), computed on first use and kept in
    ``stored``. They are recomputed whenever the stored fingerprint or result is not the one they were made from."""
    run = stored.get("investigations")
    if not isinstance(run, InvestigationRun) or run.fingerprint != stored["fingerprint"] \
            or run.result is not stored["result"]:
        run = investigate_run(stored["result"], stored["fingerprint"])
        stored["investigations"] = run
    return run
