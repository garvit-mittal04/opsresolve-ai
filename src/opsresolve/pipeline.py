"""Orchestrates ingestion -> matching -> unit normalisation -> detection. No I/O formatting here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from opsresolve.config import MatchConfig
from opsresolve.detection import RejectedKeys, detect_exceptions
from opsresolve.ingestion import Dataset, load_dataset
from opsresolve.matching import MatchResult, match_records
from opsresolve.models import OpsException
from opsresolve.profiles import ImportProfile
from opsresolve.uom import UnitNormalization, normalize_units


@dataclass
class InvestigationResult:
    dataset: Dataset
    match: MatchResult  # after unit normalisation (identical to the raw match when no units are stated)
    exceptions: list[OpsException]
    config: MatchConfig
    generated_at: datetime
    units: UnitNormalization | None = None

    @property
    def profile(self) -> ImportProfile | None:
        """The import profile the input files were read with (None: built-in format)."""
        return self.dataset.profile


def investigate(dataset: Dataset, config: MatchConfig | None = None) -> InvestigationResult:
    config = config or MatchConfig()
    match = match_records(
        dataset.purchase_orders.records, dataset.invoices.records, dataset.goods_received.records)
    rejected = RejectedKeys(
        purchase_orders=dataset.purchase_orders.rejected_keys,
        invoices=dataset.invoices.rejected_keys,
        goods_received=dataset.goods_received.rejected_keys,
    )
    conversions = dataset.uom_conversions.records if dataset.uom_conversions is not None else []
    units = normalize_units(match, conversions)
    exceptions = detect_exceptions(units.match, dataset.invoices.records, config, rejected, units.unresolved)
    return InvestigationResult(dataset, units.match, exceptions, config, datetime.now(timezone.utc), units)


def investigate_files(
    po_path: str | Path,
    invoice_path: str | Path,
    grn_path: str | Path,
    config: MatchConfig | None = None,
    uom_conversions_path: str | Path | None = None,
    profile: ImportProfile | None = None,
) -> InvestigationResult:
    """Load and investigate the files; ``profile`` (optional) declares their CSV format."""
    return investigate(load_dataset(po_path, invoice_path, grn_path, uom_conversions_path, profile), config)
